// The subscription-quota badge in the top bar.
//
// A provider that declares `supports_usage: true` gets a badge showing how
// much of its quota is left (MiniMax M Plan: a 5-hour window and a weekly
// window). This test pins the parts that are easy to break:
//
//   * the badge appears for a provider that declares the capability, and shows
//     the remaining percentage;
//   * the tooltip explains the number (window, reset time, weekly window);
//   * clicking the badge asks again;
//   * a provider without the capability gets no badge at all;
//   * the settings page can query any provider, not just the active one.
//
//   node tests/ui_usage.mjs [path-to-tea_agent-binary]
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

const NOW = 1792561091281;
const WINDOW_MS = 5 * 3600 * 1000;
const WEEK_MS = 7 * 24 * 3600 * 1000;

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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_usage_'));
fs.mkdirSync(path.join(tmp, 'ws'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });

let agent, chrome, ws, mockServer;
try {
  let quotaHits = 0;
  mockServer = http.createServer((req, res) => {
    if (req.url.startsWith('/quota/')) {
      quotaHits += 1;
      const payload = JSON.stringify({
        model_remains: [{
          model_name: 'general',
          start_time: NOW, end_time: NOW + WINDOW_MS, remains_time: 9_700_000,
          current_interval_remaining_percent: 71, current_interval_status: 1,
          weekly_start_time: NOW, weekly_end_time: NOW + WEEK_MS, weekly_remains_time: 596_000_000,
          current_weekly_remaining_percent: 97, current_weekly_status: 1,
        }],
        base_resp: { status_code: 0, status_msg: 'success' },
      });
      res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) });
      res.end(payload);
      return;
    }
    req.resume();
    req.on('end', () => {
      const sse = [
        { id: 'c', choices: [{ index: 0, delta: { content: 'hi' }, finish_reason: null }] },
        { id: 'c', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] },
      ].map((c) => 'data: ' + JSON.stringify(c) + '\n\n').join('') + 'data: [DONE]\n\n';
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Content-Length': Buffer.byteLength(sse) });
      res.end(sse);
    });
  });
  await new Promise((r) => mockServer.listen(0, '127.0.0.1', r));
  const mockPort = mockServer.address().port;

  fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mini:
    api_url: http://127.0.0.1:${mockPort}
    api_key: sk-test
    supports_usage: true
    usage_type: minimax_token_plan
    usage_url: http://127.0.0.1:${mockPort}/quota/v1/token_plan/remains
    models: {m: {supports_tools: true, max_context_tokens: 1000000}}
  plain:
    api_url: http://127.0.0.1:${mockPort}
    api_key: sk-test
    models: {m: {supports_tools: true, max_context_tokens: 1000000}}
  linky:
    api_url: http://127.0.0.1:${mockPort}
    api_key: sk-test
    usage_console_url: https://example.com/console/usage
    models: {m: {supports_tools: true, max_context_tokens: 1000000}}
