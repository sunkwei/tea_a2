// Browser test for the system prompt editor in the settings view.
//
// The contract pinned here:
//   * the settings page shows a workspace-scoped system prompt card whose editor
//     holds the persona *actually in effect* - the default one when the
//     workspace has no override, never an empty box - plus its path;
//   * editing marks it "unsaved"; revert puts the in-effect persona back;
//   * saving writes .tea_agent_run/system_prompt.md, and the server (and the
//     prompt of the next turn) sees the new persona;
//   * the preview dialog shows the full prompt that will be sent;
//   * "恢复默认" clears the override, so the built-in persona comes back;
//   * switching workspace reloads the card with that workspace's own prompt.
//
//   node tests/ui_system_prompt.mjs [path-to-tea_agent-binary]
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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_sysprompt_ui_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj_a'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj_b'), { recursive: true });

let agent, chrome, ws;
try {
  fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    default_model: m
    models: {m: {}}
roles: {main: {provider: mock, model: m}}
`);

  const agentPort = await freePort();
  const cdpPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj_a'), '--workspace', path.join(tmp, 'proj_b')], { stdio: 'ignore' });

  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
    '--window-size=1440,900',
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
  await sleep(1800);

  const openSettings = `(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('.nav-row'),
      function (x) { return x.dataset.view === 'settings'; })[0];
    if (b) b.click();
    return true;
  })()`;

  const panel = () => evalJs(`(function(){
    var ed = document.getElementById('system-prompt-editor');
    return {
      value: ed.value,
      path: document.getElementById('system-prompt-path').textContent,
      meta: document.getElementById('system-prompt-meta').textContent,
      dirty: document.getElementById('system-prompt-meta').classList.contains('dirty'),
      hint: document.getElementById('system-prompt-hint').textContent,
      saveDisabled: document.getElementById('system-prompt-save').disabled,
      revertDisabled: document.getElementById('system-prompt-revert').disabled,
      defaultDisabled: document.getElementById('system-prompt-default').disabled,
      visible: !document.getElementById('view-settings').classList.contains('hidden')
    };
  })()`);

  await evalJs(openSettings);
  await sleep(900);

  const DEFAULT_PERSONA = 'tea_agent, a pragmatic software-engineering agent';

  let p = await panel();
  check('the settings view shows the system prompt card', p && p.visible === true, JSON.stringify(p));
  check('the editor shows the persona that is really in effect (not an empty box)',
    p && p.value.indexOf(DEFAULT_PERSONA) >= 0 && p.value.indexOf('Rules:') > 0,
    JSON.stringify(p && p.value.slice(0, 120)));
  check('the path is relative to the workspace',
    p && p.path === '.tea_agent_run/system_prompt.md', JSON.stringify(p && p.path));
  check('the meta says the default is in use', p && p.meta === '默认', JSON.stringify(p && p.meta));
  check('the hint names the default persona', p && p.hint.indexOf('当前：默认提示词') >= 0,
    JSON.stringify(p && p.hint));
  check('an untouched default is not "unsaved"',
    p && p.dirty === false && p.saveDisabled === true && p.revertDisabled === true,
    JSON.stringify(p && [p.dirty, p.saveDisabled, p.revertDisabled]));

  // Edit -> dirty -> revert.
  await evalJs(`(function(){
    var ed = document.getElementById('system-prompt-editor');
    ed.value = 'You are a browser-test persona.';
    ed.dispatchEvent(new Event('input'));
    return true;
  })()`);
  await sleep(200);
  p = await panel();
  check('editing marks the prompt as unsaved',
    p && p.dirty === true && p.meta === '未保存' && p.saveDisabled === false,
    JSON.stringify(p && [p.dirty, p.meta, p.saveDisabled]));
  await evalJs(`document.getElementById('system-prompt-revert').click(); true`);
  await sleep(200);
  p = await panel();
  check('revert restores the in-effect persona',
    p && p.dirty === false && p.value.indexOf(DEFAULT_PERSONA) >= 0,
    JSON.stringify(p && [p.dirty, p.value.slice(0, 80)]));

  // Edit -> save.
  const marker = 'ui-system-prompt-' + Date.now();
  await evalJs(`(function(){
    var ed = document.getElementById('system-prompt-editor');
    ed.value = '${marker}';
    ed.dispatchEvent(new Event('input'));
    document.getElementById('system-prompt-save').click();
    return true;
  })()`);
  await sleep(900);
  p = await panel();
  check('saving clears the dirty flag', p && p.dirty === false, JSON.stringify(p && [p.dirty, p.meta]));
  check('the meta now says custom', p && p.meta.indexOf('自定义') >= 0, JSON.stringify(p && p.meta));
  check('the hint now says custom', p && p.hint.indexOf('当前：工作区自定义') >= 0,
    JSON.stringify(p && p.hint));
  const onDisk = fs.readFileSync(path.join(tmp, 'proj_a', '.tea_agent_run', 'system_prompt.md'), 'utf8');
  check('the saved prompt is on disk', onDisk.indexOf(marker) >= 0, onDisk);
  const api = JSON.parse((await getJson(agentPort, '/api/workspaces')).body);
  const widA = api.workspaces.filter((w) => w.path.endsWith('proj_a'))[0].id;
  const fetched = JSON.parse((await getJson(agentPort, `/api/workspaces/${widA}/system-prompt`)).body);
  check('the API serves the saved override',
    fetched.content.indexOf(marker) >= 0 && fetched.custom === true, JSON.stringify(fetched));
  const effective = JSON.parse((await getJson(agentPort, `/api/workspaces/${widA}/prompt`)).body);
  check('the next prompt starts with the saved persona',
    effective.system_prompt.indexOf(marker) === 0 && effective.has_system_prompt === true,
    effective.system_prompt.slice(0, 80));

  // Preview dialog.
  await evalJs(`document.getElementById('system-prompt-preview').click(); true`);
  await sleep(700);
  const preview = await evalJs(`(function(){
    var pre = document.querySelector('#dialog .prompt-preview');
    return {
      open: !document.getElementById('overlay').classList.contains('hidden'),
      text: pre ? pre.textContent : '',
      title: document.querySelector('#dialog h3') ? document.querySelector('#dialog h3').textContent : ''
    };
  })()`);
  check('the preview dialog opens', preview && preview.open === true, JSON.stringify(preview));
  check('the preview shows the full prompt including the override',
    preview && preview.text.indexOf(marker) === 0 && preview.text.indexOf('Environment (probed') > 0,
    JSON.stringify(preview && preview.text.slice(0, 100)));
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('#dialog .actions .btn'),
      function (x) { return x.textContent === '关闭'; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(200);

  // Switch workspace: the card must show proj_b's (empty) prompt.
  await evalJs(`document.getElementById('ws-chip').click(); true`);
  await sleep(300);
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('#ws-dropdown .dd-item'),
      function (x) { return x.textContent.indexOf('proj_b') >= 0; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(1200);
  await evalJs(openSettings);
  await sleep(900);
  p = await panel();
  check('another workspace shows its own (default) prompt',
    p && p.value.indexOf(DEFAULT_PERSONA) >= 0 && p.value.indexOf(marker) < 0
      && p.dirty === false && p.meta === '默认',
    JSON.stringify(p && [p.value.slice(0, 60), p.dirty, p.meta]));

  // Back to proj_a, restore the default and save.
  await evalJs(`document.getElementById('ws-chip').click(); true`);
  await sleep(300);
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('#ws-dropdown .dd-item'),
      function (x) { return x.textContent.indexOf('proj_a') >= 0; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(1200);
  await evalJs(openSettings);
  await sleep(900);
  p = await panel();
  check('the first workspace still shows the saved override', p && p.value.trim() === marker,
    JSON.stringify(p && p.value));
  await evalJs(`document.getElementById('system-prompt-default').click(); true`);
  await sleep(200);
  p = await panel();
  check('restoring the default puts the default text back and marks it unsaved',
    p && p.value.indexOf(DEFAULT_PERSONA) >= 0 && p.dirty === true,
    JSON.stringify(p && [p.value.slice(0, 60), p.dirty]));
  await evalJs(`document.getElementById('system-prompt-save').click(); true`);
  await sleep(900);
  p = await panel();
  check('saving that text returns to the default',
    p && p.meta === '默认' && p.hint.indexOf('当前：默认提示词') >= 0
      && p.value.indexOf(DEFAULT_PERSONA) >= 0,
    JSON.stringify(p && [p.meta, p.hint]));
  check('the override file is empty again',
    fs.readFileSync(path.join(tmp, 'proj_a', '.tea_agent_run', 'system_prompt.md'), 'utf8').trim() === '');
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
