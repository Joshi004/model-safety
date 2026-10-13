#!/bin/bash
# Run the two-stage toxicity scan (Detoxify -> Qwen3Guard) against the tool
# team's converted data, sharded across SLURM array tasks.
#
# This is the toxicity counterpart to scripts/tool_team_data.sh (PII). It
# does not convert anything -- point it at output/prepared/tools_team (or
# any other JSONL folder) after running that script's `convert` stage.
#
# Usage:
#   ./scripts/tool_team_toxicity_scan.sh [stage ...] [--flag value ...]
#   ./scripts/tool_team_toxicity_scan.sh                 # no stages -- interactive menu
#
# Stages (pass one or more, in any order -- duplicates are harmless):
#   screen    Pass 1 only (Detoxify).
#   verify    Pass 2 only (Qwen3Guard), reusing pass 1's output.
#   all       Both passes. Same as passing: screen verify.
#   report    Local only, no GPU, no SLURM job. Safe to run at any time.
#
# Flags (each overrides the matching setting below, for this run only):
#   --input-root PATH     --run-dir PATH         --partition NAME
#   --cpus N              --mem SIZE             --time HH:MM:SS
#   --shards N            --concurrency N        --pilot-files N
#   --threshold FLOAT     --batch-size N         --guard-concurrency N
#   --max-model-len N     --vllm-image PATH      --input-mode runs|folder
#
# --input-mode decides which files are scanned under --input-root. The
# default, 'runs', reads only the files of finished conversions and writes
# <run-dir>/manifests/scan_inputs.json listing what it skipped (an unfinished
# conversion, a stray .jsonl). 'folder' reads every .jsonl under it.
#
# Examples:
#   ./scripts/tool_team_toxicity_scan.sh all --pilot-files 3 --partition toolCall
#   ./scripts/tool_team_toxicity_scan.sh screen
#   ./scripts/tool_team_toxicity_scan.sh verify
#   ./scripts/tool_team_toxicity_scan.sh report
#
# Precedence for every setting, lowest to highest: hardcoded default in this
# file < environment variable < CLI flag above < interactive edit (only
# offered when no stage word is given at all, so scripted/automated calls
# never prompt).
#
# --run-dir defaults to output/tool_team_toxicity_pilot when --pilot-files is
# above 0, and to output/tool_team_toxicity_run otherwise, so a pilot never
# shares a folder with the full scan that follows it.
#
# --shards only matters the FIRST time a given --run-dir is used -- it decides
# how the input gets split into shards. Every later call against the same
# --run-dir resumes that existing layout and only submits whatever isn't
# finished yet; --shards is ignored and a note says so. A run folder also
# remembers its mode and the settings that change the results (--pilot-files,
# --threshold, the models, --max-model-len) and refuses a call that differs.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=lib/tool_team_common.sh
source "${SCRIPT_DIR}/lib/tool_team_common.sh"

# ---------------------------------------------------------------------------
# Config -- edit any value below directly, or override by exporting the
# environment variable before calling this script, or with a CLI flag (see
# the usage header above). CLI flags win over environment variables.
# ---------------------------------------------------------------------------

# --- Paths -------------------------------------------------------------
TOX_INPUT_ROOT="${TOX_INPUT_ROOT:-${REPO}/output/prepared/tools_team}"
# Empty on purpose -- the real default depends on --pilot-files, and is filled
# in by apply_run_dir_default() once that's known.
TOX_RUN_DIR="${TOX_RUN_DIR:-}"
TOX_INPUT_MODE="${TOX_INPUT_MODE:-runs}"   # runs = files of finished conversions only; folder = every .jsonl

# --- SLURM resources, per shard (one shard = one H100) -------------------
PARTITION="${PARTITION:-health}"    # main | health | toolCall | VLM -- pick_partition lists the real, live set
TOX_CPUS="${TOX_CPUS:-16}"
TOX_MEM="${TOX_MEM:-96G}"
TOX_TIME="${TOX_TIME:-48:00:00}"

# --- Sharding ------------------------------------------------------------
TOX_SHARDS="${TOX_SHARDS:-8}"           # how many shards to split the input into (first run only)
TOX_CONCURRENCY="${TOX_CONCURRENCY:-8}" # how many shards run at once == how many GPUs in use at once
TOX_PILOT_FILES="${TOX_PILOT_FILES:-0}" # 0 = every file; N = a deterministic spread across top-level groups

