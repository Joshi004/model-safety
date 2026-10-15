"""Look an original row up from an `_origin` note: one note, or many at once.

The validator's "Show original" (`validator/backend/runs.py`) calls this code.
`lookup()` is one note; `lookup_notes()` is many notes of one run, and reads
each original file once.

Everything here is read-only. A lookup only opens a file that the run's own
`summary.json` says it read, because the `_origin` note it starts from is
just data in a file and can't be trusted to name a safe path.

This needs pandas, pyarrow and datasets (the readers' own packages), unlike
the standard-library-only code in `modelsafety/contract/`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

# `prepare_rows.py` expects its own folder on `sys.path` (it imports
# `chunked_writer` as a bare name). The validator's reader_imports.py does the
# same, and a bare `import prepare_rows` gives both the same module.
_READERS_DIR = Path(__file__).resolve().parents[1] / "readers"
if str(_READERS_DIR) not in sys.path:
    sys.path.insert(0, str(_READERS_DIR))

import prepare_rows  # noqa: E402

from modelsafety.contract.fingerprints import (  # noqa: E402
    FINGERPRINT_RECIPE_VERSION,
    line_fingerprint,
    row_fingerprint,
)
from modelsafety.contract.origin import json_safe  # noqa: E402
from modelsafety.contract.run_finder import PREPARE_ROWS_TOOL, SHA256_HEX  # noqa: E402

clean_value = prepare_rows._clean_value
source_file_entry = prepare_rows._source_file_entry

HF_MARKER_FILES = ("state.json", "dataset_dict.json")
MAX_JSON_BYTES = 50 * 1024 * 1024  # a plain .json file has no streaming/bounded reader
PARQUET_BATCH_ROWS = 1024
MAX_RAW_LINE_CHARS = 2_000


class RunError(ValueError):
    """The folder isn't a usable run, or a lookup was refused."""


class LookupNotSupported(RunError):
    """The note is fine, but this kind of original can't be looked up yet."""


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _source_files(summary: dict[str, Any]) -> list[dict[str, Any]]:
    files = summary.get("source", {}).get("files")
    if not isinstance(files, list):
        raise RunError("This run's summary.json has no source.files list.")
    return files


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
# Checking an `_origin` note against its run
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


# -------------------------------------------------------------------------
# Reading original rows back, the way the preparing step read them
# -------------------------------------------------------------------------


def _has_integer_without_nulls(array: Any) -> bool:
    """Whether `array`, or anything nested inside it, is a non-empty integer
    array with no nulls."""
    import pyarrow as pa

    dtype = array.type
    if pa.types.is_integer(dtype):
        return len(array) > 0 and array.null_count == 0
    if pa.types.is_struct(dtype):
        return any(_has_integer_without_nulls(array.field(i)) for i in range(dtype.num_fields))
    if pa.types.is_list(dtype) or pa.types.is_large_list(dtype) or pa.types.is_fixed_size_list(dtype):
        return _has_integer_without_nulls(array.flatten())
    if pa.types.is_map(dtype):
        return _has_integer_without_nulls(array.keys) or _has_integer_without_nulls(array.items)
    return False


def may_convert_differently(group_meta: Any, batch: Any) -> bool:
    """Whether converting `batch` on its own could give different Python
    values than converting its whole row group, as `prepare_rows.py` does.

    pandas turns an integer column that has nulls into floats (5 -> 5.0), and
    one without nulls into ints. A batch with no nulls in an integer column
    that has nulls elsewhere in the row group would come out as ints where
    `prepare_rows.py` wrote floats, and the row's fingerprint would differ.
    So: true when the batch has an integer column without nulls in a
    top-level column whose integer footer columns have nulls (or no null count).
    """
    names = [
        name
        for name, column in zip(batch.schema.names, batch.columns)
        if _has_integer_without_nulls(column)
    ]
    if not names:
        return False
    for index in range(group_meta.num_columns):
        chunk = group_meta.column(index)
        if chunk.physical_type not in ("INT32", "INT64"):
            continue
        path = chunk.path_in_schema
        if not any(path == name or path.startswith(name + ".") for name in names):
            continue
        statistics = chunk.statistics
        if statistics is None or not statistics.has_null_count or statistics.null_count > 0:
            return True
    return False


