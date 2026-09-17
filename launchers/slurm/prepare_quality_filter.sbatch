#!/bin/bash
#SBATCH --job-name=medpsy_filter_prep
#SBATCH --partition=health
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=01:00:00
#SBATCH --output=synth_data_gen/logs/slurm/medpsy_filter_prep_%j.out
#SBATCH --error=synth_data_gen/logs/slurm/medpsy_filter_prep_%j.err

set -euo pipefail

REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}/synth_data_gen}"
INPUT_ROOT="${QUALITY_INPUT_ROOT:?QUALITY_INPUT_ROOT is required}"
MANIFEST_DIR="${QUALITY_MANIFEST_DIR:?QUALITY_MANIFEST_DIR is required}"
RUN_ROOT="${QUALITY_RUN_ROOT:?QUALITY_RUN_ROOT is required}"
OUTPUT_ROOT="${QUALITY_OUTPUT_ROOT:-${RUN_ROOT}}"
SHARD_COUNT="${QUALITY_SHARD_COUNT:?QUALITY_SHARD_COUNT is required}"
PILOT_FILES="${QUALITY_PILOT_FILES:-0}"
PYTHON="${QUALITY_PYTHON:-${REPO}/venv/bin/python}"

if [[ "${QUALITY_ALLOW_RERUN:-0}" == "1" && -e "${RUN_ROOT}" ]]; then
  rm -rf "${RUN_ROOT}"
fi
if [[ "${QUALITY_ALLOW_RERUN:-0}" == "1" && "${OUTPUT_ROOT}" != "${RUN_ROOT}" && -e "${OUTPUT_ROOT}" ]]; then
  rm -rf "${OUTPUT_ROOT}"
fi
mkdir -p "${MANIFEST_DIR}"
"${PYTHON}" "${REPO}/tools/data_prep/quality_filter_manifest.py" \
  --input-root "${INPUT_ROOT}" \
  --output-dir "${MANIFEST_DIR}" \
  --shards "${SHARD_COUNT}" \
  --pilot-files "${PILOT_FILES}"
touch "${MANIFEST_DIR}/_SUCCESS"
