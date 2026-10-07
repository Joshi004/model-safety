#!/usr/bin/env python3
"""Prepare arbitrary rows for MedPsy Synthetic Data Generator pipelines.

The output contract is intentionally small: map the columns required by a
pipeline to their logical names and pack every remaining source field into a
single metadata column.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

# This script is invoked directly (see the docs), so only its own directory
# lands on sys.path -- that's what makes the next import work at all. Add
# the repo root too, so the text-coverage check below can import the PII
# scanner's own extractor no matter how the caller sets PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chunked_writer import write_chunked_output  # noqa: E402
from progress_file import ROWS_PER_CLOCK_CHECK, ProgressFile  # noqa: E402
from modelsafety.contract.fingerprints import (  # noqa: E402
    FINGERPRINT_RECIPE_VERSION,
    file_sha256,
    line_fingerprint,
    row_fingerprint,
)
from modelsafety.contract.origin import ORIGIN_KEY, json_safe  # noqa: E402
from modelsafety.contract.run_identity import (  # noqa: E402
    RunIdentityError,
    check_run_identity,
    describe_input_files,
    make_identity,
)
from modelsafety.contract.textract import extract_record_text  # noqa: E402


SUPPORTED_INPUT_TYPES = ("jsonl", "json", "csv", "parquet", "huggingface")
SKIPPED_ROWS_FILENAME = "skipped_rows.ndjson"

# One original row: its ``_origin`` note, the row itself, and the
# ``source.files`` entry it counts towards (None when no per-file counts exist).
_SourceRow = tuple[Dict[str, Any], Dict[str, Any], Optional[Dict[str, Any]]]


def _parse_mapping(values: List[str]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Invalid mapping '{value}'. Expected target=source.")
        target, source = value.split("=", 1)
        target = target.strip()
        source = source.strip()
        if not target or not source:
            raise ValueError(f"Invalid mapping '{value}'. Expected target=source.")
        mapping[target] = source
    return mapping


def _clean_value(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        # pandas hands back list-typed parquet/csv columns (e.g. a "messages"
        # column holding a list of {role, content} dicts) as numpy arrays,
        # not plain Python lists. json.dumps has no idea what a numpy array
        # is, so this converts it back to something JSON-serializable --
        # then re-runs _clean_value on each item to still catch NaNs nested
        # inside (e.g. a NaN inside one of the dicts in the array).
        return [_clean_value(item) for item in value.tolist()]
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, dict):
        return {key: _clean_value(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_clean_value(item) for item in value]
    return value


def _discover_files(path: Path, pattern: str) -> List[Path]:
    """Return files matching ``pattern`` under ``path`` (or just ``path`` itself)."""
    return sorted(path.rglob(pattern)) if path.is_dir() else [path]


def _source_file_entry(path: Path, fingerprint: str) -> Dict[str, Any]:
    """The ``summary.json`` ``source.files`` entry for one original file."""
    stat = path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    return {
        "path": str(path.resolve()),
        "size_bytes": stat.st_size,
        "modified": modified.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fingerprint": fingerprint,
    }


def _start_counts(file_entry: Dict[str, Any]) -> None:
    """Add the per-file counters that ``prepare_rows()`` fills in."""
    file_entry["rows_written"] = 0
    file_entry["rows_skipped"] = 0
    file_entry["rows_left_out"] = 0


def _read_jsonl_file(
    file_path: Path,
    on_progress: Optional[Callable[[int], None]] = None,
) -> tuple[List[tuple[Dict[str, Any], Dict[str, Any]]], Dict[str, Any]]:
    """Read one JSONL file: ``(origin, row)`` pairs, plus its fingerprint and line counts.

    The file is read as bytes so every line is counted and fingerprinted
    exactly as it sits on disk, and so each row's ``_origin`` can say where
    its line starts. Any line that can't be read stops the run with
    ``<path>:<line>:`` at the front of the message.

    ``on_progress`` is called every ``ROWS_PER_CLOCK_CHECK`` lines with the
    bytes read so far in this file.
    """
    resolved = file_path.resolve()
    digest = hashlib.sha256()
    pairs: List[tuple[Dict[str, Any], Dict[str, Any]]] = []
    lines = 0
    blank_lines = 0
    next_byte = 0

    with file_path.open("rb") as handle:
        for raw in handle:
            digest.update(raw)
            lines += 1
            line_start = next_byte
            next_byte += len(raw)
            if on_progress is not None and lines % ROWS_PER_CLOCK_CHECK == 0:
                on_progress(next_byte)
            if not raw.strip():
                blank_lines += 1
                continue
            # Decode before json.loads, because json.loads on bytes quietly
            # accepts a byte order mark and UTF-16/32.
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"{resolved}:{lines}: not valid UTF-8 ({exc})") from exc
            try:
                row = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{resolved}:{lines}: not valid JSON ({exc})") from exc
            if not isinstance(row, dict):
                raise ValueError(
                    f"{resolved}:{lines}: not a JSON object (found {type(row).__name__})"
                )
            origin = {
                "path": str(resolved),
                "line": lines,
                "byte": line_start,
                "fingerprint": line_fingerprint(raw),
                "v": FINGERPRINT_RECIPE_VERSION,
            }
            pairs.append((origin, row))

    entry = _source_file_entry(file_path, digest.hexdigest())
    entry["lines"] = lines
    entry["blank_lines"] = blank_lines
    entry["rows_read"] = len(pairs)
    _start_counts(entry)
    return pairs, entry


def _iter_jsonl_files(paths: Iterable[Path]) -> Iterable[Dict[str, Any]]:
    for file_path in paths:
        for _origin, row in _read_jsonl_file(file_path)[0]:
            yield row


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    return _iter_jsonl_files(_discover_files(path, "*.jsonl"))


def _arrow_file_row_counts(dataset_dir: Path) -> List[int]:
    """Independently count the rows in each file of a ``Dataset.save_to_disk()`` folder.

    Reads the exact ``.arrow`` file(s) ``load_from_disk()`` would use --
    listed in the folder's own ``state.json``, and returned in that order --
    through ``pyarrow.ipc`` instead of the ``datasets`` library. This is a
    second, independent reader of the same data, the same role the parquet
    footer check above plays for parquet input: it can catch a problem in
    how ``datasets`` loaded the rows, which comparing a count against itself
    cannot.
    """
    import pyarrow as pa

    state = json.loads((dataset_dir / "state.json").read_text(encoding="utf-8"))
    counts = []
    for entry in state["_data_files"]:
        file_path = dataset_dir / entry["filename"]
        with pa.memory_map(str(file_path), "rb") as source:
            try:
                reader = pa.ipc.open_stream(source)
            except pa.ArrowInvalid:
                source.seek(0)
                reader = pa.ipc.open_file(source)
            counts.append(reader.read_all().num_rows)
    return counts


def _arrow_ipc_row_count(dataset_dir: Path) -> int:
    """Total rows in a ``Dataset.save_to_disk()`` folder, counted independently."""
    return sum(_arrow_file_row_counts(dataset_dir))


def _arrow_items(
    root: Path,
    split: Optional[str],
    rows: List[Dict[str, Any]],
    source_files: List[Dict[str, Any]],
    file_row_counts: Optional[List[int]],
) -> List[_SourceRow]:
    """``(origin, row, file_entry)`` for every row loaded from a ``save_to_disk`` folder.

    ``datasets`` loads the folder's files one after the other, so each row
    belongs to the file whose row range it falls in. That needs the per-file
    counts to add up to the rows loaded. If they weren't readable or don't,
    no row is assigned to a file, and the whole-split check in
    ``_run_checks`` reports the difference instead.
    """
    path = str(root.resolve())
    file_entries: List[Optional[Dict[str, Any]]] = [None] * len(rows)
    if file_row_counts is not None and sum(file_row_counts) == len(rows):
        file_entries = []
        for file_entry, count in zip(source_files, file_row_counts):
            file_entry["rows_in_footer"] = count
            _start_counts(file_entry)
            file_entries.extend([file_entry] * count)
    return [
        ({"path": path, "split": split, "row": index}, row, file_entry)
        for index, (row, file_entry) in enumerate(zip(rows, file_entries))
    ]


def _arrow_data_files(dataset_dir: Path) -> List[Path]:
    """The ``.arrow`` files a ``save_to_disk()`` folder's own ``state.json`` lists."""
    state_path = dataset_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    files = []
    for entry in state["_data_files"]:
        filename = Path(entry["filename"])
        if filename.is_absolute() or ".." in filename.parts:
            raise ValueError(
                f"{state_path} lists a data file outside its own folder: {entry['filename']}"
            )
        files.append(dataset_dir / filename)
    return files


