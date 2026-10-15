"""Starting, watching, cancelling and replacing scans.

A scan is one start of one scanner (scanners.py) over one or more datasets. It lives in

    <service root>/scans/<scan id>/

with its record in `scan.json`, the files it is given in `inputs/`, its output in `results/`
and its logs in `_logs/`. The scan ID has the same shape as a batch ID (`jobs.BATCH_ID`).

How a scan starts (D3, D4): the chosen datasets are grouped into scans (one for all of them,
or one for each, as the scanner and the mode say). A dataset that isn't converted, or that
the user chose to convert again, gets a conversion job first (conversions.start_conversion
through `conversions.submit`). The scan's own job can only be made once every dataset has a
Prepared version, so a scan that has to wait is saved without a command, and `refresh` starts
it as soon as its conversions have finished. A conversion that fails or is cancelled means
the scan is never started ("not started: conversion failed").

Rules this module keeps:

* The original data and the Prepared versions are never written to or deleted here. A scan
  only reads them. Everything this module writes is inside `<service root>/scans/<scan id>/`
  (and the one pointer in datasets.json that says which scan is a dataset's result).
* The only thing deleted is a whole `scans/<scan id>` folder, through `_delete_scan_folder`,
  which checks the ID and that the folder really is that path, and refuses a symbolic link.
  A folder is deleted when its scan was a dataset's result (D5) and nothing points at it any
  more (a newer scan replaced it, or the dataset was converted again or unregistered, D6 and
  D2), or when a scan that never became a result belonged to a dataset that was unregistered.
  A scan that failed or was cancelled is kept, so the failure can be read; so is a scan that
  is still going. A scan shared with other datasets stays while any of them still uses it.
* Every SLURM call goes through jobs.py: an argument list, `shell=False`, a 30 second timeout.
  Every job states its partition, CPUs, memory and time limit.
* Job state is never stored. It is read live from SLURM and from the files a job writes. What
  is saved is what was started (settings, command, the versions read, job IDs) and, once, the
  fact that a finished scan became the result of its datasets (`result_applied_at`).
* A scan's `_SUCCESS` is only looked for once SLURM no longer lists its job, and the empty
  `results/` folder is made before `sbatch` (on this cluster's shared disk, asking for a file
  or folder before it exists makes this machine remember "not there" for about a minute).
* One lock covers every start, cancel and replacement, so two clicks can't start two jobs for
  one scan, and a scan isn't deleted while another request is checking it. `refresh` skips
  instead of waiting when the lock is taken.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import conversions
import jobs
import registry
import scanners
from reader_imports import REPO_ROOT

SCANS_FOLDER = "scans"
SCAN_FILE = "scan.json"
MAX_DATASETS = 50
# The most of a command's output that a record keeps.
MAX_OUTPUT_CHARS = 50_000
# How often the background task looks for scans that can start or have finished.
REFRESH_SECONDS = 30
# Stands for the scan ID in the commands shown before a start: the ID is chosen when the
# scan is made, and the scan's record then holds the exact command that ran.
SCAN_ID_PLACEHOLDER = "<scan-id>"
SLURM_JOB_ID = re.compile(r"^[0-9]{1,12}$")

STATE_WAITING = "waiting_for_conversion"
STATE_CONVERSION_FAILED = "conversion_failed"
STATE_STARTING = "starting"
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_UNKNOWN = "unknown"
STATE_NOT_SCANNED = "not_scanned"

# A scan in one of these has not ended: it holds its datasets. A state that couldn't be read
# holds them too, because the job may be running.
HOLDING_STATES = (STATE_WAITING, STATE_STARTING, STATE_QUEUED, STATE_RUNNING, STATE_UNKNOWN)
# What a conversion's outcome can be, when it won't give the scan its data.
CONVERSION_FAILED_OUTCOMES = ("failed", "checks_failed", "cancelled", "not_submitted")

ACTION_USE_EXISTING = "use_existing"
ACTION_CONVERT = "convert"
ACTION_CONVERT_AGAIN = "convert_again"
CONVERT_ACTIONS = (ACTION_CONVERT, ACTION_CONVERT_AGAIN)

_LOCK = threading.Lock()


class ScanError(ValueError):
    """The request can't be carried out; the message says why."""


def _log(message: str) -> None:
    print(f"[scans] {message}", file=sys.stderr, flush=True)


# -------------------------------------------------------------------------
# Where things live, and the scan records
# -------------------------------------------------------------------------


def _scans_root() -> Path:
    return registry.service_root() / SCANS_FOLDER


def _scan_folder(scan_id: Any) -> Path:
    # The ID becomes part of a path, so only the exact format is let through.
    if not isinstance(scan_id, str) or jobs.BATCH_ID.fullmatch(scan_id) is None:
        raise ScanError(f"'{scan_id}' isn't a scan id.")
    return _scans_root() / scan_id


def _is_scan(data: Any, scan_id: str) -> bool:
    """Whether `data` is a scan record whose ID, dataset IDs and job IDs can safely be put
    into paths and commands. A record is only a file, so none of this is taken on trust."""
    if not isinstance(data, dict) or data.get("id") != scan_id or data.get("scanner") not in scanners.SCANNERS:
        return False
    datasets = data.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        return False
    if not all(isinstance(item, str) and registry.DATASET_ID.fullmatch(item) for item in datasets):
        return False
    waits = data.get("conversions")
    if not isinstance(waits, dict):
        return False
    for dataset_id, entry in waits.items():
        if dataset_id not in datasets or not isinstance(entry, dict):
            return False
        if not isinstance(entry.get("job_id"), str) or jobs.BATCH_ID.fullmatch(entry["job_id"]) is None:
            return False
    job_id = data.get("job_id")
    if job_id is not None and not (isinstance(job_id, str) and SLURM_JOB_ID.fullmatch(job_id)):
        return False
    versions = data.get("versions")
    return (
        isinstance(data.get("settings"), dict)
        and isinstance(data.get("resources"), dict)
        and (versions is None or isinstance(versions, dict))
    )


