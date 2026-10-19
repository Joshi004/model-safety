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
PARTITION="${PRIVACY_FILTER_PARTITION:-background}"
# The default input above (the medpsy filtered folder) isn't a run folder, so
# this script reads it as a plain folder unless told otherwise. The tool team
# wrapper passes "runs".
INPUT_MODE="${PRIVACY_FILTER_INPUT_MODE:-folder}"

if [[ "${MODE}" != "pilot" && "${MODE}" != "full" ]]; then
  echo "PRIVACY_FILTER_MODE must be pilot or full" >&2
  exit 2
fi
if [[ "${INPUT_MODE}" != "runs" && "${INPUT_MODE}" != "folder" ]]; then
  echo "PRIVACY_FILTER_INPUT_MODE must be runs or folder" >&2
  exit 2
fi
mkdir -p "${REPO}/logs/slurm" "${RUN_ROOT}"

export_values="ALL,REPO=${REPO},PRIVACY_FILTER_INPUT_ROOT=${INPUT_ROOT},PRIVACY_FILTER_INPUT_MODE=${INPUT_MODE},PRIVACY_FILTER_RUN_ROOT=${RUN_ROOT},PRIVACY_FILTER_BACKEND=${BACKEND},PRIVACY_FILTER_MODEL=${MODEL}"
if [[ -n "${PRIVACY_FILTER_SOURCE_MANIFEST:-}" ]]; then
  export_values+=",PRIVACY_FILTER_SOURCE_MANIFEST=${PRIVACY_FILTER_SOURCE_MANIFEST}"
fi

policy="${PRIVACY_FILTER_POLICY:-${PIPELINE_DIR}/policy.json}"
if [[ ! -f "${policy}" ]]; then
  echo "Policy file not found: ${policy}" >&2
  exit 2
fi
if [[ "${MODE}" == "pilot" ]]; then
  max_files="${PRIVACY_FILTER_MAX_FILES:-1}"
  max_units="${PRIVACY_FILTER_MAX_UNITS:-1}"
else
  max_files="${PRIVACY_FILTER_MAX_FILES:-}"
  max_units="${PRIVACY_FILTER_MAX_UNITS:-0}"
fi

# Refuses (and stops this script, so nothing is submitted) if RUN_ROOT already
# holds a different run, such as a pilot being followed by a full scan.
identity_args=(
  check
  --run-dir "${RUN_ROOT}"
  --tool pii_model
  --mode "${MODE}"
  --input-root "${INPUT_ROOT}"
  --input-mode "${INPUT_MODE}"
  --setting "input_mode=${INPUT_MODE}"
  --setting "backend=${BACKEND}"
  --setting "model=${MODEL}"
  --setting "max_files=${max_files:-all}"
  --setting "max_units=${max_units}"
  --setting "policy_sha256=$(sha256sum "${policy}" | awk '{print $1}')"
)
if [[ -n "${PRIVACY_FILTER_SOURCE_MANIFEST:-}" ]]; then
  identity_args+=(--input-list "${PRIVACY_FILTER_SOURCE_MANIFEST}")
fi
identity_state="$(python3 "${REPO}/modelsafety/contract/run_identity.py" "${identity_args[@]}")"
echo "Run identity: ${identity_state}"

manifest="${RUN_ROOT}/manifest/work_units.jsonl"
dependency=()
if [[ ! -f "${manifest}" ]]; then
  if [[ "${MODE}" == "pilot" ]]; then
    export_values+=",PRIVACY_FILTER_UNIT_MIB=${PRIVACY_FILTER_UNIT_MIB:-32},PRIVACY_FILTER_MAX_FILES=${max_files}"
  else
    export_values+=",PRIVACY_FILTER_UNIT_MIB=${PRIVACY_FILTER_UNIT_MIB:-256}"
  fi
  prepare_job="$(
    sbatch --parsable --partition="${PARTITION}" --export="${export_values}" \
      "${SCRIPT_DIR}/prepare_privacy_filter.sbatch"
  )"
  dependency=(--dependency="afterok:${prepare_job}")
  # Opt in: without it, a failed manifest job leaves the scan job pending forever on a
  # cluster that doesn't remove jobs whose dependency can never be met.
  if [[ "${PRIVACY_FILTER_KILL_ON_INVALID_DEP:-0}" == "1" ]]; then
    dependency+=(--kill-on-invalid-dep=yes)
  fi
  echo "Manifest job: ${prepare_job}"
fi

if [[ "${MODE}" == "pilot" ]]; then
  export_values+=",PRIVACY_FILTER_MAX_UNITS=${max_units}"
  scan_script="${SCRIPT_DIR}/run_privacy_filter_pilot.sbatch"
else
  scan_script="${SCRIPT_DIR}/run_privacy_filter_node.sbatch"
fi
scan_job="$(
  sbatch --parsable --partition="${PARTITION}" "${dependency[@]}" --export="${export_values}" "${scan_script}"
)"
echo "Privacy Filter ${MODE} job: ${scan_job}"
echo "Partition: ${PARTITION}"
echo "Run root: ${RUN_ROOT}"

