#!/usr/bin/env python3
"""Find the Prepared row an Original row became: the other direction from "Show original".

A Prepared row carries a note (`metadata._origin`) saying which Original row it
came from, but nothing points the other way. This answers "Original file F,
row N: where is it in the Prepared run?".

When the answer can be worked out, it is, and then checked against the row it
points at. The preparing step writes the rows it keeps as one sequence, in
the order it read them, cut into files by `output.files_written`. So the
position of Original row N is the number of rows kept from the files before
its file, plus N, if nothing before row N in its own file was dropped and the
run wasn't shuffled. That is true when the run's own counts say so:

  - the run was not shuffled (`run_identity.json`, settings.shuffle),
  - the Original file had no skipped rows (and, for JSONL, no blank lines, because
    a JSONL row is named by its line number); for an Arrow folder, the run skipped none.

The Prepared line found that way is read and its `_origin` note compared with the
Original row asked for. If any of this doesn't hold, or the note doesn't match, the
Prepared files are read line by line and every note compared; then the run's
`skipped_rows.ndjson`. Searching reads the whole run in the worst case (a run of
over a million rows takes tens of seconds).

`status` says what was found:

  found        the Prepared row; `prepared` says the file and line
  skipped      the preparing step skipped it (a required column was missing); `skipped`
               says why and where in skipped_rows.ndjson
  left_out     the run was started with a row limit (--max-rows) and the row is past it
  not_in_file  the Original file has no such row
  not_found    none of these, which the run's own counts say can't happen (a blank JSONL
               line is the one case: it isn't a row)

`method` says how it was found: `computed` (checked against the note) or `searched`.
Whether the Original row is still the same as when it was converted is a separate
question, answered by `lookup()` in original.py (the validator's "Show original").

Nothing is changed in any folder it reads. It needs the readers' packages (through
`original.py`), so run it with the readers' Python:

    modelsafety/readers/.venv/bin/python modelsafety/trace/find_prepared.py show \\
        --run-dir D --path ORIGINAL_FILE (--row N | --line N) [--split S]

Exit code: 0 if the row was found, 1 for any other answer, 2 if the run can't be used.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any

# Run as a script, so only this folder is on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modelsafety.contract.origin import ORIGIN_KEY  # noqa: E402
from modelsafety.trace.original import RunError, open_run  # noqa: E402

# original.py has put the readers' folder on sys.path, and a bare import gives the
# same module that the preparing step and the validator use.
import prepare_rows  # noqa: E402

SKIPPED_ROWS_FILENAME = prepare_rows.SKIPPED_ROWS_FILENAME
DEFAULT_METADATA_COLUMN = "metadata"

FOUND = "found"
SKIPPED = "skipped"
LEFT_OUT = "left_out"
NOT_IN_FILE = "not_in_file"
NOT_FOUND = "not_found"

COMPUTED = "computed"
SEARCHED = "searched"

_NOTES = {
    LEFT_OUT: "The run was started with a row limit (--max-rows), and this row is past it, so it was never converted.",
    NOT_IN_FILE: "The Original file has no such row.",
    NOT_FOUND: (
        "The run has no row from it and didn't skip it, which its own counts say can't happen "
        "(unless the line is a blank line in a JSONL file, which isn't a row)."
    ),
}


# -------------------------------------------------------------------------
# What a run says about itself
# -------------------------------------------------------------------------


def _whole_number(value: Any, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RunError(f"The {name} must be a whole number of at least {minimum}.")
    return value


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _source_files(summary: dict[str, Any]) -> list[dict[str, Any]]:
    source = summary.get("source")
    files = source.get("files") if isinstance(source, dict) else None
    if not isinstance(files, list) or not all(isinstance(item, dict) for item in files):
        raise RunError("This run's summary.json has no source.files list.")
    return files


def _written_files(summary: dict[str, Any]) -> list[dict[str, Any]]:
    output = summary.get("output")
    files = output.get("files_written") if isinstance(output, dict) else None
    if not isinstance(files, list) or not all(
        isinstance(item, dict) and isinstance(item.get("path"), str) and _is_count(item.get("rows")) for item in files
    ):
        raise RunError("This run's summary.json doesn't list the files it wrote (output.files_written).")
    return files


def _settings(identity: Any) -> dict[str, Any]:
    settings = identity.get("settings") if isinstance(identity, dict) else None
    return settings if isinstance(settings, dict) else {}


def run_reads_path(summary: dict[str, Any], identity: Any, path: str) -> bool:
    """Whether the run read `path`: a file listed in its `source.files`, or, for an
    Arrow folder, the folder it took as its input (a note names the folder, which
    isn't itself in `source.files`)."""
    input_info = summary.get("input")
    if isinstance(input_info, dict) and input_info.get("type") == "huggingface":
        return isinstance(identity, dict) and identity.get("input_root") == path
    try:
        return any(item.get("path") == path for item in _source_files(summary))
    except RunError:
        return False


def _prepared_file(run_dir: Path, relative: str) -> Path:
    """A file the run's summary says it wrote. A summary is just a file, so check
    that the path stays inside the run."""
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise RunError(f"This run's summary.json lists {relative!r}, which isn't inside the run folder.")
    return run_dir / pure


# -------------------------------------------------------------------------
# Matching an _origin note
# -------------------------------------------------------------------------


class _Wanted:
    """The Original row being looked for, as an `_origin` note names it."""

    def __init__(self, path: str, input_type: str, number: int, split: str | None) -> None:
        self.path = path
        # A JSONL row is named by its line (from 1), every other kind by its row (from 0).
        self.key = "line" if input_type == "jsonl" else "row"
        self.number = number
        self.split = split

    def matches(self, origin: Any) -> bool:
        if not isinstance(origin, dict) or origin.get("path") != self.path:
            return False
        value = origin.get(self.key)
        if isinstance(value, bool) or value != self.number:
            return False
        return self.split is None or origin.get("split") == self.split


def _origin_of(raw: bytes, column: str) -> Any:
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    metadata = record.get(column) if isinstance(record, dict) else None
    return metadata.get(ORIGIN_KEY) if isinstance(metadata, dict) else None


# -------------------------------------------------------------------------
# Working the position out
# -------------------------------------------------------------------------


def _computed(
    summary: dict[str, Any],
    identity: Any,
    input_type: str,
    path: str,
    number: int,
) -> tuple[str, int | None] | None:
    """`(FOUND, position)` (the row's place in the run's whole sequence of Prepared
    rows, from 0), `(LEFT_OUT, None)` or `(NOT_IN_FILE, None)`, when the run's own
    counts allow working it out. None when they don't (a shuffled run, or rows
    dropped before this one, or counts that aren't there)."""
    if _settings(identity).get("shuffle") is not False:
        return None

    if input_type == "huggingface":
        # A note's `row` counts across the whole dataset, so one skipped row
        # anywhere would move every row after it.
        rows = summary.get("rows")
        source = summary.get("source")
        written = rows.get("written") if isinstance(rows, dict) else None
        skipped = rows.get("skipped") if isinstance(rows, dict) else None
        expected = source.get("expected_rows") if isinstance(source, dict) else None
        if not _is_count(written) or skipped != 0 or not _is_count(expected):
            return None
        if number < written:
            return FOUND, number
        return (LEFT_OUT if number < expected else NOT_IN_FILE), None

    sources = _source_files(summary)
    index = next((at for at, item in enumerate(sources) if item.get("path") == path), None)
    if index is None:
        return None
    entry = sources[index]
    written, skipped, left_out = entry.get("rows_written"), entry.get("rows_skipped"), entry.get("rows_left_out")
    if not (_is_count(written) and _is_count(left_out)) or skipped != 0:
        return None
    if input_type == "jsonl" and entry.get("blank_lines") != 0:
        return None
    before = 0
    for earlier in sources[:index]:
        if not _is_count(earlier.get("rows_written")):
            return None
        before += earlier["rows_written"]

    row = number - 1 if input_type == "jsonl" else number
    if row < written:
        return FOUND, before + row
    return (LEFT_OUT if row < written + left_out else NOT_IN_FILE), None


def _locate(written_files: list[dict[str, Any]], position: int) -> tuple[dict[str, Any], int] | None:
    """The written file that holds the row at `position` of the whole sequence,
    and its line there (from 1)."""
    start = 0
    for item in written_files:
        if position < start + item["rows"]:
            return item, position - start + 1
        start += item["rows"]
    return None


def _read_line(path: Path, line: int) -> bytes | None:
    with path.open("rb") as handle:
        for number, raw in enumerate(handle, 1):
            if number == line:
                return raw
    return None


# -------------------------------------------------------------------------
# Searching
# -------------------------------------------------------------------------


def _search(
    run_dir: Path, written_files: list[dict[str, Any]], column: str, wanted: _Wanted
) -> tuple[dict[str, Any], int, dict[str, Any]] | None:
    """The first Prepared line whose note is the Original row: its file entry, its
    line (from 1) and the note. Reads the Prepared files in order."""
    for item in written_files:
        with _prepared_file(run_dir, item["path"]).open("rb") as handle:
            for line, raw in enumerate(handle, 1):
                origin = _origin_of(raw, column)
                if wanted.matches(origin):
                    return item, line, origin
    return None


def _find_skipped(run_dir: Path, wanted: _Wanted) -> dict[str, Any] | None:
    """The entry of `skipped_rows.ndjson` for the Original row: where in the file it
    is (from 1), the reason and the column that was missing."""
    skipped_path = run_dir / SKIPPED_ROWS_FILENAME
    if not skipped_path.is_file():
        return None
    with skipped_path.open("rb") as handle:
        for number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except ValueError as exc:
                raise RunError(f"{SKIPPED_ROWS_FILENAME} line {number} isn't valid JSON: {exc}") from exc
            if isinstance(entry, dict) and wanted.matches(entry.get(ORIGIN_KEY)):
                return {"number": number, "reason": entry.get("reason"), "column": entry.get("column")}
    return None


def _absent_status(summary: dict[str, Any], input_type: str, path: str, number: int) -> str:
    """Why an Original row that no Prepared row and no skipped entry names isn't
    there, from the run's own counts: every row of an Original file is written,
    skipped or left out, so it is past the end, left out, or unexplained."""
    if input_type == "huggingface":
        source = summary.get("source")
        expected = source.get("expected_rows") if isinstance(source, dict) else None
        if _is_count(expected) and number >= expected:
            return NOT_IN_FILE
        input_info = summary.get("input")
        return LEFT_OUT if isinstance(input_info, dict) and input_info.get("max_rows") is not None else NOT_FOUND

    entry = next((item for item in _source_files(summary) if item.get("path") == path), {})
    if input_type == "jsonl":
        if _is_count(entry.get("lines")) and number > entry["lines"]:
            return NOT_IN_FILE
    else:
        parts = [entry.get(key) for key in ("rows_written", "rows_skipped", "rows_left_out")]
        if all(_is_count(part) for part in parts) and number >= sum(parts):
            return NOT_IN_FILE
    return LEFT_OUT if _is_count(entry.get("rows_left_out")) and entry["rows_left_out"] > 0 else NOT_FOUND


# -------------------------------------------------------------------------
# The lookup
# -------------------------------------------------------------------------


def find_prepared_row(
    run_dir: str | Path,
    path: str,
    *,
    row: int | None = None,
    line: int | None = None,
    split: str | None = None,
) -> dict[str, Any]:
    """Where the Original row `path`, `row` (or `line`, for a JSONL file) is in the
    Prepared run `run_dir`. See the top of this file for the answer's `status` and `method`.

    `path` is the Original file as the run recorded it (an absolute path; for an
    Arrow folder, the folder). A JSONL file is asked for by `line` (from 1) and every
    other kind by `row` (from 0). `split` is only checked when given.

    Raises `RunError` if `run_dir` isn't a finished Prepared run, the run didn't read
    `path` (a lookup only follows a file the run's own summary.json lists), or the
    row number isn't the right kind for the run.
    """
    run = open_run(Path(run_dir))
    run_path = Path(run["run_dir"])
    summary, identity = run["summary"], run["run_identity"]
    input_info = summary.get("input")
    input_type = input_info.get("type") if isinstance(input_info, dict) else None
    if not isinstance(input_type, str):
        raise RunError("This run's summary.json has no input.type.")

    if input_type == "jsonl":
        if row is not None or line is None:
            raise RunError("A JSONL file's rows are named by line: give `line` (counting from 1), not `row`.")
        number = _whole_number(line, "line", 1)
    else:
        if line is not None or row is None:
            raise RunError(f"A {input_type} file's rows are named by row: give `row` (counting from 0), not `line`.")
        number = _whole_number(row, "row", 0)
    if split is not None and not isinstance(split, str):
        raise RunError("The split must be text.")
    if not isinstance(path, str) or not run_reads_path(summary, identity, path):
        raise RunError(
            f"This run didn't read {path}, so it has no row from it. A lookup only follows a file "
            "listed in the run's summary.json (source.files)."
        )

    written_files = _written_files(summary)
    column = _settings(identity).get("metadata_column")
    column = column if isinstance(column, str) and column else DEFAULT_METADATA_COLUMN
    wanted = _Wanted(path, input_type, number, split)

    def result(status: str, method: str, **extra: Any) -> dict[str, Any]:
        return {
            "status": status,
            "method": method,
            "run_dir": str(run_path),
            "path": path,
            wanted.key: number,
            "split": split,
            "prepared": None,
            "origin": None,
            "skipped": None,
            "note": _NOTES.get(status),
            **extra,
        }

    note = None
    worked_out = _computed(summary, identity, input_type, path, number)
    if worked_out is not None:
        status, position = worked_out
        if status != FOUND:
            return result(status, COMPUTED)
        located = _locate(written_files, position) if position is not None else None
        if located is not None:
            item, at = located
            raw = _read_line(_prepared_file(run_path, item["path"]), at)
            origin = _origin_of(raw, column) if raw is not None else None
            if wanted.matches(origin):
                return result(
                    FOUND, COMPUTED, prepared={"run_dir": str(run_path), "file": item["path"], "line": at}, origin=origin
                )
        note = "The place worked out from the run's counts didn't hold, so the run was searched."

    found = _search(run_path, written_files, column, wanted)
    if found is not None:
        item, at, origin = found
        return result(
            FOUND, SEARCHED, prepared={"run_dir": str(run_path), "file": item["path"], "line": at}, origin=origin, note=note
        )
    skipped = _find_skipped(run_path, wanted)
    if skipped is not None:
        return result(SKIPPED, SEARCHED, skipped=skipped, note=note)
    status = _absent_status(summary, input_type, path, number)
    return result(status, SEARCHED, note=" ".join(part for part in (note, _NOTES.get(status)) if part) or None)


# -------------------------------------------------------------------------
# Command line
# -------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="Print where an Original row is in a Prepared run, as JSON.")
    show.add_argument("--run-dir", type=Path, required=True, help="The Prepared run's output folder.")
    show.add_argument("--path", required=True, help="The Original file, as the run recorded it (an absolute path).")
    kind = show.add_mutually_exclusive_group(required=True)
    kind.add_argument("--row", type=int, help="The row, counting from 0 (every kind of file but JSONL).")
    kind.add_argument("--line", type=int, help="The line, counting from 1 (a JSONL file).")
    show.add_argument("--split", help="The Arrow split, if the run read one.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = find_prepared_row(args.run_dir, args.path, row=args.row, line=args.line, split=args.split)
    except RunError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == FOUND else 1


if __name__ == "__main__":
    raise SystemExit(main())