def _read_scan(scan_id: str) -> dict[str, Any] | None:
    data = jobs._read_json(_scan_folder(scan_id) / SCAN_FILE)
    return data if _is_scan(data, scan_id) else None


def _load_scan(scan_id: Any) -> dict[str, Any]:
    data = _read_scan(scan_id) if isinstance(scan_id, str) and jobs.BATCH_ID.fullmatch(scan_id) else None
    if data is None:
        raise ScanError(f"There's no readable scan {scan_id}.")
    return data


def _write_scan(scan: dict[str, Any]) -> None:
    jobs._write_json_atomic(_scan_folder(scan["id"]) / SCAN_FILE, scan)


def _all_scans() -> tuple[list[dict[str, Any]], list[str]]:
    """Every scan record, newest first, and a note for each folder that isn't a readable scan."""
    root = _scans_root()
    scans: list[dict[str, Any]] = []
    problems: list[str] = []
    if not root.is_dir():
        return scans, problems
    for path in sorted(root.iterdir(), reverse=True):
        if jobs.BATCH_ID.fullmatch(path.name) is None or path.is_symlink() or not path.is_dir():
            continue
        data = _read_scan(path.name)
        if data is None:
            problems.append(f"{path.name} couldn't be read as a scan.")
        else:
            scans.append(data)
    return scans, problems


def _make_scan_folder() -> tuple[str, Path]:
    """A new, empty `scans/<scan id>` folder and its ID."""
    root = _scans_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        while True:
            scan_id = jobs._new_batch_id()
            folder = root / scan_id
            try:
                folder.mkdir()
            except FileExistsError:
                continue
            return scan_id, folder
    except OSError as exc:
        raise ScanError(f"Couldn't create a scan folder in {root} ({exc}).") from exc


def _delete_scan_folder(scan_id: str) -> None:
    """Delete `<service root>/scans/<scan id>` and nothing else."""
    folder = _scan_folder(scan_id)
    if folder.is_symlink():
        raise ScanError(f"{folder} is a symbolic link, so it isn't deleted.")
    if not folder.exists():
        return
    if folder.parent != _scans_root() or folder.resolve() != folder:
        raise ScanError(f"{folder} isn't where it should be (it resolves to {folder.resolve()}), so it isn't deleted.")
    if not folder.is_dir():
        raise ScanError(f"{folder} isn't a folder, so it isn't deleted.")
    try:
        shutil.rmtree(folder)
    except OSError as exc:
        raise ScanError(f"Couldn't delete {folder} ({exc}).") from exc


def _modified_at(path: Path) -> str | None:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")


# -------------------------------------------------------------------------
# The state of a scan, read live
# -------------------------------------------------------------------------


def _state(
    state: str,
    reason: str | None = None,
    waiting_for: list[dict[str, Any]] | None = None,
    elapsed: str | None = None,
) -> dict[str, Any]:
    return {"state": state, "reason": reason, "waiting_for": waiting_for or [], "elapsed": elapsed}


def _slurm_outcome(
    job_id: str,
    active: dict[str, dict[str, Any]] | None,
    accounted: dict[str, dict[str, Any]] | None,
) -> tuple[str, str | None]:
    """`(state, reason)` of one SLURM job. `active` and `accounted` are None when squeue or
    sacct couldn't be read. This is not jobs._decide_outcome, which is made for conversions
    (it looks for a progress.json, which a scan doesn't write)."""
    if active is None:
        return STATE_UNKNOWN, "SLURM couldn't be reached, so this job's state isn't known."
    listed = active.get(job_id)
    if listed is not None:
        if listed["state"] == "PENDING":
            return STATE_QUEUED, listed["reason"]
        return STATE_RUNNING, listed["state"]
    if accounted is None:
        return STATE_UNKNOWN, "SLURM couldn't be reached, so this job's state isn't known."
    record = accounted.get(job_id)
    if record is None:
        return STATE_FAILED, "SLURM has no record of this job: it isn't queued, and accounting doesn't know it."
    state = record["state"]
    if state == "COMPLETED":
        return STATE_SUCCEEDED, None
    if state == "CANCELLED":
        return STATE_CANCELLED, record["state_text"]
    if state in jobs.STILL_GOING_STATES:
        return jobs.STILL_GOING_STATES[state], f"SLURM accounting says {state}."
    return STATE_FAILED, f"SLURM state {record['state_text'] or 'unknown'}, exit code {record['exit_code']}."


def _slurm_elapsed(
    job_id: str,
    active: dict[str, dict[str, Any]] | None,
    accounted: dict[str, dict[str, Any]] | None,
) -> str | None:
    """How long SLURM says one job has run ("1-02:03:04" or "0:42"): from squeue while it is
    listed, from sacct once it has left the queue. None when SLURM doesn't know."""
    listed = (active or {}).get(job_id)
    if listed is not None:
        return listed["elapsed"]
    record = (accounted or {}).get(job_id)
    return record["elapsed"] if record is not None else None


