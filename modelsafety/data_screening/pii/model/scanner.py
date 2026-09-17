#!/usr/bin/env python3
"""Standalone resumable Privacy Filter scanner and result aggregator."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import socket
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Any, Iterator

from backends import DEFAULT_MODEL, Backend, create_backend
from common import (
    WorkUnit,
    iter_record_texts,
    iter_unit_rows,
    label_tier,
    load_work_units,
    write_json_atomic,
)
from policy import CandidatePolicy


CANDIDATE_FIELDS = [
    "file",
    "line",
    "category",
    "PII",
    "whole line",
    "message_index",
    "role",
    "start",
    "end",
    "confidence",
    "tier",
]


def batched(iterator: Iterator[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    while batch := list(islice(iterator, size)):
        yield batch


def claim_unit(
    claims_dir: Path,
    units_dir: Path,
    unit: WorkUnit,
    stale_seconds: int,
) -> Path | None:
    success = units_dir / unit.unit_id / "_SUCCESS"
    if success.is_file():
        return None
    claims_dir.mkdir(parents=True, exist_ok=True)
    claim = claims_dir / f"{unit.unit_id}.claim"
    if claim.exists() and stale_seconds > 0:
        age = time.time() - claim.stat().st_mtime
        if age > stale_seconds:
            claim.unlink(missing_ok=True)
    try:
        descriptor = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            handle,
        )
        handle.write("\n")
    return claim


def clear_claims(output_root: Path) -> int:
    claims_dir = output_root.expanduser().resolve() / "claims"
    removed = 0
    if claims_dir.is_dir():
        for claim in claims_dir.glob("*.claim"):
            claim.unlink()
            removed += 1
    print(f"Cleared {removed} stale work claims")
    return 0


def candidate_key(row: dict[str, Any], label: str, text: str) -> str:
    payload = "\0".join(
        (
            row["source_file"],
            str(row["source_line"]),
            label,
            text,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def process_unit(
    input_root: Path,
    output_root: Path,
    unit: WorkUnit,
    backend: Backend,
    policy: CandidatePolicy,
    input_batch_size: int,
) -> dict[str, Any]:
    unit_dir = output_root / "units" / unit.unit_id
    unit_dir.mkdir(parents=True, exist_ok=True)
    spans_final = output_root / "spans" / "units" / f"{unit.unit_id}.jsonl"
    candidates_final = output_root / "candidates" / "units" / f"{unit.unit_id}.csv"
    spans_final.parent.mkdir(parents=True, exist_ok=True)
    candidates_final.parent.mkdir(parents=True, exist_ok=True)
    for stale in spans_final.parent.glob(f".{unit.unit_id}.*.partial"):
        stale.unlink()
    for stale in candidates_final.parent.glob(f".{unit.unit_id}.*.partial"):
        stale.unlink()
    spans_temp = spans_final.with_name(f".{unit.unit_id}.{os.getpid()}.partial")
    candidates_temp = candidates_final.with_name(
        f".{unit.unit_id}.{os.getpid()}.partial"
    )
    started = time.monotonic()
    rows_seen = 0
    messages = 0
    span_count = 0
    candidate_count = 0
    eligible_span_count = 0
    labels: Counter[str] = Counter()
    tiers: Counter[str] = Counter()
    policy_exclusions: Counter[str] = Counter()
    emitted_candidates: set[str] = set()
    last_progress = started

    def records() -> Iterator[dict[str, Any]]:
        nonlocal rows_seen
        for row in iter_unit_rows(input_root, unit):
            rows_seen += 1
            yield from iter_record_texts(row)

    try:
        with spans_temp.open("w", encoding="utf-8") as spans_handle, (
            candidates_temp.open("w", encoding="utf-8", newline="")
        ) as candidates_handle:
            candidate_writer = csv.DictWriter(
                candidates_handle,
                fieldnames=CANDIDATE_FIELDS,
                lineterminator="\n",
            )
            candidate_writer.writeheader()
            for batch in batched(records(), input_batch_size):
                predictions = backend.predict([row["text"] for row in batch])
                if len(predictions) != len(batch):
                    raise RuntimeError("Backend returned an unexpected prediction count")
                messages += len(batch)
                for row, spans in zip(batch, predictions):
                    for span in spans:
                        tier = label_tier(span.label)
                        policy_decision = policy.evaluate(
                            label=span.label,
                            tier=tier,
                            confidence=span.confidence,
                            span_text=span.text,
                            source_text=row["text"],
                            start=span.start,
                            end=span.end,
                        )
                        labels[span.label] += 1
                        tiers[tier] += 1
                        span_count += 1
                        if policy_decision.eligible:
                            eligible_span_count += 1
                        else:
                            policy_exclusions[
                                policy_decision.excluded_reason or "unknown"
                            ] += 1
                        output = {
                            "unit_id": unit.unit_id,
                            "source_file": row["source_file"],
                            "source_line": row["source_line"],
                            "message_index": row["message_index"],
                            "role": row["role"],
                            **span.to_dict(),
                            "tier": tier,
                            "model": backend.model_name,
                            "model_revision": getattr(
                                backend,
                                "model_revision",
                                "unknown",
                            ),
                            "backend": backend.backend_name,
                            "candidate_eligible": policy_decision.eligible,
                            "candidate_threshold": policy_decision.threshold,
                            "candidate_excluded_reason": (
                                policy_decision.excluded_reason
                            ),
                            "policy_version": policy.version,
                        }
                        spans_handle.write(
                            json.dumps(output, ensure_ascii=False, sort_keys=True) + "\n"
                        )
                        if not policy_decision.eligible:
                            continue
                        key = candidate_key(row, span.label, span.text)
                        if key in emitted_candidates:
                            continue
                        emitted_candidates.add(key)
                        candidate_count += 1
                        candidate_writer.writerow(
                            {
                                "file": row["source_file"],
                                "line": row["source_line"],
                                "category": span.label,
                                "PII": span.text,
                                "whole line": row["whole_line"],
                                "message_index": row["message_index"],
                                "role": row["role"],
                                "start": span.start,
                                "end": span.end,
                                "confidence": f"{span.confidence:.8f}",
                                "tier": tier,
                            }
                        )
                spans_handle.flush()
                candidates_handle.flush()
                now = time.monotonic()
                if now - last_progress >= 60:
                    elapsed = now - started
                    print(
                        f"{unit.unit_id}: {messages:,} messages, "
                        f"{span_count:,} spans, {messages / elapsed:.2f} messages/s",
                        flush=True,
                    )
                    last_progress = now
            os.fsync(spans_handle.fileno())
            os.fsync(candidates_handle.fileno())
        spans_temp.replace(spans_final)
        candidates_temp.replace(candidates_final)
    except BaseException:
        spans_temp.unlink(missing_ok=True)
        candidates_temp.unlink(missing_ok=True)
        raise

    elapsed = time.monotonic() - started
    summary = {
        "unit": unit.to_dict(),
        "model": backend.model_name,
        "model_revision": getattr(backend, "model_revision", "unknown"),
        "backend": backend.backend_name,
        "rows": rows_seen,
        "messages": messages,
        "spans": span_count,
        "policy_eligible_spans": eligible_span_count,
        "llm_candidates": candidate_count,
        "labels": dict(sorted(labels.items())),
        "tiers": dict(sorted(tiers.items())),
        "policy_exclusions": dict(sorted(policy_exclusions.items())),
        "policy_version": policy.version,
        "elapsed_seconds": elapsed,
        "messages_per_second": messages / elapsed if elapsed else 0.0,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json_atomic(unit_dir / "summary.json", summary)
    write_json_atomic(unit_dir / "_SUCCESS", {"completed_at": summary["completed_at"]})
    return summary


def run_worker(args: argparse.Namespace) -> int:
    input_root = args.input_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    units = load_work_units(args.work_manifest.expanduser().resolve())
    if args.max_units:
        units = units[: args.max_units]
    backend = create_backend(
        args.backend,
        args.model,
        batch_size=args.model_batch_size,
        max_tokens=args.max_tokens,
        overlap_tokens=args.overlap_tokens,
    )
    policy = CandidatePolicy.from_path(args.policy.expanduser().resolve())
    completed = 0
    for unit in units:
        claim = claim_unit(
            output_root / "claims",
            output_root / "units",
            unit,
            args.reclaim_stale_seconds,
        )
        if claim is None:
            continue
        try:
            summary = process_unit(
                input_root,
                output_root,
                unit,
                backend,
                policy,
                args.input_batch_size,
            )
            completed += 1
            print(
                f"{unit.unit_id}: {summary['messages']:,} messages, "
                f"{summary['spans']:,} spans, "
                f"{summary['messages_per_second']:.2f} messages/s",
                flush=True,
            )
        except BaseException as exc:
            write_json_atomic(
                output_root / "units" / unit.unit_id / "error.json",
                {
                    "unit": unit.to_dict(),
                    "error": f"{type(exc).__name__}: {exc}",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            raise
        finally:
            claim.unlink(missing_ok=True)
    print(f"Worker completed {completed} work units", flush=True)
    return 0


def aggregate(args: argparse.Namespace) -> int:
    output_root = args.output_root.expanduser().resolve()
    units = load_work_units(args.work_manifest.expanduser().resolve())
    if args.max_units:
        units = units[: args.max_units]
    missing = [
        unit.unit_id
        for unit in units
        if not (output_root / "units" / unit.unit_id / "_SUCCESS").is_file()
    ]
    if missing and not args.allow_incomplete:
        raise RuntimeError(f"{len(missing)} work units are incomplete")

    spans_temp = output_root / ".all_spans.jsonl.partial"
    candidates_temp = output_root / ".llm_candidates.csv.partial"
    spans_final = output_root / "all_spans.jsonl"
    candidates_final = output_root / "llm_candidates.csv"
    totals: Counter[str] = Counter()
    labels: Counter[str] = Counter()
    tiers: Counter[str] = Counter()
    policy_exclusions: Counter[str] = Counter()
    sources: dict[str, Counter[str]] = {}
    candidate_hashes: set[str] = set()
    output_root.mkdir(parents=True, exist_ok=True)

    with spans_temp.open("w", encoding="utf-8") as spans_output, (
        candidates_temp.open("w", encoding="utf-8", newline="")
    ) as candidates_output:
        writer = csv.DictWriter(
            candidates_output,
            fieldnames=CANDIDATE_FIELDS,
            lineterminator="\n",
        )
        writer.writeheader()
        for unit in units:
            unit_dir = output_root / "units" / unit.unit_id
            if not (unit_dir / "_SUCCESS").is_file():
                continue
            summary = json.loads((unit_dir / "summary.json").read_text(encoding="utf-8"))
            for key in (
                "rows",
                "messages",
                "spans",
                "policy_eligible_spans",
                "llm_candidates",
            ):
                totals[key] += int(summary[key])
            labels.update(summary["labels"])
            tiers.update(summary["tiers"])
            policy_exclusions.update(summary["policy_exclusions"])
            source_file = str(summary["unit"]["source_file"])
            source_summary = sources.setdefault(source_file, Counter())
            for key in (
                "rows",
                "messages",
                "spans",
                "policy_eligible_spans",
                "llm_candidates",
            ):
                source_summary[key] += int(summary[key])
            with (output_root / "spans" / "units" / f"{unit.unit_id}.jsonl").open(
                "r",
                encoding="utf-8",
            ) as handle:
                for line in handle:
                    spans_output.write(line)
            with (output_root / "candidates" / "units" / f"{unit.unit_id}.csv").open(
                "r",
                encoding="utf-8",
                newline="",
            ) as handle:
                for row in csv.DictReader(handle):
                    key = hashlib.sha256(
                        "\0".join(
                            (
                                row["file"],
                                row["line"],
                                row["category"],
                                row["PII"],
                            )
                        ).encode("utf-8")
                    ).hexdigest()
                    if key in candidate_hashes:
                        continue
                    candidate_hashes.add(key)
                    writer.writerow(row)
        spans_output.flush()
        candidates_output.flush()
        os.fsync(spans_output.fileno())
        os.fsync(candidates_output.fileno())
    spans_temp.replace(spans_final)
    candidates_temp.replace(candidates_final)
    totals["llm_candidates"] = len(candidate_hashes)
    summary = {
        **dict(totals),
        "labels": dict(sorted(labels.items())),
        "tiers": dict(sorted(tiers.items())),
        "policy_exclusions": dict(sorted(policy_exclusions.items())),
        "sources": {
            source_file: dict(values)
            for source_file, values in sorted(sources.items())
        },
        "completed_units": len(units) - len(missing),
        "expected_units": len(units),
        "missing_units": missing,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json_atomic(output_root / "summary.json", summary)
    print(
        f"Aggregated {summary['completed_units']}/{summary['expected_units']} units, "
        f"{summary.get('spans', 0):,} spans"
    )
    return 0


def probe(args: argparse.Namespace) -> int:
    backend = create_backend(
        args.backend,
        args.model,
        batch_size=args.model_batch_size,
        max_tokens=args.max_tokens,
        overlap_tokens=args.overlap_tokens,
    )
    texts = [
        "Patient Sarah Johnson, MRN 4872910, can be reached at sarah@example.com.",
        "The clinic is located in Boston and opens at 09:00.",
    ]
    outputs = backend.predict(texts)
    print(
        json.dumps(
            [[span.to_dict() for span in spans] for spans in outputs],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def add_backend_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend", choices=["transformers", "vllm", "openmed"], required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--model-batch-size", type=int, default=16)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--overlap-tokens", type=int, default=256)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    worker = subparsers.add_parser("worker")
    worker.add_argument("--input-root", type=Path, required=True)
    worker.add_argument("--work-manifest", type=Path, required=True)
    worker.add_argument("--output-root", type=Path, required=True)
    worker.add_argument("--input-batch-size", type=int, default=64)
    worker.add_argument("--max-units", type=int, default=0)
    worker.add_argument("--reclaim-stale-seconds", type=int, default=21600)
    worker.add_argument(
        "--policy",
        type=Path,
        default=Path(__file__).with_name("policy.json"),
    )
    add_backend_args(worker)

    aggregate_parser = subparsers.add_parser("aggregate")
    aggregate_parser.add_argument("--work-manifest", type=Path, required=True)
    aggregate_parser.add_argument("--output-root", type=Path, required=True)
    aggregate_parser.add_argument("--max-units", type=int, default=0)
    aggregate_parser.add_argument("--allow-incomplete", action="store_true")

    probe_parser = subparsers.add_parser("probe")
    add_backend_args(probe_parser)
    clear_parser = subparsers.add_parser("clear-claims")
    clear_parser.add_argument("--output-root", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "worker":
            if args.input_batch_size < 1:
                raise ValueError("--input-batch-size must be positive")
            return run_worker(args)
        if args.command == "aggregate":
            return aggregate(args)
        if args.command == "probe":
            return probe(args)
        if args.command == "clear-claims":
            return clear_claims(args.output_root)
        raise ValueError(f"Unknown command: {args.command}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

