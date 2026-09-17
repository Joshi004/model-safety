#!/bin/bash
#SBATCH --job-name=medpsy_filter
#SBATCH --partition=health
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --time=24:00:00
#SBATCH --output=synth_data_gen/logs/slurm/medpsy_filter_%A_%a.out
#SBATCH --error=synth_data_gen/logs/slurm/medpsy_filter_%A_%a.err

set -euo pipefail

REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}/synth_data_gen}"
INPUT_ROOT="${QUALITY_INPUT_ROOT:?QUALITY_INPUT_ROOT is required}"
MANIFEST_DIR="${QUALITY_MANIFEST_DIR:?QUALITY_MANIFEST_DIR is required}"
RUN_ROOT="${QUALITY_RUN_ROOT:?QUALITY_RUN_ROOT is required}"
OUTPUT_ROOT="${QUALITY_OUTPUT_ROOT:-${RUN_ROOT}}"
SHARD_ID="${SLURM_ARRAY_TASK_ID:?Submit this script as a SLURM array}"
SHARD_NAME="$(printf 'shard_%05d' "${SHARD_ID}")"
MANIFEST="${MANIFEST_DIR}/shards/${SHARD_NAME}.txt"
PYTHON="${QUALITY_PYTHON:-${REPO}/venv/bin/python}"

if [[ ! -x "${PYTHON}" ]]; then
  echo "Python environment not found: ${PYTHON}" >&2
  exit 2
fi

mkdir -p "${REPO}/logs/slurm"
"${PYTHON}" "${REPO}/tools/data_prep/quality_filter_finalize.py" shard \
  --input-root "${INPUT_ROOT}" \
  --run-root "${RUN_ROOT}" \
  --output-root "${OUTPUT_ROOT}" \
  --manifest "${MANIFEST}"