def load_json_rows(path: Path) -> list[Any]:
    """The rows of a plain `.json` file: the list in it, or the one object as a
    list of one. Refuses a file over `MAX_JSON_BYTES`."""
    size = path.stat().st_size
    if size > MAX_JSON_BYTES:
        raise ValueError(
            f"{path.name} is {size / 1e6:.0f} MB. Refusing to load a plain "
            ".json file this large for a preview -- there's no streaming "
            "reader for this format, so the real conversion would load the "
            "whole thing into memory too."
        )
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, list) else [data]


def _jsonl_result(raw: bytes, byte: int, keep_rows: bool) -> dict[str, Any]:
    """The line read at `byte`, fingerprinted from its raw bytes, the same way
    `prepare_rows.py` fingerprints a JSONL line."""
    if not raw:
        return {
            "row": None,
            "fingerprint": None,
            "raw_line": None,
            "note": f"The file has no line starting at byte {byte}.",
        }
    row = None
    raw_line = None
    if keep_rows:
        try:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                row = clean_value(parsed)
        except ValueError:  # not UTF-8, or not JSON
            pass
        if row is None:
            raw_line = raw.decode("utf-8", errors="replace").rstrip("\r\n")[:MAX_RAW_LINE_CHARS]
    return {"row": row, "fingerprint": line_fingerprint(raw), "raw_line": raw_line, "note": None}


def _original_jsonl_rows(path: Path, bytes_: list[int], keep_rows: bool) -> list[dict[str, Any]]:
    """The lines that start at each of `bytes_`, with the file opened once."""
    results: list[dict[str, Any] | None] = [None] * len(bytes_)
    with path.open("rb") as handle:
        for index in sorted(range(len(bytes_)), key=bytes_.__getitem__):
            handle.seek(bytes_[index])
            results[index] = _jsonl_result(handle.readline(), bytes_[index], keep_rows)
    return results  # type: ignore[return-value]


def _original_parquet_rows(path: Path, row_numbers: list[int]) -> list[dict[str, Any] | None]:
    """The rows at `row_numbers` of a parquet file, each the same row
    `prepare_rows.py` got from `read_row_group(...).to_pandas().to_dict("records")`.

    Every row group that holds a wanted row is read once, batch by batch, up to
    its last wanted row. A batch that `may_convert_differently` takes its rows
    from the whole group, which is read at most once.
    """
    import pyarrow.parquet as pq

    parquet_file = pq.ParquetFile(path)
    wanted = sorted(set(row_numbers))
    found: dict[int, dict[str, Any] | None] = {}
    taken = 0
    group_start = 0
    for group in range(parquet_file.num_row_groups):
        if taken == len(wanted):
            break
        group_end = group_start + parquet_file.metadata.row_group(group).num_rows
        first = taken
        while taken < len(wanted) and wanted[taken] < group_end:
            taken += 1
        if taken > first:
            offsets = [number - group_start for number in wanted[first:taken]]
            _read_group_rows(parquet_file, group, offsets, found, group_start)
        group_start = group_end
    for number in wanted[taken:]:
        found[number] = None
    return [found[number] for number in row_numbers]


def _read_group_rows(
    parquet_file: Any,
    group: int,
    offsets: list[int],
    found: dict[int, dict[str, Any] | None],
    group_start: int,
) -> None:
    """Put the rows at `offsets` (sorted, within the group) in `found`, keyed by
    their row number in the file."""
    group_meta = parquet_file.metadata.row_group(group)
    whole: list[dict[str, Any]] | None = None
    seen = 0
    next_offset = 0
    for batch in parquet_file.iter_batches(batch_size=PARQUET_BATCH_ROWS, row_groups=[group]):
        batch_end = seen + batch.num_rows
        if offsets[next_offset] >= batch_end:
            seen = batch_end
            continue
        in_batch = []
        while next_offset < len(offsets) and offsets[next_offset] < batch_end:
            in_batch.append(offsets[next_offset])
            next_offset += 1
        if may_convert_differently(group_meta, batch):
            if whole is None:
                whole = parquet_file.read_row_group(group).to_pandas().to_dict("records")
            for offset in in_batch:
                found[group_start + offset] = whole[offset]
        else:
            records = batch.to_pandas().to_dict("records")
            for offset in in_batch:
                found[group_start + offset] = records[offset - seen]
        seen = batch_end
        if next_offset == len(offsets):
            break


