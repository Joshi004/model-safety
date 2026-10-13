#!/usr/bin/env python3
"""A chain record: the name of a chain and where its folders are.

A chain is the Prepared runs, each filter step run over them, the scans each
step read, and the coverage reports, trace and row status made from them. Most
of that can be read back from the last filter step's own files (each filter run's
`summary.json` and `run_identity.json` say which folder it filtered, with what
rules and which scanner results), so the record only holds what can't be:

  <chain-dir>/chain.json
      name, the last filter step's folder, the Prepared folder, the coverage
      reports, and the trace and row status folders.

`read_chain` follows the filter runs back to the Prepared runs, reading ONE run of
each step (the first, in name order), not every run, so it is quick. It reports
what doesn't fit together as `problems` instead of raising. Checking every run
of a chain is what `row_status.py` does.

There are two versions of the record. Version 1 (`create`) is written after the
chain has run, and names the last filter step and the files that exist. Version 2
(`create_planned_chain`, used by the validator) is written BEFORE the chain runs:
every step has its place in the chain's folder (see `LAYOUT`), so the record names
folders and files that don't exist yet. `read_chain` reads both; for a version 2
record it takes the steps from `LAYOUT` until the last filter step has a run to
follow back, and doesn't report a coverage report that isn't there yet as a problem.

Nothing is changed in any folder this reads. `create` writes only `chain.json`,
and refuses to replace a different one.

Standard library only, so the validator and the launchers can import it:

    python3 modelsafety/contract/chain.py create --chain-dir C --name N --last-filter-root R \\
        [--coverage F ...] [--trace-dir T] [--row-status-dir S]
    python3 modelsafety/contract/chain.py show --chain-dir C
    python3 modelsafety/contract/chain.py list --root OUTPUT
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Run as a script, so only this folder is on sys.path.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modelsafety.contract.run_finder import (  # noqa: E402
    FILTER_ROWS_TOOL,
    MAX_RUN_FOLDERS,
    PREPARE_ROWS_TOOL,
    SUCCESS_FILENAME,
    read_json_or_none,
)
from modelsafety.contract.run_identity import IDENTITY_FILENAME  # noqa: E402

CHAIN_FILENAME = "chain.json"
CHAIN_TOOL = "chain_record"
# Written by `create_chain`, after the chain has run.
CHAIN_VERSION = 1
# Written by `create_planned_chain`, before it runs.
PLANNED_CHAIN_VERSION = 2
READABLE_VERSIONS = (CHAIN_VERSION, PLANNED_CHAIN_VERSION)
# Where each part of a version 2 chain lives, relative to the chain's folder. The
# validator starts every step into its place here, so a chain that was started
# from the UI is always laid out like this.
LAYOUT = {
    "dedup": "scans/step1_dedup",
    "coverage_step1": "coverage/step1_dedup.json",
    "filter_step1": "filter_step1",
    "regex_pii": "scans/step2_regex_pii",
    "ner": "scans/step2_ner",
    "toxicity": "scans/step2_toxicity",
    "coverage_step2": "coverage/step2.json",
    "filter_step2": "filter_step2",
    "trace": "trace",
    "row_status": "row_status",
}
# The scanners each filter step reads, with the tool name a filter run records for each.
LAYOUT_STEP_SCANNERS = {
    1: {"dedup": "dedup"},
    2: {"regex_pii": "pii_regex", "ner": "pii_model", "toxicity": "toxicity"},
}
SUMMARY_FILENAME = "summary.json"
# Tools whose summary.json the trace and the row status write.
TRACE_TOOL = "trace_findings.py"
ROW_STATUS_TOOL = "row_status.py"
COVERAGE_REPORT = "coverage"
# A filter run's parent can be another filter run; no real chain is this long.
MAX_HOPS = 20
MAX_NAME_CHARS = 100
# The first version of the row status summary that has the removed-rows index.
REMOVED_INDEX_VERSION = 2
PARTIAL_SUFFIX = ".partial"


class ChainError(ValueError):
    """A chain record can't be made, or a `chain.json` can't be used."""


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _same_path(first: Any, second: Any) -> bool:
    """Whether two values are paths to the same place. A value that isn't a path is never the same."""
    if not isinstance(first, (str, Path)) or not isinstance(second, (str, Path)):
        return False
    return _resolve(first) == _resolve(second)


