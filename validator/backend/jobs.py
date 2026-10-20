"""The SLURM helpers the conversion, scan, result and filter code share.

`conversions.py` (the `prepare_rows.py` conversion of a dataset), `scans.py`, `results.py`
and `filters.py` submit and cancel their own jobs; what they have in common is here:
`squeue`, `sacct`, `sinfo` and `sbatch` calls, checking the resources a user asks for,
the `sbatch` argument list of a conversion, and working out a job's outcome from what
SLURM says and the files the job wrote. This module deletes nothing.

Rules this module keeps:

* Every `sbatch`, `squeue`, `sacct`, `sinfo` and `scancel` call is an argument
  list run with `shell=False` and a 30 second timeout.
* Every job states its partition, CPUs, memory and time limit. Nothing here
  ever falls back to SLURM's defaults, and nothing asks for a GPU, because on
  this cluster a job without `--mem` is given a whole node's memory.
* Job state is never stored: it's read live from SLURM and from the files a job
  writes, so restarting the backend loses nothing.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import activity
import detect
import preview
import runs
from reader_imports import (
    ORIGIN_KEY,
    REPO_ROOT,
    build_parser,
    discover_input_files,
    run_identity,
)

# `background` is hidden from plain `sinfo`, has a low priority and its jobs can be preempted
# (they then restart from scratch); a user can pick any other partition for a job.
DEFAULT_RESOURCES = {"partition": "background", "cpus": 2, "mem": "32G", "time": "04:00:00"}
MAX_CPUS = 32
SLURM_TIMEOUT_SECONDS = 30
JOB_SCRIPT = "launchers/slurm/prepare_rows.sbatch"
JOB_NAME_SLUG_CHARS = 50
LOG_TAIL_LINES = 40
LOG_TAIL_BYTES = 64 * 1024
SUCCESS_MARKER = "_SUCCESS"

BATCH_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
MEMORY = re.compile(r"^(\d+)([MG])$")
TIME_PLAIN = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")
TIME_WITH_DAYS = re.compile(r"^(\d{1,3})-([01]?\d|2[0-3]):([0-5]\d):([0-5]\d)$")
MAX_RSS = re.compile(r"^(\d+(?:\.\d+)?)([KMGTP]?)$")

# Outcomes of a job that's still going.
ACTIVE_OUTCOMES = ("queued", "running", "stalled")
# What sacct says for a job squeue no longer lists but accounting hasn't closed.
STILL_GOING_STATES = {
    "PENDING": "queued",
    "REQUEUED": "queued",
    "PREEMPTED": "queued",  # a job in `background` that a higher-priority job pushed off its nodes; it restarts
    "RUNNING": "running",
    "COMPLETING": "running",
    "CONFIGURING": "running",
    "SUSPENDED": "running",
    "RESIZING": "running",
}

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


# -------------------------------------------------------------------------
# SLURM
# -------------------------------------------------------------------------


def _run(
    argv: list[str], cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """`env` replaces the environment the command starts with (None keeps ours)."""
    try:
        return subprocess.run(
            argv,
            shell=False,
            timeout=SLURM_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
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
    """Partition names from `sinfo -a` (so hidden partitions such as `background`
    are listed); its `*` marks the default partition and isn't part of the name."""
    output = _slurm(["sinfo", "-a", "-h", "-o", "%P"])
    names = {line.strip().rstrip("*") for line in output.splitlines() if line.strip()}
    if not names:
        raise SlurmUnavailable("SLURM couldn't be reached: sinfo listed no partitions.")
    return sorted(names)


# -------------------------------------------------------------------------
# How busy each partition is (read-only: `sinfo` and `squeue`)
# -------------------------------------------------------------------------

BACKGROUND_PARTITION = "background"
# Node states (SLURM's long names, flags such as `*` or `~` removed) whose GPUs can't be given to a job.
UNAVAILABLE_NODE_STATE_PREFIXES = ("drain", "down", "fail", "maint", "inval", "unk", "fut")
# Job states in which a job holds its nodes.
HOLDING_JOB_STATES = ("RUNNING", "COMPLETING")
# The GPU count in a node's GRES text: "gpu:nvidia_h100_80gb_hbm3:8(S:0-1)" or "gpu:8".
GRES_GPU_COUNT = re.compile(r"gpu:(?:[^:(,\s]+:)?(\d+)")
# The GPUs of a job in squeue's `tres-alloc` ("cpu=8,mem=64G,node=1,gres/gpu=2").
JOB_GPU_COUNT = re.compile(r"(?:^|,)gres/gpu=(\d+)")


def _gpu_count(gres: str) -> int:
    return sum(int(count) for count in GRES_GPU_COUNT.findall(gres))


