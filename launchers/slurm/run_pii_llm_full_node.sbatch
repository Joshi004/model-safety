#!/bin/bash
#SBATCH --job-name=medpsy_pii_gemma_node
#SBATCH --partition=health
#SBATCH --nodes=1
#SBATCH --gres=gpu:nvidia_h100_80gb_hbm3:8
#SBATCH --cpus-per-task=64
#SBATCH --mem=512G
#SBATCH --time=72:00:00
#SBATCH --output=synth_data_gen/logs/slurm/medpsy_pii_gemma_node_%j.out
#SBATCH --error=synth_data_gen/logs/slurm/medpsy_pii_gemma_node_%j.err

set -euo pipefail

REPO="${PII_REPO:-${SLURM_SUBMIT_DIR:-$PWD}/synth_data_gen}"
PII_DIR="${REPO}/tools/data_prep/pii_scan"
VENV="${PII_DIR}/.venv"
MANIFEST_DIR="${QUALITY_MANIFEST_DIR:?QUALITY_MANIFEST_DIR is required}"
RUN_ROOT="${QUALITY_RUN_ROOT:?QUALITY_RUN_ROOT is required}"
MODEL="${PII_LLM_MODEL:-google/gemma-4-31B-it}"
REASONING_PARSER="${PII_LLM_REASONING_PARSER:-gemma4}"
BASE_PORT="${PII_LLM_BASE_PORT:-$((20000 + (SLURM_JOB_ID % 1000) * 16))}"
MAX_MODEL_LEN="${PII_LLM_MAX_MODEL_LEN:-16384}"
MAX_NUM_SEQS="${PII_LLM_MAX_NUM_SEQS:-64}"
MAX_CONVERSATION_CHARS="${PII_MAX_CONVERSATION_CHARS:-12000}"
MAX_TOKENS="${PII_LLM_MAX_TOKENS:-4096}"
VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.92}"
VLLM_SQUASHFS_PATH="${VLLM_SQUASHFS_PATH:-${REPO}/containers/vllm-openai-v0.24.0-cu129.sqsh}"
HF_HOME="${HF_HOME:-${REPO}/cache/hf}"
HF_HUB_CACHE="${HF_HUB_CACHE:-${HF_HOME}/hub}"
HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"
VLLM_ENABLE_CUDA_COMPATIBILITY="${VLLM_ENABLE_CUDA_COMPATIBILITY:-1}"
LOG_DIR="${RUN_ROOT}/logs/pii_llm_full"
VLLM_PID=""
SERVER_MODE=""
declare -a BASE_URL_ARGS=()

if [[ -z "${HF_TOKEN:-}" && -f "${REPO}/.env" ]]; then
  set -a
  source "${REPO}/.env"
  set +a
fi
HF_TOKEN="${HF_TOKEN:-}"

