"""Starting, watching, cancelling and replacing scans.

A scan is one start of one scanner (scanners.py) over one or more datasets. It lives in

    <service root>/scans/<scan id>/

with its record in `scan.json`, the files it is given in `inputs/`, its output in `results/`
and its logs in `_logs/`. The scan ID has the same shape as a batch ID (`jobs.BATCH_ID`).

How a scan starts (D3, D4): the chosen datasets are grouped into scans (one for all of them,
or one for each, as the scanner and the mode say). A dataset that isn't converted, or that
the user chose to convert again, gets a conversion job first (`conversions.submit`). The
scan's own job can only be made once every dataset has a Prepared version, so a scan that
has to wait is saved without a command, and `refresh` starts it as soon as its conversions
have finished. A conversion that fails or is cancelled means
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
  Every job states its partition, CPUs, memory and time limit: in its command, or, for a
  scanner whose job scripts fix them (scanners.Scanner.fixed_resources), in the script's own
  `#SBATCH` lines, with only the partition chosen.
* A scan can start more than one job: the NER scan's command submits a job that plans the work
  and then the GPU job that waits for it. The ones the scan's own job waits for are the
  record's `prior_jobs`. The scan is going while any of them is, has failed or been cancelled
  when one of them has, and has succeeded when its own job has and its scanner's success
  markers exist. Cancelling it runs `scancel` on all of them.
* A scan's own job can be an array job, with a task for each shard (the toxicity scan). squeue
  lists its tasks as `<id>_<task>` and sacct has a row for each, so the job is going while any
  task is listed, and it has failed when any task has (the reason names it). `scancel` on the
  array's ID stops every task. Its markers are not a fixed list: the scanner finds them from
  the results folder (`Scanner.markers_for`, one for each shard). `Scanner.read_progress` says
  how far it has got, and is only asked while the scan's own job is running.
* Job state is never stored. It is read live from SLURM and from the files a job writes. What
  is saved is what was started (settings, command, the versions read, job IDs) and, once, the
  fact that a finished scan became the result of its datasets (`result_applied_at`).
* A scan's success markers (`_SUCCESS`, the three of the regex PII scan, or a toxicity scan's
  manifest and the `_SUCCESS` of each shard) are only looked for once SLURM no longer lists
  its job, and the empty
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
JOB_ROLE = re.compile(r"^[a-z][a-z_]{0,19}$")

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
    # The jobs the scan's own job waits for. A record from before there were any has no list.
    prior_jobs = data.get("prior_jobs", [])
    if not isinstance(prior_jobs, list):
        return False
    for job in prior_jobs:
        if not isinstance(job, dict) or not isinstance(job.get("role"), str) or JOB_ROLE.fullmatch(job["role"]) is None:
            return False
        if not isinstance(job.get("job_id"), str) or SLURM_JOB_ID.fullmatch(job["job_id"]) is None:
            return False
    versions = data.get("versions")
    return (
        isinstance(data.get("settings"), dict)
        and isinstance(data.get("resources"), dict)
        and (versions is None or isinstance(versions, dict))
        # A record from before scans could drop what they flag has no `auto_drop`.
        and isinstance(data.get("auto_drop", False), bool)
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


def _jobs_of(scan: dict[str, Any]) -> list[dict[str, str]]:
    """Every SLURM job the scan started, in the order they were started: the ones its own job
    waits for (the NER scan plans its pieces of work in a first job), then its own."""
    started = [dict(job) for job in scan.get("prior_jobs") or []]
    if scan.get("job_id"):
        started.append({"role": scanners.MAIN_JOB_ROLE, "job_id": scan["job_id"]})
    return started


def reference_datasets_of(scan: dict[str, Any]) -> list[str]:
    """The datasets of a scan that it only read as a reference (the protected ones of a duplicates
    scan, D15), in the order it read them. The scan is never their result, and nothing in them is
    flagged in it."""
    scanner = scanners.SCANNERS[scan["scanner"]]
    if scanner.reference_datasets is None:
        return []
    references = set(scanner.reference_datasets(scan["settings"], scan["mode"]))
    return [dataset_id for dataset_id in scan["datasets"] if dataset_id in references]


# -------------------------------------------------------------------------
# The state of a scan, read live
# -------------------------------------------------------------------------


def _state(
    state: str,
    reason: str | None = None,
    waiting_for: list[dict[str, Any]] | None = None,
    elapsed: str | None = None,
    own_job_running: bool = False,
) -> dict[str, Any]:
    # `own_job_running` is whether the scan's own job (not a job it waits for) is running.
    return {
        "state": state,
        "reason": reason,
        "waiting_for": waiting_for or [],
        "elapsed": elapsed,
        "own_job_running": own_job_running,
    }


# A line of `sacct -P`: a job ("123"), one task of an array job ("123_4", or "123_[4-7]" while
# its tasks are pending), and the steps inside it ("123.batch", "123_4.0"), which are skipped.
SACCT_JOB = re.compile(r"^(\d+)(?:_(\[[^\]]*\]|\d+))?(?:\.(batch|extern|\d+))?$")


def _belongs(key: str, job_id: str) -> bool:
    """Whether a squeue ID is this job, or one of the tasks of this array job ("123_4")."""
    return key == job_id or key.startswith(f"{job_id}_")


def _is_listed(job_id: str, active: dict[str, dict[str, Any]]) -> bool:
    return any(_belongs(key, job_id) for key in active)


def _sacct_rows(job_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """`sacct` for these jobs: for each, one row for the job, or one for each task of an array
    job. With explicit IDs it returns jobs that finished on an earlier day too (checked on
    this cluster), so no start time is passed."""
    if not job_ids:
        return {}
    output = jobs._slurm(["sacct", "-j", ",".join(job_ids), "-n", "-P", "-o", "JobID,State,ExitCode,Elapsed"])
    wanted = set(job_ids)
    rows: dict[str, list[dict[str, Any]]] = {}
    for line in output.splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 4:
            continue
        match = SACCT_JOB.fullmatch(parts[0])
        if match is None or match.group(1) not in wanted or match.group(3) is not None:
            continue
        parent, task, _step = match.groups()
        state, exit_code, elapsed = parts[1], parts[2], parts[3]
        rows.setdefault(parent, []).append(
            {
                "task": task,
                "state": state.split()[0] if state else "",
                "state_text": state,
                "exit_code": exit_code,
                "elapsed": elapsed,
            }
        )
    return rows


def _read_slurm(
    job_ids: list[str],
) -> tuple[dict[str, dict[str, Any]] | None, dict[str, list[dict[str, Any]]] | None, str | None]:
    """`(squeue jobs, sacct rows, error)`. Either is None when it couldn't be read. sacct is only
    asked about jobs squeue doesn't list: for an array job, one of whose tasks squeue still
    lists, the job is still going. This is `jobs._read_slurm` made to understand array jobs."""
    try:
        active = jobs.active_jobs()
    except jobs.SlurmUnavailable as exc:
        return None, None, str(exc)
    try:
        return active, _sacct_rows([job_id for job_id in job_ids if not _is_listed(job_id, active)]), None
    except jobs.SlurmUnavailable as exc:
        return active, None, str(exc)


def _slurm_outcome(
    job_id: str,
    active: dict[str, dict[str, Any]] | None,
    accounted: dict[str, list[dict[str, Any]]] | None,
) -> tuple[str, str | None]:
    """`(state, reason)` of one SLURM job, taking all the tasks of an array job together.
    `active` and `accounted` are None when squeue or sacct couldn't be read. This is not
    jobs._decide_outcome, which is made for conversions (it looks for a progress.json, which a
    scan doesn't write)."""
    if active is None:
        return STATE_UNKNOWN, "SLURM couldn't be reached, so this job's state isn't known."
    listed = [value for key, value in active.items() if _belongs(key, job_id)]
    if listed:
        going = [item for item in listed if item["state"] != "PENDING"]
        if going:
            return STATE_RUNNING, going[0]["state"]
        if all("DependencyNeverSatisfied" in item["reason"] for item in listed):
            # A job it waits for didn't finish well, so it would sit in the queue for ever.
            return STATE_FAILED, "A job it waits for didn't finish well, so it will never start. Cancel it."
        return STATE_QUEUED, listed[0]["reason"]
    if accounted is None:
        return STATE_UNKNOWN, "SLURM couldn't be reached, so this job's state isn't known."
    tasks = accounted.get(job_id)
    if not tasks:
        return STATE_FAILED, "SLURM has no record of this job: it isn't queued, and accounting doesn't know it."
    still_going = [task for task in tasks if task["state"] in jobs.STILL_GOING_STATES]
    if still_going:
        return jobs.STILL_GOING_STATES[still_going[0]["state"]], f"SLURM accounting says {still_going[0]['state']}."
    bad = [task for task in tasks if task["state"] not in ("COMPLETED", "CANCELLED")]
    if bad:
        which = f"task {bad[0]['task']}: " if bad[0]["task"] is not None else ""
        return STATE_FAILED, f"{which}SLURM state {bad[0]['state_text'] or 'unknown'}, exit code {bad[0]['exit_code']}."
    cancelled = [task for task in tasks if task["state"] == "CANCELLED"]
    if cancelled:
        return STATE_CANCELLED, cancelled[0]["state_text"]
    return STATE_SUCCEEDED, None


def _slurm_elapsed(
    job_id: str,
    active: dict[str, dict[str, Any]] | None,
    accounted: dict[str, list[dict[str, Any]]] | None,
) -> str | None:
    """How long SLURM says one job has run ("1-02:03:04" or "0:42"): from squeue while it is
    listed, from sacct once it has left the queue. For an array job, the longest of its tasks.
    None when SLURM doesn't know."""
    listed = [value for key, value in (active or {}).items() if _belongs(key, job_id)]
    if listed:
        return max(listed, key=lambda item: item.get("elapsed_seconds") or 0)["elapsed"]
    tasks = (accounted or {}).get(job_id)
    if not tasks:
        return None
    return max(tasks, key=lambda task: jobs._parse_duration(task["elapsed"]) or 0)["elapsed"]


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
            # A scanner's jobs are only named after the scan when its command says so.
            job_name = f"scan_{scan['scanner']}_{scan['id']}"
            if any(job_name in part for part in scan.get("command") or []):
                where = f"Check `squeue` for a job named {job_name}."
            else:
                where = f"Check `squeue` for jobs submitted at {scan['submitted_at']}."
            return _state(STATE_FAILED, f"It was being started when the service stopped. {where}")
        if scan.get("cancelled_at"):
            return _state(STATE_CANCELLED, "Cancelled before it started.")
        return _waiting_state(scan, cache)

    state, reason = _slurm_outcome(job_id, active, accounted)
    elapsed = _slurm_elapsed(job_id, active, accounted)
    earlier = [
        (job["role"], job["job_id"], *_slurm_outcome(job["job_id"], active, accounted))
        for job in scan.get("prior_jobs") or []
    ]
    for role, earlier_id, earlier_state, earlier_reason in earlier:
        # The scan's own job waits for these. One that failed or was cancelled means the scan
        # did too, whatever became of the job waiting for it.
        if earlier_state in (STATE_FAILED, STATE_CANCELLED):
            what = "failed" if earlier_state == STATE_FAILED else "was cancelled"
            detail = f": {earlier_reason}" if earlier_reason else "."
            return _state(earlier_state, f"The {role} job ({earlier_id}) {what}{detail}", elapsed=elapsed)
    if state == STATE_QUEUED:
        for role, earlier_id, earlier_state, _earlier_reason in earlier:
            if earlier_state == STATE_RUNNING:
                # Queued behind a job that is running: the scan is going.
                return _state(STATE_RUNNING, f"The {role} job ({earlier_id}) is running.", elapsed=_slurm_elapsed(earlier_id, active, accounted))
    if state == STATE_SUCCEEDED:
        # SLURM no longer lists the job, so it is safe to look for the markers.
        scanner = scanners.SCANNERS[scan["scanner"]]
        results = _scan_folder(scan["id"]) / scanners.RESULTS_FOLDER
        missing = [marker for marker in scanners.markers_of(scanner, results) if not (results / marker).is_file()]
        if missing:
            # A scan with a marker for each shard can miss many: name the first few.
            shown = ", ".join(missing[:3]) + (f" and {len(missing) - 3} more" if len(missing) > 3 else "")
            return _state(
                STATE_FAILED,
                f"The job ended, but its results folder has no {shown}.",
                elapsed=elapsed,
            )
    return _state(state, reason, elapsed=elapsed, own_job_running=state == STATE_RUNNING)


def _states_of(scans: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], str | None]:
    """The state of each scan, by ID, and what SLURM said when it couldn't be read. One squeue
    and one sacct for all of them. A scan that became a result is finished: nothing is asked."""
    job_ids = [
        job["job_id"] for scan in scans if scan.get("result_applied_at") is None for job in _jobs_of(scan)
    ]
    active, accounted, slurm_error = _read_slurm(job_ids) if job_ids else ({}, {}, None)
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