def _discover_input_files(args: argparse.Namespace) -> List[Path]:
    """The original files ``_read_rows`` will read, found without reading any data.

    A Hugging Face Hub dataset ID has no local files, so it gives an empty list.
    """
    input_path = Path(args.input_path)

    if args.input_type == "jsonl":
        return _discover_files(input_path, "*.jsonl")
    if args.input_type == "parquet":
        return _discover_files(input_path, "*.parquet")
    if args.input_type in ("csv", "json"):
        return [input_path]
    if args.input_type != "huggingface":
        raise ValueError(f"Unsupported input type: {args.input_type}")

    if (input_path / "state.json").exists():
        return _arrow_data_files(input_path)
    if (input_path / "dataset_dict.json").exists():
        dict_info = json.loads((input_path / "dataset_dict.json").read_text(encoding="utf-8"))
        splits = dict_info.get("splits", [])
        if args.split not in splits:
            raise ValueError(
                f"Split '{args.split}' not found; available splits: {sorted(splits)}"
            )
        return _arrow_data_files(input_path / args.split)
    if input_path.is_dir():
        return sorted(
            path
            for path in input_path.rglob("*")
            if path.is_file()
            and not any(part.startswith(".") for part in path.relative_to(input_path).parts)
        )
    if input_path.is_file():
        return [input_path]
    return []


