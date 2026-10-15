"""Dropping rows: make a filtered version of a dataset from the flagged rows the user chose.

The user picks rows on a dataset's result pages: some rows, or "all rows matching the current
filters", from one scanner or several. `check` counts what that would drop, and `submit` makes it
happen:

    1. the dataset's current Prepared version is marked `kept` (so "convert again" can never
       delete the folder the filter job is about to read),
    2. a new dataset is registered in the same team with its `parent` set (D11),
    3. `<service root>/datasets/<new id>/drop_list.jsonl` is written: one line for each row and
       scanner, with the row's file, line and fingerprint,
    4. one CPU SLURM job runs `modelsafety/filter/filter_rows.py --drop-list`, which copies the
       kept rows byte for byte into `datasets/<new id>/prepared/<version id>/`,
    5. when the job has finished and its checks passed, that folder becomes the new dataset's
       Prepared version (`registry.set_prepared`), so it can be scanned like any other.

The parent dataset, its Prepared rows and its results are never changed (apart from the `kept`
mark in step 1).

Rows are resolved from each scan's findings index (flagged.py), so what is dropped is exactly
what the result page lists, under the same choices (a toxicity result's categories and verdicts,
or a filter by reason and name). Every row is looked up again when the request is submitted,
and the request is refused if a scan was replaced in the meantime.

Rules this module keeps:

* Nothing is ever written to or deleted from original data, the parent's Prepared version or the
  scans. What is written is inside the new dataset's own folder (`drop_list.jsonl`,
  `prepared/<version id>/` by the job, `_jobs/filter.json`, `_logs/`), plus the registry.
* The only thing deleted is the new dataset itself, through `registry.unregister`, when its
  folder couldn't be set up before any job was submitted.
* Every SLURM call goes through jobs.py: an argument list, `shell=False`, a 30 second timeout.
  The job states its partition, CPUs, memory and time limit. The command shown is the command
  that runs.
* Every id, scanner key, filter and row in a request is checked against a pattern, a fixed list
  or the index itself before it is used. Paths are built from ids, never taken from a request.
* Job state is never stored. It is read live from SLURM and from the files the job writes. The
  one thing saved from those files is the new dataset's `prepared`, set once by `refresh` when
  the job has finished and its checks passed. As in conversions.py, a job's files are only looked
  for once SLURM no longer lists the job (on this cluster's shared disk, asking for a file before
  it exists makes this machine remember "not there" for about a minute).
* One lock covers every submit, so two requests can't both resolve and register at once.
  `refresh` skips instead of waiting when the lock is taken.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, NamedTuple

import flagged
import jobs
import registry
import results
import scanners
import scans
from reader_imports import (
    DROP_LIST_REASON,
    FILTER_ROWS_TOOL,
    MAX_DROP_LIST_REASONS,
    REPO_ROOT,
    read_json_or_none,
)

FILTER_FILE = "filter.json"
DROP_LIST_FILE = "drop_list.jsonl"
PREPARED_FOLDER = "prepared"
JOBS_FOLDER = "_jobs"
LOGS_FOLDER = "_logs"
FILTER_SCRIPT = REPO_ROOT / "launchers" / "slurm" / "filter_rows.sbatch"
# What the job asks for (the partition is the default one), the same as the script's own
# `#SBATCH` lines. The filter keeps the rows to drop in memory.
FILTER_RESOURCES = {"cpus": 2, "mem": "32G", "time": "04:00:00"}

# Stand for the new dataset's id and its Prepared version's id in the command shown before a
# start: both are chosen when the drop is submitted, and the record then holds the exact command.
NEW_ID_PLACEHOLDER = "<new-id>"
VERSION_PLACEHOLDER = "<version-id>"

MAX_SELECTIONS = len(scanners.SCANNERS)
# Rows ticked one by one. "All rows matching the filters" has no such cap: the server counts them.
MAX_EXPLICIT_ROWS = 10_000
# A sanity limit on the drop list (rows times scanners), which is held in memory while it is made.
MAX_DROP_ENTRIES = 5_000_000
MAX_FILE_CHARS = flagged.MAX_FILE_CHARS
MAX_FILTER_CHARS = flagged.MAX_FILTER_CHARS
MAX_OUTPUT_CHARS = scans.MAX_OUTPUT_CHARS

SHA256 = re.compile(r"^[0-9a-f]{64}$")
REFRESH_SECONDS = 30
REQUEST_KEYS = {"version_id", "selections"}
SELECTION_KEYS = {"scanner", "scan_id", "rows", "all_matching", "categories", "verdicts"}

_LOCK = threading.Lock()


class FiltersError(results.ResultsError):
    """The request can't be carried out; the message says why."""


def _log(message: str) -> None:
    print(f"[filters] {message}", file=sys.stderr, flush=True)


# -------------------------------------------------------------------------
# Where things live, and the job record: <dataset folder>/_jobs/filter.json
# -------------------------------------------------------------------------


def _dataset_folder(dataset_id: Any) -> Path:
    if not isinstance(dataset_id, str) or registry.DATASET_ID.fullmatch(dataset_id) is None:
        raise FiltersError(f"'{dataset_id}' isn't a dataset id.")
    return registry.service_root() / "datasets" / dataset_id


