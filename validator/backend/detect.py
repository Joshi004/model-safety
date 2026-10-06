"""Finds what's at a path, groups it by format, and reads a small sample.

Every sample reader here mirrors the matching branch of
`prepare_rows.py`'s own `_read_rows()` -- same file discovery, same
libraries, same calls -- just bounded to a handful of rows instead of the
whole file, so this stays cheap to run from a web backend against real,
large production data. Nothing here writes anything.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from reader_imports import arrow_ipc_row_count, clean_value

# -------------------------------------------------------------------------
# Path validation -- any absolute path the running user can read is allowed
# -------------------------------------------------------------------------


class InvalidPath(ValueError):
    """Raised when a requested path is empty or relative."""


def resolve_input_path(path_str: str) -> Path:
    """Resolve `path_str` to an absolute path, refusing empty or relative
    input. Whether it exists or is readable is left to the caller."""
    if not path_str or not path_str.strip():
        raise InvalidPath("No path given.")
    candidate = Path(path_str.strip()).expanduser()
    if not candidate.is_absolute():
        raise InvalidPath(f"Path must be absolute, got: {path_str}")
    return candidate.resolve()


# -------------------------------------------------------------------------
# Format detection
# -------------------------------------------------------------------------

MAX_WALK_FILES = 20_000  # stop scanning a huge tree rather than hang
MAX_FILES_LISTED_PER_GROUP = 50
SKIP_DIR_NAMES = {".git", "__pycache__", "node_modules", ".venv"}

EXTENSION_TO_FORMAT = {
    ".jsonl": "jsonl",
    ".json": "json",
    ".csv": "csv",
    ".parquet": "parquet",
    ".arrow": "arrow",
}

HF_MARKER_FILES = ("state.json", "dataset_dict.json")


@dataclass
class _Group:
    format: str
    input_type: str | None
    sample_target: str | None  # "folder" | "file" | None
    files: list[str] = field(default_factory=list)
    file_count: int = 0
    total_bytes: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "input_type": self.input_type,
            "sample_target": self.sample_target,
            "files": self.files,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "truncated": self.file_count > len(self.files),
            "note": self.note,
        }


def _single_file_group(path: Path) -> dict[str, Any]:
    fmt = EXTENSION_TO_FORMAT.get(path.suffix.lower())
    if fmt is None:
        return _Group(
            format="other",
            input_type=None,
            sample_target=None,
            file_count=1,
            note=f"No reader understands the '{path.suffix}' extension.",
        ).to_dict()
    if fmt == "arrow":
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return _Group(
            format="arrow",
            input_type=None,
            sample_target=None,
            files=[path.name],
            file_count=1,
            total_bytes=size,
            note=(
                "A .arrow file on its own can't be read directly "
                "(tested: fails with \"Couldn't find any data file\"). "
                "Point the reader at this file's containing folder instead."
            ),
        ).to_dict()
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return _Group(
        format=fmt,
        input_type=fmt,
        sample_target="file",
        files=[path.name],
        file_count=1,
        total_bytes=size,
    ).to_dict()


def _walk_all_files(path: Path) -> tuple[list[str], int, int]:
    """Every file's path (relative to `path`, capped at
    `MAX_FILES_LISTED_PER_GROUP`), the true total file count, and total size.

    Used for the `save_to_disk()` case below, where the dataset's own
    files may sit directly in `path` (a single `Dataset`) or one level
    down per split (a `DatasetDict`) -- same bounds (`MAX_WALK_FILES`,
    `MAX_FILES_LISTED_PER_GROUP`) as the format-grouping walk below, so a
    huge or broken dataset folder can't make this hang either.
    """
    names: list[str] = []
    file_count = 0
    total_bytes = 0
    visited = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and not d.startswith(".")]
        for name in files:
            visited += 1
            if visited > MAX_WALK_FILES:
                return names, file_count, total_bytes
            file_count += 1
            file_path = Path(root) / name
            try:
                total_bytes += file_path.stat().st_size
            except OSError:
                pass
            if len(names) < MAX_FILES_LISTED_PER_GROUP:
                names.append(str(file_path.relative_to(path)))
    return names, file_count, total_bytes


def inspect_path(path: Path) -> dict[str, Any]:
    """Describe what's at `path`: one file, a HuggingFace dataset folder,
    or a folder's files grouped by format."""
    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")

    if path.is_file():
        return {"kind": "file", "path": str(path), "groups": [_single_file_group(path)]}

    for marker in HF_MARKER_FILES:
        if (path / marker).is_file():
            names, file_count, total_bytes = _walk_all_files(path)
            group = _Group(
                format="huggingface",
                input_type="huggingface",
                sample_target="folder",
                files=names,
                file_count=file_count,
                total_bytes=total_bytes,
                note=f"Dataset saved with Dataset.save_to_disk() (has {marker}).",
            )
            return {
                "kind": "huggingface_dataset",
                "path": str(path),
                "groups": [group.to_dict()],
            }

    groups: dict[str, _Group] = {}
    other_count = 0
    other_examples: list[str] = []
    files_visited = 0
    truncated = False
    top_level_arrow_files: list[str] = []

    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and not d.startswith(".")]
        for name in files:
            files_visited += 1
            if files_visited > MAX_WALK_FILES:
                truncated = True
                break
            file_path = Path(root) / name
            rel = str(file_path.relative_to(path))
            fmt = EXTENSION_TO_FORMAT.get(file_path.suffix.lower())
            if fmt is None:
                other_count += 1
                if len(other_examples) < 10:
                    other_examples.append(rel)
                continue

            if fmt not in groups:
                sample_target = "file" if fmt in ("csv", "json") else "folder"
                input_type: str | None = fmt
                note = ""
                if fmt == "arrow":
                    input_type = None
                    sample_target = None
                    note = (
                        "A .arrow file on its own can't be read directly "
                        "(tested: fails with \"Couldn't find any data file\"). "
                        "If these sit directly in the inspected folder, use "
                        "the 'huggingface' group above instead, which reads "
                        "the whole folder as one dataset."
                    )
                groups[fmt] = _Group(
                    format=fmt, input_type=input_type, sample_target=sample_target, note=note
                )
            group = groups[fmt]
            group.file_count += 1
            try:
                group.total_bytes += file_path.stat().st_size
            except OSError:
                pass
            if len(group.files) < MAX_FILES_LISTED_PER_GROUP:
                group.files.append(rel)
            if fmt == "arrow" and Path(root) == path:
                top_level_arrow_files.append(rel)
        if truncated:
            break

    ordered = [groups[fmt] for fmt in ("jsonl", "parquet", "json", "csv", "arrow") if fmt in groups]
    result_groups = [group.to_dict() for group in ordered]

    # Loose .arrow files directly inside the inspected folder (not a
    # save_to_disk layout -- that was already handled above) are, as a
    # whole folder, readable via datasets.load_dataset's local-file
    # auto-detection. Tested directly: this is case 09 in the reader
    # investigation, and it passed where a single bare .arrow file failed.
    if top_level_arrow_files:
        top_level_arrow_bytes = sum(
            (path / rel).stat().st_size for rel in top_level_arrow_files if (path / rel).is_file()
        )
        result_groups.insert(
            0,
            _Group(
                format="huggingface",
                input_type="huggingface",
                sample_target="folder",
                files=top_level_arrow_files[:MAX_FILES_LISTED_PER_GROUP],
                file_count=len(top_level_arrow_files),
                total_bytes=top_level_arrow_bytes,
                note=(
                    f"This folder's own {len(top_level_arrow_files)} .arrow "
                    "file(s), read as one dataset via datasets.load_dataset()."
                ),
            ).to_dict(),
        )

    if other_count:
        result_groups.append(
            {
                "format": "other",
                "input_type": None,
                "sample_target": None,
                "files": other_examples,
                "file_count": other_count,
                "total_bytes": 0,
                "truncated": other_count > len(other_examples),
                "note": "No reader understands these extensions yet.",
            }
        )

    return {
        "kind": "directory",
        "path": str(path),
        "groups": result_groups,
        "walk_truncated": truncated,
    }