def _run_identity(args: argparse.Namespace, input_files: List[Path]) -> Dict[str, Any]:
    """What this run is, for ``run_identity.json``.

    Every setting counts except ``--output-dir`` (where the run lives) and
    ``--input-path`` (recorded as ``input_root``, resolved), because a
    different setting would leave the folder's old files behind.
    """
    input_path = Path(args.input_path)
    settings = {
        name: value
        for name, value in vars(args).items()
        if name not in ("output_dir", "input_path")
    }
    if input_path.exists():
        input_root = str(input_path.resolve())
        base_dir = input_path if input_path.is_dir() else input_path.parent
    else:
        # A Hub dataset ID: recorded as given, with no files.
        input_root = args.input_path
        base_dir = input_path
    return make_identity(
        tool="prepare_rows",
        mode=None,
        settings=settings,
        input_root=input_root,
        input_files=describe_input_files(base_dir, input_files),
    )


def _numbered_items(
    file_path: Path, rows: List[Dict[str, Any]], file_entry: Dict[str, Any]
) -> List[_SourceRow]:
    """``(origin, row, file_entry)`` for the rows of one file, numbered from 0."""
    path = str(file_path.resolve())
    _start_counts(file_entry)
    return [({"path": path, "row": index}, row, file_entry) for index, row in enumerate(rows)]


def _dataset_rows(dataset: Any, progress: ProgressFile) -> List[Dict[str, Any]]:
    """``list(dataset)``, reporting progress while the rows are read."""
    progress.phase("reading", unit="rows", total=dataset.num_rows)
    rows: List[Dict[str, Any]] = []
    for row in dataset:
        rows.append(row)
        if len(rows) % ROWS_PER_CLOCK_CHECK == 0:
            progress.update(len(rows))
    progress.update(len(rows))
    return rows


