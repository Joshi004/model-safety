"""The story of one row: where it came from, what each scanner found in it, and
whether it was removed (and why) or where it is in the clean data.

Everything here is read-only. A row is named by its place in the Prepared data: the
run (its path under the chain's Prepared folder), the file in it, and the line. Its
status entry comes from the chain's row status (`row_status_read.read_row`, one
read of an index and one of the status file). The Prepared line itself is read at
the byte offset the status gives, and only from a file its Prepared run's
`summary.json` lists, and it is compared with the fingerprint the status holds, so
the text shown is the text that was scanned.

From a clean row, `row_from_clean` walks the line maps of the filter steps back to
the Prepared line. From an Original row, `row_from_original` asks the Prepared runs
of the chain (modelsafety/trace/find_prepared.py) where it went.

Highlights are worked out here, not in the browser: NER spans by message and
character range (each one checked to lie inside its message's text), regex PII by
searching for the matched text (it keeps no positions), and toxicity by message.
A finding that can't be placed is listed in `unplaced` with the reason, not guessed at.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

import runs
from reader_imports import (
    FILTER_ROWS_TOOL,
    ORIGIN_KEY,
    RowStatusReadError,
    content_text,
    line_fingerprint,
    read_chain,
    read_json_or_none,
    read_row,
    read_row_status_summary,
)

# The longest text of one message that is sent to the browser.
MAX_SEGMENT_CHARS = 200_000
# The most highlights and unplaced findings sent for one row.
MAX_HIGHLIGHTS = 2000
# The most places one regex PII value is marked in one message.
MAX_MATCHES_PER_VALUE = 50
# The most rows listed as duplicates of one kept row, and the most kept copies shown for a duplicate.
MAX_DUPLICATED_BY = 50
MAX_COPIES = 3
# How many steps a line map walk can take: no real chain is this long.
MAX_STEPS = 20

LINE_MAP_FOLDER = "line_map"
STATUS_REMOVED = "removed"


class RowsError(ValueError):
    """The row can't be shown; the message says why."""


class _Context(NamedTuple):
    chain_dir: Path
    steps: list[dict[str, Any]]
    prepared_root: Path
    status_dir: Path
    summary: dict[str, Any]


# -------------------------------------------------------------------------
# The row status summary, read once and kept until the file changes
# -------------------------------------------------------------------------

_SUMMARY_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
_SUMMARY_LOCK = threading.Lock()


def status_summary(status_dir: Path) -> dict[str, Any]:
    """The summary of a finished row status folder. It is a few hundred KB, and every
    row and page needs it, so it is kept in memory and read again when the file changes.
    Raises `RowStatusReadError` if the folder isn't a finished row status."""
    summary_file = status_dir / "summary.json"
    # read_row_status_summary also checks `_SUCCESS`, so call it whenever nothing is cached.
    try:
        stat = summary_file.stat()
    except OSError:
        return read_row_status_summary(status_dir)
    key = (stat.st_mtime_ns, stat.st_size)
    with _SUMMARY_LOCK:
        cached = _SUMMARY_CACHE.get(str(status_dir))
    if cached is not None and cached[0] == key and (status_dir / "_SUCCESS").is_file():
        return cached[1]
    summary = read_row_status_summary(status_dir)
    with _SUMMARY_LOCK:
        _SUMMARY_CACHE[str(status_dir)] = (key, summary)
    return summary


def chain_context(chain_dir: Path) -> _Context:
    """What reading a chain's rows needs: its steps and its finished row status."""
    record = read_chain(chain_dir)
    if record["name"] is None:
        raise RowsError(record["problems"][0] if record["problems"] else f"{chain_dir} can't be read.")
    status = record["row_status"]
    if status is None:
        raise RowsError("This chain has no row status folder, so its rows can't be followed.")
    if not status["finished"]:
        raise RowsError("This chain's row status isn't finished, so its rows can't be followed yet.")
    try:
        summary = status_summary(Path(status["dir"]))
    except RowStatusReadError as exc:
        raise RowsError(str(exc)) from exc
    return _Context(
        chain_dir=chain_dir,
        steps=sorted(record["steps"], key=lambda step: step["number"]),
        prepared_root=Path(record["prepared_root"]),
        status_dir=Path(status["dir"]),
        summary=summary,
    )


