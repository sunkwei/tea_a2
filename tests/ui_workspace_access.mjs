// Browser test for the 文件访问范围 switch in the settings view.
//
// The contract pinned here:
//   * the settings page shows a two-option switch (受限工作区 / 全局) whose state
//     comes from the server, not from local guessing;
//   * the path of the settings.yaml it writes to is shown;
//   * "全局" asks for confirmation first (it is a permission widening), and
//     cancelling leaves everything as it was;
//   * confirming persists allow_outside_workspace=true, the card switches to the
//     warning state, and the server really serves paths outside the workspace;
//   * going back to 受限工作区 needs no confirmation, persists false, and the
//     server refuses outside paths again;
//   * an agent started with --allow-outside-workspace shows both options disabled
//     and explains that the CLI flag fixed it.
//
//   node tests/ui_workspace_access.mjs [path-to-tea_agent-binary]
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
const putJson = (port, p, body) => new Promise((res, rej) => {
  const data = JSON.stringify(body);
  const req = http.request({ host: '127.0.0.1', port, path: p, method: 'PUT',
    headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) } },
  (r) => { let b = ''; r.on('data', (d) => (b += d)); r.on('end', () => res({ status: r.statusCode, body: b })); });
  req.on('error', rej);
  req.end(data);
});

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || !detail ? '' : '  ' + detail));
};

