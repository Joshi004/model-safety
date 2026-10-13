#!/usr/bin/env python3
"""One status entry for every Prepared row of a chain: what each scanner found,
whether the row was removed (and where, and why) or where it is in the clean data.

Give it the output folder of the last filter step. For each filter run under it,
it follows the run's parent, and that run's parent, back to a Prepared run, and
reads the line map and the removed rows of every step. Then it reads the
findings of every scanner the steps used, and maps each one back to its Prepared
row. For each Prepared file it writes:

  <output-dir>/<run>/rows/<file>.ndjson    one entry for every Prepared line, in order
  <output-dir>/<run>/rows/<file>.offsets   the byte offset of every entry, to read one row
                                           or one page of rows without reading the file
                                           (see row_status_read.py)

and, for each run and each filter step,

  <output-dir>/<run>/removed/step<N>.ndjson   one line for every row the step removed, with
                                              its reasons, the NER labels, regex categories
                                              and toxicity verdicts that removed it, and its
                                              Original row. It lets the removed rows be
                                              filtered and paged without reading the filter
                                              run's removed_rows.ndjson, which can be over a
                                              gigabyte (see row_status_read.py).

and `run_identity.json`, `summary.json` and (only if every check passed)
`_SUCCESS` for the whole chain.

An entry has, for each step, the scanners that saw the row (`scanned`, with
their findings and where in the scanner's own file each one is), the ones that
read the row but found no text (`no_text`), and the ones that never saw it
because an earlier step removed it (`not_reached`). `not_scanned` lists the
last two, so a row is never shown as clean for a scan it wasn't part of.

Nothing is changed in any folder it reads. The output folder must be new, or
hold a status made from these same inputs, and can't be inside any folder the
tool reads. It needs pandas and pyarrow (through the trace module), so run it
with the readers' Python:

    modelsafety/readers/.venv/bin/python modelsafety/trace/row_status.py \\
        --filter-root LAST_FILTER_STEP --output-dir O

Exit code: 0 if every check passed, 2 if it refused to start or a check failed
(no `_SUCCESS`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path, PurePosixPath
from typing import Any

# Run as a script, so only this folder is on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modelsafety.contract.fingerprints import file_sha256, line_fingerprint  # noqa: E402
from modelsafety.contract.origin import ORIGIN_KEY, check_line  # noqa: E402
from modelsafety.contract.run_finder import (  # noqa: E402
    PREPARE_ROWS_TOOL,
    SHA256_HEX,
    SUCCESS_FILENAME,
    find_runs,
    read_json_or_none,
)
from modelsafety.contract.run_identity import (  # noqa: E402
    IDENTITY_FILENAME,
    RunIdentityError,
    check_run_identity,
    compare_run_identity,
    make_identity,
)
from modelsafety.contract.textract import extract_record_text  # noqa: E402
from modelsafety.data_screening.dedup.row_text import row_has_text as dedup_row_has_text  # noqa: E402
from modelsafety.data_screening.pii.model.common import iter_record_texts  # noqa: E402
from modelsafety.data_screening.toxicity.row_text import row_has_text as toxicity_row_has_text  # noqa: E402
from modelsafety.filter.filter_rows import LINE_MAP_FOLDER, REMOVED_ROWS_FILENAME, SUMMARY_FILENAME  # noqa: E402
from modelsafety.filter.rules import FindingsError  # noqa: E402
from modelsafety.results.coverage import LABELS, LOADERS, SCANNERS, write_report  # noqa: E402
from modelsafety.trace.findings import (  # noqa: E402
    SIDE_DUPLICATE_OF,
    SIDE_ROW,
    Finding,
    Source,
    iter_findings,
    removed_rows_source,
    scanner_sources,
)
from modelsafety.trace.row_status_read import (  # noqa: E402
    REMOVED_COUNTS,
    STATUS_IDENTITY_TOOL,
    STATUS_TOOL,
    offset_line,
    removed_index_path,
    status_paths,
)
from modelsafety.trace.trace_findings import (  # noqa: E402
    MAX_HOPS,
    _check_filter_root,
    _check_results_dir,
    _count_problems,
    _listed,
    _original_location,
    _overlaps,
    _read_run,
    _Runs,
)

# 2: each run lists its removed-rows index (`removed_index`), and the totals have
# `removed_by_step_and_reason`.
SUMMARY_VERSION = 2
PARTIAL_SUFFIX = ".partial"
PROBLEMS_SHOWN = 25
LINE_MAP_SUFFIX = ".ndjson"

# The source of each scanner's findings that goes into an entry.
DETAIL_SOURCES = {
    "regex_pii": "regex_pii_stage3",
    "ner": "ner_all_spans",
    "toxicity": "toxicity_guard",
    "dedup": "dedup_results",
}
# One bit in a row's `text` for each scanner: whether that scanner had text to read.
_BIT = {name: 1 << number for number, name in enumerate(SCANNERS)}

SCANNED = "scanned"
NO_TEXT = "no_text"
NOT_REACHED = "not_reached"


class StatusError(RuntimeError):
    """The status won't start, or a check failed. The message says why."""

    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("\n".join(self.problems))


class _Problems:
    """What is wrong so far. Keeps the first few in full and counts the rest."""

    def __init__(self) -> None:
        self.items: list[str] = []
        self.total = 0

    def add(self, text: str) -> None:
        self.total += 1
        if len(self.items) < PROBLEMS_SHOWN:
            self.items.append(text)

    def __bool__(self) -> bool:
        return self.total > 0

    def lines(self) -> list[str]:
        more = self.total - len(self.items)
        return self.items + ([f"... and {more:,} more."] if more > 0 else [])

    def raise_if_any(self) -> None:
        if self:
            raise StatusError(self.lines())


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# -------------------------------------------------------------------------
# What the chain is made of
# -------------------------------------------------------------------------


@dataclass
class _Level:
    """One run of a chain: the Prepared run (level 0) or a filter run."""

    info: Any
    summary: dict[str, Any]
    # The data files the run wrote: path, rows, bytes, sha256.
    files: list[dict[str, Any]]
    # A filter run's settings, as its run_identity.json records them.
    meta: dict[str, Any] | None = None


@dataclass
class _Step:
    number: int
    output_root: Path
    scan_root: Path
    rules: dict[str, Any]
    # {scanner: {"results_dir", "tool", "mode"}}, in the order of SCANNERS.
    scanners: dict[str, dict[str, Any]]
    sources: dict[str, Source] = field(default_factory=dict)


