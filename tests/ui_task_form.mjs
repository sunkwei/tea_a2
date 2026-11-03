// Browser test for the task queue's page: the form that creates a task.
//
// The contract pinned here:
//   * the form has every setting a task can have: the subject, the details,
//     the workspace, the start time, the provider, the model and the model
//     knobs;
//   * filling it in and pressing 排队 really creates that task - the API
//     says so (the values, not just the id);
//   * a task with a start time waits for it and says when it starts;
//   * a finished task shows what it produced, and the file can be downloaded
//     through the link the page offers.
//
//   node tests/ui_task_form.mjs [path-to-tea_agent-binary]
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
const SERVER = path.join(REPO_ROOT, 'sched', 'task_server.py');
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
const waitFor = async (fn, timeout = 8000, step = 150) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await fn()) return true;
    await sleep(step);
  }
  return false;
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_task_form_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });

let agent, queue, chrome, ws, mockServer;
const chatBodies = [];
try {
  // A tiny OpenAI-compatible mock: it reports the progress, and it writes a
  // file when the task asks for one (so the page has a result to show).
  const sse = (res, obj) => res.write('data: ' + JSON.stringify(obj) + '\n\n');
  const say = (res, t) => sse(res, { id: 'c', choices: [{ index: 0, delta: { content: t }, finish_reason: null }] });
  const call = (res, name, args) => sse(res, {
    id: 'c', choices: [{ index: 0, delta: { tool_calls: [{ index: 0, id: 'call_1', type: 'function',
      function: { name, arguments: JSON.stringify(args) } }] }, finish_reason: null }],
  });

  mockServer = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      if (!req.url.endsWith('/chat/completions')) { res.writeHead(404).end(); return; }
      let parsed = {};
      try { parsed = JSON.parse(body || '{}'); } catch { /* the mock is forgiving */ }
      chatBodies.push(parsed);
      const messages = parsed.messages || [];
      const last = messages[messages.length - 1] || {};
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      if (last.role === 'tool') {
        say(res, 'understood, continuing');
      } else if (String(last.content || '').indexOf('MAKEFILE') >= 0) {
        call(res, 'toolkit_file', { op: 'write', path: 'report.txt', content: 'hello from the task' });
      } else {
        call(res, 'toolkit_task', { op: 'update', progress: 80, note: 'the work is done' });
        say(res, 'done');
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
    models: {m: {}}
`);

  const agentPort = await freePort();
  const queuePort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj')], { stdio: 'ignore' });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch { /* not up yet */ }
    await sleep(150);
  }

  queue = spawn('python', [SERVER, '--agent', `http://127.0.0.1:${agentPort}`,
    '--port', String(queuePort), '--data-dir', path.join(tmp, 'tasks'),
    '--workspace-path', path.join(tmp, 'task-workspace'), '--provider', 'mock',
    '--allow-without-quota', '--interval', '1'],
    { stdio: 'ignore', env: { ...process.env, PYTHONUNBUFFERED: '1' } });
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

  // ---- the form has every setting a task can have -------------------------
  const fields = await evalJs(`(function(){
    var ids = ['new-title','new-prompt','new-workspace','new-when','new-when-at',
               'new-provider','new-model','knob-temperature','knob-max_tokens',
               'knob-reasoning_effort','new-allow','new-go'];
    var out = {};
    ids.forEach(function (id) { out[id] = !!document.getElementById(id); });
    return out;
  })()`);
  check('the form has a field for every setting',
    fields && Object.keys(fields).every((k) => fields[k] === true), JSON.stringify(fields));

  // The pickers are filled from the queue (the agent's workspaces, providers
  // and models).
  const pickersReady = await waitFor(async () => {
    const p = await evalJs(`(function(){
      return {
        workspaces: document.getElementById('dl-workspace').children.length,
        providers: document.getElementById('dl-provider').children.length,
        models: document.getElementById('dl-model').children.length
      };
    })()`);
    return p && p.workspaces >= 1 && p.providers >= 1 && p.models >= 1;
  }, 15000, 300);
  const pickers = await evalJs(`(function(){
    return {
      workspaces: document.getElementById('dl-workspace').children.length,
      providers: document.getElementById('dl-provider').children.length,
      models: document.getElementById('dl-model').children.length
    };
  })()`);
  check('the workspace picker knows the workspaces',
    pickers && pickers.workspaces >= 1, JSON.stringify(pickers));
  check('the provider picker knows the provider',
    pickers && pickers.providers >= 1, JSON.stringify(pickers));
  check('the model picker knows the models',
    pickers && pickers.models >= 1, JSON.stringify(pickers));

  // ---- a task with a start time -----------------------------------------
  const when = new Date(Date.now() + 2 * 3600 * 1000);
  const pad = (n) => (n < 10 ? '0' : '') + n;
  const localTime = `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}` +
    `T${pad(when.getHours())}:${pad(when.getMinutes())}`;
  await evalJs(`(function(){
    function set(id, value) {
      var n = document.getElementById(id);
      n.value = value;
      n.dispatchEvent(new Event('input', { bubbles: true }));
      n.dispatchEvent(new Event('change', { bubbles: true }));
    }
    set('new-title', '写一份报告');
    set('new-prompt', '把 README 的例子跑一遍，记下来。');
    set('new-workspace', ${JSON.stringify(path.join(tmp, 'proj'))});
    set('new-provider', 'mock');
    set('new-model', 'm');
    set('new-when', 'custom');
    set('new-when-at', ${JSON.stringify(localTime)});
    set('knob-temperature', '0.4');
    set('knob-max_tokens', '1234');
    set('knob-reasoning_effort', 'off');
    document.getElementById('new-go').click();
    return true;
  })()`);
  await sleep(1200);

  const said = await evalJs(`(function(){
    var m = document.getElementById('new-msg');
    return { text: m.textContent, ok: m.className.indexOf('ok') >= 0 };
  })()`);
  check('the page says the task was queued', said && said.ok === true, JSON.stringify(said));

  const listing = JSON.parse((await getJson(queuePort, '/api/tasks')).body);
  const tasks = (listing.data && listing.data.tasks) || [];
  const task = tasks.find((t) => t.title === '写一份报告');
  check('the task really exists', !!task, JSON.stringify(tasks.map((t) => t.title)));
  if (task) {
    check('the subject came through', task.title === '写一份报告', task.title);
    check('the details came through',
      String(task.prompt || '').indexOf('把 README 的例子跑一遍') >= 0, task.prompt);
    check('the workspace came through',
      String(task.workspace_path || '').indexOf('proj') >= 0, task.workspace_path);
    check('the provider came through', task.provider === 'mock', task.provider);
    check('the model came through', task.model === 'm', task.model);
    check('the model knobs came through',
      task.model_params && task.model_params.temperature === 0.4
      && task.model_params.max_tokens === 1234
      && task.model_params.reasoning_effort === 'off', JSON.stringify(task.model_params));
    check('the start time came through',
      task.not_before_text === localTime && task.starts_at_local, JSON.stringify([task.not_before_text, task.starts_at_local]));
    check('the task waits for its time', task.status === 'queued' && !task.started_at, task.status);
  }

  const card = await evalJs(`(function(){
    var cards = document.querySelectorAll('#s-queued .card');
    for (var i = 0; i < cards.length; i++) {
      var c = cards[i];
      if (c.querySelector('.title').textContent === '写一份报告') {
        return { text: c.textContent, pills: Array.prototype.map.call(c.querySelectorAll('.pill'),
          function (p) { return p.textContent; }) };
      }
    }
    return null;
  })()`);
  check('the queued card shows the task', !!card, JSON.stringify(card));
  check('the card shows the provider and the model',
    card && card.pills.indexOf('mock') >= 0 && card.pills.indexOf('m') >= 0,
    card ? JSON.stringify(card.pills) : '');
  check('the card shows when the task starts',
    card && card.pills.some((p) => p.indexOf('⏰') === 0),
    card ? JSON.stringify(card.pills) : '');

  // ---- a task that runs and produces a file ------------------------------
  await evalJs(`(function(){
    function set(id, value) {
      var n = document.getElementById(id);
      n.value = value;
      n.dispatchEvent(new Event('input', { bubbles: true }));
    }
    set('new-title', '做一个文件');
    set('new-prompt', 'MAKEFILE a report');
    document.getElementById('new-go').click();
    return true;
  })()`);
  await sleep(1200);

  const finished = await waitFor(async () => {
    const list = JSON.parse((await getJson(queuePort, '/api/tasks')).body);
    const one = ((list.data && list.data.tasks) || []).find((t) => t.title === '做一个文件');
    return !!one && one.status === 'done';
  }, 30000, 400);
  check('the task ran to the end', finished);

  let doneLink = '';
  const done = await waitFor(async () => {
    const shown = await evalJs(`(function(){
      var cards = document.querySelectorAll('#s-done .card');
      for (var i = 0; i < cards.length; i++) {
        if (cards[i].querySelector('.title').textContent === '做一个文件') {
          var a = cards[i].querySelector('.arts a');
          return { text: cards[i].textContent, link: a ? a.getAttribute('href') : '' };
        }
      }
      return null;
    })()`);
    if (shown && shown.link) { doneLink = shown.link; return true; }
    return false;
  }, 20000, 400);
  check('the finished card shows the file it produced', !!doneLink, JSON.stringify(doneLink));

  if (doneLink) {
    const file = await getJson(queuePort, doneLink);
    check('the download link serves the file',
      file.status === 200 && String(file.body).indexOf('hello from the task') >= 0,
      `${file.status} ${String(file.body).slice(0, 80)}`);
  }
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  if (agent) agent.kill('SIGTERM');
  if (queue) queue.kill('SIGTERM');
  if (mockServer) mockServer.close();
  await sleep(300);
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
