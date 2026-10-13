#!/bin/bash
#SBATCH --job-name=trace_findings
#SBATCH --partition=health
#SBATCH --cpus-per-task=4
#SBATCH --mem=256G
#SBATCH --time=1-00:00:00
#SBATCH --output=logs/slurm/trace_findings_%j.out
#SBATCH --error=logs/slurm/trace_findings_%j.err

# The trace of a chain, CPU only. Every argument is passed straight through to
# modelsafety/trace/trace_findings.py:
#
#   sbatch launchers/slurm/trace_findings.sbatch \
#     --output-dir O --dedup-run-dir D --pii-run-dir D --ner-run-dir D \
#     --toxicity-run-dir D --filter-root R1 --filter-root R2
#
# --mem, --cpus-per-task and --time are all set above on purpose: this
# cluster gives a job without --mem the whole node's memory (which leaves the
# node's GPUs idle), and a job without --time has no limit. Submitters
# override every #SBATCH line above on the command line, including
# --output and --error.
#
# The trace keeps one entry in memory for every distinct line it walks back,
# and NER's findings alone are millions, so it can't run on a login node or
# inside the web server. 256G is what the full run was given. How much it
# needs wasn't measured: `sacct` reported 637 MB for it, which can't be right.
#
# Exit codes: the trace exits 0 if every finding is unchanged or not_supported,
# 1 if it finished but some are changed, not_found or broken_link, and 2 if it
# refused to start or a check failed. SLURM shows an exit code of 1 or 2 as
# FAILED. With 1 the results are still written, so read the summary.json in the
# output folder before deciding the job went wrong.
#
# The row status (row_status.sbatch) reads the same chain, so submit it after
# this one finished with a 0:
#
#   sbatch --dependency=afterok:<this job id> launchers/slurm/row_status.sbatch ...

set -euo pipefail

export PATH=/usr/bin:/bin:/usr/local/bin:$PATH

REPO="${TRACE_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
PY="${REPO}/modelsafety/readers/.venv/bin/python"

# Never created here: many jobs starting at once would all try to build it
# at the same time.
if [[ ! -x "${PY}" ]]; then
  echo "error: ${PY} does not exist, so there is no readers venv to run trace_findings.py with." >&2
  echo "Create it once, before submitting:" >&2
  echo "  python3 -m venv ${REPO}/modelsafety/readers/.venv" >&2
  echo "  ${REPO}/modelsafety/readers/.venv/bin/pip install -r ${REPO}/modelsafety/readers/requirements.txt" >&2
  exit 2
fi

# trace_findings.py imports `modelsafety.*`, which needs the repo root on PYTHONPATH.
export PYTHONPATH="${REPO}${PYTHONPATH:+:${PYTHONPATH}}"

echo "============================================================"
echo "trace_findings.py"
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

exec "${PY}" "${REPO}/modelsafety/trace/trace_findings.py" "$@"
