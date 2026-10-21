// Browser test for 「重置 provider」 in the settings view.
//
// The contract pinned here:
//   * the settings page offers a 重置 provider button next to 模型提供方;
//   * clicking it opens the SAME dialog as the first-run setup (quick key
//     channel, upload, paste), not a second, thinner one;
//   * that dialog states the risk - the current provider.yaml is overwritten and
//     the old one cannot be recovered - and says which file is affected;
//   * the dialog is dismissible here (Esc, the overlay, 取消): a mis-click must
//     not be able to replace a working configuration;
//   * cancelling changes nothing on disk;
//   * applying a new configuration does replace the file, and the running agent
//     picks it up without a restart.
//
//   node tests/ui_provider_reset.mjs [path-to-tea_agent-binary]
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
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || !detail ? '' : '  ' + detail));
};

const tmp = fs.mkdtempSync(path.join(process.env.TMP || os.tmpdir(), 'tea_reset_ui_'));
const home = path.join(tmp, 'home');
const proj = path.join(tmp, 'proj');
fs.mkdirSync(home, { recursive: true });
fs.mkdirSync(proj, { recursive: true });
const configPath = path.join(home, 'provider.yaml');

const FIRST = `version: 1
providers:
  first:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-first
    models:
      first-model: {}
`;
const SECOND = `version: 1
providers:
  second:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-second
    models:
      second-model: {}
`;
fs.writeFileSync(configPath, FIRST);

let agent, chrome, ws;
try {
  const agentPort = await freePort();
  agent = spawn(BIN, ['--tea-home', home, '--port', String(agentPort), '--no-console',
    '--log-file', path.join(home, 'logs', 'tea_agent.log'),
    '--workspace', proj], { stdio: 'ignore' });

  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }
  const health = JSON.parse((await getJson(agentPort, '/api/health')).body);
  check('the agent starts with the existing configuration',
    health.config_loaded === true && health.provider === 'first', JSON.stringify(health));

  const cdpPort = await freePort();
  chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
    '--window-size=1200,900',
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
  await sleep(2000);

  // A configured install must not be blocked by the first-run gate.
  const gate = await evalJs(`(function(){
    var d = document.getElementById('dialog');
    return { open: !document.getElementById('overlay').classList.contains('hidden'),
             title: d.querySelector('h3') ? d.querySelector('h3').textContent : '' };
  })()`);
  check('a configured install shows no setup gate', gate && gate.open === false, JSON.stringify(gate));

  // The settings page carries the button.
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('.nav-row'),
      function (x) { return x.dataset.view === 'settings'; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(900);
  const hasButton = await evalJs(`(function(){
    var b = document.getElementById('provider-reset');
    return { exists: !!b, visible: !!b && b.offsetParent !== null,
             text: b ? b.textContent : '' };
  })()`);
  check('the settings page offers a 重置 provider button',
    hasButton && hasButton.exists === true && hasButton.visible === true, JSON.stringify(hasButton));

  // Clicking it opens the setup dialog - with the risk spelled out.
  await evalJs(`document.getElementById('provider-reset').click(); true`);
  await sleep(700);
  const dialog = await evalJs(`(function(){
    var d = document.getElementById('dialog');
    var warn = d.querySelector('.setup-warn');
    var path = d.querySelector('.setup-path');
    return {
      open: !document.getElementById('overlay').classList.contains('hidden'),
      title: d.querySelector('h3') ? d.querySelector('h3').textContent : '',
      warn: warn ? warn.textContent : '',
      path: path ? path.textContent : '',
      hasQuick: !!d.querySelector('#setup-provider'),
      hasPaste: !!document.getElementById('setup-yaml'),
      hasDrop: !!d.querySelector('.setup-drop'),
      locked: document.getElementById('overlay').classList.contains('setup-locked')
    };
  })()`);
  check('the reset opens the first-run configuration dialog',
    dialog && dialog.open === true && dialog.title.indexOf('重置 provider') >= 0, JSON.stringify(dialog));
  check('the dialog is the same one: quick channel, upload and paste',
    dialog && dialog.hasQuick === true && dialog.hasPaste === true && dialog.hasDrop === true,
    JSON.stringify(dialog));
  check('the risk is spelled out',
    dialog && dialog.warn.indexOf('覆盖') >= 0 && dialog.warn.indexOf('无法撤销') >= 0,
    JSON.stringify(dialog && dialog.warn));
  check('the affected file is named',
    dialog && dialog.path.indexOf('provider.yaml') >= 0, JSON.stringify(dialog && dialog.path));
  check('unlike the first-run gate, this one can be closed',
    dialog && dialog.locked === false, JSON.stringify(dialog && dialog.locked));

  // Esc dismisses it, and nothing on disk changed.
  await evalJs(`(function(){
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    return true;
  })()`);
  await sleep(500);
  const afterEsc = await evalJs(`!document.getElementById('overlay').classList.contains('hidden')`);
  check('Esc closes the reset dialog', afterEsc === false, 'open=' + afterEsc);
  check('cancelling leaves the configuration alone',
    fs.readFileSync(configPath, 'utf8') === FIRST, configPath);

  // Apply a new configuration: that is the confirmation, and it replaces.
  await evalJs(`document.getElementById('provider-reset').click(); true`);
  await sleep(700);
  await evalJs(`(function(){
    var ta = document.getElementById('setup-yaml');
    ta.value = ${JSON.stringify(SECOND)};
    ta.dispatchEvent(new Event('input'));
    return true;
  })()`);
  await sleep(200);
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('#dialog .actions .btn'),
      function (x) { return x.textContent.indexOf('上传并应用') >= 0; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(3000);

  const text = fs.readFileSync(configPath, 'utf8');
  check('applying the new configuration replaced the file',
    text.indexOf('sk-second') >= 0 && text.indexOf('sk-first') < 0, text.slice(0, 200));
  const health2 = JSON.parse((await getJson(agentPort, '/api/health')).body);
  check('the running agent picked it up without a restart',
    health2.config_loaded === true && health2.provider === 'second', JSON.stringify(health2));
} catch (e) {
  check('no exception', false, e && e.stack ? e.stack : String(e));
} finally {
  try { if (ws) ws.close(); } catch {}
  for (const p of [chrome, agent]) {
    try { if (p) p.kill(); } catch {}
  }
  await sleep(300);
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch {}
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length === 0 ? 0 : 1);
