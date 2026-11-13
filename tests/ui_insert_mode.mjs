// Browser test for the two-stage interjection ("两段式插话").
//
// The contract pinned here:
//   * the composer has a chip that says when a message takes effect:
//     "⏳ 等这轮结束" (the default) or "⚡ 立即插入";
//   * the chip is clickable, and the choice survives a reload;
//   * Enter while a turn is streaming queues the message with the chip's
//     timing; Alt+Enter always queues it for an immediate insertion;
//   * the queue strip says which timing each queued message has;
//   * a message that waits for the turn to end takes effect in the **next**
//     turn: the transcript grows by one user message and the strip empties.
//
//   node tests/ui_insert_mode.mjs [path-to-tea_agent-binary]
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
const waitFor = async (fn, timeout = 12000, step = 150) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try { if (await fn()) return true; } catch (e) { /* keep waiting */ }
    await sleep(step);
  }
  return false;
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_insert_ui_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });

let agent, chrome, ws, mockServer;
const chatBodies = [];
try {
  // ~2s per model request: long enough for the UI actions below to land while
  // the turn is still busy.
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
  m:
    api_url: http://127.0.0.1:${mockServer.address().port}
    api_key: sk-test
    default_model: m
    models: {m: {}}
`);

  const agentPort = await freePort();
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

  const chip = () => evalJs(`(function(){
    var c = document.getElementById('insert-chip');
    return { text: c.textContent, on: c.classList.contains('on'), idle: c.classList.contains('idle'),
             title: c.title };
  })()`);

  const strip = () => evalJs(`(function(){
    var s = document.getElementById('queue-strip');
    return {
      hidden: s.classList.contains('hidden'),
      items: Array.prototype.map.call(s.querySelectorAll('.queue-item'), function (row) {
        return { text: row.querySelector('.q-text').textContent,
                 when: row.querySelector('.q-when').textContent,
                 now: !!row.querySelector('.q-when.now') };
      })
    };
  })()`);

  const type = (text, alt) => evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = ${JSON.stringify(text)};
    ta.dispatchEvent(new Event('input'));
    ta.dispatchEvent(new KeyboardEvent('keydown',
      { key: 'Enter', altKey: ${alt ? 'true' : 'false'}, bubbles: true }));
    return true;
  })()`);

  const users = () => evalJs(`Array.prototype.map.call(
    document.querySelectorAll('#messages .msg.user'), function (n) { return n.textContent; })`);

  // ---- 1) the chip is there and says the default -------------------------
  let c = await chip();
  check('the composer has an insert-timing chip', !!c && c.text.indexOf('等这轮结束') >= 0, JSON.stringify(c));
  check('the chip is quiet while nothing is running', !!c && c.idle === true, JSON.stringify(c));

  // ---- 2) the chip toggles and remembers ---------------------------------
  await evalJs(`document.getElementById('insert-chip').click(); true`);
  c = await chip();
  check('the chip switches to the immediate insertion',
    !!c && c.text.indexOf('立即插入') >= 0 && c.on === true, JSON.stringify(c));
  await send('Page.reload', {});
  await sleep(1500);
  c = await chip();
  check('the choice survives a reload', !!c && c.text.indexOf('立即插入') >= 0, JSON.stringify(c));
  await evalJs(`document.getElementById('insert-chip').click(); true`);
  c = await chip();
  check('the chip switches back', !!c && c.text.indexOf('等这轮结束') >= 0, JSON.stringify(c));

  // ---- 3) Enter queues with the chip's timing ----------------------------
  await evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = 'first';
    ta.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);
  await sleep(700);
  check('the turn is streaming',
    (await evalJs(`document.getElementById('send').className.indexOf('stop') >= 0`)) === true);
  c = await chip();
  check('the chip wakes up while the turn runs', !!c && c.idle === false, JSON.stringify(c));

  await type('等这一轮结束', false);
  await sleep(500);
  let s = await strip();
  check('the queued message shows up in the strip', !!s && s.hidden === false, JSON.stringify(s));
  check('the strip says it waits for the turn to end',
    !!s && s.items.length === 1 && s.items[0].now === false, JSON.stringify(s && s.items));

  // ---- 4) Alt+Enter queues for an immediate insertion --------------------
  await type('马上插进来', true);
  await sleep(500);
  s = await strip();
  check('Alt+Enter queues the message as an immediate insertion',
    !!s && s.items.length === 2 && s.items[1].now === true, JSON.stringify(s && s.items));

  // ---- 5) the waiting message takes effect in the next turn ---------------
  const grew = await waitFor(async () => {
    const u = await users();
    return u.length >= 3 ? u : null;
  }, 40000);
  check('the waiting message became a turn of its own', grew, JSON.stringify(await users()));
  const u = await users();
  check('the transcript holds both queued messages',
    !!u && u.some((t) => t.indexOf('等这一轮结束') >= 0) && u.some((t) => t.indexOf('马上插进来') >= 0),
    JSON.stringify(u));
  s = await strip();
  check('the queue strip is empty again', !!s && s.hidden === true, JSON.stringify(s));
  check('the model saw the waiting message',
    chatBodies.some((b) => b.indexOf('等这一轮结束') >= 0), String(chatBodies.length));
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