def _write_json(path: Path, value: Any) -> None:
    temp = path.with_name(path.name + PARTIAL_SUFFIX)
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


# -------------------------------------------------------------------------
# Walking back from the last filter step
# -------------------------------------------------------------------------


def _first_run(root: Path) -> tuple[Path, dict[str, Any]] | None:
    """The first run folder in `root` (or `root` itself), in name order, with its
    summary. Reads the `summary.json` of each folder only until one is a run's."""
    folders = 0
    for current, dir_names, file_names in os.walk(root):
        dir_names.sort()
        folders += 1
        if folders > MAX_RUN_FOLDERS:
            return None
        if SUMMARY_FILENAME not in file_names:
            continue
        summary = read_json_or_none(Path(current) / SUMMARY_FILENAME)
        if isinstance(summary, dict) and summary.get("tool") in (PREPARE_ROWS_TOOL, FILTER_ROWS_TOOL):
            return Path(current), summary
    return None


def _step_of(
    run_dir: Path, summary: dict[str, Any], output_root: Path, problems: list[str]
) -> dict[str, Any] | None:
    """What a filter run says about the step that made it, or None (with a problem)."""
    identity = read_json_or_none(run_dir / IDENTITY_FILENAME)
    settings = identity.get("settings") if isinstance(identity, dict) else None
    parent = summary.get("parent")
    if not isinstance(settings, dict) or not isinstance(parent, dict):
        problems.append(f"{run_dir}: {IDENTITY_FILENAME} or {SUMMARY_FILENAME} doesn't say which run it filtered.")
        return None
    scan_root, rules, scanners = settings.get("scan_root"), settings.get("rules"), settings.get("scanner_results")
    if not isinstance(scan_root, str) or not isinstance(rules, dict) or not isinstance(scanners, dict):
        problems.append(f"{run_dir / IDENTITY_FILENAME} doesn't record the scan root, the rules and the scanner results.")
        return None
    if not _same_path(parent.get("scan_root"), scan_root):
        problems.append(f"{run_dir}: {IDENTITY_FILENAME} and {SUMMARY_FILENAME} disagree about the scanned folder.")
        return None
    return {
        "output_root": str(output_root),
        "scan_root": scan_root,
        "rules": rules,
        "scanners": scanners,
        "coverage": None,
    }


def _walk_steps(last_filter_root: Path, problems: list[str]) -> tuple[list[dict[str, Any]], Path | None]:
    """The steps of the chain ending at `last_filter_root`, numbered from 1 and
    first step first, and the folder of the Prepared runs. When the steps can't be
    followed back to Prepared runs, it gives no steps and no folder, and says why
    in `problems`."""
    steps: list[dict[str, Any]] = []
    output_root = last_filter_root
    for _ in range(MAX_HOPS + 1):
        found = _first_run(output_root)
        if found is None:
            problems.append(f"{output_root} holds no run.")
            return [], None
        run_dir, summary = found
        if summary.get("tool") == PREPARE_ROWS_TOOL:
            ordered = list(reversed(steps))
            for number, step in enumerate(ordered, 1):
                step["number"] = number
            return ordered, output_root
        step = _step_of(run_dir, summary, output_root, problems)
        if step is None:
            return [], None
        steps.append(step)
        output_root = _resolve(step["scan_root"])
    problems.append(f"The chain went through {MAX_HOPS} steps without reaching a Prepared run.")
    return [], None


