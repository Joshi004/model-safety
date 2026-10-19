#!/usr/bin/env python3
"""Check that every scan read exactly the files its runs wrote, read every row, and finished.

The folder to check (``--root``) holds runs: output folders of ``prepare_rows.py``
or ``filter_rows.py`` at any depth. Each finished run says which files it wrote
and how many rows each holds. For every scanner whose results folder you give, this report
checks, read-only:

  - the scan was made on this folder, by the right kind of scanner;
  - every stage, slice and piece of work finished (finished markers);
  - the files it read are exactly the files the finished runs wrote, by SHA-256;
  - for each file, rows read equal the run's rows, and rows with text plus
    rows without text equal rows read;
  - toxicity stage 2 judged every message stage 1 flagged.

A scanner whose results folder isn't given is reported as "not run", never as
passed. Unfinished runs under the folder, and stray ``.jsonl`` files, are
listed as not checked and don't fail the report. Nothing is changed or deleted,
and nothing is scanned.

The checks are functions (``check_scanner``, ``build_report``) so the filter
step can call them. ``--json-out`` writes the same verdicts as a JSON report
(``REPORT_VERSION``).

A scan that was given a list of files (a duplicates scan over several datasets) is checked
against exactly that list with ``--files-from``, instead of every run under the folder.

Usage:
    python3 modelsafety/results/coverage.py --root output/prepared \\
        [--pii-run-dir DIR] [--ner-run-dir DIR] \\
        [--toxicity-run-dir DIR] [--dedup-run-dir DIR] \\
        [--files-from FILE] [--json-out FILE]

Exit code: 0 if every scanner that ran passed, 2 if anything failed, if no
scanner ran, or if nothing under the folder could be checked.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

# Run as a script, so only this folder is on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modelsafety.contract.fingerprints import compare_files  # noqa: E402
from modelsafety.contract.project_paths import from_saved, to_saved  # noqa: E402
from modelsafety.contract.run_finder import (  # noqa: E402
    SCAN_INPUTS_FILENAME,
    SUCCESS_FILENAME,
    RunFinderError,
    ScanSelection,
    read_json_or_none,
    select_scan_files,
)
from modelsafety.contract.run_identity import IDENTITY_FILENAME  # noqa: E402
from modelsafety.data_screening.pii.model.common import load_work_units  # noqa: E402
from modelsafety.data_screening.pii.regex.pii_output import (  # noqa: E402
    INPUT_FILES_FILENAME,
    STAGE1_NAME,
    STAGE2_NAME,
    STAGE3_NAME,
)
from modelsafety.data_screening.toxicity.check_guard_counts import (  # noqa: E402
    RECOGNISED_VERDICTS,
    check_guard_counts,
)

REPORT_VERSION = 1

SCANNERS = ("regex_pii", "ner", "toxicity", "dedup")
LABELS = {
    "regex_pii": "regex PII",
    "ner": "NER",
    "toxicity": "toxicity",
    "dedup": "duplicates",
}
# The `tool` each scanner writes in its run_identity.json.
TOOLS = {"regex_pii": "pii_regex", "ner": "pii_model", "toxicity": "toxicity", "dedup": "dedup"}

# The four numbers every scanner records for each file it read.
COUNT_KEYS = ("rows_read", "rows_with_text", "rows_without_text", "findings")

# A part (stage, slice or piece of work) is finished, didn't finish, hasn't
# run at all, or finished but its own check failed.
FINISHED = "finished"
NOT_FINISHED = "not_finished"
NOT_RUN = "not_run"
FAILED = "failed"

TOXICITY_SHARDS = Path("scans") / "toxicity" / "shards"
DETOXIFY_SUCCESS_FILENAME = "_DETOXIFY_SUCCESS"

EXAMPLES_SHOWN = 3
PRINT_LIMIT = 10


# -------------------------------------------------------------------------
# What a loader returns
# -------------------------------------------------------------------------


@dataclass
class Part:
    """One thing that has to finish: a stage, a slice or the pieces of work.

    `files` are the Prepared files it covers (paths under the scanned
    folder), so a problem is reported against the runs it affects.
    """

    name: str
    status: str
    reason: str | None = None
    files: list[str] = field(default_factory=list)


@dataclass
class ScanRecord:
    """What one scanner's results folder says about the scan."""

    scanner: str
    results_dir: Path
    scan_root: str | None = None
    input_mode: str | None = None
    parts: list[Part] = field(default_factory=list)
    # {path: sha256} of the files the scan read, and {path: counts} for them.
    # None when the results don't say (the scan, or its record, didn't finish).
    files: dict[str, str] | None = None
    per_file: dict[str, dict[str, int]] | None = None
    # Files covered by a slice or piece of work that didn't finish. They are
    # reported under that part, never as "missing".
    not_finished_files: set[str] = field(default_factory=set)
    skipped_by_scan: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    problems: list[dict[str, Any]] = field(default_factory=list)


def make_problem(
    scanner: str | None, message: str, run: str | None = None, path: str | None = None
) -> dict[str, Any]:
    """`run` is None for a problem with the scan as a whole."""
    return {"scanner": scanner, "run": run, "path": path, "message": message}


# -------------------------------------------------------------------------
# Reading results folders (read-only)
# -------------------------------------------------------------------------


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _files(count: int) -> str:
    return f"{count:,} file" if count == 1 else f"{count:,} files"


