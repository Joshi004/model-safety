#!/bin/bash
#SBATCH --job-name=serve_smoke
#SBATCH --partition=health
#SBATCH --gres=gpu:nvidia_h100_80gb_hbm3:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=00:45:00
#SBATCH --output=logs/slurm/serve_smoke_%j.out
#SBATCH --error=logs/slurm/serve_smoke_%j.err
#
# Start one vLLM replica of a checkpoint, send it one test chat request, then
# stop it -- modelsafety/serving/serve.py's own "smoke" command, as a Slurm
# job. For finding out whether serving a checkpoint works at all, before
# wiring up a real caller (the regurgitation probe, a real pilot run, ...).
#
# A thin wrapper, not a copy of serve.py's logic: every real setting
# (checkpoint, dtype, max-model-len, thinking, sampling, family flags,
# vllm-bin, ...) is one of serve.py's own flags, passed straight through as
# this job's arguments -- see `python3 -m modelsafety.serving.serve smoke
# --help` for the full list.
#
# Usage:
#   sbatch launchers/slurm/run_serve_smoke.sbatch --checkpoint /path/to/checkpoint
#   sbatch launchers/slurm/run_serve_smoke.sbatch --checkpoint /path/to/checkpoint \
#     --extra-args='--trust-remote-code'
#   sbatch --partition=toolCall launchers/slurm/run_serve_smoke.sbatch --checkpoint /path/to/checkpoint
#
# Note the --extra-args='...' (=, not a space) above: its value itself starts
# with '-', and argparse only accepts that in the --flag=value form -- a
# space-separated --extra-args '--trust-remote-code' fails, reading the
# second word as its own unrecognized flag.
#
# The #SBATCH header above is a single-GPU, 45-minute default -- enough for
# one cold start plus one request. Override resources on the sbatch command
# line (as in the --partition example above); they win over this header.
#
# Logs and the launch report land under serve.py's own default log
# directory (output/serving/<served-name>/<this job's id>/) unless
# --log-dir overrides it.

set -euo pipefail

REPO="${REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
# serve.py imports modelsafety.serving.vllm_config as an absolute package
# import -- needs the repo root on PYTHONPATH to resolve, same as every
# other launcher here (see e.g. run_regurgitation.sbatch).
export PYTHONPATH="${REPO}${PYTHONPATH:+:${PYTHONPATH}}"

exec python3 -m modelsafety.serving.serve smoke "$@"
