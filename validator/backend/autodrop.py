"""Dropping what a scan flagged, by itself (D17).

A scan can be started with "drop duplicates automatically when the scan finishes" (the scan
record's `auto_drop`). Once such a scan has finished and become its datasets' result, and its
analysis (results.py) has finished, this module decides, once:

* **The coverage check did not pass:** nothing is dropped, for any dataset. The decision says why
  and lists the coverage problems.
* **It passed:** one filtered version is made for every dataset that has flagged rows, dropping
  all of that dataset's flagged rows (`filters.submit` with `all_matching: {}`, so exactly what
  the dataset's findings index lists). A dataset the scan only read as a reference (a protected
  one, D15) has nothing flagged and gets none. The drop list says the rows were dropped
  automatically, and the filtered versions are normal datasets (D11): the parents are untouched.

What was decided is saved in the scan's own folder:

    <service root>/scans/<scan id>/auto_drop.json

It is written once and never decided again: "Check again" doesn't repeat it, and a scan whose
decision exists is never looked at. The file says `dropping` before the first filter is submitted
and is updated after each one, so a service that stops part way is never taken for one that
should start again: the next pass finds `dropping` and marks it `interrupted`, so nothing is
dropped twice. What was already made is listed, and the rest can be dropped from the result page.

Rules this module keeps:

* The only thing written is `auto_drop.json` in a scan's folder. Everything else is done by
  filters.submit, with the rules in filters.py (nothing in original data, a Prepared version or
  a scan is written or deleted).
* Nothing here talks to SLURM itself. filters.submit does, through jobs.py.
* One lock covers a pass, so two passes can't decide for one scan. `refresh` skips instead of
  waiting when the lock is taken.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

import filters
import flagged
import jobs
import registry
import results
import scans
from reader_imports import read_json_or_none

AUTO_DROP_FILE = "auto_drop.json"
# How often the background task looks for finished scans that haven't been decided.
REFRESH_SECONDS = 30
MAX_PROBLEMS_SHOWN = results.MAX_PROBLEMS_SHOWN
# Text read back from a file is cut to this, so a damaged record can't fill a page.
MAX_TEXT_CHARS = 2000

# The outcomes. WAITING is only in the view: nothing is saved before the decision.
OUTCOME_WAITING = "waiting"
OUTCOME_DROPPING = "dropping"
OUTCOME_DROPPED = "dropped"
OUTCOME_NOTHING = "nothing_to_drop"
OUTCOME_NOT_DROPPED = "not_dropped"
OUTCOME_FAILED = "failed"
OUTCOME_INTERRUPTED = "interrupted"
OUTCOMES = (
    OUTCOME_DROPPING,
    OUTCOME_DROPPED,
    OUTCOME_NOTHING,
    OUTCOME_NOT_DROPPED,
    OUTCOME_FAILED,
    OUTCOME_INTERRUPTED,
)

_LOCK = threading.Lock()


def _log(message: str) -> None:
    print(f"[autodrop] {message}", file=sys.stderr, flush=True)


# -------------------------------------------------------------------------
# The decision's record
# -------------------------------------------------------------------------


def _record_path(scan_id: str) -> Path:
    return scans._scan_folder(scan_id) / AUTO_DROP_FILE


def _text(value: Any) -> str | None:
    return value[:MAX_TEXT_CHARS] if isinstance(value, str) else None


def _clean_entry(entry: Any) -> dict[str, Any] | None:
    """One dataset's line of a decision, as read from the file: only ids of the exact format and
    short text are let through, because the page links to the ids."""
    if not isinstance(entry, dict):
        return None
    dataset_id, filtered_id, rows = entry.get("dataset_id"), entry.get("filtered_id"), entry.get("rows")
    if not isinstance(dataset_id, str) or registry.DATASET_ID.fullmatch(dataset_id) is None:
        return None
    if filtered_id is not None and not (isinstance(filtered_id, str) and registry.DATASET_ID.fullmatch(filtered_id)):
        return None
    return {
        "dataset_id": dataset_id,
        "dataset_name": _text(entry.get("dataset_name")),
        "rows": rows if isinstance(rows, int) and not isinstance(rows, bool) else None,
        "filtered_id": filtered_id,
        "filtered_name": _text(entry.get("filtered_name")),
        "error": _text(entry.get("error")),
        "note": _text(entry.get("note")),
    }


def _clean_record(data: Any, scan_id: str) -> dict[str, Any] | None:
    """The decision in a file, checked, or None when it isn't one. A record is only a file, so
    none of it is taken on trust."""
    if not isinstance(data, dict) or data.get("scan_id") != scan_id or data.get("outcome") not in OUTCOMES:
        return None
    entries = [_clean_entry(entry) for entry in data["datasets"]] if isinstance(data.get("datasets"), list) else []
    problems = []
    for problem in data.get("problems") if isinstance(data.get("problems"), list) else []:
        if isinstance(problem, dict) and isinstance(problem.get("message"), str):
            problems.append({"message": problem["message"][:MAX_TEXT_CHARS], "path": _text(problem.get("path"))})
    return {
        "scan_id": scan_id,
        "outcome": data["outcome"],
        "decided_at": _text(data.get("decided_at")),
        "reason": _text(data.get("reason")),
        "problems": problems[:MAX_PROBLEMS_SHOWN],
        "problems_total": data.get("problems_total") if isinstance(data.get("problems_total"), int) else len(problems),
        "datasets": [entry for entry in entries if entry is not None],
    }


def _read_record(scan_id: str) -> dict[str, Any] | None:
    return _clean_record(jobs._read_json(_record_path(scan_id)), scan_id)


def _write_record(record: dict[str, Any]) -> None:
    jobs._write_json_atomic(_record_path(record["scan_id"]), record)


def _new_record(scan: dict[str, Any], outcome: str, reason: str | None = None) -> dict[str, Any]:
    return {
        "scan_id": scan["id"],
        "outcome": outcome,
        "decided_at": jobs._now_iso(),
        "reason": reason,
        "problems": [],
        "problems_total": 0,
        "datasets": [],
    }


def view_of(scan_id: Any) -> dict[str, Any] | None:
    """What came of a scan's automatic drop, for its result page: None when the scan wasn't
    started with it, `waiting` before it is decided, otherwise the saved decision. Only reads."""
    scan = scans._read_scan(scan_id) if isinstance(scan_id, str) and jobs.BATCH_ID.fullmatch(scan_id) else None
    if scan is None or not scan.get("auto_drop"):
        return None
    record = _read_record(scan["id"])
    if record is None:
        record = {**_new_record(scan, OUTCOME_WAITING), "decided_at": None}
    return record


# -------------------------------------------------------------------------
# Deciding
# -------------------------------------------------------------------------


def _coverage_problems(scan: dict[str, Any]) -> list[dict[str, Any]] | None:
    """None when the scan's coverage report says it passed; otherwise what is wrong, as
    `{message, path}`. The report is the scan's own (all of its datasets), so one failing
    dataset stops the drop for every dataset (D17)."""
    report = read_json_or_none(scans._scan_folder(scan["id"]) / results.COVERAGE_FILE)
    if not isinstance(report, dict):
        return [{"message": "coverage.json can't be read as a coverage report.", "path": None}]
    if report.get("verdict") == "pass":
        return None
    problems = [
        {"message": str(problem.get("message")), "path": problem["path"] if isinstance(problem.get("path"), str) else None}
        for problem in (report.get("problems") if isinstance(report.get("problems"), list) else [])
        if isinstance(problem, dict)
    ]
    return problems or [{"message": "The coverage report doesn't say the scan passed.", "path": None}]


def _drop_one(scan: dict[str, Any], dataset_id: str, name: str | None, reference: bool) -> dict[str, Any]:
    """One dataset's part of the decision: its filtered version if it has flagged rows."""
    entry: dict[str, Any] = {
        "dataset_id": dataset_id,
        "dataset_name": name,
        "rows": None,
        "filtered_id": None,
        "filtered_name": None,
        "error": None,
        "note": None,
    }
    if reference:
        entry.update(rows=0, note="Protected: the scan only read it, so nothing in it is flagged.")
        return entry
    try:
        opened = flagged._open_index(dataset_id, scan["scanner"])
        if opened.scan["id"] != scan["id"]:
            entry["error"] = "Its result is no longer this scan, so nothing was dropped from it."
            return entry
        entry["rows"] = opened.counts["rows"]
        if entry["rows"] == 0:
            entry["note"] = "Nothing was flagged in it."
            return entry
        version_id = scan["versions"][dataset_id]["version_id"]
        made = filters.submit(
            dataset_id,
            {"version_id": version_id, "selections": [{"scanner": scan["scanner"], "scan_id": scan["id"], "all_matching": {}}]},
            automatic_scan_id=scan["id"],
        )
        entry.update(rows=made["rows"], filtered_id=made["dataset_id"], filtered_name=made["name"])
        if made["submit_error"]:
            entry["error"] = f"The filter job didn't start: {made['submit_error']}"
    except (results.ResultsError, registry.RegistryError, jobs.JobsError, jobs.SlurmUnavailable, KeyError, TypeError, OSError) as exc:
        entry["error"] = str(exc) or exc.__class__.__name__
    return entry


