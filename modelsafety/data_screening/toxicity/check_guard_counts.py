#!/usr/bin/env python3
"""Check that stage 2 judged every message stage 1 flagged."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# The verdicts llm_check_guard.py counts as recognised; everything else it
# saw is in its "unknown" count.
RECOGNISED_VERDICTS = ("Safe", "Unsafe", "Controversial")


def load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"{label} is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not a JSON object: {path}")
    return value


def count_lines(path: Path) -> int:
    if not path.is_file():
        raise ValueError(f"Flagged file is missing: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def require_count(summary: dict[str, Any], key: str, label: str) -> int:
    value = summary.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} has no whole-number '{key}'")
    return value


def check_guard_counts(stage1_summary: Path, flagged: Path, guard_summary: Path) -> list[str]:
    """Return what doesn't add up. An empty list means every flagged message was judged."""
    stage1 = load_json(stage1_summary, "Stage 1 summary")
    guard = load_json(guard_summary, "Stage 2 summary")
    expected = require_count(stage1, "messages_flagged", "Stage 1 summary")
    total_records = require_count(guard, "total_records", "Stage 2 summary")
    errors = require_count(guard, "errors", "Stage 2 summary")
    unknown = require_count(guard, "unknown", "Stage 2 summary")
    safety = guard.get("safety")
    if not isinstance(safety, dict):
        raise ValueError("Stage 2 summary has no 'safety' counts")
    judged = sum(int(safety.get(verdict, 0)) for verdict in RECOGNISED_VERDICTS)

    problems = []
    flagged_lines = count_lines(flagged)
    if flagged_lines != expected:
        problems.append(
            f"{flagged} has {flagged_lines} records but stage 1 flagged {expected}"
        )
    if total_records != expected:
        problems.append(f"stage 2 read {total_records} records but stage 1 flagged {expected}")
    if judged + errors + unknown != expected:
        problems.append(
            f"stage 2 verdicts ({judged}) + errors ({errors}) + unknown ({unknown}) "
            f"= {judged + errors + unknown}, but stage 1 flagged {expected}"
        )
    return problems


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage1-summary", type=Path, required=True)
    parser.add_argument("--flagged", type=Path, required=True)
    parser.add_argument("--guard-summary", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        problems = check_guard_counts(args.stage1_summary, args.flagged, args.guard_summary)
    except ValueError as exc:
        print(f"Stage 2 check failed: {exc}", file=sys.stderr)
        return 1
    if problems:
        for problem in problems:
            print(f"Stage 2 check failed: {problem}", file=sys.stderr)
        return 1
    print("Stage 2 check passed: every flagged message was judged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
