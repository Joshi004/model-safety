#!/usr/bin/env python3
"""Follow every finding back to its Original row, and say whether each link holds.

A finding names a file, a line and that line's fingerprint. At every step of a
chain the trace opens that line, checks its fingerprint, and moves one step
back: through a filter run's line map to the line in its parent run, until it
reaches a Prepared run. There it reads the row's `_origin` note and looks the
Original row up (the same code as the validator's "Show original", in
`modelsafety/trace/original.py`).

It writes one line per finding to `<output-dir>/<source>.ndjson`, with the
whole walk (every hop, the Prepared row, the Original location) and a status:

  unchanged      every fingerprint matched, down to the Original row
  changed        the Original row is no longer the one that was converted
  not_found      the Original file or row isn't there any more
  not_supported  the row can't be looked up (read from the Hub, or in a
                 duplicates reference folder)
  broken_link    a fingerprint didn't match, or a step couldn't be made; the
                 entry says at which hop and why

It also traces each removed row of a filter step (`--filter-root`), from the
line it was removed from.

Nothing is changed in any folder it reads. Results go in a new folder, outside
every folder it reads. `summary.json` has the totals; `_SUCCESS` is written only
if every source wrote exactly the findings it read and the findings for each
file match the scan's own count.

It needs pandas, pyarrow and datasets, so run it with the readers' Python:

    modelsafety/readers/.venv/bin/python modelsafety/trace/trace_findings.py --output-dir O \\
        [--pii-run-dir D] [--ner-run-dir D] [--toxicity-run-dir D] [--dedup-run-dir D] \\
        [--filter-root R ...]

Exit code: 0 if every finding is unchanged or not_supported, 1 if the trace
finished but some are changed, not_found or broken_link, 2 if it refused to
start or a check failed (no `_SUCCESS`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, deque
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

# Run as a script, so only this folder is on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modelsafety.contract.fingerprints import line_fingerprint  # noqa: E402
from modelsafety.contract.origin import ORIGIN_KEY, SkippedRowsEntryError, check_line  # noqa: E402
from modelsafety.contract.run_finder import (  # noqa: E402
    FILTER_ROWS_TOOL,
    MAX_RUN_FOLDERS,
    PREPARE_ROWS_TOOL,
    SHA256_HEX,
    SUCCESS_FILENAME,
    find_runs,
    read_json_or_none,
    run_entry,
)
from modelsafety.contract.run_identity import (  # noqa: E402
    IDENTITY_FILENAME,
    RunIdentityError,
    check_run_identity,
    compare_run_identity,
    make_identity,
)
from modelsafety.filter.filter_rows import LINE_MAP_FOLDER, SUMMARY_FILENAME  # noqa: E402
from modelsafety.filter.rules import FindingsError  # noqa: E402
from modelsafety.results.coverage import LABELS, LOADERS, SCANNERS, TOOLS, write_report  # noqa: E402
from modelsafety.trace.findings import (  # noqa: E402
    SIDE_DUPLICATE_OF,
    Finding,
    Source,
    iter_findings,
    removed_rows_source,
    scanner_sources,
)
from modelsafety.trace.original import (  # noqa: E402
    LookupNotSupported,
    RunError,
    lookup_notes,
    open_run,
)

TRACE_TOOL = "trace_findings.py"
TRACE_IDENTITY_TOOL = "trace_findings"
SUMMARY_VERSION = 1
PARTIAL_SUFFIX = ".partial"
# A filter run's parent can be another filter run; no real chain is this long.
MAX_HOPS = 20
MAX_LISTED = 5
EXAMPLES_SHOWN = 3

UNCHANGED = "unchanged"
CHANGED = "changed"
NOT_FOUND = "not_found"
NOT_SUPPORTED = "not_supported"
BROKEN_LINK = "broken_link"
STATUSES = (UNCHANGED, CHANGED, NOT_FOUND, NOT_SUPPORTED, BROKEN_LINK)
PROBLEM_STATUSES = (CHANGED, NOT_FOUND, BROKEN_LINK)

REFERENCE_REASON = (
    "The row this one duplicates is in the duplicates reference folder (cross mode), "
    "which isn't a run, so it has no _origin note to follow."
)
DEFAULT_METADATA_COLUMN = "metadata"


class TraceError(RuntimeError):
    """The trace won't start, or its inputs can't be read. The message says why."""

    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("\n".join(self.problems))


# -------------------------------------------------------------------------
# What a run says about itself
# -------------------------------------------------------------------------


@dataclass
class RunInfo:
    """A run folder, read once. `problem` says why it can't be walked."""

    run_dir: Path
    name: str
    tool: str | None = None
    problem: str | None = None
    # A filter run's parent run.
    parent_dir: Path | None = None
    # The column of a Prepared row that holds its `_origin` note.
    metadata_column: str = DEFAULT_METADATA_COLUMN


