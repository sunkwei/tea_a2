// Browser test: the topic URL (`/?s=<session_id>`) is what makes two tabs two
// topics.
//
// Same browser profile, two tabs. sessionStorage is per tab, so this is where
// "each window keeps its own topic" has to hold even though localStorage is
// shared. What is pinned:
//
//   * each tab opens the session named in its own URL, and the URL stays that;
//   * a turn started in tab A does not appear in tab B (no shared "current
//     session"), and tab B can start its own turn while A streams;
//   * both answers land in their own session files, not in one conversation;
//   * reloading a tab comes back to the same topic (the URL is the identity),
//     and "新会话" clears it;
//   * the topbar 复制链接 button copies this topic's address;
//   * opening a third tab on the plain URL does not disturb the other two.
//
//   node tests/ui_topic_url.mjs [path-to-tea_agent-binary]
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
const CHUNKS = 40;                 // 100 ms each: ~4 s per answer

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const freePort = () => new Promise((res) => {
  const s = net.createServer();
  s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); });
});
const request = (opts, body) => new Promise((res, rej) => {
  const req = http.request(opts, (r) => {
    let b = ''; r.on('data', (d) => (b += d)); r.on('end', () => res({ status: r.statusCode, body: b }));
  });
  req.on('error', rej);
  if (body) req.write(body);
  req.end();
});
const getJson = (port, p) => request({ host: '127.0.0.1', port, path: p });
const postJson = (port, p, obj) => request({
  host: '127.0.0.1', port, path: p, method: 'POST',
  headers: { 'Content-Type': 'application/json' },
}, JSON.stringify(obj));

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || !detail ? '' : '  ' + detail));
};
const waitFor = async (fn, timeout = 20000, step = 150) => {
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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_topic_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });

