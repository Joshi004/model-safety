#!/usr/bin/env python3
"""Judge the messages Qwen3Guard rated Unsafe or Controversial against a written policy.

This is the third stage of the toxicity scan. It reads a shard's `guard_results/all.jsonl`
(every message the Qwen3Guard stage judged) and asks Granite Guardian, served by vLLM through an
OpenAI-compatible endpoint, whether the policy says each Unsafe or Controversial message is
unsuitable for the dataset. The prompt and the answer's parsing are the ones of
`medical_guard_pilot.py`.

Every record gets a `policy` block:

    {"skipped": true}                                a message Qwen3Guard rated Safe, or that has
                                                     no usable verdict (it keeps today's handling)
    {"decision": "NOT_GENERAL" | "KEEP_GENERAL" | "REVIEW",
     "score": "yes" | "no" | null, "parse_error": ..., "error": ..., "raw": ...,
     "truncated": ..., "model": ..., "policy_sha256": ...}

`NOT_GENERAL` is the policy saying yes (unsuitable), `KEEP_GENERAL` the policy saying no. `REVIEW`
is no usable answer, whether the request failed (`error`) or the reply had no yes or no
(`parse_error`). Readers count only `KEEP_GENERAL` as "not flagged".

`all.jsonl` is replaced, in one step, only once every record has been judged. `safe.jsonl`,
`unsafe.jsonl` and `controversial.jsonl` are Qwen3Guard's own buckets and are not changed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from medical_guard_pilot import GRANITE_MODEL, _request, granite_prompt, parse_granite

# The verdicts that go to the policy judge: the ones that remove a row (rules.py).
JUDGED_VERDICTS = ("Unsafe", "Controversial")
KEEP_DECISION = "KEEP_GENERAL"


def needs_policy_judgement(record: dict[str, Any]) -> bool:
    guard = record.get("guard")
    return isinstance(guard, dict) and "error" not in guard and guard.get("safety") in JUDGED_VERDICTS


async def judge_record(
    record: dict[str, Any],
    client: Any,
    semaphore: asyncio.Semaphore,
    policy: str,
    policy_hash: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    truncated = None
    try:
        messages, context = granite_prompt(record, policy, args.max_context_chars)
        truncated = context["truncated"]
        async with semaphore:
            raw = await _request(client, args.model, messages, args.max_tokens, None, args.retries)
        parsed = parse_granite(raw)
        block = {
            "decision": parsed["decision"],
            "score": parsed["score"],
            "parse_error": parsed["parse_error"],
            "error": None,
            "raw": raw,
        }
    except Exception as exc:  # The OpenAI SDK's exception types vary by version.
        block = {
            "decision": "REVIEW",
            "score": None,
            "parse_error": None,
            "error": f"{type(exc).__name__}: {exc}",
            "raw": "",
        }
    record["policy"] = {**block, "truncated": truncated, "model": args.model, "policy_sha256": policy_hash}
    return record


def count_record(record: dict[str, Any], counts: Counter) -> None:
    counts["total_records"] += 1
    block = record["policy"]
    if block.get("skipped"):
        counts["skipped"] += 1
        return
    counts["judged"] += 1
    if block["error"]:
        counts["errors"] += 1
    elif block["decision"] == KEEP_DECISION:
        counts["kept"] += 1
    elif block["decision"] == "NOT_GENERAL":
        counts["not_general"] += 1
    else:
        counts["review"] += 1


def iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc


async def run(args: argparse.Namespace) -> Counter:
    from openai import AsyncOpenAI

    policy = args.policy_file.read_text(encoding="utf-8")
    if not policy.strip():
        raise SystemExit(f"The policy file is empty: {args.policy_file}")
    policy_hash = hashlib.sha256(policy.encode("utf-8")).hexdigest()

    client = AsyncOpenAI(base_url=args.base_url, api_key=args.api_key, timeout=args.timeout, max_retries=0)
    semaphore = asyncio.Semaphore(args.concurrency)
    counts: Counter = Counter()
    pending: set[asyncio.Task] = set()
    max_pending = max(args.concurrency * 4, 1)
    temp = args.input.with_name(args.input.name + ".policy.tmp")

    try:
        with temp.open("w", encoding="utf-8") as out:

            def write(record: dict[str, Any]) -> None:
                count_record(record, counts)
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                if counts["total_records"] % 100 == 0:
                    print(f"policy judge: {counts['total_records']} records written", flush=True)

            async def drain(*, everything: bool = False) -> None:
                if not pending:
                    return
                done, still_pending = await asyncio.wait(
                    pending, return_when=asyncio.ALL_COMPLETED if everything else asyncio.FIRST_COMPLETED
                )
                pending.clear()
                pending.update(still_pending)
                for task in done:
                    write(task.result())

            read = 0
            for record in iter_jsonl(args.input):
                read += 1
                if needs_policy_judgement(record):
                    pending.add(asyncio.create_task(judge_record(record, client, semaphore, policy, policy_hash, args)))
                    if len(pending) >= max_pending:
                        await drain()
                else:
                    record["policy"] = {"skipped": True}
                    write(record)
            await drain(everything=True)

        if counts["total_records"] != read:
            raise RuntimeError(f"Read {read} records but wrote {counts['total_records']}; {args.input} was left as it is.")
        os.replace(temp, args.input)
    finally:
        await client.close()
        temp.unlink(missing_ok=True)

    summary = {
        "model": args.model,
        "policy_sha256": policy_hash,
        "total_records": counts["total_records"],
        "judged": counts["judged"],
        "kept": counts["kept"],
        "not_general": counts["not_general"],
        "review": counts["review"],
        "errors": counts["errors"],
        "skipped": counts["skipped"],
    }
    args.summary_file.parent.mkdir(parents=True, exist_ok=True)
    args.summary_file.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, required=True, help="A shard's guard_results/all.jsonl; replaced when done")
    parser.add_argument("--policy-file", type=Path, required=True)
    parser.add_argument("--summary-file", type=Path, required=True)
    parser.add_argument("--base_url", default="http://localhost:8000/v1", help="OpenAI-compatible base URL")
    parser.add_argument("--api_key", default=os.environ.get("OPENAI_API_KEY", "EMPTY"))
    parser.add_argument("--model", default=GRANITE_MODEL)
    parser.add_argument("--concurrency", type=int, default=8, help="Max in-flight requests")
    parser.add_argument("--max_tokens", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=180.0, help="Per-request timeout (seconds)")
    parser.add_argument("--retries", type=int, default=3, help="Retries on a failed request")
    # Lower than the pilot's 20,000: the policy (up to 6,000 characters) and the conversation
    # have to fit the model's 8,192 tokens, and text that isn't English takes more tokens.
    parser.add_argument("--max-context-chars", type=int, default=12_000)
    return parser.parse_args()


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
