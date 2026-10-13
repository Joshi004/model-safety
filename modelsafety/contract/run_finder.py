#!/usr/bin/env python3
"""Find the runs under a folder, and say which files a scan may read.

A run is the output folder of a tool that keeps its books: ``prepare_rows.py``
(a Prepared run) or ``filter_rows.py`` (a filter run, the Prepared rows that
are left after a filter step). A scan reads only the files of runs that finished: the
run's ``summary.json`` passed its checks, ``_SUCCESS`` exists, and
``output.files_written`` lists each file with its size and SHA-256. Every other
run under the folder, and every ``.jsonl`` that isn't one of those files, is
skipped and reported.

The validator's runs list uses the same run detection (``list_runs``).

Standard library only, so shell launchers can call it with the system
``python3``:

    python3 modelsafety/contract/run_finder.py files --scan-root R [--input-mode runs|folder]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

# Run as a script by the launchers, so only this folder is on sys.path.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modelsafety.contract.fingerprints import compare_files  # noqa: E402
from modelsafety.readers.progress_file import (  # noqa: E402
    END_PHASES as PROGRESS_END_PHASES,
    PHASES as PROGRESS_PHASES,
    PROGRESS_FILENAME,
    PROGRESS_TOOL,
)

PREPARE_ROWS_TOOL = "prepare_rows.py"
# The tool name `run_identity.json` records for the same program.
PREPARE_ROWS_IDENTITY_TOOL = "prepare_rows"
# The same two names for `modelsafety/filter/filter_rows.py`.
FILTER_ROWS_TOOL = "filter_rows.py"
FILTER_ROWS_IDENTITY_TOOL = "filter_rows"
JOBS_FOLDER = "_jobs"
MAX_RUN_FOLDERS = 10_000
# A run that hasn't updated its `progress.json` for this long "may have stopped".
STALE_AFTER_SECONDS = 120
SUCCESS_FILENAME = "_SUCCESS"

# "runs": read the files of finished runs. "folder": read every `.jsonl` under
# the folder, which is what scans did before runs were looked at.
INPUT_MODES = ("runs", "folder")
DEFAULT_INPUT_MODE = "runs"
INPUT_MODE_HELP = (
    "runs (default): read only the files of finished runs under the input folder and "
    "report what was skipped. folder: read every .jsonl under the input folder."
)

SCAN_INPUTS_FILENAME = "scan_inputs.json"
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_MAX_LISTED = 10


class RunFinderError(ValueError):
    """The folder can't be scanned as runs, or a file isn't the one its run wrote."""


@dataclass(frozen=True)
class RunType:
    """How one kind of run names itself in its three files."""

    summary_tool: str
    identity_tool: str
    progress_tool: str
    # What the runs list calls this kind of run.
    kind: str


RUN_KIND_PREPARED = "prepared"
RUN_KIND_FILTER = "filter"

# Adding a kind of run means adding one entry here. A filter run writes no
# `progress.json`, so its `progress_tool` is only the name a stray one would need.
RUN_TYPES = (
    RunType(PREPARE_ROWS_TOOL, PREPARE_ROWS_IDENTITY_TOOL, PROGRESS_TOOL, RUN_KIND_PREPARED),
    RunType(FILTER_ROWS_TOOL, FILTER_ROWS_IDENTITY_TOOL, FILTER_ROWS_TOOL, RUN_KIND_FILTER),
)


# -------------------------------------------------------------------------
# Watching a run (progress.json)
# -------------------------------------------------------------------------


def read_json_or_none(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _parse_time(value: Any) -> datetime | None:
    """A UTC ISO time from a file, or None when it isn't one."""
    if not isinstance(value, str):
        return None
    try:
        time = datetime.fromisoformat(value)
    except ValueError:
        return None
    return time if time.tzinfo is not None else time.replace(tzinfo=timezone.utc)


def read_progress(folder: Path, progress_tool: str = PROGRESS_TOOL) -> dict[str, Any] | None:
    """The folder's `progress.json`, or None when it's missing, unreadable,
    from another tool, or doesn't have a known phase and an `updated_at`."""
    progress = read_json_or_none(folder / PROGRESS_FILENAME)
    if not isinstance(progress, dict) or progress.get("tool") != progress_tool:
        return None
    if progress.get("phase") not in (*PROGRESS_PHASES, *PROGRESS_END_PHASES):
        return None
    if _parse_time(progress.get("updated_at")) is None:
        return None
    return progress