def _read_run(run_dir: Path) -> RunInfo:
    info = RunInfo(run_dir, str(run_dir))
    if not run_dir.is_dir():
        info.problem = f"{run_dir} isn't a folder."
        return info
    file_names = [entry.name for entry in os.scandir(run_dir) if entry.is_file()]
    entry = run_entry(run_dir.parent, run_dir, file_names)
    if entry is None:
        info.problem = f"{run_dir} isn't a run of a known kind (prepare_rows.py or filter_rows.py)."
        return info
    if entry["status"] != "checks_passed":
        info.problem = f"The run {run_dir} isn't finished (its status is {entry['status']})."
        return info
    if SUCCESS_FILENAME not in file_names:
        info.problem = f"The run {run_dir} has no {SUCCESS_FILENAME} file, so it isn't marked finished."
        return info

    summary = read_json_or_none(run_dir / SUMMARY_FILENAME)
    info.tool = summary.get("tool") if isinstance(summary, dict) else None
    if info.tool == FILTER_ROWS_TOOL:
        parent = summary.get("parent")
        parent_dir = parent.get("run_dir") if isinstance(parent, dict) else None
        if not isinstance(parent_dir, str) or not parent_dir:
            info.problem = f"{run_dir / SUMMARY_FILENAME} doesn't say which run it filtered (parent.run_dir)."
        else:
            info.parent_dir = Path(parent_dir)
    elif info.tool == PREPARE_ROWS_TOOL:
        identity = read_json_or_none(run_dir / IDENTITY_FILENAME)
        settings = identity.get("settings") if isinstance(identity, dict) else None
        column = settings.get("metadata_column") if isinstance(settings, dict) else None
        if isinstance(column, str) and column:
            info.metadata_column = column
    else:
        info.problem = f"{run_dir} isn't a run of a known kind (its tool is {info.tool!r})."
    return info


def _safe_relative(path: Any) -> bool:
    """Whether `path` is a relative path that stays inside the folder it is
    relative to. A finding or a line map is just data in a file, so it can't be
    trusted to name only files inside the run."""
    if not isinstance(path, str) or not path:
        return False
    pure = PurePosixPath(path)
    return not pure.is_absolute() and ".." not in pure.parts


class _Runs:
    """The runs a trace has looked at, and the runs under each scanned folder."""

    def __init__(self) -> None:
        self.infos: dict[Path, RunInfo] = {}
        self._roots: dict[str, dict[str, dict[str, Any]] | str] = {}
        self._located: dict[tuple[str, str], tuple[Path, str] | str] = {}

    def load(self, run_dir: Path) -> RunInfo:
        info = self.infos.get(run_dir)
        if info is None:
            info = self.infos[run_dir] = _read_run(run_dir)
        return info

    def folders(self) -> list[Path]:
        """Every run, and every scanned folder, the trace has looked at."""
        return [*self.infos, *(Path(root) for root in self._roots)]

    def _runs_under(self, scan_root: str) -> dict[str, dict[str, Any]] | str:
        runs = self._roots.get(scan_root)
        if runs is None:
            root = Path(scan_root)
            found = find_runs(root)
            if not found["exists"]:
                runs = f"The scanned folder {root} isn't a folder."
            elif found["truncated"]:
                runs = (
                    f"{root} holds more than {MAX_RUN_FOLDERS:,} folders, so not every run under it "
                    "could be found."
                )
            else:
                runs = {run["path"]: run for run in found["runs"]}
            self._roots[scan_root] = runs
        return runs

    def locate(self, scan_root: str, file: str) -> tuple[Path, str] | str:
        """The run a scanned file belongs to, and the file's path inside it. A
        string says why there is none."""
        key = (scan_root, file)
        located = self._located.get(key)
        if located is None:
            located = self._located[key] = self._locate(scan_root, file)
        return located

    def _locate(self, scan_root: str, file: str) -> tuple[Path, str] | str:
        if not _safe_relative(file):
            return f"{file!r} isn't a path inside the scanned folder {scan_root}."
        runs = self._runs_under(scan_root)
        if isinstance(runs, str):
            return runs
        pure = PurePosixPath(file)
        for parent in pure.parents:
            run = runs.get(parent.as_posix())
            if run is not None:
                return Path(run["run_dir"]), pure.relative_to(parent).as_posix()
        return f"{file} isn't inside any run under {scan_root}."