# --- Scan tuning, passed straight through to the launcher's own variables ---
TOX_THRESHOLD="${TOX_THRESHOLD:-0.5}"
TOX_STAGE1_MODEL="${TOX_STAGE1_MODEL:-multilingual}"
GUARD_MODEL="${GUARD_MODEL:-Qwen/Qwen3Guard-Gen-8B}"
TOX_BATCH_SIZE="${TOX_BATCH_SIZE:-32}"
GUARD_CONCURRENCY="${GUARD_CONCURRENCY:-8}"
VLLM_MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-8192}"

# --- vLLM image ------------------------------------------------------------
# Prefer this repo's own image if someone ever adds one; otherwise fall back
# to the shared image confirmed working against Qwen3Guard-Gen-8B in a real
# trial run (see TOXICITY_SCAN_EXPLAINED.md).
default_vllm_image() {
  local repo_image="${REPO}/containers/vllm-openai-v0.24.0-cu129.sqsh"
  if [[ -f "${repo_image}" ]]; then
    printf '%s' "${repo_image}"
  else
    printf '%s' "/home/shared/containers/vllm-0.22.0-cu129.sqsh"
  fi
}
VLLM_IMAGE="${VLLM_IMAGE:-$(default_vllm_image)}"

# Name/label pairs for the interactive review screen -- see review_settings
# in scripts/lib/tool_team_common.sh.
REVIEW_PAIRS=(
  TOX_INPUT_ROOT "Input root"
  TOX_RUN_DIR "Run dir"
  TOX_INPUT_MODE "Input mode (runs|folder)"
  PARTITION "Partition"
  TOX_CPUS "CPUs per shard"
  TOX_MEM "Memory per shard"
  TOX_TIME "Time limit"
  TOX_SHARDS "Shards (first run only)"
  TOX_CONCURRENCY "Concurrency"
  TOX_PILOT_FILES "Pilot files, 0=all"
  TOX_THRESHOLD "Detoxify threshold"
  TOX_BATCH_SIZE "Detoxify batch size"
  GUARD_CONCURRENCY "Guard concurrency"
  VLLM_MAX_MODEL_LEN "Max model length"
  VLLM_IMAGE "vLLM image"
)

# --- Which stages were requested, and whether any word was given at all --
# STAGE_GIVEN=0 is what triggers the interactive path.
DO_SCREEN=0
DO_VERIFY=0
DO_REPORT=0
STAGE_GIVEN=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [stage ...] [--flag value ...]
       $(basename "$0")                 # no stages -- interactive menu

Stages (pass one or more, in any order; duplicates are harmless):
  screen    Pass 1 only (Detoxify). Submits whichever shards haven't been
            screened yet.
  verify    Pass 2 only (Qwen3Guard), reusing pass 1's output. Requires
            'screen' (or 'all') to have already finished on every shard --
            refuses and lists any shard that hasn't, rather than silently
            skipping it.
  all       Both passes, back to back, on whichever shards aren't fully
            done yet. Pass 1 is always rerun here, never reused, so a
            partial pass-1 result is never trusted. Same as: screen verify.
  report    Local only, no GPU, no SLURM job, a few seconds. Shows how many
            shards are done / screened-only / not started, sums flagged
            messages and guard errors, and writes an aggregated guard
            report. Safe to run at any time, including while other stages
            are still running on the cluster.

Flags (each overrides the matching setting for this run only):
  --input-root PATH     --run-dir PATH         --partition NAME
  --cpus N              --mem SIZE             --time HH:MM:SS
  --shards N            --concurrency N        --pilot-files N
  --threshold FLOAT     --batch-size N         --guard-concurrency N
  --max-model-len N     --vllm-image PATH      --input-mode runs|folder

--input-mode decides which files are scanned under --input-root. 'runs' (the
default) reads only the files of finished conversions and lists everything it
skipped, with the reason, in <run-dir>/manifests/scan_inputs.json. 'folder'
reads every .jsonl under --input-root.

