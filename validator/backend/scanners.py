"""The kinds of scan the service can run, and what is particular to each.

A scanner is described by one `Scanner` entry in `SCANNERS`: its label, its settings (the
same shape the start forms are built from), the resources it asks for by default, and a few
functions that hold everything specific to it: the files it is given, the command that
runs it, where its logs are, what it found. The framework in scans.py (records, checking,
starting, waiting for conversions, state, replacing results, cancelling) never names a
scanner, so another scanner is another entry here.

Nothing here starts, stops or deletes anything. The only things that write are
`dedup_write_inputs`, which writes the scan's own `inputs/files.txt`, `ner_write_inputs`,
which writes its `inputs/policy.json`, and `toxicity_write_inputs`, which writes the copy of the
dataset's toxicity policy (`inputs/toxicity_policy.txt` and `.json`), all inside the scan's
folder; the other functions read a scan's output or the job scripts.

A scan's folder, as scans.py lays it out:

    <service root>/scans/<scan id>/
        scan.json   inputs/   results/   _logs/
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import jobs
import policies
import registry
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
    collision_probability_midpoint,
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

# The most dataset ids a `datasets` setting can hold: the most datasets one request can name.
MAX_SETTING_DATASETS = 50


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
class ResultContext:
    """What a scanner needs to say what one dataset's result holds: the scan's results folder,
    its settings and mode, the dataset and the names of every dataset the scan covered, the
    dataset's row of the coverage report and the scan's tally (both None until the scan's
    analysis has finished), and the count the result was saved with."""

    results: Path
    settings: dict[str, Any]
    mode: str | None
    dataset_id: str
    dataset_names: dict[str, str]
    coverage: dict[str, Any] | None
    tally: dict[str, Any] | None
    saved_count: int | None
    # Toxicity only: the categories and verdicts chosen on the result page. None: the ones the
    # scan was started with.
    categories_choice: list[str] | None = None
    verdicts_choice: list[str] | None = None


@dataclass(frozen=True)
class Scanner:
    key: str
    label: str
    description: str
    # The stages the scan goes through, as the UI names them.
    stages: tuple[str, ...]
    gpu: bool
    # The settings, in the shape step forms are built from: name, label, type, default, help.
    # The types are `choice`, `integer`, `decimal`, `multi` and `datasets` (see `_check_field`).
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
    # What one dataset's result holds, for its results page: `{flagged: {count, unit, rows},
    # breakdowns: [{title, items: [{name, count}], note}], facts: [{label, value}]}`, and
    # `filter` for a scanner whose reader can change what counts as flagged. A number the
    # scan's analysis hasn't produced yet is None.
    read_result: Callable[[ResultContext], dict[str, Any]]
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
    # Whether the scan's analysis (results.py) has to count something the summary files don't
    # hold, given the scan's mode. None when it never does.
    needs_tally: Callable[[str | None], bool] | None = None
    # For a scanner with a mode across datasets: the datasets of a scan across them, in the order
    # the scan reads them, from the settings and the chosen datasets (registry records, in the
    # order the request gave them). None: by path.
    order_across: Callable[[dict[str, Any], list[dict[str, Any]]], list[dict[str, Any]]] | None = None
    # Refuses settings that don't fit the datasets chosen: the settings, the mode that will apply
    # (a single dataset is always `within`) and the chosen dataset ids. What is wrong is added to
    # the problems.
    check_datasets: Callable[[dict[str, Any], str | None, list[str], list[str]], None] | None = None
    # The datasets a scan only reads as a reference: from the settings and the scan's mode. The
    # scan never becomes their result, and they have nothing flagged in it.
    reference_datasets: Callable[[dict[str, Any], str | None], tuple[str, ...]] | None = None
    # Whether the scan can be asked to drop what it flags by itself once it has finished and its
    # coverage check has passed (D17). It is a setting of the scan as a whole, not of the scanner.
    offers_auto_drop: bool = False


# -------------------------------------------------------------------------
# Settings and resources
# -------------------------------------------------------------------------


def _choice(
    name: str, label: str, choices: list[str], default: str, help_text: str, hints: dict[str, str] | None = None
) -> dict[str, Any]:
    """One of `choices`. `hints` is a short line for each choice (or some), shown next to it."""
    field: dict[str, Any] = {"name": name, "label": label, "type": "choice", "choices": choices, "default": default, "help": help_text}
    if hints is not None:
        field["hints"] = hints
    return field


def _datasets(name: str, label: str, help_text: str) -> dict[str, Any]:
    """A list of ids of the datasets chosen for the scan (which ones is checked against the request
    by scans.py). Empty by default."""
    return {"name": name, "label": label, "type": "datasets", "default": [], "help": help_text}


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
    if kind == "datasets":
        # Each id goes into a comparison with the request's datasets, and later into a command, so
        # only the exact id format is let through. The order is kept: it can mean something.
        if value is None:
            return []
        if (
            not isinstance(value, list)
            or len(value) > MAX_SETTING_DATASETS
            or not all(isinstance(item, str) and registry.DATASET_ID.fullmatch(item) for item in value)
        ):
            problems.append(f"{label} must be a list of at most {MAX_SETTING_DATASETS} dataset ids.")
            return []
        if len(set(value)) != len(value):
            problems.append(f"{label} names a dataset twice.")
            return []
        return list(value)
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
        "offers_auto_drop": scanner.offers_auto_drop,
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

# D16: how strict a match must be. Each preset is the LSH's bands and rows, with 256 permutations
# and 60-character n-grams for all of them (bands x rows is always 256). More bands of fewer rows
# flag less similar rows. These are starting points, to be checked on real data.
DEDUP_NGRAM_SIZE = 60
DEDUP_NUM_PERM = 256
DEDUP_PRESETS = {"strict": (16, 16), "standard": (32, 8), "loose": (64, 4)}
DEDUP_DEFAULT_STRICTNESS = "standard"

# D15: which copy of a group of duplicates counts as the original. The scan keeps the row it reads
# first, so what changes is the order the datasets are listed in.
ORIGINAL_PATH = "path"
ORIGINAL_PRIORITY = "priority"
ORIGINAL_PROTECTED = "protected"


def dedup_preset_midpoint(strictness: str) -> float:
    """The similarity at which a pair of rows has a 50% chance of being flagged under a preset:
    the scan's own formula, so what the dialog says and what the scan records can't differ."""
    bands, rows = DEDUP_PRESETS[strictness]
    return collision_probability_midpoint(bands, rows)


def _strictness_hints() -> dict[str, str]:
    return {name: f"flagged from about {round(dedup_preset_midpoint(name) * 100)}% similarity" for name in DEDUP_PRESETS}


def dedup_order_across(settings: dict[str, Any], datasets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The datasets of a scan across them, in the order the scan reads them (D15). By path (the
    default): the dataset first by path holds the copy that counts as the original. Priority: the
    order the user gave. Protected: the protected datasets first, in the order given, then the
    others by path. Rows are numbered in this order and the earliest row of a group of duplicates
    is the one kept."""
    by_path = sorted(datasets, key=lambda item: (item["path"], item["id"]))
    original = settings.get("original", ORIGINAL_PATH)
    if original == ORIGINAL_PRIORITY:
        return list(datasets)
    if original == ORIGINAL_PROTECTED:
        protected = set(settings.get("protected") or [])
        return [item for item in datasets if item["id"] in protected] + [item for item in by_path if item["id"] not in protected]
    return by_path


