#!/bin/bash
#SBATCH --job-name=medpsy_toxicity
#SBATCH --partition=health
#SBATCH --gres=gpu:nvidia_h100_80gb_hbm3:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=96G
#SBATCH --time=48:00:00
#SBATCH --output=logs/slurm/medpsy_toxicity_%A_%a.out
#SBATCH --error=logs/slurm/medpsy_toxicity_%A_%a.err

set -euo pipefail

REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
TOX_DIR="${REPO}/modelsafety/data_screening/toxicity"
VENV="${TOX_DIR}/.venv"
INPUT_ROOT="${QUALITY_INPUT_ROOT:?QUALITY_INPUT_ROOT is required}"
MANIFEST_DIR="${QUALITY_MANIFEST_DIR:?QUALITY_MANIFEST_DIR is required}"
RUN_ROOT="${QUALITY_RUN_ROOT:?QUALITY_RUN_ROOT is required}"
SHARD_ID="${SLURM_ARRAY_TASK_ID:?Submit this script as a SLURM array}"
SHARD_NAME="$(printf 'shard_%05d' "${SHARD_ID}")"
MANIFEST="${MANIFEST_DIR}/shards/${SHARD_NAME}.txt"
SHARD_ROOT="${RUN_ROOT}/scans/toxicity/shards/${SHARD_NAME}"
FLAGGED_FILE="${SHARD_ROOT}/flagged.jsonl"
GUARD_DIR="${SHARD_ROOT}/guard_results"
MODEL="${GUARD_MODEL:-Qwen/Qwen3Guard-Gen-8B}"
PORT="${GUARD_PORT:-$((20000 + SLURM_JOB_ID % 20000))}"
BASE_URL="http://127.0.0.1:${PORT}/v1"
CONCURRENCY="${GUARD_CONCURRENCY:-8}"
VLLM_SQUASHFS_PATH="${VLLM_SQUASHFS_PATH:-${REPO}/containers/vllm-openai-v0.24.0-cu129.sqsh}"
HF_HOME="${HF_HOME:-${REPO}/cache/hf}"
HF_HUB_CACHE="${HF_HUB_CACHE:-${HF_HOME}/hub}"
HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"
VLLM_ENABLE_CUDA_COMPATIBILITY="${VLLM_ENABLE_CUDA_COMPATIBILITY:-1}"
HF_TOKEN="${HF_TOKEN:-}"
VLLM_LOG="${SHARD_ROOT}/vllm.log"
VLLM_PID=""

cleanup() {
  if [[ -n "${VLLM_PID}" ]]; then
    kill "${VLLM_PID}" 2>/dev/null || true
    wait "${VLLM_PID}" 2>/dev/null || true
  fi
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

if [[ ! -f "${MANIFEST}" ]]; then
  echo "Missing shard manifest: ${MANIFEST}" >&2
  exit 2
fi
if [[ ! -f "${VLLM_SQUASHFS_PATH}" ]]; then
  echo "Missing vLLM SquashFS: ${VLLM_SQUASHFS_PATH}" >&2
  exit 2
fi

mkdir -p "${REPO}/logs/slurm" "${SHARD_ROOT}" "${HF_HOME}" "${HF_HUB_CACHE}"
(
  flock 9
  if [[ ! -x "${VENV}/bin/python" ]]; then
    python3 -m venv "${VENV}"
    "${VENV}/bin/pip" install -U pip wheel
    "${VENV}/bin/pip" install -r "${TOX_DIR}/requirements.txt"
  fi
) 9>"${TOX_DIR}/.venv.lock"

cd "${TOX_DIR}"
source "${VENV}/bin/activate"
if [[ "${TOX_REUSE_STAGE1:-0}" == "1" && -f "${FLAGGED_FILE}" ]]; then
  echo "Reusing existing Detoxify output: ${FLAGGED_FILE}"
else
  python run.py \
    --input-root "${INPUT_ROOT}" \
    --input-manifest "${MANIFEST}" \
    --model_name "${TOX_STAGE1_MODEL:-multilingual}" \
    --dest_file "${FLAGGED_FILE}" \
    --batch_size "${TOX_BATCH_SIZE:-32}" \
    --threshold "${TOX_THRESHOLD:-0.5}"
fi
touch "${SHARD_ROOT}/_DETOXIFY_SUCCESS"
if [[ "${TOX_STAGE1_ONLY:-0}" == "1" ]]; then
  echo "Detoxify stage complete; TOX_STAGE1_ONLY=1, skipping Qwen3Guard."
  exit 0
fi

rm -rf "${GUARD_DIR}"
mkdir -p "${GUARD_DIR}"
if [[ ! -s "${FLAGGED_FILE}" ]]; then
  : >"${GUARD_DIR}/all.jsonl"
  : >"${GUARD_DIR}/safe.jsonl"
  : >"${GUARD_DIR}/unsafe.jsonl"
  : >"${GUARD_DIR}/controversial.jsonl"
  printf '{"total_records": 0, "safety": {}, "categories": {}, "errors": 0, "unknown": 0}\n' \
    >"${GUARD_DIR}/summary.json"
else
  export HF_HOME HF_HUB_CACHE HF_HUB_DISABLE_XET VLLM_ENABLE_CUDA_COMPATIBILITY HF_TOKEN
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
      --model "${MODEL}" \
      --served-model-name "${MODEL}" \
      --gpu-memory-utilization "${VLLM_GPU_MEMORY_UTILIZATION:-0.9}" \
      --max-model-len "${VLLM_MAX_MODEL_LEN:-8192}" \
      --max-num-seqs "${CONCURRENCY}" \
      --trust-remote-code \
    >"${VLLM_LOG}" 2>&1 &
  VLLM_PID=$!
  wait_for_vllm

  python llm_check_guard.py \
    --input "${FLAGGED_FILE}" \
    --output "${GUARD_DIR}" \
    --base_url "${BASE_URL}" \
    --model "${MODEL}" \
    --concurrency "${CONCURRENCY}" \
    --max_tokens "${GUARD_MAX_TOKENS:-128}"
fi

python guard_report.py "${GUARD_DIR}" --format markdown --output "${SHARD_ROOT}/guard_report.md"
touch "${SHARD_ROOT}/_SUCCESS"
