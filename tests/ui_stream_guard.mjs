// Regression test: what the sidebar may do while a turn is running.
//
// The old contract was "a streaming turn owns the conversation": switching
// workspace, adding one, or opening a new session were all refused (with a toast,
// because a `disabled` button explains nothing). That is exactly what makes
// several topics on one server unusable - to look at another topic you had to
// stop the work first, and "stop" cancels it for every window.
//
// The new contract, pinned here:
//   * the workspace chip stays clickable and the menu still opens;
//   * selecting another workspace is allowed: the page stops *following* the
//     turn (server keeps running, the session row stays marked 进行中);
//   * "＋ 添加工作区…" opens its dialog (nothing about it touches the turn);
//   * 新建会话 is a draft in this tab: it detaches, and the running turn survives;
//   * session rows are never disabled;
//   * coming back to the running session re-attaches;
//   * 停止 still ends the turn for everybody.
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
import { acquireBrowser } from './chrome_pool.mjs';

const REPO_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const BIN = process.argv[2] || path.join(REPO_ROOT, 'build', 'bin', 'tea_agent');
const CHROME = process.env.CHROME_BIN || '/usr/bin/google-chrome';
const CHUNKS = 200;                // 200 ms each: ~40 s, the turn outlives the test

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
const waitFor = async (fn, timeout = 12000, step = 150) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try {
      const v = await fn();
      if (v) return v;
    } catch (e) { /* keep polling */ }
    await sleep(step);
  }
  return false;
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_guard_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
for (const d of ['proj_a', 'proj_b']) fs.mkdirSync(path.join(tmp, d), { recursive: true });

