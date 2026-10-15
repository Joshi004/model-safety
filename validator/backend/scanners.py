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
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import jobs
from reader_imports import (
    PII_CATEGORIES,
    PII_STAGE1_NAME,
    PII_STAGE2_NAME,
    PII_STAGE3_NAME,
    PII_SUCCESS_FILENAME,
    PII_SUMMARY_FILENAME,
    REPO_ROOT,
    CandidatePolicy,
    TOXICITY_REMOVABLE_VERDICTS,
    RunFinderError,
    label_tier,
    read_json_or_none,
    select_scan_files,
)

SUCCESS_MARKER = "_SUCCESS"
INPUTS_FOLDER = "inputs"
RESULTS_FOLDER = "results"
LOGS_FOLDER = "_logs"

# Settings the scans' own scripts read from the environment (the toxicity wrapper reads
# `TOX_`, `GUARD_`, `VLLM_` and `QUALITY_` ones). They are removed from the environment the
# scan's command is started with, so a stray export in the shell the service runs in can't
# change what the command shown does (a DEDUP_REFERENCE_ROOT would even switch the scan to
# another mode, a PRIVACY_FILTER_POLICY would swap the NER policy). Everything a scan needs is
# named in its command. HF_TOKEN and PATH are kept: the scans need them.
ENV_PREFIXES = ("DEDUP_", "PII_", "PRIVACY_FILTER_", "TOX_", "GUARD_", "VLLM_", "QUALITY_")

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
    # The types are `choice`, `integer`, `decimal` and `multi` (see `_check_field`).
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
    # More markers a finished scan has, found from its results folder, for a scan that leaves
    # one for each piece of its work (a toxicity shard) and so not a fixed list. They are
    # needed as well as `success_markers`. Use `markers_of`, which puts the two together.
    markers_for: Callable[[Path], tuple[str, ...]] | None = None
    # How far a scan that is running has got, from its results folder: one entry for each stage
    # that can be counted, `{stage, done, total, unit}`. None when nothing can be told yet.
    read_progress: Callable[[Path], list[dict[str, Any]] | None] | None = None


# -------------------------------------------------------------------------
# Settings and resources
# -------------------------------------------------------------------------


def _choice(name: str, label: str, choices: list[str], default: str, help_text: str) -> dict[str, Any]:
    return {"name": name, "label": label, "type": "choice", "choices": choices, "default": default, "help": help_text}


def _integer(name: str, label: str, low: int, high: int, default: int | None, help_text: str) -> dict[str, Any]:
    """A whole number from `low` to `high`. Empty means `default`, which may be None."""
    return {"name": name, "label": label, "type": "integer", "min": low, "max": high, "default": default, "help": help_text}


def _decimal(name: str, label: str, low: float, high: float, step: float, default: float, help_text: str) -> dict[str, Any]:
    """A number from `low` to `high` in steps of `step`, with at most two decimals."""
    return {"name": name, "label": label, "type": "decimal", "min": low, "max": high, "step": step, "default": default, "help": help_text}


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


DECIMAL_TEXT = re.compile(r"^[0-9]{1,3}(?:\.[0-9]{1,2})?$")


def _hundredths(value: Any) -> int | None:
    """A number with at most two decimals, from a number or its text, as a whole number of
    hundredths (0.35 is 35). Whole numbers are used so that "is it a multiple of the step"
    isn't decided by float rounding. None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if DECIMAL_TEXT.fullmatch(text) is None:
            return None
        number = Decimal(text)
    elif isinstance(value, (int, float)):
        # str() gives the shortest text that reads back as the same float, so 0.1 + 0.2 is
        # refused (it is 0.30000000000000004) and 0.35 is accepted.
        number = Decimal(str(value))
    else:
        return None
    # JSON read by Python accepts NaN and Infinity.
    if not number.is_finite():
        return None
    hundredths = number * 100
    if hundredths != hundredths.to_integral_value():
        return None
    return int(hundredths)


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
    if kind == "decimal":
        if value is None or value == "":
            return field["default"]
        low, high, step = (round(field[name] * 100) for name in ("min", "max", "step"))
        hundredths = _hundredths(value)
        if hundredths is None or not low <= hundredths <= high or (hundredths - low) % step != 0:
            problems.append(f"{label} must be a number from {field['min']} to {field['max']} in steps of {field['step']}.")
            return field["default"]
        return hundredths / 100
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


def _check_export_values(variables: dict[str, str]) -> None:
    """Refuse a value the command can't carry. The job scripts pass these on in an `--export`
    list, which is separated by commas, so a comma in a path would split it."""
    for name, value in variables.items():
        if "," in value or "\n" in value:
            raise ScannerError(f"{name} can't be set: its value ({value!r}) has a comma or a line break in it.")


def _cpu_cost(_settings: dict[str, Any], resources: dict[str, Any]) -> dict[str, Any]:
    """The cost of a scan that asks for the resources it was given, on CPUs only."""
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
    _check_export_values(variables)

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


def markers_of(scanner: Scanner, results: Path) -> tuple[str, ...]:
    """Every path in a scan's results folder that a finished scan of this scanner has."""
    extra = scanner.markers_for(results) if scanner.markers_for is not None else ()
    return (*scanner.success_markers, *extra)


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