def _split_top_level(text: str) -> list[str]:
    """`text` cut at the commas that aren't inside square brackets."""
    parts: list[str] = []
    depth = 0
    start = 0
    for index, character in enumerate(text):
        if character == "[":
            depth += 1
        elif character == "]":
            depth = max(depth - 1, 0)
        elif character == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return [part for part in parts if part]


def _expand_node_part(part: str) -> list[str]:
    match = re.search(r"\[([^\]]*)\]", part)
    if match is None:
        return [part]
    prefix, suffix = part[: match.start()], part[match.end() :]
    expanded: list[str] = []
    for item in match.group(1).split(","):
        low, _separator, high = item.partition("-")
        if not low.isdigit() or (high != "" and not high.isdigit()):
            return [part]  # Not a list SLURM writes; it won't match any node.
        for number in range(int(low), int(high or low) + 1):
            expanded.extend(_expand_node_part(prefix + str(number).zfill(len(low)) + suffix))
    return expanded


def _expand_nodelist(text: str) -> list[str]:
    """The node names in squeue's list: "health-[0-1,5],vlm-3" is health-0, health-1, health-5, vlm-3."""
    names: list[str] = []
    for part in _split_top_level(text.strip()):
        names.extend(_expand_node_part(part))
    return names


def _pending_task_count(job_id: str) -> int:
    """How many jobs a pending squeue line stands for: squeue joins the waiting tasks of an
    array job into one line such as "525000_[3-40%4]" (the number after % limits how many run
    at once). 1 for any other ID, or when the range isn't one this understands."""
    match = re.search(r"_\[([^\]]*)\]", job_id)
    if match is None:
        return 1
    total = 0
    for item in match.group(1).split("%", 1)[0].split(","):
        bounds, _colon, step = item.partition(":")
        low, _dash, high = bounds.partition("-")
        if not low.isdigit() or (high != "" and not high.isdigit()) or (step != "" and not step.isdigit()):
            return 1
        if step == "0":
            return 1
        total += (int(high or low) - int(low)) // int(step or 1) + 1
    return max(total, 1)


def _read_nodes() -> dict[str, dict[str, Any]]:
    """Each node's state, GPUs and used GPUs, and the partitions it is in, from `sinfo -a -N`
    (one line for each node and partition it is in)."""
    output = _slurm(
        [
            "sinfo",
            "-a",
            "-N",
            "-h",
            "-O",
            "NodeHost:60|,Partition:60|,StateLong:30|,Gres:200|,GresUsed:200|",
        ]
    )
    nodes: dict[str, dict[str, Any]] = {}
    for line in output.splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 5:
            continue
        host, partition, state, gres, gres_used = parts[:5]
        partition = partition.rstrip("*")
        if not host or not partition:
            continue
        node = nodes.setdefault(
            host,
            {
                "state": state.lower().rstrip("*~#!%$@^-+"),
                "gpus": _gpu_count(gres),
                "used": _gpu_count(gres_used),
                "partitions": set(),
            },
        )
        node["partitions"].add(partition)
    if not nodes:
        raise SlurmUnavailable("SLURM couldn't be reached: sinfo listed no nodes.")
    return nodes


def _read_jobs() -> list[dict[str, Any]]:
    """Every job of every user in any partition, `squeue -a` (without -a the jobs of a hidden
    partition such as `background` aren't listed). `gpus` is what the job holds, or, for a
    waiting job, what it asks for."""
    output = _slurm(
        [
            "squeue",
            "-a",
            "-h",
            "-O",
            "JobID:100|,Partition:100|,State:20|,NodeList:1000|,tres-alloc:300|",
        ]
    )
    listed: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 5:
            continue
        job_id, partitions, state, nodelist, tres = parts[:5]
        match = JOB_GPU_COUNT.search(tres)
        listed.append(
            {
                "id": job_id,
                "partitions": [name for name in partitions.split(",") if name],
                "state": state,
                "nodes": nodelist,
                "gpus": int(match.group(1)) if match else 0,
            }
        )
    return listed


