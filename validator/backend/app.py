"""FastAPI backend of the dataset scan service.

Started via ../run.sh, which sets VALIDATOR_FRONTEND_DIST and binds this
process to 127.0.0.1 only. There is no per-request auth and no path
allowlist: any absolute path the running user can read can be registered or
inspected, and anyone who can already reach this machine's localhost (e.g.
another user logged into the same login node) can reach this too. That's a
deliberate choice for a trusted-network deployment.

The service root is $VALIDATOR_SERVICE_ROOT, or <repo>/output/service. Everything this
process writes is under it, plus the NER and toxicity scans' own SLURM logs in the repo's
logs/slurm/. It never writes to, moves or deletes original data (the paths datasets point
at); it only checks that they exist and can be read, and reads bounded samples of them.
The routes below that act on anything are the registry, conversion, scan, result "check"
and drop ones. Every other route is read-only, apart from the background tasks described
with each group.

Dataset registry (registry.py): POST /api/datasets and PATCH /api/datasets/{id} write
<service root>/registry/datasets.json, and POST /api/datasets/{id}/unregister removes an
entry and deletes that dataset's own folder <service root>/datasets/<id>, after checking
the id and the folder's location. Unregistering is refused while a conversion, a scan or
a filter job of the dataset is going, and while the dataset has filtered versions; the scan
results only that dataset used are deleted afterwards (see the scan routes). The first read
of a new service root also writes datasets.json, from the catalog in registry_seed.py.
GET /api/teams, GET /api/datasets and GET /api/datasets/{id} only check that a dataset's
path exists and can be read.

Conversions (conversions.py): POST /api/conversions/submit starts one SLURM job per dataset
(one `prepare_rows.py` run each, CPU only), writing into
<service root>/datasets/<id>/prepared/<version id>/, with the job's record in .../_jobs and
its logs in .../_logs. With "convert again" it first deletes the dataset's current Prepared
version (setting it aside instead when a filtered dataset was made from it), and it always
deletes the folders of failed or cancelled attempts, both only inside that dataset's
`prepared` folder. POST /api/datasets/{id}/conversions/{job_id}/cancel runs `scancel` on the
one SLURM job the record lists. When a conversion has finished and passed its checks, reading
a dataset or a conversion saves it as the dataset's current Prepared version in
datasets.json. GET /api/conversions[/settings], POST /api/conversions/check and
GET /api/datasets/{id}/conversions[/{job_id}] only read, apart from that.

Scans (scans.py, scanners.py): POST /api/scans/submit first starts the conversions the
chosen datasets need (as above, one job each), then the scans. Duplicates and the rule-based
PII scan are one SLURM job each (`sbatch` on launchers/slurm/run_dedup_scan.sbatch or
run_pii_scan_and_extract.sbatch, CPU only); the NER PII scan runs
launchers/slurm/submit_privacy_filter.sh, which submits a CPU job that plans the work and the
GPU job that waits for it (only the partition is chosen: the job scripts fix the rest); the
toxicity scan runs scripts/tool_team_toxicity_scan.sh, which submits a CPU job that plans the
shards and, behind it, an array job with one GPU task for each shard (the CPUs, memory and
time of a shard are chosen). A GPU scan is refused without the confirmation it asks for. Each
scan writes into <service root>/scans/<scan id>/ (its `scan.json`, `inputs/`, `results/` and
`_logs/`). A scan that has to wait for a conversion is saved without a job and started by the
service itself, within 30 seconds of the conversion finishing (a background task, and whenever
scans or datasets are read); one whose conversion failed is never started. POST
/api/scans/{id}/cancel runs `scancel` on the jobs the scan's record lists (and only those), or
stops a waiting scan from ever starting. When a scan has finished, reading scans or datasets
saves it in datasets.json as the current result of each of its datasets for that scanner,
replacing the earlier one. A scan folder is deleted only when it was a result and nothing
points at it any more (replaced, or its dataset converted again or unregistered), only inside
<service root>/scans, and never one that is going, failed or was cancelled. A scan only reads
Prepared versions. A conversion that replaces a Prepared version (convert again) is refused
while a scan that reads or waits for the dataset is going. GET /api/scanners,
POST /api/scans/check and GET /api/scans[/{id}] only read, apart from starting and finishing
scans as above.

Results (results.py, flagged.py): when a scan has become a dataset's result, a background
task (and POST /api/datasets/{id}/results/{scanner}/check) submits one CPU SLURM job over it
(`sbatch` on launchers/slurm/run_result_analysis.sbatch, which runs
modelsafety/results/analyse.py). It writes `coverage.json` (the coverage report for that
scan), `tally.json` (counts the summary files don't hold) and each dataset's findings index
(`index/<dataset id>.jsonl`, one line for each flagged row, and its counts file) in the scan's
own folder, and the service copies the dataset's count and its coverage verdict into its
result in datasets.json. GET /api/datasets/{id}/results[/{scanner}] only read those small
files and the scanner's summary files. GET .../results/{scanner}/flagged pages the index, and
.../flagged/row opens one row, reading one line of its Prepared file at the byte offset the
index holds. Nothing is deleted.

Dropping rows (filters.py, autodrop.py): POST /api/datasets/{id}/drop/submit turns the rows
chosen on a dataset's result pages into a filtered version. It resolves the rows again from
the scans' findings indexes, marks the dataset's current Prepared version `kept` (so "convert
again" sets it aside instead of deleting it), registers a new dataset in the same team with
`parent` set, writes <service root>/datasets/<new id>/drop_list.jsonl and submits one CPU SLURM
job (`sbatch` on launchers/slurm/filter_rows.sbatch, which runs
modelsafety/filter/filter_rows.py with --drop-list) that copies the other rows byte for byte
into .../<new id>/prepared/<version id>/. The job's record is .../_jobs/filter.json and its
logs are in .../_logs. When the job has finished and its checks passed, that folder becomes
the new dataset's Prepared version (a background task, and whenever datasets or filter jobs
are read). POST .../drop/check only counts. POST /api/datasets/{id}/filter/cancel runs
`scancel` on the one job the record lists. The dataset the rows came from, its Prepared rows
and its results are never changed. A duplicates scan started with "auto_drop" does the same by
itself (a background task): when the scan's analysis has finished, if the coverage check
passed it makes one filtered version for every dataset that has flagged rows (never a
protected one), with every flagged row of the dataset, and the drop list says the rows were
dropped automatically; if the check failed, nothing is dropped. The decision is saved once in
the scan's own folder (auto_drop.json).

Looking at data: /api/inspect and /api/preview only read a bounded sample and report what
would happen; they never run prepare_rows.py's actual write step. /api/run and
/api/run/skipped read a Prepared version's (or a filter run's) summary.json and
skipped_rows.ndjson, and /api/origin looks up one original row from an `_origin` note. They
only open a file the version's own summary.json says it read.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

import autodrop
import conversions
import detect
import filters
import flagged
import jobs
import preview
import registry
import results
import runs
import scans
from reader_imports import json_safe

DEFAULT_SAMPLE_ROWS = 2
MAX_SAMPLE_ROWS = 200
DEFAULT_SKIPPED_PAGE_SIZE = 50
MAX_SKIPPED_PAGE_SIZE = 200


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Starts scans that were left waiting for a conversion, and keeps looking every 30 seconds.
    scans.start_background_refresh()
    # Submits the coverage check and counts of every finished scan that hasn't had them, and
    # copies the numbers of the ones that have finished, also every 30 seconds.
    results.start_background_refresh()
    # Makes the output of every finished filter job (rows dropped from a dataset) the new
    # dataset's Prepared version, also every 30 seconds.
    filters.start_background_refresh()
    # Drops what a duplicates scan flagged, by itself, for the scans that were started with
    # "drop duplicates automatically" and whose coverage check has passed, also every 30 seconds.
    autodrop.start_background_refresh()
    yield


app = FastAPI(title="Input validator UI", lifespan=lifespan)


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


def _conversions_call(function: Any, *args: Any) -> Any:
    """`_registry_call`, plus the errors of conversions.py and jobs.py."""
    try:
        return _registry_call(function, *args)
    except jobs.JobsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except jobs.SlurmUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _scans_call(function: Any, *args: Any) -> Any:
    """`_conversions_call`, plus the errors of scans.py."""
    try:
        return _conversions_call(function, *args)
    except scans.ScanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _results_call(function: Any, *args: Any) -> Any:
    """`_scans_call`, plus the errors of results.py."""
    try:
        return _scans_call(function, *args)
    except results.ResultsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/teams")
def teams() -> dict[str, Any]:
    """The fixed list of teams a dataset can belong to."""
    return {"teams": registry.TEAMS}


@app.get("/api/datasets")
def datasets_list(team: str | None = None) -> dict[str, Any]:
    """Every registered dataset (or one team's), each with whether its path can still be
    read, and, under `scans`, the state of each scanner on it. The first call on a new
    service root creates the registry from the old catalog. A conversion that has finished
    is made the dataset's current Prepared version first, and a scan that can start or has
    finished is started or made the dataset's result."""
    _conversions_call(conversions.refresh_all)
    _scans_call(scans.refresh)
    filters.refresh()
    datasets = _registry_call(registry.list_datasets, team)
    summaries = _scans_call(scans.dataset_summaries, datasets)
    filtered = _results_call(filters.dataset_summaries, datasets)
    return {
        "service_root": str(registry.service_root()),
        "datasets": [
            {**dataset, "scans": summaries[dataset["id"]], "filter": filtered.get(dataset["id"])} for dataset in datasets
        ],
    }


@app.get("/api/datasets/{dataset_id}")
def dataset_get(dataset_id: str) -> dict[str, Any]:
    # An id that isn't one is refused by get_dataset below, with its own message.
    if registry.DATASET_ID.fullmatch(dataset_id) is not None:
        _conversions_call(conversions.refresh, dataset_id)
        _scans_call(scans.refresh)
        filters.refresh()
    dataset = _registry_call(registry.get_dataset, dataset_id)
    return {
        **dataset,
        "scans": _scans_call(scans.dataset_summaries, [dataset])[dataset["id"]],
        # The state of this dataset's own filter job, if it is a filtered version, and the
        # filtered versions made from it.
        "filter": _results_call(filters.dataset_summaries, [dataset]).get(dataset["id"]),
        "filtered_versions": _results_call(filters.filtered_versions, dataset["id"]),
    }


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
    """Remove a dataset's entry and its folder under the service root, and the scan results
    only it used. Refused while a conversion, a scan or (for a filtered version) its filter job
    is going, and while the dataset has filtered versions of its own. The original data is
    never touched."""
    return _results_call(filters.unregister, dataset_id)


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


# -------------------------------------------------------------------------
# Starting, watching and cancelling the conversion of a dataset (conversions.py)
# -- the submit and cancel routes act on things, with the dataset registry routes
# above. A conversion uses the settings saved on the dataset, so none are sent here.
# -------------------------------------------------------------------------


class JobResources(BaseModel):
    # Any: jobs.validate_resources() explains what's wrong in words, instead
    # of the request failing with a generic validation error.
    partition: Any = None
    cpus: Any = None
    mem: Any = None
    time: Any = None


class ConversionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    # Delete the current Prepared version and convert again (D6).
    reconvert: bool = False


class ConversionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ConversionItem]
    # Null means the default resources (conversions.settings).
    resources: JobResources | None = None


