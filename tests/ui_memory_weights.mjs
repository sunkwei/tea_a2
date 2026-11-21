// Browser test for the memory view: weights, caps and the eviction trail.
//
// The contract pinned here (docs/workspace.md, "记忆的权重"):
//   * every row carries its tier and, for anything that fades, its effective
//     weight and how often it was confirmed;
//   * the header says how much of the manual / agent allowance is used;
//   * a person's note comes first, so the top row is a manual one;
//   * the notes that were evicted by the caps are visible under "已挤出".
//
//   node tests/ui_memory_weights.mjs [path-to-tea_agent-binary]
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
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || !detail ? '' : '  ' + detail));
};

const isoDaysAgo = (days) =>
  new Date(Date.now() - days * 86400000).toISOString().replace(/\.\d+Z$/, 'Z');

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_memui_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
const proj = path.join(tmp, 'proj');
const runDir = path.join(proj, '.tea_agent_run');
fs.mkdirSync(path.join(runDir, 'memory'), { recursive: true });

const notes = [
  { id: 'n_manual_0001', text: '手动记忆：回答要简洁清晰', source: 'user',
    weight_base: 1.0, created_at: isoDaysAgo(200), decay_anchor: isoDaysAgo(200) },
  { id: 'n_manual_0002', text: '手动记忆：发版在另一台机器', source: 'user',
    weight_base: 1.0, created_at: isoDaysAgo(20), decay_anchor: isoDaysAgo(20) },
  { id: 'n_strong_0001', text: '强记忆：构建只用 MinGW', source: 'agent',
    weight_base: 0.9, created_at: isoDaysAgo(0), decay_anchor: isoDaysAgo(0),
    merge_count: 2, last_merge_at: isoDaysAgo(1) },
  { id: 'n_medium_0001', text: '中档记忆：导出 HTML 的标题约定', source: 'agent',
    weight_base: 0.9, created_at: isoDaysAgo(9), decay_anchor: isoDaysAgo(9) },
  { id: 'n_weak_0001', text: '弱记忆：早期的机器状态', source: 'agent',
    weight_base: 0.9, created_at: isoDaysAgo(40), decay_anchor: isoDaysAgo(40) },
];
fs.writeFileSync(path.join(runDir, 'memory', 'memory.json'),
  JSON.stringify({ notes, updated_at: isoDaysAgo(0) }, null, 2));
fs.writeFileSync(path.join(runDir, 'memory', 'evicted.jsonl'),
  JSON.stringify({ id: 'n_gone_0001', text: '被挤出的一条旧便签', source: 'user',
    reason: 'manual_cap', evicted_at: isoDaysAgo(2) }) + '\n');

let agent, chrome, ws;
try {
  fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    default_model: m
    models: {m: {}}
`);

  const agentPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', proj], { stdio: 'ignore' });

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

  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('.nav-row'),
      function (x) { return x.dataset.view === 'memory'; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(900);

  const view = () => evalJs(`(function(){
    var rows = Array.prototype.map.call(document.querySelectorAll('#memory-list .note'), function (row) {
      var badge = row.querySelector('.badge');
      var weight = row.querySelector('.note-w');
      return {
        tier: badge ? badge.textContent : '',
        cls: badge ? badge.className : '',
        weight: weight ? weight.textContent : '',
        text: (row.querySelector('.note-text') || {}).textContent || ''
      };
    });
    var ev = document.getElementById('memory-evicted-wrap');
    return {
      hint: (document.getElementById('memory-hint') || {}).textContent || '',
      rows: rows,
      evictedHidden: ev ? ev.hidden : null,
      evictedText: Array.prototype.map.call(
        document.querySelectorAll('#memory-evicted .note-text'),
        function (n) { return n.textContent; }),
      evictedSummary: (document.getElementById('memory-evicted-summary') || {}).textContent || '',
      visible: !document.getElementById('view-memory').classList.contains('hidden')
    };
  })()`);

  const v = await view();
  check('the memory view is open', v && v.visible === true, JSON.stringify(v && v.visible));
  check('every note is listed', v && v.rows.length === 5, JSON.stringify(v && v.rows.length));
  check('the allowance is spelled out',
    v && v.hint.indexOf('手动记忆 2/20') >= 0 && v.hint.indexOf('自动记忆 3/100') >= 0,
    JSON.stringify(v && v.hint));
  check('a person\'s note says it is permanent',
    v && v.rows.some((r) => r.tier === '手动·永久' && r.cls.indexOf('tier-manual') >= 0),
    JSON.stringify(v && v.rows.map((r) => r.tier)));
  check('a fading note shows its effective weight',
    v && v.rows.some((r) => r.tier === '强' && r.weight === 'w 0.90'),
    JSON.stringify(v && v.rows.map((r) => r.weight)));
  check('the tiers are shown as strong / medium / weak',
    v && new Set(v.rows.map((r) => r.tier)).size === 4,
    JSON.stringify(v && v.rows.map((r) => r.tier)));
  check('a confirmed note says how often',
    await evalJs(`document.getElementById('memory-list').textContent.indexOf('合并 2 次') >= 0`));
  check('the person\'s note comes first',
    v && v.rows[0] && v.rows[0].cls.indexOf('tier-manual') >= 0,
    JSON.stringify(v && v.rows[0]));
  check('the evicted notes are reachable',
    v && v.evictedHidden === false && v.evictedText.indexOf('被挤出的一条旧便签') >= 0,
    JSON.stringify({ hidden: v && v.evictedHidden, text: v && v.evictedText }));
  check('the eviction says why it happened',
    v && v.evictedSummary.indexOf('已挤出') === 0 &&
    (await evalJs(`document.getElementById('memory-evicted').textContent.indexOf('手动超过 20 条') >= 0`)),
    JSON.stringify(v && v.evictedSummary));

  // Adding a note through the form shows up in the list.
  await evalJs(`(function(){
    var t = document.getElementById('memory-input');
    t.value = '界面里新加的一条手动记忆';
    document.getElementById('memory-add').click();
    return true;
  })()`);
  await sleep(900);
  const after = await view();
  check('a note added in the UI appears in the list',
    after && after.rows.length === 6 &&
    after.rows.some((r) => r.text === '界面里新加的一条手动记忆'),
    JSON.stringify(after && after.rows.map((r) => r.text)));
  check('the allowance counts it',
    after && after.hint.indexOf('手动记忆 3/20') >= 0, JSON.stringify(after && after.hint));
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
