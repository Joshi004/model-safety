"""The kinds of scan the service can run, and what is particular to each.

A scanner is described by one `Scanner` entry in `SCANNERS`: its label, its settings (the
same shape the start forms are built from), the resources it asks for by default, and a few
functions that hold everything specific to it: the files it is given, the command that
runs it, where its logs are, what it found. The framework in scans.py (records, checking,
starting, waiting for conversions, state, replacing results, cancelling) never names a
scanner, so another scanner is another entry here.

Nothing here starts, stops or deletes anything. The only things that write are
`dedup_write_inputs`, which writes the scan's own `inputs/files.txt`, and `ner_write_inputs`,
which writes its `inputs/policy.json`, both inside the scan's folder; the other functions
read a scan's output or the job scripts.

A scan's folder, as scans.py lays it out:

    <service root>/scans/<scan id>/
        scan.json   inputs/   results/   _logs/
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import jobs
from reader_imports import (
    PII_CATEGORIES,
    REPO_ROOT,
    CandidatePolicy,
    RunFinderError,
    label_tier,
    read_json_or_none,
    select_scan_files,
)

SUCCESS_MARKER = "_SUCCESS"
INPUTS_FOLDER = "inputs"
RESULTS_FOLDER = "results"
LOGS_FOLDER = "_logs"

# Settings the scans' own scripts read from the environment. They are removed from the
# environment the scan's command is started with, so a stray export in the shell the service
# runs in can't change what the command shown does (a DEDUP_REFERENCE_ROOT would even switch
# the scan to another mode, a PRIVACY_FILTER_POLICY would swap the NER policy). Everything a
# scan needs is named in its command.
ENV_PREFIXES = ("DEDUP_", "PII_", "PRIVACY_FILTER_")

# The role of a scan's own job, as opposed to the jobs it has to wait for.
MAIN_JOB_ROLE = "scan"

MODE_ACROSS = "across"
MODE_WITHIN = "within"


class ScannerError(ValueError):
    """A scan's inputs or command can't be made; the message says why."""


@dataclass(frozen=True)
class ScanContext:
    """What a scanner needs to write its inputs and build its command. `version_folders`
    holds the Prepared version folder of each dataset, in the order the scan reads them. A
    folder that doesn't exist yet (a dataset still to be converted) is a placeholder: the
    context is then only used to show the command."""

    scan_id: str
    mode: str | None
    settings: dict[str, Any]
    resources: dict[str, Any]
    folder: Path
    datasets_root: Path
    version_folders: dict[str, Path]


@dataclass(frozen=True)
class Scanner:
    key: str
    label: str
    description: str
    # The stages the scan goes through, as the UI names them.
    stages: tuple[str, ...]
    gpu: bool
    # The settings, in the shape step forms are built from: name, label, type, default, help.
    # The types are `choice`, `integer` and `multi` (see `_check_field`).
    fields: list[dict[str, Any]]
    default_resources: dict[str, Any]
    # How a scan over several datasets can be made: mode -> what it means. None when the
    # scanner always makes one scan for each dataset (D4).
    modes: dict[str, str] | None
    default_mode: str | None
    # What the headline count of a finished scan counts, for the Datasets table.
    count_label: str
    cost: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    write_inputs: Callable[[ScanContext], None]
    build_command: Callable[[ScanContext], list[str]]
    # The jobs a command started, from what it printed: `(jobs it waits for, its own job ID)`.
    # Each job it waits for is `{"role": ..., "job_id": ...}`. Called on the output of a
    # command that failed too: a job started before the failure still has to be found.
    read_jobs: Callable[[str], tuple[list[dict[str, str]], str | None]]
    # `(name, path)` of each log, for the log tails: the scan's folder, its settings, and its
    # jobs (the waited-for ones first, then its own with the role MAIN_JOB_ROLE).
    log_files: Callable[[Path, dict[str, Any], list[dict[str, str]]], list[tuple[str, Path]]]
    # What a scan leaves behind when it has finished, as paths in its results folder. The scan
    # has succeeded when SLURM says its job completed and every one of them exists.
    success_markers: tuple[str, ...]
    # What a finished scan found, from its results folder, for the scan's own page.
    read_details: Callable[[Path], dict[str, Any]]
    # The headline count of one dataset: the scan's results folder, the scan's mode, the
    # dataset and the Prepared version the scan read for it.
    headline_count: Callable[[Path, str | None, str, str], int | None]
    # For a scanner whose job scripts fix the CPUs, memory and time (and so the GPUs): what
    # they ask for, for these settings, as `{cpus, mem, time}`. Only the partition can be
    # chosen then. None when the user sets all of them.
    fixed_resources: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # What the scanner looks for, when the settings don't choose it: shown as a read-only list.
    detects: tuple[str, ...] = ()


# -------------------------------------------------------------------------
# Settings and resources
# -------------------------------------------------------------------------


def _choice(name: str, label: str, choices: list[str], default: str, help_text: str) -> dict[str, Any]:
    return {"name": name, "label": label, "type": "choice", "choices": choices, "default": default, "help": help_text}


def _integer(name: str, label: str, low: int, high: int, default: int | None, help_text: str) -> dict[str, Any]:
    """A whole number from `low` to `high`. Empty means `default`, which may be None."""
    return {"name": name, "label": label, "type": "integer", "min": low, "max": high, "default": default, "help": help_text}


def _multi(
    name: str,
    label: str,
    choices: list[str],
    default: list[str],
    help_text: str,
    groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """At least one of `choices`. `groups` is only for showing them together: each is
    `{key, choices}`, and the choices of all of them are the field's."""
    field: dict[str, Any] = {"name": name, "label": label, "type": "multi", "choices": choices, "default": default, "help": help_text}
    if groups is not None:
        field["groups"] = groups
    return field


