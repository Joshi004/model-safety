#!/bin/bash
# Installs Node.js (which brings npm) for building the validator's frontend, into
# validator/.node. run.sh calls this when it finds no npm on the machine; you can also run it
# by hand.
#
# What it does
#   - Downloads one pinned Node.js release for Linux (x64 or arm64) from https://nodejs.org.
#   - Checks the download against the SHA-256 that nodejs.org publishes for it (SHASUMS256.txt),
#     and installs nothing if they differ.
#   - Unpacks it into validator/.node (gitignored). Nothing outside that folder is written,
#     and it needs no root rights. It does not use nvm.
#
# What it never does: delete anything that was there before. If validator/.node exists but
# isn't a working install, it stops and tells you; remove that folder yourself and run again.
#
# It needs network access to nodejs.org, and curl or wget.
#
# NODE_INSTALL_DIR overrides where Node goes (default: validator/.node).

set -euo pipefail

NODE_VERSION="v24.21.0"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${NODE_INSTALL_DIR:-${SCRIPT_DIR}/.node}"
BASE_URL="https://nodejs.org/dist/${NODE_VERSION}"

fail() {
  echo "[install_node] error: $1" >&2
  exit 1
}

if [[ "$(uname -s)" != "Linux" ]]; then
  fail "this installer only supports Linux (this is $(uname -s)). Install Node.js yourself and put npm on your PATH."
fi
case "$(uname -m)" in
  x86_64) ARCH="x64" ;;
  aarch64 | arm64) ARCH="arm64" ;;
  *) fail "unsupported CPU type $(uname -m). Install Node.js yourself and put npm on your PATH." ;;
esac

ARCHIVE="node-${NODE_VERSION}-linux-${ARCH}.tar.gz"

if [[ -x "${INSTALL_DIR}/bin/node" && -x "${INSTALL_DIR}/bin/npm" ]]; then
  echo "[install_node] already installed: $("${INSTALL_DIR}/bin/node" --version) in ${INSTALL_DIR}"
  exit 0
fi
if [[ -e "${INSTALL_DIR}" ]]; then
  fail "${INSTALL_DIR} exists but has no working Node.js in it. Nothing was changed. Remove that folder yourself, then run this again."
fi

download() {
  # download <url> <file>
  if command -v curl >/dev/null 2>&1; then
    curl --fail --silent --show-error --location --max-time 600 --output "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget --quiet --timeout=60 --output-document="$2" "$1"
  else
    fail "neither curl nor wget is installed."
  fi
}

# The work folder sits next to the install folder (same disk, so the final move is a rename).
mkdir -p "$(dirname "${INSTALL_DIR}")"
WORK_DIR="$(mktemp -d "$(dirname "${INSTALL_DIR}")/.node-install.XXXXXX")"
cleanup() {
  # WORK_DIR was made by mktemp just above; only a folder with that name is ever removed.
  if [[ -n "${WORK_DIR:-}" && "$(basename "${WORK_DIR}")" == .node-install.* && -d "${WORK_DIR}" ]]; then
    rm -rf -- "${WORK_DIR}"
  fi
}
trap cleanup EXIT

echo "[install_node] downloading Node.js ${NODE_VERSION} (linux-${ARCH}) from nodejs.org ..."
download "${BASE_URL}/${ARCHIVE}" "${WORK_DIR}/${ARCHIVE}" || fail "couldn't download ${BASE_URL}/${ARCHIVE}. Check that this machine can reach nodejs.org."
download "${BASE_URL}/SHASUMS256.txt" "${WORK_DIR}/SHASUMS256.txt" || fail "couldn't download ${BASE_URL}/SHASUMS256.txt."

echo "[install_node] checking the download against nodejs.org's SHA-256 ..."
# Only the line for our archive (an exact file name match), so the other files listed there
# needn't be present.
if ! (cd "${WORK_DIR}" && awk -v name="${ARCHIVE}" '$2 == name' SHASUMS256.txt | sha256sum --check --status -); then
  fail "the downloaded file doesn't match the SHA-256 nodejs.org publishes for it. Nothing was installed."
fi

mkdir "${WORK_DIR}/unpacked"
tar -xzf "${WORK_DIR}/${ARCHIVE}" -C "${WORK_DIR}/unpacked" --strip-components=1 || fail "couldn't unpack ${ARCHIVE}."
[[ -x "${WORK_DIR}/unpacked/bin/node" && -x "${WORK_DIR}/unpacked/bin/npm" ]] || fail "the unpacked folder has no node and npm."

mv "${WORK_DIR}/unpacked" "${INSTALL_DIR}"
echo "[install_node] installed $("${INSTALL_DIR}/bin/node" --version) in ${INSTALL_DIR}"