def _read_rows(
    args: argparse.Namespace, input_files: List[Path], progress: ProgressFile
) -> tuple[List[_SourceRow], Dict[str, Any]]:
    """Read source rows, plus whatever row count the source gives us for free.

    ``input_files`` is what ``_discover_input_files`` found. ``progress``
    is told which phase this is in and how far it has got.

    Returns ``(items, source_info)``. ``items`` has one ``(origin, row,
    file_entry)`` per row, in the original order: files in the order found,
    rows in the order the reader gives them. ``origin`` is the row's
    ``_origin`` note.

    ``source_info["expected_rows"]`` is an
    independent row count read from source metadata (a parquet footer, or a
    HuggingFace dataset's own ``num_rows``) when one exists at no extra I/O
    cost, and ``None`` otherwise -- getting an independent count for jsonl,
    json, or csv would mean a second full pass over the data, which is
    exactly the expensive thing this check is supposed to avoid.
    """
    input_path = Path(args.input_path)
    expected_rows: Optional[int] = None
    expected_rows_from = "unavailable"
    files_discovered: Optional[int] = None
    source_files: List[Dict[str, Any]] = []

    if args.input_type != "jsonl" and input_files:
        # JSONL files are fingerprinted while they are read, below.
        progress.phase("fingerprinting_files", unit="files", total=len(input_files))
        for number, path in enumerate(input_files, 1):
            source_files.append(_source_file_entry(path, file_sha256(path)))
            progress.update(number)

    items: List[_SourceRow] = []

    if args.input_type == "jsonl":
        files_discovered = len(input_files)
        progress.phase(
            "reading",
            unit="bytes",
            total=sum(path.stat().st_size for path in input_files),
            files_total=len(input_files),
        )
        bytes_before = 0
        for number, file_path in enumerate(input_files, 1):
            pairs, file_entry = _read_jsonl_file(
                file_path, on_progress=lambda read: progress.update(bytes_before + read)
            )
            items.extend((origin, row, file_entry) for origin, row in pairs)
            source_files.append(file_entry)
            bytes_before += file_entry["size_bytes"]
            progress.update(bytes_before, files_done=number)
        expected_rows_from = "unavailable: jsonl has no free row count"
    elif args.input_type == "json":
        progress.phase("reading")
        with input_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        rows = data if isinstance(data, list) else [data]
        source_files[0]["rows_read"] = len(rows)
        items = _numbered_items(input_path, rows, source_files[0])
        expected_rows_from = "unavailable: json has no free row count"
    elif args.input_type == "csv":
        progress.phase("reading")
        rows = pd.read_csv(input_path).to_dict("records")
        source_files[0]["rows_read"] = len(rows)
        items = _numbered_items(input_path, rows, source_files[0])
        expected_rows_from = "unavailable: csv has no free row count"
    elif args.input_type == "parquet":
        import pyarrow.parquet as pq

        files_discovered = len(input_files)
        expected_rows = 0
        # The footers alone give the total, so progress can say "x of y rows".
        progress.phase(
            "reading",
            unit="rows",
            total=sum(pq.read_metadata(path).num_rows for path in input_files),
            files_total=len(input_files),
        )
        rows_before = 0
        # One file at a time, one row group at a time, so every row knows its
        # file and a later lookup can read just the one row group.
        for number, (file_path, file_entry) in enumerate(zip(input_files, source_files), 1):
            parquet_file = pq.ParquetFile(file_path)
            file_rows: List[Dict[str, Any]] = []
            for group in range(parquet_file.num_row_groups):
                file_rows.extend(parquet_file.read_row_group(group).to_pandas().to_dict("records"))
                progress.update(rows_before + len(file_rows))
            file_entry["rows_in_footer"] = parquet_file.metadata.num_rows
            expected_rows += parquet_file.metadata.num_rows
            items.extend(_numbered_items(file_path, file_rows, file_entry))
            rows_before += len(file_rows)
            progress.update(rows_before, files_done=number)
        expected_rows_from = "parquet file footers"
    elif args.input_type == "huggingface":
        if (input_path / "state.json").exists():
            # A single Dataset saved with Dataset.save_to_disk() -- load_dataset()
            # refuses these (it checks for this exact file and tells you to use
            # load_from_disk instead), so go straight to the function that works.
            from datasets import load_from_disk

            dataset = load_from_disk(str(input_path))
            rows = _dataset_rows(dataset, progress)
            file_row_counts: Optional[List[int]] = None
            try:
                file_row_counts = _arrow_file_row_counts(input_path)
                expected_rows = sum(file_row_counts)
                expected_rows_from = "arrow ipc row count"
            except Exception as exc:  # best-effort -- never block a real conversion on this
                expected_rows = dataset.num_rows
                expected_rows_from = (
                    f"huggingface dataset num_rows (arrow ipc read failed: {exc})"
                )
            items = _arrow_items(input_path, None, rows, source_files, file_row_counts)
        elif (input_path / "dataset_dict.json").exists():
            # A DatasetDict saved with DatasetDict.save_to_disk() -- same idea,
            # but we also need to pick the requested split out of the dict.
            from datasets import load_from_disk

            ds_dict = load_from_disk(str(input_path))
            if args.split not in ds_dict:
                raise ValueError(
                    f"Split '{args.split}' not found; available splits: {sorted(ds_dict.keys())}"
                )
            dataset = ds_dict[args.split]
            rows = _dataset_rows(dataset, progress)
            file_row_counts = None
            try:
                # Each split of a saved DatasetDict is itself a save_to_disk
                # folder, at <root>/<split>/, with its own state.json.
                file_row_counts = _arrow_file_row_counts(input_path / args.split)
                expected_rows = sum(file_row_counts)
                expected_rows_from = "arrow ipc row count"
            except Exception as exc:  # best-effort -- never block a real conversion on this
                expected_rows = dataset.num_rows
                expected_rows_from = (
                    f"huggingface dataset num_rows (arrow ipc read failed: {exc})"
                )
            items = _arrow_items(input_path, args.split, rows, source_files, file_row_counts)
        else:
            # Not a save_to_disk folder -- a Hugging Face Hub dataset ID, or a
            # local folder of raw data files a builder can auto-detect.
            from datasets import load_dataset

            kwargs: Dict[str, Any] = {"split": args.split}
            if args.hf_config:
                kwargs["name"] = args.hf_config
            # The download and build are one wait with no total; the row
            # count is only known once load_dataset returns.
            progress.phase("reading")
            dataset = load_dataset(args.input_path, **kwargs)
            expected_rows = dataset.num_rows
            expected_rows_from = "huggingface dataset num_rows"
            rows = _dataset_rows(dataset, progress)
            # A Hub ID is recorded as given; a local folder by its full path.
            path = str(input_path.resolve()) if input_path.exists() else args.input_path
            origin_start: Dict[str, Any] = {"path": path}
            if args.hf_config:
                origin_start["config"] = args.hf_config
            origin_start["split"] = args.split
            # No per-file counts here: there's no way to tell which file of a
            # builder-loaded dataset a row came from.
            items = [({**origin_start, "row": index}, row, None) for index, row in enumerate(rows)]
    else:
        raise ValueError(f"Unsupported input type: {args.input_type}")

    if args.max_rows is not None:
        for _origin, _row, file_entry in items[args.max_rows :]:
            if file_entry is not None:
                file_entry["rows_left_out"] += 1
        items = items[: args.max_rows]

    # A JSONL origin already carries the fingerprint of its raw line. Every
    # other format is fingerprinted on the row as cleaned here, which is
    # what a later lookup can reproduce.
    progress.phase("fingerprinting_rows", unit="rows", total=len(items))
    for index, (origin, row, file_entry) in enumerate(items):
        row = _clean_value(row)
        if "fingerprint" not in origin:
            origin["fingerprint"] = row_fingerprint(row)
            origin["v"] = FINGERPRINT_RECIPE_VERSION
        items[index] = (origin, row, file_entry)
        if (index + 1) % ROWS_PER_CLOCK_CHECK == 0:
            progress.update(index + 1)

    source_info = {
        "rows_read": len(items),
        "expected_rows": expected_rows,
        "expected_rows_from": expected_rows_from,
        "files_discovered": files_discovered,
        "files": source_files,
    }
    return items, source_info


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _prepare_row(
    row: Dict[str, Any],
    mapping: Dict[str, str],
    required_columns: List[str],
    metadata_column: str,
    exclude_metadata: set[str],
) -> tuple[Dict[str, Any] | None, str | None]:
    """Return ``(prepared_row, None)``, or ``(None, missing_column)`` if skipped."""
    prepared: Dict[str, Any] = {}
    used_sources: set[str] = set()

    for target, source in mapping.items():
        if source in row:
            prepared[target] = row[source]
            used_sources.add(source)
        elif target in row:
            prepared[target] = row[target]
            used_sources.add(target)

    for column in required_columns:
        if column not in prepared and column in row:
            prepared[column] = row[column]
            used_sources.add(column)
        if not _is_present(prepared.get(column)):
            return None, column

    metadata: Dict[str, Any] = {}
    existing_metadata = row.get(metadata_column)
    if isinstance(existing_metadata, dict):
        metadata.update(existing_metadata)

    excluded = set(exclude_metadata)
    excluded.update(used_sources)
    excluded.update(mapping.keys())
    excluded.add(metadata_column)

    for key, value in row.items():
        if key not in excluded:
            metadata[key] = value

    prepared[metadata_column] = metadata
    return prepared, None


