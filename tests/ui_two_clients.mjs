// Browser test: two browsers, one topic, and what they may do to each other.
//
// A turn belongs to the server, keyed by session, so more than one client may
// watch it - that is the intended setup, not an edge case: start a long task in
// one window, open the topic link (`/?s=<session_id>`) in another, follow along.
// What has to hold:
//
//   * the topic URL is enough for the second browser: it opens that session
//     (not "the most recently updated one") and attaches to the running turn;
//   * both clients see the same stream, and a message typed in either one lands
//     in the *same* turn's queue instead of replacing the turn;
//   * "离开" only drops that one client: the server keeps going, the sidebar
//     marks the session as 进行中, and clicking it re-attaches;
//   * a client is free to open a new session while the turn runs (no more
//     "stop it first"), and the running turn is unaffected;
//   * 停止 still ends the turn for everybody.
//
//   node tests/ui_two_clients.mjs [path-to-tea_agent-binary]
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
const LONG_CHUNKS = 120;           // 150 ms each: ~18 s, long enough to observe

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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_two_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });

const frame = (delta, finish) => 'data: ' + JSON.stringify({
  id: 'c1', object: 'chat.completion.chunk',
  choices: [{ index: 0, delta, finish_reason: finish || null }],
}) + '\n\n';
const mock = http.createServer((req, res) => {
  let body = '';
  req.on('data', (d) => (body += d));
  req.on('end', () => {
    let prompt = '';
    try {
      const j = JSON.parse(body);
      const users = (j.messages || []).filter((m) => m.role === 'user');
      prompt = users.length ? String(users[users.length - 1].content || '') : '';
    } catch {}
    // Every answer is long: the queued prompt starts a second iteration inside
    // the same turn, and the test needs that turn to still be running after it.
    const total = LONG_CHUNKS;
    const tag = prompt.slice(0, 4) || 'ans';
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    let n = 0;
    const timer = setInterval(() => {
      n++;
      res.write(frame({ content: tag + '#' + n + ' ' }));
      if (n >= total) {
        clearInterval(timer);
        res.write(frame({}, 'stop'));
        res.write('data: [DONE]\n\n');
        res.end();
      }
    }, 150);
    res.on('close', () => clearInterval(timer));
  });
});
await new Promise((r) => mock.listen(0, '127.0.0.1', r));

fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:${mock.address().port}
    api_key: sk-test
    default_model: m
    models: {m: {supports_tools: true}}
