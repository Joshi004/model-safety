#!/usr/bin/env python3
"""Normalize mixed benchmark test sets into prompt-only JSONL files."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path
from typing import Any, Iterator


SUPPORTED_SUFFIXES = {".jsonl", ".csv", ".parquet"}


def source_records(path: Path, relative: str) -> Iterator[tuple[int, dict[str, Any]]]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError(f"Expected JSON object at {path}:{line_number}")
                yield line_number, record
        return

    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            if relative.startswith("closed_ended/mmlu/"):
                for line_number, row in enumerate(csv.reader(handle), 1):
                    if len(row) != 6:
                        raise ValueError(f"Expected six MMLU columns at {path}:{line_number}")
                    yield line_number, {
                        "question": row[0],
                        "options": row[1:5],
                        "answer": row[5],
                    }
            else:
                for line_number, record in enumerate(csv.DictReader(handle), 2):
                    yield line_number, dict(record)
        return

    if suffix == ".parquet":
        import pyarrow.parquet as pq

        line_number = 0
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=10_000):
            for record in batch.to_pylist():
                line_number += 1
                yield line_number, record
        return

    raise ValueError(f"Unsupported benchmark file: {path}")


def normalize_messages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    messages = []
    for message in value:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, (str, list)):
            continue
        messages.append(
            {
                "role": str(message.get("role") or "user"),
                "content": content,
            }
        )
    return messages


def options_text(record: dict[str, Any]) -> str:
    options: Any = record.get("options")
    if options is None:
        options = record.get("answer_options")
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except json.JSONDecodeError:
            return options

    pairs: list[tuple[str, Any]] = []
    if isinstance(options, dict):
        pairs = [(str(key), value) for key, value in options.items()]
    elif isinstance(options, list):
        pairs = [(chr(65 + index), value) for index, value in enumerate(options)]
    elif all(key in record for key in ("opa", "opb", "opc", "opd")):
        pairs = [
            ("A", record["opa"]),
            ("B", record["opb"]),
            ("C", record["opc"]),
            ("D", record["opd"]),
        ]
    return "\n".join(f"{key}. {value}" for key, value in pairs if value not in (None, ""))


def prompt_messages(record: dict[str, Any]) -> list[dict[str, Any]]:
    messages = normalize_messages(record.get("messages"))
    if messages:
        return messages

    conversation = record.get("conversation")
    if isinstance(conversation, dict):
        messages = normalize_messages(conversation.get("messages"))
        if messages:
            return messages

    prompt = record.get("prompt")
    messages = normalize_messages(prompt)
    if messages:
        return messages

    text = ""
    for key in (
        "question",
        "Question",
        "QUESTION",
        "harmful_medical_request",
        "Text",
        "text",
        "query",
        "instruction",
        "prompt",
    ):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            break

    contexts = record.get("CONTEXTS")
    if not isinstance(contexts, list):
        contexts = record.get("Knowledge")
    if isinstance(contexts, list):
        context_text = "\n".join(str(item) for item in contexts if item)
        if context_text:
            text = f"Context:\n{context_text}\n\nQuestion:\n{text}"

    options = options_text(record)
    if options:
        text = f"{text}\n\nOptions:\n{options}"
    if not text.strip():
        return []
    return [{"role": "user", "content": text}]


def source_identifier(record: dict[str, Any]) -> str:
    for key in (
        "id",
        "sample_id",
        "question_id",
        "prompt_id",
        "Text ID",
        "canary",
        "canary_string",
    ):
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def output_relative(relative: Path) -> Path:
    if relative.suffix == ".jsonl":
        return relative
    return relative.with_suffix(relative.suffix + ".jsonl")


def prepare(input_root: Path, output_root: Path, overwrite: bool) -> dict[str, Any]:
    input_root = input_root.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    if not input_root.is_dir():
        raise ValueError(f"Benchmark root does not exist: {input_root}")
    if (
        output_root == input_root
        or input_root in output_root.parents
        or output_root in input_root.parents
    ):
        raise ValueError("Input and output roots cannot contain one another")
    if output_root.exists() and any(output_root.iterdir()):
        if not overwrite:
            raise ValueError(f"Output root is not empty: {output_root}; use --overwrite")
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    source_files = sorted(
        path
        for path in input_root.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )
    if not source_files:
        raise ValueError(f"No supported benchmark data files under {input_root}")

    file_reports = []
    total_rows = 0
    total_source_rows = 0
    total_skipped = 0
    skipped_examples: list[dict[str, Any]] = []
    for source in source_files:
        relative = source.relative_to(input_root)
        destination = output_root / output_relative(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        rows = 0
        source_rows = 0
        skipped = 0
        with destination.open("w", encoding="utf-8") as output_handle:
            for source_line, record in source_records(source, relative.as_posix()):
                source_rows += 1
                messages = prompt_messages(record)
                if not messages:
                    skipped += 1
                    if len(skipped_examples) < 20:
                        skipped_examples.append(
                            {
                                "source_file": relative.as_posix(),
                                "source_line": source_line,
                                "keys": sorted(record),
                            }
                        )
                    continue
                output = {
                    "messages": messages,
                    "metadata": {
                        "benchmark_source_file": relative.as_posix(),
                        "benchmark_source_line": source_line,
                        "benchmark_source_format": source.suffix.lower().lstrip("."),
                        "benchmark_id": source_identifier(record),
                        "benchmark_suite": relative.parts[0],
                        "benchmark_dataset": relative.parent.as_posix(),
                    },
                }
                output_handle.write(json.dumps(output, ensure_ascii=False) + "\n")
                rows += 1
        file_reports.append(
            {
                "source_file": relative.as_posix(),
                "output_file": destination.relative_to(output_root).as_posix(),
                "source_rows": source_rows,
                "rows": rows,
                "skipped_no_prompt": skipped,
                "source_bytes": source.stat().st_size,
            }
        )
        total_rows += rows
        total_source_rows += source_rows
        total_skipped += skipped

    report = {
        "input_root": str(input_root),
        "output_root": str(output_root),
        "source_files": len(source_files),
        "source_rows": total_source_rows,
        "rows": total_rows,
        "skipped_no_prompt": total_skipped,
        "skipped_examples": skipped_examples,
        "files": file_reports,
    }
    (output_root / "manifest.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (output_root / "_SUCCESS").touch()
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = prepare(args.input_root, args.output_root, args.overwrite)
    except (ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