def _shown(text: str) -> str:
    """A value from a request, short enough to put in a message."""
    return repr(text if len(text) <= 40 else text[:40] + "...")


def _check_field(field: dict[str, Any], value: Any, problems: list[str]) -> Any:
    """One setting, checked against its field. A wrong value is reported and replaced by the
    default, so every problem in a request is found in one go."""
    kind = field["type"]
    label = field["label"]
    if kind == "choice":
        if value in field["choices"]:
            return value
        problems.append(f"{label} must be one of {', '.join(field['choices'])}.")
        return field["default"]
    if kind == "integer":
        if value is None or value == "":
            return field["default"]
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,9}", value.strip()):
            value = int(value.strip())
        if isinstance(value, bool) or not isinstance(value, int) or not field["min"] <= value <= field["max"]:
            problems.append(f"{label} must be a whole number from {field['min']} to {field['max']}.")
            return field["default"]
        return value
    if kind == "multi":
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            problems.append(f"{label} must be a list of names.")
            return list(field["default"])
        unknown = [item for item in dict.fromkeys(value) if item not in field["choices"]]
        if unknown:
            problems.append(f"{label}: {', '.join(_shown(item) for item in unknown[:3])} isn't one of the choices.")
            return list(field["default"])
        if not value:
            problems.append(f"{label}: choose at least one.")
            return list(field["default"])
        # In the order of the choices, so the same selection is always the same value.
        chosen = set(value)
        return [choice for choice in field["choices"] if choice in chosen]
    raise ValueError(f"Setting {field['name']} has a type this module doesn't know: {kind}.")


def parse_settings(scanner: Scanner, raw: Any, problems: list[str]) -> dict[str, Any]:
    """Every setting of the scanner with a checked value (the default for one that isn't
    given). What is wrong is added to `problems`."""
    if raw is not None and not isinstance(raw, dict):
        problems.append("The settings must be an object of name: value pairs.")
    given = raw if isinstance(raw, dict) else {}
    known = {field["name"] for field in scanner.fields}
    for name in given:
        if name not in known:
            problems.append(f"{scanner.label} has no setting called {name!r}.")
    return {
        field["name"]: _check_field(field, given.get(field["name"], field["default"]), problems)
        for field in scanner.fields
    }


