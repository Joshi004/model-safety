#!/bin/bash
#SBATCH --job-name=medpsy_pii_gemma
#SBATCH --partition=health
#SBATCH --gres=gpu:nvidia_h100_80gb_hbm3:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=128G
#SBATCH --time=48:00:00
#SBATCH --output=synth_data_gen/logs/slurm/medpsy_pii_gemma_%j.out
#SBATCH --error=synth_data_gen/logs/slurm/medpsy_pii_gemma_%j.err

set -euo pipefail

REPO="${PII_REPO:-${SLURM_SUBMIT_DIR:-$PWD}/synth_data_gen}"
PII_DIR="${REPO}/tools/data_prep/pii_scan"
VENV="${PII_DIR}/.venv"
MANIFEST_DIR="${QUALITY_MANIFEST_DIR:?QUALITY_MANIFEST_DIR is required}"
RUN_ROOT="${QUALITY_RUN_ROOT:?QUALITY_RUN_ROOT is required}"
MODEL="${PII_LLM_MODEL:-google/gemma-4-31B-it}"
REASONING_PARSER="${PII_LLM_REASONING_PARSER:-gemma4}"
PORT="${PII_LLM_PORT:-$((20000 + SLURM_JOB_ID % 20000))}"
BASE_URL="http://127.0.0.1:${PORT}/v1"
CONCURRENCY="${PII_LLM_CONCURRENCY:-12}"
MAX_PENDING="${PII_LLM_MAX_PENDING:-48}"
MAX_MODEL_LEN="${PII_LLM_MAX_MODEL_LEN:-16384}"
MAX_CONVERSATION_CHARS="${PII_MAX_CONVERSATION_CHARS:-12000}"
MAX_TOKENS="${PII_LLM_MAX_TOKENS:-4096}"
VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.95}"
VLLM_SQUASHFS_PATH="${VLLM_SQUASHFS_PATH:-${REPO}/containers/vllm-openai-v0.24.0-cu129.sqsh}"
HF_HOME="${HF_HOME:-${REPO}/cache/hf}"
HF_HUB_CACHE="${HF_HUB_CACHE:-${HF_HOME}/hub}"
HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"
VLLM_ENABLE_CUDA_COMPATIBILITY="${VLLM_ENABLE_CUDA_COMPATIBILITY:-1}"
if [[ -z "${HF_TOKEN:-}" && -f "${REPO}/.env" ]]; then
  set -a
  source "${REPO}/.env"
  set +a
fi
HF_TOKEN="${HF_TOKEN:-}"
LOG_DIR="${RUN_ROOT}/logs/pii_llm"
VLLM_LOG="${LOG_DIR}/vllm_${SLURM_JOB_ID}.log"
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

if [[ ! -x "${VENV}/bin/python" ]]; then
  echo "PII venv is missing; the dependent CPU scan should create ${VENV}" >&2
  exit 2
fi
if [[ ! -f "${VLLM_SQUASHFS_PATH}" ]]; then
  echo "Missing vLLM SquashFS: ${VLLM_SQUASHFS_PATH}" >&2
  exit 2
fi
if [[ -z "${HF_TOKEN}" ]]; then
  echo "HF_TOKEN is unset; Gemma must already be cached or model download will fail." >&2
fi

mkdir -p "${REPO}/logs/slurm" "${LOG_DIR}" "${HF_HOME}" "${HF_HUB_CACHE}"
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
    --dtype bfloat16 \
    --tensor-parallel-size 1 \
    --gpu-memory-utilization "${VLLM_GPU_MEMORY_UTILIZATION}" \
    --max-model-len "${MAX_MODEL_LEN}" \
    --max-num-seqs "${CONCURRENCY}" \
    --reasoning-parser "${REASONING_PARSER}" \
    --trust-remote-code \
  >"${VLLM_LOG}" 2>&1 &
VLLM_PID=$!
wait_for_vllm

cd "${PII_DIR}"
source "${VENV}/bin/activate"
if [[ -n "${SLURM_ARRAY_TASK_ID:-}" ]]; then
  manifests=("${MANIFEST_DIR}/shards/$(printf 'shard_%05d.txt' "${SLURM_ARRAY_TASK_ID}")")
else
  manifests=("${MANIFEST_DIR}"/shards/shard_*.txt)
fi
for manifest in "${manifests[@]}"; do
  shard_name="$(basename "${manifest}" .txt)"
  shard_root="${RUN_ROOT}/scans/pii/shards/${shard_name}"
  input_csv="${shard_root}/stage3_extract/pii_extract.csv"
  output_dir="${shard_root}/stage4_llm"
  if [[ ! -f "${shard_root}/_CPU_SUCCESS" || ! -f "${input_csv}" ]]; then
    echo "Missing completed PII CPU shard: ${shard_name}" >&2
    exit 2
  fi
  rm -f "${output_dir}/_SUCCESS"
  python pii_llm_validator.py \
    --input "${input_csv}" \
    --output-dir "${output_dir}" \
    --base-url "${BASE_URL}" \
    --model "${MODEL}" \
    --requests-per-server "${CONCURRENCY}" \
    --max-pending "${MAX_PENDING}" \
    --max-conversation-chars "${MAX_CONVERSATION_CHARS}" \
    --max-tokens "${MAX_TOKENS}" \
    --enable-thinking \
    --no-response-format \
    --resume
  touch "${output_dir}/_SUCCESS"
done