def _items_and_resources(body: ConversionsRequest) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    items = [item.model_dump() for item in body.items]
    return items, body.resources.model_dump() if body.resources is not None else None


@app.get("/api/conversions/settings")
def conversions_settings() -> dict[str, Any]:
    """The partitions `sinfo` lists, and the default resources of a conversion."""
    return _conversions_call(conversions.settings)


@app.post("/api/conversions/check")
def conversions_check(body: ConversionsRequest) -> dict[str, Any]:
    """For each dataset: whether a conversion can start, what it would delete, and the
    commands. Starts and deletes nothing."""
    items, resources = _items_and_resources(body)
    return _conversions_call(conversions.check, items, resources)


@app.post("/api/conversions/submit")
def conversions_submit(body: ConversionsRequest) -> dict[str, Any]:
    """Start one SLURM job per dataset. The whole request is refused if any dataset
    can't be started, or if it converts a dataset again while a scan is going on it."""
    items, resources = _items_and_resources(body)
    _scans_call(scans.refuse_if_scanning, items)
    return _conversions_call(conversions.submit, items, resources)


@app.get("/api/conversions")
def conversions_list() -> dict[str, Any]:
    """Every conversion of every dataset, newest first (the Activity page)."""
    return _conversions_call(conversions.list_all)


