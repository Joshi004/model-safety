"""Read every kind of finding as one stream of `Finding`s, for the trace tool.

A finding names a file (relative to the folder that was scanned), a line, and
that line's fingerprint. This module only reads: it checks each finding names
those three things and stops on one that doesn't, naming the file and line. It
doesn't look at the Prepared data (the trace tool does that).

Every source is read in a fixed order, so two reads of the same files give the
same findings in the same order.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from modelsafety.contract.fingerprints import line_fingerprint
from modelsafety.contract.run_finder import SHA256_HEX, find_runs, read_json_or_none
from modelsafety.data_screening.pii.regex.pii_output import (
    HITS_DIRNAME,
    PII_CATEGORIES,
    STAGE1_NAME,
    STAGE3_NAME,
    parse_hit,
)
from modelsafety.filter.filter_rows import REMOVED_ROWS_FILENAME, SUMMARY_FILENAME
from modelsafety.filter.rules import (
    DEDUP_RESULTS_FILE,
    NER_SPANS_FILE,
    REGEX_PII_FILE,
    FindingsError,
    _allow_large_csv_fields,
)
from modelsafety.results.coverage import TOXICITY_SHARDS

NER_CANDIDATES_FILE = "llm_candidates.csv"

SIDE_ROW = "row"
SIDE_DUPLICATE_OF = "duplicate_of"


@dataclass(frozen=True, slots=True)
class Finding:
    """One finding, or one removed row, and the line it points at.

    `results_file` is relative to the source's results folder, and
    `results_line` is the line of that file (for a CSV, the line the record
    ends on). `file` is relative to `scan_root`. `side` is only set for
    duplicates: `row` is the duplicate, `duplicate_of` the row it duplicates.
    `reference` is true when that row is in a duplicates reference folder (cross
    mode), which isn't a run.
    """

    source: str
    results_file: str
    results_line: int
    scan_root: str
    file: str
    line: int
    sha256: str
    side: str | None = None
    reference: bool = False


@dataclass(frozen=True)
class Source:
    """One findings file (or set of files) and where it comes from.

    `scanner` is the scanner's name in `modelsafety/results/coverage.py`, None
    for a filter step's removed rows. `counted` says the number of findings for
    each file can be compared with the scan's own count.
    """

    name: str
    results_dir: Path
    scan_root: str
    scanner: str | None = None
    counted: bool = False


# -------------------------------------------------------------------------
# The sources
# -------------------------------------------------------------------------


def scanner_sources(
    scanner: str, results_dir: Path, scan_root: str
) -> tuple[list[Source], list[dict[str, str]]]:
    """The sources of one scanner's results folder, and the sources that were
    left out (each with the reason). Only regex PII's stage 3 can be left out:
    its folder isn't there when the extract stage didn't run."""
    skipped: list[dict[str, str]] = []
    if scanner == "regex_pii":
        sources = [Source("regex_pii_stage1", results_dir, scan_root, scanner, counted=True)]
        if (results_dir / STAGE3_NAME).is_dir():
            sources.append(Source("regex_pii_stage3", results_dir, scan_root, scanner))
        else:
            skipped.append(
                {
                    "source": "regex_pii_stage3",
                    "reason": f"There is no {STAGE3_NAME} folder in {results_dir}, so the extract stage didn't run.",
                }
            )
        return sources, skipped
    if scanner == "ner":
        return [
            Source("ner_all_spans", results_dir, scan_root, scanner, counted=True),
            Source("ner_llm_candidates", results_dir, scan_root, scanner),
        ], skipped
    if scanner == "toxicity":
        return [
            Source("toxicity_flagged", results_dir, scan_root, scanner, counted=True),
            Source("toxicity_guard", results_dir, scan_root, scanner),
        ], skipped
    if scanner == "dedup":
        return [Source("dedup_results", results_dir, scan_root, scanner, counted=True)], skipped
    raise ValueError(f"Unknown scanner: {scanner}")


def removed_rows_source(number: int, filter_root: Path) -> Source:
    """The removed rows of every filter run under `filter_root`. `number` counts
    the `--filter-root` options from 1, so two roots never share an output file."""
    return Source(f"filter_removed_rows_{number}", filter_root, "")


# -------------------------------------------------------------------------
# Reading
# -------------------------------------------------------------------------


def _check_location(where: str, file: Any, line: Any, sha256: Any) -> None:
    """Raise unless this names a file, a line number and a line fingerprint."""
    if not isinstance(file, str) or not file:
        raise FindingsError(f"{where}: the finding has no file.")
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        raise FindingsError(f"{where}: line {line!r} isn't a line number.")
    if not isinstance(sha256, str) or not SHA256_HEX.fullmatch(sha256):
        raise FindingsError(f"{where}: the line fingerprint of {file} line {line} isn't a SHA-256.")


def _iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """Each JSON object of a JSONL file, with its line number."""
    if not path.is_file():
        raise FindingsError(f"{path} is missing.")
    with path.open("r", encoding="utf-8") as handle:
        for number, text in enumerate(handle, 1):
            if not text.strip():
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                raise FindingsError(f"{path}:{number}: not valid JSON ({exc.msg}).") from exc
            if not isinstance(record, dict):
                raise FindingsError(f"{path}:{number}: not a JSON object.")
            yield number, record


def _iter_csv(path: Path, required: set[str]) -> Iterator[tuple[int, dict[str, str]]]:
    """Each record of a CSV file, with the line it ends on."""
    if not path.is_file():
        raise FindingsError(f"{path} is missing.")
    _allow_large_csv_fields()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise FindingsError(f"{path} doesn't have the columns {', '.join(sorted(required))}.")
        for row in reader:
            yield reader.line_num, row


def _csv_line(path: Path, number: int, row: dict[str, str]) -> int:
    try:
        return int(row["line"])
    except (TypeError, ValueError):
        raise FindingsError(f"{path}:{number}: line {row['line']!r} isn't a number.") from None


def _relative(source: Source, path: Path) -> str:
    return path.relative_to(source.results_dir).as_posix()


def _read_regex_pii_stage1(source: Source) -> Iterator[Finding]:
    """Stage 1: every match, `file|line|line fingerprint|value`, one file for
    each category. A category with no matches may have no file."""
    hits = source.results_dir / STAGE1_NAME / HITS_DIRNAME
    if not hits.is_dir():
        raise FindingsError(f"{hits} is missing.")
    for category in PII_CATEGORIES:
        path = hits / f"{category}.txt"
        if not path.is_file():
            continue
        results_file = _relative(source, path)
        with path.open("r", encoding="utf-8") as handle:
            for number, text in enumerate(handle, 1):
                if not text.strip():
                    continue
                try:
                    file, line, sha256, _value = parse_hit(text, path, number)
                except ValueError as exc:
                    raise FindingsError(str(exc)) from exc
                yield Finding(source.name, results_file, number, source.scan_root, file, line, sha256)


def _read_csv_findings(source: Source, path: Path, required: set[str]) -> Iterator[Finding]:
    results_file = _relative(source, path)
    for number, row in _iter_csv(path, required):
        where = f"{path}:{number}"
        line = _csv_line(path, number, row)
        _check_location(where, row["file"], line, row["source_sha256"])
        yield Finding(
            source.name, results_file, number, source.scan_root, row["file"], line, row["source_sha256"]
        )


def _read_regex_pii_stage3(source: Source) -> Iterator[Finding]:
    path = source.results_dir / STAGE3_NAME / REGEX_PII_FILE
    yield from _read_csv_findings(source, path, {"file", "line", "source_sha256"})


def _read_jsonl_findings(source: Source, path: Path) -> Iterator[Finding]:
    """A JSONL file with `source_file`, `source_line` and `source_sha256` in each record."""
    results_file = _relative(source, path)
    for number, record in _iter_jsonl(path):
        file, line, sha256 = record.get("source_file"), record.get("source_line"), record.get("source_sha256")
        _check_location(f"{path}:{number}", file, line, sha256)
        yield Finding(source.name, results_file, number, source.scan_root, file, line, sha256)


def _read_ner_all_spans(source: Source) -> Iterator[Finding]:
    yield from _read_jsonl_findings(source, source.results_dir / NER_SPANS_FILE)


def _read_ner_llm_candidates(source: Source) -> Iterator[Finding]:
    path = source.results_dir / NER_CANDIDATES_FILE
    yield from _read_csv_findings(source, path, {"file", "line", "source_sha256"})


def _toxicity_slices(results_dir: Path) -> Iterator[Path]:
    """The folder of each slice listed in `manifests/manifest.json`."""
    manifest_path = results_dir / "manifests" / "manifest.json"
    manifest = read_json_or_none(manifest_path)
    shards = manifest.get("shards") if isinstance(manifest, dict) else None
    if not isinstance(shards, list):
        raise FindingsError(f"{manifest_path} is missing or doesn't list the slices (shards).")
    for shard in shards:
        index = shard.get("index") if isinstance(shard, dict) else None
        if isinstance(index, bool) or not isinstance(index, int):
            raise FindingsError(f"{manifest_path} has a slice with no index.")
        yield results_dir / TOXICITY_SHARDS / f"shard_{index:05d}"


def _read_toxicity_flagged(source: Source) -> Iterator[Finding]:
    for shard_dir in _toxicity_slices(source.results_dir):
        yield from _read_jsonl_findings(source, shard_dir / "flagged.jsonl")


def _read_toxicity_guard(source: Source) -> Iterator[Finding]:
    for shard_dir in _toxicity_slices(source.results_dir):
        yield from _read_jsonl_findings(source, shard_dir / "guard_results" / "all.jsonl")


def _read_dedup_results(source: Source) -> Iterator[Finding]:
    """Each duplicate row, then the row it duplicates. In cross mode the second
    is in the reference folder."""
    path = source.results_dir / DEDUP_RESULTS_FILE
    results_file = _relative(source, path)
    for number, record in _iter_jsonl(path):
        where = f"{path}:{number}"
        file, line, sha256 = record.get("source_file"), record.get("source_line"), record.get("source_sha256")
        _check_location(where, file, line, sha256)
        yield Finding(source.name, results_file, number, source.scan_root, file, line, sha256, SIDE_ROW)
        original = record.get("duplicate_of")
        if not isinstance(original, dict):
            raise FindingsError(f"{where}: the record has no 'duplicate_of'.")
        _check_location(
            f"{where} (duplicate_of)",
            original.get("source_file"),
            original.get("source_line"),
            original.get("source_sha256"),
        )
        yield Finding(
            source.name,
            results_file,
            number,
            source.scan_root,
            original["source_file"],
            original["source_line"],
            original["source_sha256"],
            SIDE_DUPLICATE_OF,
            reference=original.get("dataset") == "reference",
        )


def _read_removed_rows(source: Source) -> Iterator[Finding]:
    """The removed rows of each filter run under the root. A removed row is a
    line of the run's parent run, so the finding starts there: its
    `scan_root` is the parent run's folder, and its `file` is inside it."""
    root = source.results_dir
    for run in find_runs(root)["runs"]:
        run_dir = Path(run["run_dir"])
        summary_path = run_dir / SUMMARY_FILENAME
        summary = read_json_or_none(summary_path)
        parent = summary.get("parent") if isinstance(summary, dict) else None
        parent_dir = parent.get("run_dir") if isinstance(parent, dict) else None
        if not isinstance(parent_dir, str) or not parent_dir:
            raise FindingsError(f"{summary_path} doesn't say which run it filtered (parent.run_dir).")

        path = run_dir / REMOVED_ROWS_FILENAME
        results_file = path.relative_to(root).as_posix()
        for number, entry in _iter_jsonl(path):
            where = f"{path}:{number}"
            origin = entry.get("_parent")
            if not isinstance(origin, dict):
                raise FindingsError(f"{where}: not a removed-row entry (no '_parent').")
            file, line, sha256 = origin.get("file"), origin.get("line"), origin.get("source_sha256")
            _check_location(where, file, line, sha256)
            saved = entry.get("line")
            if not isinstance(saved, str) or line_fingerprint(saved.encode("utf-8")) != sha256:
                raise FindingsError(
                    f"{where}: the saved line isn't the one with the fingerprint it records "
                    f"({file} line {line})."
                )
            yield Finding(source.name, results_file, number, parent_dir, file, line, sha256)


_READERS: dict[str, Callable[[Source], Iterator[Finding]]] = {
    "regex_pii_stage1": _read_regex_pii_stage1,
    "regex_pii_stage3": _read_regex_pii_stage3,
    "ner_all_spans": _read_ner_all_spans,
    "ner_llm_candidates": _read_ner_llm_candidates,
    "toxicity_flagged": _read_toxicity_flagged,
    "toxicity_guard": _read_toxicity_guard,
    "dedup_results": _read_dedup_results,
}


def iter_findings(source: Source) -> Iterator[Finding]:
    """Every finding of `source`. Raises `FindingsError` on the first one that
    can't be read, naming the file and line."""
    reader = _READERS.get(source.name)
    if reader is None:
        if source.name.startswith("filter_removed_rows_"):
            reader = _read_removed_rows
        else:
            raise ValueError(f"Unknown findings source: {source.name}")
    return reader(source)
