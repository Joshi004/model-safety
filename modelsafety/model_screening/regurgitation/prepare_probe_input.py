#!/usr/bin/env python3
"""
Turn a raw PII extract CSV into input for `pii_regurgitation_check.py`.

The regex/NER scanners emit one row per (file, line) occurrence, so the same
value -- especially a placeholder like `example@gmail.com` -- can appear
hundreds or thousands of times. Probing every occurrence would waste most of
the run re-testing the same value, and a value that repeats that often is
almost certainly guessable from context rather than memorized, which would
make a "hit" on it meaningless. This script:

  1. Keeps only the first row for each distinct (category, PII value) pair.
  2. Drops any pair that repeats more than --max-repeats times in the raw
     input (default 20) -- those are the template/placeholder values.
  3. Optionally takes an evenly spaced, deterministic --sample of what is
     left, for a pilot run.

Output is a CSV with the same required columns as the input
(file, line, source_sha256, category, PII, whole line) so it can be passed
straight to `pii_regurgitation_check.py --input`, plus a stats JSON showing
exactly what was kept and dropped at each stage.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQUIRED_COLUMNS = ["file", "line", "source_sha256", "category", "PII", "whole line"]

# Rows in this data can be very large (a full tool-calling conversation in
# the "whole line" column) -- csv's default field size limit is 128 KiB,
# too small for the largest rows measured in this corpus (~450 KB). Without
# this, csv.DictReader raises `field larger than field limit`.
csv.field_size_limit(sys.maxsize)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Deduplicate a PII extract CSV to one row per distinct value, "
            "drop values that repeat too often to be meaningful, and "
            "optionally sample the result -- ready for pii_regurgitation_check.py."
        )
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Raw PII extract CSV (file, line, source_sha256, category, PII, whole line).",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Where to write the prepared CSV (same columns as --input).",
    )
    parser.add_argument(
        "--stats-output",
        type=Path,
        default=None,
        help="Where to write the stats JSON (default: <output> with a _stats.json suffix).",
    )
    parser.add_argument(
        "--max-repeats",
        type=int,
        default=20,
        help=(
            "Drop any (category, PII value) pair that occurs more than this "
            "many times in --input (default: 20). 0 disables the filter."
        ),
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        help=(
            "Take an evenly spaced, deterministic subset of this size from "
            "what remains after deduplication and repeat filtering. "
            "0 (default) keeps everything."
        ),
    )
    return parser.parse_args()


def load_rows(input_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with input_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"Input CSV has no header: {input_path}")

        missing_columns = [name for name in REQUIRED_COLUMNS if name not in reader.fieldnames]
        if missing_columns:
            missing = ", ".join(missing_columns)
            raise ValueError(f"Input CSV is missing required columns: {missing}")

        rows = [{key: value or "" for key, value in row.items()} for row in reader]
        return rows, list(reader.fieldnames)


def dedup_key(row: dict[str, str]) -> tuple[str, str]:
    """(category, PII value) with the value whitespace-stripped, so trailing/

    leading whitespace differences from the upstream regex match don't count
    as distinct values on their own.
    """
    return row.get("category", ""), row.get("PII", "").strip()


def select_first_occurrence_per_key(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], Counter]:
    """One representative row per distinct key, in first-seen order, plus

    how many times each key occurred in total (needed for the repeat filter).
    """
    occurrence_counts: Counter = Counter()
    first_row_by_key: dict[tuple[str, str], dict[str, str]] = {}
    order: list[tuple[str, str]] = []

    for row in rows:
        key = dedup_key(row)
        occurrence_counts[key] += 1
        if key not in first_row_by_key:
            first_row_by_key[key] = row
            order.append(key)

    deduped_rows = [first_row_by_key[key] for key in order]
    return deduped_rows, occurrence_counts


def apply_repeat_filter(
    rows: list[dict[str, str]], occurrence_counts: Counter, max_repeats: int
) -> tuple[list[dict[str, str]], int, int]:
    """Drop rows whose key occurred more than max_repeats times in the raw

    input. max_repeats <= 0 disables the filter. Returns the kept rows plus
    (keys_dropped, rows_represented_by_dropped_keys) for the stats file.
    """
    if max_repeats <= 0:
        return rows, 0, 0

    kept = []
    keys_dropped = 0
    rows_represented = 0
    for row in rows:
        key = dedup_key(row)
        if occurrence_counts[key] > max_repeats:
            keys_dropped += 1
            rows_represented += occurrence_counts[key]
            continue
        kept.append(row)
    return kept, keys_dropped, rows_represented


def apply_sample(rows: list[dict[str, str]], sample_size: int) -> list[dict[str, str]]:
    """Evenly spaced, deterministic subset of size sample_size: sort by

    (category, PII, file, line) first so the selection does not depend on
    the raw input's row order, then take every Nth row by index -- same
    approach as this repo's dedup pilot sampler
    (scripts/tool_team_dedup_scan.sh's write_pilot_manifest).
    """
    if sample_size <= 0 or sample_size >= len(rows):
        return rows

    ordered = sorted(
        rows,
        key=lambda row: (
            row.get("category", ""),
            row.get("PII", "").strip(),
            row.get("file", ""),
            row.get("line", ""),
        ),
    )
    step = len(ordered) / sample_size
    return [ordered[int(index * step)] for index in range(sample_size)]


def count_by_category(rows: list[dict[str, str]]) -> dict[str, int]:
    counts = Counter(row.get("category", "") for row in rows)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def write_output_csv(output_path: Path, rows: list[dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=REQUIRED_COLUMNS,
            lineterminator="\n",
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_stats_json(stats_path: Path, stats: dict[str, Any]) -> None:
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    with stats_path.open("w", encoding="utf-8") as handle:
        json.dump(stats, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main() -> int:
    args = parse_args()

    input_path = args.input.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    stats_path = (
        args.stats_output.expanduser().resolve()
        if args.stats_output is not None
        else output_path.with_name(output_path.stem + "_stats.json")
    )

    if not input_path.exists():
        print(f"Input CSV does not exist: {input_path}", file=sys.stderr)
        return 2
    if args.max_repeats < 0:
        print("--max-repeats must be 0 or a positive integer", file=sys.stderr)
        return 2
    if args.sample < 0:
        print("--sample must be 0 or a positive integer", file=sys.stderr)
        return 2

    try:
        raw_rows, _ = load_rows(input_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    deduped_rows, occurrence_counts = select_first_occurrence_per_key(raw_rows)
    filtered_rows, keys_dropped, rows_represented_by_dropped = apply_repeat_filter(
        deduped_rows, occurrence_counts, args.max_repeats
    )
    final_rows = apply_sample(filtered_rows, args.sample)

    write_output_csv(output_path, final_rows)

    stats = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_csv": str(input_path),
        "output_csv": str(output_path),
        "rows_read": len(raw_rows),
        "distinct_values": len(deduped_rows),
        "max_repeats": args.max_repeats,
        "distinct_values_dropped_over_max_repeats": keys_dropped,
        "raw_rows_represented_by_dropped_values": rows_represented_by_dropped,
        "distinct_values_after_repeat_filter": len(filtered_rows),
        "sample_requested": args.sample,
        "rows_written": len(final_rows),
        "by_category_after_repeat_filter": count_by_category(filtered_rows),
        "by_category_written": count_by_category(final_rows),
    }
    write_stats_json(stats_path, stats)

    print(f"Read {stats['rows_read']:,} rows from {input_path}")
    print(f"Distinct (category, value) pairs: {stats['distinct_values']:,}")
    if args.max_repeats > 0:
        print(
            f"Dropped {keys_dropped:,} value(s) repeated more than {args.max_repeats}x "
            f"({rows_represented_by_dropped:,} raw rows) -> {len(filtered_rows):,} remaining"
        )
    if args.sample > 0:
        print(f"Sampled down to {len(final_rows):,} row(s)")
    print(f"Wrote {len(final_rows):,} row(s) to {output_path}")
    print(f"Wrote stats to {stats_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
