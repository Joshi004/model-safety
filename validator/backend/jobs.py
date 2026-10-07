"""Starting, watching, cancelling and deleting `prepare_rows.py` conversions.

This is the one module in the backend that acts on anything: it submits SLURM
jobs (`sbatch`), cancels them (`scancel`), and deletes run folders. Every other
module here stays read-only.

Rules this module keeps:

* Every `sbatch`, `squeue`, `sacct`, `sinfo` and `scancel` call is an argument
  list run with `shell=False` and a 30 second timeout.
* Every job states its partition, CPUs, memory and time limit. Nothing here
  ever falls back to SLURM's defaults, and nothing asks for a GPU, because on
  this cluster a job without `--mem` is given a whole node's memory.
* A run folder is never touched before the job starts: logs and batch records
  live under `<runs root>/_jobs`, outside every run folder, because a folder
  that already holds any file is refused by the run identity check.
* Deleting only happens inside the runs root, after checking every folder in
  the request. Job state is never stored: it's read live from SLURM and from
  the run folders, so restarting the backend loses nothing.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import detect
import preview
import runs
from reader_imports import (
    IDENTITY_FILENAME,
    ORIGIN_KEY,
    PROGRESS_END_PHASES,
    REPO_ROOT,
    RunIdentityError,
    build_parser,
    compare_run_identity,
    discover_input_files,
    run_identity,
)

DEFAULT_RESOURCES = {"partition": "health", "cpus": 2, "mem": "32G", "time": "04:00:00"}
MAX_CPUS = 32
SLURM_TIMEOUT_SECONDS = 30
JOB_SCRIPT = "launchers/slurm/prepare_rows.sbatch"
JOB_NAME_SLUG_CHARS = 50
LOG_TAIL_LINES = 40
LOG_TAIL_BYTES = 64 * 1024
SUCCESS_MARKER = "_SUCCESS"

BATCH_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
FOLDER_PART = re.compile(r"^[A-Za-z0-9._-]+$")
MEMORY = re.compile(r"^(\d+)([MG])$")
TIME_PLAIN = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")
TIME_WITH_DAYS = re.compile(r"^(\d{1,3})-([01]?\d|2[0-3]):([0-5]\d):([0-5]\d)$")
MAX_RSS = re.compile(r"^(\d+(?:\.\d+)?)([KMGTP]?)$")

# A planned run in one of these can't be started as it is.
BLOCKING_STATUSES = ("refused", "outside_root", "inside_run", "running", "different", "no_identity")
# Outcomes of a job that's still going.
ACTIVE_OUTCOMES = ("queued", "running", "stalled")
# What sacct says for a job squeue no longer lists but accounting hasn't closed.
STILL_GOING_STATES = {
    "PENDING": "queued",
    "REQUEUED": "queued",
    "RUNNING": "running",
    "COMPLETING": "running",
    "CONFIGURING": "running",
    "SUSPENDED": "running",
    "RESIZING": "running",
}

# Submit and delete share this lock. FastAPI runs these handlers in a thread
# pool, so without it two quick clicks could start two jobs into one folder,
# or delete a folder while a submit is checking it.
_LOCK = threading.Lock()


class JobsError(ValueError):
    """The request can't be carried out; the message says why."""


class SlurmUnavailable(RuntimeError):
    """A SLURM command couldn't be run, timed out, or failed."""


# -------------------------------------------------------------------------
# Small helpers
# -------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _write_json_atomic(path: Path, data: Any) -> None:
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def _parse_time(value: Any) -> datetime | None:
    """A UTC ISO time from a file, or None when it isn't one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_duration(text: str) -> int | None:
    """Seconds in squeue's `[D-][[HH:]MM:]SS` time used, or None."""
    try:
        days = 0
        if "-" in text:
            day_text, text = text.split("-", 1)
            days = int(day_text)
        seconds = 0
        for part in text.split(":"):
            seconds = seconds * 60 + int(part)
        return days * 86400 + seconds
    except ValueError:
        return None


def _parse_max_rss(text: str) -> int | None:
    """Bytes in sacct's MaxRSS ("14368K", unit K when none is given)."""
    match = MAX_RSS.fullmatch(text)
    if match is None:
        return None
    return int(float(match.group(1)) * 1024 ** ("KMGTP".index(match.group(2) or "K") + 1))


def _time_limit_seconds(text: str) -> int | None:
    """Seconds in `HH:MM:SS` or `D-HH:MM:SS`, or None when it isn't one."""
    match = TIME_PLAIN.fullmatch(text)
    if match is not None:
        hours, minutes, seconds = (int(part) for part in match.groups())
        return hours * 3600 + minutes * 60 + seconds
    match = TIME_WITH_DAYS.fullmatch(text)
    if match is not None:
        days, hours, minutes, seconds = (int(part) for part in match.groups())
        return days * 86400 + hours * 3600 + minutes * 60 + seconds
    return None


