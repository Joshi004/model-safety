"""Chains, for the Chains page: the stages of a chain, how far each has got, and
what each one found.

A chain is the Prepared runs, each filter step run over them, the scans each step
read, and the coverage reports, trace and row status made from them (see
modelsafety/contract/chain.py). Every chain lives in its own folder directly under
the chains root (`runs.chains_root()`), with a `chain.json`.

Everything here is read-only. A stage's state comes from the files it leaves behind
(`_SUCCESS` markers, `summary.json`, the coverage report), not from SLURM, so it is
one of `not_started` (nothing there), `unfinished` (started, no finished marker; the
job may be queued, running or dead) and `finished`. `steps.py` (the job records)
refines the ones that aren't finished into `queued`, `running` or `failed`, where a
record says which (the caller passes it in as `refine`; this module never imports it).

The numbers on the page come from small files: the `summary.json` of each Prepared
and filter run, the row status's `summary.json` and the coverage reports. No data
file is opened.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, NamedTuple

import runs
from reader_imports import (
    CHAIN_LAYOUT,
    RowStatusReadError,
    list_chains,
    list_runs,
    read_chain,
    read_json_or_none,
    read_row_status_summary,
)

SUCCESS = "_SUCCESS"
CHAIN_FILENAME = "chain.json"

STATE_NOT_STARTED = "not_started"
STATE_UNFINISHED = "unfinished"
STATE_FINISHED = "finished"
# What the step records say about a stage that isn't finished (steps.refine_stages).
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_FAILED = "failed"

# Called with a chain's folder and its stages (as `chain_view` builds them), and changes
# them in place: how `steps.py` says which unfinished stages are queued, running or failed.
StageRefiner = Callable[[Path, list[dict[str, Any]]], None]

KIND_SCAN = "scan"
KIND_COVERAGE = "coverage"
KIND_FILTER = "filter"
KIND_TRACE = "trace"
KIND_ROW_STATUS = "row_status"

# The names under a regex PII results folder, in the order the stages run.
REGEX_PII_STAGES = ("stage1_scan", "stage2_validated", "stage3_extract")
# How many problems, failures or unfinished pieces a response lists before it says "and N more".
MAX_LISTED = 20


class ChainsError(ValueError):
    """The request can't be answered; the message says why."""


class Stage(NamedTuple):
    key: str
    label: str
    kind: str
    # The filter step the stage belongs to (None for the trace and the row status).
    step: int | None
    # The stages that have to be finished first.
    needs: tuple[str, ...]


# The graph of a chain, in the order the page draws it.
STAGES = (
    Stage("dedup", "Duplicates scan", KIND_SCAN, 1, ()),
    Stage("coverage_step1", "Coverage, step 1", KIND_COVERAGE, 1, ("dedup",)),
    Stage("filter_step1", "Filter, step 1", KIND_FILTER, 1, ("coverage_step1",)),
    Stage("regex_pii", "Regex PII scan", KIND_SCAN, 2, ("filter_step1",)),
    Stage("ner", "NER scan", KIND_SCAN, 2, ("filter_step1",)),
    Stage("toxicity", "Toxicity scan", KIND_SCAN, 2, ("filter_step1",)),
    Stage("coverage_step2", "Coverage, step 2", KIND_COVERAGE, 2, ("regex_pii", "ner", "toxicity")),
    Stage("filter_step2", "Filter, step 2", KIND_FILTER, 2, ("coverage_step2",)),
    Stage("trace", "Trace", KIND_TRACE, None, ("filter_step2",)),
    Stage("row_status", "Row status", KIND_ROW_STATUS, None, ("filter_step2",)),
)
STAGE_BY_KEY = {stage.key: stage for stage in STAGES}


# -------------------------------------------------------------------------
# Finding a chain
# -------------------------------------------------------------------------


def resolve_chain_dir(raw: Any) -> Path:
    """The chain folder a request names. It has to sit directly inside the chains
    root and hold a `chain.json`, so a request can't point this module at any other
    folder."""
    if not isinstance(raw, str) or not raw.strip():
        raise ChainsError("Give the chain's folder.")
    folder = Path(raw.strip()).expanduser()
    if not folder.is_absolute():
        raise ChainsError(f"The chain's folder must be an absolute path, got: {raw}")
    folder = folder.resolve()
    root = runs.chains_root()
    if folder.parent != root:
        raise ChainsError(f"{folder} isn't directly inside the chains root ({root}).")
    if not (folder / CHAIN_FILENAME).is_file():
        raise ChainsError(f"{folder} has no {CHAIN_FILENAME}.")
    return folder


