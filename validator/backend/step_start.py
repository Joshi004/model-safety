"""Starting the steps of a chain, and making a chain: the only code in the validator
that starts anything other than a conversion.

A chain made here (`create_chain`) is a folder directly under the chains root with a
version 2 `chain.json`, so every step has its place inside it (modelsafety/contract/
chain.py, `LAYOUT`). A step is started in two moves, both read from the same plan
(`build_plan`):

* `check`: read-only. What the step reads and skips, the exact command, what it costs
  (CPUs, memory, time, GPUs), and everything that stops it from starting.
* `submit`: runs the check again under a lock, then starts it and writes a step record
  (`steps.py`) in the chain's `_jobs` folder.

How each step is started is what the command line does, never a second copy of it. The
scans go through the wrappers in `scripts/` (`tool_team_dedup_scan.sh`,
`tool_team_data.sh`, `tool_team_ner_scan.sh`, `tool_team_toxicity_scan.sh`) with an action
word, so they never prompt (stdin is closed), and the job IDs they print are read back.
The filter, the trace and the row status are `sbatch` on their own job scripts, as in the
Phase 8 commands. A coverage report is a short command the backend runs itself.

Rules this module keeps:

* Every command is an argument list run with `shell=False`. Nothing typed in the UI reaches
  a shell: a setting is checked against a fixed list or a strict pattern first.
* A step only ever writes inside `<chains root>/<chain>/` (its scan folder, filter folder,
  coverage report, trace, row status, `_logs` and `_jobs`). The wrappers also write their
  own job logs under the repo's `logs/`.
* A step is only started into a place that isn't there yet. Nothing is overwritten or
  deleted; `--overwrite` is never passed, and no folder is ever removed except the empty
  ones of a chain that couldn't be made.
* A step is only offered when everything it needs is finished and (where a coverage report
  stands between them) the report passed. A GPU step also needs its own confirmation.
* The wrappers are started with the tuning variables of the shell the validator runs in
  removed, so the command shown is the command that runs.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, NamedTuple

import chains
import jobs
import runs
import steps
from reader_imports import (
    CHAIN_LAYOUT,
    GUARD_ERROR_CHOICES,
    PII_CATEGORIES,
    REPO_ROOT,
    TOXICITY_REMOVABLE_VERDICTS,
    ChainError,
    RunFinderError,
    create_planned_chain,
    list_runs,
    read_chain,
    select_scan_files,
)

SUBMIT_TIMEOUT_SECONDS = 120
COVERAGE_TIMEOUT_SECONDS = 300
# The most of a command's output that a record keeps.
MAX_OUTPUT_CHARS = 50_000

CHAIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
# A NER label: lowercase words joined by underscores in practice; this is the strict superset.
LABEL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DEFAULT_PARTITION = jobs.DEFAULT_RESOURCES["partition"]

# Variables the wrappers read as settings. They are removed from the environment a wrapper
# starts with, so a stray export in the shell can't change what the shown command does.
# (HF_TOKEN and PATH are kept: the scans need them.)
WRAPPER_ENV_PREFIXES = ("PRIVACY_FILTER_", "TOX_", "DEDUP_", "PII_", "QUALITY_", "GUARD_", "VLLM_", "SCAN_", "CONVERT_")
WRAPPER_ENV_NAMES = ("PARTITION", "OUTPUT_ROOT", "SFT_MIX_PATH", "MOPD_MIX_PATH", "RL_MIX_PATH", "REPO")

# Serialises starting a step with the conversion submit and delete in jobs.py, and with itself.
_INLINE_LOCK = threading.Lock()

MODE_WRAPPER = "wrapper"
MODE_SBATCH = "sbatch"
MODE_INLINE = "inline"


class StartError(ValueError):
    """The step or the chain can't be started; the message says why."""


# -------------------------------------------------------------------------
# What each step takes
# -------------------------------------------------------------------------


def _choice(name: str, label: str, choices: list[str], default: str, help_text: str) -> dict[str, Any]:
    return {"name": name, "label": label, "type": "choice", "choices": choices, "default": default, "help": help_text}


def _integer(name: str, label: str, low: int, high: int, default: int | None, help_text: str) -> dict[str, Any]:
    return {"name": name, "label": label, "type": "integer", "min": low, "max": high, "default": default, "help": help_text}


def _resources(cpus: int, mem: str, time: str) -> dict[str, Any]:
    return {"cpus": cpus, "mem": mem, "time": time}