# -------------------------------------------------------------------------
# The Prepared line
# -------------------------------------------------------------------------


def _inside_run(relative: Any) -> PurePosixPath:
    pure = PurePosixPath(relative) if isinstance(relative, str) and relative else None
    if pure is None or pure.is_absolute() or ".." in pure.parts:
        raise RowsError(f"{relative!r} isn't a path inside a run.")
    return pure


def _listed_files(run_dir: Path) -> set[str] | None:
    """The files a run's `summary.json` lists in `output.files_written`, or None if it lists none."""
    summary = read_json_or_none(run_dir / "summary.json")
    output = summary.get("output") if isinstance(summary, dict) else None
    written = output.get("files_written") if isinstance(output, dict) else None
    if not isinstance(written, list):
        return None
    return {item["path"] for item in written if isinstance(item, dict) and isinstance(item.get("path"), str)}


def _segments(record: Any) -> list[dict[str, Any]]:
    """The texts a scanner reads from a row: its `text`, or each message's text, cut
    the way the scans cut them. A message with no text is kept (empty), so the
    message numbers in findings still line up."""
    if not isinstance(record, dict):
        return []
    text = record.get("text")
    if isinstance(text, str) and text:
        pieces = [(None, "text", text)]
    else:
        messages = record.get("messages")
        pieces = []
        if isinstance(messages, list):
            for index, message in enumerate(messages):
                if isinstance(message, dict):
                    pieces.append((index, str(message.get("role") or ""), content_text(message.get("content"))))
    return [
        {
            "message_index": index,
            "role": role,
            "text": text[:MAX_SEGMENT_CHARS],
            "length": len(text),
            "truncated": len(text) > MAX_SEGMENT_CHARS,
        }
        for index, role, text in pieces
    ]


def _read_prepared(ctx: _Context, entry: dict[str, Any]) -> dict[str, Any]:
    """The Prepared line a status entry stands for: the run folder, the line read at
    its byte offset, and whether it still has the fingerprint the status holds. A
    problem is reported in `problem`, with no row, rather than raised."""
    prepared = entry["prepared"]
    run_entry = ctx.summary["runs"].get(prepared["run"])
    run_dir_text = run_entry.get("prepared_run") if isinstance(run_entry, dict) else None
    result: dict[str, Any] = {
        "run": prepared["run"],
        "file": prepared["file"],
        "line": prepared["line"],
        "run_dir": run_dir_text,
        "fingerprint_ok": None,
        "record": None,
        "segments": None,
        "problem": None,
    }
    if not isinstance(run_dir_text, str):
        result["problem"] = "The row status doesn't say where this row's Prepared run is."
        return result
    run_dir = Path(run_dir_text)
    try:
        relative = _inside_run(prepared["file"])
        listed = _listed_files(run_dir)
        if listed is None or str(relative) not in listed:
            result["problem"] = f"{run_dir}'s summary.json doesn't list {relative}, so the line wasn't read."
            return result
        with (run_dir / relative).open("rb") as handle:
            handle.seek(prepared["byte_offset"])
            raw = handle.readline()
    except (OSError, RowsError, TypeError) as exc:
        result["problem"] = f"The Prepared line can't be read: {exc}"
        return result
    result["fingerprint_ok"] = line_fingerprint(raw) == prepared["source_sha256"]
    if not result["fingerprint_ok"]:
        result["problem"] = (
            "The line at this place in the Prepared file isn't the one that was scanned "
            "(its fingerprint differs), so it isn't shown."
        )
        return result
    try:
        record = json.loads(raw)
    except ValueError as exc:
        result["problem"] = f"The Prepared line isn't JSON: {exc}"
        return result
    result["record"] = record
    result["segments"] = _segments(record)
    return result