# -------------------------------------------------------------------------
# Walking one finding back
# -------------------------------------------------------------------------


@dataclass(slots=True)
class _Walk:
    """One starting point: a line of a file in a scanned folder, and the
    fingerprint the finding gave it. It moves back one run at a time."""

    scan_root: str
    file: str
    line: int
    sha256: str
    # Who gave the fingerprint this step has to match.
    says: str = "the finding"
    run_dir: Path | None = None
    hops: list[dict[str, Any]] = field(default_factory=list)
    status: str | None = None
    reason: str | None = None
    broken_at: int | None = None
    prepared: dict[str, Any] | None = None
    origin: dict[str, Any] | None = None
    original: dict[str, Any] | None = None
    file_differences: list[str] = field(default_factory=list)


def _hop(run: str | None, tool: str | None, file: str, line: int, matches: bool | None) -> dict[str, Any]:
    return {"run": run, "tool": tool, "file": file, "line": line, "fingerprint_matches": matches}


def _break(walk: _Walk, reason: str, hop: dict[str, Any] | None = None) -> None:
    """End the walk at its latest hop (or at `hop`, added first, when that step
    couldn't even be made)."""
    if hop is not None:
        walk.hops.append(hop)
    walk.status = BROKEN_LINK
    walk.reason = reason
    walk.broken_at = max(len(walk.hops) - 1, 0)


def _follow(
    walk: _Walk,
    info: RunInfo,
    file: str,
    raw: bytes,
    map_raw: bytes | None,
    advanced: list[_Walk],
) -> None:
    """One step of one walk: `raw` is the line it points at in `info`'s `file`."""
    where = info.run_dir / file
    try:
        check_line(where, walk.line, raw)
    except SkippedRowsEntryError as exc:
        _break(walk, str(exc), _hop(info.name, info.tool, file, walk.line, None))
        return
    fingerprint = line_fingerprint(raw)
    matches = fingerprint == walk.sha256
    walk.hops.append(_hop(info.name, info.tool, file, walk.line, matches))
    if not matches:
        _break(
            walk,
            f"{where} line {walk.line} has fingerprint {fingerprint}, but {walk.says} says "
            f"it is {walk.sha256}.",
        )
        return

    if info.tool == PREPARE_ROWS_TOOL:
        try:
            row = json.loads(raw.decode("utf-8"))
        except ValueError:
            _break(walk, f"{where} line {walk.line} isn't a JSON row.")
            return
        metadata = row.get(info.metadata_column) if isinstance(row, dict) else None
        origin = metadata.get(ORIGIN_KEY) if isinstance(metadata, dict) else None
        if not isinstance(origin, dict):
            _break(
                walk,
                f"{where} line {walk.line} has no {ORIGIN_KEY} note in its '{info.metadata_column}' column.",
            )
            return
        walk.origin = origin
        walk.prepared = {
            "run": info.name,
            "file": file,
            "line": walk.line,
            "source_sha256": walk.sha256,
        }
        return

    # A filter run: line k of the line map says where kept line k came from.
    map_where = f"{info.run_dir / LINE_MAP_FOLDER / PurePosixPath(file).with_suffix('.ndjson')}"
    if not map_raw:
        _break(walk, f"The line map {map_where} has no entry for line {walk.line}.")
        return
    try:
        parent = json.loads(map_raw)["_parent"]
        parent_file, parent_line, parent_sha256 = parent["file"], parent["line"], parent["source_sha256"]
    except (ValueError, KeyError, TypeError):
        _break(walk, f"{map_where} line {walk.line} isn't a line-map entry.")
        return
    if (
        not _safe_relative(parent_file)
        or isinstance(parent_line, bool)
        or not isinstance(parent_line, int)
        or parent_line < 1
        or not isinstance(parent_sha256, str)
        or not SHA256_HEX.fullmatch(parent_sha256)
    ):
        _break(walk, f"{map_where} line {walk.line} doesn't name a line of the parent run.")
        return
    if parent_sha256 != fingerprint:
        _break(
            walk,
            f"{map_where} line {walk.line} says the line it came from has fingerprint "
            f"{parent_sha256}, but the kept line has {fingerprint}.",
        )
        return
    walk.run_dir = info.parent_dir
    walk.file, walk.line, walk.sha256 = parent_file, parent_line, parent_sha256
    walk.says = f"the line map of {info.name}"
    advanced.append(walk)