# For each stage: how it is started, the default resources (None where the job script fixes
# them), and the settings it takes. The UI builds its form from this.
SPECS: dict[str, dict[str, Any]] = {
    "dedup": {
        "mode": MODE_WRAPPER,
        "resources": _resources(16, "128G", "24:00:00"),
        "fields": [
            _choice("text_scope", "Text compared", ["prompt", "all"], "prompt", "prompt compares what was asked; all compares the whole conversation."),
            _choice("exclude_system", "Leave out system messages", ["1", "0"], "1", "System messages carry tool definitions, not conversation."),
            _choice("write_filtered", "Write filtered copies of every row", ["0", "1"], "0", "Chains don't use the copies; they come to about 8.7 GB for the full data."),
        ],
    },
    "coverage_step1": {"mode": MODE_INLINE, "resources": None, "fields": []},
    "filter_step1": {"mode": MODE_SBATCH, "resources": _resources(2, "32G", "04:00:00"), "fields": []},
    "regex_pii": {
        "mode": MODE_WRAPPER,
        "resources": _resources(32, "64G", "06:00:00"),
        "fields": [_integer("workers", "Workers", 1, 64, None, "Leave empty to use one worker for each CPU.")],
    },
    "ner": {
        "mode": MODE_WRAPPER,
        "resources": None,
        "fields": [
            _choice("mode", "Size", ["full", "one_gpu"], "full", "full uses 8 GPUs on one node for up to 7 days; one_gpu uses one GPU and reads every file."),
        ],
    },
    "toxicity": {
        "mode": MODE_WRAPPER,
        "resources": _resources(16, "96G", "48:00:00"),
        "fields": [
            _integer("shards", "Shards", 1, 64, 8, "How the input is split. Only matters the first time."),
            _integer("concurrency", "Shards at once", 1, 64, 8, "How many shards run at once: one GPU each."),
        ],
    },
    "coverage_step2": {"mode": MODE_INLINE, "resources": None, "fields": []},
    "filter_step2": {
        "mode": MODE_SBATCH,
        "resources": _resources(2, "128G", "12:00:00"),
        "fields": [
            {"name": "ner_ignore_labels", "label": "NER labels that don't remove a row", "type": "list", "default": [], "help": "Names separated by commas, such as first_name, last_name."},
            {"name": "regex_ignore_categories", "label": "Regex PII categories that don't remove a row", "type": "multi", "choices": list(PII_CATEGORIES), "default": [], "help": ""},
            {"name": "toxicity_remove_verdicts", "label": "Guard verdicts that remove a row", "type": "multi", "choices": list(TOXICITY_REMOVABLE_VERDICTS), "default": list(TOXICITY_REMOVABLE_VERDICTS), "help": ""},
            _choice("toxicity_guard_errors", "A guard error", list(GUARD_ERROR_CHOICES), "remove", "remove counts it as unsafe; refuse won't start while any exist."),
        ],
    },
    "trace": {"mode": MODE_SBATCH, "resources": _resources(4, "256G", "1-00:00:00"), "fields": []},
    "row_status": {"mode": MODE_SBATCH, "resources": _resources(4, "256G", "1-00:00:00"), "fields": []},
}
assert set(SPECS) == set(chains.STAGE_BY_KEY), "every stage needs a spec"

# The job scripts of the steps that are started with `sbatch` directly.
SLURM_SCRIPTS = {
    "filter_step1": "filter_rows.sbatch",
    "filter_step2": "filter_rows.sbatch",
    "trace": "trace_findings.sbatch",
    "row_status": "row_status.sbatch",
}
# The variable each of those job scripts reads the repo's place from.
REPO_VARIABLES = {
    "filter_rows.sbatch": "FILTER_REPO",
    "trace_findings.sbatch": "TRACE_REPO",
    "row_status.sbatch": "ROW_STATUS_REPO",
}


def sbatch_header(script: str) -> dict[str, Any]:
    """The `#SBATCH` lines of a job script in launchers/slurm: cpus, mem, time and the GPUs
    its gres asks for. Empty values for anything it doesn't say."""
    found: dict[str, str] = {}
    try:
        text = (REPO_ROOT / "launchers" / "slurm" / script).read_text(encoding="utf-8")
    except OSError:
        text = ""
    for line in text.splitlines():
        match = re.match(r"^#SBATCH\s+--([a-z-]+)=(\S+)", line)
        if match:
            found[match.group(1)] = match.group(2)
    gres = found.get("gres", "")
    gpu_count = re.search(r":(\d+)$", gres) if gres.startswith("gpu") else None
    return {
        "cpus": int(found["cpus-per-task"]) if found.get("cpus-per-task", "").isdigit() else None,
        "mem": found.get("mem"),
        "time": found.get("time"),
        "gpus": int(gpu_count.group(1)) if gpu_count else (1 if gres.startswith("gpu") else 0),
    }


def spec() -> dict[str, Any]:
    """What the start form needs: the partitions, and for each stage how it is started,
    its default resources and its settings."""
    try:
        partitions, slurm_error = jobs.list_partitions(), None
    except jobs.SlurmUnavailable as exc:
        partitions, slurm_error = [], str(exc)
    stages: dict[str, Any] = {}
    for key, item in SPECS.items():
        stage = chains.STAGE_BY_KEY[key]
        stages[key] = {
            "label": stage.label,
            "mode": item["mode"],
            "resources": item["resources"],
            "fixed_resources": _fixed_resources(key),
            "fields": item["fields"],
        }
    return {
        "partitions": partitions,
        "default_partition": DEFAULT_PARTITION,
        "max_cpus": jobs.MAX_CPUS,
        "slurm_error": slurm_error,
        "chains_root": str(runs.chains_root()),
        "stages": stages,
    }


