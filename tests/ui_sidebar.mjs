// Browser test for the collapsible sidebar.
//
// The contract pinned here:
//   * the sidebar has a hide button; clicking it removes the sidebar and the
//     right-hand content takes over the freed width (the chat column gets wider);
//   * a show button appears in the top bar (not a dead end, not a hidden menu);
//   * clicking it (or Ctrl+B) brings the sidebar back and the column narrows again;
//   * the choice survives a reload (localStorage).
//
//   node tests/ui_sidebar.mjs [path-to-tea_agent-binary]
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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_sidebar_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });

let agent, chrome, ws;
try {
  // Nothing is sent in this test, so a provider pointing at a dead port is enough;
  // it only has to be a *valid* config, or the server refuses to start.
  fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    default_model: m
    models: {m: {}}
`);

  const agentPort = await freePort();
  const cdpPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj')], { stdio: 'ignore' });

  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  chrome = await acquireBrowser(tmp, 'chrome', { windowSize: '1440,900' });

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

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(1800);

  const layout = () => evalJs(`(function(){
    var sb = document.getElementById('sidebar');
    var col = document.getElementById('messages');
    var show = document.getElementById('sidebar-show');
    return {
      collapsed: document.getElementById('app').classList.contains('side-collapsed'),
      sidebarWidth: sb ? Math.round(sb.getBoundingClientRect().width) : 0,
      showVisible: !!show && show.getBoundingClientRect().width > 0,
      columnWidth: col ? Math.round(col.getBoundingClientRect().width) : 0,
      stored: JSON.parse(localStorage.getItem('tea_agent_ui_v1') || '{}').sidebar_hidden
    };
  })()`);

  let s = await layout();
  check('sidebar starts visible', s && s.sidebarWidth > 200 && s.collapsed === false, JSON.stringify(s));
  check('the tab title names the current workspace',
    (await evalJs('document.title')) === 'proj · TEA AGENT', await evalJs('document.title'));
  check('show button is hidden while the sidebar is open', s && s.showVisible === false, JSON.stringify(s));
  check('chat column starts narrow', s && s.columnWidth <= 780 + 2, String(s && s.columnWidth));

  await evalJs(`document.getElementById('sidebar-hide').click(); true`);
  await sleep(400);
  s = await layout();
  check('hide button removes the sidebar', s && s.collapsed === true && s.sidebarWidth === 0, JSON.stringify(s));
  check('show button takes its place', s && s.showVisible === true, JSON.stringify(s));
  check('the chat column widens into the freed space',
    s && s.columnWidth > 800, String(s && s.columnWidth));
  check('the choice is persisted', s && s.stored === true, JSON.stringify(s && s.stored));

  // Reload: the sidebar must stay collapsed.
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(1800);
  s = await layout();
  check('the collapsed state survives a reload',
    s && s.collapsed === true && s.sidebarWidth === 0, JSON.stringify(s));

  await evalJs(`document.getElementById('sidebar-show').click(); true`);
  await sleep(400);
  s = await layout();
  check('show button brings the sidebar back',
    s && s.collapsed === false && s.sidebarWidth > 200, JSON.stringify(s));
  check('the column narrows again', s && s.columnWidth <= 780 + 2, String(s && s.columnWidth));
  check('the choice is persisted after expanding', s && s.stored === false, JSON.stringify(s && s.stored));

  // Ctrl+B toggles both ways.
  await evalJs(`document.dispatchEvent(new KeyboardEvent('keydown', { key: 'b', ctrlKey: true, bubbles: true })); true`);
  await sleep(350);
  s = await layout();
  check('Ctrl+B collapses', s && s.collapsed === true, JSON.stringify(s));
  await evalJs(`document.dispatchEvent(new KeyboardEvent('keydown', { key: 'b', ctrlKey: true, bubbles: true })); true`);
  await sleep(350);
  s = await layout();
  check('Ctrl+B expands', s && s.collapsed === false && s.sidebarWidth > 200, JSON.stringify(s));
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  if (agent) agent.kill('SIGTERM');
  await sleep(300);
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