def sbatch_header(script: str) -> dict[str, Any]:
    """The `#SBATCH` lines of a job script in launchers/slurm: CPUs, memory, time limit and
    the GPUs its `--gres` asks for. None for any of the first three it doesn't say."""
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


def script_resources(script: str) -> dict[str, Any]:
    """`{cpus, mem, time}` a job script asks for in its own `#SBATCH` lines. A job that fixes
    its resources has to say all three: without a memory or time line SLURM gives it a whole
    node's memory and no end."""
    header = sbatch_header(script)
    missing = [name for name in ("cpus", "mem", "time") if header[name] is None]
    if missing:
        raise ScannerError(f"launchers/slurm/{script} has no #SBATCH line for its {', '.join(missing)}.")
    return {name: header[name] for name in ("cpus", "mem", "time")}


def parse_resources(scanner: Scanner, settings: dict[str, Any], raw: Any, problems: list[str]) -> dict[str, Any] | None:
    """The partition, CPUs, memory and time limit: the scanner's defaults, with whatever the
    request gives on top, checked by jobs.validate_resources. None when they can't be used.
    For a scanner whose job scripts fix the resources only the partition is taken (and
    anything else is refused); the rest are what its scripts ask for, for these settings."""
    given = raw if isinstance(raw, dict) else {}
    if scanner.fixed_resources is None:
        merged = dict(scanner.default_resources)
        merged.update({name: given[name] for name in ("partition", "cpus", "mem", "time") if given.get(name) not in (None, "")})
        try:
            return jobs.validate_resources(merged)
        except (jobs.JobsError, jobs.SlurmUnavailable) as exc:
            problems.append(str(exc))
        return None

    fixed_names = [name for name in ("cpus", "mem", "time") if given.get(name) not in (None, "")]
    if fixed_names:
        problems.append(
            f"{scanner.label} gets its CPUs, memory and time from its job scripts, so only the partition can be "
            f"chosen ({', '.join(fixed_names)} given)."
        )
    partition = given.get("partition")
    if partition in (None, ""):
        partition = scanner.default_resources["partition"]
    try:
        partitions = jobs.list_partitions()
        fixed = scanner.fixed_resources(settings)
    except (jobs.SlurmUnavailable, ScannerError) as exc:
        problems.append(str(exc))
        return None
    if not isinstance(partition, str) or partition not in partitions:
        problems.append(f"Partition {_shown(str(partition))} isn't one sinfo lists ({', '.join(partitions)}).")
        return None
    if fixed_names:
        return None
    return {"partition": partition, **fixed}


def describe(scanner: Scanner) -> dict[str, Any]:
    """The scanner as the start form needs it."""
    return {
        "key": scanner.key,
        "label": scanner.label,
        "description": scanner.description,
        "stages": list(scanner.stages),
        "gpu": scanner.gpu,
        "fields": scanner.fields,
        "default_resources": scanner.default_resources,
        "resources_fixed": scanner.fixed_resources is not None,
        "detects": list(scanner.detects),
        "modes": scanner.modes,
        "default_mode": scanner.default_mode,
        "count_label": scanner.count_label,
    }


# -------------------------------------------------------------------------
# Reading the jobs a command started
# -------------------------------------------------------------------------


def read_parsable_job(output: str) -> tuple[list[dict[str, str]], str | None]:
    """The one job of `sbatch --parsable`, which prints "<job id>" or "<job id>;<cluster>".
    What it printed instead is returned as it is, for scans.py to refuse."""
    first = output.strip().splitlines()[-1].split(";")[0] if output.strip() else None
    return [], first


def _read_wrapper_jobs(output: str, waited_for: tuple[tuple[str, str], ...], own: str) -> tuple[list[dict[str, str]], str | None]:
    """The jobs a wrapper script printed: `waited_for` and `own` are patterns with one group
    for the job ID, the first as `(role, pattern)`."""
    prior = []
    for role, pattern in waited_for:
        match = re.search(pattern, output)
        if match:
            prior.append({"role": role, "job_id": match.group(1)})
    match = re.search(own, output)
    return prior, match.group(1) if match else None