def _number(summary: dict[str, Any], section: str, key: str) -> int | None:
    block = summary.get(section)
    value = block.get(key) if isinstance(block, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _read_summary(folder: Path) -> dict[str, Any] | None:
    """The folder's `summary.json` when `prepare_rows.py` wrote it."""
    summary = _read_json(folder / "summary.json")
    if isinstance(summary, dict) and summary.get("tool") == runs.PREPARE_ROWS_TOOL:
        return summary
    return None


def _summary_view(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    checks = summary.get("checks")
    return {
        "created_at": summary.get("created_at"),
        "rows_read": _number(summary, "source", "rows_read"),
        "written": _number(summary, "rows", "written"),
        "skipped": _number(summary, "rows", "skipped"),
        "checks_passed": isinstance(checks, dict) and checks.get("passed") is True,
    }


def folder_contents(folder: Path) -> dict[str, Any]:
    """What a folder holds: file count, total size, and its run summary if any."""
    file_count = 0
    total_bytes = 0
    for current, _dir_names, file_names in os.walk(folder):
        for name in file_names:
            file_count += 1
            try:
                total_bytes += os.lstat(os.path.join(current, name)).st_size
            except OSError:
                pass
    return {
        "file_count": file_count,
        "total_bytes": total_bytes,
        "summary": _summary_view(_read_summary(folder)),
    }


# -------------------------------------------------------------------------
# SLURM
# -------------------------------------------------------------------------


def _run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            shell=False,
            timeout=SLURM_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        raise SlurmUnavailable(
            f"SLURM couldn't be reached: {argv[0]} didn't answer within {SLURM_TIMEOUT_SECONDS} seconds."
        ) from exc
    except OSError as exc:
        raise SlurmUnavailable(f"SLURM couldn't be reached: {argv[0]} couldn't be run ({exc}).") from exc


def _failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    return result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"


def _slurm(argv: list[str]) -> str:
    """Standard output of a SLURM command that has to succeed."""
    result = _run(argv)
    if result.returncode != 0:
        raise SlurmUnavailable(f"SLURM couldn't be reached: {argv[0]} failed ({_failure_detail(result)}).")
    return result.stdout


def list_partitions() -> list[str]:
    """Partition names from `sinfo`; its `*` marks the default partition and
    isn't part of the name."""
    output = _slurm(["sinfo", "-h", "-o", "%P"])
    names = {line.strip().rstrip("*") for line in output.splitlines() if line.strip()}
    if not names:
        raise SlurmUnavailable("SLURM couldn't be reached: sinfo listed no partitions.")
    return sorted(names)


def active_jobs() -> dict[str, dict[str, Any]]:
    """The current user's jobs that squeue lists, in any state, by job ID.

    The filtering by job ID happens in the callers, not with `squeue -j`:
    squeue exits 1 for a job ID it no longer knows, which would look like
    SLURM being down.
    """
    output = _slurm(["squeue", "-h", "-u", getpass.getuser(), "-o", "%i|%T|%r|%M"])
    jobs: dict[str, dict[str, Any]] = {}
    for line in output.splitlines():
        parts = line.strip().split("|", 3)
        if len(parts) != 4:
            continue
        job_id, state, reason, elapsed = parts
        jobs[job_id] = {
            "state": state,
            "reason": reason,
            "elapsed": elapsed,
            "elapsed_seconds": _parse_duration(elapsed),
        }
    return jobs


def accounting(job_ids: list[str]) -> dict[str, dict[str, Any]]:
    """One `sacct` call for these job IDs. With explicit IDs it returns jobs
    that finished on an earlier day too (checked on this cluster), so no start
    time is passed."""
    if not job_ids:
        return {}
    output = _slurm(
        ["sacct", "-j", ",".join(job_ids), "-n", "-P", "-o", "JobID,State,ExitCode,Elapsed,MaxRSS"]
    )
    wanted = set(job_ids)
    records: dict[str, dict[str, Any]] = {}
    peak_memory: dict[str, int | None] = {}
    for line in output.splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 5:
            continue
        job_id, state, exit_code, elapsed, max_rss = parts
        if job_id in wanted:
            records[job_id] = {
                "state": state.split()[0] if state else "",
                "state_text": state,
                "exit_code": exit_code,
                "elapsed": elapsed,
                "max_rss_bytes": None,
            }
        elif job_id.endswith(".batch") and job_id[: -len(".batch")] in wanted:
            peak_memory[job_id[: -len(".batch")]] = _parse_max_rss(max_rss)
    for job_id, memory in peak_memory.items():
        if job_id in records:
            records[job_id]["max_rss_bytes"] = memory
    return records


def _read_slurm(
    job_ids: list[str],
) -> tuple[dict[str, dict[str, Any]] | None, dict[str, dict[str, Any]] | None, str | None]:
    """`(squeue jobs, sacct records, error)`. Either is None when it couldn't
    be read; sacct is only asked about jobs squeue doesn't list."""
    try:
        active = active_jobs()
    except SlurmUnavailable as exc:
        return None, None, str(exc)
    try:
        records = accounting([job_id for job_id in job_ids if job_id not in active])
    except SlurmUnavailable as exc:
        return active, None, str(exc)
    return active, records, None


# -------------------------------------------------------------------------
# Resources
# -------------------------------------------------------------------------


def validate_resources(raw: dict[str, Any] | None) -> dict[str, Any]:
    """The partition, CPUs, memory and time limit, checked. Raises JobsError
    listing everything wrong. Nothing is ever defaulted."""
    raw = raw or {}
    problems: list[str] = []

    partition = raw.get("partition")
    if isinstance(partition, str) and partition.strip():
        partition = partition.strip()
    else:
        partition = None
        problems.append("Choose a partition.")

    cpus = raw.get("cpus")
    if isinstance(cpus, bool) or not isinstance(cpus, int) or not 1 <= cpus <= MAX_CPUS:
        problems.append(f"CPUs must be a whole number from 1 to {MAX_CPUS}.")

    mem = raw.get("mem")
    memory_match = MEMORY.fullmatch(mem.strip().upper()) if isinstance(mem, str) else None
    if memory_match is None or int(memory_match.group(1)) == 0:
        problems.append(
            "Memory must be a whole number of M or G above zero, such as 32G "
            "(never 0: SLURM reads that as the whole node)."
        )
    else:
        mem = f"{int(memory_match.group(1))}{memory_match.group(2)}"

    time_limit = raw.get("time")
    limit_seconds = _time_limit_seconds(time_limit.strip()) if isinstance(time_limit, str) else None
    if not limit_seconds:
        problems.append("The time limit must be HH:MM:SS or D-HH:MM:SS, and above zero.")
    else:
        time_limit = time_limit.strip()

    if partition is not None:
        partitions = list_partitions()
        if partition not in partitions:
            problems.append(f"Partition '{partition}' isn't one sinfo lists ({', '.join(partitions)}).")

    if problems:
        raise JobsError(" ".join(problems))
    return {"partition": partition, "cpus": cpus, "mem": mem, "time": time_limit}


def sbatch_args(resources: dict[str, Any], job_name: str, args: list[str], root: Path) -> list[str]:
    """The exact `sbatch` call (run from the repo root) for one run."""
    logs = root / runs.JOBS_FOLDER / "logs"
    return [
        "sbatch",
        "--parsable",
        f"--partition={resources['partition']}",
        f"--cpus-per-task={resources['cpus']}",
        f"--mem={resources['mem']}",
        f"--time={resources['time']}",
        f"--job-name={job_name}",
        f"--output={logs}/%j.out",
        f"--error={logs}/%j.err",
        f"--export=ALL,PREPARE_REPO={REPO_ROOT}",
        JOB_SCRIPT,
        *args,
    ]


# -------------------------------------------------------------------------
# Folder names and where a folder stands
# -------------------------------------------------------------------------


def slug(text: str) -> str:
    """Lowercase, runs of anything outside a-z 0-9 . _ - become one `_`,
    `_` `.` `-` trimmed from both ends; `dataset` if nothing is left."""
    cleaned = re.sub(r"[^a-z0-9._-]+", "_", text.lower()).strip("_.-")
    return cleaned or "dataset"


def suggested_folder(planned: dict[str, Any]) -> str:
    """`<team>/<group>/<label>` for a catalog entry, otherwise
    `custom/<last part of the input path>`."""
    team, group, label = (planned.get(key) for key in ("team", "group", "label"))
    if team and group and label:
        return "/".join(slug(part) for part in (team, group, label))
    last_part = Path((planned.get("input_path") or "").strip().rstrip("/")).name
    return f"custom/{slug(last_part)}"


def display_name(planned: dict[str, Any]) -> str:
    """What a planned run is called: its catalog label, or for a custom path
    the last part of the path."""
    label = (planned.get("label") or "").strip()
    if label:
        return label
    input_path = (planned.get("input_path") or "").strip()
    return Path(input_path.rstrip("/")).name or input_path or "(no name)"


def check_folder_name(name: str) -> tuple[str, str] | None:
    """None for a usable folder name (relative to the runs root), otherwise
    `(status, reason)`."""
    if not name.strip():
        return "refused", "The folder name is empty."
    if name.startswith("/"):
        return "outside_root", "The folder name must be relative to the runs root, not an absolute path."
    parts = name.split("/")
    for part in parts:
        if part in (".", ".."):
            return "outside_root", f"'{part}' isn't allowed in a folder name: it could leave the runs root."
        if not FOLDER_PART.fullmatch(part):
            return "refused", (
                f"'{part}' isn't a valid folder name part: use only letters, digits, '.', '_' and '-', "
                "with no empty parts."
            )
    if parts[0] == runs.JOBS_FOLDER:
        return "outside_root", f"'{runs.JOBS_FOLDER}' is reserved for job logs and records."
    return None


def _check_location(path: Path, root: Path) -> tuple[str, str] | None:
    """None when `path` (already normalised) is a real folder strictly inside
    the runs root and not under `_jobs`, otherwise `(status, reason)`."""
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return "outside_root", f"{path} isn't inside the runs root {root}."
    if not relative.parts:
        return "outside_root", f"{path} is the runs root itself."
    if relative.parts[0] == runs.JOBS_FOLDER:
        return "outside_root", f"{path} is under {runs.JOBS_FOLDER}, which holds job logs and records."
    if resolved != path:
        return "refused", f"{path} is, or sits under, a symbolic link (it resolves to {resolved})."
    return None


def _running_reason(run_dir: Path, active_dirs: dict[str, str]) -> tuple[str | None, str | None]:
    """`(reason, batch_id)` when a job is writing into `run_dir`: a queued or
    running job of a batch, or an unfinished `progress.json` updated lately."""
    batch_id = active_dirs.get(str(run_dir))
    if batch_id is not None:
        return f"A job for this folder is queued or running (batch {batch_id}).", batch_id
    info = runs.run_progress(run_dir)
    progress = info["progress"]
    if progress is not None and progress["phase"] not in PROGRESS_END_PHASES and not info["may_have_stopped"]:
        return "Its progress.json shows a run that hasn't finished and updated in the last 2 minutes.", None
    return None, None


# -------------------------------------------------------------------------
# Batch records: <root>/_jobs/batches/<batch_id>.json
# -------------------------------------------------------------------------


def _batches_folder(root: Path) -> Path:
    return root / runs.JOBS_FOLDER / "batches"


def _logs_folder(root: Path) -> Path:
    return root / runs.JOBS_FOLDER / "logs"


def _is_batch(data: Any) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("batch_id"), str)
        and isinstance(data.get("jobs"), list)
        and all(isinstance(job, dict) for job in data["jobs"])
    )