# -------------------------------------------------------------------------
# What is on disk
# -------------------------------------------------------------------------


def is_finished_run(entry: dict[str, Any]) -> bool:
    """Whether a runs-list entry is a run that passed its checks and was marked finished."""
    return entry["status"] == "checks_passed" and (Path(entry["run_dir"]) / SUCCESS).is_file()


def _runs_by_path(root: str | Path | None) -> dict[str, dict[str, Any]]:
    """The runs under `root`, by their path under it. Empty when there is no such folder."""
    if root is None:
        return {}
    return {entry["path"]: entry for entry in list_runs(Path(root))["runs"]}


def _count_lines(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for line in handle if line.strip())


def _scanner_dir(chain_dir: Path, steps: dict[int, dict[str, Any]], stage: Stage) -> Path:
    """The folder a scan writes its results into: the one the chain's step records
    for it, or else its place in the chain's layout."""
    step = steps.get(stage.step) if stage.step is not None else None
    scanner = step["scanners"].get(stage.key) if step is not None else None
    recorded = scanner.get("results_dir") if isinstance(scanner, dict) else None
    if isinstance(recorded, str) and recorded:
        return Path(recorded)
    return chain_dir / CHAIN_LAYOUT[stage.key]


def _marker_state(folder: Path) -> str:
    if (folder / SUCCESS).is_file():
        return STATE_FINISHED
    return STATE_UNFINISHED if folder.is_dir() else STATE_NOT_STARTED


def _dedup_state(folder: Path) -> tuple[str, dict[str, Any] | None]:
    return _marker_state(folder), None


def _regex_pii_state(folder: Path) -> tuple[str, dict[str, Any] | None]:
    done = sum((folder / name / SUCCESS).is_file() for name in REGEX_PII_STAGES)
    if done == len(REGEX_PII_STAGES):
        state = STATE_FINISHED
    else:
        state = STATE_UNFINISHED if folder.is_dir() else STATE_NOT_STARTED
    return state, {"done": done, "total": len(REGEX_PII_STAGES), "unit": "stages"}


def _ner_state(folder: Path) -> tuple[str, dict[str, Any] | None]:
    state = _marker_state(folder)
    manifest = folder / "manifest" / "work_units.jsonl"
    if not manifest.is_file():
        return state, None
    total = _count_lines(manifest)
    done = 0
    units = folder / "units"
    if units.is_dir():
        with os.scandir(units) as entries:
            done = sum(1 for entry in entries if entry.is_dir() and (Path(entry.path) / SUCCESS).is_file())
    return state, {"done": done, "total": total, "unit": "pieces"}


def toxicity_shard_dirs(folder: Path) -> list[Path] | None:
    """The folders of the toxicity scan's shards, from its manifest. None when
    the manifest isn't there or doesn't list them."""
    manifest = read_json_or_none(folder / "manifests" / "manifest.json")
    shards = manifest.get("shards") if isinstance(manifest, dict) else None
    if not isinstance(shards, list):
        return None
    indexes = [shard.get("index") for shard in shards if isinstance(shard, dict)]
    return [
        folder / "scans" / "toxicity" / "shards" / f"shard_{index:05d}"
        for index in indexes
        if isinstance(index, int) and not isinstance(index, bool) and index >= 0
    ]


def _toxicity_state(folder: Path) -> tuple[str, dict[str, Any] | None]:
    shard_dirs = toxicity_shard_dirs(folder)
    if shard_dirs is None:
        return (STATE_UNFINISHED if folder.is_dir() else STATE_NOT_STARTED), None
    done = sum((shard / SUCCESS).is_file() for shard in shard_dirs)
    state = STATE_FINISHED if shard_dirs and done == len(shard_dirs) else STATE_UNFINISHED
    return state, {"done": done, "total": len(shard_dirs), "unit": "shards"}


_SCAN_STATE = {
    "dedup": _dedup_state,
    "regex_pii": _regex_pii_state,
    "ner": _ner_state,
    "toxicity": _toxicity_state,
}