def _effective_mode(scanner: scanners.Scanner, datasets: list[Any], mode: str | None) -> str | None:
    """The mode a request really makes: a single dataset is always compared with itself."""
    if scanner.modes is None:
        return None
    return scanners.MODE_ACROSS if mode == scanners.MODE_ACROSS and len(datasets) > 1 else scanners.MODE_WITHIN


def _units(
    scanner: scanners.Scanner, datasets: list[dict[str, Any]], mode: str | None, settings: dict[str, Any]
) -> list[dict[str, Any]]:
    """The scans a request makes (D4): each is a mode and the datasets it covers. Across
    datasets they are in the order the scan reads them in, which decides which copy counts as the
    original (D10, D15): by default the order of their registered paths, so the dataset that
    comes first by path holds it, or the order the scanner's `order_across` gives."""
    if scanner.modes is None:
        return [{"mode": None, "datasets": [dataset]} for dataset in datasets]
    if _effective_mode(scanner, datasets, mode) == scanners.MODE_ACROSS:
        if scanner.order_across is not None:
            ordered = scanner.order_across(settings, datasets)
        else:
            ordered = sorted(datasets, key=lambda item: (item["path"], item["id"]))
        return [{"mode": scanners.MODE_ACROSS, "datasets": ordered}]
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


