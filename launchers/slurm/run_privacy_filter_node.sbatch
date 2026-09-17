#!/bin/bash
#SBATCH --job-name=privacy_filter_node
#SBATCH --partition=health
#SBATCH --nodes=1
#SBATCH --gres=gpu:nvidia_h100_80gb_hbm3:8
#SBATCH --cpus-per-task=64
#SBATCH --mem=512G
#SBATCH --time=7-00:00:00
#SBATCH --output=synth_data_gen/logs/slurm/privacy_filter_node_%j.out
#SBATCH --error=synth_data_gen/logs/slurm/privacy_filter_node_%j.err

set -euo pipefail

REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}/synth_data_gen}"
PIPELINE_DIR="${REPO}/tools/data_prep/pii_privacy_filter"
INPUT_ROOT="${PRIVACY_FILTER_INPUT_ROOT:?PRIVACY_FILTER_INPUT_ROOT is required}"
RUN_ROOT="${PRIVACY_FILTER_RUN_ROOT:?PRIVACY_FILTER_RUN_ROOT is required}"
WORK_MANIFEST="${PRIVACY_FILTER_WORK_MANIFEST:-${RUN_ROOT}/manifest/work_units.jsonl}"
MODEL="${PRIVACY_FILTER_MODEL:-OpenMed/privacy-filter-nemotron-v2}"
BACKEND="${PRIVACY_FILTER_BACKEND:-transformers}"
GPU_COUNT="${PRIVACY_FILTER_GPU_COUNT:-${SLURM_GPUS_ON_NODE:-8}}"
VENV="${PRIVACY_FILTER_VENV:-${PIPELINE_DIR}/.venv-isolated}"
BASE_PYTHON="${PRIVACY_FILTER_BASE_PYTHON:-${REPO}/venv/bin/python}"
HF_HOME="${HF_HOME:-${REPO}/cache/hf}"
VLLM_IMAGE="${PRIVACY_VLLM_SQUASHFS_PATH:-${REPO}/containers/vllm-openai-v0.24.0-cu129.sqsh}"

if [[ ! -f "${WORK_MANIFEST}" ]]; then
  echo "Missing work manifest: ${WORK_MANIFEST}" >&2
  exit 2
fi
mkdir -p "${REPO}/logs/slurm" "${RUN_ROOT}/worker_logs" "${HF_HOME}"
if [[ -z "${HF_TOKEN:-}" && -f "${REPO}/.env" ]]; then
  set -a
  source "${REPO}/.env"
  set +a
fi
export HF_HOME HF_TOKEN="${HF_TOKEN:-}"

common_args=(
  worker
  --input-root "${INPUT_ROOT}"
  --work-manifest "${WORK_MANIFEST}"
  --output-root "${RUN_ROOT}"
  --backend "${BACKEND}"
  --model "${MODEL}"
  --policy "${PRIVACY_FILTER_POLICY:-${PIPELINE_DIR}/policy.json}"
  --input-batch-size "${PRIVACY_FILTER_INPUT_BATCH_SIZE:-64}"
  --model-batch-size "${PRIVACY_FILTER_MODEL_BATCH_SIZE:-16}"
  --max-tokens "${PRIVACY_FILTER_MAX_TOKENS:-4096}"
  --overlap-tokens "${PRIVACY_FILTER_OVERLAP_TOKENS:-256}"
  --max-units "${PRIVACY_FILTER_MAX_UNITS:-0}"
)

declare -a pids=()
if [[ "${BACKEND}" == "vllm" ]]; then
  if [[ ! -f "${VLLM_IMAGE}" ]]; then
    echo "Missing vLLM image: ${VLLM_IMAGE}" >&2
    exit 2
  fi
  srun --ntasks=1 --gres=gpu:nvidia_h100_80gb_hbm3:1 \
    --container-image="${VLLM_IMAGE}" \
    --container-mounts="${REPO}:${REPO}" \
    --container-workdir="${PIPELINE_DIR}" \
    --no-container-entrypoint \
    /usr/bin/python3 scanner.py clear-claims --output-root "${RUN_ROOT}"
  for ((index = 0; index < GPU_COUNT; index++)); do
    srun --exclusive --ntasks=1 --gres=gpu:nvidia_h100_80gb_hbm3:1 \
      --cpus-per-task=8 --mem=56G \
      --container-image="${VLLM_IMAGE}" \
      --container-mounts="${REPO}:${REPO}" \
      --container-workdir="${PIPELINE_DIR}" \
      --container-env=HF_HOME,HF_TOKEN \
      --no-container-entrypoint \
      /usr/bin/python3 scanner.py "${common_args[@]}" \
      >"${RUN_ROOT}/worker_logs/worker_${index}.log" 2>&1 &
    pids+=("$!")
  done
else
  requirements_hash="$(sha256sum "${PIPELINE_DIR}/requirements.txt" | awk '{print $1}')"
  installed_hash="$(test -f "${VENV}/.requirements_hash" && tr -d '\n' <"${VENV}/.requirements_hash" || true)"
  if [[ ! -x "${VENV}/bin/python" || "${requirements_hash}" != "${installed_hash}" ]]; then
    "${BASE_PYTHON}" -m venv "${VENV}"
    "${VENV}/bin/pip" install -U pip wheel
    "${VENV}/bin/pip" install --force-reinstall \
      --index-url "${PRIVACY_FILTER_TORCH_INDEX_URL:-https://download.pytorch.org/whl/cu128}" \
      torch
    "${VENV}/bin/pip" install -r "${PIPELINE_DIR}/requirements.txt"
    printf '%s\n' "${requirements_hash}" >"${VENV}/.requirements_hash"
  fi
  "${VENV}/bin/python" -c \
    "from huggingface_hub import snapshot_download; snapshot_download('${MODEL}')"
  cd "${PIPELINE_DIR}"
  "${VENV}/bin/python" scanner.py clear-claims --output-root "${RUN_ROOT}"
  for ((index = 0; index < GPU_COUNT; index++)); do
    (
      export CUDA_VISIBLE_DEVICES="${index}"
      cd "${PIPELINE_DIR}"
      "${VENV}/bin/python" scanner.py "${common_args[@]}"
    ) >"${RUN_ROOT}/worker_logs/worker_${index}.log" 2>&1 &
    pids+=("$!")
  done
fi

failed=0
for pid in "${pids[@]}"; do
  if ! wait "${pid}"; then
    failed=1
  fi
done
if ((failed)); then
  echo "At least one scanner worker failed; inspect ${RUN_ROOT}/worker_logs" >&2
  exit 1
fi

if [[ "${BACKEND}" == "vllm" ]]; then
  srun --exclusive --ntasks=1 --gres=gpu:nvidia_h100_80gb_hbm3:1 \
    --container-image="${VLLM_IMAGE}" \
    --container-mounts="${REPO}:${REPO}" \
    --container-workdir="${PIPELINE_DIR}" \
    --no-container-entrypoint \
    /usr/bin/python3 scanner.py aggregate \
      --work-manifest "${WORK_MANIFEST}" \
      --output-root "${RUN_ROOT}" \
      --max-units "${PRIVACY_FILTER_MAX_UNITS:-0}"
else
  cd "${PIPELINE_DIR}"
  "${VENV}/bin/python" scanner.py aggregate \
    --work-manifest "${WORK_MANIFEST}" \
    --output-root "${RUN_ROOT}" \
    --max-units "${PRIVACY_FILTER_MAX_UNITS:-0}"
fi