def may_have_stopped(progress: dict[str, Any], now: datetime | None = None) -> bool:
    """True for a run whose last phase isn't `done` or `failed` and whose
    `updated_at` is more than `STALE_AFTER_SECONDS` old."""
    if progress.get("phase") in PROGRESS_END_PHASES:
        return False
    updated_at = _parse_time(progress.get("updated_at"))
    if updated_at is None:
        return False
    age = (now or datetime.now(timezone.utc)) - updated_at
    return age.total_seconds() > STALE_AFTER_SECONDS


# -------------------------------------------------------------------------
# The runs list
# -------------------------------------------------------------------------


def _count(summary: dict[str, Any] | None, section: str, key: str) -> int | None:
    if summary is None:
        return None
    block = summary.get(section)
    value = block.get(key) if isinstance(block, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _match_run_type(identity: Any, summary: Any) -> tuple[RunType, bool] | None:
    """The first run type that `identity` or `summary` names, and whether it is
    the summary that names it. None when neither is from a known tool."""
    for run_type in RUN_TYPES:
        identity_is_run = isinstance(identity, dict) and identity.get("tool") == run_type.identity_tool
        summary_is_run = isinstance(summary, dict) and summary.get("tool") == run_type.summary_tool
        if identity_is_run or summary_is_run:
            return run_type, summary_is_run
    return None


def run_entry(root: Path, folder: Path, file_names: list[str]) -> dict[str, Any] | None:
    """The runs-list entry for `folder`, or None when it isn't a known kind of run.

    Reads `run_identity.json`, `summary.json` and `progress.json`, whichever the folder has.
    """
    identity = None
    if "run_identity.json" in file_names:
        identity = read_json_or_none(folder / "run_identity.json")
    summary = None
    if "summary.json" in file_names:
        summary = read_json_or_none(folder / "summary.json")

    matched = _match_run_type(identity, summary)
    if matched is None:
        return None
    run_type, summary_is_run = matched

    progress = None
    if PROGRESS_FILENAME in file_names:
        progress = read_progress(folder, run_type.progress_tool)

    phase = progress["phase"] if progress is not None else None
    if phase == "failed":
        # A `progress.json` that isn't finished describes the run that is going
        # (or went wrong) now, so a `summary.json` beside it is an earlier run's.
        status = "failed"
        summary = None
    elif phase is not None and phase != "done":
        status = "may_have_stopped" if may_have_stopped(progress) else "in_progress"
        summary = None
    else:
        if not summary_is_run:
            summary = None
        if summary is None:
            status = "no_summary"
        else:
            checks = summary.get("checks")
            passed = isinstance(checks, dict) and checks.get("passed") is True
            status = "checks_passed" if passed else "checks_failed"

    source_of_time = summary if summary is not None else identity
    created_at = source_of_time.get("created_at") if isinstance(source_of_time, dict) else None
    return {
        "path": folder.relative_to(root).as_posix(),
        "run_dir": str(folder),
        "kind": run_type.kind,
        "created_at": created_at if isinstance(created_at, str) else None,
        "status": status,
        "rows_read": _count(summary, "source", "rows_read"),
        "written": _count(summary, "rows", "written"),
        # A Prepared run skips rows; a filter run removes them. Each is None for the other kind.
        "skipped": _count(summary, "rows", "skipped"),
        "removed": _count(summary, "rows", "removed"),
    }


def list_runs(root: Path) -> dict[str, Any]:
    """Every run folder under `root`.

    Only lists folders and reads each candidate's `run_identity.json`,
    `summary.json` and `progress.json`. It doesn't look inside a run folder,
    skips `_jobs`, and stops after `MAX_RUN_FOLDERS` folders (then
    `truncated` is true).
    """
    result: dict[str, Any] = {
        "root": str(root),
        "exists": root.is_dir(),
        "truncated": False,
        "runs": [],
    }
    if not result["exists"]:
        return result

    folders_seen = 0
    for current, dir_names, file_names in os.walk(root):
        folder = Path(current)
        if folder == root:
            dir_names[:] = sorted(name for name in dir_names if name != JOBS_FOLDER)
        else:
            folders_seen += 1
            if folders_seen > MAX_RUN_FOLDERS:
                result["truncated"] = True
                break
            entry = run_entry(root, folder, file_names)
            if entry is not None:
                result["runs"].append(entry)
                dir_names[:] = []
                continue
            dir_names[:] = sorted(dir_names)
    return result


def find_runs(root: Path) -> dict[str, Any]:
    """`list_runs(root)`, except that a `root` that is itself a run is the only run.

    `list_runs` never looks at `root` itself, because the validator's runs root
    holds runs. A scan can be pointed straight at one run.
    """
    if root.is_dir():
        file_names = [entry.name for entry in os.scandir(root) if entry.is_file()]
        own = run_entry(root, root, file_names)
        if own is not None:
            return {"root": str(root), "exists": True, "truncated": False, "runs": [own]}
    return list_runs(root)


# -------------------------------------------------------------------------
# Which files a scan may read
# -------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanFile:
    """One file a scan reads. `path` is relative to the scan folder.

    The other fields come from the run that wrote the file, so they are None
    in folder mode.
    """

    path: str
    run: str | None = None
    rows: int | None = None
    size_bytes: int | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class ScanSelection:
    mode: str
    scan_root: str
    files: list[ScanFile]
    skipped: list[dict[str, Any]]
    runs_found: int = 0


_STATUS_REASONS = {
    "checks_failed": "Its checks failed.",
    "in_progress": "It is still running.",
    "may_have_stopped": (
        f"Its progress.json hasn't been updated for over {STALE_AFTER_SECONDS} seconds, "
        "so it may have stopped."
    ),
    "failed": "It failed.",
    "no_summary": "It has no summary.json from its tool, so it isn't finished.",
}


def _unfinished_reason(run: dict[str, Any]) -> str | None:
    """Why a run can't be read, or None when it finished."""
    status = run["status"]
    if status != "checks_passed":
        return _STATUS_REASONS.get(status, f"Its status is {status}.")
    if not (Path(run["run_dir"]) / SUCCESS_FILENAME).is_file():
        return f"It has no {SUCCESS_FILENAME} file, so it isn't marked finished."
    return None


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _listed_files(run: dict[str, Any]) -> list[ScanFile] | None:
    """The files a finished run says it wrote, relative to the scan folder.

    None when its summary.json doesn't list them. Raises when a listed file is
    malformed, missing or the wrong size, because the run is damaged.
    """
    run_dir = Path(run["run_dir"])
    summary_path = run_dir / "summary.json"
    summary = read_json_or_none(summary_path)
    output = summary.get("output") if isinstance(summary, dict) else None
    written = output.get("files_written") if isinstance(output, dict) else None
    if not isinstance(written, list):
        return None

    files: list[ScanFile] = []
    seen: set[str] = set()
    for index, entry in enumerate(written):
        where = f"{summary_path}, output.files_written entry {index}"
        if not isinstance(entry, dict):
            raise RunFinderError(f"{where} isn't an object.")
        relative = entry.get("path")
        pure = PurePosixPath(relative) if isinstance(relative, str) and relative else None
        if pure is None or pure.is_absolute() or ".." in pure.parts or pure.suffix != ".jsonl":
            raise RunFinderError(
                f"{where} has a path ({relative!r}) that isn't a relative .jsonl path inside the run."
            )
        if relative in seen:
            raise RunFinderError(f"{where} repeats {relative}.")
        seen.add(relative)
        rows, size, sha256 = entry.get("rows"), entry.get("bytes"), entry.get("sha256")
        if not _is_count(rows) or not _is_count(size):
            raise RunFinderError(f"{where} ({relative}) needs whole-number 'rows' and 'bytes'.")
        if not isinstance(sha256, str) or not SHA256_HEX.fullmatch(sha256):
            raise RunFinderError(f"{where} ({relative}) has a 'sha256' that isn't a SHA-256 hex string.")

        data_file = run_dir / relative
        if not data_file.is_file():
            raise RunFinderError(f"{data_file} is listed by {summary_path} but is missing.")
        actual_size = data_file.stat().st_size
        if actual_size != size:
            raise RunFinderError(
                f"{data_file} is {actual_size} bytes, but {summary_path} says the run wrote {size}."
            )
        files.append(
            ScanFile(
                path=(PurePosixPath(run["path"]) / relative).as_posix(),
                run=run["path"],
                rows=rows,
                size_bytes=size,
                sha256=sha256,
            )
        )
    return files


def _stray_files(
    root: Path, runs_by_path: dict[str, dict[str, Any]], chosen: set[str]
) -> list[dict[str, Any]]:
    """Every `.jsonl` under `root` that isn't a chosen file and isn't inside a
    skipped run (a skipped run is reported as a whole)."""
    found: list[str] = []
    for current, dir_names, file_names in os.walk(root):
        folder = Path(current)
        if folder == root:
            dir_names[:] = [name for name in dir_names if name != JOBS_FOLDER]
        for name in file_names:
            if name.endswith(".jsonl"):
                found.append((folder / name).relative_to(root).as_posix())

    strays = []
    for relative in sorted(found):
        if relative in chosen:
            continue
        owner = next(
            (
                parent.as_posix()
                for parent in PurePosixPath(relative).parents
                if parent.as_posix() in runs_by_path
            ),
            None,
        )
        if owner is None:
            reason = "It isn't inside any run folder."
        elif runs_by_path[owner]["_read"]:
            reason = (
                f"It is inside the run {owner}, but that run's summary.json "
                "doesn't list it in output.files_written."
            )
        else:
            continue
        strays.append({"kind": "file", "path": relative, "status": None, "reason": reason})
    return strays


def select_scan_files(scan_root: str | Path) -> ScanSelection:
    """The files of every finished run under `scan_root`, at any depth, and
    what was skipped (unfinished runs, and `.jsonl` files outside the runs'
    lists).

    Raises `RunFinderError` when the folder holds too many folders to search,
    when a finished run's listed file is damaged, and when nothing is left to scan.
    """
    root = Path(scan_root).expanduser().resolve()
    if not root.is_dir():
        raise RunFinderError(f"Scan folder is not a directory: {root}")
    found = find_runs(root)
    if found["truncated"]:
        raise RunFinderError(
            f"{root} holds more than {MAX_RUN_FOLDERS} folders, so not every run could be found. "
            "Point the scan at a smaller folder."
        )

    files: list[ScanFile] = []
    skipped: list[dict[str, Any]] = []
    runs_by_path: dict[str, dict[str, Any]] = {}
    for run in found["runs"]:
        reason = _unfinished_reason(run)
        listed = None
        if reason is None:
            listed = _listed_files(run)
            if listed is None:
                reason = (
                    "Its summary.json doesn't list the files it wrote (output.files_written). "
                    "It was probably made before runs recorded them."
                )
        runs_by_path[run["path"]] = {**run, "_read": listed is not None}
        if listed is None:
            skipped.append(
                {"kind": "run", "path": run["path"], "status": run["status"], "reason": reason}
            )
        else:
            files.extend(listed)

    files.sort(key=lambda item: item.path)
    skipped.extend(_stray_files(root, runs_by_path, {item.path for item in files}))
    selection = ScanSelection("runs", str(root), files, skipped, runs_found=len(found["runs"]))

    if not files:
        lines = [f"Nothing to scan under {root}: no finished run lists any files."]
        lines.extend(_skipped_lines(skipped))
        lines.append("To scan a plain folder of .jsonl files, use the folder input mode.")
        raise RunFinderError("\n".join(lines))
    return selection


def folder_selection(scan_root: str | Path, relative_paths: Iterable[str]) -> ScanSelection:
    """A selection for folder mode: the files the caller found, nothing skipped."""
    root = Path(scan_root).expanduser().resolve()
    return ScanSelection("folder", str(root), [ScanFile(path) for path in relative_paths], [])


def pick_files(selection: ScanSelection, relative_paths: Iterable[str]) -> ScanSelection:
    """`selection` narrowed to the listed files, in the order listed (a pilot,
    a shard or a source manifest). Raises when a listed file isn't in it."""
    by_path = {item.path: item for item in selection.files}
    chosen = []
    for relative in relative_paths:
        item = by_path.get(relative)
        if item is None:
            where = (
                "one of the files of a finished run" if selection.mode == "runs" else "one of the files"
            )
            raise RunFinderError(f"{relative} is listed, but it isn't {where} under {selection.scan_root}.")
        chosen.append(item)
    return replace(selection, files=chosen)


def select_for_mode(scan_root: str | Path, input_mode: str) -> ScanSelection:
    """`select_scan_files` in runs mode, every `.jsonl` under the folder (sorted
    by relative path) in folder mode."""
    if input_mode not in INPUT_MODES:
        raise RunFinderError(f"The input mode must be one of {', '.join(INPUT_MODES)}; got {input_mode!r}.")
    if input_mode == "runs":
        return select_scan_files(scan_root)
    root = Path(scan_root).expanduser().resolve()
    if not root.is_dir():
        raise RunFinderError(f"Scan folder is not a directory: {root}")
    return folder_selection(
        root,
        sorted(path.relative_to(root).as_posix() for path in root.rglob("*.jsonl") if path.is_file()),
    )


def scan_file_paths(scan_root: str | Path, input_mode: str) -> list[str]:
    """The relative paths a scan would read."""
    return [item.path for item in select_for_mode(scan_root, input_mode).files]


def check_fingerprints(
    expected: dict[str, str], read_files: Iterable[dict[str, str]], what: str = "This scan"
) -> None:
    """Raise unless `read_files` (``{"path", "sha256"}`` entries, as a scan
    works them out while reading) are exactly the `expected` ``{path: sha256}``."""
    differences = compare_files(expected, {item["path"]: item["sha256"] for item in read_files})
    if not any(differences.values()):
        return
    problems = []
    for key, text in (
        ("different", "no longer match what their run wrote"),
        ("missing", "weren't read"),
        ("extra", "were read but aren't files of a finished run"),
    ):
        paths = differences[key]
        if paths:
            problems.append(f"{len(paths)} file(s) {text} (e.g. {', '.join(paths[:5])})")
    raise RunFinderError(f"{what} didn't read exactly the files its runs wrote: " + "; ".join(problems) + ".")


# -------------------------------------------------------------------------
# The report
# -------------------------------------------------------------------------


def _skipped_lines(skipped: list[dict[str, Any]]) -> list[str]:
    lines = []
    for item in skipped[:_MAX_LISTED]:
        label = "run" if item["kind"] == "run" else "file"
        status = f" ({item['status']})" if item["status"] else ""
        lines.append(f"  Skipped {label} {item['path']}{status}: {item['reason']}")
    if len(skipped) > _MAX_LISTED:
        lines.append(f"  ... and {len(skipped) - _MAX_LISTED} more skipped (see {SCAN_INPUTS_FILENAME}).")
    return lines


def _counts(selection: ScanSelection) -> dict[str, int]:
    if selection.mode != "runs":
        return {"files": len(selection.files)}
    runs_skipped = sum(1 for item in selection.skipped if item["kind"] == "run")
    return {
        "runs_found": selection.runs_found,
        "runs_read": selection.runs_found - runs_skipped,
        "runs_skipped": runs_skipped,
        "files": len(selection.files),
        "rows": sum(item.rows or 0 for item in selection.files),
        "stray_files": sum(1 for item in selection.skipped if item["kind"] == "file"),
    }


def _file_entry(selection: ScanSelection, item: ScanFile) -> dict[str, Any]:
    if selection.mode != "runs":
        return {"path": item.path}
    return {
        "path": item.path,
        "run": item.run,
        "rows": item.rows,
        "bytes": item.size_bytes,
        "sha256": item.sha256,
    }


def write_scan_inputs(path: Path, selection: ScanSelection) -> None:
    """Write `scan_inputs.json`: which files a scan reads, and what it skipped."""
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": selection.mode,
        "scan_root": selection.scan_root,
        "counts": _counts(selection),
        "files": [_file_entry(selection, item) for item in selection.files],
        "skipped": selection.skipped,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.partial")
    temp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def summarize_scan_inputs(selection: ScanSelection) -> str:
    """A few lines saying what a scan reads and what it skipped."""
    counts = _counts(selection)
    if selection.mode != "runs":
        return f"Scan inputs (every .jsonl under the folder): {selection.scan_root}\n  Files: {counts['files']:,}"
    lines = [
        f"Scan inputs (finished runs only): {selection.scan_root}",
        f"  Runs found: {counts['runs_found']:,} ({counts['runs_read']:,} read, "
        f"{counts['runs_skipped']:,} skipped)  Files: {counts['files']:,}  Rows: {counts['rows']:,}",
    ]
    lines.extend(_skipped_lines(selection.skipped))
    return "\n".join(lines)


# -------------------------------------------------------------------------
# Command line
# -------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    files = subparsers.add_parser(
        "files",
        help="Print the relative paths a scan would read, one per line. "
        "The summary of what was skipped goes to stderr.",
    )
    files.add_argument("--scan-root", type=Path, required=True)
    files.add_argument("--input-mode", choices=INPUT_MODES, default=DEFAULT_INPUT_MODE, help=INPUT_MODE_HELP)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        selection = select_for_mode(args.scan_root, args.input_mode)
    except RunFinderError as exc:
        print(exc, file=sys.stderr)
        return 2
    for item in selection.files:
        print(item.path)
    print(summarize_scan_inputs(selection), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
