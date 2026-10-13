#!/bin/bash
#SBATCH --job-name=row_status
#SBATCH --partition=health
#SBATCH --cpus-per-task=4
#SBATCH --mem=256G
#SBATCH --time=1-00:00:00
#SBATCH --output=logs/slurm/row_status_%j.out
#SBATCH --error=logs/slurm/row_status_%j.err

# The row status of a chain, CPU only. Every argument is passed straight
# through to modelsafety/trace/row_status.py:
#
#   sbatch launchers/slurm/row_status.sbatch \
#     --filter-root LAST_FILTER_STEP --output-dir O
#
# --mem, --cpus-per-task and --time are all set above on purpose: this
# cluster gives a job without --mem the whole node's memory (which leaves the
# node's GPUs idle), and a job without --time has no limit. Submitters
# override every #SBATCH line above on the command line, including
# --output and --error.
#
# The row status keeps every row and every finding in memory. The full run
# (1,143,300 rows, 9.3 million findings) peaked at 34.4 GB, so 256G leaves a
# wide margin.
#
# Submit it after the trace of the same chain finished, so that the trace's
# `_SUCCESS` is there when you look at the chain:
#
#   sbatch --dependency=afterok:<trace job id> launchers/slurm/row_status.sbatch ...
#
# The row status doesn't read the trace, so it also runs on its own. Pointing
# --output-dir at a folder that already holds a row status made from these same
# inputs rewrites it in place; a different row status there is refused.
#
# Exit codes: 0 if every check passed (then `_SUCCESS` is written), 2 if it
# refused to start or a check failed.

set -euo pipefail

export PATH=/usr/bin:/bin:/usr/local/bin:$PATH

REPO="${ROW_STATUS_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
PY="${REPO}/modelsafety/readers/.venv/bin/python"

# Never created here: many jobs starting at once would all try to build it
# at the same time.
if [[ ! -x "${PY}" ]]; then
  echo "error: ${PY} does not exist, so there is no readers venv to run row_status.py with." >&2
  echo "Create it once, before submitting:" >&2
  echo "  python3 -m venv ${REPO}/modelsafety/readers/.venv" >&2
  echo "  ${REPO}/modelsafety/readers/.venv/bin/pip install -r ${REPO}/modelsafety/readers/requirements.txt" >&2
  exit 2
fi

# row_status.py imports `modelsafety.*`, which needs the repo root on PYTHONPATH.
export PYTHONPATH="${REPO}${PYTHONPATH:+:${PYTHONPATH}}"

echo "============================================================"
echo "row_status.py"
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

exec "${PY}" "${REPO}/modelsafety/trace/row_status.py" "$@"
