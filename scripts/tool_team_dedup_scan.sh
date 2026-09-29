#!/bin/bash
# Run the MinHash/LSH near-duplicate scan (global mode) against the tool
# team's converted data.
#
# This is the dedup counterpart to scripts/tool_team_data.sh (PII) and
# scripts/tool_team_toxicity_scan.sh (toxicity). It does not convert
# anything -- point it at output/tool_team_converted (or any other JSONL
# folder) after running tool_team_data.sh's `convert` stage.
#
# Only global mode is exposed here (find duplicates within the given data).
# Cross/decontamination mode (checking training data against a separate
# benchmark set) is not offered by this wrapper -- call
# modelsafety/data_screening/dedup/run.py directly for that.
#
# Unlike the PII and toxicity scanners, this defaults to dropping the
# system prompt before comparing (--exclude-system, on by default here --
# the underlying launcher and run.py both still default it OFF). System
# messages carry tool/function definitions, not conversation content; see
# docs/data-screening/dedup.md for why that matters for this data.
#
# Usage:
#   ./scripts/tool_team_dedup_scan.sh <pilot|full|report> [--flag value ...]
#   ./scripts/tool_team_dedup_scan.sh                       # no action -- interactive menu
#
# Actions (exactly one per call):
#   pilot   A small real run over a subset of the input (--pilot-files,
#           default 50, spread evenly across every sub-dataset), to see the
#           output and measure time/memory before committing to a full run.
#   full    Every .jsonl file under --input-root, as a single job. Dedup's
#           index is global and in memory, so this can't be sharded the way
#           the toxicity scan is -- one job sees everything.
#   report  Local only, no GPU, no SLURM job, a few seconds. Reads an
#           existing run's summary.json and dedup_results.jsonl. Safe to
#           run at any time, including while a job is still on the cluster.
#
# Flags (each overrides the matching setting for this run only):
#   --input-root PATH     --run-dir PATH          --partition NAME
#   --cpus N              --mem SIZE              --time HH:MM:SS
#   --text-scope prompt|all                       --exclude-system 0|1
#   --write-filtered 0|1  --pilot-files N          --overwrite 1
#
# Resource defaults differ by action: pilot is 8 CPUs / 32G / 2 hours, full
# is 16 CPUs / 128G / 24 hours -- override with the flags above if a pilot
# run's actual memory use suggests otherwise.
#
# --overwrite is never read from the environment (it deletes --run-dir).
# Pass --overwrite 1 to replace a non-empty run dir non-interactively;
# interactively, you'll be asked to confirm instead.
#
# Examples:
#   ./scripts/tool_team_dedup_scan.sh pilot
#   ./scripts/tool_team_dedup_scan.sh full --partition toolCall
#   ./scripts/tool_team_dedup_scan.sh report --run-dir output/tool_team_dedup_pilot
#
# Precedence for every setting, lowest to highest: hardcoded default in this
# file < environment variable < CLI flag above < interactive edit (only
# offered when no action word is given at all, so scripted/automated calls
# never prompt).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=lib/tool_team_common.sh
source "${SCRIPT_DIR}/lib/tool_team_common.sh"

# ---------------------------------------------------------------------------
# Config -- edit any value below directly, or override by exporting the
# environment variable before calling this script, or with a CLI flag (see
# the usage header above). CLI flags win over environment variables.
#
# DEDUP_RUN_DIR / DEDUP_CPUS / DEDUP_MEM / DEDUP_TIME start empty on purpose
# -- their real defaults depend on which action is chosen, and are filled
# in by apply_action_defaults() once that's known.
# ---------------------------------------------------------------------------

# --- Paths -------------------------------------------------------------
DEDUP_INPUT_ROOT="${DEDUP_INPUT_ROOT:-${REPO}/output/tool_team_converted}"
DEDUP_RUN_DIR="${DEDUP_RUN_DIR:-}"

