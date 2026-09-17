#!/bin/bash
#SBATCH --job-name=medpsy_filter
#SBATCH --partition=health
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --time=24:00:00
#SBATCH --output=logs/slurm/medpsy_filter_%A_%a.out
#SBATCH --error=logs/slurm/medpsy_filter_%A_%a.err

set -euo pipefail

REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
INPUT_ROOT="${QUALITY_INPUT_ROOT:?QUALITY_INPUT_ROOT is required}"
MANIFEST_DIR="${QUALITY_MANIFEST_DIR:?QUALITY_MANIFEST_DIR is required}"
RUN_ROOT="${QUALITY_RUN_ROOT:?QUALITY_RUN_ROOT is required}"
OUTPUT_ROOT="${QUALITY_OUTPUT_ROOT:-${RUN_ROOT}}"
SHARD_ID="${SLURM_ARRAY_TASK_ID:?Submit this script as a SLURM array}"
SHARD_NAME="$(printf 'shard_%05d' "${SHARD_ID}")"
MANIFEST="${MANIFEST_DIR}/shards/${SHARD_NAME}.txt"
PYTHON="${QUALITY_PYTHON:-python3}"

if ! command -v "${PYTHON}" >/dev/null 2>&1 && [[ ! -x "${PYTHON}" ]]; then
  echo "Python environment not found: ${PYTHON}" >&2
  exit 2
fi

# quality_filter_finalize.py imports `modelsafety.contract.textract`, an
# absolute package import added when the code was ported out of a flat
# sibling-file layout. Needs the repo root on PYTHONPATH to resolve.
export PYTHONPATH="${REPO}${PYTHONPATH:+:${PYTHONPATH}}"

mkdir -p "${REPO}/logs/slurm"
"${PYTHON}" "${REPO}/modelsafety/decisions/quality_filter_finalize.py" shard \
  --input-root "${INPUT_ROOT}" \
  --run-root "${RUN_ROOT}" \
  --output-root "${OUTPUT_ROOT}" \
  --manifest "${MANIFEST}"
