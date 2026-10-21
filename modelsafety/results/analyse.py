#!/usr/bin/env python3
"""Check one finished scan and count what only its big result files can say.

For one scanner's results folder (``--results-dir``), read-only, this writes into
``--out-dir``:

  coverage.json   the coverage report for this scan (``modelsafety/results/coverage.py``).
                  A failing verdict is data, not an error: the file is written either way.
  tally.json      counts that the scanner's small summary files don't hold. Written after
                  coverage.json, and not with ``--coverage-only``:

    toxicity   Each slice's ``guard_results/all.jsonl`` holds every message the guard judged.
               The tally counts messages by (verdict, categories), the guard errors, and the
               rows by the set of "<verdict>|<category>" atoms their messages carry (an
               "error" atom for a guard error, and "<verdict>|" for a verdict with no
               category). Counts under any choice of verdicts and categories can then be
               worked out later, exactly, without reading the files again. A message the
               policy judge said is suitable (its ``policy`` block) is counted like a Safe
               one, and ``policy_kept`` says how many there were.
    ner        ``all_spans.jsonl``: the rows with a span the policy made eligible, and the
               eligible spans by label and tier.
    dedup      ``dedup_results.jsonl``, only when ``--group NAME=PREFIX`` is given (one for
               each dataset, PREFIX being the folder its files are under, with a trailing
               "/"): the duplicate rows of each group, by the group that holds the copy
               that was kept. A group named with ``--protect`` has nothing flagged: its
               duplicates are left out, and its index (below) is empty.
    regex_pii  nothing: its summary says it all.

With ``--index-dir`` it also writes the findings index of each dataset, after the tally:

  <index-dir>/<name>.jsonl        one line for each flagged row, in file and line order: where it
                                  is in the dataset's Prepared version (file relative to that
                                  version, line, fingerprint, byte offset), a short text
                                  preview (of the message a finding is in, when it names one),
                                  what was found, and for a duplicate the copy it matched.
                                  Findings are read with ``modelsafety/filter/rules.py``
                                  (``READERS``), under rules that keep everything, so the choices
                                  a user makes (NER labels, regex categories, toxicity
                                  categories and verdicts) are applied when the index is read.
  <index-dir>/<name>.counts.json  rows, findings, rows by reason and rows by name (label,
                                  category, or the dataset a kept copy is in).

``<name>`` is ``--index-name`` when ``--root`` is one dataset's Prepared version, or each
``--group`` name when ``--root`` holds several datasets. The Prepared files with flagged rows
are read once, each line checked against the fingerprint the scan recorded for it.

The files are read line by line; the row sets are held in memory (a few GB for millions of
flagged rows), which the job script asks for.

Usage:
    python3 modelsafety/results/analyse.py --scanner toxicity \\
        --root DIR --results-dir DIR --out-dir DIR \\
        [--files-from FILE] [--group NAME=PREFIX ...] [--protect NAME ...] [--coverage-only] \\
        [--index-dir DIR (--index-name NAME | with --group)]

Exit code: 0 when the files asked for were written, 2 otherwise (an unreadable result
file, a Prepared line that isn't the one that was scanned, or nowhere to write).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# Run as a script, so only this folder is on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modelsafety.contract.fingerprints import line_fingerprint  # noqa: E402
from modelsafety.contract.run_finder import RunFinderError, read_json_or_none, select_scan_files  # noqa: E402
from modelsafety.data_screening.pii.model.common import content_text  # noqa: E402
from modelsafety.filter.rules import (  # noqa: E402
    DEDUP_RESULTS_FILE,
    NER_SPANS_FILE,
    READERS,
    FilterRules,
    FindingsError,
    HitIndex,
    guard_outcome,
    policy_keeps,
)
from modelsafety.results.coverage import (  # noqa: E402
    SCANNERS,
    TOXICITY_SHARDS,
    build_report,
    check_output_path,
    read_file_list,
    write_report,
)

TALLY_VERSION = 1
COVERAGE_FILE = "coverage.json"
TALLY_FILE = "tally.json"
SAFE_VERDICT = "Safe"
ERROR_ATOM = "error"
# A row is told apart by its file and its line; the line goes in the low 32 bits.
LINE_BITS = 32
PROGRESS_EVERY = 1_000_000

INDEX_VERSION = 1
# How much of a row's text its index line keeps, and the most findings it keeps for one row
# (the line's `findings_total` still says how many there were).
PREVIEW_CHARS = 160
MAX_FINDINGS_PER_ROW = 2000
# The Prepared files read between two progress lines.
INDEX_PROGRESS_FILES = 1000
# A name becomes a file name, so it is held to this.
INDEX_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class TallyError(ValueError):
    """A result file can't be read; the message names it."""


