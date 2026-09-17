#!/bin/bash
#SBATCH --job-name=privacy_filter_compare
#SBATCH --partition=health
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=24:00:00
#SBATCH --output=synth_data_gen/logs/slurm/privacy_filter_compare_%j.out
#SBATCH --error=synth_data_gen/logs/slurm/privacy_filter_compare_%j.err

set -euo pipefail

REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}/synth_data_gen}"
PIPELINE_DIR="${REPO}/tools/data_prep/pii_privacy_filter"
RUN_ROOT="${PRIVACY_FILTER_RUN_ROOT:?PRIVACY_FILTER_RUN_ROOT is required}"
REGEX_INPUT="${PRIVACY_FILTER_REGEX_INPUT:?PRIVACY_FILTER_REGEX_INPUT is required}"
VENV="${PRIVACY_FILTER_VENV:-${PIPELINE_DIR}/.venv-isolated}"

if [[ ! -x "${VENV}/bin/python" ]]; then
  echo "Missing Privacy Filter environment: ${VENV}" >&2
  exit 2
fi
mkdir -p "${REPO}/logs/slurm"
cd "${PIPELINE_DIR}"
"${VENV}/bin/python" compare_regex.py \
  --model-spans "${RUN_ROOT}/all_spans.jsonl" \
  --regex-input "${REGEX_INPUT}" \
  --output-dir "${RUN_ROOT}/regex_comparison" \
  --work-manifest "${RUN_ROOT}/manifest/work_units.jsonl" \
  --policy "${PRIVACY_FILTER_POLICY:-${PIPELINE_DIR}/policy.json}" \
  --sample-size "${PRIVACY_FILTER_COMPARISON_SAMPLE_SIZE:-500}"

