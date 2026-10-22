// Browser test for forking a conversation from a turn in the jump index.
//
// The contract pinned here:
//   * clicking a numbered marker both jumps and selects it as the fork point;
//   * the composer says so (chip + placeholder + aria-pressed on the marker);
//   * clicking the same marker again, x or Esc cancels the selection;
//   * the next message sent while a fork point is selected lands in a NEW
//     session whose transcript is the parent's through that turn, and in the
//     parent's nothing changes;
//   * after that message the composer is back to normal (no fork chip), so the
//     following message stays in the branch.
//
//   node tests/ui_fork.mjs [path-to-tea_agent-binary]
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
const waitFor = async (fn, timeout = 15000, step = 150) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await fn()) return true;
    await sleep(step);
  }
  return false;
};

const TURNS = 4;
const turnText = (i) => '问题 ' + i + '：请检查第 ' + i + ' 个模块的构建与测试情况';
const answerText = (i) => '回答 ' + i + '\n' + Array.from({ length: 6 },
  (_, k) => '第 ' + (k + 1) + ' 行：第 ' + i + ' 轮的详细说明。').join('\n');

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_fork_ui_'));
const proj = path.join(tmp, 'proj');
const sessions = path.join(proj, '.tea_agent_run', 'sessions');
fs.mkdirSync(sessions, { recursive: true });
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });

const SID = 's_fork_ui';
const messages = [];
for (let i = 1; i <= TURNS; i++) {
  messages.push({ role: 'user', content: turnText(i), created_at: '2026-01-01T00:00:00Z' });
  messages.push({ role: 'assistant', content: answerText(i), created_at: '2026-01-01T00:00:01Z' });
}
fs.writeFileSync(path.join(sessions, SID + '.json'), JSON.stringify({
  id: SID,
  title: TURNS + ' 轮会话',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  messages,
}, null, 2));
// A real session is always in index.json: saving the branch rebuilds the index
// from it, so a fixture that exists only as a file would vanish from the
// sidebar the moment the branch is written.
fs.writeFileSync(path.join(sessions, 'index.json'), JSON.stringify([{
  id: SID,
  title: TURNS + ' 轮会话',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  message_count: messages.length,
}], null, 2));

