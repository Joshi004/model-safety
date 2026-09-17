#!/usr/bin/env python3
"""Split large JSONL inputs into stable Genesis Slurm chunks.

This script is for already pre-processed JSONL inputs where individual files are
too large for convenient pipeline runs. It uses GNU ``split`` to write smaller
JSONL shards into the layout expected by ``run_pipeline.sh``:

    output_dir/
        chunk-0/*.jsonl
        chunk-1/*.jsonl
        ...

Rows are never materialised in memory. Source files are processed in parallel
with ``--num-proc``; each worker runs one ``split`` subprocess at a time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

MANIFEST_NAME = "_chunk_manifest.json"
SPLIT_SUFFIX_LENGTH = 8


@dataclass(frozen=True)
class SourceSpec:
    index: int
    path: str
    rel_path: str
    size_bytes: int
    mtime_ns: int


@dataclass
class SourceResult:
    source: str
    rows: int
    files_written: int
    files_skipped: int
    chunk_rows: dict[str, int]


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def collect_sources(input_dir: Path, pattern: str) -> list[SourceSpec]:
    paths = sorted(path for path in input_dir.glob(pattern) if path.is_file())
    if not paths:
        raise SystemExit(f"No files matching {pattern!r} found in {input_dir}")

    specs = []
    for index, path in enumerate(paths):
        stat = path.stat()
        specs.append(
            SourceSpec(
                index=index,
                path=str(path),
                rel_path=str(path.relative_to(input_dir)),
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        )
    return specs


def write_manifest(output_dir: Path, manifest: dict[str, Any]) -> None:
    tmp_path = output_dir / f".{MANIFEST_NAME}.tmp"
    final_path = output_dir / MANIFEST_NAME
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp_path, final_path)


def load_manifest(output_dir: Path) -> dict[str, Any] | None:
    manifest_path = output_dir / MANIFEST_NAME
    if not manifest_path.exists():
        return None
    with manifest_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def manifest_fingerprint(settings: dict[str, Any], sources: list[SourceSpec]) -> str:
    payload = {
        "settings": settings,
        "sources": [asdict(source) for source in sources],
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def check_existing_manifest(
    output_dir: Path,
    settings: dict[str, Any],
    sources: list[SourceSpec],
    existing: str,
) -> None:
    manifest = load_manifest(output_dir)
    if not manifest:
        return

    expected = manifest_fingerprint(settings, sources)
    actual = manifest.get("fingerprint")
    if actual == expected:
        log.info("Existing manifest matches requested chunking settings")
        return

    if existing == "overwrite":
        log.warning("Existing manifest differs; --existing overwrite allows regeneration")
        return

    raise SystemExit(
        f"Existing manifest at {output_dir / MANIFEST_NAME} was created with "
        "different sources or settings. Use --existing overwrite to regenerate "
        "or choose a new --output-dir."
    )


def ensure_chunk_dirs(output_dir: Path, num_chunks: int, dir_prefix: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for idx in range(num_chunks):
        (output_dir / f"{dir_prefix}{idx}").mkdir(parents=True, exist_ok=True)


def output_path_for_part(
    output_dir: Path,
    dir_prefix: str,
    num_chunks: int,
    source: SourceSpec,
    part_idx: int,
) -> Path:
    chunk_idx = part_idx % num_chunks
    chunk_dir = output_dir / f"{dir_prefix}{chunk_idx}"
    safe_stem = Path(source.rel_path).with_suffix("").as_posix().replace("/", "__")
    return chunk_dir / f"{safe_stem}_{source.index:06d}_part{part_idx:06d}.jsonl"


def count_lines(path: Path) -> int:
    rows = 0
    last_chunk = b""
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            rows += chunk.count(b"\n")
            last_chunk = chunk

    if last_chunk and not last_chunk.endswith(b"\n"):
        rows += 1
    return rows


def process_source(
    source: SourceSpec,
    output_dir: str,
    num_chunks: int,
    rows_per_file: int,
    dir_prefix: str,
    existing: str,
) -> SourceResult:
    output_path = Path(output_dir)
    chunk_rows = {str(idx): 0 for idx in range(num_chunks)}
    files_written = 0
    files_skipped = 0
    rows = count_lines(Path(source.path))

    tmp_dir = output_path / f".split_tmp_{source.index:06d}_{os.getpid()}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    try:
        if rows > 0:
            subprocess.run(
                [
                    "split",
                    "-l",
                    str(rows_per_file),
                    "--numeric-suffixes=0",
                    f"--suffix-length={SPLIT_SUFFIX_LENGTH}",
                    "--additional-suffix=.jsonl",
                    source.path,
                    str(tmp_dir / "part_"),
                ],
                check=True,
            )

        parts = sorted(tmp_dir.glob("part_*.jsonl"))
        for part_idx, part_path in enumerate(parts):
            if part_idx == len(parts) - 1:
                part_rows = rows - (rows_per_file * part_idx)
            else:
                part_rows = rows_per_file

            chunk_rows[str(part_idx % num_chunks)] += part_rows
            final_path = output_path_for_part(
                output_path,
                dir_prefix,
                num_chunks,
                source,
                part_idx,
            )

            if final_path.exists():
                if existing == "fail":
                    raise FileExistsError(
                        f"Destination already exists: {final_path}. "
                        "Use --existing skip or --existing overwrite."
                    )
                if existing == "skip":
                    files_skipped += 1
                    part_path.unlink()
                    continue
                final_path.unlink()

            tmp_path = final_path.with_name(f".{final_path.name}.tmp")
            if tmp_path.exists():
                tmp_path.unlink()
            os.replace(part_path, tmp_path)
            os.replace(tmp_path, final_path)
            files_written += 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return SourceResult(
        source=source.rel_path,
        rows=rows,
        files_written=files_written,
        files_skipped=files_skipped,
        chunk_rows=chunk_rows,
    )


def merge_results(results: Iterable[SourceResult], num_chunks: int) -> dict[str, Any]:
    total_rows = 0
    total_files_written = 0
    total_files_skipped = 0
    chunk_rows = {str(idx): 0 for idx in range(num_chunks)}
    sources = []

    for result in sorted(results, key=lambda item: item.source):
        total_rows += result.rows
        total_files_written += result.files_written
        total_files_skipped += result.files_skipped
        sources.append(asdict(result))
        for chunk_idx, count in result.chunk_rows.items():
            chunk_rows[chunk_idx] += count

    return {
        "total_rows": total_rows,
        "total_files_written": total_files_written,
        "total_files_skipped": total_files_skipped,
        "chunk_rows": chunk_rows,
        "sources": sources,
    }


def main() -> None:
    cpu_count = os.cpu_count() or 1
    parser = argparse.ArgumentParser(
        description="Stream large JSONL files into stable Genesis Slurm chunks.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input-dir", required=True, help="Folder with source JSONL files.")
    parser.add_argument("--output-dir", required=True, help="Folder for chunk-N directories.")
    parser.add_argument("--num-chunks", type=positive_int, required=True)
    parser.add_argument("--rows-per-file", type=positive_int, required=True)
    parser.add_argument("--dir-prefix", default="chunk-")
    parser.add_argument("--pattern", default="*.jsonl")
    parser.add_argument(
        "--existing",
        choices=("fail", "skip", "overwrite"),
        default="fail",
        help="How to handle existing output shard files.",
    )
    parser.add_argument(
        "--num-proc",
        type=positive_int,
        default=min(cpu_count, 8),
        help="Concurrent GNU split subprocesses. Each worker processes one source file.",
    )

    args = parser.parse_args()
    input_dir = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve()

    if not input_dir.is_dir():
        raise SystemExit(f"Input directory not found: {input_dir}")

    if shutil.which("split") is None:
        raise SystemExit("GNU split not found on PATH")

    sources = collect_sources(input_dir, args.pattern)
    settings = {
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "pattern": args.pattern,
        "num_chunks": args.num_chunks,
        "rows_per_file": args.rows_per_file,
        "dir_prefix": args.dir_prefix,
    }

    ensure_chunk_dirs(output_dir, args.num_chunks, args.dir_prefix)
    check_existing_manifest(output_dir, settings, sources, args.existing)

    started = time.time()
    max_workers = min(args.num_proc, len(sources))
    log.info(
        "Preparing %d JSONL source file(s) into %d chunks, %d rows/file, %d split worker(s)",
        len(sources),
        args.num_chunks,
        args.rows_per_file,
        max_workers,
    )

    results: list[SourceResult] = []
    if max_workers <= 1:
        for source in sources:
            results.append(
                process_source(
                    source,
                    str(output_dir),
                    args.num_chunks,
                    args.rows_per_file,
                    args.dir_prefix,
                    args.existing,
                )
            )
    else:
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    process_source,
                    source,
                    str(output_dir),
                    args.num_chunks,
                    args.rows_per_file,
                    args.dir_prefix,
                    args.existing,
                )
                for source in sources
            ]
            for future in as_completed(futures):
                result = future.result()
                log.info(
                    "Processed %s: %d rows, %d written, %d skipped",
                    result.source,
                    result.rows,
                    result.files_written,
                    result.files_skipped,
                )
                results.append(result)

    summary = merge_results(results, args.num_chunks)
    manifest = {
        "version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": " ".join(sys.argv),
        "settings": settings,
        "fingerprint": manifest_fingerprint(settings, sources),
        "source_specs": [asdict(source) for source in sources],
        "summary": summary,
    }
    write_manifest(output_dir, manifest)

    elapsed = time.time() - started
    log.info("Output: %s", output_dir)
    for idx in range(args.num_chunks):
        log.info(
            "  %s%d: %d rows",
            args.dir_prefix,
            idx,
            summary["chunk_rows"][str(idx)],
        )
    log.info(
        "Done: %d rows, %d files written, %d files skipped in %.1fs",
        summary["total_rows"],
        summary["total_files_written"],
        summary["total_files_skipped"],
        elapsed,
    )
    log.info("Manifest: %s", output_dir / MANIFEST_NAME)


if __name__ == "__main__":
    main()
