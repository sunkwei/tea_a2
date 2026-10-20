// Layout regression test for the composer metrics bar.
//
// The bar must stay on ONE line at every practical width. It used to wrap onto
// a second row, which made the composer jump in height; the fix is
// `flex-wrap:nowrap` plus compact labels and width-based hiding. This test
// pins that invariant down, because it is easy to break by adding one more
// metric or a longer label.
//
//   node tests/ui_metrics_layout.mjs [path-to-tea_agent-binary]
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

// Worst-case values for every metric: the widest numbers a real turn produces.
const WORST = {
  'metric-elapsed': '386s',
  'metric-turns': '11',
  'metric-steps': '63',
  'metric-speed': '281 t/s',
  'metric-cache': '100%',
  'metric-context': '233k/1.0M 23%',
  'metric-tokens': '输出 36k·合计 12.1M·首字 1.3s',
};

const WIDTHS = [1440, 1224, 1100, 980, 900, 820, 760, 700, 600];

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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_metrics_'));
fs.mkdirSync(path.join(tmp, 'ws'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });

let agent, chrome, ws, mockServer;
try {
  mockServer = http.createServer((req, res) => {
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

  fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:${mockServer.address().port}
    api_key: sk-test
    default_model: m
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

  await send('Page.enable');
  await send('Runtime.enable');

  const setWorst = `(function(){
    var values = ${JSON.stringify(WORST)};
    Object.keys(values).forEach(function (k) {
      var n = document.getElementById(k + '-v');
      if (n) n.textContent = values[k];
    });
    var cb = document.getElementById('metric-cache-bar'); if (cb) cb.style.width = '100%';
    var xb = document.getElementById('metric-context-bar'); if (xb) xb.style.width = '23%';
    return true;
  })()`;

  for (const width of WIDTHS) {
    await send('Emulation.setDeviceMetricsOverride',
      { width, height: 900, deviceScaleFactor: 1, mobile: false });
    await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
    await sleep(900);
    await evalJs(setWorst);
    await sleep(120);

    const m = await evalJs(`(function(){
      var el = document.getElementById('metrics');
      var cs = getComputedStyle(el);
      var lineH = parseFloat(cs.lineHeight) || 16;
      // A wrapped row is at least two line boxes tall. Height is the honest
      // signal here: comparing children's top offsets is unreliable because
      // centre-aligned items of different heights round differently.
      var rows = Math.round((el.getBoundingClientRect().height) / lineH);
      var tok = document.getElementById('metric-tokens-v');
      var rng = document.createRange();
      rng.selectNodeContents(tok);
      var textW = rng.getBoundingClientRect().width;
      var vis = Array.from(el.children)
        .filter(function (c) { return getComputedStyle(c).display !== 'none'; })
        .map(function (c) { return c.id.replace('metric-', ''); });
      return { wrap: cs.flexWrap, rows: rows, textW: Math.round(textW),
               visW: Math.round(tok.clientWidth), shown: vis };
    })()`);

    check(`vw=${width}: single line`, m && m.rows === 1,
      m ? `rows=${m.rows} wrap=${m.wrap} shown=${m.shown.join(',')}` : 'no data');
    check(`vw=${width}: no metric is clipped`, m && m.textW <= m.visW + 1,
      m ? `text=${m.textW} visible=${m.visW}` : 'no data');
  }

  // The invariant that actually prevents wrapping, independent of content width.
  const wrap = await evalJs(`getComputedStyle(document.getElementById('metrics')).flexWrap`);
  check('metrics never wrap (flex-wrap:nowrap)', wrap === 'nowrap', String(wrap));
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
