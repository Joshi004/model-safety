#!/bin/bash
# Defaults shared by the SLURM launchers and the wrappers in scripts/. Sourced, never run:
#
#   source "${REPO}/launchers/slurm/defaults.sh"
#
# It only sets variables that aren't already set, so anything a caller exports first wins.

# The vLLM container image the toxicity guard, the PII LLM check and the Privacy Filter
# launchers run. This project keeps its own copy, in a folder whose name says what it holds,
# next to ABOUT.txt (what it is, who uses it, where it was copied from, its SHA-256).
# Each launcher still has its own override variable (VLLM_SQUASHFS_PATH, PRIVACY_VLLM_SQUASHFS_PATH,
# PRIVACY_FILTER_LLM_SQUASHFS_PATH, --vllm-image on the toxicity wrapper) that wins over this.
MODEL_SAFETY_CONTAINER_DIR="${MODEL_SAFETY_CONTAINER_DIR:-/home/shared/containers/qvac-model-safety__vllm-inference-server__for-toxicity-guard-and-pii-llm-scans}"
MODEL_SAFETY_VLLM_IMAGE="${MODEL_SAFETY_VLLM_IMAGE:-${MODEL_SAFETY_CONTAINER_DIR}/vllm-0.22.0-cu129.sqsh}"
