"""Results per dataset: the coverage check and the counts of a finished scan, and what a dataset's
results pages show.

When a scan has become a dataset's result (scans.py), this module runs one CPU SLURM job over
it, the *analysis* (`launchers/slurm/run_result_analysis.sbatch`, which runs
`modelsafety/results/analyse.py`). It checks the scan's coverage and counts what the scanner's
small summary files don't hold (toxicity by verdict and category, rows flagged by NER, where the
kept copy of a duplicate is), and makes each dataset's findings index: one line for each flagged
row, which flagged.py pages through. Everything it makes is in the scan's folder:

    <service root>/scans/<scan id>/
        analysis.json   what was submitted: the exact command, the job, when
        coverage.json   the coverage report for this scan (modelsafety/results/coverage.py)
        tally.json      the counts (for the scanners that need them)
        index/<dataset id>.jsonl         the flagged rows of each dataset
        index/<dataset id>.counts.json   and the counts a page filters by
        _logs/analysis-<job id>.out|.err

The pages then read only those small files, the index, and the scanner's own summary files,
never a whole result file.

Rules this module keeps:

* The only thing written is `analysis.json` (here), `coverage.json`, `tally.json` and the index
  (by the job), all inside the scan's folder, and two numbers on a dataset's result in
  datasets.json: its count and its coverage verdict (`registry.update_result`). Nothing is
  deleted. Prepared versions, scan results and original data are only read.
* Every SLURM call goes through jobs.py and scans.py's readers: an argument list, `shell=False`,
  a 30 second timeout. The job states its partition, CPUs, memory and time limit.
* Job state is never stored. It is read live from SLURM and from the files the job writes. What
  is saved is what was submitted and, once, that its numbers were copied into the registry
  (`applied_at`).
* The job's files are only looked for once SLURM no longer lists the job (on this cluster's
  shared disk, asking for a file before it exists makes this machine remember "not there" for
  about a minute). So nothing here asks for `coverage.json` or `tally.json` before then, and
  "Check again" is decided from the earlier record, not from the files.
* Every id, scanner key and filter value that comes from a request is checked against a fixed
  pattern or list before it is used, and every path is built from a scan's folder.
* One lock covers submitting and applying, so two requests can't submit two jobs for one scan.
  `refresh` skips instead of waiting when the lock is taken.
"""

from __future__ import annotations

import shlex
import sys
import threading
import time
from pathlib import Path
from typing import Any

import jobs
import registry
import scanners
import scans
from reader_imports import REPO_ROOT, read_json_or_none

ANALYSIS_FILE = "analysis.json"
COVERAGE_FILE = "coverage.json"
TALLY_FILE = "tally.json"
# Where the job writes each dataset's findings index, in the scan's folder: `<dataset id>.jsonl`
# and `<dataset id>.counts.json`.
INDEX_FOLDER = "index"
ANALYSIS_SCRIPT = REPO_ROOT / "launchers" / "slurm" / "run_result_analysis.sbatch"
# What the job asks for (the partition is the default one). The counts keep every flagged row of
# the scan in memory, hence the 32G.
ANALYSIS_RESOURCES = {"cpus": 4, "mem": "32G", "time": "04:00:00"}

# What the job is asked to do: the coverage check and the counts, or only the coverage check.
KIND_FULL = "full"
KIND_COVERAGE = "coverage"

# The name each scanner has in modelsafety/results/coverage.py.
COVERAGE_NAMES = {"dedup": "dedup", "pii_ner": "ner", "pii_regex": "regex_pii", "toxicity": "toxicity"}

# How often the background task looks for finished scans that haven't been analysed.
REFRESH_SECONDS = 30
# At most this many jobs are submitted in one pass, so a service that starts with a pile of
# finished scans doesn't flood the queue.
MAX_SUBMITS_PER_PASS = 10
# The most coverage problems one result lists.
MAX_PROBLEMS_SHOWN = 20