def _sorted_counts(counts: Any) -> dict[str, int] | None:
    """`counts` (name -> number) with the biggest first; None when it isn't such a dict."""
    if not isinstance(counts, dict):
        return None
    numbers = {name: count for name, count in counts.items() if isinstance(count, int) and not isinstance(count, bool)}
    return dict(sorted(numbers.items(), key=lambda item: (-item[1], item[0])))


def _whole_number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# -------------------------------------------------------------------------
# PII found by the NER model (pii_ner)
# -------------------------------------------------------------------------

NER_DIR = REPO_ROOT / "modelsafety" / "data_screening" / "pii" / "model"
NER_POLICY = NER_DIR / "policy.json"
NER_LABELS_FILE = NER_DIR / "nemotron_labels.json"
NER_SUBMIT_SCRIPT = REPO_ROOT / "launchers" / "slurm" / "submit_privacy_filter.sh"
NER_MODEL = "OpenMed/privacy-filter-nemotron-v2"
# The job script of each size of scan. Their `#SBATCH` lines fix the CPUs, memory, time and GPUs.
NER_SIZES = {"full": "run_privacy_filter_node.sbatch", "one_gpu": "run_privacy_filter_pilot.sbatch"}
# The job that plans the pieces of work, which the scan's own job waits for.
NER_PLAN_SCRIPT = "prepare_privacy_filter.sbatch"
NER_POLICY_FILE = "policy.json"
PLAN_JOB_ROLE = "plan"
# D7: the confidence a contextual category needs when the user turns it on. The default policy
# has none for them: they are off.
NER_CONTEXTUAL_THRESHOLD = 0.7
NER_TIERS = ("direct_identifier", "person_linked", "contextual")


def _load_ner_labels() -> list[str]:
    """The model's 55 labels, from the file saved out of its `config.json`."""
    value = json.loads(NER_LABELS_FILE.read_text(encoding="utf-8"))
    labels = value["labels"]
    if not isinstance(labels, list) or not labels or not all(isinstance(label, str) for label in labels):
        raise ScannerError(f"{NER_LABELS_FILE} has no list of labels.")
    return labels


NER_LABELS = _load_ner_labels()


def _ner_label_field() -> dict[str, Any]:
    """The categories to look for: every label of the model, grouped by tier, with the ones the
    default policy enables selected."""
    policy = CandidatePolicy.from_path(NER_POLICY)
    groups: dict[str, list[str]] = {tier: [] for tier in NER_TIERS}
    for label in NER_LABELS:
        groups[label_tier(label)].append(label)
    default = [label for label in NER_LABELS if policy.threshold_for(label, label_tier(label)) is not None]
    return _multi(
        "labels",
        "Categories to look for",
        NER_LABELS,
        default,
        "A category you leave out is never reported. Contextual categories (places, dates, demographics) are off "
        f"by default; one you turn on is reported from a confidence of {NER_CONTEXTUAL_THRESHOLD}.",
        groups=[{"key": tier, "choices": groups[tier]} for tier in NER_TIERS],
    )


def _ner_fixed_resources(settings: dict[str, Any]) -> dict[str, Any]:
    return script_resources(NER_SIZES[settings["size"]])


