#!/usr/bin/env python3
"""Optionally confirm standalone Privacy Filter candidates with a local LLM."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request

from common import write_json_atomic


PROMPT_VERSION = 1
SYSTEM_PROMPT = """You are a strict privacy reviewer.
Decide whether the exact candidate is private-person PII that warrants removing
the source row. Public, business, institutional, fictional, placeholder, generic,
or merely demographic information is not private-person PII. The exact candidate
must occur in the supplied context.

Return only JSON with exactly these fields:
{"is_private_person_pii": true/false,
 "is_public_or_business_info": true/false,
 "exact_match_found": true/false,
 "decision": "keep"/"filter",
 "confidence": "low"/"medium"/"high",
 "reason": "concise explanation",
 "evidence": "brief supporting quote"}"""


def allow_large_csv_fields() -> None:
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def row_key(row: dict[str, str], model: str) -> str:
    payload = {
        "version": PROMPT_VERSION,
        "model": model,
        "file": row.get("file", ""),
        "line": row.get("line", ""),
        "category": row.get("category", ""),
        "PII": row.get("PII", ""),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def truncate_context(context: str, candidate: str, max_chars: int) -> str:
    if max_chars <= 0 or len(context) <= max_chars:
        return context
    marker = "\n[... context truncated ...]\n"
    keep = max(0, max_chars - 2 * len(marker))
    index = context.find(candidate)
    if index < 0:
        before = keep // 2
        return context[:before] + marker + context[-(keep - before) :]
    center = index + len(candidate) // 2
    start = max(0, min(center - keep // 2, len(context) - keep))
    end = start + keep
    return (marker if start else "") + context[start:end] + (
        marker if end < len(context) else ""
    )


def parse_json_response(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Response did not contain a JSON object")
        value = json.loads(content[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("Response JSON is not an object")
    return value


def normalize_decision(value: dict[str, Any]) -> dict[str, Any]:
    private = bool(value.get("is_private_person_pii"))
    public = bool(value.get("is_public_or_business_info"))
    exact = bool(value.get("exact_match_found"))
    keep = value.get("decision") == "keep" and private and exact and not public
    return {
        "is_private_person_pii": private,
        "is_public_or_business_info": public,
        "exact_match_found": exact,
        "decision": "keep" if keep else "filter",
        "confidence": str(value.get("confidence") or "low"),
        "reason": str(value.get("reason") or ""),
        "evidence": str(value.get("evidence") or ""),
    }


def call_api(
    *,
    base_url: str,
    model: str,
    row: dict[str, str],
    max_context_chars: int,
    max_tokens: int,
    timeout: float,
    retries: int,
) -> tuple[dict[str, Any], str]:
    context = truncate_context(
        row.get("whole line", ""),
        row.get("PII", ""),
        max_context_chars,
    )
    user_prompt = (
        f"Category: {row.get('category', '')}\n"
        f"Exact candidate: {json.dumps(row.get('PII', ''), ensure_ascii=False)}\n"
        f"Source context:\n{context}"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with request.urlopen(req, timeout=timeout) as response:
                response_value = json.loads(response.read().decode("utf-8"))
            content = str(response_value["choices"][0]["message"]["content"])
            return normalize_decision(parse_json_response(content)), content
        except (
            TimeoutError,
            error.HTTPError,
            error.URLError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
        ) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2**attempt)
    assert last_error is not None
    raise RuntimeError(str(last_error)) from last_error


def load_resume(paths: list[Path]) -> dict[str, dict[str, Any]]:
    existing: dict[str, dict[str, Any]] = {}
    for path in paths:
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Malformed resume output at {path}:{line_number}") from exc
                if row.get("status") == "ok":
                    existing[str(row["row_key"])] = row
    return existing


def validate(args: argparse.Namespace) -> int:
    allow_large_csv_fields()
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    results = output_dir / "decisions.jsonl"
    partial = output_dir / ".decisions.jsonl.partial"
    confirmed = output_dir / "confirmed_candidates.csv"
    confirmed_partial = output_dir / ".confirmed_candidates.csv.partial"
    existing = load_resume([results, partial]) if args.resume else {}
    summary: Counter[str] = Counter()

    with args.input.expanduser().resolve().open(
        "r", encoding="utf-8", newline=""
    ) as input_handle, partial.open("w", encoding="utf-8") as result_handle, (
        confirmed_partial.open("w", encoding="utf-8", newline="")
    ) as confirmed_handle:
        reader = csv.DictReader(input_handle)
        if reader.fieldnames is None:
            raise ValueError("Candidate CSV has no header")
        writer = csv.DictWriter(
            confirmed_handle,
            fieldnames=reader.fieldnames + ["llm_confidence", "llm_reason", "llm_evidence"],
            lineterminator="\n",
        )
        writer.writeheader()
        futures: dict[Future[tuple[dict[str, Any], str]], tuple[dict[str, str], str]] = {}

        def record(row: dict[str, str], key: str, record_value: dict[str, Any]) -> None:
            decision = record_value["decision"]
            result_handle.write(json.dumps(record_value, ensure_ascii=False) + "\n")
            result_handle.flush()
            summary["total"] += 1
            summary[decision["decision"]] += 1
            summary[record_value["status"]] += 1
            if decision["decision"] == "keep":
                writer.writerow(
                    {
                        **row,
                        "llm_confidence": decision["confidence"],
                        "llm_reason": decision["reason"],
                        "llm_evidence": decision["evidence"],
                    }
                )

        def finish_one() -> None:
            future = next(as_completed(tuple(futures)))
            row, key = futures.pop(future)
            try:
                decision, raw = future.result()
                status, message = "ok", ""
            except RuntimeError as exc:
                decision = {
                    "decision": "filter",
                    "confidence": "low",
                    "reason": "Validation failed",
                    "evidence": "",
                    "is_private_person_pii": False,
                    "is_public_or_business_info": False,
                    "exact_match_found": False,
                }
                raw, status, message = "", "error", str(exc)
            record(
                row,
                key,
                {
                    "row_key": key,
                    "status": status,
                    "error": message,
                    "file": row.get("file", ""),
                    "line": row.get("line", ""),
                    "category": row.get("category", ""),
                    "PII": row.get("PII", ""),
                    "decision": decision,
                    "raw_model_content": raw,
                },
            )

        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            for row in reader:
                normalized = {key: value or "" for key, value in row.items()}
                key = row_key(normalized, args.model)
                if key in existing:
                    record(normalized, key, existing[key])
                    continue
                future = executor.submit(
                    call_api,
                    base_url=args.base_url,
                    model=args.model,
                    row=normalized,
                    max_context_chars=args.max_context_chars,
                    max_tokens=args.max_tokens,
                    timeout=args.timeout,
                    retries=args.retries,
                )
                futures[future] = (normalized, key)
                if len(futures) >= args.max_pending:
                    finish_one()
            while futures:
                finish_one()
        os.fsync(result_handle.fileno())
        os.fsync(confirmed_handle.fileno())
    partial.replace(results)
    confirmed_partial.replace(confirmed)
    write_json_atomic(
        output_dir / "summary.json",
        {
            **dict(summary),
            "model": args.model,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--max-pending", type=int, default=256)
    parser.add_argument("--max-context-chars", type=int, default=10000)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_pending < args.concurrency:
        print("--max-pending must be at least --concurrency", file=sys.stderr)
        return 2
    try:
        return validate(args)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

