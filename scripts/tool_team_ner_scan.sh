#!/bin/bash
# Run the NER-model (Nemotron privacy filter) PII scan against the tool
# team's converted data.
#
# This is a separate script from tool_team_data.sh on purpose: it needs
# GPUs, a gated Hugging Face model, and can take anywhere from a few
# minutes (pilot) to a week (full) -- a fundamentally different commitment
# from the CPU-only regex scan in `tool_team_data.sh scan`.
#
# Usage:
#   ./scripts/tool_team_ner_scan.sh <pilot|full> [--flag value ...]
#   ./scripts/tool_team_ner_scan.sh                 # no action -- interactive menu
#
# Flags (each overrides the matching setting below, for this run only):
#   --input-root PATH   --run-root PATH   --partition NAME
#   --input-mode runs|folder
#
# --run-root defaults to output/tool_team_ner_pilot for pilot and
# output/tool_team_ner_run for full, so a pilot never shares a folder with the
# full scan that follows it.
#
# --input-mode decides which files are scanned under --input-root. The
# default, 'runs', reads only the files of finished conversions and writes
# <run-root>/manifest/scan_inputs.json listing what it skipped (an unfinished
# conversion, a stray .jsonl). 'folder' reads every .jsonl under it.
#
# Examples:
#   ./scripts/tool_team_ner_scan.sh pilot
#   ./scripts/tool_team_ner_scan.sh full --partition toolCall
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
# Config -- what to scan, where results go, and which SLURM partition to
# submit to. Every partition on this cluster carries the same 8x H100 GPUs
# (confirmed with `sinfo`), so changing this is safe -- it's a scheduling
# choice, not a resource-compatibility one.
# ---------------------------------------------------------------------------
PRIVACY_FILTER_INPUT_ROOT="${PRIVACY_FILTER_INPUT_ROOT:-${REPO}/output/prepared/tools_team}"
# Empty on purpose -- the real default depends on the action, and is filled in
# by apply_action_defaults() once that's known.
PRIVACY_FILTER_RUN_ROOT="${PRIVACY_FILTER_RUN_ROOT:-}"
PRIVACY_FILTER_INPUT_MODE="${PRIVACY_FILTER_INPUT_MODE:-runs}"   # runs = files of finished conversions only; folder = every .jsonl
PARTITION="${PARTITION:-background}"

REVIEW_PAIRS=(
  PRIVACY_FILTER_INPUT_ROOT "Input root"
  PRIVACY_FILTER_RUN_ROOT "Run root"
  PRIVACY_FILTER_INPUT_MODE "Input mode (runs|folder)"
  PARTITION "Partition"
)

ACTION=""
STAGE_GIVEN=0

usage() {
  cat <<EOF
Usage: $(basename "$0") <pilot|full> [--flag value ...]
       $(basename "$0")                 # no action -- interactive menu

Actions:
  pilot   1 GPU, 1 source file, a few minutes. Also runs a parity check
          against a reference decoder before scanning. Run this first.
  full    All 8 GPUs on one node, every file under
          \$PRIVACY_FILTER_INPUT_ROOT, up to 7 days.

Flags (each overrides the matching setting for this run only):
  --input-root PATH   --run-root PATH   --partition NAME
  --input-mode runs|folder

--run-root defaults to output/tool_team_ner_pilot for pilot and
output/tool_team_ner_run for full. A run folder remembers which mode and
settings it was started with, and refuses a different one.

--input-mode decides which files are scanned under --input-root. 'runs' (the
default) reads only the files of finished conversions and lists everything it
skipped, with the reason, in <run-root>/manifest/scan_inputs.json. 'folder'
reads every .jsonl under --input-root.

Precedence, lowest to highest: hardcoded default in this file < environment
variable < CLI flag < interactive edit. Interactive editing is only offered
when no action word is given at all -- passing pilot or full runs fully
non-interactively.

Both actions submit sbatch job(s) and return immediately -- this script
does not wait for the job to finish.

Examples:
  $(basename "$0") pilot
  $(basename "$0") full --partition toolCall

Current settings:
  PRIVACY_FILTER_INPUT_ROOT ${PRIVACY_FILTER_INPUT_ROOT}
  PRIVACY_FILTER_RUN_ROOT   ${PRIVACY_FILTER_RUN_ROOT:-(set per action, see above)}
  PRIVACY_FILTER_INPUT_MODE ${PRIVACY_FILTER_INPUT_MODE}
  PARTITION                 ${PARTITION}
EOF
}

# apply_action_defaults -- fills in PRIVACY_FILTER_RUN_ROOT where a hardcoded
# default, environment variable, or CLI flag hasn't already set it (":="
# only assigns when the variable is unset or empty).
apply_action_defaults() {
  case "${ACTION}" in
    pilot) : "${PRIVACY_FILTER_RUN_ROOT:=${REPO}/output/tool_team_ner_pilot}" ;;
    full) : "${PRIVACY_FILTER_RUN_ROOT:=${REPO}/output/tool_team_ner_run}" ;;
  esac
}

