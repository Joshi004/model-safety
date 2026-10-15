#!/usr/bin/env python3
"""Remove the rows scanners flagged from finished runs, and keep the books.

For each run under ``--input-root`` (Prepared runs or earlier filter runs), this
writes one filter run at the same relative path under ``--output-root``:

  chunk-0/data_000000.jsonl      the kept lines, byte for byte (same names as the parent)
  line_map/chunk-0/data_000000.ndjson
                                 one entry per kept line: where it came from in the parent
  removed_rows.ndjson            one entry per removed row: where it was, why, and the whole line
  run_identity.json, summary.json, _SUCCESS

``summary.json`` has a Prepared run's shape wherever the two overlap, so the run
finder, the scanners and the coverage report read a filter run like a Prepared
run. A removed-row entry or a line-map entry starts with ``{"_parent"``, which
scanners refuse (``modelsafety/contract/origin.py``).

Nothing is changed in the input folder. The filter refuses to start unless every
run under it finished, every scanner result passes the coverage checks for
exactly those runs, and every finding points at a line whose fingerprint matches.

``--drop-list FILE`` removes exactly the rows a list names (one JSON line each: file, line,
fingerprint, and the scanner they came from, see ``rules.read_drop_list``), with the reason
``manual``. It can stand alone, or be combined with scanner results. It is held to the same
checks: every line must be a line of a finished run, and its fingerprint must match.

Usage:
    python3 modelsafety/filter/filter_rows.py --input-root R --output-root O \\
        [--pii-run-dir D] [--ner-run-dir D] [--toxicity-run-dir D] [--dedup-run-dir D] \\
        [--drop-list FILE] \\
        [--regex-ignore-category C] [--ner-ignore-label L] [--toxicity-remove-verdict V] \\
        [--toxicity-guard-errors remove|refuse] [--dedup-mode global|cross]

Exit code: 0 if every run passed, 2 if the filter refused to start or anything failed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path, PurePosixPath
from typing import Any

# Run as a script, so only this folder is on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modelsafety.contract.fingerprints import file_sha256, line_fingerprint  # noqa: E402
from modelsafety.contract.origin import check_line  # noqa: E402
from modelsafety.contract.run_finder import (  # noqa: E402
    FILTER_ROWS_IDENTITY_TOOL,
    FILTER_ROWS_TOOL,
    SUCCESS_FILENAME,
    RunFinderError,
    ScanFile,
    ScanSelection,
    read_json_or_none,
    select_scan_files,
)
from modelsafety.contract.run_identity import (  # noqa: E402
    IDENTITY_FILENAME,
    RunIdentityError,
    check_run_identity,
    compare_run_identity,
    make_identity,
)
from modelsafety.data_screening.pii.regex.pii_output import PII_CATEGORIES  # noqa: E402
from modelsafety.filter.rules import (  # noqa: E402
    DEDUP_MODES,
    DROP_LIST,
    GUARD_ERROR_CHOICES,
    READERS,
    REASON_SCANNERS,
    TOXICITY_REMOVABLE_VERDICTS,
    FilterRules,
    FindingsError,
    FindingsRead,
    HitIndex,
    read_drop_list,
)
from modelsafety.results.coverage import LABELS, LOADERS, SCANNERS, check_scanner  # noqa: E402

REMOVED_ROWS_FILENAME = "removed_rows.ndjson"
LINE_MAP_FOLDER = "line_map"
SUMMARY_FILENAME = "summary.json"
PARTIAL_SUFFIX = ".partial"
MAX_LISTED = 5

# The `--*-run-dir` option for each scanner.
RESULTS_OPTIONS = {
    "regex_pii": "--pii-run-dir",
    "ner": "--ner-run-dir",
    "toxicity": "--toxicity-run-dir",
    "dedup": "--dedup-run-dir",
}


def _label(name: str) -> str:
    """What a message calls a source of findings: a scanner, or the drop list."""
    return "drop list" if name == DROP_LIST else LABELS[name]


class FilterError(RuntimeError):
    """The filter won't start, or a run couldn't be written. The message says why."""

    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class RunPlan:
    """One run to filter. `run` is its path under the scanned folder."""

    run: str
    parent_dir: Path
    output_dir: Path
    parent_tool: str | None
    files: tuple[ScanFile, ...]

    def in_run(self, item: ScanFile) -> str:
        """The file's path inside the run folder."""
        return PurePosixPath(item.path).relative_to(self.run).as_posix()