@dataclass
class _Plan:
    output: Path
    last_root: Path
    steps: list[_Step]
    chains: dict[str, list[_Level]]
    used: tuple[str, ...]
    dedup: tuple[str, bool] | None

    @property
    def prepared_root(self) -> Path:
        return self.steps[0].scan_root

    def identity(self) -> dict[str, Any]:
        files = [
            {"path": f"{name}/{item['path']}", "rows": item["rows"], "bytes": item["bytes"], "sha256": item["sha256"]}
            for name, levels in sorted(self.chains.items())
            for item in levels[0].files
        ]
        return make_identity(
            tool=STATUS_IDENTITY_TOOL,
            mode=None,
            settings={
                "last_filter_root": str(self.last_root),
                "steps": [
                    {
                        "step": step.number,
                        "output_root": str(step.output_root),
                        "scan_root": str(step.scan_root),
                        "rules": step.rules,
                        "scanners": step.scanners,
                    }
                    for step in self.steps
                ],
            },
            input_root=str(self.prepared_root),
            input_files=files,
        )


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _files_written(summary: Any, run_dir: Path, problems: list[str]) -> list[dict[str, Any]] | None:
    """The data files a run says it wrote, or None (with a problem)."""
    output = summary.get("output") if isinstance(summary, dict) else None
    files = output.get("files_written") if isinstance(output, dict) else None
    if not isinstance(files, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and _is_count(item.get("rows"))
        and _is_count(item.get("bytes"))
        and isinstance(item.get("sha256"), str)
        and SHA256_HEX.fullmatch(item["sha256"])
        for item in files
    ):
        problems.append(f"{run_dir / SUMMARY_FILENAME} doesn't list the files the run wrote (output.files_written).")
        return None
    if len({item["path"] for item in files}) != len(files):
        problems.append(f"{run_dir / SUMMARY_FILENAME} lists a file twice in output.files_written.")
        return None
    return files


def _filter_meta(info: Any, summary: dict[str, Any], name: str, problems: list[str]) -> dict[str, Any] | None:
    """A filter run's settings from its `run_identity.json`, checked against its
    `summary.json`. None (with a problem) if they can't be read or disagree."""
    run_dir = info.run_dir
    identity = read_json_or_none(run_dir / IDENTITY_FILENAME)
    settings = identity.get("settings") if isinstance(identity, dict) else None
    parent = summary.get("parent")
    if not isinstance(settings, dict) or not isinstance(parent, dict):
        problems.append(f"{run_dir}: {IDENTITY_FILENAME} or {SUMMARY_FILENAME} doesn't say which run it filtered.")
        return None
    scan_root, parent_run = settings.get("scan_root"), settings.get("parent_run")
    rules, scanner_results = settings.get("rules"), settings.get("scanner_results")
    if (
        not isinstance(scan_root, str)
        or not isinstance(parent_run, str)
        or not isinstance(rules, dict)
        or not isinstance(scanner_results, dict)
        or not scanner_results
    ):
        problems.append(f"{run_dir / IDENTITY_FILENAME} doesn't record the scan root, the parent run, the rules and the scanner results.")
        return None
    unknown = sorted(set(scanner_results) - set(SCANNERS))
    if unknown or not all(isinstance(item, dict) and isinstance(item.get("results_dir"), str) for item in scanner_results.values()):
        problems.append(f"{run_dir / IDENTITY_FILENAME} has scanner results it can't use ({', '.join(unknown) or 'a malformed entry'}).")
        return None
    disagreements = []
    if identity.get("input_root") != parent.get("run_dir"):
        disagreements.append("the parent run")
    if parent.get("scan_root") != scan_root or parent.get("run") != parent_run:
        disagreements.append("the scanned folder or the parent run's name")
    if summary.get("rules") != rules:
        disagreements.append("the rules")
    if summary.get("scanner_results") != scanner_results:
        disagreements.append("the scanner results")
    if disagreements:
        problems.append(f"{run_dir}: {IDENTITY_FILENAME} and {SUMMARY_FILENAME} disagree about {', '.join(disagreements)}.")
        return None
    if parent_run != name:
        problems.append(f"{run_dir} filtered the run {parent_run!r}, but it is the run {name!r} under its folder.")
        return None
    output_root = run_dir
    for _ in PurePosixPath(name).parts:
        output_root = output_root.parent
    if run_dir.relative_to(output_root).as_posix() != PurePosixPath(name).as_posix():
        problems.append(f"{run_dir} isn't at the path {name!r} under its output folder.")
        return None
    return {
        "scan_root": scan_root,
        "output_root": str(output_root),
        "rules": rules,
        "scanner_results": scanner_results,
    }


def _walk_chain(last_dir: Path, name: str, problems: list[str]) -> list[_Level] | None:
    """The runs from the Prepared run to `last_dir`, oldest first. None (with a
    problem) if the chain can't be followed to a finished Prepared run."""
    levels: list[_Level] = []
    current = last_dir
    for _ in range(MAX_HOPS + 1):
        info = _read_run(current)
        if info.problem is not None:
            problems.append(f"{name}: {info.problem}")
            return None
        summary = read_json_or_none(current / SUMMARY_FILENAME)
        files = _files_written(summary, current, problems)
        if files is None:
            return None
        if info.tool == PREPARE_ROWS_TOOL:
            levels.append(_Level(info, summary, files))
            levels.reverse()
            return levels
        meta = _filter_meta(info, summary, name, problems)
        if meta is None:
            return None
        levels.append(_Level(info, summary, files, meta))
        current = info.parent_dir
    problems.append(f"{name}: the chain went through {MAX_HOPS} runs without reaching a Prepared run.")
    return None


def _dedup_settings(results_dir: Path, problems: list[str]) -> tuple[str, bool] | None:
    identity = read_json_or_none(results_dir / IDENTITY_FILENAME)
    settings = identity.get("settings") if isinstance(identity, dict) else None
    scope = settings.get("text_scope") if isinstance(settings, dict) else None
    exclude = settings.get("exclude_system") if isinstance(settings, dict) else None
    if not isinstance(scope, str) or not isinstance(exclude, bool):
        problems.append(
            f"{results_dir / IDENTITY_FILENAME} doesn't record the duplicates text scope and whether "
            "system messages were left out, so the rows the scan had no text in can't be worked out."
        )
        return None
    return scope, exclude


