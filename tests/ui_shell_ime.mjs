// Browser test: the web terminal has to accept Chinese.
//
// The bug this pins down: the terminal reads the keyboard from a hidden
// <textarea> (the IME needs a real input to compose into) and turns key
// presses into bytes.  While an input method is composing, the browser
// sends keydown events with isComposing=true - there is no character in
// them - and the Chinese text only shows up in compositionend / input.
// The terminal only listened to keydown, so every Chinese word typed in
// the browser went nowhere at all.
//
// The same test pins down the second half of the story: a Chinese
// character is twice as wide as a letter, so it takes two cells and the
// picture has to line up with the cursor.
//
//   node tests/ui_shell_ime.mjs [path-to-tea_agent-binary]
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
  s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(); res(p); });
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
const waitFor = async (fn, timeout = 25000, step = 250) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await fn()) return true;
    await sleep(step);
  }
  return false;
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_shellime_'));
const home = path.join(tmp, 'home');
const proj = path.join(tmp, 'proj');
for (const d of [home, proj]) fs.mkdirSync(d, { recursive: true });
fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    default_model: m
    models: {m: {supports_tools: true}}
`);

let agent, chrome, ws;

const startAgent = async () => {
  const port = await freePort();
  const proc = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', home,
    '--port', String(port), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', proj], { stdio: 'ignore', cwd: tmp, env: process.env });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(port, '/api/health')).status === 200) return { proc, port }; } catch {}
    await sleep(150);
  }
  throw new Error('agent did not start');
};

let id = 0;
const pending = new Map();
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

const pressKey = async (key, opts = {}) => {
  const base = { key, code: opts.code || key, windowsVirtualKeyCode: opts.vk || 0 };
  if (opts.ctrl) base.modifiers = 2;
  if (opts.text !== undefined) base.text = opts.text;
  await send('Input.dispatchKeyEvent', { type: 'keyDown', ...base });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', ...base });
};
const typeText = async (text) => {
  for (const ch of text) {
    await pressKey(ch, { text: ch, vk: ch.toUpperCase().charCodeAt(0) });
    await sleep(15);
  }
};

// The input method: first the pinyin being composed, then the word.
const typeChinese = async (pinyin, word) => {
  await send('Input.imeSetComposition', {
    text: pinyin, selectionStart: pinyin.length, selectionEnd: pinyin.length,
    replacementStart: 0, replacementEnd: 0,
  });
  await sleep(250);
  await send('Input.insertText', { text: word });
  await sleep(400);
};

const screenText = () => evalJs(`(function(){
  var rows = document.getElementById('shell-rows');
  return rows ? rows.textContent : '';
})()`);

try {
  const started = await startAgent();
  agent = started.proc;
  const agentPort = started.port;

  chrome = await acquireBrowser(tmp);
  ws = wsConnect(chrome.webSocketDebuggerUrl);
  await new Promise((r) => ws.on('open', r));
  ws.on('message', (raw) => {
    const msg = JSON.parse(raw);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  });
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(2000);

  await evalJs(`document.getElementById('nav-shell').click(); true`);
  const started3 = await waitFor(async () => {
    const st = await evalJs(`(function(){ var s = window.teaShell; return s && s.id ? s.id : ''; })()`);
    return !!st;
  });
  check('a shell session is started', started3);

  // Watch what the page sends to the shell.
  await evalJs(`(function(){
    window.__sent = [];
    var of = window.fetch;
    window.fetch = function (url, opts) {
      var u = String(url && url.url ? url.url : url);
      if (u.indexOf('/input') >= 0 && opts && opts.body) {
        try { window.__sent.push(JSON.parse(opts.body).data); } catch (e) { window.__sent.push('?'); }
      }
      return of.apply(this, arguments);
    };
    document.getElementById('shell-input').focus();
    return true;
  })()`);

  // ---- 1. the Chinese text has to reach the shell ------------------------
  // The cursor sits behind the prompt, so only the step counts.
  const cxBefore = await evalJs(`(function(){ var t = window.teaShell.term; return t ? t.cx : -1; })()`);
  await typeChinese('nihao', '你好');
  const sent = await evalJs(`JSON.stringify(window.__sent)`);
  const sentList = JSON.parse(sent);
  check('the composed word is sent to the shell', sentList.join('|') === '你好', sent);
  check('the pinyin is not sent', sentList.join('|').indexOf('ni') < 0, sent);

  // ---- 2. the word is not sent twice -------------------------------------
  await sleep(600);
  const sent2 = JSON.parse(await evalJs(`JSON.stringify(window.__sent)`));
  check('the word is sent only once', sent2.length === sentList.length,
    JSON.stringify(sent2));

  // ---- 3. the emulator counts two columns per character ------------------
  const cx = await evalJs(`(function(){ var t = window.teaShell.term; return t ? t.cx : -1; })()`);
  check('two Chinese characters take four columns', cx - cxBefore === 4,
    'cx ' + cxBefore + ' -> ' + cx);

  // ---- 4. the shell really got it ----------------------------------------
  const cursorBefore = await evalJs(`(function(){ return window.teaShell.cursor; })()`);
  await pressKey('Enter', { code: 'Enter', vk: 13, text: '\r' });
  const shown = await waitFor(async () => (await screenText()).indexOf('你好') >= 0, 20000, 300);
  check('the shell echoes the Chinese word', shown, JSON.stringify((await screenText()).slice(-200)));

  const raw = await evalJs(`(async function(){
    var id = window.teaShell.id;
    var r = await fetch('api/shell/session/' + encodeURIComponent(id) +
                        '/output?cursor=' + ${cursorBefore} + '&wait=1000');
    var j = await r.json();
    var bin = atob(j.data_b64 || '');
    var out = '';
    for (var i = 0; i < bin.length; i++) out += String.fromCharCode(bin.charCodeAt(i));
    try { out = decodeURIComponent(escape(out)); } catch (e) {}
    return out;
  })()`);
  check('the shell received the Chinese word', raw.indexOf('你好') >= 0, JSON.stringify(raw));
  check('the shell did not receive the pinyin', raw.indexOf('ni你好') < 0,
    JSON.stringify(raw));

  // ---- 5. the picture lines up -------------------------------------------
  const geom = await evalJs(`(function(){
    var rows = document.getElementById('shell-rows');
    var row = null;
    for (var i = 0; i < rows.children.length; i++) {
      if (rows.children[i].textContent.indexOf('\\u4f60\\u597d') >= 0) { row = rows.children[i]; break; }
    }
    if (!row) return { error: 'the word is not on the screen' };
    var wide = row.querySelectorAll('.term-wide');
    var out = { count: wide.length, charW: window.teaShell.charW, spans: [] };
    for (var i = 0; i < wide.length; i++) {
      var r = wide[i].getBoundingClientRect();
      out.spans.push({ text: wide[i].textContent, x: r.left, w: r.width });
    }
    return out;
  })()`);
  check('the word is drawn in wide boxes', geom.count === 2, JSON.stringify(geom));
  if (geom.count === 2) {
    const cell = geom.charW;
    check('a Chinese character is twice as wide as a letter',
      Math.abs(geom.spans[0].w - 2 * cell) <= 1.5,
      JSON.stringify(geom) + ' cell=' + cell);
    check('the second character starts two cells later',
      Math.abs((geom.spans[1].x - geom.spans[0].x) - 2 * cell) <= 1.5,
      JSON.stringify(geom));
  }

  // ---- 6. the hidden input follows the cursor ----------------------------
  // The input method puts its candidate window where the input is: if the
  // input sits in the corner of the screen, the candidates do too.
  const follow = await evalJs(`(function(){
    var input = document.getElementById('shell-input');
    var cursor = document.getElementById('term-cursor');
    var a = input.getBoundingClientRect();
    var b = cursor.getBoundingClientRect();
    return { dx: Math.round(a.left - b.left), dy: Math.round(a.top - b.top) };
  })()`);
  check('the hidden input sits on the cursor', Math.abs(follow.dx) <= 2 &&
    Math.abs(follow.dy) <= 2, JSON.stringify(follow));

  // ---- 7. the keyboard still works normally ------------------------------
  await evalJs(`document.getElementById('shell-input').focus(); true`);
  await typeText('echo ascii-ok');
  await pressKey('Enter', { code: 'Enter', vk: 13, text: '\r' });
  const ascii = await waitFor(async () => (await screenText()).indexOf('ascii-ok') >= 0, 20000, 300);
  check('ordinary typing still works', ascii, JSON.stringify((await screenText()).slice(-200)));
} catch (e) {
  check('the test itself ran', false, String(e && e.stack ? e.stack : e));
} finally {
  try { if (ws) ws.close(); } catch (e) { /* gone */ }
  try { if (chrome) chrome.kill(); } catch (e) { /* gone */ }
  try { if (agent) agent.kill(); } catch (e) { /* gone */ }
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
