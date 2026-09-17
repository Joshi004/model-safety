#!/bin/bash
#SBATCH --job-name=toxicity_scan
#SBATCH --partition=health
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=48G
#SBATCH --time=04:00:00
#SBATCH --output=synth_data_gen/logs/slurm/toxicity_scan_%j.out
#SBATCH --error=synth_data_gen/logs/slurm/toxicity_scan_%j.err

set -euo pipefail

export PATH=/usr/bin:/bin:/usr/local/bin:$PATH

REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}/synth_data_gen}"
TOX_DIR="${REPO}/tools/data_prep/toxicity"
VENV="${TOX_DIR}/.venv"

TOX_INPUT="${TOX_INPUT:-${REPO}/tmp/qa_scan_test/input/data_000000_messages_pipeline.jsonl}"
TOX_RUN_DIR="${TOX_RUN_DIR:-${REPO}/tmp/qa_scan_test/toxicity_out}"
TOX_FLAGGED_FILE="${TOX_FLAGGED_FILE:-${TOX_RUN_DIR}/flagged.jsonl}"
TOX_GUARD_DIR="${TOX_GUARD_DIR:-${TOX_RUN_DIR}/guard_results}"

RUN_ID="${SLURM_ARRAY_JOB_ID:-${SLURM_JOB_ID:-local}}"
TOX_LOG_ROOT="${TOX_LOG_ROOT:-${REPO}/logs/scans/toxicity_scan/run_${RUN_ID}}"
TOX_MAIN_LOG="${TOX_LOG_ROOT}/main/toxicity_scan_${RUN_ID}.log"
TOX_ERR_LOG="${TOX_LOG_ROOT}/err/toxicity_scan_${RUN_ID}.err"
TOX_STAGE1_LOG="${TOX_LOG_ROOT}/stage1/detoxify_${RUN_ID}.log"
TOX_STAGE2_LOG="${TOX_LOG_ROOT}/stage2/qwen3guard_${RUN_ID}.log"
TOX_REPORT_FILE="${TOX_REPORT_FILE:-${TOX_LOG_ROOT}/reports/guard_report.txt}"

TOX_STAGE1_MODEL="${TOX_STAGE1_MODEL:-multilingual}"
TOX_BATCH_SIZE="${TOX_BATCH_SIZE:-32}"
TOX_THRESHOLD="${TOX_THRESHOLD:-0.5}"

GUARD_MODEL="${GUARD_MODEL:-Qwen/Qwen3Guard-Gen-8B}"
GUARD_PORT="${GUARD_PORT:-8000}"
GUARD_BASE_URL="${GUARD_BASE_URL:-http://127.0.0.1:${GUARD_PORT}/v1}"
GUARD_CONCURRENCY="${GUARD_CONCURRENCY:-8}"
GUARD_MAX_TOKENS="${GUARD_MAX_TOKENS:-128}"

VLLM_SQUASHFS_PATH="${VLLM_SQUASHFS_PATH:-${REPO}/containers/vllm-openai-v0.24.0-cu129.sqsh}"
VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.9}"
VLLM_MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-8192}"
VLLM_ENABLE_CUDA_COMPATIBILITY="${VLLM_ENABLE_CUDA_COMPATIBILITY:-1}"
VLLM_LOG="${TOX_LOG_ROOT}/vllm/vllm_guard_${RUN_ID}.log"

HF_HOME="${HF_HOME:-${REPO}/cache/hf}"
HF_HUB_CACHE="${HF_HUB_CACHE:-${HF_HOME}/hub}"
HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"

VLLM_PID=""

log() {
  printf '[%(%Y-%m-%d %H:%M:%S)T] %s\n' -1 "$*"
}

setup_logging() {
  mkdir -p \
    "${REPO}/logs/slurm" \
    "${TOX_LOG_ROOT}/main" \
    "${TOX_LOG_ROOT}/err" \
    "${TOX_LOG_ROOT}/vllm" \
    "${TOX_LOG_ROOT}/stage1" \
    "${TOX_LOG_ROOT}/stage2" \
    "${TOX_LOG_ROOT}/reports"
  exec >>"${TOX_MAIN_LOG}" 2>>"${TOX_ERR_LOG}"
}