def _original_arrow_rows(path: Path, origins: Sequence[dict[str, Any]]) -> list[dict[str, Any] | None]:
    from datasets import load_from_disk

    loaded = load_from_disk(str(path))
    rows: list[dict[str, Any] | None] = []
    for origin in origins:
        split = origin.get("split")
        try:
            dataset = loaded[split] if split is not None else loaded
            rows.append(dataset[origin["row"]])
        except (IndexError, KeyError, OverflowError):
            rows.append(None)
    return rows


def _original_csv_rows(path: Path, row_numbers: list[int]) -> list[dict[str, Any] | None]:
    size = path.stat().st_size
    if size > MAX_JSON_BYTES:
        raise ValueError(
            f"{path.name} is {size / 1e6:.0f} MB. Refusing to load a .csv file this large "
            "to look up one row -- there's no way to read just one row of it."
        )
    records = pd.read_csv(path).to_dict("records")
    return [records[number] if number < len(records) else None for number in row_numbers]


def _original_json_rows(path: Path, row_numbers: list[int]) -> list[dict[str, Any] | None]:
    rows = load_json_rows(path)
    return [rows[number] if number < len(rows) else None for number in row_numbers]


def read_original_rows(
    input_type: str, origins: Sequence[dict[str, Any]], keep_rows: bool = True
) -> list[dict[str, Any]]:
    """Read the original rows that `origins` point at, all in one file or
    folder, reading it once. Each is fingerprinted the way the preparing step
    did it. `origins` must already be shape-checked (see `lookup_notes`).

    Returns one dict per note, in the same order, with `row` (what's there now,
    or None), `fingerprint` (None when the row isn't there any more), `raw_line`
    (JSONL only, when the line isn't a JSON object) and a `note` saying why a
    row is missing. `keep_rows=False` leaves `row` and `raw_line` as None, for a
    caller that only needs the fingerprints.
    """
    if not origins:
        return []
    path = origins[0]["path"]
    if any(origin["path"] != path for origin in origins):
        raise ValueError("read_original_rows reads the notes of one file or folder at a time.")
    target = Path(path)
    if input_type == "jsonl":
        return _original_jsonl_rows(target, [origin["byte"] for origin in origins], keep_rows)

    row_numbers = [origin["row"] for origin in origins]
    if input_type == "parquet":
        rows = _original_parquet_rows(target, row_numbers)
    elif input_type == "huggingface":
        rows = _original_arrow_rows(target, origins)
    elif input_type == "csv":
        rows = _original_csv_rows(target, row_numbers)
    elif input_type == "json":
        rows = _original_json_rows(target, row_numbers)
    else:
        raise ValueError(f"Unsupported input type: {input_type}")

    results = []
    for number, row in zip(row_numbers, rows):
        if row is None:
            results.append(
                {
                    "row": None,
                    "fingerprint": None,
                    "raw_line": None,
                    "note": f"The file has no row {number}.",
                }
            )
            continue
        row = clean_value(row)
        results.append(
            {
                "row": row if keep_rows else None,
                "fingerprint": row_fingerprint(row),
                "raw_line": None,
                "note": None,
            }
        )
    return results


# -------------------------------------------------------------------------
# Looking notes up
# -------------------------------------------------------------------------


