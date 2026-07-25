"""Verifier tests for the firmware release-publisher task.

Each test maps to a functional_criteria[] entry in scaffold_plan.yaml. The suite
starts from a clean slate (test.sh clears releases.duckdb and the gateway ledger,
then launches the gateway). Tests drive `npm run report`, independently recompute
publishable bundles from the CSV fixture, inspect DuckDB persistence, and prove
the gateway rejects revoked-key signatures.

Run via tests/test.sh, which writes /logs/verifier/reward.txt (0 or 1).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import duckdb
import pytest
import requests

APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
MANIFEST_PATH = APP_DIR / "fixtures" / "build_manifest.csv"
GOLDEN_PATH = APP_DIR / "reports" / "publications.expected.txt"
DB_PATH = APP_DIR / "releases.duckdb"
PUBLISHER_PATH = APP_DIR / "publisher" / "release-publisher.mjs"
GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:7070")
CURRENT_CERT = APP_DIR / "keys" / "current" / "current.cert.pem"
CURRENT_KEY = APP_DIR / "keys" / "current" / "current.key.pem"
REVOKED_CERT = APP_DIR / "keys" / "revoked" / "revoked.cert.pem"
REVOKED_KEY = APP_DIR / "keys" / "revoked" / "revoked.key.pem"
GATEWAY_LEDGER = APP_DIR / "distribution-gateway" / "data" / "gateway.json"

RECEIPT_RE = re.compile(r"RECEIPT=[^ ]+")
BUNDLE_LINE_RE = re.compile(
    r"^BUNDLE (?P<bundle_id>\S+) PUBLISHED RECEIPT=(?P<receipt>\S+) "
    r"TOKEN=(?P<token>\S+) STATUS=(?P<status>\S+)$"
)
SIGNED_LINE_RE = re.compile(
    r"^BUNDLE (?P<bundle_id>\S+) SIGNED KEY=(?P<key_id>\S+)$"
)


def mask_receipts(text: str) -> str:
    return RECEIPT_RE.sub("RECEIPT=<id>", text)


def reconcile_publishable_bundles(manifest_csv: Path) -> list[dict]:
    """Independent ground truth: DISTINCT rows, apply withdrawals, aggregate."""
    con = duckdb.connect(database=":memory:")
    con.execute(
        f"""
        CREATE TABLE manifest AS
        SELECT * FROM read_csv_auto('{manifest_csv.as_posix()}', header=true)
        """
    )
    rows = con.execute(
        """
        WITH deduped AS (
          SELECT DISTINCT * FROM manifest
        ),
        withdrawn AS (
          SELECT supersedes_id AS entry_id
          FROM deduped
          WHERE record_type = 'WITHDRAWAL'
            AND supersedes_id IS NOT NULL
            AND supersedes_id <> ''
        ),
        surviving_builds AS (
          SELECT *
          FROM deduped
          WHERE record_type = 'BUILD'
            AND entry_id NOT IN (SELECT entry_id FROM withdrawn)
        )
        SELECT
          bundle_id,
          COUNT(*)::INTEGER AS artifact_count,
          SUM(size_bytes)::BIGINT AS total_bytes
        FROM surviving_builds
        GROUP BY bundle_id
        ORDER BY bundle_id
        """
    ).fetchall()
    return [
        {
            "bundle_id": r[0],
            "artifact_count": int(r[1]),
            "total_bytes": int(r[2]),
        }
        for r in rows
    ]


def run_report() -> str:
    """Run the candidate publisher; fail clearly if the entrypoint is missing."""
    assert PUBLISHER_PATH.is_file(), (
        f"publisher missing at {PUBLISHER_PATH} — empty environment (Proof A) "
        "must score reward 0"
    )
    proc = subprocess.run(
        ["npm", "run", "report"],
        cwd=str(APP_DIR),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"npm run report failed ({proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    # npm prints a script banner on stdout; keep only BUNDLE lines.
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("BUNDLE ")]
    return "\n".join(lines) + ("\n" if lines else "")


def canonical_descriptor(bundle: dict) -> str:
    return (
        "{"
        f'"artifact_count":{bundle["artifact_count"]},'
        f'"bundle_id":{json.dumps(bundle["bundle_id"])},'
        f'"total_bytes":{bundle["total_bytes"]}'
        "}"
    )


def openssl_cms_sign(descriptor: str, cert: Path, key: Path) -> str:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="verify-sign-") as tmp:
        content = Path(tmp) / "descriptor.bin"
        content.write_text(descriptor, encoding="utf-8")
        proc = subprocess.run(
            [
                "openssl",
                "cms",
                "-sign",
                "-in",
                str(content),
                "-signer",
                str(cert),
                "-inkey",
                str(key),
                "-outform",
                "PEM",
                "-binary",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"openssl cms -sign failed: {proc.stderr}"
        return proc.stdout


def load_gateway_ledger() -> dict:
    if not GATEWAY_LEDGER.is_file():
        return {"publications": {}, "tokenIndex": {}}
    return json.loads(GATEWAY_LEDGER.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def expected_bundles() -> list[dict]:
    return reconcile_publishable_bundles(MANIFEST_PATH)


@pytest.fixture(scope="module")
def first_report(expected_bundles) -> str:
    _ = expected_bundles
    return run_report()


# ---------------------------------------------------------------------------
# functional_criteria[id=report_output_matches]
# ---------------------------------------------------------------------------
def test_report_output_matches(first_report: str):
    """functional_criteria[id=report_output_matches]: npm run report matches the
    golden CLI snapshot (RECEIPT values masked) with deterministic ordering."""
    golden = GOLDEN_PATH.read_text(encoding="utf-8")
    assert mask_receipts(first_report) == mask_receipts(golden)


# ---------------------------------------------------------------------------
# functional_criteria[id=withdrawals_and_duplicates_reconciled]
# ---------------------------------------------------------------------------
def test_withdrawals_and_duplicates_reconciled(
    first_report: str, expected_bundles: list[dict]
):
    """functional_criteria[id=withdrawals_and_duplicates_reconciled]: published
    bundle ids match the SQL-reconciled set (duplicates collapsed, withdrawals
    applied, fully-withdrawn bundles omitted)."""
    published = []
    for line in first_report.splitlines():
        m = BUNDLE_LINE_RE.match(line)
        if m:
            published.append(m.group("bundle_id"))

    expected_ids = [b["bundle_id"] for b in expected_bundles]
    assert published == expected_ids
    # Fully-withdrawn BND-104 must not appear.
    assert "BND-104" not in published


# ---------------------------------------------------------------------------
# functional_criteria[id=bundles_signed_with_current_key_accepted]
# ---------------------------------------------------------------------------
def test_bundles_signed_with_current_key_accepted(first_report: str):
    """functional_criteria[id=bundles_signed_with_current_key_accepted]: every
    published line is STATUS=PUBLISHED and SIGNED KEY matches the current key."""
    key_meta = requests.get(f"{GATEWAY}/v1/signing-key/current", timeout=5)
    assert key_meta.status_code == 200
    current_key_id = key_meta.json()["key_id"]

    signed = []
    published = []
    for line in first_report.splitlines():
        sm = SIGNED_LINE_RE.match(line)
        if sm:
            signed.append(sm.groupdict())
        pm = BUNDLE_LINE_RE.match(line)
        if pm:
            published.append(pm.groupdict())

    assert signed, "expected SIGNED status lines"
    assert published, "expected PUBLISHED status lines"
    assert len(signed) == len(published)

    for row in signed:
        assert row["key_id"] == current_key_id
    for row in published:
        assert row["status"] == "PUBLISHED"
        assert row["token"] == f"token-{row['bundle_id']}"


# ---------------------------------------------------------------------------
# functional_criteria[id=receipts_and_tokens_persisted_in_duckdb]
# ---------------------------------------------------------------------------
def test_receipts_and_tokens_persisted_in_duckdb(
    first_report: str, expected_bundles: list[dict]
):
    """functional_criteria[id=receipts_and_tokens_persisted_in_duckdb]: after a
    run, releases.duckdb holds publication_id + request_token per bundle."""
    assert DB_PATH.is_file(), f"expected DuckDB at {DB_PATH}"

    con = duckdb.connect(str(DB_PATH), read_only=True)
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    assert "publications" in tables, f"publications table missing; tables={tables}"

    rows = con.execute(
        """
        SELECT bundle_id, request_token, publication_id
        FROM publications
        ORDER BY bundle_id
        """
    ).fetchall()

    by_bundle = {r[0]: {"token": r[1], "publication_id": r[2]} for r in rows}
    for bundle in expected_bundles:
        bid = bundle["bundle_id"]
        assert bid in by_bundle, f"missing DuckDB row for {bid}"
        assert by_bundle[bid]["token"] == f"token-{bid}"
        assert by_bundle[bid]["publication_id"]

    # Cross-check stdout receipts against DB.
    for line in first_report.splitlines():
        m = BUNDLE_LINE_RE.match(line)
        if not m:
            continue
        bid = m.group("bundle_id")
        assert by_bundle[bid]["publication_id"] == m.group("receipt")
        assert by_bundle[bid]["token"] == m.group("token")


# ---------------------------------------------------------------------------
# functional_criteria[id=idempotent_rerun_no_duplicate_publications]
# ---------------------------------------------------------------------------
def test_idempotent_rerun_no_duplicate_publications(
    first_report: str, expected_bundles: list[dict]
):
    """functional_criteria[id=idempotent_rerun_no_duplicate_publications]: a
    second run is byte-identical and the gateway still has one publication per
    publishable bundle."""
    ledger_before = load_gateway_ledger()
    count_before = len(ledger_before.get("publications") or {})

    second = run_report()
    assert second == first_report, "re-run output must be byte-identical"

    ledger_after = load_gateway_ledger()
    count_after = len(ledger_after.get("publications") or {})
    assert count_after == count_before
    assert count_after == len(expected_bundles)

    token_index = ledger_after.get("tokenIndex") or {}
    for bundle in expected_bundles:
        token = f"token-{bundle['bundle_id']}"
        assert token in token_index, f"gateway missing token {token}"


# ---------------------------------------------------------------------------
# functional_criteria[id=revoked_key_signature_rejected]
# ---------------------------------------------------------------------------
def test_revoked_key_signature_rejected(expected_bundles: list[dict]):
    """functional_criteria[id=revoked_key_signature_rejected]: a descriptor signed
    with the revoked key is rejected as UNTRUSTED_SIGNATURE (verification is real)."""
    assert expected_bundles, "fixture must yield at least one publishable bundle"
    bundle = expected_bundles[0]
    descriptor = canonical_descriptor(bundle)

    revoked_sig = openssl_cms_sign(descriptor, REVOKED_CERT, REVOKED_KEY)
    res = requests.post(
        f"{GATEWAY}/v1/publications",
        json={
            "descriptor": descriptor,
            "signature": revoked_sig,
            "request_token": "token-verifier-revoked-probe",
        },
        timeout=10,
    )
    assert res.status_code == 400
    body = res.json()
    assert body.get("error") == "UNTRUSTED_SIGNATURE"

    # Current-key control: same descriptor must still verify when signed correctly.
    current_sig = openssl_cms_sign(descriptor, CURRENT_CERT, CURRENT_KEY)
    ok = requests.post(
        f"{GATEWAY}/v1/publications",
        json={
            "descriptor": descriptor,
            "signature": current_sig,
            "request_token": "token-verifier-current-probe",
        },
        timeout=10,
    )
    assert ok.status_code == 200
    assert ok.json().get("status") == "PUBLISHED"
