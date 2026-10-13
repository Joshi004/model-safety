"""A Prepared run (the output folder of `prepare_rows.py`) or a filter run (the
output folder of `filter_rows.py`): list the runs under every folder the
validator reads, say how far a running one has got, open a finished one, page
through a Prepared run's skipped rows, and look an original row up from an
`_origin` note.

Everything here is read-only. A lookup only opens a file that the run's own
`summary.json` says it read, because the `_origin` note it starts from is
just data in a file and can't be trusted to name a safe path. A filter run's
rows carry the `_origin` note of the Prepared run they came from, so a lookup
from a filter run follows its `parent` runs back to that Prepared run first.
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
    FILTER_ROWS_TOOL,
    FINGERPRINT_RECIPE_VERSION,
    IDENTITY_FILENAME,
    JOBS_FOLDER,
    MAX_RUN_FOLDERS,
    ORIGIN_KEY,
    PREPARE_ROWS_TOOL,
    REPO_ROOT,
    SKIPPED_ROWS_FILENAME,
    STALE_AFTER_SECONDS,
    RunError,
    chain_folders,
    find_prepared_row,
    find_run_above,
    json_safe,
    list_chains,
    list_runs,
    lookup,
    may_have_stopped,
    open_run,
    read_json_or_none,
    read_progress,
    run_reads_path,
    source_file_entry,
)

RUNS_ROOT_ENV = "VALIDATOR_RUNS_ROOT"
# The folders the runs list reads, as a list of paths separated like PATH is. When it
# isn't set, the runs list reads the runs root and the folders of every chain.
READ_ROOTS_ENV = "VALIDATOR_READ_ROOTS"
# Where the chain records (`chain.json`, see modelsafety/contract/chain.py) are looked for.
CHAINS_ROOT_ENV = "VALIDATOR_CHAINS_ROOT"
# A filter run's parent can be another filter run; no real chain is this long
# (the trace tool's limit).
MAX_PARENT_HOPS = 20
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


def chains_root() -> Path:
    """`VALIDATOR_CHAINS_ROOT` if set, otherwise `<repo>/output`."""
    configured = os.environ.get(CHAINS_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (REPO_ROOT / "output").resolve()


def read_roots() -> list[dict[str, Any]]:
    """The folders the runs list reads, each with the label the UI shows.

    `VALIDATOR_READ_ROOTS`, if set, is the whole list. Otherwise it is the runs
    root, then the Prepared folder and the output folder of each filter step of
    every chain under the chains root (read from the chain's own record, so a
    chain folder is never walked whole). A folder inside one that is already on
    the list is left off, because walking the first one finds its runs.
    """
    configured = os.environ.get(READ_ROOTS_ENV, "").strip()
    candidates: list[tuple[Path, str]] = []
    if configured:
        for item in configured.split(os.pathsep):
            if item.strip():
                folder = Path(item.strip()).expanduser().resolve()
                candidates.append((folder, folder.name or str(folder)))
    else:
        candidates.append((runs_root(), "Runs root"))
        for chain in list_chains(chains_root())["chains"]:
            if "problem" in chain:
                continue
            for number, folder in enumerate(chain_folders(chain["chain_dir"])):
                label = f"{chain['name']}: Prepared runs" if number == 0 else f"{chain['name']}: step {number}"
                candidates.append((folder.resolve(), label))

    roots: list[dict[str, Any]] = []
    for folder, label in candidates:
        if any(folder == kept["root"] or kept["root"] in folder.parents for kept in roots):
            continue
        roots.append({"root": folder, "label": label})
    return roots


def list_all_runs() -> dict[str, Any]:
    """Every root of `read_roots()` with the runs under it (`list_runs`), in the
    order the roots are listed."""
    return {"roots": [{"label": item["label"], **list_runs(item["root"])} for item in read_roots()]}


# -------------------------------------------------------------------------
# Opening a run
# -------------------------------------------------------------------------


def prepared_run_of(run_dir: Path) -> Path:
    """The Prepared run that `run_dir` came from: `run_dir` itself when it is one,
    otherwise the end of its filter runs' `parent` links. A folder that isn't a
    filter run is returned as it is, so `open_run` can say what is wrong with it.
    Reads only each run's `summary.json`."""
    current = run_dir
    for _ in range(MAX_PARENT_HOPS + 1):
        summary = read_json_or_none(current / "summary.json")
        if not isinstance(summary, dict) or summary.get("tool") != FILTER_ROWS_TOOL:
            return current
        parent = summary.get("parent")
        parent_dir = parent.get("run_dir") if isinstance(parent, dict) else None
        if not isinstance(parent_dir, str) or not Path(parent_dir).is_absolute():
            raise RunError(f"{current / 'summary.json'} doesn't say which run it filtered (parent.run_dir).")
        current = Path(parent_dir)
    raise RunError(f"{run_dir} leads back through {MAX_PARENT_HOPS} filter runs without reaching a Prepared run.")


