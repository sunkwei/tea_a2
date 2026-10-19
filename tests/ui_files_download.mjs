// Browser test for the 文件 page's 下载 button: one click must produce a real
// .zip of *this device's* .tea_agent_run for the current workspace.
//
// The download is fetched as a blob (so a server-side error can be reported
// instead of being saved as a file), which also makes it observable: the test
// watches URL.createObjectURL and inspects the blob it was handed - magic bytes,
// MIME type, size - then checks the entry names by unpacking in the page is not
// needed, tests/smoke_run_archive.py does that properly.
//
//   node tests/ui_files_download.mjs [path-to-tea_agent-binary]
//
// Requires google-chrome (or CHROME_BIN) and node >= 18.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { wsConnect } from './mini_ws.mjs';

const REPO_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const BIN = process.argv[2] || path.join(REPO_ROOT, 'build', 'bin', 'tea_agent');
const CHROME = process.env.CHROME_BIN || '/usr/bin/google-chrome';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const freePort = () => new Promise((res) => {
  const s = net.createServer();
  s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); });
});
const getJson = (port, p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port, path: p }, (r) => {
    let b = ''; r.on('data', (d) => (b += d)); r.on('end', () => res({ status: r.statusCode, body: b }));
  }).on('error', rej);
});

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? '  ' + detail : ''));
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_dl_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });
fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:1
    api_key: sk-test
    default_model: m
    models: {m: {}}
roles: {main: {provider: mock, model: m}}
`);

let agent, chrome, ws;
const pending = new Map();
let msgId = 0;
const send = (method, params) => new Promise((res) => {
  const mid = ++msgId; pending.set(mid, res);
  ws.send({ id: mid, method, params });
});
const evalJs = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.result && r.result.exceptionDetails) {
    throw new Error(JSON.stringify(r.result.exceptionDetails.exception || r.result.exceptionDetails));
  }
  return r.result && r.result.result ? r.result.result.value : undefined;
};

try {
  const agentPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj')], { stdio: 'ignore' });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }
  const wsId = JSON.parse((await getJson(agentPort, '/api/workspaces')).body).workspaces[0].id;

  // Something recognisable to find inside the archive later.
  const run = path.join(tmp, 'proj', '.tea_agent_run');
  fs.mkdirSync(path.join(run, 'scripts'), { recursive: true });
  fs.writeFileSync(path.join(run, 'scripts', 'marker.txt'), 'download-marker\n');

  const cdpPort = await freePort();
  chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
    `--remote-debugging-port=${cdpPort}`, `--user-data-dir=${path.join(tmp, 'chrome')}`,
    'about:blank'], { stdio: 'ignore' });
  let target = null;
  for (let i = 0; i < 100; i++) {
    try {
      const list = JSON.parse((await getJson(cdpPort, '/json/list')).body);
      target = list.find((t) => t.type === 'page');
      if (target) break;
    } catch {}
    await sleep(150);
  }
  if (!target) throw new Error('no chrome page target');
  ws = wsConnect(target.webSocketDebuggerUrl);
  await new Promise((r) => ws.on('open', r));
  ws.on('message', (raw) => {
    const m = JSON.parse(raw);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(1800);

  // ---- the button -------------------------------------------------------
  await evalJs(`document.querySelector('.nav-row[data-view="files"]').click(); true`);
  await sleep(700);
  const btn = await evalJs(`(function(){
    var b = document.getElementById('files-download');
    if (!b) return null;
    return { label: b.textContent, title: b.title, disabled: b.disabled,
             visible: b.offsetParent !== null,
             inFilesView: !!document.getElementById('view-files').querySelector('#files-download') };
  })()`);
  check('the files page has a download button', !!btn, JSON.stringify(btn));
  check('it says what it downloads', btn && btn.label.indexOf('下载') >= 0 &&
    btn.label.indexOf('.tea_agent_run') >= 0, btn && btn.label);
  check('it is enabled with a workspace open', btn && btn.disabled === false, JSON.stringify(btn));
  check('it explains itself in a tooltip', btn && btn.title.length > 10, btn && btn.title);

  // ---- capture what the click produces ----------------------------------
  // createObjectURL has to stay synchronous for the app, so record the blob's
  // shape now and read its first four bytes asynchronously.
  await evalJs(`(function(){
    window.__dl = null;
    var orig = URL.createObjectURL.bind(URL);
    URL.createObjectURL = function (blob) {
      window.__dl = { size: blob.size, type: blob.type, magic: '' };
      blob.slice(0, 4).arrayBuffer().then(function (ab) {
        window.__dl.magic = Array.prototype.map.call(new Uint8Array(ab),
          function (x) { return ('0' + x.toString(16)).slice(-2); }).join('');
      });
      return orig(blob);
    };
    return true;
  })()`);
  // A small run directory packs in well under a round trip, so click and sample
  // in the same evaluation: the handler disables the button synchronously.
  const busy = await evalJs(`(function(){
    document.getElementById('files-download').click();
    var b = document.getElementById('files-download');
    return { label: b.textContent, disabled: b.disabled };
  })()`);
  check('the button shows progress while packing', busy && busy.disabled === true,
    JSON.stringify(busy));

  let dl = null;
  for (let i = 0; i < 60; i++) {
    await sleep(300);
    dl = await evalJs(`window.__dl && window.__dl.magic ? window.__dl : null`);
    if (dl) break;
  }
  check('a blob was handed to the browser as a download', !!dl, JSON.stringify(dl));
  check('it is a zip (PK\\x03\\x04)', dl && dl.magic === '504b0304', dl && dl.magic);
  check('it is served as application/zip', dl && dl.type === 'application/zip', dl && dl.type);
  check('it is not empty', dl && dl.size > 200, dl && dl.size);

  const after = await evalJs(`(function(){
    var b = document.getElementById('files-download');
    return { label: b.textContent, disabled: b.disabled,
             toast: document.getElementById('toast').textContent };
  })()`);
  check('the button is usable again afterwards', after && after.disabled === false &&
    after.label === btn.label, JSON.stringify(after));
  check('the toast says what was downloaded', after && after.toast.indexOf('已下载') >= 0 &&
    after.toast.indexOf('.zip') >= 0, after && after.toast);

  // ---- and the archive really is this workspace's run directory ----------
  const r = await new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port: agentPort,
      path: `/api/workspaces/${wsId}/archive?path=.tea_agent_run` }, (resp) => {
      const chunks = [];
      resp.on('data', (d) => chunks.push(d));
      resp.on('end', () => res({ status: resp.statusCode, body: Buffer.concat(chunks) }));
    }).on('error', rej);
  });
  check('the endpoint behind the button answers', r.status === 200, 'HTTP ' + r.status);
  // Not a byte comparison: entry timestamps move and the log grows between the
  // two requests. Entry names are stored verbatim in the local header, so finding
  // the marker file's name proves it is this workspace's run directory.
  check('the archive holds the marker file from this workspace',
    r.body.includes(Buffer.from('.tea_agent_run/scripts/marker.txt')),
    'size=' + r.body.length);
  check('both downloads are the same order of magnitude',
    dl && r.body.length > 0 && Math.abs(dl.size - r.body.length) < Math.max(4096, dl.size),
    (dl && dl.size) + ' vs ' + r.body.length);
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  if (agent) { agent.kill('SIGTERM'); await sleep(300); }
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
