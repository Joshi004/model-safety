"""Fingerprints that tie a Prepared row, or a file, back to what was read.

Standard library only, so shell launchers can import it with the system
``python3``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from modelsafety.contract.project_paths import to_saved

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


def input_block(root: str, files: list[dict[str, Any]]) -> dict[str, Any]:
    """The "what this run read" block every scan summary carries.

    ``root`` is saved relative to the project root when it is inside it (see
    ``project_paths``), so the summary still matches the folder after the project is moved.
    Read it back with ``from_saved``.
    """
    return {"root": to_saved(root), "files": files, "fingerprint": files_list_fingerprint(files)}


def compare_files(expected: dict[str, str], actual: dict[str, str]) -> dict[str, list[str]]:
    """Compare two ``{path: sha256}`` maps. Each returned list is sorted."""
    return {
        "missing": sorted(expected.keys() - actual.keys()),
        "extra": sorted(actual.keys() - expected.keys()),
        "different": sorted(
            path for path in expected.keys() & actual.keys() if expected[path] != actual[path]
        ),
    }
