// Browser test for the connection status pill.
//
// Reported symptom: the top-right pill said 未连接 with a red dot while every
// other part of the page worked (workspace chip, model chip, sessions). Cause:
// /api/health was only fetched once, at boot. Any single transient failure -
// the page opening while the server was still starting, a request racing a
// restart, a background tab being suspended - pinned "未连接" forever.
//
// The contract pinned here:
//   * a single failed health check does NOT flip the pill (one hiccup is not an
//     outage);
//   * a second consecutive failure does, and the banner explains why;
//   * once health works again the pill recovers on its own, without a reload;
//   * a successful non-health request also triggers a re-check (fast recovery);
//   * the pill itself is clickable and re-checks immediately.
//
// Failures are injected at the network layer (CDP Network.setBlockedURLs for
// /api/health only), so the rest of the API keeps working exactly like the real
// situation that was reported.
//
//   node tests/ui_status.mjs [path-to-tea_agent-binary]
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
const waitFor = async (fn, timeout = 20000, step = 200) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await fn()) return true;
    await sleep(step);
  }
  return false;
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_status_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });

let agent, chrome, ws;
try {
  fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    default_model: m
    models: {m: {}}
roles: {main: {provider: mock, model: m}}
`);

  const agentPort = await freePort();
  const cdpPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj')], { stdio: 'ignore' });
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
  const pill = () => evalJs(`(function(){
    return {
      text: document.getElementById('status-text').textContent,
      dot: document.getElementById('status-dot').className,
      banner: !document.getElementById('banner').classList.contains('hidden'),
      bannerText: document.getElementById('banner-text').textContent
    };
  })()`);
  const blockHealth = (on) => send('Network.setBlockedURLs', {
    urls: on ? ['*api/health*'] : [],
  });

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Network.enable');
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });

  const connected = await waitFor(async () => {
    const p = await pill().catch(() => null);
    return p && p.text.indexOf('已连接') === 0;
  }, 15000);
  check('boot: the pill reports a connection', connected, JSON.stringify(await pill()));

  // ---- one failure is tolerated -----------------------------------------
  await blockHealth(true);
  // Wait past one poll interval (the app polls every 10s) but not two.
  await sleep(12000);
  let p = await pill();
  check('a single failed health check does not flip the pill',
    p.text.indexOf('已连接') === 0 && p.dot.indexOf('bad') < 0, JSON.stringify(p));

  // ---- two consecutive failures mean offline -----------------------------
  const wentDown = await waitFor(async () => (await pill()).text === '未连接', 20000);
  p = await pill();
  check('a second consecutive failure shows 未连接', wentDown, JSON.stringify(p));
  check('the banner explains the outage', p.banner === true && p.bannerText.indexOf('无法连接到本地服务') >= 0,
    JSON.stringify(p));

  // ---- recovery without a reload ----------------------------------------
  await blockHealth(false);
  const recovered = await waitFor(async () => (await pill()).text.indexOf('已连接') === 0, 25000);
  p = await pill();
  check('the pill recovers on its own once health works again', recovered, JSON.stringify(p));
  check('the banner is hidden again after recovery', p.banner === false, JSON.stringify(p));

  // ---- a successful request triggers an immediate re-check --------------
  await blockHealth(true);
  await waitFor(async () => (await pill()).text === '未连接', 30000);
  await blockHealth(false);
  // No waiting for the poll (10s): one ordinary UI action that hits the API has
  // to be enough. The 6s budget is well inside a poll interval, so a pass here
  // cannot be the poller's doing.
  const healed = await waitFor(async () => {
    await evalJs(`(function(){
      var b = Array.prototype.filter.call(document.querySelectorAll('.nav-row'),
        function (x) { return x.dataset.view === 'memory'; })[0];
      if (b) b.click();
      return true;
    })()`).catch(() => {});
    return (await pill()).text.indexOf('已连接') === 0;
  }, 6000, 300);
  check('a successful request makes the pill recover without waiting for the poll',
    healed, JSON.stringify(await pill()));

  // ---- the pill is clickable -------------------------------------------
  const clickTarget = await evalJs(`(function(){
    var s = document.getElementById('status');
    var r = s.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(s).cursor === 'pointer';
  })()`);
  check('the pill is clickable (pointer cursor)', clickTarget === true);
  await blockHealth(true);
  await evalJs(`document.getElementById('status').click(); true`);
  await sleep(1200);
  p = await pill();
  check('clicking the pill re-checks immediately and reports the failure',
    p.text === '未连接', JSON.stringify(p));
  await blockHealth(false);
  await evalJs(`document.getElementById('status').click(); true`);
  const backAfterClick = await waitFor(async () => (await pill()).text.indexOf('已连接') === 0, 8000);
  check('clicking again recovers', backAfterClick, JSON.stringify(await pill()));
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
