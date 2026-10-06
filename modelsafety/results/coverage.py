#!/usr/bin/env python3
"""Cross-check conversion, NER, regex PII, and toxicity coverage for converted data.

Answers, without hand-counting anything: did conversion pick up every source
row, did the NER PII scan cover every converted row, did the toxicity scan
read every converted row, and did every scan read exactly the Prepared files
the conversion wrote? This script computes nothing new -- it reads what each
stage already wrote about itself and puts the totals side by side, per
dataset where possible and always as a grand total, then complains loudly if
they don't match.

Row counts are compared as before. Files are compared by path and SHA-256:
each dataset's `output.files_written` (paths prefixed with the dataset's
folder) against the `input.files` of the NER, toxicity and regex PII scans.
A scan with no `input` block fails, because nothing proves what it read.

Traceback to original source rows is explicitly out of scope here; see
scripts/verify_sources.py for the separate question of whether conversion
picked up everything that exists upstream.

Reads:
  - <converted-root>/*/*/summary.json          (one per conversion run)
  - <ner-run-dir>/summary.json                 (scanner.py aggregate output)
  - <toxicity-run-dir>/scans/toxicity/shards/*/detoxify_summary.json
    (written by run.py --summary-file; summaries recovered with
    scripts/recover_toxicity_summary.py have no `input` block and fail)
  - <pii-run-dir>/stage1_scan/input_files.json (regex PII stage 1)

Usage:
    modelsafety/results/coverage.py \\
        [--converted-root output/tool_team_converted] \\
        [--ner-run-dir output/tool_team_ner_run] \\
        [--toxicity-run-dir output/tool_team_toxicity_run] \\
        [--pii-run-dir output/tool_team_pii_run]

Exit code: 0 if every available check matches, 2 if anything mismatches or
a stage's output isn't available yet to check at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Run as a script, so only this folder is on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from modelsafety.contract.fingerprints import compare_files  # noqa: E402


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_conversion_summaries(converted_root: Path) -> dict[str, dict[str, Any]]:
    """One entry per dataset output directory (e.g. "sft_mix/apigen_mt_clean_v3",
    "mopd_mix/train"), which is exactly how the conversion itself lays out
    one summary.json per dataset."""
    datasets: dict[str, dict[str, Any]] = {}
    for summary_path in sorted(converted_root.glob("*/*/summary.json")):
        dataset = summary_path.parent.relative_to(converted_root).as_posix()
        datasets[dataset] = _load(summary_path)
    return datasets


def load_ner_summary(ner_run_dir: Path) -> dict[str, Any] | None:
    summary_path = ner_run_dir / "summary.json"
    return _load(summary_path) if summary_path.is_file() else None


def load_regex_pii_input(pii_run_dir: Path) -> dict[str, Any] | None:
    input_path = pii_run_dir / "stage1_scan" / "input_files.json"
    return _load(input_path) if input_path.is_file() else None


def load_toxicity_summaries(toxicity_run_dir: Path) -> list[dict[str, Any]]:
    shards_dir = toxicity_run_dir / "scans" / "toxicity" / "shards"
    if not shards_dir.is_dir():
        return []
    summaries = []
    for shard_dir in sorted(shards_dir.iterdir()):
        summary_path = shard_dir / "detoxify_summary.json"
        if summary_path.is_file():
            summaries.append({**_load(summary_path), "_shard": shard_dir.name})
    return summaries


def ner_rows_for_dataset(
    ner_summary: dict[str, Any] | None, dataset_prefix: str
) -> tuple[int, int]:
    """Sum NER's own per-source-file rows/messages across every converted
    file whose relative path is under this dataset's output directory."""
    if ner_summary is None:
        return 0, 0
    rows = messages = 0
    for source_file, values in ner_summary.get("sources", {}).items():
        if source_file == dataset_prefix or source_file.startswith(dataset_prefix + "/"):
            rows += values.get("rows", 0)
            messages += values.get("messages", 0)
    return rows, messages