# -------------------------------------------------------------------------
# Reading a chain: one pass that finds out everything the page needs
# -------------------------------------------------------------------------


class _Chain(NamedTuple):
    folder: Path
    record: dict[str, Any]
    steps: dict[int, dict[str, Any]]
    prepared_runs: dict[str, dict[str, Any]]
    step_runs: dict[int, dict[str, dict[str, Any]]]
    row_status: dict[str, Any] | None


def _read_row_status_summary(record: dict[str, Any]) -> dict[str, Any] | None:
    """The row status's summary, or None until it is finished (or when it can't be read)."""
    status = record.get("row_status")
    if not isinstance(status, dict) or not status["finished"]:
        return None
    try:
        return read_row_status_summary(status["dir"])
    except RowStatusReadError:
        return None


def _gather(chain_dir: Path) -> _Chain:
    record = read_chain(chain_dir)
    if record["name"] is None:
        raise ChainsError(record["problems"][0] if record["problems"] else f"{chain_dir} can't be read.")
    steps = {step["number"]: step for step in record["steps"]}
    step_runs = {number: _runs_by_path(step["output_root"]) for number, step in steps.items()}
    return _Chain(
        folder=chain_dir,
        record=record,
        steps=steps,
        prepared_runs=_runs_by_path(record["prepared_root"]),
        step_runs=step_runs,
        row_status=_read_row_status_summary(record),
    )


# -------------------------------------------------------------------------
# The state of every stage
# -------------------------------------------------------------------------


def _scan_stage(chain: _Chain, stage: Stage) -> dict[str, Any]:
    folder = _scanner_dir(chain.folder, chain.steps, stage)
    state, progress = _SCAN_STATE[stage.key](folder)
    return {"path": str(folder), "state": state, "progress": progress}


def _coverage_stage(chain: _Chain, stage: Stage) -> dict[str, Any]:
    step = chain.steps.get(stage.step)
    layout_path = chain.folder / CHAIN_LAYOUT[stage.key]
    attached = step["coverage"] if step is not None else None
    if attached is not None:
        return {
            "path": attached["file"],
            "state": STATE_FINISHED,
            "progress": None,
            "verdict": attached.get("verdict"),
            "created_at": attached.get("created_at"),
        }
    # A report that is there but wasn't attached is for some other folder; the
    # chain's problems say so. It doesn't count as this step's report.
    return {
        "path": str(layout_path),
        "state": STATE_UNFINISHED if layout_path.exists() else STATE_NOT_STARTED,
        "progress": None,
        "verdict": None,
        "created_at": None,
    }


def _filter_stage(chain: _Chain, stage: Stage) -> dict[str, Any]:
    step = chain.steps.get(stage.step)
    if step is None:
        return {"path": None, "state": STATE_NOT_STARTED, "progress": None}
    input_runs = chain.prepared_runs if stage.step == 1 else chain.step_runs.get(stage.step - 1, {})
    output_runs = chain.step_runs[stage.step]
    expected = {path for path, entry in input_runs.items() if is_finished_run(entry)}
    done = {path for path, entry in output_runs.items() if is_finished_run(entry)}
    failed = sorted(path for path, entry in output_runs.items() if entry["status"] == "checks_failed")
    output_root = Path(step["output_root"])
    started = bool(output_runs) or (output_root.is_dir() and any(output_root.iterdir()))
    if expected and expected <= done:
        state = STATE_FINISHED
    elif started:
        state = STATE_UNFINISHED
    else:
        state = STATE_NOT_STARTED
    return {
        "path": step["output_root"],
        "state": state,
        "progress": {"done": len(done & expected), "total": len(expected), "unit": "runs"},
        "runs_failed": failed[:MAX_LISTED],
        "runs_failed_count": len(failed),
    }


def _trace_stage(chain: _Chain) -> dict[str, Any]:
    trace = chain.record["trace"]
    if trace is None:
        return {"path": None, "state": STATE_NOT_STARTED, "progress": None, "verdict": None}
    if trace["finished"]:
        state = STATE_FINISHED
    else:
        state = STATE_UNFINISHED if Path(trace["dir"]).is_dir() else STATE_NOT_STARTED
    return {"path": trace["dir"], "state": state, "progress": None, "verdict": trace["verdict"]}


