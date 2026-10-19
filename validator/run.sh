#!/bin/bash
# Starts the dataset scan service for the current user, on the current machine, on
# localhost only. The service keeps a registry of datasets (each under one team), converts
# them, scans them for duplicates, PII and toxicity, shows what the scans found, and lets
# you drop chosen rows into a filtered version. Scans never remove anything by themselves.
#
# This is a personal tool, not a shared service: run it from your own terminal on a login
# node. It acts as you (sbatch, squeue, sacct, sinfo and scancel must be on your PATH), with
# your own file permissions. Any absolute path your account can read can be registered --
# there's no folder allowlist. If your account can't read a dataset's folder, the dataset
# shows up as unavailable in the Datasets list; nothing fails.
#
# What it reads
#   - The original data a dataset points at: its existence, whether it can be read, and
#     bounded samples for the preview and "Show original". It never writes to, moves or
#     deletes original data.
#   - Its own files under the service root (below), and each SLURM job's state, read live
#     from squeue and sacct (job state is never stored).
#
# What it writes, and where
#   The service root is $VALIDATOR_SERVICE_ROOT, or <repo>/output/service. The first start
#   with no datasets.json creates it (and registry/, datasets/ and scans/) from the catalog
#   in validator/backend/registry_seed.py. Everything else is written under it:
#
#     registry/datasets.json            every registered dataset, its saved conversion
#                                       settings, current Prepared version and results
#     datasets/<id>/prepared/<version>/ the converted rows (one current Prepared version)
#     datasets/<id>/_jobs/, _logs/      conversion and filter job records and logs
#     datasets/<id>/drop_list.jsonl     a filtered dataset only: the rows dropped
#     scans/<scan id>/                  scan.json, inputs/, results/, _logs/, and what the
#                                       result analysis adds: coverage.json, tally.json,
#                                       index/ (the flagged rows of each dataset) and, for
#                                       an automatic drop, auto_drop.json
#
#   The NER and toxicity scans' own SLURM logs are written to the repo's logs/slurm/.
#   Registering, editing and unregistering a dataset change datasets.json. Every job is
#   submitted with sbatch as an argument list (never through a shell), and the command the
#   UI shows is the command that runs.
#
# How to run it
#
#   ./validator/run.sh
#
# What it does
#   Conversions: a dataset's "Convert", "Convert again" and "More -> Convert only" submit one
#   CPU-only job per dataset (modelsafety/readers/prepare_rows.py, with the settings saved on
#   the dataset). When it has finished and passed its checks, it becomes the dataset's
#   current Prepared version.
#
#   Scans: "Find duplicates", "Scan for PII" (NER or rule-based) and "Scan for toxicity". A
#   scan first converts the datasets that need it, then runs a job that only reads the
#   datasets' Prepared versions. A scan waiting for a conversion is started by this service
#   itself, within 30 seconds of the conversion finishing. Duplicates and the rule-based PII
#   scan are CPU-only. The NER scan plans on a CPU job and scans on GPUs (one, or a whole
#   node). The toxicity scan plans on a CPU job and scans with an array job, one GPU task
#   for each shard. A finished scan becomes its datasets' current result for that scanner.
#
#   Results: for each finished scan this service submits one more CPU-only job (within about
#   a minute) that checks the scan's coverage, counts what the scanner's summary files don't
#   hold, and writes the findings index. The result page and the flagged-rows list read
#   those small files; opening a flagged row reads one line of the Prepared version.
#   "Check again" on the Results tab submits that job again.
#
#   Dropping rows: "Review and drop" on a dataset's result pages turns the rows you chose
#   into a filtered version, a new dataset in the same team with a link to its parent. One
#   CPU-only job (modelsafety/filter/filter_rows.py with --drop-list) copies the other rows
#   byte for byte. The dataset the rows came from, its Prepared rows and its results are
#   never changed, and a dataset that has filtered versions can't be unregistered. A
#   duplicates scan started with "Drop duplicates automatically" does the same by itself,
#   once, when its coverage check has passed (never for a protected dataset); if the check
#   failed it drops nothing.
#
# What it deletes (only inside the service root, after checking the path)
#   - Unregister: the dataset's entry and its own folder datasets/<id>, and scan results
#     only that dataset used. Refused while a conversion, scan or filter job of the dataset
#     is going, and while it has filtered versions.
#   - Convert again: the dataset's current Prepared version and the scans that only read it.
#     A version a filtered dataset was made from is set aside and kept instead.
#   - Every conversion start: the folders of that dataset's failed or cancelled attempts,
#     inside its own prepared/ folder.
#   - A scan that was replaced by a newer one (or whose datasets were converted again or
#     unregistered), once no dataset uses it. A failed or cancelled scan is never deleted
#     automatically.
#
# What asks for confirmation
#   - Unregister: names what will be removed, then asks.
#   - Convert, Convert again, or a scan that has to convert again: when something will be
#     deleted, lists it dataset by dataset first ("Delete and convert").
#   - A GPU scan (NER, toxicity): shows the cost and needs a ticked confirmation.
#   - Drop: shows how many rows will be dropped, by scanner and reason, and the exact
#     command, before "Drop N rows".
#   Cancel (Activity page) runs scancel on the jobs a record lists, right away.
#
# The first run creates validator/backend/.venv (installing FastAPI/uvicorn
# plus modelsafety/readers' own pandas/pyarrow/datasets/numpy versions) and
# builds validator/frontend (npm install + npm run build). Both are reused
# on later runs; delete validator/backend/.venv or validator/frontend/dist
# to force a rebuild. If the machine has no npm, validator/install_node.sh
# installs a pinned Node.js into validator/.node first (needs access to
# nodejs.org; nothing outside that folder is written).
#
# The first run also picks a free localhost port and remembers it in
# validator/.validator-state; every later run reuses that exact same port,
# so the URL you bookmark or forward doesn't change. If a previous run is
# still going (e.g. left running in another terminal), running this script
# again stops that one first -- only after confirming it's actually a
# uvicorn process this script started, never just whatever happens to be
# using that port -- then starts fresh on the same port. Delete
# validator/.validator-state to go back to a freshly picked port.
#
#   Open: http://127.0.0.1:43891/
#
# There's no login or token: this is meant for a trusted network, so
# anyone who can already reach this machine's localhost (e.g. another user
# logged into the same login node) can open it too, same as any other
# unauthenticated localhost service. If you don't want that, don't run
# this on a shared machine. Open the URL above in a browser; if you're
# working through Cursor's or VS Code's remote-SSH, use its "Forwarded
# Ports" / "Open in Browser" for that port rather than trying to reach
# 127.0.0.1 directly from your laptop.
#
# Stop with Ctrl-C, or just run this script again -- either way, nothing
# is left running once you've done one or the other.

