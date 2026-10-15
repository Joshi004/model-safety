"""The flagged rows of a dataset's result: page through them, and open one to see why it was flagged.

Everything here is read-only. The rows come from the dataset's findings index, which the scan's
analysis job wrote (modelsafety/results/analyse.py, started by results.py):

    <service root>/scans/<scan id>/index/<dataset id>.jsonl        one line for each flagged row
    <service root>/scans/<scan id>/index/<dataset id>.counts.json  the counts a page filters by

A page reads only the index, never a result file or a Prepared file. Opening one row reads
that row's index line and then one line of its Prepared file, at the byte offset the index
holds, and only if the version's own `summary.json` lists the file. The line's fingerprint is
compared with the one the scan recorded, so the text shown is the text that was scanned.

The user's choices are applied here, when the index is read, and never saved (D8): for
toxicity the categories and verdicts that count as flagged, for the others a filter by reason
and by name (NER label, regex category, or the dataset a duplicate's kept copy is in).

Highlights are worked out here, not in the browser: NER spans by message and character range
(each checked to lie inside its message's text), regex PII by searching for the matched text
(the scan keeps no positions), toxicity by message. A finding that can't be placed goes in
`unplaced` with the reason, and is never guessed at.

Rules this module keeps:

* Every id, scanner key, filter and choice from a request is checked against the record, a
  fixed list or a pattern before it is used. The index is found from the scan's folder and a
  dataset id that matches `registry.DATASET_ID`. A `file` from a request is only compared with
  the index's own entries; the path that is opened is the one in the matching entry, and it
  has to be relative, without "..", and listed by the Prepared version.
* Nothing is written or deleted. Original data is never opened here.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

import registry
import results
import scanners
import scans
from reader_imports import ORIGIN_KEY, content_text, line_fingerprint, read_json_or_none

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
# A filter is one name (a label, a category, a dataset id); no real one is longer.
MAX_FILTER_CHARS = 100
# The longest `file` of a request: a path inside a Prepared version.
MAX_FILE_CHARS = 1000

# The longest text of one message that is sent to the browser.
MAX_SEGMENT_CHARS = 200_000
# The most highlights, and the most unplaced findings, sent for one row.
MAX_HIGHLIGHTS = 2000
# The most places one regex PII value is marked in one message.
MAX_MATCHES_PER_VALUE = 50

ResultsError = results.ResultsError


class _Opened(NamedTuple):
    """A dataset's flagged-rows index, found and checked."""

    dataset: dict[str, Any]
    scan: dict[str, Any]
    rows_path: Path
    counts: dict[str, Any]
    # The dataset's Prepared version that the scan read.
    version_folder: Path


# What a choice of verdicts and categories is, for toxicity: sets, as scanners.toxicity_flagged takes them.
Counted = tuple[set[str], set[str]]


# -------------------------------------------------------------------------
# Finding the index
# -------------------------------------------------------------------------


def _check_ready(scan: dict[str, Any], record: dict[str, Any] | None, found: dict[str, Any]) -> None:
    """Raise unless the analysis that wrote the scan's index has succeeded. An analysis whose
    numbers were already applied (`applied_at`) is known to have succeeded, so SLURM isn't
    asked on every page; any other is read live, as results.py does."""
    if record is None:
        raise ResultsError("This result hasn't been checked yet. Its flagged rows are listed once the check has finished.")
    if record.get("applied_at") is None:
        active, accounted, _error = results._slurm_for(found)
        state, reason = results._outcome(scan, record, active, accounted)
        if state in results.GOING_STATES:
            raise ResultsError("The check of this result is still going. Its flagged rows are listed once it has finished.")
        if state != scans.STATE_SUCCEEDED:
            raise ResultsError(f"The check of this result didn't finish ({reason or state}), so its flagged rows can't be listed.")
    if not record.get("has_index"):
        raise ResultsError(
            "This result was checked before flagged rows were listed. Use \"Check again\" on its page to list them."
        )


def _is_counts(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("rows"), int)
        and not isinstance(value.get("rows"), bool)
        and isinstance(value.get("by_reason"), dict)
        and isinstance(value.get("by_name"), dict)
    )


