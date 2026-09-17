#!/usr/bin/env python3
"""Run a medical-aware Granite Guardian versus Gemma guard pilot.

The pilot deliberately keeps the two judges independent of Qwen3Guard: Qwen and
Detoxify metadata are preserved for analysis but are not included in either
model prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
DEFAULT_POLICY = HERE / "policies/medpsy_medical_sexual_v1.txt"
GRANITE_MODEL = "ibm-granite/granite-guardian-4.1-8b"
GEMMA_MODEL = "google/gemma-4-31B-it"

GUARDIAN_NOTHINK = (
    "<no-think>As a judge agent, assess whether the provided text meets the given "
    "judging criteria using all available information, including conversations, "
    "documents, and tools. Provide your score immediately without explanation. "
    "Output empty <think>\n</think> tags followed by your score in "
    "<score></score> tags."
)
SCORE_RE = re.compile(r"<score>\s*(yes|no)\s*</score>", re.IGNORECASE)
THINK_RE = re.compile(r"<think>(.*?)</think>", re.IGNORECASE | re.DOTALL)
VALID_GEMMA_ACTIONS = {"KEEP_GENERAL", "SAFETY_ONLY", "REMOVE", "REVIEW"}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSON at {path}:{line_number}") from exc
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def source_key(row: dict[str, Any]) -> tuple[str, int, int | None]:
    message_index = row.get("message_index")
    return (
        str(row.get("source_file")),
        int(row.get("source_line")),
        int(message_index) if message_index is not None else None,
    )


def content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(content_text(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if value is None:
        return ""
    return str(value)


def _load_samples(path: Path, track: str) -> list[dict[str, Any]]:
    rows = read_jsonl(path)
    output = []
    for position, row in enumerate(rows, 1):
        sample_id = int(row.get("sample_id", position))
        output.append(
            {
                **row,
                "record_id": f"{track}:{sample_id}",
                "sample_track": track,
                "sample_id": sample_id,
            }
        )
    return output


def prepare_samples(
    qwen_sample: Path,
    sexual_sample: Path,
    shard_root: Path,
    output: Path,
) -> dict[str, Any]:
    samples = _load_samples(qwen_sample, "qwen_non_safe")
    samples.extend(_load_samples(sexual_sample, "detoxify_sexual"))
    keys = {source_key(row) for row in samples}
    matched: dict[tuple[str, int, int | None], tuple[dict[str, Any], str]] = {}
    scan_files = sorted(shard_root.glob("shard_*/guard_results/all.jsonl"))
    if not scan_files:
        raise RuntimeError(f"No Qwen guard all.jsonl files found under {shard_root}")

    scanned = 0
    for scan_file in scan_files:
        with scan_file.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"Invalid JSON at {scan_file}:{line_number}"
                    ) from exc
                scanned += 1
                key = source_key(row)
                if key in keys:
                    matched[key] = (row, scan_file.parents[1].name)
        if len(matched) == len(keys):
            break

    hydrated = []
    missing = []
    for sample in samples:
        key = source_key(sample)
        match = matched.get(key)
        if match is None:
            missing.append(sample["record_id"])
            continue
        scan_row, shard_name = match
        guard = scan_row.get("guard") or {}
        scores = scan_row.get("toxicity_scores") or {}
        hydrated.append(
            {
                **sample,
                "source_file": scan_row.get("source_file"),
                "source_line": scan_row.get("source_line"),
                "message_index": scan_row.get("message_index"),
                "role": scan_row.get("role"),
                "text": scan_row.get("text"),
                "source_record": scan_row.get("source_record"),
                "qwen_guard": {
                    "safety": guard.get("safety"),
                    "categories": guard.get("categories") or [],
                    "refusal": guard.get("refusal"),
                    "raw": guard.get("raw"),
                },
                "detoxify": {
                    "trigger_category": scan_row.get("trigger_category"),
                    "max_score": scan_row.get("toxicity"),
                    "scores": scores,
                },
                "hydrated_from_shard": shard_name,
            }
        )

    if missing:
        raise RuntimeError(
            f"Failed to hydrate {len(missing)} samples: {', '.join(missing[:10])}"
        )
    if len({row["record_id"] for row in hydrated}) != len(hydrated):
        raise RuntimeError("Duplicate record_id values in hydrated sample")

    write_jsonl(output, hydrated)
    report = {
        "rows": len(hydrated),
        "tracks": dict(Counter(row["sample_track"] for row in hydrated)),
        "scan_files": len(scan_files),
        "scan_rows_read": scanned,
        "missing": 0,
        "output": str(output),
    }
    output.with_suffix(".summary.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return report


def _fallback_message(record: dict[str, Any]) -> dict[str, str]:
    text = str(record.get("text") or "")
    role, separator, body = text.partition(": ")
    if not separator or role not in {"system", "user", "assistant", "tool"}:
        role = str(record.get("role") or "user")
        body = text
    return {"role": role, "content": body}


def context_messages(
    record: dict[str, Any], max_context_chars: int
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    source_record = record.get("source_record") or {}
    source_messages = source_record.get("messages")
    messages: list[dict[str, str]] = []
    if isinstance(source_messages, list):
        for message in source_messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "user")
            if role not in {"system", "user", "assistant", "tool"}:
                role = "user"
            messages.append(
                {"role": role, "content": content_text(message.get("content"))}
            )
    if not messages:
        messages = [_fallback_message(record)]

    raw_index = record.get("message_index")
    if isinstance(raw_index, int) and 0 <= raw_index < len(messages):
        flagged_index = raw_index
        match = "source_index"
    else:
        fallback = _fallback_message(record)
        candidates = [
            index
            for index, message in enumerate(messages)
            if message == fallback
        ]
        flagged_index = candidates[-1] if candidates else len(messages) - 1
        match = "exact" if candidates else "fallback_last"

    sliced = messages[: flagged_index + 1]
    original_count = len(sliced)
    truncated = False
    while len(sliced) > 1 and sum(len(item["content"]) for item in sliced) > max_context_chars:
        sliced.pop(0)
        truncated = True
    if len(sliced[-1]["content"]) > max_context_chars:
        content = sliced[-1]["content"]
        half = max_context_chars // 2
        sliced[-1]["content"] = (
            content[:half] + "\n[... flagged message truncated ...]\n" + content[-half:]
        )
        truncated = True

    return sliced, {
        "flagged_index": flagged_index,
        "flagged_role": messages[flagged_index]["role"],
        "match": match,
        "original_context_messages": original_count,
        "sent_context_messages": len(sliced),
        "truncated": truncated,
    }


def build_guardian_block(policy: str, flagged_role: str) -> str:
    role = "assistant" if flagged_role == "assistant" else "user"
    return (
        f"<guardian>{GUARDIAN_NOTHINK}\n\n"
        f"### Criteria: {policy.strip()}\n\n"
        f"### Scoring Schema: If the last {role}'s text meets the criteria, "
        "return 'yes'; otherwise, return 'no'."
    )


def parse_granite(raw: str) -> dict[str, Any]:
    matches = SCORE_RE.findall(raw)
    score = matches[-1].lower() if matches else None
    traces = [item.strip() for item in THINK_RE.findall(raw) if item.strip()]
    return {
        "score": score,
        "decision": (
            "NOT_GENERAL"
            if score == "yes"
            else "KEEP_GENERAL"
            if score == "no"
            else "REVIEW"
        ),
        "trace": traces[-1] if traces else None,
        "parse_error": None if score else "missing yes/no <score> tag",
    }


def _extract_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("Gemma output is not a JSON object")
    return value


def parse_gemma(raw: str) -> dict[str, Any]:
    try:
        value = _extract_json(raw)
        action = str(value.get("action") or "").upper()
        if action not in VALID_GEMMA_ACTIONS:
            raise ValueError(f"invalid action: {action!r}")
        return {
            "action": action,
            "medical_context": bool(value.get("medical_context")),
            "reason": str(value.get("reason") or ""),
            "evidence": str(value.get("evidence") or ""),
            "parse_error": None,
        }
    except (json.JSONDecodeError, ValueError) as exc:
        return {
            "action": "REVIEW",
            "medical_context": None,
            "reason": "",
            "evidence": "",
            "parse_error": str(exc),
        }


def granite_prompt(
    record: dict[str, Any], policy: str, max_context_chars: int
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    messages, context = context_messages(record, max_context_chars)
    messages.append(
        {
            "role": "user",
            "content": build_guardian_block(policy, context["flagged_role"]),
        }
    )
    return messages, context


def gemma_prompt(
    record: dict[str, Any], policy: str, max_context_chars: int
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    messages, context = context_messages(record, max_context_chars)
    flagged = messages[-1]
    system = (
        "You are an independent medical dataset safety adjudicator. Apply the policy "
        "to the flagged message, using prior turns only as context. Do not infer a "
        "decision from other classifiers. Return only one JSON object with exactly "
        'these fields: {"action":"KEEP_GENERAL|SAFETY_ONLY|REMOVE|REVIEW",'
        '"medical_context":true|false,"reason":"one concise sentence",'
        '"evidence":"a short quote from the flagged message"}.\n\n'
        f"POLICY:\n{policy.strip()}"
    )
    user = {
        "record_id": record["record_id"],
        "flagged_role": context["flagged_role"],
        "flagged_message": flagged,
        "conversation_up_to_flagged_message": messages,
    }
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": "Adjudicate this record:\n" + json.dumps(user, ensure_ascii=False),
        },
    ], context


async def _request(
    client: Any,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    response_format: dict[str, str] | None,
    retries: int,
) -> str:
    delay = 1.0
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.0,
            }
            if response_format is not None:
                kwargs["response_format"] = response_format
            response = await client.chat.completions.create(**kwargs)
            return response.choices[0].message.content or ""
        except Exception as exc:  # OpenAI SDK exception hierarchy varies by version.
            last_error = exc
            if attempt == retries:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)
    assert last_error is not None
    raise last_error


async def run_backend_async(args: argparse.Namespace) -> dict[str, Any]:
    from openai import AsyncOpenAI

    rows = read_jsonl(args.input)
    policy = args.policy.read_text(encoding="utf-8")
    policy_hash = hashlib.sha256(policy.encode("utf-8")).hexdigest()
    model = args.model or (GRANITE_MODEL if args.backend == "granite" else GEMMA_MODEL)
    client = AsyncOpenAI(
        base_url=args.base_url,
        api_key=args.api_key,
        timeout=args.timeout,
    )
    semaphore = asyncio.Semaphore(args.concurrency)

    async def process(row: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            if args.backend == "granite":
                messages, context = granite_prompt(
                    row, policy, args.max_context_chars
                )
                max_tokens = 128
                response_format = None
            else:
                messages, context = gemma_prompt(row, policy, args.max_context_chars)
                max_tokens = 512
                response_format = {"type": "json_object"}
            try:
                raw = await _request(
                    client,
                    model,
                    messages,
                    max_tokens,
                    response_format,
                    args.retries,
                )
                parsed = parse_granite(raw) if args.backend == "granite" else parse_gemma(raw)
                result = {**parsed, "raw": raw, "error": None}
            except Exception as exc:
                result = {
                    **(
                        {
                            "score": None,
                            "decision": "REVIEW",
                            "trace": None,
                            "parse_error": None,
                        }
                        if args.backend == "granite"
                        else {
                            "action": "REVIEW",
                            "medical_context": None,
                            "reason": "",
                            "evidence": "",
                            "parse_error": None,
                        }
                    ),
                    "raw": "",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            return {
                **row,
                f"{args.backend}_guard": {
                    **result,
                    **context,
                    "model": model,
                    "policy_sha256": policy_hash,
                },
            }

    tasks = [asyncio.create_task(process(row)) for row in rows]
    results = []
    for completed, task in enumerate(asyncio.as_completed(tasks), 1):
        results.append(await task)
        if completed % 25 == 0 or completed == len(tasks):
            print(f"{args.backend}: completed {completed}/{len(tasks)}", flush=True)
    await client.close()
    results.sort(key=lambda row: str(row["record_id"]))
    write_jsonl(args.output, results)

    decision_field = "decision" if args.backend == "granite" else "action"
    counts = Counter(
        row[f"{args.backend}_guard"][decision_field] for row in results
    )
    errors = sum(bool(row[f"{args.backend}_guard"]["error"]) for row in results)
    parse_errors = sum(
        bool(row[f"{args.backend}_guard"]["parse_error"]) for row in results
    )
    summary = {
        "backend": args.backend,
        "model": model,
        "rows": len(results),
        "decisions": dict(counts),
        "request_errors": errors,
        "parse_errors": parse_errors,
        "policy_sha256": policy_hash,
        "output": str(args.output),
    }
    args.output.with_suffix(".summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def _binary_action(action: str) -> str:
    if action == "KEEP_GENERAL":
        return "KEEP_GENERAL"
    if action in {"NOT_GENERAL", "SAFETY_ONLY", "REMOVE"}:
        return "NOT_GENERAL"
    return "REVIEW"


def _agreement(left: list[str], right: list[str]) -> dict[str, Any]:
    comparable = [
        (a, b) for a, b in zip(left, right) if "REVIEW" not in {a, b}
    ]
    agreed = sum(a == b for a, b in comparable)
    return {
        "comparable": len(comparable),
        "agreed": agreed,
        "rate": agreed / len(comparable) if comparable else None,
    }


def compare_results(
    sample_path: Path,
    granite_path: Path,
    gemma_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    samples = {row["record_id"]: row for row in read_jsonl(sample_path)}
    granite = {row["record_id"]: row for row in read_jsonl(granite_path)}
    gemma = {row["record_id"]: row for row in read_jsonl(gemma_path)}
    if samples.keys() != granite.keys() or samples.keys() != gemma.keys():
        raise RuntimeError("Sample, Granite, and Gemma record IDs do not match")

    comparison_rows = []
    by_track: dict[str, Counter[str]] = defaultdict(Counter)
    by_qwen: dict[str, Counter[str]] = defaultdict(Counter)
    binary_columns = {"qwen": [], "granite": [], "gemma": []}
    for record_id in sorted(samples):
        sample = samples[record_id]
        granite_result = granite[record_id]["granite_guard"]
        gemma_result = gemma[record_id]["gemma_guard"]
        qwen_safety = str((sample.get("qwen_guard") or {}).get("safety") or "Unknown")
        qwen_action = (
            "KEEP_GENERAL"
            if qwen_safety == "Safe"
            else "NOT_GENERAL"
            if qwen_safety in {"Controversial", "Unsafe"}
            else "REVIEW"
        )
        granite_action = _binary_action(str(granite_result["decision"]))
        gemma_action = _binary_action(str(gemma_result["action"]))
        binary_columns["qwen"].append(qwen_action)
        binary_columns["granite"].append(granite_action)
        binary_columns["gemma"].append(gemma_action)
        signature = "|".join((qwen_action, granite_action, gemma_action))
        by_track[str(sample["sample_track"])][signature] += 1
        by_qwen[qwen_safety][signature] += 1
        comparison_rows.append(
            {
                "record_id": record_id,
                "sample_track": sample["sample_track"],
                "sample_id": sample["sample_id"],
                "role": sample.get("role"),
                "text": sample.get("text"),
                "source_file": sample.get("source_file"),
                "source_line": sample.get("source_line"),
                "message_index": sample.get("message_index"),
                "qwen_safety": qwen_safety,
                "qwen_categories": (sample.get("qwen_guard") or {}).get("categories"),
                "qwen_action": qwen_action,
                "granite_action": granite_action,
                "granite_score": granite_result.get("score"),
                "granite_raw": granite_result.get("raw"),
                "gemma_action": gemma_action,
                "gemma_detailed_action": gemma_result.get("action"),
                "gemma_medical_context": gemma_result.get("medical_context"),
                "gemma_reason": gemma_result.get("reason"),
                "gemma_evidence": gemma_result.get("evidence"),
                "signature": signature,
            }
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "per_record.jsonl", comparison_rows)
    signatures = Counter(row["signature"] for row in comparison_rows)
    summary = {
        "rows": len(comparison_rows),
        "tracks": dict(Counter(row["sample_track"] for row in comparison_rows)),
        "binary_decisions": {
            model: dict(Counter(values)) for model, values in binary_columns.items()
        },
        "gemma_detailed_actions": dict(
            Counter(row["gemma_detailed_action"] for row in comparison_rows)
        ),
        "three_way_signatures": dict(signatures.most_common()),
        "pairwise_agreement": {
            "qwen_granite": _agreement(
                binary_columns["qwen"], binary_columns["granite"]
            ),
            "qwen_gemma": _agreement(
                binary_columns["qwen"], binary_columns["gemma"]
            ),
            "granite_gemma": _agreement(
                binary_columns["granite"], binary_columns["gemma"]
            ),
        },
        "by_track_signatures": {
            name: dict(counter.most_common()) for name, counter in by_track.items()
        },
        "by_qwen_outcome_signatures": {
            name: dict(counter.most_common()) for name, counter in by_qwen.items()
        },
        "note": (
            "Agreement analysis only. Qwen, Granite, and Gemma predictions are not "
            "human ground truth; the sample is deliberately enriched for Qwen "
            "Controversial/Unsafe and Detoxify sexual-explicit candidates."
        ),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--qwen-sample", type=Path, required=True)
    prepare.add_argument("--sexual-sample", type=Path, required=True)
    prepare.add_argument("--shard-root", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)

    run = subparsers.add_parser("run")
    run.add_argument("--backend", choices=("granite", "gemma"), required=True)
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--base-url", required=True)
    run.add_argument("--model")
    run.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    run.add_argument("--api-key", default="EMPTY")
    run.add_argument("--concurrency", type=int, default=8)
    run.add_argument("--timeout", type=float, default=180.0)
    run.add_argument("--retries", type=int, default=3)
    run.add_argument("--max-context-chars", type=int, default=20_000)

    compare = subparsers.add_parser("compare")
    compare.add_argument("--sample", type=Path, required=True)
    compare.add_argument("--granite", type=Path, required=True)
    compare.add_argument("--gemma", type=Path, required=True)
    compare.add_argument("--output-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "prepare":
        result = prepare_samples(
            args.qwen_sample, args.sexual_sample, args.shard_root, args.output
        )
    elif args.command == "run":
        result = asyncio.run(run_backend_async(args))
    else:
        result = compare_results(
            args.sample, args.granite, args.gemma, args.output_dir
        )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
