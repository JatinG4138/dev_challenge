# Author Notes — Firmware Release Publisher

## Task identity

Harbor task: implement an OpenSSL-signed firmware release publisher that talks to
a provided Express distribution gateway, reconciles a messy build manifest in
DuckDB, and prints deterministic CLI status lines.

## Six-part layout

| Part | Path | Role |
| --- | --- | --- |
| Instruction | `instruction.md` | Binding candidate spec |
| Metadata | `task.toml` | Harbor limits / tags |
| Environment | `environment/` | Docker image, gateway, fixtures, golden output; **`publisher/` empty** |
| Solution | `solution/publish.sh` + `solution/release-publisher.mjs` | Reference oracle |
| Tests | `tests/test.sh` + `tests/test_outputs.py` | Binary 0/1 grader |
| Notes | `AUTHOR_NOTES.md` | This file |

`CANDIDATE_GUIDE.md` is an optional walkthrough companion; graders must defer to
`instruction.md`.

## Why `environment/publisher/` must stay empty

Proof A (empty / negative control) builds and runs the image **without** applying
`solution/publish.sh`. If `release-publisher.mjs` were shipped under
`environment/publisher/`, `npm run report` would succeed without a solution and
Proof A could not reliably score reward `0`.

The reference implementation lives only under `solution/`. `solution/publish.sh`
copies it into `/app/publisher/` for the oracle / Proof B run.

## Reference solution behavior

`solution/release-publisher.mjs`:

1. Loads `fixtures/build_manifest.csv` into DuckDB (`releases.duckdb`).
2. Reconciles with SQL: `SELECT DISTINCT *`, drop builds superseded by
   withdrawals, aggregate surviving builds per `bundle_id`.
3. Builds canonical descriptors (sorted keys, no whitespace).
4. Signs with `/app/keys/current/` via `openssl cms -sign`.
5. POSTs to `http://127.0.0.1:7070/v1/publications` with `token-<bundle_id>`.
6. Persists receipts in a `publications` table for idempotent re-runs.
7. Prints the two status lines per bundle required by the golden file.

Fully withdrawn bundles (e.g. `BND-104` in the fixture) are omitted.

## Verifier design

`tests/test.sh`:

- Clears `releases.duckdb` and `distribution-gateway/data/`.
- Starts `distribution-gateway/server.js` in the background.
- Waits for `/healthz`.
- Runs `pytest` on `test_outputs.py`.
- Writes `/logs/verifier/reward.txt` as `1` on success, else `0`.

`tests/test_outputs.py` maps 1:1 to `scaffold_plan.yaml` functional criteria:

| Test | Criterion |
| --- | --- |
| `test_report_output_matches` | Golden CLI (RECEIPT masked) |
| `test_withdrawals_and_duplicates_reconciled` | Independent SQL ground truth |
| `test_bundles_signed_with_current_key_accepted` | STATUS=PUBLISHED + current key_id |
| `test_receipts_and_tokens_persisted_in_duckdb` | DuckDB receipts/tokens |
| `test_idempotent_rerun_no_duplicate_publications` | Byte-identical re-run; one pub/bundle |
| `test_revoked_key_signature_rejected` | Verifier-owned revoked CMS → UNTRUSTED |

## Proofs to demonstrate before submit

Requires **Docker Desktop running on the host Mac** (do not run `docker` from
inside an already-running task container).

```bash
# from the repository root
./run_proofs.sh
```

Or manually:

```bash
docker build -t fw-release-publisher environment/

# Proof A — empty run → reward 0
docker run --rm -v "$PWD/tests:/tests:ro" -v "$PWD/solution:/solution:ro" \
  -w /app fw-release-publisher bash -lc 'bash /tests/test.sh || true; cat /logs/verifier/reward.txt'

# Proof B — solution run → reward 1
docker run --rm -v "$PWD/tests:/tests:ro" -v "$PWD/solution:/solution:ro" \
  -w /app fw-release-publisher bash -lc 'bash /solution/publish.sh && bash /tests/test.sh && cat /logs/verifier/reward.txt'
```

**Proof A — empty run → reward 0**

Do **NOT** run `solution/publish.sh`. Expect `/logs/verifier/reward.txt` = `0`.

**Proof B — solution run → reward 1**

Run `bash /solution/publish.sh` first. Expect reward `1`.

## Resolved open questions (grading invariants)

- **Duplicates:** collapse rows identical across *every* column (`SELECT DISTINCT *`).
- **Withdrawals:** a WITHDRAWAL cancels the BUILD whose `entry_id` equals
  `supersedes_id`. Bundle membership (not amount-level netting) is graded.
- **Canonical descriptor:** UTF-8 JSON, lexicographically sorted keys, no
  insignificant whitespace; signed bytes === submitted `descriptor` bytes.

## Boundaries enforced on candidates

- Gateway only over HTTP (no reading `distribution-gateway/data/gateway.json`).
- No bypassing CMS verification; no using the revoked key for the publisher.
- No hardcoding golden text / receipt ids / row counts.

(The verifier itself may inspect the gateway ledger to assert publication counts.)
