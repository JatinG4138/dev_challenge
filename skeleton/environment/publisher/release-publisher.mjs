#!/usr/bin/env node

import duckdb from 'duckdb';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const GATEWAY = 'http://127.0.0.1:7070';
const MANIFEST = '/app/fixtures/build_manifest.csv';
const DB_PATH = '/app/releases.duckdb';
const CERT = '/app/keys/current/current.cert.pem';
const KEY = '/app/keys/current/current.key.pem';

const DEBUG = process.env.DEBUG === '1';
function dbg(...args) {
  if (DEBUG) console.error('[debug]', ...args);
}

function openDatabase() {
  return new duckdb.Database(DB_PATH);
}

function runExec(db, sql) {
  return new Promise((resolve, reject) => {
    db.run(sql, (err) => (err ? reject(err) : resolve()));
  });
}

function runQuery(db, sql, params = []) {
  return new Promise((resolve, reject) => {
    db.all(sql, ...params, (err, rows) => (err ? reject(err) : resolve(rows)));
  });
}

async function ensureSchema(db) {
  await runExec(
    db,
    `
    CREATE TABLE IF NOT EXISTS publications (
      bundle_id TEXT PRIMARY KEY,
      request_token TEXT NOT NULL,
      publication_id TEXT NOT NULL,
      descriptor TEXT NOT NULL
    );
    `
  );
}

async function loadManifest(db) {
  await runExec(
    db,
    `
    CREATE OR REPLACE TABLE manifest AS
    SELECT * FROM read_csv_auto('${MANIFEST}', header=true);
    `
  );

  const [{ n }] = await runQuery(db, 'SELECT COUNT(*)::INTEGER AS n FROM manifest');
  dbg('manifest rows loaded:', n);
}

async function reconcileBundles(db) {
  const sql = `
    WITH deduped AS (
      SELECT DISTINCT *
      FROM manifest
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
    ORDER BY bundle_id;
  `;

  const rows = await runQuery(db, sql);

  return rows.map((row) => ({
    bundle_id: row.bundle_id,
    artifact_count: Number(row.artifact_count),
    total_bytes: Number(row.total_bytes),
  }));
}


function buildDescriptor(bundle) {
  const artifact_count = Number(bundle.artifact_count);
  const total_bytes = Number(bundle.total_bytes);
  const bundle_id = bundle.bundle_id;

  return (
    '{' +
    `"artifact_count":${artifact_count},` +
    `"bundle_id":${JSON.stringify(bundle_id)},` +
    `"total_bytes":${total_bytes}` +
    '}'
  );
}

function signDescriptor(descriptor) {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'publisher-sign-'));
  const contentFile = path.join(tmpDir, 'descriptor.bin');

  try {
    fs.writeFileSync(contentFile, descriptor, 'utf8');

    const signature = execFileSync(
      'openssl',
      [
        'cms',
        '-sign',
        '-in',
        contentFile,
        '-signer',
        CERT,
        '-inkey',
        KEY,
        '-outform',
        'PEM',
        '-binary',
      ],
      { encoding: 'utf8' }
    );

    return signature;
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
}


async function getCurrentKey() {
  const res = await fetch(`${GATEWAY}/v1/signing-key/current`);
  if (!res.ok) {
    throw new Error(`GET /v1/signing-key/current failed with status ${res.status}`);
  }
  return res.json();
}

async function publishBundle(descriptor, signature, requestToken) {
  const res = await fetch(`${GATEWAY}/v1/publications`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      descriptor,
      signature,
      request_token: requestToken,
    }),
  });

  const body = await res.json();

  if (!res.ok) {
    throw new Error(`POST /v1/publications failed: ${JSON.stringify(body)}`);
  }

  return body;
}


async function getSavedPublication(db, bundleId) {
  const rows = await runQuery(
    db,
    'SELECT bundle_id, request_token, publication_id, descriptor FROM publications WHERE bundle_id = ?',
    [bundleId]
  );
  return rows[0] ?? null;
}

async function savePublication(db, bundleId, requestToken, publicationId, descriptor) {
  await runQuery(
    db,
    `
    INSERT INTO publications (bundle_id, request_token, publication_id, descriptor)
    VALUES (?, ?, ?, ?)
    `,
    [bundleId, requestToken, publicationId, descriptor]
  );
}


function printBundleReport(bundleId, keyId, receipt) {
  console.log(`BUNDLE ${bundleId} SIGNED KEY=${keyId}`);
  console.log(
    `BUNDLE ${bundleId} PUBLISHED RECEIPT=${receipt.publication_id} TOKEN=${receipt.request_token} STATUS=${receipt.status}`
  );
}


async function runReport() {
  const db = openDatabase();

  await ensureSchema(db);
  await loadManifest(db);

  const bundles = await reconcileBundles(db);
  dbg('publishable bundles:', bundles);

  const keyMeta = await getCurrentKey();
  dbg('current signing key:', keyMeta);

  for (const bundle of bundles) {
    const bundleId = bundle.bundle_id;
    const requestToken = `token-${bundleId}`;

    let receipt;

    const saved = await getSavedPublication(db, bundleId);
    if (saved) {
      dbg(`reusing saved publication for ${bundleId}`);
      receipt = {
        publication_id: saved.publication_id,
        request_token: saved.request_token,
        status: 'PUBLISHED',
      };
    } else {
      const descriptor = buildDescriptor(bundle);
      dbg(`descriptor for ${bundleId}:`, descriptor);

      const signature = signDescriptor(descriptor);
      dbg(`signed ${bundleId}`);

      receipt = await publishBundle(descriptor, signature, requestToken);
      dbg(`gateway receipt for ${bundleId}:`, receipt);

      await savePublication(
        db,
        bundleId,
        receipt.request_token,
        receipt.publication_id,
        descriptor
      );
    }

    printBundleReport(bundleId, keyMeta.key_id, receipt);
  }
}

async function main() {
  if (!process.argv.includes('--report')) {
    console.error('Usage: node publisher/release-publisher.mjs --report');
    process.exit(1);
  }

  await runReport();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