def _ner_cost(settings: dict[str, Any], _resources: dict[str, Any]) -> dict[str, Any]:
    script = NER_SIZES[settings["size"]]
    own = script_resources(script)
    plan = script_resources(NER_PLAN_SCRIPT)
    gpus = sbatch_header(script)["gpus"]
    return {
        "gpus": gpus,
        **own,
        "summary": (
            f"{gpus} GPU{'' if gpus == 1 else 's'}, {own['cpus']} CPUs, {own['mem']} memory, up to {own['time']}; "
            f"plus a CPU job to plan the pieces of work ({plan['cpus']} CPUs, {plan['mem']}, up to {plan['time']})."
        ),
    }


def ner_write_inputs(context: ScanContext) -> None:
    """Write `inputs/policy.json`: the default policy with the user's categories. A category
    that wasn't chosen gets no threshold, which turns it off; a chosen one keeps the default
    policy's threshold, or gets NER_CONTEXTUAL_THRESHOLD where the default has none (D7).
    Everything else in the policy is kept. The file is read back with the scan's own policy
    reader before it is used, so a bad one is refused here and not by the GPU job."""
    chosen = set(context.settings["labels"])
    policy = json.loads(NER_POLICY.read_text(encoding="utf-8"))
    default = CandidatePolicy.from_path(NER_POLICY)
    expected: dict[str, float | None] = {}
    for label in NER_LABELS:
        threshold = default.threshold_for(label, label_tier(label))
        expected[label] = (NER_CONTEXTUAL_THRESHOLD if threshold is None else threshold) if label in chosen else None
    policy["label_thresholds"] = {**policy.get("label_thresholds", {}), **expected}

    inputs = context.folder / INPUTS_FOLDER
    inputs.mkdir(parents=True, exist_ok=True)
    temp = inputs / f"{NER_POLICY_FILE}.tmp"
    temp.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    try:
        written = CandidatePolicy.from_path(temp)
        wrong = [label for label, threshold in expected.items() if written.threshold_for(label, label_tier(label)) != threshold]
    except (OSError, ValueError, KeyError) as exc:
        temp.unlink(missing_ok=True)
        raise ScannerError(f"The NER policy for this scan can't be read back: {exc}") from exc
    if wrong:
        temp.unlink(missing_ok=True)
        raise ScannerError(f"The NER policy for this scan doesn't hold the chosen categories (wrong for {', '.join(wrong[:3])}).")
    os.replace(temp, inputs / NER_POLICY_FILE)


def ner_build_command(context: ScanContext) -> list[str]:
    """`env <settings> bash launchers/slurm/submit_privacy_filter.sh`, which plans the work and
    submits the GPU job behind it. The settings are named in the command (`env` puts them in
    the script's environment), after the ones of the shell the service runs in were removed,
    so the command shown is the command that runs. This isn't scripts/tool_team_ner_scan.sh:
    that wrapper only adds defaults and printing to this script."""
    (input_root,) = context.version_folders.values()
    full = context.settings["size"] == "full"
    variables = {
        "REPO": str(REPO_ROOT),
        "PRIVACY_FILTER_MODE": "full" if full else "pilot",
        "PRIVACY_FILTER_INPUT_ROOT": str(input_root),
        "PRIVACY_FILTER_INPUT_MODE": "runs",
        "PRIVACY_FILTER_RUN_ROOT": str(context.folder / RESULTS_FOLDER),
        "PRIVACY_FILTER_PARTITION": context.resources["partition"],
        "PRIVACY_FILTER_BACKEND": "transformers",
        "PRIVACY_FILTER_MODEL": NER_MODEL,
        "PRIVACY_FILTER_POLICY": str(context.folder / INPUTS_FOLDER / NER_POLICY_FILE),
        # So a failed planning job takes the GPU job behind it with it, instead of leaving it
        # pending for ever.
        "PRIVACY_FILTER_KILL_ON_INVALID_DEP": "1",
    }
    if not full:
        # Without these the one-GPU size reads one file and one piece of work (a pilot).
        variables["PRIVACY_FILTER_MAX_FILES"] = "100000"
        variables["PRIVACY_FILTER_MAX_UNITS"] = "0"
    _check_export_values(variables)
    return ["env", *(f"{name}={value}" for name, value in variables.items()), "bash", str(NER_SUBMIT_SCRIPT)]


def ner_read_jobs(output: str) -> tuple[list[dict[str, str]], str | None]:
    return _read_wrapper_jobs(
        output,
        ((PLAN_JOB_ROLE, r"Manifest job: (\d+)"),),
        r"Privacy Filter (?:full|pilot) job: (\d+)",
    )