Precedence, lowest to highest: hardcoded default in this file < environment
variable < CLI flag < interactive edit. Interactive editing is only offered
when no stage word is given at all -- passing any stage word runs fully
non-interactively.

--run-dir defaults to output/tool_team_toxicity_pilot when --pilot-files is
above 0, and to output/tool_team_toxicity_run otherwise. To verify or report
on a pilot, pass the same --pilot-files again (or --run-dir).

--shards only matters the FIRST time a given --run-dir is used. Every later
call against the same --run-dir resumes that existing layout, ignores
--shards (with a note), and only submits shards that still need work. If
nothing is left to do, it says so and exits.

A run folder remembers its mode and the settings that change the results
(--pilot-files, --threshold, the Detoxify and guard models, --max-model-len)
and the input files it was started with. A call that differs is refused, and
the message says what differs.

'screen', 'verify', and 'all' each submit one SLURM array job (one H100 per
shard) and return immediately -- they do not wait for the job to finish.

For PII, see scripts/tool_team_data.sh instead. This script assumes that
script's 'convert' stage has already produced JSONL under --input-root.

Examples:
  $(basename "$0") all --pilot-files 3 --partition toolCall   # first real trial
  $(basename "$0") screen                                     # pass 1 across everything
  $(basename "$0") verify                                     # pass 2, once screen looks right
  $(basename "$0") report                                     # check on any of the above

Current settings:
  TOX_INPUT_ROOT      ${TOX_INPUT_ROOT}
  TOX_RUN_DIR         ${TOX_RUN_DIR:-(set from --pilot-files, see above)}
  TOX_INPUT_MODE      ${TOX_INPUT_MODE}
  PARTITION           ${PARTITION}
  TOX_CPUS            ${TOX_CPUS}
  TOX_MEM             ${TOX_MEM}
  TOX_TIME            ${TOX_TIME}
  TOX_SHARDS          ${TOX_SHARDS}
  TOX_CONCURRENCY     ${TOX_CONCURRENCY}
  TOX_PILOT_FILES     ${TOX_PILOT_FILES}
  TOX_THRESHOLD       ${TOX_THRESHOLD}
  TOX_STAGE1_MODEL    ${TOX_STAGE1_MODEL}
  GUARD_MODEL         ${GUARD_MODEL}
  TOX_BATCH_SIZE      ${TOX_BATCH_SIZE}
  GUARD_CONCURRENCY   ${GUARD_CONCURRENCY}
  VLLM_MAX_MODEL_LEN  ${VLLM_MAX_MODEL_LEN}
  VLLM_IMAGE          ${VLLM_IMAGE}
EOF
}

# parse_args <arg> ... -- one pass over every argument: -h/--help exits
# immediately; screen/verify/all/report set the matching DO_* flag(s) and
# mark STAGE_GIVEN; anything starting with -- is a value flag (see usage
# above); anything else is an error.
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h | --help)
        usage
        exit 0
        ;;
      screen)
        DO_SCREEN=1
        STAGE_GIVEN=1
        shift
        continue
        ;;
      verify)
        DO_VERIFY=1
        STAGE_GIVEN=1
        shift
        continue
        ;;
      all)
        DO_SCREEN=1
        DO_VERIFY=1
        STAGE_GIVEN=1
        shift
        continue
        ;;
      report)
        DO_REPORT=1
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
        --input-root) TOX_INPUT_ROOT="$2" ;;
        --run-dir) TOX_RUN_DIR="$2" ;;
        --input-mode) TOX_INPUT_MODE="$2" ;;
        --partition) PARTITION="$2" ;;
        --cpus) TOX_CPUS="$2" ;;
        --mem) TOX_MEM="$2" ;;
        --time) TOX_TIME="$2" ;;
        --shards) TOX_SHARDS="$2" ;;
        --concurrency) TOX_CONCURRENCY="$2" ;;
        --pilot-files) TOX_PILOT_FILES="$2" ;;
        --threshold) TOX_THRESHOLD="$2" ;;
        --batch-size) TOX_BATCH_SIZE="$2" ;;
        --guard-concurrency) GUARD_CONCURRENCY="$2" ;;
        --max-model-len) VLLM_MAX_MODEL_LEN="$2" ;;
        --vllm-image) VLLM_IMAGE="$2" ;;
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
# arguments from a real terminal. Sets the same DO_SCREEN/DO_VERIFY/DO_REPORT
# flags parse_args would. "pilot" is a menu-only convenience: it's exactly
# 'all' with --pilot-files forced to 3, not a separate CLI stage word.
show_menu() {
  local options=(
    "pilot (both passes on 3 files -- recommended first)"
    "screen"
    "verify"
    "screen + verify (all)"
    "report"
    "quit"
  )
  echo "What would you like to run?"
  PS3="Enter a number (1-${#options[@]}): "
  select choice in "${options[@]}"; do
    case "${REPLY}" in
      1) DO_SCREEN=1; DO_VERIFY=1; TOX_PILOT_FILES=3; break ;;
      2) DO_SCREEN=1; break ;;
      3) DO_VERIFY=1; break ;;
      4) DO_SCREEN=1; DO_VERIFY=1; break ;;
      5) DO_REPORT=1; break ;;
      6) exit 0 ;;
      *) echo "Invalid choice, pick a number 1-${#options[@]}." ;;
    esac
  done
}

