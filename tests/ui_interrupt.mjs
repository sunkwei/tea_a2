// Browser test: an interrupted tool says so.
//
// Regression: the stop button set the cancel flag, but a running shell command
// was not interruptible at all - the turn kept running until the command
// finished, and the tool row ended up looking like a plain failure.
//
// What is pinned here:
//   * clicking stop while a tool is running ends the turn (the command is
//     killed, the turn does not wait for it);
//   * the tool row says it was interrupted, not that it failed.
//
//   node tests/ui_interrupt.mjs [path-to-tea_agent-binary]
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

// A command that takes a minute. `ping` is the portable one (Windows has no
// `sleep`).
const SLEEP_CMD = process.platform === 'win32' ? 'ping -n 61 127.0.0.1 > nul' : 'sleep 60';

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_interrupt_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });

let agent, chrome, ws, mock;
try {
  mock = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      if (!req.url.endsWith('/chat/completions')) { res.writeHead(404).end(); return; }
      let parsed = {};
      try { parsed = JSON.parse(body || '{}'); } catch (e) { /* ignore */ }
      const messages = parsed.messages || [];
      const alreadyRan = messages.some((m) => m.role === 'assistant' && m.tool_calls);
      const chunk = (delta, finish = null) => res.write('data: ' + JSON.stringify({
        id: 'c', object: 'chat.completion.chunk',
        choices: [{ index: 0, delta, finish_reason: finish }],
      }) + '\n\n');
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      if (alreadyRan) {
        chunk({ content: '已经恢复。' });
        chunk({}, 'stop');
      } else {
        chunk({ tool_calls: [{ id: 'call_1', type: 'function',
          function: { name: 'toolkit_exec', arguments: JSON.stringify({ command: SLEEP_CMD }) } }] });
        chunk({}, 'tool_calls');
      }
      res.write('data: [DONE]\n\n');
      res.end();
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
        max_context_tokens: 32768
        max_output_tokens: 100
        supports_tools: true
`);

  const agentPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj')], { stdio: 'ignore' });
  for (let i = 0; i < 200; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

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
  const toolRows = () => evalJs(`Array.prototype.map.call(
    document.querySelectorAll('.tool-row'), function (n) { return n.textContent; }).join(' | ')`);

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

  await evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = '跑一个很久的命令';
    ta.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);

  // The tool is running ...
  const running = await waitFor(async () => (await toolRows()).indexOf('运行中') >= 0, 30000, 150);
  check('the tool row is there', running, await toolRows());
  await sleep(800);

  // ... and the stop button ends the turn.
  const stoppedAt = Date.now();
  await evalJs(`(function(){ document.getElementById('send').click(); return true; })()`);
  const ended = await waitFor(async () =>
    (await evalJs(`!!(window.teaState && !window.teaState.streaming)`)) === true, 30000, 200);
  check('the turn ended after the stop', ended);
  const elapsed = (Date.now() - stoppedAt) / 1000;
  check('the turn did not wait for the command', elapsed < 20, elapsed.toFixed(1) + 's');

  const rows = await toolRows();
  check('the tool row says it was interrupted', rows.indexOf('已中断') >= 0, rows);
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