def _decide(scan: dict[str, Any]) -> None:
    """Decide the automatic drop of one scan, once. Call it while holding `_LOCK`, for a scan
    whose analysis has finished and that has no decision yet."""
    problems = _coverage_problems(scan)
    if problems is not None:
        record = _new_record(scan, OUTCOME_NOT_DROPPED, "The coverage check failed, so nothing was dropped.")
        record["problems"] = problems[:MAX_PROBLEMS_SHOWN]
        record["problems_total"] = len(problems)
        _write_record(record)
        return

    try:
        filters._resources()
    except jobs.SlurmUnavailable as exc:
        # Nothing has been done yet, so this is simply tried again on the next pass.
        _log(f"scan {scan['id']}: not dropping yet, SLURM can't be asked ({exc})")
        return

    # Written before the first filter is submitted: a stop part way is then found as `dropping`
    # and never started again.
    record = _new_record(scan, OUTCOME_DROPPING)
    _write_record(record)
    references = set(scans.reference_datasets_of(scan))
    names = registry.dataset_names()
    for dataset_id in scan["datasets"]:
        record["datasets"].append(_drop_one(scan, dataset_id, names.get(dataset_id), dataset_id in references))
        _write_record(record)

    entries = record["datasets"]
    if any(entry["filtered_id"] for entry in entries):
        record["outcome"] = OUTCOME_DROPPED
    elif any(entry["error"] for entry in entries):
        record["outcome"] = OUTCOME_FAILED
        record["reason"] = "Nothing was dropped: " + " ".join(entry["error"] for entry in entries if entry["error"])[:MAX_TEXT_CHARS]
    else:
        record["outcome"] = OUTCOME_NOTHING
    record["decided_at"] = jobs._now_iso()
    _write_record(record)