set -euo pipefail

# The clone can be shared by several people (for example under /home/shared): files made by
# this service, and by the SLURM jobs it submits (they inherit this umask), stay writable
# for the group, so the next person can use and clean up what you created. Set before
# anything below creates a file.
umask 002

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${SCRIPT_DIR}/backend"
FRONTEND_DIR="${SCRIPT_DIR}/frontend"
VENV_DIR="${BACKEND_DIR}/.venv"

if [[ ! -x "${VENV_DIR}/bin/python3" ]]; then
  echo "[validator] creating backend venv at ${VENV_DIR} ..."
  python3 -m venv "${VENV_DIR}"
  "${VENV_DIR}/bin/pip" install --upgrade pip -q
  "${VENV_DIR}/bin/pip" install -r "${BACKEND_DIR}/requirements.txt"
fi

if [[ ! -d "${FRONTEND_DIR}/dist" ]]; then
  echo "[validator] building frontend ..."
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  # shellcheck disable=SC1091
  [[ -s "${NVM_DIR}/nvm.sh" ]] && source "${NVM_DIR}/nvm.sh"
  # No npm anywhere (nvm or the system): install Node.js into validator/.node with the
  # separate install_node.sh, and use that one.
  if ! command -v npm >/dev/null 2>&1; then
    NODE_BIN_DIR="${SCRIPT_DIR}/.node/bin"
    if [[ ! -x "${NODE_BIN_DIR}/npm" ]]; then
      echo "[validator] npm not found -- installing Node.js into ${SCRIPT_DIR}/.node ..."
      if ! bash "${SCRIPT_DIR}/install_node.sh"; then
        echo "[validator] error: couldn't install Node.js, which is needed to build the frontend. Install Node.js yourself so that npm is on your PATH, or fix the error above, then run this again." >&2
        exit 1
      fi
    fi
    export PATH="${NODE_BIN_DIR}:${PATH}"
  fi
  if [[ ! -d "${FRONTEND_DIR}/node_modules" ]]; then
    (cd "${FRONTEND_DIR}" && npm install)
  fi
  (cd "${FRONTEND_DIR}" && npm run build)
