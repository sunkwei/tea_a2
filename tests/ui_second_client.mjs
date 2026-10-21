// Browser test: a cold browser must NOT be dragged into somebody else's topic.
//
// The turn lives on the server, keyed by session, so any browser can follow it -
// but *only when asked to*. The old behaviour made a fresh page switch to
// whichever workspace had a running turn, which is exactly what breaks "two
// windows, two topics": open a new window to look at something else and it
// yanks you away.
//
// What has to hold now:
//   * a cold browser with no topic in the URL lands on the first workspace and
//     does not switch away, even though another workspace has a turn running;
//   * that other workspace's session is marked "进行中" in the sidebar once you
//     switch there, without having to open it;
//   * clicking it attaches and replays what the other client produced;
//   * opening the topic URL (`/?s=<session_id>`) goes straight there, and a
//     reload keeps the same topic;
//   * going back to the plain URL still does not hijack.
//
//   node tests/ui_second_client.mjs [path-to-tea_agent-binary]
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
const CHUNKS = 400;                // 250 ms each: ~100 s, the turn outlives every reload

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
const waitFor = async (fn, timeout = 20000, step = 200) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try {
      const v = await fn();
      if (v) return v;
    } catch (e) { /* keep polling */ }
    await sleep(step);
  }
  return false;
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_pcB_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
for (const d of ['proj_a', 'proj_b']) fs.mkdirSync(path.join(tmp, d), { recursive: true });

const frame = (delta, finish) => 'data: ' + JSON.stringify({
  id: 'c1', object: 'chat.completion.chunk',
  choices: [{ index: 0, delta, finish_reason: finish || null }],
}) + '\n\n';
const mock = http.createServer((req, res) => {
  let body = '';
  req.on('data', (d) => (body += d));
  req.on('end', () => {
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    let n = 0;
    const timer = setInterval(() => {
      n++;
      res.write(frame({ content: '字' }));
      if (n >= CHUNKS) {
        clearInterval(timer);
        res.write(frame({}, 'stop'));
        res.write('data: [DONE]\n\n');
        res.end();
      }
    }, 250);
    res.on('close', () => clearInterval(timer));
  });
});
await new Promise((r) => mock.listen(0, '127.0.0.1', r));

fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:${mock.address().port}
    api_key: sk-test
    default_model: m
    models: {m: {supports_tools: true}}
