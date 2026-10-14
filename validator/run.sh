#!/bin/bash
# Starts the input validator UI for the current user, on the current
# machine, on localhost only.
#
# This is a personal tool, not a shared service: run it from your own
# terminal on a login node (or anywhere with access to the data you want to
# inspect). It reads files with your own permissions and never writes
# anything to the data it looks at. Any absolute path your account can
# read can be inspected -- there's no folder allowlist. If your account
# can't read the folder of a registered dataset, it just shows up as
# unavailable in the Datasets list; nothing fails.
#
# Looking at data is read-only. Only these act on anything, and they act as
# you (sbatch, squeue, sacct, sinfo and scancel must be on your PATH):
#
#   Conversions: the Start panel's "Start" and "Delete and start fresh", the
#   batch review's "Start" and "Delete N folders and start fresh", and the Jobs
#   view's Cancel. Start submits CPU-only SLURM jobs (one per dataset), Cancel
#   runs scancel, and Delete removes run folders -- only inside the runs root,
#   which is $VALIDATOR_RUNS_ROOT or <repo>/output/prepared. Job logs and batch
#   records go under <runs root>/_jobs.
#
#   Dataset registry: registering, editing and unregistering a dataset write
#   <service root>/registry/datasets.json, where the service root is
#   $VALIDATOR_SERVICE_ROOT, or <repo>/output/service. The first start with no
#   datasets.json creates it (and registry/, datasets/ and scans/) from the catalog
#   in validator/backend/registry_seed.py. Unregistering also deletes that dataset's own
#   folder, <service root>/datasets/<id>, and nothing else: the original data a
#   dataset points at is only checked for existing and being readable.
#
# The runs list reads more than the runs root: also the Prepared folder and
# each filter step's output folder of every chain recorded in
# <repo>/output/*/chain.json ($VALIDATOR_CHAINS_ROOT replaces <repo>/output).
# Set $VALIDATOR_READ_ROOTS to a list of folders, separated like PATH, to read
# exactly those instead. Reading never writes; only the runs root can be
# deleted from or started into.
#
#   ./validator/run.sh
#
# The first run creates validator/backend/.venv (installing FastAPI/uvicorn
# plus modelsafety/readers' own pandas/pyarrow/datasets/numpy versions) and
# builds validator/frontend (npm install + npm run build). Both are reused
# on later runs; delete validator/backend/.venv or validator/frontend/dist
# to force a rebuild.
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