`);

/// One browser with its own profile, driven over CDP. Everything the test
/// asserts is read from the real DOM and the URL, so a "topic" is observable.
async function openClient(name, url) {
  const cdpPort = await freePort();
  const proc = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
    `--remote-debugging-port=${cdpPort}`, `--user-data-dir=${path.join(tmp, name)}`,
    'about:blank'], { stdio: 'ignore' });
  let target = null;
  for (let i = 0; i < 120; i++) {
    try {
      const list = JSON.parse((await getJson(cdpPort, '/json/list')).body);
      target = list.find((t) => t.type === 'page');
      if (target) break;
    } catch {}
    await sleep(150);
  }
  if (!target) throw new Error('no page target for ' + name);
  const sock = wsConnect(target.webSocketDebuggerUrl);
  await new Promise((r) => sock.on('open', r));
  const pending = new Map();
  let mid = 0;
  sock.on('message', (raw) => {
    const m = JSON.parse(raw);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  const send = (method, params) => new Promise((res) => {
    const id = ++mid; pending.set(id, res); sock.send({ id, method, params });
  });
  await send('Page.enable');
  await send('Runtime.enable');
  const evalJs = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.result && r.result.exceptionDetails) {
      throw new Error(name + ': ' + JSON.stringify(r.result.exceptionDetails.exception || r.result.exceptionDetails));
    }
    return r.result && r.result.result ? r.result.result.value : undefined;
  };
  if (url) await send('Page.navigate', { url });
  return {
    name, proc, evalJs,
    goto: async (u) => { await send('Page.navigate', { url: u }); await sleep(500); },
    reload: async () => { await send('Page.reload', {}); await sleep(500); },
    close: () => { try { sock.close(); } catch {} proc.kill('SIGKILL'); },
  };
}

/// The bits of the UI both clients have to agree on.
const VIEW = `(function(){
  var a = document.querySelectorAll('#messages .msg.assistant');
  var last = a.length ? a[a.length - 1] : null;
  var q = document.getElementById('queue-strip');
  var live = document.querySelector('#messages .msg.assistant.streaming');
  return { streaming: document.getElementById('send').className.indexOf('stop') >= 0,
           detachVisible: !document.getElementById('detach').classList.contains('hidden'),
           users: document.querySelectorAll('#messages .msg.user').length,
           assistants: a.length,
           queued: q.classList.contains('hidden') ? 0 : q.querySelectorAll('.queue-item').length,
           running: document.querySelectorAll('.sess-running').length,
           liveText: live ? live.textContent : '',
           textLen: last ? last.textContent.length : 0,
           search: location.search,
           title: document.title };
})()`;

const waitFor = async (fn, timeout = 25000, step = 200) => {
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

let agent, clientA, clientB;
try {
  const agentPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj')], { stdio: 'ignore' });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }
  const base = `http://127.0.0.1:${agentPort}/`;
  const turnStatus = async (sid) =>
    JSON.parse((await getJson(agentPort, '/api/chat/turn?session_id=' + encodeURIComponent(sid))).body);

  // ---- 1) client A starts a long turn -----------------------------------
  clientA = await openClient('pcA', base);
  // Wait for the app to finish booting: the composer silently refuses to send
  // before a workspace is selected (`请先选择工作区`), and the workspace list is
  // fetched after the page renders. `window.teaState` is the app's own state, so
  // this does not race the DOM.
  const appReady = (client) => client.evalJs(`(function(){
    var t = window.teaState;
    if (!t || !t.workspace) return null;
    return { ws: t.workspace.name, sessions: t.sessions.length };
  })()`);
  const booted = await waitFor(() => appReady(clientA), 30000);
  check('A boots the UI with a workspace selected', !!booted, JSON.stringify(booted));
  // B is opened before the turn starts: a cold Chrome start can outlast a short
  // mock stream, and this test is about the URL, not about boot speed.
  clientB = await openClient('pcB', base);
  const bBooted = await waitFor(() => appReady(clientB), 30000);
  check('B boots the UI on the plain URL', !!bBooted, JSON.stringify(bBooted));
  // Any JS error during the flow is otherwise invisible in headless Chrome; the
  // diagnostics below are printed only when a check fails.
  await clientA.evalJs(`(function(){
    window.__errs = [];
    window.addEventListener('error', function (e) { window.__errs.push('error: ' + e.message); });
    window.addEventListener('unhandledrejection', function (e) {
      window.__errs.push('rejection: ' + ((e.reason && (e.reason.stack || e.reason.message)) || e.reason));
    });
    return true;
  })()`);
  await clientA.evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = '长任务 from A';
    ta.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);
  const aStreaming = await waitFor(async () => {
    const v = await clientA.evalJs(VIEW);
    return v.streaming && v.liveText.length > 10 ? v : null;
  });
  check('A is streaming and its live bubble has text', !!aStreaming, JSON.stringify(aStreaming));
  if (!aStreaming) {
    console.log('A diagnostics:', await clientA.evalJs(`JSON.stringify({
      errs: window.__errs || [], toast: document.getElementById('toast').textContent,
      send: document.getElementById('send').className,
      textLen: document.getElementById('input').value.length })`));
  }
  const sid = await waitFor(async () => {
    const d = JSON.parse((await getJson(agentPort, '/api/chat/turns')).body);
    return d.turns && d.turns.length ? d.turns[0].session_id : null;
  });
  check('the topic has a URL in A', !!sid && aStreaming && aStreaming.search === '?s=' + sid,
    aStreaming && aStreaming.search);

  // ---- 2) client B opens the topic URL ---------------------------------
  await clientB.goto(base + '?s=' + sid);
  const bAttached = await waitFor(async () => {
    const v = await clientB.evalJs(VIEW);
    return v.streaming && v.users === 1 ? v : null;
  }, 30000);
  check('B opens the topic URL and attaches to the running turn', !!bAttached,
    JSON.stringify(bAttached));
  check('B landed on the same session, not "the newest one"',
    !!bAttached && bAttached.search === '?s=' + sid, bAttached && bAttached.search);
  check('B sees the question A asked', !!bAttached && bAttached.users === 1,
    JSON.stringify(bAttached));
  const bGrew = await waitFor(async () => {
    const v = await clientB.evalJs(VIEW);
    return bAttached && v.liveText.length > bAttached.liveText.length + 20 ? v : null;
  }, 15000);
  check('B follows the same stream live', !!bGrew, JSON.stringify(bGrew));

  // ---- 3) B queues a prompt; both see it adopted ------------------------
  await clientB.evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = '排队 from B';
    ta.dispatchEvent(new Event('input'));
    ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    return true;
  })()`);
  const bQueued = await waitFor(async () => {
    const v = await clientB.evalJs(VIEW);
    return v.queued === 1 ? v : null;
  }, 8000);
  check('B queued the prompt instead of starting a second turn', !!bQueued, JSON.stringify(bQueued));
  const adopted = await waitFor(async () => {
    const a = await clientA.evalJs(VIEW);
    const b = await clientB.evalJs(VIEW);
    return a.users === 2 && b.users === 2 ? { a, b } : null;
  }, 40000);
  check('both clients see the queued prompt adopted in the same turn', !!adopted,
    JSON.stringify(adopted));
  check('both are still following after the adoption',
    !!adopted && adopted.a.streaming && adopted.b.streaming,
    adopted && JSON.stringify({ a: adopted.a.streaming, b: adopted.b.streaming }));

  // ---- 4) B leaves; the turn keeps going --------------------------------
  await clientB.evalJs(`document.getElementById('detach').click(); true`);
  const bLeft = await waitFor(async () => {
    const v = await clientB.evalJs(VIEW);
    return !v.streaming && v.running === 1 ? v : null;
  }, 8000);
  check('B stopped following and the sidebar marks the session', !!bLeft, JSON.stringify(bLeft));
  const stillRunning = await turnStatus(sid);
  check('the server did not cancel the turn', stillRunning.running === true,
    JSON.stringify(stillRunning));
  const aStill = await clientA.evalJs(VIEW);
  check('A is unaffected by B leaving', aStill.streaming === true, JSON.stringify(aStill));

  // ---- 5) B is free to open another topic while the turn runs -----------
  const draft = await clientB.evalJs(`(function(){
    document.getElementById('new-session').click();
    return { empty: !!document.querySelector('#messages .chat-empty'),
             search: location.search };
  })()`);
  await sleep(600);
  const draftState = await clientB.evalJs(VIEW);
  check('B can start a new topic while A keeps streaming',
    draft && draft.empty === true && draftState.streaming === false,
    JSON.stringify(draft) + ' / ' + JSON.stringify(draftState));
  check('the new draft has no topic in the URL', draft && draft.search === '',
    JSON.stringify(draft));
  const stillRunning2 = await turnStatus(sid);
  check('B navigating away did not disturb the turn', stillRunning2.running === true,
    JSON.stringify(stillRunning2));

  // ---- 6) B comes back to the topic and re-attaches ---------------------
  await clientB.evalJs(`(function(){
    var rows = document.querySelectorAll('#session-list .sess-row');
    if (!rows.length) return false;
    rows[0].click();
    return true;
  })()`);
  const bBack = await waitFor(async () => {
    const v = await clientB.evalJs(VIEW);
    return v.streaming && v.users >= 1 ? v : null;
  }, 15000);
  check('clicking the session re-attaches B', !!bBack, JSON.stringify(bBack));
  check('the marker is gone once B follows again', !!bBack && bBack.running === 0,
    bBack && JSON.stringify(bBack));
  check('B is on the topic URL again', !!bBack && bBack.search === '?s=' + sid,
    bBack && bBack.search);

  // ---- 7) A stops it for everybody --------------------------------------
  await clientA.evalJs(`document.getElementById('send').click(); true`);
  const stopped = await waitFor(async () => {
    const st = await turnStatus(sid);
    const a = await clientA.evalJs(VIEW);
    const b = await clientB.evalJs(VIEW);
    return st.running === false && !a.streaming && !b.streaming ? { st, a, b } : null;
  }, 30000);
  check('stop cancels the turn for every client', !!stopped,
    JSON.stringify(stopped && { running: stopped.st.running, a: stopped.a.streaming,
                                b: stopped.b.streaming }));
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (clientA) clientA.close();
  if (clientB) clientB.close();
  if (agent) { agent.kill('SIGTERM'); await sleep(300); }
  mock.close();
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
