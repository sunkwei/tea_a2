// Browser test for adding a workspace (a different project directory).
//
// Two entry paths matter, and they differ by whether the server has a desktop:
//
//  1. native picker available: "＋ 添加工作区…" opens the OS directory dialog
//     immediately (no form - the browser cannot produce an absolute path, so
//     asking the user to type one is the awkward part). Picking a directory
//     adds it and switches to it.
//  2. no picker (headless server, no zenity/kdialog, no override): the manual
//     form appears, the 浏览… button is hidden, and a directory that does not
//     exist yet offers to be created instead of dead-ending.
//
// A fake TEA_AGENT_DIR_PICKER stands in for the real dialog so this runs
// without a desktop.
//
//   node tests/ui_workspace_add.mjs [path-to-tea_agent-binary]
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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_wsadd_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj_a'), { recursive: true });
fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:1
    api_key: sk-test
    default_model: m
    models: {m: {supports_tools: true}}
roles: {main: {provider: mock, model: m}}
`);

let agent, chrome, ws, cdpPort;

async function startAgent(env, extraArgs) {
  const agentPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log')]
    .concat(extraArgs || []), { stdio: 'ignore', env });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) return agentPort; } catch {}
    await sleep(150);
  }
  throw new Error('agent did not start');
}

async function stopAgent() {
  if (!agent) return;
  agent.kill('SIGTERM');
  await sleep(300);
  agent = null;
}

async function openPage(agentPort) {
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(1600);
}

let id = 0;
const pending = new Map();
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

const chip = () => evalJs(`document.getElementById('ws-chip-text').textContent`);
const openAdd = `(function(){
  document.getElementById('ws-chip').click();
  var d = document.getElementById('ws-dropdown');
  var b = Array.from(d.querySelectorAll('.dd-item'))
    .filter(function (x) { return x.textContent.indexOf('添加工作区') >= 0; })[0];
  if (!b) return false;
  b.click();
  return true;
})()`;
const submit = `(function(){
  var bs = document.querySelectorAll('#dialog .actions .btn');
  if (!bs.length) return false;
  bs[bs.length - 1].click();
  return true;
})()`;
const fill = (dir, name) => evalJs(`(function(){
  var i = document.querySelectorAll('#dialog .field .ta');
  if (i.length < 1) return false;
  i[0].value = ${JSON.stringify(dir)};
  if (i.length > 1 && ${JSON.stringify(name)}) i[1].value = ${JSON.stringify(name)};
  return true;
})()`);

try {
  // ---- 1) native picker path -------------------------------------------
  const pickedDir = path.join(tmp, 'picked_by_dialog');
  fs.mkdirSync(pickedDir, { recursive: true });
  const picker = path.join(tmp, 'picker.sh');
  fs.writeFileSync(picker, '#!/bin/sh\necho ' + JSON.stringify(pickedDir) + '\n');
  fs.chmodSync(picker, 0o755);

  const agentPort = await startAgent(
    Object.assign({}, process.env, { TEA_AGENT_DIR_PICKER: picker }),
    ['--workspace', path.join(tmp, 'proj_a')]);

  cdpPort = await freePort();
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
    const msg = JSON.parse(raw);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  });
  await send('Page.enable');
  await send('Runtime.enable');
  await openPage(agentPort);

  const menu = await evalJs(`(function(){
    document.getElementById('ws-chip').click();
    var d = document.getElementById('ws-dropdown');
    return { hidden: d.classList.contains('hidden'),
             items: Array.from(d.children).map(function (c) { return c.textContent.trim(); }) };
  })()`);
  check('workspace chip opens a menu', menu && menu.hidden === false);
  check('menu offers 添加工作区', menu && menu.items.some((t) => t.indexOf('添加工作区') >= 0),
    menu && menu.items.join(' | '));
  await evalJs(`document.getElementById('ws-chip').click(); true`);

  const health = JSON.parse((await getJson(agentPort, '/api/health')).body);
  check('/api/health reports a usable picker', health.dir_picker === true, String(health.dir_picker));

  // "添加工作区…" must go straight to the dialog, not to a form.
  await evalJs(openAdd);
  await sleep(1500);
  const afterPick = await evalJs(`(function(){
    return { dialogOpen: !document.getElementById('overlay').classList.contains('hidden'),
             chip: document.getElementById('ws-chip-text').textContent };
  })()`);
  check('no form is shown: the native dialog is used directly',
    afterPick && afterPick.dialogOpen === false, JSON.stringify(afterPick));
  check('the picked directory becomes the workspace', afterPick && afterPick.chip === 'picked_by_dialog',
    afterPick && afterPick.chip);

  const listed = JSON.parse((await getJson(agentPort, '/api/workspaces')).body).workspaces.map((w) => w.path);
  check('picked directory is registered', listed.indexOf(pickedDir) >= 0, listed.join(', '));

  // Cancelling the dialog must be a no-op, not an error dialog.
  const cancelPicker = path.join(tmp, 'cancel.sh');
  fs.writeFileSync(cancelPicker, '#!/bin/sh\nexit 1\n');
  fs.chmodSync(cancelPicker, 0o755);
  await stopAgent();
  const agentPort2 = await startAgent(
    Object.assign({}, process.env, { TEA_AGENT_DIR_PICKER: cancelPicker }),
    ['--workspace', path.join(tmp, 'proj_a')]);
  await openPage(agentPort2);
  const beforeCancel = await chip();
  await evalJs(openAdd);
  await sleep(1200);
  const afterCancel = await evalJs(`(function(){
    return { dialogOpen: !document.getElementById('overlay').classList.contains('hidden'),
             chip: document.getElementById('ws-chip-text').textContent,
             toast: document.getElementById('toast').textContent };
  })()`);
  check('cancelling the dialog changes nothing',
    afterCancel && afterCancel.chip === beforeCancel && afterCancel.dialogOpen === false,
    JSON.stringify(afterCancel));

  // ---- 2) manual fallback when there is no picker ----------------------
  await stopAgent();
  const emptyBin = path.join(tmp, 'emptybin');
  fs.mkdirSync(emptyBin, { recursive: true });
  const envNoPicker = Object.assign({}, process.env, { PATH: emptyBin });
  delete envNoPicker.TEA_AGENT_DIR_PICKER;
  const agentPort3 = await startAgent(envNoPicker, ['--workspace', path.join(tmp, 'proj_a')]);
  const health3 = JSON.parse((await getJson(agentPort3, '/api/health')).body);
  check('/api/health reports no picker', health3.dir_picker === false, String(health3.dir_picker));

  await openPage(agentPort3);
  await evalJs(openAdd);
  await sleep(900);
  const form = await evalJs(`(function(){
    var d = document.getElementById('dialog');
    var row = d.querySelector('.field-row .btn');
    return { open: !document.getElementById('overlay').classList.contains('hidden'),
             title: (d.querySelector('h3') || {}).textContent,
             browseVisible: !!(row && row.offsetParent !== null) };
  })()`);
  check('without a picker the manual form appears',
    form && form.open === true && form.title === '添加工作区', JSON.stringify(form));
  check('the browse button is hidden without a picker', form && form.browseVisible === false);

  // A directory that does not exist yet: offer to create it.
  const newDir = path.join(tmp, 'proj_new');
  await fill(newDir, 'proj_new');
  await evalJs(submit);
  await sleep(900);
  const offer = await evalJs(`document.getElementById('dialog').textContent.replace(/\\s+/g, ' ')`);
  check('missing directory offers to create it',
    offer && offer.indexOf('目录不存在') >= 0 && offer.indexOf('创建并添加') >= 0,
    offer && offer.slice(0, 70));
  await evalJs(submit);
  await sleep(1500);
  check('directory is created on confirm', fs.existsSync(newDir));
  check('UI switches to the new workspace', (await chip()) === 'proj_new', await chip());

  // Cancel leaves nothing behind.
  const neverDir = path.join(tmp, 'proj_never');
  await evalJs(openAdd);
  await sleep(600);
  await fill(neverDir, 'proj_never');
  await evalJs(submit);
  await sleep(800);
  await evalJs(`(function(){ var bs = document.querySelectorAll('#dialog .actions .btn'); if (bs.length) bs[0].click(); return true; })()`);
  await sleep(400);
  check('cancel does not create the directory', !fs.existsSync(neverDir));
  check('cancel keeps the current workspace', (await chip()) === 'proj_new', await chip());

  // A file path is still refused by the server.
  const filePath = path.join(tmp, 'not_a_dir.txt');
  fs.writeFileSync(filePath, 'x');
  const status = await new Promise((res) => {
    const body = JSON.stringify({ path: filePath, name: 'bad', create: true });
    const req = http.request({ host: '127.0.0.1', port: agentPort3, path: '/api/workspaces', method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) } },
      (r) => { r.resume(); res(r.statusCode); });
    req.end(body);
  });
  check('a file path is rejected even with create:true', status === 400, 'HTTP ' + status);
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  await stopAgent();
  await sleep(200);
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
