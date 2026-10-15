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
               worked out later, exactly, without reading the files again.
    ner        ``all_spans.jsonl``: the rows with a span the policy made eligible, and the
               eligible spans by label and tier.
    dedup      ``dedup_results.jsonl``, only when ``--group NAME=PREFIX`` is given (one for
               each dataset, PREFIX being the folder its files are under, with a trailing
               "/"): the duplicate rows of each group, by the group that holds the copy
               that was kept.
    regex_pii  nothing: its summary says it all.

The files are read line by line; the row sets are held in memory (a few GB for millions of
flagged rows), which the job script asks for.

Usage:
    python3 modelsafety/results/analyse.py --scanner toxicity \\
        --root DIR --results-dir DIR --out-dir DIR \\
        [--files-from FILE] [--group NAME=PREFIX ...] [--coverage-only]

Exit code: 0 when the files asked for were written, 2 otherwise (an unreadable result
file, or nowhere to write).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# Run as a script, so only this folder is on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modelsafety.contract.run_finder import read_json_or_none  # noqa: E402
from modelsafety.filter.rules import (  # noqa: E402
    DEDUP_RESULTS_FILE,
    NER_SPANS_FILE,
    guard_outcome,
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


class TallyError(ValueError):
    """A result file can't be read; the message names it."""


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


def _message_atoms(verdict: str, categories: list[str]) -> list[str]:
    # A verdict with no category still has to be told apart, and counted, so it has its own atom.
    return [f"{verdict}|{category}" for category in categories] or [f"{verdict}|"]


def toxicity_tally(results_dir: Path) -> dict[str, Any]:
    names = _slice_names(results_dir)
    keys = _RowKeys()
    atom_ids: dict[str, int] = {}
    row_masks: dict[int, int] = {}
    messages: Counter = Counter()
    judged = safe = guard_errors = 0

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
            else:
                raw = guard.get("categories")
                categories = sorted({c for c in (raw if isinstance(raw, list) else []) if isinstance(c, str) and c and c != "None"})
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


def dedup_tally(results_dir: Path, groups: list[tuple[str, str]]) -> dict[str, Any]:
    """`groups` is [(name, prefix)]. A file is in the group whose prefix starts its path."""
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
        duplicates[mine] += 1
        kept_in[mine][kept if kept is not None else "unassigned"] += 1
    return {
        "duplicates": sum(duplicates.values()) + unassigned,
        "unassigned": unassigned,
        "groups": {
            name: {"duplicates": duplicates[name], "kept_in": _sorted(kept_in[name])} for name, _prefix in groups
        },
    }


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
        help="Duplicates only: a dataset and the folder its files are under. Once for each dataset.",
    )
    parser.add_argument("--coverage-only", action="store_true", help="Write coverage.json only.")
    return parser.parse_args(argv)


def build_tally(scanner: str, results_dir: Path, groups: list[tuple[str, str]]) -> dict[str, Any] | None:
    """The tally for this scanner's results, or None when it has none."""
    if scanner == "toxicity":
        body = toxicity_tally(results_dir)
    elif scanner == "ner":
        body = ner_tally(results_dir)
    elif scanner == "dedup" and groups:
        body = dedup_tally(results_dir, groups)
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
        tally = build_tally(args.scanner, results_dir, args.group)
    except TallyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if tally is None:
        print("Nothing to tally for this scan.")
        return 0
    try:
        write_report(tally_path, tally)
    except OSError as exc:
        print(f"error: couldn't write {tally_path}: {exc}", file=sys.stderr)
        return 2
    print(f"Tally written to {tally_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
