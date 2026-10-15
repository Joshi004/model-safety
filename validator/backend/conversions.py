"""Starting, watching and cancelling the conversion of one registered dataset.

A conversion is one `prepare_rows.py` run submitted to SLURM with `sbatch`. It writes into

    <service root>/datasets/<dataset id>/prepared/<version id>/

and keeps its record in `.../_jobs/<version id>.json` and its SLURM logs in `.../_logs/`.
The version id has the same shape as a batch id (`jobs.BATCH_ID`), so it sorts by time.

Rules this module keeps:

* The original data is only read (to check that it can be converted). Nothing here writes
  to, moves or deletes it.
* The only thing ever deleted is a `prepared/<version id>` folder of one dataset, and only
  through `_delete_version_folder`, which checks the ids, refuses a symbolic link, and
  checks that the folder really is that path. A folder is deleted when it is the current
  version being replaced (D6), or when it is a leftover failed or cancelled attempt that
  no record of the registry points at. A version marked `kept` is never deleted.
* Every SLURM call goes through jobs.py: an argument list, `shell=False`, a 30 second
  timeout. Every job states its partition, CPUs, memory and time limit.
* Job state is never stored. It is read live from SLURM and from the files a job writes.
  The one thing saved from those files is the dataset's `prepared`, set by `refresh` when
  the newest conversion left a `_SUCCESS` and a summary whose checks passed. `refresh` is
  lazy: it runs whenever datasets or conversions are read, and never waits for a start
  that is in progress.
* A job's `summary.json` and `_SUCCESS` are only looked for once SLURM no longer lists the
  job, and nothing is looked for while it is queued. The empty version folder is made
  before `sbatch`. On this cluster's shared disk, asking for a file or folder before it
  exists makes this machine remember "not there" for about a minute, even after the job
  has written it.
* One lock covers every start, so two clicks can't start two jobs for one dataset, or
  delete a folder while another request is checking it.
"""

from __future__ import annotations

import shlex
import shutil
import threading
from pathlib import Path
from typing import Any, Callable

import jobs
import preview
import registry
import runs

PREPARED_FOLDER = "prepared"
JOBS_FOLDER = "_jobs"
LOGS_FOLDER = "_logs"
# Stands for the version id in the commands shown before a start. The id is chosen when
# the job starts; the record of the job then holds the exact command that ran.
VERSION_PLACEHOLDER = "<version-id>"
MAX_ITEMS = 200

# A dataset in one of these statuses can't be started as it is.
BLOCKING_STATUSES = ("converted", "filtered", "unavailable", "no_settings", "invalid", "slurm_error", "active")

_LOCK = threading.Lock()


class ConversionError(jobs.JobsError):
    """The request can't be carried out; the message says why."""


# -------------------------------------------------------------------------
# Where things live
# -------------------------------------------------------------------------


def _dataset_folder(dataset_id: str) -> Path:
    if not isinstance(dataset_id, str) or registry.DATASET_ID.fullmatch(dataset_id) is None:
        raise ConversionError(f"'{dataset_id}' isn't a dataset id.")
    return registry.service_root() / "datasets" / dataset_id


def _version_folder(dataset_id: str, version_id: str) -> Path:
    # The id becomes part of a path, so only the exact format is let through.
    if not isinstance(version_id, str) or jobs.BATCH_ID.fullmatch(version_id) is None:
        raise ConversionError(f"'{version_id}' isn't a conversion id.")
    return _dataset_folder(dataset_id) / PREPARED_FOLDER / version_id


def _record_path(dataset_id: str, job_id: str) -> Path:
    _version_folder(dataset_id, job_id)  # checks both ids
    return _dataset_folder(dataset_id) / JOBS_FOLDER / f"{job_id}.json"


def _logs_folder(dataset_id: str) -> Path:
    return _dataset_folder(dataset_id) / LOGS_FOLDER


# -------------------------------------------------------------------------
# Conversion records: <dataset folder>/_jobs/<version id>.json
# -------------------------------------------------------------------------


