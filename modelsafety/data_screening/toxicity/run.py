#!/usr/bin/env python3
"""Stage-1 Detoxify screen with source-level traceability and manifest support."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Any, Iterator

import accelerate
from accelerate.utils import gather_object
from tqdm import tqdm

from detoxify import Detoxify

# Not on sys.path by default when this script is run standalone (cd'd into
# this directory, no PYTHONPATH export) -- added so the shared contract
# modules import regardless of how this script is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from modelsafety.contract.fingerprints import input_block, line_fingerprint  # noqa: E402


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def resolve_inputs(
    input_path: Path | None,
    input_root: Path | None,
    input_manifest: Path | None,
) -> tuple[Path, list[Path]]:
    """Resolve a single JSONL input or a root-relative shard manifest."""
    if input_manifest is None:
        if input_path is None:
            raise ValueError("Provide --input or --input-manifest with --input-root")
        resolved = input_path.expanduser().resolve()
        if not resolved.is_file():
            raise ValueError(f"Input JSONL does not exist: {resolved}")
        return resolved.parent, [resolved]

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
    for line_number, raw_line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        relative = raw_line.strip()
        if not relative or relative.startswith("#"):
            continue
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{manifest}:{line_number}: path escapes input root") from exc
        if candidate.suffix != ".jsonl" or not candidate.is_file():
            raise ValueError(f"{manifest}:{line_number}: missing JSONL file: {relative}")
        if candidate in seen:
            raise ValueError(f"{manifest}:{line_number}: duplicate path: {relative}")
        seen.add(candidate)
        files.append(candidate)
    if not files:
        raise ValueError(f"Manifest has no JSONL inputs: {manifest}")
    return root, files


def iter_candidates(
    input_root: Path,
    files: list[Path],
    input_files: list[dict[str, str]],
    per_file: dict[str, dict[str, int]],
) -> Iterator[dict[str, Any]]:
    """Yield one Detoxify candidate per plain text or conversation message.

    Every candidate carries ``source_sha256``, the fingerprint of its line. Each
    file's path and SHA-256 is appended to ``input_files`` once the file has
    been read to the end. ``per_file`` gets one entry per file, made before the
    file is read, with the row counts: every row is read, and has text when it
    yields at least one candidate (a plain ``text`` string, even an empty one,
    is a candidate).
    """
    for path in files:
        relative = path.relative_to(input_root).as_posix()
        digest = hashlib.sha256()
        counts = per_file[relative] = {
            "rows": 0,
            "messages": 0,
            "rows_read": 0,
            "rows_with_text": 0,
            "rows_without_text": 0,
            "findings": 0,
        }
        with path.open("rb") as handle:
            for source_line, raw_line in enumerate(handle, 1):
                digest.update(raw_line)
                counts["rows_read"] += 1
                try:
                    source_record = json.loads(raw_line.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Malformed JSON at {path}:{source_line}") from exc
                source_sha256 = line_fingerprint(raw_line)

                text = source_record.get("text")
                if isinstance(text, str):
                    counts["rows_with_text"] += 1
                    yield {
                        "source_file": relative,
                        "source_line": source_line,
                        "source_sha256": source_sha256,
                        "message_index": None,
                        "role": "user",
                        "text": text,
                        "source_record": source_record,
                    }
                    continue

                row_has_candidate = False
                messages = source_record.get("messages")
                if isinstance(messages, list):
                    for message_index, message in enumerate(messages):
                        if not isinstance(message, dict):
                            continue
                        role = str(message.get("role") or "")
                        content = content_text(message.get("content"))
                        if not content:
                            continue
                        if not row_has_candidate:
                            row_has_candidate = True
                            counts["rows_with_text"] += 1
                        yield {
                            "source_file": relative,
                            "source_line": source_line,
                            "source_sha256": source_sha256,
                            "message_index": message_index,
                            "role": role,
                            "text": f"{role}: {content}",
                            "source_record": source_record,
                        }
                if not row_has_candidate:
                    counts["rows_without_text"] += 1
        input_files.append({"path": relative, "sha256": digest.hexdigest()})


def count_candidates(
    candidates: Iterator[dict[str, Any]], stats: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    """Wrap ``iter_candidates()``, counting rows and messages per source file
    as they stream through -- without buffering anything or changing what's
    yielded. A "row" is one source JSONL line; a plain-``text`` row and a
    conversation row with N messages both count as one row, but N and 1
    messages respectively (candidates for the same row are always yielded
    consecutively by ``iter_candidates``, so a change in (file, line) is
    exactly a new row).
    """
    last_row_key: tuple[str, int] | None = None
    for candidate in candidates:
        row_key = (candidate["source_file"], candidate["source_line"])
        per_file = stats["per_file"].setdefault(
            candidate["source_file"], {"rows": 0, "messages": 0}
        )
        if row_key != last_row_key:
            per_file["rows"] += 1
            stats["rows_read"] += 1
            last_row_key = row_key
        per_file["messages"] += 1
        stats["messages_scored"] += 1
        yield candidate


def verify_snapshot(input_root: Path, files: list[Path], snapshot_manifest: Path) -> None:
    """Confirm every file this run is about to scan still matches the size
    and mtime recorded when the shard manifest (with its per-file SHA-256)
    was built -- the same manifest.json shape, and the same check,
    pii_extractor.py and quality_filter_finalize.py already run before
    trusting a source file.
    """
    from modelsafety.contract.textract import load_source_snapshot, verify_source_snapshot

    snapshots = load_source_snapshot(snapshot_manifest, input_root)
    for path in files:
        relative = path.relative_to(input_root).as_posix()
        expected = snapshots.get(relative)
        if expected is None:
            raise RuntimeError(f"{relative} is not present in the snapshot manifest")
        verify_source_snapshot(path, expected)


def histogram_to_dict(histogram: list[int]) -> dict[str, int]:
    return {
        f"{index * 0.05:.2f}-{(index + 1) * 0.05:.2f}": count
        for index, count in enumerate(histogram)
    }


def write_summary_atomic(summary_file: Path, summary: dict[str, Any]) -> None:
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    temp = summary_file.with_name(f".{summary_file.name}.partial")
    temp.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(summary_file)


def normalize_predictions(result: dict[str, Any], expected: int) -> list[dict[str, float]]:
    columns: dict[str, list[float]] = {}
    for category, values in result.items():
        if not isinstance(values, list):
            values = [values]
        columns[category] = [float(value) for value in values]
        if len(columns[category]) != expected:
            raise RuntimeError(
                f"Detoxify returned {len(columns[category])} {category} scores for {expected} inputs"
            )
    return [
        {category: values[index] for category, values in columns.items()}
        for index in range(expected)
    ]


def print_histogram(histogram: list[int]) -> None:
    total = sum(histogram)
    print("\nDistribution of maximum toxicity-class score:")
    if total == 0:
        print("No candidates processed.")
        return
    for index, count in enumerate(histogram):
        start = index * 0.05
        end = (index + 1) * 0.05
        print(f"[{start:.2f} - {end:.2f}): {count:,} ({count / total * 100:.3f}%)")


def run(
    model_name: str,
    input_root: Path,
    files: list[Path],
    dest_file: Path,
    batch_size: int,
    threshold: float,
    summary_file: Path | None = None,
    snapshot_manifest: Path | None = None,
) -> None:
    # An old summary must not outlive a run that doesn't finish, so it goes first.
    if summary_file is not None:
        summary_file.unlink(missing_ok=True)

    if snapshot_manifest is not None:
        verify_snapshot(input_root, files, snapshot_manifest)

    accelerator = accelerate.Accelerator()
    stats: dict[str, Any] = {"rows_read": 0, "messages_scored": 0, "messages_flagged": 0, "per_file": {}}
    input_files: list[dict[str, str]] = []
    candidates = count_candidates(
        iter_candidates(input_root, files, input_files, stats["per_file"]), stats
    )
    model = Detoxify(model_name, device=accelerator.device)

    if accelerator.is_main_process:
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        output_handle = dest_file.open("w", encoding="utf-8")
        histogram = [0] * 20
        progress = tqdm(desc="Detoxify candidates", unit="message")

    completed = False
    try:
        accelerator.wait_for_everyone()
        while True:
            batch = list(islice(candidates, batch_size * accelerator.num_processes))
            if not batch:
                break
            texts = [candidate["text"] for candidate in batch]
            with accelerator.split_between_processes(texts, apply_padding=True) as local_texts:
                result = model.predict(local_texts, batch_size=batch_size)
                local_scores = normalize_predictions(result, len(local_texts))
            gathered_scores = gather_object(local_scores)

            if accelerator.is_main_process:
                scores = gathered_scores[: len(batch)]
                if len(scores) != len(batch):
                    raise RuntimeError(
                        f"Gathered {len(scores)} predictions for {len(batch)} candidates"
                    )
                for candidate, class_scores in zip(batch, scores):
                    max_category, max_score = max(class_scores.items(), key=lambda item: item[1])
                    histogram[min(int(max_score / 0.05), 19)] += 1
                    if max_score > threshold:
                        stats["messages_flagged"] += 1
                        stats["per_file"][candidate["source_file"]]["findings"] += 1
                        output = dict(candidate)
                        output["toxicity"] = max_score
                        output["trigger_category"] = max_category
                        output["toxicity_scores"] = class_scores
                        output_handle.write(json.dumps(output, ensure_ascii=False) + "\n")
                output_handle.flush()
                progress.update(len(batch))
        completed = True
    finally:
        if accelerator.is_main_process:
            output_handle.close()
            progress.close()
            print_histogram(histogram)
            # Only a run that read every file to the end gets a summary.
            if summary_file is not None and completed:
                write_summary_atomic(
                    summary_file,
                    {
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "source": "native",
                        "model_name": model_name,
                        "threshold": threshold,
                        "rows_read": stats["rows_read"],
                        "messages_scored": stats["messages_scored"],
                        "messages_flagged": stats["messages_flagged"],
                        "histogram": histogram_to_dict(histogram),
                        "per_file": stats["per_file"],
                        "input": input_block(str(input_root), input_files),
                        "guard_total_records": None,
                        "guard_errors": None,
                    },
                )
        accelerator.end_training()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--input-root", type=Path)
    parser.add_argument("--input-manifest", type=Path)
    parser.add_argument("--model_name", choices=["original", "unbiased", "multilingual"], default="multilingual")
    parser.add_argument("--dest_file", type=Path, required=True)
    parser.add_argument("--batch_size", type=int, default=32)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument(
        "--summary-file",
        type=Path,
        help="Optional: write a JSON summary (rows read, messages scored/"
        "flagged, score histogram, per-source-file counts) here after the "
        "run, so coverage can be checked without parsing logs.",
    )
    parser.add_argument(
        "--snapshot-manifest",
        type=Path,
        help="Optional: verify every input file's size and mtime against "
        "this manifest.json before scanning -- the same check "
        "pii_extractor.py runs before trusting a source file.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        input_root, files = resolve_inputs(args.input, args.input_root, args.input_manifest)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    run(
        model_name=args.model_name,
        input_root=input_root,
        files=files,
        dest_file=args.dest_file.expanduser().resolve(),
        batch_size=args.batch_size,
        threshold=args.threshold,
        summary_file=args.summary_file.expanduser().resolve() if args.summary_file else None,
        snapshot_manifest=(
            args.snapshot_manifest.expanduser().resolve() if args.snapshot_manifest else None
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
