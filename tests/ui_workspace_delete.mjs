// Browser test for deleting a workspace from the workspace dropdown.
//
// The contract pinned here:
//   * every row of the dropdown has a delete button next to the name;
//   * clicking it asks for confirmation and says what is (not) deleted -
//     only the registration is dropped, the project directory and its
//     .tea_agent_run stay on disk;
//   * cancelling keeps the workspace;
//   * confirming removes it from the index (GET /api/workspaces), from the
//     dropdown and from the server, but the files survive;
//   * deleting the workspace that is currently open switches the page to the
//     next one, or to the empty state when it was the last one.
//
//   node tests/ui_workspace_delete.mjs [path-to-tea_agent-binary]
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
const sendJson = (port, p, method, body) => new Promise((res, rej) => {
  const payload = JSON.stringify(body || {});
  const req = http.request({ host: '127.0.0.1', port, path: p, method,
    headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) } },
  (r) => {
    let b = ''; r.on('data', (d) => (b += d));
    r.on('end', () => res({ status: r.statusCode, body: b }));
  });
  req.on('error', rej);
  req.end(payload);
});

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || !detail ? '' : '  ' + detail));
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_wsdel_'));
const home = path.join(tmp, 'home');
const projA = path.join(tmp, 'proj_a');
const projB = path.join(tmp, 'proj_b');
for (const d of [home, projA, projB]) fs.mkdirSync(d, { recursive: true });
fs.writeFileSync(path.join(projA, 'main.c'), 'int main(void){return 0;}\n');

let agent, chrome, ws;

async function startAgent() {
  const agentPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', home,
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', projA], { stdio: 'ignore' });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) return agentPort; } catch {}
    await sleep(150);
  }
  throw new Error('agent did not start');
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
const toastText = () => evalJs(`document.getElementById('toast').textContent`);
const openMenu = () => evalJs(`(function(){
  var chip = document.getElementById('ws-chip');
  var dd = document.getElementById('ws-dropdown');
  if (dd.classList.contains('hidden')) chip.click();
  return true;
})()`);
const closeMenu = () => evalJs(`(function(){
  var chip = document.getElementById('ws-chip');
  var dd = document.getElementById('ws-dropdown');
  if (!dd.classList.contains('hidden')) chip.click();
  return true;
})()`);
const rows = () => evalJs(`Array.from(document.querySelectorAll('#ws-dropdown .dd-row')).map(function (r) {
  var it = r.querySelector('.dd-item');
  return { name: it ? it.textContent.replace(/\\d+\\s*会话|路径不存在/g, '').trim() : '',
           del: !!r.querySelector('.dd-del'),
           delTitle: r.querySelector('.dd-del') ? r.querySelector('.dd-del').title : '' };
})`);
const clickDelete = (name) => evalJs(`(function(){
  var rows = Array.from(document.querySelectorAll('#ws-dropdown .dd-row'));
  for (var i = 0; i < rows.length; i++) {
    var it = rows[i].querySelector('.dd-item');
    if (it && it.textContent.indexOf(${JSON.stringify(name)}) >= 0) {
      var d = rows[i].querySelector('.dd-del');
      if (!d) return false;
      d.click();
      return true;
    }
  }
  return false;
})()`);
const dialogText = () => evalJs(`(function(){
  var d = document.getElementById('dialog');
  return { open: !document.getElementById('overlay').classList.contains('hidden'),
           title: (d.querySelector('h3') || {}).textContent,
           text: d.textContent.replace(/\\s+/g, ' ') };
})()`);
const dialogButton = (label) => evalJs(`(function(){
  var bs = Array.from(document.querySelectorAll('#dialog .actions .btn'));
  for (var i = 0; i < bs.length; i++) if (bs[i].textContent.trim() === ${JSON.stringify(label)}) { bs[i].click(); return true; }
  return false;
})()`);