# parse_args <arg> ... -- one pass over every argument: -h/--help exits
# immediately; pilot/full set ACTION and mark STAGE_GIVEN; anything
# starting with -- is a value flag (see usage above); anything else is an
# error.
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h | --help)
        usage
        exit 0
        ;;
      pilot | full)
        ACTION="$1"
        STAGE_GIVEN=1
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
        --input-root) PRIVACY_FILTER_INPUT_ROOT="$2" ;;
        --run-root) PRIVACY_FILTER_RUN_ROOT="$2" ;;
        --input-mode) PRIVACY_FILTER_INPUT_MODE="$2" ;;
        --partition) PARTITION="$2" ;;
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
  local options=("pilot" "full" "quit")
  echo "What would you like to run?"
  PS3="Enter a number (1-3): "
  select choice in "${options[@]}"; do
    case "${REPLY}" in
      1) ACTION="pilot"; break ;;
      2) ACTION="full"; break ;;
      3) exit 0 ;;
      *) echo "Invalid choice, pick a number 1-3." ;;
    esac
  done
}

preflight_checks() {
  if [[ ! -d "${PRIVACY_FILTER_INPUT_ROOT}" ]]; then
    echo "error: ${PRIVACY_FILTER_INPUT_ROOT} does not exist -- run './scripts/tool_team_data.sh convert' first" >&2
    exit 2
  fi
  if [[ "${PRIVACY_FILTER_INPUT_MODE}" != "runs" && "${PRIVACY_FILTER_INPUT_MODE}" != "folder" ]]; then
    echo "error: --input-mode must be 'runs' or 'folder', got '${PRIVACY_FILTER_INPUT_MODE}'" >&2
    exit 2
  fi
}

run_ner_scan() {
  local mode="$1"
  preflight_checks

  echo "Scanning:  ${PRIVACY_FILTER_INPUT_ROOT}"
  echo "Input mode: ${PRIVACY_FILTER_INPUT_MODE}"
  echo "Mode:      ${mode}"
  echo "Partition: ${PARTITION}"
  echo "Run root:  ${PRIVACY_FILTER_RUN_ROOT}"
  echo

  # submit_privacy_filter.sh prints "Manifest job: N" (only when a manifest
  # doesn't exist yet) and "Privacy Filter <mode> job: N" -- captured here
  # instead of just passed through, so their job ids can be turned into
  # real status/log/cancel commands below.
  local output
  output="$(
    PRIVACY_FILTER_MODE="${mode}" \
    PRIVACY_FILTER_INPUT_ROOT="${PRIVACY_FILTER_INPUT_ROOT}" \
    PRIVACY_FILTER_INPUT_MODE="${PRIVACY_FILTER_INPUT_MODE}" \
    PRIVACY_FILTER_RUN_ROOT="${PRIVACY_FILTER_RUN_ROOT}" \
    PRIVACY_FILTER_PARTITION="${PARTITION}" \
      bash "${REPO}/launchers/slurm/submit_privacy_filter.sh"
  )"
  echo "${output}"

  local manifest_job scan_job scan_log_name
  manifest_job="$(grep -oP 'Manifest job: \K[0-9]+' <<< "${output}" || true)"
  scan_job="$(grep -oP "Privacy Filter ${mode} job: \K[0-9]+" <<< "${output}" || true)"
  if [[ "${mode}" == "pilot" ]]; then
    scan_log_name="privacy_filter_pilot"
  else
    scan_log_name="privacy_filter_node"
  fi

  if [[ -n "${manifest_job}" ]]; then
    echo
    echo "Manifest job (${manifest_job}):"
    print_job_commands "${manifest_job}" \
      "${REPO}/logs/slurm/privacy_filter_manifest_${manifest_job}.out" \
      "${REPO}/logs/slurm/privacy_filter_manifest_${manifest_job}.err"
  fi
  if [[ -n "${scan_job}" ]]; then
    echo
    echo "Scan job, ${mode} (${scan_job}):"
    print_job_commands "${scan_job}" \
      "${REPO}/logs/slurm/${scan_log_name}_${scan_job}.out" \
      "${REPO}/logs/slurm/${scan_log_name}_${scan_job}.err"
    echo "  Per-worker logs: ${PRIVACY_FILTER_RUN_ROOT}/worker_logs/"
  fi

  echo
  echo "Results, once finished, under ${PRIVACY_FILTER_RUN_ROOT}:"
  echo "  all_spans.jsonl     -- every span the model found, including the ones the policy rejected"
  echo "  llm_candidates.csv  -- the reviewable shortlist"
}

main() {
  parse_args "$@"

  if [[ "${STAGE_GIVEN}" -eq 0 ]]; then
    if [[ -t 0 ]]; then
      show_menu
      apply_action_defaults
      pick_partition
      PRIVACY_FILTER_INPUT_ROOT="$(prompt_value "Input root" "${PRIVACY_FILTER_INPUT_ROOT}")"
      PRIVACY_FILTER_RUN_ROOT="$(prompt_value "Run root" "${PRIVACY_FILTER_RUN_ROOT}")"
      review_settings "${REVIEW_PAIRS[@]}"
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

  run_ner_scan "${ACTION}"
}

main "$@"