def _layout_steps(
    chain_dir: Path, record: dict[str, Any], problems: list[str]
) -> list[dict[str, Any]]:
    """The two steps of a version 2 chain, from `LAYOUT`. A step whose filter
    folder already holds a run says what that run says (its rules and scanner
    results, as `_walk_steps` would); the others have `rules` None and the
    scanners the layout gives them, and say `planned: True`."""
    prepared_root = _resolve(record["prepared_root"])
    steps: list[dict[str, Any]] = []
    scan_root = prepared_root
    for number, output_name in ((1, LAYOUT["filter_step1"]), (2, LAYOUT["filter_step2"])):
        output_root = chain_dir / output_name
        step: dict[str, Any] | None = None
        found = _first_run(output_root)
        if found is not None:
            step = _step_of(found[0], found[1], output_root, problems)
            if step is not None and not _same_path(step["scan_root"], scan_root):
                problems.append(
                    f"Step {number} filtered {step['scan_root']}, but this chain's layout has it filter {scan_root}."
                )
        if step is None:
            step = {
                "output_root": str(output_root),
                "scan_root": str(scan_root),
                "rules": None,
                "scanners": {
                    name: {"results_dir": str(chain_dir / LAYOUT[name]), "tool": tool, "mode": None}
                    for name, tool in LAYOUT_STEP_SCANNERS[number].items()
                },
                "coverage": None,
                "planned": True,
            }
        step["number"] = number
        steps.append(step)
        scan_root = output_root
    return steps


def chain_folders(chain_dir: str | Path) -> list[Path]:
    """The folders the chain's runs are in: the Prepared folder, then the output
    folder of each filter step. Empty if the chain's record or its steps can't be read.
    Reads one run of each step, nothing else. A version 2 chain lists the filter
    folders that exist, because the later ones aren't made until their step runs."""
    record = _read_record(_resolve(chain_dir), [])
    if record is None:
        return []
    if record["version"] == PLANNED_CHAIN_VERSION:
        folder = _resolve(chain_dir)
        filter_folders = [folder / LAYOUT["filter_step1"], folder / LAYOUT["filter_step2"]]
        return [_resolve(record["prepared_root"]), *(item for item in filter_folders if item.is_dir())]
    steps, prepared_root = _walk_steps(_resolve(record["last_filter_root"]), [])
    if prepared_root is None:
        return []
    return [prepared_root, *(Path(step["output_root"]) for step in steps)]


# -------------------------------------------------------------------------
# The record
# -------------------------------------------------------------------------


def _read_record(chain_dir: Path, problems: list[str]) -> dict[str, Any] | None:
    """The `chain.json` of a chain folder, or None (with a problem)."""
    path = chain_dir / CHAIN_FILENAME
    if not path.is_file():
        problems.append(f"{chain_dir} has no {CHAIN_FILENAME}.")
        return None
    record = read_json_or_none(path)
    if not isinstance(record, dict) or record.get("tool") != CHAIN_TOOL:
        problems.append(f"{path} isn't a chain record.")
        return None
    if record.get("version") not in READABLE_VERSIONS:
        versions = " and ".join(str(version) for version in READABLE_VERSIONS)
        problems.append(f"{path} is version {record.get('version')!r}; this code reads versions {versions}.")
        return None
    coverage = record.get("coverage")
    if (
        not isinstance(record.get("name"), str)
        or not isinstance(record.get("prepared_root"), str)
        or not isinstance(record.get("last_filter_root"), str)
        or not isinstance(coverage, list)
        or not all(isinstance(item, str) for item in coverage)
        or not all(record.get(key) is None or isinstance(record.get(key), str) for key in ("trace_dir", "row_status_dir"))
    ):
        problems.append(f"{path} is missing a name, folder or coverage list, or has one of the wrong kind.")
        return None
    return record


def _check_name(name: str) -> str:
    cleaned = name.strip() if isinstance(name, str) else ""
    if not cleaned or len(cleaned) > MAX_NAME_CHARS or not cleaned.isprintable():
        raise ChainError(f"The name must be 1 to {MAX_NAME_CHARS} printable characters.")
    return cleaned


def _check_coverage_file(path: Path) -> None:
    report = read_json_or_none(path)
    if not isinstance(report, dict) or report.get("report") != COVERAGE_REPORT:
        raise ChainError(f"{path} isn't a coverage report (results/coverage.py --json-out).")


