// Regression test: what happens when you touch the sidebar during a running turn.
//
// A streaming turn owns the conversation, so switching workspace or starting a
// new session has to be refused. The original implementation set `disabled` on
// the workspace chip, which produced *no* feedback at all: the button simply
// stopped responding, and "＋ 添加工作区…" was unreachable, so it looked like
// the feature was missing rather than temporarily unavailable.
//
// The contract pinned here:
//   * the chip stays clickable and the menu still opens (not a dead end);
//   * selecting another workspace is refused with an explanation;
//   * adding a workspace is refused the same way (no dialog);
//   * creating a session is refused the same way;
//   * session rows are visibly disabled;
//   * once the turn ends, switching works again.
//
//   node tests/ui_stream_guard.mjs [path-to-tea_agent-binary]
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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_guard_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
for (const d of ['proj_a', 'proj_b']) fs.mkdirSync(path.join(tmp, d), { recursive: true });

let agent, chrome, ws, mockServer;
try {
  // A deliberately slow stream: ~4s, long enough to interact with the UI.
  mockServer = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      let n = 0;
      const timer = setInterval(() => {
        n++;
        res.write('data: ' + JSON.stringify({ id: 'c', choices: [{ index: 0, delta: { content: '字' }, finish_reason: null }] }) + '\n\n');
        if (n >= 8) {
          clearInterval(timer);
          res.write('data: ' + JSON.stringify({ id: 'c', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }) + '\n\n');
          res.write('data: [DONE]\n\n');
          res.end();
        }
      }, 400);
      res.on('close', () => clearInterval(timer));
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
    '--workspace', path.join(tmp, 'proj_a'), '--workspace', path.join(tmp, 'proj_b')], { stdio: 'ignore' });

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
  const toast = () => evalJs(`document.getElementById('toast').textContent`);
  const openMenu = `(function(){
    document.getElementById('ws-chip').click();
    return !document.getElementById('ws-dropdown').classList.contains('hidden');
  })()`;
  const clickItem = (needle) => evalJs(`(function(){
    var d = document.getElementById('ws-dropdown');
    var b = Array.from(d.querySelectorAll('.dd-item'))
      .filter(function (x) { return x.textContent.indexOf(${JSON.stringify(needle)}) >= 0; })[0];
    if (!b) return false;
    b.click();
    return true;
  })()`);

  const startTurn = `(function(){
    var ta = document.getElementById('input');
    ta.value = 'hello';
    ta.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`;
  await evalJs(startTurn);
  await sleep(1000);

  // ---- while streaming ---------------------------------------------------
  const state = await evalJs(`(function(){
    return { chipDisabled: document.getElementById('ws-chip').disabled,
             chipTitle: document.getElementById('ws-chip').title,
             sending: document.getElementById('send').className.indexOf('stop') >= 0,
             ws: document.getElementById('ws-chip-text').textContent };
  })()`);
  check('turn is streaming', state && state.sending === true, JSON.stringify(state && state.sending));
  check('workspace chip stays clickable', state && state.chipDisabled === false);
  check('chip tooltip explains the block',
    state && state.chipTitle.indexOf('正在生成回答') >= 0, state && state.chipTitle);

  check('menu still opens (not a dead end)', (await evalJs(openMenu)) === true);
  await evalJs(clickItem('proj_b'));
  await sleep(400);
  const refused = await toast();
  check('switching is refused with a reason',
    refused && refused.indexOf('正在生成回答') >= 0 && refused.indexOf('切换工作区') >= 0, refused);
  check('workspace did not change', (await chip()) === 'proj_a', await chip());

  await evalJs(openMenu);
  await sleep(200);
  await evalJs(clickItem('添加工作区'));
  await sleep(400);
  const addRefused = await toast();
  check('adding a workspace is refused with a reason',
    addRefused && addRefused.indexOf('添加工作区') >= 0, addRefused);
  check('no dialog was opened',
    (await evalJs(`document.getElementById('overlay').classList.contains('hidden')`)) === true);

  await evalJs(`document.getElementById('new-session').click(); true`);
  await sleep(300);
  const newRefused = await toast();
  check('new session is refused with a reason',
    newRefused && newRefused.indexOf('新建会话') >= 0, newRefused);

  check('session rows look disabled',
    (await evalJs(`(function(){ var r = document.querySelector('.sess-row'); return r ? r.disabled : null; })()`)) === true);

  // ---- after the turn ends ----------------------------------------------
  for (let i = 0; i < 60; i++) {
    const sending = await evalJs(`document.getElementById('send').className.indexOf('stop') >= 0`);
    if (!sending) break;
    await sleep(250);
  }
  check('composer returns to send after the turn',
    (await evalJs(`document.getElementById('send').className.indexOf('stop') < 0`)) === true);

  await evalJs(openMenu);
  await sleep(200);
  await evalJs(clickItem('proj_b'));
  await sleep(1200);
  check('switching works once the turn is over', (await chip()) === 'proj_b', await chip());
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