def partition_usage() -> dict[str, Any]:
    """For every partition `sinfo -a` lists, over the nodes it has: the nodes and GPUs that are
    free, the GPUs held by jobs of the partition itself, of `background` and of any other
    partition, the GPUs a job of this partition could get if the `background` jobs on its
    nodes were preempted, and the jobs running and waiting in it.

    The GPUs of a job on several nodes are shared evenly between them (SLURM doesn't say how
    they are really spread). A waiting job submitted to several partitions counts in each.
    When SLURM can't be asked, `partitions` is empty and `slurm_error` says why. Changes nothing.
    """
    try:
        nodes = _read_nodes()
        listed = _read_jobs()
    except SlurmUnavailable as exc:
        return {"partitions": [], "read_at": _now_iso(), "slurm_error": str(exc)}

    held_on_node: dict[str, dict[str, int]] = {}  # node -> partition of the job -> GPUs it holds there
    running_jobs: dict[str, int] = {}
    queued_jobs: dict[str, int] = {}
    queued_gpus: dict[str, int] = {}
    for job in listed:
        if job["state"] in HOLDING_JOB_STATES:
            node_names = _expand_nodelist(job["nodes"])
            for name in job["partitions"]:
                running_jobs[name] = running_jobs.get(name, 0) + 1
                if not node_names or job["gpus"] == 0:
                    continue
                share, extra = divmod(job["gpus"], len(node_names))
                for index, node_name in enumerate(node_names):
                    held = held_on_node.setdefault(node_name, {})
                    held[name] = held.get(name, 0) + share + (1 if index < extra else 0)
        elif job["state"] == "PENDING":
            tasks = _pending_task_count(job["id"])
            for name in job["partitions"]:
                queued_jobs[name] = queued_jobs.get(name, 0) + tasks
                queued_gpus[name] = queued_gpus.get(name, 0) + job["gpus"] * tasks

    partitions: list[dict[str, Any]] = []
    for name in sorted({partition for node in nodes.values() for partition in node["partitions"]}):
        is_background = name == BACKGROUND_PARTITION
        row: dict[str, Any] = {
            "name": name,
            "nodes_total": 0,
            "nodes_free": 0,
            "nodes_unavailable": 0,
            "gpus_total": 0,
            "gpus_free": 0,
            "gpus_used_by_self": 0,
            "gpus_used_by_background": None if is_background else 0,
            "gpus_used_by_others": 0,
            "gpus_reclaimable": None if is_background else 0,
            "jobs_running": running_jobs.get(name, 0),
            "jobs_queued": queued_jobs.get(name, 0),
            "gpus_queued": queued_gpus.get(name, 0),
        }
        for host, node in nodes.items():
            if name not in node["partitions"]:
                continue
            usable = not node["state"].startswith(UNAVAILABLE_NODE_STATE_PREFIXES)
            held = held_on_node.get(host, {})
            by_self = held.get(name, 0)
            by_background = 0 if is_background else held.get(BACKGROUND_PARTITION, 0)
            free = max(node["gpus"] - node["used"], 0) if usable else 0

            row["nodes_total"] += 1
            row["gpus_total"] += node["gpus"]
            row["gpus_free"] += free
            row["gpus_used_by_self"] += by_self
            row["gpus_used_by_others"] += max(node["used"] - by_self - by_background, 0)
            if not usable:
                row["nodes_unavailable"] += 1
            elif node["state"] == "idle" and node["used"] == 0:
                row["nodes_free"] += 1
            if not is_background:
                row["gpus_used_by_background"] += by_background
                if usable:
                    row["gpus_reclaimable"] += free + by_background
        partitions.append(row)

    return {"partitions": partitions, "read_at": _now_iso(), "slurm_error": None}


def active_jobs() -> dict[str, dict[str, Any]]:
    """The jobs that squeue lists, in any state, by job ID. It isn't limited to the
    current user: a job submitted by someone else running this service (the clone
    is shared) must still be found, and a job ID is unique cluster-wide.

    The filtering by job ID happens in the callers, not with `squeue -j`:
    squeue exits 1 for a job ID it no longer knows, which would look like
    SLURM being down.
    """
    output = _slurm(["squeue", "-h", "-o", "%i|%T|%r|%M"])
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


def sbatch_args(resources: dict[str, Any], job_name: str, args: list[str], logs: Path) -> list[str]:
    """The exact `sbatch` call (run from the repo root) for one run. The job's output and
    error files go in the folder `logs`, named by the SLURM job ID."""
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


# -------------------------------------------------------------------------
# Batch records: <root>/_jobs/batches/<batch_id>.json
# -------------------------------------------------------------------------


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


# -------------------------------------------------------------------------
# Settings
# -------------------------------------------------------------------------


# -------------------------------------------------------------------------
# Deleting
# -------------------------------------------------------------------------


# -------------------------------------------------------------------------
# Submitting
# -------------------------------------------------------------------------


def _new_batch_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _submit_one(logs: Path, resources: dict[str, Any], job: dict[str, Any]) -> None:
    """Run `sbatch` for one job record and note the result in it."""
    job_name = f"prepare_{slug(job['label'])[:JOB_NAME_SLUG_CHARS]}"
    argv = sbatch_args(resources, job_name, job["prepare_args"], logs)
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
    job["slurm_job_id"] = job_id
    job["out_log"] = str(logs / f"{job_id}.out")
    job["err_log"] = str(logs / f"{job_id}.err")
    dataset_id = job.get("dataset_id")
    if isinstance(dataset_id, str):
        activity.record("conversion", [dataset_id], [job_id])


# -------------------------------------------------------------------------
# Watching
# -------------------------------------------------------------------------


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


