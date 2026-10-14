"""FastAPI backend for the input validator UI.

Started via ../run.sh, which sets VALIDATOR_FRONTEND_DIST and binds this
process to 127.0.0.1 only. There is no per-request auth and no path
allowlist: any absolute path the running user can read can be inspected,
and anyone who can already reach this machine's localhost (e.g. another
user logged into the same login node) can reach this too. That's a
deliberate choice for a trusted-network deployment.

Only these routes act on anything: the /api/jobs/* ones (jobs.py) and the dataset
registry ones (registry.py). /api/jobs/submit starts SLURM conversion jobs (one
`prepare_rows.py` run each, CPU only), /api/jobs/cancel cancels them,
/api/jobs/delete deletes run folders (only inside the runs root, after the checks in
jobs.py), and submit also writes batch records under <runs root>/_jobs.
/api/jobs/settings, /api/jobs/check, /api/jobs/batches and /api/jobs/status only read.

The dataset registry routes: POST /api/datasets and PATCH /api/datasets/{id} write
<service root>/registry/datasets.json, and POST /api/datasets/{id}/unregister removes an
entry and deletes that dataset's own folder <service root>/datasets/<id>, after checking
the id and the folder's location. The first read of a new service root also writes
datasets.json, from the catalog in registry_seed.py. The service root is
$VALIDATOR_SERVICE_ROOT, or <repo>/output/service. GET /api/teams, GET /api/datasets and
GET /api/datasets/{id} only read, and only check that a dataset's path exists and can be
read. Every other route is read-only, and none of them ever writes to or deletes
original data.

This process never writes to the data it inspects: /api/inspect and
/api/preview only read a bounded sample and report what would happen,
they never run prepare_rows.py's actual write step. The Prepared-run routes
(/api/runs, /api/run, /api/run/progress, /api/run/skipped, /api/origin)
are read-only too: /api/runs lists the run folders (Prepared and filter runs)
under the runs root and the folders of each chain (runs.read_roots), reading
only each one's run_identity.json, summary.json and progress.json;
/api/run/progress reads one run's progress.json and checks whether its
summary.json and _SUCCESS exist; the others read a run's summary.json and
skipped_rows.ndjson, and look up one original row, and only open a file
the run's own summary.json says it read. /api/prepared-row goes the other way
(from an Original row to its Prepared row): it reads the summary.json of the
runs under the folders the validator reads, and the Prepared and skipped-rows
files of the runs that read that file. (The Prepared rows tab reuses
/api/preview on a run's own written files.)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

import detect
import jobs
import preview
import registry
import runs
from reader_imports import json_safe

DEFAULT_SAMPLE_ROWS = 2
MAX_SAMPLE_ROWS = 200
DEFAULT_SKIPPED_PAGE_SIZE = 50
MAX_SKIPPED_PAGE_SIZE = 200

app = FastAPI(title="Input validator UI")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True}


# -------------------------------------------------------------------------
# The dataset registry (registry.py): /api/datasets (POST and PATCH) and
# /api/datasets/{id}/unregister act; the others only read.
# -------------------------------------------------------------------------


class DatasetCreateRequest(BaseModel):
    # Any: registry.py explains what's wrong in words, instead of the request
    # failing with a generic validation error.
    model_config = ConfigDict(extra="forbid")
    team: Any = None
    name: Any = None
    path: Any = None
    description: Any = None
    notes: Any = None
    conversion: Any = None


class DatasetUpdateRequest(BaseModel):
    # Only the fields that are sent are changed (see registry.update). The path can't be
    # changed: register the dataset again instead.
    model_config = ConfigDict(extra="forbid")
    team: Any = None
    name: Any = None
    description: Any = None
    notes: Any = None
    conversion: Any = None


def _registry_call(function: Any, *args: Any) -> Any:
    try:
        return function(*args)
    except registry.DatasetNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except registry.RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except registry.RegistryCorrupt as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/teams")
def teams() -> dict[str, Any]:
    """The fixed list of teams a dataset can belong to."""
    return {"teams": registry.TEAMS}


@app.get("/api/datasets")
def datasets_list(team: str | None = None) -> dict[str, Any]:
    """Every registered dataset (or one team's), each with whether its path can still be
    read. The first call on a new service root creates the registry from the old catalog."""
    return {
        "service_root": str(registry.service_root()),
        "datasets": _registry_call(registry.list_datasets, team),
    }


@app.get("/api/datasets/{dataset_id}")
def dataset_get(dataset_id: str) -> dict[str, Any]:
    return _registry_call(registry.get_dataset, dataset_id)


@app.post("/api/datasets")
def dataset_register(body: DatasetCreateRequest) -> dict[str, Any]:
    """Register a dataset: a team, a name and the absolute path of its original data."""
    return _registry_call(registry.register, body.model_dump(exclude_unset=True))


@app.patch("/api/datasets/{dataset_id}")
def dataset_update(dataset_id: str, body: DatasetUpdateRequest) -> dict[str, Any]:
    """Change a dataset's team, name, description, notes or saved conversion settings."""
    return _registry_call(registry.update, dataset_id, body.model_dump(exclude_unset=True))


@app.post("/api/datasets/{dataset_id}/unregister")
def dataset_unregister(dataset_id: str) -> dict[str, Any]:
    """Remove a dataset's entry and its folder under the service root. The original
    data is never touched."""
    return _registry_call(registry.unregister, dataset_id)


class InspectRequest(BaseModel):
    path: str


@app.post("/api/inspect")
def inspect(body: InspectRequest) -> dict[str, Any]:
    try:
        resolved = detect.resolve_input_path(body.path)
    except detect.InvalidPath as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return detect.inspect_path(resolved)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class PreviewRequest(BaseModel):
    path: str
    input_type: str
    file: str | None = None
    split: str = "train"
    # Only used for huggingface input: prepare_rows.py's --hf-config, shown
    # in the command (and submitted by the Start panel).
    hf_config: str = ""
    max_rows: int = DEFAULT_SAMPLE_ROWS
    # Which row to start the sample at (0-indexed) -- lets the UI's row
    # navigator look at an arbitrary position in the file instead of
    # always the first `max_rows` rows. See detect.sample_rows() for how
    # each format reaches it.
    start_row: int = 0
    # jsonl only: start at the line that begins at this byte instead of at
    # `start_row` (which counts non-blank rows, not lines). Lets "Open this
    # row in the preview" land on the exact line an _origin note names.
    start_byte: int | None = None
    map: dict[str, str] = Field(default_factory=dict)
    require: list[str] = Field(default_factory=list)
    exclude_metadata: list[str] = Field(default_factory=list)
    metadata_column: str = "metadata"
    output_dir: str = "./converted"


@app.post("/api/preview")
def run_preview(body: PreviewRequest) -> dict[str, Any]:
    try:
        resolved = detect.resolve_input_path(body.path)
    except detect.InvalidPath as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    max_rows = min(max(1, body.max_rows), MAX_SAMPLE_ROWS)
    start_row = max(0, body.start_row)
    start_byte = None if body.start_byte is None else max(0, body.start_byte)

    try:
        rows, source_info = detect.sample_rows(
            resolved, body.input_type, body.file, max_rows, body.split, start_row, start_byte
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # a real reader failure -- surface it to the UI, don't 500 blindly
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc

    # The structure tree is still built -- suggest_mapping() needs it -- but
    # it's not sent to the browser. The UI shows these same sample rows
    # directly (as "input_rows" below) instead of a tree summarizing them.
    structure = preview.build_structure_tree(rows)
    suggestion = preview.suggest_mapping(structure)
    result = preview.preview_rows(
        rows, body.map, body.require, body.exclude_metadata, body.metadata_column
    )
    command = preview.build_command(
        input_path=str(resolved),
        input_type=body.input_type,
        file=body.file,
        mapping=body.map,
        required=body.require,
        exclude_metadata=body.exclude_metadata,
        output_dir=body.output_dir,
        split=body.split,
        hf_config=body.hf_config,
    )

    return {
        "source_info": source_info,
        # The exact rows _prepare_row() receives -- same cleaned sample,
        # just made JSON-safe for the browser (see origin.json_safe).
        "input_rows": [json_safe(row) for row in rows],
        "suggested_mapping": suggestion,
        "preview": result,
        "command": command,
    }


@app.get("/api/runs")
def list_runs() -> dict[str, Any]:
    """Every folder the validator reads (the runs root, and the folders of each
    chain; see runs.read_roots), whether it exists, and every prepare_rows.py or
    filter_rows.py run folder under it (see runs.list_runs)."""
    return runs.list_all_runs()


class RunRequest(BaseModel):
    run_dir: str


@app.post("/api/run")
def open_run(body: RunRequest) -> dict[str, Any]:
    """A Prepared run's or a filter run's summary.json and run_identity.json.
    `kind` says which."""
    try:
        return runs.open_any_run(detect.resolve_input_path(body.run_dir))
    except (detect.InvalidPath, runs.RunError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/run/progress")
def run_progress(body: RunRequest) -> dict[str, Any]:
    """How far a run has got: its progress.json (or null), whether its
    summary.json and _SUCCESS exist, and whether it may have stopped."""
    try:
        return runs.run_progress(detect.resolve_input_path(body.run_dir))
    except detect.InvalidPath as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class SkippedRequest(BaseModel):
    run_dir: str
    page: int = 1
    page_size: int = DEFAULT_SKIPPED_PAGE_SIZE
    reason: str | None = None
    file: str | None = None


@app.post("/api/run/skipped")
def skipped_rows(body: SkippedRequest) -> dict[str, Any]:
    """One page of a run's skipped_rows.ndjson, optionally filtered by
    reason and by original file."""
    page = max(1, body.page)
    page_size = min(max(1, body.page_size), MAX_SKIPPED_PAGE_SIZE)
    try:
        run = runs.open_run(detect.resolve_input_path(body.run_dir))
        return runs.skipped_page(run, page, page_size, body.reason, body.file)
    except (detect.InvalidPath, runs.RunError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class OriginRequest(BaseModel):
    origin: dict[str, Any]
    # The run the row belongs to (a Prepared run, or a filter run, which is
    # followed back to its Prepared run) -- or, for a row viewed outside a
    # run, the file it was shown from (its run is found by walking up from
    # there).
    run_dir: str | None = None
    prepared_path: str | None = None


@app.post("/api/origin")
def lookup_origin(body: OriginRequest) -> dict[str, Any]:
    """The original row an _origin note points at, as it is now, with one
    status: unchanged, changed or not_found."""
    try:
        run_dir = detect.resolve_input_path(body.run_dir) if body.run_dir else None
        prepared_path = detect.resolve_input_path(body.prepared_path) if body.prepared_path else None
        return runs.lookup(body.origin, runs.prepared_run_for(run_dir, prepared_path))
    except (detect.InvalidPath, runs.RunError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # a real reader failure -- surface it to the UI, don't 500 blindly
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc


class PreparedRowRequest(BaseModel):
    # The Original file as the runs recorded it (an Arrow folder, for one), and the
    # row in it: a JSONL file's rows are named by `line` (from 1), every other
    # kind's by `row` (from 0).
    original_path: str
    row: int | None = None
    line: int | None = None
    split: str | None = None
    # Ask one run (a Prepared run, or a filter run, which stands for the Prepared
    # run it came from) instead of every Prepared run that read the file.
    run_dir: str | None = None


@app.post("/api/prepared-row")
def prepared_row(body: PreparedRowRequest) -> dict[str, Any]:
    """Where an Original row is in the Prepared data: found (with the file and
    line), skipped, left out by --max-rows, or not in the file. Each answer says
    whether it was worked out (and checked) or searched for."""
    try:
        original_path = detect.resolve_input_path(body.original_path)
        run_dir = detect.resolve_input_path(body.run_dir) if body.run_dir else None
        return runs.find_prepared(original_path, body.row, body.line, body.split, run_dir)
    except (detect.InvalidPath, runs.RunError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# -------------------------------------------------------------------------
# Starting, watching, cancelling and deleting conversions (jobs.py) -- these
# routes act on things, with the dataset registry routes above.
# -------------------------------------------------------------------------


class PlannedRun(BaseModel):
    # Catalog entries send team, group and label (they decide the suggested
    # folder name); a custom path sends none of them.
    label: str | None = None
    team: str | None = None
    group: str | None = None
    input_path: str
    input_type: str
    file: str | None = None
    split: str = "train"
    hf_config: str = ""
    map: dict[str, str] = Field(default_factory=dict)
    require: list[str] = Field(default_factory=list)
    exclude_metadata: list[str] = Field(default_factory=list)
    # Relative to the runs root; null means the suggested folder name.
    run_folder: str | None = None
    # Only read by /api/jobs/submit: run it again even if it's already done.
    run_again: bool = False


class JobResources(BaseModel):
    # Any: jobs.validate_resources() explains what's wrong in words, instead
    # of the request failing with a generic validation error.
    partition: Any = None
    cpus: Any = None
    mem: Any = None
    time: Any = None


class CheckRequest(BaseModel):
    runs: list[PlannedRun]
    resources: JobResources | None = None


class SubmitRequest(BaseModel):
    runs: list[PlannedRun]
    resources: JobResources


class DeleteRequest(BaseModel):
    run_dirs: list[str]


class BatchRequest(BaseModel):
    batch_id: str


class CancelRequest(BaseModel):
    batch_id: str
    job_ids: list[str] | None = None


def _jobs_call(function: Any, *args: Any) -> Any:
    try:
        return function(*args)
    except jobs.JobsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except jobs.SlurmUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/jobs/settings")
def jobs_settings() -> dict[str, Any]:
    """The runs root, the partitions `sinfo` lists, and the default resources."""
    return _jobs_call(jobs.settings)


@app.post("/api/jobs/check")
def jobs_check(body: CheckRequest) -> dict[str, Any]:
    """For each planned run: its folder, what's already in it, and the exact
    commands. Read-only."""
    planned = [run.model_dump() for run in body.runs]
    resources = body.resources.model_dump() if body.resources is not None else None
    return _jobs_call(jobs.check, planned, resources)


@app.post("/api/jobs/delete")
def jobs_delete(body: DeleteRequest) -> dict[str, Any]:
    """Delete run folders inside the runs root (jobs.delete_folders)."""
    return _jobs_call(jobs.delete_folders, body.run_dirs)


@app.post("/api/jobs/submit")
def jobs_submit(body: SubmitRequest) -> dict[str, Any]:
    """Start one SLURM job per planned run, as one batch (jobs.submit)."""
    planned = [run.model_dump() for run in body.runs]
    return _jobs_call(jobs.submit, planned, body.resources.model_dump())


@app.get("/api/jobs/batches")
def jobs_batches() -> dict[str, Any]:
    """Every batch, newest first, with its count of jobs per outcome."""
    return _jobs_call(jobs.list_batches)


@app.post("/api/jobs/status")
def jobs_status(body: BatchRequest) -> dict[str, Any]:
    """Each job of one batch: its outcome, progress, summary and log tails."""
    return _jobs_call(jobs.batch_status, body.batch_id)


@app.post("/api/jobs/cancel")
def jobs_cancel(body: CancelRequest) -> dict[str, Any]:
    """`scancel` a batch's queued or running jobs (all, or the IDs given)."""
    return _jobs_call(jobs.cancel, body.batch_id, body.job_ids)


# Serves the built frontend (validator/frontend/dist, built by run.sh) at
# every path /api/ doesn't claim. Registered last -- FastAPI/Starlette try
# routes in the order they were added, so the explicit /api/* routes above
# always get first chance to match.
_frontend_dist = os.environ.get("VALIDATOR_FRONTEND_DIST")
if _frontend_dist and Path(_frontend_dist).is_dir():
    app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="frontend")
