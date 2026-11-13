// Browser test for the roles (the stage profiles) in the web UI.
//
// The contract pinned here:
//   * the settings page shows a role card: the list of the workspace's roles,
//     the YAML of the selected one, and which role is current;
//   * editing the YAML marks it "unsaved"; saving writes the role file and
//     the server sees it;
//   * the top bar carries a role chip: it shows the current role, its menu
//     lists the roles, and picking one switches the workspace's role;
//   * switching the workspace reloads the card with that workspace's roles.
//
//   node tests/ui_role.mjs [path-to-tea_agent-binary]
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
const postJson = (port, p, body) => new Promise((res, rej) => {
  const data = JSON.stringify(body || {});
  const req = http.request({ host: '127.0.0.1', port, path: p, method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) } },
  (r) => {
    let b = ''; r.on('data', (d) => (b += d)); r.on('end', () => res({ status: r.statusCode, body: b }));
  });
  req.on('error', rej);
  req.end(data);
});

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || !detail ? '' : '  ' + detail));
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_role_ui_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj_a'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj_b'), { recursive: true });

const OWN_ROLE = `name: 手工角色
description: 测试自己写的角色
checklist: |
  改代码前先跑一次现有测试。
`;

let agent, chrome, ws;
try {
  fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    default_model: m
    models: {m: {}}
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

  const api = JSON.parse((await getJson(agentPort, '/api/workspaces')).body);
  const widA = api.workspaces.filter((w) => w.path.endsWith('proj_a'))[0].id;
  const widB = api.workspaces.filter((w) => w.path.endsWith('proj_b'))[0].id;

  // Two roles for proj_a, written through the API (the UI's own editor is
  // checked below).
  fs.mkdirSync(path.join(tmp, 'proj_a', '.tea_agent_run', 'roles'), { recursive: true });
  fs.writeFileSync(path.join(tmp, 'proj_a', '.tea_agent_run', 'roles', 'qa.yaml'), OWN_ROLE);

  chrome = await acquireBrowser(tmp, 'chrome', { windowSize: '1440,900' });

  const target = { webSocketDebuggerUrl: chrome.webSocketDebuggerUrl };
  if (!target) throw new Error('no chrome page target');

  ws = wsConnect(target.webSocketDebuggerUrl);
  await new Promise((r) => ws.on('open', r));
  let id = 0;
  const pending = new Map();
  ws.on('message', (raw) => {
    const msg = JSON.parse(raw);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); return; }
    // The copy asks for the new name with a prompt dialog; answer it.
    if (msg.method === 'Page.javascriptDialogOpening') {
      send('Page.handleJavaScriptDialog', { accept: true, promptText: 'my-copy' });
    }
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
    var ed = document.getElementById('role-editor');
    return {
      value: ed ? ed.value : '',
      items: Array.prototype.map.call(document.querySelectorAll('#role-list .role-item'),
        function (x) { return x.textContent; }),
      dir: document.getElementById('role-dir').textContent,
      meta: document.getElementById('role-meta').textContent,
      dirty: document.getElementById('role-meta').classList.contains('dirty'),
      saveDisabled: document.getElementById('role-save').disabled,
      chip: document.getElementById('role-chip-text').textContent,
      readOnly: !!(ed && ed.readOnly),
      visible: !document.getElementById('view-settings').classList.contains('hidden')
    };
  })()`);

  await evalJs(openSettings);
  await sleep(900);

  let p = await panel();
  check('the settings view shows the role card', p && p.visible === true, JSON.stringify(p));
  check('the role list shows the built-in roles',
    p && ['需求分析', '项目设计', '开发', '测试', '运维'].every((n) =>
      p.items.join('|').indexOf(n) >= 0), JSON.stringify(p && p.items));
  check('the read-only stage is marked',
    p && p.items.join('|').indexOf('只读') >= 0, JSON.stringify(p && p.items));
  check('the built-in roles are marked',
    p && p.items.join('|').indexOf('内置') >= 0, JSON.stringify(p && p.items));
  check('the editor holds the YAML of a role',
    p && (p.value.indexOf('name: 手工角色') >= 0 || p.value.indexOf('name: 需求分析') >= 0),
    JSON.stringify(p && p.value.slice(0, 80)));
  check('the roles directory is shown', p && p.dir === '.tea_agent_run/roles',
    JSON.stringify(p && p.dir));
  check('no role is current yet', p && p.chip === '角色', JSON.stringify(p && p.chip));

  // A built-in role is a template: its editor is read-only, and the copy is
  // what the user makes writable.
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('#role-list .role-item'),
      function (x) { return x.textContent.indexOf('需求分析') >= 0; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(700);
  p = await panel();
  check('a built-in role is read-only in the editor', p && p.readOnly === true,
    JSON.stringify(p && p.readOnly));
  await evalJs(`document.getElementById('role-copy').click(); true`);
  await sleep(1200);
  p = await panel();
  check('the copy shows up as a role of its own',
    p && p.items.join('|').indexOf('需求分析（副本）') >= 0, JSON.stringify(p && p.items));
  check('the copy is writable',
    p && p.readOnly === false && p.value.indexOf('name: 需求分析（副本）') >= 0,
    JSON.stringify(p && [p.readOnly, p.value.slice(0, 60)]));

  // Pick the role the test wrote (the built-in 测试 would be ambiguous).
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('#role-list .role-item'),
      function (x) { return x.textContent.indexOf('手工角色') >= 0; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(700);
  p = await panel();
  check('the editor shows the picked role',
    p && p.value.indexOf('name: 手工角色') >= 0, JSON.stringify(p && p.value.slice(0, 80)));

  // Edit -> dirty -> save.
  const marker = 'ui-role-' + Date.now();
  await evalJs(`(function(){
    var ed = document.getElementById('role-editor');
    ed.value = 'name: 手工角色\\ndescription: 测试自己写的角色\\nchecklist: |\\n  ${marker}\\n';
    ed.dispatchEvent(new Event('input'));
    return true;
  })()`);
  await sleep(200);
  p = await panel();
  check('editing marks the role as unsaved',
    p && p.dirty === true && p.saveDisabled === false, JSON.stringify(p && [p.dirty, p.saveDisabled]));
  await evalJs(`document.getElementById('role-save').click(); true`);
  await sleep(900);
  p = await panel();
  check('saving clears the dirty flag', p && p.dirty === false, JSON.stringify(p && [p.dirty, p.meta]));
  const onDisk = fs.readFileSync(path.join(tmp, 'proj_a', '.tea_agent_run', 'roles', 'qa.yaml'), 'utf8');
  check('the saved role is on disk', onDisk.indexOf(marker) >= 0, onDisk);
  const listed = JSON.parse((await getJson(agentPort, `/api/workspaces/${widA}/roles`)).body);
  check('the API serves the saved role',
    (listed.roles || []).some((r) => r.id === 'qa' && (r.checklist || '').indexOf(marker) >= 0),
    JSON.stringify((listed.roles || []).map((r) => r.id)));

  // The chip: open the menu, pick a role, the chip follows.
  await evalJs(`document.getElementById('role-chip').click(); true`);
  await sleep(400);
  const menu = await evalJs(`(function(){
    var dd = document.getElementById('role-dropdown');
    return {
      open: !dd.classList.contains('hidden'),
      items: Array.prototype.map.call(dd.querySelectorAll('.dd-item'), function (x) { return x.textContent; })
    };
  })()`);
  check('the role menu opens', menu && menu.open === true, JSON.stringify(menu));
  check('the menu lists the roles',
    menu && menu.items.join('|').indexOf('开发') >= 0 && menu.items.join('|').indexOf('测试') >= 0,
    JSON.stringify(menu && menu.items));
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('#role-dropdown .dd-item'),
      function (x) { return x.textContent.indexOf('开发') >= 0; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(900);
  p = await panel();
  check('the chip shows the role that was picked', p && p.chip === '开发', JSON.stringify(p && p.chip));
  const after = JSON.parse((await getJson(agentPort, `/api/workspaces/${widA}/roles`)).body);
  check('the workspace points at that role', after.current_id === 'dev', JSON.stringify(after.current_id));
  const settings = fs.readFileSync(path.join(tmp, 'proj_a', '.tea_agent_run', 'settings.yaml'), 'utf8');
  check('the pointer is in the workspace settings', settings.indexOf('role') >= 0 &&
    settings.indexOf('dev') >= 0, settings);

  // Switch workspace: the roles of that workspace are shown (it has none).
  await evalJs(`document.getElementById('ws-chip').click(); true`);
  await sleep(300);
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('#ws-dropdown .dd-item'),
      function (x) { return x.textContent.indexOf('proj_b') >= 0; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(1200);
  p = await panel();
  check('the other workspace has the built-in roles',
    p && p.items.length === 5 && p.items.join('|').indexOf('需求分析') >= 0,
    JSON.stringify(p && p.items));
  check('the chip follows the workspace', p && p.chip === '角色', JSON.stringify(p && p.chip));
  const bRoles = JSON.parse((await getJson(agentPort, `/api/workspaces/${widB}/roles`)).body);
  check('the other workspace has no current role', bRoles.current_id === '',
    JSON.stringify(bRoles.current_id));
} catch (e) {
  check('the test ran to the end', false, String(e && e.message || e));
} finally {
  try { if (ws) ws.close(); } catch {}
  try { if (chrome && chrome.close) await chrome.close(); } catch {}
  try { if (agent) agent.kill(); } catch {}
}

const failed = results.filter((r) => !r.ok);
console.log('');
console.log(`${results.length} checks, ${failed.length} failed`);
process.exit(failed.length ? 1 : 0);