`);

  const agentPort = await freePort();
  const cdpPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'ws')], { stdio: 'ignore' });

  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
    `--remote-debugging-port=${cdpPort}`, `--user-data-dir=${path.join(tmp, 'chrome')}`,
    'about:blank'], { stdio: 'ignore' });

  let target = null;
  for (let i = 0; i < 100; i++) {
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
  const waitFor = async (expr, tries = 60) => {
    for (let i = 0; i < tries; i++) {
      const v = await evalJs(expr);
      if (v) return v;
      await sleep(150);
    }
    return null;
  };

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(1200);

  // ---- 1. the badge -------------------------------------------------------
  const badge = await waitFor(`(function(){
    var c = document.getElementById('usage-chip');
    if (!c || c.classList.contains('hidden')) return null;
    return { text: document.getElementById('usage-chip-text').textContent,
             title: c.title, low: c.classList.contains('low') };
  })()`);
  check('the badge shows the remaining quota', !!badge && /额度\s*71%/.test(badge.text || ''),
    badge ? badge.text : 'no badge');
  check('the tooltip explains the number', !!badge && /5 小时窗口剩余 71%/.test(badge.title || ''),
    badge ? badge.title : 'no badge');
  check('the tooltip mentions the weekly window', !!badge && /周窗口剩余 97%/.test(badge.title || ''),
    badge ? badge.title : 'no badge');
  check('the badge is not marked low at 71%', !!badge && badge.low === false,
    badge ? String(badge.low) : 'no badge');

  // ---- 2. clicking asks again --------------------------------------------
  const before = quotaHits;
  await evalJs(`document.getElementById('usage-chip').click()`);
  await sleep(800);
  check('clicking the badge queries again', quotaHits > before, `${before} -> ${quotaHits}`);

  // ---- 3. the settings page can query any provider ------------------------
  await evalJs(`document.querySelector('[data-view="settings"]').click()`);
  await sleep(400);
  const settings = await waitFor(`(function(){
    var cards = Array.from(document.querySelectorAll('#settings-providers .prov'));
    var out = [];
    cards.forEach(function (c) {
      var rows = Array.from(c.querySelectorAll('.row')).map(function (r) { return r.textContent; });
      var res = c.querySelector('.usage-result');
      out.push({ name: c.querySelector('h4').textContent, rows: rows,
                 result: res ? res.textContent : null });
    });
    return out;
  })()`);
  const miniCard = (settings || []).find((c) => c.name.indexOf('mini') === 0);
  check('the settings page lists the quota capability',
    !!miniCard && miniCard.rows.some((r) => r.indexOf('额度查询') >= 0), JSON.stringify(settings));
  check('a provider without the capability gets no quota row',
    !!(settings || []).some((c) => c.name.indexOf('plain') === 0 &&
      !c.rows.some((r) => r.indexOf('额度查询') >= 0)), JSON.stringify(settings));

  // click the 查询 button of the mini card and read the result line
  await evalJs(`(function(){
    var cards = Array.from(document.querySelectorAll('#settings-providers .prov'));
    var card = cards.filter(function (c) { return c.querySelector('h4').textContent.indexOf('mini') === 0; })[0];
    var btn = Array.from(card.querySelectorAll('button')).filter(function (b) { return b.textContent === '查询'; })[0];
    btn.click();
    return true;
  })()`);
  const result = await waitFor(`(function(){
    var cards = Array.from(document.querySelectorAll('#settings-providers .prov'));
    var card = cards.filter(function (c) { return c.querySelector('h4').textContent.indexOf('mini') === 0; })[0];
    var res = card.querySelector('.usage-result');
    var t = res ? res.textContent : '';
    // "查询中…" is the placeholder - wait for the real answer.
    if (!t || t.indexOf('查询中') === 0) return null;
    return t;
  })()`);
  check('the settings page shows the quota of the provider',
    !!result && result.indexOf('5 小时窗口剩余 71%') >= 0, result);

  // ---- 4. a provider without the capability gets no badge -----------------
  await evalJs(`(function(){
    localStorage.setItem('tea_agent_ui_v1', JSON.stringify({ model: { provider: 'plain', model: 'm' } }));
    return true;
  })()`);
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(1500);
  const hidden = await evalJs(`(function(){
    var c = document.getElementById('usage-chip');
    return { hidden: !c || c.classList.contains('hidden'),
             text: document.getElementById('usage-chip-text').textContent };
  })()`);
  check('no badge for a provider without the capability', hidden && hidden.hidden === true,
    JSON.stringify(hidden));

  // ---- 5. a provider that cannot be queried offers its console ----------
  await evalJs(`(function(){
    localStorage.setItem('tea_agent_ui_v1', JSON.stringify({ model: { provider: 'linky', model: 'm' } }));
    return true;
  })()`);
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(1500);
  // The stub has to be installed AFTER the navigation: a reload throws the
  // old JS context (and the stub) away.
  await evalJs(`(function(){
    window.__opened = [];
    window.open = function (url) { window.__opened.push(url); return {}; };
    return true;
  })()`);
  const linkChip = await evalJs(`(function(){
    var c = document.getElementById('usage-chip');
    return { hidden: !c || c.classList.contains('hidden'),
             text: document.getElementById('usage-chip-text').textContent,
             link: c ? c.classList.contains('link') : false,
             title: c ? c.title : '' };
  })()`);
  check('a provider that cannot be queried still gets a badge', linkChip && linkChip.hidden === false,
    JSON.stringify(linkChip));
  check('the badge says where to look', linkChip && /额度/.test(linkChip.text || ''),
    JSON.stringify(linkChip));
  check('the badge explains that the key is not enough',
    linkChip && /查不到|控制台/.test(linkChip.title || ''), JSON.stringify(linkChip));
  const hitsBefore = quotaHits;
  await evalJs(`document.getElementById('usage-chip').click()`);
  await sleep(500);
  const opened = await evalJs(`window.__opened`);
  check('clicking the badge opens the console', Array.isArray(opened) &&
    opened[0] === 'https://example.com/console/usage', JSON.stringify(opened));
  check('a provider without an API is never queried', quotaHits === hitsBefore,
    `${hitsBefore} -> ${quotaHits}`);
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
