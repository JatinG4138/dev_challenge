#!/bin/bash
# Reference solution entrypoint.
# Installs the working publisher into /app/publisher/ so `npm run report` succeeds.
# environment/publisher/ ships empty — this script is the only path that places
# the reference implementation into the runtime image (oracle / Proof B).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="/app/publisher"
TARGET="${TARGET_DIR}/release-publisher.mjs"
SOURCE="${SCRIPT_DIR}/release-publisher.mjs"

if [ ! -f "${SOURCE}" ]; then
  echo "error: reference publisher missing at ${SOURCE}" >&2
  exit 1
fi

mkdir -p "${TARGET_DIR}"
cp "${SOURCE}" "${TARGET}"
chmod a+r "${TARGET}"

echo "Installed reference publisher -> ${TARGET}"
