// Browser test for adding a workspace (a different project directory).
//
// The entry point is easy to miss (the chip above the nav), and two behaviours
// matter once you find it: a brand-new directory must offer to create itself
// instead of dead-ending on "path does not exist", and the UI must switch to
// the workspace it just added - otherwise nothing appears to happen.
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

let agent, chrome, ws, mockServer;
try {
  mockServer = http.createServer((req, res) => {
    req.resume();
    req.on('end', () => {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      res.end('data: [DONE]\n\n');
    });
  });
  await new Promise((r) => mockServer.listen(0, '127.0.0.1', r));

  fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:${mockServer.address().port}
    api_key: sk-test
    default_model: m
    models: {m: {supports_tools: true}}
roles: {main: {provider: mock, model: m}}
`);

  const agentPort = await freePort();
  const cdpPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj_a')], { stdio: 'ignore' });

  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

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
  const fill = (dir, name) => evalJs(`(function(){
    var i = document.querySelectorAll('#dialog .field .ta');
    i[0].value = ${JSON.stringify(dir)};
    i[1].value = ${JSON.stringify(name)};
    return true;
  })()`);
  const submit = `(function(){
    var bs = document.querySelectorAll('#dialog .actions .btn');
    bs[bs.length - 1].click();
    return true;
  })()`;

  // ---- the menu really is reachable from the chip ------------------------
  const menu = await evalJs(`(function(){
    document.getElementById('ws-chip').click();
    var d = document.getElementById('ws-dropdown');
    return { hidden: d.classList.contains('hidden'),
             items: Array.from(d.children).map(function (c) { return c.textContent.trim(); }) };
  })()`);
  check('workspace chip opens a menu', menu && menu.hidden === false);
  check('menu offers 添加工作区', menu && menu.items.some((t) => t.indexOf('添加工作区') >= 0),
    menu && menu.items.join(' | '));
  await evalJs(`document.getElementById('ws-chip').click(); true`); // close

  // ---- case A: a directory that does not exist yet -----------------------
  const newDir = path.join(tmp, 'proj_new');
  check('precondition: target dir absent', !fs.existsSync(newDir));
  await evalJs(openAdd);
  await sleep(400);
  await fill(newDir, 'proj_new');
  await evalJs(submit);
  await sleep(1000);
  const offer = await evalJs(`(function(){
    var d = document.getElementById('dialog');
    return d.textContent.replace(/\\s+/g, ' ');
  })()`);
  check('missing directory offers to create it',
    offer && offer.indexOf('目录不存在') >= 0 && offer.indexOf('创建并添加') >= 0,
    offer && offer.slice(0, 70));

  await evalJs(submit);
  await sleep(1600);
  check('directory is created on confirm', fs.existsSync(newDir));
  check('UI switches to the new workspace', (await chip()) === 'proj_new', await chip());

  // ---- case B: an existing directory ------------------------------------
  const dirB = path.join(tmp, 'proj_b');
  fs.mkdirSync(dirB);
  await evalJs(openAdd);
  await sleep(400);
  await fill(dirB, 'proj_b');
  await evalJs(submit);
  await sleep(1600);
  check('existing directory is added without the extra prompt', (await chip()) === 'proj_b', await chip());

  const listed = JSON.parse((await getJson(agentPort, '/api/workspaces')).body).workspaces.map((w) => w.name);
  check('all three workspaces are indexed',
    ['proj_a', 'proj_new', 'proj_b'].every((n) => listed.indexOf(n) >= 0), listed.join(', '));

  // ---- case C: cancelling the create prompt changes nothing -------------
  const neverDir = path.join(tmp, 'proj_never');
  await evalJs(openAdd);
  await sleep(400);
  await fill(neverDir, 'proj_never');
  await evalJs(submit);
  await sleep(900);
  await evalJs(`(function(){ var bs = document.querySelectorAll('#dialog .actions .btn'); bs[0].click(); return true; })()`);
  await sleep(500);
  check('cancel does not create the directory', !fs.existsSync(neverDir));
  check('cancel keeps the current workspace', (await chip()) === 'proj_b', await chip());

  // ---- case D: the server side refuses a path that is a file ------------
  const filePath = path.join(tmp, 'not_a_dir.txt');
  fs.writeFileSync(filePath, 'x');
  const status = await new Promise((res) => {
    const body = JSON.stringify({ path: filePath, name: 'bad', create: true });
    const req = http.request({ host: '127.0.0.1', port: agentPort, path: '/api/workspaces', method: 'POST',
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
  if (agent) agent.kill('SIGTERM');
  if (mockServer) mockServer.close();
  await sleep(300);
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