try {
  fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    default_model: m
    models: {m: {supports_tools: true}}
`);
  const agentPort = await startAgent();
  const added = await sendJson(agentPort, '/api/workspaces', 'POST', { path: projB, name: 'proj_b' });
  check('the second workspace is registered', added.status === 200, 'HTTP ' + added.status);

  // ---- the browser ----------------------------------------------------
  chrome = await acquireBrowser(tmp);
  const target = { webSocketDebuggerUrl: chrome.webSocketDebuggerUrl };
  if (!target) throw new Error('no chrome page target');
  ws = wsConnect(target.webSocketDebuggerUrl);
  await new Promise((r) => ws.on('open', r));
  ws.on('message', (raw) => {
    const msg = JSON.parse(raw);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  });
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(1800);
  check('the first workspace is open', (await chip()) === 'proj_a', await chip());

  // ---- 1) every row has a delete button -------------------------------
  await openMenu();
  let list = await rows();
  check('the dropdown lists both workspaces', list.length === 2, JSON.stringify(list));
  check('every row has a delete button', list.every((r) => r.del), JSON.stringify(list));
  check('the delete button says what it deletes',
    list.every((r) => r.delTitle.indexOf('删除工作区') >= 0), JSON.stringify(list));

  // ---- 2) the confirmation says the files stay ------------------------
  await clickDelete('proj_b');
  await sleep(300);
  let dlg = await dialogText();
  check('a confirmation dialog opens', dlg.open === true && dlg.title === '删除工作区', JSON.stringify(dlg));
  check('the dialog names the workspace', dlg.text.indexOf('proj_b') >= 0, dlg.text.slice(0, 120));
  check('the dialog says the files are not deleted',
    dlg.text.indexOf('不会被删除') >= 0, dlg.text.slice(0, 160));

  // ---- 3) cancelling keeps everything ---------------------------------
  await dialogButton('取消');
  await sleep(300);
  dlg = await dialogText();
  check('cancelling closes the dialog', dlg.open === false, JSON.stringify(dlg));
  const stillThere = JSON.parse((await getJson(agentPort, '/api/workspaces')).body).workspaces;
  check('cancelling keeps the workspace registered',
    stillThere.some((w) => w.path === projB), JSON.stringify(stillThere.map((w) => w.path)));

  // ---- 4) confirming removes it, the files stay ------------------------
  await openMenu();
  await clickDelete('proj_b');
  await sleep(300);
  await dialogButton('删除');
  await sleep(900);
  check('a toast reports the deletion', (await toastText()).indexOf('已删除工作区') >= 0, await toastText());
  const after = JSON.parse((await getJson(agentPort, '/api/workspaces')).body).workspaces;
  check('the server drops the workspace',
    !after.some((w) => w.path === projB), JSON.stringify(after.map((w) => w.path)));
  check('the project directory survives', fs.existsSync(projB));
  check('its .tea_agent_run survives', fs.existsSync(path.join(projB, '.tea_agent_run')));
  await openMenu();
  list = await rows();
  check('the dropdown no longer lists it', list.length === 1 && list[0].name.indexOf('proj_b') < 0,
    JSON.stringify(list));
  check('the open workspace did not change', (await chip()) === 'proj_a', await chip());

  // ---- 5) deleting the workspace that is open -------------------------
  await openMenu();
  await clickDelete('proj_a');
  await sleep(300);
  dlg = await dialogText();
  check('the open workspace can be deleted too', dlg.open === true && dlg.title === '删除工作区',
    JSON.stringify(dlg));
  await dialogButton('删除');
  await sleep(1200);
  const empty = JSON.parse((await getJson(agentPort, '/api/workspaces')).body).workspaces;
  check('the server drops the last workspace', empty.length === 0, JSON.stringify(empty));
  check('the page falls back to the empty state', (await chip()) === '选择工作区', await chip());
  check('the project directory survives too', fs.existsSync(projA));
  check('its .tea_agent_run survives too', fs.existsSync(path.join(projA, '.tea_agent_run')));
  const ls = await evalJs(`(function(){ try { return JSON.parse(localStorage.getItem('tea_agent_ui_v1') || '{}').workspace_id; } catch (e) { return 'x'; } })()`);
  check('the deleted workspace is not remembered', !ls, String(ls));

  // ---- 6) the API is honest about unknown ids -------------------------
  const gone = await sendJson(agentPort, '/api/workspaces/no_such_id', 'DELETE', {});
  check('deleting an unknown workspace is a 404', gone.status === 404, 'HTTP ' + gone.status);
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
