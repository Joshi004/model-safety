#!/usr/bin/env python3
"""Aggregate matched_samples.csv by training and benchmark datasets."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any


def training_dataset_name(source_file: str) -> str:
    stem = Path(source_file).stem
    for prefix in ("stage_1_", "stage_2_", "medpsy1_", "baichuan_m3_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix) :]
    if stem.endswith("_full"):
        stem = stem[: -len("_full")]
    return stem


def benchmark_dataset_name(reference_file: str) -> str:
    parts = Path(reference_file).parts
    if len(parts) >= 2 and parts[0] in {
        "closed_ended",
        "healthbench",
        "open_ended_arena",
        "safety",
    }:
        return parts[1]
    return parts[0] if parts else "unknown"


def add_match(
    groups: dict[tuple[str, ...], dict[str, Any]],
    key: tuple[str, ...],
    row: dict[str, str],
) -> None:
    group = groups.setdefault(
        key,
        {
            "pairs": 0,
            "training_rows": set(),
            "benchmark_rows": set(),
            "training_files": set(),
            "benchmark_files": set(),
        },
    )
    group["pairs"] += 1
    group["training_rows"].add((row["source_file"], int(row["source_line"])))
    group["benchmark_rows"].add(
        (row["reference_file"], int(row["reference_line"]))
    )
    group["training_files"].add(row["source_file"])
    group["benchmark_files"].add(row["reference_file"])


def write_dataset_summary(
    path: Path,
    groups: dict[tuple[str, ...], dict[str, Any]],
) -> None:
    fields = [
        "training_group",
        "training_dataset",
        "benchmark_group",
        "benchmark_dataset",
        "matched_training_rows",
        "contaminated_benchmark_rows",
        "match_pairs",
        "training_files",
        "benchmark_files",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for key in sorted(groups):
            group = groups[key]
            writer.writerow(
                {
                    "training_group": key[0],
                    "training_dataset": key[1],
                    "benchmark_group": key[2],
                    "benchmark_dataset": key[3],
                    "matched_training_rows": len(group["training_rows"]),
                    "contaminated_benchmark_rows": len(group["benchmark_rows"]),
                    "match_pairs": group["pairs"],
                    "training_files": len(group["training_files"]),
                    "benchmark_files": len(group["benchmark_files"]),
                }
            )


def write_file_summary(
    path: Path,
    groups: dict[tuple[str, ...], dict[str, Any]],
) -> None:
    fields = [
        "training_dataset",
        "training_file",
        "benchmark_dataset",
        "benchmark_file",
        "matched_training_rows",
        "contaminated_benchmark_rows",
        "match_pairs",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for key in sorted(groups):
            group = groups[key]
            writer.writerow(
                {
                    "training_dataset": key[0],
                    "training_file": key[1],
                    "benchmark_dataset": key[2],
                    "benchmark_file": key[3],
                    "matched_training_rows": len(group["training_rows"]),
                    "contaminated_benchmark_rows": len(group["benchmark_rows"]),
                    "match_pairs": group["pairs"],
                }
            )


def summarize(matched_samples: Path, output_dir: Path) -> dict[str, int]:
    dataset_groups: dict[tuple[str, ...], dict[str, Any]] = {}
    file_groups: dict[tuple[str, ...], dict[str, Any]] = {}
    rows = 0
    with matched_samples.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            rows += 1
            source_parts = Path(row["source_file"]).parts
            training_group = source_parts[0] if source_parts else "unknown"
            training_dataset = training_dataset_name(row["source_file"])
            reference_parts = Path(row["reference_file"]).parts
            benchmark_group = reference_parts[0] if reference_parts else "unknown"
            benchmark_dataset = benchmark_dataset_name(row["reference_file"])
            add_match(
                dataset_groups,
                (
                    training_group,
                    training_dataset,
                    benchmark_group,
                    benchmark_dataset,
                ),
                row,
            )
            add_match(
                file_groups,
                (
                    training_dataset,
                    row["source_file"],
                    benchmark_dataset,
                    row["reference_file"],
                ),
                row,
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    write_dataset_summary(
        output_dir / "contamination_by_training_dataset.csv",
        dataset_groups,
    )
    write_file_summary(
        output_dir / "contamination_by_training_file.csv",
        file_groups,
    )
    return {
        "match_pairs": rows,
        "dataset_relations": len(dataset_groups),
        "file_relations": len(file_groups),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matched-samples", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = summarize(
        args.matched_samples.expanduser().resolve(),
        args.output_dir.expanduser().resolve(),
    )
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