# -------------------------------------------------------------------------
# Duplicates (dedup)
# -------------------------------------------------------------------------

DEDUP_JOB_SCRIPT = REPO_ROOT / "launchers" / "slurm" / "run_dedup_scan.sbatch"
DEDUP_MANIFEST = "files.txt"


def _dedup_cost(_settings: dict[str, Any], resources: dict[str, Any]) -> dict[str, Any]:
    return {
        "gpus": 0,
        "cpus": resources["cpus"],
        "mem": resources["mem"],
        "time": resources["time"],
        "summary": f"{resources['cpus']} CPUs, {resources['mem']} memory, up to {resources['time']}. No GPU.",
    }


def dedup_write_inputs(context: ScanContext) -> None:
    """For a scan across datasets, write `inputs/files.txt`: the files of every dataset's
    Prepared version, relative to the datasets folder, one dataset after another in the order
    of `context.version_folders`. Rows are numbered in this order, and the earliest row of a
    group of duplicates is the one kept, so it is the order that decides which copy counts as
    the original. A scan within one dataset needs no list: it reads that dataset's folder."""
    if context.mode != MODE_ACROSS:
        return
    lines: list[str] = []
    for dataset_id, version_folder in context.version_folders.items():
        try:
            # A Prepared version is a finished run itself, so it is read as one.
            selection = select_scan_files(version_folder)
            relative = version_folder.relative_to(context.datasets_root).as_posix()
        except (RunFinderError, ValueError) as exc:
            raise ScannerError(f"The files of dataset {dataset_id} can't be listed: {exc}") from exc
        lines.extend(f"{relative}/{item.path}" for item in selection.files)
    inputs = context.folder / INPUTS_FOLDER
    inputs.mkdir(parents=True, exist_ok=True)
    temp = inputs / f"{DEDUP_MANIFEST}.tmp"
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temp, inputs / DEDUP_MANIFEST)


def dedup_build_command(context: ScanContext) -> list[str]:
    """`sbatch` on launchers/slurm/run_dedup_scan.sbatch, which already reads everything
    below from its environment (the settings go in `--export`, so the command shown is the
    command that runs). This doesn't go through scripts/tool_team_dedup_scan.sh: that
    wrapper has no way to pass a file list to a full run, and its checks (a job already
    running, a run folder that isn't empty) are what scans.py checks for itself."""
    results = context.folder / RESULTS_FOLDER
    logs = context.folder / LOGS_FOLDER
    if context.mode == MODE_ACROSS:
        input_root = context.datasets_root
        manifest: Path | None = context.folder / INPUTS_FOLDER / DEDUP_MANIFEST
    else:
        (input_root,) = context.version_folders.values()
        manifest = None

    variables = {
        "REPO": str(REPO_ROOT),
        "DEDUP_INPUT_ROOT": str(input_root),
        "DEDUP_INPUT_MODE": "runs",
        "DEDUP_RUN_DIR": str(results),
        "DEDUP_LOG_ROOT": str(logs / "dedup"),
        "DEDUP_TEXT_SCOPE": context.settings["text_scope"],
        "DEDUP_EXCLUDE_SYSTEM": context.settings["exclude_system"],
        "DEDUP_WRITE_FILTERED": context.settings["write_filtered"],
        "DEDUP_OVERWRITE": "0",
    }
    if manifest is not None:
        variables["DEDUP_INPUT_MANIFEST"] = str(manifest)
    for name, value in variables.items():
        # `--export` is a list separated by commas, so a comma in a path would split it.
        if "," in value or "\n" in value:
            raise ScannerError(f"{name} can't be set: its value ({value!r}) has a comma or a line break in it.")

    resources = context.resources
    return [
        "sbatch",
        "--parsable",
        f"--partition={resources['partition']}",
        f"--job-name=scan_dedup_{context.scan_id}",
        f"--cpus-per-task={resources['cpus']}",
        f"--mem={resources['mem']}",
        f"--time={resources['time']}",
        f"--output={logs}/%j.out",
        f"--error={logs}/%j.err",
        "--export=ALL," + ",".join(f"{name}={value}" for name, value in variables.items()),
        str(DEDUP_JOB_SCRIPT),
    ]