class IndexBuildError(ValueError):
    """The findings index can't be made; the message says why."""


def _records(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """Each JSON object of a JSONL file, with a "path:line" label for messages."""
    if not path.is_file():
        raise TallyError(f"{path} is missing.")
    with path.open("r", encoding="utf-8") as handle:
        for number, text in enumerate(handle, 1):
            if not text.strip():
                continue
            where = f"{path}:{number}"
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                raise TallyError(f"{where}: not valid JSON ({exc.msg}).") from exc
            if not isinstance(record, dict):
                raise TallyError(f"{where}: not a JSON object.")
            if number % PROGRESS_EVERY == 0:
                print(f"  {path.name}: {number:,} lines read", flush=True)
            yield where, record


class _RowKeys:
    """Turns (file, line) into one integer, so millions of rows fit in a set or a dict."""

    def __init__(self) -> None:
        self._files: dict[str, int] = {}

    def key(self, where: str, record: dict[str, Any]) -> int:
        file, line = record.get("source_file"), record.get("source_line")
        if not isinstance(file, str) or not file:
            raise TallyError(f"{where}: the record has no 'source_file'.")
        if not isinstance(line, int) or isinstance(line, bool) or not 0 <= line < 1 << LINE_BITS:
            raise TallyError(f"{where}: 'source_line' {line!r} isn't a line number.")
        file_id = self._files.setdefault(file, len(self._files))
        return (file_id << LINE_BITS) | line


def _sorted(counter: Counter) -> dict[str, int]:
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


# -------------------------------------------------------------------------
# Toxicity
# -------------------------------------------------------------------------


def _slice_names(results_dir: Path) -> list[str]:
    manifest_path = results_dir / "manifests" / "manifest.json"
    manifest = read_json_or_none(manifest_path)
    shards = manifest.get("shards") if isinstance(manifest, dict) else None
    if not isinstance(shards, list):
        raise TallyError(f"{manifest_path} is missing or doesn't list the slices (shards).")
    names = []
    for shard in shards:
        index = shard.get("index") if isinstance(shard, dict) else None
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise TallyError(f"{manifest_path} has a slice with no index.")
        names.append(f"shard_{index:05d}")
    return names


def _clean_categories(raw: Any) -> list[str]:
    """A message's guard categories, sorted, without the "None" the guard gives a message that
    has none. The tally and the findings index both go through this, so they agree."""
    names = raw if isinstance(raw, list) else []
    return sorted({name for name in names if isinstance(name, str) and name and name != "None"})


def _message_atoms(verdict: str, categories: list[str]) -> list[str]:
    # A verdict with no category still has to be told apart, and counted, so it has its own atom.
    return [f"{verdict}|{category}" for category in categories] or [f"{verdict}|"]


def toxicity_tally(results_dir: Path) -> dict[str, Any]:
    names = _slice_names(results_dir)
    keys = _RowKeys()
    atom_ids: dict[str, int] = {}
    row_masks: dict[int, int] = {}
    messages: Counter = Counter()
    judged = safe = guard_errors = policy_kept = 0

    for name in names:
        path = results_dir / TOXICITY_SHARDS / name / "guard_results" / "all.jsonl"
        for where, record in _records(path):
            guard = record.get("guard")
            if not isinstance(guard, dict):
                raise TallyError(f"{where}: the record has no 'guard' verdict.")
            judged += 1
            outcome, verdict = guard_outcome(guard)
            if outcome == "guard_error":
                guard_errors += 1
                atoms = [ERROR_ATOM]
            elif verdict == SAFE_VERDICT:
                safe += 1
                continue
            elif policy_keeps(record):
                # The policy judge said the message is suitable: it counts like a Safe one.
                policy_kept += 1
                continue
            else:
                categories = _clean_categories(guard.get("categories"))
                messages[(verdict, tuple(categories))] += 1
                atoms = _message_atoms(verdict, categories)
            mask = 0
            for atom in atoms:
                mask |= 1 << atom_ids.setdefault(atom, len(atom_ids))
            row = keys.key(where, record)
            row_masks[row] = row_masks.get(row, 0) | mask

    by_name = sorted(atom_ids, key=atom_ids.get)  # type: ignore[arg-type]
    rows = [
        {"atoms": [atom for atom in by_name if mask >> atom_ids[atom] & 1], "count": count}
        for mask, count in sorted(Counter(row_masks.values()).items())
    ]
    return {
        "slices": len(names),
        "messages_judged": judged,
        "safe": safe,
        "policy_kept": policy_kept,
        "guard_errors": guard_errors,
        "messages": [
            {"verdict": verdict, "categories": list(categories), "count": count}
            for (verdict, categories), count in sorted(messages.items())
        ],
        "rows": rows,
    }


# -------------------------------------------------------------------------
# NER
# -------------------------------------------------------------------------


def ner_tally(results_dir: Path) -> dict[str, Any]:
    keys = _RowKeys()
    flagged_rows: set[int] = set()
    spans = eligible = 0
    by_label: Counter = Counter()
    by_tier: Counter = Counter()
    for where, span in _records(results_dir / NER_SPANS_FILE):
        spans += 1
        is_eligible = span.get("candidate_eligible")
        if not isinstance(is_eligible, bool):
            raise TallyError(f"{where}: the span has no true or false 'candidate_eligible'.")
        if not is_eligible:
            continue
        label = span.get("label")
        if not isinstance(label, str):
            raise TallyError(f"{where}: the span has no 'label'.")
        eligible += 1
        by_label[label] += 1
        by_tier[str(span.get("tier"))] += 1
        flagged_rows.add(keys.key(where, span))
    return {
        "spans": spans,
        "eligible_spans": eligible,
        "rows_flagged": len(flagged_rows),
        "eligible_by_label": _sorted(by_label),
        "eligible_by_tier": _sorted(by_tier),
    }


# -------------------------------------------------------------------------
# Duplicates across datasets
# -------------------------------------------------------------------------


def dedup_tally(results_dir: Path, groups: list[tuple[str, str]], protected: frozenset[str] = frozenset()) -> dict[str, Any]:
    """`groups` is [(name, prefix)]. A file is in the group whose prefix starts its path. The
    duplicates in a `protected` group are left out: nothing is flagged in it, though rows in
    the other groups still match its rows."""
    cache: dict[str, str | None] = {}

    def group_of(file: Any, where: str) -> str | None:
        if not isinstance(file, str) or not file:
            raise TallyError(f"{where}: a row has no file.")
        if file not in cache:
            cache[file] = next((name for name, prefix in groups if file.startswith(prefix)), None)
        return cache[file]

    duplicates: Counter = Counter()
    kept_in: dict[str, Counter] = {name: Counter() for name, _prefix in groups}
    unassigned = 0
    for where, record in _records(results_dir / DEDUP_RESULTS_FILE):
        original = record.get("duplicate_of")
        if not isinstance(original, dict):
            raise TallyError(f"{where}: the record has no 'duplicate_of'.")
        mine = group_of(record.get("source_file"), where)
        kept = group_of(original.get("source_file"), where)
        if mine is None:
            unassigned += 1
            continue
        if mine in protected:
            continue
        duplicates[mine] += 1
        kept_in[mine][kept if kept is not None else "unassigned"] += 1
    return {
        "duplicates": sum(duplicates.values()) + unassigned,
        "unassigned": unassigned,
        "protected": sorted(protected),
        "groups": {
            name: {"duplicates": duplicates[name], "kept_in": _sorted(kept_in[name])} for name, _prefix in groups
        },
    }


# -------------------------------------------------------------------------
# The findings index
# -------------------------------------------------------------------------


def _clip(text: str) -> str:
    # Whitespace is folded, so a preview is one line in a table.
    return " ".join(text[: PREVIEW_CHARS * 4].split())[:PREVIEW_CHARS]


def _preview_of(record: Any, message_index: int | None = None) -> str:
    """The start of a Prepared row's text, for a table of rows: the message a finding is in
    (`message_index`), so the preview shows what was flagged, or when there is none, the row's
    `text`, or its first message that isn't a system message and has text."""
    if not isinstance(record, dict):
        return ""
    messages = record.get("messages")
    messages = messages if isinstance(messages, list) else []
    if message_index is not None and 0 <= message_index < len(messages) and isinstance(messages[message_index], dict):
        body = content_text(messages[message_index].get("content"))
        if body:
            return _clip(body)
    text = record.get("text")
    if isinstance(text, str) and text:
        return _clip(text)
    for message in messages:
        if isinstance(message, dict) and message.get("role") != "system":
            body = content_text(message.get("content"))
            if body:
                return _clip(body)
    return ""


def _flagged_message(reasons: dict[str, list[dict[str, Any]]]) -> int | None:
    """The first message (by its number) that a row's findings are in, when they name one."""
    numbers = [
        item["message_index"]
        for items in reasons.values()
        for item in items
        if isinstance(item.get("message_index"), int) and not isinstance(item["message_index"], bool)
    ]
    return min(numbers) if numbers else None


def _read_lines(
    path: Path,
    file: str,
    wanted: set[int],
    expected: dict[int, tuple[str, str]],
    message_of: dict[int, int | None],
) -> dict[int, tuple[int, str]]:
    """For each wanted line of one Prepared file: where it starts (its byte offset) and a
    preview of its text (of the message `message_of` names for the line, if any). Each line has
    to have the fingerprint the scan recorded for it, otherwise the file isn't what was
    scanned. Reads only as far as the last wanted line."""
    found: dict[int, tuple[int, str]] = {}
    last = max(wanted)
    offset = 0
    try:
        with path.open("rb") as handle:
            for number, raw in enumerate(handle, 1):
                if number in wanted:
                    sha256, scanner = expected[number]
                    if line_fingerprint(raw) != sha256:
                        raise IndexBuildError(
                            f"{file} line {number} isn't the line that was scanned: its fingerprint "
                            f"differs from the one {scanner} recorded."
                        )
                    try:
                        record = json.loads(raw)
                    except ValueError as exc:
                        raise IndexBuildError(f"{file} line {number}: not valid JSON ({exc}).") from exc
                    found[number] = (offset, _preview_of(record, message_of.get(number)))
                    if number == last:
                        break
                offset += len(raw)
    except OSError as exc:
        raise IndexBuildError(f"{path} can't be read: {exc}") from exc
    missing = wanted - found.keys()
    if missing:
        raise IndexBuildError(f"{file} has no line {min(missing):,}.")
    return found


def _rows_by_file(root: Path, groups: list[tuple[str, str]]) -> tuple[dict[str, int], dict[str, int]]:
    """`({file: rows}, {file: group number})` for the files of each group's Prepared version,
    with the group's prefix in front of each path, as the scan's results name them. Only the
    chosen versions are listed, never every folder under `root`."""
    rows: dict[str, int] = {}
    owner: dict[str, int] = {}
    for number, (name, prefix) in enumerate(groups):
        try:
            selection = select_scan_files(root / prefix)
        except RunFinderError as exc:
            raise IndexBuildError(f"The files of {name} can't be listed: {exc}") from exc
        for item in selection.files:
            if item.rows is not None:
                rows[f"{prefix}{item.path}"] = item.rows
                owner[f"{prefix}{item.path}"] = number
    return rows, owner


def _locate_lines(
    root: Path, hits: HitIndex, rows_by_file: dict[str, int], kept: dict[str, set[int]]
) -> dict[str, dict[int, tuple[int, str]]]:
    """Every flagged line (and the lines `kept` names: the copies duplicates matched), found in
    its Prepared file in one pass over each file that has any."""
    located: dict[str, dict[int, tuple[int, str]]] = {}
    files = sorted(rows_by_file)
    for number, file in enumerate(files, 1):
        flagged = hits.reasons_for(file)
        wanted = set(flagged) | kept.get(file, set())
        if wanted:
            message_of = {line: _flagged_message(reasons) for line, reasons in flagged.items()}
            located[file] = _read_lines(root / file, file, wanted, hits.expected_for(file), message_of)
        if number % INDEX_PROGRESS_FILES == 0:
            print(f"  {number:,} of {len(files):,} Prepared files read", flush=True)
    return located


def _entry_parts(scanner: str, reasons: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """What an index line says about the findings in one row, from what the scanner's reader
    made. Toxicity messages get their categories cleaned as the tally does, and the row's
    "atoms": the same ones the tally counts rows by, so a choice of verdicts and categories
    gives the same rows here as there."""
    findings = [item for items in reasons.values() for item in items]
    parts: dict[str, Any] = {
        "reasons": sorted(reasons),
        "labels": {},
        "categories": {},
        "atoms": [],
        "findings": findings,
        "duplicate_of": None,
    }
    if scanner == "ner":
        parts["labels"] = _sorted(Counter(item["label"] for item in findings))
    elif scanner == "regex_pii":
        parts["categories"] = _sorted(Counter(item["category"] for item in findings))
    elif scanner == "toxicity":
        messages: list[dict[str, Any]] = []
        atoms: set[str] = set()
        for reason, items in reasons.items():
            for item in items:
                categories = _clean_categories(item.get("categories"))
                messages.append({**item, "categories": categories})
                atoms.update([ERROR_ATOM] if reason == "toxicity_guard_error" else _message_atoms(item["verdict"], categories))
        messages.sort(key=lambda message: (message.get("message_index") is None, message.get("message_index") or 0))
        parts["findings"] = messages
        parts["atoms"] = sorted(atoms)
    elif scanner == "dedup":
        parts["findings"] = []
    return parts


def _kept_copy(
    reasons: dict[str, list[dict[str, Any]]],
    owner: dict[str, int],
    groups: list[tuple[str, str]],
    located: dict[str, dict[int, tuple[int, str]]],
) -> dict[str, Any]:
    """The row a duplicate matched: the dataset it is in, its place in that dataset's Prepared
    version, its fingerprint and where its line starts."""
    place = next(item for items in reasons.values() for item in items)["duplicate_of"]
    file, line, sha256 = place["file"], place["line"], place["source_sha256"]
    group = owner.get(file)
    if group is None:
        # Only in cross mode (the service doesn't use it): the copy is in a reference set, which isn't a run.
        return {"dataset": None, "file": file, "line": line, "sha256": sha256, "byte_offset": None}
    name, prefix = groups[group]
    return {
        "dataset": name,
        "file": file[len(prefix) :],
        "line": line,
        "sha256": sha256,
        "byte_offset": located[file][line][0],
    }


def _write_group(
    scanner: str,
    number: int,
    groups: list[tuple[str, str]],
    owner: dict[str, int],
    hits: HitIndex,
    located: dict[str, dict[int, tuple[int, str]]],
    index_dir: Path,
) -> int:
    """Write one dataset's `<name>.jsonl`, then its `<name>.counts.json`. Returns its rows."""
    name, prefix = groups[number]
    path = index_dir / f"{name}.jsonl"
    partial = index_dir / f".{name}.jsonl.partial"
    rows = findings = 0
    by_reason: Counter = Counter()
    by_name: Counter = Counter()
    try:
        with partial.open("w", encoding="utf-8") as handle:
            for file in sorted(file for file, group in owner.items() if group == number):
                by_line = hits.reasons_for(file)
                expected = hits.expected_for(file)
                for line in sorted(by_line):
                    parts = _entry_parts(scanner, by_line[line])
                    names = set(parts["labels"]) | set(parts["categories"])
                    total = len(parts["findings"])
                    if scanner == "dedup":
                        parts["duplicate_of"] = _kept_copy(by_line[line], owner, groups, located)
                        if parts["duplicate_of"]["dataset"] is not None:
                            names.add(parts["duplicate_of"]["dataset"])
                        total = 1
                    offset, preview = located[file][line]
                    entry = {
                        "file": file[len(prefix) :],
                        "line": line,
                        "sha256": expected[line][0],
                        "byte_offset": offset,
                        "preview": preview,
                        **parts,
                        "findings": parts["findings"][:MAX_FINDINGS_PER_ROW],
                        "findings_total": total,
                    }
                    # ASCII only: a lone surrogate in a text can't be written as UTF-8.
                    handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
                    rows += 1
                    findings += total
                    by_reason.update(parts["reasons"])
                    by_name.update(names)
        partial.replace(path)
        write_report(
            index_dir / f"{name}.counts.json",
            {
                "index_version": INDEX_VERSION,
                "dataset": name,
                "rows": rows,
                "findings": findings,
                "by_reason": _sorted(by_reason),
                "by_name": _sorted(by_name),
            },
        )
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise IndexBuildError(f"Couldn't write the index of {name}: {exc}") from exc
    return rows


def write_index(
    scanner: str,
    root: Path,
    results_dir: Path,
    groups: list[tuple[str, str]],
    index_dir: Path,
    protected: frozenset[str] = frozenset(),
) -> dict[str, int]:
    """Write the findings index of each of `groups` ([(name, prefix)], a prefix being the folder
    of the group's Prepared version under `root`, with a trailing "/", or "" when `root` is
    that version). Returns the rows flagged in each. A `protected` group (duplicates only) has
    nothing flagged: its index is empty, though the rows in the other groups that repeat its
    rows are still flagged, with its rows as the kept copies."""
    rows_by_file, owner = _rows_by_file(root, groups)
    hits = HitIndex()
    try:
        # Rules that keep every finding: the user's choices are applied when the index is read.
        READERS[scanner](results_dir, hits, FilterRules(), rows_by_file)
    except FindingsError as exc:
        raise IndexBuildError(str(exc)) from exc
    if hits.conflicts:
        raise IndexBuildError(
            f"The scan's findings disagree about the fingerprint of {len(hits.conflicts):,} row(s): "
            + " ".join(hits.conflicts[:3])
        )
    protected_numbers = {number for number, (name, _prefix) in enumerate(groups) if name in protected}
    for file, number in owner.items():
        if number in protected_numbers:
            hits.forget_findings(file)
    print(f"  {hits.rows_hit():,} flagged rows found", flush=True)

    # The copies that duplicates matched are read as well, to say where they start.
    kept: dict[str, set[int]] = {}
    if scanner == "dedup":
        for file in rows_by_file:
            for reasons in hits.reasons_for(file).values():
                for item in (item for items in reasons.values() for item in items):
                    place = item["duplicate_of"]
                    if place["file"] in rows_by_file:
                        kept.setdefault(place["file"], set()).add(place["line"])
    located = _locate_lines(root, hits, rows_by_file, kept)

    index_dir.mkdir(parents=True, exist_ok=True)
    rows: dict[str, int] = {}
    for number, (name, _prefix) in enumerate(groups):
        rows[name] = _write_group(scanner, number, groups, owner, hits, located, index_dir)
        print(f"  {name}: {rows[name]:,} rows in the index", flush=True)
    return rows


# -------------------------------------------------------------------------
# Running it
# -------------------------------------------------------------------------


def parse_group(text: str) -> tuple[str, str]:
    name, separator, prefix = text.partition("=")
    if not separator or not name or not prefix.endswith("/"):
        raise argparse.ArgumentTypeError(f"{text!r} isn't NAME=PREFIX with a PREFIX that ends in '/'.")
    return name, prefix


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scanner", required=True, choices=SCANNERS, help="Which scanner's results these are.")
    parser.add_argument("--root", type=Path, required=True, help="The folder of runs that was scanned.")
    parser.add_argument("--results-dir", type=Path, required=True, help="The scanner's results folder.")
    parser.add_argument("--out-dir", type=Path, required=True, help="Where coverage.json and tally.json go.")
    parser.add_argument("--files-from", type=Path, help="Check against only the files listed here (see coverage.py).")
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        type=parse_group,
        metavar="NAME=PREFIX",
        help="A dataset and the folder its files are under. Once for each dataset. Used for the "
        "duplicates tally, and to name each dataset's findings index.",
    )
    parser.add_argument("--coverage-only", action="store_true", help="Write coverage.json only.")
    parser.add_argument(
        "--protect",
        action="append",
        default=[],
        metavar="NAME",
        help="Duplicates only: a --group whose rows must not be flagged. Its rows still count as the kept "
        "copies of rows in the other groups. Its duplicates are left out of the tally, and its index is empty. "
        "Once for each protected group; not every group can be.",
    )
    parser.add_argument(
        "--index-dir", type=Path, help="Also write the findings index of each dataset here (see the top of this file)."
    )
    parser.add_argument(
        "--index-name",
        help="With --index-dir and no --group: the name of the one dataset that --root is the Prepared version of.",
    )
    args = parser.parse_args(argv)

    if args.protect:
        group_names = [name for name, _prefix in args.group]
        if args.scanner != "dedup":
            parser.error("--protect is only for the duplicates scanner.")
        if args.coverage_only:
            parser.error("--coverage-only writes no tally or index, so there is nothing to protect.")
        if len(set(args.protect)) != len(args.protect):
            parser.error("Each protected group can only be named once.")
        unknown = [name for name in args.protect if name not in group_names]
        if unknown:
            parser.error(f"--protect names {', '.join(unknown)}, which isn't a --group.")
        if set(args.protect) >= set(group_names):
            parser.error("Not every group can be protected: nothing would be flagged.")

    if args.index_dir is None:
        if args.index_name is not None:
            parser.error("--index-name needs --index-dir.")
    else:
        if args.coverage_only:
            parser.error("--coverage-only writes no index, so it can't be used with --index-dir.")
        if args.group and args.index_name is not None:
            parser.error("Give --index-name or --group, not both.")
        if not args.group and args.index_name is None:
            parser.error("--index-dir needs --index-name, or a --group for each dataset.")
        names = [name for name, _prefix in args.group] or [args.index_name]
        if len(set(names)) != len(names):
            parser.error("Each dataset can only be named once.")
        for name in names:
            if INDEX_NAME.fullmatch(name) is None:
                parser.error(f"{name!r} can't be used as an index name: use letters, digits, '-' and '_' (at most 64).")
    return args


def build_tally(
    scanner: str, results_dir: Path, groups: list[tuple[str, str]], protected: frozenset[str] = frozenset()
) -> dict[str, Any] | None:
    """The tally for this scanner's results, or None when it has none."""
    if scanner == "toxicity":
        body = toxicity_tally(results_dir)
    elif scanner == "ner":
        body = ner_tally(results_dir)
    elif scanner == "dedup" and groups:
        body = dedup_tally(results_dir, groups, protected)
    else:
        return None
    return {
        "tally": scanner,
        "version": TALLY_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **body,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.expanduser().resolve()
    results_dir = args.results_dir.expanduser().resolve()
    out_dir = args.out_dir.expanduser().resolve()
    try:
        coverage_path = check_output_path(out_dir / COVERAGE_FILE, [root, results_dir])
        tally_path = check_output_path(out_dir / TALLY_FILE, [root, results_dir])
        index_dir = check_output_path(args.index_dir, [root, results_dir]) if args.index_dir is not None else None
        only_files = read_file_list(args.files_from) if args.files_from is not None else None
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Coverage of the {args.scanner} scan in {results_dir}", flush=True)
    report = build_report(root, {args.scanner: results_dir}, only_files)
    try:
        write_report(coverage_path, report)
    except OSError as exc:
        print(f"error: couldn't write {coverage_path}: {exc}", file=sys.stderr)
        return 2
    print(f"Coverage {report['verdict']}: {len(report['problems'])} problem(s). Written to {coverage_path}", flush=True)

    if args.coverage_only:
        return 0
    try:
        tally = build_tally(args.scanner, results_dir, args.group, frozenset(args.protect))
    except TallyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if tally is None:
        print("Nothing to tally for this scan.", flush=True)
    else:
        try:
            write_report(tally_path, tally)
        except OSError as exc:
            print(f"error: couldn't write {tally_path}: {exc}", file=sys.stderr)
            return 2
        print(f"Tally written to {tally_path}", flush=True)

    if index_dir is None:
        return 0
    groups = args.group or [(args.index_name, "")]
    print(f"Findings index of the {args.scanner} scan, to {index_dir}", flush=True)
    try:
        write_index(args.scanner, root, results_dir, groups, index_dir, frozenset(args.protect))
    except (IndexBuildError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Index written to {index_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