def _open_index(dataset_id: str, scanner_key: Any) -> _Opened:
    key = results._scanner_key(scanner_key)
    dataset = registry.get_dataset(dataset_id)
    found = results._results_of(dataset, [key])
    if key not in found:
        raise ResultsError(f"This dataset has no finished {scanners.SCANNERS[key].label} scan.")
    scan, record = found[key]
    _check_ready(scan, record, found)
    rows_path, counts_path = results.index_files(results._folder(scan), dataset["id"])
    counts = read_json_or_none(counts_path)
    if not _is_counts(counts):
        raise ResultsError(f"The flagged-rows index of this result can't be read ({counts_path.name} is missing or isn't a count file).")
    versions = scan.get("versions")
    version = versions.get(dataset["id"]) if isinstance(versions, dict) else None
    return _Opened(dataset, scan, rows_path, counts, results._version_folder(dataset["id"], version))


def _toxicity_choice(opened: _Opened, categories: str | None, verdicts: str | None) -> Counted | None:
    """For toxicity, the verdicts and categories that count as flagged: the ones asked for, or
    the ones the scan was started with. None for every other scanner, which has none to choose."""
    if opened.scan["scanner"] != "toxicity":
        if categories is not None or verdicts is not None:
            raise ResultsError("Only the toxicity result has categories and verdicts to choose.")
        return None
    settings = opened.scan["settings"]
    chosen_categories = results._split_choice(categories, scanners.GUARD_CATEGORIES, "categories")
    chosen_verdicts = results._split_choice(verdicts, tuple(scanners.TOXICITY_REMOVABLE_VERDICTS), "verdicts")
    if chosen_categories is None:
        chosen_categories = scanners._chosen(settings.get("categories"), scanners.GUARD_CATEGORIES)
    if chosen_verdicts is None:
        chosen_verdicts = scanners._chosen(settings.get("verdicts"), tuple(scanners.TOXICITY_REMOVABLE_VERDICTS))
    return set(chosen_verdicts), set(chosen_categories)


def _counted_atoms(entry: dict[str, Any], counted: Counted) -> list[str]:
    """The atoms of a toxicity row ("error", or "<verdict>|<category>") that count as flagged
    under the choice, by the same rule the result page's numbers use."""
    atoms = entry.get("atoms")
    verdicts, categories = counted
    return [atom for atom in (atoms if isinstance(atoms, list) else []) if isinstance(atom, str) and scanners._atom_counts(atom, verdicts, categories)]


def _parse(raw: bytes, path: Path, number: int) -> dict[str, Any]:
    try:
        entry = json.loads(raw)
    except ValueError as exc:
        raise ResultsError(f"{path.name} line {number} isn't valid JSON ({exc}).") from exc
    if not isinstance(entry, dict):
        raise ResultsError(f"{path.name} line {number} isn't an index entry.")
    return entry


# -------------------------------------------------------------------------
# One page of flagged rows
# -------------------------------------------------------------------------


def _filter(name: str, value: Any, known: dict[str, Any]) -> str | None:
    """A filter from the page, which has to be one of the names the index counted."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > MAX_FILTER_CHARS:
        raise ResultsError(f"The {name} filter must be a name of at most {MAX_FILTER_CHARS} characters.")
    if value not in known:
        raise ResultsError(f"No flagged row has the {name} '{value}'.")
    return value


def _counts_of(counts: dict[str, Any], field: str) -> dict[str, int]:
    return {name: number for name, number in counts[field].items() if isinstance(number, int) and not isinstance(number, bool)}


def _biggest_first(counts: dict[str, int]) -> list[tuple[str, int]]:
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _options(opened: _Opened, names: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """What the page can filter by, with the number of rows each has (biggest first). Toxicity
    has none: it follows its categories and verdicts."""
    if opened.scan["scanner"] == "toxicity":
        return {"reasons": [], "names": []}
    by_dataset = opened.scan["scanner"] == "dedup"
    return {
        "reasons": [
            {"name": name, "count": number} for name, number in _biggest_first(_counts_of(opened.counts, "by_reason"))
        ],
        # For duplicates a name is the dataset a kept copy is in, which is shown by its name.
        "names": [
            {"name": name, "label": names.get(name, name) if by_dataset else name, "count": number}
            for name, number in _biggest_first(_counts_of(opened.counts, "by_name"))
        ],
    }


def _matches(entry: dict[str, Any], reason: str | None, name: str | None, atoms: list[str] | None) -> bool:
    """Whether an index entry passes the filters. Toxicity rows pass when something in them
    counts as flagged (`atoms` is what counts); the others when they have the reason and the name."""
    if atoms is not None:
        return len(atoms) > 0
    if reason is not None and reason not in (entry.get("reasons") or []):
        return False
    if name is None:
        return True
    duplicate = entry.get("duplicate_of")
    return (
        name in (entry.get("labels") or {})
        or name in (entry.get("categories") or {})
        or (isinstance(duplicate, dict) and duplicate.get("dataset") == name)
    )


def _page_entry(entry: dict[str, Any], atoms: list[str] | None, names: dict[str, str]) -> dict[str, Any]:
    """What the table shows of one flagged row. The findings themselves are not included: the
    row's own view has them."""
    duplicate = entry.get("duplicate_of")
    kept_in = None
    if isinstance(duplicate, dict) and isinstance(duplicate.get("dataset"), str):
        kept_in = names.get(duplicate["dataset"], duplicate["dataset"])
    labels = entry.get("labels") if isinstance(entry.get("labels"), dict) else {}
    categories = entry.get("categories") if isinstance(entry.get("categories"), dict) else {}
    return {
        "file": entry.get("file"),
        "line": entry.get("line"),
        "preview": entry.get("preview") if isinstance(entry.get("preview"), str) else "",
        "reasons": entry.get("reasons") if isinstance(entry.get("reasons"), list) else [],
        "names": {**labels, **categories},
        "atoms": atoms or [],
        "kept_in": kept_in,
    }