def _fixed_resources(key: str) -> dict[str, Any] | None:
    """For a step whose job scripts fix the resources: what each size asks for."""
    if key == "ner":
        return {
            "full": sbatch_header("run_privacy_filter_node.sbatch"),
            "one_gpu": sbatch_header("run_privacy_filter_pilot.sbatch"),
        }
    return None


# -------------------------------------------------------------------------
# The places in a chain
# -------------------------------------------------------------------------


class _Paths(NamedTuple):
    chain: Path
    prepared: Path
    dedup: Path
    coverage_step1: Path
    filter_step1: Path
    regex_pii: Path
    ner: Path
    toxicity: Path
    coverage_step2: Path
    filter_step2: Path
    trace: Path
    row_status: Path
    logs: Path

    def of(self, key: str) -> Path:
        return getattr(self, key)


def _paths(chain_dir: Path, prepared_root: str) -> _Paths:
    place = {key: chain_dir / relative for key, relative in CHAIN_LAYOUT.items()}
    return _Paths(chain=chain_dir, prepared=Path(prepared_root), logs=chain_dir / steps.LOGS_FOLDER, **place)


def _scan_dirs(paths: _Paths, keys: tuple[str, ...]) -> list[str]:
    """The `--xxx-run-dir` options of the scans in `keys`, as an argument list."""
    options = {"dedup": "--dedup-run-dir", "regex_pii": "--pii-run-dir", "ner": "--ner-run-dir", "toxicity": "--toxicity-run-dir"}
    arguments: list[str] = []
    for key in keys:
        arguments += [options[key], str(paths.of(key))]
    return arguments


# -------------------------------------------------------------------------
# Settings and resources
# -------------------------------------------------------------------------


def _parse_list(value: Any) -> list[str] | None:
    """A list of names, from a list or from text separated by commas or spaces."""
    if isinstance(value, str):
        value = [item for item in re.split(r"[,\s]+", value) if item]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return list(dict.fromkeys(item.strip() for item in value if item.strip()))


def _check_field(field: dict[str, Any], value: Any, problems: list[str]) -> Any:
    name, label = field["name"], field["label"]
    kind = field["type"]
    if kind == "choice":
        if value not in field["choices"]:
            problems.append(f"{label} must be one of {', '.join(field['choices'])}.")
            return field["default"]
        return value
    if kind == "integer":
        if value is None or value == "":
            if field["default"] is None:
                return None
            value = field["default"]
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        if isinstance(value, bool) or not isinstance(value, int) or not field["min"] <= value <= field["max"]:
            problems.append(f"{label} must be a whole number from {field['min']} to {field['max']}.")
            return field["default"]
        return value
    names = _parse_list(value)
    if names is None:
        problems.append(f"{label} must be a list of names.")
        return field["default"]
    if kind == "list":
        bad = [item for item in names if not LABEL.fullmatch(item)]
        if bad:
            problems.append(f"{label}: {', '.join(bad[:3])} isn't a name (letters, digits, _ . - only, up to 64 characters).")
            return []
        return names
    unknown = [item for item in names if item not in field["choices"]]
    if unknown:
        problems.append(f"{label}: {', '.join(unknown)} isn't one of {', '.join(field['choices'])}.")
        return field["default"]
    if name == "toxicity_remove_verdicts" and not names:
        problems.append(f"{label}: choose at least one verdict, or the filter would never remove for toxicity.")
    return names


def _parse_settings(key: str, raw: Any, problems: list[str]) -> dict[str, Any]:
    fields = SPECS[key]["fields"]
    given = raw if isinstance(raw, dict) else {}
    for name in given:
        if name not in {field["name"] for field in fields}:
            problems.append(f"{chains.STAGE_BY_KEY[key].label} has no setting called {name!r}.")
    settings = {field["name"]: _check_field(field, given.get(field["name"], field["default"]), problems) for field in fields}
    if key == "toxicity" and isinstance(settings.get("shards"), int) and isinstance(settings.get("concurrency"), int):
        if settings["concurrency"] > settings["shards"]:
            problems.append("Shards at once can't be more than the shards.")
    return settings


def _parse_resources(key: str, raw: Any, problems: list[str]) -> dict[str, Any] | None:
    """The partition, CPUs, memory and time limit, checked. NER only chooses a partition: its
    job scripts fix the rest. A coverage report has none."""
    item = SPECS[key]
    given = raw if isinstance(raw, dict) else {}
    if item["mode"] == MODE_INLINE:
        return None
    if item["resources"] is None:
        partition = given.get("partition") or DEFAULT_PARTITION
        try:
            partitions = jobs.list_partitions()
        except jobs.SlurmUnavailable as exc:
            problems.append(str(exc))
            return None
        if partition not in partitions:
            problems.append(f"Partition '{partition}' isn't one sinfo lists ({', '.join(partitions)}).")
            return None
        return {"partition": partition}
    merged = {"partition": DEFAULT_PARTITION, **item["resources"]}
    merged.update({name: given[name] for name in ("partition", "cpus", "mem", "time") if given.get(name) not in (None, "")})
    try:
        return jobs.validate_resources(merged)
    except jobs.JobsError as exc:
        problems.append(str(exc))
    except jobs.SlurmUnavailable as exc:
        problems.append(str(exc))
    return None