def _examples(paths: list[str]) -> str:
    shown = ", ".join(paths[:EXAMPLES_SHOWN])
    extra = len(paths) - EXAMPLES_SHOWN
    return f"{shown} and {extra:,} more" if extra > 0 else shown


def _shown(record: ScanRecord, path: Path) -> str:
    try:
        return path.relative_to(record.results_dir).as_posix()
    except ValueError:
        return str(path)


def _load_json(record: ScanRecord, path: Path) -> dict[str, Any] | None:
    """The JSON object in `path`, or None when the file is missing. A file
    that is there but isn't a JSON object adds a problem."""
    if not path.is_file():
        return None
    value = read_json_or_none(path)
    if not isinstance(value, dict):
        record.problems.append(
            make_problem(record.scanner, f"{_shown(record, path)} can't be read as a JSON object.")
        )
        return None
    return value


def files_in_block(block: Any) -> dict[str, str] | None:
    """{path: sha256} from a scan's `input` block, or None if it has no usable file list."""
    if not isinstance(block, dict) or not isinstance(block.get("files"), list):
        return None
    try:
        return {item["path"]: item["sha256"] for item in block["files"]}
    except (KeyError, TypeError):
        return None


def _counts(entry: Any) -> dict[str, int]:
    """The four per-file counts that are whole numbers in `entry`."""
    if not isinstance(entry, dict):
        return {}
    return {key: entry[key] for key in COUNT_KEYS if _is_count(entry.get(key))}


def _read_identity(record: ScanRecord) -> dict[str, Any] | None:
    """Check `run_identity.json` names the right scanner. The folder it
    recorded is the scan root until a summary says otherwise."""
    path = record.results_dir / IDENTITY_FILENAME
    identity = _load_json(record, path)
    if identity is None:
        if not path.is_file():
            record.problems.append(
                make_problem(
                    record.scanner,
                    f"There is no {IDENTITY_FILENAME}, so nothing shows which scan made these results.",
                )
            )
        return None
    expected = TOOLS[record.scanner]
    if identity.get("tool") != expected:
        record.problems.append(
            make_problem(
                record.scanner,
                f"{IDENTITY_FILENAME} says this folder holds {identity.get('tool')!r} results, "
                f"not {LABELS[record.scanner]} ({expected!r}).",
            )
        )
    if isinstance(identity.get("input_root"), str):
        record.scan_root = identity["input_root"]
    return identity


def _read_scan_inputs(
    record: ScanRecord, path: Path, identity: dict[str, Any] | None
) -> None:
    """What the scan skipped and which input mode it used."""
    scan_inputs = _load_json(record, path)
    mode = None
    if scan_inputs is not None:
        skipped = scan_inputs.get("skipped")
        record.skipped_by_scan = skipped if isinstance(skipped, list) else []
        mode = scan_inputs.get("mode")
    if not isinstance(mode, str) and identity is not None:
        settings = identity.get("settings")
        mode = settings.get("input_mode") if isinstance(settings, dict) else None
    record.input_mode = mode if isinstance(mode, str) else None


def _set_files(record: ScanRecord, block: Any, where: str) -> bool:
    """Take the files the scan read from its `input` block. False, with a
    problem, when the block has no usable list."""
    files = files_in_block(block)
    if files is None:
        record.problems.append(
            make_problem(
                record.scanner,
                f"{where} is missing or has no list of the files the scan read "
                "(it was made before scans recorded them, or was recovered from logs).",
            )
        )
        return False
    record.files = files
    if isinstance(block.get("root"), str):
        record.scan_root = block["root"]
    return True


def _marker_part(name: str, folder: Path) -> Part:
    """Finished when `folder` has its `_SUCCESS`."""
    label = name.capitalize()
    if not folder.is_dir():
        return Part(name, NOT_RUN, f"{label} hasn't run: there is no {folder.name} folder.")
    if not (folder / SUCCESS_FILENAME).is_file():
        return Part(
            name, NOT_FINISHED, f"{label} didn't finish: {folder.name} has no {SUCCESS_FILENAME}."
        )
    return Part(name, FINISHED)


def load_regex_pii(results_dir: Path) -> ScanRecord:
    """Regex PII: `stage1_scan`, `stage2_validated` and `stage3_extract`, each
    with its own `_SUCCESS`. Stage 1 records the files and counts."""
    record = ScanRecord("regex_pii", results_dir)
    identity = _read_identity(record)
    for number, folder_name in enumerate((STAGE1_NAME, STAGE2_NAME, STAGE3_NAME), 1):
        record.parts.append(_marker_part(f"stage {number}", results_dir / folder_name))

    stage1 = results_dir / STAGE1_NAME
    _read_scan_inputs(record, stage1 / SCAN_INPUTS_FILENAME, identity)
    if record.parts[0].status == FINISHED:
        block = _load_json(record, stage1 / INPUT_FILES_FILENAME)
        if _set_files(record, block, f"{STAGE1_NAME}/{INPUT_FILES_FILENAME}"):
            record.per_file = {entry["path"]: _counts(entry) for entry in block["files"]}
    return record