def _read_records(dataset_id: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Every record of a dataset, newest first, and a note for each unreadable one."""
    folder = _dataset_folder(dataset_id) / JOBS_FOLDER
    records: list[dict[str, Any]] = []
    problems: list[str] = []
    if not folder.is_dir():
        return records, problems
    for path in sorted(folder.glob("*.json"), reverse=True):
        if jobs.BATCH_ID.fullmatch(path.stem) is None:
            continue
        data = jobs._read_json(path)
        if not isinstance(data, dict) or data.get("job_id") != path.stem or data.get("dataset_id") != dataset_id:
            problems.append(f"{path.name} couldn't be read as a conversion record.")
            continue
        # The folder comes from the id, never from what the file says.
        data["run_dir"] = str(_version_folder(dataset_id, path.stem))
        records.append(data)
    return records, problems


def _load_record(dataset_id: str, job_id: str) -> dict[str, Any]:
    data = jobs._read_json(_record_path(dataset_id, job_id))
    if not isinstance(data, dict) or data.get("job_id") != job_id or data.get("dataset_id") != dataset_id:
        raise ConversionError(f"There's no readable conversion {job_id} for dataset {dataset_id}.")
    data["run_dir"] = str(_version_folder(dataset_id, job_id))
    return data


def _slurm_ids(records: list[dict[str, Any]]) -> list[str]:
    return [
        record["slurm_job_id"]
        for record in records
        if isinstance(record.get("slurm_job_id"), str) and record["slurm_job_id"].isdigit()
    ]


def _active_record(dataset_id: str, active: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The record of a conversion of this dataset that squeue still lists, if any."""
    records, _problems = _read_records(dataset_id)
    for record in records:
        if record.get("slurm_job_id") in active:
            return record
    return None


# -------------------------------------------------------------------------
# Reading a conversion's files
# -------------------------------------------------------------------------


def _fresh_progress(record: dict[str, Any]) -> dict[str, Any] | None:
    """The conversion's `progress.json`, but only when written after the job was submitted."""
    submitted = jobs._parse_time(record.get("submitted_at"))
    if submitted is None:
        return None
    progress = runs.read_progress(Path(record["run_dir"]))
    if progress is not None:
        updated = jobs._parse_time(progress.get("updated_at"))
        if updated is None or updated < submitted:
            return None
    return progress


def _fresh_summary(record: dict[str, Any]) -> dict[str, Any] | None:
    """The conversion's `summary.json`, but only when written after the job was submitted."""
    submitted = jobs._parse_time(record.get("submitted_at"))
    if submitted is None:
        return None
    summary = jobs._read_summary(Path(record["run_dir"]))
    if summary is not None:
        created = jobs._parse_time(summary.get("created_at"))
        if created is None or created < submitted:
            return None
    return summary


# -------------------------------------------------------------------------
# Making a finished conversion the dataset's current version
# -------------------------------------------------------------------------


# What a caller knows about SLURM: the jobs squeue lists, or None when SLURM can't be asked.
Listed = Callable[[], "dict[str, dict[str, Any]] | None"]


def _squeue_once() -> Listed:
    """A function that asks squeue the first time it is called, and remembers the answer."""
    answer: list[dict[str, dict[str, Any]] | None] = []

    def listed() -> dict[str, dict[str, Any]] | None:
        if not answer:
            try:
                answer.append(jobs.active_jobs())
            except jobs.SlurmUnavailable:
                answer.append(None)
        return answer[0]

    return listed


def _finalize(dataset_id: str, listed: Listed) -> None:
    """When the newest conversion of a dataset without a current version has finished and
    its checks passed, make it the current version. Call it while holding `_LOCK`.

    The files of a job SLURM still lists are not looked for: asking a shared disk for a
    file that doesn't exist yet makes this machine remember "not there" for about a
    minute, even after the job has written it (seen on this cluster). So SLURM is asked
    first, and only when a conversion is waiting to be picked up."""
    records, _problems = _read_records(dataset_id)
    if not records:
        return
    newest = records[0]
    # A record that already became current once is never made current again.
    if newest.get("result") is not None or newest.get("submit_error") or not newest.get("slurm_job_id"):
        return
    still_listed = listed()
    if still_listed is None or newest["slurm_job_id"] in still_listed:
        return
    folder = Path(newest["run_dir"])
    if not (folder / jobs.SUCCESS_MARKER).is_file():
        return
    summary = _fresh_summary(newest)
    view = jobs._summary_view(summary)
    if view is None or not view["checks_passed"]:
        return
    prepared = {
        "version_id": newest["job_id"],
        "folder": str(folder),
        "rows": view["written"],
        "rows_read": view["rows_read"],
        "skipped": view["skipped"],
        "finished_at": view["created_at"],
        "kept": False,
    }
    # Set first, then noted in the record: a crash between the two is repaired by the next
    # refresh, the other order would leave a finished conversion nobody ever picks up.
    if registry.set_prepared(dataset_id, prepared):
        newest["result"] = {
            "finished_at": view["created_at"],
            "rows": view["written"],
            "rows_read": view["rows_read"],
            "skipped": view["skipped"],
        }
        jobs._write_json_atomic(_record_path(dataset_id, newest["job_id"]), newest)


def refresh(dataset_id: str, listed: Listed | None = None) -> None:
    """`_finalize` for one dataset. Skipped, not waited for, while a start is in progress:
    the next read does it. `listed` is what the caller already knows from squeue."""
    if not _LOCK.acquire(blocking=False):
        return
    try:
        _finalize(dataset_id, listed or _squeue_once())
    finally:
        _LOCK.release()


def refresh_all() -> None:
    """`refresh` for every dataset that has no current version."""
    if not _LOCK.acquire(blocking=False):
        return
    try:
        listed = _squeue_once()  # shared, so squeue is asked at most once
        for dataset_id in registry.unconverted_ids():
            _finalize(dataset_id, listed)
    finally:
        _LOCK.release()


# -------------------------------------------------------------------------
# Deleting Prepared version folders
# -------------------------------------------------------------------------


def _delete_version_folder(dataset_id: str, version_id: str) -> None:
    """Delete `<dataset folder>/prepared/<version id>` and nothing else."""
    folder = _version_folder(dataset_id, version_id)
    if folder.is_symlink():
        raise ConversionError(f"{folder} is a symbolic link, so it isn't deleted.")
    if not folder.exists():
        return
    if folder.resolve() != folder:
        raise ConversionError(f"{folder} isn't where it should be (it resolves to {folder.resolve()}), so it isn't deleted.")
    if not folder.is_dir():
        raise ConversionError(f"{folder} isn't a folder, so it isn't deleted.")
    try:
        shutil.rmtree(folder)
    except OSError as exc:
        raise ConversionError(f"Couldn't delete {folder} ({exc}).") from exc


def _unused_folders(dataset: dict[str, Any]) -> list[Path]:
    """The version folders of a dataset that its registry entry doesn't point at: failed
    and cancelled attempts. The current version and the kept ones are not in the list.
    Anything in `prepared/` that isn't named like a version id is left alone."""
    parent = _dataset_folder(dataset["id"]) / PREPARED_FOLDER
    if not parent.is_dir():
        return []
    in_use = {
        version["version_id"]
        for version in [dataset.get("prepared"), *dataset["kept_versions"]]
        if isinstance(version, dict)
    }
    return [
        child
        for child in sorted(parent.iterdir())
        if jobs.BATCH_ID.fullmatch(child.name)
        and child.name not in in_use
        and child.is_dir()
        and not child.is_symlink()
    ]


# -------------------------------------------------------------------------
# Checking
# -------------------------------------------------------------------------


def _planned(dataset: dict[str, Any]) -> dict[str, Any]:
    """The dataset's saved settings in the shape `jobs._prepare_args_for` reads."""
    return {"input_path": dataset["path"], **dataset["conversion"]}


def _job_name(dataset: dict[str, Any]) -> str:
    return f"prepare_{jobs.slug(dataset['name'])[: jobs.JOB_NAME_SLUG_CHARS]}"


def _commands(
    dataset: dict[str, Any], args: list[str], resources: dict[str, Any] | None
) -> tuple[str, str | None]:
    """The `prepare_rows.py` command, and the `sbatch` call that runs it (None until the
    resources are known). Built the same way the start builds them."""
    command = f"{preview.READERS_PYTHON} {preview.PREPARE_ROWS_SCRIPT} {shlex.join(args)}"
    if resources is None:
        return command, None
    sbatch = jobs.sbatch_args(resources, _job_name(dataset), args, _logs_folder(dataset["id"]))
    return command, shlex.join(sbatch)


def _plan_item(
    dataset: dict[str, Any],
    reconvert: bool,
    resources: dict[str, Any] | None,
    active: dict[str, dict[str, Any]] | None,
    slurm_error: str | None,
) -> dict[str, Any]:
    """What starting this dataset's conversion would do, or why it can't be started.
    `active` is None when squeue couldn't be read. Deletes and starts nothing."""
    dataset_id = dataset["id"]
    prepared = dataset.get("prepared")
    item: dict[str, Any] = {
        "dataset_id": dataset_id,
        "name": dataset["name"],
        "team": dataset["team"],
        "reconvert": reconvert,
        "status": "ready",
        "reason": None,
        "can_start": False,
        "prepared": prepared,
        "will_delete": None,
        "leftovers": [],
        "command": None,
        "sbatch_command": None,
    }

    def refuse(status: str, reason: str) -> dict[str, Any]:
        item["status"], item["reason"] = status, reason
        return item

    # Already converted and kept as it is: nothing is read from the original, so this
    # comes before the availability check.
    if prepared is not None and not reconvert:
        return refuse("converted", 'Already converted. Choose "Convert again" to replace it.')
    # A filtered version is made by dropping rows from another dataset's converted data (its
    # filter job writes the Prepared version), never by converting the original data again.
    # This one gate covers check, submit, start_conversion and the scan dialog.
    if dataset.get("parent"):
        return refuse(
            "filtered",
            "This is a filtered version: it is made by dropping rows from another dataset's converted "
            "data, so it can't be converted. If its filter job failed, unregister it and drop the rows again.",
        )
    if not dataset["available"]:
        return refuse("unavailable", dataset["unavailable_reason"] or "The original data can't be read.")
    if dataset["conversion"] is None:
        return refuse("no_settings", "Set up conversion first: this dataset has no saved conversion settings.")

    output_dir = _dataset_folder(dataset_id) / PREPARED_FOLDER / VERSION_PLACEHOLDER
    try:
        args = jobs._prepare_args_for(_planned(dataset), output_dir)
        jobs._identity_for(args)
    except jobs.JobsError as exc:
        return refuse("invalid", str(exc))
    item["command"], item["sbatch_command"] = _commands(dataset, args, resources)

    if active is None:
        return refuse("slurm_error", slurm_error or "SLURM couldn't be reached.")
    running = _active_record(dataset_id, active)
    if running is not None:
        return refuse(
            "active",
            f"A conversion of this dataset is already queued or running (SLURM job {running.get('slurm_job_id')}).",
        )

    if prepared is not None:
        item["will_delete"] = {
            "version_id": prepared["version_id"],
            "folder": prepared["folder"],
            "rows": prepared.get("rows"),
            "kept": bool(prepared.get("kept")),
        }
    item["leftovers"] = [str(folder) for folder in _unused_folders(dataset)]
    item["can_start"] = True
    return item


def _validated_items(items: Any) -> list[dict[str, Any]]:
    """The `{id, reconvert}` list of a request, checked."""
    if not isinstance(items, list) or not items:
        raise ConversionError("Choose at least one dataset.")
    if len(items) > MAX_ITEMS:
        raise ConversionError(f"At most {MAX_ITEMS} datasets can be converted in one request.")
    checked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        dataset_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(dataset_id, str) or registry.DATASET_ID.fullmatch(dataset_id) is None:
            raise ConversionError(f"'{dataset_id}' isn't a dataset id.")
        if dataset_id in seen:
            raise ConversionError(f"Dataset {dataset_id} is listed twice.")
        seen.add(dataset_id)
        checked.append({"id": dataset_id, "reconvert": item.get("reconvert") is True})
    return checked


def _resources_or_default(resources_in: dict[str, Any] | None) -> dict[str, Any]:
    return resources_in if resources_in is not None else jobs.DEFAULT_RESOURCES


def settings() -> dict[str, Any]:
    """What the forms need from SLURM, and the default resources of a conversion."""
    try:
        partitions, slurm_error = jobs.list_partitions(), None
    except jobs.SlurmUnavailable as exc:
        partitions, slurm_error = [], str(exc)
    return {
        "service_root": str(registry.service_root()),
        "partitions": partitions,
        "default_resources": jobs.DEFAULT_RESOURCES,
        "max_cpus": jobs.MAX_CPUS,
        "slurm_error": slurm_error,
    }


def check(items_in: Any, resources_in: dict[str, Any] | None) -> dict[str, Any]:
    """For each dataset: whether a conversion can start, what it would delete, and the
    commands. Read-only, apart from making finished conversions current."""
    items = _validated_items(items_in)
    resources: dict[str, Any] | None = None
    resources_error: str | None = None
    slurm_error: str | None = None
    try:
        resources = jobs.validate_resources(_resources_or_default(resources_in))
    except jobs.JobsError as exc:
        resources_error = str(exc)
    except jobs.SlurmUnavailable as exc:
        slurm_error = str(exc)
    try:
        active: dict[str, dict[str, Any]] | None = jobs.active_jobs()
    except jobs.SlurmUnavailable as exc:
        active, slurm_error = None, slurm_error or str(exc)

    planned = []
    for item in items:
        refresh(item["id"], lambda: active)
        planned.append(_plan_item(registry.get_dataset(item["id"]), item["reconvert"], resources, active, slurm_error))
    return {
        "service_root": str(registry.service_root()),
        "slurm_error": slurm_error,
        "resources": resources,
        "resources_error": resources_error,
        "items": planned,
    }


# -------------------------------------------------------------------------
# Starting
# -------------------------------------------------------------------------


def _launch(dataset: dict[str, Any], plan: dict[str, Any], resources: dict[str, Any]) -> dict[str, Any]:
    """Delete what is being replaced, then write the record and run `sbatch`. `plan` is
    a startable result of `_plan_item`. Call it while holding `_LOCK`. Returns the
    record; `submit_error` in it says when `sbatch` failed."""
    dataset_id = dataset["id"]
    name = dataset["name"]

    cleared = False
    current = dataset.get("prepared")
    if current is not None and plan["reconvert"]:
        # The registry changes first, so a folder that can't be deleted leaves the dataset
        # "not converted", as D6 says, and the folder is deleted by the next start.
        cleared = registry.clear_prepared(dataset_id, current["version_id"])
    dataset = registry.get_dataset(dataset_id)  # kept_versions may have changed

    deleted: list[str] = []
    for folder in _unused_folders(dataset):
        try:
            _delete_version_folder(dataset_id, folder.name)
        except ConversionError as exc:
            state = " The dataset now shows as not converted." if cleared else ""
            raise ConversionError(f"{exc}{state} Nothing was started for '{name}'.") from exc
        deleted.append(str(folder))

    version_id = jobs._new_batch_id()
    output_dir = _version_folder(dataset_id, version_id)
    jobs_folder = _dataset_folder(dataset_id) / JOBS_FOLDER
    logs = _logs_folder(dataset_id)
    jobs_folder.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    # The empty version folder is made here, not by the job: a folder that was looked at
    # before it existed is remembered as "not there" by the shared disk for about a minute,
    # which would hide the job's files. prepare_rows.py accepts an empty folder.
    try:
        output_dir.mkdir(parents=True)
    except OSError as exc:
        raise ConversionError(f"Couldn't create {output_dir} ({exc}). Nothing was started for '{name}'.") from exc

    args = jobs._prepare_args_for(_planned(dataset), output_dir)
    command, sbatch_command = _commands(dataset, args, resources)
    record: dict[str, Any] = {
        "job_id": version_id,
        "dataset_id": dataset_id,
        "label": name,
        "created_at": jobs._now_iso(),
        "run_dir": str(output_dir),
        "input_path": dataset["path"],
        "conversion": dataset["conversion"],
        "reconvert": plan["reconvert"],
        "prepare_args": args,
        "command": command,
        "sbatch_command": sbatch_command,
        "resources": resources,
        "deleted": deleted,
        # The names below are the ones jobs._decide_outcome and jobs._fresh_files read.
        "skipped": None,
        "slurm_job_id": None,
        "submitted_at": None,
        "submit_error": None,
        "out_log": None,
        "err_log": None,
        "result": None,
    }
    record_path = jobs_folder / f"{version_id}.json"
    # Written before sbatch, so a crash part-way still leaves a record.
    jobs._write_json_atomic(record_path, record)
    jobs._submit_one(logs, resources, record)
    jobs._write_json_atomic(record_path, record)
    return record


def start_conversion(
    dataset_id: str, reconvert: bool = False, resources: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Start the conversion of one dataset with its saved settings and return its record
    (`job_id` and `dataset_id` identify it for `conversion_status`; `submit_error` is set
    when `sbatch` failed). Phase 4 calls this too, so a scan can wait for the job.

    Raises ConversionError, saying why, when the dataset has no saved settings ("Set up
    conversion first"), is already converted and `reconvert` is false, is unavailable,
    has a conversion queued or running, or its input can't be read. SlurmUnavailable
    when SLURM can't be asked. With `reconvert`, the current version is deleted first,
    or kept in place when it is marked `kept` (D6). Failed attempts are deleted too.
    `resources` defaults to jobs.DEFAULT_RESOURCES."""
    with _LOCK:
        checked = jobs.validate_resources(_resources_or_default(resources))
        active = jobs.active_jobs()
        _finalize(dataset_id, lambda: active)
        dataset = registry.get_dataset(dataset_id)
        plan = _plan_item(dataset, reconvert is True, checked, active, None)
        if not plan["can_start"]:
            raise ConversionError(f"'{plan['name']}': {plan['reason']}")
        return _launch(dataset, plan, checked)


def submit(items_in: Any, resources_in: dict[str, Any] | None) -> dict[str, Any]:
    """Start the conversions of several datasets. Every dataset is checked first, and the
    whole request is refused if any one of them can't be started."""
    items = _validated_items(items_in)
    with _LOCK:
        resources = jobs.validate_resources(_resources_or_default(resources_in))
        active = jobs.active_jobs()
        plans: list[tuple[dict[str, Any], dict[str, Any]]] = []
        problems: list[str] = []
        for item in items:
            _finalize(item["id"], lambda: active)
            dataset = registry.get_dataset(item["id"])
            plan = _plan_item(dataset, item["reconvert"], resources, active, None)
            if not plan["can_start"]:
                problems.append(f"'{plan['name']}': {plan['reason']}")
            plans.append((dataset, plan))
        if problems:
            raise ConversionError("Nothing was started. " + " ".join(problems))

        started: list[dict[str, Any]] = []
        errors: list[str] = []
        for dataset, plan in plans:
            try:
                record = _launch(dataset, plan, resources)
            except ConversionError as exc:
                errors.append(str(exc))
                continue
            started.append(
                {
                    "dataset_id": dataset["id"],
                    "name": dataset["name"],
                    "job_id": record["job_id"],
                    "slurm_job_id": record["slurm_job_id"],
                }
            )
            if record["submit_error"]:
                errors.append(f"'{dataset['name']}': {record['submit_error']}")
        return {"started": started, "errors": errors}


# -------------------------------------------------------------------------
# Watching
# -------------------------------------------------------------------------


def _slurm_for(
    records: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], str | None]:
    """`(squeue jobs, sacct records, error)` for the records that haven't finished. Nothing
    is asked of SLURM when there are none."""
    ids = _slurm_ids([record for record in records if record.get("result") is None])
    if not ids:
        return {}, {}, None
    return jobs._read_slurm(ids)


def _view(
    dataset: dict[str, Any],
    record: dict[str, Any],
    active: dict[str, dict[str, Any]] | None,
    accounting: dict[str, dict[str, Any]] | None,
    with_logs: bool,
) -> dict[str, Any]:
    slurm_id = record.get("slurm_job_id")
    listed = active.get(slurm_id) if active is not None and isinstance(slurm_id, str) else None
    sacct = accounting.get(slurm_id) if accounting is not None and isinstance(slurm_id, str) else None

    # Nothing is looked for on disk while the job is queued, and the summary only once SLURM
    # no longer lists it (see the module docstring).
    queued = listed is not None and listed["state"] == "PENDING"
    finished = active is not None and listed is None and isinstance(slurm_id, str)
    progress = _fresh_progress(record) if isinstance(slurm_id, str) and not queued else None
    summary = _fresh_summary(record) if finished else None
    if record.get("result") is not None:
        outcome, reason = "succeeded", None
    else:
        outcome, reason = jobs._decide_outcome(record, active, accounting, lambda: (progress, summary))

    out_tail = err_tail = out_log = err_log = None
    if isinstance(slurm_id, str) and slurm_id.isdigit():
        # Built from the job ID, not read from the record, so a record can't point this
        # at some other file.
        out_log = _logs_folder(dataset["id"]) / f"{slurm_id}.out"
        err_log = _logs_folder(dataset["id"]) / f"{slurm_id}.err"
        if with_logs and not queued:
            out_tail = jobs._tail_file(out_log)
            err_tail = jobs._tail_file(err_log)

    prepared = dataset.get("prepared")
    return {
        "job_id": record["job_id"],
        "dataset_id": dataset["id"],
        "created_at": record.get("created_at"),
        "submitted_at": record.get("submitted_at"),
        "outcome": outcome,
        "reason": reason,
        "current": isinstance(prepared, dict) and prepared.get("version_id") == record["job_id"],
        "folder": record["run_dir"],
        "folder_exists": Path(record["run_dir"]).is_dir(),
        "reconvert": record.get("reconvert") is True,
        "conversion": record.get("conversion"),
        "resources": record.get("resources"),
        "command": record.get("command"),
        "sbatch_command": record.get("sbatch_command"),
        "deleted": record.get("deleted") or [],
        "result": record.get("result"),
        "slurm_job_id": slurm_id,
        "slurm_state": (listed or {}).get("state") or (sacct or {}).get("state_text"),
        "exit_code": (sacct or {}).get("exit_code"),
        "elapsed": (listed or {}).get("elapsed") or (sacct or {}).get("elapsed"),
        "submit_error": record.get("submit_error"),
        "progress": progress,
        "may_have_stopped": progress is not None and runs.may_have_stopped(progress),
        "summary": jobs._summary_view(summary),
        "out_log": str(out_log) if out_log else None,
        "err_log": str(err_log) if err_log else None,
        "out_tail": out_tail,
        "err_tail": err_tail,
    }


def list_conversions(dataset_id: str) -> dict[str, Any]:
    """Every conversion of one dataset, newest first, for its History tab."""
    registry.get_dataset(dataset_id)  # an unknown or malformed id fails here
    records, _problems = _read_records(dataset_id)
    # SLURM is read before the files, so a job seen as finished has its files written.
    active, accounting, slurm_error = _slurm_for(records)
    refresh(dataset_id, lambda: active)
    dataset = registry.get_dataset(dataset_id)
    records, problems = _read_records(dataset_id)  # a conversion may just have become current
    views = [_view(dataset, record, active, accounting, False) for record in records]
    return {
        "dataset_id": dataset_id,
        "slurm_error": slurm_error,
        "problems": problems,
        "has_active": any(view["outcome"] in jobs.ACTIVE_OUTCOMES for view in views),
        "conversions": views,
    }


def list_all() -> dict[str, Any]:
    """Every conversion of every registered dataset, newest first, for the Activity page. One
    squeue and one sacct cover all of them. Each entry is what `list_conversions` shows, plus
    `dataset_name`."""
    records: list[dict[str, Any]] = []
    for dataset_id in registry.dataset_names():
        records.extend(_read_records(dataset_id)[0])
    # SLURM is read before the files, so a job seen as finished has its files written.
    active, accounting, slurm_error = _slurm_for(records)
    for dataset_id in registry.unconverted_ids():
        refresh(dataset_id, lambda: active)

    # A conversion may just have become current, so the datasets and records are read again.
    problems: list[str] = []
    views: list[dict[str, Any]] = []
    for dataset in registry.list_datasets():
        found, notes = _read_records(dataset["id"])
        problems.extend(f"{dataset['name']}: {note}" for note in notes)
        for record in found:
            views.append({**_view(dataset, record, active, accounting, False), "dataset_name": dataset["name"]})
    # The ids start with the time they were made, so this is newest first.
    views.sort(key=lambda view: view["job_id"], reverse=True)
    return {
        "slurm_error": slurm_error,
        "problems": problems,
        "has_active": any(view["outcome"] in jobs.ACTIVE_OUTCOMES for view in views),
        "conversions": views,
    }


def conversion_status(dataset_id: str, job_id: str) -> dict[str, Any]:
    """One conversion with its progress, summary and log tails. When it has just finished
    and passed its checks, this is also what makes it the dataset's current version."""
    registry.get_dataset(dataset_id)  # an unknown or malformed id fails here
    record = _load_record(dataset_id, job_id)
    # SLURM is read before the files, so a job seen as finished has its files written.
    active, accounting, slurm_error = _slurm_for([record])
    refresh(dataset_id, lambda: active)
    dataset = registry.get_dataset(dataset_id)
    record = _load_record(dataset_id, job_id)
    return {**_view(dataset, record, active, accounting, True), "slurm_error": slurm_error}


def cancel(dataset_id: str, job_id: str) -> dict[str, Any]:
    """`scancel` the one SLURM job a conversion record lists, if it is still going. The
    folder of a cancelled attempt is kept until the next conversion of the dataset."""
    registry.get_dataset(dataset_id)
    record = _load_record(dataset_id, job_id)
    slurm_id = record.get("slurm_job_id")
    if not isinstance(slurm_id, str) or not slurm_id.isdigit():
        raise ConversionError("This conversion has no SLURM job to cancel.")
    if slurm_id not in jobs.active_jobs():
        return {"cancelled": [], "not_active": [slurm_id]}
    result = jobs._run(["scancel", slurm_id])
    if result.returncode != 0:
        raise ConversionError(f"scancel failed: {jobs._failure_detail(result)}")
    return {"cancelled": [slurm_id], "not_active": []}


# -------------------------------------------------------------------------
# Unregistering
# -------------------------------------------------------------------------


def unregister(dataset_id: str) -> dict[str, Any]:
    """`registry.unregister`, refused while a conversion of the dataset is queued or
    running. SLURM is only asked when the dataset has started a conversion before."""
    registry.get_dataset(dataset_id)  # an unknown or malformed id fails here
    with _LOCK:
        ids = _slurm_ids(_read_records(dataset_id)[0])
        if ids:
            # Raises SlurmUnavailable: a running job can't be ruled out without squeue.
            active = jobs.active_jobs()
            running = [slurm_id for slurm_id in ids if slurm_id in active]
            if running:
                raise ConversionError(
                    f"A conversion of this dataset is queued or running (SLURM job {', '.join(running)}). "
                    "Cancel it first."
                )
        return registry.unregister(dataset_id)