# --- SLURM resources, filled in per-action by apply_action_defaults -------
PARTITION="${PARTITION:-health}"    # main | health | toolCall | VLM -- pick_partition lists the real, live set
DEDUP_CPUS="${DEDUP_CPUS:-}"
DEDUP_MEM="${DEDUP_MEM:-}"
DEDUP_TIME="${DEDUP_TIME:-}"

# --- Scan tuning -----------------------------------------------------------
DEDUP_TEXT_SCOPE="${DEDUP_TEXT_SCOPE:-prompt}"
DEDUP_EXCLUDE_SYSTEM="${DEDUP_EXCLUDE_SYSTEM:-1}"   # on by default here, unlike the launcher
DEDUP_WRITE_FILTERED="${DEDUP_WRITE_FILTERED:-1}"
DEDUP_PILOT_FILES="${DEDUP_PILOT_FILES:-50}"        # pilot only

# --- Destructive: only ever set via --overwrite or an interactive y/N
# confirmation, never via environment variable, since it deletes --run-dir.
OVERWRITE=0

ACTION=""
ACTION_GIVEN=0

usage() {
  cat <<EOF
Usage: $(basename "$0") <pilot|full|report> [--flag value ...]
       $(basename "$0")                       # no action -- interactive menu

Actions (exactly one per call):
  pilot   A small real run over a subset of the input (--pilot-files,
          default 50, spread evenly across every sub-dataset), to see the
          output and measure time/memory before committing to a full run.
  full    Every .jsonl file under --input-root, as a single job. Dedup's
          index is global and in memory, so this can't be sharded the way
          the toxicity scan is -- one job sees everything.
  report  Local only, no GPU, no SLURM job, a few seconds. Reads an
          existing run's summary.json and dedup_results.jsonl. Safe to run
          at any time, including while a job is still on the cluster.

Flags (each overrides the matching setting for this run only):
  --input-root PATH      Recursive JSONL root (default: output/tool_team_converted)
  --run-dir PATH         Where results go (default: output/tool_team_dedup_pilot
                          for pilot, output/tool_team_dedup_run for full/report)
  --partition NAME       SLURM partition (default: health)
  --cpus N               Default: 8 for pilot, 16 for full
  --mem SIZE             Default: 32G for pilot, 128G for full
  --time HH:MM:SS        Default: 02:00:00 for pilot, 24:00:00 for full
  --text-scope prompt|all   Default: prompt (see docs/data-screening/dedup.md)
  --exclude-system 0|1   Drop system-role messages before comparing.
                          Default: 1 in this wrapper (the launcher itself
                          still defaults to 0 -- see the note at the top of
                          this file for why this wrapper's default differs).
  --write-filtered 0|1   Write filtered/ + filtered_out/ copies of every
                          row (default: 1). A full run over all of
                          output/tool_team_converted writes about 8.7 GB
                          combined -- roughly the size of the input, since
                          every row lands in exactly one of the two
                          folders. Set to 0 to only get the reports.
  --pilot-files N        Pilot only. Default: 50.
  --overwrite 1          Replace a non-empty --run-dir non-interactively.
                          Never read from the environment -- see above.

Only global mode is offered here. For cross/decontamination mode (checking
training data against a separate benchmark set), call
modelsafety/data_screening/dedup/run.py directly with --reference-input /
--candidate-input; see docs/data-screening/dedup.md.

Precedence, lowest to highest: hardcoded default in this file < environment
variable < CLI flag < interactive edit. Interactive editing is only offered
when no action word is given at all -- passing pilot, full, or report runs
fully non-interactively.

'pilot' and 'full' submit one sbatch job and return immediately -- this
script does not wait for the job to finish. 'report' never touches SLURM.

Examples:
  $(basename "$0") pilot
  $(basename "$0") full --partition toolCall
  $(basename "$0") report --run-dir ${REPO}/output/tool_team_dedup_pilot

Current settings (pilot/full-only resource and run-dir defaults are not
shown here -- see the flag descriptions above):
  DEDUP_INPUT_ROOT      ${DEDUP_INPUT_ROOT}
  PARTITION             ${PARTITION}
  DEDUP_TEXT_SCOPE      ${DEDUP_TEXT_SCOPE}
  DEDUP_EXCLUDE_SYSTEM  ${DEDUP_EXCLUDE_SYSTEM}
  DEDUP_WRITE_FILTERED  ${DEDUP_WRITE_FILTERED}
  DEDUP_PILOT_FILES     ${DEDUP_PILOT_FILES}
EOF
}

# parse_args <arg> ... -- one pass over every argument: -h/--help exits
# immediately; pilot/full/report set ACTION (only one allowed per call) and
# mark ACTION_GIVEN; anything starting with -- is a value flag (see usage
# above); anything else is an error.
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h | --help)
        usage
        exit 0
        ;;
      pilot | full | report)
        if [[ "${ACTION_GIVEN}" -eq 1 ]]; then
          echo "error: only one action allowed per call (already have '${ACTION}')" >&2
          exit 1
        fi
        ACTION="$1"
        ACTION_GIVEN=1
        shift
        continue
        ;;
    esac

    if [[ "$1" == --* ]]; then
      if [[ $# -lt 2 ]]; then
        echo "error: $1 requires a value" >&2
        exit 1
      fi
      case "$1" in
        --input-root) DEDUP_INPUT_ROOT="$2" ;;
        --run-dir) DEDUP_RUN_DIR="$2" ;;
        --partition) PARTITION="$2" ;;
        --cpus) DEDUP_CPUS="$2" ;;
        --mem) DEDUP_MEM="$2" ;;
        --time) DEDUP_TIME="$2" ;;
        --text-scope) DEDUP_TEXT_SCOPE="$2" ;;
        --exclude-system) DEDUP_EXCLUDE_SYSTEM="$2" ;;
        --write-filtered) DEDUP_WRITE_FILTERED="$2" ;;
        --pilot-files) DEDUP_PILOT_FILES="$2" ;;
        --overwrite) OVERWRITE="$2" ;;
        *)
          echo "error: unknown flag '$1'" >&2
          usage
          exit 1
          ;;
      esac
      shift 2
      continue
    fi

    echo "error: unknown argument '$1'" >&2
    usage
    exit 1
  done
}

