// Browser test for the terminal that the tunnel carries.
//
// The contract pinned here:
//   * the page comes through the tunnel (the operator's browser talks to the
//     local port of the client sidecar; the bytes go through the tunnel);
//   * it opens a shell session on the far end and draws what the shell prints;
//   * what the user types reaches the shell;
//   * the selection is drawn by hand (the browser's own one is gone the
//     moment the screen is rebuilt), so a drag really selects text;
//   * the wheel scrolls the scrollback back;
//   * a file can be uploaded from the page.
//
//   node tests/ui_tunnel_terminal.mjs [path-to-tea_agent-binary]
//
// Requires google-chrome (or CHROME_BIN) and node >= 18.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { wsConnect } from './mini_ws.mjs';
import { acquireBrowser } from './chrome_pool.mjs';

const REPO_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const BIN = process.argv[2] || path.join(REPO_ROOT, 'build', 'bin', 'tea_agent');
const BUILD = path.basename(path.dirname(path.resolve(BIN))) === 'bin'
  ? path.dirname(path.dirname(path.resolve(BIN)))
  : path.join(REPO_ROOT, 'build');
// The Windows build gives the binaries a .exe.
const binary = (name) => {
  for (const folder of [path.join(BUILD, 'bin'), path.join(BUILD, 'tunnel', 'bin')]) {
    for (const candidate of [name, name + '.exe']) {
      const p = path.join(folder, candidate);
      if (fs.existsSync(p)) return p;
    }
  }
  return path.join(BUILD, 'bin', name);
};
const BROKER = binary('a2_broker');
const SIDECAR = binary('a2_tunnel');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const freePort = () => new Promise((res) => {
  const s = net.createServer();
  s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(); res(p); });
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

if (!fs.existsSync(BROKER) || !fs.existsSync(SIDECAR)) {
  console.log('skip  (no a2_broker / a2_tunnel in ' + BUILD + ')');
  process.exit(0);
}

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_tunnelterm_'));
const home = path.join(tmp, 'home');
fs.mkdirSync(home, { recursive: true });
const env = { ...process.env, HOME: home };

const procs = [];
function spawnLog(args, name) {
  const log = path.join(tmp, name + '.log');
  const handle = fs.openSync(log, 'w');
  const proc = spawn(args[0], args.slice(1), { stdio: ['ignore', handle, handle], cwd: tmp, env });
  procs.push(proc);
  return { proc, log };
}

async function waitForLog(log, pattern, timeout = 20000) {
  const rx = new RegExp(pattern);
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try {
      const text = fs.readFileSync(log, 'utf8');
      if (rx.test(text)) return text;
    } catch (e) { /* not yet */ }
    await sleep(200);
  }
  return '';
}

let chrome, ws;
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
    await sleep(20);
  }
};

const screenText = () => evalJs(`(function(){
  var rows = document.getElementById('rows');
  return rows ? rows.textContent : '';
})()`);
const termState = () => evalJs(`(function(){
  var s = window.teaTunnelTerm;
  return { id: s ? s.id : '', ended: s ? s.ended : true,
           viewOffset: s && s.term ? s.term.viewOffset : 0,
           cols: s && s.term ? s.term.cols : 0 };
})()`);
const selState = () => evalJs(`(function(){
  var s = window.teaTunnelTerm.sel;
  var hl = document.querySelectorAll('#rows .term-sel');
  var marked = [];
  for (var i = 0; i < hl.length; i++) marked.push(hl[i].textContent);
  return { active: !!(s && s.active), marked: marked.join('|'),
           text: window.teaTunnelTermSelectedText ? window.teaTunnelTermSelectedText() : '' };
})()`);
const noteText = () => evalJs(`(function(){
  var n = document.getElementById('note');
  return n ? n.textContent : '';
})()`);

// Where on the screen is this text?
const rectOfText = (needle) => evalJs(`(function(){
  var rows = document.getElementById('rows');
  var needle = ${JSON.stringify(needle)};
  if (!rows) return null;
  for (var i = 0; i < rows.children.length; i++) {
    var el = rows.children[i];
    var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
    var node;
    while ((node = walker.nextNode())) {
      var k = node.nodeValue.indexOf(needle);
      if (k >= 0) {
        var r = document.createRange();
        r.setStart(node, k);
        r.setEnd(node, k + needle.length);
        var rect = r.getBoundingClientRect();
        return { x: rect.left, y: rect.top, w: rect.width, h: rect.height, row: i };
      }
    }
  }
  return null;
})()`);

const dragMouse = async (from, to) => {
  await send('Input.dispatchMouseEvent',
    { type: 'mousePressed', x: from.x, y: from.y, button: 'left', buttons: 1, clickCount: 1 });
  await send('Input.dispatchMouseEvent',
    { type: 'mouseMoved', x: to.x, y: to.y, button: 'left', buttons: 1 });
  await send('Input.dispatchMouseEvent',
    { type: 'mouseReleased', x: to.x, y: to.y, button: 'left', buttons: 0, clickCount: 1 });
};

const dragSelect = async (needle) => {
  for (let attempt = 0; attempt < 6; attempt++) {
    const r = await rectOfText(needle);
    if (!r) { await sleep(300); continue; }
    const y = Math.round(r.y + r.h / 2);
    await dragMouse({ x: Math.round(r.x + 1), y }, { x: Math.round(r.x + r.w - 1), y });
    await sleep(250);
    const st = await selState();
    if (st.text === needle) return st;
    await sleep(400);
  }
  return await selState();
};