def _load_batches(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Every batch record, newest first, and a note for each unreadable one."""
    folder = _batches_folder(root)
    batches: list[dict[str, Any]] = []
    problems: list[str] = []
    if not folder.is_dir():
        return batches, problems
    for path in sorted(folder.glob("*.json"), reverse=True):
        if not BATCH_ID.fullmatch(path.stem):
            continue
        data = _read_json(path)
        if _is_batch(data):
            batches.append(data)
        else:
            problems.append(f"{path.name} couldn't be read as a batch record.")
    return batches, problems


def load_batch(root: Path, batch_id: str) -> dict[str, Any]:
    # The ID becomes part of a file name, so only the exact format is let through.
    if not isinstance(batch_id, str) or not BATCH_ID.fullmatch(batch_id):
        raise JobsError(f"'{batch_id}' isn't a batch ID.")
    data = _read_json(_batches_folder(root) / f"{batch_id}.json")
    if not _is_batch(data):
        raise JobsError(f"There's no readable batch {batch_id} under {root}.")
    return data


def _recorded_job_ids(batches: list[dict[str, Any]]) -> list[str]:
    return [
        job["slurm_job_id"]
        for batch in batches
        for job in batch["jobs"]
        if isinstance(job.get("slurm_job_id"), str) and job["slurm_job_id"].isdigit()
    ]


def _active_run_dirs(root: Path, active: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Run folder -> batch ID, for every folder a queued or running job of a
    batch writes into."""
    if not active:
        return {}
    folders: dict[str, str] = {}
    for batch in _load_batches(root)[0]:
        for job in batch["jobs"]:
            if job.get("slurm_job_id") in active and isinstance(job.get("run_dir"), str):
                folders[job["run_dir"]] = batch["batch_id"]
    return folders


# -------------------------------------------------------------------------
# Checking planned runs
# -------------------------------------------------------------------------


def _prepare_args_for(planned: dict[str, Any], run_dir: Path) -> list[str]:
    """The `prepare_rows.py` arguments for a planned run, or JobsError when
    the input or mapping couldn't be used."""
    try:
        input_path = detect.resolve_input_path(planned.get("input_path") or "")
    except detect.InvalidPath as exc:
        raise JobsError(str(exc)) from exc
    if not input_path.exists():
        raise JobsError(f"Input not found: {input_path}")
    mapping = planned.get("map") or {}
    for target, source in mapping.items():
        if not target.strip() or not source.strip():
            raise JobsError(f"Invalid mapping '{target}={source}'. Expected target=source.")
        if target.strip() == ORIGIN_KEY:
            raise JobsError(
                f"'{ORIGIN_KEY}' can't be a mapping target: the preparing step adds its own '{ORIGIN_KEY}' "
                "note to every row."
            )
    return preview.prepare_args(
        input_path=str(input_path),
        input_type=planned.get("input_type") or "",
        file=planned.get("file"),
        output_dir=str(run_dir),
        mapping=mapping,
        required=planned.get("require") or [],
        exclude_metadata=planned.get("exclude_metadata") or [],
        split=planned.get("split") or "train",
        hf_config=planned.get("hf_config") or "",
    )


def _identity_for(args: list[str]) -> dict[str, Any]:
    """The `run_identity.json` content a run with these arguments would write,
    worked out the same way `prepare_rows.py` does it, without reading data."""
    parser = build_parser()

    def refuse(message: str) -> None:
        raise JobsError(f"prepare_rows.py wouldn't accept these arguments: {message}")

    # argparse would otherwise print to stderr and exit the whole backend.
    parser.error = refuse  # type: ignore[method-assign]
    parsed = parser.parse_args(args)
    try:
        return run_identity(parsed, discover_input_files(parsed))
    except Exception as exc:  # a real reader failure -- say so, don't 500 blindly
        raise JobsError(f"Can't read the input: {type(exc).__name__}: {exc}") from exc


def _plan_one(
    root: Path,
    planned: dict[str, Any],
    resources: dict[str, Any] | None,
    active_dirs: dict[str, str],
) -> dict[str, Any]:
    label = display_name(planned)
    folder = planned.get("run_folder")
    if folder is None:
        folder = suggested_folder(planned)
    result: dict[str, Any] = {
        "label": label,
        "folder": folder,
        "run_dir": None,
        "status": "refused",
        "reason": None,
        "differences": [],
        "contents": None,
        "batch_id": None,
        "prepare_args": None,
        "command": None,
        "sbatch_command": None,
    }

    problem = check_folder_name(folder)
    if problem is not None:
        result["status"], result["reason"] = problem
        return result
    run_dir = root / folder
    result["run_dir"] = str(run_dir)

    input_error = None
    try:
        args = _prepare_args_for(planned, run_dir)
    except JobsError as exc:
        args, input_error = None, str(exc)
    if args is not None:
        result["prepare_args"] = args
        result["command"] = (
            f"{preview.READERS_PYTHON} {preview.PREPARE_ROWS_SCRIPT} {shlex.join(args)}"
        )
        if resources is not None:
            job_name = f"prepare_{slug(label)[:JOB_NAME_SLUG_CHARS]}"
            result["sbatch_command"] = shlex.join(sbatch_args(resources, job_name, args, root))

    problem = _check_location(run_dir, root)
    if problem is not None:
        result["status"], result["reason"] = problem
        return result

    for parent in run_dir.parents:
        if parent == root:
            break
        if (parent / IDENTITY_FILENAME).is_file():
            result["status"] = "inside_run"
            result["reason"] = f"{parent} is a run folder itself, and a run can't sit inside another run."
            return result

    if run_dir.exists() and not run_dir.is_dir():
        result["reason"] = f"{run_dir} exists and is not a folder."
        return result

    reason, batch_id = _running_reason(run_dir, active_dirs)
    if reason is not None:
        result.update(status="running", reason=reason, batch_id=batch_id, contents=folder_contents(run_dir))
        return result

    try:
        if args is None:
            raise JobsError(input_error or "The input couldn't be used.")
        state, differences = compare_run_identity(run_dir, _identity_for(args))
    except JobsError as exc:
        result["reason"] = str(exc)
        return result
    except RunIdentityError as exc:
        state, differences = "different", [str(exc)]

    if state == "empty":
        result["status"] = "empty"
        return result
    if state == "same":
        state = "done_same" if (run_dir / SUCCESS_MARKER).is_file() else "same_unfinished"
    result.update(status=state, differences=differences, contents=folder_contents(run_dir))
    return result


def _mark_repeats(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Refuse every run that uses a folder an earlier run in the list uses."""
    first_by_folder: dict[str, dict[str, Any]] = {}
    repeats: list[dict[str, Any]] = []
    for result in results:
        if result["run_dir"] is None:
            continue
        first = first_by_folder.setdefault(result["run_dir"], result)
        if first is result:
            continue
        result["status"] = "refused"
        result["reason"] = (
            f"'{result['label']}' uses the same folder as '{first['label']}' ({result['folder']}). "
            "A batch can't use one folder twice."
        )
        repeats.append({"folder": result["folder"], "labels": [first["label"], result["label"]]})
    return repeats


def plan_runs(planned_runs: list[dict[str, Any]], resources: dict[str, Any] | None = None) -> dict[str, Any]:
    """Where every planned run would go and what's already there. Read-only."""
    root = runs.runs_root()
    slurm_error = None
    try:
        active = active_jobs()
    except SlurmUnavailable as exc:
        active, slurm_error = {}, str(exc)
    active_dirs = _active_run_dirs(root, active)
    results = [_plan_one(root, planned, resources, active_dirs) for planned in planned_runs]
    return {
        "root": str(root),
        "slurm_error": slurm_error,
        "runs": results,
        "repeats": _mark_repeats(results),
    }


def check(planned_runs: list[dict[str, Any]], resources_in: dict[str, Any] | None) -> dict[str, Any]:
    """`plan_runs` plus the resources checked (when given)."""
    resources = None
    resources_error = None
    resources_slurm_error = None
    if resources_in is not None:
        try:
            resources = validate_resources(resources_in)
        except JobsError as exc:
            resources_error = str(exc)
        except SlurmUnavailable as exc:
            resources_slurm_error = str(exc)
    plan = plan_runs(planned_runs, resources)
    plan["resources"] = resources
    plan["resources_error"] = resources_error
    plan["slurm_error"] = plan["slurm_error"] or resources_slurm_error
    return plan


# -------------------------------------------------------------------------
# Settings
# -------------------------------------------------------------------------


def settings() -> dict[str, Any]:
    try:
        partitions, slurm_error = list_partitions(), None
    except SlurmUnavailable as exc:
        partitions, slurm_error = [], str(exc)
    return {
        "root": str(runs.runs_root()),
        "partitions": partitions,
        "default_resources": DEFAULT_RESOURCES,
        "max_cpus": MAX_CPUS,
        "slurm_error": slurm_error,
    }


# -------------------------------------------------------------------------
# Deleting
# -------------------------------------------------------------------------


def _delete_target(root: Path, raw: Any, active_dirs: dict[str, str]) -> tuple[Path | None, str | None]:
    """`(folder, None)` when it may be deleted, `(None, why not)` when not."""
    if not isinstance(raw, str) or not raw.strip() or not Path(raw.strip()).is_absolute():
        return None, f"'{raw}' isn't an absolute folder path."
    path = Path(os.path.normpath(raw.strip()))
    if path.is_symlink():
        return None, f"{path} is a symbolic link; only real folders are deleted."
    problem = _check_location(path, root)
    if problem is not None:
        return None, problem[1]
    if not path.exists():
        return None, f"{path} doesn't exist."
    if not path.is_dir():
        return None, f"{path} isn't a folder."
    reason, _batch_id = _running_reason(path, active_dirs)
    if reason is not None:
        return None, f"{path}: {reason}"
    for current, _dir_names, file_names in os.walk(path):
        if Path(current) != path and IDENTITY_FILENAME in file_names:
            return None, (
                f"{path} holds another run ({current}), so it isn't deleted. "
                "Delete that run's own folder instead."
            )
    return path, None


def delete_folders(run_dirs: list[str]) -> dict[str, Any]:
    """Delete run folders inside the runs root. Every folder is checked first;
    if any one fails, nothing is deleted."""
    if not run_dirs:
        raise JobsError("No folders were given to delete.")
    with _LOCK:
        root = runs.runs_root()
        # Raises SlurmUnavailable: a running job can't be ruled out without squeue.
        active_dirs = _active_run_dirs(root, active_jobs())
        targets: list[Path] = []
        problems: list[str] = []
        for raw in run_dirs:
            path, problem = _delete_target(root, raw, active_dirs)
            if problem is not None:
                problems.append(problem)
            elif path not in targets:
                targets.append(path)
        if problems:
            raise JobsError("Nothing was deleted. " + " ".join(problems))

        deleted: list[dict[str, Any]] = []
        for path in targets:
            contents = folder_contents(path)
            try:
                shutil.rmtree(path)
            except OSError as exc:
                done = ", ".join(item["run_dir"] for item in deleted) or "none"
                raise JobsError(f"Couldn't delete {path} ({exc}). Already deleted: {done}.") from exc
            deleted.append(
                {
                    "run_dir": str(path),
                    "file_count": contents["file_count"],
                    "total_bytes": contents["total_bytes"],
                }
            )
        return {"deleted": deleted}


# -------------------------------------------------------------------------
# Submitting
# -------------------------------------------------------------------------


def _new_batch_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _submit_one(root: Path, resources: dict[str, Any], job: dict[str, Any]) -> None:
    """Run `sbatch` for one job record and note the result in it."""
    job_name = f"prepare_{slug(job['label'])[:JOB_NAME_SLUG_CHARS]}"
    argv = sbatch_args(resources, job_name, job["prepare_args"], root)
    # Stamped first: the job can start writing before sbatch returns.
    job["submitted_at"] = _now_iso()
    try:
        result = _run(argv, cwd=REPO_ROOT)
    except SlurmUnavailable as exc:
        job["submit_error"] = f"{exc} The job may or may not have been submitted; check `squeue`."
        return
    if result.returncode != 0:
        job["submit_error"] = f"sbatch failed: {_failure_detail(result)}"
        return
    job_id = result.stdout.strip().split(";")[0]
    if not job_id.isdigit():
        job["submit_error"] = f"sbatch printed something unexpected: {result.stdout.strip()!r}"
        return
    logs = _logs_folder(root)
    job["slurm_job_id"] = job_id
    job["out_log"] = str(logs / f"{job_id}.out")
    job["err_log"] = str(logs / f"{job_id}.err")


def submit(planned_runs: list[dict[str, Any]], resources_in: dict[str, Any] | None) -> dict[str, Any]:
    """Start one SLURM job per planned run, as one batch. Refuses the whole
    request when the resources or any run is unusable."""
    if not planned_runs:
        raise JobsError("There are no runs to start.")
    with _LOCK:
        resources = validate_resources(resources_in)
        plan = plan_runs(planned_runs, resources)
        if plan["slurm_error"]:
            raise SlurmUnavailable(plan["slurm_error"])
        blocked = [
            f"'{result['label']}' is {result['status']}" + (f": {result['reason']}" if result["reason"] else "")
            for result in plan["runs"]
            if result["status"] in BLOCKING_STATUSES
        ]
        if blocked:
            raise JobsError("Nothing was started. " + " ".join(blocked))

        root = Path(plan["root"])
        jobs: list[dict[str, Any]] = []
        for planned, result in zip(planned_runs, plan["runs"]):
            skipped = result["status"] == "done_same" and not planned.get("run_again")
            args = result["prepare_args"]
            jobs.append(
                {
                    "label": result["label"],
                    "input_path": args[1],
                    "input_type": planned["input_type"],
                    "run_dir": result["run_dir"],
                    "prepare_args": args,
                    "skipped": "already_done" if skipped else None,
                    "slurm_job_id": None,
                    "submitted_at": None,
                    "submit_error": None,
                    "out_log": None,
                    "err_log": None,
                }
            )

        batch_id = _new_batch_id()
        batch = {
            "batch_id": batch_id,
            "created_at": _now_iso(),
            "resources": resources,
            "jobs": jobs,
        }
        _batches_folder(root).mkdir(parents=True, exist_ok=True)
        _logs_folder(root).mkdir(parents=True, exist_ok=True)
        record_path = _batches_folder(root) / f"{batch_id}.json"
        # Written before the first sbatch, so a crash part-way still leaves a record.
        _write_json_atomic(record_path, batch)
        for job in jobs:
            if job["skipped"] is None:
                _submit_one(root, resources, job)
                _write_json_atomic(record_path, batch)

        return {
            "batch_id": batch_id,
            "submitted": sum(1 for job in jobs if job["slurm_job_id"]),
            "skipped": sum(1 for job in jobs if job["skipped"]),
            "submit_errors": [
                f"{job['label']}: {job['submit_error']}" for job in jobs if job["submit_error"]
            ],
        }


# -------------------------------------------------------------------------
# Watching
# -------------------------------------------------------------------------


def _fresh_files(job: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The job's `progress.json` and `summary.json`, but only when written
    after the job was submitted (a folder reused after an unfinished run still
    holds the old run's files)."""
    submitted = _parse_time(job.get("submitted_at"))
    if submitted is None or not isinstance(job.get("run_dir"), str):
        return None, None
    run_dir = Path(job["run_dir"])
    progress = runs.run_progress(run_dir)["progress"]
    if progress is not None:
        updated = _parse_time(progress.get("updated_at"))
        if updated is None or updated < submitted:
            progress = None
    summary = _read_summary(run_dir)
    if summary is not None:
        created = _parse_time(summary.get("created_at"))
        if created is None or created < submitted:
            summary = None
    return progress, summary


def _decide_outcome(
    job: dict[str, Any],
    active: dict[str, dict[str, Any]] | None,
    records: dict[str, dict[str, Any]] | None,
    fresh_files: Callable[[], tuple[dict[str, Any] | None, dict[str, Any] | None]],
) -> tuple[str, str | None]:
    """`(outcome, reason)` by the first rule that matches (see the plan's
    "How a job's outcome is decided"). `active` and `records` are None when
    squeue or sacct couldn't be read."""
    if job.get("skipped"):
        return "skipped", "Already done with the same settings, so it wasn't run again."
    if job.get("submit_error"):
        return "not_submitted", job["submit_error"]
    job_id = job.get("slurm_job_id")
    if not isinstance(job_id, str) or not job_id:
        return "unknown", "No SLURM job ID was recorded for this job."
    if active is None:
        return "unknown", "SLURM couldn't be reached, so this job's state isn't known."

    listed = active.get(job_id)
    if listed is not None:
        if listed["state"] == "PENDING":
            return "queued", listed["reason"]
        progress, _summary = fresh_files()
        if progress is not None:
            if runs.may_have_stopped(progress):
                return "stalled", "Its progress.json hasn't been updated for over 2 minutes."
        elif (listed["elapsed_seconds"] or 0) > runs.STALE_AFTER_SECONDS:
            return "stalled", "It started over 2 minutes ago and has not written a progress.json."
        return "running", listed["state"]

    if records is None:
        return "unknown", "SLURM couldn't be reached, so this job's state isn't known."
    record = records.get(job_id)
    if record is None:
        return "unknown", "SLURM has no record of this job."

    state = record["state"]
    exit_code = record["exit_code"]
    if state == "COMPLETED":
        return "succeeded", None
    if state == "FAILED" and exit_code.split(":")[0] == "2" and fresh_files()[1] is not None:
        return "checks_failed", "It ran to the end, but its own checks failed (see the summary)."
    if state == "CANCELLED":
        return "cancelled", record["state_text"]
    if state in STILL_GOING_STATES:
        return STILL_GOING_STATES[state], f"SLURM accounting says {state}."
    return "failed", f"SLURM state {record['state_text'] or 'unknown'}, exit code {exit_code}."


def _tail_file(path: Path) -> str | None:
    """The last `LOG_TAIL_LINES` lines of a log, reading at most the last
    `LOG_TAIL_BYTES`. None when there's no such file."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - LOG_TAIL_BYTES)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return None
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # the first line was probably cut in half
    return "\n".join(lines[-LOG_TAIL_LINES:])


def _relative_folder(root: Path, run_dir: Any) -> str | None:
    if not isinstance(run_dir, str):
        return None
    try:
        return Path(run_dir).relative_to(root).as_posix()
    except ValueError:
        return run_dir


def _job_view(
    root: Path,
    index: int,
    job: dict[str, Any],
    active: dict[str, dict[str, Any]] | None,
    records: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    progress, summary = _fresh_files(job)
    outcome, reason = _decide_outcome(job, active, records, lambda: (progress, summary))
    job_id = job.get("slurm_job_id")
    listed = active.get(job_id) if active is not None and isinstance(job_id, str) else None
    record = records.get(job_id) if records is not None and isinstance(job_id, str) else None

    out_tail = err_tail = None
    out_log = err_log = None
    if isinstance(job_id, str) and job_id.isdigit():
        # Built from the job ID, not read from the record, so a record can't
        # point this at some other file.
        out_log = _logs_folder(root) / f"{job_id}.out"
        err_log = _logs_folder(root) / f"{job_id}.err"
        out_tail = _tail_file(out_log)
        err_tail = _tail_file(err_log)

    return {
        "index": index,
        "label": job.get("label"),
        "input_path": job.get("input_path"),
        "input_type": job.get("input_type"),
        "run_dir": job.get("run_dir"),
        "folder": _relative_folder(root, job.get("run_dir")),
        "slurm_job_id": job_id,
        "submitted_at": job.get("submitted_at"),
        "skipped": job.get("skipped"),
        "submit_error": job.get("submit_error"),
        "outcome": outcome,
        "reason": reason,
        "slurm_state": (listed or {}).get("state") or (record or {}).get("state_text"),
        "exit_code": (record or {}).get("exit_code"),
        "elapsed": (listed or {}).get("elapsed") or (record or {}).get("elapsed"),
        "max_rss_bytes": (record or {}).get("max_rss_bytes"),
        "progress": progress,
        "may_have_stopped": progress is not None and runs.may_have_stopped(progress),
        "summary": _summary_view(summary),
        "out_log": str(out_log) if out_log else None,
        "err_log": str(err_log) if err_log else None,
        "out_tail": out_tail,
        "err_tail": err_tail,
    }


def _count_outcomes(outcomes: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def batch_status(batch_id: str) -> dict[str, Any]:
    """Every job of one batch, with its outcome and details."""
    root = runs.runs_root()
    batch = load_batch(root, batch_id)
    active, records, slurm_error = _read_slurm(_recorded_job_ids([batch]))
    jobs = [_job_view(root, index, job, active, records) for index, job in enumerate(batch["jobs"])]
    outcomes = [job["outcome"] for job in jobs]
    return {
        "batch_id": batch["batch_id"],
        "created_at": batch.get("created_at"),
        "resources": batch.get("resources"),
        "slurm_error": slurm_error,
        "jobs": jobs,
        "by_outcome": _count_outcomes(outcomes),
        "has_active": any(outcome in ACTIVE_OUTCOMES for outcome in outcomes),
    }


def list_batches() -> dict[str, Any]:
    """Every batch, newest first, with its count of jobs per outcome."""
    root = runs.runs_root()
    batches, problems = _load_batches(root)
    active, records, slurm_error = _read_slurm(_recorded_job_ids(batches))
    listed = []
    for batch in batches:
        outcomes = [
            _decide_outcome(job, active, records, lambda job=job: _fresh_files(job))[0]
            for job in batch["jobs"]
        ]
        listed.append(
            {
                "batch_id": batch["batch_id"],
                "created_at": batch.get("created_at"),
                "resources": batch.get("resources"),
                "job_count": len(batch["jobs"]),
                "labels": [job.get("label") for job in batch["jobs"]],
                "by_outcome": _count_outcomes(outcomes),
                "has_active": any(outcome in ACTIVE_OUTCOMES for outcome in outcomes),
            }
        )
    return {
        "root": str(root),
        "exists": root.is_dir(),
        "slurm_error": slurm_error,
        "problems": problems,
        "batches": listed,
    }


def cancel(batch_id: str, job_ids: list[str] | None) -> dict[str, Any]:
    """`scancel` the queued or running jobs of a batch (all of them, or just
    `job_ids`). Refuses an ID that isn't in that batch's record."""
    root = runs.runs_root()
    batch = load_batch(root, batch_id)
    recorded = _recorded_job_ids([batch])
    if job_ids:
        unknown = [job_id for job_id in job_ids if job_id not in recorded]
        if unknown:
            raise JobsError(f"Job {', '.join(unknown)} isn't in batch {batch_id}, so it won't be cancelled.")
        wanted = list(dict.fromkeys(job_ids))
    else:
        wanted = recorded

    active = active_jobs()
    targets = [job_id for job_id in wanted if job_id in active]
    if targets:
        result = _run(["scancel", *targets])
        if result.returncode != 0:
            raise JobsError(f"scancel failed: {_failure_detail(result)}")
    return {"cancelled": targets, "not_active": [job_id for job_id in wanted if job_id not in active]}
