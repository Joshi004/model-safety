#!/bin/bash
#SBATCH --job-name=privacy_filter_pilot
#SBATCH --partition=health
#SBATCH --gres=gpu:nvidia_h100_80gb_hbm3:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=128G
#SBATCH --time=12:00:00
#SBATCH --output=synth_data_gen/logs/slurm/privacy_filter_pilot_%j.out
#SBATCH --error=synth_data_gen/logs/slurm/privacy_filter_pilot_%j.err

set -euo pipefail

REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}/synth_data_gen}"
PIPELINE_DIR="${REPO}/tools/data_prep/pii_privacy_filter"
INPUT_ROOT="${PRIVACY_FILTER_INPUT_ROOT:?PRIVACY_FILTER_INPUT_ROOT is required}"
RUN_ROOT="${PRIVACY_FILTER_RUN_ROOT:?PRIVACY_FILTER_RUN_ROOT is required}"
WORK_MANIFEST="${PRIVACY_FILTER_WORK_MANIFEST:-${RUN_ROOT}/manifest/work_units.jsonl}"
MODEL="${PRIVACY_FILTER_MODEL:-OpenMed/privacy-filter-nemotron-v2}"
BACKEND="${PRIVACY_FILTER_BACKEND:-transformers}"
VENV="${PRIVACY_FILTER_VENV:-${PIPELINE_DIR}/.venv-isolated}"
BASE_PYTHON="${PRIVACY_FILTER_BASE_PYTHON:-${REPO}/venv/bin/python}"
HF_HOME="${HF_HOME:-${REPO}/cache/hf}"
VLLM_IMAGE="${PRIVACY_VLLM_SQUASHFS_PATH:-${REPO}/containers/vllm-openai-v0.24.0-cu129.sqsh}"

mkdir -p "${REPO}/logs/slurm" "${RUN_ROOT}/worker_logs" "${HF_HOME}"
if [[ -z "${HF_TOKEN:-}" && -f "${REPO}/.env" ]]; then
  set -a
  source "${REPO}/.env"
  set +a
fi
export HF_HOME HF_TOKEN="${HF_TOKEN:-}"

args=(
  worker
  --input-root "${INPUT_ROOT}"
  --work-manifest "${WORK_MANIFEST}"
  --output-root "${RUN_ROOT}"
  --backend "${BACKEND}"
  --model "${MODEL}"
  --policy "${PRIVACY_FILTER_POLICY:-${PIPELINE_DIR}/policy.json}"
  --input-batch-size "${PRIVACY_FILTER_INPUT_BATCH_SIZE:-32}"
  --model-batch-size "${PRIVACY_FILTER_MODEL_BATCH_SIZE:-8}"
  --max-tokens "${PRIVACY_FILTER_MAX_TOKENS:-4096}"
  --overlap-tokens "${PRIVACY_FILTER_OVERLAP_TOKENS:-256}"
  --max-units "${PRIVACY_FILTER_MAX_UNITS:-1}"
)

if [[ "${BACKEND}" == "vllm" ]]; then
  srun --ntasks=1 \
    --container-image="${VLLM_IMAGE}" \
    --container-mounts="${REPO}:${REPO}" \
    --container-workdir="${PIPELINE_DIR}" \
    --no-container-entrypoint \
    /usr/bin/python3 scanner.py clear-claims --output-root "${RUN_ROOT}"
  srun --ntasks=1 \
    --container-image="${VLLM_IMAGE}" \
    --container-mounts="${REPO}:${REPO}" \
    --container-workdir="${PIPELINE_DIR}" \
    --container-env=HF_HOME,HF_TOKEN \
    --no-container-entrypoint \
    /usr/bin/python3 scanner.py "${args[@]}"
  aggregate_python=(srun --ntasks=1
    --container-image="${VLLM_IMAGE}"
    --container-mounts="${REPO}:${REPO}"
    --container-workdir="${PIPELINE_DIR}"
    --no-container-entrypoint
    /usr/bin/python3)
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
  "${VENV}/bin/python" parity_benchmark.py \
    --model "${MODEL}" \
    --output "${RUN_ROOT}/parity.json" \
    --batch-size "${PRIVACY_FILTER_PARITY_BATCH_SIZE:-4}" \
    --max-tokens "${PRIVACY_FILTER_MAX_TOKENS:-4096}" \
    --overlap-tokens "${PRIVACY_FILTER_OVERLAP_TOKENS:-256}" \
    --minimum-f1 "${PRIVACY_FILTER_PARITY_MINIMUM_F1:-0.95}"
  "${VENV}/bin/python" scanner.py clear-claims --output-root "${RUN_ROOT}"
  "${VENV}/bin/python" scanner.py "${args[@]}"
  aggregate_python=("${VENV}/bin/python")
fi

"${aggregate_python[@]}" scanner.py aggregate \
  --work-manifest "${WORK_MANIFEST}" \
  --output-root "${RUN_ROOT}" \
  --max-units "${PRIVACY_FILTER_MAX_UNITS:-1}"

