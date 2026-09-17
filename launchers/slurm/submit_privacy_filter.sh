#!/bin/bash
# Submit the standalone Privacy Filter manifest and GPU scan jobs.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
PIPELINE_DIR="${REPO}/modelsafety/data_screening/pii/model"
INPUT_ROOT="${PRIVACY_FILTER_INPUT_ROOT:-${REPO}/output/medpsy2/baichuan_cleaning/filtered}"
RUN_ROOT="${PRIVACY_FILTER_RUN_ROOT:-${REPO}/output/medpsy2/privacy_filter_nemotron}"
MODE="${PRIVACY_FILTER_MODE:-pilot}"
BACKEND="${PRIVACY_FILTER_BACKEND:-transformers}"
MODEL="${PRIVACY_FILTER_MODEL:-OpenMed/privacy-filter-nemotron-v2}"

if [[ "${MODE}" != "pilot" && "${MODE}" != "full" ]]; then
  echo "PRIVACY_FILTER_MODE must be pilot or full" >&2
  exit 2
fi
mkdir -p "${REPO}/logs/slurm" "${RUN_ROOT}"

export_values="ALL,REPO=${REPO},PRIVACY_FILTER_INPUT_ROOT=${INPUT_ROOT},PRIVACY_FILTER_RUN_ROOT=${RUN_ROOT},PRIVACY_FILTER_BACKEND=${BACKEND},PRIVACY_FILTER_MODEL=${MODEL}"
if [[ -n "${PRIVACY_FILTER_SOURCE_MANIFEST:-}" ]]; then
  export_values+=",PRIVACY_FILTER_SOURCE_MANIFEST=${PRIVACY_FILTER_SOURCE_MANIFEST}"
fi

manifest="${RUN_ROOT}/manifest/work_units.jsonl"
dependency=()
if [[ ! -f "${manifest}" ]]; then
  if [[ "${MODE}" == "pilot" ]]; then
    export_values+=",PRIVACY_FILTER_UNIT_MIB=${PRIVACY_FILTER_UNIT_MIB:-32},PRIVACY_FILTER_MAX_FILES=${PRIVACY_FILTER_MAX_FILES:-1}"
  else
    export_values+=",PRIVACY_FILTER_UNIT_MIB=${PRIVACY_FILTER_UNIT_MIB:-256}"
  fi
  prepare_job="$(
    sbatch --parsable --export="${export_values}" \
      "${SCRIPT_DIR}/prepare_privacy_filter.sbatch"
  )"
  dependency=(--dependency="afterok:${prepare_job}")
  echo "Manifest job: ${prepare_job}"
fi

if [[ "${MODE}" == "pilot" ]]; then
  export_values+=",PRIVACY_FILTER_MAX_UNITS=${PRIVACY_FILTER_MAX_UNITS:-1}"
  scan_script="${SCRIPT_DIR}/run_privacy_filter_pilot.sbatch"
else
  scan_script="${SCRIPT_DIR}/run_privacy_filter_node.sbatch"
fi
scan_job="$(
  sbatch --parsable "${dependency[@]}" --export="${export_values}" "${scan_script}"
)"
echo "Privacy Filter ${MODE} job: ${scan_job}"
echo "Run root: ${RUN_ROOT}"