def _row_status_stage(chain: _Chain) -> dict[str, Any]:
    status = chain.record["row_status"]
    if status is None:
        return {"path": None, "state": STATE_NOT_STARTED, "progress": None, "verdict": None}
    if status["finished"]:
        state = STATE_FINISHED
    else:
        state = STATE_UNFINISHED if Path(status["dir"]).is_dir() else STATE_NOT_STARTED
    return {"path": status["dir"], "state": state, "progress": None, "verdict": status["verdict"]}


def _prepared_blocker(chain: _Chain) -> str | None:
    """Why no scan can start yet, or None when the Prepared folder has a finished run."""
    if any(is_finished_run(entry) for entry in chain.prepared_runs.values()):
        return None
    return f"{chain.record['prepared_root']} holds no finished Prepared run."


def _stages(chain: _Chain) -> list[dict[str, Any]]:
    """Every stage with its state, and whether it can be started now."""
    built: dict[str, dict[str, Any]] = {}
    for stage in STAGES:
        if stage.kind == KIND_SCAN:
            body = _scan_stage(chain, stage)
        elif stage.kind == KIND_COVERAGE:
            body = _coverage_stage(chain, stage)
        elif stage.kind == KIND_FILTER:
            body = _filter_stage(chain, stage)
        elif stage.kind == KIND_TRACE:
            body = _trace_stage(chain)
        else:
            body = _row_status_stage(chain)
        built[stage.key] = {
            "key": stage.key,
            "label": stage.label,
            "kind": stage.kind,
            "step": stage.step,
            "needs": list(stage.needs),
            "verdict": None,
            **body,
        }

    startable = chain.record["version"] == 2
    for stage in STAGES:
        entry = built[stage.key]
        blocked_by: list[str] = []
        if not stage.needs and stage.kind == KIND_SCAN:
            reason = _prepared_blocker(chain)
            if reason is not None:
                blocked_by.append(reason)
        for need in stage.needs:
            earlier = built[need]
            if earlier["state"] != STATE_FINISHED:
                blocked_by.append(f"{earlier['label']} hasn't finished.")
            elif earlier["kind"] == KIND_COVERAGE and earlier["verdict"] != "pass":
                blocked_by.append(f"{earlier['label']} didn't pass (verdict: {earlier['verdict']}).")
        entry["blocked_by"] = blocked_by
        # Only a chain made by the validator has its places laid out for it to start steps into.
        entry["ready"] = startable and entry["state"] == STATE_NOT_STARTED and not blocked_by
    return [built[stage.key] for stage in STAGES]


# -------------------------------------------------------------------------
# Numbers: the funnel and the dataset table
# -------------------------------------------------------------------------


def _row_status_reasons(summary: dict[str, Any] | None, run: str, step: int) -> dict[str, int] | None:
    """The reasons the row status gives for the rows `step` removed from `run`."""
    run_entry = summary["runs"].get(run) if summary is not None else None
    steps = run_entry.get("steps") if isinstance(run_entry, dict) else None
    if not isinstance(steps, list):
        return None
    for item in steps:
        if isinstance(item, dict) and item.get("step") == step and isinstance(item.get("removed_by_reason"), dict):
            return item["removed_by_reason"]
    return None


def _datasets(chain: _Chain) -> list[dict[str, Any]]:
    """One row for each Prepared run: its rows, and what each filter step kept and
    removed from it. A number is None until the run it comes from has finished."""
    table = []
    for path, prepared in sorted(chain.prepared_runs.items()):
        finished = is_finished_run(prepared)
        steps = []
        for number in sorted(chain.steps):
            entry = chain.step_runs[number].get(path)
            done = entry is not None and is_finished_run(entry)
            steps.append(
                {
                    "step": number,
                    "state": STATE_FINISHED if done else (STATE_UNFINISHED if entry is not None else STATE_NOT_STARTED),
                    "rows_in": entry["rows_read"] if done else None,
                    "kept": entry["written"] if done else None,
                    "removed": entry["removed"] if done else None,
                    "removed_by_reason": _row_status_reasons(chain.row_status, path, number) if done else None,
                }
            )
        table.append(
            {
                "run": path,
                "run_dir": prepared["run_dir"],
                "prepared": prepared["written"] if finished else None,
                "prepared_state": STATE_FINISHED if finished else STATE_UNFINISHED,
                "steps": steps,
            }
        )
    return table


