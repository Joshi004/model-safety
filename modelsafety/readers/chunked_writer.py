"""Shared chunked-output writer — used by all data preparation scripts.

Writes records into JSONL files distributed round-robin across chunk
directories, matching the layout expected by ``run_with_slurm.sh``:

    output_dir/
        chunk-0/  data_000000.jsonl
        chunk-1/  data_000001.jsonl
        ...
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


def write_chunked_output(
    records: List[Dict[str, Any]],
    output_dir: str,
    num_chunks: int,
    rows_per_file: int,
    dir_prefix: str = "chunk-",
    file_prefix: str = "data",
    on_file_written: Optional[Callable[[int], None]] = None,
) -> Dict[str, Any]:
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
    on_file_written
        Optional callback, called after each file with the number of rows
        written so far. It doesn't change what is written or returned.

    Returns
    -------
    dict
        ``{"files": int, "rows_on_disk": int, "bytes_on_disk": int,
        "files_written": list}`` counted directly from the files this call
        wrote -- not a directory glob, so a rerun into a non-empty output
        directory can't inflate the counts. ``files_written`` has one
        ``{"path", "rows", "bytes", "sha256"}`` entry per file, in write
        order, with ``path`` relative to ``output_dir`` and ``sha256`` taken
        from the exact bytes written.
    """
    if not records:
        log.info("No records to write")
        return {"files": 0, "rows_on_disk": 0, "bytes_on_disk": 0, "files_written": []}

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

    # Tracked per chunk as we write, rather than globbing the chunk
    # directories afterwards, so a rerun into a non-empty output directory
    # can't inflate what this call reports as written.
    chunk_file_counts = [0] * num_chunks
    chunk_row_counts = [0] * num_chunks
    bytes_on_disk = 0
    rows_written = 0
    written_files: List[Dict[str, Any]] = []

    for file_idx, batch in enumerate(file_batches):
        chunk_idx = file_idx % num_chunks
        filename = f"{file_prefix}_{file_idx:06d}.jsonl"
        filepath = chunk_dirs[chunk_idx] / filename
        digest = hashlib.sha256()
        with open(filepath, "wb") as f:
            for rec in batch:
                line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
                digest.update(line)
                f.write(line)
        file_bytes = filepath.stat().st_size
        chunk_file_counts[chunk_idx] += 1
        chunk_row_counts[chunk_idx] += len(batch)
        bytes_on_disk += file_bytes
        written_files.append(
            {
                "path": filepath.relative_to(output_path).as_posix(),
                "rows": len(batch),
                "bytes": file_bytes,
                "sha256": digest.hexdigest(),
            }
        )
        rows_written += len(batch)
        if on_file_written is not None:
            on_file_written(rows_written)

    # Summary
    for i, d in enumerate(chunk_dirs):
        log.info("  %s%d: %d files, %d rows", dir_prefix, i,
                 chunk_file_counts[i], chunk_row_counts[i])

    files_written = sum(chunk_file_counts)
    rows_on_disk = sum(chunk_row_counts)
    log.info("  total files: %d, total records: %d", files_written, rows_on_disk)

    return {
        "files": files_written,
        "rows_on_disk": rows_on_disk,
        "bytes_on_disk": bytes_on_disk,
        "files_written": written_files,
    }
