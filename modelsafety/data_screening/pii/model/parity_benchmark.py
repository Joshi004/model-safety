#!/usr/bin/env python3
"""Compare the custom batched decoder with the OpenMed reference decoder."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from backends import DEFAULT_MODEL, TransformersBackend, _make_windows
from bioes import merge_window_spans, spans_from_path, viterbi_decode
from common import write_json_atomic


FIXTURES = [
    "Patient Sarah Johnson (DOB 03/15/1985), MRN 4872910, phone "
    "415-555-0123, email sarah.johnson@example.com.",
    "Paciente María García, DNI 12345678Z, vive en Bogotá.",
    "联系患者李明：liming@example.cn，电话 +86 10 5555 0123。",
    "IPv6 2001:db8:85a3::8a2e:370:7334 belongs to device 00:1B:44:11:3A:B7.",
    "The hospital's public website is https://hospital.example.org.",
]


def span_key(span) -> tuple[str, int, int, str]:
    return span.label, span.start, span.end, span.text


def overlap_matches(candidate_spans, reference_spans, minimum_overlap: float) -> int:
    pairs = []
    for candidate_index, candidate in enumerate(candidate_spans):
        for reference_index, reference in enumerate(reference_spans):
            if candidate.label != reference.label:
                continue
            intersection = max(
                0,
                min(candidate.end, reference.end) - max(candidate.start, reference.start),
            )
            union = max(candidate.end, reference.end) - min(
                candidate.start,
                reference.start,
            )
            overlap = intersection / union if union else 1.0
            if overlap >= minimum_overlap:
                pairs.append((overlap, candidate_index, reference_index))
    used_candidates = set()
    used_references = set()
    for _, candidate_index, reference_index in sorted(pairs, reverse=True):
        if candidate_index in used_candidates or reference_index in used_references:
            continue
        used_candidates.add(candidate_index)
        used_references.add(reference_index)
    return len(used_candidates)


def f1_score(matches: int, candidate_count: int, reference_count: int) -> tuple[float, float, float]:
    precision = matches / candidate_count if candidate_count else float(reference_count == 0)
    recall = matches / reference_count if reference_count else float(candidate_count == 0)
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return precision, recall, f1


def benchmark(args: argparse.Namespace) -> dict[str, object]:
    from openmed.core.decoding import (
        TokenLabelInfo,
        viterbi_decode as openmed_viterbi_decode,
        zero_viterbi_biases,
    )

    texts = list(FIXTURES)
    if args.fixture_jsonl:
        with args.fixture_jsonl.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    value = json.loads(line)
                    texts.append(str(value["text"]))
    texts.append(
        ("No private identifiers appear in this clinical discussion. " * 600)
        + "Contact long.context@example.org near the end."
    )

    started = time.monotonic()
    candidate = TransformersBackend(
        args.model,
        batch_size=args.batch_size,
        max_tokens=args.max_tokens,
        overlap_tokens=args.overlap_tokens,
    )
    load_seconds = time.monotonic() - started
    windows = _make_windows(
        candidate.tokenizer,
        texts,
        args.max_tokens,
        args.overlap_tokens,
    )
    windows.sort(key=lambda item: len(item.input_ids))
    candidate_outputs = [[] for _ in texts]
    reference_outputs = [[] for _ in texts]
    token_count = 0
    path_mismatches = 0
    label_info = TokenLabelInfo(candidate.labels)
    biases = zero_viterbi_biases()

    started = time.monotonic()
    torch = candidate.torch
    with torch.inference_mode():
        for start in range(0, len(windows), args.batch_size):
            batch = windows[start : start + args.batch_size]
            max_length = max(len(window.input_ids) for window in batch)
            input_ids = []
            attention_masks = []
            for window in batch:
                pad = max_length - len(window.input_ids)
                input_ids.append(
                    window.input_ids + [candidate.tokenizer.pad_token_id] * pad
                )
                attention_masks.append([1] * len(window.input_ids) + [0] * pad)
            logits = candidate.model(
                input_ids=torch.tensor(input_ids, dtype=torch.long, device=candidate.device),
                attention_mask=torch.tensor(
                    attention_masks,
                    dtype=torch.long,
                    device=candidate.device,
                ),
            ).logits.float().cpu().numpy()
            for window, scores in zip(batch, logits):
                length = len(window.input_ids)
                values = scores[:length]
                shifted = values - values.max(axis=-1, keepdims=True)
                exp_values = np.exp(shifted)
                probabilities = exp_values / exp_values.sum(axis=-1, keepdims=True)
                log_scores = shifted - np.log(
                    exp_values.sum(axis=-1, keepdims=True)
                )
                candidate_path = viterbi_decode(log_scores, candidate.labels)
                reference_path = openmed_viterbi_decode(
                    log_scores.tolist(),
                    label_info=label_info,
                    biases=biases,
                )
                token_count += length
                path_mismatches += sum(
                    candidate_label != reference_label
                    for candidate_label, reference_label in zip(
                        candidate_path,
                        reference_path,
                    )
                )
                candidate_outputs[window.owner].extend(
                    spans_from_path(
                        texts[window.owner],
                        window.offsets,
                        probabilities,
                        candidate.labels,
                        candidate_path,
                    )
                )
                reference_outputs[window.owner].extend(
                    spans_from_path(
                        texts[window.owner],
                        window.offsets,
                        probabilities,
                        candidate.labels,
                        reference_path,
                    )
                )
    inference_seconds = time.monotonic() - started
    candidate_outputs = [merge_window_spans(spans) for spans in candidate_outputs]
    reference_outputs = [merge_window_spans(spans) for spans in reference_outputs]

    exact_matches = 0
    tolerant_matches = 0
    candidate_count = 0
    reference_count = 0
    comparisons = []
    for text, candidate_spans, reference_spans in zip(
        texts,
        candidate_outputs,
        reference_outputs,
    ):
        candidate_keys = {span_key(span) for span in candidate_spans}
        reference_keys = {span_key(span) for span in reference_spans}
        exact_matches += len(candidate_keys & reference_keys)
        tolerant_matches += overlap_matches(
            candidate_spans,
            reference_spans,
            args.minimum_overlap,
        )
        candidate_count += len(candidate_keys)
        reference_count += len(reference_keys)
        comparisons.append(
            {
                "text_prefix": text[:200],
                "candidate": [span.to_dict() for span in candidate_spans],
                "reference": [span.to_dict() for span in reference_spans],
                "candidate_only": sorted(candidate_keys - reference_keys),
                "reference_only": sorted(reference_keys - candidate_keys),
            }
        )
    exact_precision, exact_recall, exact_f1 = f1_score(
        exact_matches,
        candidate_count,
        reference_count,
    )
    precision, recall, f1 = f1_score(
        tolerant_matches,
        candidate_count,
        reference_count,
    )
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "fixture_count": len(texts),
        "candidate_backend": "transformers_constrained_bioes",
        "reference_backend": "openmed_core_decoding_viterbi",
        "token_count": token_count,
        "token_path_mismatches": path_mismatches,
        "token_path_match_rate": (
            (token_count - path_mismatches) / token_count if token_count else 1.0
        ),
        "candidate_spans": candidate_count,
        "reference_spans": reference_count,
        "exact_span_matches": exact_matches,
        "exact_span_precision": exact_precision,
        "exact_span_recall": exact_recall,
        "exact_span_f1": exact_f1,
        "tolerant_span_matches": tolerant_matches,
        "tolerant_span_precision": precision,
        "tolerant_span_recall": recall,
        "tolerant_span_f1": f1,
        "minimum_character_overlap": args.minimum_overlap,
        "minimum_required_f1": args.minimum_f1,
        "passed": f1 >= args.minimum_f1 and path_mismatches == 0,
        "model_load_seconds": load_seconds,
        "inference_and_decode_seconds": inference_seconds,
        "comparisons": comparisons,
    }
    write_json_atomic(args.output, summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture-jsonl", type=Path)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--overlap-tokens", type=int, default=256)
    parser.add_argument("--minimum-f1", type=float, default=0.95)
    parser.add_argument("--minimum-overlap", type=float, default=0.8)
    parser.add_argument("--allow-failure", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        summary = benchmark(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        f"Parity F1={summary['tolerant_span_f1']:.4f} "
        f"({summary['tolerant_span_matches']}/{summary['reference_spans']} reference spans)"
    )
    return 0 if summary["passed"] or args.allow_failure else 1


if __name__ == "__main__":
    raise SystemExit(main())