def _origin_location(origin: Dict[str, Any]) -> str:
    if "line" in origin:
        return f"{origin['path']}:{origin['line']}"
    return f"{origin['path']} row {origin['row']}"


def _refuse_existing_origin(
    row: Dict[str, Any], origin: Dict[str, Any], metadata_column: str
) -> None:
    """Stop if the row already has the ``_origin`` name this step adds itself.

    That also happens when the input is data this step already prepared.
    """
    if not isinstance(row, dict):
        return
    existing_metadata = row.get(metadata_column)
    if ORIGIN_KEY in row:
        where = "a top-level column"
    elif isinstance(existing_metadata, dict) and ORIGIN_KEY in existing_metadata:
        where = f"a key inside its '{metadata_column}' column"
    else:
        return
    raise ValueError(
        f"{_origin_location(origin)}: the row already has '{ORIGIN_KEY}' as {where}. "
        f"The preparing step adds its own '{ORIGIN_KEY}' note to every row, so it "
        "can't read rows that already have one (for example, rows it prepared earlier)."
    )


def _skipped_entry(
    origin: Dict[str, Any],
    row: Dict[str, Any],
    target: str,
    mapping: Dict[str, str],
) -> Dict[str, Any]:
    """The ``skipped_rows.ndjson`` entry for a row whose ``target`` came up empty.

    ``_origin`` has to stay the first key: the tripwire in
    ``modelsafety/contract/origin.py`` recognises these entries by it.
    """
    source = mapping.get(target, target)
    reason = "missing_column" if source not in row and target not in row else "empty_column"
    if source in row:
        column = source
    elif target in row:
        column = target
    else:
        column = source
    return {
        ORIGIN_KEY: origin,
        "reason": reason,
        "column": column,
        "used_as": target,
        "row": json_safe(row),
    }


