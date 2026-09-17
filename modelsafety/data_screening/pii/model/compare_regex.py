#!/usr/bin/env python3
"""Compare standalone Privacy Filter spans with existing regex candidates."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from common import label_tier, write_json_atomic
from policy import CandidatePolicy


REGEX_LABEL_MAP = {
    "btc_addresses": "account_number",
    "credit_cards": "credit_debit_card",
    "emails": "email",
    "ips": "ipv4",
    "ipv6s": "ipv6",
    "phones": "phone_number",
    "phones_with_exts": "phone_number",
    "po_boxes": "street_address",
    "postcodes": "postcode",
    "street_addresses": "street_address",
    "zip_codes": "postcode",
}
MODEL_LABEL_MAP = {
    "private_email": "email",
    "private_phone": "phone_number",
    "private_address": "street_address",
    "private_person": "person_name",
    "private_url": "url",
    "private_date": "date_of_birth",
}
OUTPUT_FIELDS = ["file", "line", "label", "text", "source"]


def normalize_text(value: str) -> str:
    return " ".join(value.strip().split()).casefold()


def key(file: str, line: int, label: str, text: str) -> tuple[str, int, str, str]:
    return file, line, label, normalize_text(text)


def model_candidates(
    path: Path,
    *,
    eligible_only: bool = False,
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
                item_key = key(
                    str(row["source_file"]),
                    int(row["source_line"]),
                    MODEL_LABEL_MAP.get(str(row["label"]), str(row["label"])),
                    str(row["text"]),
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Malformed model span at {path}:{line_number}") from exc
            values[item_key] = {
                "file": item_key[0],
                "line": item_key[1],
                "label": item_key[2],
                "text": str(row["text"]),
                "source": "model",
            }
    return values


def regex_csvs(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
        return
    yield from sorted(path.rglob("pii_extract.csv"))


def regex_candidates(
    path: Path,
    allowed_rows: set[tuple[str, int]],
) -> dict[tuple[str, int, str, str], dict[str, object]]:
    values = {}
    csv.field_size_limit(sys.maxsize)
    files = list(regex_csvs(path))
    if not files:
        raise ValueError(f"No regex pii_extract.csv found under {path}")
    for csv_path in files:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                category = str(row.get("category") or "")
                label = REGEX_LABEL_MAP.get(category, category)
                source_row = (str(row["file"]), int(row["line"]))
                if source_row not in allowed_rows:
                    continue
                item_key = key(
                    source_row[0],
                    source_row[1],
                    label,
                    str(row["PII"]),
                )
                values[item_key] = {
                    "file": item_key[0],
                    "line": item_key[1],
                    "label": item_key[2],
                    "text": str(row["PII"]),
                    "source": "regex",
                }
    return values


def manifest_scope(path: Path) -> set[tuple[str, int]]:
    scope: set[tuple[str, int]] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                unit = json.loads(line)
                source_file = str(unit["source_file"])
                start_line = int(unit["start_line"])
                end_line = int(unit["end_line"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Malformed work unit at {path}:{line_number}") from exc
            scope.update(
                (source_file, source_line)
                for source_line in range(start_line, end_line + 1)
            )
    if not scope:
        raise ValueError(f"Work manifest contains no source rows: {path}")
    return scope


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
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def compare(
    model_path: Path,
    regex_path: Path,
    output_dir: Path,
    sample_size: int,
    work_manifest: Path | None = None,
    eligible_only: bool = False,
    policy_path: Path | None = None,
) -> dict[str, object]:
    policy = CandidatePolicy.from_path(policy_path) if policy_path else None
    model = model_candidates(
        model_path,
        eligible_only=eligible_only,
        policy=policy,
    )
    model_rows = {(item[0], item[1]) for item in model}
    if work_manifest is None:
        inferred = model_path.parent / "manifest" / "work_units.jsonl"
        work_manifest = inferred if inferred.is_file() else None
    scope = manifest_scope(work_manifest) if work_manifest else model_rows
    regex = regex_candidates(regex_path, scope)
    model_keys = set(model)
    regex_keys = set(regex)
    both_keys = model_keys & regex_keys
    model_only_keys = model_keys - regex_keys
    regex_only_keys = regex_keys - model_keys
    output_dir.mkdir(parents=True, exist_ok=True)

    both = [{**model[item], "source": "both"} for item in both_keys]
    model_only = [model[item] for item in model_only_keys]
    regex_only = [regex[item] for item in regex_only_keys]
    write_csv(output_dir / "overlap_sample.csv", deterministic_sample(both, sample_size))
    write_csv(
        output_dir / "model_only_sample.csv",
        deterministic_sample(model_only, sample_size),
    )
    write_csv(
        output_dir / "regex_only_sample.csv",
        deterministic_sample(regex_only, sample_size),
    )

    by_label = {}
    labels = sorted({item[2] for item in model_keys | regex_keys})
    for label in labels:
        by_label[label] = {
            "model": sum(item[2] == label for item in model_keys),
            "regex": sum(item[2] == label for item in regex_keys),
            "overlap": sum(item[2] == label for item in both_keys),
        }
    regex_rows = {(item[0], item[1]) for item in regex_keys}
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model_spans": len(model_keys),
        "model_scope": (
            f"policy_v{policy.version}"
            if policy is not None
            else ("candidate_eligible" if eligible_only else "all_spans")
        ),
        "regex_spans": len(regex_keys),
        "scope_rows": len(scope),
        "exact_overlap": len(both_keys),
        "model_only": len(model_only_keys),
        "regex_only": len(regex_only_keys),
        "model_rows": len(model_rows),
        "regex_rows": len(regex_rows),
        "row_overlap": len(model_rows & regex_rows),
        "by_label": by_label,
        "note": "This is an overlap report, not precision/recall; no ground truth is used.",
    }
    write_json_atomic(output_dir / "summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-spans", type=Path, required=True)
    parser.add_argument("--regex-input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--work-manifest", type=Path)
    parser.add_argument("--eligible-only", action="store_true")
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--sample-size", type=int, default=500)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        summary = compare(
            args.model_spans.expanduser().resolve(),
            args.regex_input.expanduser().resolve(),
            args.output_dir.expanduser().resolve(),
            args.sample_size,
            args.work_manifest.expanduser().resolve() if args.work_manifest else None,
            args.eligible_only,
            args.policy.expanduser().resolve() if args.policy else None,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        f"Model={summary['model_spans']:,}, regex={summary['regex_spans']:,}, "
        f"exact overlap={summary['exact_overlap']:,}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