def _dataset(dataset_id: str, cache: dict[Any, Any]) -> dict[str, Any] | None:
    key = ("dataset", dataset_id)
    if key not in cache:
        try:
            cache[key] = registry.get_dataset(dataset_id)
        except registry.DatasetNotFound:
            cache[key] = None
    return cache[key]


def _conversion_view(dataset_id: str, job_id: str, cache: dict[Any, Any]) -> dict[str, Any] | None:
    """One conversion of a dataset as conversions.list_conversions shows it (which also makes
    a conversion that has just finished the dataset's current Prepared version)."""
    key = ("conversions", dataset_id)
    if key not in cache:
        try:
            cache[key] = {view["job_id"]: view for view in conversions.list_conversions(dataset_id)["conversions"]}
        except (registry.DatasetNotFound, jobs.JobsError):
            cache[key] = {}
    return cache[key].get(job_id)


def _waiting_state(scan: dict[str, Any], cache: dict[Any, Any]) -> dict[str, Any]:
    """The state of a scan that has no job yet: waiting for conversions, not to be started
    because one failed, or ready to start."""
    failures: list[str] = []
    waiting: list[dict[str, Any]] = []
    for dataset_id in scan["datasets"]:
        dataset = _dataset(dataset_id, cache)
        if dataset is None:
            failures.append(f"Dataset {dataset_id} isn't registered any more.")
            continue
        name = dataset["name"]
        entry = scan["conversions"].get(dataset_id)
        if entry is None:
            if dataset["prepared"] is None:
                failures.append(f"'{name}' isn't converted any more: it was converted again after this scan was made.")
            continue
        view = _conversion_view(dataset_id, entry["job_id"], cache)
        if view is None:
            failures.append(f"The conversion of '{name}' ({entry['job_id']}) has no record any more.")
        elif view["current"]:
            continue
        elif view["outcome"] in CONVERSION_FAILED_OUTCOMES:
            detail = f": {view['reason']}" if view["reason"] else "."
            failures.append(f"The conversion of '{name}' {view['outcome'].replace('_', ' ')}{detail}")
        else:
            waiting.append({"dataset_id": dataset_id, "name": name, "job_id": entry["job_id"], "outcome": view["outcome"]})
    if failures:
        return _state(STATE_CONVERSION_FAILED, " ".join(failures))
    if waiting:
        return _state(STATE_WAITING, None, waiting)
    return _state(STATE_STARTING)


def _state_of(
    scan: dict[str, Any],
    active: dict[str, dict[str, Any]] | None,
    accounted: dict[str, dict[str, Any]] | None,
    cache: dict[Any, Any],
) -> dict[str, Any]:
    if scan.get("result_applied_at") is not None:
        return _state(STATE_SUCCEEDED)
    job_id = scan.get("job_id")
    if job_id is None:
        if scan.get("submit_error"):
            return _state(STATE_FAILED, scan["submit_error"])
        if scan.get("submitted_at"):
            # The record is written before `sbatch` runs, and again after. Starting it again
            # could run the scan twice.
            job_name = f"scan_{scan['scanner']}_{scan['id']}"
            return _state(STATE_FAILED, f"It was being started when the service stopped. Check `squeue` for a job named {job_name}.")
        if scan.get("cancelled_at"):
            return _state(STATE_CANCELLED, "Cancelled before it started.")
        return _waiting_state(scan, cache)
    state, reason = _slurm_outcome(job_id, active, accounted)
    if state == STATE_SUCCEEDED:
        # SLURM no longer lists the job, so it is safe to look for the marker.
        if not (_scan_folder(scan["id"]) / scanners.RESULTS_FOLDER / scanners.SUCCESS_MARKER).is_file():
            return _state(
                STATE_FAILED,
                f"The job ended, but its results folder has no {scanners.SUCCESS_MARKER} marker.",
                elapsed=_slurm_elapsed(job_id, active, accounted),
            )
    return _state(state, reason, elapsed=_slurm_elapsed(job_id, active, accounted))


def _states_of(scans: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], str | None]:
    """The state of each scan, by ID, and what SLURM said when it couldn't be read. One squeue
    and one sacct for all of them. A scan that became a result is finished: nothing is asked."""
    job_ids = [scan["job_id"] for scan in scans if scan.get("job_id") and scan.get("result_applied_at") is None]
    active, accounted, slurm_error = jobs._read_slurm(job_ids) if job_ids else ({}, {}, None)
    cache: dict[Any, Any] = {}
    return {scan["id"]: _state_of(scan, active, accounted, cache) for scan in scans}, slurm_error


# -------------------------------------------------------------------------
# Planning: what a scan would do, or why it can't be started
# -------------------------------------------------------------------------


def _scanner(key: Any) -> scanners.Scanner:
    scanner = scanners.SCANNERS.get(key) if isinstance(key, str) else None
    if scanner is None:
        raise ScanError(f"There is no scanner {key!r}. The scanners are: {', '.join(scanners.SCANNERS)}.")
    return scanner