# -------------------------------------------------------------------------
# Declared schema (free -- from file/dataset metadata, no row reading)
# -------------------------------------------------------------------------


def _arrow_type_to_tree(dtype: Any) -> dict[str, Any]:
    import pyarrow as pa

    if pa.types.is_struct(dtype):
        return {"type": "struct", "fields": {f.name: _arrow_type_to_tree(f.type) for f in dtype}}
    if pa.types.is_list(dtype) or pa.types.is_large_list(dtype):
        return {"type": "list", "item": _arrow_type_to_tree(dtype.value_type)}
    if pa.types.is_map(dtype):
        return {
            "type": "map",
            "key": _arrow_type_to_tree(dtype.key_type),
            "value": _arrow_type_to_tree(dtype.item_type),
        }
    return {"type": str(dtype)}


def _arrow_schema_to_tree(schema: Any) -> dict[str, Any]:
    return {"type": "struct", "fields": {f.name: _arrow_type_to_tree(f.type) for f in schema}}


# -------------------------------------------------------------------------
# Sampling -- each branch mirrors prepare_rows.py's _read_rows()
# -------------------------------------------------------------------------


def _sample_jsonl(
    target: Path, max_rows: int, start_row: int = 0
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    files = sorted(target.rglob("*.jsonl")) if target.is_dir() else [target]
    rows: list[dict[str, Any]] = []
    sampled_from: str | None = None
    # No index into a jsonl file -- reaching row `start_row` means counting
    # (not parsing) every non-blank line before it first. Cheap relative to
    # json.loads(), but still a full scan up to that point; carries across
    # files in the same folder, same as the "stop once max_rows is reached"
    # below already does.
    skipped = 0
    for file_path in files:
        if not file_path.is_file():
            continue
        with file_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                if skipped < start_row:
                    skipped += 1
                    continue
                rows.append(json.loads(line))
                sampled_from = sampled_from or str(file_path)
                if len(rows) >= max_rows:
                    break
        if len(rows) >= max_rows:
            break
    info = {
        "expected_rows": None,
        "expected_rows_from": "unavailable: jsonl has no free row count",
        "files_discovered": len(files),
        "sampled_from_file": sampled_from,
        "declared_schema": None,
    }
    return rows, info


MAX_JSON_BYTES = 50 * 1024 * 1024  # a plain .json file has no streaming/bounded reader


def _sample_json(
    target: Path, max_rows: int, start_row: int = 0
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    size = target.stat().st_size
    if size > MAX_JSON_BYTES:
        raise ValueError(
            f"{target.name} is {size / 1e6:.0f} MB. Refusing to load a plain "
            ".json file this large for a preview -- there's no streaming "
            "reader for this format, so the real conversion would load the "
            "whole thing into memory too."
        )
    with target.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    all_rows = data if isinstance(data, list) else [data]
    info = {
        "expected_rows": len(all_rows),
        "expected_rows_from": "full file read (the whole small .json file was loaded)",
        "files_discovered": 1,
        "sampled_from_file": str(target),
        "declared_schema": None,
    }
    # The whole file is already a plain list in memory -- start_row is just
    # a slice offset, not a second read.
    return all_rows[start_row : start_row + max_rows], info


def _sample_csv(
    target: Path, max_rows: int, start_row: int = 0
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # Row 0 (the header) is always kept; skiprows=range(1, start_row + 1)
    # skips exactly the next start_row data rows, same as prepare_rows.py's
    # own pandas-based csv reader would scan through to reach that point --
    # there's no index into a csv any more than there is into jsonl.
    skiprows = range(1, start_row + 1) if start_row > 0 else None
    frame = pd.read_csv(target, skiprows=skiprows, nrows=max_rows)
    info = {
        "expected_rows": None,
        "expected_rows_from": "unavailable: csv has no free row count",
        "files_discovered": 1,
        "sampled_from_file": str(target),
        "declared_schema": None,
    }
    return frame.to_dict("records"), info


def _sample_parquet(
    target: Path, max_rows: int, start_row: int = 0
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    import pyarrow.parquet as pq

    files = sorted(target.rglob("*.parquet")) if target.is_dir() else [target]

    expected_rows: int | None = None
    expected_rows_from = "unavailable: parquet footer read failed"
    try:
        expected_rows = sum(pq.ParquetFile(f).metadata.num_rows for f in files)
        expected_rows_from = "parquet file footers"
    except Exception as exc:  # best-effort, matches prepare_rows.py's own try/except
        expected_rows_from = f"unavailable: parquet footer read failed ({exc})"

    declared_schema = None
    if files:
        try:
            declared_schema = _arrow_schema_to_tree(pq.ParquetFile(files[0]).schema_arrow)
        except Exception:
            declared_schema = None

    rows: list[dict[str, Any]] = []
    sampled_from: str | None = None
    # Unlike jsonl/csv, a parquet file's own metadata already says how many
    # rows are in each row group -- a group entirely before start_row can be
    # skipped without reading any of its actual data. Only the one group
    # start_row falls inside (and whichever follow, up to max_rows) is
    # actually read, via read_row_group() rather than the sequential
    # iter_batches() the old (always-from-the-start) version used.
    remaining_to_skip = start_row
    for file_path in files:
        try:
            parquet_file = pq.ParquetFile(file_path)
        except Exception:
            continue
        for group_index in range(parquet_file.metadata.num_row_groups):
            group_rows = parquet_file.metadata.row_group(group_index).num_rows
            if remaining_to_skip >= group_rows:
                remaining_to_skip -= group_rows
                continue
            batch_rows = parquet_file.read_row_group(group_index).to_pandas().to_dict("records")
            if remaining_to_skip > 0:
                batch_rows = batch_rows[remaining_to_skip:]
                remaining_to_skip = 0
            rows.extend(batch_rows)
            sampled_from = sampled_from or str(file_path)
            if len(rows) >= max_rows:
                break
        if len(rows) >= max_rows:
            break

    info = {
        "expected_rows": expected_rows,
        "expected_rows_from": expected_rows_from,
        "files_discovered": len(files),
        "sampled_from_file": sampled_from,
        "declared_schema": declared_schema,
    }
    return rows[:max_rows], info


def _hf_declared_schema(dataset: Any) -> dict[str, Any] | None:
    try:
        return _arrow_schema_to_tree(dataset.features.arrow_schema)
    except Exception:
        return None


def _sample_huggingface(
    target: Path, max_rows: int, split: str, start_row: int = 0
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from datasets import load_dataset, load_from_disk

    if (target / "state.json").is_file():
        dataset = load_from_disk(str(target))
        try:
            expected_rows = arrow_ipc_row_count(target)
            expected_rows_from = "arrow ipc row count"
        except Exception as exc:
            expected_rows = dataset.num_rows
            expected_rows_from = f"huggingface dataset num_rows (arrow ipc read failed: {exc})"
    elif (target / "dataset_dict.json").is_file():
        dataset_dict = load_from_disk(str(target))
        if split not in dataset_dict:
            raise ValueError(f"Split '{split}' not found; available: {sorted(dataset_dict.keys())}")
        dataset = dataset_dict[split]
        try:
            expected_rows = arrow_ipc_row_count(target / split)
            expected_rows_from = "arrow ipc row count"
        except Exception as exc:
            expected_rows = dataset.num_rows
            expected_rows_from = f"huggingface dataset num_rows (arrow ipc read failed: {exc})"
    else:
        # A folder of raw data files (e.g. loose .arrow files) rather than a
        # save_to_disk layout -- same call prepare_rows.py's own "else"
        # branch makes.
        dataset = load_dataset(str(target), split=split)
        expected_rows = dataset.num_rows
        expected_rows_from = "huggingface dataset num_rows"

    # Arrow-backed, so dataset[i] is already O(1) random access -- jumping
    # to an arbitrary start_row costs nothing extra here, unlike every
    # other format above.
    end = min(start_row + max_rows, len(dataset))
    rows = [dataset[i] for i in range(start_row, end)]
    info = {
        "expected_rows": expected_rows,
        "expected_rows_from": expected_rows_from,
        "files_discovered": None,
        "sampled_from_file": str(target),
        "declared_schema": _hf_declared_schema(dataset),
    }
    return rows, info


def sample_rows(
    path: Path,
    input_type: str,
    file: str | None,
    max_rows: int,
    split: str = "train",
    start_row: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read up to `max_rows` rows starting at `start_row`, the same way
    prepare_rows.py's `_read_rows()` would for this `input_type` (just
    windowed instead of always starting at the top), then apply the same
    `_clean_value()` cleanup it applies before returning -- so a numpy
    array or a NaN in the sample looks exactly like it would to the real
    converter.

    `file`, when given, is a path relative to `path` (one entry from an
    `inspect_path()` group's `files` list) -- required for csv/json
    (the real reader only ever takes one file for those), optional for
    jsonl/parquet/huggingface (which read the whole folder; `file` just
    narrows the sample to one file within it).

    `start_row` has no index to use for jsonl/csv -- reaching it means
    scanning (not fully parsing) every row before it, same cost the real
    reader would already pay to reach that point. parquet can skip whole
    row groups via metadata, and a huggingface dataset is O(1) random
    access -- see each `_sample_*` function's own comment.
    """
    if path.is_file():
        target = path
    elif file:
        target = path / file
        if not target.is_file():
            raise FileNotFoundError(f"Not a file under {path}: {file}")
    elif input_type in ("csv", "json"):
        raise ValueError(f"'{input_type}' needs a specific file (pass 'file').")
    else:
        target = path

    if input_type == "jsonl":
        rows, info = _sample_jsonl(target, max_rows, start_row)
    elif input_type == "json":
        rows, info = _sample_json(target, max_rows, start_row)
    elif input_type == "csv":
        rows, info = _sample_csv(target, max_rows, start_row)
    elif input_type == "parquet":
        rows, info = _sample_parquet(target, max_rows, start_row)
    elif input_type == "huggingface":
        rows, info = _sample_huggingface(target, max_rows, split, start_row)
    else:
        raise ValueError(f"Unsupported input type: {input_type}")

    rows = [clean_value(row) for row in rows]
    return rows, info
