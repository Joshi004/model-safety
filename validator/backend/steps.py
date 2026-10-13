"""The steps of a chain as jobs: a record for each step that was started, what SLURM
says about its jobs, how far its stage has got, and cancelling.

A chain step (a scan, a coverage report, a filter, the trace or the row status) is
started once and leaves one JSON record in the chain's own folder,
`<chain>/_jobs/<id>.json`: the stage, the folders it reads and writes, its settings
and resources, the exact command, what the command printed, and the SLURM jobs it
started (a scan can start several that wait for each other). Records are written
atomically and never changed or deleted by this module.

Like conversions (jobs.py), nothing about a job's state is stored. It is read live from
SLURM (`squeue`, `sacct`) and, for progress, from the files the stage writes (the
markers `chains.py` reads), so restarting the backend loses nothing. The one thing
this module acts on is `scancel`, and only on job IDs a record lists.

A job step the UI starts that SLURM doesn't run (a coverage report is a short command
run by the backend) has no jobs; its record says how the command ended in `inline`.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chains
import jobs
import runs
from reader_imports import REPO_ROOT, list_chains

RECORD_TOOL = "chain_step_job"
RECORD_VERSION = 1
RECORDS_FOLDER = "_jobs"
LOGS_FOLDER = "_logs"
# What a record's output may hold and how much of it is sent back.
MAX_WRAPPER_OUTPUT_CHARS = 20_000

# SLURM jobs that don't end by themselves, in the order the page should name them.
ACTIVE_OUTCOMES = ("running", "queued")
JOB_ID = re.compile(r"^\d+$")

OUTCOME_QUEUED = "queued"
OUTCOME_RUNNING = "running"
OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_UNKNOWN = "unknown"


class StepsError(ValueError):
    """The request can't be carried out; the message says why."""


# -------------------------------------------------------------------------
# The records
# -------------------------------------------------------------------------


def records_folder(chain_dir: Path) -> Path:
    return chain_dir / RECORDS_FOLDER


def _is_record(data: Any) -> bool:
    return (
        isinstance(data, dict)
        and data.get("tool") == RECORD_TOOL
        and data.get("version") == RECORD_VERSION
        and isinstance(data.get("id"), str)
        and jobs.BATCH_ID.fullmatch(data["id"]) is not None
        and data.get("stage") in chains.STAGE_BY_KEY
        and isinstance(data.get("jobs"), list)
        and all(isinstance(item, dict) for item in data["jobs"])
    )