@dataclass
class FilterPlan:
    input_root: Path
    output_root: Path
    rules: FilterRules
    selection: ScanSelection
    runs: list[RunPlan]
    # {scanner: {"results_dir", "tool", "mode"}} for each scanner given.
    scanners: dict[str, dict[str, Any]]
    index: HitIndex
    reads: dict[str, FindingsRead]
    # {"path", "sha256"} of the drop list, or None when none was given.
    drop_list: dict[str, str] | None = None


@dataclass
class FileResult:
    """What was done to one parent file."""

    path: str
    rows_in: int
    kept: int = 0
    removed: int = 0
    removed_by_reason: Counter = field(default_factory=Counter)
    kept_bytes: int = 0
    kept_sha256: str = ""

    @property
    def map_path(self) -> str:
        return f"{LINE_MAP_FOLDER}/{PurePosixPath(self.path).with_suffix('.ndjson').as_posix()}"


# -------------------------------------------------------------------------
# Before starting (read-only)
# -------------------------------------------------------------------------


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _path_problems(
    input_root: Path, output_root: Path, results: dict[str, Path], drop_list: Path | None = None
) -> list[str]:
    problems = []
    if not input_root.is_dir():
        problems.append(f"The input folder isn't a directory: {input_root}")
    if _overlaps(output_root, input_root):
        problems.append(
            f"The output folder ({output_root}) and the input folder ({input_root}) must not be "
            "the same, or inside one another."
        )
    if drop_list is not None:
        if not drop_list.is_file():
            problems.append(f"The drop list isn't a file: {drop_list}")
        if _overlaps(output_root, drop_list):
            problems.append(
                f"The drop list ({drop_list}) must not be the output folder ({output_root}), or inside it."
            )
    for name, folder in results.items():
        if _overlaps(output_root, folder):
            problems.append(
                f"The output folder ({output_root}) and the {_label(name)} results ({folder}) must "
                "not be the same, or inside one another."
            )
    return problems


def _listed(lines: list[str], total: int) -> str:
    shown = "; ".join(lines[:MAX_LISTED])
    return f"{shown}; and {total - MAX_LISTED:,} more" if total > MAX_LISTED else shown


def _scanner_info(name: str, results_dir: Path) -> dict[str, Any]:
    identity = read_json_or_none(results_dir / IDENTITY_FILENAME)
    identity = identity if isinstance(identity, dict) else {}
    return {"results_dir": str(results_dir), "tool": identity.get("tool"), "mode": identity.get("mode")}


def _run_plans(selection: ScanSelection, input_root: Path, output_root: Path) -> list[RunPlan]:
    files_by_run: dict[str, list[ScanFile]] = {}
    for item in selection.files:
        files_by_run.setdefault(item.run or "", []).append(item)
    plans = []
    for run, files in sorted(files_by_run.items()):
        parent_dir = input_root / run
        summary = read_json_or_none(parent_dir / SUMMARY_FILENAME)
        tool = summary.get("tool") if isinstance(summary, dict) else None
        plans.append(RunPlan(run, parent_dir, output_root / run, tool, tuple(files)))
    return plans


