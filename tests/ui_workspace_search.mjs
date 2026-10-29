// Browser test for the 文件 page's search box: the workspace search must find
// files by name and by content (a literal, complete string match), show the
// matching lines, and open the file that was clicked.
//
//   node tests/ui_workspace_search.mjs [path-to-tea_agent-binary]
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
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? '  ' + detail : ''));
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_wsearch_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj', 'src'), { recursive: true });
fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:1
    api_key: sk-test
    default_model: m
    models: {m: {}}
`);
// Files with something recognisable in them.
fs.writeFileSync(path.join(tmp, 'proj', 'src', 'main.cpp'),
  'int main() {\n  // ZEBRAFISH marker\n  return 0;\n}\n');
fs.writeFileSync(path.join(tmp, 'proj', 'notes.txt'), 'nothing special here\n');

let agent, chrome, ws;
const pending = new Map();
let msgId = 0;
const send = (method, params) => new Promise((res) => {
  const mid = ++msgId; pending.set(mid, res);
  ws.send({ id: mid, method, params });
});
const evalJs = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.result && r.result.exceptionDetails) {
    throw new Error(JSON.stringify(r.result.exceptionDetails.exception || r.result.exceptionDetails));
  }
  return r.result && r.result.result ? r.result.result.value : undefined;
};
// The app keeps its state in one object; waiting for it is how the other
// browser tests tell "the page is up" from "the page is still booting".
const waitState = async (expr, tries = 60) => {
  for (let i = 0; i < tries; i++) {
    const v = await evalJs(expr);
    if (v) return v;
    await sleep(200);
  }
  return null;
};

try {
  const agentPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj')], { stdio: 'ignore' });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  const cdpPort = await freePort();
  chrome = await acquireBrowser(tmp);
  const target = { webSocketDebuggerUrl: chrome.webSocketDebuggerUrl };
  if (!target) throw new Error('no chrome page target');
  ws = wsConnect(target.webSocketDebuggerUrl);
  await new Promise((r) => ws.on('open', r));
  ws.on('message', (raw) => {
    const m = JSON.parse(raw);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await waitState('window.teaState && window.teaState.workspace ? true : false');

  // ---- the files page ----------------------------------------------------
  await evalJs(`document.querySelector('.nav-row[data-view="files"]').click(); true`);
  await sleep(700);
  const box = await evalJs(`(function(){
    var i = document.getElementById('files-search-input');
    var s = document.getElementById('files-search-scope');
    var g = document.getElementById('files-search-go');
    var c = document.getElementById('files-search-clear');
    var e = document.getElementById('files-search-exact');
    if (!i || !g) return null;
    return { input: !!i, scope: s ? s.value : null, go: !!g, clear: !!c,
             exact: !!e, visible: i.offsetParent !== null };
  })()`);
  check('the files page has a search box', !!box && box.input && box.go, JSON.stringify(box));
  check('the search box is visible', box && box.visible, JSON.stringify(box));
  check('it searches names and content by default', box && box.scope === 'both', JSON.stringify(box));
  check('there is a 完全匹配 switch and a way back',
    box && box.exact && box.clear, JSON.stringify(box));

  // ---- a content search --------------------------------------------------
  await evalJs(`(function(){
    var i = document.getElementById('files-search-input');
    i.value = 'ZEBRAFISH';
    document.getElementById('files-search-go').click();
    return true;
  })()`);
  const found = await waitState(`(function(){
    var list = document.getElementById('files-list');
    if (!list) return null;
    var txt = list.textContent || '';
    // 等结果本身，别把目录列表当成结果（目录里只有 "src"，没有 "src/main.cpp"）。
    if (txt.indexOf('src/main.cpp') < 0) return null;
    return { rows: list.querySelectorAll('.file-row').length, text: txt.slice(0, 400),
             note: document.getElementById('files-note').textContent };
  })()`);
  check('the search finds the file', found && (found.text || '').indexOf('src/main.cpp') >= 0,
    JSON.stringify(found));
  check('the matching line is shown', found && (found.text || '').indexOf('ZEBRAFISH') >= 0,
    JSON.stringify(found));
  check('the note says how many results there are',
    found && (found.note || '').indexOf('个结果') >= 0, found && found.note);

  // ---- the file itself opens --------------------------------------------
  await evalJs(`(function(){
    var rows = document.querySelectorAll('#files-list .file-row');
    for (var i = 0; i < rows.length; i++) {
      if ((rows[i].textContent || '').indexOf('src/main.cpp') >= 0) { rows[i].click(); return true; }
    }
    return false;
  })()`);
  const opened = await waitState(`(function(){
    var h = document.getElementById('files-preview-head');
    var e = document.getElementById('files-editor');
    if (!h || h.textContent.indexOf('main.cpp') < 0) return null;
    return { head: h.textContent, content: e ? e.value : '' };
  })()`);
  check('clicking a result opens the file', !!opened, JSON.stringify(opened));
  check('the file content is in the editor',
    opened && (opened.content || '').indexOf('ZEBRAFISH') >= 0, opened && opened.content);

  // ---- 完全匹配 -----------------------------------------------------------
  await evalJs(`(function(){
    var i = document.getElementById('files-search-input');
    i.value = 'main.cpp';
    document.getElementById('files-search-exact').checked = true;
    document.getElementById('files-search-go').click();
    return true;
  })()`);
  const exact = await waitState(`(function(){
    var list = document.getElementById('files-list');
    var txt = (list && list.textContent) || '';
    if (txt.indexOf('src/main.cpp') < 0) return null;
    return { text: txt.slice(0, 300) };
  })()`);
  check('完全匹配 finds the file by its exact name', !!exact, JSON.stringify(exact));

  await evalJs(`(function(){
    var i = document.getElementById('files-search-input');
    i.value = 'main';
    document.getElementById('files-search-go').click();
    return true;
  })()`);
  let partial = null;
  for (let i = 0; i < 40; i++) {
    await sleep(150);
    partial = await evalJs(`(function(){
      var list = document.getElementById('files-list');
      return (list && list.textContent) || '';
    })()`);
    if (partial && partial.indexOf('没有匹配') >= 0) break;
  }
  check('完全匹配 does not match a part of the name',
    partial && partial.indexOf('没有匹配') >= 0, JSON.stringify(partial));

  // ---- back to the directory ---------------------------------------------
  await evalJs(`document.getElementById('files-search-clear').click(); true`);
  await sleep(400);
  const back = await evalJs(`(function(){
    var list = document.getElementById('files-list');
    var rows = list.querySelectorAll('.file-row');
    return { rows: rows.length, text: (list.textContent || '').slice(0, 200),
             value: document.getElementById('files-search-input').value };
  })()`);
  check('返回目录 brings the directory listing back',
    back && back.rows > 0 && (back.text || '').indexOf('src') >= 0, JSON.stringify(back));
  check('the search box is emptied', back && back.value === '', JSON.stringify(back));
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  if (agent) { agent.kill('SIGTERM'); await sleep(300); }
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
