#!/bin/bash
# Submit the complete MedPsy PII and toxicity filtering dependency graph.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${QUALITY_REPO:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
export QUALITY_INPUT_ROOT="${QUALITY_INPUT_ROOT:-${REPO}/output/medpsy2/baichuan_cleaning/filtered}"
export QUALITY_RUN_ROOT="${QUALITY_RUN_ROOT:-${REPO}/output/medpsy2/pii_toxicity_cleaning}"
export QUALITY_OUTPUT_ROOT="${QUALITY_OUTPUT_ROOT:-${QUALITY_RUN_ROOT}}"
export QUALITY_MANIFEST_DIR="${QUALITY_MANIFEST_DIR:-${QUALITY_RUN_ROOT}/manifests}"
export QUALITY_SHARD_COUNT="${QUALITY_SHARD_COUNT:-16}"
export QUALITY_PILOT_FILES="${QUALITY_PILOT_FILES:-0}"
export QUALITY_ARRAY_CONCURRENCY="${QUALITY_ARRAY_CONCURRENCY:-8}"
export QUALITY_PII_ARRAY_CONCURRENCY="${QUALITY_PII_ARRAY_CONCURRENCY:-4}"
export REPO

if [[ -z "${HF_TOKEN:-}" && -f "${REPO}/.env" ]]; then
  set -a
  source "${REPO}/.env"
  set +a
fi

if [[ ! -d "${QUALITY_INPUT_ROOT}" ]]; then
  echo "Input root does not exist: ${QUALITY_INPUT_ROOT}" >&2
  exit 2
fi
if [[ -e "${QUALITY_OUTPUT_ROOT}/_SUCCESS" && "${QUALITY_ALLOW_RERUN:-0}" != "1" ]]; then
  echo "Completed output already exists: ${QUALITY_OUTPUT_ROOT}" >&2
  echo "Set QUALITY_ALLOW_RERUN=1 only if an intentional rerun is required." >&2
  exit 2
fi
if [[ -e "${QUALITY_MANIFEST_DIR}/manifest.json" && "${QUALITY_ALLOW_RERUN:-0}" != "1" ]]; then
  echo "An existing run is present at ${QUALITY_RUN_ROOT}." >&2
  echo "Resume failed jobs directly, or set QUALITY_ALLOW_RERUN=1 for a clean worker-node rerun." >&2
  exit 2
fi
if (( QUALITY_SHARD_COUNT < 1 )); then
  echo "QUALITY_SHARD_COUNT must be at least 1" >&2
  exit 2
fi
if (( QUALITY_ARRAY_CONCURRENCY < 1 || QUALITY_PII_ARRAY_CONCURRENCY < 1 )); then
  echo "Array concurrency values must be at least 1" >&2
  exit 2
fi
if (( QUALITY_PILOT_FILES > 0 && QUALITY_PILOT_FILES < QUALITY_SHARD_COUNT )); then
  export QUALITY_SHARD_COUNT="${QUALITY_PILOT_FILES}"
fi

mkdir -p "${REPO}/logs/slurm"
array_spec="0-$((QUALITY_SHARD_COUNT - 1))%${QUALITY_ARRAY_CONCURRENCY}"
pii_array_spec="0-$((QUALITY_SHARD_COUNT - 1))%${QUALITY_PII_ARRAY_CONCURRENCY}"
submitted_jobs=()
cancel_partial_submission() {
  if (( ${#submitted_jobs[@]} )); then
    scancel "${submitted_jobs[@]}" 2>/dev/null || true
  fi
}
trap cancel_partial_submission ERR

prep_job="$(sbatch --parsable "${REPO}/tools/data_prep/prepare_quality_filter.sbatch")"
prep_job="${prep_job%%;*}"
submitted_jobs+=("${prep_job}")
pii_job="$(sbatch --parsable \
  --dependency="afterok:${prep_job}" \
  --array="${pii_array_spec}" \
  "${REPO}/tools/data_prep/pii_scan/run_pii_scan_array.sbatch")"
pii_job="${pii_job%%;*}"
submitted_jobs+=("${pii_job}")
toxicity_job="$(sbatch --parsable \
  --dependency="afterok:${prep_job}" \
  --array="${array_spec}" \
  "${REPO}/tools/data_prep/toxicity/run_toxicity_scan_array.sbatch")"
toxicity_job="${toxicity_job%%;*}"
submitted_jobs+=("${toxicity_job}")
pii_llm_job="$(sbatch --parsable \
  --dependency="afterok:${pii_job}" \
  --array="${array_spec}" \
  "${REPO}/tools/data_prep/pii_scan/run_pii_llm.sbatch")"
pii_llm_job="${pii_llm_job%%;*}"
submitted_jobs+=("${pii_llm_job}")
finalize_job="$(sbatch --parsable \
  --dependency="afterok:${pii_llm_job}:${toxicity_job}" \
  --array="${array_spec}" \
  "${REPO}/tools/data_prep/run_quality_filter_finalize.sbatch")"
finalize_job="${finalize_job%%;*}"
submitted_jobs+=("${finalize_job}")
report_job="$(sbatch --parsable \
  --dependency="afterok:${finalize_job}" \
  "${REPO}/tools/data_prep/run_quality_filter_report.sbatch")"
report_job="${report_job%%;*}"
submitted_jobs+=("${report_job}")
trap - ERR

echo "Submitted MedPsy quality-filter pipeline"
echo "  prepare:       ${prep_job}"
echo "  PII CPU array: ${pii_job}"
echo "  toxicity:      ${toxicity_job}"
echo "  PII Gemma:     ${pii_llm_job}"
echo "  final filter:  ${finalize_job}"
echo "  report:        ${report_job}"
echo "Final success marker: ${QUALITY_OUTPUT_ROOT}/_SUCCESS"
