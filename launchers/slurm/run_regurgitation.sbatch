#!/bin/bash
#SBATCH --job-name=regurgitation
#SBATCH --partition=background
#SBATCH --gres=gpu:nvidia_h100_80gb_hbm3:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=128G
#SBATCH --time=24:00:00
#SBATCH --output=logs/slurm/tool_team_regurgitation_%j.out
#SBATCH --error=logs/slurm/tool_team_regurgitation_%j.err
#
# Run the PII regurgitation probe
# (modelsafety/model_screening/regurgitation/pii_regurgitation_check.py,
# unchanged from medpsy) against one checkpoint: prepare the probe input,
# start one or more vLLM replicas of the checkpoint under test, wait for
# them to come up, run the probe against all of them, then tear the
# servers down.
#
# The #SBATCH header above is only a safe single-GPU default for a direct
# `sbatch` call -- always submitted through scripts/tool_team_regurgitation.sh
# in practice, which passes the real --partition/--gres/--cpus-per-task/
# --mem/--time on the sbatch command line (those win over this header) and
# writes the config file this script sources. See that script for the
# settings review/edit step.
#
# Usage (direct submission, without the wrapper):
#   sbatch --partition=<p> --gres=gpu:<type>:<n> --cpus-per-task=<c> --mem=<m> --time=<t> \
#     --export=ALL,REGURG_CONFIG=<run_dir>/run_config.env \
#     launchers/slurm/run_regurgitation.sbatch

set -euo pipefail

REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
REGURG_DIR="${REPO}/modelsafety/model_screening/regurgitation"
# prepare_probe_input.py and pii_regurgitation_check.py both import
# modelsafety.contract.textract as an absolute package import -- needs the
# repo root on PYTHONPATH to resolve, same as every other launcher here.
export PYTHONPATH="${REPO}${PYTHONPATH:+:${PYTHONPATH}}"

CONFIG="${REGURG_CONFIG:?REGURG_CONFIG is required (path to a run_config.env written by scripts/tool_team_regurgitation.sh)}"
if [[ ! -f "${CONFIG}" ]]; then
  echo "error: config file not found: ${CONFIG}" >&2
  exit 2
fi
# shellcheck disable=SC1090
source "${CONFIG}"

# --- Required settings, all written into run_config.env by the wrapper ---
CHECKPOINT_PATH="${REGURG_CHECKPOINT_PATH:?REGURG_CHECKPOINT_PATH missing from ${CONFIG}}"
RUN_DIR="${REGURG_RUN_DIR:?REGURG_RUN_DIR missing from ${CONFIG}}"
INPUT_CSV="${REGURG_INPUT_CSV:?REGURG_INPUT_CSV missing from ${CONFIG}}"
SERVED_NAME="${REGURG_SERVED_NAME:?REGURG_SERVED_NAME missing from ${CONFIG}}"
GPU_COUNT="${REGURG_GPU_COUNT:?REGURG_GPU_COUNT missing from ${CONFIG}}"
TP_SIZE="${REGURG_TP_SIZE:?REGURG_TP_SIZE missing from ${CONFIG}}"
DTYPE="${REGURG_DTYPE:?REGURG_DTYPE missing from ${CONFIG}}"
MAX_MODEL_LEN="${REGURG_MAX_MODEL_LEN:?REGURG_MAX_MODEL_LEN missing from ${CONFIG}}"
THINKING="${REGURG_THINKING:?REGURG_THINKING missing from ${CONFIG}}"
MAX_TOKENS="${REGURG_MAX_TOKENS:?REGURG_MAX_TOKENS missing from ${CONFIG}}"
TRIALS="${REGURG_TRIALS:?REGURG_TRIALS missing from ${CONFIG}}"
TEMPERATURE="${REGURG_TEMPERATURE:?REGURG_TEMPERATURE missing from ${CONFIG}}"
MAX_REPEATS="${REGURG_MAX_REPEATS:?REGURG_MAX_REPEATS missing from ${CONFIG}}"
SAMPLE_SIZE="${REGURG_SAMPLE_SIZE:?REGURG_SAMPLE_SIZE missing from ${CONFIG}}"

# --- Settings with a fallback (present in every config the wrapper writes,
# but tolerant of a hand-edited config missing one) --------------------
FAMILY_FLAGS_JSON="${REGURG_FAMILY_FLAGS_JSON:-[]}"
# The default value here MUST be quoted ("{}", not {}): bash's parameter
# expansion parser treats an unquoted {} in a ${VAR:-default} default as a
# nested brace pair and appends a spurious extra "}" to the result once
# REGURG_SAMPLING_JSON is actually set -- reproduced and confirmed while
# writing this script. Quoting the default avoids that parse entirely.
SAMPLING_JSON="${REGURG_SAMPLING_JSON:-"{}"}"
EXTRA_VLLM_ARGS="${REGURG_EXTRA_VLLM_ARGS:-}"
MAX_CONTEXT_CHARS="${REGURG_MAX_CONTEXT_CHARS:-150000}"
REQUESTS_PER_SERVER="${REGURG_REQUESTS_PER_SERVER:-64}"
VLLM_BIN="${REGURG_VLLM_BIN:-/home/shared/agentic_slm/qvac-research-tool-call/evaluation/services/vllm/.venv/bin/vllm}"
# VLLM_STARTUP_TIMEOUT: bare name on purpose -- matches the existing
# convention in launchers/slurm/run_pii_llm.sbatch and
# run_pii_llm_full_node.sbatch, so an operator who already knows those
# scripts can override this one the same way.
VLLM_STARTUP_TIMEOUT="${VLLM_STARTUP_TIMEOUT:-1800}"
HF_HOME="${HF_HOME:-${REPO}/cache/hf}"