# -------------------------------------------------------------------------
# What happened to a row
# -------------------------------------------------------------------------


def summarize(entry: dict[str, Any]) -> dict[str, Any]:
    """A row's fate in a few fields: kept (and where in the clean data) or removed (at
    which step, for which reasons), and what each step did."""
    final = entry.get("final") if isinstance(entry.get("final"), dict) else {}
    steps = [item for item in entry.get("steps", []) if isinstance(item, dict)]
    removing = next((item for item in steps if item.get("outcome") == STATUS_REMOVED), None)
    reasons = [reason.get("reason") for reason in (removing or {}).get("reasons", []) if isinstance(reason, dict)]
    return {
        "status": final.get("status"),
        "removed_at_step": final.get("step") if final.get("status") == STATUS_REMOVED else None,
        "reasons": reasons,
        "clean": (
            {key: final.get(key) for key in ("run", "file", "line", "byte_offset")}
            if final.get("status") == "kept"
            else None
        ),
        "step_outcomes": [{"step": item.get("step"), "outcome": item.get("outcome")} for item in steps],
    }


def _removal_keys(entry: dict[str, Any]) -> dict[int, dict[str, set[Any]]]:
    """For each step that removed the row, the findings its reasons name: NER spans by
    (message, start, end, label), regex PII by (category, value), toxicity by message."""
    keys: dict[int, dict[str, set[Any]]] = {}
    for step in entry.get("steps", []):
        if not isinstance(step, dict) or step.get("outcome") != STATUS_REMOVED:
            continue
        found: dict[str, set[Any]] = {"ner": set(), "regex_pii": set(), "toxicity": set()}
        for reason in step.get("reasons", []):
            if not isinstance(reason, dict):
                continue
            for finding in reason.get("findings", []):
                if not isinstance(finding, dict):
                    continue
                if reason.get("scanner") == "ner":
                    found["ner"].add((finding.get("message_index"), finding.get("start"), finding.get("end"), finding.get("label")))
                elif reason.get("scanner") == "regex_pii":
                    found["regex_pii"].add((finding.get("category"), finding.get("value")))
                elif reason.get("scanner") == "toxicity":
                    found["toxicity"].add(finding.get("message_index"))
        keys[step["step"]] = found
    return keys