let agent, chrome, ws, mockServer;
try {
  mockServer = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      if (!req.url.endsWith('/chat/completions')) {
        res.writeHead(404).end();
        return;
      }
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      res.write('data: ' + JSON.stringify({
        id: 'c', choices: [{ index: 0, delta: { content: '分支回答' }, finish_reason: null }],
      }) + '\n\n');
      res.write('data: ' + JSON.stringify({
        id: 'c', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
      }) + '\n\n');
      res.write('data: [DONE]\n\n');
      res.end();
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
    '--workspace', proj], { stdio: 'ignore' });
  for (let i = 0; i < 200; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
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
  const apiJson = async (p) => JSON.parse((await getJson(agentPort, p)).body);

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  const booted = await waitFor(async () => {
    try {
      return (await evalJs(`(function(){
        var rail = document.getElementById('turn-index');
        return !!rail && !rail.classList.contains('hidden') &&
          rail.querySelectorAll('.turn-dot').length === ${TURNS};
      })()`)) === true;
    } catch (e) { return false; }
  }, 20000, 250);
  if (!booted) throw new Error('the turn index never rendered');

  const clickDot = (n) => evalJs(
    `document.querySelectorAll('#turn-index .turn-dot')[${n - 1}].click(); true`);
  const strip = () => evalJs(`(function(){
    var s = document.getElementById('fork-strip');
    return { hidden: s.classList.contains('hidden'), text: s.textContent,
             tag: (s.querySelector('.f-tag') || {}).textContent || '' };
  })()`);
  const armed = (n) => evalJs(
    `document.querySelectorAll('#turn-index .turn-dot')[${n - 1}].classList.contains('fork')`);
  const pressed = (n) => evalJs(
    `document.querySelectorAll('#turn-index .turn-dot')[${n - 1}].getAttribute('aria-pressed')`);

  // 1. Clicking a marker selects it as the fork point (and still jumps).
  await clickDot(3);
  await sleep(200);
  const armed3 = await strip();
  check('clicking a marker selects it as the fork point', await armed(3) === true);
  check('the marker reports aria-pressed', (await pressed(3)) === 'true');
  check('the composer shows the fork point and its turn preview',
    armed3.hidden === false && armed3.tag === '分叉点' && armed3.text.indexOf('第 3 条') >= 0 &&
      armed3.text.indexOf('问题 3') >= 0, JSON.stringify(armed3));
  check('the composer placeholder switches to fork mode',
    (await evalJs(`document.getElementById('input').placeholder`)).indexOf('分叉模式') === 0);

  // 2. Clicking the same marker again cancels.
  await clickDot(3);
  await sleep(200);
  check('clicking the armed marker again cancels the fork point',
    (await armed(3)) === false && (await strip()).hidden === true);
  check('the placeholder returns to normal',
    (await evalJs(`document.getElementById('input').placeholder`)).indexOf('输入消息') === 0);

  // 3. Esc cancels too.
  await clickDot(2);
  await sleep(150);
  check('a different marker becomes the fork point', (await armed(2)) === true);
  await evalJs(`document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); true`);
  await sleep(200);
  check('Esc cancels the fork point', (await strip()).hidden === true && (await armed(2)) === false);

  // 3b. The chip's x cancels too.
  await clickDot(4);
  await sleep(150);
  await evalJs(`document.getElementById('fork-strip').querySelector('.f-cancel').click(); true`);
  await sleep(200);
  check('the chip x cancels the fork point',
    (await strip()).hidden === true && (await armed(4)) === false);

  // 4. Arm turn 2 and send: the message must open a branch, not append here.
  await clickDot(2);
  await sleep(150);
  const wsList = await apiJson('/api/workspaces');
  const wsId = wsList.workspaces[0].id;
  await evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = '分叉出来的问题';
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  })()`);
  await evalJs(`document.getElementById('send').click(); true`);
  const finished = await waitFor(async () => {
    try {
      const s = await strip();
      const streaming = await evalJs(`document.getElementById('send').classList.contains('stop')`);
      return s.hidden === true && streaming === false;
    } catch (e) { return false; }
  }, 25000, 200);
  check('sending clears the fork point once the turn starts', finished);

  let child = null;
  const branched = await waitFor(async () => {
    const l = await apiJson(`/api/workspaces/${encodeURIComponent(wsId)}/sessions`);
    child = (l.sessions || []).filter((s) => s.id !== SID)[0] || null;
    return !!child;
  }, 10000, 200);
  check('the send created a second session', branched);
  if (child) {
    check('the branch records where it came from',
      child.forked_from === SID && child.fork_turn === 2, JSON.stringify(child));
    check('the branch title names the parent turn',
      String(child.title).indexOf('分叉 2 · ') === 0, child.title);
    const childData = await apiJson(`/api/sessions/${encodeURIComponent(child.id)}`);
    const childUsers = (childData.messages || []).filter((m) => m.role === 'user');
    check('the branch keeps the parent turns up to the fork point, plus the new message',
      childUsers.length === 3 &&
        childUsers[0].content === turnText(1) &&
        childUsers[1].content === turnText(2) &&
        childUsers[2].content === '分叉出来的问题',
      JSON.stringify(childUsers.map((m) => m.content)));
  }

  const parentData = await apiJson(`/api/sessions/${encodeURIComponent(SID)}`);
  const parentUsers = (parentData.messages || []).filter((m) => m.role === 'user');
  check('the parent keeps all of its turns and none of the branch',
    parentUsers.length === TURNS &&
      !parentUsers.some((m) => m.content === '分叉出来的问题'),
    JSON.stringify(parentUsers.map((m) => m.content)));

  // 5. The UI itself is now showing the branch (3 user bubbles, not 4).
  const shown = await evalJs(`document.querySelectorAll('#messages .msg.user').length`);
  check('the chat view switches to the new branch', shown === 3, 'bubbles=' + shown);
  const childShown = await evalJs(`(function(){
    var b = document.querySelectorAll('#messages .msg.user');
    return b.length ? b[b.length - 1].textContent : '';
  })()`);
  check('the new message is the last bubble of the branch',
    childShown.indexOf('分叉出来的问题') >= 0, childShown);
  const crumbs = await evalJs(`document.getElementById('crumbs').textContent`);
  check('the breadcrumb follows the branch', String(crumbs).indexOf('分叉 2') >= 0, crumbs);

  // 6. Without a fork point the next message stays in this branch.
  await evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = '继续在分支里说';
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    document.getElementById('send').click();
    return true;
  })()`);
  const stillOneBranch = await waitFor(async () => {
    const l = await apiJson(`/api/workspaces/${encodeURIComponent(wsId)}/sessions`);
    const c = (l.sessions || []).filter((s) => s.id !== SID)[0];
    if (!c) return false;
    const d = await apiJson(`/api/sessions/${encodeURIComponent(c.id)}`);
    const users = (d.messages || []).filter((m) => m.role === 'user');
    return users.length === 4 && users[3].content === '继续在分支里说';
  }, 20000, 250);
  check('a normal follow-up stays in the branch (no new session)', stillOneBranch);
  const finalList = await apiJson(`/api/workspaces/${encodeURIComponent(wsId)}/sessions`);
  check('no third session was created', (finalList.sessions || []).length === 2,
    'sessions=' + (finalList.sessions || []).length);
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