@app.get("/api/datasets/{dataset_id}/conversions")
def dataset_conversions(dataset_id: str) -> dict[str, Any]:
    """Every conversion of one dataset, newest first."""
    return _conversions_call(conversions.list_conversions, dataset_id)


@app.get("/api/datasets/{dataset_id}/conversions/{job_id}")
def dataset_conversion(dataset_id: str, job_id: str) -> dict[str, Any]:
    """One conversion: its outcome, progress, summary and log tails."""
    return _conversions_call(conversions.conversion_status, dataset_id, job_id)


@app.post("/api/datasets/{dataset_id}/conversions/{job_id}/cancel")
def dataset_conversion_cancel(dataset_id: str, job_id: str) -> dict[str, Any]:
    """`scancel` the one SLURM job this conversion's record lists."""
    return _conversions_call(conversions.cancel, dataset_id, job_id)


# -------------------------------------------------------------------------
# Starting, watching and cancelling scans (scans.py, scanners.py) -- the submit and
# cancel routes act on things. A scan reads the Prepared versions of the datasets it
# covers, so the datasets that need it are converted first (a separate job each).
# -------------------------------------------------------------------------


class ScanDatasetItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    # Delete the dataset's current Prepared version and convert it again first (D6).
    reconvert: bool = False


class ScansRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Any for these: scans.py explains what's wrong in words, instead of the request
    # failing with a generic validation error.
    scanner: Any = None
    datasets: list[ScanDatasetItem]
    # "across" or "within" for the scanners that have modes (D4); null for the others.
    mode: Any = None
    settings: Any = None
    # Null means the scanner's own default resources (scans.settings).
    resources: JobResources | None = None
    confirm_gpu: bool = False
    # Drop what the scan flags by itself, once it has finished and its coverage check has passed
    # (D17). Any for the same reason as the fields above; only the duplicates scan offers it.
    auto_drop: Any = False


