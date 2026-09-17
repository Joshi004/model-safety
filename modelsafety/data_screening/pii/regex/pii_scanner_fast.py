#!/usr/bin/env python3
"""
PII Scanner FAST - True line-level parallelism with chunking
Pre-scans files, creates chunks, distributes evenly across workers
"""

import hashlib
import json
import os
import shutil
import time
from multiprocessing import Pool
from pathlib import Path

from piiregex import regexes as AVAILABLE_REGEXES
from tqdm import tqdm

from pii_output import (
    PII_CATEGORIES,
    print_run_summary,
    resolve_run_dir,
    stage1_paths,
    write_stage1_metrics,
)
from modelsafety.contract.textract import (
    extract_record_text,
    load_source_snapshot,
    verify_source_snapshot,
)

CHUNK_SIZE = int(os.environ.get("PII_CHUNK_SIZE", "10000"))
PII_REGEXES = {category: AVAILABLE_REGEXES[category] for category in PII_CATEGORIES}


def normalize_hit_item(item):
    """Keep the pipe-delimited hit format one physical line per candidate."""
    return json.dumps(str(item), ensure_ascii=False)


def discover_jsonl_files(base_dir, manifest_path=None):
    """Discover all JSONL files or load a relative-path shard manifest."""
    root = Path(base_dir).resolve()
    if manifest_path is None:
        return sorted(str(path) for path in root.rglob("*.jsonl") if path.is_file())

    manifest = Path(manifest_path).expanduser().resolve()
    files = []
    seen = set()
    for line_number, raw_line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        relative = raw_line.strip()
        if not relative or relative.startswith("#"):
            continue
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise SystemExit(
                f"{manifest}:{line_number}: path escapes PII_BASE_DIR: {relative}"
            ) from exc
        if candidate.suffix != ".jsonl" or not candidate.is_file():
            raise SystemExit(f"{manifest}:{line_number}: missing JSONL file: {relative}")
        if candidate in seen:
            raise SystemExit(f"{manifest}:{line_number}: duplicate path: {relative}")
        seen.add(candidate)
        files.append(str(candidate))
    return files


def create_chunks(jsonl_files, expected_sha256=None):
    print("Pre-scanning files to create chunks...")
    chunks = []
    expected_sha256 = expected_sha256 or {}

    for file_path in tqdm(jsonl_files, desc="Scanning files", unit="file"):
        digest = hashlib.sha256()
        with open(file_path, "rb") as handle:
            next_line = 1
            while True:
                start_offset = handle.tell()
                start_line = next_line
                lines_in_chunk = 0
                for _ in range(CHUNK_SIZE):
                    raw_line = handle.readline()
                    if not raw_line:
                        break
                    digest.update(raw_line)
                    lines_in_chunk += 1
                    next_line += 1
                if lines_in_chunk == 0:
                    break
                chunks.append(
                    (
                        file_path,
                        start_line,
                        start_line + lines_in_chunk - 1,
                        start_offset,
                        handle.tell(),
                    )
                )
        expected = expected_sha256.get(file_path)
        if expected is not None and digest.hexdigest() != expected:
            raise RuntimeError(f"Source content differs from snapshot: {file_path}")

    return chunks


def process_chunk(args):
    (
        worker_id,
        chunk_id,
        file_path,
        start_line,
        end_line,
        start_offset,
        end_offset,
        base_dir,
        workers_dir,
    ) = args

    shard_name = f"worker_{worker_id}_chunk_{chunk_id}"
    worker_output_dir = os.path.join(workers_dir, shard_name)
    os.makedirs(worker_output_dir, exist_ok=True)

    output_files = {}
    for category in PII_CATEGORIES:
        output_file_path = os.path.join(worker_output_dir, f"{category}.txt")
        output_files[category] = open(output_file_path, "a", encoding="utf-8", buffering=1)

    lines_processed = 0
    pii_counts = {category: 0 for category in PII_CATEGORIES}

    try:
        with open(file_path, "rb") as f:
            f.seek(start_offset)
            current_line = start_line
            while current_line <= end_line and f.tell() < end_offset:
                raw_line = f.readline()
                if not raw_line:
                    raise RuntimeError(
                        f"Unexpected EOF at {file_path}:{current_line} "
                        f"for chunk {start_line}-{end_line}"
                    )
                lines_processed += 1
                try:
                    line = raw_line.decode("utf-8")
                    data = json.loads(line)
                    text = extract_record_text(data)
                    if text:
                        source_sha256 = hashlib.sha256(
                            raw_line.rstrip(b"\r\n")
                        ).hexdigest()
                        for category, pattern in PII_REGEXES.items():
                            pii_items = pattern.findall(text)
                            if pii_items:
                                rel_path = os.path.relpath(file_path, base_dir)
                                for item in set(pii_items):
                                    normalized_item = normalize_hit_item(item)
                                    output_files[category].write(
                                        f"{rel_path}|{current_line}|{source_sha256}|"
                                        f"{normalized_item}\n"
                                    )
                                    pii_counts[category] += 1
                except Exception as exc:
                    raise RuntimeError(
                        f"Failed to scan {file_path}:{current_line}: {exc}"
                    ) from exc
                current_line += 1
    except Exception as e:
        raise RuntimeError(
            f"Worker {worker_id} failed chunk {chunk_id} "
            f"({file_path}:{start_line}-{end_line}): {e}"
        ) from e
    finally:
        for handle in output_files.values():
            handle.close()

    return shard_name, lines_processed, pii_counts