def load_records(chain_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """The chain's step records, newest first, and what couldn't be read."""
    folder = records_folder(chain_dir)
    if not folder.is_dir():
        return [], []
    found: list[dict[str, Any]] = []
    problems: list[str] = []
    for path in sorted(folder.glob("*.json")):
        data = jobs._read_json(path)
        if not _is_record(data) or data["id"] != path.stem:
            problems.append(f"{path} isn't a chain step record.")
            continue
        found.append(data)
    found.sort(key=lambda item: item.get("submitted_at") or "", reverse=True)
    return found, problems


def load_record(chain_dir: Path, record_id: Any) -> dict[str, Any]:
    if not isinstance(record_id, str) or jobs.BATCH_ID.fullmatch(record_id) is None:
        raise StepsError("That isn't a step record ID.")
    data = jobs._read_json(records_folder(chain_dir) / f"{record_id}.json")
    if not _is_record(data) or data["id"] != record_id:
        raise StepsError(f"There is no step record {record_id} in {chain_dir.name}.")
    return data


def _job_ids(records: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for record in records:
        for job in record["jobs"]:
            job_id = job.get("job_id")
            if isinstance(job_id, str) and JOB_ID.fullmatch(job_id) and job_id not in ids:
                ids.append(job_id)
    return ids


# -------------------------------------------------------------------------
# What SLURM says
# -------------------------------------------------------------------------

# A line of `sacct -P`: a job ("123"), one task of an array job ("123_4", or "123_[4-7]" while
# its tasks are pending), and the steps inside it ("123.batch", "123_4.0"), which only the
# peak memory is read from.
SACCT_JOB = re.compile(r"^(\d+)(?:_(\[[^\]]*\]|\d+))?(?:\.(batch|extern|\d+))?$")


def _belongs(key: str, job_id: str) -> bool:
    """Whether a squeue ID is this job, or one of the tasks of this array job."""
    return key == job_id or key.startswith(f"{job_id}_")


def _sacct_rows(job_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """`sacct` for these jobs: for each, a row for the job (or for each task of an array
    job), with its state, exit code, time and peak memory. With explicit IDs it returns
    jobs that finished on an earlier day too, so no start time is passed."""
    if not job_ids:
        return {}
    output = jobs._slurm(["sacct", "-j", ",".join(job_ids), "-n", "-P", "-o", "JobID,State,ExitCode,Elapsed,MaxRSS"])
    wanted = set(job_ids)
    rows: dict[str, list[dict[str, Any]]] = {}
    peak: dict[tuple[str, str | None], int | None] = {}
    for line in output.splitlines():
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 5:
            continue
        match = SACCT_JOB.fullmatch(parts[0])
        if match is None or match.group(1) not in wanted:
            continue
        parent, task, step = match.groups()
        state, exit_code, elapsed, max_rss = parts[1], parts[2], parts[3], parts[4]
        if step is None:
            rows.setdefault(parent, []).append(
                {
                    "task": task,
                    "state": state.split()[0] if state else "",
                    "state_text": state,
                    "exit_code": exit_code,
                    "elapsed": elapsed,
                    "max_rss_bytes": None,
                }
            )
        elif step == "batch":
            peak[(parent, task)] = jobs._parse_max_rss(max_rss)
    for (parent, task), memory in peak.items():
        for row in rows.get(parent, []):
            if row["task"] == task:
                row["max_rss_bytes"] = memory
    return rows


def _read_slurm(
    job_ids: list[str],
) -> tuple[dict[str, dict[str, Any]] | None, dict[str, list[dict[str, Any]]] | None, str | None]:
    """`(squeue jobs, sacct rows, error)`. Either is None when it couldn't be read; sacct is
    only asked about jobs squeue doesn't list. Nothing to ask without IDs."""
    if not job_ids:
        return {}, {}, None
    try:
        active = jobs.active_jobs()
    except jobs.SlurmUnavailable as exc:
        return None, None, str(exc)
    finished = [job_id for job_id in job_ids if not any(_belongs(key, job_id) for key in active)]
    try:
        return active, _sacct_rows(finished), None
    except jobs.SlurmUnavailable as exc:
        return active, None, str(exc)


def _slurm_outcome(
    job_id: str,
    active: dict[str, dict[str, Any]] | None,
    accounted: dict[str, list[dict[str, Any]]] | None,
) -> tuple[str, str | None]:
    """`(outcome, reason)` of one SLURM job, taking all the tasks of an array job together.
    `active` and `accounted` are None when squeue or sacct couldn't be read."""
    if active is None:
        return OUTCOME_UNKNOWN, "SLURM couldn't be reached, so this job's state isn't known."
    listed = [value for key, value in active.items() if _belongs(key, job_id)]
    if listed:
        if any(item["state"] != "PENDING" for item in listed):
            return OUTCOME_RUNNING, next(item["state"] for item in listed if item["state"] != "PENDING")
        if all("DependencyNeverSatisfied" in item["reason"] for item in listed):
            # A job it waits for failed, so it would sit in the queue for ever.
            return OUTCOME_FAILED, "A job it waits for didn't finish well, so it will never start. Cancel it."
        return OUTCOME_QUEUED, listed[0]["reason"]
    if accounted is None:
        return OUTCOME_UNKNOWN, "SLURM couldn't be reached, so this job's state isn't known."
    tasks = accounted.get(job_id)
    if not tasks:
        return OUTCOME_UNKNOWN, "SLURM has no record of this job."
    going = [task for task in tasks if task["state"] in jobs.STILL_GOING_STATES]
    if going:
        return jobs.STILL_GOING_STATES[going[0]["state"]], f"SLURM accounting says {going[0]['state']}."
    bad = [task for task in tasks if task["state"] not in ("COMPLETED", "CANCELLED")]
    if bad:
        which = f"task {bad[0]['task']}: " if bad[0]["task"] is not None else ""
        return OUTCOME_FAILED, f"{which}SLURM state {bad[0]['state_text'] or 'unknown'}, exit code {bad[0]['exit_code']}."
    cancelled = [task for task in tasks if task["state"] == "CANCELLED"]
    if cancelled:
        return OUTCOME_CANCELLED, cancelled[0]["state_text"]
    return OUTCOME_SUCCEEDED, None


def _record_outcome(job_outcomes: list[str], inline: Any) -> str:
    """One outcome for a whole record. A job still going comes first, then anything
    that went wrong, then success."""
    if isinstance(inline, dict) and not job_outcomes:
        return OUTCOME_SUCCEEDED if inline.get("exit_code") == 0 else OUTCOME_FAILED
    if not job_outcomes:
        return OUTCOME_UNKNOWN
    for outcome in (OUTCOME_RUNNING, OUTCOME_QUEUED, OUTCOME_FAILED, OUTCOME_CANCELLED, OUTCOME_UNKNOWN):
        if outcome in job_outcomes:
            return outcome
    return OUTCOME_SUCCEEDED


# -------------------------------------------------------------------------
# Logs
# -------------------------------------------------------------------------


def _readable_log(chain_dir: Path, text: Any) -> Path | None:
    """A log a record names, if it lies in the chain's `_logs` folder or the repo's
    `logs/slurm` folder. A record is only a file, so it can't point this at any other file."""
    if not isinstance(text, str) or not text:
        return None
    try:
        path = Path(text).resolve()
    except (OSError, RuntimeError):
        return None
    for root in ((chain_dir / LOGS_FOLDER).resolve(), (REPO_ROOT / "logs" / "slurm").resolve()):
        if root in path.parents:
            return path
    return None


# -------------------------------------------------------------------------
# One record, as the page shows it
# -------------------------------------------------------------------------


def _job_view(
    chain_dir: Path,
    job: dict[str, Any],
    active: dict[str, dict[str, Any]] | None,
    accounted: dict[str, list[dict[str, Any]]] | None,
    detail: bool,
) -> dict[str, Any]:
    job_id = job.get("job_id")
    valid = isinstance(job_id, str) and JOB_ID.fullmatch(job_id) is not None
    if valid:
        outcome, reason = _slurm_outcome(job_id, active, accounted)
    else:
        outcome, reason = OUTCOME_UNKNOWN, "No SLURM job ID was recorded for this job."
    listed = [value for key, value in active.items() if _belongs(key, job_id)] if valid and active is not None else []
    tasks = (accounted or {}).get(job_id, []) if valid else []
    # An array job has one row per task: say how many are in each state.
    states: dict[str, int] = {}
    for state in [item["state"] for item in listed] + [item["state"] for item in tasks]:
        states[state] = states.get(state, 0) + 1
    elapsed_seconds = [item.get("elapsed_seconds") or 0 for item in listed]
    longest = listed[elapsed_seconds.index(max(elapsed_seconds))]["elapsed"] if listed else None
    bad = next((task for task in tasks if task["state"] not in ("COMPLETED", "CANCELLED")), None)
    view: dict[str, Any] = {
        "job_id": job_id if valid else None,
        "role": job.get("role"),
        "depends_on": [item for item in job.get("depends_on", []) if isinstance(item, str)],
        "outcome": outcome,
        "reason": reason,
        "slurm_state": ", ".join(f"{name} {count}" if len(states) > 1 or count > 1 else name for name, count in states.items()) or None,
        "exit_code": (bad or (tasks[0] if tasks else {})).get("exit_code"),
        "elapsed": longest or (max(tasks, key=lambda task: task["elapsed"])["elapsed"] if tasks else None),
        "max_rss_bytes": max((task["max_rss_bytes"] or 0 for task in tasks), default=0) or None,
    }
    out_log = _readable_log(chain_dir, job.get("out_log"))
    err_log = _readable_log(chain_dir, job.get("err_log"))
    view["out_log"] = str(out_log) if out_log else None
    view["err_log"] = str(err_log) if err_log else None
    if detail:
        view["out_tail"] = jobs._tail_file(out_log) if out_log else None
        view["err_tail"] = jobs._tail_file(err_log) if err_log else None
    return view


def _record_view(
    chain_dir: Path,
    record: dict[str, Any],
    active: dict[str, dict[str, Any]] | None,
    accounted: dict[str, list[dict[str, Any]]] | None,
    stage: dict[str, Any] | None,
    detail: bool,
) -> dict[str, Any]:
    job_views = [_job_view(chain_dir, job, active, accounted, detail) for job in record["jobs"]]
    outcome = _record_outcome([item["outcome"] for item in job_views], record.get("inline"))
    reasons = [item["reason"] for item in job_views if item["outcome"] == outcome and item["reason"]]
    stage_definition = chains.STAGE_BY_KEY[record["stage"]]
    view: dict[str, Any] = {
        "id": record["id"],
        "chain_dir": str(chain_dir),
        "chain": chain_dir.name,
        "stage": record["stage"],
        "label": stage_definition.label,
        "submitted_at": record.get("submitted_at"),
        "resources": record.get("resources"),
        "gpus": record.get("gpus") if isinstance(record.get("gpus"), int) else 0,
        "inputs": record.get("inputs"),
        "output": record.get("output"),
        "outcome": outcome,
        "reason": reasons[0] if reasons else None,
        "inline": record.get("inline"),
        "warnings": record.get("warnings", []),
        "jobs": job_views,
        "has_active": any(item["outcome"] in ACTIVE_OUTCOMES for item in job_views),
        # How far the stage has got is read from its own files, not from SLURM.
        "stage_state": stage["state"] if stage else None,
        "progress": stage["progress"] if stage else None,
        "stage_verdict": stage.get("verdict") if stage else None,
    }
    if detail:
        output = record.get("wrapper_output")
        view.update(
            command_text=record.get("command_text"),
            settings=record.get("settings"),
            wrapper_output=output[:MAX_WRAPPER_OUTPUT_CHARS] if isinstance(output, str) else None,
        )
    return view


# -------------------------------------------------------------------------
# The Jobs page
# -------------------------------------------------------------------------


def list_all() -> dict[str, Any]:
    """Every step record of every chain, newest first, with the outcome of its jobs,
    and how far its stage has got. One squeue and one sacct for all of them."""
    listing = list_chains(runs.chains_root())
    per_chain: list[tuple[Path, list[dict[str, Any]]]] = []
    problems: list[str] = []
    for item in listing["chains"]:
        if "problem" in item:
            continue
        chain_dir = Path(item["chain_dir"])
        records, bad = load_records(chain_dir)
        problems.extend(bad)
        if records:
            per_chain.append((chain_dir, records))

    active, accounted, slurm_error = _read_slurm(_job_ids([record for _, records in per_chain for record in records]))
    views: list[dict[str, Any]] = []
    for chain_dir, records in per_chain:
        try:
            stages = chains.stage_states(chain_dir)
        except chains.ChainsError:
            stages = {}
        views.extend(_record_view(chain_dir, record, active, accounted, stages.get(record["stage"]), False) for record in records)
    views.sort(key=lambda item: item["submitted_at"] or "", reverse=True)
    return {
        "root": str(runs.chains_root()),
        "slurm_error": slurm_error,
        "problems": problems,
        "records": views,
    }


def record_status(chain_dir: Path, record_id: str) -> dict[str, Any]:
    """One record in full: the command, what it printed, each job and the end of its logs."""
    record = load_record(chain_dir, record_id)
    active, accounted, slurm_error = _read_slurm(_job_ids([record]))
    try:
        stage = chains.stage_states(chain_dir).get(record["stage"])
    except chains.ChainsError:
        stage = None
    view = _record_view(chain_dir, record, active, accounted, stage, True)
    view["slurm_error"] = slurm_error
    return view


def cancel(chain_dir: Path, record_id: str, job_ids: list[str] | None) -> dict[str, Any]:
    """`scancel` the queued or running jobs of a record (all of them, or just
    `job_ids`). Refuses an ID the record doesn't list."""
    record = load_record(chain_dir, record_id)
    recorded = _job_ids([record])
    if job_ids:
        unknown = [job_id for job_id in job_ids if job_id not in recorded]
        if unknown:
            raise StepsError(f"Job {', '.join(unknown)} isn't in record {record_id}, so it won't be cancelled.")
        wanted = list(dict.fromkeys(job_ids))
    else:
        wanted = recorded
    try:
        active = jobs.active_jobs()
        # `scancel <array job ID>` stops every task of the array.
        targets = [job_id for job_id in wanted if any(_belongs(key, job_id) for key in active)]
        if targets:
            result = jobs._run(["scancel", *targets])
            if result.returncode != 0:
                raise StepsError(f"scancel failed: {jobs._failure_detail(result)}")
    except jobs.SlurmUnavailable as exc:
        raise StepsError(str(exc)) from exc
    return {"cancelled": targets, "not_active": [job_id for job_id in wanted if job_id not in targets]}


# -------------------------------------------------------------------------
# Refining the state of the chain's stages
# -------------------------------------------------------------------------


# A step started this recently whose jobs SLURM has no word on is treated as just started.
RECENT_SECONDS = 15 * 60


def _is_recent(record: dict[str, Any]) -> bool:
    submitted = jobs._parse_time(record.get("submitted_at"))
    if submitted is None:
        return False
    return (datetime.now(timezone.utc) - submitted).total_seconds() < RECENT_SECONDS


def refine_stages(chain_dir: Path, stages: list[dict[str, Any]]) -> None:
    """Say, for each stage that hasn't finished, what its newest step record says: it is
    `queued` or `running` (SLURM), or it `failed` or was cancelled. A stage without a
    record, or one the files say is finished, keeps the state the files gave it. Every
    stage with a record gets a `job` (its record's ID and outcome). Changes `stages` in place."""
    records, _ = load_records(chain_dir)
    if not records:
        return
    newest: dict[str, dict[str, Any]] = {}
    for record in records:  # newest first
        newest.setdefault(record["stage"], record)
    active, accounted, _error = _read_slurm(_job_ids(list(newest.values())))
    for stage in stages:
        record = newest.get(stage["key"])
        if record is None:
            continue
        job_views = [_job_view(chain_dir, job, active, accounted, False) for job in record["jobs"]]
        outcome = _record_outcome([item["outcome"] for item in job_views], record.get("inline"))
        stage["job"] = {
            "record_id": record["id"],
            "outcome": outcome,
            "job_ids": [item["job_id"] for item in job_views if item["job_id"]],
        }
        if stage["state"] == chains.STATE_FINISHED:
            continue
        if outcome in ACTIVE_OUTCOMES:
            stage["state"] = outcome
        elif outcome in (OUTCOME_FAILED, OUTCOME_CANCELLED):
            stage["state"] = chains.STATE_FAILED
        elif outcome == OUTCOME_UNKNOWN and stage["state"] == chains.STATE_NOT_STARTED and _is_recent(record):
            # Just started, and SLURM can't say yet (or can't be reached): starting it again
            # could run it twice.
            stage["ready"] = False
            stage["blocked_by"] = [
                *stage["blocked_by"],
                f"It was started a moment ago (record {record['id']}) and SLURM doesn't say yet whether it is running.",
            ]
        # Whatever the state became, the stage can only be started if it hasn't been.
        stage["ready"] = stage["ready"] and stage["state"] == chains.STATE_NOT_STARTED
