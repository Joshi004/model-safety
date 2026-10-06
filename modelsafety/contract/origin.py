"""The ``_origin`` note on Prepared rows, and the skipped-rows tripwire.

Standard library only, so launchers that run with the system ``python3`` can
import it.

A skipped-rows entry (``skipped_rows.ndjson``) always starts with
``{"_origin"``, because the writer puts ``_origin`` first. No Prepared row
starts that way: there the note sits inside ``metadata``. So the first few
bytes of a line tell the two apart.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from modelsafety.contract.fingerprints import NON_JSON_KEY

ORIGIN_KEY = "_origin"
SKIPPED_ENTRY_PREFIX = b'{"_origin"'

_MAX_REPR_CHARS = 200


class SkippedRowsEntryError(ValueError):
    """A skipped-rows entry turned up where a Prepared row was expected."""


def json_safe(value: Any) -> Any:
    """A copy of ``value`` that ``json.dumps`` can always write.

    Plain strings, numbers, booleans, nulls, lists and dicts pass through
    unchanged. A value JSON can't hold (a timestamp, raw bytes, a numpy
    scalar, inf/-inf, NaN) is replaced by a marker naming its real Python
    type and the first 200 characters of its real ``repr``.
    """
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    try:
        json.dumps(value, allow_nan=False)
        return value
    except (TypeError, ValueError):
        return {
            NON_JSON_KEY: {
                "type": type(value).__name__,
                "repr": repr(value)[:_MAX_REPR_CHARS],
            }
        }


def is_skipped_entry(raw_line: bytes) -> bool:
    return raw_line.lstrip().startswith(SKIPPED_ENTRY_PREFIX)


def check_line(path: str | Path, line_number: int, raw_line: bytes) -> None:
    """Raise ``SkippedRowsEntryError`` if ``raw_line`` is a skipped-rows entry."""
    if is_skipped_entry(raw_line):
        raise SkippedRowsEntryError(
            f"{path} line {line_number} is a skipped-rows entry, not a Prepared row. "
            "Skipped rows must never be scanned."
        )


def check_first_line(path: str | Path) -> None:
    """Check the first non-blank line of a file."""
    with Path(path).open("rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if raw_line.strip():
                check_line(path, line_number, raw_line)
                return
