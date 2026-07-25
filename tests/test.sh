#!/bin/bash
# Verifier entrypoint for the firmware release-publisher task.
# Starts the distribution gateway, runs pytest, writes binary reward.

if [ "$PWD" = "/" ]; then
    echo "Error: No working directory set. Please set a WORKDIR in your Dockerfile before running this script."
    exit 1
fi

set -euo pipefail

mkdir -p /logs/verifier

APP_DIR="${APP_DIR:-/app}"
GATEWAY_DIR="${APP_DIR}/distribution-gateway"
GATEWAY_PID=""

cleanup() {
    if [ -n "${GATEWAY_PID}" ] && kill -0 "${GATEWAY_PID}" 2>/dev/null; then
        kill "${GATEWAY_PID}" 2>/dev/null || true
        wait "${GATEWAY_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Fresh state for a deterministic grade.
rm -f "${APP_DIR}/releases.duckdb"
rm -rf "${GATEWAY_DIR}/data"
mkdir -p "${GATEWAY_DIR}/data"

# Launch the provided gateway in the background.
(
    cd "${GATEWAY_DIR}"
    node server.js
) > /logs/verifier/gateway.log 2>&1 &
GATEWAY_PID=$!

# Wait until /healthz responds (Node fetch — curl is not in the image).
ready=0
for _ in $(seq 1 60); do
    if node -e "fetch('http://127.0.0.1:7070/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" \
        2>/dev/null; then
        ready=1
        break
    fi
    sleep 0.25
done

if [ "${ready}" -ne 1 ]; then
    echo "error: distribution-gateway did not become ready on :7070" >&2
    echo "--- gateway.log ---" >&2
    cat /logs/verifier/gateway.log >&2 || true
    echo 0 > /logs/verifier/reward.txt
    exit 1
fi

set +e
# Shared-mode verifier: pytest is preinstalled in the image.
python3 -m pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA
code=$?
set -e

echo "pytest exit code: ${code}"

if [ "$code" -eq 0 ]; then
    echo 1 > /logs/verifier/reward.txt
else
    echo 0 > /logs/verifier/reward.txt
fi

exit "$code"
