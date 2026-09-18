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
from pathlib import Path
from typing import Any, Dict, Iterable, List

import numpy as np
import pandas as pd

from chunked_writer import write_chunked_output


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


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    paths = sorted(path.rglob("*.jsonl")) if path.is_dir() else [path]
    for file_path in paths:
        with file_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def _read_rows(args: argparse.Namespace) -> List[Dict[str, Any]]:
    input_path = Path(args.input_path)

    if args.input_type == "jsonl":
        rows = list(_iter_jsonl(input_path))
    elif args.input_type == "json":
        with input_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        rows = data if isinstance(data, list) else [data]
    elif args.input_type == "csv":
        rows = pd.read_csv(input_path).to_dict("records")
    elif args.input_type == "parquet":
        rows = pd.read_parquet(input_path).to_dict("records")
    elif args.input_type == "huggingface":
        if (input_path / "state.json").exists():
            # A single Dataset saved with Dataset.save_to_disk() -- load_dataset()
            # refuses these (it checks for this exact file and tells you to use
            # load_from_disk instead), so go straight to the function that works.
            from datasets import load_from_disk

            rows = list(load_from_disk(str(input_path)))
        elif (input_path / "dataset_dict.json").exists():
            # A DatasetDict saved with DatasetDict.save_to_disk() -- same idea,
            # but we also need to pick the requested split out of the dict.
            from datasets import load_from_disk

            ds_dict = load_from_disk(str(input_path))
            if args.split not in ds_dict:
                raise ValueError(
                    f"Split '{args.split}' not found; available splits: {sorted(ds_dict.keys())}"
                )
            rows = list(ds_dict[args.split])
        else:
            # Not a save_to_disk folder -- a Hugging Face Hub dataset ID, or a
            # local folder of raw data files a builder can auto-detect.
            from datasets import load_dataset

            kwargs: Dict[str, Any] = {"split": args.split}
            if args.hf_config:
                kwargs["name"] = args.hf_config
            rows = list(load_dataset(args.input_path, **kwargs))
    else:
        raise ValueError(f"Unsupported input type: {args.input_type}")

    if args.max_rows is not None:
        rows = rows[: args.max_rows]
    return [_clean_value(row) for row in rows]


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
) -> Dict[str, Any] | None:
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
            return None

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
    return prepared


def prepare_rows(args: argparse.Namespace) -> List[Dict[str, Any]]:
    mapping = _parse_mapping(args.map or [])
    rows = _read_rows(args)
    if args.shuffle:
        random.seed(args.seed)
        random.shuffle(rows)

    prepared_rows: List[Dict[str, Any]] = []
    skipped = 0
    exclude_metadata = set(args.exclude_metadata or [])

    for row in rows:
        prepared = _prepare_row(
            row=row,
            mapping=mapping,
            required_columns=args.require or [],
            metadata_column=args.metadata_column,
            exclude_metadata=exclude_metadata,
        )
        if prepared is None:
            skipped += 1
            continue
        prepared_rows.append(prepared)

    print(f"Prepared {len(prepared_rows)} rows; skipped {skipped} rows.")
    return prepared_rows


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


def main() -> None:
    args = build_parser().parse_args()
    rows = prepare_rows(args)
    write_chunked_output(
        rows,
        output_dir=args.output_dir,
        num_chunks=args.num_chunks,
        rows_per_file=args.rows_per_file,
        dir_prefix=args.dir_prefix,
        file_prefix=args.file_prefix,
    )


if __name__ == "__main__":
    main()
