#!/bin/bash
#SBATCH --job-name=prepare_rows
#SBATCH --partition=background
#SBATCH --cpus-per-task=2
#SBATCH --mem=32G
#SBATCH --time=04:00:00
#SBATCH --output=logs/slurm/prepare_rows_%j.out
#SBATCH --error=logs/slurm/prepare_rows_%j.err

# One prepare_rows.py conversion, CPU only. Every argument is passed straight
# through to modelsafety/readers/prepare_rows.py:
#
#   sbatch launchers/slurm/prepare_rows.sbatch \
#     --input-path P --input-type jsonl --output-dir D --map text=question
#
# --mem, --cpus-per-task and --time are all set above on purpose: this
# cluster gives a job without --mem the whole node's memory (which leaves the
# node's GPUs idle), and a job without --time has no limit. Submitters
# override every #SBATCH line above on the command line, including
# --output and --error (the validator UI logs under its runs root).

set -euo pipefail

export PATH=/usr/bin:/bin:/usr/local/bin:$PATH

REPO="${PREPARE_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
PY="${REPO}/modelsafety/readers/.venv/bin/python"

# Never created here: many jobs starting at once would all try to build it
# at the same time.
if [[ ! -x "${PY}" ]]; then
  echo "error: ${PY} does not exist, so there is no readers venv to run prepare_rows.py with." >&2
  echo "Create it once, before submitting:" >&2
  echo "  python3 -m venv ${REPO}/modelsafety/readers/.venv" >&2
  echo "  ${REPO}/modelsafety/readers/.venv/bin/pip install -r ${REPO}/modelsafety/readers/requirements.txt" >&2
  exit 2
fi

# prepare_rows.py imports `modelsafety.contract.*`, which needs the repo
# root on PYTHONPATH.
export PYTHONPATH="${REPO}${PYTHONPATH:+:${PYTHONPATH}}"

echo "============================================================"
echo "prepare_rows.py"
echo "============================================================"
echo "Job ID:    ${SLURM_JOB_ID:-local}"
echo "Node:      ${SLURMD_NODENAME:-unknown}"
echo "CPUs:      ${SLURM_CPUS_PER_TASK:-unknown}"
echo "Repo:      ${REPO}"
printf 'Arguments: '
printf '%q ' "$@"
echo
echo "============================================================"
echo

exec "${PY}" "${REPO}/modelsafety/readers/prepare_rows.py" "$@"