# apply_run_dir_default -- fills in TOX_RUN_DIR where a hardcoded default,
# environment variable, or CLI flag hasn't already set it (":=" only assigns
# when the variable is unset or empty). A pilot gets its own folder, so the
# full scan that follows it never lands on top of it.
apply_run_dir_default() {
  if (( TOX_PILOT_FILES > 0 )); then
    : "${TOX_RUN_DIR:=${REPO}/output/tool_team_toxicity_pilot}"
  else
    : "${TOX_RUN_DIR:=${REPO}/output/tool_team_toxicity_run}"
  fi
}

# ---------------------------------------------------------------------------
# Shard-state helpers -- one shard's on-disk layout is fixed by
# run_toxicity_scan_array.sbatch itself: ${TOX_RUN_DIR}/scans/toxicity/shards/shard_NNNNN/
# ---------------------------------------------------------------------------

manifest_path() {
  printf '%s/manifests/manifest.json' "${TOX_RUN_DIR}"
}

read_shard_count() {
  python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['shard_count'])" "$1"
}

shard_dir() {
  printf '%s/scans/toxicity/shards/shard_%05d' "${TOX_RUN_DIR}" "$1"
}

is_screened() { [[ -f "$(shard_dir "$1")/_DETOXIFY_SUCCESS" ]]; }
is_done() { [[ -f "$(shard_dir "$1")/_SUCCESS" ]]; }