# show_menu -- interactive numbered menu used when the script is run with no
# arguments from a real terminal. Sets ACTION the same way parse_args would.
show_menu() {
  local options=("pilot" "full" "report" "quit")
  echo "What would you like to run?"
  PS3="Enter a number (1-${#options[@]}): "
  select choice in "${options[@]}"; do
    case "${REPLY}" in
      1) ACTION="pilot"; break ;;
      2) ACTION="full"; break ;;
      3) ACTION="report"; break ;;
      4) exit 0 ;;
      *) echo "Invalid choice, pick a number 1-${#options[@]}." ;;
    esac
  done
}

# apply_action_defaults -- fills in DEDUP_RUN_DIR / DEDUP_CPUS / DEDUP_MEM /
# DEDUP_TIME with per-action defaults, but only where a hardcoded default,
# environment variable, or CLI flag hasn't already set a value (":=" only
# assigns when the variable is unset or empty).
apply_action_defaults() {
  case "${ACTION}" in
    pilot)
      : "${DEDUP_RUN_DIR:=${REPO}/output/tool_team_dedup_pilot}"
      : "${DEDUP_CPUS:=8}"
      : "${DEDUP_MEM:=32G}"
      : "${DEDUP_TIME:=02:00:00}"
      ;;
    full)
      : "${DEDUP_RUN_DIR:=${REPO}/output/tool_team_dedup_run}"
      : "${DEDUP_CPUS:=16}"
      : "${DEDUP_MEM:=128G}"
      : "${DEDUP_TIME:=24:00:00}"
      ;;
    report)
      : "${DEDUP_RUN_DIR:=${REPO}/output/tool_team_dedup_run}"
      ;;
  esac
}