cleanup() {
  if [[ -n "${VLLM_PID}" ]]; then
    kill "${VLLM_PID}" 2>/dev/null || true
    wait "${VLLM_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

wait_for_servers() {
  local count="$1"
  local deadline=$((SECONDS + ${VLLM_STARTUP_TIMEOUT:-2400}))
  local index port healthy
  while ((SECONDS < deadline)); do
    if ! kill -0 "${VLLM_PID}" 2>/dev/null; then
      return 1
    fi
    healthy=1
    for ((index = 0; index < count; index++)); do
      port=$((BASE_PORT + index))
      if ! curl --silent --fail "http://127.0.0.1:${port}/health" >/dev/null; then
        healthy=0
        break
      fi
    done
    if ((healthy)); then
      return 0
    fi
    sleep 10
  done
  return 1
}

stop_servers() {
  if [[ -n "${VLLM_PID}" ]]; then
    kill "${VLLM_PID}" 2>/dev/null || true
    wait "${VLLM_PID}" 2>/dev/null || true
    VLLM_PID=""
  fi
}

if [[ ! -x "${VENV}/bin/python" ]]; then
  echo "PII environment is missing: ${VENV}" >&2
  exit 2
fi
if [[ ! -f "${VLLM_SQUASHFS_PATH}" ]]; then
  echo "Missing vLLM SquashFS: ${VLLM_SQUASHFS_PATH}" >&2
  exit 2
fi

mkdir -p "${REPO}/logs/slurm" "${LOG_DIR}" "${HF_HOME}" "${HF_HUB_CACHE}"
export HF_HOME HF_HUB_CACHE HF_HUB_DISABLE_XET VLLM_ENABLE_CUDA_COMPATIBILITY HF_TOKEN

echo "Starting four data-parallel Gemma replicas (TP=2, DP=4)"
srun \
  --ntasks=1 \
  --gres=gpu:nvidia_h100_80gb_hbm3:8 \
  --container-image="${VLLM_SQUASHFS_PATH}" \
  --container-mounts="${HF_HOME}:${HF_HOME}" \
  --container-workdir="${HF_HOME}" \
  --container-env=HF_HOME,HF_HUB_CACHE,HF_HUB_DISABLE_XET,VLLM_ENABLE_CUDA_COMPATIBILITY,HF_TOKEN \
  --no-container-entrypoint \
  /usr/bin/python3 -m vllm.entrypoints.openai.api_server \
    --host 127.0.0.1 \
    --port "${BASE_PORT}" \
    --data-parallel-supervisor-port "$((BASE_PORT + 8))" \
    --model "${MODEL}" \
    --served-model-name "${MODEL}" \
    --dtype bfloat16 \
    --tensor-parallel-size 2 \
    --data-parallel-size 4 \
    --data-parallel-size-local 4 \
    --gpu-memory-utilization "${VLLM_GPU_MEMORY_UTILIZATION}" \
    --max-model-len "${MAX_MODEL_LEN}" \
    --max-num-seqs "${MAX_NUM_SEQS}" \
    --reasoning-parser "${REASONING_PARSER}" \
    --trust-remote-code \
  >"${LOG_DIR}/vllm_dp4_tp2.log" 2>&1 &
VLLM_PID=$!

if wait_for_servers 1; then
  SERVER_MODE="dp4_tp2"
  BASE_URL_ARGS=(--base-url "http://127.0.0.1:${BASE_PORT}/v1")
  REQUESTS_PER_SERVER="${PII_LLM_CONCURRENCY:-512}"
  MAX_PENDING="${PII_LLM_MAX_PENDING:-1024}"
else
  echo "DP=4 startup failed; falling back to eight one-GPU replicas" >&2
  stop_servers
  export PII_LLM_MODEL="${MODEL}"
  export PII_LLM_BASE_PORT="${BASE_PORT}"
  export PII_LLM_REPLICA_COUNT=8
  export PII_LLM_MAX_MODEL_LEN="${MAX_MODEL_LEN}"
  export PII_LLM_MAX_NUM_SEQS="${MAX_NUM_SEQS}"
  export PII_LLM_REASONING_PARSER="${REASONING_PARSER}"
  export PII_LLM_SERVER_LOG_DIR="${LOG_DIR}/fallback_replicas"
  export VLLM_GPU_MEMORY_UTILIZATION
  srun \
    --ntasks=1 \
    --gres=gpu:nvidia_h100_80gb_hbm3:8 \
    --container-image="${VLLM_SQUASHFS_PATH}" \
    --container-mounts="${REPO}:${REPO}" \
    --container-workdir="${HF_HOME}" \
    --container-env=HF_HOME,HF_HUB_CACHE,HF_HUB_DISABLE_XET,VLLM_ENABLE_CUDA_COMPATIBILITY,HF_TOKEN,PII_LLM_MODEL,PII_LLM_BASE_PORT,PII_LLM_REPLICA_COUNT,PII_LLM_MAX_MODEL_LEN,PII_LLM_MAX_NUM_SEQS,PII_LLM_REASONING_PARSER,PII_LLM_SERVER_LOG_DIR,VLLM_GPU_MEMORY_UTILIZATION \
    --no-container-entrypoint \
    /bin/bash "${PII_DIR}/launch_vllm_replicas.sh" &
  VLLM_PID=$!
  if ! wait_for_servers 8; then
    echo "Fallback Gemma replicas failed to start; inspect ${LOG_DIR}" >&2
    exit 1
  fi
  SERVER_MODE="replicas8_tp1"
  BASE_URL_ARGS=()
  for ((index = 0; index < 8; index++)); do
    BASE_URL_ARGS+=(--base-url "http://127.0.0.1:$((BASE_PORT + index))/v1")
  done
  REQUESTS_PER_SERVER="${PII_LLM_FALLBACK_REQUESTS_PER_SERVER:-64}"
  MAX_PENDING="${PII_LLM_FALLBACK_MAX_PENDING:-2048}"
fi

echo "Gemma server mode: ${SERVER_MODE}"
cd "${PII_DIR}"
source "${VENV}/bin/activate"
for manifest in "${MANIFEST_DIR}"/shards/shard_*.txt; do
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
    "${BASE_URL_ARGS[@]}" \
    --model "${MODEL}" \
    --requests-per-server "${REQUESTS_PER_SERVER}" \
    --max-pending "${MAX_PENDING}" \
    --max-conversation-chars "${MAX_CONVERSATION_CHARS}" \
    --max-tokens "${MAX_TOKENS}" \
    --enable-thinking \
    --no-response-format \
    --timeout "${PII_LLM_TIMEOUT:-600}" \
    --retries "${PII_LLM_RETRIES:-3}" \
    --resume
  touch "${output_dir}/_SUCCESS"
done