const tmp = fs.mkdtempSync(path.join(process.env.TMP || os.tmpdir(), 'tea_access_ui_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });
fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    models: {m: {}}
settings: {}
`);
// One file outside the workspace, to prove the switch changes real behaviour.
const outsideFile = path.join(tmp, 'outside.txt');
fs.writeFileSync(outsideFile, 'outside the workspace boundary');

let agent, pinned, chrome, ws;

const connect = async (port) => {
  const cdpPort = await freePort();
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
  await send('Page.navigate', { url: `http://127.0.0.1:${port}/` });
  await sleep(1800);
  return evalJs;
};

const openSettings = `(function(){
  var b = Array.prototype.filter.call(document.querySelectorAll('.nav-row'),
    function (x) { return x.dataset.view === 'settings'; })[0];
  if (b) b.click();
  return true;
})()`;

const panel = (evalJs) => evalJs(`(function(){
  var ws = document.getElementById('access-workspace');
  var gl = document.getElementById('access-global');
  var dlg = document.getElementById('dialog');
  return {
    workspaceChecked: ws.getAttribute('aria-checked'),
    globalChecked: gl.getAttribute('aria-checked'),
    workspaceDisabled: ws.disabled,
    globalDisabled: gl.disabled,
    path: document.getElementById('access-path').textContent,
    meta: document.getElementById('access-meta').textContent,
    hint: document.getElementById('access-hint').textContent,
    hintWarn: document.getElementById('access-hint').classList.contains('warn'),
    dialogOpen: !document.getElementById('overlay').classList.contains('hidden'),
    dialogTitle: dlg.querySelector('h3') ? dlg.querySelector('h3').textContent : ''
  };
})()`);

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
  const outsideQuery = `/api/workspaces/${wsId}/file?path=` + encodeURIComponent(outsideFile);

  const evalJs = await connect(agentPort);
  await evalJs(openSettings);
  await sleep(900);

  let p = await panel(evalJs);
  check('the settings page shows the access switch', p && p.path === '.tea_agent_run/settings.yaml',
    JSON.stringify(p));
  check('a fresh workspace is restricted', p && p.workspaceChecked === 'true' && p.globalChecked === 'false',
    JSON.stringify(p));
  check('the hinted text explains the restriction',
    p && p.hint.indexOf('只能在当前工作区内') >= 0 && p.hintWarn === false, JSON.stringify(p && p.hint));

  // Global asks first; cancelling changes nothing.
  await evalJs(`document.getElementById('access-global').click(); true`);
  await sleep(300);
  p = await panel(evalJs);
  check('choosing 全局 opens a confirmation dialog',
    p && p.dialogOpen === true && p.dialogTitle.indexOf('解除工作区边界') >= 0, JSON.stringify(p));
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('#dialog .actions .btn'),
      function (x) { return x.textContent === '取消'; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(400);
  p = await panel(evalJs);
  check('cancelling leaves the workspace restricted',
    p && p.workspaceChecked === 'true' && p.dialogOpen === false, JSON.stringify(p));
  const stillBlocked = await getJson(agentPort, outsideQuery);
  check('and the server still refuses outside paths', stillBlocked.status === 400,
    'status=' + stillBlocked.status);

  // Confirm -> global.
  await evalJs(`document.getElementById('access-global').click(); true`);
  await sleep(300);
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('#dialog .actions .btn'),
      function (x) { return x.textContent === '解除边界'; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(900);
  p = await panel(evalJs);
  check('confirming switches the card to 全局',
    p && p.globalChecked === 'true' && p.workspaceChecked === 'false', JSON.stringify(p));
  check('the warning is shown while global', p && p.hintWarn === true &&
    p.hint.indexOf('访问整台机器上的任意路径') >= 0, JSON.stringify(p && p.hint));
  const allowed = await getJson(agentPort, outsideQuery);
  check('the server now serves the outside file', allowed.status === 200 &&
    allowed.body.indexOf('outside the workspace boundary') >= 0, 'status=' + allowed.status);
  const settingsOnDisk = fs.readFileSync(path.join(tmp, 'proj', '.tea_agent_run', 'settings.yaml'), 'utf8');
  check('the choice is persisted to settings.yaml', /allow_outside_workspace:\s*true/.test(settingsOnDisk),
    settingsOnDisk.trim());

  // Back to restricted, no dialog this time.
  await evalJs(`document.getElementById('access-workspace').click(); true`);
  await sleep(900);
  p = await panel(evalJs);
  check('tightening needs no confirmation', p && p.dialogOpen === false, JSON.stringify(p));
  check('the card is back to 受限工作区',
    p && p.workspaceChecked === 'true' && p.globalChecked === 'false', JSON.stringify(p));
  const blockedAgain = await getJson(agentPort, outsideQuery);
  check('outside paths are refused again', blockedAgain.status === 400, 'status=' + blockedAgain.status);

  // --- CLI-fixed instance: the switch must explain instead of lying ----------
  ws.close();
  chrome.kill();
  await sleep(500);

  const pinnedPort = await freePort();
  pinned = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(pinnedPort), '--no-console', '--log-file', path.join(tmp, 'pinned.log'),
    '--workspace', path.join(tmp, 'proj'), '--allow-outside-workspace'], { stdio: 'ignore' });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(pinnedPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  const evalJs2 = await connect(pinnedPort);
  await evalJs2(openSettings);
  await sleep(900);
  const q = await panel(evalJs2);
  check('a CLI-started agent shows 全局 as in effect',
    q && q.globalChecked === 'true', JSON.stringify(q));
  check('both options are disabled when the CLI fixed it',
    q && q.workspaceDisabled === true && q.globalDisabled === true, JSON.stringify(q));
  check('the meta says the command line fixed it', q && q.meta === '命令行固定',
    JSON.stringify(q && q.meta));
  check('the hint explains why it cannot be loosened',
    q && q.hint.indexOf('--allow-outside-workspace') >= 0, JSON.stringify(q && q.hint));
  const direct = await putJson(pinnedPort, `/api/workspaces/${wsId}/settings`,
    { allow_outside_workspace: false });
  check('the API refuses to loosen a CLI-fixed boundary', direct.status === 409,
    'status=' + direct.status);
} catch (e) {
  check('no exception', false, e && e.stack ? e.stack : String(e));
} finally {
  try { if (ws) ws.close(); } catch {}
  for (const p of [chrome, agent, pinned]) {
    try { if (p) p.kill(); } catch {}
  }
  await sleep(300);
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch {}
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length === 0 ? 0 : 1);
