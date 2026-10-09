"""A Prepared run (the output folder of `prepare_rows.py`): list the runs
under the runs root, say how far a running one has got, open a finished one,
page through its skipped rows, and look an original row up from an `_origin`
note.

Everything here is read-only. A lookup only opens a file that the run's own
`summary.json` says it read, because the `_origin` note it starts from is
just data in a file and can't be trusted to name a safe path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# The run detection (the runs list, a run's status and progress) lives in
# modelsafety/contract/run_finder.py, which scans use too. The lookup of an
# original row (open_run, find_run_above, lookup, RunError) lives in
# modelsafety/trace/original.py, which the trace tool uses too. The names the
# other backend modules read as `runs.X` are re-exported here.
from reader_imports import (  # noqa: F401
    FINGERPRINT_RECIPE_VERSION,
    JOBS_FOLDER,
    MAX_RUN_FOLDERS,
    ORIGIN_KEY,
    PREPARE_ROWS_TOOL,
    REPO_ROOT,
    SKIPPED_ROWS_FILENAME,
    STALE_AFTER_SECONDS,
    RunError,
    find_run_above,
    json_safe,
    list_runs,
    lookup,
    may_have_stopped,
    open_run,
    read_progress,
    source_file_entry,
)

RUNS_ROOT_ENV = "VALIDATOR_RUNS_ROOT"
PREVIEW_CHARS = 160


# -------------------------------------------------------------------------
# Watching a run (progress.json)
# -------------------------------------------------------------------------


def run_progress(run_dir: Path) -> dict[str, Any]:
    """How far the run in `run_dir` has got. `progress` is None when there is
    no usable `progress.json` (a missing folder isn't an error)."""
    progress = read_progress(run_dir)
    return {
        "run_dir": str(run_dir),
        "progress": progress,
        "has_summary": (run_dir / "summary.json").is_file(),
        "has_success": (run_dir / "_SUCCESS").is_file(),
        "may_have_stopped": progress is not None and may_have_stopped(progress),
    }


# -------------------------------------------------------------------------
# The runs list
# -------------------------------------------------------------------------


def runs_root() -> Path:
    """`VALIDATOR_RUNS_ROOT` if set, otherwise `<repo>/output/prepared`."""
    configured = os.environ.get(RUNS_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (REPO_ROOT / "output" / "prepared").resolve()


# -------------------------------------------------------------------------
# The skipped-rows page
# -------------------------------------------------------------------------


def _skipped_total(summary: dict[str, Any], reason: str | None, file: str | None) -> int | None:
    """How many entries match, from `summary.json`. None when both filters
    are set, because the summary only counts each filter on its own."""
    skipped_file = summary["rows"]["skipped_file"]
    if reason is not None and file is not None:
        return None
    if reason is not None:
        return skipped_file.get("by_reason", {}).get(reason, 0)
    if file is not None:
        return skipped_file.get("by_file", {}).get(file, 0)
    return skipped_file.get("entries")


def skipped_page(
    run: dict[str, Any],
    page: int,
    page_size: int,
    reason: str | None = None,
    file: str | None = None,
) -> dict[str, Any]:
    """One page of `skipped_rows.ndjson`, read from the top of the file.

    Always opens `<run folder>/skipped_rows.ndjson`, never a file name taken
    from the summary.
    """
    skipped_path = Path(run["run_dir"]) / SKIPPED_ROWS_FILENAME
    if not skipped_path.is_file():
        raise RunError(f"{SKIPPED_ROWS_FILENAME} is missing from {run['run_dir']}.")

    first_wanted = (page - 1) * page_size
    matched = 0
    has_next = False
    items: list[dict[str, Any]] = []
    with skipped_path.open("rb") as handle:
        for number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except ValueError as exc:
                raise RunError(f"{SKIPPED_ROWS_FILENAME} line {number} isn't valid JSON: {exc}") from exc
            origin = entry.get(ORIGIN_KEY) if isinstance(entry, dict) else None
            if not isinstance(origin, dict):
                raise RunError(f"{SKIPPED_ROWS_FILENAME} line {number} isn't a skipped-rows entry.")

            if reason is not None and entry.get("reason") != reason:
                continue
            if file is not None and origin.get("path") != file:
                continue
            if matched >= first_wanted:
                if len(items) == page_size:
                    has_next = True
                    break
                preview = json.dumps(entry.get("row"), ensure_ascii=False)[:PREVIEW_CHARS]
                items.append({"number": number, "entry": entry, "preview": preview})
            matched += 1

    return {
        "page": page,
        "page_size": page_size,
        "total": _skipped_total(run["summary"], reason, file),
        "has_next": has_next,
        "entries": items,
    }