def load_ner(results_dir: Path) -> ScanRecord:
    """NER: one piece of work per line of `manifest/work_units.jsonl`, each
    with `units/<id>/_SUCCESS`, then an aggregate `summary.json` and `_SUCCESS`."""
    record = ScanRecord("ner", results_dir)
    identity = _read_identity(record)
    manifest_dir = results_dir / "manifest"
    _read_scan_inputs(record, manifest_dir / SCAN_INPUTS_FILENAME, identity)

    units = None
    units_path = manifest_dir / "work_units.jsonl"
    if not units_path.is_file():
        record.parts.append(
            Part(
                "pieces of work",
                NOT_RUN,
                "Pieces of work haven't been planned: there is no manifest/work_units.jsonl.",
            )
        )
    else:
        try:
            units = load_work_units(units_path)
        except ValueError as exc:
            record.parts.append(
                Part("pieces of work", FAILED, f"The pieces of work can't be listed: {exc}")
            )
    if units is not None:
        unfinished = [
            unit
            for unit in units
            if not (results_dir / "units" / unit.unit_id / SUCCESS_FILENAME).is_file()
        ]
        affected = sorted({unit.source_file for unit in unfinished})
        if unfinished:
            first = unfinished[0]
            record.parts.append(
                Part(
                    "pieces of work",
                    NOT_FINISHED,
                    f"{len(unfinished):,} of {len(units):,} pieces of work didn't finish, "
                    f"e.g. {first.unit_id} (in {first.source_file}).",
                    affected,
                )
            )
            record.not_finished_files.update(affected)
        else:
            record.parts.append(Part("pieces of work", FINISHED))
        record.details = {
            "pieces_of_work": len(units),
            "pieces_finished": len(units) - len(unfinished),
        }

    summary = _load_json(record, results_dir / "summary.json")
    if summary is None:
        record.parts.append(
            Part("aggregate", NOT_RUN, "Aggregate hasn't run: there is no summary.json.")
        )
        return record
    if not (results_dir / SUCCESS_FILENAME).is_file():
        missing = summary.get("missing_units")
        why = ""
        if isinstance(missing, list) and missing:
            why = f" It lists {len(missing):,} missing pieces of work."
        record.parts.append(
            Part(
                "aggregate",
                NOT_FINISHED,
                f"Aggregate didn't finish: there is no {SUCCESS_FILENAME}.{why}",
            )
        )
        return record

    expected_units = summary.get("expected_units")
    if units is not None and _is_count(expected_units) and expected_units != len(units):
        record.parts.append(
            Part(
                "aggregate",
                FAILED,
                f"Aggregate expected {expected_units:,} pieces of work, but "
                f"manifest/work_units.jsonl lists {len(units):,}.",
            )
        )
    else:
        record.parts.append(Part("aggregate", FINISHED))
    if _set_files(record, summary.get("input"), "summary.json"):
        sources = summary.get("sources")
        record.per_file = (
            {path: _counts(entry) for path, entry in sources.items()}
            if isinstance(sources, dict)
            else {}
        )
    return record


