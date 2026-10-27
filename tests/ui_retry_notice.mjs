// Browser test: the conversation says that the provider is busy and that the
// agent is waiting for it.
//
// Regression: a provider that answers HTTP 529 ("当前为整点高峰时段，服务器短暂
// 繁忙，通常 1-5 分钟内恢复") used to end the turn at once. The user saw
// "出错了：HTTP 529: …" and had to send the message again by hand - even
// though the provider itself says the same request succeeds a minute later.
//
// What is pinned here:
//   * while the turn waits, the live bubble says the server is busy and how
//     long the wait still is (a countdown);
//   * the turn then finishes with the normal answer - the retry happened by
//     itself.
//
//   node tests/ui_retry_notice.mjs [path-to-tea_agent-binary]
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

// The provider's own words, verbatim.
const OVERLOAD_BODY = {
  type: 'error',
  error: {
    type: 'overloaded_error',
    message: '当前为整点高峰时段，服务器短暂繁忙，通常 1-5 分钟内恢复。请稍后重试 (2064)',
    http_code: '529',
  },
  request_id: '072f39a987269aa44e442b8926a15cdd',
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_retry_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });
// The wait is a minute by default; two seconds is long enough to see the
// countdown in a browser and short enough for a test.
fs.mkdirSync(path.join(tmp, 'proj', '.tea_agent_run'), { recursive: true });
fs.writeFileSync(path.join(tmp, 'proj', '.tea_agent_run', 'settings.yaml'),
  'settings:\n  overload_retries: 3\n  overload_backoff_ms: 2000\n  overload_max_backoff_ms: 4000\n');

let agent, chrome, ws, mock;
try {
  let busy = 0;
  mock = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      if (!req.url.endsWith('/chat/completions')) { res.writeHead(404).end(); return; }
      busy += 1;
      if (busy === 1) {
        // The busy answer: HTTP 529 with the provider's explanation.
        const out = JSON.stringify(OVERLOAD_BODY);
        res.writeHead(529, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(out) });
        res.end(out);
        return;
      }
      const chunk = (delta, finish = null) => res.write('data: ' + JSON.stringify({
        id: 'c', object: 'chat.completion.chunk',
        choices: [{ index: 0, delta, finish_reason: finish }],
      }) + '\n\n');
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      chunk({ content: '等到了，' });
      setTimeout(() => {
        chunk({ content: '回答正常。' });
        chunk({}, 'stop');
        res.write('data: [DONE]\n\n');
        res.end();
      }, 400);
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
  const notices = () => evalJs(`Array.prototype.map.call(
    document.querySelectorAll('.msg-notice'), function (n) { return n.textContent; }).join(' | ')`);
  const said = () => evalJs(`(function(){
    var m = (window.teaState && window.teaState.messages) || [];
    var out = [];
    for (var i = 0; i < m.length; i++) out.push(m[i].content || '');
    return out.join('\\n');
  })()`);
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

  await sendMessage('来一句');

  // The waiting is announced: the server is busy and how long the wait is.
  const waiting = await waitFor(async () => (await notices()).indexOf('服务端繁忙') >= 0, 30000, 150);
  check('the conversation says that the server is busy', waiting, await notices());
  const counted = await waitFor(async () => /还剩\s*\d+\s*秒/.test(await notices()), 10000, 200);
  check('the notice counts the wait down', counted, await notices());

  // The retry happens by itself and the turn ends with the normal answer.
  const done = await waitFor(async () =>
    (await evalJs(`!!(window.teaState && !window.teaState.streaming)`)) === true, 60000, 250);
  check('the turn finished after the retry', done);
  const text = await said();
  check('the answer is the normal one', String(text).indexOf('回答正常') >= 0, String(text).slice(0, 200));
  const noError = await evalJs(`(window.teaState && window.teaState.streamError) || ''`);
  check('no error was shown', !noError, noError);
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