def _make_plan(filter_root: str | Path, output_dir: str | Path) -> _Plan:
    """Check everything that must hold before a row is read. Raises `StatusError`
    naming every problem found. Writes nothing."""
    output = Path(output_dir).expanduser().resolve()
    last = Path(filter_root).expanduser().resolve()
    problems: list[str] = []
    _check_filter_root(last, problems)
    if problems:
        raise StatusError(problems)
    found = find_runs(last)
    if found["truncated"]:
        raise StatusError(f"{last} holds too many folders, so not every run under it could be found.")

    chains: dict[str, list[_Level]] = {}
    for run in sorted(found["runs"], key=lambda item: item["path"]):
        levels = _walk_chain(Path(run["run_dir"]), run["path"], problems)
        if levels is not None:
            chains[run["path"]] = levels
    if problems:
        raise StatusError(problems)

    depths = {len(levels) for levels in chains.values()}
    if len(depths) != 1:
        raise StatusError(
            "The runs under the filter folder were made by a different number of steps: "
            + ", ".join(f"{name} ({len(levels) - 1})" for name, levels in sorted(chains.items()))
        )
    reference = next(iter(chains.values()))
    steps: list[_Step] = []
    for number in range(1, len(reference)):
        meta = reference[number].meta
        for name, levels in sorted(chains.items()):
            if levels[number].meta != meta:
                problems.append(
                    f"Step {number} of {name} was made with different settings (scanned folder, rules or "
                    "scanner results) from the same step of another run."
                )
        steps.append(
            _Step(
                number,
                Path(meta["output_root"]),
                Path(meta["scan_root"]),
                meta["rules"],
                {name: meta["scanner_results"][name] for name in SCANNERS if name in meta["scanner_results"]},
            )
        )
    if problems:
        raise StatusError(problems)
    if not steps:
        raise StatusError(f"{last} holds Prepared runs, not filter runs.")

    names = set(chains)
    for step in steps:
        expected_scan_root = steps[step.number - 2].output_root if step.number > 1 else step.scan_root
        if step.scan_root.resolve() != expected_scan_root.resolve():
            problems.append(f"Step {step.number} scanned {step.scan_root}, not the output of step {step.number - 1}.")
        if step.number < len(steps):
            _check_filter_root(step.output_root, problems)
        elif step.output_root.resolve() != last:
            problems.append(f"The last step wrote {step.output_root}, not {last}.")
        roots = [step.output_root] + ([step.scan_root] if step.number == 1 else [])
        for root in roots:
            there = find_runs(root)
            if there["truncated"] or {run["path"] for run in there["runs"]} != names:
                problems.append(f"{root} doesn't hold exactly the runs {last} was made from.")
    if problems:
        raise StatusError(problems)

    # The scanners each step used.
    used = tuple(name for name in SCANNERS if any(name in step.scanners for step in steps))
    dedup: tuple[str, bool] | None = None
    scan_roots: set[Path] = set()
    results_dirs: list[Path] = []
    for step in steps:
        for name, info in step.scanners.items():
            folder = Path(info["results_dir"]).expanduser().resolve()
            results_dirs.append(folder)
            checked = _check_results_dir(name, folder, problems)
            if checked is None:
                continue
            scan_roots.add(Path(checked["scan_root"]))
            if Path(checked["scan_root"]).resolve() != step.scan_root.resolve():
                problems.append(
                    f"The {LABELS[name]} results ({folder}) are of {checked['scan_root']}, but step "
                    f"{step.number} filtered {step.scan_root}."
                )
                continue
            wanted, left_out = scanner_sources(name, folder, checked["scan_root"])
            source = next((item for item in wanted if item.name == DETAIL_SOURCES[name]), None)
            if source is None:
                problems.append(f"{LABELS[name]}: {left_out[0]['reason'] if left_out else 'no findings to read'}")
                continue
            step.sources[name] = source
            if name == "dedup":
                settings = _dedup_settings(folder, problems)
                if settings is not None and dedup not in (None, settings):
                    problems.append("The duplicates scans of different steps used different text settings.")
                dedup = settings if settings is not None else dedup
    if problems:
        raise StatusError(problems)

    protected = [last, *(step.output_root for step in steps), steps[0].scan_root, *results_dirs, *scan_roots]
    for folder in sorted(set(protected)):
        if _overlaps(output, folder):
            problems.append(
                f"The output folder ({output}) must not be the same as, or inside or around, a "
                f"folder the tool reads ({folder})."
            )
    if problems:
        raise StatusError(problems)

    plan = _Plan(output, last, steps, chains, used, dedup)
    try:
        state, differences = compare_run_identity(output, plan.identity())
    except RunIdentityError as exc:
        raise StatusError(str(exc)) from exc
    if state == "no_identity":
        problems.append(f"{output} isn't empty and has no {IDENTITY_FILENAME}, so it isn't a row status folder.")
    elif state == "different":
        problems.append(
            f"{output} already holds a different row status (previous -> requested): " + "; ".join(differences)
        )
    if problems:
        raise StatusError(problems)
    return plan


# -------------------------------------------------------------------------
# The rows
# -------------------------------------------------------------------------


class _Row:
    """One Prepared row. `at` is where it is now: (file, line, byte offset) in the
    latest run it is in, or None once a step has removed it."""

    __slots__ = ("run", "file", "line", "offset", "sha256", "origin", "text", "at", "steps", "removed_at")

    def __init__(self, run: str, file: str, line: int, offset: int, sha256: str, origin: Any, text: int) -> None:
        self.run = run
        self.file = file
        self.line = line
        self.offset = offset
        self.sha256 = sha256
        self.origin = origin
        self.text = text
        self.at: tuple[str, int, int] | None = (file, line, offset)
        self.steps: list[dict[str, Any]] = []
        self.removed_at: int | None = None


@dataclass
class _Run:
    name: str
    levels: list[_Level]
    # rows[level][file]: the rows of that level's file, in line order. Level 0 is
    # every Prepared row; later levels are the rows each filter run kept.
    rows: list[dict[str, list[_Row]]] = field(default_factory=list)
    # Counts for each step: rows in, kept, removed, and removed for each reason.
    step_counts: list[dict[str, Any]] = field(default_factory=list)


def _read_prepared(run: _Run, plan: _Plan, problems: _Problems) -> None:
    """Read each Prepared file once: where every line is, its fingerprint, its
    origin note, and whether each scanner had text in it."""
    level = run.levels[0]
    column = level.info.metadata_column
    used = plan.used
    want_regex, want_ner = "regex_pii" in used, "ner" in used
    want_toxicity, want_dedup = "toxicity" in used, "dedup" in used
    scope, exclude_system = plan.dedup if plan.dedup is not None else ("prompt", False)
    run.rows.append({})
    for entry in level.files:
        file = entry["path"]
        path = level.info.run_dir / file
        rows: list[_Row] = []
        digest = hashlib.sha256()
        offset = 0
        try:
            with path.open("rb") as handle:
                for number, raw in enumerate(handle, 1):
                    digest.update(raw)
                    if not raw.strip():
                        raise ValueError(f"{path} line {number} is blank, so it isn't a row.")
                    check_line(path, number, raw)
                    record = json.loads(raw.decode("utf-8"))
                    if not isinstance(record, dict):
                        raise ValueError(f"{path} line {number} isn't a JSON object.")
                    metadata = record.get(column)
                    origin = metadata.get(ORIGIN_KEY) if isinstance(metadata, dict) else None
                    mask = 0
                    if want_regex and extract_record_text(record):
                        mask |= _BIT["regex_pii"]
                    if want_ner and next(iter_record_texts({"record": record}), None) is not None:
                        mask |= _BIT["ner"]
                    if want_toxicity and toxicity_row_has_text(record):
                        mask |= _BIT["toxicity"]
                    if want_dedup and dedup_row_has_text(record, scope, exclude_system):
                        mask |= _BIT["dedup"]
                    rows.append(
                        _Row(
                            run.name,
                            file,
                            number,
                            offset,
                            line_fingerprint(raw),
                            _original_location(origin) if isinstance(origin, dict) else None,
                            mask,
                        )
                    )
                    offset += len(raw)
        except (OSError, ValueError) as exc:
            problems.add(f"{run.name}: {exc}")
            continue
        if (len(rows), offset, digest.hexdigest()) != (entry["rows"], entry["bytes"], entry["sha256"]):
            problems.add(
                f"{path} has {len(rows):,} rows, {offset:,} bytes and SHA-256 {digest.hexdigest()}, but its run "
                f"wrote {entry['rows']:,} rows, {entry['bytes']:,} bytes and SHA-256 {entry['sha256']}. "
                "It changed after the run finished."
            )
            continue
        run.rows[0][file] = rows