def _version_folder(dataset_id: str, version_id: str) -> Path:
    if not isinstance(version_id, str) or jobs.BATCH_ID.fullmatch(version_id) is None:
        raise FiltersError(f"'{version_id}' isn't a version id.")
    return _dataset_folder(dataset_id) / PREPARED_FOLDER / version_id


def _record_path(dataset_id: str) -> Path:
    return _dataset_folder(dataset_id) / JOBS_FOLDER / FILTER_FILE


def _is_record(data: Any, dataset_id: str) -> bool:
    """Whether `data` is a filter record whose ids can safely go into a path or a command. A
    record is only a file, so none of this is taken on trust."""
    if not isinstance(data, dict) or data.get("dataset_id") != dataset_id:
        return False
    parent_id, version_id, job_id = data.get("parent_id"), data.get("version_id"), data.get("job_id")
    if not isinstance(parent_id, str) or registry.DATASET_ID.fullmatch(parent_id) is None:
        return False
    if not isinstance(version_id, str) or jobs.BATCH_ID.fullmatch(version_id) is None:
        return False
    if job_id is not None and not (isinstance(job_id, str) and scans.SLURM_JOB_ID.fullmatch(job_id)):
        return False
    return isinstance(data.get("drop"), dict) and isinstance(data["drop"].get("rows"), int)


def _read_record(dataset_id: str) -> dict[str, Any] | None:
    data = jobs._read_json(_record_path(dataset_id))
    return data if _is_record(data, dataset_id) else None


def _write_record(record: dict[str, Any]) -> None:
    jobs._write_json_atomic(_record_path(record["dataset_id"]), record)


# -------------------------------------------------------------------------
# The request
# -------------------------------------------------------------------------


class _Selection(NamedTuple):
    """The rows chosen from one scanner's result. `rows` is None for "all rows matching the
    filters" (`reason` and `name`, or for toxicity the categories and verdicts). `categories` and
    `verdicts` are the choice toxicity's page was showing, as the comma-separated text
    flagged.py takes (None: the scan's own)."""

    scanner: str
    scan_id: str
    rows: list[tuple[str, int]] | None
    reason: str | None
    name: str | None
    categories: str | None
    verdicts: str | None