# The analysis hasn't been submitted yet. The other states are scans.py's.
STATE_PENDING = "pending"
GOING_STATES = (scans.STATE_QUEUED, scans.STATE_RUNNING, scans.STATE_UNKNOWN)

_LOCK = threading.Lock()


class ResultsError(ValueError):
    """The request can't be carried out; the message says why."""


def _log(message: str) -> None:
    print(f"[results] {message}", file=sys.stderr, flush=True)


# -------------------------------------------------------------------------
# The analysis record
# -------------------------------------------------------------------------


def _folder(scan: dict[str, Any]) -> Path:
    return scans._scan_folder(scan["id"])


def _analysis_path(scan_id: str) -> Path:
    return scans._scan_folder(scan_id) / ANALYSIS_FILE


def _is_analysis(data: Any, scan_id: str) -> bool:
    """Whether `data` is an analysis record whose job ID can safely go into a path or command."""
    if not isinstance(data, dict) or data.get("scan_id") != scan_id or data.get("kind") not in (KIND_FULL, KIND_COVERAGE):
        return False
    job_id = data.get("job_id")
    if job_id is not None and not (isinstance(job_id, str) and scans.SLURM_JOB_ID.fullmatch(job_id)):
        return False
    # A record made before the findings index existed has neither of these two.
    if any(key in data and not isinstance(data[key], bool) for key in ("writes_index", "has_index")):
        return False
    return isinstance(data.get("writes_tally"), bool) and isinstance(data.get("has_tally"), bool)


def _read_analysis(scan_id: str) -> dict[str, Any] | None:
    data = jobs._read_json(_analysis_path(scan_id))
    return data if _is_analysis(data, scan_id) else None


def _write_analysis(record: dict[str, Any]) -> None:
    jobs._write_json_atomic(_analysis_path(record["scan_id"]), record)


def _needs_tally(scan: dict[str, Any]) -> bool:
    needs = scanners.SCANNERS[scan["scanner"]].needs_tally
    return needs is not None and needs(scan["mode"])


def index_files(scan_folder: Path, dataset_id: str) -> tuple[Path, Path]:
    """The paths of a dataset's findings index in a scan's folder: the flagged rows, and the
    counts. `dataset_id` has to be a registry id, which is what makes it safe in a file name."""
    if registry.DATASET_ID.fullmatch(dataset_id) is None:
        raise ResultsError(f"'{dataset_id}' isn't a dataset id.")
    folder = scan_folder / INDEX_FOLDER
    return folder / f"{dataset_id}.jsonl", folder / f"{dataset_id}.counts.json"


def _is_across(scan: dict[str, Any]) -> bool:
    """Whether the scan read several datasets through one list of files (D4)."""
    return scan["mode"] == scanners.MODE_ACROSS and len(scan["datasets"]) > 1


def _dataset_run(scan: dict[str, Any], dataset_id: str) -> str:
    """The run the coverage report calls this dataset's Prepared version: "." when the scan read
    the version folder itself, otherwise its path under the datasets folder."""
    if not _is_across(scan):
        return "."
    version_id = scan["versions"][dataset_id]["version_id"]
    return f"{dataset_id}/prepared/{version_id}"


# -------------------------------------------------------------------------
# Submitting the analysis
# -------------------------------------------------------------------------


def _version_folder(dataset_id: str, version: Any) -> Path:
    """The Prepared version folder a scan's record says it read, checked to be where the
    registry puts it: the record is only a file."""
    version_id = version.get("version_id") if isinstance(version, dict) else None
    folder = Path(str(version.get("folder"))) if isinstance(version, dict) else None
    if not isinstance(version_id, str) or jobs.BATCH_ID.fullmatch(version_id) is None or folder is None:
        raise ResultsError(f"The scan's record has no usable Prepared version for dataset {dataset_id}.")
    if folder != registry.service_root() / "datasets" / dataset_id / "prepared" / version_id:
        raise ResultsError(f"The scan's record puts dataset {dataset_id}'s Prepared version in {folder}, which isn't where it is kept.")
    return folder


