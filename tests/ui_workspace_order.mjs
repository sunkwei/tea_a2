// Browser test: the workspace dropdown is ordered by **most recently used**.
//
// The dropdown is a list of projects, and the project you just worked in is
// the one you want next - not the one you added first. The index file keeps
// the registration order, so the agent records when a workspace was used and
// orders the list by that.
//
// What is pinned here:
//   * the dropdown starts in registration order (nothing has been used yet);
//   * a turn in the second workspace moves it to the front;
//   * the tooltip says when it was last used.
//
//   node tests/ui_workspace_order.mjs [path-to-tea_agent-binary]
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
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? '  ' + detail : ''));
};
const waitFor = async (fn, timeout = 20000, step = 200) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try { if (await fn()) return true; } catch (e) { /* keep waiting */ }
    await sleep(step);
  }
  return false;
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_wsorder_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj_a'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj_b'), { recursive: true });

// A provider that answers right away: the test needs a turn to finish.
const mock = http.createServer((req, res) => {
  let body = '';
  req.on('data', (d) => (body += d));
  req.on('end', () => {
    if (!req.url.endsWith('/chat/completions')) { res.writeHead(404).end(); return; }
    const chunk = (delta, finish = null) => res.write('data: ' + JSON.stringify({
      id: 'c', object: 'chat.completion.chunk',
      choices: [{ index: 0, delta, finish_reason: finish }],
    }) + '\n\n');
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    chunk({ content: '好的' });
    chunk({}, 'stop');
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
`);

let agent, chrome, ws;
try {
  const agentPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj_a'), '--workspace', path.join(tmp, 'proj_b')],
    { stdio: 'ignore' });
  const up = await waitFor(async () => {
    try { return (await getJson(agentPort, '/api/health')).status === 200; } catch { return false; }
  });
  if (!up) throw new Error('the agent did not come up');

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
  // The dropdown's items, in the order they are drawn.
  const menuItems = () => evalJs(`(function(){
    var d = document.getElementById('ws-dropdown');
    var rows = Array.from(d.querySelectorAll('.dd-item'))
      .filter(function (x) { return x.textContent.indexOf('添加工作区') < 0; });
    return rows.map(function (x) {
      var t = x.getAttribute('title') || '';
      return { name: (x.childNodes[0] && x.childNodes[0].textContent || x.textContent).trim(),
               title: t };
    });
  })()`);
  const openMenu = () => evalJs(`(function(){
    document.getElementById('ws-chip').click();
    return true;
  })()`);
  const closeMenu = () => evalJs(`(function(){
    document.getElementById('ws-chip').click();
    return true;
  })()`);

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

  // The names of the two projects (the directories are named proj_a / proj_b).
  await openMenu();
  await sleep(400);
  let items = await menuItems();
  const names = items.map((i) => i.name);
  check('the dropdown lists both workspaces', names.length === 2, names.join(' | '));
  check('the list starts in registration order',
    names.join(',') === 'proj_a,proj_b', names.join(','));
  // The page landed on proj_a, so that one counts as "used" (it is the
  // workspace in front of the user); proj_b nobody has touched yet.
  const firstItem = items.filter((i) => i.name === 'proj_a')[0] || {};
  const secondItem = items.filter((i) => i.name === 'proj_b')[0] || {};
  check('the tooltip says when the workspace in use was last used',
    /上次使用/.test(firstItem.title || ''), firstItem.title);
  check('a workspace nobody used has no timestamp to show',
    !/上次使用/.test(secondItem.title || ''), secondItem.title);

  // Switch to the second workspace and send a message there: that is a use.
  await evalJs(`(function(){
    var d = document.getElementById('ws-dropdown');
    var rows = Array.from(d.querySelectorAll('.dd-item'))
      .filter(function (x) { return x.textContent.indexOf('添加工作区') < 0; });
    var b = rows.filter(function (x) { return (x.textContent || '').indexOf('proj_b') >= 0; })[0];
    if (!b) return false;
    b.click();
    return true;
  })()`);
  await sleep(1200);
  const onB = await evalJs(`(window.teaState && window.teaState.workspace &&
    window.teaState.workspace.name) || ''`);
  check('the second workspace is the current one', onB === 'proj_b', onB);

  await evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = '在第二个工作区里说句话';
    ta.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);
  const done = await waitFor(async () =>
    (await evalJs(`!!(window.teaState && !window.teaState.streaming)`)) === true, 30000, 250);
  check('the turn in the second workspace finished', done);

  // The dropdown now starts with the workspace that was just used.
  await openMenu();
  await sleep(600);
  items = await menuItems();
  const after = items.map((i) => i.name);
  check('the workspace just used comes first', after.join(',') === 'proj_b,proj_a', after.join(','));

  // The used workspace says when it was used (the tooltip).
  items = await menuItems();
  const used = items.filter((i) => i.name === 'proj_b')[0];
  check('the tooltip says when it was last used',
    !!used && /上次使用/.test(used.title || ''), used && used.title);

  // The API keeps the **registration** order on purpose: a cold browser lands
  // on the first workspace, and it must not be dragged into somebody else's
  // running turn just because that turn is the most recent one.
  const raw = JSON.parse((await getJson(agentPort, '/api/workspaces')).body || 'null');
  const apiOrder = (raw.workspaces || []).map((w) => w.name);
  check('the API keeps the registration order', apiOrder.join(',') === 'proj_a,proj_b',
    apiOrder.join(','));
  const stamps = (raw.workspaces || []).map((w) => w.last_used_at);
  check('the used workspace carries the stamp', !!stamps[1], stamps.join(' | '));
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
