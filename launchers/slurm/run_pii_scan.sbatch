#!/bin/bash
#SBATCH --job-name=pii_scan
#SBATCH --partition=health
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=02:00:00
#SBATCH --output=logs/slurm/pii_scan_%j.out
#SBATCH --error=logs/slurm/pii_scan_%j.err

set -euo pipefail

export PATH=/usr/bin:/bin:/usr/local/bin:$PATH

REPO="${PII_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
PII_DIR="${REPO}/modelsafety/data_screening/pii/regex"
VENV="${PII_DIR}/.venv"
# pii_scanner_fast.py imports `modelsafety.contract.textract`, an absolute
# package import added when the code was ported out of a flat sibling-file
# layout. It needs the repo root on PYTHONPATH to resolve.
export PYTHONPATH="${REPO}${PYTHONPATH:+:${PYTHONPATH}}"

export PII_BASE_DIR="${PII_BASE_DIR:-${REPO}/tmp/qa_scan_test/input}"
export PII_RUN_DIR="${PII_RUN_DIR:-${REPO}/tmp/qa_scan_test/pii_run}"
export PII_NUM_WORKERS="${PII_NUM_WORKERS:-${SLURM_CPUS_PER_TASK:-4}}"
export PII_CHUNK_SIZE="${PII_CHUNK_SIZE:-10000}"

RUN_ID="${SLURM_ARRAY_JOB_ID:-${SLURM_JOB_ID:-local}}"
PII_LOG_ROOT="${PII_LOG_ROOT:-${REPO}/logs/scans/pii_scan/run_${RUN_ID}}"
PII_MAIN_LOG="${PII_LOG_ROOT}/main/pii_scan_${RUN_ID}.log"
PII_ERR_LOG="${PII_LOG_ROOT}/err/pii_scan_${RUN_ID}.err"
PII_STAGE1_LOG="${PII_LOG_ROOT}/stage1/regex_scan_${RUN_ID}.log"
PII_STAGE2_LOG="${PII_LOG_ROOT}/stage2/heuristic_validation_${RUN_ID}.log"
PII_REPORT_FILE="${PII_REPORT_FILE:-${PII_LOG_ROOT}/reports/summary_${RUN_ID}.txt}"

mkdir -p \
  "${REPO}/logs/slurm" \
  "${PII_LOG_ROOT}/main" \
  "${PII_LOG_ROOT}/err" \
  "${PII_LOG_ROOT}/stage1" \
  "${PII_LOG_ROOT}/stage2" \
  "${PII_LOG_ROOT}/reports"

exec >>"${PII_MAIN_LOG}" 2>>"${PII_ERR_LOG}"

cd "${PII_DIR}"

if [[ ! -x "${VENV}/bin/python" ]]; then
  echo "Creating PII venv..."
  python3 -m venv "${VENV}"
  "${VENV}/bin/pip" install -U pip wheel
  "${VENV}/bin/pip" install -r requirements.txt
fi

source "${VENV}/bin/activate"

echo "============================================================"
echo "PII scan (stage 1 + 2)"
echo "============================================================"
echo "Node:           ${SLURMD_NODENAME:-unknown}"
echo "CPUs:           ${SLURM_CPUS_PER_TASK}"
echo "Input root:     ${PII_BASE_DIR}"
echo "Run root:       ${PII_RUN_DIR}"
echo "Log root:       ${PII_LOG_ROOT}"
echo "Main log:       ${PII_MAIN_LOG}"
echo "Error log:      ${PII_ERR_LOG}"
echo "Workers:        ${PII_NUM_WORKERS}"
echo "Chunk size:     ${PII_CHUNK_SIZE}"
echo

echo "--- Stage 1: regex scan ---"
python pii_scanner_fast.py 2>&1 | tee "${PII_STAGE1_LOG}"

echo
echo "--- Stage 2: heuristic validation ---"
python pii_validator.py 2>&1 | tee "${PII_STAGE2_LOG}"

echo
echo "============================================================"
echo "Done. Read summaries:"
echo "  ${PII_RUN_DIR}/stage1_scan/metrics.txt"
echo "  ${PII_RUN_DIR}/stage2_validated/metrics.txt"
echo "  ${PII_REPORT_FILE}"
echo "============================================================"
{
  cat "${PII_RUN_DIR}/stage1_scan/metrics.txt"
  echo
  cat "${PII_RUN_DIR}/stage2_validated/metrics.txt"
} | tee "${PII_REPORT_FILE}"
