#!/bin/bash
#SBATCH --job-name=privacy_filter_llm
#SBATCH --partition=health
#SBATCH --gres=gpu:nvidia_h100_80gb_hbm3:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=128G
#SBATCH --time=72:00:00
#SBATCH --output=logs/slurm/privacy_filter_llm_%j.out
#SBATCH --error=logs/slurm/privacy_filter_llm_%j.err

set -euo pipefail

REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
PIPELINE_DIR="${REPO}/modelsafety/data_screening/pii/model"
RUN_ROOT="${PRIVACY_FILTER_RUN_ROOT:?PRIVACY_FILTER_RUN_ROOT is required}"
INPUT="${PRIVACY_FILTER_LLM_INPUT:-${RUN_ROOT}/llm_candidates.csv}"
MODEL="${PRIVACY_FILTER_LLM_MODEL:-google/gemma-4-31B-it}"
VENV="${PRIVACY_FILTER_VENV:-${PIPELINE_DIR}/.venv-isolated}"
VLLM_IMAGE="${PRIVACY_FILTER_LLM_SQUASHFS_PATH:-${REPO}/containers/vllm-openai-v0.24.0-cu129.sqsh}"
HF_HOME="${HF_HOME:-${REPO}/cache/hf}"
PORT="${PRIVACY_FILTER_LLM_PORT:-$((20000 + SLURM_JOB_ID % 20000))}"
LOG_DIR="${RUN_ROOT}/llm_validation"
VLLM_PID=""

cleanup() {
  if [[ -n "${VLLM_PID}" ]]; then
    kill "${VLLM_PID}" 2>/dev/null || true
    wait "${VLLM_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if [[ ! -f "${INPUT}" ]]; then
  echo "Missing candidate CSV: ${INPUT}" >&2
  exit 2
fi
if [[ ! -x "${VENV}/bin/python" ]]; then
  echo "Missing Privacy Filter environment: ${VENV}" >&2
  exit 2
fi
mkdir -p "${REPO}/logs/slurm" "${LOG_DIR}" "${HF_HOME}"
if [[ -z "${HF_TOKEN:-}" && -f "${REPO}/.env" ]]; then
  set -a
  source "${REPO}/.env"
  set +a
fi
export HF_HOME HF_TOKEN="${HF_TOKEN:-}"

srun --ntasks=1 \
  --container-image="${VLLM_IMAGE}" \
  --container-mounts="${HF_HOME}:${HF_HOME}" \
  --container-workdir="${HF_HOME}" \
  --container-env=HF_HOME,HF_TOKEN \
  --no-container-entrypoint \
  /usr/bin/python3 -m vllm.entrypoints.openai.api_server \
    --host 127.0.0.1 \
    --port "${PORT}" \
    --model "${MODEL}" \
    --served-model-name "${MODEL}" \
    --dtype bfloat16 \
    --gpu-memory-utilization "${VLLM_GPU_MEMORY_UTILIZATION:-0.92}" \
    --max-model-len "${PRIVACY_FILTER_LLM_MAX_MODEL_LEN:-8192}" \
    --max-num-seqs "${PRIVACY_FILTER_LLM_CONCURRENCY:-64}" \
    --trust-remote-code \
  >"${LOG_DIR}/vllm.log" 2>&1 &
VLLM_PID=$!

deadline=$((SECONDS + ${VLLM_STARTUP_TIMEOUT:-1800}))
while ! curl --silent --fail "http://127.0.0.1:${PORT}/health" >/dev/null; do
  if ! kill -0 "${VLLM_PID}" 2>/dev/null; then
    echo "vLLM exited during startup; inspect ${LOG_DIR}/vllm.log" >&2
    exit 1
  fi
  if ((SECONDS >= deadline)); then
    echo "Timed out waiting for vLLM" >&2
    exit 1
  fi
  sleep 10
done

cd "${PIPELINE_DIR}"
"${VENV}/bin/python" llm_validate.py \
  --input "${INPUT}" \
  --output-dir "${LOG_DIR}" \
  --base-url "http://127.0.0.1:${PORT}/v1" \
  --model "${MODEL}" \
  --concurrency "${PRIVACY_FILTER_LLM_CONCURRENCY:-64}" \
  --max-pending "${PRIVACY_FILTER_LLM_MAX_PENDING:-256}" \
  --max-context-chars "${PRIVACY_FILTER_LLM_MAX_CONTEXT_CHARS:-10000}" \
  --max-tokens "${PRIVACY_FILTER_LLM_MAX_TOKENS:-256}" \
  --resume

