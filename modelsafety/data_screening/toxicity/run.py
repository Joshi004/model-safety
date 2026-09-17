#!/usr/bin/env python3
"""Stage-1 Detoxify screen with source-level traceability and manifest support."""

from __future__ import annotations

import argparse
import json
from itertools import islice
from pathlib import Path
from typing import Any, Iterator

import accelerate
from accelerate.utils import gather_object
from tqdm import tqdm

from detoxify import Detoxify


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


def iter_candidates(input_root: Path, files: list[Path]) -> Iterator[dict[str, Any]]:
    """Yield one Detoxify candidate per plain text or conversation message."""
    for path in files:
        relative = path.relative_to(input_root).as_posix()
        with path.open("r", encoding="utf-8") as handle:
            for source_line, line in enumerate(handle, 1):
                try:
                    source_record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Malformed JSON at {path}:{source_line}") from exc

                text = source_record.get("text")
                if isinstance(text, str):
                    yield {
                        "source_file": relative,
                        "source_line": source_line,
                        "message_index": None,
                        "role": "user",
                        "text": text,
                        "source_record": source_record,
                    }
                    continue

                messages = source_record.get("messages")
                if not isinstance(messages, list):
                    continue
                for message_index, message in enumerate(messages):
                    if not isinstance(message, dict):
                        continue
                    role = str(message.get("role") or "")
                    content = content_text(message.get("content"))
                    if not content:
                        continue
                    yield {
                        "source_file": relative,
                        "source_line": source_line,
                        "message_index": message_index,
                        "role": role,
                        "text": f"{role}: {content}",
                        "source_record": source_record,
                    }


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
) -> None:
    accelerator = accelerate.Accelerator()
    candidates = iter_candidates(input_root, files)
    model = Detoxify(model_name, device=accelerator.device)

    if accelerator.is_main_process:
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        output_handle = dest_file.open("w", encoding="utf-8")
        histogram = [0] * 20
        progress = tqdm(desc="Detoxify candidates", unit="message")

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
                        output = dict(candidate)
                        output["toxicity"] = max_score
                        output["trigger_category"] = max_category
                        output["toxicity_scores"] = class_scores
                        output_handle.write(json.dumps(output, ensure_ascii=False) + "\n")
                output_handle.flush()
                progress.update(len(batch))
    finally:
        if accelerator.is_main_process:
            output_handle.close()
            progress.close()
            print_histogram(histogram)
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
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
