#!/bin/bash
# Shared interactive/config helpers for scripts/tool_team_data.sh,
# scripts/tool_team_ner_scan.sh, scripts/tool_team_toxicity_scan.sh,
# scripts/tool_team_dedup_scan.sh, and scripts/tool_team_regurgitation.sh.
# Meant to be sourced, not executed:
#   source "${SCRIPT_DIR}/lib/tool_team_common.sh"
# The sourcing script is expected to already have `set -euo pipefail` active.

# get_var <name> -- print the current value of the global variable called
# <name>. set_var <name> <value> -- assign it. Both use a nameref so the
# same review/prompt code works across scripts with different variable sets.
get_var() {
  local -n ref="$1"
  printf '%s' "${ref}"
}

set_var() {
  local -n ref="$1"
  ref="$2"
}

# die <message> [exit-code] -- consistent error-and-exit, default code 2.
die() {
  echo "error: $1" >&2
  exit "${2:-2}"
}

# list_partitions -- one partition name per line, straight from this
# cluster's live scheduler state (never hardcoded). The `sinfo` default
# marks the partition with a trailing "*"; that's a display convention, not
# part of the name, so it's stripped. Prints nothing (not an error) if
# `sinfo` is unavailable -- callers decide how to fall back.
list_partitions() {
  sinfo -h -o "%P" 2>/dev/null | sed 's/\*$//' | sort -u || true
}

# pick_partition -- numbered menu over the live partition list; sets the
# global PARTITION variable. Both calling scripts use that exact name for
# their partition setting. Falls back to keeping the current value, with a
# warning, if `sinfo` can't be reached (e.g. running off-cluster).
pick_partition() {
  local partitions=()
  local line
  while IFS= read -r line; do
    [[ -n "${line}" ]] && partitions+=("${line}")
  done < <(list_partitions)

  if [[ ${#partitions[@]} -eq 0 ]]; then
    echo "warning: could not list partitions via sinfo -- keeping PARTITION=${PARTITION}" >&2
    return
  fi

  echo "Available partitions (current: ${PARTITION}):"
  local choice
  select choice in "${partitions[@]}"; do
    if [[ -n "${choice}" ]]; then
      PARTITION="${choice}"
      break
    fi
    echo "Invalid choice, pick a number 1-${#partitions[@]}."
  done
}

# prompt_value <label> <current> -- "label [current]: ", Enter keeps
# <current>. Prints the resolved value on stdout.
prompt_value() {
  local label="$1" current="$2" input
  read -r -p "${label} [${current}]: " input
  if [[ -z "${input}" ]]; then
    printf '%s' "${current}"
  else
    printf '%s' "${input}"
  fi
}

# review_settings <name> <label> [<name> <label> ...] -- numbered list of
# every resolved setting; enter a number to change it, Enter to continue.
# The row named exactly "PARTITION" re-opens pick_partition instead of a
# plain text prompt, since that one has a fixed set of valid values.
review_settings() {
  local args=("$@")
  local total=${#args[@]}
  local names=() labels=()
  local i
  for ((i = 0; i < total; i += 2)); do
    names+=("${args[i]}")
    labels+=("${args[i + 1]}")
  done

  local count=${#names[@]}
  local idx name choice sel new_value
  while true; do
    echo
    echo "Current settings:"
    for ((idx = 0; idx < count; idx++)); do
      printf "  %2d) %-16s %s\n" "$((idx + 1))" "${labels[idx]}" "$(get_var "${names[idx]}")"
    done
    echo
    read -r -p "Enter a number to change a value, or press Enter to continue: " choice
    if [[ -z "${choice}" ]]; then
      break
    fi
    if ! [[ "${choice}" =~ ^[0-9]+$ ]] || ((choice < 1 || choice > count)); then
      echo "Invalid choice."
      continue
    fi
    sel=$((choice - 1))
    name="${names[sel]}"
    if [[ "${name}" == "PARTITION" ]]; then
      pick_partition
    else
      new_value="$(prompt_value "${labels[sel]}" "$(get_var "${name}")")"
      set_var "${name}" "${new_value}"
    fi
  done
}

# print_job_commands <job_id> <main_log> <err_log> -- the exact,
# copy-pasteable commands to check on a submitted SLURM job: live status,
# tailing the actual log FILE (not its directory), full accounting, and how
# to cancel it.
print_job_commands() {
  local job_id="$1" main_log="$2" err_log="$3"
  echo
  echo "  Status:      squeue -j ${job_id}"
  echo "  Live log:    tail -f ${main_log}"
  echo "  Errors:      tail -f ${err_log}"
  echo "  Accounting:  sacct -j ${job_id} --format=JobID,JobName,State,Elapsed,MaxRSS"
  echo "  Cancel:      scancel ${job_id}"
}
