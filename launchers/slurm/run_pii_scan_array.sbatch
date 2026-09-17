#!/bin/bash
#SBATCH --job-name=medpsy_pii_cpu
#SBATCH --partition=health
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=64
#SBATCH --mem=64G
#SBATCH --time=24:00:00
#SBATCH --output=synth_data_gen/logs/slurm/medpsy_pii_cpu_%A_%a.out
#SBATCH --error=synth_data_gen/logs/slurm/medpsy_pii_cpu_%A_%a.err

# Intentionally non-exclusive: this CPU-only job must leave GPUs and the
# remaining node CPUs available to other workloads.

set -euo pipefail

REPO="${PII_REPO:-${SLURM_SUBMIT_DIR:-$PWD}/synth_data_gen}"
PII_DIR="${REPO}/tools/data_prep/pii_scan"
VENV="${PII_DIR}/.venv"
INPUT_ROOT="${QUALITY_INPUT_ROOT:?QUALITY_INPUT_ROOT is required}"
MANIFEST_DIR="${QUALITY_MANIFEST_DIR:?QUALITY_MANIFEST_DIR is required}"
RUN_ROOT="${QUALITY_RUN_ROOT:?QUALITY_RUN_ROOT is required}"
SHARD_ID="${SLURM_ARRAY_TASK_ID:?Submit this script as a SLURM array}"
SHARD_NAME="$(printf 'shard_%05d' "${SHARD_ID}")"
MANIFEST="${MANIFEST_DIR}/shards/${SHARD_NAME}.txt"

if [[ ! -f "${MANIFEST}" ]]; then
  echo "Missing shard manifest: ${MANIFEST}" >&2
  exit 2
fi

mkdir -p "${REPO}/logs/slurm" "${RUN_ROOT}/scans/pii/shards"
(
  flock 9
  if [[ ! -x "${VENV}/bin/python" ]]; then
    python3 -m venv "${VENV}"
    "${VENV}/bin/pip" install -U pip wheel
    "${VENV}/bin/pip" install -r "${PII_DIR}/requirements.txt"
  fi
) 9>"${PII_DIR}/.venv.lock"

export PII_BASE_DIR="${INPUT_ROOT}"
export PII_INPUT_MANIFEST="${MANIFEST}"
export PII_SNAPSHOT_MANIFEST="${MANIFEST_DIR}/manifest.json"
export PII_RUN_DIR="${RUN_ROOT}/scans/pii/shards/${SHARD_NAME}"
export PII_NUM_WORKERS="${PII_NUM_WORKERS:-${SLURM_CPUS_PER_TASK:-64}}"
export PII_CHUNK_SIZE="${PII_CHUNK_SIZE:-10000}"

mkdir -p "${PII_RUN_DIR}/stage3_extract"
rm -f "${PII_RUN_DIR}/_CPU_SUCCESS"
cd "${PII_DIR}"
source "${VENV}/bin/activate"

python pii_scanner_fast.py
python pii_validator.py
python pii_extractor.py \
  --input-dir "${PII_RUN_DIR}/stage2_validated/hits" \
  --source-root "${INPUT_ROOT}" \
  --snapshot-manifest "${PII_SNAPSHOT_MANIFEST}" \
  --output "${PII_RUN_DIR}/stage3_extract/pii_extract.csv"

touch "${PII_RUN_DIR}/_CPU_SUCCESS"