def _optional_name(value: Any, label: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > MAX_FILTER_CHARS:
        raise FiltersError(f"The {label} filter must be a name of at most {MAX_FILTER_CHARS} characters.")
    return value


def _choice_text(value: Any, label: str) -> str | None:
    """A list of choices as the comma-separated text flagged.py takes. None stays None (the
    scan's own), and an empty list stays empty (none chosen)."""
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise FiltersError(f"The {label} must be a list of names.")
    return ",".join(value)


def _rows(value: Any, scanner: str) -> list[tuple[str, int]]:
    if not isinstance(value, list) or not value:
        raise FiltersError(f"No rows are chosen from the {scanner} result.")
    rows: list[tuple[str, int]] = []
    for row in value:
        file = row.get("file") if isinstance(row, dict) else None
        line = row.get("line") if isinstance(row, dict) else None
        if not isinstance(file, str) or not file or len(file) > MAX_FILE_CHARS:
            raise FiltersError(f"Every chosen row needs a file of at most {MAX_FILE_CHARS} characters.")
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            raise FiltersError("Every chosen row needs a line number of at least 1.")
        rows.append((file, line))
    return rows


def _validated(request: Any) -> tuple[str, list[_Selection]]:
    """`(version id, selections)` from a request, checked. Raises FiltersError saying what is
    wrong. Nothing is read from disk."""
    if not isinstance(request, dict):
        raise FiltersError("The request must be an object.")
    unknown = sorted(str(key) for key in set(request) - REQUEST_KEYS)
    if unknown:
        raise FiltersError(f"Unknown fields in the request: {', '.join(unknown)}.")
    version_id = request.get("version_id")
    if not isinstance(version_id, str) or jobs.BATCH_ID.fullmatch(version_id) is None:
        raise FiltersError("The request needs the version_id of the dataset's converted data.")
    raw = request.get("selections")
    if not isinstance(raw, list) or not raw:
        raise FiltersError("Choose rows from at least one result.")
    if len(raw) > MAX_SELECTIONS:
        raise FiltersError(f"At most {MAX_SELECTIONS} results can be chosen from at once.")

    selections: list[_Selection] = []
    seen: set[str] = set()
    explicit = 0
    for item in raw:
        if not isinstance(item, dict):
            raise FiltersError("Each selection must be an object.")
        unknown = sorted(str(key) for key in set(item) - SELECTION_KEYS)
        if unknown:
            raise FiltersError(f"Unknown fields in a selection: {', '.join(unknown)}.")
        scanner = results._scanner_key(item.get("scanner"))
        if scanner in seen:
            raise FiltersError(f"{scanners.SCANNERS[scanner].label} is chosen twice.")
        seen.add(scanner)
        scan_id = item.get("scan_id")
        if not isinstance(scan_id, str) or jobs.BATCH_ID.fullmatch(scan_id) is None:
            raise FiltersError(f"The {scanners.SCANNERS[scanner].label} selection needs the scan_id it was made from.")

        rows, everything = item.get("rows"), item.get("all_matching")
        if (rows is None) == (everything is None):
            raise FiltersError(
                f"The {scanners.SCANNERS[scanner].label} selection must have either 'rows' or 'all_matching'."
            )
        reason = name = None
        if rows is not None:
            checked_rows = _rows(rows, scanners.SCANNERS[scanner].label)
            explicit += len(checked_rows)
            if explicit > MAX_EXPLICIT_ROWS:
                raise FiltersError(
                    f"At most {MAX_EXPLICIT_ROWS:,} rows can be chosen one by one. "
                    'Use "all rows matching the filters" for more.'
                )
        else:
            checked_rows = None
            if not isinstance(everything, dict) or not set(everything) <= {"reason", "name"}:
                raise FiltersError("'all_matching' must be an object that may have a 'reason' and a 'name'.")
            reason, name = _optional_name(everything.get("reason"), "reason"), _optional_name(everything.get("name"), "name")
        selections.append(
            _Selection(
                scanner,
                scan_id,
                checked_rows,
                reason,
                name,
                _choice_text(item.get("categories"), "categories"),
                _choice_text(item.get("verdicts"), "verdicts"),
            )
        )
    return version_id, selections


# -------------------------------------------------------------------------
# Resolving the rows from the findings index
# -------------------------------------------------------------------------


class _Resolved(NamedTuple):
    # One drop-list line each, in file, line and scanner order:
    # (file, line, scanner, scan id, fingerprint, reasons).
    lines: list[tuple[str, int, str, str, str, tuple[str, ...]]]
    counts: dict[str, Any]


def _toxicity_reasons(atoms: list[str]) -> tuple[str, ...]:
    """The reasons filter_rows.py knows for the atoms of a toxicity row that count as flagged: an
    error is a guard error, "<verdict>|<category>" is the verdict."""
    reasons = {
        "toxicity_guard_error" if atom == "error" else f"toxicity_{atom.partition('|')[0].lower()}" for atom in atoms
    }
    return tuple(sorted(reasons))


def _drop_entry(entry: dict[str, Any], atoms: list[str] | None, where: str) -> tuple[str, int, str, tuple[str, ...]]:
    """`(file, line, fingerprint, reasons)` for one index entry, checked: the index is only a file."""
    file, line, sha256 = entry.get("file"), entry.get("line"), entry.get("sha256")
    if not isinstance(file, str) or not file or isinstance(line, bool) or not isinstance(line, int) or line < 1:
        raise FiltersError(f"{where} doesn't name a row (it has no file and line).")
    if not isinstance(sha256, str) or SHA256.fullmatch(sha256) is None:
        raise FiltersError(f"{where}: {file} line {line} has no line fingerprint, so it can't be dropped safely.")
    reasons = _toxicity_reasons(atoms) if atoms is not None else entry.get("reasons")
    if (
        not isinstance(reasons, (list, tuple))
        or not 1 <= len(reasons) <= MAX_DROP_LIST_REASONS
        or any(not isinstance(reason, str) or DROP_LIST_REASON.fullmatch(reason) is None for reason in reasons)
    ):
        raise FiltersError(f"{where}: {file} line {line} has reasons that can't be put in a drop list.")
    return file, line, sha256, tuple(reasons)


def _resolve_one(
    dataset_id: str, version_id: str, selection: _Selection
) -> list[tuple[str, int, str, tuple[str, ...]]]:
    """The rows of one scanner's result that the selection names, found in the dataset's findings
    index under the same choices the result page applies. Raises if the result isn't the one the
    rows were chosen from, or a chosen row isn't flagged (any more)."""
    label = scanners.SCANNERS[selection.scanner].label
    opened = flagged._open_index(dataset_id, selection.scanner)
    if opened.scan["id"] != selection.scan_id:
        raise FiltersError(
            f"The {label} result of this dataset changed since the rows were chosen (the scan "
            f"{selection.scan_id} was replaced by {opened.scan['id']}). Open the result page again and choose again."
        )
    scanned = (opened.scan.get("versions") or {}).get(dataset_id)
    prepared = opened.dataset.get("prepared")
    current = prepared.get("version_id") if isinstance(prepared, dict) else None
    if current != version_id or not isinstance(scanned, dict) or scanned.get("version_id") != version_id:
        raise FiltersError(
            f"The {label} result wasn't made from this dataset's current converted data ({version_id}). "
            "It may have been converted again. Open the result page again."
        )

    counted = flagged._toxicity_choice(opened, selection.categories, selection.verdicts)
    wanted_reason = wanted_name = None
    if selection.rows is None:
        if counted is not None:
            if selection.reason is not None or selection.name is not None:
                raise FiltersError("A toxicity result is filtered by its categories and verdicts, not by reason or name.")
        else:
            wanted_reason = flagged._filter("reason", selection.reason, opened.counts["by_reason"])
            wanted_name = flagged._filter("name", selection.name, opened.counts["by_name"])

    wanted = set(selection.rows) if selection.rows is not None else None
    found: list[tuple[str, int, str, tuple[str, ...]]] = []
    try:
        with opened.rows_path.open("rb") as handle:
            for number, raw in enumerate(handle, 1):
                if not raw.strip():
                    continue
                entry = flagged._parse(raw, opened.rows_path, number)
                place = (entry.get("file"), entry.get("line"))
                if wanted is not None and place not in wanted:
                    continue
                atoms = flagged._counted_atoms(entry, counted) if counted is not None else None
                if wanted is None:
                    if not flagged._matches(entry, wanted_reason, wanted_name, atoms):
                        continue
                elif atoms is not None and not atoms:
                    # Chosen under an earlier choice of categories and verdicts, and not flagged now.
                    continue
                found.append(_drop_entry(entry, atoms, f"{opened.rows_path.name} line {number}"))
                if wanted is not None:
                    wanted.discard(place)
                    if not wanted:
                        break
                if len(found) > MAX_DROP_ENTRIES:
                    raise FiltersError(f"More than {MAX_DROP_ENTRIES:,} rows match, which is more than a drop can hold.")
    except OSError as exc:
        raise FiltersError(f"The flagged-rows index can't be read: {exc}") from exc

    if wanted:
        example = sorted(wanted)[0]
        raise FiltersError(
            f"{len(wanted):,} of the rows chosen from the {label} result aren't flagged in it (any more) under "
            f"this choice, for example {example[0]} line {example[1]}. Open the result page again and choose again."
        )
    if not found:
        raise FiltersError(f"No rows of the {label} result match, so there is nothing to drop from it.")
    return found


def _resolve(dataset_id: str, version_id: str, selections: list[_Selection]) -> _Resolved:
    """Every selection's rows, put together. A row that several scanners flagged is one row to
    drop, listed once for each scanner."""
    fingerprints: dict[tuple[str, int], str] = {}
    lines: list[tuple[str, int, str, str, str, tuple[str, ...]]] = []
    by_scanner: list[dict[str, Any]] = []
    for selection in selections:
        found = _resolve_one(dataset_id, version_id, selection)
        by_reason: Counter = Counter()
        for file, line, sha256, reasons in found:
            if fingerprints.setdefault((file, line), sha256) != sha256:
                raise FiltersError(
                    f"The results disagree about {file} line {line}: two scans recorded different fingerprints "
                    "for it. Check the results again."
                )
            by_reason.update(reasons)
            lines.append((file, line, selection.scanner, selection.scan_id, sha256, reasons))
        if len(lines) > MAX_DROP_ENTRIES:
            raise FiltersError(f"More than {MAX_DROP_ENTRIES:,} rows match, which is more than a drop can hold.")
        by_scanner.append(
            {
                "scanner": selection.scanner,
                "label": scanners.SCANNERS[selection.scanner].label,
                "scan_id": selection.scan_id,
                "rows": len(found),
                "by_reason": [
                    {"reason": reason, "count": count} for reason, count in sorted(by_reason.items(), key=lambda item: (-item[1], item[0]))
                ],
            }
        )
    lines.sort(key=lambda item: (item[0], item[1], item[2]))
    rows = len(fingerprints)
    counts = {"rows": rows, "entries": len(lines), "overlap": len(lines) - rows, "by_scanner": by_scanner}
    return _Resolved(lines, counts)


# -------------------------------------------------------------------------
# The command
# -------------------------------------------------------------------------


def _command(parent_folder: Path, dataset_folder: Path, version_id: str, dataset_id: str, resources: dict[str, Any]) -> list[str]:
    """The `sbatch` call that runs the filter. Built the same way for the check (with
    placeholders for the ids) and for the real start."""
    scanners._check_export_values({"FILTER_REPO": str(REPO_ROOT)})
    logs = dataset_folder / LOGS_FOLDER
    return [
        "sbatch",
        "--parsable",
        f"--partition={resources['partition']}",
        f"--cpus-per-task={resources['cpus']}",
        f"--mem={resources['mem']}",
        f"--time={resources['time']}",
        f"--job-name=filter_{dataset_id}",
        f"--output={logs}/filter-%j.out",
        f"--error={logs}/filter-%j.err",
        f"--export=ALL,FILTER_REPO={REPO_ROOT}",
        str(FILTER_SCRIPT),
        "--input-root",
        str(parent_folder),
        "--output-root",
        str(dataset_folder / PREPARED_FOLDER / version_id),
        "--drop-list",
        str(dataset_folder / DROP_LIST_FILE),
    ]


def _resources() -> dict[str, Any]:
    """The job's resources, checked against SLURM (which raises SlurmUnavailable if it can't be asked)."""
    return jobs.validate_resources({"partition": jobs.DEFAULT_RESOURCES["partition"], **FILTER_RESOURCES})


# -------------------------------------------------------------------------
# Checking
# -------------------------------------------------------------------------


def _parent_version(dataset: dict[str, Any], version_id: str) -> Path:
    prepared = dataset.get("prepared")
    if not isinstance(prepared, dict):
        raise FiltersError(f"'{dataset['name']}' isn't converted, so no rows can be dropped from it.")
    if prepared.get("version_id") != version_id:
        raise FiltersError("The dataset was converted again since the rows were chosen. Open its results again.")
    try:
        return results._version_folder(dataset["id"], prepared)
    except results.ResultsError as exc:
        raise FiltersError(str(exc)) from exc


def check(dataset_id: str, request: Any) -> dict[str, Any]:
    """What dropping these rows would do: the rows, counted by scanner and reason, the name the
    new dataset would get, anything worth a warning, and the command. Read-only."""
    version_id, selections = _validated(request)
    dataset = registry.get_dataset(dataset_id)
    parent_folder = _parent_version(dataset, version_id)
    dropped = _resolve(dataset_id, version_id, selections)

    blocked: list[str] = []
    resources: dict[str, Any] | None = None
    command_text: str | None = None
    try:
        resources = _resources()
    except jobs.JobsError as exc:
        blocked.append(str(exc))
    except jobs.SlurmUnavailable as exc:
        blocked.append(str(exc))
    if resources is not None:
        folder = registry.service_root() / "datasets" / NEW_ID_PLACEHOLDER
        command_text = shlex.join(_command(parent_folder, folder, VERSION_PLACEHOLDER, NEW_ID_PLACEHOLDER, resources))

    warnings: list[str] = []
    for item in dropped.counts["by_scanner"]:
        result = (dataset.get("results") or {}).get(item["scanner"])
        if isinstance(result, dict) and result.get("coverage") == "fail":
            warnings.append(
                f"The coverage check of the {item['label']} result failed: the scan may not have read exactly this "
                "dataset's converted data. The rows it flagged can still be dropped."
            )
    rows_in = dataset["prepared"].get("rows")
    if isinstance(rows_in, int) and dropped.counts["rows"] >= rows_in:
        warnings.append("This drops every row of the dataset, so the new dataset would have no rows.")

    return {
        "dataset_id": dataset_id,
        "dataset_name": dataset["name"],
        "version_id": version_id,
        "rows_in_dataset": rows_in,
        "counts": dropped.counts,
        "new_name": registry.filtered_name(dataset_id, dropped.counts["rows"]),
        "warnings": warnings,
        "blocked": blocked,
        "can_start": not blocked,
        "resources": resources,
        "command_text": command_text,
    }


# -------------------------------------------------------------------------
# Submitting
# -------------------------------------------------------------------------


def _write_drop_list(path: Path, lines: list[tuple[str, int, str, str, str, tuple[str, ...]]], automatic: bool = False) -> None:
    """One line for each row and scanner. An automatic drop (D17) says so on every line, so the
    filter removes the rows with the reason `automatic` instead of `manual`."""
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        for file, line, scanner, scan_id, sha256, reasons in lines:
            entry: dict[str, Any] = {
                "file": file,
                "line": line,
                "sha256": sha256,
                "scanner": scanner,
                "scan_id": scan_id,
                "reasons": list(reasons),
            }
            if automatic:
                entry["automatic"] = True
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
    os.replace(temp, path)


def _description(parent_name: str, counts: dict[str, Any], automatic_scan_id: str | None = None) -> str:
    parts = ", ".join(f"{item['rows']:,} from the {item['label']} result" for item in counts["by_scanner"])
    rows = counts["rows"]
    plural = "" if rows == 1 else "s"
    if automatic_scan_id is not None:
        return (
            f"Made automatically from {parent_name} when scan {automatic_scan_id} finished: it dropped the "
            f"{rows:,} row{plural} the scan flagged ({parts}). The rows that are left are copied unchanged."
        )
    return (
        f"Made from {parent_name} by dropping {rows:,} row{plural} chosen from its scan results "
        f"({parts}). The rows that are left are copied unchanged."
    )


def _undo(dataset_id: str) -> None:
    """Remove the dataset a drop registered, when nothing was submitted for it."""
    try:
        registry.unregister(dataset_id)
    except registry.RegistryError as exc:
        _log(f"couldn't remove dataset {dataset_id} after a failed start: {exc}")


def submit(dataset_id: str, request: Any, automatic_scan_id: str | None = None) -> dict[str, Any]:
    """Drop the chosen rows: resolve them again, mark the parent's version kept, register the new
    dataset, write its drop list and submit the filter job. Refused, with nothing changed, when
    the request is wrong, a result changed, or SLURM can't be asked. A failure before `sbatch`
    runs removes the new dataset again; one from `sbatch` itself leaves it registered, showing
    why, and it can be unregistered.

    `automatic_scan_id` is only given by autodrop.py (D17), for the drop it makes by itself after
    that scan: the drop list, the record and the new dataset's description then say so. The HTTP
    routes never pass it."""
    version_id, selections = _validated(request)
    with _LOCK:
        resources = _resources()
        dataset = registry.get_dataset(dataset_id)
        parent_folder = _parent_version(dataset, version_id)
        dropped = _resolve(dataset_id, version_id, selections)

        if not registry.mark_kept(dataset_id, version_id):
            raise FiltersError("The dataset was converted again since the rows were chosen. Open its results again.")
        child = registry.register_filtered(
            dataset_id, dropped.counts["rows"], _description(dataset["name"], dropped.counts, automatic_scan_id)
        )
        child_id = child["id"]

        new_version = jobs._new_batch_id()
        folder = _dataset_folder(child_id)
        output = _version_folder(child_id, new_version)
        argv = _command(parent_folder, folder, new_version, child_id, resources)
        record: dict[str, Any] = {
            "dataset_id": child_id,
            "parent_id": dataset_id,
            "parent_version": {key: dataset["prepared"].get(key) for key in ("version_id", "folder", "rows")},
            "version_id": new_version,
            "created_at": jobs._now_iso(),
            # Set when the drop was made by itself after a scan (D17), None when a user chose it.
            "automatic": {"scan_id": automatic_scan_id} if automatic_scan_id is not None else None,
            "drop": dropped.counts,
            "resources": resources,
            "command": argv,
            "command_text": shlex.join(argv),
            "submitted_at": None,
            "job_id": None,
            "submit_error": None,
            "wrapper_output": None,
            "result": None,
        }
        try:
            (folder / JOBS_FOLDER).mkdir(parents=True, exist_ok=True)
            (folder / LOGS_FOLDER).mkdir(exist_ok=True)
            _write_drop_list(folder / DROP_LIST_FILE, dropped.lines, automatic_scan_id is not None)
            # Made here, not by the job: a folder that was looked at before it existed is
            # remembered as "not there" by the shared disk for about a minute.
            output.mkdir(parents=True)
            # Stamped and written before `sbatch` runs: the job can start before it returns,
            # and a crash part-way must not leave a drop that would be started a second time.
            record["submitted_at"] = jobs._now_iso()
            _write_record(record)
        except OSError as exc:
            _undo(child_id)
            raise FiltersError(f"Nothing was started: couldn't set up {folder} ({exc}).") from exc

        try:
            result = jobs._run(argv, cwd=REPO_ROOT, env=scans._clean_environment())
        except jobs.SlurmUnavailable as exc:
            record["submit_error"] = f"{exc} The job may or may not have been submitted; check `squeue`."
        else:
            record["wrapper_output"] = (result.stdout + result.stderr)[-MAX_OUTPUT_CHARS:]
            slurm_id = result.stdout.strip().split(";")[0]
            if result.returncode != 0:
                record["submit_error"] = f"sbatch failed: {jobs._failure_detail(result)}"
            elif scans.SLURM_JOB_ID.fullmatch(slurm_id) is None:
                record["submit_error"] = f"sbatch printed something unexpected: {result.stdout.strip()!r}"
            else:
                record["job_id"] = slurm_id
        _write_record(record)
        return {
            "dataset_id": child_id,
            "name": child["name"],
            "parent_id": dataset_id,
            "rows": dropped.counts["rows"],
            "counts": dropped.counts,
            "job_id": record["job_id"],
            "version_id": new_version,
            "submit_error": record["submit_error"],
        }


# -------------------------------------------------------------------------
# Making a finished filter the dataset's Prepared version
# -------------------------------------------------------------------------


def _finished(dataset_id: str, record: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """`(prepared, None)` for a filter job SLURM no longer lists that wrote a finished run, or
    `(None, why not)`. Only call it once SLURM no longer lists the job."""
    folder = _version_folder(dataset_id, record["version_id"])
    if not (folder / jobs.SUCCESS_MARKER).is_file():
        return None, f"The job ended, but its output folder has no {jobs.SUCCESS_MARKER}. See its log."
    summary = read_json_or_none(folder / "summary.json")
    if not isinstance(summary, dict) or summary.get("tool") != FILTER_ROWS_TOOL:
        return None, "The job ended, but it didn't write a summary.json of the filter. See its log."
    checks = summary.get("checks")
    if not isinstance(checks, dict) or checks.get("passed") is not True:
        failures = checks.get("failures") if isinstance(checks, dict) else None
        detail = f": {failures[0]}" if isinstance(failures, list) and failures else "."
        return None, f"The filter's own checks failed{detail}"
    written = (summary.get("rows") or {}).get("written")
    removed = (summary.get("rows") or {}).get("removed")
    rows_read = (summary.get("source") or {}).get("rows_read")
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (written, removed, rows_read)):
        return None, "The filter's summary.json doesn't say how many rows it kept and removed."
    if removed != record["drop"]["rows"]:
        return None, f"The filter removed {removed:,} rows, but {record['drop']['rows']:,} were chosen."
    created = jobs._parse_time(summary.get("created_at"))
    submitted = jobs._parse_time(record.get("submitted_at"))
    if created is None or (submitted is not None and created < submitted):
        return None, "The filter's summary.json is older than the job, so it isn't this job's."
    return (
        {
            "version_id": record["version_id"],
            "folder": str(folder),
            "rows": written,
            "rows_read": rows_read,
            "skipped": None,
            "finished_at": summary.get("created_at"),
            "kept": False,
            "removed": removed,
        },
        None,
    )


def _pending() -> list[tuple[str, dict[str, Any]]]:
    """The filtered datasets whose job was submitted and hasn't been made current yet."""
    found = []
    for dataset_id in registry.filtered_without_version():
        record = _read_record(dataset_id)
        if record is not None and record.get("job_id") and record.get("result") is None and not record.get("submit_error"):
            found.append((dataset_id, record))
    return found


def _finalize_pending() -> None:
    pending = _pending()
    if not pending:
        return
    active, accounted, _error = scans._read_slurm([record["job_id"] for _id, record in pending])
    for dataset_id, record in pending:
        state, _reason = scans._slurm_outcome(record["job_id"], active, accounted)
        if state != scans.STATE_SUCCEEDED:
            continue
        prepared, _why = _finished(dataset_id, record)
        if prepared is None:
            continue
        # Set first, then noted in the record: a crash between the two is repaired by the next
        # pass, the other order would leave a finished filter nobody ever picks up.
        registry.set_prepared(dataset_id, prepared)
        record["result"] = {key: prepared[key] for key in ("finished_at", "rows", "removed")}
        _write_record(record)


def refresh() -> None:
    """Make the output of every finished filter job its dataset's Prepared version. Skipped, not
    waited for, while a submit is in progress: the next read does it. SLURM is only asked when a
    filter is waiting to be picked up."""
    if not _LOCK.acquire(blocking=False):
        return
    try:
        _finalize_pending()
    except (jobs.JobsError, jobs.SlurmUnavailable, registry.RegistryError, OSError) as exc:
        _log(f"refresh: {exc}")
    finally:
        _LOCK.release()


def _refresh_forever() -> None:
    while True:
        try:
            refresh()
        except Exception as exc:  # the task has to outlive any one bad pass
            _log(f"refresh failed: {exc!r}")
        time.sleep(REFRESH_SECONDS)


def start_background_refresh() -> None:
    """Start the task that calls `refresh` every `REFRESH_SECONDS`, so a finished filter becomes
    its dataset's data even when nobody is looking. A daemon thread: it ends with the process."""
    threading.Thread(target=_refresh_forever, name="filter-refresh", daemon=True).start()


# -------------------------------------------------------------------------
# Watching
# -------------------------------------------------------------------------


def _state_of(
    dataset: dict[str, Any],
    record: dict[str, Any] | None,
    active: dict[str, dict[str, Any]] | None,
    accounted: dict[str, list[dict[str, Any]]] | None,
) -> tuple[str, str | None]:
    """`(state, reason)` of a filtered dataset's job, with the states scans.py uses."""
    if isinstance(dataset.get("prepared"), dict) or (record is not None and record.get("result") is not None):
        return scans.STATE_SUCCEEDED, None
    if record is None:
        return scans.STATE_FAILED, "This filtered version has no readable record of its job, so it was never started."
    if record.get("submit_error"):
        return scans.STATE_FAILED, record["submit_error"]
    job_id = record.get("job_id")
    if job_id is None:
        return scans.STATE_FAILED, "It was being started when the service stopped. Check `squeue` for a job named filter_" + dataset["id"] + "."
    state, reason = scans._slurm_outcome(job_id, active, accounted)
    if state == scans.STATE_SUCCEEDED:
        # SLURM no longer lists the job, so it is safe to look for its files.
        prepared, why = _finished(dataset["id"], record)
        if prepared is None:
            return scans.STATE_FAILED, why
    return state, reason


def _slurm_for(pairs: list[tuple[dict[str, Any], dict[str, Any] | None]]) -> tuple[Any, Any, str | None]:
    """`(squeue jobs, sacct rows, error)` for the jobs of the datasets that haven't finished. Nothing
    is asked of SLURM when there are none."""
    ids = [
        record["job_id"]
        for dataset, record in pairs
        if record is not None
        and record.get("job_id")
        and record.get("result") is None
        and not record.get("submit_error")
        and not isinstance(dataset.get("prepared"), dict)
    ]
    return scans._read_slurm(ids) if ids else ({}, {}, None)


def _drop_numbers(dataset: dict[str, Any], record: dict[str, Any] | None) -> int | None:
    """Rows dropped: what the filter reported once it has finished, what was chosen before."""
    prepared = dataset.get("prepared")
    if isinstance(prepared, dict) and isinstance(prepared.get("removed"), int):
        return prepared["removed"]
    return record["drop"]["rows"] if record is not None else None


def _summary(
    dataset: dict[str, Any],
    record: dict[str, Any] | None,
    active: Any,
    accounted: Any,
    names: dict[str, str],
) -> dict[str, Any]:
    state, reason = _state_of(dataset, record, active, accounted)
    parent_id = dataset["parent"]
    prepared = dataset.get("prepared")
    version_id = record.get("version_id") if record is not None else None
    return {
        "state": state,
        "reason": reason,
        "removed": _drop_numbers(dataset, record),
        "parent_id": parent_id,
        "parent_name": names.get(parent_id),
        "version_id": version_id or (prepared.get("version_id") if isinstance(prepared, dict) else None),
    }


def dataset_summaries(datasets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """For each filtered dataset among `datasets` (as the registry returns them), by id: the state
    of its filter job, how many rows it drops, and its parent. One squeue and one sacct for all."""
    filtered = [dataset for dataset in datasets if dataset.get("parent")]
    if not filtered:
        return {}
    records = {dataset["id"]: _read_record(dataset["id"]) for dataset in filtered if dataset.get("prepared") is None}
    active, accounted, _error = _slurm_for([(dataset, records.get(dataset["id"])) for dataset in filtered])
    names = registry.dataset_names()
    return {
        dataset["id"]: _summary(dataset, records.get(dataset["id"]), active, accounted, names) for dataset in filtered
    }


def filtered_versions(dataset_id: str) -> list[dict[str, Any]]:
    """The datasets made from this one by dropping rows, newest first, with the state of each."""
    children = registry.filtered_versions(dataset_id)
    summaries = dataset_summaries(children)
    return [
        {
            "id": child["id"],
            "name": child["name"],
            "created_at": child["created_at"],
            "rows": child["prepared"].get("rows") if isinstance(child.get("prepared"), dict) else None,
            **{key: summaries[child["id"]][key] for key in ("state", "reason", "removed")},
        }
        for child in children
    ]


def _view(
    dataset: dict[str, Any],
    record: dict[str, Any] | None,
    active: Any,
    accounted: Any,
    names: dict[str, str],
    with_logs: bool,
) -> dict[str, Any]:
    summary = _summary(dataset, record, active, accounted, names)
    job_id = record.get("job_id") if record is not None else None
    view: dict[str, Any] = {
        "dataset_id": dataset["id"],
        "dataset_name": dataset["name"],
        **summary,
        "created_at": record.get("created_at") if record is not None else None,
        "submitted_at": record.get("submitted_at") if record is not None else None,
        "finished_at": ((record.get("result") or {}).get("finished_at") if record is not None else None),
        "elapsed": scans._slurm_elapsed(job_id, active, accounted) if job_id is not None else None,
        "job_id": job_id,
        "drop": record.get("drop") if record is not None else None,
        # `{scan_id}` for a drop made by itself after a scan (D17), None for one a user chose.
        "automatic": record.get("automatic") if record is not None else None,
        "folder": str(_version_folder(dataset["id"], record["version_id"])) if record is not None else None,
    }
    if with_logs:
        view.update(
            command_text=record.get("command_text") if record is not None else None,
            wrapper_output=record.get("wrapper_output") if record is not None else None,
            logs=[],
        )
        if job_id is not None:
            for label, suffix in (("Filter output", "out"), ("Filter errors", "err")):
                path = _dataset_folder(dataset["id"]) / LOGS_FOLDER / f"filter-{job_id}.{suffix}"
                # Nothing is looked for on disk while the job is queued (see the module docstring).
                tail = jobs._tail_file(path) if summary["state"] != scans.STATE_QUEUED else None
                view["logs"].append({"name": label, "path": str(path), "tail": tail})
    return view


def status(dataset_id: str) -> dict[str, Any]:
    """One filtered dataset's job in full: its state, what it drops, the command and log tails."""
    refresh()
    dataset = registry.get_dataset(dataset_id)
    if not dataset.get("parent"):
        raise FiltersError(f"'{dataset['name']}' isn't a filtered version.")
    record = _read_record(dataset_id)
    active, accounted, slurm_error = _slurm_for([(dataset, record)])
    return {**_view(dataset, record, active, accounted, registry.dataset_names(), True), "slurm_error": slurm_error}


def list_all() -> dict[str, Any]:
    """Every filter job of every filtered dataset, newest first, for the Activity page. One squeue
    and one sacct cover all of them."""
    refresh()
    filtered = [dataset for dataset in registry.list_datasets() if dataset.get("parent")]
    records = {dataset["id"]: _read_record(dataset["id"]) for dataset in filtered}
    active, accounted, slurm_error = _slurm_for([(dataset, records[dataset["id"]]) for dataset in filtered])
    names = registry.dataset_names()
    views = [_view(dataset, records[dataset["id"]], active, accounted, names, False) for dataset in filtered]
    # The version ids start with the time they were made, so this is newest first. A dataset
    # with no readable record has none, and goes last.
    views.sort(key=lambda view: view["created_at"] or "", reverse=True)
    return {
        "slurm_error": slurm_error,
        "has_active": any(view["state"] in (scans.STATE_QUEUED, scans.STATE_RUNNING, scans.STATE_UNKNOWN) for view in views),
        "filters": views,
    }


# -------------------------------------------------------------------------
# Cancelling, and unregistering
# -------------------------------------------------------------------------


def cancel(dataset_id: str) -> dict[str, Any]:
    """`scancel` the one SLURM job a filtered dataset's record lists, if it is still going. The
    dataset stays, showing the job as cancelled; unregister it to remove it."""
    registry.get_dataset(dataset_id)
    record = _read_record(dataset_id)
    job_id = record.get("job_id") if record is not None else None
    if not isinstance(job_id, str):
        raise FiltersError("This filtered version has no SLURM job to cancel.")
    with _LOCK:
        active = jobs.active_jobs()
        if not scans._is_listed(job_id, active):
            return {"cancelled": [], "not_active": [job_id]}
        result = jobs._run(["scancel", job_id])
        if result.returncode != 0:
            raise FiltersError(f"scancel failed: {jobs._failure_detail(result)}")
    return {"cancelled": [job_id], "not_active": []}


def unregister(dataset_id: str) -> dict[str, Any]:
    """`scans.unregister`, refused while the dataset's filter job is queued or running. SLURM is
    only asked when the dataset has submitted a filter job. A dataset that has filtered versions
    of its own is refused by the registry."""
    registry.get_dataset(dataset_id)  # an unknown or malformed id fails here
    with _LOCK:
        record = _read_record(dataset_id)
        job_id = record.get("job_id") if record is not None else None
        if isinstance(job_id, str):
            # Raises SlurmUnavailable: a running job can't be ruled out without squeue.
            if scans._is_listed(job_id, jobs.active_jobs()):
                raise FiltersError(f"The filter job of this dataset is queued or running (SLURM job {job_id}). Cancel it first.")
        return scans.unregister(dataset_id)