def prepare_rows(
    args: argparse.Namespace,
    input_files: List[Path],
    progress: ProgressFile,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    """Return ``(prepared_rows, skipped_entries, source_info, rows_info)``.

    ``input_files`` is what ``_discover_input_files`` found. ``progress``
    reports the reading, fingerprinting and preparing phases.

    ``source_info`` comes from ``_read_rows`` (option 1's free metadata
    count, see ``DATA_VALIDATION_OPTIONS.md``). ``rows_info`` adds the
    write-side counts: how many rows were kept, how many were skipped, and
    -- unlike the original bare total -- which required column caused each
    skip.

    Every row is judged in its original order, so ``skipped_entries`` reads
    file by file, line by line. Only the kept rows are shuffled, afterwards.
    """
    mapping = _parse_mapping(args.map or [])
    items, source_info = _read_rows(args, input_files, progress)

    prepared_rows: List[Dict[str, Any]] = []
    skipped_entries: List[Dict[str, Any]] = []
    skipped_by_required_column: Dict[str, int] = {}
    exclude_metadata = set(args.exclude_metadata or [])

    progress.phase("preparing", unit="rows", total=len(items))
    # `judged` is how many rows came before this one.
    for judged, (origin, row, file_entry) in enumerate(items):
        if judged and judged % ROWS_PER_CLOCK_CHECK == 0:
            progress.update(
                judged, rows_written=len(prepared_rows), rows_skipped=len(skipped_entries)
            )
        _refuse_existing_origin(row, origin, args.metadata_column)
        prepared, missing_column = _prepare_row(
            row=row,
            mapping=mapping,
            required_columns=args.require or [],
            metadata_column=args.metadata_column,
            exclude_metadata=exclude_metadata,
        )
        if prepared is None:
            key = missing_column or "unknown"
            skipped_by_required_column[key] = skipped_by_required_column.get(key, 0) + 1
            skipped_entries.append(_skipped_entry(origin, row, key, mapping))
            if file_entry is not None:
                file_entry["rows_skipped"] += 1
            continue
        prepared[args.metadata_column][ORIGIN_KEY] = origin
        prepared_rows.append(prepared)
        if file_entry is not None:
            file_entry["rows_written"] += 1
    progress.update(
        len(items), rows_written=len(prepared_rows), rows_skipped=len(skipped_entries)
    )

    if args.shuffle:
        random.seed(args.seed)
        random.shuffle(prepared_rows)

    skipped = sum(skipped_by_required_column.values())
    print(f"Prepared {len(prepared_rows)} rows; skipped {skipped} rows.")

    rows_info = {
        "written": len(prepared_rows),
        "skipped": skipped,
        "skipped_by_required_column": skipped_by_required_column,
    }
    return prepared_rows, skipped_entries, source_info, rows_info


def _text_coverage(
    rows: List[Dict[str, Any]], on_progress: Optional[Callable[[int], None]] = None
) -> Dict[str, Any]:
    """Coverage of the scanner's own text extractor over the prepared rows.

    Calls ``extract_record_text()`` -- the exact function the PII scanner
    itself calls on every row (see ``modelsafety/contract/textract.py``) --
    so this cannot disagree with what a later scan will actually see. This
    is option 4 in ``DATA_VALIDATION_OPTIONS.md``.

    ``on_progress`` is called every ``ROWS_PER_CLOCK_CHECK`` rows with how
    many have been looked at.
    """
    if not rows:
        return {
            "rows_with_text": 0,
            "coverage": 0.0,
            "total_characters": 0,
            "mean_characters": 0.0,
        }

    lengths: List[int] = []
    for count, row in enumerate(rows, 1):
        lengths.append(len(extract_record_text(row)))
        if on_progress is not None and count % ROWS_PER_CLOCK_CHECK == 0:
            on_progress(count)
    rows_with_text = sum(1 for length in lengths if length > 0)
    total_characters = sum(lengths)
    return {
        "rows_with_text": rows_with_text,
        "coverage": rows_with_text / len(rows),
        "total_characters": total_characters,
        "mean_characters": total_characters / len(rows),
    }


def _write_skipped_rows(output_dir: Path, entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Write ``skipped_rows.ndjson`` (always, even with no entries) and report on it.

    ``entries`` is the number of lines found on disk afterwards, not the
    number written. No ``sort_keys``: ``_origin`` has to stay the first key.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    skipped_path = output_dir / SKIPPED_ROWS_FILENAME
    with skipped_path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    with skipped_path.open("rb") as handle:
        entries_on_disk = sum(1 for _ in handle)

    by_reason: Dict[str, int] = {}
    by_file: Dict[str, int] = {}
    for entry in entries:
        by_reason[entry["reason"]] = by_reason.get(entry["reason"], 0) + 1
        path = entry[ORIGIN_KEY]["path"]
        by_file[path] = by_file.get(path, 0) + 1
    return {
        "file": SKIPPED_ROWS_FILENAME,
        "entries": entries_on_disk,
        "by_reason": by_reason,
        "by_file": by_file,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-path", required=True, help="Input file, directory, or HuggingFace dataset ID.")
    parser.add_argument("--input-type", choices=SUPPORTED_INPUT_TYPES, default="jsonl")
    parser.add_argument("--output-dir", required=True, help="Output directory for chunked JSONL files.")
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="TARGET=SOURCE",
        help="Map a source column to the logical column required by a pipeline.",
    )
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        help="Require a mapped/prepared column to be present and non-empty.",
    )
    parser.add_argument("--metadata-column", default="metadata")
    parser.add_argument(
        "--exclude-metadata",
        action="append",
        default=[],
        help="Additional source columns to omit from metadata.",
    )
    parser.add_argument("--split", default="train", help="HuggingFace split.")
    parser.add_argument("--hf-config", default="", help="Optional HuggingFace dataset config/name.")
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("--shuffle", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--num-chunks", type=int, default=1)
    parser.add_argument("--rows-per-file", type=int, default=1000)
    parser.add_argument("--dir-prefix", default="chunk-")
    parser.add_argument("--file-prefix", default="data")
    return parser


def _run_checks(
    source_info: Dict[str, Any],
    rows_info: Dict[str, Any],
    output_info: Dict[str, Any],
    coverage_info: Dict[str, Any],
    max_rows: Optional[int],
) -> Dict[str, Any]:
    """Reconcile counts and judge text coverage.

    Implements options 1 and 4 from ``DATA_VALIDATION_OPTIONS.md``. Hard
    failures are for the things that are exact and can't be argued with: a
    count that doesn't add up, or zero rows/zero text coverage. Partial text
    coverage is only a warning -- it's often legitimate, and guessing a pass
    threshold is exactly what that document's "score first, filter second"
    section warns against.
    """
    failures: List[str] = []
    warnings: List[str] = []

    rows_read = source_info["rows_read"]
    written = rows_info["written"]
    skipped = rows_info["skipped"]
    if rows_read != written + skipped:
        failures.append(
            f"Row accounting does not add up: {rows_read} read != "
            f"{written} written + {skipped} skipped"
        )

    for file_info in source_info["files"]:
        if "lines" not in file_info:
            continue
        if file_info["lines"] != file_info["blank_lines"] + file_info["rows_read"]:
            failures.append(
                f"Line accounting does not add up for {file_info['path']}: "
                f"{file_info['lines']} lines != {file_info['blank_lines']} blank + "
                f"{file_info['rows_read']} rows read"
            )

    for file_info in source_info["files"]:
        if "rows_written" not in file_info:
            continue
        accounted = (
            file_info["rows_written"] + file_info["rows_skipped"] + file_info["rows_left_out"]
        )
        if "lines" in file_info:
            in_file, in_file_name = file_info["lines"], "lines"
            accounted += file_info["blank_lines"]
        elif "rows_in_footer" in file_info:
            in_file, in_file_name = file_info["rows_in_footer"], "rows in the file's own count"
        else:
            in_file, in_file_name = file_info["rows_read"], "rows read"
        if in_file != accounted:
            failures.append(
                f"File accounting does not add up for {file_info['path']}: {in_file} "
                f"{in_file_name} != {file_info.get('blank_lines', 0)} blank + "
                f"{file_info['rows_written']} written + {file_info['rows_skipped']} skipped + "
                f"{file_info['rows_left_out']} left out"
            )

    skipped_entries_on_disk = rows_info["skipped_file"]["entries"]
    if skipped_entries_on_disk != skipped:
        failures.append(
            f"{SKIPPED_ROWS_FILENAME} holds {skipped_entries_on_disk} entries on disk, "
            f"but {skipped} rows were skipped"
        )

    rows_on_disk = output_info["rows_on_disk"]
    if rows_on_disk != written:
        failures.append(
            f"Rows on disk ({rows_on_disk}) do not match rows written ({written})"
        )

    expected_rows = source_info["expected_rows"]
    if expected_rows is not None:
        expected_after_slice = (
            min(max_rows, expected_rows) if max_rows is not None else expected_rows
        )
        if rows_read != expected_after_slice:
            failures.append(
                f"Rows read ({rows_read}) do not match the source's own row count "
                f"({expected_after_slice}, from {source_info['expected_rows_from']})"
            )

    if written == 0:
        failures.append(
            "No rows were written -- every input row was skipped, or the input was empty"
        )
    elif coverage_info["rows_with_text"] == 0:
        failures.append(
            "0% text coverage -- extract_record_text() finds no scannable text "
            "in any written row"
        )
    elif coverage_info["coverage"] < 1.0:
        warnings.append(
            f"Only {coverage_info['coverage'] * 100:.1f}% of written rows have "
            "scannable text"
        )

    return {"passed": not failures, "failures": failures, "warnings": warnings}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    output_dir = Path(args.output_dir)

    # Checked before the output folder is touched, so a refusal leaves
    # nothing behind.
    reserved = f"the preparing step adds its own '{ORIGIN_KEY}' note to every row"
    map_targets = [value.split("=", 1)[0].strip() for value in args.map or []]
    if ORIGIN_KEY in map_targets:
        parser.error(f"'{ORIGIN_KEY}' can't be a --map target: {reserved}.")
    if args.metadata_column == ORIGIN_KEY:
        parser.error(f"--metadata-column can't be '{ORIGIN_KEY}': {reserved}.")

    # Before anything is touched: refuse an output folder that holds a
    # different run, so old and new files never end up side by side.
    input_files = _discover_input_files(args)
    try:
        check_run_identity(output_dir, _run_identity(args, input_files))
    except RunIdentityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)

    # The identity check passed, so this folder is this run's to write into.
    progress = ProgressFile(output_dir)
    success_marker = output_dir / "_SUCCESS"
    try:
        progress.phase("finding_files")

        # Option 3: a stale marker from a previous, possibly-failed run must not
        # be mistaken for this run having succeeded.
        if success_marker.exists():
            success_marker.unlink()

        prepared_rows, skipped_entries, source_info, rows_info = prepare_rows(
            args, input_files, progress
        )
        progress.phase("checking_text", unit="rows", total=len(prepared_rows))
        coverage_info = _text_coverage(prepared_rows, on_progress=progress.update)
        progress.phase("writing", unit="rows", total=len(prepared_rows))
        output_info = write_chunked_output(
            prepared_rows,
            output_dir=args.output_dir,
            num_chunks=args.num_chunks,
            rows_per_file=args.rows_per_file,
            dir_prefix=args.dir_prefix,
            file_prefix=args.file_prefix,
            on_file_written=progress.update,
        )
        rows_info["skipped_file"] = _write_skipped_rows(output_dir, skipped_entries)

        print(
            f"Text coverage: {coverage_info['rows_with_text']}/{rows_info['written']} rows "
            f"({coverage_info['coverage'] * 100:.1f}%) have scannable text."
        )

        progress.phase("finishing")
        checks = _run_checks(
            source_info=source_info,
            rows_info=rows_info,
            output_info=output_info,
            coverage_info=coverage_info,
            max_rows=args.max_rows,
        )

        # The report is written whether or not the run passes, so a failure is
        # diagnosable from a file sitting next to the data, not just scrollback.
        summary = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "tool": "prepare_rows.py",
            "input": {
                "path": str(args.input_path),
                "type": args.input_type,
                "map": _parse_mapping(args.map or []),
                "require": args.require or [],
                "max_rows": args.max_rows,
            },
            "source": source_info,
            "rows": rows_info,
            "output": output_info,
            "text_coverage": coverage_info,
            "checks": checks,
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        # "done" means the run finished, whether or not its checks passed;
        # summary.json says which.
        progress.phase("done")
    except BaseException as exc:
        try:
            progress.fail(str(exc) or type(exc).__name__)
        except OSError:
            pass  # Reporting the failure must never replace the run's own error.
        raise

    for warning in checks["warnings"]:
        print(f"WARNING: {warning}")

    if not checks["passed"]:
        for failure in checks["failures"]:
            print(f"FAILED: {failure}")
        raise SystemExit(2)

    # Only touch the marker once every check above has passed -- see the
    # marker-file discussion (option 3) in DATA_VALIDATION_OPTIONS.md.
    success_marker.touch()


if __name__ == "__main__":
    main()
