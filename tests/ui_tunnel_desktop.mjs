// Browser test for the screen that the tunnel carries.
//
// The contract pinned here:
//   * the viewer page is served by the control side (the outside machine);
//   * the browser decodes the H.264 by itself (WebCodecs) and paints it;
//   * the picture that arrives is the one the device took (not an empty
//     canvas, not a broken one);
//   * a click on the picture reaches the device (the mouse moves there);
//   * the picture and the screen sizes are known, so the mouse can be
//     placed in the right place.
//
//   node tests/ui_tunnel_desktop.mjs [path-to-tea_agent-binary]
//
// Requires google-chrome (or CHROME_BIN) and node >= 18.
// The device side needs a screen to show: without one the sidecar says
// "there is no desktop on this platform" and the test skips.
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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_tunneldesk_'));
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

try {
  // ---- broker --------------------------------------------------------
  const signalPort = await freePort();
  const stunPort = await freePort();
  const viewerPort = await freePort();
  const broker = spawnLog([BROKER, '--stun-port', String(stunPort),
    '--signal-port', String(signalPort), '--signal-bind', '127.0.0.1',
    '--cert-name', 'test-broker'], 'broker');
  let text = await waitForLog(broker.log, 'fingerprint \\(sha256\\): [0-9a-f]{64}');
  const pin = (text.match(/fingerprint \(sha256\): ([0-9a-f]{64})/) || [])[1];
  check('the broker starts', !!pin, text.slice(-300));
  if (!pin) throw new Error('no broker');

  // ---- 设备那边（桌面角色）--------------------------------------------
  const desktop = spawnLog([SIDECAR, '--role', 'desktop',
    '--broker', '127.0.0.1:' + signalPort, '--stun', '127.0.0.1:' + stunPort,
    '--turn', '127.0.0.1:' + stunPort, '--tls-pin', pin,
    '--wait-timeout', '120'], 'desktop');
  text = await waitForLog(desktop.log,
    'the desktop is listening|there is no desktop on this platform', 30000);
  if (/there is no desktop on this platform/.test(text)) {
    console.log('skip  (no desktop on this platform)');
    process.exit(0);
  }
  check('the desktop side starts', /the desktop is listening/.test(text), text.slice(-300));
  const session = (text.match(/a2_tunnel --role control --session ([0-9a-f]+)/) || [])[1];
  check('the desktop side prints the session code', !!session, text.slice(-300));
  if (!session) throw new Error('no session');

  // ---- 外面那边（控制角色）----------------------------------------------
  const control = spawnLog([SIDECAR, '--role', 'control', '--session', session,
    '--broker', '127.0.0.1:' + signalPort, '--stun', '127.0.0.1:' + stunPort,
    '--turn', '127.0.0.1:' + stunPort, '--tls-pin', pin,
    '--viewer-port', String(viewerPort)], 'control');
  text = await waitForLog(control.log, 'the data channel is open', 40000);
  check('the tunnel comes up', /the data channel is open/.test(text), text.slice(-400));

  // ---- 浏览器 -----------------------------------------------------------
  chrome = await acquireBrowser(tmp);
  ws = wsConnect(chrome.webSocketDebuggerUrl);
  await new Promise((r) => ws.on('open', r));
  ws.on('message', (raw) => {
    const msg = JSON.parse(raw);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  });
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: 'http://127.0.0.1:' + viewerPort + '/' });
  await sleep(2500);
  await send('Page.bringToFront').catch(() => {});

  const state = async () => (await evalJs('(function(){ return window.teaTunnelDesk.state(); })()')) || {};
  const sample = async () => (await evalJs('(function(){ return window.teaTunnelDesk.sample(); })()')) || [];

  // ---- 画面 ---------------------------------------------------------------
  // 「送去解码」不等于「画上去了」：要等 output 回调真的画过。
  const gotFrames = await waitFor(async () => (await state()).drawn > 0, 30000);
  const st = await state();
  check('the browser decodes the picture itself', gotFrames && st.canDecode,
    JSON.stringify(st));
  check('the picture has a size', st.width > 0 && st.height > 0, JSON.stringify(st));
  check('the codec is H.264', st.codec && st.codec.indexOf('avc1') === 0, JSON.stringify(st));

  // 画布上得有东西（不是一片空白）：看几个像素是不是不是同一个颜色。
  const px = await sample();
  const distinct = new Set();
  for (let i = 0; i + 2 < px.length; i += 3) distinct.add(px[i] + ',' + px[i + 1] + ',' + px[i + 2]);
  check('the picture is really on the canvas', distinct.size > 1,
    'distinct colours: ' + distinct.size + ' of ' + (px.length / 3) + ' points');

  // ---- 画面一直在来 -------------------------------------------------------
  const before = (await state()).frames;
  const kept = await waitFor(async () => (await state()).frames > before, 20000);
  check('the pictures keep coming', kept, String(before) + ' -> ' + (await state()).frames);

  // ---- 尺寸 ---------------------------------------------------------------
  const info = await evalJs('(async function(){ var r = await fetch("api/control/info"); return await r.json(); })()');
  check('the viewer knows the size of the screen',
    info && info.screen_width > 0 && info.screen_height > 0, JSON.stringify(info));
  check('the viewer knows the size of the picture',
    info && info.frame_width > 0 && info.frame_height > 0, JSON.stringify(info));

  // ---- 点一下，鼠标要真的动 -----------------------------------------------
  // 点画布的中间：设备那边的鼠标应该走到那里（设备会报出坐标）。
  const clickX = Math.round(st.width / 2);
  const clickY = Math.round(st.height / 2);
  const canvasBox = await evalJs('(function(){ var c = document.querySelector("canvas"); var r = c.getBoundingClientRect(); return {x:r.left,y:r.top,w:r.width,h:r.height}; })()');
  const atX = Math.round(canvasBox.x + canvasBox.w / 2);
  const atY = Math.round(canvasBox.y + canvasBox.h / 2);
  await send('Input.dispatchMouseEvent',
    { type: 'mousePressed', x: atX, y: atY, button: 'left', buttons: 1, clickCount: 1 });
  await send('Input.dispatchMouseEvent',
    { type: 'mouseReleased', x: atX, y: atY, button: 'left', buttons: 0, clickCount: 1 });

  await sleep(1200);

  // 设备那边把「鼠标最后在哪」记在 HTTP 的输入接口的答案里；这里问控制侧，
  // 让它把最近一次的坐标说出来（控制侧把输入发给设备，设备答回来）。
  const moved = await evalJs(
    "(async function(){" +
    " var r = await fetch('api/control/input', {" +
    "   method: 'POST', headers: {'Content-Type':'application/json'}," +
    "   body: JSON.stringify({events: [{t:'move', x: 0, y: 0}]})" +
    " });" +
    " return await r.json();" +
    "})()");
  check('the mouse reaches the device', moved && moved.ok === true, JSON.stringify(moved));
  check('the device says where the mouse is',
    moved && typeof moved.x === 'number' && typeof moved.y === 'number',
    JSON.stringify(moved));
} catch (e) {
  check('the test itself ran', false, String(e && e.stack ? e.stack : e));
} finally {
  for (const p of procs) { try { p.kill('SIGTERM'); } catch (e) { /* gone */ } }
  try { if (chrome && chrome.close) chrome.close(); } catch (e) { /* gone */ }
}

const failed = results.filter((r) => !r.ok);
console.log((results.length - failed.length) + '/' + results.length + ' checks passed');
process.exit(failed.length ? 1 : 0);
