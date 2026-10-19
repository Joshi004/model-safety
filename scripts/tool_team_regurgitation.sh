#!/bin/bash
# Run the PII regurgitation probe (medpsy's pii_regurgitation_check.py,
# unchanged) against a served checkpoint.
#
# Deploy settings -- served name, dtype, max sequence length, family-
# specific `vllm serve` flags, thinking support, sampling profile -- are
# fetched straight from the checkpoint's own config.json /
# generation_config.json / chat_template.jinja (see
# modelsafety/serving/vllm_config.py). Every derived value
# is printed, and in interactive mode every one of them is editable before
# anything is submitted.
#
# This script only prepares the probe input, derives/reviews settings, and
# submits one sbatch job (launchers/slurm/run_regurgitation.sbatch), which
# serves the checkpoint with vLLM and runs the probe against it.
#
# Usage:
#   ./scripts/tool_team_regurgitation.sh <pilot|full|report> [--flag value ...]
#   ./scripts/tool_team_regurgitation.sh                       # no action -- interactive menu
#
# Actions (exactly one per call):
#   pilot   1 GPU, 200 sampled PII values -- a first real run.
#   full    4 GPUs by default, every PII value that survives the repeat
#           filter (see prepare_probe_input.py's --max-repeats).
#   report  Local only, no GPU, no SLURM job, a few seconds. Reads an
#           existing run's results and prints a summary, plus the deploy
#           config that run used. Safe to run at any time.
#
# Flags (each overrides the matching setting for this run only):
#   --checkpoint PATH        --input-csv PATH         --run-dir PATH
#   --partition NAME         --gpu-count N            --tp-size N
#   --cpus N                 --mem SIZE               --time HH:MM:SS
#   --dtype NAME             --max-model-len N        --family-flags JSON
#   --thinking 0|1           --sampling JSON          --max-tokens N
#   --extra-vllm-args STR    --vllm-bin PATH
#   --trials N               --temperature FLOAT      --max-repeats N
#   --sample-size N          --max-context-chars N    --requests-per-server N
#
# Precedence for every setting, lowest to highest: value derived from the
# checkpoint (deploy settings only) or hardcoded default in this file <
# environment variable < CLI flag above < interactive edit. Interactive
# editing is only offered when no action word is given at all -- passing
# pilot, full, or report runs fully non-interactively. Deploy settings are
# only derived for pilot/full; report never touches the checkpoint.
#
# A --run-dir is reused across calls (so a killed/failed job can be
# resubmitted without repeating finished work -- see --resume in the probe
# and the "reuse existing probe_input.csv" note in the sbatch launcher),
# but only if the checkpoint, input CSV, thinking mode, temperature,
# trials, and token budget all still match what that run dir was started
# with. Otherwise this refuses and asks for a different --run-dir, rather
# than silently mixing results from two different configurations.
#
# Examples:
#   ./scripts/tool_team_regurgitation.sh pilot
#   ./scripts/tool_team_regurgitation.sh full --gpu-count 8 --partition toolCall
#   ./scripts/tool_team_regurgitation.sh full --checkpoint /path/to/other/checkpoint
#   ./scripts/tool_team_regurgitation.sh report --run-dir output/tool_team_regurgitation/<served-name>/pilot

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=lib/tool_team_common.sh
source "${SCRIPT_DIR}/lib/tool_team_common.sh"

VLLM_CONFIG_PY="${REPO}/modelsafety/serving/vllm_config.py"
SBATCH_SCRIPT="${REPO}/launchers/slurm/run_regurgitation.sbatch"
# Every partition on this cluster carries the same H100 GPUs under this
# same gres name (confirmed with `sinfo`), same as every other GPU wrapper
# in this repo -- this is a property of the cluster's hardware, not a
# per-run setting, so it is not exposed as one here.
GRES_TYPE="nvidia_h100_80gb_hbm3"

# ---------------------------------------------------------------------------
# Config -- edit any value below directly, or override by exporting the
# environment variable before calling this script, or with a CLI flag (see
# the usage header above). CLI flags win over environment variables.
#
# RUN_DIR / GPU_COUNT / CPUS / MEM / TIME / SAMPLE_SIZE start empty on
# purpose -- their real defaults depend on the action (pilot vs full) and,
# for RUN_DIR, on the checkpoint's served name, so they are filled in by
# apply_action_defaults() / derive_checkpoint_settings() once those are
# known. TP_SIZE / DTYPE / MAX_MODEL_LEN / THINKING / FAMILY_FLAGS_JSON /
# SAMPLING_JSON / MAX_TOKENS start empty for the same reason -- they come
# from the checkpoint unless an environment variable or CLI flag already
# set them.
# ---------------------------------------------------------------------------