def _describe_old_scan(old: dict[str, Any] | None, finished_at: Any, names: dict[str, str]) -> str:
    """"(across 'A', 'B', finished 2026-10-12)": how an earlier result was made, for a warning."""
    details: list[str] = []
    if old is not None and old.get("mode") == scanners.MODE_ACROSS and len(old["datasets"]) > 1:
        details.append("across " + ", ".join(f"'{names.get(dataset_id, dataset_id)}'" for dataset_id in old["datasets"]))
    elif old is not None and old.get("mode") == scanners.MODE_WITHIN:
        details.append("within this dataset only")
    if isinstance(finished_at, str) and finished_at:
        details.append(f"finished {finished_at[:10]}")
    return f" ({', '.join(details)})" if details else ""


def _replacement_warnings(
    scanner: scanners.Scanner, unit: dict[str, Any], settings: dict[str, Any], names: dict[str, str]
) -> list[str]:
    """What the scan will do to the datasets' current results, in words (the dialog shows them
    as warnings). A dataset has one current result for each scanner, so a new scan replaces it,
    and a dataset that was in the same earlier scan but isn't in this one keeps that result.
    Read-only: nothing is started or changed."""
    warnings: list[str] = []
    in_unit = [dataset["id"] for dataset in unit["datasets"]]
    references = set(scanner.reference_datasets(settings, unit["mode"])) if scanner.reference_datasets is not None else set()
    for dataset in unit["datasets"]:
        name = dataset["name"]
        if dataset["id"] in references:
            warnings.append(
                f"'{name}' is only read as a reference: its current {scanner.label} result stays as it is, and nothing in it is flagged."
            )
            continue
        current = (dataset.get("results") or {}).get(scanner.key)
        if not isinstance(current, dict):
            continue
        old_id = current.get("scan_id")
        old = _read_scan(old_id) if isinstance(old_id, str) and jobs.BATCH_ID.fullmatch(old_id) else None
        warnings.append(
            f"'{name}' already has a {scanner.label} result{_describe_old_scan(old, current.get('finished_at'), names)}. "
            "This scan replaces it."
        )
        if old is None or old.get("mode") != scanners.MODE_ACROSS or len(old["datasets"]) < 2:
            continue
        if unit["mode"] == scanners.MODE_WITHIN:
            warnings.append(f"'{name}' will no longer show {scanner.count_label} against the other datasets of that scan.")
        # A partner of the earlier scan that this one doesn't cover keeps what it had.
        for partner_id in old["datasets"]:
            if partner_id == dataset["id"] or partner_id in in_unit:
                continue
            try:
                kept = (registry.get_dataset(partner_id).get("results") or {}).get(scanner.key)
            except registry.DatasetNotFound:
                continue
            if not isinstance(kept, dict) or kept.get("scan_id") != old_id:
                continue
            partner = names.get(partner_id, partner_id)
            warnings.append(
                f"'{partner}' keeps its {scanner.label} result from that scan, which compared it with '{name}'. "
                "This scan does not change it."
            )
            if unit["mode"] == scanners.MODE_ACROSS:
                others = ", ".join(f"'{item['name']}'" for item in unit["datasets"] if item["id"] not in old["datasets"])
                if others:
                    warnings.append(f"'{partner}' is not compared with {others}.")
    return warnings


