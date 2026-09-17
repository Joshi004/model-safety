#!/usr/bin/env python3
"""Join PII/toxicity decisions and create byte-preserved dataset partitions."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from modelsafety.contract.textract import load_source_snapshot, verify_source_snapshot


Location = tuple[str, int]
PiiCandidateKey = tuple[str, int, str, str, str]
EXCLUDED_PII_CATEGORIES = {"zip_codes", "postcodes"}


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc


def compact_pii(record: dict[str, Any]) -> dict[str, Any]:
    decision = record.get("decision") or {}
    return {
        "category": record.get("category"),
        "candidate": record.get("PII"),
        "source_sha256": record.get("source_sha256"),
        "status": record.get("status"),
        "decision": decision.get("decision"),
        "confidence": decision.get("confidence"),
        "reason": decision.get("reason"),
        "evidence": decision.get("evidence"),
        "error": record.get("error"),
    }


def load_pii_decisions(
    path: Path,
) -> tuple[
    dict[Location, list[dict[str, Any]]],
    dict[Location, set[str]],
    set[PiiCandidateKey],
    Counter,
]:
    rejected: dict[Location, list[dict[str, Any]]] = defaultdict(list)
    source_hashes: dict[Location, set[str]] = defaultdict(set)
    candidate_keys: set[PiiCandidateKey] = set()
    counts: Counter = Counter()
    for record in iter_jsonl(path):
        counts["candidates"] += 1
        category = str(record.get("category") or "")
        if category in EXCLUDED_PII_CATEGORIES:
            raise ValueError(
                f"Excluded PII category {category!r} found in Stage-4 results"
            )
        source_sha256 = str(record.get("source_sha256") or "")
        if len(source_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in source_sha256
        ):
            raise ValueError(f"PII decision lacks a valid source_sha256: {record}")
        try:
            location = (str(record["file"]), int(record["line"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"PII decision lacks a valid source location: {record}") from exc
        key = (
            location[0],
            location[1],
            source_sha256,
            category,
            str(record.get("PII") or ""),
        )
        if key in candidate_keys:
            raise ValueError(f"Duplicate PII decision: {key}")
        candidate_keys.add(key)
        source_hashes[location].add(source_sha256)
        status = str(record.get("status") or "")
        decision = (record.get("decision") or {}).get("decision")
        if status == "error":
            counts["errors"] += 1
        elif status != "ok":
            raise ValueError(f"Unknown PII validation status {status!r}: {record}")
        elif decision == "keep":
            counts["confirmed"] += 1
            rejected[location].append(compact_pii(record))
        elif decision == "filter":
            counts["filtered_candidates"] += 1
        else:
            raise ValueError(f"Unknown PII decision {decision!r}: {record}")
    return rejected, source_hashes, candidate_keys, counts


def load_stage3_candidate_keys(path: Path) -> set[PiiCandidateKey]:
    csv.field_size_limit(sys.maxsize)
    required = {"file", "line", "source_sha256", "category", "PII"}
    keys: set[PiiCandidateKey] = set()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(f"Invalid Stage-3 PII CSV schema: {path}")
        for row_number, row in enumerate(reader, 2):
            try:
                key = (
                    str(row["file"]),
                    int(row["line"]),
                    str(row["source_sha256"]),
                    str(row["category"]),
                    str(row["PII"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Invalid Stage-3 PII row at {path}:{row_number}") from exc
            if key in keys:
                raise ValueError(f"Duplicate Stage-3 PII row at {path}:{row_number}")
            keys.add(key)
    return keys


def compact_toxicity(record: dict[str, Any], outcome: str) -> dict[str, Any]:
    guard = record.get("guard") or {}
    return {
        "message_index": record.get("message_index"),
        "role": record.get("role"),
        "detoxify_score": record.get("toxicity"),
        "detoxify_category": record.get("trigger_category"),
        "guard_outcome": outcome,
        "guard_categories": guard.get("categories") or [],
        "guard_error": guard.get("error"),
        "guard_raw": guard.get("raw"),
    }


def load_toxicity_decisions(path: Path) -> tuple[dict[Location, list[dict[str, Any]]], Counter]:
    rejected: dict[Location, list[dict[str, Any]]] = defaultdict(list)
    counts: Counter = Counter()
    for record in iter_jsonl(path):
        counts["candidates"] += 1
        try:
            location = (str(record["source_file"]), int(record["source_line"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Toxicity decision lacks a valid source location: {record}") from exc
        guard = record.get("guard") or {}
        safety = str(guard.get("safety") or "").lower()
        if guard.get("error"):
            outcome = "error"
        elif safety in {"unsafe", "controversial", "safe"}:
            outcome = safety
        else:
            outcome = "error"
        counts[outcome] += 1
        if outcome in {"unsafe", "controversial", "error"}:
            rejected[location].append(compact_toxicity(record, outcome))
    return rejected, counts


def reason_labels(
    pii: list[dict[str, Any]],
    toxicity: list[dict[str, Any]],
) -> list[str]:
    labels = set()
    for item in pii:
        labels.add("pii_error" if item.get("status") == "error" else "pii")
    for item in toxicity:
        outcome = item.get("guard_outcome")
        labels.add(f"toxicity_{outcome}")
    return sorted(labels)


def atomic_outputs(clean_path: Path, rejected_path: Path):
    suffix = f".partial.{os.getpid()}"
    clean_tmp = clean_path.with_name(clean_path.name + suffix)
    rejected_tmp = rejected_path.with_name(rejected_path.name + suffix)
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    rejected_path.parent.mkdir(parents=True, exist_ok=True)
    return clean_tmp, rejected_tmp


def process_source_file(
    *,
    input_root: Path,
    output_root: Path,
    relative: str,
    pii: dict[Location, list[dict[str, Any]]],
    pii_source_hashes: dict[Location, set[str]],
    toxicity: dict[Location, list[dict[str, Any]]],
    review_handle: Any,
    expected_snapshot: dict[str, Any],
) -> dict[str, Any]:
    source = input_root / relative
    verify_source_snapshot(source, expected_snapshot)
    clean_path = output_root / "filtered" / relative
    rejected_path = output_root / "filtered_out" / relative
    clean_tmp, rejected_tmp = atomic_outputs(clean_path, rejected_path)

    counts: Counter = Counter()
    reason_counts: Counter = Counter()
    file_digest = hashlib.sha256()
    try:
        with source.open("rb") as input_handle, clean_tmp.open("wb") as clean_handle, (
            rejected_tmp.open("wb")
        ) as rejected_handle:
            for source_line, raw_line in enumerate(input_handle, 1):
                file_digest.update(raw_line)
                location = (relative, source_line)
                pii_items = pii.get(location, [])
                expected_pii_hashes = pii_source_hashes.get(location, set())
                toxicity_items = toxicity.get(location, [])
                if expected_pii_hashes:
                    source_sha256 = hashlib.sha256(
                        raw_line.rstrip(b"\r\n")
                    ).hexdigest()
                    if expected_pii_hashes != {source_sha256}:
                        raise RuntimeError(
                            f"PII source hash mismatch at {relative}:{source_line}"
                        )
                labels = reason_labels(pii_items, toxicity_items)
                counts["input_rows"] += 1
                counts["input_bytes"] += len(raw_line)
                if labels:
                    rejected_handle.write(raw_line)
                    counts["rejected_rows"] += 1
                    counts["rejected_bytes"] += len(raw_line)
                    reason_counts.update(labels)
                    has_error = any(label.endswith("_error") for label in labels)
                    has_pii = any(label.startswith("pii") for label in labels)
                    has_toxicity = any(label.startswith("toxicity") for label in labels)
                    if has_error:
                        counts["error_rejected_rows"] += 1
                    elif has_pii and has_toxicity:
                        counts["both_rejected_rows"] += 1
                    elif has_pii:
                        counts["pii_rejected_rows"] += 1
                    else:
                        counts["toxicity_rejected_rows"] += 1
                    review_handle.write(
                        json.dumps(
                            {
                                "source_file": relative,
                                "source_line": source_line,
                                "reasons": labels,
                                "pii": pii_items,
                                "toxicity": toxicity_items,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                else:
                    clean_handle.write(raw_line)
                    counts["clean_rows"] += 1
                    counts["clean_bytes"] += len(raw_line)
        rows_by_file = counts["input_rows"]
        for source_file, source_line in set(pii_source_hashes) | set(toxicity):
            if source_file == relative and not 1 <= source_line <= rows_by_file:
                raise RuntimeError(
                    f"Decision points outside shard input: {source_file}:{source_line}"
                )
        if file_digest.hexdigest() != expected_snapshot["sha256"]:
            raise RuntimeError(f"Source content differs from snapshot: {source}")
        verify_source_snapshot(source, expected_snapshot)
        clean_tmp.replace(clean_path)
        rejected_tmp.replace(rejected_path)
    except BaseException:
        clean_tmp.unlink(missing_ok=True)
        rejected_tmp.unlink(missing_ok=True)
        raise

    if counts["input_rows"] != counts["clean_rows"] + counts["rejected_rows"]:
        raise RuntimeError(f"Row reconciliation failed for {relative}")
    if counts["input_bytes"] != counts["clean_bytes"] + counts["rejected_bytes"]:
        raise RuntimeError(f"Byte reconciliation failed for {relative}")
    return {"source_file": relative, **counts, "reason_counts": dict(reason_counts)}


def read_manifest(path: Path) -> list[str]:
    files = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(files) != len(set(files)):
        raise ValueError(f"Duplicate paths in manifest: {path}")
    return files


def run_shard(args: argparse.Namespace) -> int:
    shard_name = args.manifest.stem
    manifest_files = read_manifest(args.manifest)
    metadata_path = args.manifest.parent.parent / "manifest.json"
    snapshots = load_source_snapshot(metadata_path, args.input_root)
    if set(manifest_files) - set(snapshots):
        missing = sorted(set(manifest_files) - set(snapshots))
        raise RuntimeError(f"Manifest paths missing from snapshot metadata: {missing[:5]}")
    pii_path = (
        args.run_root
        / "scans"
        / "pii"
        / "shards"
        / shard_name
        / "stage4_llm"
        / "pii_llm_validation_results.jsonl"
    )
    stage3_path = (
        args.run_root
        / "scans"
        / "pii"
        / "shards"
        / shard_name
        / "stage3_extract"
        / "pii_extract.csv"
    )
    toxicity_path = (
        args.run_root
        / "scans"
        / "toxicity"
        / "shards"
        / shard_name
        / "guard_results"
        / "all.jsonl"
    )
    expected_pii_keys = load_stage3_candidate_keys(stage3_path)
    pii, pii_source_hashes, actual_pii_keys, pii_counts = load_pii_decisions(pii_path)
    if actual_pii_keys != expected_pii_keys:
        missing = len(expected_pii_keys - actual_pii_keys)
        unexpected = len(actual_pii_keys - expected_pii_keys)
        raise RuntimeError(
            f"Stage-3/4 PII reconciliation failed: "
            f"{missing} missing and {unexpected} unexpected decisions"
        )
    toxicity, toxicity_counts = load_toxicity_decisions(toxicity_path)
    decision_files = {
        source_file for source_file, _ in set(pii_source_hashes) | set(toxicity)
    }
    unexpected_files = decision_files - set(manifest_files)
    if unexpected_files:
        raise RuntimeError(
            f"Decision files outside shard manifest: {sorted(unexpected_files)[:5]}"
        )

    staging_root = args.output_root / ".staging" / shard_name
    if staging_root.exists():
        shutil.rmtree(staging_root)
    review_dir = staging_root / "review" / "rejected_rows"
    pii_review_dir = staging_root / "review" / "pii"
    toxicity_review_dir = staging_root / "review" / "toxicity"
    report_dir = args.output_root / "reports" / "shards"
    review_dir.mkdir(parents=True, exist_ok=True)
    pii_review_dir.mkdir(parents=True, exist_ok=True)
    toxicity_review_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    with (pii_review_dir / f"{shard_name}.jsonl").open("w", encoding="utf-8") as handle:
        for (source_file, source_line), items in sorted(pii.items()):
            handle.write(
                json.dumps(
                    {"source_file": source_file, "source_line": source_line, "candidates": items},
                    ensure_ascii=False,
                )
                + "\n"
            )
    with (toxicity_review_dir / f"{shard_name}.jsonl").open("w", encoding="utf-8") as handle:
        for (source_file, source_line), items in sorted(toxicity.items()):
            handle.write(
                json.dumps(
                    {"source_file": source_file, "source_line": source_line, "messages": items},
                    ensure_ascii=False,
                )
                + "\n"
            )
    review_path = review_dir / f"{shard_name}.jsonl"
    file_reports = []
    with review_path.open("w", encoding="utf-8") as review_handle:
        for relative in manifest_files:
            file_reports.append(
                process_source_file(
                    input_root=args.input_root,
                    output_root=staging_root,
                    relative=relative,
                    pii=pii,
                    pii_source_hashes=pii_source_hashes,
                    toxicity=toxicity,
                    review_handle=review_handle,
                    expected_snapshot=snapshots[relative],
                )
            )

    rows_by_file = {report["source_file"]: report["input_rows"] for report in file_reports}
    for source_file, source_line in set(pii_source_hashes) | set(toxicity):
        if source_file not in rows_by_file or not 1 <= source_line <= rows_by_file[source_file]:
            raise RuntimeError(
                f"Decision points outside shard input: {source_file}:{source_line}"
            )

    for relative in manifest_files:
        for partition in ("filtered", "filtered_out"):
            staged = staging_root / partition / relative
            destination = args.output_root / partition / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(destination)
    for review_kind in ("rejected_rows", "pii", "toxicity"):
        staged = staging_root / "review" / review_kind / f"{shard_name}.jsonl"
        destination = args.output_root / "review" / review_kind / staged.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged.replace(destination)
    shutil.rmtree(staging_root)
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "shard": shard_name,
        "manifest": str(args.manifest),
        "pii_candidates": dict(pii_counts),
        "toxicity_candidates": dict(toxicity_counts),
        "files": file_reports,
    }
    report_path = report_dir / f"{shard_name}.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (report_dir / f"{shard_name}._SUCCESS").touch()
    return 0


def run_aggregate(args: argparse.Namespace) -> int:
    manifest_metadata = json.loads((args.manifest_dir / "manifest.json").read_text(encoding="utf-8"))
    expected = [f"shard_{index:05d}" for index in range(manifest_metadata["shard_count"])]
    report_dir = args.output_root / "reports"
    shard_dir = report_dir / "shards"
    reports = []
    for shard_name in expected:
        success = shard_dir / f"{shard_name}._SUCCESS"
        report_path = shard_dir / f"{shard_name}.json"
        if not success.is_file() or not report_path.is_file():
            raise RuntimeError(f"Missing finalized shard: {shard_name}")
        reports.append(json.loads(report_path.read_text(encoding="utf-8")))

    totals: Counter = Counter()
    reasons: Counter = Counter()
    file_rows = []
    pii_candidates: Counter = Counter()
    toxicity_candidates: Counter = Counter()
    for report in reports:
        pii_candidates.update(report["pii_candidates"])
        toxicity_candidates.update(report["toxicity_candidates"])
        for file_report in report["files"]:
            file_rows.append(file_report)
            for key in (
                "input_rows",
                "input_bytes",
                "clean_rows",
                "clean_bytes",
                "rejected_rows",
                "rejected_bytes",
                "pii_rejected_rows",
                "toxicity_rejected_rows",
                "both_rejected_rows",
                "error_rejected_rows",
            ):
                totals[key] += file_report.get(key, 0)
            reasons.update(file_report.get("reason_counts") or {})

    if totals["input_rows"] != totals["clean_rows"] + totals["rejected_rows"]:
        raise RuntimeError("Aggregate row reconciliation failed")
    if totals["input_bytes"] != totals["clean_bytes"] + totals["rejected_bytes"]:
        raise RuntimeError("Aggregate byte reconciliation failed")

    report_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_root": str(args.input_root),
        "output_root": str(args.output_root),
        "manifest": str(args.manifest_dir / "manifest.json"),
        "policy": {
            "pii": "reject only stage-4a keep decisions; unresolved errors are safe",
            "toxicity": "reject Unsafe, Controversial, unknown, and guard errors",
        },
        "models": {
            "pii": os.environ.get("PII_LLM_MODEL", "google/gemma-4-31B-it"),
            "toxicity_stage1": os.environ.get("TOX_STAGE1_MODEL", "multilingual"),
            "toxicity_stage2": os.environ.get("GUARD_MODEL", "Qwen/Qwen3Guard-Gen-8B"),
        },
        "totals": dict(totals),
        "rejection_reasons": dict(reasons),
        "pii_candidates": dict(pii_candidates),
        "toxicity_candidates": dict(toxicity_candidates),
    }
    (report_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )

    with (report_dir / "per_file.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = [
            "source_file",
            "input_rows",
            "input_bytes",
            "clean_rows",
            "clean_bytes",
            "rejected_rows",
            "rejected_bytes",
            "pii_rejected_rows",
            "toxicity_rejected_rows",
            "both_rejected_rows",
            "error_rejected_rows",
            "reason_counts",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in sorted(file_rows, key=lambda item: item["source_file"]):
            output = dict(row)
            output["reason_counts"] = json.dumps(output.get("reason_counts") or {}, sort_keys=True)
            writer.writerow(output)
    (args.output_root / "_SUCCESS").touch()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    shard = subparsers.add_parser("shard")
    shard.add_argument("--input-root", type=Path, required=True)
    shard.add_argument("--run-root", type=Path, required=True)
    shard.add_argument("--output-root", type=Path, required=True)
    shard.add_argument("--manifest", type=Path, required=True)

    aggregate = subparsers.add_parser("aggregate")
    aggregate.add_argument("--input-root", type=Path, required=True)
    aggregate.add_argument("--manifest-dir", type=Path, required=True)
    aggregate.add_argument("--output-root", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "shard":
        return run_shard(args)
    return run_aggregate(args)


if __name__ == "__main__":
    raise SystemExit(main())