# --- What to probe ----------------------------------------------------
CHECKPOINT="${CHECKPOINT:-/home/patrik/exps/merging/outputs/Qwen3.5-0.8B-RL-Think-eopd-alwayson-v11c-267986-s600+Think-s1_OPD_longcot-s2_RL_rlmixv11a-s3_RL_rlmixv11c_slerp}"
INPUT_CSV="${INPUT_CSV:-${REPO}/output/tool_team_pii_run/stage3_extract/pii_extract.csv}"
RUN_DIR="${RUN_DIR:-}"

# --- SLURM resources, filled in per-action by apply_action_defaults -------
PARTITION="${PARTITION:-background}"    # background (low priority, can be preempted) | main | health | toolCall | VLM -- pick_partition lists the real, live set
GPU_COUNT="${GPU_COUNT:-}"
CPUS="${CPUS:-}"
MEM="${MEM:-}"
TIME="${TIME:-}"
SAMPLE_SIZE="${SAMPLE_SIZE:-}"       # pilot only; full always probes every value that survives the repeat filter

# --- Deploy settings -- filled in from the checkpoint by
# derive_checkpoint_settings unless an environment variable or CLI flag
# already set them ----------------------------------------------------
TP_SIZE="${TP_SIZE:-}"
DTYPE="${DTYPE:-}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-}"
THINKING="${THINKING:-}"
FAMILY_FLAGS_JSON="${FAMILY_FLAGS_JSON:-}"
SAMPLING_JSON="${SAMPLING_JSON:-}"
EXTRA_VLLM_ARGS="${EXTRA_VLLM_ARGS:-}"
VLLM_BIN="${VLLM_BIN:-/home/shared/agentic_slm/qvac-research-tool-call/evaluation/services/vllm/.venv/bin/vllm}"

# --- Probe tuning -------------------------------------------------------
MAX_TOKENS="${MAX_TOKENS:-}"          # filled in from THINKING (4096 if on, 256 if off) unless overridden
TRIALS="${TRIALS:-5}"
TEMPERATURE="${TEMPERATURE:-0.7}"
MAX_REPEATS="${MAX_REPEATS:-20}"
MAX_CONTEXT_CHARS="${MAX_CONTEXT_CHARS:-150000}"
REQUESTS_PER_SERVER="${REQUESTS_PER_SERVER:-64}"

# SERVED_NAME is set by derive_checkpoint_settings(); declared here only so
# `set -u` doesn't complain if something reads it before that runs.
SERVED_NAME="${SERVED_NAME:-}"

ACTION=""
ACTION_GIVEN=0