def flagged_page(
    dataset_id: str,
    scanner_key: Any,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    reason: str | None = None,
    name: str | None = None,
    categories: str | None = None,
    verdicts: str | None = None,
) -> dict[str, Any]:
    """One page of the dataset's flagged rows for a scanner, in file and line order. A row has
    to pass every filter. With no filter the lines before the page are skipped without being
    read as JSON, and the total comes from the counts file; with one, the whole index is read
    (and for toxicity there always is one: the choice of categories and verdicts)."""
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ResultsError("The page must be a whole number of at least 1.")
    page_size = min(max(1, page_size), MAX_PAGE_SIZE)
    opened = _open_index(dataset_id, scanner_key)
    counted = _toxicity_choice(opened, categories, verdicts)
    if counted is not None and (reason not in (None, "") or name not in (None, "")):
        raise ResultsError("A toxicity result is filtered by its categories and verdicts, not by reason or name.")
    wanted_reason = _filter("reason", reason, opened.counts["by_reason"]) if counted is None else None
    wanted_name = _filter("name", name, opened.counts["by_name"]) if counted is None else None

    names = registry.dataset_names()
    filtered = counted is not None or wanted_reason is not None or wanted_name is not None
    first = (page - 1) * page_size
    entries: list[dict[str, Any]] = []
    matched = 0
    try:
        with opened.rows_path.open("rb") as handle:
            for number, raw in enumerate(handle, 1):
                if not raw.strip():
                    continue
                if not filtered:
                    if number <= first:
                        continue
                    if len(entries) == page_size:
                        break
                entry = _parse(raw, opened.rows_path, number)
                atoms = _counted_atoms(entry, counted) if counted is not None else None
                if filtered:
                    if not _matches(entry, wanted_reason, wanted_name, atoms):
                        continue
                    matched += 1
                    if not first < matched <= first + page_size:
                        continue
                entries.append(_page_entry(entry, atoms, names))
    except OSError as exc:
        raise ResultsError(f"The flagged-rows index can't be read: {exc}") from exc

    total = matched if filtered else opened.counts["rows"]
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_next": total > first + page_size,
        "entries": entries,
        "options": _options(opened, names),
    }


# -------------------------------------------------------------------------
# The Prepared line
# -------------------------------------------------------------------------


def _inside_version(relative: Any) -> PurePosixPath:
    pure = PurePosixPath(relative) if isinstance(relative, str) and relative else None
    if pure is None or pure.is_absolute() or ".." in pure.parts:
        raise ResultsError(f"{relative!r} isn't a path inside a Prepared version.")
    return pure


def _listed_files(folder: Path) -> set[str] | None:
    """The files a Prepared version's `summary.json` lists in `output.files_written`, or None."""
    summary = read_json_or_none(folder / "summary.json")
    output = summary.get("output") if isinstance(summary, dict) else None
    written = output.get("files_written") if isinstance(output, dict) else None
    if not isinstance(written, list):
        return None
    return {item["path"] for item in written if isinstance(item, dict) and isinstance(item.get("path"), str)}


def _segments(record: Any) -> list[dict[str, Any]]:
    """The texts a scanner reads from a row: its `text`, or each message's text, cut the way the
    scans cut them. A message with no text is kept (empty), so the message numbers in findings
    still line up."""
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