def _validated_items(items: Any) -> list[dict[str, Any]]:
    """The `{id, reconvert}` list of a request, checked."""
    if not isinstance(items, list) or not items:
        raise ScanError("Choose at least one dataset.")
    if len(items) > MAX_DATASETS:
        raise ScanError(f"At most {MAX_DATASETS} datasets can be scanned in one request.")
    checked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        dataset_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(dataset_id, str) or registry.DATASET_ID.fullmatch(dataset_id) is None:
            raise ScanError(f"'{dataset_id}' isn't a dataset id.")
        if dataset_id in seen:
            raise ScanError(f"Dataset {dataset_id} is listed twice.")
        seen.add(dataset_id)
        checked.append({"id": dataset_id, "reconvert": item.get("reconvert") is True})
    return checked


def _check_mode(scanner: scanners.Scanner, mode: Any, problems: list[str]) -> str | None:
    if scanner.modes is None:
        if mode is not None:
            problems.append(f"{scanner.label} makes one scan for each dataset, so it has no mode.")
        return None
    if mode is None:
        return scanner.default_mode
    if mode not in scanner.modes:
        problems.append(f"The mode must be one of {', '.join(scanner.modes)}.")
        return scanner.default_mode
    return mode


def _units(scanner: scanners.Scanner, datasets: list[dict[str, Any]], mode: str | None) -> list[dict[str, Any]]:
    """The scans a request makes (D4): each is a mode and the datasets it covers. Across
    datasets they are in the order of their registered paths, which is the order the scan
    reads them in, so the dataset that comes first by path holds the copy that counts as the
    original (D10)."""
    if scanner.modes is None:
        return [{"mode": None, "datasets": [dataset]} for dataset in datasets]
    if mode == scanners.MODE_ACROSS and len(datasets) > 1:
        return [{"mode": scanners.MODE_ACROSS, "datasets": sorted(datasets, key=lambda item: (item["path"], item["id"]))}]
    return [{"mode": scanners.MODE_WITHIN, "datasets": [dataset]} for dataset in datasets]


def _holding_scans() -> dict[str, list[dict[str, Any]]]:
    """For each dataset, the scans that hold it right now (waiting, queued or running): scan
    ID, scanner and state."""
    scans, _problems = _all_scans()
    pending = [scan for scan in scans if scan.get("result_applied_at") is None]
    states, _error = _states_of(pending)
    held: dict[str, list[dict[str, Any]]] = {}
    for scan in pending:
        state = states[scan["id"]]["state"]
        if state in HOLDING_STATES:
            for dataset_id in scan["datasets"]:
                held.setdefault(dataset_id, []).append({"scan_id": scan["id"], "scanner": scan["scanner"], "state": state})
    return held


def _action(item: dict[str, Any]) -> str | None:
    """What a dataset needs before it can be scanned, from conversions.check's answer for it;
    None when it can't be made ready. A dataset that is already converted and stays so
    (`converted`) is the usual case, not a refusal."""
    if item["status"] == "converted":
        return ACTION_USE_EXISTING
    if item["status"] == "ready":
        return ACTION_CONVERT_AGAIN if item["reconvert"] else ACTION_CONVERT
    return None


def _plan_unit(
    scanner: scanners.Scanner,
    unit: dict[str, Any],
    conversion_items: dict[str, dict[str, Any]],
    held: dict[str, list[dict[str, Any]]],
    settings: dict[str, Any],
    resources: dict[str, Any] | None,
    request_ok: bool,
) -> dict[str, Any]:
    datasets_root = registry.service_root() / "datasets"
    blocked: list[str] = []
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    version_folders: dict[str, Path] = {}
    for dataset in unit["datasets"]:
        item = conversion_items[dataset["id"]]
        action = _action(item)
        name = dataset["name"]
        prepared = dataset["prepared"]
        if action is None:
            blocked.append(f"'{name}': {item['reason']}")
        for other in held.get(dataset["id"], []):
            if other["scanner"] == scanner.key:
                blocked.append(
                    f"'{name}': a {scanner.label} scan ({other['scan_id']}) is already {other['state'].replace('_', ' ')} on it."
                )
            elif action == ACTION_CONVERT_AGAIN:
                blocked.append(
                    f"'{name}': a {scanners.SCANNERS[other['scanner']].label} scan ({other['scan_id']}) is {other['state'].replace('_', ' ')} "
                    "on it, and converting again would delete the data it reads."
                )
        if action == ACTION_CONVERT_AGAIN and item["will_delete"] is not None:
            warnings.append(
                f"'{name}' will be converted again: its current converted data ({item['will_delete']['rows']} rows) "
                "and its scan results will be removed."
            )
        rows.append(
            {
                "id": dataset["id"],
                "name": name,
                "team": dataset["team"],
                "action": action,
                "status": item["status"],
                "reason": None if action is not None else item["reason"],
                "prepared": (
                    {key: prepared.get(key) for key in ("version_id", "rows", "finished_at")} if isinstance(prepared, dict) else None
                ),
                "will_delete": item["will_delete"],
                "leftovers": item["leftovers"],
                "conversion_command": item["command"],
                "conversion_sbatch_command": item["sbatch_command"],
            }
        )
        if action == ACTION_USE_EXISTING:
            version_folders[dataset["id"]] = Path(prepared["folder"])
        else:
            # Not known until the conversion starts.
            version_folders[dataset["id"]] = (
                datasets_root / dataset["id"] / conversions.PREPARED_FOLDER / conversions.VERSION_PLACEHOLDER
            )

    plan: dict[str, Any] = {
        "mode": unit["mode"],
        "datasets": rows,
        "blocked": blocked,
        "warnings": warnings,
        "cost": None,
        "needs_gpu_confirmation": False,
        "command_text": None,
    }
    if request_ok and resources is not None:
        plan["cost"] = scanner.cost(settings, resources)
        plan["needs_gpu_confirmation"] = bool(plan["cost"]["gpus"])
        context = scanners.ScanContext(
            scan_id=SCAN_ID_PLACEHOLDER,
            mode=unit["mode"],
            settings=settings,
            resources=resources,
            folder=_scans_root() / SCAN_ID_PLACEHOLDER,
            datasets_root=datasets_root,
            version_folders=version_folders,
        )
        try:
            plan["command_text"] = shlex.join(scanner.build_command(context))
        except scanners.ScannerError as exc:
            blocked.append(str(exc))
    return plan


