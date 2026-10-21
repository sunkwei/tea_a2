// Browser test for the todo panel above the composer.
//
// The contract pinned here:
//   * a session without a task list shows no panel at all;
//   * opening a session that has one renders every step with its status, the
//     "done/total" counter and a progress bar of the right width;
//   * the sidebar row carries the same progress chip, and it turns "done" when
//     everything is finished;
//   * the panel arrives live from the `todo` SSE event while a turn runs, and
//     the counter/bar/items update without a reload;
//   * clicking an item ticks it (PUT), which persists across a reload;
//   * the header collapses and expands the list;
//   * a brand-new draft clears the panel.
//
//   node tests/ui_todo.mjs [path-to-tea_agent-binary]
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
const waitFor = async (fn, timeout = 20000, step = 150) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await fn()) return true;
    await sleep(step);
  }
  return false;
};

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || !detail ? '' : '  ' + detail));
};

const STEPS = ['读取现有实现', '实现 todo 工具', '在界面上呈现', '补充测试'];
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_todo_ui_'));
const proj = path.join(tmp, 'proj');
const sessions = path.join(proj, '.tea_agent_run', 'sessions');
fs.mkdirSync(sessions, { recursive: true });
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });

const WITH_TODO = 's_todo_ui';
const WITHOUT_TODO = 's_plain_ui';
const todos = STEPS.map((t, i) => ({
  id: 't' + (i + 1), text: t, status: i === 0 ? 'done' : (i === 1 ? 'active' : 'pending'),
}));
fs.writeFileSync(path.join(sessions, WITH_TODO + '.json'), JSON.stringify({
  id: WITH_TODO, title: '有任务的会话',
  created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:01Z',
  messages: [{ role: 'user', content: '做一个 todo list', created_at: '2026-01-01T00:00:00Z' }],
  todos,
}, null, 2));
fs.writeFileSync(path.join(sessions, WITHOUT_TODO + '.json'), JSON.stringify({
  id: WITHOUT_TODO, title: '普通会话',
  created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
  messages: [{ role: 'user', content: '你好', created_at: '2026-01-01T00:00:00Z' }],
}, null, 2));
fs.writeFileSync(path.join(sessions, 'index.json'), JSON.stringify([
  { id: WITH_TODO, title: '有任务的会话', created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:01Z', message_count: 1, todo_total: 4, todo_done: 1 },
  { id: WITHOUT_TODO, title: '普通会话', created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z', message_count: 1 },
], null, 2));

let agent, chrome, ws, mockServer;
try {
  // A tiny provider: the first request returns a toolkit_todo call, the follow-up
  // (after the tool result) answers with text. That is enough to see the live
  // `todo` event land in the UI.
  let phase = 0;
  mockServer = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      if (!req.url.endsWith('/chat/completions')) { res.writeHead(404).end(); return; }
      const msgs = JSON.parse(body || '{}').messages || [];
      const afterTool = msgs.length && msgs[msgs.length - 1].role === 'tool';
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      const send = (o) => res.write('data: ' + JSON.stringify(o) + '\n\n');
      if (!afterTool && phase === 0) {
        phase = 1;
        send({
          id: 'c', choices: [{
            index: 0,
            delta: {
              role: 'assistant',
              tool_calls: [{
                index: 0, id: 'call_todo', type: 'function',
                function: {
                  name: 'toolkit_todo',
                  arguments: JSON.stringify({
                    op: 'set',
                    items: STEPS.map((t, i) => ({
                      text: t, status: i === 0 ? 'done' : (i === 1 ? 'active' : 'pending'),
                    })),
                  }),
                },
              }],
            },
            finish_reason: null,
          }],
        });
        send({ id: 'c', choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] });
      } else {
        send({ id: 'c', choices: [{ index: 0, delta: { content: '计划已更新。' },
          finish_reason: null }] });
        send({ id: 'c', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] });
      }
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
    models: {m: {max_context_tokens: 32768, supports_tools: true}}
`);

  const agentPort = await freePort();
  const cdpPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'),
    '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', proj], { stdio: 'ignore' });
  for (let i = 0; i < 120; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
    '--window-size=1280,900',
    `--remote-debugging-port=${cdpPort}`, `--user-data-dir=${path.join(tmp, 'chrome')}`,
    'about:blank'], { stdio: 'ignore' });
  let target = null;
  for (let i = 0; i < 120; i++) {
    try {
      const list = JSON.parse((await getJson(cdpPort, '/json/list')).body);
      target = list.find((t) => t.type === 'page');
      if (target) break;
    } catch {}
    await sleep(150);
  }
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

  const panel = () => evalJs(`(function(){
    var p = document.getElementById('todo-panel');
    return {
      hidden: p.classList.contains('hidden'),
      collapsed: p.classList.contains('collapsed'),
      count: document.getElementById('todo-count').textContent,
      allDone: document.getElementById('todo-count').classList.contains('all-done'),
      barWidth: document.getElementById('todo-bar').style.width,
      items: Array.prototype.map.call(document.querySelectorAll('#todo-list .todo-item'),
        function (n) {
          return {
            text: n.querySelector('.todo-text').textContent,
            cls: n.className,
            mark: n.querySelector('.todo-mark').textContent,
            checked: n.getAttribute('aria-checked')
          };
        }),
      sidebarChips: Array.prototype.map.call(document.querySelectorAll('.sess-todo'),
        function (n) { return { text: n.textContent, done: n.classList.contains('done') }; })
    };
  })()`);

  // The todo session is the most recently updated one, so it opens by default.
  const loaded = await waitFor(async () => {
    const p = await panel();
    return p && p.hidden === false && p.items.length === 4;
  });
  check('a session with a task list shows the panel', loaded);

  let p = await panel();
  check('the counter shows done/total', p.count === '1/4', p.count);
  check('the progress bar matches the progress', p.barWidth === '25%', p.barWidth);
  check('every step is listed', p.items.length === 4
    && p.items.map((i) => i.text).join('|').indexOf('实现 todo 工具') >= 0,
    JSON.stringify(p.items.map((i) => i.text)));
  check('statuses are rendered distinctly',
    p.items[0].cls.indexOf('done') >= 0 && p.items[1].cls.indexOf('active') >= 0
      && p.items[2].cls.indexOf('pending') >= 0,
    JSON.stringify(p.items.map((i) => i.cls)));
  check('the marks are ✓ / ● / ○',
    p.items[0].mark === '✓' && p.items[1].mark === '●' && p.items[2].mark === '○',
    JSON.stringify(p.items.map((i) => i.mark)));
  check('items are exposed as checkboxes',
    p.items[0].checked === 'true' && p.items[1].checked === 'false',
    JSON.stringify(p.items.map((i) => i.checked)));
  check('the sidebar shows the same progress', p.sidebarChips.some((c) => c.text === '1/4'),
    JSON.stringify(p.sidebarChips));

  // ---- clicking an item ticks it off -----------------------------------
  await evalJs(`document.querySelectorAll('#todo-list .todo-item')[2].click(); true`);
  const ticked = await waitFor(async () => {
    const q = await panel();
    return q.count === '2/4';
  });
  check('clicking an item marks it done', ticked, (await panel()).count);
  const disk = JSON.parse(fs.readFileSync(path.join(sessions, WITH_TODO + '.json'), 'utf8'));
  check('the tick is persisted to the session file',
    disk.todos[2].status === 'done', JSON.stringify(disk.todos.map((t) => t.status)));

  // A reload must show the new state (it comes from the server, not local state).
  await send('Page.reload');
  await sleep(2500);
  p = await panel();
  check('the tick survives a reload', p.count === '2/4' && !p.hidden, JSON.stringify(p.count));
  check('the reloaded item is styled as done', p.items[2].cls.indexOf('done') >= 0,
    p.items[2].cls);

  // ---- collapse / expand ------------------------------------------------
  await evalJs(`document.getElementById('todo-head').click(); true`);
  await sleep(200);
  p = await panel();
  const listHidden = await evalJs(
    `getComputedStyle(document.getElementById('todo-list')).display === 'none'`);
  check('the header collapses the list', p.collapsed === true && listHidden === true,
    JSON.stringify([p.collapsed, listHidden]));
  await evalJs(`document.getElementById('todo-head').click(); true`);
  await sleep(200);
  const listShown = await evalJs(
    `getComputedStyle(document.getElementById('todo-list')).display !== 'none'`);
  check('clicking again expands it', listShown === true);

  // ---- live update from a running turn ---------------------------------
  // The panel's description is stale on purpose: the agent will replace the list
  // through toolkit_todo and the SSE `todo` event must update it in place.
  await evalJs(`(function(){
    var i = document.getElementById('input');
    i.value = '重新规划一下';
    i.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);
  const live = await waitFor(async () => {
    const q = await panel();
    return q.count === '1/4' && q.items.length === 4 && q.items[1].cls.indexOf('active') >= 0;
  }, 25000);
  check('a running turn updates the panel live', live, (await panel()).count);
  p = await panel();
  check('the bar follows the live update', p.barWidth === '25%', p.barWidth);
  check('the sidebar chip follows too', p.sidebarChips.some((c) => c.text === '1/4'),
    JSON.stringify(p.sidebarChips));

  // ---- everything done => the panel gets out of the way ----------------
  // A finished task list is dead weight above the composer: it stays in the
  // session and in the sidebar chip, but the panel itself must not come back
  // in the next round.
  await sleep(2000);   // let the turn that just ran finish (todos are locked while it does)
  for (const idx of [1, 2, 3]) {
    await evalJs(`document.querySelectorAll('#todo-list .todo-item')[${idx}].click(); true`);
    await sleep(700);
  }
  const finished = await waitFor(async () => {
    const q = await panel();
    return q.count === '4/4';
  });
  check('the last tick completes the list', finished, (await panel()).count);
  p = await panel();
  check('a finished list hides the panel', p.hidden === true, JSON.stringify(p.hidden));
  check('the sidebar still shows the finished progress',
    p.sidebarChips.some((c) => c.text === '4/4' && c.done === true),
    JSON.stringify(p.sidebarChips));

  // The next round must not bring the finished list back.
  await evalJs(`(function(){
    var i = document.getElementById('input');
    i.value = '再来一轮';
    i.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);
  await sleep(3000);
  p = await panel();
  check('the next round does not show the finished list', p.hidden === true,
    JSON.stringify([p.hidden, p.count]));

  // ---- a session without a list shows nothing ---------------------------
  await evalJs(`(function(){
    var rows = document.querySelectorAll('#session-list .sess-row');
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].textContent.indexOf('普通会话') >= 0) { rows[i].click(); return true; }
    }
    return false;
  })()`);
  await sleep(1200);
  p = await panel();
  check('a session without a task list hides the panel', p.hidden === true,
    JSON.stringify(p.hidden));

  // ---- a new draft clears it -------------------------------------------
  await evalJs(`document.getElementById('new-session').click(); true`);
  await sleep(600);
  p = await panel();
  check('a new draft has no panel', p.hidden === true && p.items.length === 0,
    JSON.stringify([p.hidden, p.items.length]));
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