def ner_log_files(folder: Path, settings: dict[str, Any], jobs_started: list[dict[str, str]]) -> list[tuple[str, Path]]:
    # The job scripts name their own SLURM logs (`logs/slurm/` in the repo, by job ID).
    slurm = REPO_ROOT / "logs" / "slurm"
    full = settings["size"] == "full"
    files: list[tuple[str, Path]] = []
    for job in jobs_started:
        if job["role"] == PLAN_JOB_ROLE:
            files.append(("Planning output", slurm / f"privacy_filter_manifest_{job['job_id']}.out"))
            files.append(("Planning errors", slurm / f"privacy_filter_manifest_{job['job_id']}.err"))
        elif job["role"] == MAIN_JOB_ROLE:
            name = "node" if full else "pilot"
            files.append(("Scan output", slurm / f"privacy_filter_{name}_{job['job_id']}.out"))
            files.append(("Scan errors", slurm / f"privacy_filter_{name}_{job['job_id']}.err"))
    if full and main_job_id(jobs_started) is not None:
        files.append(("Worker on GPU 0", folder / RESULTS_FOLDER / "worker_logs" / "worker_0.log"))
    return files


NER_DETAIL_KEYS = (
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
)


def ner_read_details(results: Path) -> dict[str, Any]:
    summary = read_json_or_none(results / "summary.json")
    if not isinstance(summary, dict):
        return {}
    details = {key: summary.get(key) for key in NER_DETAIL_KEYS}
    details["labels"] = _sorted_counts(summary.get("labels"))
    missing = summary.get("missing_units")
    details["missing_units"] = len(missing) if isinstance(missing, list) else None
    return details


def ner_headline_count(results: Path, _mode: str | None, _dataset_id: str, _version_id: str) -> int | None:
    """The spans the policy counts: the scan reads one dataset, so its totals are the dataset's."""
    summary = read_json_or_none(results / "summary.json")
    return _whole_number(summary.get("policy_eligible_spans")) if isinstance(summary, dict) else None


# -------------------------------------------------------------------------
# PII found by rules (pii_regex)
# -------------------------------------------------------------------------

REGEX_JOB_SCRIPT = REPO_ROOT / "launchers" / "slurm" / "run_pii_scan_and_extract.sbatch"
# The marker each of the three stages leaves; the scan is finished when all of them are there.
REGEX_MARKERS = tuple(f"{stage}/{PII_SUCCESS_FILENAME}" for stage in (PII_STAGE1_NAME, PII_STAGE2_NAME, PII_STAGE3_NAME))
REGEX_SUMMARY = f"{PII_STAGE2_NAME}/{PII_SUMMARY_FILENAME}"


def regex_build_command(context: ScanContext) -> list[str]:
    """`sbatch` on launchers/slurm/run_pii_scan_and_extract.sbatch, which reads everything below
    from its environment (the settings go in `--export`, so the command shown is the command
    that runs). `PII_LOG_ROOT` keeps the job's own logs inside the scan's folder."""
    (input_root,) = context.version_folders.values()
    logs = context.folder / LOGS_FOLDER
    resources = context.resources
    variables = {
        "PII_REPO": str(REPO_ROOT),
        "PII_BASE_DIR": str(input_root),
        "PII_INPUT_MODE": "runs",
        "PII_RUN_DIR": str(context.folder / RESULTS_FOLDER),
        "PII_STAGES": "all",
        "PII_NUM_WORKERS": str(context.settings["workers"] or resources["cpus"]),
        "PII_CHUNK_SIZE": "10000",
        "PII_LOG_ROOT": str(logs / "pii"),
    }
    _check_export_values(variables)
    return [
        "sbatch",
        "--parsable",
        f"--partition={resources['partition']}",
        f"--job-name=scan_pii_regex_{context.scan_id}",
        f"--cpus-per-task={resources['cpus']}",
        f"--mem={resources['mem']}",
        f"--time={resources['time']}",
        f"--output={logs}/%j.out",
        f"--error={logs}/%j.err",
        "--export=ALL," + ",".join(f"{name}={value}" for name, value in variables.items()),
        str(REGEX_JOB_SCRIPT),
    ]