usage() {
  cat <<EOF
Usage: $(basename "$0") <pilot|full|report> [--flag value ...]
       $(basename "$0")                       # no action -- interactive menu

Actions (exactly one per call):
  pilot   1 GPU, 200 sampled PII values -- a first real run.
  full    4 GPUs by default, every PII value that survives the repeat filter.
  report  Local only, no GPU, no SLURM job, a few seconds. Safe to run at
          any time.

Flags (each overrides the matching setting for this run only):
  --checkpoint PATH        --input-csv PATH         --run-dir PATH
  --partition NAME         --gpu-count N            --tp-size N
  --cpus N                 --mem SIZE               --time HH:MM:SS
  --dtype NAME             --max-model-len N        --family-flags JSON
  --thinking 0|1           --sampling JSON          --max-tokens N
  --extra-vllm-args STR    --vllm-bin PATH
  --trials N               --temperature FLOAT      --max-repeats N
  --sample-size N          --max-context-chars N    --requests-per-server N

Deploy settings (dtype, max-model-len, family flags, thinking, sampling)
default to values derived from --checkpoint's own config files -- see
modelsafety/serving/vllm_config.py. Passing one of the
matching flags above overrides the derived value.

Precedence, lowest to highest: value derived from the checkpoint or
hardcoded default in this file < environment variable < CLI flag <
interactive edit. Interactive editing is only offered when no action word
is given at all -- passing pilot, full, or report runs fully
non-interactively. report never touches the checkpoint.

A --run-dir is reused across calls, but only if the checkpoint, input CSV,
thinking mode, temperature, trials, and token budget all still match what
that run dir was started with -- otherwise this refuses and asks for a
different --run-dir.

'pilot' and 'full' submit one sbatch job and return immediately -- this
script does not wait for the job to finish. 'report' never touches SLURM.

Examples:
  $(basename "$0") pilot
  $(basename "$0") full --gpu-count 8 --partition toolCall
  $(basename "$0") full --checkpoint /path/to/other/checkpoint
  $(basename "$0") report --run-dir output/tool_team_regurgitation/<served-name>/pilot

Current settings:
  CHECKPOINT            ${CHECKPOINT}
  INPUT_CSV             ${INPUT_CSV}
  PARTITION             ${PARTITION}
  VLLM_BIN              ${VLLM_BIN}
  TRIALS                ${TRIALS}
  TEMPERATURE           ${TEMPERATURE}
  MAX_REPEATS           ${MAX_REPEATS}
  MAX_CONTEXT_CHARS     ${MAX_CONTEXT_CHARS}
  REQUESTS_PER_SERVER   ${REQUESTS_PER_SERVER}
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
        --checkpoint) CHECKPOINT="$2" ;;
        --input-csv) INPUT_CSV="$2" ;;
        --run-dir) RUN_DIR="$2" ;;
        --partition) PARTITION="$2" ;;
        --gpu-count) GPU_COUNT="$2" ;;
        --tp-size) TP_SIZE="$2" ;;
        --cpus) CPUS="$2" ;;
        --mem) MEM="$2" ;;
        --time) TIME="$2" ;;
        --dtype) DTYPE="$2" ;;
        --max-model-len) MAX_MODEL_LEN="$2" ;;
        --family-flags) FAMILY_FLAGS_JSON="$2" ;;
        --thinking) THINKING="$2" ;;
        --sampling) SAMPLING_JSON="$2" ;;
        --max-tokens) MAX_TOKENS="$2" ;;
        --extra-vllm-args) EXTRA_VLLM_ARGS="$2" ;;
        --vllm-bin) VLLM_BIN="$2" ;;
        --trials) TRIALS="$2" ;;
        --temperature) TEMPERATURE="$2" ;;
        --max-repeats) MAX_REPEATS="$2" ;;
        --sample-size) SAMPLE_SIZE="$2" ;;
        --max-context-chars) MAX_CONTEXT_CHARS="$2" ;;
        --requests-per-server) REQUESTS_PER_SERVER="$2" ;;
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

# apply_action_defaults -- fills in GPU_COUNT / CPUS / MEM / TIME /
# SAMPLE_SIZE with per-action defaults, but only where a hardcoded
# default, environment variable, or CLI flag hasn't already set a value
# (":=" only assigns when the variable is unset or empty). Resource
# defaults are a starting point, not a measured optimum -- see "Things to
# confirm during the pilot" in the design notes: adjust --cpus/--mem/--time
# once a real run shows actual throughput and memory use.
apply_action_defaults() {
  case "${ACTION}" in
    pilot)
      : "${GPU_COUNT:=1}"
      : "${SAMPLE_SIZE:=200}"
      : "${CPUS:=16}"
      : "${MEM:=128G}"
      : "${TIME:=04:00:00}"
      ;;
    full)
      : "${GPU_COUNT:=4}"
      : "${SAMPLE_SIZE:=0}"
      : "${CPUS:=32}"
      : "${MEM:=256G}"
      : "${TIME:=24:00:00}"
      ;;
    report) ;;
  esac
}