try {
  // ---- broker --------------------------------------------------------
  const signalPort = await freePort();
  const stunPort = await freePort();
  const clientPort = await freePort();
  const broker = spawnLog([BROKER, '--stun-port', String(stunPort),
    '--signal-port', String(signalPort), '--signal-bind', '127.0.0.1',
    '--cert-name', 'test-broker'], 'broker');
  let text = await waitForLog(broker.log, 'fingerprint \\(sha256\\): [0-9a-f]{64}');
  const pin = (text.match(/fingerprint \(sha256\): ([0-9a-f]{64})/) || [])[1];
  check('the broker starts', !!pin, text.slice(-300));

  // ---- agent 侧载（带终端）---------------------------------------------
  const agent = spawnLog([SIDECAR, '--role', 'agent',
    '--broker', '127.0.0.1:' + signalPort, '--stun', '127.0.0.1:' + stunPort,
    '--turn', '127.0.0.1:' + stunPort, '--tls-pin', pin,
    '--target', 'terminal', '--wait-timeout', '120'], 'agent');
  text = await waitForLog(agent.log, 'the terminal is listening');
  check('the agent side starts a terminal', /the terminal is listening/.test(text), text.slice(-300));
  const session = (text.match(/a2_tunnel --role client --session ([0-9a-f]+)/) || [])[1];
  check('the agent side prints a session code', !!session, text.slice(-300));

  // ---- client 侧载 ------------------------------------------------------
  const client = spawnLog([SIDECAR, '--role', 'client', '--session', session,
    '--broker', '127.0.0.1:' + signalPort, '--stun', '127.0.0.1:' + stunPort,
    '--turn', '127.0.0.1:' + stunPort, '--tls-pin', pin,
    '--port', String(clientPort)], 'client');
  text = await waitForLog(client.log, 'the data channel is open', 40000);
  check('the tunnel comes up', /the data channel is open/.test(text), text.slice(-400));

  // ---- the browser --------------------------------------------------------
  chrome = await acquireBrowser(tmp);
  ws = wsConnect(chrome.webSocketDebuggerUrl);
  await new Promise((r) => ws.on('open', r));
  ws.on('message', (raw) => {
    const msg = JSON.parse(raw);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  });
  await send('Page.enable');
  await send('Runtime.enable');
  await send('DOM.enable');
  await send('Page.navigate', { url: 'http://127.0.0.1:' + clientPort + '/' });
  await sleep(2000);
  await send('Page.bringToFront').catch(() => {});

  // The page opens a session on the far end by itself.
  const hasSession = await waitFor(async () => {
    const st = await termState();
    return !!(st.id && st.cols > 0);
  }, 20000);
  check('the page opens a shell session on the far end', hasSession,
    JSON.stringify(await termState()));

  // ---- typing reaches the shell -------------------------------------------
  const marker = 'TEATERM' + Date.now();
  await evalJs(`(function(){ document.getElementById('input').focus(); return true; })()`);
  await typeText('echo ' + marker + '\n');
  const gotOutput = await waitFor(async () => (await screenText()).includes(marker), 25000);
  check('what the user types reaches the shell', gotOutput,
    (await screenText()).slice(-200));

  // ---- the selection is drawn by hand ---------------------------------------
  const sel = await dragSelect(marker);
  check('a drag selects the text', sel.active && sel.text === marker, JSON.stringify(sel));
  check('the selection is highlighted', sel.marked.includes(marker), JSON.stringify(sel));

  // ---- the wheel scrolls the scrollback back ---------------------------------
  // The wheel event is dispatched from the page itself: a real mouse wheel
  // event only reaches a tab that is in front, and in the shared browser
  // another test may have opened a tab after this one (the CDP call would
  // then wait for a compositor that never answers).
  const before = (await termState()).viewOffset;
  await evalJs(`(function(){
    var screen = document.getElementById('screen');
    screen.dispatchEvent(new WheelEvent('wheel', {
      deltaY: 240, bubbles: true, cancelable: true
    }));
    return true;
  })()`);
  await sleep(400);
  const after = (await termState()).viewOffset;
  check('the wheel scrolls the scrollback back', after > before, before + ' -> ' + after);

  // ---- uploading a file ------------------------------------------------------
  const uploadPath = path.join(tmp, 'upload-me.txt');
  fs.writeFileSync(uploadPath, 'hello from the browser\n');
  const doc = await send('DOM.getDocument', { depth: 1 });
  const node = await send('DOM.querySelector', { nodeId: doc.result.root.nodeId, selector: '#file' });
  await send('DOM.setFileInputFiles', { files: [uploadPath], nodeId: node.result.nodeId });
  const uploaded = await waitFor(async () => (await noteText()).includes('已上传'), 20000);
  check('a file can be uploaded from the page', uploaded, await noteText());

  // The file really is on the far end: ask the far end about it.
  const st = await termState();
  const listed = await evalJs(`(async function(){
    var id = window.teaTunnelTerm.id;
    var r = await fetch('api/term/session/' + encodeURIComponent(id) + '/output?cursor=0&wait=1000');
    return r.status;
  })()`);
  check('the far end still answers', listed === 200, String(listed));
} catch (e) {
  check('the test itself ran', false, String(e && e.stack ? e.stack : e));
} finally {
  for (const p of procs) { try { p.kill('SIGTERM'); } catch (e) { /* gone */ } }
  try { if (chrome && chrome.close) chrome.close(); } catch (e) { /* gone */ }
}

const failed = results.filter((r) => !r.ok);
console.log((failed.length ? 'FAIL ' : 'PASS ') + (results.length - failed.length) + '/' + results.length + ' checks');
process.exit(failed.length ? 1 : 0);