def _funnel(chain: _Chain, stages: list[dict[str, Any]], datasets: list[dict[str, Any]]) -> dict[str, Any]:
    """Rows in, and kept and removed by each filter step, added up over the datasets.
    A step that hasn't finished has no numbers, because a partial sum would look like a result."""
    state_of = {stage["key"]: stage["state"] for stage in stages}
    totals = chain.row_status.get("totals") if chain.row_status is not None else None
    by_reason = totals.get("removed_by_step_and_reason") if isinstance(totals, dict) else None
    steps = []
    for number in sorted(chain.steps):
        finished = state_of.get(f"filter_step{number}") == STATE_FINISHED
        per_dataset = [step for item in datasets for step in item["steps"] if step["step"] == number]
        kept = sum(step["kept"] or 0 for step in per_dataset) if finished else None
        removed = sum(step["removed"] or 0 for step in per_dataset) if finished else None
        steps.append(
            {
                "step": number,
                "state": state_of.get(f"filter_step{number}", STATE_NOT_STARTED),
                "kept": kept,
                "removed": removed,
                "removed_by_reason": by_reason.get(str(number)) if isinstance(by_reason, dict) else None,
            }
        )
    prepared = sum(item["prepared"] or 0 for item in datasets)
    last = steps[-1] if steps else None
    return {
        "prepared": prepared,
        "prepared_runs": len(datasets),
        "steps": steps,
        "clean": last["kept"] if last is not None else None,
    }


def _trace_block(chain: _Chain) -> dict[str, Any] | None:
    trace = chain.record["trace"]
    if trace is None:
        return None
    block = {"dir": trace["dir"], "finished": trace["finished"], "verdict": trace["verdict"], "totals": None}
    if trace["finished"]:
        summary = read_json_or_none(Path(trace["dir"]) / "summary.json")
        totals = summary.get("totals") if isinstance(summary, dict) else None
        if isinstance(totals, dict):
            block["totals"] = {"findings": totals.get("findings"), "by_status": totals.get("by_status")}
    return block


def _row_status_block(chain: _Chain) -> dict[str, Any] | None:
    status = chain.record["row_status"]
    if status is None:
        return None
    block = {**status, "totals": None}
    totals = chain.row_status.get("totals") if chain.row_status is not None else None
    if isinstance(totals, dict):
        block["totals"] = {
            key: totals.get(key)
            for key in (
                "prepared_rows",
                "kept",
                "removed",
                "removed_by_step",
                "removed_by_step_and_reason",
                "rows_without_origin",
            )
        }
    return block


# -------------------------------------------------------------------------
# The chain page
# -------------------------------------------------------------------------


def stage_states(chain_dir: Path) -> dict[str, dict[str, Any]]:
    """Each stage of the chain by its key, as `chain_view` lists them, without the
    funnel and the dataset table."""
    return {stage["key"]: stage for stage in _stages(_gather(chain_dir))}


def chain_view(chain_dir: Path, refine: StageRefiner | None = None) -> dict[str, Any]:
    """Everything the chain's page shows: the stages and their states, the funnel,
    the dataset table, the trace and the row status. Read-only, and it opens only
    summary files. `refine` may change the stages' states (see `StageRefiner`)."""
    chain = _gather(chain_dir)
    stages = _stages(chain)
    if refine is not None:
        refine(chain_dir, stages)
    datasets = _datasets(chain)
    problems = list(chain.record["problems"])
    if sorted(chain.steps) != [1, 2]:
        problems.append("This page draws chains of two filter steps; this chain's steps are different.")
    return {
        "chain_dir": str(chain_dir),
        "name": chain.record["name"],
        "version": chain.record["version"],
        "created_at": chain.record["created_at"],
        "startable": chain.record["version"] == 2,
        "prepared_root": chain.record["prepared_root"],
        "last_filter_root": chain.record["last_filter_root"],
        "problems": problems,
        "steps": [chain.steps[number] for number in sorted(chain.steps)],
        "stages": stages,
        "funnel": _funnel(chain, stages, datasets),
        "datasets": datasets,
        "trace": _trace_block(chain),
        "row_status": _row_status_block(chain),
    }