def regex_log_files(folder: Path, _settings: dict[str, Any], jobs_started: list[dict[str, str]]) -> list[tuple[str, Path]]:
    # The job script writes its own output into PII_LOG_ROOT (`<logs>/pii`); the SLURM files
    # only hold what happens before that.
    logs = folder / LOGS_FOLDER
    job_id = main_job_id(jobs_started)
    if job_id is None:
        return []
    return [
        ("Scan output", logs / "pii" / "main" / f"pii_scan_and_extract_{job_id}.log"),
        ("Scan errors", logs / "pii" / "err" / f"pii_scan_and_extract_{job_id}.err"),
        ("SLURM output", logs / f"{job_id}.out"),
        ("SLURM errors", logs / f"{job_id}.err"),
    ]


def regex_read_details(results: Path) -> dict[str, Any]:
    summary = read_json_or_none(results / REGEX_SUMMARY)
    if not isinstance(summary, dict):
        return {}
    categories = summary.get("categories")
    kept = (
        {name: entry.get("kept") for name, entry in categories.items() if isinstance(entry, dict)}
        if isinstance(categories, dict)
        else None
    )
    return {
        "raw_hits": summary.get("raw_total"),
        "kept_hits": summary.get("kept_total"),
        "rows_with_hits": summary.get("unique_locations"),
        "kept_by_category": _sorted_counts(kept),
    }


def regex_headline_count(results: Path, _mode: str | None, _dataset_id: str, _version_id: str) -> int | None:
    """The hits that passed the checks that drop false ones. The scan reads one dataset, so
    its total is the dataset's."""
    summary = read_json_or_none(results / REGEX_SUMMARY)
    return _whole_number(summary.get("kept_total")) if isinstance(summary, dict) else None


# -------------------------------------------------------------------------
# Toxicity (toxicity)
# -------------------------------------------------------------------------

# The wrapper plans the shards (a prepare job) and submits one array job behind it: a task per
# shard, which screens it with Detoxify and then judges what was flagged with Qwen3Guard.
TOXICITY_WRAPPER = REPO_ROOT / "scripts" / "tool_team_toxicity_scan.sh"
TOXICITY_ARRAY_SCRIPT = "run_toxicity_scan_array.sbatch"
TOXICITY_MANIFEST = "manifests/manifest.json"
# Where the array job writes each shard (as modelsafety/results/coverage.py reads them).
TOXICITY_SHARDS_FOLDER = "scans/toxicity/shards"
DETOXIFY_SUCCESS_MARKER = "_DETOXIFY_SUCCESS"
TOXICITY_STAGES = (
    "Quick screen: Detoxify (Unitary, multilingual model)",
    "Judge: Qwen3Guard-Gen-8B",
)
# What Qwen3Guard names a message's categories (`CATEGORY_RE` in llm_check_guard.py, which needs
# `openai` and so isn't imported: keep them in step), without the "None" it gives a message
# that has none.
GUARD_CATEGORIES = (
    "Violent",
    "Non-violent Illegal Acts",
    "Sexual Content or Sexual Acts",
    "PII",
    "Suicide & Self-Harm",
    "Unethical Acts",
    "Politically Sensitive Topics",
    "Copyright Violation",
    "Jailbreak",
)


def _toxicity_check_shards(settings: dict[str, Any]) -> None:
    if settings["concurrency"] > settings["shards"]:
        raise ScannerError("Shards at once can't be more than the shards.")


def _toxicity_cost(settings: dict[str, Any], resources: dict[str, Any]) -> dict[str, Any]:
    _toxicity_check_shards(settings)
    # One GPU for each shard that runs at once.
    per_shard = sbatch_header(TOXICITY_ARRAY_SCRIPT)["gpus"]
    if not per_shard:
        # Without this a scan that does use GPUs would start without the GPU confirmation.
        raise ScannerError(f"launchers/slurm/{TOXICITY_ARRAY_SCRIPT} has no GPU request, so its cost can't be shown.")
    gpus = per_shard * settings["concurrency"]
    return {
        "gpus": gpus,
        "cpus": resources["cpus"],
        "mem": resources["mem"],
        "time": resources["time"],
        "summary": (
            f"Up to {settings['shards']} shards, {settings['concurrency']} at once: up to {gpus} GPU{'' if gpus == 1 else 's'} "
            f"at once. Each shard {resources['cpus']} CPUs, {resources['mem']} memory, up to {resources['time']}. "
            "There are never more shards than the dataset has files. A small CPU job plans the shards first."
        ),
    }


