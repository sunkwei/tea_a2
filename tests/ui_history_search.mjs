// Browser test for the sidebar's history search: searching the conversations
// and the trajectory of a workspace, and jumping to the message that matched.
//
//   node tests/ui_history_search.mjs [path-to-tea_agent-binary]
//
// The sessions are written to disk before the agent starts (a browser test has
// no model to talk to), so the search runs against real session files.
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
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? '  ' + detail : ''));
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_hsearch_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
const proj = path.join(tmp, 'proj');
const runDir = path.join(proj, '.tea_agent_run');
fs.mkdirSync(path.join(runDir, 'sessions'), { recursive: true });
fs.mkdirSync(path.join(runDir, 'trajectories'), { recursive: true });
fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:1
    api_key: sk-test
    default_model: m
    models: {m: {}}
`);

// Two conversations. The first one talks about otters (水獭); the second one
// does not, so a search must not drag it in.
fs.writeFileSync(path.join(runDir, 'sessions', 's_otter.json'), JSON.stringify({
  id: 's_otter',
  title: '关于水獭的讨论',
  created_at: '2026-10-29T07:00:00Z',
  updated_at: '2026-10-29T07:10:00Z',
  messages: [
    { role: 'user', content: '水獭到底吃什么？' },
    { role: 'assistant', content: '水獭主要吃鱼，也吃蟹和贝类。' },
  ],
}));
fs.writeFileSync(path.join(runDir, 'sessions', 's_other.json'), JSON.stringify({
  id: 's_other',
  title: '别的话题',
  created_at: '2026-10-29T06:00:00Z',
  updated_at: '2026-10-29T06:10:00Z',
  messages: [
    { role: 'user', content: '今天天气怎么样' },
    { role: 'assistant', content: '还行。' },
  ],
}));
// A trajectory: the word only ever appeared in a command's output.
fs.writeFileSync(path.join(runDir, 'trajectories', 's_other.jsonl'),
  JSON.stringify({
    turn_id: 't_1', started_at: '2026-10-29T06:05:00Z', finished_at: '2026-10-29T06:05:01Z',
    ok: true, iterations: 1,
    steps: [{
      index: 0, kind: 'tool', name: 'toolkit_exec',
      arguments: '{"command":"grep -r otter ."}', output: 'docs/水獭笔记.md:1: 水獭吃鱼', ok: true,
    }],
  }) + '\n');

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
const waitState = async (expr, tries = 60) => {
  for (let i = 0; i < tries; i++) {
    const v = await evalJs(expr);
    if (v) return v;
    await sleep(200);
  }
  return null;
};

try {
  const agentPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', proj], { stdio: 'ignore' });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  chrome = await acquireBrowser(tmp);
  const target = { webSocketDebuggerUrl: chrome.webSocketDebuggerUrl };
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
  await waitState('window.teaState && window.teaState.workspace ? true : false');
  // 会话列表已经加载出来
  await waitState(`(window.teaState && window.teaState.sessions && window.teaState.sessions.length) ? true : false`);

  // ---- the search box ----------------------------------------------------
  const box = await evalJs(`(function(){
    var i = document.getElementById('session-search');
    if (!i) return null;
    return { visible: i.offsetParent !== null, placeholder: i.placeholder };
  })()`);
  check('the sidebar has a history search box', !!box && box.visible, JSON.stringify(box));
  check('it says what it searches', box && /历史/.test(box.placeholder || ''), box && box.placeholder);

  // ---- search the conversation -------------------------------------------
  await evalJs(`(function(){
    var i = document.getElementById('session-search');
    i.value = '水獭';
    i.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    return true;
  })()`);
  // 等**搜索结果**那一栏（普通会话列表里也有「水獭」两个字，别把它当结果）。
  const found = await waitState(`(function(){
    var box = document.getElementById('session-list');
    if (!box || !box.querySelector('.sess-search-head')) return null;
    var txt = box.textContent || '';
    if (txt.indexOf('水獭') < 0) return null;
    return { text: txt.slice(0, 400),
             marked: !!box.querySelector('.sess-snip b'),
             rows: box.querySelectorAll('.sess-item').length };
  })()`);
  check('the search finds the conversation', found && (found.text || '').indexOf('水獭') >= 0,
    JSON.stringify(found));
  check('the matching word is marked', found && found.marked, JSON.stringify(found));
  check('the session title is shown', found && (found.text || '').indexOf('关于水獭的讨论') >= 0,
    JSON.stringify(found));

  // ---- the trajectory is searched too -------------------------------------
  await evalJs(`(function(){
    var i = document.getElementById('session-search');
    i.value = 'grep -r otter';
    i.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    return true;
  })()`);
  const traj = await waitState(`(function(){
    var box = document.getElementById('session-list');
    if (!box || !box.querySelector('.sess-search-head')) return null;
    var txt = box.textContent || '';
    if (txt.indexOf('轨迹') < 0) return null;
    return { text: txt.slice(0, 400) };
  })()`);
  check('the trajectory is searched too', !!traj, JSON.stringify(traj));

  // ---- clicking a result opens that session ------------------------------
  await evalJs(`(function(){
    var i = document.getElementById('session-search');
    i.value = '水獭';
    i.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    return true;
  })()`);
  await waitState(`(function(){
    var box = document.getElementById('session-list');
    if (!box || !box.querySelector('.sess-search-head')) return false;
    return (box.textContent || '').indexOf('关于水獭的讨论') >= 0;
  })()`);
  await evalJs(`(function(){
    var rows = document.querySelectorAll('#session-list .sess-item .sess-row');
    for (var i = 0; i < rows.length; i++) {
      if ((rows[i].textContent || '').indexOf('关于水獭的讨论') >= 0) { rows[i].click(); return true; }
    }
    return false;
  })()`);
  // 等「命中」的那条消息被标出来：页面刚打开时 newest 会话可能已经自动打开了，
  // 所以这里等的是**点击之后**才出现的落点标记，不是「消息在不在」。
  const opened = await waitState(`(function(){
    var m = document.getElementById('messages');
    if (!m || (m.textContent || '').indexOf('水獭到底吃什么') < 0) return null;
    if (!m.querySelector('.msg.hit')) return null;
    return { text: (m.textContent || '').slice(0, 200),
             session: window.teaState && window.teaState.session && window.teaState.session.id };
  })()`);
  check('the conversation opens', !!opened, JSON.stringify(opened));
  check('the matched message is highlighted', !!opened, JSON.stringify(opened));
  check('the right session is open', opened && opened.session === 's_otter',
    opened && opened.session);

  // ---- back to the plain list --------------------------------------------
  await evalJs(`(function(){
    var i = document.getElementById('session-search');
    i.value = '';
    i.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    return true;
  })()`);
  await sleep(400);
  const back = await evalJs(`(function(){
    var box = document.getElementById('session-list');
    return { text: (box.textContent || '').slice(0, 200),
             value: document.getElementById('session-search').value,
             head: !!box.querySelector('.sess-search-head') };
  })()`);
  check('Esc brings the session list back', back && !back.head, JSON.stringify(back));
  check('the search box is emptied', back && back.value === '', JSON.stringify(back));
  check('the sessions are listed again',
    back && (back.text || '').indexOf('关于水獭的讨论') >= 0, JSON.stringify(back));
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  if (agent) { agent.kill('SIGTERM'); await sleep(300); }
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
