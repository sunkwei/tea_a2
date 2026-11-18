// Browser test: the conversation says that the last turn was cut off.
//
// The scenario: the agent is working on a turn, the machine goes away (the
// process is killed), and the agent comes back. The turn is gone from memory,
// so the conversation used to look like a question that was never answered -
// with nothing that said "that turn existed, and it was cut off".
//
// What is pinned here:
//   * after the restart the conversation says the last turn was interrupted,
//     and says how far it got;
//   * the request that was never answered can be put back into the composer
//     with one click (re-running it is the user's decision, not the program's);
//   * a session whose last turn ended normally shows no such notice.
//
//   node tests/ui_recovery_notice.mjs [path-to-tea_agent-binary]
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
    try { if (await fn()) return true; } catch (e) { /* keep waiting */ }
    await sleep(step);
  }
  return false;
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_recovery_ui_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });

const providerPort = await freePort();
const agentPort = await freePort();
// The provider never answers: the turn is still working when the machine goes
// away.
const mock = http.createServer((req, res) => {
  let body = '';
  req.on('data', (d) => (body += d));
  req.on('end', () => {
    if (!req.url.endsWith('/chat/completions')) { res.writeHead(404).end(); return; }
    setTimeout(() => { try { res.end(); } catch (e) { /* gone */ } }, 300000);
  });
});
await new Promise((r) => mock.listen(providerPort, '127.0.0.1', r));

fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:${providerPort}
    api_key: sk-test
    default_model: m
    models:
      m:
        max_context_tokens: 32768
        max_output_tokens: 100
`);

const startAgent = () => spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'),
  '--tea-home', path.join(tmp, 'home'), '--port', String(agentPort), '--no-console',
  '--log-file', path.join(tmp, 'agent.log'), '--workspace', path.join(tmp, 'proj')],
  { stdio: 'ignore' });

let agent, chrome, ws;
try {
  agent = startAgent();
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

  // A turn that starts and never finishes.
  const submitted = await postJson(agentPort, '/api/chat', {
    message: '把这一轮做完', workspace_id: wid, provider: 'mock', model: 'm', async: true,
  });
  check('the turn was accepted', submitted.status === 202, JSON.stringify(submitted.body));
  const sessionId = (submitted.body || {}).session_id || '';

  // Wait for the trajectory to be on the disk: that is the record that says
  // "the turn started".
  const trajPath = path.join(tmp, 'proj', '.tea_agent_run', 'trajectories', sessionId + '.jsonl');
  const onDisk = await waitFor(() => fs.existsSync(trajPath), 15000, 150);
  check('the trajectory of the turn is on the disk', onDisk, trajPath);

  // The machine goes away.
  agent.kill('SIGKILL');
  await sleep(800);

  // ... and comes back.
  agent = startAgent();
  for (let i = 0; i < 200; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  chrome = await acquireBrowser(tmp, 'chrome', { windowSize: '1280,900' });
  if (!chrome) throw new Error('no chrome page target');
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
  const notices = () => evalJs(`Array.prototype.map.call(
    document.querySelectorAll('.msg-notice.recovery-notice'),
    function (n) { return n.textContent; }).join(' | ')`);

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

  const interrupted = await waitFor(async () =>
    (await notices()).indexOf('上一轮被中断了') >= 0, 25000, 250);
  check('the conversation says the last turn was interrupted', interrupted, await notices());

  const again = await waitFor(async () =>
    (await evalJs(`!!document.querySelector('.recovery-notice .recovery-again')`)) === true,
    10000, 200);
  check('there is a way to ask again', again);

  // The click puts the unanswered request back into the composer - it does
  // not send it: re-running the work is the user's call.
  const composer = await evalJs(`(function(){
    var b = document.querySelector('.recovery-notice .recovery-again');
    if (!b) return '';
    b.click();
    var ta = document.getElementById('input');
    return ta ? ta.value : '';
  })()`);
  check('the request that was never answered is back in the composer',
    String(composer).indexOf('把这一轮做完') >= 0, String(composer));
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
