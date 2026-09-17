#!/usr/bin/env python3
"""
Validate extracted PII candidates with an OpenAI-compatible chat-completions API.

The input CSV is expected to contain:
    file,line,source_sha256,category,PII,whole line

The "whole line" column is the original JSONL record. The same text extraction
contract is used by the regex scanner, extractor, and LLM validator.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import urlparse, urlunparse

from modelsafety.contract.textract import parse_record_text

try:
    from tqdm import tqdm
except ModuleNotFoundError:
    class _SimpleTqdm:
        def __init__(self, iterable: Any = None, **kwargs: Any) -> None:
            self.iterable = iterable if iterable is not None else []
            self.desc = kwargs.get("desc") or "Progress"
            self.unit = kwargs.get("unit") or "item"
            self.total = kwargs.get("total")
            if self.total is None:
                try:
                    self.total = len(self.iterable)
                except TypeError:
                    self.total = None
            self.count = 0
            self.postfix = ""

        def __iter__(self) -> Any:
            for item in self.iterable:
                yield item
                self.count += 1
                self._print_progress()

        def set_postfix(self, **kwargs: Any) -> None:
            self.postfix = ", ".join(f"{key}={value}" for key, value in kwargs.items())
            self._print_progress()

        def update(self, n: int = 1) -> None:
            self.count += n
            self._print_progress()

        def _print_progress(self) -> None:
            if self.total:
                message = f"\r{self.desc}: {self.count}/{self.total} {self.unit}"
            else:
                message = f"\r{self.desc}: {self.count} {self.unit}"
            if self.postfix:
                message = f"{message} [{self.postfix}]"
            print(message, end="", file=sys.stderr, flush=True)
            if self.total and self.count >= self.total:
                print(file=sys.stderr, flush=True)

        def close(self) -> None:
            if self.count:
                print(file=sys.stderr, flush=True)
            return None

    def tqdm(iterable: Any = None, **kwargs: Any) -> _SimpleTqdm:
        return _SimpleTqdm(iterable, **kwargs)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = SCRIPT_DIR / "out" / "validated-pii-fast-data" / "pii_extract.csv"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "out" / "llm-validated-pii-fast-data"
DEFAULT_BASE_URLS = (
    os.environ.get("OPENAI_BASE_URLS")
    or os.environ.get("OPENAI_BASE_URL")
    or "http://127.0.0.1:8000/v1"
)
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "local-model")

SYSTEM_PROMPT = """You review one regex candidate from a conversation.

Private-person PII is information that can distinguish or trace a real natural
person, alone or combined with information linked to that person.

Keep the candidate for removal only when the conversation establishes that it
identifies, contacts, locates, or traces a specific private person.

Filter the candidate when it is:
- public, professional, institutional, or business information;
- fictional, synthetic, generic, illustrative, test data, or a placeholder;
- a malformed extraction or substring of a larger code or scientific token; or
- not clearly linked to a real private person.

First-person wording alone does not establish that an example is real. When
uncertain, filter. Judge only the supplied candidate, not nearby values.

Return only valid JSON. Do not include markdown or any text outside the JSON.
The JSON object must have exactly this shape:
{
  "is_private_person_pii": true or false,
  "is_public_or_business_info": true or false,
  "decision": "keep" or "filter",
  "confidence": "low" or "medium" or "high",
  "reason": "one or two concise sentences explaining the decision",
  "evidence": "brief quote or context supporting the decision"
}
"""

USER_PROMPT_TEMPLATE = """Review this exact PII candidate.

Candidate metadata:
- source_file: {source_file}
- source_line: {source_line}
- category: {category}
- candidate: {candidate}