const frame = (delta, finish) => 'data: ' + JSON.stringify({
  id: 'c1', object: 'chat.completion.chunk',
  choices: [{ index: 0, delta, finish_reason: finish || null }],
}) + '\n\n';
const mock = http.createServer((req, res) => {
  let body = '';
  req.on('data', (d) => (body += d));
  req.on('end', () => {
    let prompt = '';
    try {
      const j = JSON.parse(body);
      const users = (j.messages || []).filter((m) => m.role === 'user');
      prompt = users.length ? String(users[users.length - 1].content || '') : '';
    } catch {}
    const m = /topic\s*([ab])/i.exec(prompt);
    const tag = m ? 'topic' + m[1].toLowerCase() : 'ans';
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    let n = 0;
    const timer = setInterval(() => {
      n++;
      res.write(frame({ content: tag + n + ' ' }));
      if (n >= CHUNKS) {
        clearInterval(timer);
        res.write(frame({}, 'stop'));
        res.write('data: [DONE]\n\n');
        res.end();
      }
    }, 100);
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
    models: {m: {}}
`);

/// A tab in the same browser (same profile → same localStorage, own
/// sessionStorage). CDP gives each page its own socket.
async function openTab(cdpPort, url) {
  // PUT /json/new?<url> creates a target in the running browser.
  const created = JSON.parse((await request({
    host: '127.0.0.1', port: cdpPort,
    path: '/json/new?' + encodeURIComponent(url || 'about:blank'), method: 'PUT',
  })).body);
  const sock = wsConnect(created.webSocketDebuggerUrl);
  await new Promise((r) => sock.on('open', r));
  const pending = new Map();
  let mid = 0;
  sock.on('message', (raw) => {
    const m = JSON.parse(raw);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  const send = (method, params) => new Promise((res) => {
    const id = ++mid; pending.set(id, res); sock.send({ id, method, params });
  });
  await send('Page.enable');
  await send('Runtime.enable');
  const evalJs = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.result && r.result.exceptionDetails) {
      throw new Error(JSON.stringify(r.result.exceptionDetails.exception || r.result.exceptionDetails));
    }
    return r.result && r.result.result ? r.result.result.value : undefined;
  };
  const ready = () => evalJs(`(function(){
    var t = window.teaState;
    if (!t || !t.workspace) return null;
    return { workspace: t.workspace.name, session: t.session ? t.session.id : null,
             search: location.search };
  })()`);
  return {
    evalJs, send, ready,
    goto: async (u) => { await send('Page.navigate', { url: u }); await sleep(400); },
    reload: async () => { await send('Page.reload', {}); await sleep(400); },
    close: () => { try { sock.close(); } catch {} },
  };
}

const VIEW = `(function(){
  var a = document.querySelectorAll('#messages .msg.assistant');
  var live = document.querySelector('#messages .msg.assistant.streaming');
  return { streaming: document.getElementById('send').className.indexOf('stop') >= 0,
           users: document.querySelectorAll('#messages .msg.user').length,
           userText: Array.prototype.map.call(document.querySelectorAll('#messages .msg.user'),
             function (n) { return n.textContent; }).join('|'),
           liveText: live ? live.textContent : '',
           lastText: a.length ? a[a.length - 1].textContent : '',
           search: location.search };
})()`;

let agent, chrome, tabA, tabB, tabC;
try {
  const agentPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj')], { stdio: 'ignore' });
  for (let i = 0; i < 120; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }
  const base = `http://127.0.0.1:${agentPort}/`;
  const wid = JSON.parse((await getJson(agentPort, '/api/workspaces')).body).workspaces[0].id;
  const mk = async (title) => JSON.parse((await postJson(agentPort,
    '/api/workspaces/' + wid + '/sessions', { title })).body).id;
  const sidA = await mk('topic A');
  const sidB = await mk('topic B');

  // ---- one browser, two tabs, two topics --------------------------------
  chrome = await acquireBrowser(tmp);
  const cdpPort = chrome.cdpPort;
  for (let i = 0; i < 120; i++) {
    try {
      const list = JSON.parse((await getJson(cdpPort, '/json/list')).body);
      if (list.some((t) => t.type === 'page')) break;
    } catch {}
    await sleep(150);
  }

  tabA = await openTab(cdpPort, base + '?s=' + sidA);
  const aBoot = await waitFor(() => tabA.ready(), 30000);
  check('tab A opens the session named in its URL', !!aBoot && aBoot.session === sidA,
    JSON.stringify(aBoot));
  tabB = await openTab(cdpPort, base + '?s=' + sidB);
  const bBoot = await waitFor(() => tabB.ready(), 30000);
  check('tab B opens its own session', !!bBoot && bBoot.session === sidB, JSON.stringify(bBoot));
  check('the two tabs are on different topics', !!aBoot && !!bBoot && aBoot.session !== bBoot.session,
    (aBoot && aBoot.session) + ' / ' + (bBoot && bBoot.session));

  // ---- A sends; B is untouched ------------------------------------------
  await tabA.evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = 'topic A question';
    ta.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);
  const aStreaming = await waitFor(async () => {
    const v = await tabA.evalJs(VIEW);
    return v.streaming && v.users === 1 ? v : null;
  });
  check('tab A streams its own answer', !!aStreaming, JSON.stringify(aStreaming));
  await sleep(800);
  const bIdle = await tabB.evalJs(VIEW);
  check('tab B does not see tab A\'s turn', bIdle.streaming === false && bIdle.users === 0,
    JSON.stringify(bIdle));
  check('tab B\'s URL is still its own topic', !bIdle.search || bIdle.search === '?s=' + sidB,
    bIdle.search);

  // ---- B sends while A is still streaming --------------------------------
  await tabB.evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = 'topic B question';
    ta.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);
  const bothStream = await waitFor(async () => {
    const a = await tabA.evalJs(VIEW);
    const b = await tabB.evalJs(VIEW);
    return a.streaming && b.streaming && b.users === 1 ? { a, b } : null;
  }, 15000);
  check('both tabs stream at the same time', !!bothStream,
    !!bothStream ? '' : JSON.stringify({ a: await tabA.evalJs(VIEW), b: await tabB.evalJs(VIEW) }));
  if (bothStream) {
    check('each tab shows only its own question',
      bothStream.a.userText.indexOf('topic A question') >= 0 &&
      bothStream.a.userText.indexOf('topic B') < 0 &&
      bothStream.b.userText.indexOf('topic B question') >= 0 &&
      bothStream.b.userText.indexOf('topic A') < 0,
      JSON.stringify({ a: bothStream.a.userText, b: bothStream.b.userText }));
    check('each tab shows only its own answer',
      bothStream.a.liveText.indexOf('topicb') < 0 && bothStream.b.liveText.indexOf('topica') < 0,
      JSON.stringify({ a: bothStream.a.liveText.slice(0, 40), b: bothStream.b.liveText.slice(0, 40) }));
  }

  const done = await waitFor(async () => {
    const a = await tabA.evalJs(VIEW);
    const b = await tabB.evalJs(VIEW);
    return !a.streaming && !b.streaming && a.lastText.indexOf('topica') >= 0 &&
      b.lastText.indexOf('topicb') >= 0 ? { a, b } : null;
  }, 30000);
  check('both turns finish with their own answer', !!done,
    !!done ? '' : JSON.stringify({ a: await tabA.evalJs(VIEW), b: await tabB.evalJs(VIEW) }));
  const sessA = JSON.parse((await getJson(agentPort, '/api/sessions/' + sidA)).body);
  const sessB = JSON.parse((await getJson(agentPort, '/api/sessions/' + sidB)).body);
  const textsA = sessA.messages.map((m) => m.content || '').join('\n');
  const textsB = sessB.messages.map((m) => m.content || '').join('\n');
  check('the two questions went into two session files',
    textsA.indexOf('topic A question') >= 0 && textsA.indexOf('topic B question') < 0 &&
    textsB.indexOf('topic B question') >= 0 && textsB.indexOf('topic A question') < 0,
    JSON.stringify({ a: textsA.slice(0, 80), b: textsB.slice(0, 80) }));

  // ---- reloading keeps the topic ---------------------------------------
  await tabA.reload();
  const aReload = await waitFor(async () => {
    const r = await tabA.ready();
    return r && r.session === sidA && r.search === '?s=' + sidA ? r : null;
  }, 30000);
  check('reloading tab A comes back to topic A', !!aReload, JSON.stringify(aReload));
  const aAfter = await waitFor(async () => {
    const v = await tabA.evalJs(VIEW);
    return v.users === 1 && v.lastText.indexOf('topica') >= 0 ? v : null;
  }, 15000);
  check('the transcript is still there after the reload', !!aAfter, JSON.stringify(aAfter));

  // ---- copy link --------------------------------------------------------
  const copied = await tabA.evalJs(`(function(){
    window.__copied = null;
    try {
      navigator.clipboard.writeText = function (text) { window.__copied = text; return Promise.resolve(); };
    } catch (e) {}
    var b = document.getElementById('session-link');
    if (!b || b.classList.contains('hidden')) return 'no button';
    b.click();
    return 'clicked';
  })()`);
  check('the copy-link button is available', copied === 'clicked', String(copied));
  const link = await waitFor(() => tabA.evalJs(`window.__copied || null`), 5000);
  check('it copies this topic\'s URL', !!link && link.indexOf('?s=' + sidA) > 0, String(link));

  // ---- a new draft clears the topic ------------------------------------
  await tabB.evalJs(`(function(){ document.getElementById('new-session').click(); return true; })()`);
  const cleared = await waitFor(async () => {
    const r = await tabB.ready();
    return r && r.session === null && r.search === '' ? r : null;
  }, 10000);
  check('新会话 clears the topic URL for that tab only', !!cleared, JSON.stringify(cleared));
  const aUnaffected = await tabA.evalJs(VIEW);
  check('tab A keeps its own topic', aUnaffected.search === '?s=' + sidA,
    aUnaffected.search);

  // ---- a third tab on the plain URL ------------------------------------
  tabC = await openTab(cdpPort, base);
  const cBoot = await waitFor(() => tabC.ready(), 30000);
  check('a third tab boots on the plain URL', !!cBoot, JSON.stringify(cBoot));
  const aStill = await tabA.evalJs(VIEW);
  check('opening it did not move the other tabs', aStill.search === '?s=' + sidA &&
    aStill.users === 1, JSON.stringify({ search: aStill.search, users: aStill.users }));
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  for (const t of [tabA, tabB, tabC]) if (t) t.close();
  if (chrome) chrome.kill('SIGKILL');
  if (agent) { agent.kill('SIGTERM'); await sleep(300); }
  mock.close();
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