# build_review_pairs -- sets the global REVIEW_PAIRS array for
# review_settings (see scripts/lib/tool_team_common.sh). Only used for
# pilot/full; report's interactive path just prompts for the run dir.
build_review_pairs() {
  REVIEW_PAIRS=(
    DEDUP_INPUT_ROOT "Input root"
    DEDUP_RUN_DIR "Run dir"
    PARTITION "Partition"
    DEDUP_CPUS "CPUs"
    DEDUP_MEM "Memory"
    DEDUP_TIME "Time limit"
    DEDUP_TEXT_SCOPE "Text scope (prompt/all)"
    DEDUP_EXCLUDE_SYSTEM "Exclude system prompt (0/1)"
    DEDUP_WRITE_FILTERED "Write filtered copies (0/1)"
  )
  if [[ "${ACTION}" == "pilot" ]]; then
    REVIEW_PAIRS+=(DEDUP_PILOT_FILES "Pilot files")
  fi
}

# validate_settings -- checked once, after any interactive edit, right
# before paths get resolved and used. Fails loudly on a bad value instead
# of letting it reach sbatch or run.py with a confusing downstream error.
validate_settings() {
  case "${DEDUP_TEXT_SCOPE}" in
    prompt | all) ;;
    *) die "--text-scope must be 'prompt' or 'all', got '${DEDUP_TEXT_SCOPE}'" ;;
  esac
  case "${DEDUP_EXCLUDE_SYSTEM}" in
    0 | 1) ;;
    *) die "--exclude-system must be 0 or 1, got '${DEDUP_EXCLUDE_SYSTEM}'" ;;
  esac
  case "${DEDUP_WRITE_FILTERED}" in
    0 | 1) ;;
    *) die "--write-filtered must be 0 or 1, got '${DEDUP_WRITE_FILTERED}'" ;;
  esac
  case "${OVERWRITE}" in
    0 | 1) ;;
    *) die "--overwrite must be 0 or 1, got '${OVERWRITE}'" ;;
  esac
  if [[ "${ACTION}" == "pilot" ]] && { ! [[ "${DEDUP_PILOT_FILES}" =~ ^[0-9]+$ ]] || [[ "${DEDUP_PILOT_FILES}" -lt 1 ]]; }; then
    die "--pilot-files must be a positive integer, got '${DEDUP_PILOT_FILES}'"
  fi
}

# ---------------------------------------------------------------------------
# Preflight, active-job guard, run-dir safety
# ---------------------------------------------------------------------------

# preflight_checks -- runs on the login node before submitting, so problems
# show up right away instead of after a queue wait.
preflight_checks() {
  if [[ ! -d "${DEDUP_INPUT_ROOT}" ]]; then
    die "input root does not exist: ${DEDUP_INPUT_ROOT} -- run './scripts/tool_team_data.sh convert' first"
  fi
  if [[ -z "$(find "${DEDUP_INPUT_ROOT}" -name '*.jsonl' -type f -print -quit)" ]]; then
    die "no .jsonl files found under ${DEDUP_INPUT_ROOT}"
  fi
  if [[ "${DEDUP_RUN_DIR}" == "${DEDUP_INPUT_ROOT}" || "${DEDUP_RUN_DIR}" == "${DEDUP_INPUT_ROOT}/"* ]]; then
    die "run dir (${DEDUP_RUN_DIR}) is inside the input root (${DEDUP_INPUT_ROOT}) -- choose a --run-dir outside --input-root"
  fi
  if [[ "${DEDUP_INPUT_ROOT}" == "${DEDUP_RUN_DIR}/"* ]]; then
    die "input root (${DEDUP_INPUT_ROOT}) is inside the run dir (${DEDUP_RUN_DIR}) -- choose a --run-dir that doesn't contain the input"
  fi
}

