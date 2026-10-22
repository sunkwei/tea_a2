// Browser test: a turn keeps running after the page goes away, and the reopened
// page picks it up instead of showing a conversation that stopped mid-sentence.
//
// The flow is the one people actually hit: send a message, close the tab (here:
// navigate to about:blank, which tears the SSE connection down the same way),
// come back a few seconds later. The server never cancelled the turn, so the
// reopened page asks GET /api/chat/turn, re-attaches with POST /api/chat/attach
// and replays the whole turn - the live bubble has to come back with everything
// that was generated while nobody was watching, without duplicating the messages
// that were already persisted, and then follow the rest live to the end.
//
// Two turns are exercised, because they differ in what is already on disk when
// the page comes back:
//
//   A. a single-iteration answer - nothing but the question is persisted;
//   B. a tool-calling turn - the first iteration (assistant + tool result) is
//      already saved, so the replay has to rebuild it in the live bubble without
//      showing it twice. `start.messages_before` is what makes that possible.
//
//   node tests/ui_stream_resume.mjs [path-to-tea_agent-binary]
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
const CHUNKS = 40;                 // ~12s of streaming per answer

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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_resume_ui_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });

// Request 1: a slow answer. Request 2: one tool call. Request 3+: the slow
// answer again (the second half of the tool turn).
let requests = 0;
let upstreamFinished = 0;
const frame = (delta, finish) => 'data: ' + JSON.stringify({
  id: 'c1', object: 'chat.completion.chunk',
  choices: [{ index: 0, delta: delta, finish_reason: finish || null }],
}) + '\n\n';

