#!/bin/bash
#SBATCH --job-name=medpsy_detoxify_node
#SBATCH --partition=health
#SBATCH --nodes=1
#SBATCH --gres=gpu:nvidia_h100_80gb_hbm3:8
#SBATCH --cpus-per-task=64
#SBATCH --mem=512G
#SBATCH --time=48:00:00
#SBATCH --output=synth_data_gen/logs/slurm/medpsy_detoxify_node_%j.out
#SBATCH --error=synth_data_gen/logs/slurm/medpsy_detoxify_node_%j.err

set -euo pipefail

REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}/synth_data_gen}"
TOX_DIR="${REPO}/tools/data_prep/toxicity"
VENV="${TOX_DIR}/.venv"
INPUT_ROOT="${QUALITY_INPUT_ROOT:?QUALITY_INPUT_ROOT is required}"
MANIFEST_DIR="${QUALITY_MANIFEST_DIR:?QUALITY_MANIFEST_DIR is required}"
RUN_ROOT="${QUALITY_RUN_ROOT:?QUALITY_RUN_ROOT is required}"
SHARD_IDS="${TOX_SHARD_IDS:?TOX_SHARD_IDS is required, for example 10,11,12}"
GPU_COUNT="${SLURM_GPUS_ON_NODE:-8}"

IFS="," read -r -a shards <<<"${SHARD_IDS}"
if ((${#shards[@]} > GPU_COUNT)); then
  echo "Requested ${#shards[@]} shard workers but only ${GPU_COUNT} GPUs are allocated" >&2
  exit 2
fi
if [[ ! -x "${VENV}/bin/python" ]]; then
  echo "Toxicity environment is missing: ${VENV}" >&2
  exit 2
fi

mkdir -p "${REPO}/logs/slurm"
declare -a pids=()
declare -a names=()

cleanup() {
  local pid
  for pid in "${pids[@]:-}"; do
    kill "${pid}" 2>/dev/null || true
  done
}
trap cleanup INT TERM

for index in "${!shards[@]}"; do
  shard_id="${shards[$index]}"
  if [[ ! "${shard_id}" =~ ^[0-9]+$ ]]; then
    echo "Invalid shard ID: ${shard_id}" >&2
    exit 2
  fi

  shard_name="$(printf 'shard_%05d' "${shard_id}")"
  manifest="${MANIFEST_DIR}/shards/${shard_name}.txt"
  shard_root="${RUN_ROOT}/scans/toxicity/shards/${shard_name}"
  flagged_file="${shard_root}/flagged.jsonl"
  worker_log="${shard_root}/detoxify_multigpu.log"

  if [[ -f "${shard_root}/_DETOXIFY_SUCCESS" ]]; then
    echo "${shard_name} is already complete; skipping"
    continue
  fi
  if [[ ! -f "${manifest}" ]]; then
    echo "Missing shard manifest: ${manifest}" >&2
    exit 2
  fi

  mkdir -p "${shard_root}"
  (
    export CUDA_VISIBLE_DEVICES="${index}"
    export TOKENIZERS_PARALLELISM=false
    cd "${TOX_DIR}"
    source "${VENV}/bin/activate"
    python run.py \
      --input-root "${INPUT_ROOT}" \
      --input-manifest "${manifest}" \
      --model_name "${TOX_STAGE1_MODEL:-multilingual}" \
      --dest_file "${flagged_file}" \
      --batch_size "${TOX_BATCH_SIZE:-32}" \
      --threshold "${TOX_THRESHOLD:-0.5}"
    touch "${shard_root}/_DETOXIFY_SUCCESS"
  ) >"${worker_log}" 2>&1 &
  pids+=("$!")
  names+=("${shard_name}")
  echo "Started ${shard_name} on local GPU ${index} (PID ${pids[-1]})"
done

failed=0
for index in "${!pids[@]}"; do
  if wait "${pids[$index]}"; then
    echo "Completed ${names[$index]}"
  else
    echo "Failed ${names[$index]}; inspect its detoxify_multigpu.log" >&2
    failed=1
  fi
done

exit "${failed}"