def toxicity_build_command(context: ScanContext) -> list[str]:
    """`env TOX_KILL_ON_INVALID_DEP=1 bash scripts/tool_team_toxicity_scan.sh all ...`, which
    submits the job that plans the shards and, behind it, the array job. Every setting is a
    flag, after the variables of the shell the service runs in were removed, so the command
    shown is the command that runs. The categories and verdicts aren't in it: the scan always
    judges every category, and they only decide what counts as flagged when results are read
    (D8)."""
    _toxicity_check_shards(context.settings)
    (input_root,) = context.version_folders.values()
    results = context.folder / RESULTS_FOLDER
    # The wrapper passes these on in an `--export` list, which is separated by commas.
    _check_export_values({"TOX_INPUT_ROOT": str(input_root), "TOX_RUN_DIR": str(results)})
    settings = context.settings
    resources = context.resources
    return [
        "env",
        # So a failed planning job takes the array job behind it with it, instead of leaving it
        # pending for ever.
        "TOX_KILL_ON_INVALID_DEP=1",
        "bash",
        str(TOXICITY_WRAPPER),
        "all",
        "--input-root",
        str(input_root),
        "--run-dir",
        str(results),
        "--input-mode",
        "runs",
        "--pilot-files",
        "0",
        "--shards",
        str(settings["shards"]),
        "--concurrency",
        str(settings["concurrency"]),
        "--threshold",
        str(settings["threshold"]),
        "--cpus",
        str(resources["cpus"]),
        "--mem",
        str(resources["mem"]),
        "--time",
        str(resources["time"]),
        "--partition",
        str(resources["partition"]),
    ]


def toxicity_read_jobs(output: str) -> tuple[list[dict[str, str]], str | None]:
    return _read_wrapper_jobs(
        output,
        ((PLAN_JOB_ROLE, r"Submitted prepare job: (\d+)"),),
        r"Submitted (?:all )?job: (\d+)",
    )


def toxicity_log_files(folder: Path, _settings: dict[str, Any], jobs_started: list[dict[str, str]]) -> list[tuple[str, Path]]:
    # The wrapper names its own SLURM logs (`logs/slurm/` in the repo, by job ID). Of the shards,
    # the first one's are shown.
    slurm = REPO_ROOT / "logs" / "slurm"
    files: list[tuple[str, Path]] = []
    for job in jobs_started:
        if job["role"] == PLAN_JOB_ROLE:
            files.append(("Planning output", slurm / f"tool_team_toxicity_prep_{job['job_id']}.out"))
            files.append(("Planning errors", slurm / f"tool_team_toxicity_prep_{job['job_id']}.err"))
        elif job["role"] == MAIN_JOB_ROLE:
            files.append(("Shard 0 output", slurm / f"tool_team_toxicity_all_{job['job_id']}_0.out"))
            files.append(("Shard 0 errors", slurm / f"tool_team_toxicity_all_{job['job_id']}_0.err"))
            files.append(("Shard 0 model server", folder / RESULTS_FOLDER / TOXICITY_SHARDS_FOLDER / "shard_00000" / "vllm.log"))
    return files


def _toxicity_shard_names(results: Path) -> list[str] | None:
    """The names of the scan's shards (`shard_00000`, ...), from the manifest the prepare job
    wrote. None when it isn't there or doesn't list them."""
    manifest = read_json_or_none(results / TOXICITY_MANIFEST)
    shards = manifest.get("shards") if isinstance(manifest, dict) else None
    if not isinstance(shards, list):
        return None
    indexes = [shard.get("index") for shard in shards if isinstance(shard, dict)]
    return [
        f"shard_{index:05d}"
        for index in dict.fromkeys(indexes)
        if isinstance(index, int) and not isinstance(index, bool) and index >= 0
    ]


def toxicity_markers(results: Path) -> tuple[str, ...]:
    """The `_SUCCESS` of every shard. The manifest itself is one of the scanner's own
    `success_markers`, so a missing manifest is reported as that."""
    return tuple(f"{TOXICITY_SHARDS_FOLDER}/{name}/{SUCCESS_MARKER}" for name in _toxicity_shard_names(results) or ())