def _plan_unit(
    scanner: scanners.Scanner,
    unit: dict[str, Any],
    conversion_items: dict[str, dict[str, Any]],
    held: dict[str, list[dict[str, Any]]],
    settings: dict[str, Any],
    resources: dict[str, Any] | None,
    request_ok: bool,
    names: dict[str, str],
) -> dict[str, Any]:
    datasets_root = registry.service_root() / "datasets"
    blocked: list[str] = []
    warnings: list[str] = _replacement_warnings(scanner, unit, settings, names)
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
            plan["cost"] = scanner.cost(settings, resources)
            plan["needs_gpu_confirmation"] = bool(plan["cost"]["gpus"])
            plan["command_text"] = shlex.join(scanner.build_command(context))
        except scanners.ScannerError as exc:
            blocked.append(str(exc))
    return plan


def _check_auto_drop(scanner: scanners.Scanner, value: Any, problems: list[str]) -> bool:
    """Whether the scan is asked to drop what it flags by itself (D17). Only a scanner that offers
    it can be asked."""
    if value is None or value is False:
        return False
    if value is not True:
        problems.append("auto_drop must be true or false.")
        return False
    if not scanner.offers_auto_drop:
        problems.append(f"{scanner.label} can't drop what it flags by itself.")
        return False
    return True


