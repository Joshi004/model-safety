#!/usr/bin/env python3
"""Read the per-row status written by `row_status.py`: one row, or one page of rows.

Standard library only, so the validator can import it. Nothing is written.

A status folder holds, for each Prepared file, a status file with one entry per
Prepared line (line k is entry k) and an index of byte offsets. The index has
one line for each entry plus one: each line is the offset of an entry in the
status file as 20 digits and a newline (21 bytes), and the last one is the
size of the status file. So entry k starts at offset line k and ends where
offset line k+1 starts, and reading it takes one read of the index and one of
the status file, however large the files are.

    python3 modelsafety/trace/row_status_read.py show --status-dir D --run R --file F --line N [--count K]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any

SUMMARY_FILENAME = "summary.json"
SUCCESS_FILENAME = "_SUCCESS"
STATUS_TOOL = "row_status.py"
STATUS_IDENTITY_TOOL = "row_status"

ROWS_FOLDER = "rows"
STATUS_SUFFIX = ".ndjson"
OFFSETS_SUFFIX = ".offsets"
OFFSET_DIGITS = 20
OFFSET_LINE_BYTES = OFFSET_DIGITS + 1
# One page can't be larger than this, so a request can't read a whole file.
MAX_PAGE_ROWS = 1000


class RowStatusReadError(ValueError):
    """The status can't be read, or the row asked for isn't in it."""


def status_paths(run: str, file: str) -> tuple[str, str]:
    """The status file and the offsets file of a Prepared file, relative to the
    status folder. `run` is the run's path under the scanned folder, and `file`
    the Prepared file's path inside the run."""
    base = PurePosixPath(run) / ROWS_FOLDER / PurePosixPath(file).with_suffix("")
    return f"{base}{STATUS_SUFFIX}", f"{base}{OFFSETS_SUFFIX}"


def offset_line(offset: int) -> bytes:
    """One line of an offsets file."""
    if offset < 0 or offset >= 10**OFFSET_DIGITS:
        raise RowStatusReadError(f"The offset {offset} doesn't fit in {OFFSET_DIGITS} digits.")
    return f"{offset:0{OFFSET_DIGITS}d}\n".encode("ascii")


