#!/bin/bash
#SBATCH --job-name=medpsy_guard_pilot
#SBATCH --partition=health
#SBATCH --gres=gpu:nvidia_h100_80gb_hbm3:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=128G
#SBATCH --time=08:00:00
#SBATCH --output=logs/slurm/medpsy_guard_pilot_%j.out
#SBATCH --error=logs/slurm/medpsy_guard_pilot_%j.err

set -euo pipefail

REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
TOX_DIR="${REPO}/modelsafety/data_screening/toxicity"
VENV="${TOX_DIR}/.venv"
RUN_ROOT="${QUALITY_RUN_ROOT:-${REPO}/output/medpsy2/pii_toxicity_cleaning}"
PILOT_ROOT="${MEDICAL_GUARD_PILOT_ROOT:-${REPO}/output/medpsy2/medical_guard_pilot_v1}"
QWEN_SAMPLE="${MEDICAL_GUARD_QWEN_SAMPLE:-${REPO}/tmp/qwen_guard_non_safe_review_sample.jsonl}"
SEXUAL_SAMPLE="${MEDICAL_GUARD_SEXUAL_SAMPLE:-${REPO}/tmp/detoxify_sexual_review_sample.jsonl}"
INPUT="${PILOT_ROOT}/samples/hydrated.jsonl"
GRANITE_OUTPUT="${PILOT_ROOT}/granite/all.jsonl"
GEMMA_OUTPUT="${PILOT_ROOT}/gemma/all.jsonl"
GRANITE_MODEL="${GRANITE_MODEL:-ibm-granite/granite-guardian-4.1-8b}"
GEMMA_MODEL="${GEMMA_MODEL:-google/gemma-4-31B-it}"
PORT="${MEDICAL_GUARD_PORT:-$((20000 + SLURM_JOB_ID % 20000))}"
BASE_URL="http://127.0.0.1:${PORT}/v1"
CONCURRENCY="${MEDICAL_GUARD_CONCURRENCY:-8}"
VLLM_SQUASHFS_PATH="${VLLM_SQUASHFS_PATH:-${REPO}/containers/vllm-openai-v0.24.0-cu129.sqsh}"
HF_HOME="${HF_HOME:-${REPO}/cache/hf}"
HF_HUB_CACHE="${HF_HUB_CACHE:-${HF_HOME}/hub}"
HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"
VLLM_ENABLE_CUDA_COMPATIBILITY="${VLLM_ENABLE_CUDA_COMPATIBILITY:-1}"
VLLM_PID=""
VLLM_LOG=""

if [[ -z "${HF_TOKEN:-}" && -f "${REPO}/.env" ]]; then
  set -a
  source "${REPO}/.env"
  set +a
fi
HF_TOKEN="${HF_TOKEN:-}"

stop_vllm() {
  if [[ -n "${VLLM_PID}" ]]; then
    kill "${VLLM_PID}" 2>/dev/null || true
    wait "${VLLM_PID}" 2>/dev/null || true
    VLLM_PID=""
  fi
}

cleanup() {
  stop_vllm
}
trap cleanup EXIT INT TERM

wait_for_vllm() {
  local deadline=$((SECONDS + ${VLLM_STARTUP_TIMEOUT:-1800}))
  while (( SECONDS < deadline )); do
    if curl --silent --fail "http://127.0.0.1:${PORT}/health" >/dev/null; then
      return 0
    fi
    if ! kill -0 "${VLLM_PID}" 2>/dev/null; then
      echo "vLLM exited during startup; inspect ${VLLM_LOG}" >&2
      return 1
    fi
    sleep 10
  done
  echo "Timed out waiting for vLLM; inspect ${VLLM_LOG}" >&2
  return 1
}

start_vllm() {
  local model="$1"
  local max_model_len="$2"
  local gpu_memory_utilization="$3"
  local log_name="$4"
  VLLM_LOG="${PILOT_ROOT}/logs/${log_name}"
  srun \
    --ntasks=1 \
    --container-image="${VLLM_SQUASHFS_PATH}" \
    --container-mounts="${HF_HOME}:${HF_HOME}" \
    --container-workdir="${HF_HOME}" \
    --container-env=HF_HOME,HF_HUB_CACHE,HF_HUB_DISABLE_XET,VLLM_ENABLE_CUDA_COMPATIBILITY,HF_TOKEN \
    --no-container-entrypoint \
    /usr/bin/python3 -m vllm.entrypoints.openai.api_server \
      --host 127.0.0.1 \
      --port "${PORT}" \
      --model "${model}" \
      --served-model-name "${model}" \
      --dtype bfloat16 \
      --tensor-parallel-size 1 \
      --gpu-memory-utilization "${gpu_memory_utilization}" \
      --max-model-len "${max_model_len}" \
      --max-num-seqs "${CONCURRENCY}" \
      --trust-remote-code \
    >"${VLLM_LOG}" 2>&1 &
  VLLM_PID=$!
  wait_for_vllm
}

if [[ ! -x "${VENV}/bin/python" ]]; then
  echo "Missing toxicity venv: ${VENV}" >&2
  exit 2
fi
if [[ ! -f "${VLLM_SQUASHFS_PATH}" ]]; then
  echo "Missing vLLM SquashFS: ${VLLM_SQUASHFS_PATH}" >&2
  exit 2
fi
if [[ ! -f "${QWEN_SAMPLE}" || ! -f "${SEXUAL_SAMPLE}" ]]; then
  echo "Missing one or both review sample files." >&2
  exit 2
fi

mkdir -p \
  "${REPO}/logs/slurm" \
  "${PILOT_ROOT}/samples" \
  "${PILOT_ROOT}/granite" \
  "${PILOT_ROOT}/gemma" \
  "${PILOT_ROOT}/comparison" \
  "${PILOT_ROOT}/logs" \
  "${HF_HOME}" \
  "${HF_HUB_CACHE}"

export HF_HOME HF_HUB_CACHE HF_HUB_DISABLE_XET VLLM_ENABLE_CUDA_COMPATIBILITY HF_TOKEN
cd "${TOX_DIR}"
source "${VENV}/bin/activate"

python medical_guard_pilot.py prepare \
  --qwen-sample "${QWEN_SAMPLE}" \
  --sexual-sample "${SEXUAL_SAMPLE}" \
  --shard-root "${RUN_ROOT}/scans/toxicity/shards" \
  --output "${INPUT}"

start_vllm "${GRANITE_MODEL}" 8192 0.90 "granite_vllm.log"
python medical_guard_pilot.py run \
  --backend granite \
  --input "${INPUT}" \
  --output "${GRANITE_OUTPUT}" \
  --base-url "${BASE_URL}" \
  --model "${GRANITE_MODEL}" \
  --concurrency "${CONCURRENCY}"
stop_vllm
sleep 10

start_vllm "${GEMMA_MODEL}" 16384 0.95 "gemma_vllm.log"
python medical_guard_pilot.py run \
  --backend gemma \
  --input "${INPUT}" \
  --output "${GEMMA_OUTPUT}" \
  --base-url "${BASE_URL}" \
  --model "${GEMMA_MODEL}" \
  --concurrency "${CONCURRENCY}"
stop_vllm

python medical_guard_pilot.py compare \
  --sample "${INPUT}" \
  --granite "${GRANITE_OUTPUT}" \
  --gemma "${GEMMA_OUTPUT}" \
  --output-dir "${PILOT_ROOT}/comparison"

touch "${PILOT_ROOT}/_SUCCESS"
echo "Medical guard pilot completed: ${PILOT_ROOT}"