def _check_dataset_settings(
    scanner: scanners.Scanner, settings: dict[str, Any], mode: str | None, dataset_ids: list[str], problems: list[str]
) -> None:
    """A setting that is a list of datasets may only name datasets of this request, and the
    scanner can have more to say about how its settings fit them."""
    for field in scanner.fields:
        if field["type"] != "datasets":
            continue
        stray = [dataset_id for dataset_id in settings[field["name"]] if dataset_id not in dataset_ids]
        if stray:
            problems.append(f"{field['label']} names {len(stray)} dataset(s) that aren't among the ones chosen.")
    if scanner.check_datasets is not None:
        scanner.check_datasets(settings, mode, dataset_ids, problems)


def _plan(
    scanner_key: Any, items_in: Any, mode_in: Any, settings_in: Any, resources_in: Any, auto_drop_in: Any = False
) -> dict[str, Any]:
    """Everything the check shows, and everything `submit` does. Read-only. What is wrong with
    the request as a whole is in `blocked`, and what is wrong with one scan in that scan's own
    `blocked`; nothing can be started while either isn't empty."""
    scanner = _scanner(scanner_key)
    items = _validated_items(items_in)
    problems: list[str] = []
    mode = _check_mode(scanner, mode_in, problems)
    settings = scanners.parse_settings(scanner, settings_in, problems)
    resources = scanners.parse_resources(scanner, settings, resources_in, problems)
    auto_drop = _check_auto_drop(scanner, auto_drop_in, problems)
    _check_dataset_settings(scanner, settings, _effective_mode(scanner, items, mode), [item["id"] for item in items], problems)

    # D3 is already worked out by the conversions' own check, for one dataset at a time.
    conversion_plan = conversions.check(items, jobs.DEFAULT_RESOURCES)
    conversion_items = {item["dataset_id"]: item for item in conversion_plan["items"]}
    if conversion_plan["slurm_error"]:
        problems.append(conversion_plan["slurm_error"])
    datasets = [registry.get_dataset(item["id"]) for item in items]
    held = _holding_scans()
    names = registry.dataset_names()

    units = [
        _plan_unit(scanner, unit, conversion_items, held, settings, resources, not problems, names)
        for unit in _units(scanner, datasets, mode, settings)
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
        "auto_drop": auto_drop,
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


def check(
    scanner_key: Any, items_in: Any, mode_in: Any, settings_in: Any, resources_in: Any, auto_drop_in: Any = False
) -> dict[str, Any]:
    """The plan for a request: for each scan it would make, each dataset's conversion, the
    command (with `<version-id>` and `<scan-id>` where those aren't known yet), the cost and
    what stops it. Starts and deletes nothing."""
    return _plan(scanner_key, items_in, mode_in, settings_in, resources_in, auto_drop_in)


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
        prior, job_id = scanner.read_jobs(result.stdout)
        # Kept even when the command failed: a job it started first is still there.
        scan["prior_jobs"] = [job for job in prior if SLURM_JOB_ID.fullmatch(job["job_id"])]
        if result.returncode != 0:
            scan["submit_error"] = f"The command that starts the scan failed: {jobs._failure_detail(result)}"
        elif job_id is None or SLURM_JOB_ID.fullmatch(job_id) is None:
            scan["submit_error"] = f"The command that starts the scan printed something unexpected: {result.stdout.strip()!r}"
        else:
            scan["job_id"] = job_id
        if scan.get("submit_error") and scan["prior_jobs"]:
            started = ", ".join(f"the {job['role']} job {job['job_id']}" for job in scan["prior_jobs"])
            scan["submit_error"] += f" It had already submitted {started}, which may still be running: stop it with scancel."
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
        # Whether the scan drops what it flags by itself once it has finished and its coverage
        # check has passed (D17). What came of that is in the scan's auto_drop.json (autodrop.py).
        "auto_drop": plan["auto_drop"],
        "created_at": jobs._now_iso(),
        # Only the datasets that were converted for this scan: the others are used as they are.
        "conversions": waits,
        "cancelled_at": None,
        # Everything from here down is filled in when the scan's own job is started.
        "versions": None,
        "command": None,
        "command_text": None,
        "prior_jobs": [],
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
    scanner_key: Any,
    items_in: Any,
    mode_in: Any,
    settings_in: Any,
    resources_in: Any,
    confirm_gpu: bool,
    auto_drop_in: Any = False,
) -> dict[str, Any]:
    """Start the conversions the datasets need, and the scans. The check is made again under
    the lock, and the whole request is refused if any scan can't be started. A scan whose
    datasets are all converted is started now; any other waits for its conversions."""
    with _LOCK:
        _maintain()
        plan = _plan(scanner_key, items_in, mode_in, settings_in, resources_in, auto_drop_in)
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
    """A scan that has succeeded becomes the current result of each of its datasets (D5), once,
    except the datasets it only read as a reference (D15). Call it while holding `_LOCK`. The registry is changed first and the record noted
    afterwards, so a crash in between is repaired by the next pass (setting a result is
    idempotent), rather than leaving a finished scan nobody ever picks up."""
    scanner = scanners.SCANNERS[scan["scanner"]]
    results = _scan_folder(scan["id"]) / scanners.RESULTS_FOLDER
    # The last marker written is when the scan was complete.
    stamps = [_modified_at(results / marker) for marker in scanners.markers_of(scanner, results)]
    finished_at = max((stamp for stamp in stamps if stamp is not None), default=None)
    versions = scan.get("versions") or {}
    references = reference_datasets_of(scan)
    for dataset_id in scan["datasets"]:
        if dataset_id in references:
            # Read only so that rows in the others can match it: its own result stays as it was.
            continue
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
        # The scan's own job, and all its jobs (the ones it waits for first) with their roles.
        "job_id": scan.get("job_id"),
        "jobs": _jobs_of(scan),
        "settings": scan["settings"],
        "resources": scan["resources"],
        "auto_drop": bool(scan.get("auto_drop")),
        "conversions": scan["conversions"],
        "folder": str(folder),
        "is_result": scan.get("result_applied_at") is not None,
        # How far it has got, per stage, for a scanner that can tell. Only looked for while the
        # scan's own job is running: nothing is looked for on disk before that (see the module
        # docstring), when a folder asked for early is remembered as "not there".
        "progress": (
            scanner.read_progress(folder / scanners.RESULTS_FOLDER)
            if state["own_job_running"] and scanner.read_progress is not None
            else None
        ),
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
    started = _jobs_of(scan)
    if started:
        # Nothing is looked for on disk while the job is queued (see the module docstring).
        for name, path in scanner.log_files(folder, scan["settings"], started):
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
    by_id = {scan["id"]: scan for scan in scans}
    names = registry.dataset_names()

    def context_of(scan: dict[str, Any] | None, dataset_id: str) -> dict[str, Any]:
        """What the scan compared this dataset with: the other datasets it read, in the order it
        read them (a name is None when the dataset isn't registered any more), and the
        strictness it was started with (only the duplicates scan has one)."""
        if scan is None:
            return {"compared_with": [], "strictness": None}
        others = [{"id": other, "name": names.get(other)} for other in scan["datasets"] if other != dataset_id]
        strictness = scan["settings"].get("strictness")
        return {"compared_with": others, "strictness": strictness if isinstance(strictness, str) else None}

    def summary(dataset: dict[str, Any], key: str) -> dict[str, Any]:
        empty = {
            "state": STATE_NOT_SCANNED,
            "scan_id": None,
            "finished_at": None,
            "count": None,
            "mode": None,
            "reason": None,
            # "pass" or "fail" once the result's coverage check has been made (results.py).
            "coverage": None,
            # The other datasets the scan compared this one with, and its strictness.
            "compared_with": [],
            "strictness": None,
        }
        mine = [scan for scan in pending if scan["scanner"] == key and dataset["id"] in scan["datasets"]]  # newest first
        for scan in mine:
            state = states[scan["id"]]
            if state["state"] in HOLDING_STATES:
                return {
                    **empty,
                    "state": state["state"],
                    "scan_id": scan["id"],
                    "mode": scan["mode"],
                    "reason": state["reason"],
                    **context_of(scan, dataset["id"]),
                }
        result = dataset["results"].get(key)
        if isinstance(result, dict):
            return {
                **empty,
                **{name: result.get(name) for name in ("scan_id", "finished_at", "count", "mode", "coverage")},
                "state": STATE_SUCCEEDED,
                **context_of(by_id.get(result.get("scan_id")), dataset["id"]),
            }
        prepared = dataset["prepared"]
        for scan in mine:
            if dataset["id"] in reference_datasets_of(scan):
                continue  # only read for the others: its failure says nothing about this dataset
            version = (scan.get("versions") or {}).get(dataset["id"])
            if isinstance(version, dict):
                current = isinstance(prepared, dict) and prepared.get("version_id") == version.get("version_id")
            else:
                # Never started: about the conversion the dataset didn't get.
                current = prepared is None
            if current:
                state = states[scan["id"]]
                return {
                    **empty,
                    "state": state["state"],
                    "scan_id": scan["id"],
                    "mode": scan["mode"],
                    "reason": state["reason"],
                    **context_of(scan, dataset["id"]),
                }
        return empty

    return {dataset["id"]: {key: summary(dataset, key) for key in scanners.SCANNERS} for dataset in datasets}