def _read_prepared(folder: Path, file: Any, offset: Any, sha256: Any) -> dict[str, Any]:
    """One Prepared line: read at its byte offset, checked against the fingerprint the scan
    recorded, with its texts and its `_origin` note. A problem is reported in `problem`, with
    no text, rather than raised."""
    result: dict[str, Any] = {
        "run_dir": str(folder),
        "fingerprint_ok": None,
        "segments": None,
        "origin": None,
        "problem": None,
    }
    try:
        relative = _inside_version(file)
        listed = _listed_files(folder)
        if listed is None or str(relative) not in listed:
            result["problem"] = f"{folder}'s summary.json doesn't list {relative}, so the line wasn't read."
            return result
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            result["problem"] = "The index doesn't say where this line starts."
            return result
        with (folder / relative).open("rb") as handle:
            handle.seek(offset)
            raw = handle.readline()
    except (OSError, ResultsError) as exc:
        result["problem"] = f"The Prepared line can't be read: {exc}"
        return result
    result["fingerprint_ok"] = line_fingerprint(raw) == sha256
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
    result["segments"] = _segments(record)
    metadata = record.get("metadata") if isinstance(record, dict) else None
    result["origin"] = metadata.get(ORIGIN_KEY) if isinstance(metadata, dict) else None
    return result


# -------------------------------------------------------------------------
# What was found in a row
# -------------------------------------------------------------------------