def dedup_reference_datasets(settings: dict[str, Any], mode: str | None) -> tuple[str, ...]:
    """The protected datasets of a scan across datasets: their rows are read, so that rows in the
    others can match them, but nothing in them is flagged."""
    if mode == MODE_ACROSS and settings.get("original") == ORIGINAL_PROTECTED:
        return tuple(settings.get("protected") or [])
    return ()


def dedup_check_datasets(settings: dict[str, Any], mode: str | None, dataset_ids: list[str], problems: list[str]) -> None:
    """The original-copy choice has to fit the datasets: it only means something when rows are
    compared across two or more datasets, and at least one dataset must stay unprotected."""
    original = settings.get("original", ORIGINAL_PATH)
    protected = settings.get("protected") or []
    if mode != MODE_ACROSS:
        if original != ORIGINAL_PATH or protected:
            problems.append(
                "Which copy counts as the original only applies when rows are compared across two or more datasets."
            )
        return
    if original == ORIGINAL_PROTECTED:
        if not protected:
            problems.append("Choose at least one dataset to protect, or choose another way to pick the original copy.")
        elif len(set(protected)) >= len(set(dataset_ids)):
            problems.append("At least one dataset has to stay unprotected, or the scan would flag nothing.")
    elif protected:
        problems.append("Datasets can only be protected when 'Never flag rows in these datasets' is chosen.")


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

    # A scan saved before there was a choice of strictness was made with the standard one.
    bands, rows = DEDUP_PRESETS[context.settings.get("strictness", DEDUP_DEFAULT_STRICTNESS)]
    variables = {
        "REPO": str(REPO_ROOT),
        "DEDUP_INPUT_ROOT": str(input_root),
        "DEDUP_INPUT_MODE": "runs",
        "DEDUP_RUN_DIR": str(results),
        "DEDUP_LOG_ROOT": str(logs / "dedup"),
        "DEDUP_TEXT_SCOPE": context.settings["text_scope"],
        "DEDUP_EXCLUDE_SYSTEM": context.settings["exclude_system"],
        "DEDUP_NGRAM_SIZE": str(DEDUP_NGRAM_SIZE),
        "DEDUP_NUM_PERM": str(DEDUP_NUM_PERM),
        "DEDUP_BANDS": str(bands),
        "DEDUP_ROWS_PER_BAND": str(rows),
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


# The full scan's job asks for 8 GPUs (`run_privacy_filter_node.sbatch`) and starts a worker on
# each, every one with its own `worker_<n>.log`. One that isn't there is left out of the page.
NER_FULL_GPU_COUNT = 8


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
        for index in range(NER_FULL_GPU_COUNT):
            files.append((f"Worker on GPU {index}", folder / RESULTS_FOLDER / "worker_logs" / f"worker_{index}.log"))
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
# shard, which screens it with Detoxify, judges what was flagged with Qwen3Guard and then has
# Granite Guardian judge, against the dataset's toxicity policy, what Qwen3Guard rated Unsafe or
# Controversial.
TOXICITY_WRAPPER = REPO_ROOT / "scripts" / "tool_team_toxicity_scan.sh"
TOXICITY_ARRAY_SCRIPT = "run_toxicity_scan_array.sbatch"
TOXICITY_MANIFEST = "manifests/manifest.json"
# Where the array job writes each shard (as modelsafety/results/coverage.py reads them).
TOXICITY_SHARDS_FOLDER = "scans/toxicity/shards"
DETOXIFY_SUCCESS_MARKER = "_DETOXIFY_SUCCESS"
GUARD_SUCCESS_MARKER = "_GUARD_SUCCESS"
POLICY_SUCCESS_MARKER = "_POLICY_SUCCESS"
# What the policy judge writes in a shard (policy_judge.py's summary file).
POLICY_SUMMARY_FILE = "policy_results/summary.json"
# The copy of the dataset's toxicity policy a scan is started with, in the scan's `inputs/`: the
# text the job reads, and a record of where it came from.
TOXICITY_POLICY_TEXT = "toxicity_policy.txt"
TOXICITY_POLICY_INFO = "toxicity_policy.json"
TOXICITY_STAGES = (
    "Quick screen: Detoxify (Unitary, multilingual model)",
    "Judge: Qwen3Guard-Gen-8B",
    "Policy judge: Granite Guardian 4.1 8B",
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


def toxicity_write_inputs(context: ScanContext) -> None:
    """Copy the policy the scan's dataset uses (its own, or its team's) into the scan's folder:
    `inputs/toxicity_policy.txt`, which the policy judge reads, and `inputs/toxicity_policy.json`,
    which says where it came from and holds its SHA-256, for the result page. The scan keeps this
    copy: a policy changed later never changes a scan that has started."""
    (dataset_id,) = context.version_folders
    try:
        policy = policies.dataset_policy(dataset_id)
    except registry.RegistryCorrupt as exc:
        raise ScannerError(f"The toxicity policy of this dataset can't be read: {exc}") from exc
    # The job hashes this file as it is, so the hash is of exactly what is written.
    text = policy["text"] + "\n"
    info = {
        "source": policy["source"],
        "team": policy["team"],
        "text": policy["text"],
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    inputs = context.folder / INPUTS_FOLDER
    inputs.mkdir(parents=True, exist_ok=True)
    for name, content in ((TOXICITY_POLICY_TEXT, text), (TOXICITY_POLICY_INFO, json.dumps(info, indent=2) + "\n")):
        temp = inputs / f"{name}.tmp"
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, inputs / name)


def toxicity_build_command(context: ScanContext) -> list[str]:
    """`env TOX_KILL_ON_INVALID_DEP=1 bash scripts/tool_team_toxicity_scan.sh all ...`, which
    submits the job that plans the shards and, behind it, the array job. Every setting is a
    flag, after the variables of the shell the service runs in were removed, so the command
    shown is the command that runs. The categories and verdicts aren't in it: the scan always
    judges every category, and they only decide what counts as flagged when results are read
    (D8). The policy is: `--policy-file` is the copy `toxicity_write_inputs` made."""
    _toxicity_check_shards(context.settings)
    (input_root,) = context.version_folders.values()
    results = context.folder / RESULTS_FOLDER
    policy_file = context.folder / INPUTS_FOLDER / TOXICITY_POLICY_TEXT
    # The wrapper passes these on in an `--export` list, which is separated by commas.
    _check_export_values(
        {"TOX_INPUT_ROOT": str(input_root), "TOX_RUN_DIR": str(results), "TOX_POLICY_FILE": str(policy_file)}
    )
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
        "--policy-file",
        str(policy_file),
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
    # the first one's are shown, or the one in the job's "task" (an array task is a shard index)
    # when that is given: the task a failed scan failed in. The policy judge's server comes before
    # the model server's, so a failure looks in the later stage's log first.
    slurm = REPO_ROOT / "logs" / "slurm"
    files: list[tuple[str, Path]] = []
    for job in jobs_started:
        if job["role"] == PLAN_JOB_ROLE:
            files.append(("Planning output", slurm / f"tool_team_toxicity_prep_{job['job_id']}.out"))
            files.append(("Planning errors", slurm / f"tool_team_toxicity_prep_{job['job_id']}.err"))
        elif job["role"] == MAIN_JOB_ROLE:
            task = job.get("task")
            shard = int(task) if isinstance(task, str) and task.isdigit() else 0
            shard_folder = folder / RESULTS_FOLDER / TOXICITY_SHARDS_FOLDER / f"shard_{shard:05d}"
            files.append((f"Shard {shard} output", slurm / f"tool_team_toxicity_all_{job['job_id']}_{shard}.out"))
            files.append((f"Shard {shard} errors", slurm / f"tool_team_toxicity_all_{job['job_id']}_{shard}.err"))
            files.append((f"Shard {shard} policy judge server", shard_folder / "vllm_policy.log"))
            files.append((f"Shard {shard} model server", shard_folder / "vllm.log"))
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
    """Shards screened, shards judged and shards the policy judge has finished, out of all of
    them. The shards' folder is listed and only the folders in it are looked into, not each
    shard's by name: asking for one before a task has made it would be remembered as "not
    there" for about a minute. (The listing of the shards' folder itself can still hit that for
    a poll or two when the first task has only just started; then there is simply no progress
    to show yet.) The policy stage is only listed for a scan that was started with a policy."""
    names = _toxicity_shard_names(results)
    if not names:
        return None
    shards_root = results / TOXICITY_SHARDS_FOLDER
    screened = 0
    judged = 0
    policy_judged = 0
    try:
        with os.scandir(shards_root) as entries:
            present = {entry.name for entry in entries if entry.is_dir()}
        for name in names:
            if name not in present:
                continue
            with os.scandir(shards_root / name) as files:
                made = {entry.name for entry in files}
            screened += DETOXIFY_SUCCESS_MARKER in made
            policy_judged += POLICY_SUCCESS_MARKER in made
            # `_SUCCESS` comes after the policy stage, so Qwen3Guard's own marker is what says it is
            # done; a shard from before that marker existed only has `_SUCCESS`.
            judged += GUARD_SUCCESS_MARKER in made or SUCCESS_MARKER in made
    except OSError:
        return None
    progress = [
        {"stage": TOXICITY_STAGES[0], "done": screened, "total": len(names), "unit": "shards"},
        {"stage": TOXICITY_STAGES[1], "done": judged, "total": len(names), "unit": "shards"},
    ]
    if (results.parent / INPUTS_FOLDER / TOXICITY_POLICY_TEXT).is_file():
        progress.append({"stage": TOXICITY_STAGES[2], "done": policy_judged, "total": len(names), "unit": "shards"})
    return progress


def _add_counts(into: dict[str, int], counts: Any) -> None:
    if isinstance(counts, dict):
        for name, count in counts.items():
            if isinstance(count, int) and not isinstance(count, bool):
                into[name] = into.get(name, 0) + count


def toxicity_read_details(results: Path) -> dict[str, Any]:
    """What the shards found, added up: from each shard's `detoxify_summary.json`,
    `guard_results/summary.json` and, for a scan that had a policy, `policy_results/summary.json`."""
    names = _toxicity_shard_names(results)
    if names is None:
        return {}
    totals = {"rows_read": 0, "messages_scored": 0, "messages_flagged": 0, "guard_records": 0, "guard_errors": 0, "guard_unknown": 0}
    # What the policy judge did (policy_judge.py's summary keys, with the prefix `policy_`).
    policy_totals = {"policy_judged": 0, "policy_kept": 0, "policy_not_general": 0, "policy_review": 0, "policy_errors": 0}
    verdicts: dict[str, int] = {}
    categories: dict[str, int] = {}
    judged = 0
    policy_shards = 0
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
        policy = read_json_or_none(shard / POLICY_SUMMARY_FILE)
        if isinstance(policy, dict):
            policy_shards += 1
            for key in policy_totals:
                policy_totals[key] += _whole_number(policy.get(key.removeprefix("policy_"))) or 0
    details: dict[str, Any] = {
        "shards": len(names),
        "shards_judged": judged,
        **totals,
        "verdicts": _sorted_counts(verdicts),
        "categories": _sorted_counts(categories),
    }
    # Only for a scan that had a policy stage, so the page of an older scan doesn't show zeros.
    if policy_shards:
        details["shards_policy_judged"] = policy_shards
        details.update(policy_totals)
    return details


def _toxicity_policy_copy(results: Path) -> dict[str, str] | None:
    """The policy the scan was started with, `{source, team, text, sha256}`, from the copy in its
    `inputs/`. None for a scan that has none (made before there was a policy judge)."""
    info = read_json_or_none(results.parent / INPUTS_FOLDER / TOXICITY_POLICY_INFO)
    if not isinstance(info, dict):
        return None
    copy = {key: info.get(key) for key in ("source", "team", "text", "sha256")}
    if not all(isinstance(value, str) for value in copy.values()):
        return None
    return copy  # type: ignore[return-value]


def toxicity_headline_count(results: Path, _mode: str | None, _dataset_id: str, _version_id: str) -> int | None:
    """None: the messages that count as flagged depend on the categories and verdicts the scan
    was started with (D8), and the summaries don't hold verdicts by category. The count is
    set by results.py once the scan's analysis has tallied the judged messages, so the chip
    never shows a number that later changes."""
    return None


# -------------------------------------------------------------------------
# What one dataset's result holds (read_result)
# -------------------------------------------------------------------------


def _flagged(count: int | None, unit: str, rows: int | None = None) -> dict[str, Any]:
    return {"count": count, "unit": unit, "rows": rows}


def _breakdown(title: str, counts: dict[str, int] | None, note: str | None = None) -> dict[str, Any] | None:
    """A table of name and count, biggest first; None when the counts aren't known."""
    if counts is None:
        return None
    return {"title": title, "items": [{"name": name, "count": count} for name, count in counts.items()], "note": note}


def _known(*breakdowns: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [item for item in breakdowns if item is not None]


def _strictness_note(context: ResultContext) -> dict[str, str]:
    """The preset the scan was started with, and what it recorded of the LSH it used and the
    similarity that LSH flags from (the scan's `summary.json`)."""
    strictness = context.settings.get("strictness", DEDUP_DEFAULT_STRICTNESS)
    name = strictness.capitalize() if strictness in DEDUP_PRESETS else str(strictness)
    summary = read_json_or_none(context.results / "summary.json")
    configuration = summary.get("configuration") if isinstance(summary, dict) else None
    configuration = configuration if isinstance(configuration, dict) else {}
    bands, rows = _whole_number(configuration.get("bands")), _whole_number(configuration.get("rows_per_band"))
    midpoint = configuration.get("collision_probability_midpoint_similarity")
    if bands is None or rows is None or not isinstance(midpoint, float):
        return {"label": "Match strictness", "text": f"{name} (what the scan recorded isn't available)."}
    return {
        "label": "Match strictness",
        "text": f"{name}: {bands} bands of {rows} rows. A pair of rows has a 50% chance of being flagged at {midpoint * 100:.1f}% similarity (recorded by the scan).",
    }


def _original_note(context: ResultContext) -> dict[str, str] | None:
    """Which copy counted as the original, for a scan across datasets (D15). `dataset_names` is
    in the order the scan read the datasets."""
    if context.mode != MODE_ACROSS or len(context.dataset_names) < 2:
        return None
    original = context.settings.get("original", ORIGINAL_PATH)
    if original == ORIGINAL_PRIORITY:
        text = "priority: " + ", then ".join(context.dataset_names.values())
    elif original == ORIGINAL_PROTECTED:
        protected = context.settings.get("protected") or []
        text = "protected: " + ", ".join(context.dataset_names.get(dataset_id, dataset_id) for dataset_id in protected)
    else:
        text = "first by path"
    return {"label": "Original copy", "text": text}


def _text_scope_note(context: ResultContext) -> dict[str, str]:
    """Which part of each row the scan compared. A scan saved without these settings was made
    with the dialog's defaults: the prompt only, and system messages left out (as flagged.py
    assumes when it compares a row with its kept copy)."""
    scope = context.settings.get("text_scope")
    scope = scope if scope in ("prompt", "all") else "prompt"
    left_out = context.settings.get("exclude_system", "1") in ("1", 1, True)
    if scope == "prompt":
        text = "The prompt only (a final assistant reply is left out)"
    else:
        text = "Every message"
    text += "; system messages left out." if left_out else "; system messages included."
    return {"label": "Text compared", "text": text}


def dedup_read_result(context: ResultContext) -> dict[str, Any]:
    """The duplicate rows in this dataset (the coverage report's `findings` for its files). For
    a scan across datasets, also where the copy that was kept is: in this dataset or another."""
    findings = _whole_number(context.coverage.get("findings")) if context.coverage is not None else None
    count = findings if findings is not None else context.saved_count
    breakdowns: list[dict[str, Any] | None] = []
    if context.mode == MODE_ACROSS and len(context.dataset_names) > 1 and context.tally is not None:
        groups = context.tally.get("groups")
        mine = groups.get(context.dataset_id) if isinstance(groups, dict) else None
        kept_in = _sorted_counts(mine.get("kept_in")) if isinstance(mine, dict) else None
        if kept_in is not None:
            where = {
                (
                    "This dataset"
                    if dataset_id == context.dataset_id
                    else context.dataset_names.get(dataset_id, "A file outside the chosen datasets")
                ): number
                for dataset_id, number in kept_in.items()
            }
            breakdowns.append(
                _breakdown(
                    "Where the kept copy of each duplicate is",
                    where,
                    "Rows in this dataset that repeat a row kept in the dataset named.",
                )
            )
    notes = [
        item for item in (_original_note(context), _text_scope_note(context), _strictness_note(context)) if item is not None
    ]
    return {"flagged": _flagged(count, "duplicate rows", count), "breakdowns": _known(*breakdowns), "facts": [], "notes": notes}


def regex_read_result(context: ResultContext) -> dict[str, Any]:
    details = regex_read_details(context.results)
    kept = _whole_number(details.get("kept_hits"))
    raw = _whole_number(details.get("raw_hits"))
    facts = [] if raw is None else [{"label": "Matches before the checks that drop false ones", "value": raw}]
    return {
        "flagged": _flagged(kept if kept is not None else context.saved_count, "PII findings", _whole_number(details.get("rows_with_hits"))),
        "breakdowns": _known(_breakdown("Categories (matches kept)", _sorted_counts(details.get("kept_by_category")))),
        "facts": facts,
    }


def ner_read_result(context: ResultContext) -> dict[str, Any]:
    """The spans the policy counts, from the tally (which also has the rows) once there is one;
    until then, the scan's own total."""
    details = ner_read_details(context.results)
    tally = context.tally
    eligible = _whole_number(tally.get("eligible_spans")) if tally is not None else _whole_number(details.get("policy_eligible_spans"))
    found = _whole_number(details.get("spans"))
    facts = [] if found is None else [{"label": "Spans the model found", "value": found}]
    return {
        "flagged": _flagged(
            eligible if eligible is not None else context.saved_count,
            "PII findings",
            _whole_number(tally.get("rows_flagged")) if tally is not None else None,
        ),
        "breakdowns": _known(
            _breakdown("Labels (spans the policy counts)", _sorted_counts(tally.get("eligible_by_label")) if tally is not None else None),
            _breakdown("Groups of labels", _sorted_counts(tally.get("eligible_by_tier")) if tally is not None else None),
        ),
        "facts": facts,
    }


def _chosen(value: Any, allowed: tuple[str, ...]) -> list[str]:
    """The items of `allowed` that `value` (a list) names, in the order of `allowed`. Anything
    that isn't a list is every item: the scan was started with the defaults."""
    if not isinstance(value, list):
        return list(allowed)
    return [item for item in allowed if item in value]


def _atom_counts(atom: str, verdicts: set[str], categories: set[str]) -> bool:
    """Whether one of a row's tally atoms ("error", or "<verdict>|<category>") counts as
    flagged. A guard error always does (D9). So does a verdict with no category: it has no
    category to untick. So does a category this service doesn't know."""
    if atom == "error":
        return True
    verdict, _separator, category = atom.partition("|")
    return verdict in verdicts and (category == "" or category in categories or category not in GUARD_CATEGORIES)


def toxicity_flagged(tally: dict[str, Any], verdicts: list[str], categories: list[str]) -> dict[str, Any]:
    """What counts as flagged under these verdicts and categories, from the toxicity tally: the
    messages, the rows, and the messages by verdict and by category. A message can have several
    categories, so the categories add up to more than the messages."""
    chosen_verdicts, chosen_categories = set(verdicts), set(categories)
    errors = _whole_number(tally.get("guard_errors")) or 0
    messages = errors
    by_verdict: dict[str, int] = {}
    by_category: dict[str, int] = {}
    without_category = 0
    entries = tally.get("messages")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("categories"), list):
            continue
        verdict, count = entry.get("verdict"), _whole_number(entry.get("count"))
        if not isinstance(verdict, str) or count is None or verdict not in chosen_verdicts:
            continue
        named = [category for category in entry["categories"] if isinstance(category, str)]
        counted = [category for category in named if category in chosen_categories or category not in GUARD_CATEGORIES]
        if named and not counted:
            continue
        messages += count
        by_verdict[verdict] = by_verdict.get(verdict, 0) + count
        if not named:
            without_category += count
        for category in counted:
            by_category[category] = by_category.get(category, 0) + count
    rows = 0
    row_entries = tally.get("rows")
    for entry in row_entries if isinstance(row_entries, list) else []:
        atoms = entry.get("atoms") if isinstance(entry, dict) else None
        count = _whole_number(entry.get("count")) if isinstance(entry, dict) else None
        if isinstance(atoms, list) and count is not None and any(
            isinstance(atom, str) and _atom_counts(atom, chosen_verdicts, chosen_categories) for atom in atoms
        ):
            rows += count
    return {
        "messages": messages,
        "rows": rows,
        "guard_errors": errors,
        "by_verdict": by_verdict,
        "by_category": by_category,
        "without_category": without_category,
    }


def toxicity_read_result(context: ResultContext) -> dict[str, Any]:
    """The messages that count as flagged under the scan's saved categories and verdicts (D8), or
    the ones the user chose on the page, which only changes what is shown. The Detoxify
    threshold is not among them: it decided what the judge saw."""
    saved_categories = _chosen(context.settings.get("categories"), GUARD_CATEGORIES)
    saved_verdicts = _chosen(context.settings.get("verdicts"), TOXICITY_REMOVABLE_VERDICTS)
    categories = saved_categories if context.categories_choice is None else context.categories_choice
    verdicts = saved_verdicts if context.verdicts_choice is None else context.verdicts_choice
    details = toxicity_read_details(context.results)
    facts = [
        {"label": label, "value": details[key]}
        for label, key in (
            ("Messages Detoxify scored", "messages_scored"),
            ("Messages sent to the judge", "messages_flagged"),
            ("Messages the judge rated", "guard_records"),
        )
        if _whole_number(details.get(key)) is not None
    ]
    # What the policy judge did, for a scan that had one. A message it kept is not counted below;
    # one it gave no usable answer for (an error, or no yes or no) still is.
    if _whole_number(details.get("policy_judged")) is not None:
        facts.append({"label": "Messages sent to the policy judge", "value": details["policy_judged"]})
        facts.append({"label": "Messages the policy judge kept", "value": details["policy_kept"]})
        no_answer = details["policy_review"] + details["policy_errors"]
        if no_answer:
            facts.append({"label": "Policy judge errors (count as flagged)", "value": no_answer})
    flagged = _flagged(None, "messages")
    breakdowns: list[dict[str, Any] | None] = []
    if context.tally is not None:
        found = toxicity_flagged(context.tally, verdicts, categories)
        flagged = _flagged(found["messages"], "messages", found["rows"])
        extra = {}
        if found["guard_errors"]:
            extra["Guard error (counts as flagged)"] = found["guard_errors"]
        breakdowns = [
            _breakdown("Verdicts", {**_sorted_counts(found["by_verdict"]), **extra}),
            _breakdown(
                "Categories",
                {
                    **_sorted_counts(found["by_category"]),
                    **({"No category given": found["without_category"]} if found["without_category"] else {}),
                },
                "A message can have several categories, so these add up to more than the flagged messages.",
            ),
        ]
    return {
        "flagged": flagged,
        "breakdowns": _known(*breakdowns),
        "facts": facts,
        "filter": {
            "categories": list(GUARD_CATEGORIES),
            "verdicts": list(TOXICITY_REMOVABLE_VERDICTS),
            "selected_categories": categories,
            "selected_verdicts": verdicts,
            "saved_categories": saved_categories,
            "saved_verdicts": saved_verdicts,
            "threshold": context.settings.get("threshold"),
        },
        # The policy the scan was started with, or None for a scan made before there was one.
        "policy": _toxicity_policy_copy(context.results),
    }


SCANNERS: dict[str, Scanner] = {
    "dedup": Scanner(
        key="dedup",
        label="Duplicates",
        description="Finds rows that repeat, exactly or nearly, inside one dataset or across several.",
        stages=("Scan",),
        gpu=False,
        fields=[
            _choice(
                "strictness",
                "Match strictness",
                list(DEDUP_PRESETS),
                DEDUP_DEFAULT_STRICTNESS,
                "How alike two rows must be to count as duplicates. Strict finds fewer, Loose finds more. "
                "The percentage is the similarity at which a pair has a 50% chance of being flagged.",
                _strictness_hints(),
            ),
            _choice(
                "original",
                "Which copy counts as the original",
                [ORIGINAL_PATH, ORIGINAL_PRIORITY, ORIGINAL_PROTECTED],
                ORIGINAL_PATH,
                "Only when comparing across two or more datasets. The copy that counts as the original is kept; "
                "the other copies are flagged.",
                {
                    ORIGINAL_PATH: "The dataset that comes first by path holds the original.",
                    ORIGINAL_PRIORITY: "You order the datasets: a row in a higher one counts as the original.",
                    ORIGINAL_PROTECTED: "Nothing is flagged in the datasets you tick, but rows in the others that repeat theirs are.",
                },
            ),
            _datasets(
                "protected",
                "Datasets to never flag rows in",
                "Used with 'Never flag rows in these datasets'. Their rows are still compared, so rows in the "
                "other datasets that repeat them are flagged.",
            ),
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
        read_result=dedup_read_result,
        needs_tally=lambda mode: mode == MODE_ACROSS,
        order_across=dedup_order_across,
        check_datasets=dedup_check_datasets,
        reference_datasets=dedup_reference_datasets,
        offers_auto_drop=True,
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
        read_result=ner_read_result,
        needs_tally=lambda _mode: True,
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
        read_result=regex_read_result,
        detects=tuple(PII_CATEGORIES),
    ),
    "toxicity": Scanner(
        key="toxicity",
        label="Toxicity",
        description=(
            "Finds toxic messages in three stages: a fast model (Detoxify) screens every message, "
            "Qwen3Guard judges the ones it flagged, and Granite Guardian re-judges the ones Qwen3Guard "
            "rated Unsafe or Controversial against the dataset's toxicity policy. A message only counts as "
            "flagged when the policy doesn't say it is suitable. It needs GPUs."
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
        write_inputs=toxicity_write_inputs,
        build_command=toxicity_build_command,
        read_jobs=toxicity_read_jobs,
        log_files=toxicity_log_files,
        # The manifest is what lists the shards; `markers_for` adds the `_SUCCESS` of each.
        success_markers=(TOXICITY_MANIFEST,),
        read_details=toxicity_read_details,
        headline_count=toxicity_headline_count,
        read_result=toxicity_read_result,
        needs_tally=lambda _mode: True,
        markers_for=toxicity_markers,
        read_progress=toxicity_progress,
    ),
}