# derive_checkpoint_settings -- resolves CHECKPOINT, runs vllm_config.py
# against it, and fills TP_SIZE / DTYPE / MAX_MODEL_LEN / FAMILY_FLAGS_JSON
# / THINKING / SAMPLING_JSON / MAX_TOKENS from the result, but only where
# an environment variable or CLI flag hasn't already set that setting.
# Only called for pilot/full -- report never touches the checkpoint.
derive_checkpoint_settings() {
  CHECKPOINT="$(realpath -m -- "${CHECKPOINT}")"
  if [[ ! -d "${CHECKPOINT}" ]]; then
    die "checkpoint directory does not exist: ${CHECKPOINT}"
  fi

  echo "Reading deploy settings from ${CHECKPOINT} ..."
  local derived
  derived="$(python3 "${VLLM_CONFIG_PY}" --checkpoint "${CHECKPOINT}")" || {
    die "vllm_config.py could not derive settings for this checkpoint (see its own error above)"
  }
  eval "${derived}"

  if [[ "${CKPT_WEIGHT_BYTES}" -eq 0 ]]; then
    die "no weight files (*.safetensors or *.bin) found under checkpoint: ${CHECKPOINT}"
  fi

  SERVED_NAME="${CKPT_SERVED_NAME}"
  : "${TP_SIZE:=${CKPT_TP_SUGGESTED}}"
  : "${DTYPE:=${CKPT_DTYPE}}"
  : "${MAX_MODEL_LEN:=${CKPT_MAX_MODEL_LEN}}"
  : "${FAMILY_FLAGS_JSON:=${CKPT_FAMILY_FLAGS_JSON}}"

  if [[ -z "${THINKING}" ]]; then
    if [[ "${CKPT_THINKING_SUPPORTED}" -eq 1 ]]; then
      THINKING=1
    else
      THINKING=0
    fi
  elif [[ "${THINKING}" == "1" && "${CKPT_THINKING_SUPPORTED}" -eq 0 ]]; then
    echo "note: --thinking 1 was requested but this checkpoint's chat template does not appear to read an enable_thinking kwarg -- proceeding anyway, but the flag may have no effect." >&2
  fi

  if [[ -z "${SAMPLING_JSON}" ]]; then
    if [[ "${THINKING}" == "1" ]]; then
      SAMPLING_JSON="${CKPT_SAMPLING_THINK_JSON}"
    else
      SAMPLING_JSON="${CKPT_SAMPLING_INSTRUCT_JSON}"
    fi
  fi

  if [[ -z "${MAX_TOKENS}" ]]; then
    if [[ "${THINKING}" == "1" ]]; then
      MAX_TOKENS=4096
    else
      MAX_TOKENS=256
    fi
  fi

  echo "  Served name:      ${SERVED_NAME}"
  echo "  Model type:       ${CKPT_MODEL_TYPE} (family: ${CKPT_FAMILY})"
  echo "  Weight size:      ${CKPT_WEIGHT_HUMAN}"
  echo "  dtype:            ${DTYPE}"
  echo "  Max model len:    ${MAX_MODEL_LEN}"
  echo "  Family flags:     ${FAMILY_FLAGS_JSON}"
  echo "  Thinking:         ${THINKING} (checkpoint supports enable_thinking: ${CKPT_THINKING_SUPPORTED})"
  echo "  Sampling profile: ${SAMPLING_JSON}"
  echo "  Token budget:     ${MAX_TOKENS}"
  echo "  TP size:          ${TP_SIZE} (suggested from weight size: ${CKPT_TP_SUGGESTED})"
  echo
}

# build_review_pairs -- sets the global REVIEW_PAIRS array for
# review_settings (see scripts/lib/tool_team_common.sh). Only used for
# pilot/full; report's interactive path just prompts for the run dir.
build_review_pairs() {
  REVIEW_PAIRS=(
    INPUT_CSV "Input CSV (raw PII extract)"
    RUN_DIR "Run dir"
    PARTITION "Partition"
    GPU_COUNT "GPU count"
    TP_SIZE "Tensor parallel size"
    CPUS "CPUs"
    MEM "Memory"
    TIME "Time limit"
    DTYPE "dtype"
    MAX_MODEL_LEN "Max model length"
    THINKING "Thinking (0/1)"
    MAX_TOKENS "Token budget"
    FAMILY_FLAGS_JSON "Family vllm serve flags (JSON array)"
    SAMPLING_JSON "Sampling override (JSON object, {} = none)"
    EXTRA_VLLM_ARGS "Extra vllm serve args (free text)"
    VLLM_BIN "vLLM binary path"
    TRIALS "Trials per value"
    TEMPERATURE "Sampling temperature"
    MAX_REPEATS "Max repeats before dropping a value"
    MAX_CONTEXT_CHARS "Max context characters sent to the model"
    REQUESTS_PER_SERVER "Concurrent requests per replica"
  )
  if [[ "${ACTION}" == "pilot" ]]; then
    REVIEW_PAIRS+=(SAMPLE_SIZE "Sample size (pilot only)")
  fi
}

