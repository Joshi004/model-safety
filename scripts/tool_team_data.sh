#!/bin/bash
# Convert the tool team's three datasets (SFT mix, MOPD mix, RL mix) into the
# JSONL shape modelsafety's scanners expect, and run the regex PII pipeline
# against the result.
#
# Usage:
#   ./scripts/tool_team_data.sh [stage ...] [--flag value ...]
#   ./scripts/tool_team_data.sh                 # no stages -- interactive menu
#
# Stages (pass one or more, in any order -- duplicates are harmless):
#   convert   Convert the SFT mix, MOPD mix, and RL mix to JSONL.
#   scan      Submit the regex PII scan (stage 1 + stage 2).
#   extract   Submit the source-row extractor (stage 3).
#   all       Shorthand for: convert scan extract
#
# Flags (each overrides the matching setting below, for this run only):
#   --sft-mix-path PATH   --mopd-mix-path PATH   --rl-mix-path PATH
#   --output-root PATH     --pii-run-dir PATH      --partition NAME
#   --cpus N               --mem SIZE              --time HH:MM:SS
#   --workers N            --chunk-size N
#
# Examples:
#   ./scripts/tool_team_data.sh convert
#   ./scripts/tool_team_data.sh scan extract --partition toolCall
#   ./scripts/tool_team_data.sh all
#
# Precedence for every setting, lowest to highest: hardcoded default in this
# file < environment variable < CLI flag above < interactive edit (only
# offered when no stage word is given at all, so scripted/automated calls
# never prompt).
#
# The GPU-based NER-model scan is a separate script on purpose -- see
# scripts/tool_team_ner_scan.sh.

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

# --- Source datasets (inputs to `convert`) --------------------------------
SFT_MIX_PATH="${SFT_MIX_PATH:-/home/shared/agentic_slm/data/sft/processed/qwen3_5/s1_2_0824}"
MOPD_MIX_PATH="${MOPD_MIX_PATH:-/home/shared/agentic_slm/data/rl/processed/qwen35-xml/decontaminated/mopd_mixv4_0907}"
RL_MIX_PATH="${RL_MIX_PATH:-/home/shared/agentic_slm/data/rl/processed/qwen35-xml/decontaminated/rl_mix_v12_ifmt_0903}"

# --- Output locations ------------------------------------------------------
OUTPUT_ROOT="${OUTPUT_ROOT:-${REPO}/output/tool_team_converted}"   # `convert` writes here, `scan` reads here
PII_RUN_DIR="${PII_RUN_DIR:-${REPO}/output/tool_team_pii_run}"     # `scan` and `extract` write here

# --- SLURM resources for the scan/extract job -------------------------------
PARTITION="${PARTITION:-health}"        # main | health | toolCall | VLM -- pick_partition lists the real, live set
SCAN_CPUS="${SCAN_CPUS:-32}"
SCAN_MEM="${SCAN_MEM:-64G}"
SCAN_TIME="${SCAN_TIME:-06:00:00}"

# --- Scanner tuning ----------------------------------------------------------
PII_NUM_WORKERS="${PII_NUM_WORKERS:-32}"    # keep roughly equal to SCAN_CPUS
PII_CHUNK_SIZE="${PII_CHUNK_SIZE:-10000}"   # lines per work chunk

# --- Tooling (rarely changed) ------------------------------------------------
READERS_PY="${REPO}/modelsafety/readers/.venv/bin/python"
PREPARE_ROWS="${REPO}/modelsafety/readers/prepare_rows.py"

# Name/label pairs for the interactive review screen -- see review_settings
# in scripts/lib/tool_team_common.sh.
REVIEW_PAIRS=(
  SFT_MIX_PATH "SFT mix path"
  MOPD_MIX_PATH "MOPD mix path"
  RL_MIX_PATH "RL mix path"
  OUTPUT_ROOT "Output root"
  PII_RUN_DIR "PII run dir"
  PARTITION "Partition"
  SCAN_CPUS "Scan CPUs"
  SCAN_MEM "Scan memory"
  SCAN_TIME "Scan time limit"
  PII_NUM_WORKERS "PII workers"
  PII_CHUNK_SIZE "PII chunk size"
)

# --- Which stages were requested, and whether any word (stage or action)
# was given at all -- STAGE_GIVEN=0 is what triggers the interactive path.
DO_CONVERT=0
DO_SCAN=0
DO_EXTRACT=0
STAGE_GIVEN=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [stage ...] [--flag value ...]
       $(basename "$0")                 # no stages -- interactive menu

