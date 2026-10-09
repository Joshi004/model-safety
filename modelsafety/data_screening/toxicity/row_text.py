"""Which text of a row the toxicity scan scores.

Standard library only, so the per-row status can ask the same question the
scan asked without importing Detoxify or accelerate. `run.py` uses this too,
so the two can't disagree.
"""

from __future__ import annotations

from typing import Any, Iterator


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


def iter_row_texts(record: dict[str, Any]) -> Iterator[tuple[int | None, str, str]]:
    """Each text of a row that Detoxify scores, as ``(message_index, role, text)``.

    A plain ``text`` string is one text (even an empty one), and then
    ``messages`` aren't read. Otherwise each message with some text is one,
    and ``text`` is ``"<role>: <content>"``. A row that yields nothing has no
    text for this scan.
    """
    text = record.get("text")
    if isinstance(text, str):
        yield None, "user", text
        return

    messages = record.get("messages")
    if not isinstance(messages, list):
        return
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        content = content_text(message.get("content"))
        if not content:
            continue
        yield message_index, role, f"{role}: {content}"


def row_has_text(record: dict[str, Any]) -> bool:
    return next(iter_row_texts(record), None) is not None
