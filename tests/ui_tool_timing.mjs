// Browser test: a slow tool says when it started and how long it may take.
//
// A build that runs for three minutes looks exactly like a hung agent in the
// UI: one row, a spinner, no numbers. The user cannot tell "still working"
// from "stuck", and cannot tell whether waiting another minute is worth it.
//
// What is pinned here:
//   * while the tool runs, the row shows the elapsed time ticking on its own
//     (the server only speaks at the start and at the end);
//   * the row says what the deadline is - the same number the tool enforces;
//   * the panel carries all three facts: when it started, how long, and the
//     deadline;
//   * after the call finishes, the numbers survive in the transcript.
//
//   node tests/ui_tool_timing.mjs [path-to-tea_agent-binary]
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
const waitFor = async (fn, timeout = 30000, step = 200) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await fn()) return true;
    await sleep(step);
  }
  return false;
};

// A command that takes a while, with an explicit deadline of 30 s.
const SLEEP_CMD = process.platform === 'win32' ? 'ping -n 8 127.0.0.1 > nul' : 'sleep 6';
const TIMEOUT_MS = 30000;

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_tool_timing_'));
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
        chunk({ content: '命令跑完了。' });
        chunk({}, 'stop');
      } else {
        chunk({ tool_calls: [{ id: 'call_1', type: 'function',
          function: { name: 'toolkit_exec', arguments: JSON.stringify({
            command: SLEEP_CMD, timeout_ms: TIMEOUT_MS }) } }] });
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
      const body = JSON.parse((await getJson(agentPort, '/api/workspaces')).body || 'null');
      if (body && body.workspaces && body.workspaces.length) { wid = body.workspaces[0].id; return true; }
    } catch (e) { /* not up yet */ }
    return false;
  }, 20000, 250);
  if (!gotWs) throw new Error('the agent never listed its workspaces');

  chrome = await acquireBrowser(tmp, 'chrome', { windowSize: '1280,900' });
  ws = wsConnect(chrome.webSocketDebuggerUrl);
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
  const booted = await waitFor(async () => {
    try {
      return (await evalJs(`!!document.getElementById('input') &&
        !!(window.teaState && window.teaState.workspace)`)) === true;
    } catch (e) { return false; }
  }, 20000, 250);
  check('the page came up', booted);

  // The tool row: its line and, once open, its panel.
  const rowText = () => evalJs(`(function(){
    var r = document.querySelector('#messages .tool-row:not(.reasoning):not(.traj-row)');
    return r ? r.textContent : '';
  })()`);
  const panelText = () => evalJs(`(function(){
    var r = document.querySelector('#messages .tool-row.open:not(.reasoning):not(.traj-row) .tool-panel');
    return r ? r.textContent : '';
  })()`);

  await evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = '跑一个很久的命令';
    ta.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);

  // The row appears while the command is still running.
  const running = await waitFor(async () => (await rowText()).indexOf('运行中') >= 0, 30000, 150);
  check('the tool row is there', running, await rowText());

  // The elapsed time ticks on its own: the server said nothing in between,
  // so the browser has to count.
  const first = await evalJs(`(function(){
    var e = document.querySelector('#messages .tool-elapsed[data-started-at]');
    return e ? e.textContent : '';
  })()`);
  check('the running row shows the elapsed time', /已用|\d/.test(first || ''), first);
  await sleep(1600);
  const second = await evalJs(`(function(){
    var e = document.querySelector('#messages .tool-elapsed[data-started-at]');
    return e ? e.textContent : '';
  })()`);
  check('the elapsed time ticks while the tool runs',
    !!first && !!second && first !== second, first + ' -> ' + second);

  const limit = await evalJs(`(function(){
    var e = document.querySelector('#messages .tool-limit');
    return e ? e.textContent : '';
  })()`);
  check('the row says what the deadline is', limit.indexOf('上限') >= 0, limit);
  check('the deadline is the one the command was given (30 s)',
    limit.indexOf('30') >= 0, limit);

  // The panel carries all three facts.
  const panel = await panelText();
  check('the panel says when the call started', panel.indexOf('启动') >= 0, panel.slice(0, 200));
  check('the panel says how long it has been running', panel.indexOf('已用') >= 0, panel.slice(0, 200));
  check('the panel says the deadline', panel.indexOf('超时') >= 0, panel.slice(0, 200));

  // When the call ends, the numbers stay in the transcript.
  const done = await waitFor(async () => {
    const t = await rowText();
    return t.indexOf('运行中') < 0 && t.length > 0;
  }, 30000, 200);
  check('the tool finished', done, await rowText());
  const after = await rowText();
  check('the finished row still shows the elapsed time', /\d/.test(after), after);

  // A reload must not lose the numbers: they live in the session.
  await send('Page.reload');
  await sleep(2500);
  const reloaded = await waitFor(async () => {
    const t = await rowText();
    return t.length > 0;
  }, 20000, 200);
  check('the tool row survives a reload', reloaded, await rowText());
  const afterReload = await evalJs(`(function(){
    var rows = document.querySelectorAll('#messages .tool-row:not(.reasoning):not(.traj-row)');
    var out = [];
    for (var i = 0; i < rows.length; i++) out.push(rows[i].textContent);
    return out.join(' | ');
  })()`);
  check('the reloaded transcript still shows the elapsed time',
    /\d/.test(afterReload || ''), afterReload);
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
