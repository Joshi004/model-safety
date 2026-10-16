"""A word-by-word comparison of a duplicate row with the copy that was kept.

The duplicates scan says only that two rows are alike; this shows where. It reads the texts the
row view already has (a list of segments, as flagged.py makes them) and marks, on each side, the
words that are in both rows ("same") and the words that are only in that row ("different").

Only the messages the scan compared are looked at (the caller passes their numbers), so a final
assistant reply or a system message the scan left out never makes a real duplicate look unlike.

This is a plain word diff (difflib), not the scan's 60-character MinHash, so the share it gives
is a guide to how alike the rows are, not the scan's own score. Words are matched ignoring case,
which roughly matches how the scan prepares text. Nothing is written, and nothing here reads files.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, NamedTuple

# The most words compared on one side. A longer text isn't compared: the diff's cost grows
# faster than its length, and a reviewer can't read a mark on every word of it anyway.
MAX_DIFF_WORDS = 10_000

_WORD = re.compile(r"\w+")


class _Word(NamedTuple):
    message_index: int | None
    start: int
    end: int
    key: str


def _words(segments: list[dict[str, Any]], compared: list[int | None]) -> list[_Word]:
    """The words of the compared segments, in order, with where each is in its segment's text."""
    wanted = set(compared)
    words: list[_Word] = []
    for segment in segments:
        if segment["message_index"] not in wanted:
            continue
        for match in _WORD.finditer(segment["text"]):
            words.append(_Word(segment["message_index"], match.start(), match.end(), match.group().lower()))
    return words


def _marks(words: list[_Word], kinds: list[str]) -> list[dict[str, Any]]:
    """One mark for each run of neighbouring words in a message that have the same kind. A run
    covers the space between its words, so the mark reads as one piece of text."""
    marks: list[dict[str, Any]] = []
    for word, kind in zip(words, kinds):
        last = marks[-1] if marks else None
        if last is not None and last["kind"] == kind and last["message_index"] == word.message_index:
            last["end"] = word.end
        else:
            marks.append(
                {
                    "scanner": "dedup",
                    "kind": kind,
                    "message_index": word.message_index,
                    "start": word.start,
                    "end": word.end,
                }
            )
    return marks


def compare(
    row_segments: list[dict[str, Any]],
    kept_segments: list[dict[str, Any]],
    row_compared: list[int | None],
    kept_compared: list[int | None],
    max_marks: int,
) -> dict[str, Any]:
    """Compare the compared words of the flagged row with those of the kept copy.

    Returns `row_highlights` and `kept_highlights` (marks with `kind` "same" or "different", in
    the shape of the other highlights), `same_share` (the part of the words that are the same, 0
    to 1, counted over both rows), `cut` (some marks were left out, at most `max_marks` a side
    are kept) and `problem` (why nothing was compared, or None).
    """
    result: dict[str, Any] = {
        "row_highlights": [],
        "kept_highlights": [],
        "same_share": None,
        "cut": False,
        "problem": None,
    }
    row_words = _words(row_segments, row_compared)
    kept_words = _words(kept_segments, kept_compared)
    if not row_words or not kept_words:
        result["problem"] = "One of the rows has no words in the part the scan compared, so there is nothing to compare."
        return result
    if len(row_words) > MAX_DIFF_WORDS or len(kept_words) > MAX_DIFF_WORDS:
        result["problem"] = f"The rows are too long to compare word by word (more than {MAX_DIFF_WORDS:,} words)."
        return result

    matcher = SequenceMatcher(None, [word.key for word in row_words], [word.key for word in kept_words], autojunk=False)
    row_kinds = ["different"] * len(row_words)
    kept_kinds = ["different"] * len(kept_words)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            row_kinds[block.a + offset] = "same"
            kept_kinds[block.b + offset] = "same"

    row_marks = _marks(row_words, row_kinds)
    kept_marks = _marks(kept_words, kept_kinds)
    result["cut"] = len(row_marks) > max_marks or len(kept_marks) > max_marks
    result["row_highlights"] = row_marks[:max_marks]
    result["kept_highlights"] = kept_marks[:max_marks]
    result["same_share"] = matcher.ratio()
    return result
