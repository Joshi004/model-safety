#!/usr/bin/env python3
"""Build a filtered tree from selected rows in an existing match report."""

from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import os
import shutil
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


@dataclass(frozen=True)
class MatchRule:
    source_glob: str
    reference_glob: str

    def matches(self, row: dict[str, str]) -> bool:
        return fnmatch.fnmatchcase(
            row["source_file"], self.source_glob
        ) and fnmatch.fnmatchcase(row["reference_file"], self.reference_glob)


def parse_rule(value: str) -> MatchRule:
    try:
        source_glob, reference_glob = value.split("::", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "rules must use SOURCE_GLOB::REFERENCE_GLOB"
        ) from exc
    if not source_glob or not reference_glob:
        raise argparse.ArgumentTypeError(
            "rules must have non-empty source and reference globs"
        )
    return MatchRule(source_glob, reference_glob)


def validate_relative_path(value: str, field: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"Invalid relative {field}: {value!r}")
    return path.as_posix()


def select_matches(
    matched_samples: Path,
    rules: Iterable[MatchRule],
) -> tuple[list[dict[str, str]], dict[tuple[str, int], list[dict[str, str]]]]:
    rules = list(rules)
    if not rules:
        raise ValueError("At least one match rule is required")

    selected: list[dict[str, str]] = []
    by_source: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    required = {"source_file", "source_line", "reference_file", "reference_line"}
    with matched_samples.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"Match report is missing columns: {', '.join(sorted(missing))}"
            )
        for row in reader:
            row["source_file"] = validate_relative_path(
                row["source_file"], "source_file"
            )
            row["reference_file"] = validate_relative_path(
                row["reference_file"], "reference_file"
            )
            if not any(rule.matches(row) for rule in rules):
                continue
            try:
                source_line = int(row["source_line"])
                reference_line = int(row["reference_line"])
            except ValueError as exc:
                raise ValueError("Match report line numbers must be integers") from exc
            if source_line < 1 or reference_line < 1:
                raise ValueError("Match report line numbers must be positive")
            selected.append(row)
            by_source[(row["source_file"], source_line)].append(row)

    if not selected:
        raise ValueError("No rows in the match report satisfy the supplied rules")
    return selected, dict(by_source)