def _is_position(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _ner_highlight(
    step: int, finding: dict[str, Any], by_index: dict[Any, dict[str, Any]], caused: set[Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The highlight for one NER span (None when it can't be placed) and its description."""
    item = {
        "step": step,
        "scanner": "ner",
        "label": finding.get("label"),
        "tier": finding.get("tier"),
        "eligible": finding.get("eligible"),
        "confidence": finding.get("confidence"),
        "message_index": finding.get("message_index"),
        "start": finding.get("start"),
        "end": finding.get("end"),
        "caused_removal": (finding.get("message_index"), finding.get("start"), finding.get("end"), finding.get("label")) in caused,
    }
    segment = by_index.get(finding.get("message_index"))
    if segment is None:
        return None, {**item, "why": "the span names a message the row doesn't have"}
    start, end = finding.get("start"), finding.get("end")
    if not (_is_position(start) and _is_position(end) and start < end <= segment["length"]):
        return None, {**item, "why": "the span lies outside the message's text"}
    if end > len(segment["text"]):
        return None, {**item, "why": "the span is past the part of the message that is shown"}
    return {**item, "matched": segment["text"][start:end]}, item


def _regex_highlights(
    step: int, finding: dict[str, Any], segments: list[dict[str, Any]], caused: set[Any]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Every place a regex PII value is in the row's text (it keeps no positions)."""
    value = finding.get("value")
    base = {
        "step": step,
        "scanner": "regex_pii",
        "category": finding.get("category"),
        "value": value,
        "caused_removal": (finding.get("category"), value) in caused,
    }
    if not isinstance(value, str) or not value:
        return [], {**base, "why": "the finding has no matched text to look for"}
    found: list[dict[str, Any]] = []
    for segment in segments:
        text = segment["text"]
        at = text.find(value)
        count = 0
        while at != -1 and count < MAX_MATCHES_PER_VALUE:
            found.append({**base, "message_index": segment["message_index"], "start": at, "end": at + len(value)})
            count += 1
            at = text.find(value, at + len(value))
    if not found:
        return [], {**base, "why": "the matched text isn't in the row's text (the scan read the whole line, so it may be part of the row's JSON)"}
    return found, None


def _findings(
    entry: dict[str, Any], segments: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], bool]:
    """The row's findings placed in its text: highlights (NER and regex PII spans),
    flags for whole messages (toxicity), the findings that couldn't be placed, and
    whether the list was cut short."""
    by_index = {segment["message_index"]: segment for segment in segments}
    removal = _removal_keys(entry)
    highlights: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []
    unplaced: list[dict[str, Any]] = []
    for step in entry.get("steps", []):
        if not isinstance(step, dict) or not isinstance(step.get("scanners"), dict):
            continue
        number = step["step"]
        caused = removal.get(number, {})
        ner = step["scanners"].get("ner")
        if isinstance(ner, dict) and ner.get("state") == "scanned":
            for finding in ner.get("findings", []):
                placed, description = _ner_highlight(number, finding, by_index, caused.get("ner", set()))
                if placed is not None:
                    highlights.append(placed)
                else:
                    unplaced.append(description)
        regex = step["scanners"].get("regex_pii")
        if isinstance(regex, dict) and regex.get("state") == "scanned":
            for finding in regex.get("findings", []):
                found, missing = _regex_highlights(number, finding, segments, caused.get("regex_pii", set()))
                highlights.extend(found)
                if missing is not None:
                    unplaced.append(missing)
        toxicity = step["scanners"].get("toxicity")
        if isinstance(toxicity, dict) and toxicity.get("state") == "scanned":
            for finding in toxicity.get("findings", []):
                index = finding.get("message_index")
                item = {
                    "step": number,
                    "scanner": "toxicity",
                    "message_index": index,
                    "role": finding.get("role"),
                    "verdict": finding.get("verdict"),
                    "outcome": finding.get("outcome"),
                    "error": finding.get("error"),
                    "categories": finding.get("categories"),
                    "detoxify_score": finding.get("detoxify_score"),
                    "caused_removal": index in caused.get("toxicity", set()),
                }
                if index in by_index:
                    flags.append(item)
                else:
                    unplaced.append({**item, "why": "the finding names a message the row doesn't have"})
    cut = len(highlights) > MAX_HIGHLIGHTS or len(unplaced) > MAX_HIGHLIGHTS
    return highlights[:MAX_HIGHLIGHTS], flags, unplaced[:MAX_HIGHLIGHTS], cut


# -------------------------------------------------------------------------
# Duplicates
# -------------------------------------------------------------------------


def _duplicate_places(entry: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The kept copies this row was removed as a duplicate of, and the rows removed
    as duplicates of this one. Each place is `{step, run, file, line}`."""
    copies: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for step in entry.get("steps", []):
        dedup = step.get("scanners", {}).get("dedup") if isinstance(step, dict) else None
        if not isinstance(dedup, dict) or dedup.get("state") != "scanned":
            continue
        for finding in dedup.get("findings", []):
            place = finding.get("duplicate_of") if isinstance(finding, dict) else None
            if isinstance(place, dict):
                copies.append({"step": step["step"], **{key: place.get(key) for key in ("run", "file", "line")}})
        for item in dedup.get("duplicated_by", []):
            place = item.get("row") if isinstance(item, dict) else None
            if isinstance(place, dict):
                duplicates.append({"step": step["step"], **{key: place.get(key) for key in ("run", "file", "line")}})
    return copies, duplicates


def _copy_story(ctx: _Context, place: dict[str, Any], with_text: bool) -> dict[str, Any]:
    """What became of the kept copy at `place`: its fate, and (for the first copies) its text."""
    result: dict[str, Any] = {"place": place, "story": None, "segments": None, "problem": None}
    try:
        entry = read_row(ctx.status_dir, place["run"], place["file"], place["line"], summary=ctx.summary)
    except RowStatusReadError as exc:
        result["problem"] = str(exc)
        return result
    result["story"] = summarize(entry)
    result["origin"] = entry.get("origin")
    if with_text:
        prepared = _read_prepared(ctx, entry)
        result["segments"] = prepared["segments"]
        result["run_dir"] = prepared["run_dir"]
        result["problem"] = prepared["problem"]
    return result


# -------------------------------------------------------------------------
# One row
# -------------------------------------------------------------------------


def row_story(chain_dir: Path, run: str, file: str, line: int) -> dict[str, Any]:
    """Everything the Rows page shows for the Prepared row at `run`, `file`, `line`.
    Raises `RowsError` when the chain has no finished row status or the row isn't in it."""
    ctx = chain_context(chain_dir)
    if isinstance(line, bool) or not isinstance(line, int):
        raise RowsError("The line must be a whole number.")
    try:
        entry = read_row(ctx.status_dir, run, file, line, summary=ctx.summary)
    except RowStatusReadError as exc:
        raise RowsError(str(exc)) from exc
    return _story_of(ctx, entry)


def _story_of(ctx: _Context, entry: dict[str, Any]) -> dict[str, Any]:
    prepared = _read_prepared(ctx, entry)
    segments = prepared["segments"] or []
    highlights, flags, unplaced, cut = _findings(entry, segments)
    copies, duplicated_by = _duplicate_places(entry)
    metadata = prepared["record"].get("metadata") if isinstance(prepared["record"], dict) else None
    return {
        "chain_dir": str(ctx.chain_dir),
        "location": {key: entry["prepared"][key] for key in ("run", "file", "line")},
        "summary": summarize(entry),
        "entry": entry,
        "prepared": {key: prepared[key] for key in ("run_dir", "fingerprint_ok", "problem")},
        # The full `_origin` note of the Prepared line (the status entry's `origin` is only
        # where the row came from), which "Show original" needs. None when the line isn't shown.
        "origin": metadata.get(ORIGIN_KEY) if isinstance(metadata, dict) else None,
        "metadata": metadata,
        "segments": prepared["segments"],
        "highlights": highlights,
        "message_flags": flags,
        "unplaced": unplaced,
        "findings_cut": cut,
        "duplicate_of": [_copy_story(ctx, place, index < MAX_COPIES) for index, place in enumerate(copies[:MAX_COPIES])],
        "duplicated_by": {"items": duplicated_by[:MAX_DUPLICATED_BY], "more": max(0, len(duplicated_by) - MAX_DUPLICATED_BY)},
    }


# -------------------------------------------------------------------------
# From a clean row
# -------------------------------------------------------------------------


def _nth_json_line(path: Path, number: int) -> dict[str, Any]:
    """Line `number` (from 1) of an ndjson file, parsed."""
    try:
        with path.open("rb") as handle:
            for current, raw in enumerate(handle, 1):
                if current == number:
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise RowsError(f"{path} line {number} isn't an object.")
                    return value
    except (OSError, ValueError) as exc:
        if isinstance(exc, RowsError):
            raise
        raise RowsError(f"{path} can't be read: {exc}") from exc
    raise RowsError(f"{path} has no line {number}.")


def row_from_clean(chain_dir: Path, run: str, file: str, line: int) -> dict[str, Any]:
    """The story of the Prepared row that became line `line` of `file` in the clean
    data (the output of the chain's last filter step), found by following each
    step's line map back, and checking the fingerprint at each hop."""
    ctx = chain_context(chain_dir)
    if run not in ctx.summary["runs"]:
        raise RowsError(f"The chain has no run {run!r}.")
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        raise RowsError("The line must be a whole number of at least 1.")
    if not 0 < len(ctx.steps) <= MAX_STEPS:
        raise RowsError("The chain has no filter steps to follow back.")

    hops: list[dict[str, Any]] = []
    current_file, current_line = file, line
    for step in reversed(ctx.steps):
        run_dir = Path(step["output_root"]) / run
        summary = read_json_or_none(run_dir / "summary.json")
        if not isinstance(summary, dict) or summary.get("tool") != FILTER_ROWS_TOOL:
            raise RowsError(f"{run_dir} isn't a filter run, so its line map can't be read.")
        relative = _inside_run(current_file)
        if str(relative) not in (_listed_files(run_dir) or set()):
            raise RowsError(f"{run_dir}'s summary.json doesn't list {relative} among the files it wrote.")
        map_relative = f"{LINE_MAP_FOLDER}/{relative.with_suffix('.ndjson')}"
        listed_maps = {
            item.get("path"): item.get("entries")
            for item in (summary.get("line_map") or {}).get("files", [])
            if isinstance(item, dict)
        }
        if map_relative not in listed_maps:
            raise RowsError(f"{run_dir}'s summary.json doesn't list the line map {map_relative}.")
        entries = listed_maps[map_relative]
        if isinstance(entries, int) and not 1 <= current_line <= entries:
            raise RowsError(f"Line {current_line} isn't a line of {relative} in {run_dir} (it has {entries:,} rows).")
        parent = _nth_json_line(run_dir / map_relative, current_line).get("_parent")
        if not isinstance(parent, dict) or not isinstance(parent.get("file"), str) or not isinstance(parent.get("line"), int):
            raise RowsError(f"{run_dir / map_relative} line {current_line} doesn't say which line it came from.")
        hops.append({"step": step["number"], "file": current_file, "line": current_line, "parent": parent})
        current_file, current_line = parent["file"], parent["line"]

    try:
        entry = read_row(ctx.status_dir, run, current_file, current_line, summary=ctx.summary)
    except RowStatusReadError as exc:
        raise RowsError(str(exc)) from exc
    story = _story_of(ctx, entry)
    story["from_clean"] = {
        "clean": {"run": run, "file": file, "line": line},
        "hops": hops,
        # The Prepared row's fingerprint is the one the first hop's line map recorded.
        "fingerprint_matches": entry["prepared"]["source_sha256"] == hops[-1]["parent"].get("source_sha256"),
    }
    return story


# -------------------------------------------------------------------------
# From an Original row
# -------------------------------------------------------------------------


def row_from_original(
    chain_dir: Path, original_path: Path, row: int | None, line: int | None, split: str | None
) -> dict[str, Any]:
    """Where an Original row went, among the Prepared runs of this chain: the answers
    of `runs.find_prepared` for each run that read the file (each with `run`, the run's
    path under the chain's Prepared folder), and the story of the first one found."""
    ctx = chain_context(chain_dir)
    answer = runs.find_prepared(original_path, row, line, split, None)
    matches: list[dict[str, Any]] = []
    for match in answer["matches"]:
        run_dir = match.get("run_dir")
        if not isinstance(run_dir, str):
            continue
        try:
            relative = Path(run_dir).relative_to(ctx.prepared_root).as_posix()
        except ValueError:
            continue  # a Prepared run of some other chain
        matches.append({**match, "run": relative})

    story = None
    for match in matches:
        prepared = match.get("prepared")
        if match.get("status") == "found" and isinstance(prepared, dict):
            story = row_story(chain_dir, match["run"], prepared["file"], prepared["line"])
            break
    return {"path": answer["path"], "runs_checked": answer["runs_checked"], "matches": matches, "row": story}
