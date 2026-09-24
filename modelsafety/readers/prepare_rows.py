#!/usr/bin/env python3
"""Prepare arbitrary rows for MedPsy Synthetic Data Generator pipelines.

The output contract is intentionally small: map the columns required by a
pipeline to their logical names and pack every remaining source field into a
single metadata column.
"""

from __future__ import annotations

import argparse
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


def _iter_jsonl_files(paths: Iterable[Path]) -> Iterable[Dict[str, Any]]:
    for file_path in paths:
        with file_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


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


def _read_rows(args: argparse.Namespace) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Read source rows, plus whatever row count the source gives us for free.

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

    if args.input_type == "jsonl":
        jsonl_files = _discover_files(input_path, "*.jsonl")
        files_discovered = len(jsonl_files)
        rows = list(_iter_jsonl_files(jsonl_files))
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
        parquet_files = _discover_files(input_path, "*.parquet")
        files_discovered = len(parquet_files)
        try:
            import pyarrow.parquet as pq

            expected_rows = sum(pq.ParquetFile(f).metadata.num_rows for f in parquet_files)
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
) -> tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    """Return ``(prepared_rows, source_info, rows_info)``.

    ``source_info`` comes straight from ``_read_rows`` (option 1's free
    metadata count, see ``DATA_VALIDATION_OPTIONS.md``). ``rows_info`` adds
    the write-side counts: how many rows were kept, how many were skipped,
    and -- unlike the original bare total -- which required column caused
    each skip.
    """
    mapping = _parse_mapping(args.map or [])
    rows, source_info = _read_rows(args)
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

    # Option 3: a stale marker from a previous, possibly-failed run must not
    # be mistaken for this run having succeeded.
    output_dir = Path(args.output_dir)
    success_marker = output_dir / "_SUCCESS"
    if success_marker.exists():
        success_marker.unlink()

    prepared_rows, source_info, rows_info = prepare_rows(args)
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