class _PathCheck:
    """What the checks that depend only on a note's path found: an error to
    raise, the file isn't there, or it can be read (with the `source.files`
    entries to compare against the disk)."""

    def __init__(self) -> None:
        self.error: RunError | None = None
        self.not_found = False
        self.entries: list[dict[str, Any]] = []
        self._differences: list[str] | None = None

    @property
    def differences(self) -> list[str]:
        if self._differences is None:
            self._differences = _file_differences(self.entries)
        return self._differences


def _check_path(run: dict[str, Any], origin: dict[str, Any], input_type: str) -> _PathCheck:
    check = _PathCheck()
    path = origin["path"]
    # A note from datasets.load_dataset: a Hub ID, or a local folder of raw files.
    if "config" in origin:
        check.error = LookupNotSupported(
            "Looking up rows read with load_dataset (a Hub dataset) isn't supported yet."
        )
        return check
    try:
        check.entries = _files_to_compare(run, origin, input_type)
    except RunError as exc:
        check.error = exc
        return check
    if input_type == "huggingface" and not Path(path).is_absolute():
        check.error = LookupNotSupported("Looking up rows from a Hub dataset isn't supported yet.")
        return check

    target = Path(path)
    is_there = target.is_dir() if input_type == "huggingface" else target.is_file()
    if not is_there:
        check.not_found = True
    elif input_type == "huggingface" and not any(
        (target / marker).is_file() for marker in HF_MARKER_FILES
    ):
        check.error = LookupNotSupported(
            "Looking up rows read with load_dataset (a local folder of raw files) isn't supported yet."
        )
    return check


def lookup_notes(
    run: dict[str, Any], origins: Sequence[Any], keep_rows: bool = True
) -> list[dict[str, Any] | Exception]:
    """Look up many `_origin` notes of one run (from `open_run`). Each original
    file is read once, however many notes point into it.

    Returns one item per note, in order: the result dict `lookup` returns
    (`unchanged`, `changed` or `not_found`), or the exception `lookup` would
    have raised for it. A `LookupNotSupported` means the note is fine but its
    original can't be looked up yet. `keep_rows=False` leaves `row` and
    `raw_line` empty in the results (see `read_original_rows`).
    """
    input_type = run["summary"].get("input", {}).get("type")
    if not isinstance(input_type, str):
        return [RunError("This run's summary.json has no input.type.")] * len(origins)

    outcomes: list[dict[str, Any] | Exception | None] = [None] * len(origins)
    checks: dict[tuple[str, bool], _PathCheck] = {}
    groups: dict[str, list[int]] = {}
    for index, origin in enumerate(origins):
        try:
            _check_origin_shape(origin, input_type)
        except RunError as exc:
            outcomes[index] = exc
            continue
        key = (origin["path"], "config" in origin)
        if key not in checks:
            checks[key] = _check_path(run, origin, input_type)
        check = checks[key]
        if check.error is not None:
            outcomes[index] = check.error
        elif check.not_found:
            outcomes[index] = _result("not_found", run, input_type, origin)
        else:
            groups.setdefault(origin["path"], []).append(index)

    for path, indices in groups.items():
        differences = checks[(path, False)].differences
        group_origins = [origins[index] for index in indices]
        try:
            originals = read_original_rows(input_type, group_origins, keep_rows)
        except FileNotFoundError:
            for index in indices:
                outcomes[index] = _result(
                    "not_found", run, input_type, origins[index], file_differences=differences
                )
            continue
        except Exception as exc:  # a real reader failure: each note of this file gets it
            for index in indices:
                outcomes[index] = exc
            continue
        for index, original in zip(indices, originals):
            origin = origins[index]
            unchanged = original["fingerprint"] == origin["fingerprint"]
            outcomes[index] = _result(
                "unchanged" if unchanged else "changed",
                run,
                input_type,
                origin,
                file_differences=differences,
                row=json_safe(original["row"]) if original["row"] is not None else None,
                raw_line=original["raw_line"],
                note=original["note"],
            )
    return outcomes  # type: ignore[return-value]


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
    outcome = lookup_notes(run, [origin])[0]
    if isinstance(outcome, Exception):
        raise outcome
    return outcome