# check_no_active_job -- refuses to submit if a dedup job from this wrapper
# is already queued or running, so an overwrite can't clobber a live run's
# output and two scans can't collide trying to write the same run dir.
check_no_active_job() {
  local active
  active="$(squeue --me -h -n "tt_dedup_pilot,tt_dedup_full" -o '%i %j %T' 2>/dev/null || true)"
  if [[ -n "${active}" ]]; then
    echo "error: a dedup job from this wrapper is already queued or running:" >&2
    echo "${active}" >&2
    echo "Wait for it to finish (or scancel it) before submitting another." >&2
    exit 2
  fi
}

# check_run_dir -- decides whether it's safe to (re)use DEDUP_RUN_DIR.
# Missing or empty: proceed. Otherwise: non-interactive callers need
# --overwrite 1, interactive callers get a y/N confirmation. Either way,
# the actual deletion happens inside run.py via DEDUP_OVERWRITE=1 -- this
# function only decides whether that's allowed to happen.
check_run_dir() {
  if [[ ! -e "${DEDUP_RUN_DIR}" ]] || [[ -z "$(ls -A "${DEDUP_RUN_DIR}" 2>/dev/null)" ]]; then
    return
  fi

  local state="started but didn't finish"
  [[ -f "${DEDUP_RUN_DIR}/_SUCCESS" ]] && state="already finished"

  if [[ "${OVERWRITE}" -eq 1 ]]; then
    echo "Note: ${DEDUP_RUN_DIR} ${state} -- --overwrite 1 given, replacing it."
    return
  fi

  if [[ -t 0 ]]; then
    local answer
    read -r -p "${DEDUP_RUN_DIR} ${state}. Delete it and rerun? [y/N]: " answer
    if [[ "${answer}" =~ ^[Yy]$ ]]; then
      OVERWRITE=1
      return
    fi
    die "not overwriting ${DEDUP_RUN_DIR} -- nothing submitted"
  fi

  die "${DEDUP_RUN_DIR} ${state} -- pass --overwrite 1 to replace it"
}

# write_pilot_manifest <path> -- picks DEDUP_PILOT_FILES paths spread evenly
# by index across every .jsonl under DEDUP_INPUT_ROOT (relative to it) and
# writes them one per line to <path>. Even spacing by index, not random,
# keeps this deterministic and covers all three mixes and their
# sub-datasets in rough proportion to how many files each one has.
write_pilot_manifest() {
  local manifest_path="$1"
  python3 - "${DEDUP_INPUT_ROOT}" "${DEDUP_PILOT_FILES}" "${manifest_path}" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
count = int(sys.argv[2])
out_path = Path(sys.argv[3])

files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.jsonl") if p.is_file())
if not files:
    sys.exit("no .jsonl files found")

if count >= len(files):
    selected = files
else:
    step = len(files) / count
    selected = [files[int(i * step)] for i in range(count)]

out_path.write_text("\n".join(selected) + "\n", encoding="utf-8")
print(f"{len(selected)} of {len(files)} file(s) selected -> {out_path}")
PY
}

# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