def _identity(
    run: RunPlan,
    input_root: Path,
    rules: FilterRules,
    scanners: dict[str, dict[str, Any]],
    drop_list: dict[str, str] | None = None,
) -> dict[str, Any]:
    settings = {
        "scan_root": str(input_root),
        "parent_run": run.run,
        "rules": rules.to_dict(),
        "scanner_results": scanners,
    }
    if drop_list is not None:
        # Only when given, so a filter run without one keeps the identity it always had.
        settings["drop_list"] = drop_list
    return make_identity(
        tool=FILTER_ROWS_IDENTITY_TOOL,
        mode=None,
        settings=settings,
        input_root=str(run.parent_dir),
        input_files=sorted(
            (
                {
                    "path": run.in_run(item),
                    "rows": item.rows,
                    "bytes": item.size_bytes,
                    "sha256": item.sha256,
                }
                for item in run.files
            ),
            key=lambda entry: entry["path"],
        ),
    )


def _run_problems(
    runs: list[RunPlan],
    input_root: Path,
    rules: FilterRules,
    scanners: dict[str, dict[str, Any]],
    drop_list: dict[str, str] | None = None,
) -> list[str]:
    """Each run's output folder is empty, or already holds this same filter run."""
    problems = []
    for run in runs:
        for item in run.files:
            if run.in_run(item).split("/")[0] == LINE_MAP_FOLDER:
                problems.append(
                    f"{item.path} is inside a folder called {LINE_MAP_FOLDER}, which a filter run "
                    "keeps for its line maps."
                )
        try:
            state, differences = compare_run_identity(
                run.output_dir, _identity(run, input_root, rules, scanners, drop_list)
            )
        except RunIdentityError as exc:
            problems.append(str(exc))
            continue
        if state == "no_identity":
            problems.append(
                f"{run.output_dir} isn't empty and has no {IDENTITY_FILENAME}, so it isn't a "
                "filter run of this filter."
            )
        elif state == "different":
            problems.append(
                f"{run.output_dir} already holds a different filter run (previous -> requested): "
                + "; ".join(differences)
            )
    return problems


def _count_problems(
    name: str, results_dir: Path, read: FindingsRead, selection: ScanSelection
) -> list[str]:
    """The findings file has exactly the findings the scan counted, for every file."""
    if read.findings_by_file is None:
        return []
    recorded = LOADERS[name](results_dir).per_file or {}
    off = []
    for item in selection.files:
        counted = recorded.get(item.path, {}).get("findings")
        found = read.findings_by_file.get(item.path, 0)
        if counted != found:
            off.append(
                f"{item.path} (the scan counted {'no' if counted is None else f'{counted:,}'}, "
                f"its findings file has {found:,})"
            )
    if not off:
        return []
    return [
        f"{_label(name)}: the findings file doesn't match the scan's own count of findings for "
        f"{len(off):,} file(s): {_listed(off, len(off))}."
    ]