def _read_group(info: RunInfo, file: str, walks: list[_Walk], advanced: list[_Walk]) -> None:
    """Every walk that is at a line of `file` in `info`'s run: the file (and a
    filter run's line map) is read once, in order, as far as the last line wanted."""
    waiting = deque(sorted(walks, key=lambda walk: walk.line))
    data_path = info.run_dir / file
    is_filter = info.tool == FILTER_ROWS_TOOL
    map_path = info.run_dir / LINE_MAP_FOLDER / PurePosixPath(file).with_suffix(".ndjson")
    try:
        with data_path.open("rb") as data, (map_path.open("rb") if is_filter else nullcontext()) as line_map:
            number = 0
            for raw in data:
                number += 1
                map_raw = line_map.readline() if is_filter else None
                while waiting and waiting[0].line == number:
                    _follow(waiting.popleft(), info, file, raw, map_raw, advanced)
                if not waiting:
                    return
        for walk in waiting:
            _break(
                walk,
                f"{data_path} has {number:,} lines, so line {walk.line} isn't there.",
                _hop(info.name, info.tool, file, walk.line, None),
            )
    except OSError as exc:
        for walk in waiting:
            _break(walk, f"Can't read {data_path} or its line map: {exc}", _hop(info.name, info.tool, file, walk.line, None))


def _walk_back(walks: list[_Walk], runs: _Runs) -> None:
    """Move every walk back until it is at a Prepared row, or broken."""
    active = [walk for walk in walks if walk.status is None and walk.prepared is None]
    for _ in range(MAX_HOPS):
        if not active:
            return
        groups: dict[tuple[Path, str], list[_Walk]] = {}
        for walk in active:
            info = runs.load(walk.run_dir)
            if info.problem is not None:
                _break(walk, info.problem, _hop(info.name, info.tool, walk.file, walk.line, None))
                continue
            groups.setdefault((walk.run_dir, walk.file), []).append(walk)
        advanced: list[_Walk] = []
        for (run_dir, file), group in sorted(groups.items()):
            _read_group(runs.load(run_dir), file, group, advanced)
        active = advanced
    for walk in active:
        _break(
            walk,
            f"The walk went through {MAX_HOPS} runs without reaching a Prepared run.",
            _hop(None, None, walk.file, walk.line, None),
        )


def _original_location(origin: dict[str, Any]) -> dict[str, Any]:
    return {key: origin[key] for key in ("path", "line", "byte", "row", "split", "config") if key in origin}


def _look_up_originals(walks: list[_Walk]) -> int:
    """Look each Prepared row's Original row up, reading each Original file once
    for each run. Returns how many Prepared rows were looked up."""
    by_run: dict[Path, dict[tuple[str, int, str], list[_Walk]]] = {}
    for walk in walks:
        if walk.prepared is not None:
            key = (walk.prepared["file"], walk.prepared["line"], walk.prepared["source_sha256"])
            by_run.setdefault(walk.run_dir, {}).setdefault(key, []).append(walk)

    looked_up = 0
    for run_dir, rows in sorted(by_run.items()):
        try:
            run = open_run(run_dir)
        except RunError as exc:
            for group in rows.values():
                for walk in group:
                    _break(walk, str(exc))
            continue
        groups = list(rows.values())
        outcomes = lookup_notes(run, [group[0].origin for group in groups], keep_rows=False)
        looked_up += len(groups)
        for group, outcome in zip(groups, outcomes):
            for walk in group:
                _finish(walk, outcome)
    return looked_up