if (( GPU_COUNT % TP_SIZE != 0 )); then
  echo "error: REGURG_GPU_COUNT (${GPU_COUNT}) is not divisible by REGURG_TP_SIZE (${TP_SIZE})" >&2
  exit 2
fi
REPLICA_COUNT=$((GPU_COUNT / TP_SIZE))
if (( REPLICA_COUNT < 1 )); then
  echo "error: computed 0 vLLM replicas from GPU_COUNT=${GPU_COUNT} TP_SIZE=${TP_SIZE}" >&2
  exit 2
fi

if [[ ! -x "${VLLM_BIN}" ]]; then
  echo "error: vLLM binary not found or not executable: ${VLLM_BIN}" >&2
  exit 2
fi
if [[ ! -f "${CHECKPOINT_PATH}/config.json" ]]; then
  echo "error: no config.json under checkpoint: ${CHECKPOINT_PATH}" >&2
  exit 2
fi
if [[ ! -f "${INPUT_CSV}" ]]; then
  echo "error: input CSV does not exist: ${INPUT_CSV}" >&2
  exit 2
fi

LOG_DIR="${RUN_DIR}/logs"
RESULTS_DIR="${RUN_DIR}/results"
PROBE_INPUT_CSV="${RUN_DIR}/probe_input.csv"
mkdir -p "${REPO}/logs/slurm" "${LOG_DIR}" "${RESULTS_DIR}" "${HF_HOME}"

# The token, if any, only comes from your own environment; no file in the repo is read for it.
# The checkpoint is always a local directory (vllm_config.py refuses
# anything else), so serving it needs no network access. Offline mode
# turns an accidental Hub call into a clear failure instead of a hang.
export HF_HOME
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_TOKEN="${HF_TOKEN:-}"

# ---------------------------------------------------------------------------
# Step 1: prepare the probe input (skip if a previous attempt already
# produced it -- lets a failed/killed job be resubmitted without re-reading
# the raw CSV).
# ---------------------------------------------------------------------------
if [[ -f "${PROBE_INPUT_CSV}" ]]; then
  echo "Reusing existing probe input: ${PROBE_INPUT_CSV}"
else
  echo "Preparing probe input from ${INPUT_CSV}"
  python3 "${REGURG_DIR}/prepare_probe_input.py" \
    --input "${INPUT_CSV}" \
    --output "${PROBE_INPUT_CSV}" \
    --max-repeats "${MAX_REPEATS}" \
    --sample "${SAMPLE_SIZE}"
fi

# ---------------------------------------------------------------------------
# Steps 2-4: serve the checkpoint and run the probe against it in one call.
#
# modelsafety/serving/serve.py's own "run" command starts REPLICA_COUNT
# vLLM replicas (TP_SIZE GPUs each), waits until every one of them is
# actually serving SERVED_NAME -- not just answering on the port, see
# serve.py's own port-sharing check -- runs the probe below with
# {base_urls} expanded to their comma-separated OpenAI-compatible base
# URLs, then stops every replica no matter how the probe ended or whether
# this job's time limit hit first. See modelsafety/serving/serve.py.
#
# THINKING (0/1, this launcher's own convention, already resolved by
# derive_checkpoint_settings() in scripts/tool_team_regurgitation.sh)
# becomes serve.py's --thinking (on/off) -- serve.py also has an "auto"
# mode, not used here, since there is nothing left to auto-detect by the
# time this script runs.
# ---------------------------------------------------------------------------
THINKING_MODE="off"
if [[ "${THINKING}" == "1" ]]; then
  THINKING_MODE="on"
fi

echo
echo "Serving ${REPLICA_COUNT} replica(s), TP=${TP_SIZE}, served as '${SERVED_NAME}', then running the probe"
# --extra-args="${EXTRA_VLLM_ARGS}" (=, not a space) matters whenever
# EXTRA_VLLM_ARGS itself starts with "-" (e.g. "--trust-remote-code"):
# argparse only accepts that in the --flag=value form -- a space-separated
# --extra-args "--trust-remote-code" fails, reading the second word as its
# own unrecognized flag.
python3 -m modelsafety.serving.serve run \
  --checkpoint "${CHECKPOINT_PATH}" \
  --served-name "${SERVED_NAME}" \
  --tp-size "${TP_SIZE}" \
  --replicas "${REPLICA_COUNT}" \
  --dtype "${DTYPE}" \
  --max-model-len "${MAX_MODEL_LEN}" \
  --thinking "${THINKING_MODE}" \
  --family-flags-json "${FAMILY_FLAGS_JSON}" \
  --sampling-json "${SAMPLING_JSON}" \
  --extra-args="${EXTRA_VLLM_ARGS}" \
  --vllm-bin "${VLLM_BIN}" \
  --log-dir "${LOG_DIR}" \
  --ready-timeout "${VLLM_STARTUP_TIMEOUT}" \
  -- \
  python3 "${REGURG_DIR}/pii_regurgitation_check.py" \
    --input "${PROBE_INPUT_CSV}" \
    --output-dir "${RESULTS_DIR}" \
    --base-url "{base_urls}" \
    --model "${SERVED_NAME}" \
    --trials "${TRIALS}" \
    --temperature "${TEMPERATURE}" \
    --max-tokens "${MAX_TOKENS}" \
    --max-context-chars "${MAX_CONTEXT_CHARS}" \
    --requests-per-server "${REQUESTS_PER_SERVER}" \
    --resume

touch "${RUN_DIR}/_SUCCESS"
echo
echo "Done. Results under ${RESULTS_DIR}"
