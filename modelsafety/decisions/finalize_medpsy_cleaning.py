#!/usr/bin/env python3
"""Combine quality-filter and decontamination rejects into the final dataset."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


Location = tuple[str, int]


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_number}") from exc


def relative_path(value: Any, *, field: str) -> str:
    path = PurePosixPath(str(value))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"Invalid relative {field}: {value!r}")
    return path.as_posix()


def load_decontamination_locations(path: Path) -> set[Location]:
    locations: set[Location] = set()
    for record in iter_jsonl(path):
        location = (
            relative_path(record.get("source_file"), field="source_file"),
            int(record["source_line"]),
        )
        if location[1] < 1:
            raise ValueError(f"Invalid source line in {path}: {location[1]}")
        if location in locations:
            raise ValueError(f"Duplicate decontamination location: {location}")
        locations.add(location)
    if not locations:
        raise ValueError(f"No decontamination locations found in {path}")
    return locations


def load_quality_locations(
    review_dir: Path,
) -> tuple[set[Location], set[Location], set[Location]]:
    rejected: set[Location] = set()
    pii: set[Location] = set()
    toxicity: set[Location] = set()
    review_files = sorted(review_dir.glob("*.jsonl"))
    if not review_files:
        raise ValueError(f"No quality review files found under {review_dir}")

    for review_file in review_files:
        for record in iter_jsonl(review_file):
            location = (
                relative_path(record.get("source_file"), field="source_file"),
                int(record["source_line"]),
            )
            if location[1] < 1:
                raise ValueError(f"Invalid source line in {review_file}: {location[1]}")
            if location in rejected:
                raise ValueError(f"Duplicate quality rejection location: {location}")
            reasons = {str(reason) for reason in record.get("reasons") or []}
            if any(reason.startswith("pii") for reason in reasons):
                pii.add(location)
            if any(reason.startswith("toxicity") for reason in reasons):
                toxicity.add(location)
            if not reasons or location not in pii | toxicity:
                raise ValueError(f"Unrecognized rejection reasons at {location}: {reasons}")
            rejected.add(location)
    return rejected, pii, toxicity


def prepare_output(output_root: Path) -> None:
    if output_root.exists() and any(output_root.iterdir()):
        raise ValueError(f"Final output directory is not empty: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)


def hardlink(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.link(source, destination)


def partition_file(
    source: Path,
    clean_path: Path,
    rejected_path: Path,
    rejected_lines: set[int],
) -> tuple[int, int]:
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    rejected_path.parent.mkdir(parents=True, exist_ok=True)
    remaining = set(rejected_lines)
    input_rows = 0
    rejected_rows = 0
    with (
        source.open("rb") as source_handle,
        clean_path.open("wb") as clean_handle,
        rejected_path.open("wb") as rejected_handle,
    ):
        for source_line, raw_line in enumerate(source_handle, 1):
            input_rows += 1
            if source_line in rejected_lines:
                rejected_handle.write(raw_line)
                rejected_rows += 1
                remaining.discard(source_line)
            else:
                clean_handle.write(raw_line)
    if remaining:
        raise ValueError(
            f"Rejected source coordinate does not exist: {source}:{min(remaining)}"
        )
    return input_rows, rejected_rows


def link_quality_tree(
    source_root: Path,
    quality_root: Path,
    output_root: Path,
    all_rejected: set[Location],
    rewritten_files: set[str],
) -> dict[str, int]:
    quality_clean_root = quality_root / "filtered"
    quality_rejected_root = quality_root / "filtered_out"
    clean_files = sorted(path for path in quality_clean_root.rglob("*.jsonl") if path.is_file())
    if not clean_files:
        raise ValueError(f"No quality-filtered JSONL files found under {quality_clean_root}")

    source_relatives = {
        path.relative_to(quality_clean_root).as_posix(): path for path in clean_files
    }
    missing = rewritten_files - set(source_relatives)
    if missing:
        raise ValueError(f"Decontaminated source is absent from quality output: {min(missing)}")

    linked_files = 0
    rewritten_rows = 0
    rewritten_rejected_rows = 0
    rejected_by_file: dict[str, set[int]] = defaultdict(set)
    for source_file, source_line in all_rejected:
        if source_file in rewritten_files:
            rejected_by_file[source_file].add(source_line)

    for relative, quality_clean in source_relatives.items():
        quality_rejected = quality_rejected_root / relative
        if not quality_rejected.is_file():
            raise ValueError(f"Missing quality rejected partition: {quality_rejected}")
        destination_clean = output_root / "filtered" / relative
        destination_rejected = output_root / "filtered_out" / relative
        if relative not in rewritten_files:
            hardlink(quality_clean, destination_clean)
            hardlink(quality_rejected, destination_rejected)
            linked_files += 1
            continue

        input_rows, rejected_rows = partition_file(
            source_root / relative,
            destination_clean,
            destination_rejected,
            rejected_by_file[relative],
        )
        rewritten_rows += input_rows
        rewritten_rejected_rows += rejected_rows

    return {
        "output_files": len(clean_files),
        "hardlinked_files": linked_files,
        "rewritten_files": len(rewritten_files),
        "rewritten_input_rows": rewritten_rows,
        "rewritten_rejected_rows": rewritten_rejected_rows,
    }


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Required report does not exist: {path}") from exc


def finalize(args: argparse.Namespace) -> dict[str, Any]:
    source_root = args.source_root.expanduser().resolve()
    quality_root = args.quality_root.expanduser().resolve()
    decontamination_root = args.decontamination_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    baichuan_report_paths = [
        path.expanduser().resolve() for path in args.baichuan_report
    ]
    baichuan_reports = [load_json(path) for path in baichuan_report_paths]
    baichuan_totals = {
        key: sum(int(report["totals"][key]) for report in baichuan_reports)
        for key in ("total_rows", "filtered_rows", "filtered_out_rows")
    }
    quality_summary = load_json(quality_root / "reports" / "summary.json")
    decontamination_summary = load_json(decontamination_root / "summary.json")

    for root, label in (
        (source_root, "source"),
        (quality_root, "quality"),
        (decontamination_root, "decontamination"),
    ):
        if not root.is_dir():
            raise ValueError(f"{label.title()} root does not exist: {root}")
    for marker in (quality_root / "_SUCCESS", decontamination_root / "_SUCCESS"):
        if not marker.is_file():
            raise ValueError(f"Required success marker does not exist: {marker}")

    decontaminated = load_decontamination_locations(
        decontamination_root / "selected_rows.jsonl"
    )
    quality_rejected, pii_rejected, toxicity_rejected = load_quality_locations(
        quality_root / "review" / "rejected_rows"
    )
    if quality_rejected != pii_rejected | toxicity_rejected:
        raise ValueError("Quality rejection locations do not reconcile with PII/toxicity")

    quality_totals = quality_summary.get("totals") or {}
    source_rows = int(quality_totals["input_rows"])
    if source_rows != baichuan_totals["filtered_rows"]:
        raise ValueError("Baichuan and quality-filter input row counts do not match")
    if len(quality_rejected) != int(quality_totals["rejected_rows"]):
        raise ValueError("Quality review and summary rejected-row counts do not match")
    if len(decontaminated) != int(decontamination_summary["removed_rows"]):
        raise ValueError("Decontamination locations and summary do not match")

    prepare_output(output_root)
    all_rejected = quality_rejected | decontaminated
    materialization = link_quality_tree(
        source_root,
        quality_root,
        output_root,
        all_rejected,
        {source_file for source_file, _ in decontaminated},
    )

    toxicity_after_decontamination = toxicity_rejected - decontaminated
    pii_after_prior_stages = pii_rejected - decontaminated - toxicity_rejected
    after_baichuan = source_rows
    after_decontamination = after_baichuan - len(decontaminated)
    after_toxicity = after_decontamination - len(toxicity_after_decontamination)
    after_pii = after_toxicity - len(pii_after_prior_stages)
    if after_pii != source_rows - len(all_rejected):
        raise ValueError("Sequential stage counts do not reconcile with final row count")

    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "output_root": str(output_root),
        "lineage": {
            "baichuan_source_reports": [str(path) for path in baichuan_report_paths],
            "baichuan_filtered_root": str(source_root),
            "decontamination_root": str(decontamination_root),
            "quality_filter_root": str(quality_root),
        },
        "stage_counts": {
            "input_rows": baichuan_totals["total_rows"],
            "baichuan_removed_rows": baichuan_totals["filtered_out_rows"],
            "after_baichuan_rows": after_baichuan,
            "decontamination_removed_rows": len(decontaminated),
            "after_decontamination_rows": after_decontamination,
            "toxicity_removed_rows": len(toxicity_after_decontamination),
            "after_toxicity_rows": after_toxicity,
            "pii_removed_rows": len(pii_after_prior_stages),
            "after_pii_rows": after_pii,
            "final_rows": after_pii,
        },
        "overlap_counts": {
            "decontamination_already_rejected_by_quality": len(
                decontaminated & quality_rejected
            ),
            "pii_and_toxicity": len(pii_rejected & toxicity_rejected),
            "all_unique_post_baichuan_rejections": len(all_rejected),
        },
        "quality_filter_summary": quality_summary,
        "materialization": materialization,
    }
    reports_dir = output_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    (output_root / "_SUCCESS").touch()
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--quality-root", type=Path, required=True)
    parser.add_argument("--decontamination-root", type=Path, required=True)
    parser.add_argument(
        "--baichuan-report",
        type=Path,
        action="append",
        required=True,
        help="Baichuan filtering report; repeat for every imported source group",
    )
    parser.add_argument("--output-root", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    summary = finalize(parse_args())
    print(json.dumps(summary["stage_counts"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
