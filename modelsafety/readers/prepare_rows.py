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
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

# This script is invoked directly (see the docs), so only its own directory
# lands on sys.path -- that's what makes the next import work at all. Add
# the repo root too, so the text-coverage check below can import the PII
# scanner's own extractor no matter how the caller sets PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chunked_writer import write_chunked_output  # noqa: E402
from modelsafety.contract.fingerprints import file_sha256  # noqa: E402
from modelsafety.contract.run_identity import (  # noqa: E402
    RunIdentityError,
    check_run_identity,
    describe_input_files,
    make_identity,
)
from modelsafety.contract.textract import extract_record_text  # noqa: E402


SUPPORTED_INPUT_TYPES = ("jsonl", "json", "csv", "parquet", "huggingface")


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


def _read_jsonl_file(file_path: Path) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Read one JSONL file: its rows, plus its fingerprint and line counts.

    The file is read as bytes so every line is counted and fingerprinted
    exactly as it sits on disk. Any line that can't be read stops the run
    with ``<path>:<line>:`` at the front of the message.
    """
    resolved = file_path.resolve()
    digest = hashlib.sha256()
    rows: List[Dict[str, Any]] = []
    lines = 0
    blank_lines = 0

    with file_path.open("rb") as handle:
        for raw in handle:
            digest.update(raw)
            lines += 1
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
            rows.append(row)

    entry = _source_file_entry(file_path, digest.hexdigest())
    entry["lines"] = lines
    entry["blank_lines"] = blank_lines
    entry["rows_read"] = len(rows)
    return rows, entry


def _iter_jsonl_files(paths: Iterable[Path]) -> Iterable[Dict[str, Any]]:
    for file_path in paths:
        yield from _read_jsonl_file(file_path)[0]


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    return _iter_jsonl_files(_discover_files(path, "*.jsonl"))


def _arrow_ipc_row_count(dataset_dir: Path) -> int:
    """Independently count rows in a ``Dataset.save_to_disk()`` folder.

    Reads the exact ``.arrow`` file(s) ``load_from_disk()`` would use --
    listed in the folder's own ``state.json`` -- through ``pyarrow.ipc``
    instead of the ``datasets`` library. This is a second, independent
    reader of the same data, the same role the parquet footer check above
    plays for parquet input: it can catch a problem in how ``datasets``
    loaded the rows, which comparing a count against itself cannot.
    """
    import pyarrow as pa

    state = json.loads((dataset_dir / "state.json").read_text(encoding="utf-8"))
    total = 0
    for entry in state["_data_files"]:
        file_path = dataset_dir / entry["filename"]
        with pa.memory_map(str(file_path), "rb") as source:
            try:
                reader = pa.ipc.open_stream(source)
            except pa.ArrowInvalid:
                source.seek(0)
                reader = pa.ipc.open_file(source)
            total += reader.read_all().num_rows
    return total


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


def _read_rows(
    args: argparse.Namespace, input_files: List[Path]
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Read source rows, plus whatever row count the source gives us for free.

    ``input_files`` is what ``_discover_input_files`` found.

    Returns ``(rows, source_info)``. ``source_info["expected_rows"]`` is an
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

    if args.input_type != "jsonl":
        # JSONL files are fingerprinted while they are read, below.
        source_files = [_source_file_entry(path, file_sha256(path)) for path in input_files]

    if args.input_type == "jsonl":
        files_discovered = len(input_files)
        rows = []
        for file_path in input_files:
            file_rows, file_entry = _read_jsonl_file(file_path)
            rows.extend(file_rows)
            source_files.append(file_entry)
        expected_rows_from = "unavailable: jsonl has no free row count"
    elif args.input_type == "json":
        with input_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        rows = data if isinstance(data, list) else [data]
        expected_rows_from = "unavailable: json has no free row count"
    elif args.input_type == "csv":
        rows = pd.read_csv(input_path).to_dict("records")
        expected_rows_from = "unavailable: csv has no free row count"
    elif args.input_type == "parquet":
        files_discovered = len(input_files)
        try:
            import pyarrow.parquet as pq

            expected_rows = sum(pq.ParquetFile(f).metadata.num_rows for f in input_files)
            expected_rows_from = "parquet file footers"
        except Exception as exc:  # best-effort -- never block a real conversion on this
            expected_rows_from = f"unavailable: parquet footer read failed ({exc})"
        rows = pd.read_parquet(input_path).to_dict("records")
    elif args.input_type == "huggingface":
        if (input_path / "state.json").exists():
            # A single Dataset saved with Dataset.save_to_disk() -- load_dataset()
            # refuses these (it checks for this exact file and tells you to use
            # load_from_disk instead), so go straight to the function that works.
            from datasets import load_from_disk

            dataset = load_from_disk(str(input_path))
            rows = list(dataset)
            try:
                expected_rows = _arrow_ipc_row_count(input_path)
                expected_rows_from = "arrow ipc row count"
            except Exception as exc:  # best-effort -- never block a real conversion on this
                expected_rows = dataset.num_rows
                expected_rows_from = (
                    f"huggingface dataset num_rows (arrow ipc read failed: {exc})"
                )
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
            rows = list(dataset)
            try:
                # Each split of a saved DatasetDict is itself a save_to_disk
                # folder, at <root>/<split>/, with its own state.json.
                expected_rows = _arrow_ipc_row_count(input_path / args.split)
                expected_rows_from = "arrow ipc row count"
            except Exception as exc:  # best-effort -- never block a real conversion on this
                expected_rows = dataset.num_rows
                expected_rows_from = (
                    f"huggingface dataset num_rows (arrow ipc read failed: {exc})"
                )
        else:
            # Not a save_to_disk folder -- a Hugging Face Hub dataset ID, or a
            # local folder of raw data files a builder can auto-detect.
            from datasets import load_dataset

            kwargs: Dict[str, Any] = {"split": args.split}
            if args.hf_config:
                kwargs["name"] = args.hf_config
            dataset = load_dataset(args.input_path, **kwargs)
            expected_rows = dataset.num_rows
            expected_rows_from = "huggingface dataset num_rows"
            rows = list(dataset)
    else:
        raise ValueError(f"Unsupported input type: {args.input_type}")

    if args.max_rows is not None:
        rows = rows[: args.max_rows]
    rows = [_clean_value(row) for row in rows]

    source_info = {
        "rows_read": len(rows),
        "expected_rows": expected_rows,
        "expected_rows_from": expected_rows_from,
        "files_discovered": files_discovered,
        "files": source_files,
    }
    return rows, source_info


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


def prepare_rows(
    args: argparse.Namespace,
    input_files: List[Path],
) -> tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    """Return ``(prepared_rows, source_info, rows_info)``.

    ``input_files`` is what ``_discover_input_files`` found.

    ``source_info`` comes straight from ``_read_rows`` (option 1's free
    metadata count, see ``DATA_VALIDATION_OPTIONS.md``). ``rows_info`` adds
    the write-side counts: how many rows were kept, how many were skipped,
    and -- unlike the original bare total -- which required column caused
    each skip.
    """
    mapping = _parse_mapping(args.map or [])
    rows, source_info = _read_rows(args, input_files)
    if args.shuffle:
        random.seed(args.seed)
        random.shuffle(rows)

    prepared_rows: List[Dict[str, Any]] = []
    skipped_by_required_column: Dict[str, int] = {}
    exclude_metadata = set(args.exclude_metadata or [])

    for row in rows:
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
            continue
        prepared_rows.append(prepared)

    skipped = sum(skipped_by_required_column.values())
    print(f"Prepared {len(prepared_rows)} rows; skipped {skipped} rows.")

    rows_info = {
        "written": len(prepared_rows),
        "skipped": skipped,
        "skipped_by_required_column": skipped_by_required_column,
    }
    return prepared_rows, source_info, rows_info


def _text_coverage(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Coverage of the scanner's own text extractor over the prepared rows.

    Calls ``extract_record_text()`` -- the exact function the PII scanner
    itself calls on every row (see ``modelsafety/contract/textract.py``) --
    so this cannot disagree with what a later scan will actually see. This
    is option 4 in ``DATA_VALIDATION_OPTIONS.md``.
    """
    if not rows:
        return {
            "rows_with_text": 0,
            "coverage": 0.0,
            "total_characters": 0,
            "mean_characters": 0.0,
        }

    lengths = [len(extract_record_text(row)) for row in rows]
    rows_with_text = sum(1 for length in lengths if length > 0)
    total_characters = sum(lengths)
    return {
        "rows_with_text": rows_with_text,
        "coverage": rows_with_text / len(rows),
        "total_characters": total_characters,
        "mean_characters": total_characters / len(rows),
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
    args = build_parser().parse_args()
    output_dir = Path(args.output_dir)

    # Before anything is touched: refuse an output folder that holds a
    # different run, so old and new files never end up side by side.
    input_files = _discover_input_files(args)
    try:
        check_run_identity(output_dir, _run_identity(args, input_files))
    except RunIdentityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)

    # Option 3: a stale marker from a previous, possibly-failed run must not
    # be mistaken for this run having succeeded.
    success_marker = output_dir / "_SUCCESS"
    if success_marker.exists():
        success_marker.unlink()

    prepared_rows, source_info, rows_info = prepare_rows(args, input_files)
    coverage_info = _text_coverage(prepared_rows)
    output_info = write_chunked_output(
        prepared_rows,
        output_dir=args.output_dir,
        num_chunks=args.num_chunks,
        rows_per_file=args.rows_per_file,
        dir_prefix=args.dir_prefix,
        file_prefix=args.file_prefix,
    )

    print(
        f"Text coverage: {coverage_info['rows_with_text']}/{rows_info['written']} rows "
        f"({coverage_info['coverage'] * 100:.1f}%) have scannable text."
    )

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