def nearest_run_above(path: Path) -> Path:
    """The nearest folder above `path` that holds the `summary.json` of a Prepared
    or filter run. A `summary.json` from any other tool is walked past."""
    for folder in path.parents:
        summary = read_json_or_none(folder / "summary.json")
        if isinstance(summary, dict) and summary.get("tool") in (PREPARE_ROWS_TOOL, FILTER_ROWS_TOOL):
            return folder
    raise FileNotFoundError(f"No prepare_rows.py or filter_rows.py run found above {path}.")


def prepared_run_for(run_dir: Path | None, prepared_path: Path | None) -> Path:
    """The Prepared run whose `source.files` a row's `_origin` note is checked
    against. `run_dir` is a Prepared or filter run; `prepared_path` is a file shown
    from one, outside a run (its run is found by walking up from there)."""
    if run_dir is None and prepared_path is None:
        raise RunError("Give the run folder, or the Prepared file the row was shown from.")
    return prepared_run_of(run_dir if run_dir is not None else nearest_run_above(prepared_path))


def open_filter_run(run_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    """A filter run's `summary.json` and `run_identity.json` (None if it has
    none), and the Prepared run it came from (None, with the reason, if that
    can't be found)."""
    prepared_dir: str | None = None
    problem: str | None = None
    try:
        found = prepared_run_of(run_dir)
        found_summary = read_json_or_none(found / "summary.json")
        if isinstance(found_summary, dict) and found_summary.get("tool") == PREPARE_ROWS_TOOL:
            prepared_dir = str(found)
        else:
            problem = f"{found} isn't a Prepared run any more (moved or deleted?), so the rows' Original can't be looked up."
    except RunError as exc:
        problem = str(exc)
    return {
        "kind": "filter",
        "run_dir": str(run_dir),
        "summary": summary,
        "run_identity": read_json_or_none(run_dir / IDENTITY_FILENAME),
        "prepared_run_dir": prepared_dir,
        "prepared_run_problem": problem,
    }


def open_any_run(run_dir: Path) -> dict[str, Any]:
    """A Prepared run (`open_run`) or a filter run (`open_filter_run`), by the
    tool its `summary.json` names. `kind` says which. Anything else raises `RunError`."""
    summary = read_json_or_none(run_dir / "summary.json")
    if isinstance(summary, dict) and summary.get("tool") == FILTER_ROWS_TOOL:
        return open_filter_run(run_dir, summary)
    # Not a filter run: open_run opens a Prepared run, or says why it can't.
    return {"kind": "prepared", **open_run(run_dir)}


# -------------------------------------------------------------------------
# From an Original row to its Prepared row
# -------------------------------------------------------------------------


def runs_reading(original_path: Path) -> list[Path]:
    """The finished Prepared runs, under every folder the validator reads, that read
    `original_path` (a file listed in their `source.files`, or the Arrow folder
    they took as their input). Reads each run's `summary.json` and `run_identity.json`."""
    found: list[Path] = []
    for item in read_roots():
        for entry in list_runs(item["root"])["runs"]:
            if entry["kind"] != "prepared" or entry["status"] != "checks_passed":
                continue
            run_dir = Path(entry["run_dir"])
            summary = read_json_or_none(run_dir / "summary.json")
            identity = read_json_or_none(run_dir / IDENTITY_FILENAME)
            if isinstance(summary, dict) and run_reads_path(summary, identity, str(original_path)) and run_dir not in found:
                found.append(run_dir)
    return found


def find_prepared(
    original_path: Path, row: int | None, line: int | None, split: str | None, run_dir: Path | None
) -> dict[str, Any]:
    """Where an Original row is in the Prepared data (see
    modelsafety/trace/find_prepared.py for the answers). With `run_dir` (a Prepared
    run, or a filter run, which stands for the Prepared run it came from) it asks
    that one run, and raises `RunError` if it can't be used. Without, it asks every
    Prepared run that read the file, and a run that can't be used is listed with its
    `error` instead of failing the rest."""
    if run_dir is not None:
        prepared = prepared_run_of(run_dir)
        match = find_prepared_row(prepared, str(original_path), row=row, line=line, split=split)
        return {"path": str(original_path), "runs_checked": 1, "matches": [match]}
    candidates = runs_reading(original_path)
    matches: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            matches.append(find_prepared_row(candidate, str(original_path), row=row, line=line, split=split))
        except RunError as exc:
            matches.append({"run_dir": str(candidate), "error": str(exc)})
    return {"path": str(original_path), "runs_checked": len(candidates), "matches": matches}


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