def _plan(
    scanner_key: Any, items_in: Any, mode_in: Any, settings_in: Any, resources_in: Any
) -> dict[str, Any]:
    """Everything the check shows, and everything `submit` does. Read-only. What is wrong with
    the request as a whole is in `blocked`, and what is wrong with one scan in that scan's own
    `blocked`; nothing can be started while either isn't empty."""
    scanner = _scanner(scanner_key)
    items = _validated_items(items_in)
    problems: list[str] = []
    mode = _check_mode(scanner, mode_in, problems)
    settings = scanners.parse_settings(scanner, settings_in, problems)
    resources = scanners.parse_resources(scanner, resources_in, problems)

    # D3 is already worked out by the conversions' own check, for one dataset at a time.
    conversion_plan = conversions.check(items, jobs.DEFAULT_RESOURCES)
    conversion_items = {item["dataset_id"]: item for item in conversion_plan["items"]}
    if conversion_plan["slurm_error"]:
        problems.append(conversion_plan["slurm_error"])
    datasets = [registry.get_dataset(item["id"]) for item in items]
    held = _holding_scans()

    units = [
        _plan_unit(scanner, unit, conversion_items, held, settings, resources, not problems)
        for unit in _units(scanner, datasets, mode)
    ]
    needs_conversion = any(row["action"] in CONVERT_ACTIONS for unit in units for row in unit["datasets"])
    if needs_conversion and conversion_plan["resources_error"]:
        problems.append(conversion_plan["resources_error"])
    return {
        "scanner": scanner.key,
        "label": scanner.label,
        "mode": mode,
        "service_root": str(registry.service_root()),
        "slurm_error": conversion_plan["slurm_error"],
        "settings": settings,
        "resources": resources,
        "blocked": problems,
        "units": units,
        "can_start": not problems and not any(unit["blocked"] for unit in units),
    }


def _all_problems(plan: dict[str, Any]) -> list[str]:
    return [*plan["blocked"], *(problem for unit in plan["units"] for problem in unit["blocked"])]


def settings() -> dict[str, Any]:
    """What the scan forms need: the scanners, and what SLURM offers."""
    try:
        partitions, slurm_error = jobs.list_partitions(), None
    except jobs.SlurmUnavailable as exc:
        partitions, slurm_error = [], str(exc)
    return {
        "service_root": str(registry.service_root()),
        "scanners": {key: scanners.describe(scanner) for key, scanner in scanners.SCANNERS.items()},
        "partitions": partitions,
        "default_partition": jobs.DEFAULT_RESOURCES["partition"],
        "max_cpus": jobs.MAX_CPUS,
        "slurm_error": slurm_error,
    }


def check(scanner_key: Any, items_in: Any, mode_in: Any, settings_in: Any, resources_in: Any) -> dict[str, Any]:
    """The plan for a request: for each scan it would make, each dataset's conversion, the
    command (with `<version-id>` and `<scan-id>` where those aren't known yet), the cost and
    what stops it. Starts and deletes nothing."""
    return _plan(scanner_key, items_in, mode_in, settings_in, resources_in)


# -------------------------------------------------------------------------
# Starting
# -------------------------------------------------------------------------


def _clean_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if not name.startswith(scanners.ENV_PREFIXES)}


def _start_scan_job(scan: dict[str, Any]) -> None:
    """Write the scan's inputs and run `sbatch` for it. Call it while holding `_LOCK`, and only
    for a scan in the `starting` state: every dataset has a Prepared version. What goes wrong
    is noted in the record's `submit_error` instead of raised, so one scan that can't start
    doesn't stop the others."""
    scanner = scanners.SCANNERS[scan["scanner"]]
    folder = _scan_folder(scan["id"])
    try:
        versions: dict[str, Any] = {}
        for dataset_id in scan["datasets"]:
            dataset = registry.get_dataset(dataset_id)
            prepared = dataset.get("prepared")
            if not isinstance(prepared, dict):
                raise ScanError(f"'{dataset['name']}' isn't converted.")
            versions[dataset_id] = {key: prepared.get(key) for key in ("version_id", "folder", "rows")}
        context = scanners.ScanContext(
            scan_id=scan["id"],
            mode=scan["mode"],
            settings=scan["settings"],
            resources=scan["resources"],
            folder=folder,
            datasets_root=registry.service_root() / "datasets",
            version_folders={dataset_id: Path(version["folder"]) for dataset_id, version in versions.items()},
        )
        # Made here, not by the job: SLURM won't make a log folder, and a results folder that
        # was looked at before it existed is remembered as "not there" for about a minute.
        for child in (scanners.INPUTS_FOLDER, scanners.LOGS_FOLDER, scanners.RESULTS_FOLDER):
            (folder / child).mkdir(exist_ok=True)
        scanner.write_inputs(context)
        argv = scanner.build_command(context)
    except (ScanError, scanners.ScannerError, registry.RegistryError, OSError) as exc:
        scan["submit_error"] = f"Nothing was started: {exc}"
        _write_scan(scan)
        return

    scan["versions"] = versions
    scan["command"] = argv
    scan["command_text"] = shlex.join(argv)
    # Stamped and written before `sbatch` runs: the job can start before it returns, and a
    # crash part-way must not leave a scan that would be started a second time.
    scan["submitted_at"] = jobs._now_iso()
    _write_scan(scan)
    try:
        result = jobs._run(argv, cwd=REPO_ROOT, env=_clean_environment())
    except jobs.SlurmUnavailable as exc:
        scan["submit_error"] = f"{exc} The job may or may not have been submitted; check `squeue`."
    else:
        scan["wrapper_output"] = (result.stdout + result.stderr)[-MAX_OUTPUT_CHARS:]
        job_id = result.stdout.strip().split(";")[0]
        if result.returncode != 0:
            scan["submit_error"] = f"sbatch failed: {jobs._failure_detail(result)}"
        elif SLURM_JOB_ID.fullmatch(job_id) is None:
            scan["submit_error"] = f"sbatch printed something unexpected: {result.stdout.strip()!r}"
        else:
            scan["job_id"] = job_id
    _write_scan(scan)


