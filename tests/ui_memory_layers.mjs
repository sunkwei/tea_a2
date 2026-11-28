// Browser test for the knowledge pages (the topic layer of the memory).
//
// The contract pinned here (docs/memory-design.md):
//   * the memory view lists the pages of this workspace, with their title and
//     their description;
//   * clicking a page opens it in the editor;
//   * saving writes the file (the agent writes the same files with the file
//     tools - a person and the agent share them);
//   * a page can be deleted.
//
//   node tests/ui_memory_layers.mjs [path-to-tea_agent-binary]
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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_knowui_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
const proj = path.join(tmp, 'proj');
const runDir = path.join(proj, '.tea_agent_run');
fs.mkdirSync(path.join(runDir, 'knowledge'), { recursive: true });
fs.writeFileSync(path.join(runDir, 'knowledge', 'mqtt-broker.md'),
  '# MQTT Broker\n\nThe broker talks to the devices.\n\nSee also [[Zone]].\n');
fs.writeFileSync(path.join(runDir, 'knowledge', 'zone.md'),
  '# Zone\n\nA zone belongs to the device.\n');
fs.writeFileSync(path.join(runDir, 'knowledge', 'index.md'),
  '# 目录\n\n- MQTT Broker: what the broker does\n- Zone: what a zone is\n');

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
  await sleep(1200);

  // ---- 1. the pages are listed -----------------------------------------
  const list = await evalJs(`(function(){
    return Array.prototype.map.call(document.querySelectorAll('#knowledge-list .note'),
      function (row) { return (row.querySelector('.note-text') || {}).textContent || ''; });
  })()`);
  // Three pages: the two the agent wrote, and the catalog (index.md) - it is a
  // page too, and a person may want to fix the descriptions in it.
  check('the memory view lists the knowledge pages',
    Array.isArray(list) && list.length === 3, JSON.stringify(list));
  check('the pages are listed by their title',
    Array.isArray(list) && list.indexOf('MQTT Broker') >= 0 && list.indexOf('Zone') >= 0 &&
    list.indexOf('目录') >= 0, JSON.stringify(list));
  const indexMarked = await evalJs(`(function(){
    var rows = document.querySelectorAll('#knowledge-list .note');
    for (var i = 0; i < rows.length; i++) {
      var badge = rows[i].querySelector('.badge');
      if (badge && badge.textContent === '目录') return true;
    }
    return false;
  })()`);
  check('the catalog is marked as the catalog', indexMarked === true, String(indexMarked));

  const described = await evalJs(`(function(){
    var rows = document.querySelectorAll('#knowledge-list .note');
    var out = [];
    for (var i = 0; i < rows.length; i++) {
      var texts = rows[i].querySelectorAll('.note-text');
      out.push(texts.length > 1 ? texts[1].textContent : '');
    }
    return out;
  })()`);
  check('the description from index.md is shown',
    Array.isArray(described) && described.join('|').indexOf('what the broker does') >= 0,
    JSON.stringify(described));

  // ---- 2. clicking a page opens it --------------------------------------
  await evalJs(`(function(){
    var names = document.querySelectorAll('#knowledge-list .note .note-text');
    for (var i = 0; i < names.length; i++) {
      if (names[i].textContent === 'MQTT Broker') { names[i].click(); return true; }
    }
    return false;
  })()`);
  await sleep(700);
  const opened = await evalJs(`(function(){
    return {
      content: (document.getElementById('knowledge-editor') || {}).value || '',
      slug: (document.getElementById('knowledge-slug') || {}).value || '',
      path: (document.getElementById('knowledge-path') || {}).textContent || ''
    };
  })()`);
  check('clicking a page opens it in the editor',
    opened.content.indexOf('The broker talks to the devices') >= 0, JSON.stringify(opened));
  check('the page name is in the file name field', opened.slug === 'mqtt-broker', opened.slug);

  // ---- 3. saving writes the file ----------------------------------------
  await evalJs(`(function(){
    var ed = document.getElementById('knowledge-editor');
    ed.value = ed.value + '\\nEdited by a person.\\n';
    ed.dispatchEvent(new Event('input'));
    document.getElementById('knowledge-save').click();
    return true;
  })()`);
  await sleep(900);
  const saved = fs.readFileSync(path.join(runDir, 'knowledge', 'mqtt-broker.md'), 'utf8');
  check('saving writes the page to disk', saved.indexOf('Edited by a person.') >= 0, saved.slice(0, 120));

  // ---- 4. a new page ------------------------------------------------------
  await evalJs(`(function(){
    document.getElementById('knowledge-new').click();
    document.getElementById('knowledge-slug').value = 'new-page';
    document.getElementById('knowledge-editor').value = '# New Page\\n\\nA page a person wrote.\\n';
    document.getElementById('knowledge-save').click();
    return true;
  })()`);
  await sleep(900);
  const created = fs.existsSync(path.join(runDir, 'knowledge', 'new-page.md'));
  check('a new page is written', created, String(created));

  // ---- 5. deleting a page -------------------------------------------------
  await evalJs(`(function(){
    document.getElementById('knowledge-slug').value = 'new-page';
    document.getElementById('knowledge-delete').click();
    return true;
  })()`);
  await sleep(900);
  const gone = !fs.existsSync(path.join(runDir, 'knowledge', 'new-page.md'));
  check('a page can be deleted', gone, String(gone));

  const after = await evalJs(`(function(){
    return Array.prototype.map.call(document.querySelectorAll('#knowledge-list .note'),
      function (row) { return (row.querySelector('.note-text') || {}).textContent || ''; });
  })()`);
  check('the list is refreshed after the delete',
    Array.isArray(after) && after.indexOf('New Page') < 0, JSON.stringify(after));

} catch (e) {
  check('the test ran', false, String(e && e.message ? e.message : e));
} finally {
  try { if (ws) ws.close(); } catch {}
  try { if (chrome) await chrome.close(); } catch {}
  try { if (agent) agent.kill(); } catch {}
}

const failed = results.filter((r) => !r.ok);
console.log('');
console.log(results.length + ' checks, ' + failed.length + ' failed');
for (const f of failed) console.log('  FAILED: ' + f.name);
process.exit(failed.length ? 1 : 0);