def _finish(walk: _Walk, outcome: dict[str, Any] | Exception) -> None:
    if isinstance(outcome, LookupNotSupported):
        walk.status, walk.reason = NOT_SUPPORTED, str(outcome)
    elif isinstance(outcome, RunError):
        _break(walk, str(outcome))
    elif isinstance(outcome, Exception):
        _break(walk, f"The Original row couldn't be read: {type(outcome).__name__}: {outcome}")
    else:
        walk.status = outcome["status"]
        walk.original = _original_location(walk.origin)
        walk.file_differences = outcome["file_differences"]
        if walk.status == NOT_FOUND:
            walk.reason = outcome["note"] or "The Original file isn't there any more."
        elif walk.status == CHANGED:
            walk.reason = "The Original row has changed since the conversion read it."


# -------------------------------------------------------------------------
# Before starting (read-only)
# -------------------------------------------------------------------------


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _listed(lines: list[str]) -> str:
    shown = "; ".join(lines[:MAX_LISTED])
    return f"{shown}; and {len(lines) - MAX_LISTED:,} more" if len(lines) > MAX_LISTED else shown


def _check_results_dir(name: str, folder: Path, problems: list[str]) -> dict[str, str] | None:
    """What a scanner's results folder says about the scan, or None (with a
    problem) when the trace can't use it."""
    label = LABELS[name]
    if not folder.is_dir():
        problems.append(f"The {label} results folder isn't a folder: {folder}")
        return None
    identity_path = folder / IDENTITY_FILENAME
    identity = read_json_or_none(identity_path)
    if not isinstance(identity, dict):
        problems.append(
            f"{identity_path} is missing or can't be read, so nothing shows which scan made these "
            f"{label} results."
        )
        return None
    if identity.get("tool") != TOOLS[name]:
        problems.append(
            f"{identity_path} says this folder holds {identity.get('tool')!r} results, "
            f"not {label} ({TOOLS[name]!r})."
        )
        return None
    scan_root = identity.get("input_root")
    if not isinstance(scan_root, str) or not scan_root:
        problems.append(f"{identity_path} doesn't say which folder was scanned (input_root).")
        return None
    settings = identity.get("settings")
    mode = settings.get("input_mode") if isinstance(settings, dict) else None
    if mode != "runs":
        problems.append(
            f"The {label} scan was made with input mode {mode!r}. The trace walks from each finding "
            "to the run that holds it, so it needs a scan made in 'runs' mode."
        )
        return None
    if not Path(scan_root).is_dir():
        problems.append(f"The {label} scan read {scan_root}, which isn't a folder any more.")
        return None
    return {"results_dir": str(folder), "scan_root": scan_root, "tool": identity["tool"], "input_mode": mode}


def _check_filter_root(root: Path, problems: list[str]) -> None:
    """Every run under `root` is a finished filter run."""
    if not root.is_dir():
        problems.append(f"The filter folder isn't a folder: {root}")
        return
    found = find_runs(root)
    if found["truncated"]:
        problems.append(f"{root} holds more than {MAX_RUN_FOLDERS:,} folders, so not every run could be found.")
        return
    if not found["runs"]:
        problems.append(f"{root} holds no runs.")
        return
    bad = []
    for run in found["runs"]:
        info = _read_run(Path(run["run_dir"]))
        if info.problem is not None:
            bad.append(f"{run['path']}: {info.problem}")
        elif info.tool != FILTER_ROWS_TOOL:
            bad.append(f"{run['path']} isn't a filter run (its tool is {info.tool!r}).")
    if bad:
        problems.append(f"{root} doesn't hold only finished filter runs: {_listed(bad)}")


@dataclass
class _Plan:
    output: Path
    scanners: dict[str, dict[str, str]]
    filter_roots: list[Path]
    sources: list[Source]
    skipped_sources: list[dict[str, str]]

    def identity(self) -> dict[str, Any]:
        return make_identity(
            tool=TRACE_IDENTITY_TOOL,
            mode=None,
            settings={
                "scanners": {
                    name: {"results_dir": item["results_dir"], "scan_root": item["scan_root"]}
                    for name, item in self.scanners.items()
                },
                "filter_roots": [str(root) for root in self.filter_roots],
            },
            input_root="",
            input_files=[],
        )


