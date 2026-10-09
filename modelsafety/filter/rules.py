"""What the filter step removes: the rules, and the findings each rule reads.

``FilterRules`` says which findings remove a row. One reader per scanner
reads that scanner's findings, checks each one points at a real line of a
finished run, and adds the ones the rules remove to a ``HitIndex``. The filter
writer then looks each line up in the index.

A reader stops on any finding it can't read, and names the file and line.
Standard library only, apart from the shared contract code.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from modelsafety.contract.run_finder import SHA256_HEX, read_json_or_none
from modelsafety.data_screening.pii.regex.pii_output import PII_CATEGORIES, STAGE3_NAME
from modelsafety.data_screening.toxicity.check_guard_counts import RECOGNISED_VERDICTS
from modelsafety.results.coverage import TOXICITY_SHARDS

REGEX_PII_FILE = "pii_extract.csv"
NER_SPANS_FILE = "all_spans.jsonl"
DEDUP_RESULTS_FILE = "dedup_results.jsonl"

TOXICITY_REMOVABLE_VERDICTS = ("Unsafe", "Controversial")
GUARD_ERROR_CHOICES = ("remove", "refuse")
DEDUP_MODES = ("global", "cross")

# Each reason a row can be removed for, and the scanner it comes from.
REASON_SCANNERS = {
    "regex_pii": "regex_pii",
    "ner": "ner",
    "toxicity_unsafe": "toxicity",
    "toxicity_controversial": "toxicity",
    "toxicity_guard_error": "toxicity",
    "duplicate": "dedup",
    "duplicate_of_reference": "dedup",
}


class FindingsError(ValueError):
    """A scanner's findings can't be read, or point at a line that isn't there."""


@dataclass(frozen=True)
class FilterRules:
    """Which findings remove a row. A rule only matters for a scanner whose
    results are given to the filter."""

    regex_ignore_categories: tuple[str, ...] = ()
    ner_ignore_labels: tuple[str, ...] = ()
    toxicity_remove_verdicts: tuple[str, ...] = TOXICITY_REMOVABLE_VERDICTS
    # "remove": a guard error, or a verdict the guard didn't give, counts as
    # unsafe. "refuse": the filter won't start while any exist.
    toxicity_guard_errors: str = "remove"
    dedup_modes: tuple[str, ...] = DEDUP_MODES

    def validate(self) -> list[str]:
        """What is wrong with these rules, in plain words. Empty when they are fine."""
        problems = []
        unknown = sorted(set(self.regex_ignore_categories) - set(PII_CATEGORIES))
        if unknown:
            problems.append(
                f"Unknown regex PII categories to ignore: {', '.join(unknown)}. "
                f"They are: {', '.join(PII_CATEGORIES)}."
            )
        unknown = sorted(set(self.toxicity_remove_verdicts) - set(TOXICITY_REMOVABLE_VERDICTS))
        if unknown:
            problems.append(
                f"Unknown toxicity verdicts to remove: {', '.join(unknown)}. "
                f"They are: {', '.join(TOXICITY_REMOVABLE_VERDICTS)}."
            )
        if self.toxicity_guard_errors not in GUARD_ERROR_CHOICES:
            problems.append(
                f"toxicity_guard_errors must be one of {', '.join(GUARD_ERROR_CHOICES)}; "
                f"got {self.toxicity_guard_errors!r}."
            )
        unknown = sorted(set(self.dedup_modes) - set(DEDUP_MODES))
        if unknown:
            problems.append(
                f"Unknown duplicates modes: {', '.join(unknown)}. They are: {', '.join(DEDUP_MODES)}."
            )
        return problems

    def to_dict(self) -> dict[str, Any]:
        """The same rules always give the same dict, whatever order they were given in."""
        return {
            "regex_ignore_categories": sorted(set(self.regex_ignore_categories)),
            "ner_ignore_labels": sorted(set(self.ner_ignore_labels)),
            "toxicity_remove_verdicts": sorted(set(self.toxicity_remove_verdicts)),
            "toxicity_guard_errors": self.toxicity_guard_errors,
            "dedup_modes": sorted(set(self.dedup_modes)),
        }