# -------------------------------------------------------------------------
# The commands
# -------------------------------------------------------------------------


class _Command(NamedTuple):
    argv: list[str]
    env: dict[str, str]
    # How to read the job IDs back: (role, pattern with one group) for a wrapper's output.
    # A job that `sbatch --parsable` printed has the one role "job" and no pattern.
    patterns: tuple[tuple[str, str], ...]


def _script(name: str, *arguments: str) -> list[str]:
    return ["bash", str(REPO_ROOT / "scripts" / name), *arguments]


def _sbatch(
    key: str, paths: _Paths, resources: dict[str, Any], arguments: list[str], dependency: list[str]
) -> list[str]:
    """`sbatch` on one of the chain's own job scripts, logging into the chain's `_logs`."""
    script = SLURM_SCRIPTS[key]
    log = f"{paths.logs}/{key}_%j.log"
    argv = [
        "sbatch",
        "--parsable",
        f"--partition={resources['partition']}",
        f"--cpus-per-task={resources['cpus']}",
        f"--mem={resources['mem']}",
        f"--time={resources['time']}",
        f"--job-name={paths.chain.name}_{key}",
        f"--output={log}",
        f"--error={log}",
        f"--export=ALL,{REPO_VARIABLES[script]}={REPO_ROOT}",
    ]
    if dependency:
        argv += [f"--dependency=afterok:{':'.join(dependency)}", "--kill-on-invalid-dep=yes"]
    return [*argv, str(REPO_ROOT / "launchers" / "slurm" / script), *arguments]


def _command(
    key: str,
    paths: _Paths,
    settings: dict[str, Any],
    resources: dict[str, Any] | None,
    dependency: list[str],
) -> _Command:
    r = resources or {}
    if key == "dedup":
        argv = _script(
            "tool_team_dedup_scan.sh", "full", "--input-root", str(paths.prepared), "--run-dir", str(paths.dedup),
            "--partition", r["partition"], "--cpus", str(r["cpus"]), "--mem", r["mem"], "--time", r["time"],
            "--text-scope", settings["text_scope"], "--exclude-system", settings["exclude_system"],
            "--write-filtered", settings["write_filtered"],
        )
        return _Command(argv, {}, (("scan", r"Submitted dedup full job: (\d+)"),))
    if key in ("coverage_step1", "coverage_step2"):
        first = key == "coverage_step1"
        root = paths.prepared if first else paths.filter_step1
        scans = _scan_dirs(paths, ("dedup",) if first else ("regex_pii", "ner", "toxicity"))
        out = paths.coverage_step1 if first else paths.coverage_step2
        argv = [sys.executable, str(REPO_ROOT / "modelsafety" / "results" / "coverage.py"), "--root", str(root), *scans, "--json-out", str(out)]
        return _Command(argv, {}, ())
    if key in ("filter_step1", "filter_step2"):
        first = key == "filter_step1"
        arguments = ["--input-root", str(paths.prepared if first else paths.filter_step1), "--output-root", str(paths.of(key))]
        arguments += _scan_dirs(paths, ("dedup",) if first else ("regex_pii", "ner", "toxicity"))
        if not first:
            for label in settings["ner_ignore_labels"]:
                arguments += ["--ner-ignore-label", label]
            for category in settings["regex_ignore_categories"]:
                arguments += ["--regex-ignore-category", category]
            if sorted(settings["toxicity_remove_verdicts"]) != sorted(TOXICITY_REMOVABLE_VERDICTS):
                for verdict in settings["toxicity_remove_verdicts"]:
                    arguments += ["--toxicity-remove-verdict", verdict]
            arguments += ["--toxicity-guard-errors", settings["toxicity_guard_errors"]]
        return _Command(_sbatch(key, paths, r, arguments, []), {}, ())
    if key == "regex_pii":
        argv = _script(
            "tool_team_data.sh", "scan", "extract", "--output-root", str(paths.filter_step1), "--pii-run-dir", str(paths.regex_pii),
            "--partition", r["partition"], "--cpus", str(r["cpus"]), "--mem", r["mem"], "--time", r["time"],
            "--workers", str(settings["workers"] or r["cpus"]),
        )
        return _Command(argv, {}, (("scan", r"Submitted PII job \(stages=all\): (\d+)"),))
    if key == "ner":
        full = settings["mode"] == "full"
        argv = _script(
            "tool_team_ner_scan.sh", "full" if full else "pilot", "--input-root", str(paths.filter_step1),
            "--run-root", str(paths.ner), "--partition", r["partition"],
        )
        # Without these two, the one-GPU mode reads 1 file and 1 piece of work.
        env = {} if full else {"PRIVACY_FILTER_MAX_FILES": "100000", "PRIVACY_FILTER_MAX_UNITS": "0"}
        return _Command(argv, env, (("manifest", r"Manifest job: (\d+)"), ("scan", r"Privacy Filter (?:full|pilot) job: (\d+)")))
    if key == "toxicity":
        argv = _script(
            "tool_team_toxicity_scan.sh", "all", "--input-root", str(paths.filter_step1), "--run-dir", str(paths.toxicity),
            "--pilot-files", "0", "--shards", str(settings["shards"]), "--concurrency", str(settings["concurrency"]),
            "--cpus", str(r["cpus"]), "--mem", r["mem"], "--time", r["time"], "--partition", r["partition"],
        )
        return _Command(argv, {}, (("prepare", r"Submitted prepare job: (\d+)"), ("scan", r"Submitted (?:all )?job: (\d+)")))
    if key == "trace":
        arguments = ["--output-dir", str(paths.trace), *_scan_dirs(paths, ("dedup", "regex_pii", "ner", "toxicity"))]
        arguments += ["--filter-root", str(paths.filter_step1), "--filter-root", str(paths.filter_step2)]
        return _Command(_sbatch(key, paths, r, arguments, []), {}, ())
    # row_status
    arguments = ["--filter-root", str(paths.filter_step2), "--output-dir", str(paths.row_status)]
    return _Command(_sbatch(key, paths, r, arguments, dependency), {}, ())