def merge_worker_files(hits_dir, workers_dir, shard_names):
    print("\nMerging worker shards into stage1_scan/hits/ ...")
    os.makedirs(hits_dir, exist_ok=True)
    merged_counts = {category: 0 for category in PII_CATEGORIES}

    for category in tqdm(PII_CATEGORIES, desc="Merging categories"):
        final_file = os.path.join(hits_dir, f"{category}.txt")
        with open(final_file, "w", encoding="utf-8") as outf:
            for shard_name in shard_names:
                worker_file = os.path.join(workers_dir, shard_name, f"{category}.txt")
                if not os.path.exists(worker_file):
                    continue
                with open(worker_file, "r", encoding="utf-8") as inf:
                    for line in inf:
                        outf.write(line)
                        merged_counts[category] += 1
        print(f"  {category}: {merged_counts[category]:,} hits")

    return merged_counts


def main():
    base_dir = os.environ.get("PII_BASE_DIR")
    if not base_dir:
        raise SystemExit("PII_BASE_DIR is required (root directory to scan for **/*.jsonl)")

    base_dir = str(Path(base_dir).expanduser().resolve())
    manifest_path = os.environ.get("PII_INPUT_MANIFEST")
    run_dir = resolve_run_dir()
    paths = stage1_paths(run_dir)
    paths["stage_dir"].mkdir(parents=True, exist_ok=True)
    if paths["hits_dir"].exists():
        shutil.rmtree(paths["hits_dir"])
    paths["hits_dir"].mkdir(parents=True, exist_ok=True)
    if paths["workers_dir"].exists():
        shutil.rmtree(paths["workers_dir"])
    paths["workers_dir"].mkdir(parents=True, exist_ok=True)

    num_workers = int(os.environ.get("PII_NUM_WORKERS", "100"))

    print("=" * 60)
    print("PII Scanner FAST — stage 1 (regex scan)")
    print("=" * 60)
    print(f"Input root:   {base_dir}")
    print(f"Manifest:     {manifest_path or 'directory discovery'}")
    print(f"Run root:     {run_dir}")
    print(f"Stage output: {paths['stage_dir']}")
    print(f"Hits:         {paths['hits_dir']}")
    print(f"Workers:      {paths['workers_dir']}")
    print(f"Workers:      {num_workers}  |  Chunk size: {CHUNK_SIZE}")
    print()

    print("Discovering JSONL files...")
    jsonl_files = discover_jsonl_files(base_dir, manifest_path)
    snapshot_path = os.environ.get("PII_SNAPSHOT_MANIFEST")
    snapshots = {}
    if snapshot_path:
        snapshots = load_source_snapshot(Path(snapshot_path), Path(base_dir))
        for file_path in jsonl_files:
            source = Path(file_path)
            relative = source.relative_to(base_dir).as_posix()
            expected = snapshots.get(relative)
            if expected is None:
                raise RuntimeError(f"Source is absent from snapshot: {relative}")
            verify_source_snapshot(source, expected)
    print(f"Found {len(jsonl_files)} JSONL files\n")

    if not jsonl_files:
        print("No JSONL files found. Exiting.")
        return

    start_chunk_time = time.time()
    expected_sha256 = {}
    for file_path in jsonl_files:
        if snapshots:
            relative = Path(file_path).relative_to(base_dir).as_posix()
            expected_sha256[file_path] = snapshots[relative]["sha256"]
    chunks = create_chunks(jsonl_files, expected_sha256)
    chunk_time = time.time() - start_chunk_time
    print(f"\nCreated {len(chunks)} chunks in {chunk_time:.2f}s\n")

    work_items = []
    chunk_assignments = [[] for _ in range(num_workers)]
    for i, chunk in enumerate(chunks):
        chunk_assignments[i % num_workers].append(chunk)
    for worker_id in range(num_workers):
        for chunk_id, (
            file_path,
            start_line,
            end_line,
            start_offset,
            end_offset,
        ) in enumerate(chunk_assignments[worker_id]):
            work_items.append((
                worker_id,
                chunk_id,
                file_path,
                start_line,
                end_line,
                start_offset,
                end_offset,
                base_dir,
                str(paths["workers_dir"]),
            ))

    print(f"Processing {len(work_items)} chunks with {num_workers} workers ...")
    start_time = time.time()
    with Pool(processes=num_workers) as pool:
        results = list(tqdm(
            pool.imap_unordered(process_chunk, work_items),
            total=len(work_items),
            desc="Scanning",
            unit="chunk",
        ))
    processing_time = time.time() - start_time
    if snapshots:
        for file_path in jsonl_files:
            source = Path(file_path)
            relative = source.relative_to(base_dir).as_posix()
            verify_source_snapshot(source, snapshots[relative])

    total_lines = sum(result[1] for result in results)
    shard_names = sorted(result[0] for result in results)
    final_pii_counts = merge_worker_files(
        str(paths["hits_dir"]),
        str(paths["workers_dir"]),
        shard_names,
    )

    write_stage1_metrics(
        paths["metrics_file"],
        run_dir=run_dir,
        base_dir=Path(base_dir),
        total_files=len(jsonl_files),
        total_chunks=len(chunks),
        chunk_size=CHUNK_SIZE,
        total_lines=total_lines,
        processing_time=processing_time,
        pii_counts=final_pii_counts,
        num_workers=num_workers,
    )

    print(f"\nStage 1 summary: {paths['metrics_file']}")
    print_run_summary(run_dir)
    print("\nStage 1 complete.")


if __name__ == "__main__":
    main()