def _create_scan(
    scanner: scanners.Scanner, unit: dict[str, Any], plan: dict[str, Any], waits: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    scan_id, _folder = _make_scan_folder()
    scan = {
        "id": scan_id,
        "scanner": scanner.key,
        "mode": unit["mode"],
        "datasets": [row["id"] for row in unit["datasets"]],
        "settings": plan["settings"],
        "resources": plan["resources"],
        "created_at": jobs._now_iso(),
        # Only the datasets that were converted for this scan: the others are used as they are.
        "conversions": waits,
        "cancelled_at": None,
        # Everything from here down is filled in when the scan's own job is started.
        "versions": None,
        "command": None,
        "command_text": None,
        "job_id": None,
        "submitted_at": None,
        "submit_error": None,
        "wrapper_output": None,
        # Set once the scan has become the result of its datasets.
        "finished_at": None,
        "result_applied_at": None,
    }
    _write_scan(scan)
    return scan


def submit(
    scanner_key: Any, items_in: Any, mode_in: Any, settings_in: Any, resources_in: Any, confirm_gpu: bool
) -> dict[str, Any]:
    """Start the conversions the datasets need, and the scans. The check is made again under
    the lock, and the whole request is refused if any scan can't be started. A scan whose
    datasets are all converted is started now; any other waits for its conversions."""
    with _LOCK:
        _maintain()
        plan = _plan(scanner_key, items_in, mode_in, settings_in, resources_in)
        if not plan["can_start"]:
            raise ScanError("Nothing was started. " + " ".join(_all_problems(plan)))
        gpu_units = [unit for unit in plan["units"] if unit["needs_gpu_confirmation"]]
        if gpu_units and not confirm_gpu:
            raise ScanError(
                f"Nothing was started. This scan uses GPUs ({gpu_units[0]['cost']['summary']}); confirm that you want to start it."
            )

        scanner = _scanner(scanner_key)
        wanted = [
            {"id": row["id"], "reconvert": row["action"] == ACTION_CONVERT_AGAIN}
            for unit in plan["units"]
            for row in unit["datasets"]
            if row["action"] in CONVERT_ACTIONS
        ]
        started: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        if wanted:
            outcome = conversions.submit(wanted, jobs.DEFAULT_RESOURCES)
            started = {entry["dataset_id"]: entry for entry in outcome["started"]}
            errors = list(outcome["errors"])

        created: list[dict[str, Any]] = []
        for unit in plan["units"]:
            waits: dict[str, dict[str, Any]] = {}
            missing: list[str] = []
            for row in unit["datasets"]:
                if row["action"] not in CONVERT_ACTIONS:
                    continue
                entry = started.get(row["id"])
                if entry is None:
                    missing.append(f"'{row['name']}'")
                else:
                    waits[row["id"]] = {"reconvert": row["action"] == ACTION_CONVERT_AGAIN, "job_id": entry["job_id"]}
            if missing:
                errors.append(f"No scan was made over {', '.join(missing)}: its conversion didn't start.")
                continue
            scan = _create_scan(scanner, unit, plan, waits)
            if not waits:
                _start_scan_job(scan)
            created.append(scan)

        states, slurm_error = _states_of(created)
        names = registry.dataset_names()
        return {
            "slurm_error": slurm_error,
            "scans": [_view(scan, states[scan["id"]], names) for scan in created],
            "conversions": list(started.values()),
            "errors": errors,
        }


# -------------------------------------------------------------------------
# Finished scans become results, and replaced ones go
# -------------------------------------------------------------------------


def _apply_result(scan: dict[str, Any]) -> None:
    """A scan that has succeeded becomes the current result of each of its datasets (D5), once.
    Call it while holding `_LOCK`. The registry is changed first and the record noted
    afterwards, so a crash in between is repaired by the next pass (setting a result is
    idempotent), rather than leaving a finished scan nobody ever picks up."""
    scanner = scanners.SCANNERS[scan["scanner"]]
    results = _scan_folder(scan["id"]) / scanners.RESULTS_FOLDER
    finished_at = _modified_at(results / scanners.SUCCESS_MARKER)
    versions = scan.get("versions") or {}
    for dataset_id in scan["datasets"]:
        version = versions.get(dataset_id)
        version_id = version.get("version_id") if isinstance(version, dict) else None
        if not isinstance(version_id, str) or jobs.BATCH_ID.fullmatch(version_id) is None:
            continue
        result = {
            "scan_id": scan["id"],
            "finished_at": finished_at,
            "mode": scan["mode"],
            "count": scanner.headline_count(results, scan["mode"], dataset_id, version_id),
        }
        try:
            registry.set_result(dataset_id, scan["scanner"], result, version_id)
        except registry.DatasetNotFound:
            continue
    scan["finished_at"] = finished_at
    scan["result_applied_at"] = jobs._now_iso()
    _write_scan(scan)


def _delete_unreferenced(scans: list[dict[str, Any]]) -> None:
    """Delete the folder of every scan that was a result and isn't any more: nothing points at
    it (D5, D6, D2). A scan that is some dataset's result, or that never became one (still
    going, failed or cancelled), stays."""
    applied = [scan for scan in scans if scan.get("result_applied_at") is not None]
    if not applied:
        return
    pointers = registry.result_pointers()
    for scan in applied:
        if scan["id"] in pointers:
            continue
        try:
            _delete_scan_folder(scan["id"])
        except ScanError as exc:
            _log(str(exc))


def _maintain() -> None:
    """Start the scans whose conversions have finished, make the scans that have finished the
    results of their datasets, and delete the scans that no longer are any dataset's result.
    Call it while holding `_LOCK`."""
    scans, _problems = _all_scans()
    pending = [scan for scan in scans if scan.get("result_applied_at") is None]
    states, _error = _states_of(pending)
    for scan in pending:
        state = states[scan["id"]]["state"]
        try:
            if state == STATE_STARTING:
                _start_scan_job(scan)
            elif state == STATE_SUCCEEDED:
                _apply_result(scan)
        except (ScanError, jobs.JobsError, jobs.SlurmUnavailable, registry.RegistryError, OSError) as exc:
            _log(f"scan {scan['id']}: {exc}")
    _delete_unreferenced(scans)


def refresh() -> None:
    """`_maintain`, unless a start or a cancel is in progress: the next read does it. It runs
    whenever scans or datasets are read, and every `REFRESH_SECONDS` in the background."""
    if not _LOCK.acquire(blocking=False):
        return
    try:
        _maintain()
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
    """Start the task that calls `refresh` every `REFRESH_SECONDS`, so a scan waiting for a
    conversion starts even when nobody is looking, and one left waiting when the service was
    stopped is picked up when it starts again. A daemon thread: it ends with the process."""
    threading.Thread(target=_refresh_forever, name="scan-refresh", daemon=True).start()


# -------------------------------------------------------------------------
# Watching
# -------------------------------------------------------------------------


def _view(
    scan: dict[str, Any],
    state: dict[str, Any],
    names: dict[str, str],
    detail: bool = False,
    slurm_error: str | None = None,
) -> dict[str, Any]:
    scanner = scanners.SCANNERS[scan["scanner"]]
    folder = _scan_folder(scan["id"])
    view: dict[str, Any] = {
        "id": scan["id"],
        "scanner": scan["scanner"],
        "label": scanner.label,
        "mode": scan["mode"],
        "datasets": [{"id": dataset_id, "name": names.get(dataset_id)} for dataset_id in scan["datasets"]],
        "state": state["state"],
        "reason": state["reason"],
        "waiting_for": state["waiting_for"],
        # SLURM's run time of the job. None for a scan without a job, and for one that already
        # became a result (SLURM isn't asked about those).
        "elapsed": state["elapsed"],
        "created_at": scan.get("created_at"),
        "submitted_at": scan.get("submitted_at"),
        "finished_at": scan.get("finished_at"),
        "cancelled_at": scan.get("cancelled_at"),
        "job_id": scan.get("job_id"),
        "settings": scan["settings"],
        "resources": scan["resources"],
        "conversions": scan["conversions"],
        "folder": str(folder),
        "is_result": scan.get("result_applied_at") is not None,
    }
    if not detail:
        return view

    view.update(
        slurm_error=slurm_error,
        versions=scan.get("versions"),
        command_text=scan.get("command_text"),
        wrapper_output=scan.get("wrapper_output"),
        details=None,
        logs=[],
    )
    if state["state"] == STATE_SUCCEEDED:
        view["details"] = scanner.read_details(folder / scanners.RESULTS_FOLDER)
    job_id = scan.get("job_id")
    if job_id is not None:
        # Nothing is looked for on disk while the job is queued (see the module docstring).
        for name, path in scanner.log_files(folder / scanners.LOGS_FOLDER, job_id):
            tail = jobs._tail_file(path) if state["state"] != STATE_QUEUED else None
            view["logs"].append({"name": name, "path": str(path), "tail": tail})
    return view


def list_scans() -> dict[str, Any]:
    """Every scan, newest first, with its state read live: the Activity page's list."""
    refresh()
    scans, problems = _all_scans()
    states, slurm_error = _states_of(scans)
    names = registry.dataset_names() if scans else {}
    return {
        "service_root": str(registry.service_root()),
        "slurm_error": slurm_error,
        "problems": problems,
        "scans": [_view(scan, states[scan["id"]], names) for scan in scans],
    }


def get_scan(scan_id: Any) -> dict[str, Any]:
    """One scan in full: its command, versions read, numbers (once finished) and log tails."""
    refresh()
    scan = _load_scan(scan_id)
    states, slurm_error = _states_of([scan])
    return _view(scan, states[scan["id"]], registry.dataset_names(), detail=True, slurm_error=slurm_error)


def dataset_summaries(datasets: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """For each dataset (as the registry returns it) and each scanner: its state, the scan, when
    it finished and the headline count. A scan that is going shows its own state. Otherwise the
    scan that is the dataset's result shows as `succeeded`, and otherwise the dataset's latest
    failed or cancelled attempt, if it is about the version the dataset has now."""
    scans, _problems = _all_scans()
    pending = [scan for scan in scans if scan.get("result_applied_at") is None]
    states, _error = _states_of(pending)

    def summary(dataset: dict[str, Any], key: str) -> dict[str, Any]:
        empty = {"state": STATE_NOT_SCANNED, "scan_id": None, "finished_at": None, "count": None, "mode": None, "reason": None}
        mine = [scan for scan in pending if scan["scanner"] == key and dataset["id"] in scan["datasets"]]  # newest first
        for scan in mine:
            state = states[scan["id"]]
            if state["state"] in HOLDING_STATES:
                return {**empty, "state": state["state"], "scan_id": scan["id"], "mode": scan["mode"], "reason": state["reason"]}
        result = dataset["results"].get(key)
        if isinstance(result, dict):
            return {**empty, **{name: result.get(name) for name in ("scan_id", "finished_at", "count", "mode")}, "state": STATE_SUCCEEDED}
        prepared = dataset["prepared"]
        for scan in mine:
            version = (scan.get("versions") or {}).get(dataset["id"])
            if isinstance(version, dict):
                current = isinstance(prepared, dict) and prepared.get("version_id") == version.get("version_id")
            else:
                # Never started: about the conversion the dataset didn't get.
                current = prepared is None
            if current:
                state = states[scan["id"]]
                return {**empty, "state": state["state"], "scan_id": scan["id"], "mode": scan["mode"], "reason": state["reason"]}
        return empty

    return {dataset["id"]: {key: summary(dataset, key) for key in scanners.SCANNERS} for dataset in datasets}


# -------------------------------------------------------------------------
# Cancelling, and the conversions and unregistering that scans hold up
# -------------------------------------------------------------------------


def cancel(scan_id: Any) -> dict[str, Any]:
    """Stop a scan. One that is still waiting for its conversions is stopped without ever
    starting, and the conversions keep running (cancel them too if they aren't wanted). One with
    a job gets `scancel` on the one job its record lists, if SLURM still has it."""
    with _LOCK:
        scan = _load_scan(scan_id)
        states, _error = _states_of([scan])
        state = states[scan["id"]]["state"]
        job_id = scan.get("job_id")
        if job_id is None:
            if state not in (STATE_WAITING, STATE_STARTING):
                raise ScanError(f"This scan is {state.replace('_', ' ')}, so there is nothing to cancel.")
            scan["cancelled_at"] = jobs._now_iso()
            _write_scan(scan)
            return {"scan_id": scan["id"], "cancelled": [], "not_active": [], "stopped_waiting": True}
        if job_id not in jobs.active_jobs():
            return {"scan_id": scan["id"], "cancelled": [], "not_active": [job_id], "stopped_waiting": False}
        result = jobs._run(["scancel", job_id])
        if result.returncode != 0:
            raise ScanError(f"scancel failed: {jobs._failure_detail(result)}")
        return {"scan_id": scan["id"], "cancelled": [job_id], "not_active": [], "stopped_waiting": False}


def refuse_if_scanning(items: list[dict[str, Any]]) -> None:
    """Refuse a request to convert datasets again (which deletes their Prepared versions) while
    a scan that waits for, or reads, one of them is going: the scan's data would go with it."""
    wanted = {item["id"] for item in items if item.get("reconvert")}
    if not wanted:
        return
    with _LOCK:
        held = _holding_scans()
    for dataset_id in sorted(wanted):
        for other in held.get(dataset_id, []):
            raise ScanError(
                f"A {scanners.SCANNERS[other['scanner']].label} scan ({other['scan_id']}) is "
                f"{other['state'].replace('_', ' ')} on dataset {dataset_id}, and converting again would delete the data it reads. "
                "Cancel the scan, or wait for it, first."
            )


def unregister(dataset_id: str) -> dict[str, Any]:
    """`conversions.unregister`, refused while a scan of the dataset is waiting, queued or
    running. Afterwards the scans that were only this dataset's, and the scans that were
    results of it and of nothing else, are deleted (D2); a scan shared with a dataset that is
    still registered stays."""
    registry.get_dataset(dataset_id)  # an unknown or malformed id fails here
    with _LOCK:
        scans, _problems = _all_scans()
        mine = [scan for scan in scans if dataset_id in scan["datasets"] and scan.get("result_applied_at") is None]
        states, _error = _states_of(mine)
        holding = [scan["id"] for scan in mine if states[scan["id"]]["state"] in HOLDING_STATES]
        if holding:
            raise ScanError(
                f"A scan of this dataset is waiting, queued or running ({', '.join(holding)}), or its state can't be read. "
                "Cancel it first."
            )
        removed = conversions.unregister(dataset_id)
        for scan in mine:
            # A failed or cancelled attempt over this dataset alone has nothing left to read.
            if scan["datasets"] == [dataset_id]:
                try:
                    _delete_scan_folder(scan["id"])
                except ScanError as exc:
                    _log(str(exc))
        _maintain()
    return removed