Stages (pass one or more, in any order; duplicates are harmless):
  convert   Convert the SFT mix, MOPD mix, and RL mix to JSONL under
            \$OUTPUT_ROOT (all three run in parallel, locally, ~40s).
  scan      Submit the regex PII scan (stage 1 + stage 2) against
            \$OUTPUT_ROOT. Requires 'convert' to have already run (or be
            requested in this same command).
  extract   Submit the source-row extractor (stage 3) against
            \$PII_RUN_DIR. Requires 'scan' to have already run (or be
            requested in this same command).
  all       Shorthand for: convert scan extract

Flags (each overrides the matching setting for this run only):
  --sft-mix-path PATH    --mopd-mix-path PATH   --rl-mix-path PATH
  --output-root PATH     --pii-run-dir PATH      --partition NAME
  --cpus N               --mem SIZE              --time HH:MM:SS
  --workers N            --chunk-size N

Precedence, lowest to highest: hardcoded default in this file < environment
variable < CLI flag < interactive edit. Interactive editing is only offered
when no stage word is given at all -- passing any stage word runs fully
non-interactively.

'scan' and 'extract' always submit one sbatch job and return immediately --
they do not wait for the job to finish. 'convert' runs locally and blocks
until done.

Examples:
  $(basename "$0") convert
  $(basename "$0") scan extract --partition toolCall
  $(basename "$0") all

For the GPU-based NER-model scan, see scripts/tool_team_ner_scan.sh instead
-- it is a separate script on purpose (different resources, different risk
profile: minutes to a week, 0 GPUs vs up to 8).

Current settings:
  SFT_MIX_PATH    ${SFT_MIX_PATH}
  MOPD_MIX_PATH   ${MOPD_MIX_PATH}
  RL_MIX_PATH     ${RL_MIX_PATH}
  OUTPUT_ROOT     ${OUTPUT_ROOT}
  PII_RUN_DIR     ${PII_RUN_DIR}
  PARTITION       ${PARTITION}
  SCAN_CPUS       ${SCAN_CPUS}
  SCAN_MEM        ${SCAN_MEM}
  SCAN_TIME       ${SCAN_TIME}
  PII_NUM_WORKERS ${PII_NUM_WORKERS}
  PII_CHUNK_SIZE  ${PII_CHUNK_SIZE}
EOF
}

# parse_args <arg> ... -- one pass over every argument: -h/--help exits
# immediately; convert/scan/extract/all set the matching DO_* flag and mark
# STAGE_GIVEN; anything starting with -- is a value flag (see usage above);
# anything else is an error.
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h | --help)
        usage
        exit 0
        ;;
      convert)
        DO_CONVERT=1
        STAGE_GIVEN=1
        shift
        continue
        ;;
      scan)
        DO_SCAN=1
        STAGE_GIVEN=1
        shift
        continue
        ;;
      extract)
        DO_EXTRACT=1
        STAGE_GIVEN=1
        shift
        continue
        ;;
      all)
        DO_CONVERT=1
        DO_SCAN=1
        DO_EXTRACT=1
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
        --sft-mix-path) SFT_MIX_PATH="$2" ;;
        --mopd-mix-path) MOPD_MIX_PATH="$2" ;;
        --rl-mix-path) RL_MIX_PATH="$2" ;;
        --output-root) OUTPUT_ROOT="$2" ;;
        --pii-run-dir) PII_RUN_DIR="$2" ;;
        --partition) PARTITION="$2" ;;
        --cpus) SCAN_CPUS="$2" ;;
        --mem) SCAN_MEM="$2" ;;
        --time) SCAN_TIME="$2" ;;
        --workers) PII_NUM_WORKERS="$2" ;;
        --chunk-size) PII_CHUNK_SIZE="$2" ;;
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
# arguments from a real terminal. Sets the same DO_CONVERT/DO_SCAN/DO_EXTRACT
# flags parse_args would.
show_menu() {
  local options=(
    "convert"
    "scan"
    "extract"
    "convert + scan"
    "scan + extract"
    "convert + scan + extract (all)"
    "quit"
  )
  echo "What would you like to run?"
  PS3="Enter a number (1-${#options[@]}): "
  select choice in "${options[@]}"; do
    case "${REPLY}" in
      1) DO_CONVERT=1; break ;;
      2) DO_SCAN=1; break ;;
      3) DO_EXTRACT=1; break ;;
      4) DO_CONVERT=1; DO_SCAN=1; break ;;
      5) DO_SCAN=1; DO_EXTRACT=1; break ;;
      6) DO_CONVERT=1; DO_SCAN=1; DO_EXTRACT=1; break ;;
      7) exit 0 ;;
      *) echo "Invalid choice, pick a number 1-${#options[@]}." ;;
    esac
  done
}