def _is_position(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _ner_highlight(finding: dict[str, Any], by_index: dict[Any, dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The highlight for one NER span (None when it can't be placed) and its description."""
    item = {
        "scanner": "ner",
        "label": finding.get("label"),
        "tier": finding.get("tier"),
        "confidence": finding.get("confidence"),
        "message_index": finding.get("message_index"),
        "start": finding.get("start"),
        "end": finding.get("end"),
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


def _regex_highlights(finding: dict[str, Any], segments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Every place a regex PII value is in the row's text (the scan keeps no positions)."""
    value = finding.get("value")
    base = {"scanner": "regex_pii", "category": finding.get("category"), "value": value}
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


def _message_counts(finding: dict[str, Any], counted: Counted) -> bool:
    """Whether one judged message counts as flagged under the choice: a guard error always does
    (D9), and so does a verdict with no category."""
    verdict = finding.get("verdict")
    if not isinstance(verdict, str):
        return True
    categories = [name for name in (finding.get("categories") or []) if isinstance(name, str)]
    atoms = [f"{verdict}|{name}" for name in categories] or [f"{verdict}|"]
    verdicts, chosen = counted
    return any(scanners._atom_counts(atom, verdicts, chosen) for atom in atoms)


def _findings(
    scanner: str, entry: dict[str, Any], segments: list[dict[str, Any]], counted: Counted | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], bool]:
    """The row's findings placed in its text: highlights (NER and regex PII spans), flags for
    whole messages (toxicity), the findings that couldn't be placed, and whether the list was
    cut short (here, or by the index, which keeps a bounded number of findings for a row)."""
    by_index = {segment["message_index"]: segment for segment in segments}
    findings = [item for item in (entry.get("findings") or []) if isinstance(item, dict)]
    highlights: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []
    unplaced: list[dict[str, Any]] = []
    if scanner == "pii_ner":
        for finding in findings:
            placed, description = _ner_highlight(finding, by_index)
            if placed is not None:
                highlights.append(placed)
            else:
                unplaced.append(description)
    elif scanner == "pii_regex":
        # A value can be in a row many times, and is looked for once.
        seen: set[tuple[Any, Any]] = set()
        for finding in findings:
            key = (finding.get("category"), finding.get("value"))
            if key in seen:
                continue
            seen.add(key)
            found, missing = _regex_highlights(finding, segments)
            highlights.extend(found)
            if missing is not None:
                unplaced.append(missing)
    elif scanner == "toxicity" and counted is not None:
        for finding in findings:
            item = {
                "scanner": "toxicity",
                "message_index": finding.get("message_index"),
                "role": finding.get("role"),
                "verdict": finding.get("verdict"),
                "error": finding.get("error"),
                "categories": finding.get("categories") or [],
                "detoxify_score": finding.get("detoxify_score"),
                "detoxify_category": finding.get("detoxify_category"),
                "counts": _message_counts(finding, counted),
            }
            if finding.get("message_index") in by_index:
                flags.append(item)
            else:
                unplaced.append({**item, "why": "the finding names a message the row doesn't have"})
    total = entry.get("findings_total")
    cut = (
        len(highlights) > MAX_HIGHLIGHTS
        or len(unplaced) > MAX_HIGHLIGHTS
        or (isinstance(total, int) and not isinstance(total, bool) and total > len(findings))
    )
    return highlights[:MAX_HIGHLIGHTS], flags, unplaced[:MAX_HIGHLIGHTS], cut


def _kept_copy(opened: _Opened, entry: dict[str, Any], names: dict[str, str]) -> dict[str, Any] | None:
    """For a duplicate: the copy it matched, which dataset it is in, and its text, read the same
    way as the row's own (the copy can be in another dataset's Prepared version)."""
    place = entry.get("duplicate_of")
    if not isinstance(place, dict):
        return None
    dataset = place.get("dataset")
    story: dict[str, Any] = {
        "dataset_id": dataset if isinstance(dataset, str) else None,
        "dataset": names.get(dataset, dataset) if isinstance(dataset, str) else None,
        "file": place.get("file"),
        "line": place.get("line"),
        "run_dir": None,
        "segments": None,
        "origin": None,
        "problem": None,
    }
    if not isinstance(dataset, str):
        story["problem"] = "The copy is in a reference set, not in a dataset of this service."
        return story
    try:
        if dataset == opened.dataset["id"]:
            folder = opened.version_folder
        else:
            versions = opened.scan.get("versions")
            folder = results._version_folder(dataset, versions.get(dataset) if isinstance(versions, dict) else None)
    except ResultsError as exc:
        story["problem"] = str(exc)
        return story
    prepared = _read_prepared(folder, place.get("file"), place.get("byte_offset"), place.get("sha256"))
    story.update({key: prepared[key] for key in ("run_dir", "segments", "origin", "problem")})
    return story


# -------------------------------------------------------------------------
# One flagged row
# -------------------------------------------------------------------------


def _find_entry(path: Path, file: str, line: int) -> dict[str, Any]:
    """The index entry of one row. The index is read from the top; a line is only parsed when it
    holds the file's name, which is how the index writes it (as JSON, so the same way for any name)."""
    needle = json.dumps(file).encode()
    try:
        with path.open("rb") as handle:
            for number, raw in enumerate(handle, 1):
                if needle not in raw:
                    continue
                entry = _parse(raw, path, number)
                if entry.get("file") == file and entry.get("line") == line:
                    return entry
    except OSError as exc:
        raise ResultsError(f"The flagged-rows index can't be read: {exc}") from exc
    raise ResultsError(f"{file} line {line} isn't a flagged row of this result.")


def flagged_row(
    dataset_id: str,
    scanner_key: Any,
    file: Any,
    line: Any,
    categories: str | None = None,
    verdicts: str | None = None,
) -> dict[str, Any]:
    """Everything the row view shows for one flagged row: its Prepared text (fingerprint checked),
    where it came from (`origin`, for "Show original"), what each scanner found and where in the
    text, and for a duplicate the copy it matched. For toxicity, `categories` and `verdicts`
    only decide which messages are marked as counting."""
    if not isinstance(file, str) or not file or len(file) > MAX_FILE_CHARS:
        raise ResultsError(f"The file must be a path of at most {MAX_FILE_CHARS} characters.")
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        raise ResultsError("The line must be a whole number of at least 1.")
    opened = _open_index(dataset_id, scanner_key)
    counted = _toxicity_choice(opened, categories, verdicts)
    scanner = opened.scan["scanner"]
    entry = _find_entry(opened.rows_path, file, line)
    names = registry.dataset_names()

    prepared = _read_prepared(opened.version_folder, entry.get("file"), entry.get("byte_offset"), entry.get("sha256"))
    highlights, flags, unplaced, cut = _findings(scanner, entry, prepared["segments"] or [], counted)
    total = entry.get("findings_total")
    return {
        "dataset_id": opened.dataset["id"],
        "scanner": scanner,
        "location": {"file": entry["file"], "line": entry["line"]},
        "reasons": entry.get("reasons") if isinstance(entry.get("reasons"), list) else [],
        "prepared": {key: prepared[key] for key in ("run_dir", "fingerprint_ok", "problem")},
        "origin": prepared["origin"],
        "segments": prepared["segments"],
        "highlights": highlights,
        "message_flags": flags,
        "unplaced": unplaced,
        "findings_cut": cut,
        "findings_total": total if isinstance(total, int) and not isinstance(total, bool) else None,
        "duplicate_of": _kept_copy(opened, entry, names) if scanner == "dedup" else None,
    }