join_by_comma() {
  local IFS=,
  echo "$*"
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

# Universal, cheap, always run: catches the run dir being nested inside the
# input root, which would make scan output get picked up as input next run
# (manifest.py discovers every *.jsonl under --input-root, recursively).
preflight_checks() {
  local resolved_input resolved_run
  resolved_input="$(realpath -m -- "${TOX_INPUT_ROOT}")"
  resolved_run="$(realpath -m -- "${TOX_RUN_DIR}")"
  if [[ "${resolved_run}" == "${resolved_input}" || "${resolved_run}" == "${resolved_input}/"* ]]; then
    die "run dir (${TOX_RUN_DIR}) is inside the input root (${TOX_INPUT_ROOT}) -- scan output would be picked up as input on the next run. Choose a --run-dir outside --input-root."
  fi
  if [[ "${TOX_INPUT_MODE}" != "runs" && "${TOX_INPUT_MODE}" != "folder" ]]; then
    die "--input-mode must be 'runs' or 'folder', got '${TOX_INPUT_MODE}'"
  fi
}

# check_identity -- stops this script if TOX_RUN_DIR already holds a different
# run (another mode, other results-changing settings, or changed input files),
# or output from before run identities existed. Nothing is submitted then.
check_identity() {
  local mode="full" state
  if (( TOX_PILOT_FILES > 0 )); then
    mode="pilot"
  fi
  state="$(python3 "${REPO}/modelsafety/contract/run_identity.py" check \
    --run-dir "${TOX_RUN_DIR}" \
    --tool toxicity \
    --mode "${mode}" \
    --input-root "${TOX_INPUT_ROOT}" \
    --input-mode "${TOX_INPUT_MODE}" \
    --setting "input_mode=${TOX_INPUT_MODE}" \
    --setting "pilot_files=${TOX_PILOT_FILES}" \
    --setting "threshold=${TOX_THRESHOLD}" \
    --setting "stage1_model=${TOX_STAGE1_MODEL}" \
    --setting "guard_model=${GUARD_MODEL}" \
    --setting "vllm_max_model_len=${VLLM_MAX_MODEL_LEN}")"
  echo "Run identity: ${state}"
}

# Only needed before actually submitting screen/verify/all.
preflight_for_submit() {
  if [[ ! -d "${TOX_INPUT_ROOT}" ]]; then
    die "input root does not exist: ${TOX_INPUT_ROOT} -- run './scripts/tool_team_data.sh convert' first"
  fi
  if [[ "${DO_VERIFY}" -eq 1 && ! -f "${VLLM_IMAGE}" ]]; then
    die "vLLM image not found: ${VLLM_IMAGE} -- set --vllm-image to a valid SquashFS path"
  fi
}

# Only relevant the first time a --run-dir is used (no manifest yet): make
# sure --shards doesn't exceed how many files/pilot files are actually
# available, so the prepare job can't fail with a confusing error after
# the fact. manifest.py itself would reject --shards > selected file count.
clamp_shards_for_fresh_run() {
  # The files the scan will read: those of finished runs, or every .jsonl.
  # The finder prints why it skipped anything, and fails when nothing is left.
  local file_list file_count=0
  file_list="$(python3 "${REPO}/modelsafety/contract/run_finder.py" files \
    --scan-root "${TOX_INPUT_ROOT}" --input-mode "${TOX_INPUT_MODE}")" \
    || die "nothing to scan under ${TOX_INPUT_ROOT} (see the reason above)"
  if [[ -n "${file_list}" ]]; then
    file_count="$(wc -l <<< "${file_list}")"
  fi
  if (( file_count == 0 )); then
    die "no .jsonl files found under ${TOX_INPUT_ROOT}"
  fi

  local effective="${file_count}"
  if (( TOX_PILOT_FILES > 0 && TOX_PILOT_FILES < effective )); then
    effective="${TOX_PILOT_FILES}"
  fi

  if (( TOX_SHARDS > effective )); then
    echo "note: only ${effective} file(s) available (input has ${file_count} total, pilot-files=${TOX_PILOT_FILES}); reducing --shards from ${TOX_SHARDS} to ${effective}."
    TOX_SHARDS="${effective}"
  fi
}

# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

submit_prepare() {
  sbatch --parsable --partition="${PARTITION}" \
    --job-name="tox_prep" \
    --cpus-per-task=2 --mem=8G --time=01:00:00 \
    --output="${REPO}/logs/slurm/tool_team_toxicity_prep_%j.out" \
    --error="${REPO}/logs/slurm/tool_team_toxicity_prep_%j.err" \
    --export=ALL,REPO="${REPO}",QUALITY_INPUT_ROOT="${TOX_INPUT_ROOT}",QUALITY_INPUT_MODE="${TOX_INPUT_MODE}",QUALITY_MANIFEST_DIR="${TOX_RUN_DIR}/manifests",QUALITY_RUN_ROOT="${TOX_RUN_DIR}",QUALITY_SHARD_COUNT="${TOX_SHARDS}",QUALITY_PILOT_FILES="${TOX_PILOT_FILES}",QUALITY_ALLOW_RERUN=0 \
    "${REPO}/launchers/slurm/prepare_quality_filter.sbatch"
}

# submit_array <stage-label> <TOX_STAGE1_ONLY> <TOX_REUSE_STAGE1> <dependency-job-id-or-empty> <shard-index> ...
submit_array() {
  local stage_label="$1" tox_stage1_only="$2" tox_reuse_stage1="$3" dependency="$4"
  shift 4
  local -a indices=("$@")

  local array_spec
  array_spec="$(join_by_comma "${indices[@]}")%${TOX_CONCURRENCY}"

  local -a dep_args=()
  if [[ -n "${dependency}" ]]; then
    dep_args=(--dependency="afterok:${dependency}")
  fi

  sbatch --parsable --partition="${PARTITION}" \
    --job-name="tox_${stage_label}" \
    --cpus-per-task="${TOX_CPUS}" --mem="${TOX_MEM}" --time="${TOX_TIME}" \
    --output="${REPO}/logs/slurm/tool_team_toxicity_${stage_label}_%A_%a.out" \
    --error="${REPO}/logs/slurm/tool_team_toxicity_${stage_label}_%A_%a.err" \
    --array="${array_spec}" \
    "${dep_args[@]}" \
    --export=ALL,REPO="${REPO}",QUALITY_INPUT_ROOT="${TOX_INPUT_ROOT}",QUALITY_MANIFEST_DIR="${TOX_RUN_DIR}/manifests",QUALITY_RUN_ROOT="${TOX_RUN_DIR}",TOX_STAGE1_ONLY="${tox_stage1_only}",TOX_REUSE_STAGE1="${tox_reuse_stage1}",TOX_THRESHOLD="${TOX_THRESHOLD}",TOX_STAGE1_MODEL="${TOX_STAGE1_MODEL}",GUARD_MODEL="${GUARD_MODEL}",TOX_BATCH_SIZE="${TOX_BATCH_SIZE}",GUARD_CONCURRENCY="${GUARD_CONCURRENCY}",VLLM_MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN}",VLLM_SQUASHFS_PATH="${VLLM_IMAGE}",HF_TOKEN="${HF_TOKEN:-}" \
    "${REPO}/launchers/slurm/run_toxicity_scan_array.sbatch"
}

