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
import re
from pathlib import Path
from typing import Any

import detect
# The run detection (the runs list, a run's status and progress) lives in
# modelsafety/contract/run_finder.py, which scans use too. The names the other
# backend modules read as `runs.X` are re-exported here.
from reader_imports import (  # noqa: F401
    FINGERPRINT_RECIPE_VERSION,
    JOBS_FOLDER,
    MAX_RUN_FOLDERS,
    ORIGIN_KEY,
    PREPARE_ROWS_TOOL,
    REPO_ROOT,
    SKIPPED_ROWS_FILENAME,
    STALE_AFTER_SECONDS,
    json_safe,
    list_runs,
    may_have_stopped,
    read_progress,
    source_file_entry,
)

RUNS_ROOT_ENV = "VALIDATOR_RUNS_ROOT"
PREVIEW_CHARS = 160
SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class RunError(ValueError):
    """The folder isn't a usable run, or a lookup was refused."""


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _source_files(summary: dict[str, Any]) -> list[dict[str, Any]]:
    files = summary.get("source", {}).get("files")
    if not isinstance(files, list):
        raise RunError("This run's summary.json has no source.files list.")
    return files


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
# Opening a run
# -------------------------------------------------------------------------


def open_run(run_dir: Path) -> dict[str, Any]:
    """The run's `summary.json` and `run_identity.json` (None if it has none).

    Refuses a folder whose `summary.json` isn't from `prepare_rows.py`.
    """
    summary_path = run_dir / "summary.json"
    if not summary_path.is_file():
        raise RunError(
            f"No summary.json in {run_dir}. Pick the output folder of a prepare_rows.py run."
        )
    try:
        summary = _read_json(summary_path)
    except (OSError, ValueError) as exc:
        raise RunError(f"Can't read {summary_path}: {exc}") from exc
    if not isinstance(summary, dict) or summary.get("tool") != PREPARE_ROWS_TOOL:
        tool = summary.get("tool") if isinstance(summary, dict) else None
        raise RunError(
            f"{summary_path} isn't from prepare_rows.py (its tool is {tool!r}). "
            "Pick the output folder of a prepare_rows.py run."
        )
    if not isinstance(summary.get("rows", {}).get("skipped_file"), dict):
        raise RunError(
            f"{summary_path} has no rows.skipped_file, so this run was made before "
            "skipped rows were recorded."
        )

    identity = None
    identity_path = run_dir / "run_identity.json"
    if identity_path.is_file():
        try:
            identity = _read_json(identity_path)
        except (OSError, ValueError) as exc:
            raise RunError(f"Can't read {identity_path}: {exc}") from exc

    return {"run_dir": str(run_dir), "summary": summary, "run_identity": identity}


def find_run_above(prepared_path: Path) -> Path:
    """The nearest folder above `prepared_path` that holds a `summary.json`
    from `prepare_rows.py`. A `summary.json` from any other tool is walked past."""
    for folder in prepared_path.parents:
        summary_path = folder / "summary.json"
        if not summary_path.is_file():
            continue
        try:
            summary = _read_json(summary_path)
        except (OSError, ValueError):
            continue
        if isinstance(summary, dict) and summary.get("tool") == PREPARE_ROWS_TOOL:
            return folder
    raise FileNotFoundError(f"No prepare_rows.py run found above {prepared_path}.")


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


# -------------------------------------------------------------------------
# Looking an original row up from an `_origin` note
# -------------------------------------------------------------------------


def _whole_number(origin: dict[str, Any], key: str, minimum: int) -> None:
    value = origin.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RunError(f"The _origin note's '{key}' must be a whole number of at least {minimum}.")


def _check_origin_shape(origin: Any, input_type: str) -> None:
    if not isinstance(origin, dict):
        raise RunError("The _origin note isn't an object.")
    if not isinstance(origin.get("path"), str) or not origin["path"]:
        raise RunError("The _origin note has no path.")
    fingerprint = origin.get("fingerprint")
    if not isinstance(fingerprint, str) or not SHA256_HEX.fullmatch(fingerprint):
        raise RunError("The _origin note's fingerprint isn't a SHA-256 hex string.")
    version = origin.get("v")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != FINGERPRINT_RECIPE_VERSION
    ):
        raise RunError(
            f"This validator only knows fingerprint recipe v{FINGERPRINT_RECIPE_VERSION}, "
            f"but the _origin note says v{version}."
        )
    if input_type == "jsonl":
        _whole_number(origin, "line", 1)
        _whole_number(origin, "byte", 0)
    else:
        _whole_number(origin, "row", 0)
    split = origin.get("split")
    if split is not None and not isinstance(split, str):
        raise RunError("The _origin note's split must be text or null.")


