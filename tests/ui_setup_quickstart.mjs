// Browser test for the first-run quick setup ("只填 API key").
//
// The contract pinned here:
//   * a fresh install (no provider.yaml) opens the upload gate, and that gate
//     offers the quick channel: a provider select (DeepSeek or MiniMax), a
//     password field for the key, and a "用 key 创建配置" button, with the cursor
//     already in the key field;
//   * picking the other provider changes the hint and the key placeholder, so the
//     dialog says which key shape it wants (sk-... vs sk-cp-...);
//   * the full-upload path (drop zone + paste box) is still there;
//   * an empty key is refused in place and nothing is written;
//   * pasting a key and pressing Enter writes provider.yaml (DeepSeek,
//     deepseek-flash) and the page reloads into a working, configured UI: the
//     gate is gone, the status pill says 已连接, the model chip shows the model.
//
//   node tests/ui_setup_quickstart.mjs [path-to-tea_agent-binary]
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
const KEY = 'sk-ui-quickstart-' + Date.now();

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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_quickstart_ui_'));
const home = path.join(tmp, 'home');
const proj = path.join(tmp, 'proj');
fs.mkdirSync(home, { recursive: true });
fs.mkdirSync(proj, { recursive: true });
const configPath = path.join(home, 'provider.yaml');

