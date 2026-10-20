// Browser test for the agent.md panel in the memory view.
//
// The contract pinned here:
//   * the panel shows the workspace's agent.md, with its path and byte count;
//   * editing marks it "unsaved" and the revert button puts the server copy back;
//   * saving writes it, and the server (and therefore the next turn's prompt)
//     sees the new content;
//   * scripts under .tea_agent_run/scripts/ are listed next to the editor;
//   * switching to a workspace with a different document reloads the panel.
//
//   node tests/ui_agent_doc.mjs [path-to-tea_agent-binary]
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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_agentdoc_ui_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj_a'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj_b'), { recursive: true });

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
  const cdpPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj_a'), '--workspace', path.join(tmp, 'proj_b')], { stdio: 'ignore' });

  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  // A reusable script for proj_a, so the panel has something to list.
  const scriptsA = path.join(tmp, 'proj_a', '.tea_agent_run', 'scripts');
  fs.mkdirSync(scriptsA, { recursive: true });
  fs.writeFileSync(path.join(scriptsA, 'smoke.sh'), '#!/bin/sh\necho hi\n');

  chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
    '--window-size=1440,900',
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
  await sleep(1800);

  const openMemory = `(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('.nav-row'),
      function (x) { return x.dataset.view === 'memory'; })[0];
    if (b) b.click();
    return true;
  })()`;

  // Go to the memory view (the agent.md card lives at its top).
  await evalJs(openMemory);
  await sleep(900);

  const panel = () => evalJs(`(function(){
    var ed = document.getElementById('agent-doc-editor');
    return {
      value: ed.value,
      path: document.getElementById('agent-doc-path').textContent,
      meta: document.getElementById('agent-doc-meta').textContent,
      dirty: document.getElementById('agent-doc-meta').classList.contains('dirty'),
      scripts: Array.prototype.map.call(
        document.querySelectorAll('#agent-doc-scripts .s-nm'), function (n) { return n.textContent; }),
      visible: !document.getElementById('view-memory').classList.contains('hidden')
    };
  })()`);

  let p = await panel();
  check('the memory view shows the agent.md card', p && p.visible === true, JSON.stringify(p));
  check('the editor is seeded from the server document',
    p && p.value.indexOf('## Pitfalls') >= 0, JSON.stringify(p && p.value.slice(0, 60)));
  check('the path is relative to the workspace',
    p && p.path === '.tea_agent_run/agent.md', JSON.stringify(p && p.path));
  check('the script is listed', p && p.scripts.indexOf('smoke.sh') >= 0, JSON.stringify(p && p.scripts));

  // Edit -> dirty -> revert.
  await evalJs(`(function(){
    var ed = document.getElementById('agent-doc-editor');
    ed.value = ed.value + '\\n- a hand written line\\n';
    ed.dispatchEvent(new Event('input'));
    return true;
  })()`);
  await sleep(200);
  p = await panel();
  check('editing marks the document as unsaved', p && p.dirty === true && p.meta === '未保存',
    JSON.stringify(p && [p.dirty, p.meta]));
  await evalJs(`document.getElementById('agent-doc-revert').click(); true`);
  await sleep(200);
  p = await panel();
  check('revert restores the server copy',
    p && p.dirty === false && p.value.indexOf('a hand written line') < 0,
    JSON.stringify(p && p.value.slice(-40)));

  // Edit -> save.
  const marker = 'ui-test-marker-' + Date.now();
  await evalJs(`(function(){
    var ed = document.getElementById('agent-doc-editor');
    ed.value = '# agent.md\\n\\n## Pitfalls\\n- ${marker}\\n';
    ed.dispatchEvent(new Event('input'));
    document.getElementById('agent-doc-save').click();
    return true;
  })()`);
  await sleep(900);
  p = await panel();
  check('saving clears the dirty flag', p && p.dirty === false, JSON.stringify(p && [p.dirty, p.meta]));
  const onDisk = fs.readFileSync(path.join(tmp, 'proj_a', '.tea_agent_run', 'agent.md'), 'utf8');
  check('the saved document is on disk', onDisk.indexOf(marker) >= 0, onDisk);
  const api = JSON.parse((await getJson(agentPort, '/api/workspaces')).body);
  const widA = api.workspaces.filter((w) => w.path.endsWith('proj_a'))[0].id;
  const fetched = JSON.parse((await getJson(agentPort, `/api/workspaces/${widA}/agent-doc`)).body);
  check('the API serves the saved document', fetched.content.indexOf(marker) >= 0, fetched.content);

  // Switching workspace reloads the panel with that workspace's document.
  await evalJs(`(function(){
    document.getElementById('ws-chip').click();
    return true;
  })()`);
  await sleep(300);
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('#ws-dropdown .dd-item'),
      function (x) { return x.textContent.indexOf('proj_b') >= 0; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(1200);
  await evalJs(openMemory);
  await sleep(900);
  p = await panel();
  check('another workspace shows its own (fresh) document',
    p && p.value.indexOf(marker) < 0 && p.value.indexOf('## Pitfalls') >= 0,
    JSON.stringify(p && p.value.slice(0, 60)));
  check('the scripts of the other workspace are not listed',
    p && p.scripts.length === 0, JSON.stringify(p && p.scripts));
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
