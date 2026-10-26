// Browser test: the conversation says when the context was compacted.
//
// Regression: the compaction used to happen silently. A long turn reached 87%
// of the window, the provider answered 400 ("context window exceeds limit")
// and the user only saw the error - nothing in the conversation said that the
// oldest turns had been folded into a summary.
//
// What is pinned here:
//   * while the turn runs, a compaction leaves a marker in the live bubble
//     ("上下文已压缩：N 条旧消息已折叠为摘要");
//   * after the turn, the session reports how much was folded and the chat
//     says so above the messages;
//   * a session that was never compacted has no marker.
//
//   node tests/ui_compaction.mjs [path-to-tea_agent-binary]
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
const postJson = (port, p, body) => new Promise((res, rej) => {
  const data = JSON.stringify(body || {});
  const req = http.request({ host: '127.0.0.1', port, path: p, method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) } },
  (r) => {
    let b = ''; r.on('data', (d) => (b += d)); r.on('end', () => {
      try { res({ status: r.statusCode, body: JSON.parse(b || 'null') }); }
      catch (e) { res({ status: r.statusCode, body: b }); }
    });
  });
  req.on('error', rej);
  req.end(data);
});

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || !detail ? '' : '  ' + detail));
};
const waitFor = async (fn, timeout = 30000, step = 200) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await fn()) return true;
    await sleep(step);
  }
  return false;
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_compaction_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });
// compaction_keep_recent: 2, so a two-turn conversation is enough to fold the
// first turn (the default 4 would need four).
fs.mkdirSync(path.join(tmp, 'proj', '.tea_agent_run'), { recursive: true });
fs.writeFileSync(path.join(tmp, 'proj', '.tea_agent_run', 'settings.yaml'),
  'settings:\n  compaction_keep_recent: 2\n');

let agent, chrome, ws, mock;
try {
  // A tiny window, so two turns cross the pressure trigger. The mock answers
  // the summarisation request with text, the way a real provider does.
  mock = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      if (!req.url.endsWith('/chat/completions')) { res.writeHead(404).end(); return; }
      let parsed = {};
      try { parsed = JSON.parse(body || '{}'); } catch (e) { /* ignore */ }
      const messages = parsed.messages || [];
      const isSummary = messages.some((m) => m.role === 'system' &&
        typeof m.content === 'string' && m.content.indexOf('compress conversation history') >= 0);
      if (isSummary) {
        const out = JSON.stringify({
          id: 's', object: 'chat.completion', model: parsed.model,
          choices: [{ index: 0, message: { role: 'assistant', content: 'COMPACTED SUMMARY' },
            finish_reason: 'stop' }],
        });
        res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(out) });
        res.end(out);
        return;
      }
      // A slow answer: the compaction happens at the start of the turn, and the
      // marker lives in the live bubble until the turn ends - so the answer has
      // to take a moment, otherwise the marker is gone before anybody looks.
      setTimeout(() => {
        res.writeHead(200, { 'Content-Type': 'text/event-stream' });
        const chunk = (delta, finish = null) => res.write('data: ' + JSON.stringify({
          id: 'c', object: 'chat.completion.chunk',
          choices: [{ index: 0, delta, finish_reason: finish }],
        }) + '\n\n');
        chunk({ content: '好的。' });
        chunk({}, 'stop');
        res.write('data: [DONE]\n\n');
        res.end();
      }, 1500);
    });
  });
  await new Promise((r) => mock.listen(0, '127.0.0.1', r));

  fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:${mock.address().port}
    api_key: sk-test
    default_model: m
    models:
      m:
        max_context_tokens: 1500
        max_output_tokens: 100
`);

  const agentPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj')], { stdio: 'ignore' });
  for (let i = 0; i < 200; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  // Warm the agent up over the API before the browser starts: the first turn
  // pays for the environment probe, and a cold Chrome start starves the box
  // badly enough to turn that probe into a minute-long wait.
  let wid = '';
  const gotWs = await waitFor(async () => {
    try {
      const raw = (await getJson(agentPort, '/api/workspaces')).body;
      const body = JSON.parse(raw || 'null');
      if (body && body.workspaces && body.workspaces.length) { wid = body.workspaces[0].id; return true; }
    } catch (e) { /* not up yet */ }
    return false;
  }, 20000, 250);
  if (!gotWs) throw new Error('the agent never listed its workspaces');
  const warm = await postJson(agentPort, `/api/workspaces/${wid}/sessions`, { title: 'warmup' });
  const warmId = (warm.body && warm.body.id) || '';
  if (warmId) {
    await postJson(agentPort, '/api/chat', {
      workspace_id: wid, session_id: warmId, provider: 'mock', message: 'warmup',
    });
  }

  chrome = await acquireBrowser(tmp, 'chrome', { windowSize: '1280,900' });
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
  const notices = () => evalJs(`Array.prototype.map.call(
    document.querySelectorAll('.msg-notice'), function (n) { return n.textContent; }).join(' | ')`);
  const sendMessage = async (text) => {
    await evalJs(`(function(){
      var ta = document.getElementById('input');
      ta.value = ${JSON.stringify(text)};
      ta.dispatchEvent(new Event('input'));
      document.getElementById('send').click();
      return true;
    })()`);
  };

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  const booted = await waitFor(async () => {
    try {
      return (await evalJs(`!!document.getElementById('input') &&
        !!(window.teaState && window.teaState.workspace)`)) === true;
    } catch (e) { return false; }
  }, 20000, 250);
  check('the page came up', booted);

  // A session that was never compacted: no marker.
  check('a fresh session has no compaction marker', (await notices()) === '', await notices());

  // Two long turns: the second one crosses the pressure trigger.
  const pad = 'x'.repeat(400);
  await sendMessage('第一轮 ' + pad);
  // Wait for the turn to be *over*: a message sent while a turn runs is
  // queued, and a queued message never makes a turn of its own.
  const firstDone = await waitFor(async () =>
    (await evalJs(`!!(window.teaState && !window.teaState.streaming)`)) === true, 30000, 250);
  check('the first turn finished', firstDone);
  await sendMessage('第二轮 ' + pad);

  // The marker shows up while the turn runs ...
  const live = await waitFor(async () => (await notices()).indexOf('上下文已压缩') >= 0, 30000, 150);
  check('the compaction leaves a marker in the conversation', live, await notices());

  // ... and the session reports what was folded.
  const sid = await evalJs(`(window.teaState && window.teaState.session && window.teaState.session.id) || ''`);
  const session = JSON.parse((await getJson(agentPort, '/api/sessions/' + sid)).body || 'null');
  check('the session reports the folded messages',
    (session.summary_upto || 0) > 0 && (session.summary_bytes || 0) > 0,
    `upto=${session.summary_upto} bytes=${session.summary_bytes}`);

  // After the turn the chat says so above the messages.
  const durable = await waitFor(async () => (await notices()).indexOf('此会话的前') >= 0, 30000, 250);
  check('after the turn the chat says how much was folded', durable, await notices());
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  if (agent) agent.kill('SIGTERM');
  if (mock) mock.close();
  await sleep(300);
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
