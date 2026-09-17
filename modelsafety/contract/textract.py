"""Shared source-text and snapshot contract for the PII pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def extract_record_text(record: dict[str, Any]) -> str:
    """Return the exact text scanned and later reviewed by the LLM."""
    text = record.get("text")
    if isinstance(text, str) and text:
        return text

    messages = record.get("messages")
    if not isinstance(messages, list):
        return ""
    rendered = []
    for index, message in enumerate(messages, 1):
        if not isinstance(message, dict):
            continue
        content = extract_content_text(message.get("content"))
        if content:
            role = str(message.get("role") or f"message_{index}").upper()
            rendered.append(f"{role}:\n{content}")
    return "\n\n".join(rendered)


def parse_record_text(raw_line: str) -> str:
    try:
        record = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        raise ValueError("source row is not valid JSON") from exc
    if not isinstance(record, dict):
        raise ValueError("source row must be a JSON object")
    return extract_record_text(record)


def load_source_snapshot(
    manifest_path: Path,
    source_root: Path,
) -> dict[str, dict[str, Any]]:
    metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_root = Path(metadata["input_root"]).resolve()
    actual_root = source_root.resolve()
    if expected_root != actual_root:
        raise RuntimeError(
            f"Snapshot input root is {expected_root}, but source root is {actual_root}"
        )
    snapshots = {str(item["relative_path"]): item for item in metadata["files"]}
    for relative, snapshot in snapshots.items():
        digest = str(snapshot.get("sha256") or "")
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise RuntimeError(
                f"Snapshot lacks a valid SHA-256 for {relative}; rebuild the manifest"
            )
    return snapshots


def verify_source_snapshot(source: Path, expected: dict[str, Any]) -> None:
    stat = source.stat()
    expected_size = int(expected["size_bytes"])
    expected_mtime = int(expected["mtime_ns"])
    if stat.st_size != expected_size or stat.st_mtime_ns != expected_mtime:
        raise RuntimeError(
            f"Source changed after manifest creation: {source} "
            f"(expected size={expected_size} mtime_ns={expected_mtime}, "
            f"found size={stat.st_size} mtime_ns={stat.st_mtime_ns})"
        )