def prepare_output(output_dir: Path, overwrite: bool) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        if not overwrite:
            raise ValueError(f"Output directory is not empty: {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)


def link_or_copy(source: Path, destination: Path, copy_mode: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if copy_mode == "hardlink":
        os.link(source, destination)
    elif copy_mode == "copy":
        shutil.copy2(source, destination)
    else:
        raise ValueError(f"Unsupported copy mode: {copy_mode}")


def partition_selected_files(
    input_root: Path,
    output_dir: Path,
    selected_rows: dict[tuple[str, int], list[dict[str, str]]],
    *,
    copy_mode: str,
) -> dict[str, int]:
    selected_by_file: dict[str, set[int]] = defaultdict(set)
    for source_file, source_line in selected_rows:
        selected_by_file[source_file].add(source_line)

    files = sorted(path for path in input_root.rglob("*.jsonl") if path.is_file())
    if not files:
        raise ValueError(f"No JSONL files found under: {input_root}")
    file_relatives = {path.relative_to(input_root).as_posix() for path in files}
    missing_files = set(selected_by_file) - file_relatives
    if missing_files:
        raise ValueError(
            "Selected source file is absent from input root: "
            + min(missing_files)
        )

    removed_rows = 0
    rewritten_input_rows = 0
    for source in files:
        relative = source.relative_to(input_root)
        relative_text = relative.as_posix()
        kept_path = output_dir / "filtered" / relative
        removed_path = output_dir / "filtered_out" / relative
        removed_path.parent.mkdir(parents=True, exist_ok=True)
        wanted_lines = selected_by_file.get(relative_text)
        if not wanted_lines:
            link_or_copy(source, kept_path, copy_mode)
            removed_path.touch()
            continue

        kept_path.parent.mkdir(parents=True, exist_ok=True)
        remaining = set(wanted_lines)
        with (
            source.open("rb") as source_handle,
            kept_path.open("wb") as kept_handle,
            removed_path.open("wb") as removed_handle,
        ):
            for source_line, raw_line in enumerate(source_handle, 1):
                rewritten_input_rows += 1
                if source_line in wanted_lines:
                    removed_handle.write(raw_line)
                    remaining.discard(source_line)
                    removed_rows += 1
                else:
                    kept_handle.write(raw_line)
        if remaining:
            raise ValueError(
                f"Selected source coordinate does not exist: "
                f"{relative_text}:{min(remaining)}"
            )

    return {
        "input_files": len(files),
        "rewritten_files": len(selected_by_file),
        "rewritten_input_rows": rewritten_input_rows,
        "removed_rows": removed_rows,
    }


def write_selected_matches(
    path: Path,
    selected: list[dict[str, str]],
) -> None:
    fieldnames = list(selected[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(selected)


def write_selected_rows(
    path: Path,
    selected_rows: dict[tuple[str, int], list[dict[str, str]]],
) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for (source_file, source_line), matches in sorted(selected_rows.items()):
            record = {
                "source_file": source_file,
                "source_line": source_line,
                "matches": [
                    {
                        "reference_file": row["reference_file"],
                        "reference_line": int(row["reference_line"]),
                    }
                    for row in matches
                ],
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def postprocess(
    input_root: Path,
    matched_samples: Path,
    output_dir: Path,
    rules: Iterable[MatchRule],
    *,
    copy_mode: str = "hardlink",
    overwrite: bool = False,
) -> dict[str, Any]:
    input_root = input_root.expanduser().resolve()
    matched_samples = matched_samples.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    rules = list(rules)

    if not input_root.is_dir():
        raise ValueError(f"Input root is not a directory: {input_root}")
    if not matched_samples.is_file():
        raise ValueError(f"Match report does not exist: {matched_samples}")
    if (
        output_dir == input_root
        or input_root in output_dir.parents
        or output_dir in input_root.parents
    ):
        raise ValueError("Input and output directories cannot contain one another")
    if output_dir in matched_samples.parents:
        raise ValueError("Output directory cannot contain the match report")

    selected, selected_rows = select_matches(matched_samples, rules)
    prepare_output(output_dir, overwrite)
    write_selected_matches(output_dir / "selected_matches.csv", selected)
    write_selected_rows(output_dir / "selected_rows.jsonl", selected_rows)
    partition = partition_selected_files(
        input_root,
        output_dir,
        selected_rows,
        copy_mode=copy_mode,
    )

    benchmark_rows = {
        (row["reference_file"], int(row["reference_line"])) for row in selected
    }
    summary: dict[str, Any] = {
        "mode": "selected_match_postprocess",
        "input_root": str(input_root),
        "matched_samples": str(matched_samples),
        "output_dir": str(output_dir),
        "copy_mode_for_unchanged_files": copy_mode,
        "selected_match_pairs": len(selected),
        "selected_training_rows": len(selected_rows),
        "selected_benchmark_rows": len(benchmark_rows),
        **partition,
        "rules": [
            {
                "source_glob": rule.source_glob,
                "reference_glob": rule.reference_glob,
            }
            for rule in rules
        ],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "_SUCCESS").touch()
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--matched-samples", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--rule",
        type=parse_rule,
        action="append",
        required=True,
        help="Select matches using SOURCE_GLOB::REFERENCE_GLOB; repeat as needed",
    )
    parser.add_argument(
        "--copy-mode",
        choices=("hardlink", "copy"),
        default="hardlink",
        help="How to materialize unchanged JSONL files (default: hardlink)",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = postprocess(
        args.input_root,
        args.matched_samples,
        args.output_dir,
        args.rule,
        copy_mode=args.copy_mode,
        overwrite=args.overwrite,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