preflight_checks() {
  if [[ ! -x "${READERS_PY}" ]]; then
    echo "error: reader venv not found at ${READERS_PY}" >&2
    echo "Build it first (see HOW_TO_SCAN_YOUR_DATA.md):" >&2
    echo "  cd ${REPO}/modelsafety/readers" >&2
    echo "  python3 -m venv .venv" >&2
    echo "  .venv/bin/pip install -U pip wheel" >&2
    echo "  .venv/bin/pip install -r requirements.txt" >&2
    exit 2
  fi
  local path
  for path in "${SFT_MIX_PATH}" "${MOPD_MIX_PATH}" "${RL_MIX_PATH}"; do
    if [[ ! -d "${path}" ]]; then
      echo "error: dataset path does not exist: ${path}" >&2
      exit 2
    fi
  done
}

# convert_one <input-path> <input-type> <output-dir> <log-file> [extra prepare_rows.py args...]
# Launches one backgrounded prepare_rows.py call. Every row shape here needs
# a "messages" column to be present and non-empty, so --require messages is
# always passed; callers add --map messages=<source column> on top when the
# source doesn't already call it "messages" (parquet's "prompt" column).
convert_one() {
  local input_path="$1" input_type="$2" output_dir="$3" log_file="$4"
  shift 4
  mkdir -p "$(dirname "${log_file}")"
  PYTHONPATH="${REPO}" "${READERS_PY}" "${PREPARE_ROWS}" \
    --input-path "${input_path}" --input-type "${input_type}" \
    --require messages "$@" \
    --output-dir "${output_dir}" > "${log_file}" 2>&1 &
}

# sum_rows <log-file> <log-file> ... -- total the "Prepared N rows" line
# across a set of job logs. Tolerant of missing/empty logs: contributes 0.
sum_rows() {
  local total=0 n f
  for f in "$@"; do
    [[ -f "${f}" ]] || continue
    n="$(grep -oP 'Prepared \K[0-9]+' "${f}" 2>/dev/null || true)"
    total=$((total + ${n:-0}))
  done
  echo "${total}"
}

print_summary() {
  local log_dir="$1" elapsed="$2"

  local sft_rows mopd_rows rl_rows total_rows
  sft_rows="$(sum_rows "${log_dir}"/sft_mix_*.log)"
  mopd_rows="$(sum_rows "${log_dir}"/mopd_mix_*.log)"
  rl_rows="$(sum_rows "${log_dir}"/rl_mix_*.log)"
  total_rows=$((sft_rows + mopd_rows + rl_rows))

  echo
  echo "=== Conversion complete (${elapsed}s) ==="
  printf "%-10s %10s rows -> %s\n" "SFT mix:" "${sft_rows}" "${OUTPUT_ROOT}/sft_mix/"
  printf "%-10s %10s rows -> %s\n" "MOPD mix:" "${mopd_rows}" "${OUTPUT_ROOT}/mopd_mix/"
  printf "%-10s %10s rows -> %s\n" "RL mix:" "${rl_rows}" "${OUTPUT_ROOT}/rl_mix/"
  printf "%-10s %10s rows\n" "Total:" "${total_rows}"

  echo
  echo "Logs: ${log_dir}/"
  echo "  Tail one job:  tail -f ${log_dir}/sft_mix_<name>.log   (or mopd_mix_<split>.log / rl_mix_<split>.log)"
  echo "  Find failures: grep -l FAILED ${log_dir}/*.log"

  local failed
  failed="$(grep -l "FAILED" "${log_dir}"/*.log 2>/dev/null || true)"
  if [[ -n "${failed}" ]]; then
    echo
    echo "WARNING: some jobs reported FAILED -- check these logs:" >&2
    echo "${failed}" >&2
    exit 2
  fi
}