# validate_settings -- checked once, after any interactive edit, right
# before paths get resolved and used. Fails loudly on a bad value instead
# of letting it reach sbatch or vllm serve with a confusing downstream
# error.
validate_settings() {
  case "${THINKING}" in
    0 | 1) ;;
    *) die "--thinking must be 0 or 1, got '${THINKING}'" ;;
  esac
  if ! [[ "${GPU_COUNT}" =~ ^[0-9]+$ ]] || [[ "${GPU_COUNT}" -lt 1 ]]; then
    die "--gpu-count must be a positive integer, got '${GPU_COUNT}'"
  fi
  if ! [[ "${TP_SIZE}" =~ ^[0-9]+$ ]] || [[ "${TP_SIZE}" -lt 1 ]]; then
    die "--tp-size must be a positive integer, got '${TP_SIZE}'"
  fi
  if (( GPU_COUNT % TP_SIZE != 0 )); then
    die "--gpu-count (${GPU_COUNT}) must be divisible by --tp-size (${TP_SIZE})"
  fi
  if ! [[ "${TRIALS}" =~ ^[0-9]+$ ]] || [[ "${TRIALS}" -lt 1 ]]; then
    die "--trials must be a positive integer, got '${TRIALS}'"
  fi
  if ! [[ "${MAX_TOKENS}" =~ ^[0-9]+$ ]] || [[ "${MAX_TOKENS}" -lt 1 ]]; then
    die "--max-tokens must be a positive integer, got '${MAX_TOKENS}'"
  fi
  if ! [[ "${MAX_REPEATS}" =~ ^[0-9]+$ ]]; then
    die "--max-repeats must be 0 or a positive integer, got '${MAX_REPEATS}'"
  fi
  if ! [[ "${SAMPLE_SIZE}" =~ ^[0-9]+$ ]]; then
    die "--sample-size must be 0 or a positive integer, got '${SAMPLE_SIZE}'"
  fi
  if ! [[ "${MAX_CONTEXT_CHARS}" =~ ^[0-9]+$ ]]; then
    die "--max-context-chars must be 0 or a positive integer, got '${MAX_CONTEXT_CHARS}'"
  fi
  if ! [[ "${REQUESTS_PER_SERVER}" =~ ^[0-9]+$ ]] || [[ "${REQUESTS_PER_SERVER}" -lt 1 ]]; then
    die "--requests-per-server must be a positive integer, got '${REQUESTS_PER_SERVER}'"
  fi
  python3 -c "import json,sys; json.loads(sys.argv[1])" "${FAMILY_FLAGS_JSON}" >/dev/null 2>&1 \
    || die "--family-flags is not valid JSON: ${FAMILY_FLAGS_JSON}"
  python3 -c "import json,sys; json.loads(sys.argv[1])" "${SAMPLING_JSON}" >/dev/null 2>&1 \
    || die "--sampling is not valid JSON: ${SAMPLING_JSON}"
}

# ---------------------------------------------------------------------------
# Active-job guard, run-dir compatibility check
# ---------------------------------------------------------------------------

# check_no_active_job -- refuses to submit if a regurgitation job from this
# wrapper is already queued or running, so two calls can't collide
# submitting against the same run dir at once.
check_no_active_job() {
  local active
  active="$(squeue --me -h -n "regurg_pilot,regurg_full" -o '%i %j %T' 2>/dev/null || true)"
  if [[ -n "${active}" ]]; then
    echo "error: a regurgitation job from this wrapper is already queued or running:" >&2
    echo "${active}" >&2
    echo "Wait for it to finish (or scancel it) before submitting another." >&2
    exit 2
  fi
}