const mock = http.createServer((req, res) => {
  let body = '';
  req.on('data', (d) => (body += d));
  req.on('end', () => {
    requests++;
    const which = requests;
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    if (which === 2) {
      res.write(frame({
        role: 'assistant',
        tool_calls: [{ index: 0, id: 'call_1', type: 'function',
          function: { name: 'toolkit_exec', arguments: JSON.stringify({ command: 'echo resume-probe' }) } }],
      }));
      res.write(frame({}, 'tool_calls'));
      res.write('data: [DONE]\n\n');
      res.end();
      return;
    }
    let n = 0;
    const timer = setInterval(() => {
      n++;
      res.write(frame({ content: '字' }));
      if (n >= CHUNKS) {
        clearInterval(timer);
        res.write(frame({}, 'stop'));
        res.write('data: [DONE]\n\n');
        res.end();
        upstreamFinished++;
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

/// What the chat column looks like right now.
const view = () => evalJs(`(function(){
  var asss = document.querySelectorAll('#messages .msg.assistant');
  var last = asss.length ? asss[asss.length - 1] : null;
  return { streaming: document.getElementById('send').className.indexOf('stop') >= 0,
           users: document.querySelectorAll('#messages .msg.user').length,
           assistants: asss.length,
           toolRows: document.querySelectorAll('#messages .tool-row:not(.traj-row):not(.reasoning)').length,
           liveToolRows: last ? last.querySelectorAll('.tool-row').length : 0,
           // Rows that belong to the one command this turn runs: the question is
           // "is it rendered once", not "how many commands exist in the whole
           // session" (earlier turns have their own).
           probeRows: Array.prototype.filter.call(
             document.querySelectorAll('#messages .msg > .tool-row'),
             function (r) { return r.textContent.indexOf('resume-probe') >= 0; }).length,
           live: !!document.querySelector('#messages .msg.assistant.streaming'),
           text: last ? last.textContent : '' };
})()`);

const say = (text) => evalJs(`(function(){
  var ta = document.getElementById('input');
  ta.value = ${JSON.stringify(text)};
  ta.dispatchEvent(new Event('input'));
  document.getElementById('send').click();
  return true;
})()`);

try {
  const agentPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj')], { stdio: 'ignore' });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }
  const wsId = JSON.parse((await getJson(agentPort, '/api/workspaces')).body).workspaces[0].id;
  const turnStatus = async (sid) =>
    JSON.parse((await getJson(agentPort, '/api/chat/turn?session_id=' + encodeURIComponent(sid))).body);

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
  const url = `http://127.0.0.1:${agentPort}/`;
  await send('Page.navigate', { url });
  await sleep(1800);

  // ================= A) a plain answer, interrupted ======================
  await say('answer slowly');
  await sleep(2500);
  const beforeA = await view();
  check('A: the turn is streaming', beforeA.streaming === true && beforeA.live === true,
    JSON.stringify(beforeA));
  check('A: text is arriving', beforeA.text.length > 0, 'len=' + beforeA.text.length);

  const sid = JSON.parse((await getJson(agentPort,
    '/api/workspaces/' + encodeURIComponent(wsId) + '/sessions')).body).sessions[0].id;

  await send('Page.navigate', { url: 'about:blank' });       // <- the tab is closed
  await sleep(2500);
  let st = await turnStatus(sid);
  check('A: the turn is still running with no page open', st.running === true, JSON.stringify(st));
  check('A: it kept producing events while nobody watched', st.last_seq > beforeA.text.length,
    'last_seq=' + st.last_seq);

  await send('Page.navigate', { url });                      // <- and opened again
  await sleep(2500);
  const resumedA = await view();
  check('A: the reopened page is streaming again', resumedA.streaming === true, JSON.stringify(resumedA));
  check('A: the replay caught up with what was generated meanwhile',
    resumedA.text.length > beforeA.text.length,
    'before=' + beforeA.text.length + ' resumed=' + resumedA.text.length);
  check('A: the replayed turn is not duplicated',
    resumedA.users === 1 && resumedA.assistants === 1, JSON.stringify(resumedA));

  let fin = resumedA;
  for (let i = 0; i < 60; i++) {
    fin = await view();
    if (!fin.streaming) break;
    await sleep(500);
  }
  check('A: the turn completes after the re-attach', fin.streaming === false && upstreamFinished === 1,
    JSON.stringify(fin));
  check('A: the final answer is rendered in full', fin.text.length >= CHUNKS, 'len=' + fin.text.length);
  check('A: still one question and one answer', fin.users === 1 && fin.assistants === 1,
    JSON.stringify(fin));
  st = await turnStatus(sid);
  check('A: the server reports the turn as finished', st.running === false, JSON.stringify(st));

  // ============ B) a tool turn, interrupted mid second iteration =========
  await say('now call a tool');
  // Wait until the tool has run and the slow answer of iteration 2 is under way:
  // that is the moment when the first iteration is already on disk.
  let beforeB = null;
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    beforeB = await view();
    if (beforeB.streaming && beforeB.liveToolRows >= 1 && beforeB.text.length > 3) break;
  }
  check('B: the tool ran and the answer started streaming',
    beforeB && beforeB.liveToolRows === 1 && beforeB.text.length > 3, JSON.stringify(beforeB));
  const persistedMid = JSON.parse((await getJson(agentPort, '/api/sessions/' + encodeURIComponent(sid))).body);
  check('B: the first iteration is already persisted', persistedMid.messages.length >= 4,
    persistedMid.messages.map((m) => m.role).join(','));

  await send('Page.navigate', { url: 'about:blank' });
  await sleep(2500);
  st = await turnStatus(sid);
  check('B: the tool turn also survives the disconnect', st.running === true, JSON.stringify(st));

  await send('Page.navigate', { url });
  await sleep(2500);
  const resumedB = await view();
  check('B: streaming again after the reload', resumedB.streaming === true, JSON.stringify(resumedB));
  check('B: the rebuilt turn shows the tool call exactly once',
    resumedB.liveToolRows === 1 && resumedB.probeRows === 1, JSON.stringify(resumedB));
  check('B: no duplicated question bubbles', resumedB.users === 2,
    'users=' + resumedB.users + ' assistants=' + resumedB.assistants);
  check('B: the answer kept growing across the reload', resumedB.text.length > beforeB.text.length,
    'before=' + beforeB.text.length + ' resumed=' + resumedB.text.length);

  fin = resumedB;
  for (let i = 0; i < 60; i++) {
    fin = await view();
    if (!fin.streaming) break;
    await sleep(500);
  }
  check('B: the tool turn completes after the re-attach', fin.streaming === false && upstreamFinished === 2,
    JSON.stringify(fin));
  check('B: one tool row and the full answer remain',
    fin.probeRows === 1 && fin.text.length >= CHUNKS, JSON.stringify(fin));
  check('B: the conversation has two questions and three answers',
    fin.users === 2 && fin.assistants === 3, JSON.stringify(fin));
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