def _scan_arguments(body: ScansRequest) -> tuple[Any, ...]:
    items = [item.model_dump() for item in body.datasets]
    resources = body.resources.model_dump() if body.resources is not None else None
    return body.scanner, items, body.mode, body.settings, resources


@app.get("/api/scanners")
def scanners_list() -> dict[str, Any]:
    """The scanners with their settings, modes and default resources, and the partitions
    `sinfo` lists."""
    return _scans_call(scans.settings)


@app.post("/api/scans/check")
def scans_check(body: ScansRequest) -> dict[str, Any]:
    """For each scan the request would make: what each dataset needs first (use the converted
    data, convert, or convert again), the command, the cost and everything that stops it.
    Starts and deletes nothing."""
    return _scans_call(scans.check, *_scan_arguments(body), body.auto_drop)


@app.post("/api/scans/submit")
def scans_submit(body: ScansRequest) -> dict[str, Any]:
    """Start the conversions the datasets need and the scans: a scan whose datasets are all
    converted starts now, any other waits for its conversions. The whole request is refused
    if any scan can't be started."""
    return _scans_call(scans.submit, *_scan_arguments(body), body.confirm_gpu, body.auto_drop)


@app.get("/api/scans")
def scans_list() -> dict[str, Any]:
    """Every scan, newest first, with its state read live."""
    return _scans_call(scans.list_scans)


@app.get("/api/scans/{scan_id}")
def scans_get(scan_id: str) -> dict[str, Any]:
    """One scan in full: its command, the versions it read, what it found, and its log tails."""
    return _scans_call(scans.get_scan, scan_id)


@app.post("/api/scans/{scan_id}/cancel")
def scans_cancel(scan_id: str) -> dict[str, Any]:
    """Stop a scan: `scancel` on the one job its record lists, or, for a scan still waiting
    for its conversions, stop it from ever starting. The conversions keep running."""
    return _scans_call(scans.cancel, scan_id)


# -------------------------------------------------------------------------
# A dataset's results (results.py): what each scanner found in it, and whether the scan read
# exactly its data. Only "check" acts: it submits the one CPU analysis job again.
# -------------------------------------------------------------------------


@app.get("/api/datasets/{dataset_id}/results")
def dataset_results(dataset_id: str) -> dict[str, Any]:
    """Every scanner's result for the dataset: the numbers for this dataset only, the coverage
    verdict and the state of the analysis that made them. Reads small files only."""
    return _results_call(results.dataset_results, dataset_id)


@app.get("/api/datasets/{dataset_id}/results/{scanner}")
def dataset_result(
    dataset_id: str, scanner: str, categories: str | None = None, verdicts: str | None = None
) -> dict[str, Any]:
    """One scanner's result for the dataset, in full. For toxicity, `categories` and `verdicts`
    (comma-separated; empty means none) change what counts as flagged in the answer, and
    nothing else: nothing is saved. `auto_drop` says what came of the scan's automatic drop (D17):
    null when the scan wasn't started with it."""
    view = _results_call(results.dataset_result, dataset_id, scanner, categories, verdicts)
    # Not in results.py: the automatic drop makes filtered versions (filters.py), which imports it.
    view["auto_drop"] = autodrop.view_of(view["result"]["scan_id"]) if view.get("result") is not None else None
    return view