def main_job_id(jobs_started: list[dict[str, str]]) -> str | None:
    """The ID of the scan's own job among the jobs of a scan."""
    return next((job["job_id"] for job in jobs_started if job["role"] == MAIN_JOB_ROLE), None)


def dedup_log_files(folder: Path, _settings: dict[str, Any], jobs_started: list[dict[str, str]]) -> list[tuple[str, Path]]:
    # The job script writes its own output into DEDUP_LOG_ROOT (set to `<logs>/dedup`), so the
    # SLURM files only hold what happens before that, such as a failed `cd`.
    logs = folder / LOGS_FOLDER
    job_id = main_job_id(jobs_started)
    if job_id is None:
        return []
    return [
        ("Scan output", logs / "dedup" / "main" / f"dedup_{job_id}.log"),
        ("Scan errors", logs / "dedup" / "err" / f"dedup_{job_id}.err"),
        ("SLURM output", logs / f"{job_id}.out"),
        ("SLURM errors", logs / f"{job_id}.err"),
    ]


DEDUP_DETAIL_KEYS = (
    "mode",
    "input_mode",
    "files",
    "rows_total",
    "rows_hashed",
    "rows_without_text",
    "candidate_pairs",
    "duplicate_rows",
    "configuration",
)


def dedup_read_details(results: Path) -> dict[str, Any]:
    summary = read_json_or_none(results / "summary.json")
    return {key: summary.get(key) for key in DEDUP_DETAIL_KEYS} if isinstance(summary, dict) else {}


def dedup_headline_count(results: Path, mode: str | None, dataset_id: str, version_id: str) -> int | None:
    """The duplicate rows found in one dataset: the `findings` of its files in the scan's
    `summary.json`. The files of a scan across datasets are named from the datasets folder,
    so a dataset's are the ones under its own Prepared version; a scan within one dataset
    read that dataset's folder, so every file in it is the dataset's."""
    summary = read_json_or_none(results / "summary.json")
    per_file = summary.get("per_file") if isinstance(summary, dict) else None
    if not isinstance(per_file, dict):
        return None
    prefix = f"{dataset_id}/prepared/{version_id}/" if mode == MODE_ACROSS else ""
    total = 0
    for path, entry in per_file.items():
        findings = entry.get("findings") if isinstance(entry, dict) else None
        if path.startswith(prefix) and isinstance(findings, int) and not isinstance(findings, bool):
            total += findings
    return total


SCANNERS: dict[str, Scanner] = {
    "dedup": Scanner(
        key="dedup",
        label="Duplicates",
        description="Finds rows that repeat, exactly or nearly, inside one dataset or across several.",
        stages=("Scan",),
        gpu=False,
        fields=[
            _choice("text_scope", "Text compared", ["prompt", "all"], "prompt", "prompt compares what was asked; all compares the whole conversation."),
            _choice("exclude_system", "Leave out system messages", ["1", "0"], "1", "System messages carry tool definitions, not conversation."),
            _choice("write_filtered", "Write filtered copies of every row", ["0", "1"], "0", "The scan only flags duplicates; the copies aren't used."),
        ],
        # What launchers/slurm/run_dedup_scan.sbatch asks for by itself.
        default_resources={"partition": jobs.DEFAULT_RESOURCES["partition"], "cpus": 16, "mem": "128G", "time": "24:00:00"},
        modes={
            MODE_ACROSS: "Compare rows across all the chosen datasets together, so rows are compared between datasets too.",
            MODE_WITHIN: "Compare each dataset only with itself.",
        },
        default_mode=MODE_ACROSS,
        count_label="duplicate rows",
        cost=_dedup_cost,
        write_inputs=dedup_write_inputs,
        build_command=dedup_build_command,
        read_jobs=read_parsable_job,
        log_files=dedup_log_files,
        success_markers=(SUCCESS_MARKER,),
        read_details=dedup_read_details,
        headline_count=dedup_headline_count,
    ),
}