def list_all_chains(refine: StageRefiner | None = None) -> dict[str, Any]:
    """The chains under the chains root, each with its funnel and a line for each
    stage. A chain that can't be read is listed with its `problem`."""
    listing = list_chains(runs.chains_root())
    for item in listing["chains"]:
        if "problem" in item:
            continue
        try:
            view = chain_view(Path(item["chain_dir"]), refine)
        except ChainsError as exc:
            item["problem"] = str(exc)
            continue
        item.update(
            startable=view["startable"],
            problems=view["problems"],
            funnel=view["funnel"],
            stages=[
                {key: stage[key] for key in ("key", "label", "state", "progress", "verdict")}
                for stage in view["stages"]
            ],
            trace=view["trace"] and {key: view["trace"][key] for key in ("finished", "verdict")},
            row_status=view["row_status"] and {key: view["row_status"][key] for key in ("finished", "verdict")},
        )
    return listing


# -------------------------------------------------------------------------
# One stage's results
# -------------------------------------------------------------------------


def _pick(source: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: source.get(key) for key in keys} if isinstance(source, dict) else {}


def _add_counts(into: dict[str, int], counts: Any) -> None:
    if isinstance(counts, dict):
        for name, count in counts.items():
            if isinstance(count, int) and not isinstance(count, bool):
                into[name] = into.get(name, 0) + count


def _sorted_counts(counts: dict[str, int]) -> dict[str, int]:
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _shorten(items: list[Any]) -> dict[str, Any]:
    return {"items": items[:MAX_LISTED], "more": max(0, len(items) - MAX_LISTED)}


def _removed_totals(summary: dict[str, Any] | None, step: int) -> dict[str, dict[str, int]] | None:
    """What the row status's removed-rows indexes count for `step`, added up over
    every run: rows removed by reason, NER label, regex category and toxicity verdict.
    None when the row status isn't finished or has no such index."""
    if summary is None:
        return None
    totals: dict[str, dict[str, int]] = {name: {} for name in ("by_reason", "by_ner_label", "by_regex_category", "by_toxicity_verdict")}
    found = False
    for run_entry in summary["runs"].values():
        index = run_entry.get("removed_index") if isinstance(run_entry, dict) else None
        entry = index.get(str(step)) if isinstance(index, dict) else None
        if not isinstance(entry, dict):
            continue
        found = True
        for name, counts in totals.items():
            _add_counts(counts, entry.get(name))
    return {name: _sorted_counts(counts) for name, counts in totals.items()} if found else None


def _coverage_section(chain: _Chain, stage: Stage) -> dict[str, Any] | None:
    """What the step's coverage report says about this scanner."""
    step = chain.steps.get(stage.step)
    attached = step["coverage"] if step is not None else None
    if attached is None:
        return None
    report = read_json_or_none(Path(attached["file"]))
    section = report.get("scanners", {}).get(stage.key) if isinstance(report, dict) else None
    if not isinstance(section, dict):
        return None
    problems = [
        item for item in report.get("problems", []) if isinstance(item, dict) and item.get("scanner") == stage.key
    ]
    files = section.get("files") if isinstance(section.get("files"), dict) else {}
    return {
        "file": attached["file"],
        "status": section.get("status"),
        "reason": section.get("reason"),
        "parts": section.get("parts", []),
        "files": {
            "expected": files.get("expected"),
            "read": files.get("read"),
            **{name: _shorten(list(files.get(name) or [])) for name in ("missing", "extra", "different", "not_finished")},
        },
        "rows": section.get("rows"),
        "details": section.get("details"),
        "problems": _shorten(problems),
    }


def _dedup_details(folder: Path) -> dict[str, Any]:
    summary = read_json_or_none(folder / "summary.json")
    return _pick(
        summary,
        (
            "mode",
            "input_mode",
            "files",
            "rows_total",
            "rows_hashed",
            "rows_without_text",
            "candidate_pairs",
            "duplicate_rows",
            "configuration",
        ),
    )


