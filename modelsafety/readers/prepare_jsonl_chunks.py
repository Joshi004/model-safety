#!/usr/bin/env python3
"""Split a flat folder of JSONL files into Genesis chunk directories."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
from pathlib import Path
from typing import Iterable

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def collect_input_files(input_dir: Path, pattern: str) -> list[Path]:
    files = sorted(path for path in input_dir.glob(pattern) if path.is_file())
    if not files:
        raise SystemExit(f"No files matching {pattern!r} found in {input_dir}")
    return files


def count_jsonl_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def validate_jsonl(paths: Iterable[Path]) -> None:
    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SystemExit(
                        f"Invalid JSON in {path}:{line_number}: {exc}"
                    ) from exc


def copy_into_chunks(
    files: list[Path],
    output_dir: Path,
    num_chunks: int,
    dir_prefix: str,
    existing: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    chunk_dirs = [output_dir / f"{dir_prefix}{idx}" for idx in range(num_chunks)]
    for chunk_dir in chunk_dirs:
        chunk_dir.mkdir(parents=True, exist_ok=True)

    chunk_file_counts = [0] * num_chunks
    chunk_row_counts = [0] * num_chunks
    copied = 0
    skipped = 0

    for file_idx, source in enumerate(files):
        chunk_idx = file_idx % num_chunks
        destination = chunk_dirs[chunk_idx] / source.name

        if destination.exists():
            if existing == "fail":
                raise SystemExit(
                    f"Destination already exists: {destination}. "
                    "Use --existing skip or --existing overwrite."
                )
            if existing == "skip":
                log.info("SKIP existing: %s", destination)
                skipped += 1
                continue
            destination.unlink()

        shutil.copy2(source, destination)
        copied += 1
        chunk_file_counts[chunk_idx] += 1
        chunk_row_counts[chunk_idx] += count_jsonl_rows(destination)

    log.info("")
    log.info("Input:  %s", files[0].parent)
    log.info("Output: %s", output_dir)
    for idx, chunk_dir in enumerate(chunk_dirs):
        total_files = len(list(chunk_dir.glob("*.jsonl")))
        total_rows = sum(count_jsonl_rows(path) for path in chunk_dir.glob("*.jsonl"))
        log.info(
            "  %s%d/  %d new files, %d new rows (%d total files, %d total rows)",
            dir_prefix,
            idx,
            chunk_file_counts[idx],
            chunk_row_counts[idx],
            total_files,
            total_rows,
        )
    log.info("")
    log.info(
        "Done: %d copied, %d skipped, %d source files, %d chunks",
        copied,
        skipped,
        len(files),
        num_chunks,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split a flat folder of JSONL files into Genesis chunk directories.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input-dir", required=True, help="Folder with flat JSONL files.")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Parent folder for chunk directories. Defaults to --input-dir.",
    )
    parser.add_argument(
        "--num-chunks",
        type=positive_int,
        required=True,
        help="Number of chunk directories to create. Match this with Slurm array size.",
    )
    parser.add_argument("--dir-prefix", default="chunk-", help="Chunk directory prefix.")
    parser.add_argument("--pattern", default="*.jsonl", help="Input file glob pattern.")
    parser.add_argument(
        "--existing",
        choices=("fail", "skip", "overwrite"),
        default="fail",
        help="What to do when a destination chunk file already exists.",
    )
    parser.add_argument(
        "--validate-jsonl",
        action="store_true",
        help="Parse input files before copying and fail on invalid JSONL.",
    )

    args = parser.parse_args()
    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir) if args.output_dir else input_dir

    if not input_dir.is_dir():
        raise SystemExit(f"Input directory not found: {input_dir}")

    files = collect_input_files(input_dir, args.pattern)
    if args.validate_jsonl:
        validate_jsonl(files)

    copy_into_chunks(
        files=files,
        output_dir=output_dir,
        num_chunks=args.num_chunks,
        dir_prefix=args.dir_prefix,
        existing=args.existing,
    )


if __name__ == "__main__":
    main()
