#!/usr/bin/env python3
"""Recover a per-shard detoxify_summary.json for a toxicity run that
finished before run.py could write one natively.

Reads what's already on disk for a completed run -- the shard's own SLURM
stderr log (for the final "Detoxify candidates: N" progress count), its
manifest file list (to independently recompute rows read, the same way the
NER manifest check does), flagged.jsonl (for messages flagged), and
guard_results/summary.json (for the guard error/total counts, if pass 2 also
ran) -- and writes one detoxify_summary.json per shard, in the same shape
run.py's own --summary-file writes natively, so downstream checks (e.g.
modelsafety/results/coverage.py) can read either without caring which.

This is a one-time recovery tool, not something a normal run needs: any run
started after run.py's native --summary-file support exists writes its own
summary directly and never needs this script.

Usage:
    scripts/recover_toxicity_summary.py \\
        --run-dir output/tool_team_toxicity_run \\
        --job-id 413334 \\
        --shards 8
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MESSAGE_COUNT_RE = re.compile(r"Detoxify candidates:\s*([0-9]+)message")


def shard_dir(run_dir: Path, shard: int) -> Path:
    return run_dir / "scans" / "toxicity" / "shards" / f"shard_{shard:05d}"


def recovered_messages_scored(log_path: Path) -> int:
    """The last "Detoxify candidates: N" progress count in the log is the
    final total -- tqdm rewrites the same line as it goes, so only the last
    match (by position in the file, not by value) is meaningful."""
    if not log_path.is_file():
        raise FileNotFoundError(f"Missing shard log: {log_path}")
    text = log_path.read_text(encoding="utf-8", errors="replace")
    matches = MESSAGE_COUNT_RE.findall(text)
    if not matches:
        raise ValueError(f"No 'Detoxify candidates:' progress line found in {log_path}")
    return int(matches[-1])


def recovered_rows_read(run_dir: Path, input_root: Path, shard: int) -> int:
    """Independently recompute rows read for this shard from its own
    manifest file list -- not from anything the scan itself reported."""
    manifest_path = run_dir / "manifests" / "shards" / f"shard_{shard:05d}.txt"
    total = 0
    for relative in manifest_path.read_text(encoding="utf-8").split():
        source = input_root / relative
        with source.open("rb") as handle:
            total += sum(1 for _ in handle)
    return total


def recovered_messages_flagged(shard_path: Path) -> int:
    flagged_file = shard_path / "flagged.jsonl"
    if not flagged_file.is_file():
        return 0
    with flagged_file.open("r", encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def recovered_guard_stats(shard_path: Path) -> tuple[int | None, int | None]:
    summary_path = shard_path / "guard_results" / "summary.json"
    if not summary_path.is_file():
        return None, None
    guard_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    return guard_summary.get("total_records"), guard_summary.get("errors")


def recover_one_shard(
    run_dir: Path,
    input_root: Path,
    job_id: str,
    stage_label: str,
    shard: int,
) -> dict[str, Any]:
    log_path = run_dir.parents[1] / "logs" / "slurm" / f"tool_team_toxicity_{stage_label}_{job_id}_{shard}.err"
    path = shard_dir(run_dir, shard)

    messages_scored = recovered_messages_scored(log_path)
    rows_read = recovered_rows_read(run_dir, input_root, shard)
    messages_flagged = recovered_messages_flagged(path)
    guard_total, guard_errors = recovered_guard_stats(path)

    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "recovered_from_logs",
        "recovered_from": str(log_path),
        "model_name": "multilingual",
        "threshold": 0.5,
        "rows_read": rows_read,
        "messages_scored": messages_scored,
        "messages_flagged": messages_flagged,
        "histogram": None,
        "per_file": None,
        "guard_total_records": guard_total,
        "guard_errors": guard_errors,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--input-root",
        type=Path,
        help="Defaults to the input_root recorded in <run-dir>/manifests/manifest.json",
    )
    parser.add_argument("--job-id", required=True, help="The array job id the logs are named with")
    parser.add_argument(
        "--stage-label",
        default="all",
        help="The stage word used in the log filenames, e.g. tool_team_toxicity_<label>_<job>_<shard>.err",
    )
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true", help="Print what would be written, write nothing")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.expanduser().resolve()

    if args.input_root is not None:
        input_root = args.input_root.expanduser().resolve()
    else:
        manifest = json.loads((run_dir / "manifests" / "manifest.json").read_text(encoding="utf-8"))
        input_root = Path(manifest["input_root"])

    written = []
    for shard in range(args.shards):
        summary = recover_one_shard(run_dir, input_root, args.job_id, args.stage_label, shard)
        target = shard_dir(run_dir, shard) / "detoxify_summary.json"
        print(
            f"shard {shard}: rows_read={summary['rows_read']:,} "
            f"messages_scored={summary['messages_scored']:,} "
            f"messages_flagged={summary['messages_flagged']:,} "
            f"guard_errors={summary['guard_errors']} -> {target}"
        )
        if not args.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f".{target.name}.partial")
            temp.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp.replace(target)
        written.append(summary)

    total_rows = sum(s["rows_read"] for s in written)
    total_messages = sum(s["messages_scored"] for s in written)
    total_flagged = sum(s["messages_flagged"] for s in written)
    total_errors = sum(s["guard_errors"] or 0 for s in written)
    print(
        f"\n{len(written)} shard(s): {total_rows:,} rows, {total_messages:,} messages scored, "
        f"{total_flagged:,} flagged, {total_errors} guard error(s)"
    )
    if args.dry_run:
        print("(dry run -- nothing written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