fi

# Keeping the port consistent across runs -- see STATE_FILE below. A PID
# is only ever acted on after _is_our_uvicorn confirms its actual command
# line is this script's own invocation further down (uvicorn app:app from
# this exact venv) -- never just "whatever is using that port", since
# other people's unrelated processes can be running on this same shared
# login node.
STATE_FILE="${SCRIPT_DIR}/.validator-state"

_is_our_uvicorn() {
  local pid="$1"
  [[ -r "/proc/${pid}/cmdline" ]] || return 1
  grep -qa "uvicorn" "/proc/${pid}/cmdline" 2>/dev/null \
    && grep -qa "app:app" "/proc/${pid}/cmdline" 2>/dev/null
}

_port_is_free() {
  "${VENV_DIR}/bin/python3" -c "
import socket, sys
s = socket.socket()
try:
    s.bind(('127.0.0.1', int(sys.argv[1])))
except OSError:
    sys.exit(1)
s.close()
" "$1"
}

PORT=""
OLD_PID=""
if [[ -f "${STATE_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${STATE_FILE}"
  PORT="${VALIDATOR_LAST_PORT:-}"
  OLD_PID="${VALIDATOR_LAST_PID:-}"
fi

if [[ -n "${OLD_PID}" ]] && kill -0 "${OLD_PID}" 2>/dev/null && _is_our_uvicorn "${OLD_PID}"; then
  echo "[validator] a previous instance is still running (pid ${OLD_PID}, port ${PORT}) -- stopping it ..."
  kill "${OLD_PID}" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "${OLD_PID}" 2>/dev/null || break
    sleep 0.25
  done
  if kill -0 "${OLD_PID}" 2>/dev/null; then
    echo "[validator] previous instance didn't stop in time, forcing it (SIGKILL) ..."
    kill -9 "${OLD_PID}" 2>/dev/null || true
    sleep 0.5
  fi
fi

if [[ -z "${PORT}" ]]; then
  # No saved port yet (first run, or the state file was removed): bind to
  # port 0 and ask the OS what it picked, the same way as before -- this
  # becomes the fixed port for every run after this one.
  PORT="$("${VENV_DIR}/bin/python3" -c "
import socket
with socket.socket() as s:
    s.bind(('127.0.0.1', 0))
    print(s.getsockname()[1])
")"
elif ! _port_is_free "${PORT}"; then
  echo "[validator] error: port ${PORT} (this validator's usual port) is in use by something else -- not a previous run of this script, since that would already have been stopped above. Free it yourself, or delete ${STATE_FILE} to switch to a freshly picked port." >&2
  exit 1
fi

export VALIDATOR_FRONTEND_DIST="${FRONTEND_DIR}/dist"

# Written before exec below, using $$ -- exec replaces this shell's
# program but keeps its PID, so $$ is exactly the PID uvicorn will run as.
cat > "${STATE_FILE}" <<EOF
VALIDATOR_LAST_PORT=${PORT}
VALIDATOR_LAST_PID=$$
EOF

echo "[validator] starting on 127.0.0.1:${PORT} ..."
echo "[validator] Open: http://127.0.0.1:${PORT}/"

cd "${BACKEND_DIR}"
exec "${VENV_DIR}/bin/python3" -m uvicorn app:app --host 127.0.0.1 --port "${PORT}"