def _states(row: _Row, scanners: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for name in scanners:
        if row.text & _BIT[name]:
            state: dict[str, Any] = {"state": SCANNED, "findings": []}
            if name == "dedup":
                state["duplicated_by"] = []
        else:
            state = {"state": NO_TEXT}
        states[name] = state
    return states


def _kept_files(
    run: _Run, step: _Step, scanners: tuple[str, ...], problems: _Problems, counts: dict[str, Counter]
) -> None:
    """Read each kept file of the step's run next to its line map. Every kept line
    is a copy of a line of the run before: the same fingerprint, at the byte
    offset the line map says."""
    number = step.number
    level, parent = run.levels[number], run.levels[number - 1]
    summary = level.summary
    parent_rows = run.rows[number - 1]
    map_entries = {}
    line_map = summary.get("line_map")
    for item in line_map.get("files", []) if isinstance(line_map, dict) else []:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            map_entries[item["path"]] = item
    run.rows.append({})
    for entry in level.files:
        file = entry["path"]
        rows_before = parent_rows.get(file)
        label = f"{run.name}, step {number}, {file}"
        if rows_before is None:
            problems.add(f"{label}: the run before didn't have this file.")
            continue
        data_path = level.info.run_dir / file
        map_rel = f"{LINE_MAP_FOLDER}/{PurePosixPath(file).with_suffix(LINE_MAP_SUFFIX).as_posix()}"
        map_path = level.info.run_dir / map_rel
        data_digest, map_digest = hashlib.sha256(), hashlib.sha256()
        kept: list[_Row] = []
        size = 0
        previous = 0
        try:
            with data_path.open("rb") as data, map_path.open("rb") as line_map_handle:
                for raw, map_raw in zip_longest(data, line_map_handle):
                    if raw is None or map_raw is None:
                        raise ValueError(f"{label}: the file and its line map don't have the same number of lines.")
                    data_digest.update(raw)
                    map_digest.update(map_raw)
                    check_line(data_path, len(kept) + 1, raw)
                    try:
                        origin = json.loads(map_raw)["_parent"]
                        parent_file, parent_line = origin["file"], origin["line"]
                        parent_offset, parent_sha256 = origin["byte_offset"], origin["source_sha256"]
                    except (ValueError, KeyError, TypeError):
                        raise ValueError(f"{map_path} line {len(kept) + 1} isn't a line-map entry.") from None
                    if (
                        parent_file != file
                        or isinstance(parent_line, bool)
                        or not isinstance(parent_line, int)
                        or not previous < parent_line <= len(rows_before)
                    ):
                        raise ValueError(f"{map_path} line {len(kept) + 1} doesn't point at a later line of {file}.")
                    previous = parent_line
                    row = rows_before[parent_line - 1]
                    fingerprint = line_fingerprint(raw)
                    if fingerprint != parent_sha256 or fingerprint != row.sha256:
                        raise ValueError(
                            f"{label} line {len(kept) + 1} has fingerprint {fingerprint}; its line map says "
                            f"{parent_sha256}; the line it came from ({file} line {parent_line}) has {row.sha256}."
                        )
                    if row.at is None or parent_offset != row.at[2]:
                        raise ValueError(
                            f"{map_path} line {len(kept) + 1} says the line it came from starts at byte "
                            f"{parent_offset}, but it starts at byte {row.at[2] if row.at else None}."
                        )
                    if len(row.steps) != number - 1:
                        raise ValueError(f"{label}: the row at {file} line {parent_line} is kept or removed twice.")
                    row.steps.append(
                        {
                            "step": number,
                            "outcome": "kept",
                            "at": {"file": file, "line": len(kept) + 1, "byte_offset": size},
                            "scanners": _states(row, scanners),
                        }
                    )
                    row.at = (file, len(kept) + 1, size)
                    kept.append(row)
                    size += len(raw)
        except (OSError, ValueError) as exc:
            problems.add(str(exc))
            continue
        written = (len(kept), size, data_digest.hexdigest())
        if written != (entry["rows"], entry["bytes"], entry["sha256"]):
            problems.add(
                f"{label}: the kept file has {written[0]:,} rows, {written[1]:,} bytes and SHA-256 {written[2]}, "
                f"but the run says {entry['rows']:,}, {entry['bytes']:,} and {entry['sha256']}."
            )
            continue
        listed = map_entries.get(map_rel)
        if listed is None or (listed.get("entries"), listed.get("sha256")) != (len(kept), map_digest.hexdigest()):
            problems.add(f"{label}: the line map {map_rel} isn't the one the run's summary.json lists.")
            continue
        counts[file]["kept"] = len(kept)
        run.rows[number][file] = kept


def _removed_rows(
    run: _Run,
    step: _Step,
    scanners: tuple[str, ...],
    removed: list[Finding],
    problems: _Problems,
    counts: dict[str, Counter],
) -> None:
    number = step.number
    parent_rows = run.rows[number - 1]
    for finding in removed:
        where = f"{finding.results_file} line {finding.results_line}"
        rows_before = parent_rows.get(finding.file)
        if rows_before is None or not 1 <= finding.line <= len(rows_before):
            problems.add(f"{where}: {finding.file} line {finding.line} isn't a line of the run before.")
            continue
        row = rows_before[finding.line - 1]
        detail = finding.detail or {}
        reasons = detail.get("reasons")
        if row.sha256 != finding.sha256:
            problems.add(f"{where}: {finding.file} line {finding.line} has fingerprint {row.sha256}, not {finding.sha256}.")
        elif row.at is None or detail.get("byte_offset") != row.at[2]:
            problems.add(f"{where}: {finding.file} line {finding.line} isn't at the byte offset the removed row says.")
        elif not isinstance(reasons, list) or not reasons:
            problems.add(f"{where}: the removed row has no reasons.")
        elif len(row.steps) != number - 1:
            problems.add(f"{where}: {finding.file} line {finding.line} is kept or removed twice.")
        else:
            row.steps.append(
                {
                    "step": number,
                    "outcome": "removed",
                    "removed_entry": [finding.results_file, finding.results_line],
                    "reasons": reasons,
                    "scanners": _states(row, scanners),
                }
            )
            row.at = None
            row.removed_at = number
            counts[finding.file]["removed"] += 1
            for item in reasons:
                counts[finding.file][f"reason:{item.get('reason') if isinstance(item, dict) else None}"] += 1


def _file_facts(item: dict[str, Any]) -> tuple[Any, Any, Any]:
    return item.get("rows"), item.get("bytes"), item.get("sha256")


def _apply_step(
    run: _Run, step: _Step, removed: list[Finding], removed_digest: tuple[str, int], problems: _Problems
) -> None:
    """Follow every row through one filter run: kept (and where), or removed (and
    why). Every row of the run before must be one or the other, once."""
    number = step.number
    level, parent = run.levels[number], run.levels[number - 1]
    summary = level.summary
    scanners = tuple(step.scanners)
    parent_rows = run.rows[number - 1]
    label = f"{run.name}, step {number}"

    source = summary.get("source")
    read = (
        {item.get("path"): _file_facts(item) for item in source.get("files", []) if isinstance(item, dict)}
        if isinstance(source, dict)
        else {}
    )
    wrote = {item["path"]: _file_facts(item) for item in parent.files}
    if read != wrote:
        problems.add(f"{label}: the filter run read different files from the ones the run before wrote.")
        return
    if {item["path"] for item in level.files} != set(wrote):
        problems.add(f"{label}: the filter run wrote different files from the ones it read.")
        return

    counts: dict[str, Counter] = {file: Counter() for file in parent_rows}
    _kept_files(run, step, scanners, problems, counts)
    _removed_rows(run, step, scanners, removed, problems, counts)
    if problems:
        return

    for file, rows_before in parent_rows.items():
        if any(len(row.steps) != number for row in rows_before):
            problems.add(f"{label}: some rows of {file} are neither kept nor removed.")
    recorded = summary.get("per_file")
    recorded = recorded if isinstance(recorded, dict) else {}
    totals: Counter = Counter()
    for file, rows_before in parent_rows.items():
        counted = counts[file]
        by_reason = {key[len("reason:") :]: value for key, value in sorted(counted.items()) if key.startswith("reason:")}
        stat = {
            "rows_in": len(rows_before),
            "rows_kept": counted["kept"],
            "rows_removed": counted["removed"],
            "removed_by_reason": by_reason,
        }
        if recorded.get(file) != stat:
            problems.add(f"{label}: {file} has {stat} here, but the filter run's summary.json says {recorded.get(file)}.")
        totals["rows_in"] += stat["rows_in"]
        totals["kept"] += stat["rows_kept"]
        totals["removed"] += stat["rows_removed"]
        totals.update({f"reason:{key}": value for key, value in by_reason.items()})
    if set(recorded) != set(parent_rows):
        problems.add(f"{label}: the filter run's summary.json lists different files from the ones it read.")
    if (summary.get("rows"), (summary.get("source") or {}).get("rows_read")) != (
        {"written": totals["kept"], "removed": totals["removed"]},
        totals["rows_in"],
    ):
        problems.add(f"{label}: the totals in the filter run's summary.json differ from the rows read here.")
    by_reason_total = {key[len("reason:") :]: value for key, value in sorted(totals.items()) if key.startswith("reason:")}
    if summary.get("removed_by_reason") != by_reason_total:
        problems.add(f"{label}: removed_by_reason in the filter run's summary.json differs from the removed rows.")
    removed_file = summary.get("removed_rows")
    if not isinstance(removed_file, dict) or (removed_file.get("sha256"), removed_file.get("entries")) != (
        removed_digest[0],
        totals["removed"],
    ):
        problems.add(f"{label}: removed_rows.ndjson isn't the one the filter run's summary.json lists.")
    run.step_counts.append(
        {
            "step": number,
            "run": str(level.info.run_dir),
            "rows_in": totals["rows_in"],
            "kept": totals["kept"],
            "removed": totals["removed"],
            "removed_by_reason": by_reason_total,
        }
    )


def _not_reached(run: _Run, step: _Step) -> int:
    """Rows an earlier step removed have no scan in this step. Returns how many."""
    number = step.number
    entry = {name: {"state": NOT_REACHED} for name in step.scanners}
    missing = 0
    for rows in run.rows[0].values():
        for row in rows:
            if len(row.steps) == number - 1:
                row.steps.append({"step": number, "outcome": NOT_REACHED, "scanners": dict(entry)})
                missing += 1
    return missing


# -------------------------------------------------------------------------
# What each scanner found
# -------------------------------------------------------------------------


def _check_scan_counts(runs: dict[str, _Run], step: _Step, problems: _Problems) -> None:
    """For each file the scanner read: rows read, rows with text and rows without
    text equal what the scan recorded."""
    number = step.number
    expected: dict[str, dict[str, int]] = {}
    for run in runs.values():
        for file, rows in run.rows[number - 1].items():
            expected[f"{run.name}/{file}"] = {"rows": len(rows)}
    for name, info in step.scanners.items():
        recorded = LOADERS[name](Path(info["results_dir"]).expanduser().resolve()).per_file
        if recorded is None:
            problems.add(f"{LABELS[name]} (step {number}): the results don't record counts for each file.")
            continue
        bit = _BIT[name]
        off = []
        for run in runs.values():
            for file, rows in run.rows[number - 1].items():
                path = f"{run.name}/{file}"
                with_text = sum(1 for row in rows if row.text & bit)
                want = {"rows_read": len(rows), "rows_with_text": with_text, "rows_without_text": len(rows) - with_text}
                got = {key: (recorded.get(path) or {}).get(key) for key in want}
                if got != want:
                    off.append(f"{path} (here {want}, the scan {got})")
        extra = sorted(set(recorded) - set(expected))
        if extra:
            off.append(f"{', '.join(extra[:3])} (counted by the scan, not part of this step)")
        if off:
            problems.add(
                f"{LABELS[name]} (step {number}): its counts of rows with and without text differ from the "
                f"rows of {len(off):,} file(s): {_listed(off)}."
            )


def _attach_findings(
    step: _Step, name: str, runs: dict[str, _Run], locator: _Runs, problems: _Problems
) -> int:
    """Read one scanner's findings of a step and put each in the entry of its
    Prepared row. Returns how many findings (not counting the duplicated side)
    were attached."""
    source = step.sources[name]
    number = step.number
    scan_root = source.scan_root
    cache: dict[str, list[_Row] | None] = {}

    def where_of(finding: Finding, other: bool = False) -> str:
        return f"{LABELS[name]} {finding.results_file} line {finding.results_line}" + (" (the other row)" if other else "")

    def rows_of(file: str, finding: Finding, other: bool) -> list[_Row] | None:
        if file in cache:
            return cache[file]
        rows: list[_Row] | None = None
        located = locator.locate(scan_root, file)
        if isinstance(located, str):
            problems.add(f"{where_of(finding, other)}: {located}")
        else:
            run_dir, in_run = located
            run = runs.get(run_dir.relative_to(Path(scan_root)).as_posix())
            rows = run.rows[number - 1].get(in_run) if run is not None else None
            if rows is None:
                problems.add(f"{where_of(finding, other)}: {file} isn't a file of the run before step {number}.")
        cache[file] = rows
        return rows

    def row_at(file: str, line: int, sha256: str, finding: Finding, other: bool = False) -> _Row | None:
        rows = rows_of(file, finding, other)
        if rows is None:
            return None
        if not 1 <= line <= len(rows):
            problems.add(f"{where_of(finding, other)}: line {line} isn't a line of {file} (it has {len(rows):,} rows).")
            return None
        row = rows[line - 1]
        if row.sha256 != sha256:
            problems.add(
                f"{where_of(finding, other)}: {file} line {line} has fingerprint {row.sha256}, "
                f"but the finding says {sha256}."
            )
            return None
        return row

    found_by_file: Counter = Counter()
    attached = 0
    for finding in iter_findings(source):
        if finding.side != SIDE_DUPLICATE_OF:
            found_by_file[finding.file] += 1
        if finding.reference:
            continue
        row = row_at(finding.file, finding.line, finding.sha256, finding)
        if row is None:
            continue
        state = row.steps[number - 1]["scanners"].get(name)
        if state is None or state["state"] != SCANNED:
            problems.add(
                f"{where_of(finding)}: {finding.file} line {finding.line} has a finding, but the scan had "
                "no text in that row."
            )
            continue
        at = [finding.results_file, finding.results_line]
        detail = finding.detail or {}
        if name == "dedup":
            other = detail["other"]
            if other["reference"]:
                other_at = {"reference": True, "file": other["file"], "line": other["line"]}
            else:
                other_row = row_at(other["file"], other["line"], other["source_sha256"], finding, other=True)
                if other_row is None:
                    continue
                other_at = {"run": other_row.run, "file": other_row.file, "line": other_row.line}
            if finding.side == SIDE_ROW:
                state["findings"].append({"duplicate_of": other_at, "at": at})
                attached += 1
            else:
                state["duplicated_by"].append({"row": other_at, "at": at})
        else:
            detail["at"] = at
            state["findings"].append(detail)
            attached += 1
    if name != "regex_pii":
        for text in _count_problems(source, found_by_file):
            problems.add(f"Step {number}: {text}")
    return attached


# -------------------------------------------------------------------------
# Writing
# -------------------------------------------------------------------------


def _entry(row: _Row) -> dict[str, Any]:
    not_scanned = []
    for step in row.steps:
        for name, state in step["scanners"].items():
            if state["state"] == NO_TEXT:
                not_scanned.append({"step": step["step"], "scanner": name, "why": NO_TEXT})
            elif state["state"] == NOT_REACHED:
                not_scanned.append({"step": step["step"], "scanner": name, "why": "removed_earlier"})
    if row.at is not None:
        file, line, offset = row.at
        final: dict[str, Any] = {"status": "kept", "run": row.run, "file": file, "line": line, "byte_offset": offset}
    else:
        final = {"status": "removed", "step": row.removed_at}
    return {
        "prepared": {
            "run": row.run,
            "file": row.file,
            "line": row.line,
            "byte_offset": row.offset,
            "source_sha256": row.sha256,
        },
        "origin": row.origin,
        "steps": row.steps,
        "final": final,
        "not_scanned": not_scanned,
    }


def _prefix(run: str, file: str, line: int) -> bytes:
    return f'{{"prepared":{{"run":{_dumps(run)},"file":{_dumps(file)},"line":{line},'.encode("utf-8")


def _removed_entry(row: _Row) -> dict[str, Any]:
    """The removed-rows index entry of a row a step removed: where the row is,
    why it was removed, and the names (NER labels, regex PII categories, toxicity
    verdicts) of what removed it, each with how many findings."""
    step = row.steps[row.removed_at - 1]
    reasons: list[Any] = []
    ner_labels: Counter = Counter()
    regex_categories: Counter = Counter()
    toxicity_verdicts: Counter = Counter()
    for item in step["reasons"]:
        reason = item.get("reason") if isinstance(item, dict) else None
        reasons.append(reason)
        findings = item.get("findings") if isinstance(item, dict) else None
        for finding in findings if isinstance(findings, list) else ():
            if not isinstance(finding, dict):
                continue
            if reason == "ner" and isinstance(finding.get("label"), str):
                ner_labels[finding["label"]] += 1
            elif reason == "regex_pii" and isinstance(finding.get("category"), str):
                regex_categories[finding["category"]] += 1
            elif isinstance(reason, str) and reason.startswith("toxicity_"):
                # A guard error has no verdict.
                verdict = finding.get("verdict")
                toxicity_verdicts[verdict if isinstance(verdict, str) else "guard_error"] += 1
    return {
        "run": row.run,
        "step": row.removed_at,
        "file": row.file,
        "line": row.line,
        "byte_offset": row.offset,
        "reasons": reasons,
        "ner_labels": dict(sorted(ner_labels.items())),
        "regex_categories": dict(sorted(regex_categories.items())),
        "toxicity_verdicts": dict(sorted(toxicity_verdicts.items())),
        "original": row.origin,
    }


class _RemovedIndex:
    """The removed-rows index of one run and one filter step, written as the
    rows go by, with what it holds counted for `summary.json`."""

    def __init__(self, path: str, handle: Any) -> None:
        self.path = path
        self.handle = handle
        self.entries = 0
        self.bytes = 0
        self.digest = hashlib.sha256()
        self.counts: dict[str, Counter] = {key: Counter() for key in REMOVED_COUNTS}

    def add(self, row: _Row) -> None:
        entry = _removed_entry(row)
        data = (_dumps(entry) + "\n").encode("utf-8")
        self.handle.write(data)
        self.entries += 1
        self.bytes += len(data)
        self.digest.update(data)
        # A row counts once for each name it has, however many findings it has under it.
        for key, field_name in REMOVED_COUNTS.items():
            self.counts[key].update({str(name): 1 for name in entry[field_name]})

    def summary(self) -> dict[str, Any]:
        return {
            "file": self.path,
            "entries": self.entries,
            "bytes": self.bytes,
            "sha256": self.digest.hexdigest(),
            **{key: dict(sorted(counter.items())) for key, counter in self.counts.items()},
        }


def _write_run(out: Path, run: _Run, totals: dict[str, Counter], temps: list[tuple[Path, Path]]) -> dict[str, Any]:
    """Write the status file and the offsets file of every Prepared file of a run,
    and the removed-rows index of each of its filter steps."""
    files: dict[str, Any] = {}
    with ExitStack() as stack:
        indexes: dict[int, _RemovedIndex] = {}
        for number in range(1, len(run.levels)):
            index_rel = removed_index_path(run.name, number)
            index_final = out / index_rel
            if any(final == index_final for _, final in temps):
                raise StatusError(f"{run.name}: two files would be written to {index_rel}.")
            index_final.parent.mkdir(parents=True, exist_ok=True)
            index_temp = index_final.with_name(index_final.name + PARTIAL_SUFFIX)
            temps.append((index_temp, index_final))
            indexes[number] = _RemovedIndex(index_rel, stack.enter_context(index_temp.open("wb")))

        for file, rows in run.rows[0].items():
            status_rel, offsets_rel = status_paths(run.name, file)
            status_final, offsets_final = out / status_rel, out / offsets_rel
            if any(final in (status_final, offsets_final) for _, final in temps):
                raise StatusError(f"{run.name}: two Prepared files would be written to {status_rel}.")
            status_final.parent.mkdir(parents=True, exist_ok=True)
            status_temp = status_final.with_name(status_final.name + PARTIAL_SUFFIX)
            offsets_temp = offsets_final.with_name(offsets_final.name + PARTIAL_SUFFIX)
            temps.extend([(status_temp, status_final), (offsets_temp, offsets_final)])
            offset = 0
            with status_temp.open("wb") as status, offsets_temp.open("wb") as offsets:
                for row in rows:
                    for step in row.steps:
                        for name, state in step["scanners"].items():
                            totals[f"step{step['step']}:{name}"][state["state"]] += 1
                            totals[f"step{step['step']}:{name}"]["findings"] += len(state.get("findings", ()))
                    if row.origin is None:
                        totals["rows"]["without_origin"] += 1
                    if row.removed_at is not None:
                        indexes[row.removed_at].add(row)
                    offsets.write(offset_line(offset))
                    data = (_dumps(_entry(row)) + "\n").encode("utf-8")
                    status.write(data)
                    offset += len(data)
                offsets.write(offset_line(offset))
            files[file] = {
                "rows": len(rows),
                "status_file": status_rel,
                "offsets_file": offsets_rel,
                "status_bytes": offset,
                "offsets_bytes": (len(rows) + 1) * len(offset_line(0)),
            }
    return {"files": files, "removed_index": {str(number): index.summary() for number, index in indexes.items()}}


def _verify_removed_index(out: Path, run: _Run, number: int, item: dict[str, Any]) -> list[str]:
    """Read a written removed-rows index back: the file on disk is the bytes that
    were written, it has as many entries as the step removed rows, and its counts
    by reason are the filter run's own."""
    label = f"{run.name}, step {number}"
    path = out / item["file"]
    digest = hashlib.sha256()
    lines = 0
    with path.open("rb") as handle:
        for raw in handle:
            digest.update(raw)
            lines += 1
    problems = []
    if (lines, path.stat().st_size, digest.hexdigest()) != (item["entries"], item["bytes"], item["sha256"]):
        problems.append(f"{label}: the removed-rows index on disk isn't what was written.")
    counted = run.step_counts[number - 1]
    if item["entries"] != counted["removed"]:
        problems.append(
            f"{label}: the removed-rows index has {item['entries']:,} entries, "
            f"but the step removed {counted['removed']:,} rows."
        )
    if item["by_reason"] != counted["removed_by_reason"]:
        problems.append(f"{label}: the removed-rows index counts rows by reason differently from the filter run.")
    return problems


def _verify_written(out: Path, run: str, file: str, item: dict[str, Any]) -> list[str]:
    """Read a written status file back with its index: every entry starts where
    the index says, and is the status of the line it should be."""
    rows = item["rows"]
    status_path, offsets_path = out / item["status_file"], out / item["offsets_file"]
    if offsets_path.stat().st_size != item["offsets_bytes"] or status_path.stat().st_size != item["status_bytes"]:
        return [f"{run}/{file}: the files on disk aren't the sizes that were written."]
    expected = offsets_path.read_bytes()
    starts = [int(expected[at : at + 20]) for at in range(0, len(expected), 21)]
    position = number = 0
    with status_path.open("rb") as handle:
        for number, raw in enumerate(handle, 1):
            if number > rows or starts[number - 1] != position or not raw.startswith(_prefix(run, file, number)):
                return [f"{run}/{file}: entry {number} isn't where the index says, or isn't the status of line {number}."]
            position += len(raw)
    if number != rows or starts[-1] != position:
        return [f"{run}/{file}: {number:,} entries were written for {rows:,} rows."]
    return []


# -------------------------------------------------------------------------
# The status
# -------------------------------------------------------------------------


def build_row_status(filter_root: str | Path, output_dir: str | Path) -> dict[str, Any]:
    """Write the status of every Prepared row of the chain that ends at
    `filter_root` (the output folder of its last filter step) into `output_dir`,
    and return the summary.

    Writes the status files, `summary.json` and `run_identity.json`, and (only if
    every check passed) `_SUCCESS`. Raises `StatusError` when it refuses to start
    or a check fails before anything is written. Nothing it reads is changed.
    """
    plan = _make_plan(filter_root, output_dir)
    out = plan.output
    # A marker or summary from an earlier try must not look like this one's, if this one fails.
    for name in (SUCCESS_FILENAME, SUMMARY_FILENAME):
        (out / name).unlink(missing_ok=True)
    runs = {name: _Run(name, levels) for name, levels in plan.chains.items()}
    problems = _Problems()

    print(f"Reading the Prepared rows of {len(runs):,} run(s) ...", flush=True)
    for run in runs.values():
        _read_prepared(run, plan, problems)
    problems.raise_if_any()
    prepared_rows = sum(len(rows) for run in runs.values() for rows in run.rows[0].values())
    print(f"  {prepared_rows:,} rows.", flush=True)

    # Follow every row through every step, before any finding is read: a duplicate
    # can be a row of another run.
    for step in plan.steps:
        print(f"Step {step.number}: reading the line maps and the removed rows ...", flush=True)
        removed_by_parent: dict[str, list[Finding]] = {}
        parents = {run.levels[step.number].summary["parent"]["run_dir"]: run.name for run in runs.values()}
        try:
            for finding in iter_findings(removed_rows_source(step.number, step.output_root)):
                if finding.scan_root not in parents:
                    problems.add(f"{finding.results_file}: removed a row of {finding.scan_root}, which isn't a run of this chain.")
                    continue
                removed_by_parent.setdefault(parents[finding.scan_root], []).append(finding)
        except (FindingsError, OSError, UnicodeDecodeError) as exc:
            raise StatusError(f"Step {step.number}: {exc}") from exc
        for run in runs.values():
            removed_path = run.levels[step.number].info.run_dir / REMOVED_ROWS_FILENAME
            try:
                digest = (file_sha256(removed_path), removed_path.stat().st_size)
            except OSError as exc:
                problems.add(f"{removed_path} can't be read: {exc}")
                continue
            _apply_step(run, step, removed_by_parent.get(run.name, []), digest, problems)
            problems.raise_if_any()
            _not_reached(run, step)
        _check_scan_counts(runs, step, problems)
        problems.raise_if_any()

    locator = _Runs()
    attached: Counter = Counter()
    for step in plan.steps:
        for name in step.scanners:
            print(f"Step {step.number}: reading the {LABELS[name]} findings ...", flush=True)
            try:
                found = _attach_findings(step, name, runs, locator, problems)
            except (FindingsError, OSError, UnicodeDecodeError) as exc:
                raise StatusError(f"Step {step.number}, {LABELS[name]}: {exc}") from exc
            attached[(step.number, name)] = found
            print(f"  {found:,} finding(s).", flush=True)
            problems.raise_if_any()

    # Every check on what was read has passed. Write.
    check_run_identity(out, plan.identity())
    totals: dict[str, Counter] = {"rows": Counter()}
    for step in plan.steps:
        for name in step.scanners:
            totals[f"step{step.number}:{name}"] = Counter()
    temps: list[tuple[Path, Path]] = []
    run_summaries: dict[str, Any] = {}
    try:
        for run in runs.values():
            print(f"Writing {run.name} ...", flush=True)
            run_summaries[run.name] = _write_run(out, run, totals, temps)
        for temp, final in temps:
            os.replace(temp, final)
    except BaseException:
        for temp, _final in temps:
            temp.unlink(missing_ok=True)
        raise

    failures: list[str] = []
    for run in runs.values():
        for file, item in run_summaries[run.name]["files"].items():
            failures.extend(_verify_written(out, run.name, file, item))
            if item["rows"] != len(run.rows[0][file]):
                failures.append(f"{run.name}/{file}: {item['rows']:,} entries for {len(run.rows[0][file]):,} Prepared rows.")
        for number, item in run_summaries[run.name]["removed_index"].items():
            failures.extend(_verify_removed_index(out, run, int(number), item))
        kept = sum(len(rows) for rows in run.rows[-1].values())
        removed = sum(step["removed"] for step in run.step_counts)
        prepared = sum(len(rows) for rows in run.rows[0].values())
        if kept + removed != prepared or kept != sum(item["rows"] for item in run.levels[-1].files):
            failures.append(f"{run.name}: kept ({kept:,}) plus removed ({removed:,}) isn't the {prepared:,} Prepared rows.")
        run_summaries[run.name].update(
            {
                "prepared_run": str(run.levels[0].info.run_dir),
                "rows": {"prepared": prepared, "kept": kept, "removed": removed},
                "steps": run.step_counts,
            }
        )

    warnings = []
    if totals["rows"]["without_origin"]:
        warnings.append(
            f"{totals['rows']['without_origin']:,} Prepared row(s) have no {ORIGIN_KEY} note, so their entries "
            "say nothing about the Original row."
        )
    kept_total = sum(item["rows"]["kept"] for item in run_summaries.values())
    removed_by_step = {
        str(step.number): sum(run.step_counts[step.number - 1]["removed"] for run in runs.values()) for step in plan.steps
    }
    # The same totals for each reason, added up over every run, so a page doesn't have to.
    removed_by_step_and_reason: dict[str, Counter] = {str(step.number): Counter() for step in plan.steps}
    for run in runs.values():
        for counted in run.step_counts:
            removed_by_step_and_reason[str(counted["step"])].update(counted["removed_by_reason"])
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool": STATUS_TOOL,
        "report_version": SUMMARY_VERSION,
        "inputs": {
            "last_filter_root": str(plan.last_root),
            "prepared_root": str(plan.prepared_root),
            "steps": [
                {
                    "step": step.number,
                    "output_root": str(step.output_root),
                    "scan_root": str(step.scan_root),
                    "rules": step.rules,
                    "scanners": step.scanners,
                }
                for step in plan.steps
            ],
        },
        "runs": run_summaries,
        "totals": {
            "prepared_rows": prepared_rows,
            "kept": kept_total,
            "removed": sum(removed_by_step.values()),
            "removed_by_step": removed_by_step,
            "removed_by_step_and_reason": {
                number: dict(sorted(counter.items())) for number, counter in removed_by_step_and_reason.items()
            },
            "by_step_and_scanner": {
                key.removeprefix("step").replace(":", " ", 1): dict(sorted(value.items()))
                for key, value in totals.items()
                if key != "rows"
            },
            "findings_by_step_and_scanner": {f"{number} {name}": count for (number, name), count in sorted(attached.items())},
            "rows_without_origin": totals["rows"]["without_origin"],
        },
        "verdict": "pass" if not failures else "fail",
        "checks": {"passed": not failures, "failures": failures, "warnings": warnings},
    }
    write_report(out / SUMMARY_FILENAME, summary)
    if summary["checks"]["passed"]:
        (out / SUCCESS_FILENAME).touch()
    return summary


