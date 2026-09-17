#!/usr/bin/env python3
"""Summarize guard safety labels for a folder of JSONL files."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import TextIO


LABELS = ("unsafe", "controversial", "safe", "error", "unknown")
DISPLAY_LABELS = LABELS
ROLES = ("user", "assistant")


def empty_counts() -> dict[str, dict[str, int]]:
    return {
        label: {"total": 0, "user": 0, "assistant": 0}
        for label in LABELS
    }


def iter_input_files(folder: Path) -> list[Path]:
    all_file = folder / "all.jsonl"
    if all_file.is_file():
        return [all_file]

    split_files = [
        folder / f"{label}.jsonl"
        for label in LABELS
        if (folder / f"{label}.jsonl").is_file()
    ]

    if split_files:
        return split_files

    return sorted(folder.glob("*.jsonl"))


def summarize(folder: Path) -> tuple[int, dict[str, dict[str, int]]]:
    counts = empty_counts()
    total_samples = 0

    for path in iter_input_files(folder):
        fallback_safety = path.stem.lower()

        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue

                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc

                guard = row.get("guard") or {}
                if guard.get("error"):
                    safety = "error"
                else:
                    safety = str(guard.get("safety") or fallback_safety).lower()
                    if safety not in counts:
                        safety = "unknown"
                role = str(guard.get("flagged_role") or "").lower()

                if safety not in counts:
                    continue

                total_samples += 1
                counts[safety]["total"] += 1

                if role in ROLES:
                    counts[safety][role] += 1

    return total_samples, counts


def merge_counts(
    target: dict[str, dict[str, int]],
    source: dict[str, dict[str, int]],
) -> None:
    for label in LABELS:
        for key in ("total", "user", "assistant"):
            target[label][key] += source[label][key]


def build_rows(total_samples: int, counts: dict[str, dict[str, int]]) -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = [
        {
            "category": "total samples",
            "total": total_samples,
            "user": "",
            "assistant": "",
        }
    ]

    for label in DISPLAY_LABELS:
        row = counts[label]
        rows.append(
            {
                "category": f"{label} samples",
                "total": row["total"],
                "user": row["user"],
                "assistant": row["assistant"],
            }
        )

    return rows


def write_csv(
    reports: list[tuple[str, list[dict[str, str | int]]]],
    output: TextIO,
) -> None:
    writer = csv.DictWriter(
        output,
        fieldnames=["scope", "category", "total", "user", "assistant"],
    )
    writer.writeheader()
    for scope, rows in reports:
        for row in rows:
            writer.writerow({"scope": scope, **row})


def write_markdown_table(rows: list[dict[str, str | int]], output: TextIO) -> None:
    output.write("| category | total | user | assistant |\n")
    output.write("|---|---:|---:|---:|\n")
    for row in rows:
        output.write(
            f"| {row['category']} | {row['total']} | {row['user']} | {row['assistant']} |\n"
        )


def write_markdown(
    reports: list[tuple[str, list[dict[str, str | int]]]],
    output: TextIO,
) -> None:
    for index, (title, rows) in enumerate(reports):
        if index:
            output.write("\n")
        output.write(f"## {title}\n\n")
        write_markdown_table(rows, output)


def write_plain_table(
    rows: list[dict[str, str | int]],
    output: TextIO,
) -> None:
    headers = ["category", "total", "user", "assistant"]
    widths = {
        header: max(len(header), *(len(str(row[header])) for row in rows))
        for header in headers
    }

    output.write(
        "  ".join(header.ljust(widths[header]) for header in headers) + "\n"
    )
    output.write(
        "  ".join("-" * widths[header] for header in headers) + "\n"
    )
    for row in rows:
        output.write(
            "  ".join(str(row[header]).ljust(widths[header]) for header in headers)
            + "\n"
        )


def write_plain_reports(
    reports: list[tuple[str, list[dict[str, str | int]]]],
    output: TextIO,
) -> None:
    for index, (title, rows) in enumerate(reports):
        if index:
            output.write("\n")
        output.write(f"{title}\n")
        output.write("=" * len(title) + "\n")
        write_plain_table(rows, output)


def write_rich(reports: list[tuple[str, list[dict[str, str | int]]]]) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:
        print(
            "rich is not installed; falling back to a plain terminal table.",
            file=sys.stderr,
        )
        write_plain_reports(reports, sys.stdout)
        return

    console = Console()
    for title, rows in reports:
        table = Table(title=title)
        table.add_column("category")
        table.add_column("total", justify="right")
        table.add_column("user", justify="right")
        table.add_column("assistant", justify="right")

        for row in rows:
            table.add_row(
                str(row["category"]),
                str(row["total"]),
                str(row["user"]),
                str(row["assistant"]),
            )

        console.print(table)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize Safe/Unsafe/Controversial guard labels in JSONL files."
    )
    parser.add_argument(
        "folders",
        type=Path,
        nargs="+",
        help="One or more folders containing guard output JSONL files.",
    )
    parser.add_argument(
        "--format",
        choices=("rich", "markdown", "csv"),
        default="rich",
        help="Output format. Defaults to rich.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional file to write markdown or CSV output to. Rich output is stdout only.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    folders = [folder.expanduser() for folder in args.folders]

    for folder in folders:
        if not folder.is_dir():
            print(
                f"error: folder does not exist or is not a directory: {folder}",
                file=sys.stderr,
            )
            return 2

    reports: list[tuple[str, list[dict[str, str | int]]]] = []
    aggregate_total = 0
    aggregate_counts = empty_counts()

    for folder in folders:
        try:
            total_samples, counts = summarize(folder)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        reports.append((str(folder), build_rows(total_samples, counts)))
        aggregate_total += total_samples
        merge_counts(aggregate_counts, counts)

    reports.append(("aggregated", build_rows(aggregate_total, aggregate_counts)))

    if args.format == "rich":
        if args.output:
            print("error: --output is only supported for markdown and csv formats", file=sys.stderr)
            return 2
        write_rich(reports)
        return 0

    if args.output:
        with args.output.open("w", encoding="utf-8", newline="") as output:
            if args.format == "csv":
                write_csv(reports, output)
            else:
                write_markdown(reports, output)
        return 0

    if args.format == "csv":
        write_csv(reports, sys.stdout)
    else:
        write_markdown(reports, sys.stdout)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
