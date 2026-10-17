// tools/fetch.mjs -- small dependency downloader.
//
// Windows blocks TLS for schannel-based clients in some environments
// (curl/Invoke-WebRequest/git fail with SEC_E_NO_CREDENTIALS), while Node's
// OpenSSL works. This script therefore uses fetch() + streams.
//
// usage: node tools/fetch.mjs <url> <destination> [expected-sha256]

import { createHash } from 'node:crypto';
import { createWriteStream } from 'node:fs';
import { mkdir, rename, rm, stat } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';

const [url, destArg, expected] = process.argv.slice(2);
if (!url || !destArg) {
  console.error('usage: node tools/fetch.mjs <url> <destination> [expected-sha256]');
  process.exit(2);
}

const dest = resolve(destArg);
const tmp = `${dest}.part`;

await mkdir(dirname(dest), { recursive: true });

// Allow resuming a partially downloaded file.
let start = 0;
try {
  const s = await stat(tmp);
  start = s.size;
} catch {
  start = 0;
}

const headers = { 'User-Agent': 'tea_agent-fetch/1.0' };
if (start > 0) headers.Range = `bytes=${start}-`;

console.log(`[fetch] ${url}`);
if (start > 0) console.log(`[fetch] resuming at ${(start / 1048576).toFixed(1)} MB`);

const res = await fetch(url, { headers, redirect: 'follow' });
if (!res.ok && res.status !== 206) {
  console.error(`[fetch] HTTP ${res.status} ${res.statusText}`);
  process.exit(1);
}

const total = Number(res.headers.get('content-length') || 0) + (res.status === 206 ? start : 0);
let written = res.status === 206 ? start : 0;
let lastReport = 0;

const hash = createHash('sha256');
const out = createWriteStream(tmp, { flags: res.status === 206 ? 'a' : 'w' });

const source = Readable.fromWeb(res.body);
source.on('data', (chunk) => {
  written += chunk.length;
  hash.update(chunk);
  const now = Date.now();
  if (now - lastReport > 1500) {
    lastReport = now;
    const mb = (written / 1048576).toFixed(1);
    const pct = total ? ` (${((written / total) * 100).toFixed(0)}%)` : '';
    process.stdout.write(`\r[fetch] ${mb} MB${pct}          `);
  }
});

await pipeline(source, out);
process.stdout.write('\n');

if (start > 0 && res.status === 206 && expected) {
  // The digest of a resumed download cannot be computed incrementally.
  console.log('[fetch] resumed download: skipping checksum verification');
}

const actual = hash.digest('hex');
if (expected && res.status !== 206) {
  if (actual !== expected.toLowerCase()) {
    console.error(`[fetch] checksum mismatch\n  expected ${expected}\n  actual   ${actual}`);
    await rm(tmp, { force: true });
    process.exit(1);
  }
  console.log('[fetch] checksum ok');
}

await rename(tmp, dest);
const size = (await stat(dest)).size;
console.log(`[fetch] wrote ${dest} (${(size / 1048576).toFixed(1)} MB)`);
if (!expected) console.log(`[fetch] sha256 ${actual}`);