# print_array_job_commands <job_id> <stage_label> -- the exact,
# copy-pasteable commands to check on a submitted array job: every task at
# once, per-task log naming, accounting, cancellation, where the real
# artifacts land, and how to check on it all once it's done.
print_array_job_commands() {
  local job_id="$1" stage_label="$2"
  echo
  echo "  Status (all shards):  squeue -j ${job_id}"
  echo "  Logs (one per shard): logs/slurm/tool_team_toxicity_${stage_label}_${job_id}_<shard>.out"
  echo "                        (e.g. shard 0: logs/slurm/tool_team_toxicity_${stage_label}_${job_id}_0.out)"
  echo "  Accounting:           sacct -j ${job_id} --format=JobID,State,Elapsed,MaxRSS"
  echo "  Cancel whole job:     scancel ${job_id}"
  echo "  Cancel one shard:     scancel ${job_id}_<shard>"
  echo "  Per-shard artifacts:  ${TOX_RUN_DIR}/scans/toxicity/shards/shard_<00000>/"
  echo "                        (flagged.jsonl, vllm.log, guard_results/, _DETOXIFY_SUCCESS, _SUCCESS)"
  echo "  Once finished:        $(basename "$0") report --run-dir ${TOX_RUN_DIR}"
}

# fresh_run <stage-label> <TOX_STAGE1_ONLY> <TOX_REUSE_STAGE1> -- no manifest
# exists yet at TOX_RUN_DIR: submit the prepare job, then the full array of
# shards depending on it.
fresh_run() {
  local stage_label="$1" tox_stage1_only="$2" tox_reuse_stage1="$3"

  clamp_shards_for_fresh_run

  echo "Input:     ${TOX_INPUT_ROOT}"
  echo "Run dir:   ${TOX_RUN_DIR}"
  echo "Shards:    ${TOX_SHARDS}"
  echo "Partition: ${PARTITION}"
  echo

  local prep_job
  prep_job="$(submit_prepare)"
  echo "Submitted prepare job: ${prep_job}"
  print_job_commands "${prep_job}" \
    "${REPO}/logs/slurm/tool_team_toxicity_prep_${prep_job}.out" \
    "${REPO}/logs/slurm/tool_team_toxicity_prep_${prep_job}.err"

  local -a indices=()
  mapfile -t indices < <(seq 0 $((TOX_SHARDS - 1)))

  local job_id
  job_id="$(submit_array "${stage_label}" "${tox_stage1_only}" "${tox_reuse_stage1}" "${prep_job}" "${indices[@]}")"
  echo
  echo "Submitted ${stage_label} job: ${job_id} (waiting on prepare job ${prep_job})"
  print_array_job_commands "${job_id}" "${stage_label}"
}

