// Browser test for the "销毁" (destroy) button in the settings view.
//
// The contract pinned here:
//   * the settings page has a danger card with a 销毁 button;
//   * clicking it asks "是否真的彻底删除痕迹？" and lists exactly what will be
//     deleted (the tea home and each workspace's .tea_agent_run);
//   * cancelling deletes nothing;
//   * confirming deletes tea home (provider.yaml included) and every
//     workspace's .tea_agent_run, leaves the project directories alone, and the
//     page comes back in its first-run state (the upload gate, because there is
//     no provider.yaml any more) with the cached workspace/session ids cleared.
//
//   node tests/ui_destroy.mjs [path-to-tea_agent-binary]
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
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || !detail ? '' : '  ' + detail));
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_destroy_ui_'));
const home = path.join(tmp, 'home');
const projA = path.join(tmp, 'proj_a');
const projB = path.join(tmp, 'proj_b');
for (const d of [home, projA, projB]) fs.mkdirSync(d, { recursive: true });
fs.writeFileSync(path.join(projA, 'main.c'), 'int main(void){return 0;}\n');

let agent, chrome, ws;
try {
  fs.writeFileSync(path.join(home, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    default_model: m
    models: {m: {}}
`);

  const agentPort = await freePort();
  const cdpPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(home, 'provider.yaml'), '--tea-home', home,
    '--port', String(agentPort), '--no-console',
    '--log-file', path.join(home, 'logs', 'tea_agent.log'),
    '--workspace', projA, '--workspace', projB], { stdio: 'ignore' });

  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  chrome = await acquireBrowser(tmp, 'chrome', { windowSize: '1440,900' });

  const target = { webSocketDebuggerUrl: chrome.webSocketDebuggerUrl };
  if (!target) throw new Error('no chrome page target');

  ws = wsConnect(target.webSocketDebuggerUrl);
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
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(1800);

  const openSettings = `(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('.nav-row'),
      function (x) { return x.dataset.view === 'settings'; })[0];
    if (b) b.click();
    return true;
  })()`;

  await evalJs(openSettings);
  await sleep(900);

  const card = await evalJs(`(function(){
    var b = document.getElementById('destroy-run');
    return {
      visible: !document.getElementById('view-settings').classList.contains('hidden'),
      label: b ? b.textContent : '',
      danger: b ? b.classList.contains('danger') : false,
      home: document.getElementById('destroy-home').textContent
    };
  })()`);
  check('the settings view shows the destroy card',
    card && card.visible === true && card.label === '销毁…', JSON.stringify(card));
  check('the button is styled as dangerous', card && card.danger === true, JSON.stringify(card));
  check('the card names the real tea home', card && card.home === home,
    JSON.stringify(card && card.home));

  // Open the confirmation dialog.
  await evalJs(`document.getElementById('destroy-run').click(); true`);
  await sleep(900);
  const dlg = await evalJs(`(function(){
    var rows = Array.prototype.map.call(document.querySelectorAll('#dialog .destroy-row'),
      function (r) { return r.textContent; });
    var buttons = Array.prototype.map.call(document.querySelectorAll('#dialog .actions .btn'),
      function (b) { return b.textContent; });
    var d = document.getElementById('dialog');
    return {
      open: !document.getElementById('overlay').classList.contains('hidden'),
      title: d.querySelector('h3') ? d.querySelector('h3').textContent : '',
      text: d.textContent,
      rows: rows,
      buttons: buttons
    };
  })()`);
  check('the dialog asks the question',
    dlg && dlg.text.indexOf('是否真的彻底删除痕迹？') >= 0, JSON.stringify(dlg && dlg.text.slice(0, 120)));
  check('the dialog lists the tea home', dlg && dlg.rows.join('|').indexOf(home) >= 0,
    JSON.stringify(dlg && dlg.rows));
  check('the dialog lists both workspace run dirs',
    dlg && dlg.rows.join('|').indexOf(path.join(projA, '.tea_agent_run')) >= 0
      && dlg.rows.join('|').indexOf(path.join(projB, '.tea_agent_run')) >= 0,
    JSON.stringify(dlg && dlg.rows));
  check('the dialog offers cancel and a confirm',
    dlg && dlg.buttons.indexOf('取消') >= 0 && dlg.buttons.indexOf('彻底删除') >= 0,
    JSON.stringify(dlg && dlg.buttons));

  // Cancel deletes nothing.
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('#dialog .actions .btn'),
      function (x) { return x.textContent === '取消'; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(300);
  check('cancelling leaves everything in place',
    fs.existsSync(path.join(home, 'provider.yaml'))
      && fs.existsSync(path.join(projA, '.tea_agent_run'))
      && fs.existsSync(path.join(projB, '.tea_agent_run')));

  // Confirm and wait for the reload.
  await evalJs(`document.getElementById('destroy-run').click(); true`);
  await sleep(900);
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('#dialog .actions .btn'),
      function (x) { return x.textContent === '彻底删除'; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(3500);

  check('the tea home is gone', !fs.existsSync(home), home);
  check('the workspace run dirs are gone',
    !fs.existsSync(path.join(projA, '.tea_agent_run'))
      && !fs.existsSync(path.join(projB, '.tea_agent_run')));
  check('the project directory and its files survive',
    fs.existsSync(path.join(projA, 'main.c')));

  const after = await evalJs(`({
    url: location.href,
    gate: !!document.querySelector('#dialog .setup'),
    gateOpen: !document.getElementById('overlay').classList.contains('hidden'),
    gateText: document.getElementById('dialog').textContent,
    storage: localStorage.getItem('tea_agent_ui_v1')
  })`);
  check('the page is back in its first-run state',
    after && after.url.indexOf(`:${agentPort}/`) >= 0, JSON.stringify(after && after.url));
  check('the upload gate is shown again',
    after && after.gate === true && after.gateOpen === true
      && after.gateText.indexOf('provider.yaml') >= 0,
    JSON.stringify(after && [after.gate, after.gateOpen]));
  // The page re-saves its (now empty) state on load, so the stored entry may
  // exist again - what matters is that it no longer points at deleted ids.
  let stored = null;
  try { stored = JSON.parse(after && after.storage ? after.storage : 'null'); } catch {}
  check('the cached workspace/session ids were cleared',
    !stored || (stored.workspace_id == null && stored.session_id == null),
    JSON.stringify(after && after.storage));

  const health = JSON.parse((await getJson(agentPort, '/api/health')).body);
  check('the server reports an unconfigured agent', health.config_loaded === false,
    JSON.stringify(health.config_loaded));
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  if (agent) agent.kill('SIGTERM');
  await sleep(300);
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