# -------------------------------------------------------------------------
# Command line
# -------------------------------------------------------------------------


def _print_summary(summary: dict[str, Any], output: Path) -> None:
    totals = summary["totals"]
    print()
    print(f"{'run':44s} {'prepared':>10s} {'kept':>10s} {'removed':>10s}")
    for name, run in summary["runs"].items():
        rows = run["rows"]
        print(f"{name:44s} {rows['prepared']:>10,} {rows['kept']:>10,} {rows['removed']:>10,}")
    print(f"{'TOTAL':44s} {totals['prepared_rows']:>10,} {totals['kept']:>10,} {totals['removed']:>10,}")
    for step, count in totals["removed_by_step"].items():
        print(f"  removed at step {step}: {count:,}")
    for warning in summary["checks"]["warnings"]:
        print(f"WARNING: {warning}")
    for failure in summary["checks"]["failures"]:
        print(f"FAILED: {failure}")
    print(f"Written to {output}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--filter-root",
        type=Path,
        required=True,
        help="The output folder of the last filter step of the chain.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Where the status is written: a new folder, outside every folder the tool reads.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = build_row_status(args.filter_root, args.output_dir)
    except (StatusError, RunIdentityError) as exc:
        print("error:", file=sys.stderr)
        for problem in getattr(exc, "problems", [str(exc)]):
            print(f"  - {problem}", file=sys.stderr)
        return 2
    _print_summary(summary, args.output_dir.expanduser().resolve())
    return 0 if summary["checks"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