run_screen() {
  check_identity
  local manifest
  manifest="$(manifest_path)"
  if [[ ! -f "${manifest}" ]]; then
    fresh_run "screen" 1 0
    return
  fi

  local shard_count
  shard_count="$(read_shard_count "${manifest}")"
  local -a remaining=()
  local i
  for ((i = 0; i < shard_count; i++)); do
    is_screened "$i" || remaining+=("$i")
  done

  if [[ "${#remaining[@]}" -eq 0 ]]; then
    echo "Nothing to do -- all ${shard_count} shard(s) already screened."
    return 0
  fi

  echo "Note: existing run found at ${TOX_RUN_DIR} (${shard_count} shards) -- --shards ignored, resuming."
  echo "Screening: ${#remaining[@]} of ${shard_count} shard(s) (pass 1 only)"
  local job_id
  job_id="$(submit_array screen 1 0 "" "${remaining[@]}")"
  echo "Submitted screen job: ${job_id}"
  print_array_job_commands "${job_id}" "screen"
}

run_verify() {
  local manifest
  manifest="$(manifest_path)"
  if [[ ! -f "${manifest}" ]]; then
    die "no run found at ${TOX_RUN_DIR} -- run 'screen' or 'all' first"
  fi
  check_identity

  local shard_count
  shard_count="$(read_shard_count "${manifest}")"

  local -a ready=() not_ready=()
  local i
  for ((i = 0; i < shard_count; i++)); do
    if is_done "$i"; then
      continue
    elif is_screened "$i"; then
      ready+=("$i")
    else
      not_ready+=("$i")
    fi
  done

  if [[ "${#not_ready[@]}" -gt 0 ]]; then
    die "shard(s) $(join_by_comma "${not_ready[@]}") haven't finished pass 1 yet -- run 'screen' or 'all' on them first"
  fi

  if [[ "${#ready[@]}" -eq 0 ]]; then
    echo "Nothing to do -- all ${shard_count} shard(s) are already fully done."
    return 0
  fi

  echo "Note: existing run found at ${TOX_RUN_DIR} (${shard_count} shards) -- --shards ignored, resuming."
  echo "Verifying: ${#ready[@]} of ${shard_count} shard(s) (pass 2 only, reusing pass 1)"
  local job_id
  job_id="$(submit_array verify 0 1 "" "${ready[@]}")"
  echo "Submitted verify job: ${job_id}"
  print_array_job_commands "${job_id}" "verify"
}

run_all() {
  check_identity
  local manifest
  manifest="$(manifest_path)"
  if [[ ! -f "${manifest}" ]]; then
    fresh_run "all" 0 0
    return
  fi

  local shard_count
  shard_count="$(read_shard_count "${manifest}")"
  local -a remaining=()
  local i
  for ((i = 0; i < shard_count; i++)); do
    is_done "$i" || remaining+=("$i")
  done

  if [[ "${#remaining[@]}" -eq 0 ]]; then
    echo "Nothing to do -- all ${shard_count} shard(s) already fully done."
    return 0
  fi

  echo "Note: existing run found at ${TOX_RUN_DIR} (${shard_count} shards) -- --shards ignored, resuming."
  echo "Running both passes: ${#remaining[@]} of ${shard_count} shard(s) (pass 1 is rerun for these, not reused)"
  local job_id
  job_id="$(submit_array all 0 0 "" "${remaining[@]}")"
  echo "Submitted job: ${job_id}"
  print_array_job_commands "${job_id}" "all"
}

# ---------------------------------------------------------------------------
# Report -- local only, safe at any time.
# ---------------------------------------------------------------------------