run_convert() {
  preflight_checks

  local log_dir="${OUTPUT_ROOT}/_logs"
  mkdir -p "${log_dir}"

  echo "Converting:"
  echo "  SFT mix:  ${SFT_MIX_PATH}"
  echo "  MOPD mix: ${MOPD_MIX_PATH}"
  echo "  RL mix:   ${RL_MIX_PATH}"
  echo "Output:     ${OUTPUT_ROOT}"
  echo

  local start_time
  start_time=$(date +%s)

  # --- SFT mix -----------------------------------------------------------
  # 61 independent sub-dataset folders under train_shards_keep/. Each one's
  # HuggingFace dataset already stores the conversation under a column
  # literally called "messages" -- no --map needed.
  local sft_shards="${SFT_MIX_PATH}/train_shards_keep"
  local sft_count=0
  local d name
  for d in "${sft_shards}"/*/; do
    [[ -d "${d}" ]] || continue
    name="$(basename "${d}")"
    convert_one "${d}" "huggingface" "${OUTPUT_ROOT}/sft_mix/${name}" \
      "${log_dir}/sft_mix_${name}.log"
    sft_count=$((sft_count + 1))
  done

  # --- MOPD mix and RL mix ------------------------------------------------
  # Both store the conversation under a column called "prompt" (already
  # role/content-shaped) -- one rename away from "messages".
  local f
  for f in train test; do
    convert_one "${MOPD_MIX_PATH}/${f}.parquet" "parquet" \
      "${OUTPUT_ROOT}/mopd_mix/${f}" "${log_dir}/mopd_mix_${f}.log" \
      --map messages=prompt
    convert_one "${RL_MIX_PATH}/${f}.parquet" "parquet" \
      "${OUTPUT_ROOT}/rl_mix/${f}" "${log_dir}/rl_mix_${f}.log" \
      --map messages=prompt
  done

  echo "Launched ${sft_count} SFT sub-dataset jobs + 4 MOPD/RL jobs, waiting for all to finish..."
  wait

  local end_time elapsed
  end_time=$(date +%s)
  elapsed=$((end_time - start_time))

  print_summary "${log_dir}" "${elapsed}"
}

# Submits one sbatch job covering whichever of scan (stage 1+2) and extract
# (stage 3) were requested, via PII_STAGES. Always returns immediately --
# regex scanning must go through sbatch, never directly on the login node.
run_scan_and_extract() {
  local stages
  if [[ "${DO_SCAN}" -eq 1 && "${DO_EXTRACT}" -eq 1 ]]; then
    stages="all"
  elif [[ "${DO_SCAN}" -eq 1 ]]; then
    stages="scan"
  else
    stages="extract"
  fi

  # Stage-aware preflight: only require earlier output to already exist if
  # the stage that produces it wasn't ALSO requested in this same command.
  if [[ "${DO_SCAN}" -eq 1 && "${DO_CONVERT}" -eq 0 && ! -d "${OUTPUT_ROOT}" ]]; then
    echo "error: ${OUTPUT_ROOT} does not exist -- run '$(basename "$0") convert' first" >&2
    exit 2
  fi
  if [[ "${DO_EXTRACT}" -eq 1 && "${DO_SCAN}" -eq 0 && ! -d "${PII_RUN_DIR}/stage2_validated/hits" ]]; then
    echo "error: ${PII_RUN_DIR}/stage2_validated/hits does not exist -- run '$(basename "$0") scan' first" >&2
    exit 2
  fi

  echo "Scanning:  ${OUTPUT_ROOT}"
  echo "Run dir:   ${PII_RUN_DIR}"
  echo "Stages:    ${stages}"
  echo "Partition: ${PARTITION}"
  echo

  local job_id
  job_id="$(
    sbatch --parsable --partition="${PARTITION}" --job-name="pii_${stages}" \
      --cpus-per-task="${SCAN_CPUS}" --mem="${SCAN_MEM}" --time="${SCAN_TIME}" \
      --export=ALL,PII_BASE_DIR="${OUTPUT_ROOT}",PII_RUN_DIR="${PII_RUN_DIR}",PII_STAGES="${stages}",PII_NUM_WORKERS="${PII_NUM_WORKERS}",PII_CHUNK_SIZE="${PII_CHUNK_SIZE}" \
      "${REPO}/launchers/slurm/run_pii_scan_and_extract.sbatch"
  )"

  echo "Submitted PII job (stages=${stages}): ${job_id}"

  local log_root="${REPO}/logs/scans/pii_scan_and_extract/run_${job_id}"
  print_job_commands "${job_id}" \
    "${log_root}/main/pii_scan_and_extract_${job_id}.log" \
    "${log_root}/err/pii_scan_and_extract_${job_id}.err"

  echo
  echo "Results, once finished:"
  if [[ "${stages}" == "scan" || "${stages}" == "all" ]]; then
    echo "  ${PII_RUN_DIR}/stage1_scan/metrics.txt"
    echo "  ${PII_RUN_DIR}/stage2_validated/metrics.txt"
  fi
  if [[ "${stages}" == "extract" || "${stages}" == "all" ]]; then
    echo "  ${PII_RUN_DIR}/stage3_extract/pii_extract.csv"
  fi
}

main() {
  parse_args "$@"

  if [[ "${STAGE_GIVEN}" -eq 0 ]]; then
    if [[ -t 0 ]]; then
      show_menu
      pick_partition
      OUTPUT_ROOT="$(prompt_value "Output root" "${OUTPUT_ROOT}")"
      PII_RUN_DIR="$(prompt_value "PII run dir" "${PII_RUN_DIR}")"
      review_settings "${REVIEW_PAIRS[@]}"
    else
      usage
      exit 1
    fi
  fi

  if [[ "${DO_CONVERT}" -eq 0 && "${DO_SCAN}" -eq 0 && "${DO_EXTRACT}" -eq 0 ]]; then
    usage
    exit 1
  fi

  if [[ "${DO_CONVERT}" -eq 1 ]]; then
    run_convert
  fi

  if [[ "${DO_SCAN}" -eq 1 || "${DO_EXTRACT}" -eq 1 ]]; then
    run_scan_and_extract
  fi
}

main "$@"
