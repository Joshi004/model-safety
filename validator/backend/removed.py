"""The Removed page: browse the rows a filter step took out of one dataset of a
chain, by reason, NER label, regex category and toxicity verdict, a page at a time.

Everything here is read-only. It wraps `read_removed_page` (modelsafety/trace/
row_status_read.py), which pages the removed-rows index the row status builds
(`<run>/removed/step<N>.ndjson`, one short line per removed row) instead of the
filter run's `removed_rows.ndjson`, which can be over a gigabyte. The row status's
summary, which has the counts each filter offers, is kept in memory by
`rows.status_summary` until the file changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import rows
from reader_imports import RowStatusReadError, read_removed_page

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
# A filter is one name (a label, a category, a verdict); no real one is longer.
MAX_FILTER_CHARS = 100

COUNT_FIELDS = ("by_reason", "by_ner_label", "by_regex_category", "by_toxicity_verdict")


class RemovedError(ValueError):
    """The page can't be shown; the message says why."""


def _count_dict(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {name: count for name, count in value.items() if isinstance(count, int) and not isinstance(count, bool)}


def removed_options(chain_dir: Path) -> dict[str, Any]:
    """What the page can filter by: for each dataset and each filter step, how many
    rows were removed and the counts by reason, NER label, regex category and toxicity
    verdict (what the index holds), plus where the clean data is."""
    try:
        ctx = rows.chain_context(chain_dir)
    except rows.RowsError as exc:
        raise RemovedError(str(exc)) from exc
    runs: list[dict[str, Any]] = []
    for run, entry in sorted(ctx.summary["runs"].items()):
        index = entry.get("removed_index") if isinstance(entry, dict) else None
        steps: dict[str, Any] = {}
        if isinstance(index, dict):
            for step, item in index.items():
                if isinstance(item, dict):
                    steps[step] = {"entries": item.get("entries"), **{name: _count_dict(item.get(name)) for name in COUNT_FIELDS}}
        row_counts = entry.get("rows") if isinstance(entry, dict) else None
        runs.append({"run": run, "rows": row_counts if isinstance(row_counts, dict) else None, "steps": steps})
    return {
        "chain_dir": str(chain_dir),
        "steps": [step["number"] for step in ctx.steps],
        # The last filter step's output folder: the clean data, one run per dataset.
        "clean_root": ctx.steps[-1]["output_root"] if ctx.steps else None,
        "has_removed_index": any(item["steps"] for item in runs),
        "runs": runs,
    }


def _filter(name: str, value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > MAX_FILTER_CHARS:
        raise RemovedError(f"The {name} filter must be a name of at most {MAX_FILTER_CHARS} characters.")
    return value


def removed_page(
    chain_dir: Path,
    run: str,
    step: int,
    reason: str | None,
    ner_label: str | None,
    regex_category: str | None,
    toxicity_verdict: str | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """One page of the rows `step` removed from `run`. A row has to match every
    filter given. Each entry says where the row is in the Prepared data (`run`, `file`,
    `line`), why it was removed, what was found, and where it came from."""
    try:
        ctx = rows.chain_context(chain_dir)
        return read_removed_page(
            ctx.status_dir,
            run,
            step,
            reason=_filter("reason", reason),
            ner_label=_filter("NER label", ner_label),
            regex_category=_filter("regex category", regex_category),
            toxicity_verdict=_filter("toxicity verdict", toxicity_verdict),
            page=page,
            page_size=min(page_size, MAX_PAGE_SIZE),
            summary=ctx.summary,
        )
    except (rows.RowsError, RowStatusReadError) as exc:
        raise RemovedError(str(exc)) from exc
