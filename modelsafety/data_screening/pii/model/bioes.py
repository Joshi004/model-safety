from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class Span:
    label: str
    start: int
    end: int
    text: str
    confidence: float

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "confidence": self.confidence,
        }


def split_tag(label: str) -> tuple[str, str]:
    if label == "O":
        return "O", ""
    if "-" not in label:
        raise ValueError(f"Invalid BIOES label: {label}")
    prefix, category = label.split("-", 1)
    if prefix not in {"B", "I", "E", "S"} or not category:
        raise ValueError(f"Invalid BIOES label: {label}")
    return prefix, category


def viterbi_decode(scores: np.ndarray, labels: Sequence[str]) -> list[int]:
    """Decode BIOES labels with exact transition constraints in O(T*K)."""
    values = np.asarray(scores, dtype=np.float32)
    if values.ndim != 2 or values.shape[1] != len(labels):
        raise ValueError("scores must have shape [tokens, labels]")
    token_count, label_count = values.shape
    if token_count == 0:
        return []

    parsed = [split_tag(label) for label in labels]
    o_indices = [index for index, (prefix, _) in enumerate(parsed) if prefix == "O"]
    if len(o_indices) != 1:
        raise ValueError("BIOES vocabulary must contain exactly one O label")
    o_index = o_indices[0]
    by_category: dict[str, dict[str, int]] = {}
    for index, (prefix, category) in enumerate(parsed):
        if prefix == "O":
            continue
        by_category.setdefault(category, {})[prefix] = index
    for category, tags in by_category.items():
        if set(tags) != {"B", "I", "E", "S"}:
            raise ValueError(f"Incomplete BIOES labels for {category}")

    neg_inf = np.float32(-1e30)
    previous = np.full(label_count, neg_inf, dtype=np.float32)
    backpointers = np.full((token_count, label_count), -1, dtype=np.int32)
    previous[o_index] = values[0, o_index]
    b_indices = np.asarray([tags["B"] for tags in by_category.values()])
    i_indices = np.asarray([tags["I"] for tags in by_category.values()])
    e_indices = np.asarray([tags["E"] for tags in by_category.values()])
    s_indices = np.asarray([tags["S"] for tags in by_category.values()])
    previous[b_indices] = values[0, b_indices]
    previous[s_indices] = values[0, s_indices]

    closed_indices = np.concatenate(
        (np.asarray([o_index]), e_indices, s_indices)
    )

    for token_index in range(1, token_count):
        current = np.full(label_count, neg_inf, dtype=np.float32)
        closed_scores = previous[closed_indices]
        closed_source = int(closed_indices[int(closed_scores.argmax())])
        closed_score = previous[closed_source]

        current[o_index] = closed_score + values[token_index, o_index]
        backpointers[token_index, o_index] = closed_source
        current[b_indices] = closed_score + values[token_index, b_indices]
        current[s_indices] = closed_score + values[token_index, s_indices]
        backpointers[token_index, b_indices] = closed_source
        backpointers[token_index, s_indices] = closed_source

        choose_b = previous[b_indices] >= previous[i_indices]
        open_sources = np.where(choose_b, b_indices, i_indices)
        open_scores = np.maximum(previous[b_indices], previous[i_indices])
        current[i_indices] = open_scores + values[token_index, i_indices]
        current[e_indices] = open_scores + values[token_index, e_indices]
        backpointers[token_index, i_indices] = open_sources
        backpointers[token_index, e_indices] = open_sources
        previous = current

    final_index = int(closed_indices[int(previous[closed_indices].argmax())])
    path = [final_index]
    for token_index in range(token_count - 1, 0, -1):
        final_index = int(backpointers[token_index, final_index])
        if final_index < 0:
            raise RuntimeError("BIOES decoder produced a broken path")
        path.append(final_index)
    path.reverse()
    return path


def spans_from_path(
    text: str,
    offsets: Sequence[tuple[int, int]],
    probabilities: np.ndarray,
    labels: Sequence[str],
    path: Sequence[int],
) -> list[Span]:
    if len(offsets) != len(path):
        raise ValueError("offset and label path lengths differ")
    spans: list[Span] = []

    def trimmed(start: int, end: int) -> tuple[int, int]:
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        return start, end

    index = 0
    while index < len(path):
        prefix, category = split_tag(labels[path[index]])
        if prefix == "O":
            index += 1
            continue
        if prefix == "S":
            start, end = offsets[index]
            start, end = trimmed(start, end)
            if end > start:
                spans.append(
                    Span(
                        category,
                        start,
                        end,
                        text[start:end],
                        float(probabilities[index, path[index]]),
                    )
                )
            index += 1
            continue
        if prefix != "B":
            raise RuntimeError(f"Constrained path unexpectedly begins with {prefix}")

        end_index = index + 1
        while end_index < len(path):
            end_prefix, end_category = split_tag(labels[path[end_index]])
            if end_category != category:
                break
            if end_prefix == "E":
                break
            end_index += 1
        if end_index >= len(path) or split_tag(labels[path[end_index]]) != ("E", category):
            raise RuntimeError("Constrained path contains an unterminated span")
        start = offsets[index][0]
        end = offsets[end_index][1]
        start, end = trimmed(start, end)
        if end > start:
            selected = [
                probabilities[pos, path[pos]] for pos in range(index, end_index + 1)
            ]
            spans.append(
                Span(category, start, end, text[start:end], float(np.mean(selected)))
            )
        index = end_index + 1
    return spans


def merge_window_spans(spans: Sequence[Span]) -> list[Span]:
    """Deduplicate high-overlap predictions emitted by overlapping windows."""
    merged: list[Span] = []
    for candidate in sorted(
        spans,
        key=lambda span: (span.start, span.end, span.label, -span.confidence),
    ):
        replacement_index = None
        for index in range(len(merged) - 1, -1, -1):
            existing = merged[index]
            if existing.end <= candidate.start:
                break
            if existing.label != candidate.label:
                continue
            intersection = max(
                0,
                min(existing.end, candidate.end) - max(existing.start, candidate.start),
            )
            shorter = min(existing.end - existing.start, candidate.end - candidate.start)
            if shorter and intersection / shorter >= 0.5:
                replacement_index = index
                break
        if replacement_index is None:
            merged.append(candidate)
        elif candidate.confidence > merged[replacement_index].confidence:
            merged[replacement_index] = candidate
    return sorted(merged, key=lambda span: (span.start, span.end, span.label))