cleanup() {
  if [[ -n "${VLLM_PID}" ]] && kill -0 "${VLLM_PID}" 2>/dev/null; then
    log "Stopping vLLM server (pid=${VLLM_PID})"
    kill "${VLLM_PID}" 2>/dev/null || true
    wait "${VLLM_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

ensure_venv() {
  cd "${TOX_DIR}"
  if [[ ! -x "${VENV}/bin/python" ]]; then
    log "Creating toxicity venv at ${VENV}"
    python3 -m venv "${VENV}"
    "${VENV}/bin/pip" install -U pip wheel
    "${VENV}/bin/pip" install -r requirements.txt
  fi
  # SentencePiece is required by the multilingual Detoxify tokenizer.
  if ! "${VENV}/bin/python" - <<'PY'
import importlib.util
raise SystemExit(0 if importlib.util.find_spec("sentencepiece") else 1)
PY
  then
    "${VENV}/bin/pip" install sentencepiece
  fi
}

wait_for_vllm() {
  local deadline=$((SECONDS + ${VLLM_STARTUP_TIMEOUT:-900}))
  while (( SECONDS < deadline )); do
    if "${VENV}/bin/python" - <<PY >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("http://127.0.0.1:${GUARD_PORT}/health", timeout=5).read()
PY
    then
      log "vLLM health check OK"
      return 0
    fi

    if [[ -n "${VLLM_PID}" ]] && ! kill -0 "${VLLM_PID}" 2>/dev/null; then
      log "vLLM process exited before becoming healthy"
      return 1
    fi

    sleep 5
  done

  log "Timed out waiting for vLLM health endpoint"
  return 1
}

start_vllm() {
  if [[ ! -f "${VLLM_SQUASHFS_PATH}" ]]; then
    log "FATAL: vLLM SquashFS image not found: ${VLLM_SQUASHFS_PATH}"
    return 1
  fi

  mkdir -p "${HF_HOME}" "${HF_HUB_CACHE}" "${TOX_LOG_ROOT}/vllm"
  export HF_HOME HF_HUB_CACHE HF_HUB_DISABLE_XET VLLM_ENABLE_CUDA_COMPATIBILITY

  log "Starting vLLM guard server"
  log "  model: ${GUARD_MODEL}"
  log "  image: ${VLLM_SQUASHFS_PATH}"
  log "  log:   ${VLLM_LOG}"

  srun \
    --ntasks=1 \
    --container-image="${VLLM_SQUASHFS_PATH}" \
    --container-mounts="${HF_HOME}:${HF_HOME}" \
    --container-workdir=/tmp \
    --container-env=HF_HOME,HF_HUB_CACHE,HF_HUB_DISABLE_XET,VLLM_ENABLE_CUDA_COMPATIBILITY \
    --no-container-entrypoint \
    /usr/bin/python3 -m vllm.entrypoints.openai.api_server \
      --host 127.0.0.1 \
      --port "${GUARD_PORT}" \
      --model "${GUARD_MODEL}" \
      --gpu-memory-utilization "${VLLM_GPU_MEMORY_UTILIZATION}" \
      --max-model-len "${VLLM_MAX_MODEL_LEN}" \
      --trust-remote-code \
    > "${VLLM_LOG}" 2>&1 &

  VLLM_PID=$!
  wait_for_vllm || {
    log "FATAL: vLLM failed to start. Last 80 log lines:"
    tail -n 80 "${VLLM_LOG}" || true
    return 1
  }
}

run_stage1() {
  log "Stage 1: Detoxify screen"
  mkdir -p "$(dirname "${TOX_FLAGGED_FILE}")"
  cd "${TOX_DIR}"
  source "${VENV}/bin/activate"
  python run.py \
    --input "${TOX_INPUT}" \
    --model_name "${TOX_STAGE1_MODEL}" \
    --dest_file "${TOX_FLAGGED_FILE}" \
    --batch_size "${TOX_BATCH_SIZE}" \
    --threshold "${TOX_THRESHOLD}" \
    2>&1 | tee "${TOX_STAGE1_LOG}"
}

run_stage2() {
  log "Stage 2: Qwen3Guard verification"
  rm -rf "${TOX_GUARD_DIR}"
  mkdir -p "${TOX_GUARD_DIR}"
  cd "${TOX_DIR}"
  source "${VENV}/bin/activate"
  python llm_check_guard.py \
    --input "${TOX_FLAGGED_FILE}" \
    --output "${TOX_GUARD_DIR}" \
    --base_url "${GUARD_BASE_URL}" \
    --model "${GUARD_MODEL}" \
    --concurrency "${GUARD_CONCURRENCY}" \
    --max_tokens "${GUARD_MAX_TOKENS}" \
    2>&1 | tee "${TOX_STAGE2_LOG}"
}

write_report() {
  log "Report: ${TOX_REPORT_FILE}"
  cd "${TOX_DIR}"
  source "${VENV}/bin/activate"
  python guard_report.py "${TOX_GUARD_DIR}" --format rich \
    2>&1 | tee "${TOX_REPORT_FILE}"
}

main() {
  setup_logging
  mkdir -p "${TOX_RUN_DIR}"
  ensure_venv

  log "============================================================"
  log "Toxicity scan (stage 1 + vLLM + stage 2)"
  log "============================================================"
  log "Node:          ${SLURMD_NODENAME:-unknown}"
  log "Input:         ${TOX_INPUT}"
  log "Run dir:       ${TOX_RUN_DIR}"
  log "Flagged file:  ${TOX_FLAGGED_FILE}"
  log "Guard output:  ${TOX_GUARD_DIR}"
  log "Report:        ${TOX_REPORT_FILE}"
  log "Log root:      ${TOX_LOG_ROOT}"
  log "Main log:      ${TOX_MAIN_LOG}"
  log "Error log:     ${TOX_ERR_LOG}"
  log "vLLM log:      ${VLLM_LOG}"
  log "Threshold:     ${TOX_THRESHOLD}"
  log "Guard model:   ${GUARD_MODEL}"
  log

  run_stage1

  if [[ ! -s "${TOX_FLAGGED_FILE}" ]]; then
    log "No stage-1 flagged rows. Skipping vLLM and stage 2."
    mkdir -p "${TOX_GUARD_DIR}"
    : > "${TOX_GUARD_DIR}/all.jsonl"
    : > "${TOX_GUARD_DIR}/safe.jsonl"
    : > "${TOX_GUARD_DIR}/unsafe.jsonl"
    : > "${TOX_GUARD_DIR}/controversial.jsonl"
    write_report
    return 0
  fi

  log "Stage-1 flagged rows: $(wc -l < "${TOX_FLAGGED_FILE}")"
  start_vllm
  run_stage2
  write_report

  log "Done"
}

main "$@"
