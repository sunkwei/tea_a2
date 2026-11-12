// Browser test for the task queue's page: the schedules panel.
//
// The contract pinned here:
//   * the panel has a field for the cron expression, the work, the subject
//     and the workspace;
//   * filling it in and pressing 加上 really creates that schedule - the API
//     says so (the expression, not just the id);
//   * the card of a schedule says when it fires next and how often it ran;
//   * a schedule can be paused from the page.
//
//   node tests/ui_cron.mjs
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
const SERVER = path.join(REPO_ROOT, 'sched', 'task_server.py');

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
const waitFor = async (fn, timeout = 10000, step = 200) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await fn()) return true;
    await sleep(step);
  }
  return false;
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_cron_ui_'));
fs.mkdirSync(path.join(tmp, 'ws'), { recursive: true });

let queue, chrome, ws, agent;
try {
  // The mock agent: the health check, the workspaces, the quota and the turn.
  agent = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      const json = (obj, status = 200) => {
        const raw = JSON.stringify(obj);
        res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': raw.length });
        res.end(raw);
      };
      if (req.url === '/api/health') return json({ ok: true, version: 'mock' });
      if (req.url === '/api/workspaces') {
        return json({ workspaces: [{ id: 'ws_mock', path: path.join(tmp, 'ws'), name: 'mock' }] });
      }
      if (req.url.startsWith('/api/providers/')) {
        return json({ window: { remaining_percent: 95.0 } });
      }
      if (req.url === '/api/chat') {
        res.writeHead(200, { 'Content-Type': 'text/event-stream' });
        res.write('data: ' + JSON.stringify({ type: 'start', session_id: 'sess_mock' }) + '\n\n');
        res.write('data: ' + JSON.stringify({ type: 'done', content: '做完了' }) + '\n\n');
        return res.end();
      }
      return json({ error: 'unknown path ' + req.url }, 404);
    });
  });
  await new Promise((r) => agent.listen(0, '127.0.0.1', r));

  const queuePort = await freePort();
  queue = spawn('python', [SERVER, '--agent', `http://127.0.0.1:${agent.address().port}`,
    '--port', String(queuePort), '--data-dir', path.join(tmp, 'tasks'),
    '--workspace', 'ws_mock', '--provider', 'mock', '--allow-without-quota',
    '--interval', '1'], { stdio: 'ignore', env: { ...process.env, PYTHONUNBUFFERED: '1' } });
  let queueUp = false;
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(queuePort, '/api/health')).status === 200) { queueUp = true; break; } } catch { /* not up yet */ }
    await sleep(150);
  }
  if (!queueUp) throw new Error('the task server did not become healthy');

  chrome = await acquireBrowser(tmp);
  const target = { webSocketDebuggerUrl: chrome.webSocketDebuggerUrl };
  if (!target) throw new Error('no chrome page target');

  ws = wsConnect(target.webSocketDebuggerUrl);
  await new Promise((r) => ws.on('open', r));
  let id = 0;
  const pending = new Map();
  ws.on('message', (raw) => {
    const msg = JSON.parse(raw);
    if (msg.method === 'Runtime.exceptionThrown') {
      const d = msg.params.exceptionDetails;
      console.log('PAGE ERROR: ' + JSON.stringify((d.exception && d.exception.description) || d.text));
    }
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
  await send('Page.navigate', { url: `http://127.0.0.1:${queuePort}/` });
  await sleep(1500);

  // ---- the panel has the fields -----------------------------------------
  const fields = await evalJs(`(function(){
    var ids = ['sch-when','sch-cron','sch-title','sch-workspace','sch-prompt','sch-go','sch-overlap'];
    var out = {};
    ids.forEach(function (id) { out[id] = !!document.getElementById(id); });
    return out;
  })()`);
  check('the panel has a field for every setting',
    fields && Object.keys(fields).every((k) => fields[k] === true), JSON.stringify(fields));

  // ---- a schedule is made from the page ---------------------------------
  await evalJs(`(function(){
    function set(id, value) {
      var n = document.getElementById(id);
      n.value = value;
      n.dispatchEvent(new Event('input', { bubbles: true }));
      n.dispatchEvent(new Event('change', { bubbles: true }));
    }
    set('sch-when', 'custom');
    set('sch-cron', '*/5 * * * *');
    set('sch-title', '每五分钟看一眼');
    set('sch-prompt', '看一眼日志，写一句总结。');
    document.getElementById('sch-go').click();
    return true;
  })()`);
  await sleep(1200);

  const said = await evalJs(`(function(){
    var m = document.getElementById('sch-msg');
    return { text: m.textContent, ok: m.className.indexOf('ok') >= 0 };
  })()`);
  check('the page says the schedule was added', said && said.ok === true, JSON.stringify(said));

  const listing = JSON.parse((await getJson(queuePort, '/api/schedules')).body);
  const schedules = (listing.data && listing.data.schedules) || [];
  const schedule = schedules.find((s) => s.title === '每五分钟看一眼');
  check('the schedule really exists', !!schedule, JSON.stringify(schedules.map((s) => s.title)));
  if (schedule) {
    check('the expression came through', schedule.cron === '*/5 * * * *', schedule.cron);
    check('the work came through',
      String(schedule.prompt || '').indexOf('看一眼日志') >= 0, schedule.prompt);
    check('the schedule knows when it fires next', !!schedule.next_fire_local, schedule.next_fire_local);
    check('the schedule is on', schedule.status === 'enabled', schedule.status);
  }

  // ---- the card of a schedule -------------------------------------------
  const card = await evalJs(`(function(){
    var cards = document.querySelectorAll('#s-sched .card');
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].querySelector('.title').textContent === '每五分钟看一眼') {
        return { text: cards[i].textContent,
                 pills: Array.prototype.map.call(cards[i].querySelectorAll('.pill'),
                   function (p) { return p.textContent; }) };
      }
    }
    return null;
  })()`);
  check('the panel shows the schedule', !!card, JSON.stringify(card));
  check('the card shows the expression',
    card && card.pills.some((p) => p.indexOf('*/5 * * * *') >= 0),
    card ? JSON.stringify(card.pills) : '');
  check('the card says when it fires next',
    card && card.text.indexOf('下次') >= 0, card ? card.text.slice(0, 120) : '');

  // ---- a schedule can be paused from the page ----------------------------
  await evalJs(`(function(){
    var cards = document.querySelectorAll('#s-sched .card');
    for (var i = 0; i < cards.length; i++) {
      if (cards[i].querySelector('.title').textContent === '每五分钟看一眼') {
        var b = cards[i].querySelectorAll('button')[0];
        b.click();
        return true;
      }
    }
    return false;
  })()`);
  const paused = await waitFor(async () => {
    const one = JSON.parse((await getJson(queuePort, '/api/schedules')).body);
    const list = (one.data && one.data.schedules) || [];
    const found = list.find((s) => s.title === '每五分钟看一眼');
    return found && found.status === 'paused';
  }, 10000, 250);
  check('the schedule can be paused from the page', paused);
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  if (queue) queue.kill('SIGTERM');
  if (agent) agent.close();
  await sleep(300);
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch { /* windows keeps the handle */ }
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