`);

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
const view = () => evalJs(`(function(){
  var a = document.querySelectorAll('#messages .msg.assistant');
  var last = a.length ? a[a.length - 1] : null;
  var t = window.teaState || {};
  return { workspace: document.getElementById('ws-chip-text').textContent,
           workspaceName: t.workspace ? t.workspace.name : null,
           sessionId: t.session ? t.session.id : null,
           title: document.title,
           streaming: document.getElementById('send').className.indexOf('stop') >= 0,
           users: document.querySelectorAll('#messages .msg.user').length,
           assistants: a.length,
           running: document.querySelectorAll('.sess-running').length,
           search: location.search,
           textLen: last ? last.textContent.length : 0,
           empty: !!document.querySelector('#messages .chat-empty') };
})()`);

async function turnStatus(port, sessionId) {
  try {
    return JSON.parse((await getJson(port,
      '/api/chat/turn?session_id=' + encodeURIComponent(sessionId))).body);
  } catch (e) {
    return null;
  }
}

try {
  const agentPort = await freePort();
  // proj_a is registered first: a cold boot with nothing to go on lands there.
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj_a'), '--workspace', path.join(tmp, 'proj_b')],
    { stdio: 'ignore' });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }
  const base = `http://127.0.0.1:${agentPort}/`;
  const list = JSON.parse((await getJson(agentPort, '/api/workspaces')).body).workspaces;
  check('two workspaces, proj_a first', list.length === 2 && list[0].name === 'proj_a',
    list.map((w) => w.name).join(','));
  const wsB = list[1].id;

  // ---- PC A: a plain HTTP client starts a turn in proj_b, then goes away --
  let sid = null;
  let frames = 0;
  const a = http.request({ host: '127.0.0.1', port: agentPort, path: '/api/chat', method: 'POST',
    headers: { 'Content-Type': 'application/json' } }, (res) => {
    res.on('data', (d) => {
      for (const line of d.toString().split('\n')) {
        if (line.indexOf('data:') !== 0) continue;
        frames++;
        try { const ev = JSON.parse(line.slice(5).trim()); if (ev.session_id) sid = ev.session_id; } catch {}
      }
    });
  });
  a.end(JSON.stringify({ workspace_id: wsB, message: 'long task from PC A' }));
  await sleep(2500);
  a.destroy();
  check('PC A started a turn in proj_b and left', !!sid && frames > 1,
    'frames=' + frames + ' session=' + sid);

  const turns = JSON.parse((await getJson(agentPort, '/api/chat/turns')).body);
  const mine = (turns.turns || []).filter((t) => t.session_id === sid)[0];
  check('GET /api/chat/turns lists the running turn', !!mine && mine.running === true,
    JSON.stringify(turns));
  check('it names the workspace the turn belongs to', mine && mine.workspace_id === wsB,
    mine && mine.workspace_id);

  // ---- PC B: a brand new browser profile, nothing in common with A -------
  const cdpPort = await freePort();
  chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
    `--remote-debugging-port=${cdpPort}`, `--user-data-dir=${path.join(tmp, 'pcB')}`,
    'about:blank'], { stdio: 'ignore' });
  let target = null;
  for (let i = 0; i < 120; i++) {
    try {
      const l = JSON.parse((await getJson(cdpPort, '/json/list')).body);
      target = l.find((t) => t.type === 'page');
      if (target) break;
    } catch {}
    await sleep(150);
  }
  if (!target) throw new Error('no chrome page target');
  ws = wsConnect(target.webSocketDebuggerUrl);
  await new Promise((r) => ws.on('open', r));
  ws.on('message', (raw) => {
    const m = JSON.parse(raw);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  await send('Page.enable');
  await send('Runtime.enable');
  const ready = () => evalJs(`(function(){
    var t = window.teaState;
    if (!t || !t.workspace) return null;
    return { ws: t.workspace.name, sessions: t.sessions.length };
  })()`);
  const goto = async (u) => { await send('Page.navigate', { url: u }); await sleep(600); };

  await goto(base);
  const boot = await waitFor(ready, 30000);
  check('PC B boots without a topic', !!boot, JSON.stringify(boot));

  // The whole point: proj_b's running turn must not pull this page over.
  const bHome = await waitFor(async () => {
    const v = await view();
    return v.workspaceName === 'proj_a' ? v : null;
  }, 15000);
  check('PC B stays on the first workspace, no hijack', !!bHome, JSON.stringify(bHome));
  check('PC B is not streaming a turn it did not ask for',
    bHome && bHome.streaming === false, bHome && JSON.stringify(bHome));
  check('PC B tab title follows that workspace', bHome && bHome.title === 'proj_a · TEA AGENT',
    bHome && bHome.title);

  // ---- switching there shows the running session, marked, not opened -----
  await evalJs(`(function(){ document.getElementById('ws-chip').click(); return true; })()`);
  const picked = await evalJs(`(function(){
    var items = document.querySelectorAll('#ws-dropdown .dd-item');
    for (var i = 0; i < items.length; i++) {
      if (items[i].textContent.indexOf('proj_b') === 0) { items[i].click(); return items[i].textContent; }
    }
    return null;
  })()`);
  check('the workspace menu lists proj_b', picked && picked.indexOf('proj_b') === 0, String(picked));
  const bList = await waitFor(async () => {
    const v = await view();
    return v.workspaceName === 'proj_b' ? v : null;
  }, 15000);
  check('switching to proj_b opens the workspace', !!bList, JSON.stringify(bList));
  // Opening a workspace auto-opens its newest session, which here is the running
  // one - so B may already be following it. Step off it (a draft) to see the row
  // marked in the sidebar: following a turn is never implicit for another topic.
  await evalJs(`(function(){ document.getElementById('new-session').click(); return true; })()`);
  const marked = await waitFor(async () => {
    const v = await view();
    return v.running >= 1 && v.streaming === false ? v : null;
  }, 15000);
  check('the running session is marked 进行中 in the sidebar', !!marked, JSON.stringify(marked));

  const before = await turnStatus(agentPort, sid);
  const clicked = await evalJs(`(function(){
    var row = document.querySelector('#session-list .sess-row');
    if (!row) return null;
    row.click();
    return row.textContent;
  })()`);
  check('the session row can be clicked', !!clicked, String(clicked));
  const attached = await waitFor(async () => {
    const v = await view();
    return v.streaming && v.sessionId === sid ? v : null;
  }, 20000);
  check('clicking it attaches to the running turn', !!attached, JSON.stringify(attached));
  check('the topic URL follows the session', attached && attached.search === '?s=' + sid,
    attached && attached.search);
  const grew = await waitFor(async () => {
    const v = await view();
    return attached && v.textLen > attached.textLen + 10 ? v : null;
  }, 20000);
  check('B replays and then follows what A produced', !!grew, JSON.stringify(grew));
  check('the server did not cancel the turn for B', !!before && before.running === true,
    JSON.stringify(before));

  // ---- reloading keeps the topic (the URL is the identity) --------------
  await goto(base + '?s=' + sid);
  const reloaded = await waitFor(async () => {
    const v = await view();
    return v.sessionId === sid && v.streaming ? v : null;
  }, 30000);
  check('reloading ?s=… comes back to the same topic and re-attaches', !!reloaded,
    JSON.stringify(reloaded));

  // ---- a genuinely new browser still does not get hijacked --------------
  // Clearing both stores is what a fresh profile looks like. The turn in proj_b
  // is still running, so if anything ever brings the auto-jump back, this fails.
  await evalJs(`(function(){ localStorage.clear(); sessionStorage.clear(); return true; })()`);
  await goto(base);
  const plain = await waitFor(async () => {
    const v = await view();
    return v.workspaceName === 'proj_a' ? v : null;
  }, 30000);
  check('a fresh profile lands on the first workspace while proj_b runs',
    !!plain, JSON.stringify(plain));
  check('and it is not following that turn', plain && plain.streaming === false,
    plain && JSON.stringify(plain));

  await evalJs(`fetch('/api/chat/stop', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({session_id: ${JSON.stringify(sid)}})}).then(r=>r.status)`).catch(() => {});
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  if (agent) { agent.kill('SIGTERM'); await sleep(300); }
  mock.close();
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
