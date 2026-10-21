#!/bin/bash
# Build (or rebuild) the Python environment the NER-model PII scan runs in:
# modelsafety/data_screening/pii/model/.venv-isolated.
#
# The scan's SLURM jobs (run_privacy_filter_node.sbatch, run_privacy_filter_pilot.sbatch) never
# install anything: several jobs changing one shared environment at the same time broke it. They
# stop at once when the environment is missing or older than requirements.txt, and ask for this
# script to be run.
#
# Usage (on the login node, with no PII job queued or running):
#   ./scripts/setup_pii_env.sh
#
# Settings (environment only):
#   PRIVACY_FILTER_VENV            where the environment is (default: .venv-isolated above)
#   PRIVACY_FILTER_BASE_PYTHON     the Python it is made from (default: python3)
#   PRIVACY_FILTER_TORCH_INDEX_URL where torch comes from (default: the CUDA 12.8 wheel index)
#   PRIVACY_FILTER_TORCH_CUDA      the CUDA version that torch must be built for (default: 12.8,
#                                  what the cluster's GPU driver supports)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
PIPELINE_DIR="${REPO}/modelsafety/data_screening/pii/model"
VENV="${PRIVACY_FILTER_VENV:-${PIPELINE_DIR}/.venv-isolated}"
BASE_PYTHON="${PRIVACY_FILTER_BASE_PYTHON:-python3}"
TORCH_INDEX_URL="${PRIVACY_FILTER_TORCH_INDEX_URL:-https://download.pytorch.org/whl/cu128}"
TORCH_CUDA="${PRIVACY_FILTER_TORCH_CUDA:-12.8}"
# Every job that runs in this environment, whoever started it: it is shared.
PII_JOB_NAMES="privacy_filter_node,privacy_filter_pilot,privacy_filter_compare,privacy_filter_llm"

running="$(squeue -h -n "${PII_JOB_NAMES}" -o '%i %u %j %T')"
if [[ -n "${running}" ]]; then
  echo "These PII jobs are queued or running, and use this environment:" >&2
  echo "${running}" >&2
  echo "Wait for them to end (or cancel them), then run this again." >&2
  exit 1
fi

# Two runs of this script at the same time would break the environment the same way.
exec 9>"${PIPELINE_DIR}/.venv.lock"
if ! flock -n 9; then
  echo "Another run of this script is building the environment. Wait for it to finish." >&2
  exit 1
fi

requirements_hash="$(sha256sum "${PIPELINE_DIR}/requirements.txt" | awk '{print $1}')"
torch_pin="$(grep -E '^torch==' "${PIPELINE_DIR}/requirements.txt" || true)"
if [[ -z "${torch_pin}" ]]; then
  echo "${PIPELINE_DIR}/requirements.txt has no 'torch==<version>' line to install torch from." >&2
  exit 1
fi
echo "Building ${VENV}"
# The marker goes first, so an environment this run doesn't finish is never taken as ready.
rm -f "${VENV}/.requirements_hash"
# Rebuilt where it is: an environment can't be moved, its scripts hold its own path.
"${BASE_PYTHON}" -m venv --clear "${VENV}"
"${VENV}/bin/pip" install -U pip wheel
"${VENV}/bin/pip" install --index-url "${TORCH_INDEX_URL}" "${torch_pin}"
"${VENV}/bin/pip" install -r "${PIPELINE_DIR}/requirements.txt"

torch_cuda="$("${VENV}/bin/python" -c 'import torch; print(torch.version.cuda or "none")')"
if [[ "${torch_cuda}" != "${TORCH_CUDA}" ]]; then
  echo "torch in ${VENV} is built for CUDA ${torch_cuda}, not ${TORCH_CUDA}: the GPU driver can't run it." >&2
  echo "Check the torch and openmed pins in ${PIPELINE_DIR}/requirements.txt. The environment is not marked ready." >&2
  exit 1
fi

printf '%s\n' "${requirements_hash}" >"${VENV}/.requirements_hash"
echo "Ready: ${VENV} (torch built for CUDA ${torch_cuda})."