def _listed_files(path: Path) -> list[str]:
    """The relative paths in a slice's list of files (one per line)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip()]


def _toxicity_stage1(
    record: ScanRecord, name: str, shard_dir: Path, listed: list[str]
) -> dict[str, Any] | None:
    """Add the slice's stage 1 part. Returns its `detoxify_summary.json` when
    stage 1 finished. The files of an unfinished stage 1 go in
    `not_finished_files`."""
    part_name = f"{name} stage 1"
    label = f"Slice {name}, stage 1"
    summary = None
    if not shard_dir.is_dir():
        part = Part(
            part_name, NOT_RUN, f"{label} hasn't run: there is no {TOXICITY_SHARDS / name} folder.", listed
        )
    elif not (shard_dir / DETOXIFY_SUCCESS_FILENAME).is_file():
        part = Part(
            part_name,
            NOT_FINISHED,
            f"{label} didn't finish: there is no {DETOXIFY_SUCCESS_FILENAME}.",
            listed,
        )
    else:
        summary = _load_json(record, shard_dir / "detoxify_summary.json")
        if summary is None:
            part = Part(
                part_name,
                NOT_FINISHED,
                f"{label} didn't finish: there is no detoxify_summary.json.",
                listed,
            )
        else:
            part = Part(part_name, FINISHED)
    record.parts.append(part)
    if part.status != FINISHED:
        record.not_finished_files.update(listed)
        return None
    return summary


def _toxicity_stage2(name: str, shard_dir: Path, files: list[str]) -> Part:
    """The slice's stage 2 part: finished only when its `_SUCCESS` is there
    and stage 2 judged everything stage 1 flagged."""
    part_name = f"{name} stage 2"
    label = f"Slice {name}, stage 2"
    if not (shard_dir / "guard_results").is_dir():
        return Part(part_name, NOT_RUN, f"{label} hasn't run: there is no guard_results folder.", files)
    if not (shard_dir / SUCCESS_FILENAME).is_file():
        return Part(
            part_name, NOT_FINISHED, f"{label} didn't finish: there is no {SUCCESS_FILENAME}.", files
        )
    try:
        problems = check_guard_counts(
            shard_dir / "detoxify_summary.json",
            shard_dir / "flagged.jsonl",
            shard_dir / "guard_results" / "summary.json",
        )
    except ValueError as exc:
        return Part(part_name, FAILED, f"{label} can't be checked against stage 1: {exc}", files)
    if problems:
        return Part(
            part_name,
            FAILED,
            f"{label} doesn't match stage 1: {'; '.join(problems)}",
            files,
        )
    return Part(part_name, FINISHED)


def load_toxicity(results_dir: Path) -> ScanRecord:
    """Toxicity: the slices listed in `manifests/manifest.json`, each with a
    stage 1 (`_DETOXIFY_SUCCESS`, `detoxify_summary.json`) and a stage 2
    (`_SUCCESS`, `guard_results/summary.json`).

    A slice whose stage 1 finished counts its files and rows, even if its
    stage 2 didn't finish. The stage 2 part says so.
    """
    record = ScanRecord("toxicity", results_dir)
    identity = _read_identity(record)
    manifests = results_dir / "manifests"
    _read_scan_inputs(record, manifests / SCAN_INPUTS_FILENAME, identity)
    record.parts.append(_marker_part("manifest", manifests))

    manifest = _load_json(record, manifests / "manifest.json")
    if manifest is None:
        return record
    shards = manifest.get("shards")
    if not isinstance(shards, list):
        record.problems.append(
            make_problem("toxicity", "manifests/manifest.json doesn't list the slices (shards).")
        )
        return record
    if isinstance(manifest.get("input_root"), str):
        record.scan_root = manifest["input_root"]
    manifest_root = record.scan_root

    read_by: dict[str, str] = {}
    totals = {"flagged": 0, "judged": 0, "guard_errors": 0, "unknown": 0}
    slices_finished = 0
    for shard in shards:
        index = shard.get("index") if isinstance(shard, dict) else None
        if not _is_count(index):
            record.problems.append(
                make_problem("toxicity", "manifests/manifest.json has a slice with no index.")
            )
            continue
        name = f"shard_{index:05d}"
        shard_dir = results_dir / TOXICITY_SHARDS / name
        listed = _listed_files(manifests / "shards" / f"{name}.txt")

        stage1 = _toxicity_stage1(record, name, shard_dir, listed)
        if stage1 is None:
            continue
        block = stage1.get("input")
        files = files_in_block(block)
        if files is None:
            record.problems.append(
                make_problem(
                    "toxicity",
                    f"{name}/detoxify_summary.json has no list of the files it read "
                    "(it was recovered from logs, or made before scans recorded them).",
                )
            )
            record.not_finished_files.update(listed)
            continue
        slice_root = block.get("root")
        if manifest_root is not None and isinstance(slice_root, str):
            if from_saved(slice_root).resolve() != from_saved(manifest_root).resolve():
                record.problems.append(
                    make_problem(
                        "toxicity",
                        f"{name} scanned {slice_root}, but the manifest says {manifest_root}.",
                    )
                )

        if record.files is None:
            record.files, record.per_file = {}, {}
        per_file = stage1.get("per_file")
        for path, sha256 in files.items():
            if path in read_by:
                record.problems.append(
                    make_problem(
                        "toxicity",
                        f"{path} was read by two slices ({read_by[path]} and {name}).",
                        path=path,
                    )
                )
                continue
            read_by[path] = name
            record.files[path] = sha256
            record.per_file[path] = _counts(per_file.get(path)) if isinstance(per_file, dict) else {}

        stage2 = _toxicity_stage2(name, shard_dir, list(files))
        record.parts.append(stage2)
        if stage2.status == FINISHED:
            slices_finished += 1
            guard = read_json_or_none(shard_dir / "guard_results" / "summary.json")
            safety = guard.get("safety") if isinstance(guard, dict) else None
            if isinstance(guard, dict) and isinstance(safety, dict):
                totals["flagged"] += stage1.get("messages_flagged", 0)
                totals["judged"] += sum(int(safety.get(v, 0)) for v in RECOGNISED_VERDICTS)
                totals["guard_errors"] += guard.get("errors", 0)
                totals["unknown"] += guard.get("unknown", 0)

    record.details = {"slices": len(shards), "slices_finished": slices_finished, **totals}
    return record


def load_dedup(results_dir: Path) -> ScanRecord:
    """Duplicates: `summary.json`, then `_SUCCESS`. In cross mode the counts
    are under `candidate`, and only the candidate side is read as runs."""
    record = ScanRecord("dedup", results_dir)
    identity = _read_identity(record)
    _read_scan_inputs(record, results_dir / SCAN_INPUTS_FILENAME, identity)

    summary_path = results_dir / "summary.json"
    summary = _load_json(record, summary_path)
    if summary is None:
        if summary_path.is_file():
            record.parts.append(
                Part("scan", NOT_FINISHED, "The scan didn't finish: summary.json can't be read.")
            )
        else:
            record.parts.append(Part("scan", NOT_RUN, "The scan hasn't run: there is no summary.json."))
        return record
    if not (results_dir / SUCCESS_FILENAME).is_file():
        record.parts.append(
            Part("scan", NOT_FINISHED, f"The scan didn't finish: there is no {SUCCESS_FILENAME}.")
        )
        return record
    record.parts.append(Part("scan", FINISHED))

    cross = summary.get("mode") == "cross"
    counted = summary.get("candidate") if cross else summary
    counted = counted if isinstance(counted, dict) else {}
    if _set_files(record, summary.get("input"), "summary.json"):
        per_file = counted.get("per_file")
        record.per_file = (
            {path: _counts(entry) for path, entry in per_file.items()}
            if isinstance(per_file, dict)
            else {}
        )
    record.details = {"mode": summary.get("mode"), "duplicate_rows": counted.get("duplicate_rows")}
    reference = summary.get("reference")
    if cross and isinstance(reference, dict):
        record.details["reference"] = {
            "input_root": reference.get("input_root"),
            "files": reference.get("files"),
            "rows_total": reference.get("rows_total"),
            "note": "Read as a plain folder, so it isn't checked against runs.",
        }
    return record


LOADERS: dict[str, Callable[[Path], ScanRecord]] = {
    "regex_pii": load_regex_pii,
    "ner": load_ner,
    "toxicity": load_toxicity,
    "dedup": load_dedup,
}


# -------------------------------------------------------------------------
# Checks. Each returns a list of problems; none changes anything.
# -------------------------------------------------------------------------


def _run_lookup(selection: ScanSelection) -> dict[str, str | None]:
    return {item.path: item.run for item in selection.files}


def _owner_run(path: str, run_names: set[str | None]) -> str | None:
    """The finished run whose folder holds `path`, or None."""
    for parent in PurePosixPath(path).parents:
        if parent.as_posix() in run_names:
            return parent.as_posix()
    return None


def _group(paths: list[str], owner: Callable[[str], str | None]) -> dict[str | None, list[str]]:
    groups: dict[str | None, list[str]] = {}
    for path in paths:
        groups.setdefault(owner(path), []).append(path)
    return groups


def check_scan_root(record: ScanRecord, selection: ScanSelection) -> list[dict[str, Any]]:
    """The scan was made on the folder being checked."""
    if record.scan_root is None:
        if all(part.status == FINISHED for part in record.parts):
            return [make_problem(record.scanner, "The results don't record which folder was scanned.")]
        return []
    # The results record the root relative to the project root when it is inside it.
    if from_saved(record.scan_root).resolve() != Path(selection.scan_root).resolve():
        return [
            make_problem(
                record.scanner,
                f"This scan was made on {record.scan_root}, not on {selection.scan_root}.",
            )
        ]
    return []


def check_parts(record: ScanRecord, selection: ScanSelection) -> list[dict[str, Any]]:
    """Every stage, slice and piece of work finished. A part that covers
    files is reported against each run those files belong to."""
    run_of = _run_lookup(selection)
    problems = []
    for part in record.parts:
        if part.status == FINISHED:
            continue
        runs = {run_of.get(path) for path in part.files} or {None}
        for run in sorted(runs, key=lambda item: (item is None, item or "")):
            problems.append(make_problem(record.scanner, part.reason or part.name, run=run))
    return problems


def file_differences(record: ScanRecord, selection: ScanSelection) -> dict[str, list[str]] | None:
    """Files the scan didn't read, read but shouldn't have, or read with a
    different SHA-256. Files of an unfinished part are left out. None when the
    results don't list the files."""
    if record.files is None:
        return None
    skip = record.not_finished_files
    expected = {item.path: item.sha256 for item in selection.files if item.path not in skip}
    actual = {path: sha256 for path, sha256 in record.files.items() if path not in skip}
    return compare_files(expected, actual)