let agent, chrome, ws;
try {
  const agentPort = await freePort();
  const cdpPort = await freePort();
  agent = spawn(BIN, ['--tea-home', home, '--port', String(agentPort), '--no-console',
    '--log-file', path.join(home, 'logs', 'tea_agent.log'),
    '--workspace', proj], { stdio: 'ignore' });

  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  chrome = await acquireBrowser(tmp, 'chrome', { windowSize: '1100,1000' });

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

  const gate = await evalJs(`(function(){
    var d = document.getElementById('dialog');
    var sel = document.getElementById('setup-provider');
    var key = document.getElementById('setup-key');
    var btn = document.getElementById('setup-quick-apply');
    return {
      open: !document.getElementById('overlay').classList.contains('hidden'),
      title: d.querySelector('h3') ? d.querySelector('h3').textContent : '',
      text: d.textContent,
      hasSelect: !!sel,
      options: sel ? Array.prototype.map.call(sel.options, function (o) { return o.textContent; }) : [],
      keyType: key ? key.type : '',
      keyPlaceholder: key ? key.placeholder : '',
      hasQuickBtn: !!btn,
      quickBtnText: btn ? btn.textContent : '',
      focused: document.activeElement ? document.activeElement.id : '',
      hasDropZone: !!document.querySelector('#dialog .setup-drop'),
      hasPasteBox: !!document.getElementById('setup-yaml')
    };
  })()`);
  check('the setup gate opens on a fresh install', gate && gate.open === true, JSON.stringify(gate));
  check('the gate still asks for provider.yaml', gate && gate.text.indexOf('provider.yaml') >= 0,
    JSON.stringify(gate && gate.text.slice(0, 80)));
  check('the quick channel is offered', gate && gate.text.indexOf('快捷开始：只填 API key') >= 0,
    JSON.stringify(gate && gate.text.slice(0, 200)));
  check('it offers DeepSeek and MiniMax',
    gate && gate.options.length === 2
      && gate.options[0].indexOf('DeepSeek') >= 0 && gate.options[0].indexOf('deepseek-flash') >= 0
      && gate.options[1].indexOf('MiniMax') >= 0
      && gate.options[1].indexOf('MiniMax-M3.1-Flash') >= 0, JSON.stringify(gate && gate.options));
  check('the key field is a password field with a sk- placeholder',
    gate && gate.keyType === 'password' && gate.keyPlaceholder.indexOf('sk-') === 0,
    JSON.stringify(gate && [gate.keyType, gate.keyPlaceholder]));
  check('there is a one-click create button',
    gate && gate.hasQuickBtn === true && gate.quickBtnText.indexOf('key') >= 0,
    JSON.stringify(gate && gate.quickBtnText));
  check('the cursor starts in the key field (fastest path)',
    gate && gate.focused === 'setup-key', JSON.stringify(gate && gate.focused));
  check('the full upload path is still available',
    gate && gate.hasDropZone === true && gate.hasPasteBox === true, JSON.stringify(gate));

  // The other quick channel says what it wants: MiniMax uses an M Plan key
  // (sk-cp-...), and the hint names its default model.
  const switched = await evalJs(`(function(){
    var sel = document.getElementById('setup-provider');
    sel.value = 'minimax';
    sel.dispatchEvent(new Event('change'));
    var key = document.getElementById('setup-key');
    var hint = document.getElementById('setup-quick-hint');
    return { hint: hint ? hint.textContent : '', placeholder: key ? key.placeholder : '' };
  })()`);
  check('picking MiniMax explains its key and its default model',
    switched && switched.hint.indexOf('MiniMax') >= 0
      && switched.hint.indexOf('MiniMax-M3.1-Flash') >= 0, JSON.stringify(switched));
  check('the key placeholder follows the provider',
    switched && switched.placeholder.indexOf('sk-cp-') === 0, JSON.stringify(switched));
  await evalJs(`(function(){
    var sel = document.getElementById('setup-provider');
    sel.value = 'deepseek';
    sel.dispatchEvent(new Event('change'));
    return true;
  })()`);
  const back = await evalJs(`(function(){
    var key = document.getElementById('setup-key');
    var hint = document.getElementById('setup-quick-hint');
    return { hint: hint ? hint.textContent : '', placeholder: key ? key.placeholder : '' };
  })()`);
  check('switching back restores the DeepSeek hint',
    back && back.hint.indexOf('DeepSeek') >= 0 && back.placeholder.indexOf('sk-') === 0,
    JSON.stringify(back));

  // An empty key must be refused in place, without writing anything.
  await evalJs(`document.getElementById('setup-quick-apply').click(); true`);
  await sleep(500);
  const empty = await evalJs(`(function(){
    var e = document.getElementById('setup-error');
    return { open: !document.getElementById('overlay').classList.contains('hidden'),
             error: e ? e.textContent : '' };
  })()`);
  check('an empty key is refused in the dialog',
    empty && empty.open === true && empty.error.indexOf('API key') >= 0, JSON.stringify(empty));
  check('the refusal wrote no file', !fs.existsSync(configPath), configPath);

  // Paste the key and press Enter (the documented fast path).
  await evalJs(`(function(){
    var k = document.getElementById('setup-key');
    k.value = '${KEY}';
    k.dispatchEvent(new Event('input'));
    k.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    return true;
  })()`);
  await sleep(3500);

  check('provider.yaml was written', fs.existsSync(configPath), configPath);
  const text = fs.existsSync(configPath) ? fs.readFileSync(configPath, 'utf8') : '';
  check('the file carries the pasted key', text.indexOf(KEY) >= 0, text.slice(0, 200));
  // No default_model: key - the FIRST model in the list is the default.
  check('the first model in the file is deepseek-flash',
    text.indexOf('models:') >= 0 &&
    text.indexOf('deepseek-flash:') === text.indexOf('deepseek-flash:', text.indexOf('models:')) &&
    text.indexOf('deepseek-flash:') < text.indexOf('deepseek-chat:'), text.slice(0, 500));
  check('the file carries no default_model: key',
    !/^\s*default_model:/m.test(text), text.slice(0, 400));
  check('the file only declares DeepSeek',
    text.indexOf('DeepSeek:') >= 0 && text.indexOf('opencode:') < 0, text.slice(0, 200));

  const after = await evalJs(`({
    gate: !!document.querySelector('#dialog .setup'),
    overlay: !document.getElementById('overlay').classList.contains('hidden'),
    overlayClass: document.getElementById('overlay').className,
    dialogHtml: document.getElementById('dialog').innerHTML.slice(0, 300),
    status: document.getElementById('status-text').textContent,
    model: document.getElementById('model-chip-text').textContent,
    url: location.href
  })`);
  check('the page reloaded', after && after.url.indexOf(`:${agentPort}/`) >= 0,
    JSON.stringify(after && after.url));
  check('the setup gate is gone', after && after.gate === false,
    JSON.stringify(after && [after.gate, after.overlayClass, after.dialogHtml]));
  check('no dialog is left blocking the UI',
    after && after.overlay === false, JSON.stringify(after && [after.overlayClass, after.dialogHtml]));
  check('the status pill says 已连接', after && after.status.indexOf('已连接') >= 0,
    JSON.stringify(after && after.status));
  check('the model chip shows the quick-start model',
    after && after.model.indexOf('deepseek-flash') >= 0, JSON.stringify(after && after.model));

  const health = JSON.parse((await getJson(agentPort, '/api/health')).body);
  check('the server is configured',
    health.config_loaded === true && health.provider === 'DeepSeek'
      && health.model === 'deepseek-flash', JSON.stringify(health));
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