run_report() {
  local manifest
  manifest="$(manifest_path)"
  if [[ ! -f "${manifest}" ]]; then
    die "no run found at ${TOX_RUN_DIR} -- nothing has been submitted yet"
  fi

  local shard_count
  shard_count="$(read_shard_count "${manifest}")"

  local done=0 screened_only=0 not_started=0 flagged_total=0 error_total=0
  local -a done_dirs=()
  local i dir flagged_file summary_file n

  for ((i = 0; i < shard_count; i++)); do
    dir="$(shard_dir "$i")"
    flagged_file="${dir}/flagged.jsonl"

    if is_done "$i"; then
      done=$((done + 1))
      done_dirs+=("${dir}/guard_results")
      summary_file="${dir}/guard_results/summary.json"
      if [[ -f "${summary_file}" ]]; then
        n="$(grep -oP '"errors":\s*\K[0-9]+' "${summary_file}" 2>/dev/null || true)"
        error_total=$((error_total + ${n:-0}))
      fi
    elif is_screened "$i"; then
      screened_only=$((screened_only + 1))
    else
      not_started=$((not_started + 1))
    fi

    if [[ -f "${flagged_file}" ]]; then
      n="$(wc -l < "${flagged_file}" 2>/dev/null || true)"
      flagged_total=$((flagged_total + ${n:-0}))
    fi
  done

  echo "Run:    ${TOX_RUN_DIR}"
  echo "Shards: ${shard_count} total -- ${done} fully done, ${screened_only} screened only (pass 2 not run), ${not_started} not started"
  echo "Messages flagged by pass 1 so far: ${flagged_total}"

  if [[ "${error_total}" -gt 0 ]]; then
    echo
    echo "WARNING: ${error_total} guard error(s) across finished shards." >&2
    echo "These count as unsafe downstream. The 8192-token conversation limit is the most likely cause -- check vllm.log under the affected shard folders." >&2
  fi

  if [[ "${#done_dirs[@]}" -eq 0 ]]; then
    echo
    echo "No shards have finished pass 2 yet -- nothing to summarize."
    return 0
  fi

  echo
  echo "=== Guard report (${#done_dirs[@]} finished shard(s)) ==="
  python3 "${REPO}/modelsafety/data_screening/toxicity/guard_report.py" \
    "${done_dirs[@]}" --format rich

  mkdir -p "${TOX_RUN_DIR}/reports"
  python3 "${REPO}/modelsafety/data_screening/toxicity/guard_report.py" \
    "${done_dirs[@]}" --format markdown --output "${TOX_RUN_DIR}/reports/guard_report.md"
  echo
  echo "Saved: ${TOX_RUN_DIR}/reports/guard_report.md"
}

# ---------------------------------------------------------------------------

main() {
  parse_args "$@"

  if [[ "${STAGE_GIVEN}" -eq 0 ]]; then
    if [[ -t 0 ]]; then
      show_menu
      apply_run_dir_default
      pick_partition
      TOX_INPUT_ROOT="$(prompt_value "Input root" "${TOX_INPUT_ROOT}")"
      TOX_RUN_DIR="$(prompt_value "Run dir" "${TOX_RUN_DIR}")"
      review_settings "${REVIEW_PAIRS[@]}"
    else
      usage
      exit 1
    fi
  else
    apply_run_dir_default
  fi

  if [[ "${DO_SCREEN}" -eq 0 && "${DO_VERIFY}" -eq 0 && "${DO_REPORT}" -eq 0 ]]; then
    usage
    exit 1
  fi

  # Resolve to absolute paths before anything else. run_toxicity_scan_array.sbatch
  # cd's into the toxicity module directory before it uses these, so a
  # relative path here would silently resolve against the wrong directory.
  TOX_INPUT_ROOT="$(realpath -m -- "${TOX_INPUT_ROOT}")"
  TOX_RUN_DIR="$(realpath -m -- "${TOX_RUN_DIR}")"
  VLLM_IMAGE="$(realpath -m -- "${VLLM_IMAGE}")"

  preflight_checks

  if [[ "${DO_SCREEN}" -eq 1 || "${DO_VERIFY}" -eq 1 ]]; then
    if [[ -z "${HF_TOKEN:-}" && -f "${REPO}/.env" ]]; then
      set -a
      # shellcheck disable=SC1091
      source "${REPO}/.env"
      set +a
    fi

    preflight_for_submit

    if [[ "${DO_SCREEN}" -eq 1 && "${DO_VERIFY}" -eq 1 ]]; then
      run_all
    elif [[ "${DO_SCREEN}" -eq 1 ]]; then
      run_screen
    else
      run_verify
    fi
  fi

  if [[ "${DO_REPORT}" -eq 1 ]]; then
    echo
    echo "=== Current state ==="
    run_report
  fi
}

main "$@"