def check_files(record: ScanRecord, selection: ScanSelection) -> list[dict[str, Any]]:
    """The scan read exactly the files the finished runs wrote."""
    differences = file_differences(record, selection)
    if differences is None:
        return []
    run_of = _run_lookup(selection)
    run_names = set(run_of.values())
    problems = []
    for kind, template in (
        ("missing", "Didn't read {files} the run wrote, e.g. {examples}."),
        ("different", "The SHA-256 of {files} differs from what the run wrote, e.g. {examples}."),
    ):
        for run, paths in _group(differences[kind], run_of.get).items():
            message = template.format(files=_files(len(paths)), examples=_examples(paths))
            problems.append(make_problem(record.scanner, message, run=run, path=paths[0]))
    for run, paths in _group(differences["extra"], lambda path: _owner_run(path, run_names)).items():
        message = f"Read {_files(len(paths))} that no finished run lists, e.g. {_examples(paths)}."
        if record.input_mode == "folder":
            message += " This scan was made in folder mode, which reads every .jsonl under the folder."
        problems.append(make_problem(record.scanner, message, run=run, path=paths[0]))
    return problems


def check_rows(record: ScanRecord, selection: ScanSelection) -> list[dict[str, Any]]:
    """For each file the scan read: rows read equal the run's rows, and rows
    with text plus rows without text equal rows read."""
    if record.files is None or record.per_file is None:
        return []
    problems = []
    without_counts: dict[str | None, list[str]] = {}
    for item in selection.files:
        if item.path in record.not_finished_files or item.path not in record.files:
            continue
        counts = record.per_file.get(item.path, {})
        if any(key not in counts for key in COUNT_KEYS):
            without_counts.setdefault(item.run, []).append(item.path)
            continue
        read = counts["rows_read"]
        if read != item.rows:
            problems.append(
                make_problem(
                    record.scanner,
                    f"Read {read:,} rows of {item.path}, but the run wrote {item.rows:,}.",
                    run=item.run,
                    path=item.path,
                )
            )
        split = counts["rows_with_text"] + counts["rows_without_text"]
        if split != read:
            problems.append(
                make_problem(
                    record.scanner,
                    f"{item.path}: rows with text ({counts['rows_with_text']:,}) plus rows without "
                    f"text ({counts['rows_without_text']:,}) is {split:,}, not the {read:,} rows read.",
                    run=item.run,
                    path=item.path,
                )
            )
    for run, paths in without_counts.items():
        problems.append(
            make_problem(
                record.scanner,
                f"No row counts for {_files(len(paths))} in the scan's results "
                f"(it was made before scans recorded them), e.g. {_examples(paths)}.",
                run=run,
                path=paths[0],
            )
        )
    return problems