def _command(scan: dict[str, Any], kind: str) -> list[str]:
    """The `sbatch` call that runs the analysis of `scan`."""
    folder = _folder(scan)
    versions = scan.get("versions")
    if not isinstance(versions, dict):
        raise ResultsError("The scan's record doesn't say which Prepared versions it read.")
    for dataset_id in scan["datasets"]:
        if registry.DATASET_ID.fullmatch(dataset_id) is None:
            raise ResultsError(f"The scan's record names '{dataset_id}', which isn't a dataset id.")
        _version_folder(dataset_id, versions.get(dataset_id))

    args = ["--scanner", COVERAGE_NAMES[scan["scanner"]]]
    if _is_across(scan):
        # One scan over several datasets reads `<service root>/datasets` through its list of
        # files, so it is checked against that list and not against every dataset.
        root = registry.service_root() / "datasets"
        files_from = folder / scanners.INPUTS_FOLDER / scanners.DEDUP_MANIFEST
        args += ["--root", str(root), "--results-dir", str(folder / scanners.RESULTS_FOLDER)]
        args += ["--out-dir", str(folder), "--files-from", str(files_from)]
        if kind == KIND_FULL:
            # The groups name each dataset's files, for the tally and for the index.
            for dataset_id in scan["datasets"]:
                args += ["--group", f"{dataset_id}={dataset_id}/prepared/{versions[dataset_id]['version_id']}/"]
            # A dataset the scan only read as a reference (D15) has nothing flagged in it: no
            # index, nothing in the tally. Rows in the others can still point at its rows.
            for dataset_id in scans.reference_datasets_of(scan):
                args += ["--protect", dataset_id]
    elif len(scan["datasets"]) == 1:
        root = _version_folder(scan["datasets"][0], versions.get(scan["datasets"][0]))
        args += ["--root", str(root), "--results-dir", str(folder / scanners.RESULTS_FOLDER), "--out-dir", str(folder)]
        if kind == KIND_FULL:
            args += ["--index-name", scan["datasets"][0]]
    else:
        raise ResultsError("A scan over several datasets that isn't a comparison across them can't be analysed.")
    if kind == KIND_COVERAGE:
        args.append("--coverage-only")
    else:
        args += ["--index-dir", str(folder / INDEX_FOLDER)]

    scanners._check_export_values({"ANALYSE_REPO": str(REPO_ROOT)})
    logs = folder / scanners.LOGS_FOLDER
    return [
        "sbatch",
        "--parsable",
        f"--partition={jobs.DEFAULT_RESOURCES['partition']}",
        f"--cpus-per-task={ANALYSIS_RESOURCES['cpus']}",
        f"--mem={ANALYSIS_RESOURCES['mem']}",
        f"--time={ANALYSIS_RESOURCES['time']}",
        f"--job-name=analysis_{scan['id']}",
        f"--output={logs}/analysis-%j.out",
        f"--error={logs}/analysis-%j.err",
        f"--export=ALL,ANALYSE_REPO={REPO_ROOT}",
        str(ANALYSIS_SCRIPT),
        *args,
    ]