def _make_plan(
    results_dirs: dict[str, str | Path | None], filter_roots: Sequence[str | Path], output_dir: str | Path
) -> _Plan:
    """Check everything that must hold before a row is read. Raises `TraceError`
    naming every problem found. Writes nothing."""
    output = Path(output_dir).expanduser().resolve()
    unknown = sorted(set(results_dirs) - set(SCANNERS))
    if unknown:
        raise TraceError(f"Unknown scanners: {', '.join(unknown)}. They are: {', '.join(SCANNERS)}.")
    given = {
        name: Path(folder).expanduser().resolve()
        for name, folder in results_dirs.items()
        if folder is not None
    }
    roots = [Path(root).expanduser().resolve() for root in filter_roots]

    problems: list[str] = []
    if not given and not roots:
        problems.append("Nothing to trace: give at least one scanner results folder or --filter-root.")

    scanners: dict[str, dict[str, str]] = {}
    sources: list[Source] = []
    skipped: list[dict[str, str]] = []
    for name in SCANNERS:
        if name not in given:
            continue
        checked = _check_results_dir(name, given[name], problems)
        if checked is None:
            continue
        scanners[name] = checked
        found, left_out = scanner_sources(name, given[name], checked["scan_root"])
        sources.extend(found)
        skipped.extend(left_out)
    for number, root in enumerate(roots, 1):
        _check_filter_root(root, problems)
        sources.append(removed_rows_source(number, root))

    read = [Path(item["results_dir"]) for item in scanners.values()]
    read += [Path(item["scan_root"]) for item in scanners.values()] + roots
    for folder in read:
        if _overlaps(output, folder):
            problems.append(
                f"The output folder ({output}) must not be the same as, or inside or around, a "
                f"folder the trace reads ({folder})."
            )
    if problems:
        raise TraceError(problems)

    plan = _Plan(output, scanners, roots, sources, skipped)
    try:
        state, differences = compare_run_identity(output, plan.identity())
    except RunIdentityError as exc:
        raise TraceError(str(exc)) from exc
    if state == "no_identity":
        problems.append(f"{output} isn't empty and has no {IDENTITY_FILENAME}, so it isn't a trace output folder.")
    elif state == "different":
        problems.append(
            f"{output} already holds a different trace (previous -> requested): " + "; ".join(differences)
        )
    if problems:
        raise TraceError(problems)
    return plan


# -------------------------------------------------------------------------
# Checks against the scans' own counts
# -------------------------------------------------------------------------


def _count_problems(source: Source, found_by_file: Counter) -> list[str]:
    """The findings for each file are the number the scan counted."""
    label = LABELS[source.scanner]
    recorded = LOADERS[source.scanner](source.results_dir).per_file
    if recorded is None:
        return [
            f"{label}: the results don't record how many findings each file has (the scan didn't "
            "finish, or was made before scans counted them), so the findings can't be checked "
            "against the scan's own count."
        ]
    off = []
    for path in sorted(set(recorded) | set(found_by_file)):
        counted = recorded.get(path, {}).get("findings")
        found = found_by_file.get(path, 0)
        if counted != found:
            off.append(
                f"{path} (the scan counted {'no' if counted is None else f'{counted:,}'}, "
                f"its findings file has {found:,})"
            )
    if not off:
        return []
    return [
        f"{label}: the findings file doesn't match the scan's own count of findings for "
        f"{len(off):,} file(s): {_listed(off)}."
    ]


# -------------------------------------------------------------------------
# Writing
# -------------------------------------------------------------------------


def _render(source: Source, finding: Finding, walk: _Walk | None) -> tuple[str, str, str | None]:
    """One output line, and the finding's status and reason."""
    entry: dict[str, Any] = {
        "finding": {
            "source": source.name,
            "results_dir": str(source.results_dir),
            "results_file": finding.results_file,
            "results_line": finding.results_line,
            "scan_root": finding.scan_root,
            "file": finding.file,
            "line": finding.line,
            "source_sha256": finding.sha256,
            "side": finding.side,
        }
    }
    if walk is None:
        status, reason = NOT_SUPPORTED, REFERENCE_REASON
        entry.update(
            status=status, reason=reason, broken_at_hop=None, hops=[], prepared=None, original=None,
            file_differences=[],
        )
    else:
        status, reason = walk.status, walk.reason
        entry.update(
            status=status,
            reason=reason,
            broken_at_hop=walk.broken_at,
            hops=walk.hops,
            prepared=walk.prepared,
            original=walk.original,
            file_differences=walk.file_differences,
        )
    return json.dumps(entry, ensure_ascii=False) + "\n", status, reason


def _sum_counts(counter: Counter) -> dict[str, int]:
    return {status: counter.get(status, 0) for status in STATUSES}


# -------------------------------------------------------------------------
# The trace
# -------------------------------------------------------------------------