class HitIndex:
    """The findings that remove rows, by file and line, and the fingerprint
    each finding says its line has.

    A `file` is a path relative to the scanned folder, as scanners write it.
    """

    def __init__(self) -> None:
        # file -> line -> reason -> [finding, ...]
        self._reasons: dict[str, dict[int, dict[str, list[dict[str, Any]]]]] = {}
        # file -> line -> (fingerprint, scanner that said so)
        self._expected: dict[str, dict[int, tuple[str, str]]] = {}
        self.conflicts: list[str] = []

    def expect(self, file: str, line: int, sha256: str, scanner: str) -> None:
        """Record that `scanner` says this line has this fingerprint."""
        by_line = self._expected.setdefault(file, {})
        known = by_line.get(line)
        if known is None:
            by_line[line] = (sha256, scanner)
        elif known[0] != sha256:
            self.conflicts.append(
                f"{file} line {line}: {known[1]} says its fingerprint is {known[0]}, "
                f"but {scanner} says {sha256}."
            )

    def add(
        self, file: str, line: int, sha256: str, scanner: str, reason: str, finding: dict[str, Any]
    ) -> None:
        """Record a finding that removes the row at this line."""
        self.expect(file, line, sha256, scanner)
        by_line = self._reasons.setdefault(file, {})
        reasons = by_line.get(line)
        if reasons is None:
            reasons = by_line[line] = {}
        reasons.setdefault(reason, []).append(finding)

    def reasons_for(self, file: str) -> dict[int, dict[str, list[dict[str, Any]]]]:
        return self._reasons.get(file, {})

    def expected_for(self, file: str) -> dict[int, tuple[str, str]]:
        return self._expected.get(file, {})

    def rows_hit(self) -> int:
        return sum(len(by_line) for by_line in self._reasons.values())


@dataclass
class FindingsRead:
    """What one reader found in a scanner's results."""

    scanner: str
    # Every finding the scanner wrote, per file, whether or not a rule removes
    # it. None when it can't be compared with the scan's own count.
    findings_by_file: Counter | None = None
    # Rows the rules remove, as findings (a row can have several).
    hit_findings: int = 0
    # Why the filter must refuse, in plain words.
    problems: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


# -------------------------------------------------------------------------
# Reading
# -------------------------------------------------------------------------


def _check_location(
    where: str, file: Any, line: Any, sha256: Any, rows_by_file: dict[str, int]
) -> None:
    """Raise unless this names a line of a finished run's file, with a fingerprint."""
    rows = rows_by_file.get(file) if isinstance(file, str) else None
    if rows is None:
        raise FindingsError(
            f"{where}: {file!r} isn't a file of a finished run under the scanned folder."
        )
    if not isinstance(line, int) or isinstance(line, bool) or not 1 <= line <= rows:
        raise FindingsError(f"{where}: line {line!r} isn't a line of {file} (it has {rows:,} rows).")
    if not isinstance(sha256, str) or not SHA256_HEX.fullmatch(sha256):
        raise FindingsError(f"{where}: the line fingerprint of {file} line {line} isn't a SHA-256.")


