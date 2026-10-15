#!/bin/bash
#SBATCH --job-name=result_analysis
#SBATCH --partition=health
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --time=04:00:00
#SBATCH --output=logs/slurm/result_analysis_%j.out
#SBATCH --error=logs/slurm/result_analysis_%j.err

# The coverage check, the counts and the findings index of one finished scan, CPU only. Every
# argument is passed straight through to modelsafety/results/analyse.py:
#
#   sbatch launchers/slurm/run_result_analysis.sbatch \
#     --scanner toxicity --root R --results-dir D --out-dir O [--files-from F] \
#     [--group NAME=PREFIX ...] [--coverage-only] \
#     [--index-dir I (--index-name NAME | with --group)]
#
# --mem, --cpus-per-task and --time are all set above on purpose: this
# cluster gives a job without --mem the whole node's memory (which leaves the
# node's GPUs idle), and a job without --time has no limit. Submitters
# override every #SBATCH line above on the command line, including
# --output and --error (the validator UI logs inside the scan's folder).
#
# The counts and the index keep every flagged row in memory (a few GB for millions
# of NER spans), which the 32G above covers.

set -euo pipefail

export PATH=/usr/bin:/bin:/usr/local/bin:$PATH

REPO="${ANALYSE_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
PY="${REPO}/modelsafety/readers/.venv/bin/python"

# Never created here: many jobs starting at once would all try to build it
# at the same time.
if [[ ! -x "${PY}" ]]; then
  echo "error: ${PY} does not exist, so there is no readers venv to run analyse.py with." >&2
  echo "Create it once, before submitting:" >&2
  echo "  python3 -m venv ${REPO}/modelsafety/readers/.venv" >&2
  echo "  ${REPO}/modelsafety/readers/.venv/bin/pip install -r ${REPO}/modelsafety/readers/requirements.txt" >&2
  exit 2
fi

# analyse.py imports `modelsafety.*`, which needs the repo root on PYTHONPATH.
export PYTHONPATH="${REPO}${PYTHONPATH:+:${PYTHONPATH}}"

echo "============================================================"
echo "analyse.py"
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

exec "${PY}" "${REPO}/modelsafety/results/analyse.py" "$@"
