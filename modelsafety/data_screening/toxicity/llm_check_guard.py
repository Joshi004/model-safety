"""Classify conversations from JSONL using Qwen3Guard-Gen via an OpenAI-compatible endpoint.

For each input record we:
  1. Locate the flagged message inside `conversation` using the `text` field
     (formatted as ``"<role>: <content>"``).
  2. Truncate the conversation to ``[0 .. flagged_index]`` (inclusive).
  3. Send that slice to a Qwen3Guard endpoint.
  4. Parse the model's response (Safety / Categories / Refusal) and write the
     original record back out with an added ``guard`` block.

The script talks to any OpenAI-compatible chat-completions endpoint
(e.g. vLLM or SGLang serving Qwen/Qwen3Guard-Gen-8B).
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re
import sys
from collections import Counter
from typing import Any

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from tqdm import tqdm


SAFETY_RE = re.compile(r"Safety:\s*(Safe|Unsafe|Controversial)")
CATEGORY_RE = re.compile(
    r"(Violent|Non-violent Illegal Acts|Sexual Content or Sexual Acts|PII|"
    r"Suicide & Self-Harm|Unethical Acts|Politically Sensitive Topics|"
    r"Copyright Violation|Jailbreak|None)"
)
REFUSAL_RE = re.compile(r"Refusal:\s*(Yes|No)")

RETRYABLE_EXC = (
    RateLimitError,
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
)


def parse_guard(raw: str, last_role: str) -> dict[str, Any]:
    """Parse a Qwen3Guard response string into structured fields."""
    safety_match = SAFETY_RE.search(raw)
    categories = list(dict.fromkeys(CATEGORY_RE.findall(raw)))
    out: dict[str, Any] = {
        "safety": safety_match.group(1) if safety_match else None,
        "categories": categories,
        "raw": raw,
    }
    if last_role == "assistant":
        refusal_match = REFUSAL_RE.search(raw)
        out["refusal"] = refusal_match.group(1) if refusal_match else None
    else:
        out["refusal"] = None
    return out


def locate_flagged(conversation: list[dict[str, Any]], text: str) -> tuple[int | None, str]:
    """Find the index of the flagged message in ``conversation``.

    Returns ``(index, match_kind)`` where ``match_kind`` is one of
    ``"exact"``, ``"fallback"``, or ``"missing"``.
    """
    if not conversation:
        return None, "missing"

    role, sep, content = text.partition(": ")
    if not sep:
        role, content = "", text

    if role in {"user", "assistant", "system", "tool"}:
        for i in range(len(conversation) - 1, -1, -1):
            m = conversation[i]
            if m.get("role") == role and m.get("content") == content:
                return i, "exact"

    fallback_role = role if role in {"user", "assistant"} else None
    if fallback_role is None:
        fallback_role = "assistant" if text.startswith("assistant:") else "user"

    for i in range(len(conversation) - 1, -1, -1):
        if conversation[i].get("role") == fallback_role:
            return i, "fallback"

    return None, "missing"


async def classify(
    client: AsyncOpenAI,
    messages: list[dict[str, Any]],
    model: str,
    max_tokens: int,
    temperature: float,
    max_retries: int,
) -> str:
    """Call the chat-completions endpoint with retry/backoff. Returns raw content."""
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return resp.choices[0].message.content or ""
        except RETRYABLE_EXC as e:
            last_exc = e
            if attempt == max_retries:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
        except APIError as e:
            raise e
    assert last_exc is not None
    raise last_exc


async def process_record(
    record: dict[str, Any],
    client: AsyncOpenAI,
    sem: asyncio.Semaphore,
    model: str,
    max_tokens: int,
    temperature: float,
    max_retries: int,
) -> dict[str, Any]:
    """Process one input record: locate flagged message, classify, attach guard block."""
    source_record = record.get("source_record")
    conversation = []
    if isinstance(source_record, dict) and isinstance(source_record.get("messages"), list):
        conversation = source_record["messages"]
    elif isinstance(record.get("conversation"), list):
        conversation = record["conversation"]

    text = record.get("text", "")
    source_index = record.get("message_index")
    if isinstance(source_index, int) and 0 <= source_index < len(conversation):
        idx, match_kind = source_index, "source_index"
    elif not conversation and isinstance(source_record, dict) and isinstance(
        source_record.get("text"), str
    ):
        conversation = [{"role": record.get("role") or "user", "content": source_record["text"]}]
        idx, match_kind = 0, "source_text"
    else:
        idx, match_kind = locate_flagged(conversation, text)

    if idx is None:
        record["guard"] = {"error": "flagged message not found"}
        return record

    msgs = conversation[: idx + 1]
    last_role = msgs[-1].get("role", "")

    try:
        async with sem:
            raw = await classify(
                client=client,
                messages=msgs,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                max_retries=max_retries,
            )
    except Exception as e:
        record["guard"] = {
            "error": f"{type(e).__name__}: {e}",
            "flagged_role": last_role,
            "flagged_index": idx,
        }
        return record

    guard = parse_guard(raw, last_role=last_role)
    guard["flagged_role"] = last_role
    guard["flagged_index"] = idx
    if match_kind != "exact":
        guard["match"] = match_kind
    record["guard"] = guard
    return record


def iter_jsonl(path: str):
    with open(path, "r") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                print(f"[warn] {path}:{ln}: invalid JSON ({e}); skipping", file=sys.stderr)


def resolve_inputs(input_arg: str) -> list[str]:
    """Expand ``input_arg`` into a list of jsonl files."""
    if os.path.isdir(input_arg):
        files = sorted(glob.glob(os.path.join(input_arg, "*.jsonl")))
        if not files:
            raise SystemExit(f"No .jsonl files found in directory: {input_arg}")
        return files
    matches = glob.glob(input_arg)
    if matches:
        return sorted(matches)
    if os.path.exists(input_arg):
        return [input_arg]
    raise SystemExit(f"Input not found: {input_arg}")


SAFETY_BUCKETS = ("safe", "unsafe", "controversial")


def open_output_files(output_dir: str) -> dict[str, Any]:
    """Create the output directory and open the four destination files."""
    os.makedirs(output_dir, exist_ok=True)
    handles = {"all": open(os.path.join(output_dir, "all.jsonl"), "w")}
    for bucket in SAFETY_BUCKETS:
        handles[bucket] = open(os.path.join(output_dir, f"{bucket}.jsonl"), "w")
    return handles


def route_bucket(record: dict[str, Any]) -> str | None:
    """Return the per-safety bucket name for a record, or None."""
    guard = record.get("guard") or {}
    safety = (guard.get("safety") or "").lower()
    return safety if safety in SAFETY_BUCKETS else None


def print_summary(safety_counts: Counter, category_counts: Counter, error_count: int, total: int) -> None:
    print()
    print(f"Processed {total} records.")
    print("Safety distribution:")
    for label in ("Safe", "Controversial", "Unsafe"):
        n = safety_counts.get(label, 0)
        pct = (n / total * 100) if total else 0.0
        print(f"  {label:13s}: {n:6d} ({pct:5.2f}%)")
    unknown = total - sum(safety_counts.get(k, 0) for k in ("Safe", "Controversial", "Unsafe")) - error_count
    if unknown:
        print(f"  {'Unknown':13s}: {unknown:6d}")
    if error_count:
        print(f"  {'Error':13s}: {error_count:6d}")
    if category_counts:
        print("Category occurrences (across all flagged records):")
        for cat, n in category_counts.most_common():
            print(f"  {cat:38s}: {n}")


async def run_file(
    input_path: str,
    handles: dict[str, Any],
    client: AsyncOpenAI,
    model: str,
    concurrency: int,
    max_tokens: int,
    temperature: float,
    max_retries: int,
) -> tuple[Counter, Counter, int, int]:
    sem = asyncio.Semaphore(concurrency)
    safety_counts: Counter = Counter()
    category_counts: Counter = Counter()
    error_count = 0
    total = 0
    pending: set[asyncio.Task] = set()
    max_pending = max(concurrency * 4, concurrency)

    def record_result(rec: dict[str, Any]) -> None:
        nonlocal error_count, total
        total += 1
        guard = rec.get("guard") or {}
        if "error" in guard:
            error_count += 1
        else:
            safety = guard.get("safety")
            if safety:
                safety_counts[safety] += 1
            for category in guard.get("categories", []) or []:
                if category != "None":
                    category_counts[category] += 1
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        bucket = route_bucket(rec)
        handles["all"].write(line)
        handles["all"].flush()
        if bucket is not None:
            handles[bucket].write(line)
            handles[bucket].flush()

    async def drain_completed(*, all_pending: bool = False) -> None:
        if not pending:
            return
        return_when = asyncio.ALL_COMPLETED if all_pending else asyncio.FIRST_COMPLETED
        done, still_pending = await asyncio.wait(pending, return_when=return_when)
        pending.clear()
        pending.update(still_pending)
        for task in done:
            record_result(task.result())
            pbar.update(1)

    with tqdm(desc=os.path.basename(input_path), unit="record") as pbar:
        for record in iter_jsonl(input_path):
            pending.add(
                asyncio.create_task(
                    process_record(
                        record,
                        client,
                        sem,
                        model,
                        max_tokens,
                        temperature,
                        max_retries,
                    )
                )
            )
            if len(pending) >= max_pending:
                await drain_completed()
        await drain_completed(all_pending=True)

    return safety_counts, category_counts, error_count, total


async def main_async(args: argparse.Namespace) -> None:
    inputs = resolve_inputs(args.input)
    client = AsyncOpenAI(
        base_url=args.base_url,
        api_key=args.api_key,
        timeout=args.request_timeout,
        max_retries=0,
    )

    total_safety: Counter = Counter()
    total_categories: Counter = Counter()
    total_errors = 0
    total_records = 0

    handles = open_output_files(args.output)
    print(f"Writing results to {args.output}/{{all,safe,unsafe,controversial}}.jsonl")

    try:
        for input_path in inputs:
            print(f"\n=> {input_path}")
            s, c, e, n = await run_file(
                input_path=input_path,
                handles=handles,
                client=client,
                model=args.model,
                concurrency=args.concurrency,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                max_retries=args.max_retries,
            )
            total_safety.update(s)
            total_categories.update(c)
            total_errors += e
            total_records += n
    finally:
        for f in handles.values():
            try:
                f.close()
            except Exception:
                pass
        await client.close()

    print_summary(total_safety, total_categories, total_errors, total_records)
    recognized = sum(total_safety.get(label, 0) for label in ("Safe", "Unsafe", "Controversial"))
    summary = {
        "input_files": inputs,
        "model": args.model,
        "total_records": total_records,
        "safety": dict(total_safety),
        "categories": dict(total_categories),
        "errors": total_errors,
        "unknown": total_records - recognized - total_errors,
    }
    with open(os.path.join(args.output, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--input", required=True, help="Input .jsonl file, glob, or directory")
    p.add_argument(
        "--output",
        required=True,
        help="Output directory; writes all.jsonl + safe.jsonl + unsafe.jsonl + controversial.jsonl",
    )
    p.add_argument("--base_url", default="http://localhost:8000/v1", help="OpenAI-compatible base URL")
    p.add_argument("--api_key", default=os.environ.get("OPENAI_API_KEY", "EMPTY"))
    p.add_argument("--model", default="Qwen/Qwen3Guard-Gen-8B")
    p.add_argument("--concurrency", type=int, default=64, help="Max in-flight requests")
    p.add_argument("--max_tokens", type=int, default=128)
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--request_timeout", type=float, default=120.0, help="Per-request timeout (seconds)")
    p.add_argument("--max_retries", type=int, default=5, help="Retries on transient errors")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