def _maintain() -> None:
    """Decide the automatic drop of every scan that asked for it, is still some dataset's result,
    and has a finished analysis with the findings index. Call it while holding `_LOCK`."""
    all_scans, _problems = scans._all_scans()
    wanting = [scan for scan in all_scans if scan.get("auto_drop") and scan.get("result_applied_at") is not None]
    if not wanting:
        return
    pointers = registry.result_pointers()
    for scan in wanting:
        if scan["id"] not in pointers:
            continue  # replaced by a newer scan: nothing to drop from it any more
        if _record_path(scan["id"]).exists():
            record = _read_record(scan["id"])
            if record is not None and record["outcome"] == OUTCOME_DROPPING:
                # We hold the lock and nothing is going on, so an earlier run stopped part way.
                record["outcome"] = OUTCOME_INTERRUPTED
                record["reason"] = (
                    "The service stopped while it was dropping, so the rest wasn't done (to be sure nothing is dropped "
                    "twice). The versions already made are listed; drop the others from the result page."
                )
                _write_record(record)
            continue  # decided: never again, and a record that can't be read is left alone too
        analysis = results._read_analysis(scan["id"])
        # `applied_at` is set once the analysis succeeded and its numbers were copied.
        if analysis is None or analysis.get("applied_at") is None or not analysis.get("has_index"):
            continue
        try:
            _decide(scan)
        except (registry.RegistryError, jobs.JobsError, jobs.SlurmUnavailable, OSError) as exc:
            _log(f"scan {scan['id']}: {exc}")


def refresh() -> None:
    """`_maintain`, unless a pass is already going."""
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
    """Start the task that calls `refresh` every `REFRESH_SECONDS`, so a finished scan is decided
    even when nobody is looking. A daemon thread: it ends with the process."""
    threading.Thread(target=_refresh_forever, name="autodrop-refresh", daemon=True).start()
