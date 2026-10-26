// Browser test: a device with NO provider.yaml must still be usable.
//
// Reported symptom: the web UI blocked on "需要配置 provider.yaml" and the
// 终端 (terminal) feature could not be reached at all — the first-run dialog
// was an un-dismissable full-screen overlay, and the terminal lives in the
// sidebar *behind* it.
//
// The contract pinned here:
//   * without a provider.yaml the model-dependent views (chat, files, memory,
//     logs) are greyed out, but 终端 and 设置 stay usable — the device is not
//     dead, the operator can still get a shell and configure the box;
//   * 设置 is where the provider gets configured (upload a file, or paste an
//     API key); after that every view comes back;
//   * the terminal starts in the project directory, not in the server's cwd;
//   * a provider.yaml written by hand is picked up ("重新检测" asks the server
//     to re-read the file — reloading the page would not, the config lives in
//     the server's memory).
//
//   node tests/ui_setup_terminal.mjs [path-to-tea_agent-binary]
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
  s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(); res(p); });
});
const getJson = (port, p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port, path: p }, (r) => {
    let b = ''; r.on('data', (d) => (b += d)); r.on('end', () => res({ status: r.statusCode, body: b }));
  }).on('error', rej);
});

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || !detail ? '' : '  ' + detail));
};
const waitFor = async (fn, timeout = 20000, step = 250) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await fn()) return true;
    await sleep(step);
  }
  return false;
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_setupterm_'));
const home = path.join(tmp, 'home');
const proj = path.join(tmp, 'proj');
for (const d of [home, proj]) fs.mkdirSync(d, { recursive: true });
const configPath = path.join(home, 'provider.yaml');