def check_filter_inputs(
    input_root: str | Path,
    output_root: str | Path,
    results_dirs: dict[str, str | Path | None],
    rules: FilterRules,
    drop_list: str | Path | None = None,
) -> FilterPlan:
    """Check everything that must hold before a row is written, and read every
    scanner's findings and the drop list. Returns the plan, or raises `FilterError`
    naming every problem found at that point. Writes nothing."""
    root = Path(input_root).expanduser().resolve()
    output = Path(output_root).expanduser().resolve()
    unknown = sorted(set(results_dirs) - set(SCANNERS))
    if unknown:
        raise FilterError(f"Unknown scanners: {', '.join(unknown)}. They are: {', '.join(SCANNERS)}.")
    given = {
        name: Path(folder).expanduser().resolve()
        for name, folder in results_dirs.items()
        if folder is not None
    }
    drop = Path(drop_list).expanduser().resolve() if drop_list is not None else None

    problems = rules.validate()
    if not given and drop is None:
        problems.append("No scanner results folder and no drop list was given, so there is nothing to filter by.")
    problems.extend(_path_problems(root, output, given, drop))
    if problems:
        raise FilterError(problems)
    # Read once here, so the identity pins the exact list that is filtered by.
    drop_info = {"path": str(drop), "sha256": file_sha256(drop)} if drop is not None else None

    try:
        selection = select_scan_files(root)
    except RunFinderError as exc:
        raise FilterError(str(exc)) from exc
    if selection.skipped:
        lines = [
            f"{item['kind']} {item['path']}" + (f" ({item['status']})" if item["status"] else "")
            + f": {item['reason']}"
            for item in selection.skipped
        ]
        problems.append(
            f"{len(lines):,} run(s) or file(s) under {root} were skipped, so the clean data would "
            f"quietly miss them. Finish or remove them first. {_listed(lines, len(lines))}"
        )

    for name in SCANNERS:
        if name not in given:
            continue
        section = check_scanner(name, given[name], selection)
        if section["status"] == "pass":
            continue
        reasons = [item["message"] for item in section.get("problems", [])] or [
            section.get("reason") or "It didn't pass."
        ]
        problems.append(
            f"The {LABELS[name]} results ({given[name]}) don't pass the coverage checks "
            f"for these runs: {_listed(reasons, len(reasons))}"
        )

    scanners = {name: _scanner_info(name, folder) for name, folder in given.items()}
    runs = _run_plans(selection, root, output)
    problems.extend(_run_problems(runs, root, rules, scanners, drop_info))
    if problems:
        raise FilterError(problems)

    rows_by_file = {item.path: item.rows for item in selection.files}
    index = HitIndex()
    reads: dict[str, FindingsRead] = {}
    for name in SCANNERS:
        if name not in given:
            continue
        print(f"Reading the {LABELS[name]} findings from {given[name]} ...", flush=True)
        try:
            read = READERS[name](given[name], index, rules, rows_by_file)
        except FindingsError as exc:
            raise FilterError(f"{LABELS[name]}: {exc}") from exc
        reads[name] = read
        problems.extend(read.problems)
        problems.extend(_count_problems(name, given[name], read, selection))
        print(f"  {read.hit_findings:,} finding(s) remove rows.", flush=True)
        ignored = read.details.get("ignored_by_category") or read.details.get("ignored_by_label")
        if ignored:
            print(
                "  Ignored by the rules: " + ", ".join(f"{key} {count:,}" for key, count in ignored.items()),
                flush=True,
            )
        if read.details.get("guard_errors_by_slice"):
            print(
                f"  Guard errors or verdicts not given: {sum(read.details['guard_errors_by_slice'].values()):,}.",
                flush=True,
            )
    if drop is not None:
        print(f"Reading the drop list {drop} ...", flush=True)
        try:
            read = read_drop_list(drop, index, rows_by_file)
        except FindingsError as exc:
            raise FilterError(f"{_label(DROP_LIST)}: {exc}") from exc
        reads[DROP_LIST] = read
        problems.extend(read.problems)
        print(f"  {read.hit_findings:,} line(s) in the drop list.", flush=True)
    problems.extend(index.conflicts[:MAX_LISTED])
    if problems:
        raise FilterError(problems)
    return FilterPlan(root, output, rules, selection, runs, scanners, index, reads, drop_info)


# -------------------------------------------------------------------------
# Writing one run
# -------------------------------------------------------------------------


def _write_json_atomic(path: Path, value: Any) -> None:
    temp = path.with_name(path.name + PARTIAL_SUFFIX)
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)