# check_run_dir_compatibility -- if RUN_DIR already holds a run_config.env
# from a previous call, compare the settings that would change what the
# results mean (checkpoint, input CSV, thinking, temperature, trials,
# token budget) against the current resolved values. Dies with exactly
# what differs rather than silently mixing two configurations' results
# together. A run dir with no existing config (fresh, or never submitted)
# always passes.
check_run_dir_compatibility() {
  local existing_config="${RUN_DIR}/run_config.env"
  if [[ ! -f "${existing_config}" ]]; then
    return 0
  fi

  local mismatch
  mismatch="$(
    # shellcheck disable=SC1090
    source "${existing_config}"
    problems=()
    [[ "${REGURG_CHECKPOINT_PATH}" != "${CHECKPOINT}" ]] && problems+=("checkpoint: ${REGURG_CHECKPOINT_PATH} -> ${CHECKPOINT}")
    [[ "${REGURG_INPUT_CSV}" != "${INPUT_CSV}" ]] && problems+=("input CSV: ${REGURG_INPUT_CSV} -> ${INPUT_CSV}")
    [[ "${REGURG_THINKING}" != "${THINKING}" ]] && problems+=("thinking: ${REGURG_THINKING} -> ${THINKING}")
    [[ "${REGURG_TEMPERATURE}" != "${TEMPERATURE}" ]] && problems+=("temperature: ${REGURG_TEMPERATURE} -> ${TEMPERATURE}")
    [[ "${REGURG_TRIALS}" != "${TRIALS}" ]] && problems+=("trials: ${REGURG_TRIALS} -> ${TRIALS}")
    [[ "${REGURG_MAX_TOKENS}" != "${MAX_TOKENS}" ]] && problems+=("token budget: ${REGURG_MAX_TOKENS} -> ${MAX_TOKENS}")
    if [[ ${#problems[@]} -gt 0 ]]; then
      printf '%s\n' "${problems[@]}"
    fi
  )"

  if [[ -n "${mismatch}" ]]; then
    echo "error: ${RUN_DIR} already holds a run with different settings (previous -> requested):" >&2
    echo "${mismatch}" >&2
    echo "Use a different --run-dir, or match the previous settings exactly to resume." >&2
    exit 2
  fi
}

# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

# submit_job -- resolve remaining paths, preflight, write run_config.env,
# submit run_regurgitation.sbatch. Always returns immediately -- serving a
# checkpoint and running the probe must go through sbatch, never directly
# on the login node.
submit_job() {
  RUN_DIR="$(realpath -m -- "${RUN_DIR}")"
  INPUT_CSV="$(realpath -m -- "${INPUT_CSV}")"
  VLLM_BIN="$(realpath -m -- "${VLLM_BIN}")"

  check_no_active_job
  check_run_dir_compatibility

  if [[ ! -f "${INPUT_CSV}" ]]; then
    die "input CSV does not exist: ${INPUT_CSV}"
  fi
  if [[ ! -x "${VLLM_BIN}" ]]; then
    die "vLLM binary not found or not executable: ${VLLM_BIN}"
  fi

  mkdir -p "${RUN_DIR}" "${REPO}/logs/slurm"

  local config_path="${RUN_DIR}/run_config.env"
  {
    printf 'export REGURG_CHECKPOINT_PATH=%q\n' "${CHECKPOINT}"
    printf 'export REGURG_RUN_DIR=%q\n' "${RUN_DIR}"
    printf 'export REGURG_INPUT_CSV=%q\n' "${INPUT_CSV}"
    printf 'export REGURG_SERVED_NAME=%q\n' "${SERVED_NAME}"
    printf 'export REGURG_GPU_COUNT=%q\n' "${GPU_COUNT}"
    printf 'export REGURG_TP_SIZE=%q\n' "${TP_SIZE}"
    printf 'export REGURG_DTYPE=%q\n' "${DTYPE}"
    printf 'export REGURG_MAX_MODEL_LEN=%q\n' "${MAX_MODEL_LEN}"
    printf 'export REGURG_THINKING=%q\n' "${THINKING}"
    printf 'export REGURG_MAX_TOKENS=%q\n' "${MAX_TOKENS}"
    printf 'export REGURG_TRIALS=%q\n' "${TRIALS}"
    printf 'export REGURG_TEMPERATURE=%q\n' "${TEMPERATURE}"
    printf 'export REGURG_MAX_REPEATS=%q\n' "${MAX_REPEATS}"
    printf 'export REGURG_SAMPLE_SIZE=%q\n' "${SAMPLE_SIZE}"
    printf 'export REGURG_FAMILY_FLAGS_JSON=%q\n' "${FAMILY_FLAGS_JSON}"
    printf 'export REGURG_SAMPLING_JSON=%q\n' "${SAMPLING_JSON}"
    printf 'export REGURG_EXTRA_VLLM_ARGS=%q\n' "${EXTRA_VLLM_ARGS}"
    printf 'export REGURG_MAX_CONTEXT_CHARS=%q\n' "${MAX_CONTEXT_CHARS}"
    printf 'export REGURG_REQUESTS_PER_SERVER=%q\n' "${REQUESTS_PER_SERVER}"
    printf 'export REGURG_VLLM_BIN=%q\n' "${VLLM_BIN}"
  } > "${config_path}"

  echo "Checkpoint:  ${CHECKPOINT}"
  echo "Served name: ${SERVED_NAME}"
  echo "Run dir:     ${RUN_DIR}"
  echo "Action:      ${ACTION}"
  echo "Partition:   ${PARTITION}"
  echo "Resources:   ${GPU_COUNT}x ${GRES_TYPE} GPU(s) (TP=${TP_SIZE} -> $((GPU_COUNT / TP_SIZE)) replica(s)), ${CPUS} CPUs, ${MEM} memory, ${TIME} time limit"
  echo "Config file: ${config_path}"
  echo

  local job_id
  job_id="$(
    sbatch --parsable --partition="${PARTITION}" --job-name="regurg_${ACTION}" \
      --gres="gpu:${GRES_TYPE}:${GPU_COUNT}" \
      --cpus-per-task="${CPUS}" --mem="${MEM}" --time="${TIME}" \
      --output="${REPO}/logs/slurm/tool_team_regurgitation_${ACTION}_%j.out" \
      --error="${REPO}/logs/slurm/tool_team_regurgitation_${ACTION}_%j.err" \
      --export=ALL,REPO="${REPO}",REGURG_CONFIG="${config_path}" \
      "${SBATCH_SCRIPT}"
  )" || die "sbatch submission failed (see sbatch's own error output above)"

  echo "Submitted regurgitation ${ACTION} job: ${job_id}"
  print_job_commands "${job_id}" \
    "${REPO}/logs/slurm/tool_team_regurgitation_${ACTION}_${job_id}.out" \
    "${REPO}/logs/slurm/tool_team_regurgitation_${ACTION}_${job_id}.err"
  echo "  Per-replica vLLM logs: ${RUN_DIR}/logs/"

  echo
  echo "Once finished:"
  echo "  $(basename "$0") report --run-dir ${RUN_DIR}"
}

# ---------------------------------------------------------------------------
# Report -- local only, safe at any time.
# ---------------------------------------------------------------------------

run_report() {
  if [[ -z "${RUN_DIR}" ]]; then
    die "no --run-dir given -- results live under ${REPO}/output/tool_team_regurgitation/<served-name>/<pilot|full>; run 'ls ${REPO}/output/tool_team_regurgitation' to see what's there"
  fi
  RUN_DIR="$(realpath -m -- "${RUN_DIR}")"

  local config_path="${RUN_DIR}/run_config.env"
  if [[ ! -f "${config_path}" ]]; then
    die "no run found at ${RUN_DIR} -- nothing has been submitted yet"
  fi

  # shellcheck disable=SC1090
  source "${config_path}"

  echo "Run:              ${RUN_DIR}"
  echo "Checkpoint:       ${REGURG_CHECKPOINT_PATH}"
  echo "Served name:      ${REGURG_SERVED_NAME}"
  echo "dtype:            ${REGURG_DTYPE}"
  echo "Max model len:    ${REGURG_MAX_MODEL_LEN}"
  echo "Family flags:     ${REGURG_FAMILY_FLAGS_JSON}"
  echo "Thinking:         ${REGURG_THINKING}"
  echo "Sampling profile: ${REGURG_SAMPLING_JSON}"
  echo "Token budget:     ${REGURG_MAX_TOKENS}"
  echo "GPUs / TP size:   ${REGURG_GPU_COUNT} / ${REGURG_TP_SIZE}"
  echo "Trials:           ${REGURG_TRIALS}"
  echo "Temperature:      ${REGURG_TEMPERATURE}"
  echo

  if [[ ! -f "${RUN_DIR}/_SUCCESS" ]]; then
    echo "Run at ${RUN_DIR} hasn't finished yet (no _SUCCESS marker)."
    local active
    active="$(squeue --me -h -n "regurg_pilot,regurg_full" -o '%i %j %T %M' 2>/dev/null || true)"
    if [[ -n "${active}" ]]; then
      echo
      echo "Active job(s):"
      echo "${active}"
    else
      echo "No regurg_pilot/regurg_full job currently queued or running -- it may have failed. Check:"
      echo "  ${RUN_DIR}/logs/"
    fi
    return 0
  fi

  local summary_path="${RUN_DIR}/results/pii_regurgitation_summary.json"
  local hits_path="${RUN_DIR}/results/pii_regurgitation_hits.csv"
  if [[ ! -f "${summary_path}" ]]; then
    echo "No summary found at ${summary_path} -- the probe may not have completed."
    return 0
  fi

  python3 - "${summary_path}" "${hits_path}" <<'PY'
import csv
import json
import sys

summary_path, hits_path = sys.argv[1], sys.argv[2]
with open(summary_path, encoding="utf-8") as handle:
    summary = json.load(handle)

print(f"Rows checked:      {summary.get('total_rows', 0):,}")
print(f"Rows regurgitated: {summary.get('hit_rows', 0):,}")
print(f"Rows with errors:  {summary.get('error_rows', 0) + summary.get('partial_error_rows', 0):,}")
print(
    f"Total trials run:  {summary.get('total_trials', 0):,} "
    f"({summary.get('total_trial_errors', 0):,} trial errors)"
)

by_category = summary.get("by_category", {})
if by_category:
    print()
    print("By category (regurgitated / total):")
    for category, counts in sorted(
        by_category.items(), key=lambda item: -item[1].get("regurgitated", 0)
    ):
        print(f"  {counts.get('regurgitated', 0):5,d} / {counts.get('total', 0):5,d}  {category}")

match_kinds = summary.get("match_kinds", {})
if match_kinds:
    print()
    print("How hits matched:")
    for kind, count in sorted(match_kinds.items(), key=lambda item: -item[1]):
        print(f"  {count:5,d}  {kind}")

try:
    with open(hits_path, encoding="utf-8", newline="") as handle:
        hit_rows = list(csv.DictReader(handle))
except FileNotFoundError:
    hit_rows = []

print()
print(f"Hit rows written to: {hits_path} ({len(hit_rows):,} rows)")
if hit_rows:
    print("First few hits:")
    for row in hit_rows[:5]:
        print(f"  {row.get('category', '')}: {row.get('file', '')}:{row.get('line', '')}")
PY

  echo
  echo "Full detail:"
  echo "  ${RUN_DIR}/results/pii_regurgitation_results.jsonl   (every trial, every response)"
  echo "  ${RUN_DIR}/results/pii_regurgitation_responses.csv   (one row per probed value)"
}

# ---------------------------------------------------------------------------

main() {
  parse_args "$@"

  if [[ "${ACTION_GIVEN}" -eq 0 ]]; then
    if [[ -t 0 ]]; then
      show_menu
      apply_action_defaults
      if [[ "${ACTION}" == "report" ]]; then
        RUN_DIR="$(prompt_value "Run dir" "${RUN_DIR}")"
      else
        CHECKPOINT="$(prompt_value "Checkpoint path" "${CHECKPOINT}")"
        derive_checkpoint_settings
        : "${RUN_DIR:=${REPO}/output/tool_team_regurgitation/${SERVED_NAME}/${ACTION}}"
        pick_partition
        INPUT_CSV="$(prompt_value "Input CSV" "${INPUT_CSV}")"
        RUN_DIR="$(prompt_value "Run dir" "${RUN_DIR}")"
        build_review_pairs
        review_settings "${REVIEW_PAIRS[@]}"
      fi
    else
      usage
      exit 1
    fi
  else
    apply_action_defaults
    if [[ "${ACTION}" != "report" ]]; then
      derive_checkpoint_settings
      : "${RUN_DIR:=${REPO}/output/tool_team_regurgitation/${SERVED_NAME}/${ACTION}}"
    fi
  fi

  if [[ -z "${ACTION}" ]]; then
    usage
    exit 1
  fi

  case "${ACTION}" in
    pilot | full)
      validate_settings
      submit_job
      ;;
    report)
      run_report
      ;;
  esac
}

main "$@"