Full conversation:
{conversation}
"""

REQUIRED_COLUMNS = ["file", "line", "source_sha256", "category", "PII", "whole line"]
EXCLUDED_CATEGORIES = {"zip_codes", "postcodes"}
RESULT_FIELDS = [
    "llm_decision",
    "llm_confidence",
    "llm_reason",
    "llm_evidence",
    "llm_is_private_person_pii",
    "llm_is_public_or_business_info",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate PII candidates from pii_extract.csv with an "
            "OpenAI-compatible chat-completions server."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"Input CSV path (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--base-url",
        action="append",
        default=None,
        help=(
            "OpenAI-compatible base URL. Can be repeated or comma-separated. "
            "Bare IP:PORT values are normalized to http://IP:PORT/v1. "
            f"Default comes from OPENAI_BASE_URLS/OPENAI_BASE_URL or {DEFAULT_BASE_URLS}."
        ),
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENAI_API_KEY", ""),
        help="API key for the server. Optional for many local servers.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model name to send in requests (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Sampling temperature (default: 0.0)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=600,
        help="Maximum response tokens (default: 600)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="HTTP timeout in seconds (default: 120)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="Number of retries after a failed request (default: 2)",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=2.0,
        help="Initial retry delay in seconds; doubles per retry (default: 2)",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.0,
        help=(
            "Delay after each successful request inside a worker thread, "
            "in seconds (default: 0)."
        ),
    )
    parser.add_argument(
        "--requests-per-server",
        type=int,
        default=1,
        help=(
            "Maximum concurrent LLM requests per configured server. "
            "Total workers = endpoints * requests-per-server (default: 1)."
        ),
    )
    parser.add_argument(
        "--max-pending",
        type=int,
        default=0,
        help=(
            "Maximum submitted requests awaiting completion. "
            "0 uses four times the worker count."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only process the first N rows, useful for smoke tests. 0 means all rows.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse existing JSONL decisions and only call the LLM for missing rows.",
    )
    parser.add_argument(
        "--cache-tag",
        default=os.environ.get("PII_LLM_CACHE_TAG", ""),
        help="Optional run tag included in resume keys when model deployments change.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse rows and print prompt payloads without calling the server.",
    )
    parser.add_argument(
        "--no-response-format",
        action="store_true",
        help=(
            "Do not send OpenAI response_format. Use this if your local server "
            "does not support JSON mode."
        ),
    )
    parser.add_argument(
        "--enable-thinking",
        action="store_true",
        help="Enable the model chat template's reasoning/thinking mode.",
    )
    parser.add_argument(
        "--max-conversation-chars",
        type=int,
        default=0,
        help=(
            "Optional max characters of conversation to send. "
            "0 means send the whole conversation (default: 0)."
        ),
    )
    return parser.parse_args()


def normalize_base_url(base_url: str) -> str:
    normalized = base_url.strip()
    if not normalized:
        return ""

    if not normalized.startswith(("http://", "https://")):
        normalized = f"http://{normalized}"

    parsed = urlparse(normalized)
    path = parsed.path.rstrip("/")
    if path in {"", "/"}:
        parsed = parsed._replace(path="/v1")
    return urlunparse(parsed).rstrip("/")


def parse_base_urls(values: list[str] | None) -> list[str]:
    raw_values = values or [DEFAULT_BASE_URLS]
    base_urls = []

    for raw_value in raw_values:
        for item in raw_value.split(","):
            normalized = normalize_base_url(item)
            if normalized:
                base_urls.append(normalized)

    if not base_urls:
        raise ValueError("At least one --base-url endpoint is required")

    return base_urls


class RoundRobinEndpoints:
    def __init__(self, base_urls: list[str]) -> None:
        self.base_urls = base_urls
        self.index = 0

    def next(self) -> str:
        base_url = self.base_urls[self.index % len(self.base_urls)]
        self.index += 1
        return base_url


def row_key(row: dict[str, str], validation_signature: str = "") -> str:
    key_payload = {
        "file": row.get("file", ""),
        "line": row.get("line", ""),
        "source_sha256": row.get("source_sha256", ""),
        "category": row.get("category", ""),
        "PII": row.get("PII", ""),
        "whole_line_sha256": hashlib.sha256(
            row.get("whole line", "").encode("utf-8", errors="replace")
        ).hexdigest(),
        "validation_signature": validation_signature,
    }
    serialized = json.dumps(key_payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def conversation_from_whole_line(raw_line: str) -> tuple[str, bool]:
    return parse_record_text(raw_line), True


def maybe_truncate_conversation(
    conversation: str,
    max_chars: int,
    focus: str = "",
) -> tuple[str, bool]:
    if max_chars <= 0 or len(conversation) <= max_chars:
        return conversation, False

    marker = "\n[... conversation truncated ...]\n"
    focus_index = conversation.find(focus) if focus else -1
    if focus_index >= 0:
        keep_chars = max(0, max_chars - 2 * len(marker))
        center = focus_index + len(focus) // 2
        start = max(0, min(center - keep_chars // 2, len(conversation) - keep_chars))
        end = start + keep_chars
        prefix = marker if start > 0 else ""
        suffix = marker if end < len(conversation) else ""
        return prefix + conversation[start:end] + suffix, True

    keep_chars = max(0, max_chars - len(marker))
    before = keep_chars // 2
    after = keep_chars - before
    return conversation[:before] + marker + conversation[-after:], True


def build_messages(row: dict[str, str], conversation: str) -> list[dict[str, str]]:
    user_prompt = USER_PROMPT_TEMPLATE.format(
        source_file=json.dumps(row.get("file", ""), ensure_ascii=False),
        source_line=json.dumps(row.get("line", ""), ensure_ascii=False),
        category=json.dumps(row.get("category", ""), ensure_ascii=False),
        candidate=json.dumps(row.get("PII", ""), ensure_ascii=False),
        conversation=conversation,
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def call_openai_compatible_api(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    timeout: float,
    use_response_format: bool,
    enable_thinking: bool,
) -> tuple[dict[str, Any], str]:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if use_response_format:
        payload["response_format"] = {"type": "json_object"}
    if enable_thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": True}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = request.Request(
        chat_completions_url(base_url),
        data=body,
        headers=headers,
        method="POST",
    )

    with request.urlopen(req, timeout=timeout) as response:
        response_body = response.read().decode("utf-8", errors="replace")

    response_json = json.loads(response_body)
    content = extract_response_content(response_json)
    return response_json, content


def call_with_retries(
    *,
    endpoints: RoundRobinEndpoints,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    timeout: float,
    retries: int,
    retry_delay: float,
    use_response_format: bool,
    enable_thinking: bool,
) -> tuple[dict[str, Any], str, str]:
    last_error: Exception | None = None
    last_base_url = ""
    for attempt in range(retries + 1):
        base_url = endpoints.next()
        last_base_url = base_url
        try:
            response_json, content = call_openai_compatible_api(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                use_response_format=use_response_format,
                enable_thinking=enable_thinking,
            )
            return response_json, content, base_url
        except (
            TimeoutError,
            error.HTTPError,
            error.URLError,
            json.JSONDecodeError,
        ) as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(retry_delay * (2**attempt))

    assert last_error is not None
    raise RuntimeError(f"{last_error} (last endpoint: {last_base_url})") from last_error


def call_single_endpoint_with_retries(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    timeout: float,
    retries: int,
    retry_delay: float,
    use_response_format: bool,
    enable_thinking: bool,
) -> tuple[dict[str, Any], str]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return call_openai_compatible_api(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                use_response_format=use_response_format,
                enable_thinking=enable_thinking,
            )
        except (
            TimeoutError,
            error.HTTPError,
            error.URLError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(retry_delay * (2**attempt))

    assert last_error is not None
    raise RuntimeError(f"{last_error} (endpoint: {base_url})") from last_error


def extract_response_content(response_json: dict[str, Any]) -> str:
    choices = response_json.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("response has no choices")

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise ValueError("response choice is not an object")

    message = first_choice.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]

    text = first_choice.get("text")
    if isinstance(text, str):
        return text

    raise ValueError("response choice has no message.content or text")


def extract_response_reasoning(response_json: dict[str, Any]) -> str:
    choices = response_json.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return ""
    message = first_choice.get("message")
    if not isinstance(message, dict):
        return ""
    for field in ("reasoning", "reasoning_content"):
        reasoning = message.get(field)
        if isinstance(reasoning, str) and reasoning:
            return reasoning
    return ""


def parse_model_json(content: str) -> dict[str, Any]:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if fenced_match:
        return json.loads(fenced_match.group(1))

    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(content[start : end + 1])

    raise ValueError("model response did not contain a JSON object")


def normalize_decision(model_result: dict[str, Any]) -> dict[str, Any]:
    expected_fields = {
        "is_private_person_pii",
        "is_public_or_business_info",
        "decision",
        "confidence",
        "reason",
        "evidence",
    }
    if set(model_result) != expected_fields:
        raise ValueError("model response has an invalid field set")
    if not isinstance(model_result["is_private_person_pii"], bool) or not isinstance(
        model_result["is_public_or_business_info"], bool
    ):
        raise ValueError("model response PII flags must be booleans")
    if not isinstance(model_result["reason"], str) or not isinstance(
        model_result["evidence"], str
    ):
        raise ValueError("model response reason and evidence must be strings")

    is_private_person_pii = model_result["is_private_person_pii"]
    is_public_or_business_info = model_result["is_public_or_business_info"]
    raw_decision = model_result["decision"]
    if raw_decision not in {"keep", "filter"}:
        raise ValueError("model response decision must be keep or filter")
    derived_keep = is_private_person_pii and not is_public_or_business_info
    decision = "keep" if derived_keep else "filter"
    if raw_decision != decision:
        raise ValueError("model response decision conflicts with its PII flags")

    confidence = model_result["confidence"]
    if confidence not in {"low", "medium", "high"}:
        raise ValueError("model response confidence must be low, medium, or high")

    return {
        "is_private_person_pii": is_private_person_pii,
        "is_public_or_business_info": is_public_or_business_info,
        "decision": decision,
        "confidence": confidence,
        "reason": model_result["reason"].strip(),
        "evidence": model_result["evidence"].strip(),
    }


def error_decision(message: str) -> dict[str, Any]:
    return {
        "is_private_person_pii": False,
        "is_public_or_business_info": False,
        "decision": "filter",
        "confidence": "low",
        "reason": f"LLM validation failed: {message}",
        "evidence": "",
    }


def allow_large_csv_fields() -> None:
    """Raise the CSV parser limit for source records larger than 128 KiB."""
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def iter_rows(input_path: Path, limit: int | None):
    allow_large_csv_fields()
    effective_limit = None if limit == 0 else limit
    with input_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"Input CSV has no header: {input_path}")

        missing_columns = [name for name in REQUIRED_COLUMNS if name not in reader.fieldnames]
        if missing_columns:
            missing = ", ".join(missing_columns)
            raise ValueError(f"Input CSV is missing required columns: {missing}")

        for row_index, row in enumerate(reader, start=1):
            normalized = {key: value or "" for key, value in row.items()}
            source_sha256 = normalized.get("source_sha256", "")
            if len(source_sha256) != 64 or any(
                character not in "0123456789abcdef"
                for character in source_sha256
            ):
                raise ValueError(
                    f"Invalid source_sha256 at {input_path}:{row_index + 1}"
                )
            actual_sha256 = hashlib.sha256(
                normalized.get("whole line", "").encode("utf-8")
            ).hexdigest()
            if actual_sha256 != source_sha256:
                raise ValueError(
                    f"Source hash mismatch at {input_path}:{row_index + 1}"
                )
            category = normalized.get("category", "")
            if category in EXCLUDED_CATEGORIES:
                raise ValueError(
                    f"Excluded category {category!r} found in {input_path}; "
                    "rerun PII stages 1-3 with the current policy"
                )
            yield normalized
            if effective_limit is not None and row_index >= effective_limit:
                break


def count_rows(input_path: Path, limit: int | None) -> int:
    return sum(1 for _ in iter_rows(input_path, limit))


def load_rows(input_path: Path, limit: int | None) -> list[dict[str, str]]:
    """Backward-compatible eager loader used by external callers and tests."""
    return list(iter_rows(input_path, limit))


def load_existing_results(results_path: Path) -> dict[str, dict[str, Any]]:
    existing = {}
    if not results_path.exists():
        return existing

    with results_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print(
                    f"Skipping malformed JSONL result at {results_path}:{line_number}",
                    file=sys.stderr,
                )
                continue

            key = record.get("row_key")
            if isinstance(key, str) and record.get("status") != "error":
                existing[key] = record

    return existing


def validation_signature(args: argparse.Namespace) -> str:
    payload = {
        "version": 7,
        "model": args.model,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "max_conversation_chars": args.max_conversation_chars,
        "use_response_format": not args.no_response_format,
        "enable_thinking": args.enable_thinking,
        "cache_tag": args.cache_tag,
        "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "user_prompt_sha256": hashlib.sha256(
            USER_PROMPT_TEMPLATE.encode("utf-8")
        ).hexdigest(),
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class ExistingResultCache:
    """Disk-backed lookup for resumable successful result records."""

    def __init__(self, results_paths: list[Path], database_path: Path) -> None:
        database_path.unlink(missing_ok=True)
        self.database_path = database_path
        self.connection = sqlite3.connect(database_path)
        self.connection.execute(
            "CREATE TABLE results (row_key TEXT PRIMARY KEY, record TEXT NOT NULL)"
        )
        for results_path in results_paths:
            if not results_path.is_file():
                continue
            batch = []
            with results_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"Malformed resume result at {results_path}:{line_number}"
                        ) from exc
                    key = record.get("row_key")
                    if isinstance(key, str) and record.get("status") != "error":
                        batch.append((key, json.dumps(record, ensure_ascii=False)))
                    if len(batch) >= 1000:
                        self.connection.executemany(
                            "INSERT OR REPLACE INTO results VALUES (?, ?)", batch
                        )
                        self.connection.commit()
                        batch.clear()
            if batch:
                self.connection.executemany(
                    "INSERT OR REPLACE INTO results VALUES (?, ?)", batch
                )
                self.connection.commit()

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT record FROM results WHERE row_key = ?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def close(self) -> None:
        self.connection.close()
        self.database_path.unlink(missing_ok=True)


def write_jsonl_record(handle: Any, record: dict[str, Any]) -> None:
    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    handle.flush()


def csv_row_with_decision(row: dict[str, str], decision: dict[str, Any]) -> dict[str, Any]:
    output_row = dict(row)
    output_row.update(
        {
            "llm_decision": decision["decision"],
            "llm_confidence": decision["confidence"],
            "llm_reason": decision["reason"],
            "llm_evidence": decision["evidence"],
            "llm_is_private_person_pii": decision["is_private_person_pii"],
            "llm_is_public_or_business_info": decision["is_public_or_business_info"],
        }
    )
    return output_row


def update_summary(
    *,
    summary: dict[str, Any],
    category: str,
    decision: dict[str, Any],
    status: str,
    resumed: bool,
) -> None:
    summary["total_rows"] += 1
    summary["by_category"][category]["total"] += 1

    if resumed:
        summary["resumed_rows"] += 1

    if status == "error":
        summary["failed_rows"] += 1
        summary["by_category"][category]["failed"] += 1

    decision_name = decision["decision"]
    summary[f"{decision_name}_rows"] += 1
    summary["by_category"][category][decision_name] += 1
    summary["confidence"][decision["confidence"]] += 1


def print_dry_run_payload(
    *,
    row_index: int,
    row: dict[str, str],
    messages: list[dict[str, str]],
    conversation_parsed: bool,
    conversation_truncated: bool,
) -> None:
    payload = {
        "row_index": row_index,
        "file": row.get("file", ""),
        "line": row.get("line", ""),
        "category": row.get("category", ""),
        "PII": row.get("PII", ""),
        "conversation_parsed": conversation_parsed,
        "conversation_truncated": conversation_truncated,
        "messages": messages,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def write_summary(summary_path: Path, summary: dict[str, Any]) -> None:
    serializable_summary = {
        **summary,
        "by_category": dict(summary["by_category"]),
        "confidence": dict(summary["confidence"]),
    }
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(serializable_summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def validate_row_with_llm(
    *,
    row_index: int,
    row: dict[str, str],
    row_key_value: str,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
    retries: int,
    retry_delay: float,
    request_delay: float,
    use_response_format: bool,
    enable_thinking: bool,
    max_conversation_chars: int,
) -> dict[str, Any]:
    category = row.get("category", "")
    candidate = row.get("PII", "")
    conversation, parsed = conversation_from_whole_line(row.get("whole line", ""))
    if not candidate or candidate not in conversation:
        raise RuntimeError(
            f"PII source mismatch at {row.get('file')}:{row.get('line')}: "
            f"{candidate!r} is absent from the reviewed text"
        )
    conversation, truncated = maybe_truncate_conversation(
        conversation,
        max_conversation_chars,
        candidate,
    )
    messages = build_messages(row, conversation)
    started_at = datetime.now(timezone.utc).isoformat()
    raw_response_json: dict[str, Any] = {}
    raw_content = ""
    raw_reasoning = ""

    try:
        raw_response_json, raw_content = call_single_endpoint_with_retries(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            retries=retries,
            retry_delay=retry_delay,
            use_response_format=use_response_format,
            enable_thinking=enable_thinking,
        )
        raw_reasoning = extract_response_reasoning(raw_response_json)
        model_result = parse_model_json(raw_content)
        decision = normalize_decision(model_result)
        status = "ok"
        error_message = ""
    except Exception as exc:
        decision = error_decision(str(exc))
        status = "error"
        error_message = str(exc)

    if request_delay > 0 and status == "ok":
        time.sleep(request_delay)

    result_record = {
        "row_key": row_key_value,
        "row_index": row_index,
        "status": status,
        "error": error_message,
        "created_at": started_at,
        "file": row.get("file", ""),
        "line": row.get("line", ""),
        "source_sha256": row.get("source_sha256", ""),
        "category": category,
        "PII": row.get("PII", ""),
        "conversation": conversation,
        "source_record": row.get("whole line", ""),
        "conversation_parsed": parsed,
        "conversation_truncated": truncated,
        "decision": decision,
        "raw_model_content": raw_content,
        "raw_model_reasoning": raw_reasoning,
        "raw_response_id": raw_response_json.get("id"),
        "model": model,
        "base_url": base_url,
    }

    return {
        "row": row,
        "row_index": row_index,
        "category": category,
        "decision": decision,
        "status": status,
        "resumed": False,
        "result_record": result_record,
    }


def main() -> int:
    args = parse_args()

    input_path = args.input.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    results_path = output_dir / "pii_llm_validation_results.jsonl"
    summary_path = output_dir / "pii_llm_validation_summary.json"
    confirmed_csv_path = output_dir / "pii_llm_confirmed.csv"
    results_temp_path = output_dir / ".pii_llm_validation_results.jsonl.partial"
    confirmed_temp_path = output_dir / ".pii_llm_confirmed.csv.partial"

    if not input_path.exists():
        print(f"Input CSV does not exist: {input_path}", file=sys.stderr)
        return 2

    try:
        base_urls = parse_base_urls(args.base_url)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.requests_per_server < 1:
        print("--requests-per-server must be at least 1", file=sys.stderr)
        return 2

    if args.dry_run:
        total_input_rows = count_rows(input_path, args.limit)
        print(f"Dry run: parsed {total_input_rows:,} rows from {input_path}", file=sys.stderr)
        dry_run_progress = tqdm(
            iter_rows(input_path, args.limit),
            desc="Building dry-run prompts",
            unit="row",
            total=total_input_rows,
            dynamic_ncols=True,
        )
        for row_index, row in enumerate(dry_run_progress, start=1):
            conversation, parsed = conversation_from_whole_line(row.get("whole line", ""))
            candidate = row.get("PII", "")
            if not candidate or candidate not in conversation:
                raise RuntimeError(
                    f"PII source mismatch at {row.get('file')}:{row.get('line')}: "
                    f"{candidate!r} is absent from the reviewed text"
                )
            conversation, truncated = maybe_truncate_conversation(
                conversation,
                args.max_conversation_chars,
                candidate,
            )
            messages = build_messages(row, conversation)
            print_dry_run_payload(
                row_index=row_index,
                row=row,
                messages=messages,
                conversation_parsed=parsed,
                conversation_truncated=truncated,
            )
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    signature = validation_signature(args)
    existing_cache = (
        ExistingResultCache(
            [results_path, results_temp_path],
            output_dir / ".resume_cache.sqlite3",
        )
        if args.resume
        else None
    )
    worker_count = len(base_urls) * args.requests_per_server
    max_pending = args.max_pending or worker_count * 4
    if max_pending < worker_count:
        print("--max-pending must be at least the total worker count", file=sys.stderr)
        return 2

    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_csv": str(input_path),
        "output_dir": str(output_dir),
        "base_urls": base_urls,
        "endpoint_count": len(base_urls),
        "model": args.model,
        "validation_signature": signature,
        "use_response_format": not args.no_response_format,
        "enable_thinking": args.enable_thinking,
        "limit": args.limit,
        "resume": args.resume,
        "requests_per_server": args.requests_per_server,
        "worker_count": worker_count,
        "max_pending": max_pending,
        "total_rows": 0,
        "keep_rows": 0,
        "filter_rows": 0,
        "failed_rows": 0,
        "resumed_rows": 0,
        "by_category": defaultdict(lambda: {"total": 0, "keep": 0, "filter": 0, "failed": 0}),
        "confidence": Counter(),
    }

    with results_temp_path.open("w", encoding="utf-8") as results_handle, (
        confirmed_temp_path.open("w", encoding="utf-8", newline="")
    ) as confirmed_handle:
        fieldnames = REQUIRED_COLUMNS + RESULT_FIELDS
        confirmed_writer = csv.DictWriter(
            confirmed_handle,
            fieldnames=fieldnames,
            lineterminator="\n",
            extrasaction="ignore",
        )
        confirmed_writer.writeheader()

        progress = tqdm(
            desc="LLM-validating PII",
            unit="row",
            dynamic_ncols=True,
        )
        futures: dict[Future[dict[str, Any]], None] = {}
        executors: list[ThreadPoolExecutor] = []

        def record_result(result: dict[str, Any], *, write_result: bool) -> None:
            decision = result["decision"]
            update_summary(
                summary=summary,
                category=result["category"],
                decision=decision,
                status=result["status"],
                resumed=result["resumed"],
            )
            if write_result:
                write_jsonl_record(results_handle, result["result_record"])
            if decision["decision"] == "keep":
                confirmed_writer.writerow(csv_row_with_decision(result["row"], decision))
            progress.set_postfix(
                keep=summary["keep_rows"],
                filter=summary["filter_rows"],
                errors=summary["failed_rows"],
                resumed=summary["resumed_rows"],
            )
            progress.update(1)

        try:
            executors = [
                ThreadPoolExecutor(
                    max_workers=args.requests_per_server,
                    thread_name_prefix=f"llm-{endpoint_index + 1}",
                )
                for endpoint_index in range(len(base_urls))
            ]

            def record_one_completed() -> None:
                future = next(as_completed(tuple(futures)))
                futures.pop(future, None)
                record_result(future.result(), write_result=True)

            work_index = 0
            for row_index, row in enumerate(iter_rows(input_path, args.limit), start=1):
                key = row_key(row, signature)
                category = row.get("category", "")

                result_record = existing_cache.get(key) if existing_cache is not None else None
                if result_record is not None:
                    decision = normalize_decision(result_record.get("decision", {}))
                    record_result(
                        {
                            "row": row,
                            "row_index": row_index,
                            "category": category,
                            "decision": decision,
                            "status": str(result_record.get("status") or "ok"),
                            "resumed": True,
                            "result_record": result_record,
                        },
                        write_result=True,
                    )
                    continue

                endpoint_index = work_index % len(base_urls)
                work_index += 1
                future = executors[endpoint_index].submit(
                    validate_row_with_llm,
                    row_index=row_index,
                    row=row,
                    row_key_value=key,
                    base_url=base_urls[endpoint_index],
                    api_key=args.api_key,
                    model=args.model,
                    temperature=args.temperature,
                    max_tokens=args.max_tokens,
                    timeout=args.timeout,
                    retries=args.retries,
                    retry_delay=args.retry_delay,
                    request_delay=args.request_delay,
                    use_response_format=not args.no_response_format,
                    enable_thinking=args.enable_thinking,
                    max_conversation_chars=args.max_conversation_chars,
                )
                futures[future] = None
                if len(futures) >= max_pending:
                    record_one_completed()

            for future in as_completed(futures):
                record_result(future.result(), write_result=True)
        finally:
            progress.close()
            for executor in executors:
                executor.shutdown(wait=False, cancel_futures=True)

    if existing_cache is not None:
        existing_cache.close()
    results_temp_path.replace(results_path)
    confirmed_temp_path.replace(confirmed_csv_path)
    write_summary(summary_path, summary)

    print(f"Wrote detailed decisions to {results_path}")
    print(f"Wrote confirmed PII CSV to {confirmed_csv_path}")
    print(f"Wrote summary JSON to {summary_path}")
    print(f"Kept {summary['keep_rows']:,} of {summary['total_rows']:,} rows")
    if summary["failed_rows"]:
        print(f"Rows with LLM/API failures: {summary['failed_rows']:,}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