def read_summary(status_dir: str | Path) -> dict[str, Any]:
    """The summary of a finished status folder. Raises if the folder has no
    `_SUCCESS`, or its summary isn't a row status summary."""
    folder = Path(status_dir)
    if not (folder / SUCCESS_FILENAME).is_file():
        raise RowStatusReadError(f"{folder} has no {SUCCESS_FILENAME}, so it isn't a finished row status.")
    try:
        summary = json.loads((folder / SUMMARY_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RowStatusReadError(f"{folder / SUMMARY_FILENAME} can't be read: {exc}") from exc
    if not isinstance(summary, dict) or summary.get("tool") != STATUS_TOOL or not isinstance(
        summary.get("runs"), dict
    ):
        raise RowStatusReadError(f"{folder / SUMMARY_FILENAME} isn't the summary of a row status.")
    return summary


def _file_entry(summary: dict[str, Any], run: str, file: str) -> dict[str, Any]:
    run_entry = summary["runs"].get(run)
    files = run_entry.get("files") if isinstance(run_entry, dict) else None
    if not isinstance(files, dict):
        raise RowStatusReadError(f"The row status has no run {run!r}.")
    entry = files.get(file)
    if not isinstance(entry, dict):
        raise RowStatusReadError(f"The run {run!r} has no Prepared file {file!r} in the row status.")
    return entry


def _inside(folder: Path, relative: Any) -> Path:
    """`relative` as a path inside `folder`. Only paths the summary lists get here,
    but a summary is just a file, so check anyway."""
    if not isinstance(relative, str) or not relative:
        raise RowStatusReadError("The summary has a file entry with no path.")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise RowStatusReadError(f"The summary lists {relative!r}, which isn't inside the status folder.")
    return folder / pure


def read_rows(
    status_dir: str | Path,
    run: str,
    file: str,
    first_line: int,
    count: int = 1,
    *,
    summary: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The entries of Prepared lines `first_line` to `first_line + count - 1` of
    `file` in `run`, without reading the rest of the file. A page that runs past
    the last line is cut short. Pass `summary` (from `read_summary`) to read many
    pages without reading the summary each time."""
    folder = Path(status_dir)
    summary = summary if summary is not None else read_summary(folder)
    entry = _file_entry(summary, run, file)
    rows = entry.get("rows")
    if isinstance(first_line, bool) or not isinstance(first_line, int) or isinstance(rows, bool) or not isinstance(rows, int):
        raise RowStatusReadError("The line and the row count must be whole numbers.")
    if not 1 <= first_line <= rows:
        raise RowStatusReadError(f"Line {first_line} isn't a line of {run}/{file} (it has {rows:,} rows).")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_PAGE_ROWS:
        raise RowStatusReadError(f"A page has between 1 and {MAX_PAGE_ROWS:,} rows, not {count!r}.")
    count = min(count, rows - first_line + 1)

    status_path = _inside(folder, entry.get("status_file"))
    offsets_path = _inside(folder, entry.get("offsets_file"))
    try:
        with offsets_path.open("rb") as handle:
            handle.seek((first_line - 1) * OFFSET_LINE_BYTES)
            block = handle.read((count + 1) * OFFSET_LINE_BYTES)
        if len(block) != (count + 1) * OFFSET_LINE_BYTES:
            raise RowStatusReadError(f"{offsets_path} is shorter than the {rows:,} rows it should index.")
        offsets = [
            int(block[start : start + OFFSET_DIGITS])
            for start in range(0, len(block), OFFSET_LINE_BYTES)
        ]
        if any(later <= earlier for earlier, later in zip(offsets, offsets[1:])):
            raise RowStatusReadError(f"{offsets_path} doesn't have increasing offsets after line {first_line}.")
        with status_path.open("rb") as handle:
            handle.seek(offsets[0])
            data = handle.read(offsets[-1] - offsets[0])
    except (OSError, ValueError) as exc:
        if isinstance(exc, RowStatusReadError):
            raise
        raise RowStatusReadError(f"Can't read the status of {run}/{file}: {exc}") from exc
    if len(data) != offsets[-1] - offsets[0]:
        raise RowStatusReadError(f"{status_path} is shorter than its index says.")

    result = []
    for number, (start, end) in enumerate(zip(offsets, offsets[1:]), first_line):
        try:
            row = json.loads(data[start - offsets[0] : end - offsets[0]])
        except ValueError as exc:
            raise RowStatusReadError(f"{status_path} entry {number} isn't JSON: {exc}") from exc
        prepared = row.get("prepared") if isinstance(row, dict) else None
        if not isinstance(prepared, dict) or prepared.get("line") != number:
            raise RowStatusReadError(f"{status_path} entry {number} isn't the status of line {number}.")
        result.append(row)
    return result


def read_row(
    status_dir: str | Path,
    run: str,
    file: str,
    line: int,
    *,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The status of one Prepared line."""
    return read_rows(status_dir, run, file, line, 1, summary=summary)[0]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Show the status of one Prepared row, or a page of them.")
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="Print rows as JSON.")
    show.add_argument("--status-dir", type=Path, required=True, help="A finished row status folder.")
    show.add_argument("--run", required=True, help="The run's path under the scanned folder.")
    show.add_argument("--file", required=True, help="The Prepared file's path inside the run.")
    show.add_argument("--line", type=int, required=True, help="The first line.")
    show.add_argument("--count", type=int, default=1, help="How many lines (default 1).")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rows = read_rows(args.status_dir, args.run, args.file, args.line, args.count)
    except RowStatusReadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for row in rows:
        print(json.dumps(row, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