# run_dedup <pilot|full> -- preflight, guard, run-dir check, then submit
# run_dedup_scan.sbatch. Always returns immediately -- dedup must go
# through sbatch, never directly on the login node.
run_dedup() {
  local action="$1"
  local manifest=""

  preflight_checks
  check_no_active_job
  check_run_dir

  # Global mode only: never let a stray inherited value flip the launcher
  # into cross/decontamination mode via --export=ALL.
  unset DEDUP_REFERENCE_ROOT DEDUP_CANDIDATE_ROOT DEDUP_INPUT_MANIFEST

  if [[ "${action}" == "pilot" ]]; then
    manifest="${DEDUP_RUN_DIR%/}.files.txt"
    write_pilot_manifest "${manifest}"
  fi

  echo "Input root:     ${DEDUP_INPUT_ROOT}"
  echo "Run dir:        ${DEDUP_RUN_DIR}"
  echo "Action:         ${action}"
  echo "Partition:      ${PARTITION}"
  echo "Resources:      ${DEDUP_CPUS} CPUs, ${DEDUP_MEM} memory, ${DEDUP_TIME} time limit"
  echo "Text scope:     ${DEDUP_TEXT_SCOPE}"
  echo "Exclude system: ${DEDUP_EXCLUDE_SYSTEM}"
  if [[ -n "${manifest}" ]]; then
    echo "File manifest:  ${manifest}"
  fi
  echo

  local export_spec
  export_spec="ALL,REPO=${REPO},DEDUP_INPUT_ROOT=${DEDUP_INPUT_ROOT},DEDUP_RUN_DIR=${DEDUP_RUN_DIR},DEDUP_TEXT_SCOPE=${DEDUP_TEXT_SCOPE},DEDUP_EXCLUDE_SYSTEM=${DEDUP_EXCLUDE_SYSTEM},DEDUP_WRITE_FILTERED=${DEDUP_WRITE_FILTERED},DEDUP_OVERWRITE=${OVERWRITE}"
  if [[ -n "${manifest}" ]]; then
    export_spec="${export_spec},DEDUP_INPUT_MANIFEST=${manifest}"
  fi

  local job_id
  job_id="$(
    sbatch --parsable --partition="${PARTITION}" --job-name="tt_dedup_${action}" \
      --cpus-per-task="${DEDUP_CPUS}" --mem="${DEDUP_MEM}" --time="${DEDUP_TIME}" \
      --output="${REPO}/logs/slurm/tool_team_dedup_${action}_%j.out" \
      --error="${REPO}/logs/slurm/tool_team_dedup_${action}_%j.err" \
      --export="${export_spec}" \
      "${REPO}/launchers/slurm/run_dedup_scan.sbatch"
  )"

  echo "Submitted dedup ${action} job: ${job_id}"
  print_job_commands "${job_id}" \
    "${REPO}/logs/scans/dedup/run_${job_id}/main/dedup_${job_id}.log" \
    "${REPO}/logs/scans/dedup/run_${job_id}/err/dedup_${job_id}.err"

  echo
  echo "Once finished:"
  echo "  $(basename "$0") report --run-dir ${DEDUP_RUN_DIR}"
}

# ---------------------------------------------------------------------------
# Report -- local only, safe at any time.
# ---------------------------------------------------------------------------

run_report() {
  if [[ ! -e "${DEDUP_RUN_DIR}" ]]; then
    die "no run found at ${DEDUP_RUN_DIR} -- nothing has been submitted yet (a pilot run defaults to ${REPO}/output/tool_team_dedup_pilot)"
  fi

  if [[ ! -f "${DEDUP_RUN_DIR}/_SUCCESS" ]]; then
    echo "Run at ${DEDUP_RUN_DIR} hasn't finished yet (no _SUCCESS marker)."
    local active
    active="$(squeue --me -h -n "tt_dedup_pilot,tt_dedup_full" -o '%i %j %T %M' 2>/dev/null || true)"
    if [[ -n "${active}" ]]; then
      echo
      echo "Active job(s):"
      echo "${active}"
    else
      echo "No tt_dedup_* job currently queued or running -- it may have failed. Check:"
      echo "  ls ${REPO}/logs/scans/dedup/"
    fi
    return 0
  fi

  python3 - "${DEDUP_RUN_DIR}" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

run_dir = Path(sys.argv[1])
summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))

if summary.get("mode") != "global":
    print(f"summary.json mode is '{summary.get('mode')}' -- this wrapper only summarizes global-mode runs.")
    print(f"Raw file: {run_dir / 'summary.json'}")
    sys.exit(0)