def trace_findings(
    results_dirs: dict[str, str | Path | None],
    filter_roots: Sequence[str | Path],
    output_dir: str | Path,
) -> dict[str, Any]:
    """Trace every finding of the given scanner results, and every removed row of
    the given filter roots, back to the Original row.

    `results_dirs` is ``{scanner: results folder or None}`` with the scanners named
    as in ``modelsafety/results/coverage.py`` (regex_pii, ner, toxicity, dedup).
    Writes one NDJSON file per source, `summary.json`, `run_identity.json` and
    (only if every check passed) `_SUCCESS` into `output_dir`, and returns the
    summary. Raises `TraceError` when it refuses to start or can't read a findings
    file; nothing is written then.
    """
    plan = _make_plan(results_dirs, filter_roots, output_dir)
    runs = _Runs()

    # Pass 1: read every finding, and collect each line that has to be walked back.
    starts: dict[tuple[str, str, int, str], _Walk] = {}
    counts: dict[str, int] = {}
    failures: list[str] = []
    for source in plan.sources:
        print(f"Reading the findings of {source.name} ...", flush=True)
        found = 0
        found_by_file: Counter = Counter()
        try:
            for finding in iter_findings(source):
                found += 1
                if finding.side != SIDE_DUPLICATE_OF:
                    found_by_file[finding.file] += 1
                if finding.reference:
                    continue
                key = (finding.scan_root, finding.file, finding.line, finding.sha256)
                if key not in starts:
                    starts[key] = _Walk(finding.scan_root, finding.file, finding.line, finding.sha256)
        except (FindingsError, OSError, UnicodeDecodeError) as exc:
            raise TraceError(f"{source.name}: {exc}") from exc
        counts[source.name] = found
        print(f"  {found:,} finding(s).", flush=True)
        if source.counted:
            failures.extend(_count_problems(source, found_by_file))

    # Walk every line back to a Prepared row, then look its Original row up.
    walks = list(starts.values())
    print(f"Walking {len(walks):,} distinct line(s) back through their runs ...", flush=True)
    for walk in walks:
        located = runs.locate(walk.scan_root, walk.file)
        if isinstance(located, str):
            _break(walk, located, _hop(None, None, walk.file, walk.line, None))
        else:
            walk.run_dir, walk.file = located
    _walk_back(walks, runs)
    print("Looking the Original rows up ...", flush=True)
    prepared_rows = _look_up_originals(walks)
    unfinished = [walk for walk in walks if walk.status is None]
    if unfinished:
        raise TraceError(f"{len(unfinished):,} walk(s) ended with no status. This is a bug in the trace.")

    for folder in runs.folders():
        if _overlaps(plan.output, folder):
            raise TraceError(
                f"The output folder ({plan.output}) must not be the same as, or inside or around, a "
                f"run the trace read ({folder})."
            )

    # Pass 2: write one line per finding.
    try:
        check_run_identity(plan.output, plan.identity())
    except RunIdentityError as exc:
        raise TraceError(str(exc)) from exc
    for name in (SUCCESS_FILENAME, SUMMARY_FILENAME):
        (plan.output / name).unlink(missing_ok=True)

    totals: Counter = Counter()
    source_summaries: dict[str, Any] = {}
    temps: list[tuple[Path, Path]] = []
    try:
        for source in plan.sources:
            print(f"Writing {source.name}.ndjson ...", flush=True)
            final = plan.output / f"{source.name}.ndjson"
            temp = final.with_name(final.name + PARTIAL_SUFFIX)
            temps.append((temp, final))
            by_status: Counter = Counter()
            examples: dict[str, list[dict[str, Any]]] = {}
            written = 0
            try:
                with temp.open("w", encoding="utf-8") as handle:
                    for finding in iter_findings(source):
                        walk = None
                        if not finding.reference:
                            walk = starts[(finding.scan_root, finding.file, finding.line, finding.sha256)]
                        line, status, reason = _render(source, finding, walk)
                        handle.write(line)
                        written += 1
                        by_status[status] += 1
                        if status != UNCHANGED and len(examples.setdefault(status, [])) < EXAMPLES_SHOWN:
                            examples[status].append(
                                {
                                    "results_file": finding.results_file,
                                    "results_line": finding.results_line,
                                    "file": finding.file,
                                    "line": finding.line,
                                    "reason": reason,
                                }
                            )
            except (FindingsError, OSError, UnicodeDecodeError, KeyError) as exc:
                raise TraceError(
                    f"{source.name}: couldn't read the findings a second time ({type(exc).__name__}: {exc}). "
                    "The findings file changed while the trace ran."
                ) from exc
            if written != counts[source.name]:
                failures.append(
                    f"{source.name}: {counts[source.name]:,} findings were read the first time and "
                    f"{written:,} the second. The findings file changed while the trace ran."
                )
            if sum(by_status.values()) != written:
                failures.append(f"{source.name}: the statuses don't add up to the {written:,} lines written.")
            totals.update(by_status)
            source_summaries[source.name] = {
                "results_dir": str(source.results_dir),
                "output": final.name,
                "findings": counts[source.name],
                "lines_written": written,
                "by_status": _sum_counts(by_status),
                "examples": examples,
            }
        for temp, final in temps:
            os.replace(temp, final)
    except BaseException:
        for temp, _final in temps:
            temp.unlink(missing_ok=True)
        raise

    for source in source_summaries.values():
        source["bytes"] = (plan.output / source["output"]).stat().st_size
    problems_found = sum(totals[status] for status in PROBLEM_STATUSES)
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool": TRACE_TOOL,
        "report_version": SUMMARY_VERSION,
        "inputs": {
            "scanners": plan.scanners,
            "filter_roots": [str(root) for root in plan.filter_roots],
        },
        "skipped_sources": plan.skipped_sources,
        "sources": source_summaries,
        "totals": {
            "findings": sum(counts.values()),
            "distinct_lines_walked": len(walks),
            "prepared_rows_looked_up": prepared_rows,
            "by_status": _sum_counts(totals),
        },
        "verdict": "pass" if not failures and not problems_found else "fail",
        "checks": {"passed": not failures, "failures": failures},
    }
    write_report(plan.output / SUMMARY_FILENAME, summary)
    if summary["checks"]["passed"]:
        (plan.output / SUCCESS_FILENAME).touch()
    return summary


