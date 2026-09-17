#!/bin/bash

set -euo pipefail

MODEL="${PII_LLM_MODEL:?PII_LLM_MODEL is required}"
BASE_PORT="${PII_LLM_BASE_PORT:?PII_LLM_BASE_PORT is required}"
REPLICA_COUNT="${PII_LLM_REPLICA_COUNT:-8}"
MAX_MODEL_LEN="${PII_LLM_MAX_MODEL_LEN:-16384}"
MAX_NUM_SEQS="${PII_LLM_MAX_NUM_SEQS:-64}"
REASONING_PARSER="${PII_LLM_REASONING_PARSER:-gemma4}"
GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.95}"
LOG_DIR="${PII_LLM_SERVER_LOG_DIR:?PII_LLM_SERVER_LOG_DIR is required}"

declare -a pids=()

cleanup() {
  local pid
  for pid in "${pids[@]:-}"; do
    kill "${pid}" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

mkdir -p "${LOG_DIR}"
for ((index = 0; index < REPLICA_COUNT; index++)); do
  port=$((BASE_PORT + index))
  CUDA_VISIBLE_DEVICES="${index}" /usr/bin/python3 \
    -m vllm.entrypoints.openai.api_server \
    --host 127.0.0.1 \
    --port "${port}" \
    --model "${MODEL}" \
    --served-model-name "${MODEL}" \
    --dtype bfloat16 \
    --tensor-parallel-size 1 \
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
    --max-model-len "${MAX_MODEL_LEN}" \
    --max-num-seqs "${MAX_NUM_SEQS}" \
    --reasoning-parser "${REASONING_PARSER}" \
    --trust-remote-code \
    >"${LOG_DIR}/replica_${index}.log" 2>&1 &
  pids+=("$!")
done

failed=0
for pid in "${pids[@]}"; do
  if ! wait "${pid}"; then
    failed=1
  fi
done
exit "${failed}"