def toxicity_totals(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Grand totals always available from any mix of native/recovered
    summaries; a merged per_file breakdown only when every summary that
    contributed has one (native runs only -- recovered ones don't)."""
    rows = sum(s.get("rows_read", 0) for s in summaries)
    messages = sum(s.get("messages_scored", 0) for s in summaries)
    flagged = sum(s.get("messages_flagged", 0) for s in summaries)

    per_shard_errors = [
        {"shard": s["_shard"], "errors": s.get("guard_errors")}
        for s in summaries
        if s.get("guard_errors")
    ]
    total_errors = sum(e["errors"] for e in per_shard_errors)

    has_per_file = bool(summaries) and all(s.get("per_file") is not None for s in summaries)
    per_file: dict[str, dict[str, int]] | None = {} if has_per_file else None
    if has_per_file:
        for s in summaries:
            for source_file, values in s["per_file"].items():
                entry = per_file.setdefault(source_file, {"rows": 0, "messages": 0})  # type: ignore[union-attr]
                entry["rows"] += values.get("rows", 0)
                entry["messages"] += values.get("messages", 0)

    return {
        "rows": rows,
        "messages": messages,
        "flagged": flagged,
        "guard_errors_total": total_errors,
        "guard_errors_by_shard": per_shard_errors,
        "per_file": per_file,
    }


def toxicity_rows_for_dataset(
    tox_totals: dict[str, Any], dataset_prefix: str
) -> tuple[int | None, int | None]:
    per_file = tox_totals.get("per_file")
    if per_file is None:
        return None, None
    rows = messages = 0
    for source_file, values in per_file.items():
        if source_file == dataset_prefix or source_file.startswith(dataset_prefix + "/"):
            rows += values["rows"]
            messages += values["messages"]
    return rows, messages


def expected_files(conversion: dict[str, dict[str, Any]]) -> tuple[dict[str, str], list[str]]:
    """Every Prepared file the conversions wrote, as {path under the converted
    root: sha256}, plus the datasets whose summary.json doesn't list them."""
    expected: dict[str, str] = {}
    without_files: list[str] = []
    for dataset, summary in sorted(conversion.items()):
        written = (summary.get("output") or {}).get("files_written")
        if not isinstance(written, list):
            without_files.append(dataset)
            continue
        for entry in written:
            expected[f"{dataset}/{entry['path']}"] = entry["sha256"]
    return expected, without_files


def files_in_block(block: Any) -> dict[str, str] | None:
    """{path: sha256} from a scan's `input` block, or None if it has no usable file list."""
    if not isinstance(block, dict) or not isinstance(block.get("files"), list):
        return None
    try:
        return {item["path"]: item["sha256"] for item in block["files"]}
    except (KeyError, TypeError):
        return None


def check_scan_files(
    label: str,
    where: str,
    block: Any,
    expected: dict[str, str],
    failures: list[str],
    file_differences: dict[str, dict[str, list[str]]],
) -> None:
    """Compare one scan's `input` block with the expected files.

    A scan with no usable block, or with different files, adds a message to
    `failures`. Its differences go in `file_differences[label]` whenever it
    has a usable block.
    """
    actual = files_in_block(block)
    if actual is None:
        failures.append(
            f"{label}: {where} has no input block listing the files it read "
            "(it was made before scans recorded them, or was recovered from logs)"
        )
        return
    differences = compare_files(expected, actual)
    file_differences[label] = differences
    if any(differences.values()):
        examples = "; ".join(
            f"{kind} e.g. {paths[:5]}" for kind, paths in differences.items() if paths
        )
        failures.append(
            f"{label} did not read exactly the files conversion wrote "
            f"({where}, root {block.get('root')}): "
            f"{len(differences['missing'])} missing, {len(differences['extra'])} extra, "
            f"{len(differences['different'])} with a different SHA-256. {examples}"
        )


def combined_toxicity_input(
    summaries: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Every shard's `input` block as one block, or None plus the shards
    that have no usable one."""
    shards_without = [s["_shard"] for s in summaries if files_in_block(s.get("input")) is None]
    if shards_without:
        return None, shards_without
    files = [item for s in summaries for item in s["input"]["files"]]
    roots = sorted({str(s["input"].get("root")) for s in summaries})
    return {"root": ", ".join(roots), "files": files}, []


def build_rows(
    conversion: dict[str, dict[str, Any]],
    ner_summary: dict[str, Any] | None,
    tox_totals: dict[str, Any],
    tox_available: bool,
    file_differences: dict[str, dict[str, list[str]]],
) -> list[dict[str, Any]]:
    rows = []
    for dataset, conv_summary in sorted(conversion.items()):
        converted_rows = conv_summary["rows"]["written"]
        ner_rows, ner_messages = ner_rows_for_dataset(ner_summary, dataset)
        tox_rows, tox_messages = toxicity_rows_for_dataset(tox_totals, dataset)

        problems = []
        if ner_summary is not None and ner_rows != converted_rows:
            problems.append("NER")
        if tox_rows is not None and tox_rows != converted_rows:
            problems.append("toxicity")
        for label, differences in file_differences.items():
            differing = [path for paths in differences.values() for path in paths]
            if any(path.startswith(dataset + "/") for path in differing):
                problems.append(f"{label} files")
        status = "MISMATCH: " + ",".join(problems) if problems else "OK"

        rows.append(
            {
                "dataset": dataset,
                "converted_rows": converted_rows,
                "ner_rows": ner_rows if ner_summary is not None else None,
                "ner_messages": ner_messages if ner_summary is not None else None,
                "tox_rows": tox_rows,
                "tox_messages": tox_messages,
                "status": status,
            }
        )
    return rows


def fmt(value: Any) -> str:
    if value is None:
        return "-"
    return f"{value:,}"


def print_table(rows: list[dict[str, Any]], tox_available: bool) -> None:
    width = max((len(r["dataset"]) for r in rows), default=8)
    tox_cols = "  tox_rows  tox_msgs" if tox_available else ""
    print(f"{'dataset':<{width}}  converted  ner_rows  ner_msgs{tox_cols}  status")
    for r in rows:
        line = (
            f"{r['dataset']:<{width}}  {fmt(r['converted_rows']):>9}  "
            f"{fmt(r['ner_rows']):>8}  {fmt(r['ner_messages']):>8}"
        )
        if tox_available:
            line += f"  {fmt(r['tox_rows']):>8}  {fmt(r['tox_messages']):>8}"
        line += f"  {r['status']}"
        print(line)


def parse_args() -> argparse.Namespace:
    repo = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--converted-root", type=Path, default=repo / "output" / "tool_team_converted"
    )
    parser.add_argument(
        "--ner-run-dir", type=Path, default=repo / "output" / "tool_team_ner_run"
    )
    parser.add_argument(
        "--toxicity-run-dir", type=Path, default=repo / "output" / "tool_team_toxicity_run"
    )
    parser.add_argument(
        "--pii-run-dir", type=Path, default=repo / "output" / "tool_team_pii_run"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    converted_root = args.converted_root.expanduser().resolve()
    ner_run_dir = args.ner_run_dir.expanduser().resolve()
    toxicity_run_dir = args.toxicity_run_dir.expanduser().resolve()
    pii_run_dir = args.pii_run_dir.expanduser().resolve()

    if not converted_root.is_dir():
        print(f"error: converted root does not exist: {converted_root}", file=sys.stderr)
        return 2

    conversion = load_conversion_summaries(converted_root)
    if not conversion:
        print(f"error: no conversion summary.json files found under {converted_root}", file=sys.stderr)
        return 2

    ner_summary = load_ner_summary(ner_run_dir)
    tox_summaries = load_toxicity_summaries(toxicity_run_dir)
    tox_totals = toxicity_totals(tox_summaries)
    tox_available = bool(tox_summaries)
    regex_input = load_regex_pii_input(pii_run_dir)

    file_failures: list[str] = []
    file_differences: dict[str, dict[str, list[str]]] = {}
    expected, datasets_without_files = expected_files(conversion)
    if datasets_without_files:
        file_failures.append(
            f"{len(datasets_without_files)} conversion summary.json file(s) have no "
            f"output.files_written (e.g. {datasets_without_files[:5]}), so the files the "
            "scans read can't be checked -- convert again into a new folder"
        )
    else:
        if ner_summary is not None:
            check_scan_files(
                "NER",
                str(ner_run_dir / "summary.json"),
                ner_summary.get("input"),
                expected,
                file_failures,
                file_differences,
            )
        if tox_available:
            tox_input, shards_without_input = combined_toxicity_input(tox_summaries)
            if tox_input is None:
                file_failures.append(
                    f"toxicity: no input block listing the files it read in shard(s) "
                    f"{', '.join(shards_without_input)} (recovered from logs, or made before "
                    "scans recorded them)"
                )
            else:
                check_scan_files(
                    "toxicity",
                    str(toxicity_run_dir / "scans" / "toxicity" / "shards" / "*" / "detoxify_summary.json"),
                    tox_input,
                    expected,
                    file_failures,
                    file_differences,
                )
        if regex_input is not None:
            check_scan_files(
                "regex PII",
                str(pii_run_dir / "stage1_scan" / "input_files.json"),
                regex_input,
                expected,
                file_failures,
                file_differences,
            )

    rows = build_rows(conversion, ner_summary, tox_totals, tox_available, file_differences)

    print("=== Per-dataset coverage ===")
    print_table(rows, tox_available)

    total_converted = sum(s["rows"]["written"] for s in conversion.values())
    incomplete = []
    mismatches = list(file_failures)

    print()
    print("=== Totals ===")
    print(f"Conversion:  {total_converted:,} rows written across {len(conversion)} dataset(s)")

    if ner_summary is None:
        print("NER:         not aggregated yet (no summary.json under --ner-run-dir)")
        incomplete.append("NER")
    else:
        print(f"NER:         {ner_summary['rows']:,} rows, {ner_summary['messages']:,} messages scanned")
        if ner_summary["rows"] != total_converted:
            mismatches.append(f"NER scanned {ner_summary['rows']:,} rows, conversion wrote {total_converted:,}")

    if not tox_available:
        print("Toxicity:    no detoxify_summary.json files found under --toxicity-run-dir")
        incomplete.append("toxicity")
    else:
        print(
            f"Toxicity:    {tox_totals['rows']:,} rows, {tox_totals['messages']:,} messages read "
            f"across {len(tox_summaries)} shard(s), {tox_totals['flagged']:,} flagged"
        )
        if tox_totals["rows"] != total_converted:
            mismatches.append(
                f"toxicity read {tox_totals['rows']:,} rows, conversion wrote {total_converted:,}"
            )
        if ner_summary is not None and tox_totals["messages"] != ner_summary["messages"]:
            mismatches.append(
                f"toxicity scored {tox_totals['messages']:,} messages, "
                f"NER scanned {ner_summary['messages']:,}"
            )

    if regex_input is None:
        print("Regex PII:   no stage1_scan/input_files.json under --pii-run-dir")
        incomplete.append("regex PII")
    else:
        regex_files = files_in_block(regex_input)
        listed = f"{len(regex_files):,}" if regex_files is not None else "no"
        print(f"Regex PII:   {listed} files listed in stage1_scan/input_files.json")

    per_dataset_mismatches = [r for r in rows if r["status"] != "OK"]
    if per_dataset_mismatches:
        mismatches.append(f"{len(per_dataset_mismatches)} dataset(s) mismatched -- see table above")

    if tox_available and tox_totals["guard_errors_total"]:
        print()
        print(
            f"WARNING: {tox_totals['guard_errors_total']} guard error(s) across "
            f"{len(tox_totals['guard_errors_by_shard'])} shard(s) -- these count as unsafe "
            "downstream, not a coverage problem:"
        )
        for entry in tox_totals["guard_errors_by_shard"]:
            print(f"  {entry['shard']}: {entry['errors']} error(s)")

    print()
    if mismatches:
        print("FAIL:")
        for problem in mismatches:
            print(f"  - {problem}")
        return 2
    if incomplete:
        print(f"INCOMPLETE: {', '.join(incomplete)} not available to check yet.")
        return 2
    print(
        "PASS: conversion, NER, toxicity, and regex PII all agree on row/message counts, "
        "and every scan read exactly the files conversion wrote."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