# -------------------------------------------------------------------------
# Command line
# -------------------------------------------------------------------------

RESULTS_OPTIONS = {
    "regex_pii": "--pii-run-dir",
    "ner": "--ner-run-dir",
    "toxicity": "--toxicity-run-dir",
    "dedup": "--dedup-run-dir",
}


def _print_summary(summary: dict[str, Any], output: Path) -> None:
    print()
    print(f"{'source':28s} {'findings':>10s} " + " ".join(f"{status:>13s}" for status in STATUSES))
    for name, source in summary["sources"].items():
        counts = " ".join(f"{source['by_status'][status]:>13,}" for status in STATUSES)
        print(f"{name:28s} {source['findings']:>10,} {counts}")
    totals = summary["totals"]
    counts = " ".join(f"{totals['by_status'][status]:>13,}" for status in STATUSES)
    print(f"{'TOTAL':28s} {totals['findings']:>10,} {counts}")
    for item in summary["skipped_sources"]:
        print(f"Left out {item['source']}: {item['reason']}")
    for name, source in summary["sources"].items():
        for status, items in source["examples"].items():
            for item in items:
                print(
                    f"  {name} {status}: {item['results_file']} line {item['results_line']} "
                    f"({item['file']} line {item['line']}): {item['reason']}"
                )
    for failure in summary["checks"]["failures"]:
        print(f"FAILED: {failure}")
    print(f"Written to {output}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Where the trace is written: a new folder, outside every folder the trace reads.",
    )
    for name in SCANNERS:
        parser.add_argument(
            RESULTS_OPTIONS[name],
            type=Path,
            help=f"{LABELS[name]} results, made in 'runs' input mode.",
        )
    parser.add_argument(
        "--filter-root",
        type=Path,
        action="append",
        default=[],
        help="The output folder of a filter step: trace its removed rows too. Repeatable.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    results_dirs = {
        "regex_pii": args.pii_run_dir,
        "ner": args.ner_run_dir,
        "toxicity": args.toxicity_run_dir,
        "dedup": args.dedup_run_dir,
    }
    try:
        summary = trace_findings(results_dirs, args.filter_root, args.output_dir)
    except TraceError as exc:
        print("error:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2
    _print_summary(summary, args.output_dir.expanduser().resolve())
    if not summary["checks"]["passed"]:
        return 2
    return 0 if summary["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
