#!/bin/bash
#SBATCH --job-name=medpsy_filter_report
#SBATCH --partition=health
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=01:00:00
#SBATCH --output=logs/slurm/medpsy_filter_report_%j.out
#SBATCH --error=logs/slurm/medpsy_filter_report_%j.err

set -euo pipefail

REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
INPUT_ROOT="${QUALITY_INPUT_ROOT:?QUALITY_INPUT_ROOT is required}"
MANIFEST_DIR="${QUALITY_MANIFEST_DIR:?QUALITY_MANIFEST_DIR is required}"
RUN_ROOT="${QUALITY_RUN_ROOT:?QUALITY_RUN_ROOT is required}"
OUTPUT_ROOT="${QUALITY_OUTPUT_ROOT:-${RUN_ROOT}}"
PYTHON="${QUALITY_PYTHON:-python3}"

# quality_filter_finalize.py imports `modelsafety.contract.textract`, an
# absolute package import added when the code was ported out of a flat
# sibling-file layout. Needs the repo root on PYTHONPATH to resolve.
export PYTHONPATH="${REPO}${PYTHONPATH:+:${PYTHONPATH}}"

"${PYTHON}" "${REPO}/modelsafety/decisions/quality_filter_finalize.py" aggregate \
  --input-root "${INPUT_ROOT}" \
  --manifest-dir "${MANIFEST_DIR}" \
  --output-root "${OUTPUT_ROOT}"