def _command_text(command: _Command) -> str:
    prefix = " ".join(f"{name}={shlex.quote(value)}" for name, value in command.env.items())
    text = shlex.join(command.argv)
    return f"{prefix} {text}" if prefix else text


# -------------------------------------------------------------------------
# The plan
# -------------------------------------------------------------------------

# Where each stage reads its files from, to say what it reads and skips.
def _input_roots(key: str, paths: _Paths) -> list[Path]:
    if key in ("dedup", "coverage_step1", "filter_step1"):
        return [paths.prepared]
    if key in ("regex_pii", "ner", "toxicity", "coverage_step2", "filter_step2"):
        return [paths.filter_step1]
    return [paths.filter_step1, paths.filter_step2]


# The summary of a folder's files is for showing on the form while settings change, and listing
# a big folder (20,000 files) takes seconds, so the form may reuse one this young. Starting a
# step never does: it reads the folder again.
SUMMARY_MAX_AGE_SECONDS = 60
_SUMMARY_CACHE: dict[str, tuple[float, tuple[dict[str, Any] | None, str | None]]] = {}


def _input_summary(root: Path, fresh: bool) -> tuple[dict[str, Any] | None, str | None]:
    """What a step would read under `root`, and what it would skip (an unfinished run, a
    stray .jsonl): from the run finder, the same code the scans use. `(None, why not)` when
    there is nothing finished to read."""
    now = time.monotonic()
    cached = _SUMMARY_CACHE.get(str(root))
    if not fresh and cached is not None and now - cached[0] < SUMMARY_MAX_AGE_SECONDS:
        return cached[1]
    answer = _read_input_summary(root)
    _SUMMARY_CACHE[str(root)] = (now, answer)
    return answer


