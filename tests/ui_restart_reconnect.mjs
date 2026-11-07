// Browser test: the agent restarts, and the page reconnects by itself.
//
// The flow people actually hit: tea_agent is restarted (a new build, a crash, the
// tray host restarting it) while the page is open. The stream dies with the
// server. What must happen next is *not* "the user hits refresh":
//
//   * the page says what is going on ("服务重启了，正在重新连接…"),
//   * it waits for the service to come back on its own,
//   * it reads the conversation again (everything that was written down is
//     still there - the transcript lives on disk),
//   * and the page is usable again: the next message streams.
//
// The page is never reloaded in this test, so every bit of the recovery is the
// page's own doing.
//
//   node tests/ui_restart_reconnect.mjs [path-to-tea_agent-binary]
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
const CHUNKS = 40;                  // ~12s of streaming per answer

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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_restart_ui_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });

// A slow answer: long enough that the server is still streaming when it dies.
let requests = 0;
const frame = (delta, finish) => 'data: ' + JSON.stringify({
  id: 'c1', object: 'chat.completion.chunk',
  choices: [{ index: 0, delta: delta, finish_reason: finish || null }],
}) + '\n\n';

const mock = http.createServer((req, res) => {
  let body = '';
  req.on('data', (d) => (body += d));
  req.on('end', () => {
    requests++;
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

let agent, chrome, ws;
const pending = new Map();
let msgId = 0;
const send = (method, params) => new Promise((res) => {
  const mid = ++msgId; pending.set(mid, res);
  ws.send({ id: mid, method, params });
});
const evalJs = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.result && r.result.exceptionDetails) {
    throw new Error(JSON.stringify(r.result.exceptionDetails.exception || r.result.exceptionDetails));
  }
  return r.result && r.result.result ? r.result.result.value : undefined;
};
const waitFor = async (fn, timeout, step) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    let v = null;
    try { v = await fn(); } catch (e) { v = null; }
    if (v) return v;
    await sleep(step || 300);
  }
  return null;
};

/// What the chat column looks like right now.
const view = () => evalJs(`(function(){
  var asss = document.querySelectorAll('#messages .msg.assistant');
  var last = asss.length ? asss[asss.length - 1] : null;
  var pill = document.getElementById('status-text');
  return { streaming: document.getElementById('send').className.indexOf('stop') >= 0,
           users: document.querySelectorAll('#messages .msg.user').length,
           assistants: asss.length,
           live: !!document.querySelector('#messages .msg.assistant.streaming'),
           text: last ? last.textContent : '',
           whole: (document.getElementById('messages') || {}).textContent || '',
           errors: document.querySelectorAll('#messages .msg.error').length,
           notices: Array.prototype.filter.call(
             document.querySelectorAll('#messages .msg-notice'),
             function (n) { return /重新连接|还没有回来/.test(n.textContent); }).length,
           pill: pill ? pill.textContent : '',
           input: (document.getElementById('input') || {}).value || '' };
})()`);

const say = (text) => evalJs(`(function(){
  var ta = document.getElementById('input');
  ta.value = ${JSON.stringify(text)};
  ta.dispatchEvent(new Event('input'));
  document.getElementById('send').click();
  return true;
})()`);

const startAgent = (port) => new Promise((res, rej) => {
  const p = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(port), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj')], { stdio: 'ignore' });
  p.on('exit', () => {});
  p.on('error', rej);
  res(p);
});

try {
  const agentPort = await freePort();
  agent = await startAgent(agentPort);
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  const cdpPort = await freePort();
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
  await sleep(2000);

  // ---- a turn that is streaming when the server dies ----------------------
  await say('answer slowly');
  const streaming = await waitFor(async () => {
    const v = await view();
    return v.streaming && v.text.length > 0 ? v : null;
  }, 15000, 250);
  check('the turn is streaming', !!streaming,
    streaming ? JSON.stringify(streaming) : 'never started');

  // ---- the server goes away ----------------------------------------------
  agent.kill('SIGKILL');
  await new Promise((r) => agent.on('exit', r));
  agent = null;
  await sleep(300);

  const noticed = await waitFor(async () => {
    const v = await view();
    return (v.pill.indexOf('未连接') === 0 || v.notices > 0) ? v : null;
  }, 20000, 400);
  check('the page notices that the service is gone', !!noticed,
    noticed ? JSON.stringify(noticed) : JSON.stringify(await view()));

  // ---- and comes back ----------------------------------------------------
  agent = await startAgent(agentPort);
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  const back = await waitFor(async () => {
    const v = await view();
    return v.pill.indexOf('已连接') === 0 ? v : null;
  }, 45000, 400);
  check('the page reconnects on its own', !!back,
    back ? JSON.stringify(back) : JSON.stringify(await view()));

  const settled = await waitFor(async () => {
    const v = await view();
    return v.errors === 0 ? v : null;
  }, 15000, 400);
  check('the stale error is gone', !!settled,
    settled ? JSON.stringify(settled) : JSON.stringify(await view()));

  // ---- the page is usable again ------------------------------------------
  await say('second question');
  const again = await waitFor(async () => {
    const v = await view();
    return v.streaming && v.text.length > 0 ? v : null;
  }, 20000, 250);
  check('the page can chat again', !!again,
    again ? JSON.stringify(again) : JSON.stringify(await view()));

  const sent = await view();
  check('both questions are on the screen', sent.users >= 2, JSON.stringify(sent));
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  if (agent) { agent.kill('SIGTERM'); await sleep(300); }
  mock.close();
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