rows_total = summary.get("rows_total", 0)
rows_hashed = summary.get("rows_hashed", 0)
rows_without_text = summary.get("rows_without_text", 0)
duplicate_rows = summary.get("duplicate_rows", 0)
kept_rows = summary.get("kept_rows")
pct = (100 * duplicate_rows / rows_hashed) if rows_hashed else 0.0

print(f"Run:                {run_dir}")
print(f"Rows total:         {rows_total:,}")
print(f"Rows hashed:        {rows_hashed:,}")
print(f"Rows without text:  {rows_without_text:,}")
print(f"Duplicate rows:     {duplicate_rows:,} ({pct:.2f}% of hashed rows)")
if kept_rows is not None:
    print(f"Kept rows:          {kept_rows:,}")

cfg = summary.get("configuration", {})
print()
print("Configuration:")
print(f"  text_scope:      {cfg.get('text_scope')}")
print(f"  exclude_system:  {cfg.get('exclude_system', 'not recorded (run predates this flag)')}")
print(f"  ngram_size:      {cfg.get('ngram_size')}")
print(f"  lsh shape:       {cfg.get('bands')} bands x {cfg.get('rows_per_band')} rows/band")

results_path = run_dir / "dedup_results.jsonl"
if results_path.exists():
    def group_of(rel_path):
        parts = Path(rel_path).parts
        return "/".join(parts[:2]) if len(parts) >= 2 else parts[0]

    by_group = Counter()
    cross_group = 0
    with results_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            dup_group = group_of(row["source_file"])
            ref_group = group_of(row["duplicate_of"]["source_file"])
            by_group[dup_group] += 1
            if dup_group != ref_group:
                cross_group += 1

    if by_group:
        print()
        print("Top sub-datasets by duplicate rows found:")
        for group, count in by_group.most_common(10):
            print(f"  {count:7,d}  {group}")
        print()
        print(f"Duplicates pointing to a row in a DIFFERENT sub-dataset: {cross_group:,} of {duplicate_rows:,}")

print()
print(f"Duplicate pairs (side by side): {run_dir / 'matched_samples.csv'}")
print(f"Kept copy:                     {run_dir / 'filtered'}")
print(f"Removed copy:                  {run_dir / 'filtered_out'}")
PY
}

# ---------------------------------------------------------------------------

main() {
  parse_args "$@"

  if [[ "${ACTION_GIVEN}" -eq 0 ]]; then
    if [[ -t 0 ]]; then
      show_menu
      apply_action_defaults
      if [[ "${ACTION}" == "report" ]]; then
        DEDUP_RUN_DIR="$(prompt_value "Run dir" "${DEDUP_RUN_DIR}")"
      else
        pick_partition
        DEDUP_INPUT_ROOT="$(prompt_value "Input root" "${DEDUP_INPUT_ROOT}")"
        DEDUP_RUN_DIR="$(prompt_value "Run dir" "${DEDUP_RUN_DIR}")"
        build_review_pairs
        review_settings "${REVIEW_PAIRS[@]}"
      fi
    else
      usage
      exit 1
    fi
  else
    apply_action_defaults
  fi

  if [[ -z "${ACTION}" ]]; then
    usage
    exit 1
  fi

  validate_settings

  # Resolve to absolute paths before anything else, same reasoning as the
  # toxicity wrapper: run_dedup_scan.sbatch cd's into the dedup module
  # directory before it uses these, so a relative path here would silently
  # resolve against the wrong directory.
  DEDUP_INPUT_ROOT="$(realpath -m -- "${DEDUP_INPUT_ROOT}")"
  DEDUP_RUN_DIR="$(realpath -m -- "${DEDUP_RUN_DIR}")"

  case "${ACTION}" in
    pilot | full) run_dedup "${ACTION}" ;;
    report) run_report ;;
  esac
}

main "$@"