let agent, chrome, ws, mockServer;
try {
  mockServer = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      let n = 0;
      const timer = setInterval(() => {
        n++;
        res.write('data: ' + JSON.stringify({ id: 'c', choices: [{ index: 0, delta: { content: '字' }, finish_reason: null }] }) + '\n\n');
        if (n >= CHUNKS) {
          clearInterval(timer);
          res.write('data: ' + JSON.stringify({ id: 'c', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }) + '\n\n');
          res.write('data: [DONE]\n\n');
          res.end();
        }
      }, 200);
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
`);

  const agentPort = await freePort();
  const cdpPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj_a'), '--workspace', path.join(tmp, 'proj_b')], { stdio: 'ignore' });

  for (let i = 0; i < 120; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  chrome = await acquireBrowser(tmp);

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
  const ready = () => evalJs(`(function(){
    var t = window.teaState;
    if (!t || !t.workspace) return null;
    return { ws: t.workspace.name, session: t.session ? t.session.id : null };
  })()`);
  const boot = await waitFor(ready, 30000);
  check('the UI boots on proj_a', !!boot && boot.ws === 'proj_a', JSON.stringify(boot));

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
  const runningTurn = async () => {
    const d = JSON.parse((await getJson(agentPort, '/api/chat/turns')).body);
    return (d.turns || [])[0] || null;
  };

  await evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = 'hello';
    ta.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);
  const streaming = await waitFor(async () => {
    const v = await evalJs(`(function(){
      var t = window.teaState;
      return { streaming: t.streaming, session: t.session ? t.session.id : null,
               chipDisabled: document.getElementById('ws-chip').disabled,
               chipTitle: document.getElementById('ws-chip').title };
    })()`);
    return v.streaming && v.session ? v : null;
  }, 15000);
  check('the turn is streaming', !!streaming, JSON.stringify(streaming));
  const sid = streaming && streaming.session;
  check('the workspace chip stays clickable', streaming && streaming.chipDisabled === false,
    JSON.stringify(streaming));
  check('the chip tooltip no longer says switching is blocked',
    streaming && streaming.chipTitle.indexOf('正在生成回答') < 0, streaming && streaming.chipTitle);

  // ---- switching workspace keeps the turn, drops the follow -------------
  check('the menu still opens', (await evalJs(openMenu)) === true);
  await evalJs(clickItem('proj_b'));
  const switched = await waitFor(async () => {
    const v = await evalJs(`(function(){
      var t = window.teaState;
      return { ws: t.workspace ? t.workspace.name : null, streaming: t.streaming };
    })()`);
    return v.ws === 'proj_b' ? v : null;
  }, 15000);
  check('switching workspace is allowed while a turn runs', !!switched, JSON.stringify(switched));
  check('this tab stopped following that turn', switched && switched.streaming === false,
    JSON.stringify(switched));
  const detached = await waitFor(async () => {
    const d = await evalJs(`window.teaState.detached`);
    return d === sid ? d : null;
  }, 5000);
  check('the page remembers it left that turn (so it is not pulled back)', !!detached,
    String(detached));
  const afterSwitch = await runningTurn();
  check('the server turn is still running', !!afterSwitch && afterSwitch.running === true,
    JSON.stringify(afterSwitch));

  // ---- adding a workspace opens its dialog ------------------------------
  await evalJs(openMenu);
  await sleep(200);
  await evalJs(clickItem('添加工作区'));
  const dialog = await waitFor(async () =>
    (await evalJs(`document.getElementById('overlay').classList.contains('hidden')`)) === false, 5000);
  check('adding a workspace opens its dialog', !!dialog);
  await evalJs(`(function(){
    var b = Array.from(document.querySelectorAll('#overlay button'))
      .filter(function (x) { return x.textContent.indexOf('取消') >= 0; })[0];
    if (b) b.click(); else document.getElementById('overlay').classList.add('hidden');
    return true;
  })()`);
  await sleep(200);
  check('the running turn survived the dialog',
    !!(await runningTurn()), JSON.stringify(await runningTurn()));

  // Back to proj_a, where the turn lives. Opening the workspace auto-opens its
  // newest session, i.e. the running one, so this tab follows it again.
  await evalJs(openMenu);
  await sleep(200);
  await evalJs(clickItem('proj_a'));
  const backToA = await waitFor(async () => {
    const v = await evalJs(`(function(){
      var t = window.teaState;
      return { ws: t.workspace ? t.workspace.name : null, streaming: t.streaming };
    })()`);
    return v.ws === 'proj_a' ? v : null;
  }, 15000);
  check('switching back re-opens proj_a', !!backToA, JSON.stringify(backToA));

  // ---- a new session is just a draft in this tab ------------------------
  await evalJs(`document.getElementById('new-session').click(); true`);
  const draft = await waitFor(async () => {
    const v = await evalJs(`(function(){
      var t = window.teaState;
      return { session: t.session, streaming: t.streaming, search: location.search,
               empty: !!document.querySelector('#messages .chat-empty') };
    })()`);
    return v.session === null && v.empty ? v : null;
  }, 10000);
  check('新建会话 opens a draft without stopping anything', !!draft, JSON.stringify(draft));
  check('the draft has no topic in the URL', draft && draft.search === '',
    draft && draft.search);
  check('the server turn is still running', !!(await runningTurn()));

  // ---- the running session is marked, clicking it re-attaches -----------
  const marked = await waitFor(async () => {
    const n = await evalJs(`document.querySelectorAll('.sess-running').length`);
    return n >= 1 ? n : null;
  }, 10000);
  check('the sidebar marks the running session 进行中', !!marked, String(marked));
  check('session rows are not disabled',
    (await evalJs(`(function(){ var r = document.querySelector('.sess-row'); return r ? r.disabled : null; })()`)) === false);

  await evalJs(`(function(){ var r = document.querySelector('#session-list .sess-row'); if (r) r.click(); return true; })()`);
  const back = await waitFor(async () => {
    const v = await evalJs(`(function(){
      var t = window.teaState;
      return { streaming: t.streaming, session: t.session ? t.session.id : null,
               search: location.search };
    })()`);
    return v.streaming && v.session === sid ? v : null;
  }, 20000);
  check('clicking the session re-attaches this tab', !!back, JSON.stringify(back));
  check('the topic URL points at it', back && back.search === '?s=' + sid, back && back.search);

  // ---- 停止 still ends it for everybody ---------------------------------
  await evalJs(`document.getElementById('send').click(); true`);
  const stopped = await waitFor(async () => {
    const t = await runningTurn();
    const streamingNow = await evalJs(`window.teaState.streaming`);
    return !t && streamingNow === false ? true : null;
  }, 20000);
  check('stop cancels the turn on the server and in the page', !!stopped);
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