def toxicity_progress(results: Path) -> list[dict[str, Any]] | None:
    """Shards screened and shards judged, out of all of them. The shards' folder is listed and
    only the folders in it are looked into, not each shard's by name: asking for one before a
    task has made it would be remembered as "not there" for about a minute. (The listing of
    the shards' folder itself can still hit that for a poll or two when the first task has
    only just started; then there is simply no progress to show yet.)"""
    names = _toxicity_shard_names(results)
    if not names:
        return None
    shards_root = results / TOXICITY_SHARDS_FOLDER
    screened = 0
    judged = 0
    try:
        with os.scandir(shards_root) as entries:
            present = {entry.name for entry in entries if entry.is_dir()}
        for name in names:
            if name not in present:
                continue
            with os.scandir(shards_root / name) as files:
                made = {entry.name for entry in files}
            screened += DETOXIFY_SUCCESS_MARKER in made
            judged += SUCCESS_MARKER in made
    except OSError:
        return None
    return [
        {"stage": TOXICITY_STAGES[0], "done": screened, "total": len(names), "unit": "shards"},
        {"stage": TOXICITY_STAGES[1], "done": judged, "total": len(names), "unit": "shards"},
    ]


def _add_counts(into: dict[str, int], counts: Any) -> None:
    if isinstance(counts, dict):
        for name, count in counts.items():
            if isinstance(count, int) and not isinstance(count, bool):
                into[name] = into.get(name, 0) + count


def toxicity_read_details(results: Path) -> dict[str, Any]:
    """What the shards found, added up: from each shard's `detoxify_summary.json` and
    `guard_results/summary.json`."""
    names = _toxicity_shard_names(results)
    if names is None:
        return {}
    totals = {"rows_read": 0, "messages_scored": 0, "messages_flagged": 0, "guard_records": 0, "guard_errors": 0, "guard_unknown": 0}
    verdicts: dict[str, int] = {}
    categories: dict[str, int] = {}
    judged = 0
    for name in names:
        shard = results / TOXICITY_SHARDS_FOLDER / name
        detoxify = read_json_or_none(shard / "detoxify_summary.json")
        if isinstance(detoxify, dict):
            for key in ("rows_read", "messages_scored", "messages_flagged"):
                totals[key] += _whole_number(detoxify.get(key)) or 0
        guard = read_json_or_none(shard / "guard_results" / "summary.json")
        if isinstance(guard, dict):
            judged += 1
            for key, source in (("guard_records", "total_records"), ("guard_errors", "errors"), ("guard_unknown", "unknown")):
                totals[key] += _whole_number(guard.get(source)) or 0
            _add_counts(verdicts, guard.get("safety"))
            _add_counts(categories, guard.get("categories"))
    return {
        "shards": len(names),
        "shards_judged": judged,
        **totals,
        "verdicts": _sorted_counts(verdicts),
        "categories": _sorted_counts(categories),
    }


