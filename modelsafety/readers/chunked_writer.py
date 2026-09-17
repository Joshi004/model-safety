"""Shared chunked-output writer — used by all data preparation scripts.

Writes records into JSONL files distributed round-robin across chunk
directories, matching the layout expected by ``run_with_slurm.sh``:

    output_dir/
        chunk-0/  data_000000.jsonl
        chunk-1/  data_000001.jsonl
        ...
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger(__name__)


def write_chunked_output(
    records: List[Dict[str, Any]],
    output_dir: str,
    num_chunks: int,
    rows_per_file: int,
    dir_prefix: str = "chunk-",
    file_prefix: str = "data",
) -> None:
    """Write records into JSONL files distributed round-robin across chunks.

    Parameters
    ----------
    records
        List of dicts to write.
    output_dir
        Parent directory; chunk sub-directories are created inside.
    num_chunks
        Number of chunk directories.
    rows_per_file
        Maximum rows per JSONL file.
    dir_prefix
        Prefix for chunk directory names (default: ``chunk-``).
    file_prefix
        Prefix for JSONL filenames (default: ``data``).
    """
    if not records:
        log.info("No records to write")
        return

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    chunk_dirs = []
    for i in range(num_chunks):
        d = output_path / f"{dir_prefix}{i}"
        d.mkdir(parents=True, exist_ok=True)
        chunk_dirs.append(d)

    # Split into files of <= rows_per_file
    file_batches: List[List[Dict]] = []
    for start in range(0, len(records), rows_per_file):
        file_batches.append(records[start : start + rows_per_file])

    files_written = 0
    for file_idx, batch in enumerate(file_batches):
        chunk_idx = file_idx % num_chunks
        filename = f"{file_prefix}_{file_idx:06d}.jsonl"
        filepath = chunk_dirs[chunk_idx] / filename
        with open(filepath, "w", encoding="utf-8") as f:
            for rec in batch:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        files_written += 1

    # Summary
    for i, d in enumerate(chunk_dirs):
        jsonl_files = list(d.glob("*.jsonl"))
        total_rows = sum(sum(1 for _ in open(jf)) for jf in jsonl_files)
        log.info("  %s%d: %d files, %d rows", dir_prefix, i,
                 len(jsonl_files), total_rows)

    log.info("  total files: %d, total records: %d", files_written, len(records))
