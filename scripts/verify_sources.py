#!/usr/bin/env python3
"""Reconcile converted row counts against each source's own published counts.

Two separate questions, kept separate on purpose:

  1. Did conversion read every row that exists in the file(s) we pointed it
     at? Checked against an INDEPENDENT count of the same source (MOPD/RL's
     own decontamination summary, or a direct pyarrow read of the SFT arrow
     files) -- a mismatch here is a real bug in this pipeline.

  2. Does the source's own manifest agree with what's actually in its own
     materialized data? This is upstream of us -- reported as a warning,
     never as a failure, and never blocks anything.

Needs pyarrow for the SFT check, which is not in the system Python on this
cluster. Run with the readers module's own venv:

    modelsafety/readers/.venv/bin/python scripts/verify_sources.py

Usage:
    scripts/verify_sources.py [--output-root PATH] [--sft-mix-path PATH]
                               [--mopd-mix-path PATH] [--rl-mix-path PATH]

Exit code is 0 if every independent-count check matches, 2 otherwise. The
upstream manifest-vs-materialized-data warning never affects the exit code.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _arrow_row_count(dataset_dir: Path) -> int:
    """Independent row count for a ``Dataset.save_to_disk()`` folder.

    Deliberately standalone from modelsafety/readers/prepare_rows.py's
    identical helper -- this script is meant to be run on its own, and
    duplicating ~15 lines is simpler than wiring up a cross-module import
    for a read-only verification tool.
    """
    import pyarrow as pa

    state = _load(dataset_dir / "state.json")
    total = 0
    for entry in state["_data_files"]:
        file_path = dataset_dir / entry["filename"]
        with pa.memory_map(str(file_path), "rb") as source:
            try:
                reader = pa.ipc.open_stream(source)
            except pa.ArrowInvalid:
                source.seek(0)
                reader = pa.ipc.open_file(source)
            total += reader.read_all().num_rows
    return total


def check_decontaminated_mix(
    name: str, mix_path: Path, output_root: Path
) -> list[dict[str, Any]]:
    """MOPD/RL: compare converted rows against the mix's own recorded
    ``rows_out`` per split -- an independent count the tool team already
    computed and published, not something we have to derive ourselves."""
    summary = _load(mix_path / "summary.json")
    per_split = {
        entry["split"]: entry["rows_out"]
        for entry in summary["decontamination"]["per_split"]
    }
    rows = []
    for split, expected in sorted(per_split.items()):
        converted_summary_path = output_root / name / split / "summary.json"
        if not converted_summary_path.is_file():
            rows.append(
                {
                    "dataset": f"{name}/{split}",
                    "expected": expected,
                    "converted": None,
                    "match": False,
                    "note": f"missing {converted_summary_path}",
                }
            )
            continue
        converted = _load(converted_summary_path)["rows"]["written"]
        rows.append(
            {
                "dataset": f"{name}/{split}",
                "expected": expected,
                "converted": converted,
                "match": converted == expected,
                "note": "rows_out from the mix's own summary.json",
            }
        )
    return rows


def check_sft(
    sft_mix_path: Path, output_root: Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """SFT: compare converted rows per dataset against an independent arrow
    count, then separately compare that arrow total against the mix's own
    claimed total -- kept apart so an upstream discrepancy in the second
    comparison is never mistaken for a conversion bug in the first."""
    shards_dir = sft_mix_path / "train_shards_keep"
    rows: list[dict[str, Any]] = []
    arrow_total = 0
    for dataset_dir in sorted(shards_dir.iterdir()):
        if not (dataset_dir / "state.json").is_file():
            continue
        name = dataset_dir.name
        try:
            arrow_rows = _arrow_row_count(dataset_dir)
        except Exception as exc:  # pragma: no cover -- surfaced in the table, not raised
            rows.append(
                {
                    "dataset": f"sft_mix/{name}",
                    "expected": None,
                    "converted": None,
                    "match": False,
                    "note": f"arrow read failed: {exc}",
                }
            )
            continue

        arrow_total += arrow_rows
        converted_summary_path = output_root / "sft_mix" / name / "summary.json"
        if not converted_summary_path.is_file():
            rows.append(
                {
                    "dataset": f"sft_mix/{name}",
                    "expected": arrow_rows,
                    "converted": None,
                    "match": False,
                    "note": f"missing {converted_summary_path}",
                }
            )
            continue
        converted = _load(converted_summary_path)["rows"]["written"]
        rows.append(
            {
                "dataset": f"sft_mix/{name}",
                "expected": arrow_rows,
                "converted": converted,
                "match": converted == arrow_rows,
                "note": "independent pyarrow count",
            }
        )

    manifest_claim = _load(sft_mix_path / "train_shards.json").get("n_rows")
    upstream = {
        "arrow_total": arrow_total,
        "manifest_n_rows": manifest_claim,
        "gap": (manifest_claim - arrow_total) if manifest_claim is not None else None,
    }
    return rows, upstream


def print_table(rows: list[dict[str, Any]]) -> None:
    width = max((len(r["dataset"]) for r in rows), default=8)
    print(f"{'dataset':<{width}}  {'expected':>10}  {'converted':>10}  status")
    print(f"{'-' * width}  {'-' * 10}  {'-' * 10}  {'-' * 8}")
    for r in rows:
        expected = r["expected"] if r["expected"] is not None else "-"
        converted = r["converted"] if r["converted"] is not None else "-"
        status = "OK" if r["match"] else "MISMATCH"
        print(
            f"{r['dataset']:<{width}}  {str(expected):>10}  {str(converted):>10}  "
            f"{status:<8}  {r['note']}"
        )


def parse_args() -> argparse.Namespace:
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root", type=Path, default=repo / "output" / "tool_team_converted"
    )
    parser.add_argument(
        "--sft-mix-path",
        type=Path,
        default=Path("/home/shared/agentic_slm/data/sft/processed/qwen3_5/s1_2_0824"),
    )
    parser.add_argument(
        "--mopd-mix-path",
        type=Path,
        default=Path(
            "/home/shared/agentic_slm/data/rl/processed/qwen35-xml/decontaminated/mopd_mixv4_0907"
        ),
    )
    parser.add_argument(
        "--rl-mix-path",
        type=Path,
        default=Path(
            "/home/shared/agentic_slm/data/rl/processed/qwen35-xml/decontaminated/rl_mix_v12_ifmt_0903"
        ),
    )
    return parser.parse_args()


def main() -> int:
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        print(
            "error: pyarrow is required for the SFT arrow check.\n"
            "Run this script with the readers module's own venv:\n"
            "  modelsafety/readers/.venv/bin/python scripts/verify_sources.py",
            file=sys.stderr,
        )
        return 2

    args = parse_args()
    output_root = args.output_root.expanduser().resolve()

    all_rows: list[dict[str, Any]] = []
    all_rows += check_decontaminated_mix("mopd_mix", args.mopd_mix_path, output_root)
    all_rows += check_decontaminated_mix("rl_mix", args.rl_mix_path, output_root)
    sft_rows, upstream = check_sft(args.sft_mix_path, output_root)
    all_rows += sft_rows

    print("=== Conversion vs. independent source counts ===")
    print_table(all_rows)

    mismatches = [r for r in all_rows if not r["match"]]
    print()
    print(f"{len(all_rows) - len(mismatches)}/{len(all_rows)} datasets match their independent count.")

    print()
    print("=== Upstream check (informational -- not a conversion bug either way) ===")
    print(f"SFT: sum of independent per-dataset arrow counts = {upstream['arrow_total']:,}")
    if upstream["manifest_n_rows"] is not None:
        print(f"SFT: train_shards.json claims n_rows            = {upstream['manifest_n_rows']:,}")
    if upstream["gap"]:
        print(
            f"NOTE: {upstream['gap']:,}-row gap between the tool team's manifest and "
            "their own materialized data. Not something this pipeline can fix or "
            "caused -- worth asking the tool team about, but it does not affect "
            "the pass/fail result above."
        )
    elif upstream["gap"] == 0:
        print("No gap.")

    if mismatches:
        print(
            f"\n{len(mismatches)} mismatch(es) against an independent count -- see table above.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