# -------------------------------------------------------------------------
# One scanner's section of the report
# -------------------------------------------------------------------------


def _part_dict(part: Part) -> dict[str, Any]:
    return {
        "name": part.name,
        "status": part.status,
        "reason": part.reason,
        "files_affected": len(part.files),
    }


def _sum_counts(record: ScanRecord, paths: list[str]) -> dict[str, int | None]:
    """Total of the four counts over `paths` that the scan read to the end."""
    if record.per_file is None:
        return {key: None for key in COUNT_KEYS}
    totals = {key: 0 for key in COUNT_KEYS}
    for path in paths:
        if path in record.not_finished_files:
            continue
        counts = record.per_file.get(path, {})
        for key in COUNT_KEYS:
            totals[key] += counts.get(key, 0)
    return totals


def _per_run(
    record: ScanRecord, selection: ScanSelection, problems: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Each run's verdict for this scanner. A problem with no run (a stage that
    didn't finish, a wrong scan root) fails every run."""
    by_run: dict[str, list[str]] = {}
    for item in selection.files:
        by_run.setdefault(item.run or "", []).append(item.path)
    scan_wide = any(problem["run"] is None for problem in problems)
    failing = {problem["run"] for problem in problems if problem["run"] is not None}
    result = {}
    for run, paths in sorted(by_run.items()):
        files_read = None
        if record.files is not None:
            files_read = sum(
                1 for path in paths if path in record.files and path not in record.not_finished_files
            )
        result[run] = {
            "status": "fail" if scan_wide or run in failing else "pass",
            "files_read": files_read,
            **_sum_counts(record, paths),
        }
    return result


def not_run_section(results_dir: Path | None, reason: str) -> dict[str, Any]:
    return {
        "status": "not_run",
        "results_dir": to_saved(results_dir) if results_dir is not None else None,
        "reason": reason,
    }


def check_scanner(
    name: str, results_dir: str | Path | None, selection: ScanSelection
) -> dict[str, Any]:
    """Check one scanner's results against the finished runs in `selection`.

    Returns the scanner's section of the report, plus `problems` (the full
    list) and `per_run` (each run's verdict), which `build_report` moves out.
    `status` is `pass`, `fail`, or `not_run` when no results folder was given
    or it doesn't exist. Read-only.
    """
    if name not in LOADERS:
        raise ValueError(f"Unknown scanner {name!r}; expected one of {', '.join(SCANNERS)}.")
    if results_dir is None:
        return not_run_section(None, "No results folder was given.")
    results_path = Path(results_dir).expanduser().resolve()
    if not results_path.is_dir():
        return not_run_section(results_path, f"The results folder doesn't exist: {results_path}")

    record = LOADERS[name](results_path)
    problems = [
        *record.problems,
        *check_scan_root(record, selection),
        *check_parts(record, selection),
        *check_files(record, selection),
        *check_rows(record, selection),
    ]

    differences = file_differences(record, selection)
    expected_paths = {item.path for item in selection.files}
    files_section = {
        "expected": len(selection.files),
        "read": (
            len(record.files.keys() - record.not_finished_files) if record.files is not None else None
        ),
        "missing": differences["missing"] if differences else [],
        "extra": differences["extra"] if differences else [],
        "different": differences["different"] if differences else [],
        "not_finished": sorted(record.not_finished_files & expected_paths),
    }
    read_paths = [
        item.path for item in selection.files if record.files is not None and item.path in record.files
    ]
    totals = _sum_counts(record, read_paths)
    return {
        "status": "fail" if problems else "pass",
        "results_dir": to_saved(results_path),
        "scan_root": record.scan_root,
        "input_mode": record.input_mode,
        "parts": [_part_dict(part) for part in record.parts],
        "files": files_section,
        "rows": {
            "read": totals["rows_read"],
            "with_text": totals["rows_with_text"],
            "without_text": totals["rows_without_text"],
            "findings": totals["findings"],
        },
        "skipped_by_scan": record.skipped_by_scan,
        "details": record.details,
        "problems": problems,
        "per_run": _per_run(record, selection, problems),
    }


# -------------------------------------------------------------------------
# Narrowing the report to the files a scan was given
# -------------------------------------------------------------------------


def read_file_list(path: Path) -> list[str]:
    """The relative paths in a scan's list of files (one per line, blanks skipped)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip()]


def narrow_selection(selection: ScanSelection, only_files: Iterable[str]) -> ScanSelection:
    """`selection` narrowed to `only_files`, the files a scan was given (relative to the scan
    root, as in the selection). A scan over a list of files reads only those, so it is checked
    against them and not against every run under the folder.

    What was skipped is narrowed too: only a stray file inside a chosen run still counts, and
    the number of runs found is the number of chosen runs. Raises RunFinderError when the
    list is empty or names a file that no finished run wrote."""
    wanted = set(only_files)
    if not wanted:
        raise RunFinderError("The scan's list of files is empty.")
    known = {item.path for item in selection.files}
    unknown = sorted(wanted - known)
    if unknown:
        raise RunFinderError(
            f"The scan's list of files names {_files(len(unknown))} that no finished run under "
            f"{selection.scan_root} wrote, e.g. {_examples(unknown)}."
        )
    files = [item for item in selection.files if item.path in wanted]
    chosen_runs = {item.run for item in files if item.run is not None}
    skipped = [
        entry
        for entry in selection.skipped
        if entry["kind"] == "file"
        and any(parent.as_posix() in chosen_runs for parent in PurePosixPath(entry["path"]).parents)
    ]
    return replace(selection, files=files, skipped=skipped, runs_found=len(chosen_runs))


# -------------------------------------------------------------------------
# The whole report
# -------------------------------------------------------------------------


def build_report(
    root: str | Path,
    results_dirs: dict[str, str | Path | None],
    only_files: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Check every scanner in `results_dirs` ({scanner: folder or None})
    against the finished runs under `root`, or only against `only_files` (paths
    relative to `root`) when a scan was given a list of files. See the module
    docstring for the JSON this returns."""
    root_path = Path(root).expanduser().resolve()
    report: dict[str, Any] = {
        "report": "coverage",
        "version": REPORT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "root": to_saved(root_path),
        "verdict": "fail",
        "scanners_checked": [],
        "scanners_not_run": [],
        "runs": {"found": None, "checked": None, "files": 0, "rows": 0, "not_checked": []},
        "per_run": [],
        "scanners": {},
        "problems": [],
    }
    try:
        selection = select_scan_files(root_path)
        if only_files is not None:
            selection = narrow_selection(selection, only_files)
    except RunFinderError as exc:
        report["scanners_not_run"] = [name for name in SCANNERS if results_dirs.get(name) is None]
        report["problems"].append(make_problem(None, str(exc)))
        return report

    runs_skipped = sum(1 for item in selection.skipped if item["kind"] == "run")
    report["runs"] = {
        "found": selection.runs_found,
        "checked": selection.runs_found - runs_skipped,
        "files": len(selection.files),
        "rows": sum(item.rows or 0 for item in selection.files),
        "not_checked": selection.skipped,
    }

    sections = {name: check_scanner(name, results_dirs.get(name), selection) for name in SCANNERS}
    report["scanners_checked"] = [name for name in SCANNERS if sections[name]["status"] != "not_run"]
    report["scanners_not_run"] = [name for name in SCANNERS if sections[name]["status"] == "not_run"]

    files_by_run: dict[str, list[Any]] = {}
    for item in selection.files:
        files_by_run.setdefault(item.run or "", []).append(item)
    for run, items in sorted(files_by_run.items()):
        scanners = {}
        for name in SCANNERS:
            section = sections[name]
            scanners[name] = {"status": "not_run"} if section["status"] == "not_run" else section["per_run"][run]
        report["per_run"].append(
            {
                "run": run,
                "files": len(items),
                "rows": sum(item.rows or 0 for item in items),
                "scanners": scanners,
            }
        )

    for name in SCANNERS:
        section = sections[name]
        report["problems"].extend(section.pop("problems", []))
        section.pop("per_run", None)
        report["scanners"][name] = section
    if not report["scanners_checked"]:
        report["problems"].append(
            make_problem(None, "No scanner results folder was given, so nothing was checked.")
        )
    report["verdict"] = (
        "pass"
        if report["scanners_checked"]
        and all(sections[name]["status"] == "pass" for name in report["scanners_checked"])
        else "fail"
    )
    return report


# -------------------------------------------------------------------------
# Printing and writing
# -------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    return "-" if value is None else f"{value:,}"


def _print_table(report: dict[str, Any]) -> None:
    cell_names = {"pass": "OK", "fail": "FAIL", "not_run": "not run"}
    header = ["run", "files", "rows", *(LABELS[name] for name in SCANNERS)]
    table = [
        [
            item["run"],
            _fmt(item["files"]),
            _fmt(item["rows"]),
            *(cell_names[item["scanners"][name]["status"]] for name in SCANNERS),
        ]
        for item in report["per_run"]
    ]
    widths = [max(len(row[column]) for row in [header, *table]) for column in range(len(header))]
    for row in [header, *table]:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def _print_scanner(name: str, section: dict[str, Any], problems: list[dict[str, Any]]) -> None:
    label = LABELS[name]
    if section["status"] == "not_run":
        print(f"\n=== {label}: NOT RUN ===")
        print(f"  {section['reason']}")
        return
    print(f"\n=== {label}: {section['status'].upper()} ===")
    print(f"  Results:    {section['results_dir']}")
    print(f"  Scanned:    {section['scan_root'] or '-'}  (input mode: {section['input_mode'] or '-'})")
    done = sum(1 for part in section["parts"] if part["status"] == FINISHED)
    print(f"  Finished:   {done} of {len(section['parts'])} parts")
    for part in section["parts"]:
        if part["status"] != FINISHED:
            print(f"    - {part['reason']}")
    files, rows = section["files"], section["rows"]
    print(f"  Files:      {_fmt(files['expected'])} expected, {_fmt(files['read'])} read")
    print(
        f"  Rows:       {_fmt(rows['read'])} read, {_fmt(rows['with_text'])} with text, "
        f"{_fmt(rows['without_text'])} without, {_fmt(rows['findings'])} findings"
    )
    if section["details"]:
        print("  Details:    " + ", ".join(f"{key}={value}" for key, value in section["details"].items()))
    skipped = section["skipped_by_scan"]
    if skipped:
        print(f"  The scan skipped {len(skipped):,} run(s) or file(s), listed in its scan_inputs.json.")
    if problems:
        print(f"  Problems ({len(problems):,}):")
        for problem in problems[:PRINT_LIMIT]:
            where = f"[{problem['run']}] " if problem["run"] else ""
            print(f"    - {where}{problem['message']}")
        if len(problems) > PRINT_LIMIT:
            print(f"    ... and {len(problems) - PRINT_LIMIT:,} more (use --json-out for the full list)")


def print_report(report: dict[str, Any]) -> None:
    print(f"Coverage report for {report['root']}")
    runs = report["runs"]
    if runs["found"] is not None:
        print(
            f"Runs: {runs['found']:,} found, {runs['checked']:,} checked "
            f"({runs['files']:,} files, {runs['rows']:,} rows), "
            f"{len(runs['not_checked']):,} not checked."
        )
        for item in runs["not_checked"][:PRINT_LIMIT]:
            status = f" ({item['status']})" if item["status"] else ""
            print(f"  Not checked: {item['kind']} {item['path']}{status}: {item['reason']}")
        if len(runs["not_checked"]) > PRINT_LIMIT:
            print(f"  ... and {len(runs['not_checked']) - PRINT_LIMIT:,} more not checked.")
    for problem in report["problems"]:
        if problem["scanner"] is None:
            print(f"Problem: {problem['message']}")

    if report["per_run"]:
        print("\n=== Per-run coverage ===")
        _print_table(report)
    for name, section in report["scanners"].items():
        problems = [problem for problem in report["problems"] if problem["scanner"] == name]
        _print_scanner(name, section, problems)

    print()
    checked = [LABELS[name] for name in report["scanners_checked"]]
    not_run = [LABELS[name] for name in report["scanners_not_run"]]
    suffix = f" Not run: {', '.join(not_run)}." if not_run and report["runs"]["found"] is not None else ""
    if report["verdict"] == "pass":
        print(f"PASS for {', '.join(checked)}.{suffix}")
        return
    failed = [LABELS[name] for name in report["scanners_checked"] if report["scanners"][name]["status"] == "fail"]
    if failed:
        print(f"FAIL: {', '.join(failed)} did not pass.{suffix}")
    else:
        print(f"FAIL: nothing could be checked.{suffix}")


def check_output_path(path: Path, protected: list[Path | None]) -> Path:
    """The resolved report path, unless it is inside a folder the report reads."""
    target = path.expanduser().resolve()
    for folder in protected:
        if folder is None:
            continue
        resolved = Path(folder).expanduser().resolve()
        if target == resolved or resolved in target.parents:
            raise ValueError(
                f"--json-out {target} is inside {resolved}. The report only reads that folder, "
                "so write the report somewhere else."
            )
    return target


def write_report(path: Path, report: dict[str, Any]) -> None:
    """Write the JSON report atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.partial")
    temp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--root", type=Path, required=True, help="The folder of runs that was scanned."
    )
    parser.add_argument("--pii-run-dir", type=Path, help="Regex PII results folder.")
    parser.add_argument("--ner-run-dir", type=Path, help="NER PII results folder.")
    parser.add_argument("--toxicity-run-dir", type=Path, help="Toxicity results folder.")
    parser.add_argument("--dedup-run-dir", type=Path, help="Duplicates results folder.")
    parser.add_argument(
        "--files-from",
        type=Path,
        help=(
            "Check against only the files listed here (one path per line, relative to --root), "
            "for a scan that was given a list of files. Without it, every finished run under "
            "--root is expected to have been read."
        ),
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help="Also write the report as JSON here. Not allowed inside --root or a results folder.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    results_dirs = {
        "regex_pii": args.pii_run_dir,
        "ner": args.ner_run_dir,
        "toxicity": args.toxicity_run_dir,
        "dedup": args.dedup_run_dir,
    }
    json_out = None
    if args.json_out is not None:
        try:
            json_out = check_output_path(args.json_out, [args.root, *results_dirs.values()])
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    only_files = None
    if args.files_from is not None:
        try:
            only_files = read_file_list(args.files_from)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"error: couldn't read {args.files_from}: {exc}", file=sys.stderr)
            return 2

    report = build_report(args.root, results_dirs, only_files)
    print_report(report)
    if json_out is not None:
        try:
            write_report(json_out, report)
        except OSError as exc:
            print(f"error: couldn't write {json_out}: {exc}", file=sys.stderr)
            return 2
        print(f"JSON report: {json_out}")
    return 0 if report["verdict"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
