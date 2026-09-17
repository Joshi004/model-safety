from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


CONTEXTUAL_LABELS = {
    "age",
    "blood_type",
    "city",
    "company_name",
    "country",
    "county",
    "date",
    "date_time",
    "education_level",
    "employment_status",
    "gender",
    "language",
    "marital_status",
    "nationality",
    "occupation",
    "political_view",
    "private_date",
    "race_ethnicity",
    "religious_belief",
    "sexuality",
    "state",
    "time",
}
PERSON_LINKED_LABELS = {"first_name", "last_name", "private_person"}


def label_tier(label: str) -> str:
    if label in PERSON_LINKED_LABELS:
        return "person_linked"
    if label in CONTEXTUAL_LABELS:
        return "contextual"
    return "direct_identifier"


def is_actionable_label(label: str) -> bool:
    return label_tier(label) != "contextual"


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


@dataclass(frozen=True)
class WorkUnit:
    unit_id: str
    source_file: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    source_size: int
    source_mtime_ns: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkUnit":
        return cls(
            unit_id=str(value["unit_id"]),
            source_file=str(value["source_file"]),
            start_byte=int(value["start_byte"]),
            end_byte=int(value["end_byte"]),
            start_line=int(value["start_line"]),
            end_line=int(value["end_line"]),
            source_size=int(value["source_size"]),
            source_mtime_ns=int(value["source_mtime_ns"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "source_file": self.source_file,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "source_size": self.source_size,
            "source_mtime_ns": self.source_mtime_ns,
        }


def make_unit_id(source_file: str, start_byte: int, end_byte: int) -> str:
    payload = f"{source_file}\0{start_byte}\0{end_byte}".encode()
    return hashlib.sha256(payload).hexdigest()[:20]


def load_work_units(path: Path) -> list[WorkUnit]:
    units: list[WorkUnit] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                units.append(WorkUnit.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Malformed work unit at {path}:{line_number}") from exc
    if not units:
        raise ValueError(f"Work manifest has no units: {path}")
    return units


def verify_source(root: Path, unit: WorkUnit) -> Path:
    source = (root / unit.source_file).resolve()
    try:
        source.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"Work unit escapes input root: {unit.source_file}") from exc
    stat = source.stat()
    if stat.st_size != unit.source_size or stat.st_mtime_ns != unit.source_mtime_ns:
        raise RuntimeError(f"Source changed after manifest creation: {unit.source_file}")
    return source


def iter_unit_rows(root: Path, unit: WorkUnit) -> Iterator[dict[str, Any]]:
    source = verify_source(root, unit)
    with source.open("rb") as handle:
        handle.seek(unit.start_byte)
        source_line = unit.start_line
        while handle.tell() < unit.end_byte:
            raw_line = handle.readline()
            if not raw_line:
                break
            try:
                text_line = raw_line.decode("utf-8")
                record = json.loads(text_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Malformed JSON at {source}:{source_line}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"JSON row is not an object at {source}:{source_line}")
            yield {
                "source_file": unit.source_file,
                "source_line": source_line,
                "whole_line": text_line.rstrip("\r\n"),
                "record": record,
            }
            source_line += 1


def iter_record_texts(row: dict[str, Any]) -> Iterator[dict[str, Any]]:
    record = row["record"]
    text = record.get("text")
    if isinstance(text, str) and text:
        yield {
            **row,
            "message_index": None,
            "role": "text",
            "text": text,
        }
        return
    messages = record.get("messages")
    if not isinstance(messages, list):
        return
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        text = content_text(message.get("content"))
        if not text:
            continue
        yield {
            **row,
            "message_index": message_index,
            "role": str(message.get("role") or ""),
            "text": text,
        }


def atomic_replace(temp_path: Path, final_path: Path) -> None:
    os.replace(temp_path, final_path)


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.partial")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)