def _ner_details(folder: Path) -> dict[str, Any]:
    summary = read_json_or_none(folder / "summary.json")
    details = _pick(
        summary,
        (
            "created_at",
            "expected_units",
            "completed_units",
            "rows_read",
            "rows_with_text",
            "rows_without_text",
            "messages",
            "findings",
            "spans",
            "policy_eligible_spans",
            "llm_candidates",
            "tiers",
            "policy_exclusions",
        ),
    )
    if isinstance(summary, dict):
        labels = summary.get("labels")
        details["labels"] = _sorted_counts({k: v for k, v in labels.items() if isinstance(v, int)}) if isinstance(labels, dict) else None
        missing = summary.get("missing_units")
        details["missing_units"] = len(missing) if isinstance(missing, list) else None
    return details


def _toxicity_details(folder: Path) -> dict[str, Any]:
    shard_dirs = toxicity_shard_dirs(folder)
    if shard_dirs is None:
        return {"shards": None}
    totals = {"rows_read": 0, "messages_scored": 0, "messages_flagged": 0, "guard_records": 0, "guard_errors": 0, "guard_unknown": 0}
    safety: dict[str, int] = {}
    categories: dict[str, int] = {}
    judged = 0
    for shard in shard_dirs:
        detoxify = read_json_or_none(shard / "detoxify_summary.json")
        if isinstance(detoxify, dict):
            for key in ("rows_read", "messages_scored", "messages_flagged"):
                value = detoxify.get(key)
                totals[key] += value if isinstance(value, int) else 0
        guard = read_json_or_none(shard / "guard_results" / "summary.json")
        if isinstance(guard, dict):
            judged += 1
            for key, source in (("guard_records", "total_records"), ("guard_errors", "errors"), ("guard_unknown", "unknown")):
                value = guard.get(source)
                totals[key] += value if isinstance(value, int) else 0
            _add_counts(safety, guard.get("safety"))
            _add_counts(categories, guard.get("categories"))
    return {
        "shards": len(shard_dirs),
        "shards_finished": sum((shard / SUCCESS).is_file() for shard in shard_dirs),
        "shards_judged": judged,
        **totals,
        "verdicts": _sorted_counts(safety),
        "categories": _sorted_counts(categories),
    }


def _regex_pii_details(folder: Path, chain: _Chain, stage: Stage) -> dict[str, Any]:
    """Regex PII keeps no summary of its own (its findings are a CSV of over a gigabyte
    for the full run), so the categories shown are the ones the filter removed rows for."""
    removed = _removed_totals(chain.row_status, stage.step or 0)
    return {
        "stages_finished": sum((folder / name / SUCCESS).is_file() for name in REGEX_PII_STAGES),
        "stages": len(REGEX_PII_STAGES),
        "removed_by_category": removed["by_regex_category"] if removed is not None else None,
    }


def _scan_details(chain: _Chain, stage: Stage) -> dict[str, Any]:
    folder = _scanner_dir(chain.folder, chain.steps, stage)
    if stage.key == "dedup":
        return _dedup_details(folder)
    if stage.key == "ner":
        return _ner_details(folder)
    if stage.key == "toxicity":
        return _toxicity_details(folder)
    return _regex_pii_details(folder, chain, stage)


def _filter_details(chain: _Chain, stage: Stage) -> dict[str, Any]:
    """A filter step added up over its runs: rows read, kept and removed, the reasons,
    and the rules and scanner results the first run used (and whether any run used others)."""
    step = chain.steps[stage.step]
    totals = {"runs": 0, "rows_read": 0, "kept": 0, "removed": 0}
    by_reason: dict[str, int] = {}
    rules: Any = None
    scanner_results: Any = None
    rules_differ = False
    failures: list[dict[str, Any]] = []
    for path, entry in sorted(chain.step_runs[stage.step].items()):
        summary = read_json_or_none(Path(entry["run_dir"]) / "summary.json")
        if not isinstance(summary, dict):
            continue
        totals["runs"] += 1
        rows = summary.get("rows") if isinstance(summary.get("rows"), dict) else {}
        source = summary.get("source") if isinstance(summary.get("source"), dict) else {}
        for key, value in (("rows_read", source.get("rows_read")), ("kept", rows.get("written")), ("removed", rows.get("removed"))):
            totals[key] += value if isinstance(value, int) else 0
        _add_counts(by_reason, summary.get("removed_by_reason"))
        if rules is None:
            rules, scanner_results = summary.get("rules"), summary.get("scanner_results")
        elif summary.get("rules") != rules:
            rules_differ = True
        checks = summary.get("checks")
        if isinstance(checks, dict) and checks.get("passed") is not True:
            failures.append({"run": path, "failures": checks.get("failures", [])})
    return {
        "input_root": step["scan_root"],
        "output_root": step["output_root"],
        **totals,
        "removed_by_reason": _sorted_counts(by_reason),
        "rules": rules if rules is not None else step.get("rules"),
        "rules_differ": rules_differ,
        "scanner_results": scanner_results,
        "failed_runs": _shorten(failures),
    }


