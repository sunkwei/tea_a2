// Browser test: two clients, one turn.
//
// A turn belongs to the server, not to the browser that started it, so more than
// one client may watch it - and this is an encouraged setup, not an edge case:
// start a long task from the desktop, walk over to the laptop and pick it up.
//
// What has to hold:
//   * a second client (fresh profile, no localStorage) boots straight into the
//     running turn and follows the same stream;
//   * either client can queue a prompt, and both see it adopted;
//   * a client can *leave* without cancelling: 离开 stops following, the server
//     keeps going, the sidebar marks the session, and clicking it re-attaches;
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
const CHUNKS = 40;                 // ~12s per model answer

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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_two_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });

let modelRequests = 0;
const frame = (delta, finish) => 'data: ' + JSON.stringify({
  id: 'c1', object: 'chat.completion.chunk',
  choices: [{ index: 0, delta, finish_reason: finish || null }],
}) + '\n\n';
const mock = http.createServer((req, res) => {
  let body = '';
  req.on('data', (d) => (body += d));
  req.on('end', () => {
    modelRequests++;
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    let n = 0;
    const timer = setInterval(() => {
      n++;
      res.write(frame({ content: '字' }));
      if (n >= CHUNKS) {
        clearInterval(timer);
        res.write(frame({}, 'stop'));
        res.write('data: [DONE]\n\n');
        res.end();
      }
    }, 300);
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

/// One browser with its own profile, driven over CDP.
async function openClient(name, url) {
  const cdpPort = await freePort();
  const proc = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
    `--remote-debugging-port=${cdpPort}`, `--user-data-dir=${path.join(tmp, name)}`,
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
  if (url) {
    await send('Page.navigate', { url });
    await sleep(2000);
  }
  return {
    name,
    proc,
    evalJs,
    goto: async (u) => { await send('Page.navigate', { url: u }); await sleep(2000); },
    close: () => { try { sock.close(); } catch {} proc.kill('SIGKILL'); },
  };
}

/// The bits of the UI both clients have to agree on.
const VIEW = `(function(){
  var a = document.querySelectorAll('#messages .msg.assistant');
  var last = a.length ? a[a.length - 1] : null;
  var q = document.getElementById('queue-strip');
  return { streaming: document.getElementById('send').className.indexOf('stop') >= 0,
           detachVisible: !document.getElementById('detach').classList.contains('hidden'),
           users: document.querySelectorAll('#messages .msg.user').length,
           queued: q.classList.contains('hidden') ? 0 : q.querySelectorAll('.queue-item').length,
           running: document.querySelectorAll('.sess-running').length,
           textLen: last ? last.textContent.length : 0 };
})()`;

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
  const url = `http://127.0.0.1:${agentPort}/`;
  const turnStatus = async (sid) =>
    JSON.parse((await getJson(agentPort, '/api/chat/turn?session_id=' + encodeURIComponent(sid))).body);
  const sessionId = async () => {
    const wsId = JSON.parse((await getJson(agentPort, '/api/workspaces')).body).workspaces[0].id;
    const s = JSON.parse((await getJson(agentPort,
      '/api/workspaces/' + encodeURIComponent(wsId) + '/sessions')).body).sessions;
    return s.length ? s[0].id : null;
  };

  // ---- 1) client A starts the turn --------------------------------------
  clientA = await openClient('pcA', url);
  await clientA.evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = 'a long task from A';
    ta.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);
  await sleep(2500);
  const a1 = await clientA.evalJs(VIEW);
  check('A is streaming', a1.streaming === true && a1.detachVisible === true, JSON.stringify(a1));
  const sid = await sessionId();

  // ---- 2) client B boots into the same turn -----------------------------
  clientB = await openClient('pcB', url);
  await sleep(1500);
  const b1 = await clientB.evalJs(VIEW);
  check('B boots straight into the running turn', b1.streaming === true, JSON.stringify(b1));
  check('B replayed what A had already received', b1.textLen >= a1.textLen,
    'A=' + a1.textLen + ' B=' + b1.textLen);
  check('B sees the same question', b1.users === 1, JSON.stringify(b1));
  check('B can leave without cancelling', b1.detachVisible === true);

  await sleep(2000);
  const a2 = await clientA.evalJs(VIEW);
  const b2 = await clientB.evalJs(VIEW);
  check('both clients follow the same stream', a2.textLen > a1.textLen && b2.textLen > b1.textLen,
    'A ' + a1.textLen + '->' + a2.textLen + ', B ' + b1.textLen + '->' + b2.textLen);
  check('both are close to each other', Math.abs(a2.textLen - b2.textLen) <= 4,
    'A=' + a2.textLen + ' B=' + b2.textLen);

  // ---- 3) B queues a prompt; both see it adopted ------------------------
  await clientB.evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = 'queued from B';
    ta.dispatchEvent(new Event('input'));
    ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    return true;
  })()`);
  await sleep(1200);
  const bq = await clientB.evalJs(VIEW);
  check('B queued the prompt', bq.queued === 1, JSON.stringify(bq));

  // The turn adopts it at the next iteration boundary, i.e. after this answer.
  let adopted = null;
  for (let i = 0; i < 60; i++) {
    await sleep(700);
    const v = await clientB.evalJs(VIEW);
    if (v.users >= 2) { adopted = v; break; }
  }
  check('the queued prompt was adopted as a second question', adopted && adopted.users === 2,
    JSON.stringify(adopted));
  const aAfter = await clientA.evalJs(VIEW);
  check('A saw the adoption too', aAfter.users === 2, JSON.stringify(aAfter));
  check('the turn is still running for both', adopted.streaming === true && aAfter.streaming === true,
    'A=' + aAfter.streaming + ' B=' + adopted.streaming);

  // ---- 4) B leaves; the turn keeps going --------------------------------
  await clientB.evalJs(`document.getElementById('detach').click(); true`);
  await sleep(1200);
  const b3 = await clientB.evalJs(VIEW);
  check('B stopped following', b3.streaming === false && b3.detachVisible === false, JSON.stringify(b3));
  check('B marks the session as still running', b3.running === 1, JSON.stringify(b3));
  const st = await turnStatus(sid);
  check('the server did not cancel the turn', st.running === true, JSON.stringify(st));
  const a3 = await clientA.evalJs(VIEW);
  check('A is unaffected by B leaving', a3.streaming === true, JSON.stringify(a3));

  // B is free to look elsewhere now (this was blocked while following).
  const draft = await clientB.evalJs(`(function(){
    document.getElementById('new-session').click();
    return { empty: !!document.querySelector('#messages .chat-empty'),
             toast: document.getElementById('toast').textContent };
  })()`);
  await sleep(400);
  const stillRunning = await turnStatus(sid);
  check('B can open a new session while the turn runs',
    draft && draft.empty === true, JSON.stringify(draft));
  check('B navigating away did not disturb the turn', stillRunning.running === true,
    JSON.stringify(stillRunning));

  // ---- 5) B comes back to the session and re-attaches -------------------
  await clientB.evalJs(`(function(){
    var rows = document.querySelectorAll('#session-list .sess-row');
    if (!rows.length) return false;
    rows[0].click();
    return true;
  })()`);
  await sleep(2000);
  const b4 = await clientB.evalJs(VIEW);
  check('clicking the session re-attaches B', b4.streaming === true, JSON.stringify(b4));
  check('the marker is gone once B follows again', b4.running === 0, JSON.stringify(b4));

  // ---- 6) A stops it for everybody --------------------------------------
  await clientA.evalJs(`document.getElementById('send').click(); true`);
  await sleep(2500);
  const st2 = await turnStatus(sid);
  check('stop cancels the turn on the server', st2.running === false, JSON.stringify(st2));
  const a4 = await clientA.evalJs(VIEW);
  const b5 = await clientB.evalJs(VIEW);
  check('both clients stopped', a4.streaming === false && b5.streaming === false,
    'A=' + a4.streaming + ' B=' + b5.streaming);
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
