#!/bin/bash
#SBATCH --job-name=medpsy_final_clean
#SBATCH --partition=health
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --time=24:00:00
#SBATCH --output=logs/slurm/medpsy_final_clean_%j.out
#SBATCH --error=logs/slurm/medpsy_final_clean_%j.err

set -euo pipefail

REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
MEDPSY_ROOT="${MEDPSY_ROOT:-${REPO}/output/medpsy2}"
SOURCE_ROOT="${FINAL_SOURCE_ROOT:-${MEDPSY_ROOT}/baichuan_cleaning/filtered}"
QUALITY_ROOT="${FINAL_QUALITY_ROOT:-${MEDPSY_ROOT}/pii_toxicity_cleaning}"
DECONTAMINATION_ROOT="${FINAL_DECONTAMINATION_ROOT:-${MEDPSY_ROOT}/baichuan_cleaning_decontaminated_general_sources_only}"
BAICHUAN_REPORT="${FINAL_BAICHUAN_REPORT:-${MEDPSY_ROOT}/baichuan_cleaning/report.json}"
BAICHUAN_LEGACY_REPORT="${FINAL_BAICHUAN_LEGACY_REPORT:-${MEDPSY_ROOT}/baichuan_cleaning/legacy_import_report.json}"
OUTPUT_ROOT="${FINAL_OUTPUT_ROOT:-${MEDPSY_ROOT}/final_cleaned_data}"
PYTHON="${QUALITY_PYTHON:-python3}"

if ! command -v "${PYTHON}" >/dev/null 2>&1 && [[ ! -x "${PYTHON}" ]]; then
  echo "Python environment not found: ${PYTHON}" >&2
  exit 2
fi

mkdir -p "${REPO}/logs/slurm"
"${PYTHON}" "${REPO}/modelsafety/decisions/finalize_medpsy_cleaning.py" \
  --source-root "${SOURCE_ROOT}" \
  --quality-root "${QUALITY_ROOT}" \
  --decontamination-root "${DECONTAMINATION_ROOT}" \
  --baichuan-report "${BAICHUAN_REPORT}" \
  --baichuan-report "${BAICHUAN_LEGACY_REPORT}" \
  --output-root "${OUTPUT_ROOT}"
