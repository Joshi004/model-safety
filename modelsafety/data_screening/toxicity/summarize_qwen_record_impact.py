#!/usr/bin/env python3
"""Summarize message-level Qwen3Guard results at source-record level."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


OUTCOMES = ("Safe", "Controversial", "Unsafe", "Error", "Unknown")


def outcome_of(row: dict[str, Any]) -> str:
    guard = row.get("guard") or {}
    if "error" in guard:
        return "Error"
    safety = guard.get("safety")
    return str(safety) if safety in OUTCOMES else "Unknown"


def record_key(row: dict[str, Any]) -> tuple[str, int]:
    return str(row["source_file"]), int(row["source_line"])


def load_excluded_locations(path: Path | None) -> set[tuple[str, int]]:
    if path is None:
        return set()
    locations = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
                locations.add((str(row["source_file"]), int(row["source_line"])))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid excluded location at {path}:{line_number}") from exc
    return locations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shards_root", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--initial-records", type=int, default=58_318_002)
    parser.add_argument(
        "--exclude-locations",
        type=Path,
        help="Optional JSONL source coordinates removed by an earlier filtering stage",
    )
    args = parser.parse_args()
    excluded_locations = load_excluded_locations(args.exclude_locations)

    shard_files = sorted(args.shards_root.glob("shard_*/guard_results/all.jsonl"))
    if len(shard_files) != 16:
        raise RuntimeError(f"Expected 16 completed guard shards, found {len(shard_files)}")

    message_outcomes: Counter[str] = Counter()
    outcomes_by_record: dict[tuple[str, int], Counter[str]] = defaultdict(Counter)
    message_indexes_by_record: dict[tuple[str, int], set[int | None]] = defaultdict(set)

    for path in shard_files:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"Invalid JSON at {path}:{line_number}") from exc
                key = record_key(row)
                outcome = outcome_of(row)
                message_outcomes[outcome] += 1
                outcomes_by_record[key][outcome] += 1
                message_indexes_by_record[key].add(row.get("message_index"))

    all_candidate_records = set(outcomes_by_record)
    controversial_records = {
        key for key, counts in outcomes_by_record.items() if counts["Controversial"]
    }
    unsafe_records = {
        key for key, counts in outcomes_by_record.items() if counts["Unsafe"]
    }
    error_records = {
        key for key, counts in outcomes_by_record.items() if counts["Error"]
    }
    nonsafe_or_error_records = controversial_records | unsafe_records | error_records
    unsafe_or_error_records = unsafe_records | error_records

    candidate_messages_per_record = Counter(
        sum(counts.values()) for counts in outcomes_by_record.values()
    )
    routed_messages_per_record = Counter(
        counts["Controversial"] + counts["Unsafe"] + counts["Error"]
        for counts in outcomes_by_record.values()
        if counts["Controversial"] + counts["Unsafe"] + counts["Error"]
    )
    multi_candidate_examples = sorted(
        (
            {
                "source_file": key[0],
                "source_line": key[1],
                "candidate_messages": sum(outcomes_by_record[key].values()),
                "distinct_message_indexes": len(message_indexes_by_record[key]),
                "outcomes": dict(outcomes_by_record[key]),
            }
            for key in all_candidate_records
            if sum(outcomes_by_record[key].values()) > 1
        ),
        key=lambda row: (-row["candidate_messages"], row["source_file"], row["source_line"]),
    )[:20]

    summary = {
        "initial_source_records": args.initial_records,
        "guard_message_candidates": sum(message_outcomes.values()),
        "message_outcomes": {
            outcome: message_outcomes[outcome] for outcome in OUTCOMES
        },
        "unique_source_records": {
            "with_any_detoxify_candidate": len(all_candidate_records),
            "controversial": len(controversial_records),
            "unsafe": len(unsafe_records),
            "error": len(error_records),
            "controversial_or_unsafe_or_error": len(nonsafe_or_error_records),
            "unsafe_or_error": len(unsafe_or_error_records),
            "unsafe_only_policy": len(unsafe_records),
        },
        "record_overlap": {
            "controversial_and_unsafe": len(controversial_records & unsafe_records),
            "controversial_and_error": len(controversial_records & error_records),
            "unsafe_and_error": len(unsafe_records & error_records),
            "all_three": len(
                controversial_records & unsafe_records & error_records
            ),
        },
        "earlier_stage_exclusions": {
            "excluded_source_records": len(excluded_locations),
            "also_rejected_by_toxicity": len(
                excluded_locations & nonsafe_or_error_records
            ),
            "new_toxicity_rejections": len(
                nonsafe_or_error_records - excluded_locations
            ),
        },
        "candidate_messages_per_source_record": {
            str(count): records
            for count, records in sorted(candidate_messages_per_record.items())
        },
        "routed_messages_per_source_record": {
            str(count): records
            for count, records in sorted(routed_messages_per_record.items())
        },
        "hypothetical_remaining_records": {
            "reject_controversial_unsafe_error": (
                args.initial_records - len(nonsafe_or_error_records)
            ),
            "reject_unsafe_error": args.initial_records - len(unsafe_or_error_records),
            "reject_unsafe_only": args.initial_records - len(unsafe_records),
        },
        "multi_candidate_record_examples": multi_candidate_examples,
        "note": (
            "Source records are uniquely identified by (source_file, source_line). "
            "A conversation record can contain multiple messages, each with its own "
            "message_index and Detoxify/Qwen result."
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
