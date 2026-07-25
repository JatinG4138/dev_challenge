# Firmware Release Publisher

Release engineering rotated the firmware code-signing key. Bundles signed with
the old (revoked) key are rejected by the distribution gateway with
`UNTRUSTED_SIGNATURE`. Implement a publisher that reconciles the build
manifest, signs with the **current** key, submits to the gateway, and emits
deterministic status lines.

## Deliverable

Create exactly this file:

```
/app/publisher/release-publisher.mjs
```

Run it with:

```
npm run report
# equivalent to: node publisher/release-publisher.mjs --report
```

Do **not** modify `distribution-gateway/`, the signing keys, fixtures, or
`package.json`.

## Environment (under `/app`)

| Path | Purpose |
| --- | --- |
| `fixtures/build_manifest.csv` | Raw build / withdrawal records |
| `reports/publications.expected.txt` | Golden CLI output to reproduce |
| `package.json` | Defines `npm run report`; `duckdb` is already installed |
| `distribution-gateway/` | Express gateway on port **7070** (do not modify) |
| `keys/current/` | Current signing keypair (`current.key.pem`, `current.cert.pem`) |
| `keys/revoked/` | Revoked keypair — signing with it must fail verification |
| `publisher/` | Empty — place your `release-publisher.mjs` here |

Create `releases.duckdb` at runtime; it is not pre-created.

The gateway must already be running when you execute `npm run report`
(`cd /app/distribution-gateway && node server.js`). The grader starts it for you.

### Manifest schema

```
entry_id,bundle_id,component_id,version,size_bytes,record_type,supersedes_id,recorded_at
```

- `record_type` is `BUILD` or `WITHDRAWAL`.
- A `WITHDRAWAL` row's `supersedes_id` is the `entry_id` of the `BUILD` it cancels.

### Gateway contract (`http://127.0.0.1:7070`)

- `GET /v1/signing-key/current` → `{ key_id, algorithm, certificate_ref, status }`
- `POST /v1/publications` with body
  `{ descriptor, signature, request_token }` →
  `{ publication_id, request_token, status: "PUBLISHED" }` on success, or
  `{ error: "UNTRUSTED_SIGNATURE" }` if the signature does not verify against
  the current certificate.

Re-posting the same `request_token` replays the original receipt (no duplicate
publication). See `distribution-gateway/README.md` for verification details.

## Requirements

### 1. Reconcile the manifest with SQL in DuckDB

Load `fixtures/build_manifest.csv` into DuckDB (`releases.duckdb`) and derive
publishable bundles with SQL:

1. **Collapse exact duplicates.** Rows identical across *every* column count once.
2. **Apply withdrawals.** A `BUILD` whose `entry_id` appears as some
   `WITHDRAWAL`'s `supersedes_id` is cancelled and must not contribute.
3. A bundle is **publishable** only if at least one surviving `BUILD` remains.
   A bundle whose every build was withdrawn is skipped entirely.

For each publishable bundle compute `artifact_count` (number of surviving builds)
and `total_bytes` (sum of their `size_bytes`).

### 2. Canonical release descriptor

For each publishable bundle, build a UTF-8 JSON string with lexicographically
sorted keys and **no insignificant whitespace**:

```
{"artifact_count":<n>,"bundle_id":"<id>","total_bytes":<sum>}
```

The bytes you sign must be exactly the bytes you send as `descriptor`.

### 3. Sign with OpenSSL CMS (current key)

Produce a detached CMS signature (PEM, `-outform PEM -binary`) over the
descriptor using:

- signer: `/app/keys/current/current.cert.pem`
- key: `/app/keys/current/current.key.pem`

Do **not** use the revoked keypair.

### 4. Submit over HTTP

For each publishable bundle in ascending `bundle_id` order:

1. Read `key_id` from `GET /v1/signing-key/current`.
2. Sign the descriptor.
3. `POST /v1/publications` with
   `request_token` = `token-<bundle_id>` (deterministic).

### 5. Persist receipts (idempotency)

Store each `request_token`, `publication_id`, and related state in
`releases.duckdb` so a second run reuses stored receipts instead of creating
duplicate publications. Re-running must produce byte-identical stdout.

### 6. Deterministic stdout

Emit exactly two lines per publishable bundle, ordered by `bundle_id`:

```
BUNDLE <bundle_id> SIGNED KEY=<key_id>
BUNDLE <bundle_id> PUBLISHED RECEIPT=<publication_id> TOKEN=<request_token> STATUS=PUBLISHED
```

`<key_id>` must come from the gateway. Your output must match
`reports/publications.expected.txt` when `RECEIPT=` values are masked.

## Boundaries (automatic fail)

- Interact with the gateway **only over HTTP**. Do not read or write
  `distribution-gateway/data/gateway.json`.
- Do not disable or bypass signature verification.
- Do not sign with the revoked key.
- Do not hardcode the golden text, receipt ids, or row counts — derive
  everything from the manifest and the gateway so a changed manifest would
  still produce a correct result.
- Keep output ordering deterministic (`bundle_id` ascending).
