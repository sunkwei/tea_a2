// Browser test for the 文件 page's workspace-bundle buttons: 导出工作区包 must
// hand a real .zip to the browser, and 导入工作区包 must take a .zip and say
// what came in (and what was kept because this device already had it).
//
// The import is driven through the same <input type="file"> the page builds on
// the fly: the prototype's click() is intercepted, the file is put into the
// input with a DataTransfer, and the change event is dispatched - the page
// cannot tell the difference.
//
//   node tests/ui_workspace_bundle.mjs [path-to-tea_agent-binary]
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
import { acquireBrowser } from './chrome_pool.mjs';

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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_bundle_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });
fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:1
    api_key: sk-test
    default_model: m
    models: {m: {}}
`);

// One topic in the workspace, so the bundle has something to carry.
const run = path.join(tmp, 'proj', '.tea_agent_run');
fs.mkdirSync(path.join(run, 'sessions'), { recursive: true });
fs.writeFileSync(path.join(run, 'sessions', 's_uitest01.json'), JSON.stringify({
  id: 's_uitest01', title: 'the ui topic',
  created_at: '2026-11-25T00:00:00Z', updated_at: '2026-11-25T00:00:00Z',
  messages: [{ role: 'user', content: 'hello' }, { role: 'assistant', content: 'hi' }],
}, null, 2) + '\n');
fs.writeFileSync(path.join(run, 'sessions', 'index.json'), JSON.stringify([
  { id: 's_uitest01', title: 'the ui topic' }]));

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

  const cdpPort = await freePort();
  chrome = await acquireBrowser(tmp);
  const target = { webSocketDebuggerUrl: chrome.webSocketDebuggerUrl };
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

  // ---- the two buttons ---------------------------------------------------
  await evalJs(`document.querySelector('.nav-row[data-view="files"]').click(); true`);
  await sleep(700);
  const buttons = await evalJs(`(function(){
    function info(id) {
      var b = document.getElementById(id);
      if (!b) return null;
      return { label: b.textContent, title: b.title, disabled: b.disabled,
               visible: b.offsetParent !== null };
    }
    return { download: info('bundle-download'), upload: info('bundle-import') };
  })()`);
  check('the files page has an export button', !!buttons.download, JSON.stringify(buttons));
  check('it says what it exports', buttons.download && buttons.download.label.indexOf('导出') >= 0,
    buttons.download && buttons.download.label);
  check('it is enabled with a workspace open',
    buttons.download && buttons.download.disabled === false, JSON.stringify(buttons.download));
  check('it explains itself in a tooltip',
    buttons.download && buttons.download.title.length > 10, buttons.download && buttons.download.title);
  check('the files page has an import button', !!buttons.upload, JSON.stringify(buttons));
  check('the import button says what it imports',
    buttons.upload && buttons.upload.label.indexOf('导入') >= 0, buttons.upload && buttons.upload.label);

  // ---- the export produces a zip ----------------------------------------
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
  const busy = await evalJs(`(function(){
    document.getElementById('bundle-download').click();
    var b = document.getElementById('bundle-download');
    return { label: b.textContent, disabled: b.disabled };
  })()`);
  check('the export button shows progress while packing', busy && busy.disabled === true,
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

  const after = await evalJs(`(function(){
    var b = document.getElementById('bundle-download');
    return { label: b.textContent, disabled: b.disabled,
             toast: document.getElementById('toast').textContent };
  })()`);
  check('the export button is usable again afterwards', after && after.disabled === false,
    JSON.stringify(after));
  check('the toast says what was exported', after && after.toast.indexOf('已导出') >= 0 &&
    after.toast.indexOf('.zip') >= 0, after && after.toast);

  // ---- the import takes a zip -------------------------------------------
  // The page builds the file input on the fly, so the input's click() is
  // intercepted: the file is put in with a DataTransfer and the change event
  // is dispatched - the page cannot tell the difference.
  await evalJs(`(function(){
    window.__picked = null;
    var orig = HTMLInputElement.prototype.click;
    HTMLInputElement.prototype.click = function () {
      if (this.type === 'file') { window.__picked = this; return; }
      return orig.apply(this, arguments);
    };
    return true;
  })()`);
  await evalJs(`document.getElementById('bundle-import').click(); true`);
  await sleep(300);
  const picked = await evalJs(`!!window.__picked`);
  check('the import button asks for a file', picked === true, picked);

  const imported = await evalJs(`(async function(){
    var resp = await fetch('/api/workspaces/' + ${JSON.stringify(wsId)} + '/bundle');
    var blob = await resp.blob();
    var dt = new DataTransfer();
    dt.items.add(new File([blob], 'workspace-bundle.zip', { type: 'application/zip' }));
    window.__picked.files = dt.files;
    window.__picked.dispatchEvent(new Event('change'));
    return blob.size;
  })()`);
  check('the bundle was handed to the page', imported > 0, imported);

  let toast = '';
  for (let i = 0; i < 60; i++) {
    await sleep(300);
    toast = await evalJs(`document.getElementById('toast').textContent || ''`);
    if (toast.indexOf('导入完成') >= 0 || toast.indexOf('导入失败') >= 0) break;
  }
  check('the toast says the import is done', toast.indexOf('导入完成') >= 0, toast);
  check('the toast says what was kept (this device already had the topic)',
    toast.indexOf('保留本地') >= 0, toast);
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  if (agent) { agent.kill('SIGTERM'); await sleep(300); }
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch {}
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
