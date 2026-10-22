// Browser test for queued prompts ("消息插队").
//
// The contract pinned here:
//   * Enter while a turn is streaming enqueues the text instead of starting a
//     second turn, and clears the composer;
//   * the queued prompts show up in the composer's queue strip in order;
//   * × cancels one before it is adopted;
//   * when the agent loop adopts it, the chip disappears and the text becomes a
//     normal user bubble in the transcript;
//   * the turn continues (a second model request within the same SSE turn),
//     no second session/turn is created;
//   * once the turn is over the composer is back to "send" and the strip is gone.
//
//   node tests/ui_queue.mjs [path-to-tea_agent-binary]
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

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || !detail ? '' : '  ' + detail));
};
const waitFor = async (fn, timeout = 8000, step = 150) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await fn()) return true;
    await sleep(step);
  }
  return false;
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_queue_ui_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });

let agent, chrome, ws, mockServer;
const chatBodies = [];
try {
  // ~2s per model request: comfortably longer than the UI actions below, and the
  // adopted prompt triggers a second request inside the same turn.
  mockServer = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      if (!req.url.endsWith('/chat/completions')) {
        res.writeHead(404).end();
        return;
      }
      chatBodies.push(body);
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      let n = 0;
      const timer = setInterval(() => {
        n++;
        res.write('data: ' + JSON.stringify({
          id: 'c', choices: [{ index: 0, delta: { content: '字' }, finish_reason: null }],
        }) + '\n\n');
        if (n >= 6) {
          clearInterval(timer);
          res.write('data: ' + JSON.stringify({
            id: 'c', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
          }) + '\n\n');
          res.write('data: [DONE]\n\n');
          res.end();
        }
      }, 350);
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
    models: {m: {}}
`);

  const agentPort = await freePort();
  const cdpPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj')], { stdio: 'ignore' });

  for (let i = 0; i < 100; i++) {
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
  await sleep(1800);

  const queueState = () => evalJs(`(function(){
    var strip = document.getElementById('queue-strip');
    return {
      hidden: strip.classList.contains('hidden'),
      items: Array.prototype.map.call(strip.querySelectorAll('.queue-item'), function (row) {
        return row.querySelector('.q-text').textContent;
      }),
      input: document.getElementById('input').value
    };
  })()`);
  const enqueue = (text) => evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = ${JSON.stringify(text)};
    ta.dispatchEvent(new Event('input'));
    ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    return true;
  })()`);

  await evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = 'hello';
    ta.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);
  await sleep(600);
  check('turn is streaming',
    (await evalJs(`document.getElementById('send').className.indexOf('stop') >= 0`)) === true);

  await enqueue('queued one');
  await sleep(300);
  await enqueue('queued two');
  await sleep(400);

  let q = await queueState();
  check('Enter while streaming shows the queue strip', q && q.hidden === false, JSON.stringify(q));
  check('both prompts are listed in order',
    q && JSON.stringify(q.items) === JSON.stringify(['queued one', 'queued two']),
    JSON.stringify(q && q.items));
  check('the composer is cleared after queueing', q && q.input === '', JSON.stringify(q && q.input));
  check('no second turn was started',
    (await evalJs(`document.querySelectorAll('.sess-row').length`)) === 1);

  await evalJs(`(function(){
    var row = document.querySelector('#queue-strip .queue-item');
    row.querySelector('.q-cancel').click();
    return true;
  })()`);
  await sleep(500);
  q = await queueState();
  check('× cancels one prompt before adoption',
    q && JSON.stringify(q.items) === JSON.stringify(['queued two']), JSON.stringify(q && q.items));

  const adopted = await waitFor(async () => {
    return (await evalJs(`document.querySelectorAll('.msg.user').length`)) >= 2;
  });
  check('the adopted prompt becomes a user bubble', adopted);
  const bubbles = await evalJs(`Array.prototype.map.call(
    document.querySelectorAll('.msg.user'), function (n) { return n.textContent; })`);
  check('the adopted text is the one that was queued',
    Array.isArray(bubbles) && bubbles.some((t) => t.indexOf('queued two') >= 0),
    JSON.stringify(bubbles));
  q = await queueState();
  check('the adopted prompt left the queue strip', q && q.hidden === true, JSON.stringify(q));

  const finished = await waitFor(async () => {
    return (await evalJs(`document.getElementById('send').className.indexOf('stop') < 0`)) === true;
  }, 12000);
  check('the extended turn finishes', finished);
  check('the agent loop made exactly two model requests', chatBodies.length === 2, String(chatBodies.length));
  check('still one session', (await evalJs(`document.querySelectorAll('.sess-row').length`)) === 1);

  await sleep(600);  // finalizeStream reloads the session from disk
  const finalUsers = await evalJs(`Array.prototype.map.call(
    document.querySelectorAll('.msg.user'), function (n) { return n.textContent; })`);
  check('the transcript shows both user messages after the reload',
    Array.isArray(finalUsers) && finalUsers.length === 2
    && finalUsers[0].indexOf('hello') >= 0 && finalUsers[1].indexOf('queued two') >= 0,
    JSON.stringify(finalUsers));
  q = await queueState();
  check('the queue strip stays hidden after the turn', q && q.hidden === true, JSON.stringify(q));
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
