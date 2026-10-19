// Browser test for the top-bar breadcrumbs.
//
// Regression: the crumbs were only rendered from showView(), which runs during
// boot *before* workspaces and sessions are loaded. The bar therefore kept the
// placeholder it painted first ("无工作区 / 新会话") even though the sidebar chip
// showed a workspace and the session list showed a conversation.
//
// What is pinned here:
//   * after boot the bar shows the selected workspace and the session title;
//   * the title the server derives from the first message replaces "新会话"
//     once that turn finished (it only exists after the reload);
//   * switching workspace updates the bar (and falls back to 新会话 when that
//     workspace has no session yet);
//   * starting a new session draft resets it to 新会话;
//   * switching back reopens that workspace's session with its title;
//   * renaming a session updates the bar immediately.
//
//   node tests/ui_crumbs.mjs [path-to-tea_agent-binary]
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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_crumbs_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj_a'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj_b'), { recursive: true });

let agent, chrome, ws, mock;
try {
  mock = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      if (!req.url.endsWith('/chat/completions')) { res.writeHead(404).end(); return; }
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      const chunk = (delta, finish = null) => res.write('data: ' + JSON.stringify({
        id: 'c', object: 'chat.completion.chunk',
        choices: [{ index: 0, delta, finish_reason: finish }],
      }) + '\n\n');
      chunk({ content: '好的。' });
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
    models: {m: {}}
roles: {main: {provider: mock, model: m}}
`);

  const agentPort = await freePort();
  const cdpPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj_a'), '--workspace', path.join(tmp, 'proj_b')], { stdio: 'ignore' });
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
  const crumbs = () => evalJs(`Array.prototype.map.call(
    document.querySelectorAll('#crumbs .crumb'), function (n) { return n.textContent; }).join(' / ')`);
  const pickWorkspace = async (needle) => {
    await evalJs(`document.getElementById('ws-chip').click(); true`);
    await sleep(300);
    await evalJs(`(function(){
      var b = Array.prototype.filter.call(document.querySelectorAll('#ws-dropdown .dd-item'),
        function (x) { return x.textContent.indexOf(${JSON.stringify(needle)}) >= 0; })[0];
      if (b) b.click();
      return true;
    })()`);
    await sleep(1400);
  };

  await send('Page.enable');
  await send('Runtime.enable');

  /// Boot involves three async round trips (health, providers, workspaces) plus
  /// a session fetch; a fixed sleep is flaky on a loaded machine.
  const booted = async () => {
    await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
    return waitFor(async () => {
      try {
        return (await evalJs(`(function(){
          var crumbs = document.querySelectorAll('#crumbs .crumb');
          var chip = document.getElementById('ws-chip-text');
          return crumbs.length >= 2 && chip && chip.textContent !== '未选择' &&
            chip.textContent !== '选择工作区';
        })()`)) === true;
      } catch (e) { return false; }
    }, 15000, 250);
  };

  await booted();

  check('boot: the bar names the workspace, not 无工作区',
    (await crumbs()) === 'proj_a / 新会话', await crumbs());

  // First turn: the server derives the session title from the message.
  const text = '检查构建脚本里的静态链接开关';
  await evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = ${JSON.stringify(text)};
    ta.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);
  const titled = await waitFor(async () => (await crumbs()) === `proj_a / ${text}`);
  check('after the first turn the bar shows the derived session title', titled, await crumbs());

  // Reload: same workspace + session must come back from localStorage.
  await booted();
  check('a reload keeps workspace and session in the bar',
    (await crumbs()) === `proj_a / ${text}`, await crumbs());

  // Switching workspace: proj_b has no session yet.
  await pickWorkspace('proj_b');
  check('switching workspace updates the bar',
    (await crumbs()).indexOf('proj_b /') === 0, await crumbs());

  // New session draft.
  await evalJs(`document.getElementById('new-session').click(); true`);
  await sleep(400);
  check('a new session draft shows 新会话', (await crumbs()) === 'proj_b / 新会话', await crumbs());

  // Switch back: that workspace's session reopens with its title.
  await pickWorkspace('proj_a');
  check('switching back reopens the session with its title',
    (await crumbs()) === `proj_a / ${text}`, await crumbs());

  // Rename from the sidebar: the bar must follow immediately.
  const renameClicked = await evalJs(`(function(){
    var item = document.querySelector('.sess-item.active') || document.querySelector('.sess-item');
    if (!item) return false;
    var btns = item.querySelectorAll('.sess-tool');
    for (var i = 0; i < btns.length; i++) {
      if (btns[i].textContent === '改名') { btns[i].click(); return true; }
    }
    return false;
  })()`);
  await sleep(500);
  if (renameClicked) {
    await evalJs(`(function(){
      var input = document.querySelector('#dialog input.ta');
      input.value = 'renamed by test';
      var btns = document.querySelectorAll('#dialog button');
      for (var i = 0; i < btns.length; i++) {
        if (btns[i].textContent === '保存') { btns[i].click(); return true; }
      }
      return false;
    })()`);
    await sleep(1000);
  }
  check('renaming a session updates the bar',
    (await crumbs()) === 'proj_a / renamed by test', await crumbs());
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
