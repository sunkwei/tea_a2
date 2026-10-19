// Browser test for the conversation jump index (right-hand turn rail).
//
// Requirement: a long session (30+ turns) must be navigable. Every user message
// gets a numbered marker; hovering a marker shows a tip with at most the first
// 64 characters of that user's input; clicking scrolls to that turn. The marker
// for the turn currently in view is highlighted while scrolling.
//
//   node tests/ui_turn_index.mjs [path-to-tea_agent-binary]
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

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || !detail ? '' : '  ' + detail));
};
const waitFor = async (fn, timeout = 12000, step = 150) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await fn()) return true;
    await sleep(step);
  }
  return false;
};

const TURNS = 30;
const LONG = 'LONG-QUESTION-' + 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'.repeat(3);
const turnText = (i) => i === 10
  ? LONG
  : '问题 ' + i + '：请检查第 ' + i + ' 个模块的构建与测试情况';
const longChars = Array.from(LONG);
const longPreview = longChars.slice(0, 64).join('') + (longChars.length > 64 ? '…' : '');
const answerText = (i) => '回答 ' + i + '\n' + Array.from({ length: 8 },
  (_, k) => '第 ' + (k + 1) + ' 行：这里是第 ' + i + ' 轮的详细说明，用来把页面撑得足够长。').join('\n');

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_turn_index_'));
const proj = path.join(tmp, 'proj');
const sessions = path.join(proj, '.tea_agent_run', 'sessions');
fs.mkdirSync(sessions, { recursive: true });
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });

const SID = 's_turn_index';
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

let agent, chrome, ws, mock;
try {
  mock = http.createServer((req, res) => { res.writeHead(404).end(); });
  await new Promise((r) => mock.listen(0, '127.0.0.1', r));

  fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:${mock.address().port}
    api_key: sk-test
    default_model: m
    models: {m: {}}
roles: {main: {provider: mock, model: m}}
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

  chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
    '--window-size=1280,900',
    `--remote-debugging-port=${cdpPort}`, `--user-data-dir=${path.join(tmp, 'chrome')}`,
    'about:blank'], { stdio: 'ignore' });
  let target = null;
  for (let i = 0; i < 200; i++) {
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
  const dotCount = () => evalJs(`document.querySelectorAll('#turn-index .turn-dot').length`);
  const railHidden = () => evalJs(`document.getElementById('turn-index').classList.contains('hidden')`);
  const activeTurn = () => evalJs(`document.getElementById('turn-index').dataset.active || ''`);

  await send('Page.enable');
  await send('Runtime.enable');
  const booted = async () => {
    await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
    return waitFor(async () => {
      try {
        return (await evalJs(`(function(){
          var rail = document.getElementById('turn-index');
          return !!rail && !rail.classList.contains('hidden') &&
            rail.querySelectorAll('.turn-dot').length === ${TURNS};
        })()`)) === true;
      } catch (e) { return false; }
    }, 15000, 250);
  };

  if (!(await booted())) throw new Error('the turn index never rendered');

  check('renders one numbered marker per user turn',
    (await dotCount()) === TURNS, String(await dotCount()));
  check('the rail reports the turn count',
    (await evalJs(`document.getElementById('turn-index').dataset.count`)) === String(TURNS));

  const firstTitle = await evalJs(`document.querySelector('#turn-index .turn-dot').title`);
  check('the first marker carries a title with its user input',
    firstTitle === '第 1 条：' + turnText(1), firstTitle);

  const tenthTitle = await evalJs(`document.querySelectorAll('#turn-index .turn-dot')[9].title`);
  check('a long input is cut to its first 64 characters',
    tenthTitle === '第 10 条：' + longPreview &&
      Array.from(longPreview.replace('…', '')).length === 64, tenthTitle);

  // Hovering marker 10 must open the styled tip (not just the native title).
  await evalJs(`(function(){
    var dot = document.querySelectorAll('#turn-index .turn-dot')[9];
    dot.dispatchEvent(new MouseEvent('mouseenter'));
    return true;
  })()`);
  await sleep(120);
  const tip = await evalJs(`(function(){
    var t = document.getElementById('turn-tip');
    var dot = document.querySelectorAll('#turn-index .turn-dot')[9];
    var tr = t.getBoundingClientRect(), dr = dot.getBoundingClientRect();
    return {
      hidden: t.classList.contains('hidden'),
      turn: t.dataset.turn,
      text: t.querySelector('.tip-text') ? t.querySelector('.tip-text').textContent : '',
      whole: t.textContent,
      leftOfDot: tr.right <= dr.left,
      visible: tr.width > 0 && tr.height > 0
    };
  })()`);
  check('hovering a marker opens its tip', !tip.hidden && tip.visible, JSON.stringify(tip));
  check('the tip names the turn and shows the 64-char preview',
    tip.turn === '10' && tip.text === longPreview && tip.whole.indexOf('第 10 条') >= 0, JSON.stringify(tip));
  check('the tip sits beside the marker without covering it', tip.leftOfDot === true, JSON.stringify(tip));

  // Click marker 10: the corresponding user bubble must land near the top.
  await evalJs(`document.querySelectorAll('#turn-index .turn-dot')[9].click(); true`);
  await sleep(250);
  const atTen = await evalJs(`(function(){
    var s = document.getElementById('chat-scroll');
    var n = document.querySelector('#messages .msg.user[data-mid="18"]');
    return {
      active: document.getElementById('turn-index').dataset.active,
      offset: n ? Math.round(n.getBoundingClientRect().top - s.getBoundingClientRect().top) : -999,
      scrolled: s.scrollTop > 0
    };
  })()`);
  check('clicking a marker jumps to that turn',
    atTen.active === '10' && atTen.scrolled && atTen.offset >= 0 && atTen.offset <= 40,
    JSON.stringify(atTen));

  // Scrolling back to the top highlights turn 1; the bottom highlights turn 30.
  await evalJs(`document.getElementById('chat-scroll').scrollTop = 0; true`);
  await sleep(250);
  check('scrolling to the top highlights the first turn', (await activeTurn()) === '1', await activeTurn());
  await evalJs(`(function(){ var s = document.getElementById('chat-scroll'); s.scrollTop = s.scrollHeight; return true; })()`);
  await sleep(250);
  check('scrolling to the bottom highlights the last turn',
    (await activeTurn()) === String(TURNS), await activeTurn());
  const tipHidden = await evalJs(`document.getElementById('turn-tip').classList.contains('hidden')`);
  check('the tip closes when the marker is no longer hovered', tipHidden === true);

  // A fresh session has no turns, so the rail has to disappear.
  await evalJs(`document.getElementById('new-session').click(); true`);
  const hiddenDraft = await waitFor(async () => (await railHidden()) === true, 4000, 100);
  check('a new session hides the index', hiddenDraft && (await dotCount()) === 0);

  // Reopening the persisted session brings the index back.
  await evalJs(`(function(){
    var row = document.querySelector('#session-list .sess-row');
    if (row) row.click();
    return !!row;
  })()`);
  const reopened = await waitFor(async () => (await dotCount()) === TURNS, 6000, 150);
  check('reopening the session restores the index', reopened, String(await dotCount()));
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
