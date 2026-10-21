// Browser test for the tab blink: a turn that finishes while the page is in the
// background has to make the tab itself say so.
//
//   node tests/ui_tab_blink.mjs [path-to-tea_agent-binary]
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

// A provider that answers with one short line - the turn ends in a second.
const mock = http.createServer((req, res) => {
  let body = '';
  req.on('data', (d) => (body += d));
  req.on('end', () => {
    if (!req.url.endsWith('/chat/completions')) { res.writeHead(404).end(); return; }
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    res.write('data: ' + JSON.stringify({
      id: 'c', choices: [{ index: 0, delta: { content: 'done' }, finish_reason: 'stop' }],
    }) + '\n\n');
    res.write('data: [DONE]\n\n');
    res.end();
  });
});

const tmp = fs.mkdtempSync(path.join(process.env.TMP || os.tmpdir(), 'tea_blink_'));
const home = path.join(tmp, 'home');
const proj = path.join(tmp, 'proj');
fs.mkdirSync(home, { recursive: true });
fs.mkdirSync(proj, { recursive: true });
fs.writeFileSync(path.join(home, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    default_model: m
    models:
      m: {}
`);

let agent, chrome, ws;
try {
  const mockPort = await freePort();
  await new Promise((res) => mock.listen(mockPort, '127.0.0.1', res));
  fs.writeFileSync(path.join(home, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:${mockPort}/v1
    api_key: sk-test
    default_model: m
    models:
      m: {}
`);

  const agentPort = await freePort();
  agent = spawn(BIN, ['--tea-home', home, '--port', String(agentPort), '--no-console',
    '--log-file', path.join(home, 'logs', 'tea_agent.log'), '--workspace', proj], { stdio: 'ignore' });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  const cdpPort = await freePort();
  chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
    '--window-size=1200,900',
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
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(2000);

  const title = () => evalJs('document.title');
  const before = await title();
  check('the tab has a title', !!before, JSON.stringify(before));

  // The page is "in the background": the window has no focus.
  await send('Emulation.setFocusEmulationEnabled', { enabled: false });
  await sleep(200);

  // Send a message and let the turn finish.
  await evalJs(`(function(){
    var i = document.getElementById('input');
    i.value = 'hello';
    i.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);

  // Sample the title for a few seconds: a blinking title alternates.
  const seen = new Set();
  for (let i = 0; i < 14; i++) {
    seen.add(await title());
    await sleep(250);
  }
  const titles = Array.from(seen);
  check('the title blinks while the tab is in the background', titles.length >= 2,
    JSON.stringify(titles));
  check('one of the blinking titles is marked', titles.some((t) => t.indexOf('●') === 0),
    JSON.stringify(titles));

  // Focus comes back -> the blinking stops.
  await send('Emulation.setFocusEmulationEnabled', { enabled: true });
  await evalJs(`(function(){
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('focus'));
    return true;
  })()`);
  await sleep(1400);
  const a = await title();
  await sleep(1400);
  const b = await title();
  check('the blinking stops when the user comes back', a === b, JSON.stringify([a, b]));
  check('the title is back to the plain one', a.indexOf('●') !== 0, JSON.stringify(a));
} catch (e) {
  check('no exception', false, e && e.stack ? e.stack : String(e));
} finally {
  try { if (ws) ws.close(); } catch {}
  for (const p of [chrome, agent]) {
    try { if (p) p.kill(); } catch {}
  }
  try { mock.close(); } catch {}
  await sleep(300);
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch {}
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length === 0 ? 0 : 1);