@app.post("/api/datasets/{dataset_id}/results/{scanner}/check")
def dataset_result_check(dataset_id: str, scanner: str) -> dict[str, Any]:
    """Run the dataset's result analysis for a scanner again: one CPU SLURM job that checks the
    scan's coverage (and counts, if they aren't there yet). Refused while one is going."""
    return _results_call(results.check_again, dataset_id, scanner)


# -------------------------------------------------------------------------
# A dataset's flagged rows (flagged.py): page through the findings index of a result, and open
# one row. Read-only.
# -------------------------------------------------------------------------


@app.get("/api/datasets/{dataset_id}/results/{scanner}/flagged")
def dataset_flagged(
    dataset_id: str,
    scanner: str,
    page: int = 1,
    page_size: int = flagged.DEFAULT_PAGE_SIZE,
    reason: str | None = None,
    name: str | None = None,
    categories: str | None = None,
    verdicts: str | None = None,
) -> dict[str, Any]:
    """One page of the result's flagged rows, from its findings index. `reason` and `name` (a NER
    label, a regex category, or the dataset a kept copy is in) filter the others; toxicity
    takes `categories` and `verdicts` (comma-separated; empty means none) instead."""
    return _results_call(
        flagged.flagged_page, dataset_id, scanner, page, page_size, reason, name, categories, verdicts
    )


@app.get("/api/datasets/{dataset_id}/results/{scanner}/flagged/row")
def dataset_flagged_row(
    dataset_id: str,
    scanner: str,
    file: str,
    line: int,
    categories: str | None = None,
    verdicts: str | None = None,
) -> dict[str, Any]:
    """One flagged row: its Prepared text, what was found and where in it, and for a duplicate
    the copy it matched. `file` and `line` are the row's place in the dataset's Prepared version."""
    return _results_call(flagged.flagged_row, dataset_id, scanner, file, line, categories, verdicts)


# -------------------------------------------------------------------------
# Dropping rows (filters.py): the rows chosen on a dataset's result pages become a filtered
# version, a new dataset made by one CPU SLURM job. "check" only counts; "submit" acts.
# -------------------------------------------------------------------------


class DropRequest(BaseModel):
    # Any: filters.py explains what's wrong in words, instead of the request failing with a
    # generic validation error. `selections` is a list with one entry per scanner:
    # {scanner, scan_id, rows: [{file, line}] | null, all_matching: {reason, name} | null,
    # categories, verdicts}.
    model_config = ConfigDict(extra="forbid")
    version_id: Any = None
    selections: Any = None


@app.post("/api/datasets/{dataset_id}/drop/check")
def dataset_drop_check(dataset_id: str, body: DropRequest) -> dict[str, Any]:
    """What dropping the chosen rows would do: the rows counted by scanner and reason, the name
    the new dataset would get, warnings, and the exact command. Starts nothing."""
    return _results_call(filters.check, dataset_id, body.model_dump(exclude_unset=True))


@app.post("/api/datasets/{dataset_id}/drop/submit")
def dataset_drop_submit(dataset_id: str, body: DropRequest) -> dict[str, Any]:
    """Drop the chosen rows: mark the dataset's converted data as kept, register the new
    filtered dataset, write its drop list and submit the filter job. Nothing in the dataset
    itself, its converted rows or its results changes."""
    return _results_call(filters.submit, dataset_id, body.model_dump(exclude_unset=True))


@app.get("/api/filters")
def filters_list() -> dict[str, Any]:
    """Every filter job of every filtered dataset, newest first (the Activity page)."""
    return _results_call(filters.list_all)


@app.get("/api/datasets/{dataset_id}/filter")
def dataset_filter(dataset_id: str) -> dict[str, Any]:
    """One filtered dataset's filter job: its state, what it drops, the command and log tails."""
    return _results_call(filters.status, dataset_id)


@app.post("/api/datasets/{dataset_id}/filter/cancel")
def dataset_filter_cancel(dataset_id: str) -> dict[str, Any]:
    """`scancel` the one SLURM job a filtered dataset's record lists."""
    return _results_call(filters.cancel, dataset_id)


# Serves the built frontend (validator/frontend/dist, built by run.sh) at
# every path /api/ doesn't claim. Registered last -- FastAPI/Starlette try
# routes in the order they were added, so the explicit /api/* routes above
# always get first chance to match.
_frontend_dist = os.environ.get("VALIDATOR_FRONTEND_DIST")
if _frontend_dist and Path(_frontend_dist).is_dir():
    app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="frontend")