# -------------------------------------------------------------------------
# Cancelling, and the conversions and unregistering that scans hold up
# -------------------------------------------------------------------------


def cancel(scan_id: Any) -> dict[str, Any]:
    """Stop a scan. One that is still waiting for its conversions is stopped without ever
    starting, and the conversions keep running (cancel them too if they aren't wanted). One with
    jobs gets `scancel` on the jobs its record lists (and only those), the ones SLURM still has."""
    with _LOCK:
        scan = _load_scan(scan_id)
        states, _error = _states_of([scan])
        state = states[scan["id"]]["state"]
        job_ids = [job["job_id"] for job in _jobs_of(scan)]
        if not job_ids:
            if state not in (STATE_WAITING, STATE_STARTING):
                raise ScanError(f"This scan is {state.replace('_', ' ')}, so there is nothing to cancel.")
            scan["cancelled_at"] = jobs._now_iso()
            _write_scan(scan)
            return {"scan_id": scan["id"], "cancelled": [], "not_active": [], "stopped_waiting": True}
        active = jobs.active_jobs()
        # `scancel <array job ID>` stops every task of an array job, so the job is going while
        # any of its tasks is listed.
        going = [job_id for job_id in job_ids if _is_listed(job_id, active)]
        not_active = [job_id for job_id in job_ids if job_id not in going]
        if not going:
            return {"scan_id": scan["id"], "cancelled": [], "not_active": not_active, "stopped_waiting": False}
        result = jobs._run(["scancel", *going])
        if result.returncode != 0:
            raise ScanError(f"scancel failed: {jobs._failure_detail(result)}")
        return {"scan_id": scan["id"], "cancelled": going, "not_active": not_active, "stopped_waiting": False}


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
