#!/usr/bin/env python3
"""Compare two Privacy Filter span artifacts on the same manifest scope."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from common import label_tier, write_json_atomic
from policy import CandidatePolicy


CATEGORY_MAP = {
    "private_address": "address",
    "street_address": "address",
    "postcode": "address",
    "coordinate": "address",
    "private_date": "date",
    "date_of_birth": "date",
    "private_email": "email",
    "email": "email",
    "private_person": "person",
    "first_name": "person",
    "last_name": "person",
    "private_phone": "phone",
    "phone_number": "phone",
    "fax_number": "phone",
    "private_url": "url",
    "url": "url",
    "user_name": "account",
    "account_number": "account",
    "password": "secret",
    "pin": "secret",
    "cvv": "secret",
    "secret": "secret",
}
FIELDS = ["file", "line", "category", "label", "text", "source"]


def normalize_text(value: str) -> str:
    return " ".join(value.strip().split()).casefold()


def canonical_category(label: str) -> str:
    return CATEGORY_MAP.get(label, label)


def load_spans(
    path: Path,
    *,
    eligible_only: bool,
    source_name: str,
    policy: CandidatePolicy | None = None,
) -> dict[tuple[str, int, str, str], dict[str, object]]:
    values = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if policy is not None:
                    if row.get("candidate_excluded_reason") == "control_markup":
                        continue
                    span_text = str(row["text"])
                    decision = policy.evaluate(
                        label=str(row["label"]),
                        tier=label_tier(str(row["label"])),
                        confidence=float(row["confidence"]),
                        span_text=span_text,
                        source_text=span_text,
                        start=0,
                        end=len(span_text),
                    )
                    if not decision.eligible:
                        continue
                elif eligible_only and row.get("candidate_eligible") is not True:
                    continue
                label = str(row["label"])
                item_key = (
                    str(row["source_file"]),
                    int(row["source_line"]),
                    canonical_category(label),
                    normalize_text(str(row["text"])),
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Malformed span at {path}:{line_number}") from exc
            values[item_key] = {
                "file": item_key[0],
                "line": item_key[1],
                "category": item_key[2],
                "label": label,
                "text": str(row["text"]),
                "source": source_name,
            }
    return values


def deterministic_sample(
    rows: Iterable[dict[str, object]],
    limit: int,
) -> list[dict[str, object]]:
    return sorted(
        rows,
        key=lambda row: hashlib.sha256(
            json.dumps(row, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    )[:limit]


def write_csv(path: Path, rows: Iterable[dict[str, object]]) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.partial")
    with temp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def compare(
    left_path: Path,
    right_path: Path,
    output_dir: Path,
    *,
    left_name: str,
    right_name: str,
    eligible_only: bool,
    sample_size: int,
    policy_path: Path | None = None,
) -> dict[str, object]:
    policy = CandidatePolicy.from_path(policy_path) if policy_path else None
    left = load_spans(
        left_path,
        eligible_only=eligible_only,
        source_name=left_name,
        policy=policy,
    )
    right = load_spans(
        right_path,
        eligible_only=eligible_only,
        source_name=right_name,
        policy=policy,
    )
    left_keys = set(left)
    right_keys = set(right)
    overlap_keys = left_keys & right_keys
    left_only_keys = left_keys - right_keys
    right_only_keys = right_keys - left_keys
    left_rows = {(key[0], key[1]) for key in left_keys}
    right_rows = {(key[0], key[1]) for key in right_keys}
    output_dir.mkdir(parents=True, exist_ok=True)

    write_csv(
        output_dir / f"{left_name}_only_sample.csv",
        deterministic_sample((left[key] for key in left_only_keys), sample_size),
    )
    write_csv(
        output_dir / f"{right_name}_only_sample.csv",
        deterministic_sample((right[key] for key in right_only_keys), sample_size),
    )
    write_csv(
        output_dir / "exact_overlap_sample.csv",
        deterministic_sample(
            ({**left[key], "source": "both"} for key in overlap_keys),
            sample_size,
        ),
    )

    categories = sorted({key[2] for key in left_keys | right_keys})
    by_category = {
        category: {
            left_name: sum(key[2] == category for key in left_keys),
            right_name: sum(key[2] == category for key in right_keys),
            "exact_overlap": sum(key[2] == category for key in overlap_keys),
        }
        for category in categories
    }
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": (
            f"policy_v{policy.version}"
            if policy is not None
            else ("candidate_eligible" if eligible_only else "all_spans")
        ),
        "left": left_name,
        "right": right_name,
        "left_spans": len(left_keys),
        "right_spans": len(right_keys),
        "exact_span_overlap": len(overlap_keys),
        "left_only": len(left_only_keys),
        "right_only": len(right_only_keys),
        "left_rows": len(left_rows),
        "right_rows": len(right_rows),
        "row_overlap": len(left_rows & right_rows),
        "left_labels": dict(sorted(Counter(row["label"] for row in left.values()).items())),
        "right_labels": dict(
            sorted(Counter(row["label"] for row in right.values()).items())
        ),
        "by_category": by_category,
        "note": (
            "Overlap is not precision/recall. Selecting the better model requires "
            "manual or ground-truth review of the model-only samples."
        ),
    }
    write_json_atomic(output_dir / "summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left-spans", type=Path, required=True)
    parser.add_argument("--right-spans", type=Path, required=True)
    parser.add_argument("--left-name", default="nemotron")
    parser.add_argument("--right-name", default="openai")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--eligible-only", action="store_true")
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--sample-size", type=int, default=500)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = compare(
        args.left_spans.expanduser().resolve(),
        args.right_spans.expanduser().resolve(),
        args.output_dir.expanduser().resolve(),
        left_name=args.left_name,
        right_name=args.right_name,
        eligible_only=args.eligible_only,
        sample_size=args.sample_size,
        policy_path=args.policy.expanduser().resolve() if args.policy else None,
    )
    print(
        f"{summary['left']}={summary['left_spans']:,}, "
        f"{summary['right']}={summary['right_spans']:,}, "
        f"exact overlap={summary['exact_span_overlap']:,}, "
        f"row overlap={summary['row_overlap']:,}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
