#!/bin/bash
#SBATCH --job-name=dedup_scan
#SBATCH --partition=health
#SBATCH --cpus-per-task=16
#SBATCH --mem=128G
#SBATCH --time=24:00:00
#SBATCH --output=synth_data_gen/logs/slurm/dedup_scan_%j.out
#SBATCH --error=synth_data_gen/logs/slurm/dedup_scan_%j.err

set -euo pipefail

export PATH=/usr/bin:/bin:/usr/local/bin:$PATH

REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}/synth_data_gen}"
DEDUP_DIR="${REPO}/tools/data_prep/dedup"
VENV="${DEDUP_DIR}/.venv"

DEDUP_INPUT_ROOT="${DEDUP_INPUT_ROOT:-${REPO}/tmp/qa_scan_test/input}"
DEDUP_INPUT_MANIFEST="${DEDUP_INPUT_MANIFEST:-}"
DEDUP_REFERENCE_ROOT="${DEDUP_REFERENCE_ROOT:-}"
DEDUP_CANDIDATE_ROOT="${DEDUP_CANDIDATE_ROOT:-}"
DEDUP_RUN_DIR="${DEDUP_RUN_DIR:-${REPO}/tmp/qa_scan_test/dedup_out}"
DEDUP_TEXT_SCOPE="${DEDUP_TEXT_SCOPE:-prompt}"
DEDUP_NGRAM_SIZE="${DEDUP_NGRAM_SIZE:-60}"
DEDUP_NUM_PERM="${DEDUP_NUM_PERM:-256}"
DEDUP_BANDS="${DEDUP_BANDS:-32}"
DEDUP_ROWS_PER_BAND="${DEDUP_ROWS_PER_BAND:-8}"
DEDUP_WORKERS="${DEDUP_WORKERS:-${SLURM_CPUS_PER_TASK:-16}}"
DEDUP_CHUNKSIZE="${DEDUP_CHUNKSIZE:-100}"
DEDUP_REPORT_TEXT_CHARS="${DEDUP_REPORT_TEXT_CHARS:-2000}"
DEDUP_WRITE_FILTERED="${DEDUP_WRITE_FILTERED:-1}"
DEDUP_OVERWRITE="${DEDUP_OVERWRITE:-0}"

RUN_ID="${SLURM_JOB_ID:-local}"
DEDUP_LOG_ROOT="${DEDUP_LOG_ROOT:-${REPO}/logs/scans/dedup/run_${RUN_ID}}"
DEDUP_MAIN_LOG="${DEDUP_LOG_ROOT}/main/dedup_${RUN_ID}.log"
DEDUP_ERR_LOG="${DEDUP_LOG_ROOT}/err/dedup_${RUN_ID}.err"

mkdir -p \
  "${REPO}/logs/slurm" \
  "${DEDUP_LOG_ROOT}/main" \
  "${DEDUP_LOG_ROOT}/err"
exec >>"${DEDUP_MAIN_LOG}" 2>>"${DEDUP_ERR_LOG}"

(
  flock 9
  if [[ ! -x "${VENV}/bin/python" ]]; then
    echo "Creating dedup venv at ${VENV}"
    python3 -m venv "${VENV}"
    "${VENV}/bin/pip" install -U pip wheel
    "${VENV}/bin/pip" install -r "${DEDUP_DIR}/requirements.txt"
  fi
) 9>"${DEDUP_DIR}/.venv.lock"

args=(
  --output-dir "${DEDUP_RUN_DIR}"
  --text-scope "${DEDUP_TEXT_SCOPE}"
  --ngram-size "${DEDUP_NGRAM_SIZE}"
  --num-perm "${DEDUP_NUM_PERM}"
  --bands "${DEDUP_BANDS}"
  --rows-per-band "${DEDUP_ROWS_PER_BAND}"
  --workers "${DEDUP_WORKERS}"
  --chunksize "${DEDUP_CHUNKSIZE}"
  --report-text-chars "${DEDUP_REPORT_TEXT_CHARS}"
)

if [[ -n "${DEDUP_REFERENCE_ROOT}" || -n "${DEDUP_CANDIDATE_ROOT}" ]]; then
  if [[ -z "${DEDUP_REFERENCE_ROOT}" || -z "${DEDUP_CANDIDATE_ROOT}" ]]; then
    echo "Both DEDUP_REFERENCE_ROOT and DEDUP_CANDIDATE_ROOT are required" >&2
    exit 2
  fi
  if [[ -n "${DEDUP_INPUT_MANIFEST}" ]]; then
    echo "DEDUP_INPUT_MANIFEST is not supported in cross mode" >&2
    exit 2
  fi
  args+=(
    --reference-input "${DEDUP_REFERENCE_ROOT}"
    --candidate-input "${DEDUP_CANDIDATE_ROOT}"
  )
  DEDUP_MODE="cross"
elif [[ -n "${DEDUP_INPUT_MANIFEST}" ]]; then
  args+=(--input-root "${DEDUP_INPUT_ROOT}" --input-manifest "${DEDUP_INPUT_MANIFEST}")
  DEDUP_MODE="global"
else
  args+=(--input "${DEDUP_INPUT_ROOT}")
  DEDUP_MODE="global"
fi
if [[ "${DEDUP_WRITE_FILTERED}" != "1" ]]; then
  args+=(--no-write-filtered)
fi
if [[ "${DEDUP_OVERWRITE}" == "1" ]]; then
  args+=(--overwrite)
fi

echo "============================================================"
echo "MinHash/LSH dedup scan"
echo "============================================================"
echo "Node:          ${SLURMD_NODENAME:-unknown}"
echo "CPUs:          ${SLURM_CPUS_PER_TASK:-unknown}"
echo "Memory:        ${SLURM_MEM_PER_NODE:-unknown} MB"
echo "Mode:          ${DEDUP_MODE}"
echo "Input root:    ${DEDUP_INPUT_ROOT}"
echo "Manifest:      ${DEDUP_INPUT_MANIFEST:-none}"
echo "Reference:     ${DEDUP_REFERENCE_ROOT:-none}"
echo "Candidate:     ${DEDUP_CANDIDATE_ROOT:-none}"
echo "Run dir:       ${DEDUP_RUN_DIR}"
echo "Text scope:    ${DEDUP_TEXT_SCOPE}"
echo "N-gram size:   ${DEDUP_NGRAM_SIZE}"
echo "LSH shape:     ${DEDUP_BANDS} x ${DEDUP_ROWS_PER_BAND}"
echo "Workers:       ${DEDUP_WORKERS}"
echo "CSV text max:  ${DEDUP_REPORT_TEXT_CHARS}"
echo "Write output:  ${DEDUP_WRITE_FILTERED}"
echo "Log root:      ${DEDUP_LOG_ROOT}"
echo

cd "${DEDUP_DIR}"
source "${VENV}/bin/activate"
python run.py "${args[@]}"
if [[ "${DEDUP_MODE}" == "cross" ]]; then
  python summarize_cross_matches.py \
    --matched-samples "${DEDUP_RUN_DIR}/matched_samples.csv" \
    --output-dir "${DEDUP_RUN_DIR}"
fi

echo
echo "Done. Summary: ${DEDUP_RUN_DIR}/summary.json"