def _reason_entries(reasons: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [
        {"reason": reason, "scanner": REASON_SCANNERS[reason], "findings": findings}
        for reason, findings in sorted(reasons.items())
    ]


def _filter_file(
    plan: FilterPlan,
    run: RunPlan,
    item: ScanFile,
    removed_handle: Any,
    temps: list[Path],
    finals: list[tuple[Path, Path]],
) -> FileResult:
    """Read one parent file once, in order. Kept lines are copied byte for byte
    (with a line-map entry each); removed lines go to `removed_rows.ndjson`."""
    in_run = run.in_run(item)
    parent_path = run.parent_dir / in_run
    result = FileResult(path=in_run, rows_in=0)
    kept_final = run.output_dir / in_run
    map_final = run.output_dir / result.map_path
    kept_temp = kept_final.with_name(kept_final.name + PARTIAL_SUFFIX)
    map_temp = map_final.with_name(map_final.name + PARTIAL_SUFFIX)
    for folder in (kept_final.parent, map_final.parent):
        folder.mkdir(parents=True, exist_ok=True)
    temps.extend([kept_temp, map_temp])
    finals.extend([(kept_temp, kept_final), (map_temp, map_final)])

    hits = plan.index.reasons_for(item.path)
    expected = plan.index.expected_for(item.path)
    parent_digest = hashlib.sha256()
    kept_digest = hashlib.sha256()
    file_json = json.dumps(in_run)
    offset = 0
    with parent_path.open("rb") as parent, kept_temp.open("wb") as kept, map_temp.open("wb") as line_map:
        for number, raw in enumerate(parent, 1):
            parent_digest.update(raw)
            if not raw.strip():
                raise FilterError(f"{parent_path} line {number} is blank, so it isn't a row.")
            try:
                check_line(parent_path, number, raw)
            except ValueError as exc:
                raise FilterError(str(exc)) from exc
            fingerprint = line_fingerprint(raw)
            said = expected.get(number)
            if said is not None and said[0] != fingerprint:
                if said[1] == DROP_LIST:
                    who, made = "the drop list says", "The drop list was made from"
                else:
                    who, made = f"the {LABELS[said[1]]} results say", "The results were made from"
                raise FilterError(
                    f"{item.path} line {number}: {who} its fingerprint is "
                    f"{said[0]}, but the line is {fingerprint}. {made} a "
                    "different version of this line."
                )
            reasons = hits.get(number)
            if reasons:
                try:
                    line_text = raw.rstrip(b"\r\n").decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise FilterError(f"{parent_path} line {number} isn't valid UTF-8.") from exc
                entry = {
                    "_parent": {
                        "file": in_run,
                        "line": number,
                        "byte_offset": offset,
                        "source_sha256": fingerprint,
                    },
                    "reasons": _reason_entries(reasons),
                    "line": line_text,
                }
                removed_handle.write((json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
                result.removed += 1
                result.removed_by_reason.update(reasons.keys())
            else:
                kept.write(raw)
                kept_digest.update(raw)
                line_map.write(
                    (
                        f'{{"_parent": {{"file": {file_json}, "line": {number}, '
                        f'"byte_offset": {offset}, "source_sha256": "{fingerprint}"}}}}\n'
                    ).encode("utf-8")
                )
                result.kept += 1
                result.kept_bytes += len(raw)
            offset += len(raw)
            result.rows_in = number

    if (result.rows_in, offset, parent_digest.hexdigest()) != (item.rows, item.size_bytes, item.sha256):
        raise FilterError(
            f"{parent_path} has {result.rows_in:,} rows, {offset:,} bytes and SHA-256 "
            f"{parent_digest.hexdigest()}, but its run wrote {item.rows:,} rows, {item.size_bytes:,} "
            f"bytes and SHA-256 {item.sha256}. It changed after the run finished."
        )
    result.kept_sha256 = kept_digest.hexdigest()
    return result


def _check_kept_file(output_dir: Path, result: FileResult) -> list[str]:
    """Read a kept file again next to its line map: line for line, each kept
    line's fingerprint is its map entry's, and the bytes are the ones written."""
    kept_path, map_path = output_dir / result.path, output_dir / result.map_path
    if result.rows_in != result.kept + result.removed:
        return [
            f"{result.path}: {result.rows_in:,} rows in, but {result.kept:,} kept plus "
            f"{result.removed:,} removed is {result.kept + result.removed:,}."
        ]
    digest = hashlib.sha256()
    size = lines = 0
    previous_line = 0
    with kept_path.open("rb") as kept, map_path.open("rb") as line_map:
        for raw, map_raw in zip_longest(kept, line_map):
            if raw is None or map_raw is None:
                return [
                    f"{result.path} and its line map {result.map_path} don't have the same "
                    f"number of lines (they differ after line {lines:,})."
                ]
            lines += 1
            digest.update(raw)
            size += len(raw)
            try:
                parent = json.loads(map_raw)["_parent"]
                source_line, source_sha256, source_file = (
                    parent["line"],
                    parent["source_sha256"],
                    parent["file"],
                )
            except (ValueError, KeyError, TypeError):
                return [f"{result.map_path} line {lines:,} isn't a line-map entry."]
            if source_file != result.path or not isinstance(source_line, int) or source_line <= previous_line:
                return [f"{result.map_path} line {lines:,} doesn't point at a later line of {result.path}."]
            if line_fingerprint(raw) != source_sha256:
                return [
                    f"{result.path} line {lines:,} has a different fingerprint from the line it "
                    f"came from ({result.path} line {source_line:,} of the parent)."
                ]
            previous_line = source_line
    problems = []
    if lines != result.kept:
        problems.append(f"{result.path} has {lines:,} lines on disk, but {result.kept:,} were kept.")
    if size != result.kept_bytes or kept_path.stat().st_size != result.kept_bytes:
        problems.append(f"{result.path} is not the {result.kept_bytes:,} bytes that were written.")
    if digest.hexdigest() != result.kept_sha256:
        problems.append(f"{result.path} on disk has a different SHA-256 from the bytes that were written.")
    return problems


def _check_removed_file(path: Path, results: list[FileResult]) -> list[str]:
    """Each entry's whole line has the fingerprint its `_parent` says, and the
    entries per file are the numbers removed."""
    seen: Counter = Counter()
    with path.open("rb") as handle:
        for number, raw in enumerate(handle, 1):
            try:
                entry = json.loads(raw)
                parent, line_text = entry["_parent"], entry["line"]
                file, fingerprint = parent["file"], parent["source_sha256"]
                has_reasons = bool(entry["reasons"])
            except (ValueError, KeyError, TypeError):
                return [f"{path.name} line {number:,} isn't a removed-row entry."]
            if not has_reasons:
                return [f"{path.name} line {number:,} has no reasons."]
            if line_fingerprint(line_text.encode("utf-8")) != fingerprint:
                return [
                    f"{path.name} line {number:,}: the saved line isn't the one with the fingerprint "
                    f"it records ({file} line {parent.get('line')})."
                ]
            seen[file] += 1
    expected = Counter({result.path: result.removed for result in results if result.removed})
    if seen != expected:
        return [
            f"{path.name} has {sum(seen.values()):,} entries, but {sum(expected.values()):,} rows "
            "were removed (or they are in different files)."
        ]
    return []


def _file_entry(output_dir: Path, relative: str) -> dict[str, Any]:
    path = output_dir / relative
    return {"path": relative, "bytes": path.stat().st_size, "sha256": file_sha256(path)}


def _filter_run(plan: FilterPlan, run: RunPlan) -> dict[str, Any]:
    """Write one filter run. Returns its summary; the run is finished only if
    `summary["checks"]["passed"]` (then `_SUCCESS` is there)."""
    output_dir = run.output_dir
    try:
        check_run_identity(
            output_dir, _identity(run, plan.input_root, plan.rules, plan.scanners, plan.drop_list)
        )
    except RunIdentityError as exc:
        raise FilterError(str(exc)) from exc
    # A marker or summary from an earlier try must not look like this run's.
    for name in (SUCCESS_FILENAME, SUMMARY_FILENAME):
        (output_dir / name).unlink(missing_ok=True)

    removed_final = output_dir / REMOVED_ROWS_FILENAME
    removed_temp = removed_final.with_name(removed_final.name + PARTIAL_SUFFIX)
    temps: list[Path] = [removed_temp]
    finals: list[tuple[Path, Path]] = [(removed_temp, removed_final)]
    results: list[FileResult] = []
    try:
        with removed_temp.open("wb") as removed_handle:
            for item in run.files:
                results.append(_filter_file(plan, run, item, removed_handle, temps, finals))
        for temp, final in finals:
            os.replace(temp, final)
    except BaseException:
        for temp in temps:
            temp.unlink(missing_ok=True)
        raise

    failures: list[str] = []
    for result in results:
        failures.extend(_check_kept_file(output_dir, result))
    failures.extend(_check_removed_file(removed_final, results))

    kept_total = sum(result.kept for result in results)
    removed_total = sum(result.removed for result in results)
    removed_by_reason: Counter = Counter()
    for result in results:
        removed_by_reason.update(result.removed_by_reason)
    files_written = [
        {
            "path": result.path,
            "rows": result.kept,
            "bytes": (output_dir / result.path).stat().st_size,
            "sha256": file_sha256(output_dir / result.path),
        }
        for result in results
    ]
    for entry, result in zip(files_written, results):
        if (entry["rows"], entry["bytes"], entry["sha256"]) != (
            result.kept,
            result.kept_bytes,
            result.kept_sha256,
        ):
            failures.append(f"{result.path} on disk isn't what was written.")
    warnings = []
    if kept_total == 0:
        warnings.append("Every row was removed, so this run has no rows.")

    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool": FILTER_ROWS_TOOL,
        "parent": {
            "run": run.run,
            "run_dir": str(run.parent_dir),
            "tool": run.parent_tool,
            "scan_root": str(plan.input_root),
            "files": len(run.files),
        },
        "scanner_results": plan.scanners,
        "rules": plan.rules.to_dict(),
        "source": {
            "rows_read": sum(result.rows_in for result in results),
            "files": [
                {"path": run.in_run(item), "rows": item.rows, "bytes": item.size_bytes, "sha256": item.sha256}
                for item in run.files
            ],
        },
        "rows": {"written": kept_total, "removed": removed_total},
        "removed_by_reason": dict(sorted(removed_by_reason.items())),
        "per_file": {
            result.path: {
                "rows_in": result.rows_in,
                "rows_kept": result.kept,
                "rows_removed": result.removed,
                "removed_by_reason": dict(sorted(result.removed_by_reason.items())),
            }
            for result in results
        },
        "output": {
            "files": len(files_written),
            "rows_on_disk": kept_total,
            "bytes_on_disk": sum(entry["bytes"] for entry in files_written),
            "files_written": files_written,
        },
        "removed_rows": {**_file_entry(output_dir, REMOVED_ROWS_FILENAME), "entries": removed_total},
        "line_map": {
            "folder": LINE_MAP_FOLDER,
            "files": [
                {**_file_entry(output_dir, result.map_path), "entries": result.kept} for result in results
            ],
        },
        "checks": {"passed": not failures, "failures": failures, "warnings": warnings},
    }
    if plan.drop_list is not None:
        # Only when given: a run without a drop list keeps the summary it always had.
        summary["drop_list"] = {**plan.drop_list, "entries": plan.reads[DROP_LIST].hit_findings}
    _write_json_atomic(output_dir / SUMMARY_FILENAME, summary)
    if summary["checks"]["passed"]:
        (output_dir / SUCCESS_FILENAME).touch()
    return summary


def filter_runs(
    input_root: str | Path,
    output_root: str | Path,
    results_dirs: dict[str, str | Path | None],
    rules: FilterRules | None = None,
    drop_list: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Filter every run under `input_root` into `output_root`.

    `results_dirs` is ``{scanner: results folder or None}`` with the scanners
    named as in ``modelsafety/results/coverage.py`` (regex_pii, ner, toxicity,
    dedup). `drop_list` is a file of rows to remove too (see `rules.read_drop_list`);
    either that or a scanner's results is needed. Returns each run's `summary.json`
    as a dict. It stops after the first run whose checks failed, which is the last
    item. Raises `FilterError` when it refuses to start or can't write a run.
    """
    plan = check_filter_inputs(input_root, output_root, results_dirs, rules or FilterRules(), drop_list)
    summaries = []
    for run in plan.runs:
        print(f"Filtering {run.run} ({len(run.files):,} files) ...", flush=True)
        summary = _filter_run(plan, run)
        summaries.append(summary)
        print(_describe(run, summary), flush=True)
        if not summary["checks"]["passed"]:
            break
    return summaries


# -------------------------------------------------------------------------
# Command line
# -------------------------------------------------------------------------


def _describe(run: RunPlan, summary: dict[str, Any]) -> str:
    rows_in = summary["source"]["rows_read"]
    lines = [
        f"  {run.run}: {rows_in:,} rows in, {summary['rows']['written']:,} kept, "
        f"{summary['rows']['removed']:,} removed."
    ]
    for reason, count in summary["removed_by_reason"].items():
        lines.append(f"    {reason}: {count:,}")
    for warning in summary["checks"]["warnings"]:
        lines.append(f"    WARNING: {warning}")
    for failure in summary["checks"]["failures"]:
        lines.append(f"    FAILED: {failure}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input-root", type=Path, required=True, help="Folder of finished runs.")
    parser.add_argument("--output-root", type=Path, required=True, help="Where the filter runs are written.")
    for name in SCANNERS:
        parser.add_argument(
            RESULTS_OPTIONS[name],
            type=Path,
            help=f"{LABELS[name]} results made on the input folder. At least one scanner, or --drop-list, is required.",
        )
    parser.add_argument(
        "--drop-list",
        type=Path,
        help="A JSONL file of rows to remove, each with its file, line, fingerprint and the scanner it "
        "came from. They are removed with the reason 'manual'. Can be used alone.",
    )
    parser.add_argument(
        "--regex-ignore-category",
        action="append",
        choices=PII_CATEGORIES,
        help="Regex PII category that doesn't remove a row. Repeatable.",
    )
    parser.add_argument(
        "--ner-ignore-label",
        action="append",
        help="NER label that doesn't remove a row. Repeatable.",
    )
    parser.add_argument(
        "--toxicity-remove-verdict",
        action="append",
        choices=TOXICITY_REMOVABLE_VERDICTS,
        help="Guard verdict that removes a row. Repeatable. Default: Unsafe and Controversial.",
    )
    parser.add_argument(
        "--toxicity-guard-errors",
        choices=GUARD_ERROR_CHOICES,
        default="remove",
        help="remove (default): a guard error counts as unsafe. refuse: don't start while any exist.",
    )
    parser.add_argument(
        "--dedup-mode",
        action="append",
        choices=DEDUP_MODES,
        help="Duplicates mode the results may be in. Repeatable. Default: global and cross.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rules = FilterRules(
        regex_ignore_categories=tuple(args.regex_ignore_category or ()),
        ner_ignore_labels=tuple(args.ner_ignore_label or ()),
        toxicity_remove_verdicts=tuple(args.toxicity_remove_verdict or TOXICITY_REMOVABLE_VERDICTS),
        toxicity_guard_errors=args.toxicity_guard_errors,
        dedup_modes=tuple(args.dedup_mode or DEDUP_MODES),
    )
    results_dirs = {
        "regex_pii": args.pii_run_dir,
        "ner": args.ner_run_dir,
        "toxicity": args.toxicity_run_dir,
        "dedup": args.dedup_run_dir,
    }
    try:
        summaries = filter_runs(args.input_root, args.output_root, results_dirs, rules, args.drop_list)
    except FilterError as exc:
        print("error:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    failed = [summary for summary in summaries if not summary["checks"]["passed"]]
    if failed:
        print(f"Stopped at the first run that failed ({failed[0]['parent']['run']}).", file=sys.stderr)
        return 2
    print(f"{len(summaries):,} run(s) written, all passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
