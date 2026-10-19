// Browser test: a second machine picks up the turn the first one started.
//
// The turn lives on the server, keyed by session, so "resume" must not depend on
// the browser that started it. This opens a *fresh* profile - no localStorage, no
// history, nothing in common with the client that sent the message - against a
// server that has two workspaces, with the running turn in the second one. A cold
// boot used to land on the first workspace in the index and show an empty chat;
// now it asks GET /api/chat/turns, switches to the workspace that has work in
// progress, opens that session and re-attaches.
//
//   node tests/ui_second_client.mjs [path-to-tea_agent-binary]
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
const CHUNKS = 40;                 // ~12s per answer

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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_pcB_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
for (const d of ['proj_a', 'proj_b']) fs.mkdirSync(path.join(tmp, d), { recursive: true });

const frame = (delta, finish) => 'data: ' + JSON.stringify({
  id: 'c1', object: 'chat.completion.chunk',
  choices: [{ index: 0, delta, finish_reason: finish || null }],
}) + '\n\n';
const mock = http.createServer((req, res) => {
  let body = '';
  req.on('data', (d) => (body += d));
  req.on('end', () => {
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
roles: {main: {provider: mock, model: m}}
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
const view = () => evalJs(`(function(){
  var a = document.querySelectorAll('#messages .msg.assistant');
  var last = a.length ? a[a.length - 1] : null;
  return { workspace: document.getElementById('ws-chip-text').textContent,
           title: document.title,
           streaming: document.getElementById('send').className.indexOf('stop') >= 0,
           users: document.querySelectorAll('#messages .msg.user').length,
           assistants: a.length,
           textLen: last ? last.textContent.length : 0,
           empty: !!document.querySelector('#messages .chat-empty') };
})()`);

try {
  const agentPort = await freePort();
  // proj_a is registered first: a cold boot that ignores running turns lands there.
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj_a'), '--workspace', path.join(tmp, 'proj_b')],
    { stdio: 'ignore' });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }
  const list = JSON.parse((await getJson(agentPort, '/api/workspaces')).body).workspaces;
  check('two workspaces, proj_a first', list.length === 2 && list[0].name === 'proj_a',
    list.map((w) => w.name).join(','));
  const wsB = list[1].id;

  // ---- PC A: a plain HTTP client starts a turn, then goes away -----------
  let sid = null;
  let frames = 0;
  const a = http.request({ host: '127.0.0.1', port: agentPort, path: '/api/chat', method: 'POST',
    headers: { 'Content-Type': 'application/json' } }, (res) => {
    res.on('data', (d) => {
      for (const line of d.toString().split('\n')) {
        if (line.indexOf('data:') !== 0) continue;
        frames++;
        try { const ev = JSON.parse(line.slice(5).trim()); if (ev.session_id) sid = ev.session_id; } catch {}
      }
    });
  });
  a.end(JSON.stringify({ workspace_id: wsB, message: 'long task from PC A' }));
  await sleep(2500);
  a.destroy();
  check('PC A started a turn in proj_b and left', !!sid && frames > 1, 'frames=' + frames + ' session=' + sid);

  // ---- the server can say what is running, and where --------------------
  const turns = JSON.parse((await getJson(agentPort, '/api/chat/turns')).body);
  const mine = (turns.turns || []).filter((t) => t.session_id === sid)[0];
  check('GET /api/chat/turns lists the running turn', !!mine && mine.running === true,
    JSON.stringify(turns));
  check('it names the workspace the turn belongs to', mine && mine.workspace_id === wsB,
    mine && mine.workspace_id);

  // ---- PC B: a brand new browser profile, nothing in common with A -------
  const cdpPort = await freePort();
  chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
    `--remote-debugging-port=${cdpPort}`, `--user-data-dir=${path.join(tmp, 'pcB')}`,
    'about:blank'], { stdio: 'ignore' });
  let target = null;
  for (let i = 0; i < 100; i++) {
    try {
      const l = JSON.parse((await getJson(cdpPort, '/json/list')).body);
      target = l.find((t) => t.type === 'page');
      if (target) break;
    } catch {}
    await sleep(150);
  }
  if (!target) throw new Error('no chrome page target');
  ws = wsConnect(target.webSocketDebuggerUrl);
  await new Promise((r) => ws.on('open', r));
  ws.on('message', (raw) => {
    const m = JSON.parse(raw);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(3000);

  const boot = await view();
  check('PC B lands on the workspace that has work in progress', boot.workspace === 'proj_b',
    JSON.stringify(boot));
  check('PC B is streaming the turn it did not start', boot.streaming === true, JSON.stringify(boot));
  check('PC B sees the question PC A asked', boot.users === 1, JSON.stringify(boot));
  check('PC B replayed what had been generated so far', boot.textLen > 0, 'len=' + boot.textLen);
  check('the tab title follows the workspace', boot.title === 'proj_b · TEA AGENT', boot.title);

  await sleep(3000);
  const later = await view();
  check('PC B keeps following live', later.textLen > boot.textLen,
    boot.textLen + ' -> ' + later.textLen);

  // ---- and it runs to the end on PC B -----------------------------------
  let fin = later;
  for (let i = 0; i < 60; i++) {
    fin = await view();
    if (!fin.streaming) break;
    await sleep(500);
  }
  check('the turn completes while PC B watches', fin.streaming === false, JSON.stringify(fin));
  check('the full answer is rendered on PC B', fin.textLen >= CHUNKS, 'len=' + fin.textLen);
  check('one question, one answer', fin.users === 1 && fin.assistants === 1, JSON.stringify(fin));

  const after = JSON.parse((await getJson(agentPort, '/api/chat/turns')).body);
  check('nothing is reported as running afterwards', (after.turns || []).length === 0,
    JSON.stringify(after.turns));
  const sess = JSON.parse((await getJson(agentPort, '/api/sessions/' + sid)).body);
  check('the session belongs to proj_b and holds the answer',
    sess.workspace_id === wsB && sess.messages.some((m) => m.role === 'assistant' && m.content.length === CHUNKS),
    sess.messages.map((m) => m.role + ':' + (m.content || '').length).join(','));
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
