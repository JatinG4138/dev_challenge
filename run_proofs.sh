#!/bin/bash
# Build the task image and demonstrate Proof A (reward 0) + Proof B (reward 1).
# Requires Docker Desktop / daemon running on the host (not inside a container).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${IMAGE:-fw-release-publisher}"

echo "==> Building image ${IMAGE}"
docker build -t "${IMAGE}" "${ROOT}/environment"

run_in_container() {
  local name="$1"
  shift
  docker run --rm --name "${name}" \
    -v "${ROOT}/tests:/tests:ro" \
    -v "${ROOT}/solution:/solution:ro" \
    -v "${ROOT}/logs/${name}:/logs/verifier" \
    -w /app \
    "${IMAGE}" \
    bash -lc "$*"
}

mkdir -p "${ROOT}/logs/proof-a" "${ROOT}/logs/proof-b"

echo "==> Proof A: empty environment (no solution) — expect reward 0"
run_in_container proof-a '
  bash /tests/test.sh || true
  echo -n "reward="
  cat /logs/verifier/reward.txt
  test "$(cat /logs/verifier/reward.txt)" = "0"
'

echo "==> Proof B: apply solution then verify — expect reward 1"
run_in_container proof-b '
  bash /solution/publish.sh
  bash /tests/test.sh
  echo -n "reward="
  cat /logs/verifier/reward.txt
  test "$(cat /logs/verifier/reward.txt)" = "1"
'

echo "Both proofs passed."
