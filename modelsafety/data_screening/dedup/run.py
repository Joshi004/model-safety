#!/usr/bin/env python3
"""MinHash/LSH near-deduplication for recursive JSONL datasets.

The methodology adapts the Apache-2.0-licensed Cerebras Model Zoo
deduplication pipeline. See THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import multiprocessing
import os
import re
import shutil
import string
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from datasketch import MinHash, MinHashLSH


@dataclass(frozen=True)
class Candidate:
    key: str
    source_file: str
    source_line: int
    text: str


@dataclass
class ScanStats:
    rows_total: int = 0
    rows_hashed: int = 0
    rows_without_text: int = 0
    candidate_pairs: int = 0


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def extract_text(record: dict[str, Any], text_scope: str) -> str:
    """Extract the content used for deduplication.

    ``prompt`` follows the intent of the source implementation: when the final
    message is an assistant response, it is excluded so repeated prompts with
    different generated answers still deduplicate.
    """
    text = record.get("text")
    if isinstance(text, str):
        return text

    messages = record.get("messages")
    if not isinstance(messages, list):
        return ""
    usable = [message for message in messages if isinstance(message, dict)]
    if text_scope == "prompt" and len(usable) > 1:
        last_role = str(usable[-1].get("role") or "").lower()
        if last_role == "assistant":
            usable = usable[:-1]
    parts = [content_text(message.get("content")) for message in usable]
    return "\n".join(part for part in parts if part)


def normalize_text(text: str) -> str:
    lowered = text.lower().translate(str.maketrans("", "", string.punctuation))
    return re.sub(r"\s+", " ", lowered).strip()


def text_features(text: str, width: int) -> Iterator[bytes]:
    normalized = normalize_text(text)
    if not normalized:
        return
    if len(normalized) <= width:
        yield normalized.encode("utf-8")
        return
    for index in range(len(normalized) - width + 1):
        yield normalized[index : index + width].encode("utf-8")


def make_minhash(candidate: Candidate, width: int, num_perm: int) -> tuple[Candidate, MinHash]:
    minhash = MinHash(num_perm=num_perm)
    for feature in text_features(candidate.text, width):
        minhash.update(feature)
    return candidate, minhash


def _hash_worker(args: tuple[Candidate, int, int]) -> tuple[Candidate, MinHash]:
    return make_minhash(*args)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def resolve_inputs(
    input_path: Path | None,
    input_root: Path | None,
    input_manifest: Path | None,
) -> tuple[Path, list[Path]]:
    if input_manifest is not None:
        if input_root is None:
            raise ValueError("--input-root is required with --input-manifest")
        root = input_root.expanduser().resolve()
        manifest = input_manifest.expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Input root is not a directory: {root}")
        if not manifest.is_file():
            raise ValueError(f"Input manifest does not exist: {manifest}")

        files: list[Path] = []
        seen: set[Path] = set()
        for line_number, raw_line in enumerate(
            manifest.read_text(encoding="utf-8").splitlines(), 1
        ):
            relative = raw_line.strip()
            if not relative or relative.startswith("#"):
                continue
            candidate = (root / relative).resolve()
            if not _is_relative_to(candidate, root):
                raise ValueError(f"{manifest}:{line_number}: path escapes input root")
            if candidate.suffix != ".jsonl" or not candidate.is_file():
                raise ValueError(f"{manifest}:{line_number}: missing JSONL file: {relative}")
            if candidate in seen:
                raise ValueError(f"{manifest}:{line_number}: duplicate path: {relative}")
            files.append(candidate)
            seen.add(candidate)
        if not files:
            raise ValueError(f"Manifest has no JSONL inputs: {manifest}")
        return root, files

    if input_path is None:
        raise ValueError("Provide --input, or --input-root with --input-manifest")
    resolved = input_path.expanduser().resolve()
    if resolved.is_file():
        if resolved.suffix != ".jsonl":
            raise ValueError(f"Input file must be JSONL: {resolved}")
        return resolved.parent, [resolved]
    if not resolved.is_dir():
        raise ValueError(f"Input path does not exist: {resolved}")
    files = sorted(path.resolve() for path in resolved.rglob("*.jsonl") if path.is_file())
    if not files:
        raise ValueError(f"No JSONL files found under: {resolved}")
    return resolved, files


def _record_key(
    order: int,
    dataset: str,
    source_file: str,
    source_line: int,
) -> str:
    return f"{order:020d}\0{dataset}\0{source_file}\0{source_line}"


def _key_reference(key: str) -> dict[str, Any]:
    order, dataset, source_file, source_line = key.split("\0", 3)
    return {
        "dataset": dataset,
        "source_file": source_file,
        "source_line": int(source_line),
        "source_order": int(order),
    }


def iter_candidates(
    input_root: Path,
    files: Iterable[Path],
    text_scope: str,
    stats: ScanStats,
    dataset: str = "input",
) -> Iterator[Candidate]:
    order = 0
    for path in files:
        relative = path.relative_to(input_root).as_posix()
        with path.open("r", encoding="utf-8") as handle:
            for source_line, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                stats.rows_total += 1
                order += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Malformed JSON at {path}:{source_line}") from exc
                if not isinstance(record, dict):
                    raise ValueError(f"Expected a JSON object at {path}:{source_line}")
                text = extract_text(record, text_scope)
                if not normalize_text(text):
                    stats.rows_without_text += 1
                    continue
                yield Candidate(
                    key=_record_key(order, dataset, relative, source_line),
                    source_file=relative,
                    source_line=source_line,
                    text=text,
                )


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        parent = self.parent.setdefault(item, item)
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, first: str, second: str) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root == second_root:
            return
        first_order = _key_reference(first_root)["source_order"]
        second_order = _key_reference(second_root)["source_order"]
        if first_order <= second_order:
            self.parent[second_root] = first_root
        else:
            self.parent[first_root] = second_root


def hashed_candidates(
    candidates: Iterable[Candidate],
    width: int,
    num_perm: int,
    workers: int,
    chunksize: int,
) -> Iterator[tuple[Candidate, MinHash]]:
    if workers == 1:
        for candidate in candidates:
            yield make_minhash(candidate, width, num_perm)
        return

    tasks = ((candidate, width, num_perm) for candidate in candidates)
    with multiprocessing.Pool(processes=workers) as pool:
        yield from pool.imap(_hash_worker, tasks, chunksize=chunksize)


def validate_lsh_parameters(width: int, num_perm: int, bands: int, rows: int) -> None:
    if width < 1:
        raise ValueError("--ngram-size must be positive")
    if num_perm < 1 or bands < 1 or rows < 1 or bands * rows != num_perm:
        raise ValueError("--bands * --rows-per-band must equal --num-perm")


def detect_duplicates(
    input_root: Path,
    files: list[Path],
    pairs_file: Path,
    *,
    text_scope: str,
    width: int,
    num_perm: int,
    bands: int,
    rows: int,
    workers: int,
    chunksize: int,
    stats: ScanStats,
) -> list[dict[str, Any]]:
    validate_lsh_parameters(width, num_perm, bands, rows)

    index = MinHashLSH(num_perm=num_perm, params=(bands, rows))
    components = UnionFind()
    source = iter_candidates(input_root, files, text_scope, stats)
    pairs_file.parent.mkdir(parents=True, exist_ok=True)

    with pairs_file.open("w", encoding="utf-8") as pairs_handle:
        for candidate, minhash in hashed_candidates(
            source, width, num_perm, workers, chunksize
        ):
            matches = sorted(
                index.query(minhash),
                key=lambda key: _key_reference(key)["source_order"],
            )
            current_ref = _key_reference(candidate.key)
            for match in matches:
                match_ref = _key_reference(match)
                components.union(match, candidate.key)
                stats.candidate_pairs += 1
                pairs_handle.write(
                    json.dumps(
                        {
                            "left": match_ref,
                            "right": current_ref,
                            "method": "minhash_lsh_band_collision",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            index.insert(candidate.key, minhash)
            stats.rows_hashed += 1
            if stats.rows_hashed % 100_000 == 0:
                print(f"Hashed {stats.rows_hashed:,} rows", flush=True)

    groups: dict[str, list[str]] = {}
    for key in components.parent:
        groups.setdefault(components.find(key), []).append(key)

    decisions: list[dict[str, Any]] = []
    for members in groups.values():
        canonical = min(members, key=lambda key: _key_reference(key)["source_order"])
        canonical_ref = _key_reference(canonical)
        for duplicate in members:
            if duplicate == canonical:
                continue
            reference = _key_reference(duplicate)
            decisions.append(
                {
                    "source_file": reference["source_file"],
                    "source_line": reference["source_line"],
                    "duplicate_of": {
                        "source_file": canonical_ref["source_file"],
                        "source_line": canonical_ref["source_line"],
                    },
                    "method": "minhash_lsh",
                }
            )
    decisions.sort(
        key=lambda item: (
            item["source_file"],
            item["source_line"],
        )
    )
    return decisions


def detect_cross_duplicates(
    reference_root: Path,
    reference_files: list[Path],
    candidate_root: Path,
    candidate_files: list[Path],
    pairs_file: Path,
    *,
    text_scope: str,
    width: int,
    num_perm: int,
    bands: int,
    rows: int,
    workers: int,
    chunksize: int,
    reference_stats: ScanStats,
    candidate_stats: ScanStats,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    Counter[str],
]:
    """Match candidate rows against a read-only reference dataset."""
    validate_lsh_parameters(width, num_perm, bands, rows)
    index = MinHashLSH(num_perm=num_perm, params=(bands, rows))

    reference_source = iter_candidates(
        reference_root,
        reference_files,
        text_scope,
        reference_stats,
        dataset="reference",
    )
    reference_rows_by_file: Counter[str] = Counter()
    for reference, minhash in hashed_candidates(
        reference_source, width, num_perm, workers, chunksize
    ):
        index.insert(reference.key, minhash)
        reference_stats.rows_hashed += 1
        reference_rows_by_file[reference.source_file] += 1
        if reference_stats.rows_hashed % 100_000 == 0:
            print(
                f"Indexed {reference_stats.rows_hashed:,} reference rows",
                flush=True,
            )

    pairs_file.parent.mkdir(parents=True, exist_ok=True)
    decisions: list[dict[str, Any]] = []
    direct_matches: list[dict[str, Any]] = []
    candidate_source = iter_candidates(
        candidate_root,
        candidate_files,
        text_scope,
        candidate_stats,
        dataset="candidate",
    )
    with pairs_file.open("w", encoding="utf-8") as pairs_handle:
        for candidate, minhash in hashed_candidates(
            candidate_source, width, num_perm, workers, chunksize
        ):
            matches = sorted(
                index.query(minhash),
                key=lambda key: _key_reference(key)["source_order"],
            )
            current_ref = _key_reference(candidate.key)
            for match in matches:
                match_ref = _key_reference(match)
                candidate_stats.candidate_pairs += 1
                direct_matches.append(
                    {
                        "source_dataset": "candidate",
                        "source_file": candidate.source_file,
                        "source_line": candidate.source_line,
                        "duplicate_of": {
                            "dataset": "reference",
                            "source_file": match_ref["source_file"],
                            "source_line": match_ref["source_line"],
                        },
                        "method": "cross_minhash_lsh",
                    }
                )
                pairs_handle.write(
                    json.dumps(
                        {
                            "left": match_ref,
                            "right": current_ref,
                            "method": "cross_minhash_lsh_band_collision",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            if matches:
                canonical_ref = _key_reference(matches[0])
                decisions.append(
                    {
                        "source_dataset": "candidate",
                        "source_file": candidate.source_file,
                        "source_line": candidate.source_line,
                        "duplicate_of": {
                            "dataset": "reference",
                            "source_file": canonical_ref["source_file"],
                            "source_line": canonical_ref["source_line"],
                        },
                        "method": "cross_minhash_lsh",
                    }
                )
            candidate_stats.rows_hashed += 1
            if candidate_stats.rows_hashed % 100_000 == 0:
                print(
                    f"Matched {candidate_stats.rows_hashed:,} candidate rows",
                    flush=True,
                )
    decisions.sort(key=lambda item: (item["source_file"], item["source_line"]))
    direct_matches.sort(
        key=lambda item: (
            item["source_file"],
            item["source_line"],
            item["duplicate_of"]["source_file"],
            item["duplicate_of"]["source_line"],
        )
    )
    return decisions, direct_matches, reference_rows_by_file


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_selected_records(
    input_root: Path,
    files: list[Path],
    coordinates: set[tuple[str, int]],
) -> dict[tuple[str, int], dict[str, Any]]:
    wanted_by_file: dict[str, set[int]] = {}
    for source_file, source_line in coordinates:
        wanted_by_file.setdefault(source_file, set()).add(source_line)

    file_by_relative = {
        path.relative_to(input_root).as_posix(): path
        for path in files
    }
    records: dict[tuple[str, int], dict[str, Any]] = {}
    for relative, wanted_lines in wanted_by_file.items():
        source = file_by_relative.get(relative)
        if source is None:
            raise ValueError(f"Matched source file is not in the input set: {relative}")
        remaining = set(wanted_lines)
        with source.open("r", encoding="utf-8") as handle:
            for source_line, line in enumerate(handle, 1):
                if source_line not in remaining:
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError(f"Expected a JSON object at {source}:{source_line}")
                records[(relative, source_line)] = record
                remaining.remove(source_line)
                if not remaining:
                    break
        if remaining:
            missing = min(remaining)
            raise ValueError(f"Matched source coordinate does not exist: {relative}:{missing}")
    return records


def record_identifier(record: dict[str, Any]) -> str:
    for key in ("id", "uuid", "sample_id", "question_id", "task_id"):
        if key not in record:
            continue
        value = record[key]
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        for key in ("benchmark_id", "id", "uuid", "sample_id", "question_id"):
            value = metadata.get(key)
            if value not in (None, ""):
                return str(value)
    return ""


def report_text(record: dict[str, Any], text_scope: str, max_chars: int) -> str:
    text = extract_text(record, text_scope)
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


def write_matched_samples_csv(
    path: Path,
    decisions: list[dict[str, Any]],
    datasets: dict[str, tuple[Path, list[Path]]],
    *,
    text_scope: str,
    max_chars: int,
) -> None:
    if max_chars < 0:
        raise ValueError("--report-text-chars cannot be negative")

    wanted: dict[str, set[tuple[str, int]]] = {}
    resolved_decisions: list[tuple[dict[str, Any], str, str]] = []
    for decision in decisions:
        source_dataset = str(decision.get("source_dataset") or "input")
        reference = decision["duplicate_of"]
        reference_dataset = str(reference.get("dataset") or source_dataset)
        if source_dataset not in datasets:
            raise ValueError(f"Unknown source dataset in decision: {source_dataset}")
        if reference_dataset not in datasets:
            raise ValueError(f"Unknown reference dataset in decision: {reference_dataset}")
        wanted.setdefault(source_dataset, set()).add(
            (decision["source_file"], decision["source_line"])
        )
        wanted.setdefault(reference_dataset, set()).add(
            (reference["source_file"], reference["source_line"])
        )
        resolved_decisions.append((decision, source_dataset, reference_dataset))

    records_by_dataset = {
        dataset: load_selected_records(root, files, wanted.get(dataset, set()))
        for dataset, (root, files) in datasets.items()
    }
    fieldnames = [
        "source_dataset",
        "source_root",
        "source_file",
        "source_line",
        "source_id",
        "source_text",
        "reference_dataset",
        "reference_root",
        "reference_file",
        "reference_line",
        "reference_id",
        "reference_text",
        "method",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for decision, source_dataset, reference_dataset in resolved_decisions:
            reference = decision["duplicate_of"]
            source_key = (decision["source_file"], decision["source_line"])
            reference_key = (reference["source_file"], reference["source_line"])
            source_record = records_by_dataset[source_dataset][source_key]
            reference_record = records_by_dataset[reference_dataset][reference_key]
            writer.writerow(
                {
                    "source_dataset": source_dataset,
                    "source_root": str(datasets[source_dataset][0]),
                    "source_file": decision["source_file"],
                    "source_line": decision["source_line"],
                    "source_id": record_identifier(source_record),
                    "source_text": report_text(source_record, text_scope, max_chars),
                    "reference_dataset": reference_dataset,
                    "reference_root": str(datasets[reference_dataset][0]),
                    "reference_file": reference["source_file"],
                    "reference_line": reference["source_line"],
                    "reference_id": record_identifier(reference_record),
                    "reference_text": report_text(
                        reference_record, text_scope, max_chars
                    ),
                    "method": decision["method"],
                }
            )


def write_contamination_by_benchmark_csv(
    path: Path,
    direct_matches: list[dict[str, Any]],
    reference_rows_by_file: Counter[str],
) -> dict[str, Any]:
    contaminated_lines: dict[str, set[int]] = {}
    matched_training_rows: dict[str, set[tuple[str, int]]] = {}
    for match in direct_matches:
        reference = match["duplicate_of"]
        reference_file = reference["source_file"]
        contaminated_lines.setdefault(reference_file, set()).add(
            reference["source_line"]
        )
        matched_training_rows.setdefault(reference_file, set()).add(
            (match["source_file"], match["source_line"])
        )

    rows = []
    for reference_file in sorted(reference_rows_by_file):
        benchmark_rows = reference_rows_by_file[reference_file]
        contaminated_rows = len(contaminated_lines.get(reference_file, set()))
        rows.append(
            {
                "benchmark_file": reference_file,
                "benchmark_rows": benchmark_rows,
                "contaminated_benchmark_rows": contaminated_rows,
                "contaminated_percent": (
                    contaminated_rows / benchmark_rows * 100
                    if benchmark_rows
                    else 0.0
                ),
                "matched_training_rows": len(
                    matched_training_rows.get(reference_file, set())
                ),
            }
        )

    fieldnames = [
        "benchmark_file",
        "benchmark_rows",
        "contaminated_benchmark_rows",
        "contaminated_percent",
        "matched_training_rows",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    reference_rows = sum(reference_rows_by_file.values())
    contaminated_reference_rows = sum(len(lines) for lines in contaminated_lines.values())
    return {
        "reference_rows": reference_rows,
        "contaminated_reference_rows": contaminated_reference_rows,
        "contaminated_reference_percent": (
            contaminated_reference_rows / reference_rows * 100
            if reference_rows
            else 0.0
        ),
        "benchmark_files_with_contamination": sum(
            bool(lines) for lines in contaminated_lines.values()
        ),
        "benchmark_files": len(reference_rows_by_file),
    }


def write_partition(
    input_root: Path,
    files: list[Path],
    output_dir: Path,
    decisions: list[dict[str, Any]],
) -> tuple[int, int]:
    duplicate_keys = {
        (item["source_file"], item["source_line"]) for item in decisions
    }
    kept_count = 0
    duplicate_count = 0
    for source in files:
        relative = source.relative_to(input_root)
        kept_path = output_dir / "filtered" / relative
        duplicate_path = output_dir / "filtered_out" / relative
        kept_path.parent.mkdir(parents=True, exist_ok=True)
        duplicate_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            source.open("rb") as source_handle,
            kept_path.open("wb") as kept_handle,
            duplicate_path.open("wb") as duplicate_handle,
        ):
            for source_line, raw_line in enumerate(source_handle, 1):
                key = (relative.as_posix(), source_line)
                if key in duplicate_keys:
                    duplicate_handle.write(raw_line)
                    duplicate_count += 1
                else:
                    kept_handle.write(raw_line)
                    kept_count += 1
    return kept_count, duplicate_count


def collision_probability_midpoint(bands: int, rows: int) -> float:
    return (1.0 - math.pow(0.5, 1.0 / bands)) ** (1.0 / rows)


def run(
    input_root: Path,
    files: list[Path],
    output_dir: Path,
    *,
    text_scope: str = "prompt",
    width: int = 60,
    num_perm: int = 256,
    bands: int = 32,
    rows: int = 8,
    workers: int = 1,
    chunksize: int = 100,
    report_text_chars: int = 2000,
    write_filtered: bool = True,
    overwrite: bool = False,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    if (
        output_dir == input_root
        or _is_relative_to(input_root, output_dir)
        or _is_relative_to(output_dir, input_root)
    ):
        raise ValueError("Input and output directories cannot contain one another")
    if output_dir.exists() and any(output_dir.iterdir()):
        if not overwrite:
            raise ValueError(f"Output directory is not empty: {output_dir}; use --overwrite")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    stats = ScanStats()
    decisions = detect_duplicates(
        input_root,
        files,
        output_dir / "duplicate_pairs.jsonl",
        text_scope=text_scope,
        width=width,
        num_perm=num_perm,
        bands=bands,
        rows=rows,
        workers=workers,
        chunksize=chunksize,
        stats=stats,
    )
    write_jsonl(output_dir / "dedup_results.jsonl", decisions)
    write_matched_samples_csv(
        output_dir / "matched_samples.csv",
        decisions,
        {"input": (input_root, files)},
        text_scope=text_scope,
        max_chars=report_text_chars,
    )

    kept_rows: int | None = None
    filtered_duplicate_rows: int | None = None
    if write_filtered:
        kept_rows, filtered_duplicate_rows = write_partition(
            input_root, files, output_dir, decisions
        )

    summary = {
        "mode": "global",
        "input_root": str(input_root),
        "output_dir": str(output_dir),
        "files": len(files),
        "rows_total": stats.rows_total,
        "rows_hashed": stats.rows_hashed,
        "rows_without_text": stats.rows_without_text,
        "candidate_pairs": stats.candidate_pairs,
        "duplicate_rows": len(decisions),
        "kept_rows": kept_rows,
        "filtered_duplicate_rows": filtered_duplicate_rows,
        "configuration": {
            "text_scope": text_scope,
            "ngram_size": width,
            "num_perm": num_perm,
            "bands": bands,
            "rows_per_band": rows,
            "collision_probability_midpoint_similarity": collision_probability_midpoint(
                bands, rows
            ),
            "workers": workers,
            "report_text_chars": report_text_chars,
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (output_dir / "_SUCCESS").touch()
    return summary


def run_cross(
    reference_root: Path,
    reference_files: list[Path],
    candidate_root: Path,
    candidate_files: list[Path],
    output_dir: Path,
    *,
    text_scope: str = "prompt",
    width: int = 60,
    num_perm: int = 256,
    bands: int = 32,
    rows: int = 8,
    workers: int = 1,
    chunksize: int = 100,
    report_text_chars: int = 2000,
    write_filtered: bool = True,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Remove candidate rows that match any row in the retained reference."""
    overlapping_files = set(reference_files).intersection(candidate_files)
    if overlapping_files:
        example = sorted(overlapping_files)[0]
        raise ValueError(f"Reference and candidate inputs overlap: {example}")

    output_dir = output_dir.expanduser().resolve()
    for input_root in (reference_root, candidate_root):
        if (
            output_dir == input_root
            or _is_relative_to(input_root, output_dir)
            or _is_relative_to(output_dir, input_root)
        ):
            raise ValueError("Input and output directories cannot contain one another")
    if output_dir.exists() and any(output_dir.iterdir()):
        if not overwrite:
            raise ValueError(f"Output directory is not empty: {output_dir}; use --overwrite")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    reference_stats = ScanStats()
    candidate_stats = ScanStats()
    decisions, direct_matches, reference_rows_by_file = detect_cross_duplicates(
        reference_root,
        reference_files,
        candidate_root,
        candidate_files,
        output_dir / "duplicate_pairs.jsonl",
        text_scope=text_scope,
        width=width,
        num_perm=num_perm,
        bands=bands,
        rows=rows,
        workers=workers,
        chunksize=chunksize,
        reference_stats=reference_stats,
        candidate_stats=candidate_stats,
    )
    write_jsonl(output_dir / "dedup_results.jsonl", decisions)
    write_matched_samples_csv(
        output_dir / "matched_samples.csv",
        direct_matches,
        {
            "reference": (reference_root, reference_files),
            "candidate": (candidate_root, candidate_files),
        },
        text_scope=text_scope,
        max_chars=report_text_chars,
    )
    contamination_summary = write_contamination_by_benchmark_csv(
        output_dir / "contamination_by_benchmark.csv",
        direct_matches,
        reference_rows_by_file,
    )

    kept_rows: int | None = None
    filtered_duplicate_rows: int | None = None
    if write_filtered:
        kept_rows, filtered_duplicate_rows = write_partition(
            candidate_root,
            candidate_files,
            output_dir,
            decisions,
        )

    summary = {
        "mode": "cross",
        "reference": {
            "input_root": str(reference_root),
            "files": len(reference_files),
            "rows_total": reference_stats.rows_total,
            "rows_hashed": reference_stats.rows_hashed,
            "rows_without_text": reference_stats.rows_without_text,
            **contamination_summary,
        },
        "candidate": {
            "input_root": str(candidate_root),
            "files": len(candidate_files),
            "rows_total": candidate_stats.rows_total,
            "rows_hashed": candidate_stats.rows_hashed,
            "rows_without_text": candidate_stats.rows_without_text,
            "kept_rows": kept_rows,
            "duplicate_rows": len(decisions),
            "filtered_duplicate_rows": filtered_duplicate_rows,
        },
        "candidate_pairs": candidate_stats.candidate_pairs,
        "output_dir": str(output_dir),
        "configuration": {
            "text_scope": text_scope,
            "ngram_size": width,
            "num_perm": num_perm,
            "bands": bands,
            "rows_per_band": rows,
            "collision_probability_midpoint_similarity": collision_probability_midpoint(
                bands, rows
            ),
            "workers": workers,
            "report_text_chars": report_text_chars,
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (output_dir / "_SUCCESS").touch()
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="A JSONL file or recursive JSONL root")
    parser.add_argument("--input-root", type=Path)
    parser.add_argument("--input-manifest", type=Path)
    parser.add_argument(
        "--reference-input",
        type=Path,
        help="Cross mode: retained reference JSONL file or recursive root",
    )
    parser.add_argument(
        "--candidate-input",
        type=Path,
        help="Cross mode: candidate JSONL file or recursive root to filter",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--text-scope", choices=["prompt", "all"], default="prompt")
    parser.add_argument("--ngram-size", type=int, default=60)
    parser.add_argument("--num-perm", type=int, default=256)
    parser.add_argument("--bands", type=int, default=32)
    parser.add_argument("--rows-per-band", type=int, default=8)
    parser.add_argument("--workers", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--chunksize", type=int, default=100)
    parser.add_argument(
        "--report-text-chars",
        type=int,
        default=2000,
        help="Maximum compared-text characters per CSV cell; 0 writes full text",
    )
    parser.add_argument("--no-write-filtered", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        cross_requested = args.reference_input is not None or args.candidate_input is not None
        common = {
            "text_scope": args.text_scope,
            "width": args.ngram_size,
            "num_perm": args.num_perm,
            "bands": args.bands,
            "rows": args.rows_per_band,
            "workers": args.workers,
            "chunksize": args.chunksize,
            "report_text_chars": args.report_text_chars,
            "write_filtered": not args.no_write_filtered,
            "overwrite": args.overwrite,
        }
        if cross_requested:
            if args.reference_input is None or args.candidate_input is None:
                raise ValueError(
                    "Cross mode requires both --reference-input and --candidate-input"
                )
            if args.input is not None or args.input_root is not None or args.input_manifest is not None:
                raise ValueError(
                    "Do not combine global input arguments with cross-mode inputs"
                )
            reference_root, reference_files = resolve_inputs(
                args.reference_input, None, None
            )
            candidate_root, candidate_files = resolve_inputs(
                args.candidate_input, None, None
            )
            summary = run_cross(
                reference_root,
                reference_files,
                candidate_root,
                candidate_files,
                args.output_dir,
                **common,
            )
        else:
            input_root, files = resolve_inputs(
                args.input, args.input_root, args.input_manifest
            )
            summary = run(
                input_root,
                files,
                args.output_dir,
                **common,
            )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