def create_chain(
    chain_dir: str | Path,
    name: str,
    last_filter_root: str | Path,
    coverage: Iterable[str | Path] = (),
    trace_dir: str | Path | None = None,
    row_status_dir: str | Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Write `<chain_dir>/chain.json`. Returns the record and whether it was
    written: False when the same record was already there.

    The Prepared folder is found by following the filter runs under
    `last_filter_root` back. Raises `ChainError` if that isn't a chain of at least
    one filter step, a coverage file isn't a coverage report, or the folder
    already has a different record (this never replaces one).
    """
    folder = _resolve(chain_dir)
    if not folder.is_dir():
        raise ChainError(f"{folder} isn't a folder. Make the chain's folder first.")
    last = _resolve(last_filter_root)
    problems: list[str] = []
    steps, prepared_root = _walk_steps(last, problems)
    if problems:
        raise ChainError("\n".join(problems))
    if not steps or prepared_root is None:
        raise ChainError(f"{last} holds Prepared runs, not filter runs, so it isn't the end of a chain.")

    coverage_files = [_resolve(item) for item in coverage]
    for path in coverage_files:
        _check_coverage_file(path)
    if len(set(coverage_files)) != len(coverage_files):
        raise ChainError("A coverage file is listed twice.")

    record = {
        "tool": CHAIN_TOOL,
        "version": CHAIN_VERSION,
        "name": _check_name(name),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "prepared_root": str(prepared_root),
        "last_filter_root": str(last),
        "coverage": [str(path) for path in coverage_files],
        "trace_dir": str(_resolve(trace_dir)) if trace_dir is not None else None,
        "row_status_dir": str(_resolve(row_status_dir)) if row_status_dir is not None else None,
    }

    return _write_record(folder, record)


def _write_record(folder: Path, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Write `record` as `<folder>/chain.json`. Returns the record and whether it was
    written: False when the same record (`created_at` aside) was already there.
    Raises `ChainError` rather than replace a different one."""
    path = folder / CHAIN_FILENAME
    if path.exists():
        existing = read_json_or_none(path)
        if not isinstance(existing, dict):
            raise ChainError(f"{path} exists but isn't a chain record, so it was left alone.")
        compared = {key: value for key, value in existing.items() if key != "created_at"}
        wanted = {key: value for key, value in record.items() if key != "created_at"}
        if compared == wanted:
            return existing, False
        different = sorted(key for key in set(compared) | set(wanted) if compared.get(key) != wanted.get(key))
        raise ChainError(
            f"{path} already holds a different record (it differs in {', '.join(different)}). "
            "It was left alone; delete it first to make a new one."
        )
    _write_json(path, record)
    return record, True


def create_planned_chain(
    chain_dir: str | Path, name: str, prepared_root: str | Path
) -> tuple[dict[str, Any], bool]:
    """Write a version 2 `<chain_dir>/chain.json`, before any step has run. Every
    other part of the chain is named by its place in `LAYOUT`, inside `chain_dir`.

    Returns the record and whether it was written. Raises `ChainError` if
    `chain_dir` or `prepared_root` isn't a folder, the name isn't usable, or the
    folder already has a different record (this never replaces one).
    """
    folder = _resolve(chain_dir)
    if not folder.is_dir():
        raise ChainError(f"{folder} isn't a folder. Make the chain's folder first.")
    prepared = _resolve(prepared_root)
    if not prepared.is_dir():
        raise ChainError(f"{prepared} isn't a folder, so it can't hold the chain's Prepared runs.")
    record = {
        "tool": CHAIN_TOOL,
        "version": PLANNED_CHAIN_VERSION,
        "name": _check_name(name),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "prepared_root": str(prepared),
        "last_filter_root": str(folder / LAYOUT["filter_step2"]),
        "coverage": [str(folder / LAYOUT["coverage_step1"]), str(folder / LAYOUT["coverage_step2"])],
        "trace_dir": str(folder / LAYOUT["trace"]),
        "row_status_dir": str(folder / LAYOUT["row_status"]),
    }
    return _write_record(folder, record)


# -------------------------------------------------------------------------
# Reading a chain back
# -------------------------------------------------------------------------


def _read_summary_of(folder: Path, tool: str) -> tuple[dict[str, Any] | None, str | None]:
    """A finished tool folder's summary.json (None until it has a `_SUCCESS`), and
    a problem if the summary is there but isn't `tool`'s."""
    if not (folder / SUCCESS_FILENAME).is_file():
        return None, None
    summary = read_json_or_none(folder / SUMMARY_FILENAME)
    if not isinstance(summary, dict) or summary.get("tool") != tool:
        return None, f"{folder / SUMMARY_FILENAME} isn't a {tool} summary."
    return summary, None


def _attach_coverage(record: dict[str, Any], steps: list[dict[str, Any]], problems: list[str]) -> None:
    """Put each coverage report on the step that scanned the folder it checked. A
    version 2 record names its reports before they are made, so one that isn't
    there yet is left off, not reported."""
    for item in record["coverage"]:
        if record["version"] == PLANNED_CHAIN_VERSION and not Path(item).exists():
            continue
        report = read_json_or_none(Path(item))
        if not isinstance(report, dict) or report.get("report") != COVERAGE_REPORT:
            problems.append(f"{item} isn't a coverage report, or can't be read.")
            continue
        step = next((step for step in steps if _same_path(step["scan_root"], report.get("root"))), None)
        if step is None:
            problems.append(f"{item} checked {report.get('root')}, which no step of this chain scanned.")
        elif step["coverage"] is not None:
            problems.append(f"Step {step['number']} has two coverage reports: {step['coverage']['file']} and {item}.")
        else:
            step["coverage"] = {"file": item, "verdict": report.get("verdict"), "created_at": report.get("created_at")}


def _read_trace(
    record: dict[str, Any], steps: list[dict[str, Any]], problems: list[str]
) -> dict[str, Any] | None:
    folder = record.get("trace_dir")
    if folder is None:
        return None
    summary, problem = _read_summary_of(Path(folder), TRACE_TOOL)
    if problem:
        problems.append(problem)
    traced = {
        "dir": folder,
        "finished": summary is not None,
        "verdict": summary.get("verdict") if summary else None,
    }
    if summary is not None:
        inputs = summary.get("inputs")
        roots = inputs.get("filter_roots") if isinstance(inputs, dict) else None
        wanted = {str(_resolve(step["output_root"])) for step in steps}
        if not isinstance(roots, list) or {str(_resolve(item)) for item in roots if isinstance(item, str)} != wanted:
            problems.append(f"The trace in {folder} traced different filter steps from this chain's.")
    return traced


def _read_row_status(record: dict[str, Any], problems: list[str]) -> dict[str, Any] | None:
    folder = record.get("row_status_dir")
    if folder is None:
        return None
    summary, problem = _read_summary_of(Path(folder), ROW_STATUS_TOOL)
    if problem:
        problems.append(problem)
    status = {
        "dir": folder,
        "finished": summary is not None,
        "verdict": summary.get("verdict") if summary else None,
        "report_version": summary.get("report_version") if summary else None,
        "has_removed_index": False,
    }
    if summary is not None:
        version = summary.get("report_version")
        status["has_removed_index"] = isinstance(version, int) and version >= REMOVED_INDEX_VERSION
        inputs = summary.get("inputs")
        if not isinstance(inputs, dict) or not _same_path(inputs.get("last_filter_root"), record["last_filter_root"]):
            problems.append(f"The row status in {folder} was made from a different last filter step from this chain's.")
    return status


def read_chain(chain_dir: str | Path) -> dict[str, Any]:
    """Everything about one chain that its record and its last filter step can say:
    the steps (output folder, scanned folder, rules, scanner results, coverage
    report), and whether the trace and the row status are finished.

    Never raises for a chain that doesn't fit together: `problems` lists what
    doesn't (an empty list means it does) and the rest is filled in as far as it
    could be. `trace` and `row_status` are None when the record names no folder
    for them, and say `finished: False` while the folder has no `_SUCCESS`.
    """
    folder = _resolve(chain_dir)
    problems: list[str] = []
    result: dict[str, Any] = {
        "chain_dir": str(folder),
        "version": None,
        "name": None,
        "created_at": None,
        "prepared_root": None,
        "last_filter_root": None,
        "steps": [],
        "trace": None,
        "row_status": None,
        "problems": problems,
    }
    record = _read_record(folder, problems)
    if record is None:
        return result
    result.update(
        version=record["version"],
        name=record["name"],
        created_at=record.get("created_at"),
        prepared_root=record["prepared_root"],
        last_filter_root=record["last_filter_root"],
    )

    last_filter_root = _resolve(record["last_filter_root"])
    if record["version"] == PLANNED_CHAIN_VERSION and _first_run(last_filter_root) is None:
        # The last filter step hasn't produced a run yet, so there is nothing to follow back.
        steps, prepared_root = _layout_steps(folder, record, problems), _resolve(record["prepared_root"])
    else:
        steps, prepared_root = _walk_steps(last_filter_root, problems)
    result["steps"] = steps
    if prepared_root is not None and not _same_path(prepared_root, record["prepared_root"]):
        problems.append(f"The record says the Prepared runs are in {record['prepared_root']}, but the steps lead to {prepared_root}.")
    if not steps and prepared_root is not None:
        problems.append(f"{record['last_filter_root']} holds Prepared runs, not filter runs.")

    _attach_coverage(record, steps, problems)
    result["trace"] = _read_trace(record, steps, problems)
    result["row_status"] = _read_row_status(record, problems)
    return result


def list_chains(root: str | Path) -> dict[str, Any]:
    """The chains directly under `root`: each subfolder that has a `chain.json`.
    Reads only the records, so it is quick. A record that can't be used is listed
    with a `problem` instead of its fields."""
    folder = _resolve(root)
    result: dict[str, Any] = {"root": str(folder), "exists": folder.is_dir(), "chains": []}
    if not result["exists"]:
        return result
    try:
        children = sorted(child for child in folder.iterdir() if (child / CHAIN_FILENAME).is_file())
    except OSError:
        return result
    for child in children:
        problems: list[str] = []
        record = _read_record(child, problems)
        if record is None:
            result["chains"].append({"chain_dir": str(child), "problem": problems[0]})
            continue
        result["chains"].append(
            {
                "chain_dir": str(child),
                "version": record["version"],
                "name": record["name"],
                "created_at": record.get("created_at"),
                "prepared_root": record["prepared_root"],
                "last_filter_root": record["last_filter_root"],
            }
        )
    return result


# -------------------------------------------------------------------------
# Command line
# -------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="Write chain.json into the chain's folder.")
    create.add_argument("--chain-dir", type=Path, required=True, help="The chain's folder (it must exist).")
    create.add_argument("--name", required=True, help="The chain's name.")
    create.add_argument("--last-filter-root", type=Path, required=True, help="The output folder of the last filter step.")
    create.add_argument("--coverage", type=Path, action="append", default=[], help="A coverage report (JSON). Repeatable.")
    create.add_argument("--trace-dir", type=Path, help="The trace's output folder (may not exist yet).")
    create.add_argument("--row-status-dir", type=Path, help="The row status's output folder (may not exist yet).")

    show = commands.add_parser("show", help="Print what the record and the last filter step say about the chain.")
    show.add_argument("--chain-dir", type=Path, required=True)

    listing = commands.add_parser("list", help="Print the chains directly under a folder.")
    listing.add_argument("--root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "create":
        try:
            record, written = create_chain(
                args.chain_dir, args.name, args.last_filter_root, args.coverage, args.trace_dir, args.row_status_dir
            )
        except ChainError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"{'Wrote' if written else 'Already there:'} {_resolve(args.chain_dir) / CHAIN_FILENAME}")
        print(json.dumps(record, indent=2, ensure_ascii=False))
        return 0
    if args.command == "show":
        chain = read_chain(args.chain_dir)
        print(json.dumps(chain, indent=2, ensure_ascii=False))
        return 0 if not chain["problems"] else 1
    print(json.dumps(list_chains(args.root), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
