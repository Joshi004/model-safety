#!/bin/bash
#SBATCH --job-name=privacy_filter_manifest
#SBATCH --partition=health
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=24:00:00
#SBATCH --output=logs/slurm/privacy_filter_manifest_%j.out
#SBATCH --error=logs/slurm/privacy_filter_manifest_%j.err

set -euo pipefail

REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
PIPELINE_DIR="${REPO}/modelsafety/data_screening/pii/model"
INPUT_ROOT="${PRIVACY_FILTER_INPUT_ROOT:?PRIVACY_FILTER_INPUT_ROOT is required}"
RUN_ROOT="${PRIVACY_FILTER_RUN_ROOT:?PRIVACY_FILTER_RUN_ROOT is required}"

mkdir -p "${REPO}/logs/slurm" "${RUN_ROOT}/manifest"

args=(
  --input-root "${INPUT_ROOT}"
  --output-dir "${RUN_ROOT}/manifest"
  --target-mib "${PRIVACY_FILTER_UNIT_MIB:-256}"
)
if [[ -n "${PRIVACY_FILTER_SOURCE_MANIFEST:-}" ]]; then
  args+=(--source-manifest "${PRIVACY_FILTER_SOURCE_MANIFEST}")
fi
if [[ -n "${PRIVACY_FILTER_MAX_FILES:-}" ]]; then
  args+=(--max-files "${PRIVACY_FILTER_MAX_FILES}")
fi

cd "${PIPELINE_DIR}"
python3 build_work_manifest.py "${args[@]}"