def _iter_json_lines(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """Each JSON object of a JSONL file, with a "path:line" label for messages."""
    if not path.is_file():
        raise FindingsError(f"{path} is missing.")
    with path.open("r", encoding="utf-8") as handle:
        for number, text in enumerate(handle, 1):
            if not text.strip():
                continue
            where = f"{path}:{number}"
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                raise FindingsError(f"{where}: not valid JSON ({exc.msg}).") from exc
            if not isinstance(record, dict):
                raise FindingsError(f"{where}: not a JSON object.")
            yield where, record


def _allow_large_csv_fields() -> None:
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def read_regex_pii(
    results_dir: Path, index: HitIndex, rules: FilterRules, rows_by_file: dict[str, int]
) -> FindingsRead:
    """Stage 3 (`stage3_extract/pii_extract.csv`): one row per match that passed
    stages 1 and 2, and that stage 3 checked against the Prepared line.

    Stage 3 has fewer matches than stage 1 on purpose, so its count can't be
    compared with the scan's own.
    """
    path = results_dir / STAGE3_NAME / REGEX_PII_FILE
    if not path.is_file():
        raise FindingsError(f"{path} is missing.")
    ignored: Counter = Counter()
    result = FindingsRead("regex_pii")
    _allow_large_csv_fields()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"file", "line", "source_sha256", "category", "PII"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise FindingsError(f"{path} doesn't have the columns {', '.join(sorted(required))}.")
        for row in reader:
            where = f"{path}:{reader.line_num}"
            try:
                line = int(row["line"])
            except (TypeError, ValueError):
                raise FindingsError(f"{where}: line {row['line']!r} isn't a number.") from None
            file, sha256, category = row["file"], row["source_sha256"], row["category"]
            _check_location(where, file, line, sha256, rows_by_file)
            if category not in PII_CATEGORIES:
                raise FindingsError(f"{where}: {category!r} isn't a regex PII category.")
            if category in rules.regex_ignore_categories:
                ignored[category] += 1
                continue
            index.add(file, line, sha256, "regex_pii", "regex_pii", {"category": category, "value": row["PII"]})
            result.hit_findings += 1
    result.details = {"ignored_by_category": dict(sorted(ignored.items()))}
    return result


def read_ner(
    results_dir: Path, index: HitIndex, rules: FilterRules, rows_by_file: dict[str, int]
) -> FindingsRead:
    """`all_spans.jsonl`: every span the model found. A span removes its row when
    the NER policy marked it `candidate_eligible` and its label isn't ignored."""
    result = FindingsRead("ner", findings_by_file=Counter())
    ignored: Counter = Counter()
    eligible_spans = 0
    for where, span in _iter_json_lines(results_dir / NER_SPANS_FILE):
        file, line, sha256 = span.get("source_file"), span.get("source_line"), span.get("source_sha256")
        _check_location(where, file, line, sha256, rows_by_file)
        result.findings_by_file[file] += 1
        eligible = span.get("candidate_eligible")
        if not isinstance(eligible, bool):
            raise FindingsError(f"{where}: the span has no true or false 'candidate_eligible'.")
        if not eligible:
            continue
        eligible_spans += 1
        label = span.get("label")
        if not isinstance(label, str):
            raise FindingsError(f"{where}: the span has no 'label'.")
        if label in rules.ner_ignore_labels:
            ignored[label] += 1
            continue
        index.add(
            file,
            line,
            sha256,
            "ner",
            "ner",
            {
                "label": label,
                "tier": span.get("tier"),
                "message_index": span.get("message_index"),
                "start": span.get("start"),
                "end": span.get("end"),
                "confidence": span.get("confidence"),
            },
        )
        result.hit_findings += 1
    result.details = {
        "eligible_spans": eligible_spans,
        "ignored_by_label": dict(sorted(ignored.items())),
    }
    return result


def read_toxicity(
    results_dir: Path, index: HitIndex, rules: FilterRules, rows_by_file: dict[str, int]
) -> FindingsRead:
    """Each slice's `guard_results/all.jsonl`: the messages stage 1 flagged, with
    Qwen3Guard's verdict. A guard error, or a verdict that isn't Safe, Unsafe or
    Controversial, counts as a guard error."""
    manifest_path = results_dir / "manifests" / "manifest.json"
    manifest = read_json_or_none(manifest_path)
    shards = manifest.get("shards") if isinstance(manifest, dict) else None
    if not isinstance(shards, list):
        raise FindingsError(f"{manifest_path} is missing or doesn't list the slices (shards).")

    result = FindingsRead("toxicity", findings_by_file=Counter())
    verdicts: Counter = Counter()
    guard_errors_by_slice: Counter = Counter()
    for shard in shards:
        shard_index = shard.get("index") if isinstance(shard, dict) else None
        if not isinstance(shard_index, int) or isinstance(shard_index, bool):
            raise FindingsError(f"{manifest_path} has a slice with no index.")
        name = f"shard_{shard_index:05d}"
        for where, record in _iter_json_lines(
            results_dir / TOXICITY_SHARDS / name / "guard_results" / "all.jsonl"
        ):
            file, line, sha256 = (
                record.get("source_file"),
                record.get("source_line"),
                record.get("source_sha256"),
            )
            _check_location(where, file, line, sha256, rows_by_file)
            result.findings_by_file[file] += 1
            guard = record.get("guard")
            if not isinstance(guard, dict):
                raise FindingsError(f"{where}: the record has no 'guard' verdict.")

            verdict = guard.get("safety")
            if "error" in guard or verdict not in RECOGNISED_VERDICTS:
                outcome = "guard_error"
                verdict = None
                guard_errors_by_slice[name] += 1
            else:
                outcome = verdict.lower()
            verdicts[outcome] += 1

            if outcome == "guard_error":
                if rules.toxicity_guard_errors != "remove":
                    continue
            elif verdict not in rules.toxicity_remove_verdicts:
                continue
            index.add(
                file,
                line,
                sha256,
                "toxicity",
                f"toxicity_{outcome}",
                {
                    "message_index": record.get("message_index"),
                    "role": record.get("role"),
                    "verdict": verdict,
                    "error": guard.get("error"),
                    "categories": guard.get("categories") or [],
                    "detoxify_score": record.get("toxicity"),
                    "detoxify_category": record.get("trigger_category"),
                },
            )
            result.hit_findings += 1

    if guard_errors_by_slice and rules.toxicity_guard_errors == "refuse":
        shown = ", ".join(f"{name}: {count:,}" for name, count in sorted(guard_errors_by_slice.items())[:5])
        result.problems.append(
            f"Toxicity has {sum(guard_errors_by_slice.values()):,} guard error(s) or verdict(s) "
            f"the guard didn't give ({shown}). Re-run them, or set the guard errors rule to remove."
        )
    result.details = {
        "outcomes": dict(sorted(verdicts.items())),
        "guard_errors_by_slice": dict(sorted(guard_errors_by_slice.items())),
    }
    return result


def read_dedup(
    results_dir: Path, index: HitIndex, rules: FilterRules, rows_by_file: dict[str, int]
) -> FindingsRead:
    """`dedup_results.jsonl`: every row marked as a duplicate. The row it
    duplicates is kept, so it only has to be a real line (global mode); in cross
    mode it is in the reference folder, which isn't a run, so it is only recorded."""
    summary_path = results_dir / "summary.json"
    summary = read_json_or_none(summary_path)
    mode = summary.get("mode") if isinstance(summary, dict) else None
    result = FindingsRead("dedup", findings_by_file=Counter())
    if mode not in DEDUP_MODES:
        raise FindingsError(f"{summary_path} is missing or doesn't say whether the mode is global or cross.")
    result.details = {"mode": mode}
    if mode not in rules.dedup_modes:
        # Not read, so there are no findings to compare with the scan's own count.
        result.findings_by_file = None
        result.problems.append(
            f"The duplicates results are {mode} mode, which the rules don't allow "
            f"(allowed: {', '.join(sorted(rules.dedup_modes)) or 'none'})."
        )
        return result

    reason = "duplicate" if mode == "global" else "duplicate_of_reference"
    for where, record in _iter_json_lines(results_dir / DEDUP_RESULTS_FILE):
        file, line, sha256 = record.get("source_file"), record.get("source_line"), record.get("source_sha256")
        _check_location(where, file, line, sha256, rows_by_file)
        result.findings_by_file[file] += 1
        original = record.get("duplicate_of")
        if not isinstance(original, dict):
            raise FindingsError(f"{where}: the record has no 'duplicate_of'.")
        finding = {
            "duplicate_of": {
                "file": original.get("source_file"),
                "line": original.get("source_line"),
                "source_sha256": original.get("source_sha256"),
            }
        }
        if mode == "global":
            _check_location(
                f"{where} (duplicate_of)",
                original.get("source_file"),
                original.get("source_line"),
                original.get("source_sha256"),
                rows_by_file,
            )
            index.expect(
                original["source_file"], original["source_line"], original["source_sha256"], "dedup"
            )
        else:
            if not isinstance(original.get("source_file"), str) or not isinstance(
                original.get("source_line"), int
            ):
                raise FindingsError(f"{where}: 'duplicate_of' has no file and line.")
            finding["duplicate_of"]["dataset"] = "reference"
        index.add(file, line, sha256, "dedup", reason, finding)
        result.hit_findings += 1
    return result


READERS = {
    "regex_pii": read_regex_pii,
    "ner": read_ner,
    "toxicity": read_toxicity,
    "dedup": read_dedup,
}