let agent, chrome, ws;
try {
  const port = await freePort();
  agent = spawn(BIN, ['--tea-home', home, '--port', String(port), '--no-console',
    '--log-file', path.join(tmp, 'agent.log'), '--workspace', proj], { stdio: 'ignore' });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(port, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  const health = JSON.parse((await getJson(port, '/api/health')).body);
  check('the server starts without a provider.yaml', health.config_loaded === false,
    JSON.stringify(health.config_loaded));

  chrome = await acquireBrowser(tmp, 'chrome', { windowSize: '1280,900' });
  ws = wsConnect(chrome.webSocketDebuggerUrl);
  await new Promise((r) => ws.on('open', r));
  let id = 0;
  const pending = new Map();
  ws.on('message', (raw) => {
    const msg = JSON.parse(raw);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  });
  const send = (method, params) => new Promise((res) => {
    const mid = ++id; pending.set(mid, res);
    ws.send({ id: mid, method, params });
  });
  const evalJs = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.result && r.result.exceptionDetails) {
      throw new Error(JSON.stringify(r.result.exceptionDetails.exception || r.result.exceptionDetails));
    }
    return r.result && r.result.result ? r.result.result.value : undefined;
  };

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: `http://127.0.0.1:${port}/` });
  await sleep(2500);

  // ---- 1) no wall: the app is usable, the model-bound views are greyed ---
  const nav = await evalJs(`(function(){
    var out = {};
    Array.prototype.forEach.call(document.querySelectorAll('.nav-row'), function (b) {
      out[b.dataset.view] = b.classList.contains('off');
    });
    return {
      off: out,
      view: document.querySelector('.view:not(.hidden)') ? document.querySelector('.view:not(.hidden)').id : '',
      dialogOpen: !document.getElementById('overlay').classList.contains('hidden'),
      status: document.getElementById('status-text').textContent,
      banner: !document.getElementById('banner').classList.contains('hidden')
    };
  })()`);
  check('no dialog blocks the app', nav && nav.dialogOpen === false, JSON.stringify(nav && nav.dialogOpen));
  check('the status pill says 未配置', nav && nav.status === '未配置', nav && nav.status);
  check('the banner explains the state', nav && nav.banner === true, JSON.stringify(nav));
  check('the chat view is greyed out', nav && nav.off.chat === true, JSON.stringify(nav && nav.off));
  check('the files view is greyed out', nav && nav.off.files === true, JSON.stringify(nav && nav.off));
  check('the memory view is greyed out', nav && nav.off.memory === true, JSON.stringify(nav && nav.off));
  check('the logs view is greyed out', nav && nav.off.logs === true, JSON.stringify(nav && nav.off));
  check('the terminal stays enabled', nav && nav.off.shell === false, JSON.stringify(nav && nav.off));
  check('the settings stay enabled', nav && nav.off.settings === false, JSON.stringify(nav && nav.off));

  // A greyed-out entry must not open its view; it explains why and sends the
  // user to 设置, where the provider gets configured.
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('.nav-row'),
      function (x) { return x.dataset.view === 'chat'; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(600);
  const afterChat = await evalJs(`(function(){
    var v = document.querySelector('.view:not(.hidden)');
    return { view: v ? v.id : '' };
  })()`);
  check('a greyed-out entry does not open its view', afterChat && afterChat.view !== 'view-chat',
    JSON.stringify(afterChat));

  // ---- 2) the terminal works ------------------------------------------
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('.nav-row'),
      function (x) { return x.dataset.view === 'shell'; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(1200);
  const viewOpen = await evalJs(`!document.getElementById('view-shell').classList.contains('hidden')`);
  check('the terminal view opens', viewOpen === true);

  const started = await waitFor(async () => {
    const s = await evalJs(`(function(){ var s = window.teaShell; return s ? s.id : ''; })()`);
    return !!s;
  });
  check('a shell session starts', started === true);

  // The shell must start in the project directory, not in the server's cwd.
  const meta = await evalJs(`document.getElementById('shell-meta').textContent`);
  check('the shell runs in the project directory', meta && meta.indexOf(proj) >= 0, meta);

  // ---- 3) 设置 is where the provider gets configured -------------------
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('.nav-row'),
      function (x) { return x.dataset.view === 'settings'; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(800);
  const settings = await evalJs(`(function(){
    var card = document.getElementById('setup-card');
    var btn = document.getElementById('setup-config');
    var reset = document.getElementById('provider-reset-row');
    return {
      cardVisible: !!card && !card.classList.contains('hidden'),
      hasButton: !!btn,
      resetHidden: !!reset && reset.classList.contains('hidden')
    };
  })()`);
  check('the settings page shows the config card', settings && settings.cardVisible === true,
    JSON.stringify(settings));
  check('the card has a config button', settings && settings.hasButton === true, JSON.stringify(settings));
  check('the reset row is hidden (nothing to overwrite)', settings && settings.resetHidden === true,
    JSON.stringify(settings));

  // The config button opens the provider dialog (upload / paste / API key).
  await evalJs(`document.getElementById('setup-config').click(); true`);
  await sleep(900);
  const dialog = await evalJs(`(function(){
    var d = document.getElementById('dialog');
    return {
      open: !document.getElementById('overlay').classList.contains('hidden'),
      text: d ? d.textContent : '',
      hasQuick: !!document.getElementById('setup-key'),
      hasPaste: !!document.getElementById('setup-yaml')
    };
  })()`);
  check('the config dialog opens', dialog && dialog.open === true, JSON.stringify(dialog));
  check('it offers the API key shortcut', dialog && dialog.hasQuick === true, JSON.stringify(dialog));
  check('it offers the file/paste route', dialog && dialog.hasPaste === true, JSON.stringify(dialog));

  // ---- 4) a hand-written provider.yaml is picked up --------------------
  // The point of deferring: write the file (from the terminal, from scp, from
  // anywhere) and tell the server to look again. The server holds the config
  // in memory, so reloading the page would NOT be enough.
  fs.writeFileSync(configPath, `version: 1
providers:
  Hand:
    api_url: https://api.deepseek.com
    api_key: sk-handwritten
    models:
      deepseek-chat: {}
`, 'utf8');
  const reload = await fetch(`http://127.0.0.1:${port}/api/config/reload`, { method: 'POST' });
  const reloadBody = await reload.json();
  check('the server re-reads the file', reload.status === 200 && reloadBody.ok === true,
    JSON.stringify(reloadBody));
  const after2 = JSON.parse((await getJson(port, '/api/health')).body);
  check('the hand-written provider is live', after2.config_loaded === true &&
    after2.provider === 'Hand', JSON.stringify(after2));

  // A broken file must not take the working configuration down with it.
  fs.writeFileSync(configPath, 'providers: [broken' + String.fromCharCode(10), 'utf8');
  const broken = await fetch(`http://127.0.0.1:${port}/api/config/reload`, { method: 'POST' });
  const brokenBody = await broken.json();
  check('a broken file is refused', broken.status === 400 &&
    (brokenBody.error || {}).code === 'invalid_config', JSON.stringify(brokenBody));
  const after3 = JSON.parse((await getJson(port, '/api/health')).body);
  check('the running configuration survives it', after3.config_loaded === true &&
    after3.provider === 'Hand', JSON.stringify(after3));
} catch (e) {
  check('the test ran to the end', false, e && e.message ? e.message : String(e));
} finally {
  if (agent) { try { agent.kill(); } catch {} }
  if (ws) { try { ws.close(); } catch {} }
  if (chrome) { try { await chrome.kill(); } catch {} }
  // Chrome may still be writing its profile: a failed cleanup must not fail
  // the test (the temp dir is under /tmp and gets collected anyway).
  try { fs.rmSync(tmp, { recursive: true, force: true, maxRetries: 3 }); } catch {}
}

console.log();
console.log(`${results.filter((r) => r.ok).length}/${results.length} checks passed`);
const failed = results.filter((r) => !r.ok).map((r) => r.name);
if (failed.length) {
  console.log('FAILED: ' + failed.join(', '));
  process.exit(1);
}