def _read_input_summary(root: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        selection = select_scan_files(root)
    except RunFinderError as exc:
        return None, str(exc)
    skipped_runs = [item for item in selection.skipped if item["kind"] == "run"]
    return (
        {
            "root": str(root),
            "runs_found": selection.runs_found,
            "runs_read": selection.runs_found - len(skipped_runs),
            "runs_skipped": len(skipped_runs),
            "files": len(selection.files),
            "rows": sum(item.rows or 0 for item in selection.files),
            "skipped": selection.skipped[:10],
            "skipped_more": max(0, len(selection.skipped) - 10),
        },
        None,
    )


def _output_state(path: Path) -> str:
    if not path.exists():
        return "new"
    if path.is_dir() and not any(path.iterdir()):
        return "empty"
    return "has_files"


def _dedup_job_running() -> str | None:
    """What `squeue` lists for this wrapper's dedup jobs (the wrapper refuses to start
    another while one is queued or running), or None. A SLURM that can't be asked is no answer."""
    try:
        result = jobs._run(["squeue", "--me", "-h", "-n", "tt_dedup_pilot,tt_dedup_full", "-o", "%i %j %T"])
    except jobs.SlurmUnavailable:
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def _cost(key: str, settings: dict[str, Any], resources: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the step asks of the cluster, in numbers and in a sentence."""
    if SPECS[key]["mode"] == MODE_INLINE:
        return {"gpus": 0, "cpus": None, "mem": None, "time": None, "summary": "Runs inside the validator for up to 5 minutes. No SLURM job."}
    if key == "ner":
        header = sbatch_header("run_privacy_filter_node.sbatch" if settings["mode"] == "full" else "run_privacy_filter_pilot.sbatch")
        manifest = sbatch_header("prepare_privacy_filter.sbatch")
        gpus = header["gpus"]
        return {
            **header,
            "summary": (
                f"{gpus} GPU{'' if gpus == 1 else 's'}, {header['cpus']} CPUs, {header['mem']} memory, up to {header['time']}; "
                f"plus a CPU job to plan the pieces of work ({manifest['cpus']} CPUs, {manifest['mem']}, up to {manifest['time']})."
            ),
        }
    if resources is None:
        return None
    gpus = 0
    summary = f"{resources['cpus']} CPUs, {resources['mem']} memory, up to {resources['time']}."
    if key == "toxicity":
        header = sbatch_header("run_toxicity_scan_array.sbatch")
        gpus = header["gpus"] * (settings.get("concurrency") or 0)
        summary = (
            f"{settings['shards']} shards, up to {settings['concurrency']} at once: up to {gpus} GPUs at once; "
            f"each shard {resources['cpus']} CPUs, {resources['mem']} memory, up to {resources['time']}."
        )
    return {"gpus": gpus, "cpus": resources["cpus"], "mem": resources["mem"], "time": resources["time"], "summary": summary}


def build_plan(chain_dir: Path, key: str, raw_settings: Any, raw_resources: Any, fresh: bool = False) -> dict[str, Any]:
    """Everything the check shows, and everything `submit` does: it reads only. The plan says
    what is wrong in `blocked`; nothing can be started while that isn't empty. With `fresh`,
    the folders a step reads are listed again instead of reusing a recent answer."""
    stage = chains.STAGE_BY_KEY.get(key)
    if stage is None:
        raise StartError(f"There is no stage {key!r}. The stages are: {', '.join(chains.STAGE_BY_KEY)}.")
    record = read_chain(chain_dir)
    if record["name"] is None:
        raise StartError(record["problems"][0] if record["problems"] else f"{chain_dir} can't be read.")
    paths = _paths(chain_dir, record["prepared_root"])
    states = chains.stage_states(chain_dir)
    steps.refine_stages(chain_dir, list(states.values()))
    state = states[key]

    blocked: list[str] = []
    warnings: list[str] = []
    if record["version"] != 2:
        blocked.append(
            "This chain was recorded after it ran, so it has no places laid out for new steps. "
            "Make a new chain here to run steps."
        )
    if state["state"] != chains.STATE_NOT_STARTED:
        blocked.append(
            f"{stage.label} has already been started (it is {state['state'].replace('_', ' ')}). "
            "A step is never started into a place that already has something in it."
        )
    else:
        blocked.extend(state["blocked_by"])

    problems: list[str] = []
    settings = _parse_settings(key, raw_settings, problems)
    resources = _parse_resources(key, raw_resources, problems)
    blocked.extend(problems)

    summaries = []
    for root in _input_roots(key, paths):
        summary, reason = _input_summary(root, fresh)
        if summary is not None:
            summaries.append(summary)
        elif state["state"] == chains.STATE_NOT_STARTED and not state["blocked_by"]:
            blocked.append(reason or f"Nothing to read under {root}.")
    if summaries and summaries[0]["runs_skipped"] > 0:
        warnings.append(f"{summaries[0]['runs_skipped']} run(s) under {summaries[0]['root']} aren't finished and will be skipped.")

    output = paths.of(key)
    if key == "dedup" and (running := _dedup_job_running()) is not None:
        blocked.append(f"A dedup job from the wrapper is already queued or running ({running}). Wait for it, or cancel it.")

    dependency: list[str] = []
    if key == "row_status":
        trace_job = states["trace"].get("job")
        if trace_job and states["trace"]["state"] in (chains.STATE_QUEUED, chains.STATE_RUNNING):
            dependency = list(trace_job["job_ids"])
            warnings.append("The trace is still going, so the row status waits for it to finish well (it is cancelled if the trace fails).")

    plan: dict[str, Any] = {
        "chain_dir": str(chain_dir),
        "chain": chain_dir.name,
        "stage": key,
        "label": stage.label,
        "mode": SPECS[key]["mode"],
        "state": state["state"],
        "inputs": [str(root) for root in _input_roots(key, paths)],
        "input_summaries": summaries,
        "output": str(output),
        "output_state": _output_state(output),
        "settings": settings,
        "resources": resources,
        "cost": _cost(key, settings, resources) if not problems else None,
        "needs_gpu_confirmation": False,
        "dependency": dependency,
        "command": None,
        "command_text": None,
        "blocked": blocked,
        "warnings": warnings,
        "can_start": False,
    }
    if not problems:
        command = _command(key, paths, settings, resources, dependency)
        plan["command"] = command.argv
        plan["command_text"] = _command_text(command)
        plan["needs_gpu_confirmation"] = bool(plan["cost"] and plan["cost"]["gpus"])
    if not blocked and not state["ready"]:
        blocked.append("This step can't be started now.")
    plan["can_start"] = not blocked and plan["command"] is not None
    return plan


def check(chain_dir: Path, key: str, settings: Any, resources: Any) -> dict[str, Any]:
    """The plan for a step, read-only: for the cost screen of the start form."""
    return build_plan(chain_dir, key, settings, resources)


# -------------------------------------------------------------------------
# Starting
# -------------------------------------------------------------------------


def _clean_environment(extra: dict[str, str]) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in WRAPPER_ENV_NAMES and not name.startswith(WRAPPER_ENV_PREFIXES)
    }
    environment.update(extra)
    return environment


def _output_text(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return (value or "")[-MAX_OUTPUT_CHARS:]


def _logs_of(key: str, role: str, job_id: str, settings: dict[str, Any], chain_dir: Path) -> tuple[str | None, str | None]:
    """Where a job's log is: the file its job script or wrapper names, built from the job ID."""
    logs = REPO_ROOT / "logs" / "slurm"
    if key in SLURM_SCRIPTS:
        log = chain_dir / steps.LOGS_FOLDER / f"{key}_{job_id}.log"
        return str(log), str(log)
    stem = {
        ("dedup", "scan"): f"tool_team_dedup_full_{job_id}",
        ("regex_pii", "scan"): f"pii_scan_and_extract_{job_id}",
        ("ner", "manifest"): f"privacy_filter_manifest_{job_id}",
        ("ner", "scan"): f"privacy_filter_{'node' if settings.get('mode') == 'full' else 'pilot'}_{job_id}",
        ("toxicity", "prepare"): f"tool_team_toxicity_prep_{job_id}",
        # An array job: one log for each shard. The first one is shown.
        ("toxicity", "scan"): f"tool_team_toxicity_all_{job_id}_0",
    }.get((key, role))
    if stem is None:
        return None, None
    return str(logs / f"{stem}.out"), str(logs / f"{stem}.err")


def _read_jobs(
    key: str, command: _Command, output: str, settings: dict[str, Any], chain_dir: Path, dependency: list[str]
) -> list[dict[str, Any]]:
    """The jobs a command started, from what it printed. `dependency` is the jobs the command
    was told to wait for."""
    found: list[dict[str, Any]] = []
    if not command.patterns:
        # `sbatch --parsable`: "<job id>" or "<job id>;<cluster>".
        first = output.strip().splitlines()[-1].split(";")[0] if output.strip() else ""
        matches = [("job", first)] if first.isdigit() else []
    else:
        matches = []
        for role, pattern in command.patterns:
            match = re.search(pattern, output)
            if match:
                matches.append((role, match.group(1)))
    previous: list[str] = []
    for role, job_id in matches:
        out_log, err_log = _logs_of(key, role, job_id, settings, chain_dir)
        # A scan's own job waits for the manifest or prepare job printed before it.
        waits_for = list(previous) if role == "scan" else list(dependency)
        found.append({"job_id": job_id, "role": role, "depends_on": waits_for, "out_log": out_log, "err_log": err_log})
        previous.append(job_id)
    return found


def _run_command(command: _Command, timeout: int) -> tuple[int | None, str, bool]:
    """`(exit code, output, timed out)`. The command can't prompt: its input is closed."""
    try:
        result = subprocess.run(
            command.argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=REPO_ROOT,
            env=_clean_environment(command.env),
            timeout=timeout,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        return None, _output_text(exc.stdout), True
    except OSError as exc:
        raise StartError(f"{command.argv[0]} couldn't be run ({exc}).") from exc
    return result.returncode, _output_text(result.stdout), False


def submit(chain_dir: Path, key: str, settings: Any, resources: Any, confirm_gpu: bool) -> dict[str, Any]:
    """Start the step. The check is made again under a lock, and the step is refused if it
    fails. A step that asks for GPUs also needs `confirm_gpu`. Returns the step record as the
    Jobs page shows it."""
    inline = SPECS.get(key, {}).get("mode") == MODE_INLINE
    with (_INLINE_LOCK if inline else jobs._LOCK):
        plan = build_plan(chain_dir, key, settings, resources, fresh=True)
        if not plan["can_start"]:
            raise StartError("Nothing was started. " + " ".join(plan["blocked"]))
        if plan["needs_gpu_confirmation"] and not confirm_gpu:
            raise StartError(
                f"Nothing was started. This step uses GPUs ({plan['cost']['summary']}); confirm that you want to start it."
            )
        paths = _paths(chain_dir, read_chain(chain_dir)["prepared_root"])
        command = _command(key, paths, plan["settings"], plan["resources"], plan["dependency"])

        # The places the step writes into, made before it starts: SLURM won't make a log folder.
        for folder in (paths.logs, steps.records_folder(chain_dir)):
            folder.mkdir(exist_ok=True)
        if inline:
            paths.of(key).parent.mkdir(exist_ok=True)

        submitted_at = jobs._now_iso()
        timeout = COVERAGE_TIMEOUT_SECONDS if inline else SUBMIT_TIMEOUT_SECONDS
        code, output, timed_out = _run_command(command, timeout)
        started = [] if inline else _read_jobs(key, command, output, plan["settings"], chain_dir, plan["dependency"])

        warnings = list(plan["warnings"])
        if timed_out and inline:
            raise StartError(
                f"The coverage report didn't finish within {timeout // 60} minutes, so it was stopped. "
                "Run it from a terminal instead:\n" + plan["command_text"]
            )
        if timed_out:
            warnings.append(f"The start command didn't finish within {timeout} seconds and was stopped.")
        elif not inline and code != 0:
            warnings.append(f"The start command ended with exit code {code}.")
        if not inline and not started:
            if code != 0 or timed_out:
                raise StartError(
                    f"Nothing was started: the command {'timed out' if timed_out else f'ended with exit code {code}'}.\n{output.strip()}"
                )
            warnings.append("The command finished, but no SLURM job ID could be read from what it printed. Check `squeue`.")
        elif not inline and (code != 0 or timed_out):
            warnings.append("It may have started only some of its jobs. Check the list below and `squeue`.")

        # A step whose job scripts fix its CPUs, memory and time still says what they are.
        recorded = plan["resources"]
        if recorded is not None and plan["cost"] is not None and "cpus" not in recorded:
            recorded = {**recorded, **{name: plan["cost"][name] for name in ("cpus", "mem", "time") if plan["cost"].get(name) is not None}}

        record_id = jobs._new_batch_id()
        record = {
            "tool": steps.RECORD_TOOL,
            "version": steps.RECORD_VERSION,
            "id": record_id,
            "chain_dir": str(chain_dir),
            "stage": key,
            "submitted_at": submitted_at,
            "inputs": plan["inputs"],
            "output": plan["output"],
            "settings": plan["settings"],
            "resources": recorded,
            "gpus": plan["cost"]["gpus"] if plan["cost"] else 0,
            "command_text": plan["command_text"],
            "wrapper_output": output,
            "jobs": started,
            "warnings": warnings,
        }
        if inline:
            record["inline"] = {"exit_code": code}
        jobs._write_json_atomic(steps.records_folder(chain_dir) / f"{record_id}.json", record)
    return steps.record_status(chain_dir, record_id)


# -------------------------------------------------------------------------
# Making a chain
# -------------------------------------------------------------------------


def prepared_root_candidates() -> dict[str, Any]:
    """The folders under the runs root that hold Prepared runs, for the New chain form: each
    with how many runs are finished and how many aren't, and the runs root itself."""
    root = runs.runs_root()
    listing = list_runs(root)
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in listing["runs"]:
        if entry["kind"] != "prepared" or "/" not in entry["path"]:
            continue
        groups.setdefault(entry["path"].split("/")[0], []).append(entry)

    def describe(path: Path, label: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
        finished = [item for item in entries if chains.is_finished_run(item)]
        return {
            "path": str(path),
            "label": label,
            "runs": len(entries),
            "unfinished": len(entries) - len(finished),
            "rows": sum(item["written"] or 0 for item in finished),
        }

    candidates = [describe(root / name, name, entries) for name, entries in sorted(groups.items())]
    all_entries = [entry for entries in groups.values() for entry in entries]
    if all_entries:
        candidates.append(describe(root, f"{root.name} (every dataset under it)", all_entries))
    return {"root": str(root), "chains_root": str(runs.chains_root()), "candidates": candidates}


def create_chain(name: Any, prepared_root: Any) -> dict[str, Any]:
    """Make `<chains root>/<name>/` with its `_logs` and `_jobs` folders and a version 2
    `chain.json`. The folder must be new; the Prepared folder must hold Prepared runs, all of
    them finished. Nothing existing is changed, and an empty folder is all that is removed
    if the chain can't be made."""
    if not isinstance(name, str) or CHAIN_NAME.fullmatch(name.strip()) is None:
        raise StartError("The name must be 1 to 100 letters, digits, dots, dashes or underscores, starting with a letter or digit.")
    name = name.strip()
    if not isinstance(prepared_root, str) or not Path(prepared_root.strip()).expanduser().is_absolute():
        raise StartError("Give the Prepared folder as an absolute path.")
    prepared = Path(prepared_root.strip()).expanduser().resolve()
    if not prepared.is_dir():
        raise StartError(f"{prepared} isn't a folder.")
    listing = list_runs(prepared)
    entries = listing["runs"]
    if listing["truncated"]:
        raise StartError(f"{prepared} holds too many folders to list its runs. Choose a smaller folder.")
    if not entries:
        raise StartError(f"{prepared} holds no Prepared runs.")
    other = [item["path"] for item in entries if item["kind"] != "prepared"]
    if other:
        raise StartError(f"{prepared} holds runs that aren't Prepared runs ({', '.join(other[:3])}).")
    unfinished = [item["path"] for item in entries if not chains.is_finished_run(item)]
    if unfinished:
        raise StartError(
            f"{len(unfinished)} run(s) under {prepared} aren't finished ({', '.join(unfinished[:3])}"
            f"{', ...' if len(unfinished) > 3 else ''}). A chain starts from finished runs only."
        )

    root = runs.chains_root()
    if not root.is_dir():
        raise StartError(f"The chains root {root} doesn't exist.")
    folder = root / name
    try:
        folder.mkdir()
    except FileExistsError as exc:
        raise StartError(f"{folder} already exists, so no chain was made. Choose another name.") from exc
    except OSError as exc:
        raise StartError(f"{folder} can't be made: {exc}") from exc
    made = [folder]
    try:
        for child in (steps.LOGS_FOLDER, steps.RECORDS_FOLDER):
            (folder / child).mkdir()
            made.append(folder / child)
        record, _ = create_planned_chain(folder, name, prepared)
    except (OSError, ChainError) as exc:
        for path in reversed(made):
            try:
                path.rmdir()  # only an empty folder can go
            except OSError:
                pass
        raise StartError(f"The chain couldn't be made: {exc}") from exc
    return {"chain_dir": str(folder), "name": name, "prepared_root": str(prepared), "runs": len(entries), "record": record}
