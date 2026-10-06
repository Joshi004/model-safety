"""Fingerprints that tie a Prepared row, or a file, back to what was read.

Standard library only, so shell launchers can import it with the system
``python3``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# Bump when the recipe for row_fingerprint() changes, so old _origin notes
# can still be checked with the recipe they were made with.
FINGERPRINT_RECIPE_VERSION = 1

NON_JSON_KEY = "__non_json_value__"


def line_fingerprint(raw_line: bytes) -> str:
    """SHA-256 of one JSONL line's bytes, without the line ending."""
    return hashlib.sha256(raw_line.rstrip(b"\r\n")).hexdigest()


def _non_json_marker(value: Any) -> dict[str, Any]:
    return {NON_JSON_KEY: {"type": type(value).__name__, "repr": repr(value)}}


def row_fingerprint(row: dict[str, Any]) -> str:
    """SHA-256 of a row written as JSON with its keys in sorted order."""
    text = json.dumps(
        row,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_non_json_marker,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def files_list_fingerprint(files: list[dict[str, Any]]) -> str:
    """One SHA-256 for a list of ``{"path": ..., "sha256": ...}`` entries."""
    pairs = sorted([item["path"], item["sha256"]] for item in files)
    text = json.dumps(pairs, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