def toxicity_headline_count(results: Path, _mode: str | None, _dataset_id: str, _version_id: str) -> int | None:
    """The messages the guard didn't call Safe: Unsafe, Controversial, and the ones with no
    verdict it could use, which count as flagged (D9). It doesn't depend on the categories and
    verdicts the user chose, which only apply when the results are read (D8)."""
    names = _toxicity_shard_names(results)
    if not names:
        return None
    total = 0
    for name in names:
        guard = read_json_or_none(results / TOXICITY_SHARDS_FOLDER / name / "guard_results" / "summary.json")
        records = _whole_number(guard.get("total_records")) if isinstance(guard, dict) else None
        safety = guard.get("safety") if isinstance(guard, dict) else None
        if records is None or not isinstance(safety, dict):
            # A shard that can't be counted would make the total too low.
            return None
        total += records - (_whole_number(safety.get("Safe")) or 0)
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
        cost=_cpu_cost,
        write_inputs=dedup_write_inputs,
        build_command=dedup_build_command,
        read_jobs=read_parsable_job,
        log_files=dedup_log_files,
        success_markers=(SUCCESS_MARKER,),
        read_details=dedup_read_details,
        headline_count=dedup_headline_count,
    ),
    "pii_ner": Scanner(
        key="pii_ner",
        label="PII (NER)",
        description=(
            "Finds personal information with a model that reads every message and marks what it "
            "recognises: names, addresses, ID numbers and more. You choose the categories."
        ),
        stages=("Plan the pieces of work", "Model scan", "Combine the results"),
        gpu=True,
        fields=[
            _choice(
                "size",
                "Size",
                list(NER_SIZES),
                "full",
                "full uses every GPU of one node, for a big dataset; one_gpu uses a single GPU. "
                "What each asks for is shown below.",
            ),
            _ner_label_field(),
        ],
        # Only the partition is chosen; the job scripts fix the rest (the full size's values here).
        default_resources={"partition": jobs.DEFAULT_RESOURCES["partition"], **script_resources(NER_SIZES["full"])},
        modes=None,
        default_mode=None,
        count_label="PII findings",
        cost=_ner_cost,
        write_inputs=ner_write_inputs,
        build_command=ner_build_command,
        read_jobs=ner_read_jobs,
        log_files=ner_log_files,
        success_markers=(SUCCESS_MARKER,),
        read_details=ner_read_details,
        headline_count=ner_headline_count,
        fixed_resources=_ner_fixed_resources,
    ),
    "pii_regex": Scanner(
        key="pii_regex",
        label="PII (rules)",
        description=(
            "Finds personal information with fixed patterns, then drops the matches that fail "
            "checks for false positives. It can't find names or ID numbers."
        ),
        stages=("Pattern scan", "Validation", "Extract"),
        gpu=False,
        fields=[_integer("workers", "Workers", 1, 64, None, "Leave empty to use one worker for each CPU.")],
        default_resources={"partition": jobs.DEFAULT_RESOURCES["partition"], "cpus": 32, "mem": "64G", "time": "06:00:00"},
        modes=None,
        default_mode=None,
        count_label="PII findings",
        cost=_cpu_cost,
        write_inputs=lambda _context: None,
        build_command=regex_build_command,
        read_jobs=read_parsable_job,
        log_files=regex_log_files,
        success_markers=REGEX_MARKERS,
        read_details=regex_read_details,
        headline_count=regex_headline_count,
        detects=tuple(PII_CATEGORIES),
    ),
    "toxicity": Scanner(
        key="toxicity",
        label="Toxicity",
        description=(
            "Finds toxic messages in two stages: a fast model (Detoxify) screens every message, and "
            "Qwen3Guard judges the ones it flagged. It needs GPUs."
        ),
        stages=TOXICITY_STAGES,
        gpu=True,
        fields=[
            _decimal(
                "threshold",
                "Detoxify threshold",
                0.05,
                0.95,
                0.05,
                0.5,
                "A message goes to the judge when its highest Detoxify score is above this. A lower value sends "
                "more messages to the judge, so the scan is slower and finds more. Changing it needs a new scan.",
            ),
            _multi(
                "categories",
                "Guard categories that count as flagged",
                list(GUARD_CATEGORIES),
                list(GUARD_CATEGORIES),
                "The judge always rates every category. This only decides what counts as flagged when you read the "
                "results, so you can change it there without scanning again.",
            ),
            _multi(
                "verdicts",
                "Guard verdicts that count as flagged",
                list(TOXICITY_REMOVABLE_VERDICTS),
                list(TOXICITY_REMOVABLE_VERDICTS),
                "Like the categories, this is applied when you read the results. A message the judge gave no usable "
                "verdict (a guard error) always counts as flagged.",
            ),
            _integer(
                "shards",
                "Shards",
                1,
                64,
                8,
                "How the converted files are split between jobs. There are never more shards than files.",
            ),
            _integer("concurrency", "Shards at once", 1, 64, 8, "How many shards run at the same time: one GPU each."),
        ],
        # What the wrapper asks for by itself, for each shard.
        default_resources={"partition": jobs.DEFAULT_RESOURCES["partition"], "cpus": 16, "mem": "96G", "time": "48:00:00"},
        modes=None,
        default_mode=None,
        count_label="messages",
        cost=_toxicity_cost,
        write_inputs=lambda _context: None,
        build_command=toxicity_build_command,
        read_jobs=toxicity_read_jobs,
        log_files=toxicity_log_files,
        # The manifest is what lists the shards; `markers_for` adds the `_SUCCESS` of each.
        success_markers=(TOXICITY_MANIFEST,),
        read_details=toxicity_read_details,
        headline_count=toxicity_headline_count,
        markers_for=toxicity_markers,
        read_progress=toxicity_progress,
    ),
}