def _coverage_details(chain: _Chain, stage: Stage) -> dict[str, Any]:
    step = chain.steps.get(stage.step)
    attached = step["coverage"] if step is not None else None
    if attached is None:
        return {"report": None}
    report = read_json_or_none(Path(attached["file"]))
    if not isinstance(report, dict):
        return {"report": None}
    scanners = report.get("scanners") if isinstance(report.get("scanners"), dict) else {}
    problems = [item for item in report.get("problems", []) if isinstance(item, dict)]
    return {
        "report": attached["file"],
        "created_at": report.get("created_at"),
        "root": report.get("root"),
        "verdict": report.get("verdict"),
        "runs": report.get("runs") and {key: report["runs"].get(key) for key in ("found", "checked", "files", "rows")},
        "scanners_checked": report.get("scanners_checked"),
        "scanners_not_run": report.get("scanners_not_run"),
        "scanner_status": {name: section.get("status") for name, section in scanners.items() if isinstance(section, dict)},
        "problems": _shorten(problems),
    }


def _trace_details(chain: _Chain) -> dict[str, Any]:
    trace = chain.record["trace"]
    summary = read_json_or_none(Path(trace["dir"]) / "summary.json") if trace is not None and trace["finished"] else None
    if not isinstance(summary, dict):
        return {"summary": None}
    sources = summary.get("sources") if isinstance(summary.get("sources"), dict) else {}
    return {
        "created_at": summary.get("created_at"),
        "verdict": summary.get("verdict"),
        "totals": summary.get("totals"),
        "sources": {
            name: {"findings": item.get("findings"), "by_status": item.get("by_status")}
            for name, item in sources.items()
            if isinstance(item, dict)
        },
        "checks": summary.get("checks"),
    }


def _row_status_details(chain: _Chain) -> dict[str, Any]:
    summary = chain.row_status
    if summary is None:
        return {"summary": None}
    return {
        "created_at": summary.get("created_at"),
        "verdict": summary.get("verdict"),
        "report_version": summary.get("report_version"),
        "totals": summary.get("totals"),
        "checks": summary.get("checks"),
    }


def step_results(chain_dir: Path, key: str, refine: StageRefiner | None = None) -> dict[str, Any]:
    """What one stage of the chain found, in plain numbers (see the `_*_details`
    functions), with the coverage report's verdict for a scan. `refine` may change
    the stages' states (see `StageRefiner`)."""
    stage = STAGE_BY_KEY.get(key)
    if stage is None:
        raise ChainsError(f"There is no stage {key!r}. The stages are: {', '.join(STAGE_BY_KEY)}.")
    chain = _gather(chain_dir)
    stages = _stages(chain)
    if refine is not None:
        refine(chain_dir, stages)
    state = next(item for item in stages if item["key"] == key)
    result: dict[str, Any] = {
        "chain_dir": str(chain_dir),
        "stage": {name: state[name] for name in ("key", "label", "kind", "step", "path", "state", "progress", "verdict")},
        "coverage": None,
        "details": None,
    }
    if stage.kind == KIND_SCAN:
        result["coverage"] = _coverage_section(chain, stage)
        result["details"] = _scan_details(chain, stage)
    elif stage.kind == KIND_COVERAGE:
        result["details"] = _coverage_details(chain, stage)
    elif stage.kind == KIND_FILTER:
        result["details"] = _filter_details(chain, stage)
        # Counts by NER label, regex category and toxicity verdict: only the row status has them.
        result["details"]["row_status_counts"] = _removed_totals(chain.row_status, stage.step or 0)
    elif stage.kind == KIND_TRACE:
        result["details"] = _trace_details(chain)
    else:
        result["details"] = _row_status_details(chain)
    return result
