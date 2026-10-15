"""Which text of a row the duplicates scan compares.

Standard library only, so other code can ask the same question the scan asked
without importing datasketch. `run.py` uses this too, so the two can't
disagree.
"""

from __future__ import annotations

import re
import string
from typing import Any


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


def extract_text(record: dict[str, Any], text_scope: str, exclude_system: bool = False) -> str:
    """Extract the content used for deduplication.

    ``prompt`` follows the intent of the source implementation: when the final
    message is an assistant response, it is excluded so repeated prompts with
    different generated answers still deduplicate.

    ``exclude_system`` drops every ``system``-role message before that rule is
    applied. System messages typically carry tool/function definitions rather
    than conversation content, and comparing them can make otherwise-unrelated
    rows look similar just because they share the same (or a similarly shaped)
    tool schema. Off by default: existing runs are unaffected unless this is
    explicitly requested.
    """
    text = record.get("text")
    if isinstance(text, str):
        return text

    messages = record.get("messages")
    if not isinstance(messages, list):
        return ""
    usable = [message for message in messages if isinstance(message, dict)]
    if exclude_system:
        usable = [
            message for message in usable if str(message.get("role") or "").lower() != "system"
        ]
    if text_scope == "prompt" and len(usable) > 1:
        last_role = str(usable[-1].get("role") or "").lower()
        if last_role == "assistant":
            usable = usable[:-1]
    parts = [content_text(message.get("content")) for message in usable]
    return "\n".join(part for part in parts if part)


def normalize_text(text: str) -> str:
    lowered = text.lower().translate(str.maketrans("", "", string.punctuation))
    return re.sub(r"\s+", " ", lowered).strip()


def row_has_text(record: dict[str, Any], text_scope: str, exclude_system: bool = False) -> bool:
    """Whether the scan has anything to compare in this row."""
    return bool(normalize_text(extract_text(record, text_scope, exclude_system)))