def _submit(scan: dict[str, Any], kind: str, earlier: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write the analysis record and run `sbatch` for it. Call it while holding `_LOCK`. What
    goes wrong is noted in the record's `submit_error` instead of raised, so one scan that
    can't be analysed doesn't stop the others. `earlier` is the record of an analysis that
    succeeded before, whose tally and index are still there (a check of the coverage only
    keeps them)."""
    writes_tally = kind == KIND_FULL and _needs_tally(scan)
    writes_index = kind == KIND_FULL
    record: dict[str, Any] = {
        "scan_id": scan["id"],
        "scanner": scan["scanner"],
        "kind": kind,
        "writes_tally": writes_tally,
        "writes_index": writes_index,
        "has_tally": writes_tally or bool(earlier and earlier.get("has_tally")),
        "has_index": writes_index or bool(earlier and earlier.get("has_index")),
        "submitted_at": None,
        "command": None,
        "command_text": None,
        "job_id": None,
        "submit_error": None,
        "wrapper_output": None,
        "applied_at": None,
    }
    try:
        argv = _command(scan, kind)
    except (ResultsError, scanners.ScannerError, KeyError, TypeError) as exc:
        record["submit_error"] = f"Nothing was started: {exc}"
        _write_analysis(record)
        return record

    record["command"] = argv
    record["command_text"] = shlex.join(argv)
    # Stamped and written before `sbatch` runs: the job can start before it returns, and a
    # crash part-way must not leave a scan that would be submitted a second time.
    record["submitted_at"] = jobs._now_iso()
    _write_analysis(record)
    try:
        result = jobs._run(argv, cwd=REPO_ROOT, env=scans._clean_environment())
    except jobs.SlurmUnavailable as exc:
        record["submit_error"] = f"{exc} The job may or may not have been submitted; check `squeue`."
    else:
        record["wrapper_output"] = (result.stdout + result.stderr)[-scans.MAX_OUTPUT_CHARS :]
        job_id = result.stdout.strip().split(";")[0]
        if result.returncode != 0:
            record["submit_error"] = f"sbatch failed: {jobs._failure_detail(result)}"
        elif scans.SLURM_JOB_ID.fullmatch(job_id) is None:
            record["submit_error"] = f"sbatch printed something unexpected: {result.stdout.strip()!r}"
        else:
            record["job_id"] = job_id
    _write_analysis(record)
    return record


# -------------------------------------------------------------------------
# The state of an analysis, read live
# -------------------------------------------------------------------------


def _expected_files(scan: dict[str, Any], record: dict[str, Any]) -> list[str]:
    """The files, relative to the scan's folder, that the analysis in `record` has to write."""
    expected = [COVERAGE_FILE]
    if record["writes_tally"]:
        expected.append(TALLY_FILE)
    if record.get("writes_index"):
        for dataset_id in scan["datasets"]:
            expected += [f"{INDEX_FOLDER}/{dataset_id}.jsonl", f"{INDEX_FOLDER}/{dataset_id}.counts.json"]
    return expected


def _outcome(
    scan: dict[str, Any],
    record: dict[str, Any] | None,
    active: dict[str, dict[str, Any]] | None,
    accounted: dict[str, list[dict[str, Any]]] | None,
) -> tuple[str, str | None]:
    """`(state, reason)` of a scan's analysis. It has succeeded when SLURM says the job
    completed and the files it should have written are there."""
    if record is None:
        return STATE_PENDING, None
    job_id = record.get("job_id")
    if job_id is None:
        return scans.STATE_FAILED, record.get("submit_error") or "The job was never submitted."
    state, reason = scans._slurm_outcome(job_id, active, accounted)
    if state == scans.STATE_SUCCEEDED:
        missing = [name for name in _expected_files(scan, record) if not (_folder(scan) / name).is_file()]
        if missing:
            return scans.STATE_FAILED, f"The job finished but didn't write {', '.join(missing)}. See its log."
    return state, reason


def _read_outputs(scan: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The coverage report and the tally (None for a scan that has none). Only call it for an
    analysis that has succeeded."""
    folder = _folder(scan)
    report = read_json_or_none(folder / COVERAGE_FILE)
    tally = read_json_or_none(folder / TALLY_FILE) if _needs_tally(scan) else None
    return (
        report if isinstance(report, dict) else None,
        tally if isinstance(tally, dict) else None,
    )


def _coverage_slice(report: dict[str, Any], scan: dict[str, Any], dataset_id: str) -> dict[str, Any]:
    """What the coverage report says about one dataset's Prepared version: its verdict, the
    problems that concern it (or the whole scan), and its row of numbers. A problem that isn't
    about one run fails every dataset of the scan, as the report itself says."""
    name = COVERAGE_NAMES[scan["scanner"]]
    run = _dataset_run(scan, dataset_id)
    per_run = report.get("per_run")
    row = next((item for item in per_run if isinstance(item, dict) and item.get("run") == run), None) if isinstance(per_run, list) else None
    scanners_of_row = row.get("scanners") if isinstance(row, dict) else None
    entry = scanners_of_row.get(name) if isinstance(scanners_of_row, dict) else None
    entry = entry if isinstance(entry, dict) else None
    status = entry.get("status") if entry is not None else None
    verdict = status if status in registry.COVERAGE_VERDICTS else "fail"

    all_problems = report.get("problems")
    problems = [
        problem
        for problem in (all_problems if isinstance(all_problems, list) else [])
        if isinstance(problem, dict) and problem.get("scanner") in (name, None) and problem.get("run") in (None, run)
    ]
    messages = [
        {"message": str(problem.get("message")), "path": problem["path"] if isinstance(problem.get("path"), str) else None}
        for problem in problems
    ]
    section = report.get("scanners", {}).get(name) if isinstance(report.get("scanners"), dict) else None
    if status == "not_run" and isinstance(section, dict) and isinstance(section.get("reason"), str):
        messages.append({"message": section["reason"], "path": None})
    if verdict == "fail" and not messages:
        messages.append({"message": "The coverage report has no entry for this dataset's converted data.", "path": None})
    return {
        "verdict": verdict,
        "problems": messages[:MAX_PROBLEMS_SHOWN],
        "problems_total": len(messages),
        "checked_at": report.get("created_at") if isinstance(report.get("created_at"), str) else None,
        "row": entry,
    }


# -------------------------------------------------------------------------
# A dataset's result, as the pages show it
# -------------------------------------------------------------------------


def _scanner_key(value: Any) -> str:
    if not isinstance(value, str) or value not in scanners.SCANNERS:
        raise ResultsError(f"'{value}' isn't a scanner. They are: {', '.join(scanners.SCANNERS)}.")
    return value


def _split_choice(text: str | None, allowed: tuple[str, ...], what: str) -> list[str] | None:
    """A comma-separated choice from the page: None when it wasn't given, [] when it was given
    empty. Anything not in `allowed` is refused."""
    if text is None:
        return None
    chosen = [item.strip() for item in text.split(",") if item.strip()]
    unknown = sorted(set(chosen) - set(allowed))
    if unknown:
        raise ResultsError(f"Unknown {what}: {', '.join(unknown)}. They are: {', '.join(allowed)}.")
    return [item for item in allowed if item in chosen]


def _results_of(dataset: dict[str, Any], keys: list[str]) -> dict[str, tuple[dict[str, Any], dict[str, Any] | None]]:
    """For each of `keys` that is the dataset's result: the scan's record and its analysis record."""
    found: dict[str, tuple[dict[str, Any], dict[str, Any] | None]] = {}
    results = dataset.get("results") or {}
    for key in keys:
        result = results.get(key)
        scan_id = result.get("scan_id") if isinstance(result, dict) else None
        scan = scans._read_scan(scan_id) if isinstance(scan_id, str) and jobs.BATCH_ID.fullmatch(scan_id) else None
        if scan is not None:
            found[key] = (scan, _read_analysis(scan["id"]))
    return found


def _analysis_view(
    scan: dict[str, Any],
    record: dict[str, Any] | None,
    state: str,
    reason: str | None,
    active: dict[str, dict[str, Any]] | None,
    accounted: dict[str, list[dict[str, Any]]] | None,
    detail: bool,
) -> dict[str, Any]:
    job_id = record.get("job_id") if record is not None else None
    view: dict[str, Any] = {
        "state": state,
        "reason": reason,
        "kind": record.get("kind") if record is not None else None,
        "job_id": job_id,
        "submitted_at": record.get("submitted_at") if record is not None else None,
        "elapsed": scans._slurm_elapsed(job_id, active, accounted) if job_id is not None else None,
    }
    if detail:
        view["command_text"] = record.get("command_text") if record is not None else None
        view["logs"] = []
        if job_id is not None:
            for label, suffix in (("Analysis output", "out"), ("Analysis errors", "err")):
                path = _folder(scan) / scanners.LOGS_FOLDER / f"analysis-{job_id}.{suffix}"
                # Nothing is looked for on disk while the job is queued (see the module docstring).
                view["logs"].append(
                    {"name": label, "path": str(path), "tail": jobs._tail_file(path) if state != scans.STATE_QUEUED else None}
                )
    return view


def _result_context(
    scan: dict[str, Any],
    dataset_id: str,
    names: dict[str, str],
    saved_count: Any,
    coverage_row: dict[str, Any] | None,
    tally: dict[str, Any] | None,
    categories: list[str] | None = None,
    verdicts: list[str] | None = None,
) -> scanners.ResultContext:
    return scanners.ResultContext(
        results=_folder(scan) / scanners.RESULTS_FOLDER,
        settings=scan["settings"],
        mode=scan["mode"],
        dataset_id=dataset_id,
        dataset_names={other: names.get(other, other) for other in scan["datasets"]},
        coverage=coverage_row,
        tally=tally,
        saved_count=saved_count if isinstance(saved_count, int) and not isinstance(saved_count, bool) else None,
        categories_choice=categories,
        verdicts_choice=verdicts,
    )


def _scanner_view(
    dataset: dict[str, Any],
    key: str,
    summary: dict[str, Any],
    found: dict[str, tuple[dict[str, Any], dict[str, Any] | None]],
    names: dict[str, str],
    slurm: tuple[dict[str, dict[str, Any]] | None, dict[str, list[dict[str, Any]]] | None],
    categories: list[str] | None = None,
    verdicts: list[str] | None = None,
    detail: bool = False,
) -> dict[str, Any]:
    scanner = scanners.SCANNERS[key]
    view: dict[str, Any] = {
        "scanner": key,
        "label": scanner.label,
        "count_label": scanner.count_label,
        "stages": list(scanner.stages),
        "summary": summary,
        "result": None,
    }
    if key not in found:
        return view
    scan, record = found[key]
    active, accounted = slurm
    state, reason = _outcome(scan, record, active, accounted)
    report, tally = _read_outputs(scan) if state == scans.STATE_SUCCEEDED else (None, None)
    coverage = _coverage_slice(report, scan, dataset["id"]) if report is not None else None
    row = coverage["row"] if coverage is not None else None
    saved = dataset["results"][key]
    body = scanner.read_result(_result_context(scan, dataset["id"], names, saved.get("count"), row, tally, categories, verdicts))
    view["result"] = {
        "scan_id": scan["id"],
        "finished_at": saved.get("finished_at"),
        "mode": scan["mode"],
        "settings": scan["settings"],
        # A dataset that isn't registered any more is kept, with no name, so "compared with" never
        # hides a partner.
        "datasets": [{"id": other, "name": names.get(other)} for other in scan["datasets"]],
        "analysis": _analysis_view(scan, record, state, reason, active, accounted, detail),
        # Whether the flagged rows of this dataset can be listed (flagged.py): only once the
        # analysis that wrote the index has succeeded.
        "has_index": state == scans.STATE_SUCCEEDED and record is not None and bool(record.get("has_index")),
        "coverage": None if coverage is None else {name: value for name, value in coverage.items() if name != "row"},
        "rows": {
            "scanned": row.get("rows_read") if row is not None else None,
            "with_text": row.get("rows_with_text") if row is not None else None,
            "without_text": row.get("rows_without_text") if row is not None else None,
        },
        # Lines of text about how the scan was made, `{label, text}`, for a scanner that has any.
        "notes": [],
        **body,
    }
    return view


def _slurm_for(found: dict[str, tuple[dict[str, Any], dict[str, Any] | None]]) -> tuple[Any, Any, str | None]:
    job_ids = [record["job_id"] for _scan, record in found.values() if record is not None and record.get("job_id")]
    if not job_ids:
        return {}, {}, None  # nothing to ask SLURM about
    return scans._read_slurm(job_ids)


def dataset_results(dataset_id: str) -> dict[str, Any]:
    """Every scanner's result for one dataset, for its Results tab. Only reads."""
    dataset = registry.get_dataset(dataset_id)
    summaries = scans.dataset_summaries([dataset])[dataset["id"]]
    names = registry.dataset_names()
    found = _results_of(dataset, list(scanners.SCANNERS))
    active, accounted, slurm_error = _slurm_for(found)
    prepared = dataset.get("prepared")
    return {
        "dataset_id": dataset["id"],
        "name": dataset["name"],
        "prepared": (
            {"version_id": prepared.get("version_id"), "rows": prepared.get("rows")} if isinstance(prepared, dict) else None
        ),
        "slurm_error": slurm_error,
        "scanners": {
            key: _scanner_view(dataset, key, summaries[key], found, names, (active, accounted)) for key in scanners.SCANNERS
        },
    }


def dataset_result(dataset_id: str, scanner_key: Any, categories: str | None = None, verdicts: str | None = None) -> dict[str, Any]:
    """One scanner's result for one dataset, for its result page: the same as one entry of
    `dataset_results`, with the analysis's command and logs. For toxicity, `categories` and
    `verdicts` (comma-separated, from the page) change what counts as flagged in what is shown,
    and nothing else: they are never saved (D8)."""
    key = _scanner_key(scanner_key)
    if key != "toxicity" and (categories is not None or verdicts is not None):
        raise ResultsError("Only the toxicity result has categories and verdicts to choose.")
    chosen_categories = _split_choice(categories, scanners.GUARD_CATEGORIES, "categories")
    chosen_verdicts = _split_choice(verdicts, tuple(scanners.TOXICITY_REMOVABLE_VERDICTS), "verdicts")

    dataset = registry.get_dataset(dataset_id)
    summaries = scans.dataset_summaries([dataset])[dataset["id"]]
    found = _results_of(dataset, [key])
    active, accounted, slurm_error = _slurm_for(found)
    view = _scanner_view(
        dataset, key, summaries[key], found, registry.dataset_names(), (active, accounted), chosen_categories, chosen_verdicts, True
    )
    return {"dataset_id": dataset["id"], "name": dataset["name"], "slurm_error": slurm_error, **view}


def check_again(dataset_id: str, scanner_key: Any) -> dict[str, Any]:
    """Submit the analysis of the dataset's current result for a scanner again ("Check again").
    It repeats only the coverage check when the counts are already there, and everything
    otherwise (the first one failed, or never ran). Refused while an analysis is going."""
    key = _scanner_key(scanner_key)
    with _LOCK:
        dataset = registry.get_dataset(dataset_id)
        found = _results_of(dataset, [key])
        if key not in found:
            raise ResultsError(f"This dataset has no finished {scanners.SCANNERS[key].label} scan to check.")
        scan, record = found[key]
        # An earlier analysis that succeeded and has everything a full one writes: only the
        # coverage is checked again, and the tally and the index it made are kept.
        earlier: dict[str, Any] | None = None
        if record is not None:
            active, accounted, _error = _slurm_for(found)
            state, _reason = _outcome(scan, record, active, accounted)
            if state in GOING_STATES:
                raise ResultsError("The check of this result is still going. Wait for it to finish.")
            complete = (
                state == scans.STATE_SUCCEEDED
                and bool(record.get("has_index"))
                and (bool(record.get("has_tally")) or not _needs_tally(scan))
            )
            earlier = record if complete else None
        try:
            new = _submit(scan, KIND_COVERAGE if earlier is not None else KIND_FULL, earlier)
        except OSError as exc:
            raise ResultsError(f"Couldn't write the scan's record of the check ({exc}).") from exc
    return {"scan_id": scan["id"], "job_id": new.get("job_id"), "submit_error": new.get("submit_error")}


# -------------------------------------------------------------------------
# Analysing finished scans, in the background
# -------------------------------------------------------------------------


def _apply(scan: dict[str, Any], record: dict[str, Any]) -> None:
    """Copy the exact count and the coverage verdict of a finished analysis into the result of
    each of the scan's datasets, once. Writing them is idempotent, so a crash before
    `applied_at` is saved is repaired by the next pass. Call it while holding `_LOCK`."""
    report, tally = _read_outputs(scan)
    if report is None:
        _log(f"scan {scan['id']}: coverage.json can't be read as a JSON object")
        return
    scanner = scanners.SCANNERS[scan["scanner"]]
    names = registry.dataset_names()
    for dataset_id in scan["datasets"]:
        try:
            dataset = registry.get_dataset(dataset_id)
            saved = (dataset.get("results") or {}).get(scan["scanner"])
            if not isinstance(saved, dict) or saved.get("scan_id") != scan["id"]:
                continue  # a newer scan has replaced it
            coverage = _coverage_slice(report, scan, dataset_id)
            body = scanner.read_result(_result_context(scan, dataset_id, names, saved.get("count"), coverage["row"], tally))
            fields: dict[str, Any] = {registry.RESULT_COVERAGE_KEY: coverage["verdict"]}
            count = body["flagged"]["count"]
            if count is not None:
                fields[registry.RESULT_COUNT_KEY] = count
            registry.update_result(dataset_id, scan["scanner"], scan["id"], fields)
        except registry.DatasetNotFound:
            continue
    record["applied_at"] = jobs._now_iso()
    _write_analysis(record)


def _maintain() -> None:
    """Submit the analysis of every scan that is some dataset's result and has none, and copy
    the numbers of the ones that have finished into the registry. Call it while holding `_LOCK`."""
    all_scans, _problems = scans._all_scans()
    pointers = registry.result_pointers()
    current = [scan for scan in all_scans if scan.get("result_applied_at") is not None and scan["id"] in pointers]
    records = {scan["id"]: _read_analysis(scan["id"]) for scan in current}

    submitted = 0
    for scan in current:
        record = records[scan["id"]]
        # No analysis yet, or one that finished before the findings index existed (its record
        # has no `has_index` at all): a full one makes the index. A record that says it has no
        # index, or whose job failed, is left for "Check again", so a scan whose index can't
        # be made isn't submitted again every 30 seconds.
        needs = record is None or (record.get("applied_at") is not None and "has_index" not in record)
        if not needs or submitted >= MAX_SUBMITS_PER_PASS:
            continue
        try:
            records[scan["id"]] = _submit(scan, KIND_FULL)
            submitted += 1
        except OSError as exc:
            _log(f"scan {scan['id']}: {exc}")

    unapplied = [
        (scan, records[scan["id"]])
        for scan in current
        if records[scan["id"]] is not None and records[scan["id"]].get("applied_at") is None and records[scan["id"]].get("job_id")
    ]
    if not unapplied:
        return
    active, accounted, _error = scans._read_slurm([record["job_id"] for _scan, record in unapplied])
    for scan, record in unapplied:
        state, _reason = _outcome(scan, record, active, accounted)
        if state != scans.STATE_SUCCEEDED:
            continue
        try:
            _apply(scan, record)
        except (registry.RegistryError, OSError) as exc:
            _log(f"scan {scan['id']}: {exc}")


def refresh() -> None:
    """`_maintain`, unless a submit or a check is in progress: the next pass does it."""
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
    """Start the task that calls `refresh` every `REFRESH_SECONDS`, so a finished scan is
    analysed even when nobody is looking. A daemon thread: it ends with the process."""
    threading.Thread(target=_refresh_forever, name="results-refresh", daemon=True).start()