def _refuse_unlisted(path: str) -> RunError:
    return RunError(
        f"This run didn't read {path}, so it won't be opened. A lookup only opens a file "
        "listed in the run's summary.json (source.files)."
    )


def _files_to_compare(
    run: dict[str, Any], origin: dict[str, Any], input_type: str
) -> list[dict[str, Any]]:
    """The `source.files` entries a lookup checks against the disk. Raises if
    the note names something this run didn't read.

    An Arrow folder's note names the folder, which isn't itself in
    `source.files`; it's allowed only when it is the run's own input root, and
    then every `.arrow` file the run read is checked.
    """
    path = origin["path"]
    files = _source_files(run["summary"])
    if input_type == "huggingface":
        identity = run["run_identity"]
        if not isinstance(identity, dict) or identity.get("input_root") != path:
            raise _refuse_unlisted(path)
        return files
    matching = [entry for entry in files if entry.get("path") == path]
    if not matching:
        raise _refuse_unlisted(path)
    return matching


def _file_differences(entries: list[dict[str, Any]]) -> list[str]:
    """What changed on disk since the run read these files, judged by size
    and last-modified time alone (no file is read)."""
    differences: list[str] = []
    for recorded in entries:
        path = recorded.get("path")
        try:
            now = source_file_entry(Path(path), "")
        except (OSError, TypeError):
            differences.append(f"{path} is missing now.")
            continue
        if now["size_bytes"] != recorded.get("size_bytes"):
            differences.append(
                f"{path} is {now['size_bytes']} bytes now; it was "
                f"{recorded.get('size_bytes')} bytes when this run read it."
            )
        if now["modified"] != recorded.get("modified"):
            differences.append(
                f"{path} was last modified {now['modified']}; "
                f"it was {recorded.get('modified')} when this run read it."
            )
    return differences


def _result(
    status: str, run: dict[str, Any], input_type: str, origin: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    return {
        "status": status,
        "run_dir": run["run_dir"],
        "input_type": input_type,
        "path": origin["path"],
        "file_differences": [],
        "row": None,
        "raw_line": None,
        "note": None,
        **extra,
    }


def lookup(
    origin: Any, run_dir: Path | None = None, prepared_path: Path | None = None
) -> dict[str, Any]:
    """Find the original row an `_origin` note points at and say whether it
    is still the row the preparing step read: `unchanged`, `changed` or
    `not_found`. Anything else (refused, not supported) raises `RunError`.

    The run is `run_dir`, or the nearest run above `prepared_path` (a
    Prepared file viewed outside a run).
    """
    if run_dir is None and prepared_path is None:
        raise RunError("Give the run folder, or the Prepared file the row was shown from.")
    run = open_run(run_dir if run_dir is not None else find_run_above(prepared_path))

    input_type = run["summary"].get("input", {}).get("type")
    if not isinstance(input_type, str):
        raise RunError("This run's summary.json has no input.type.")
    _check_origin_shape(origin, input_type)
    path = origin["path"]

    # A note from datasets.load_dataset: a Hub ID, or a local folder of raw files.
    if "config" in origin:
        raise RunError("Looking up rows read with load_dataset (a Hub dataset) isn't supported yet.")
    entries = _files_to_compare(run, origin, input_type)
    if input_type == "huggingface" and not Path(path).is_absolute():
        raise RunError("Looking up rows from a Hub dataset isn't supported yet.")

    target = Path(path)
    is_there = target.is_dir() if input_type == "huggingface" else target.is_file()
    if not is_there:
        return _result("not_found", run, input_type, origin)
    if input_type == "huggingface" and not any(
        (target / marker).is_file() for marker in detect.HF_MARKER_FILES
    ):
        raise RunError(
            "Looking up rows read with load_dataset (a local folder of raw files) isn't supported yet."
        )

    differences = _file_differences(entries)
    try:
        original = detect.read_original_row(input_type, origin)
    except FileNotFoundError:
        return _result("not_found", run, input_type, origin, file_differences=differences)

    unchanged = original["fingerprint"] == origin["fingerprint"]
    return _result(
        "unchanged" if unchanged else "changed",
        run,
        input_type,
        origin,
        file_differences=differences,
        row=json_safe(original["row"]) if original["row"] is not None else None,
        raw_line=original["raw_line"],
        note=original["note"],
    )
