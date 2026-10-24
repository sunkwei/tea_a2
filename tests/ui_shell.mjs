// Browser test for the web terminal (the shell on this machine).
//
// The contract pinned here:
//   * the terminal entry appears in the sidebar only when the server says it
//     can provide one (TEA_AGENT_SHELL=off hides it again);
//   * opening it starts a shell session on this machine;
//   * what the user types reaches the shell, and what the shell prints comes
//     back and is drawn on the screen;
//   * the keys are translated the way a terminal does it (Enter is a CR,
//     Ctrl+C is 0x03, the arrow keys are escape sequences);
//   * the emulator draws what a terminal would draw (colours, cursor moves,
//     clearing the screen);
//   * "clear" wipes the picture, "end" closes the session.
//
//   node tests/ui_shell.mjs [path-to-tea_agent-binary]
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

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_shellui_'));
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

let agent, agentOff, chrome, ws;

async function startAgent(env) {
  const port = await freePort();
  const proc = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', home,
    '--port', String(port), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', proj], { stdio: 'ignore', cwd: tmp, env: { ...process.env, ...(env || {}) } });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(port, '/api/health')).status === 200) return { proc, port }; } catch {}
    await sleep(150);
  }
  throw new Error('agent did not start');
}

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

// A real key press (the page sees a trusted event, like a human typing).
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

const screenText = () => evalJs(`(function(){
  var rows = document.getElementById('shell-rows');
  return rows ? rows.textContent : '';
})()`);
const shellState = () => evalJs(`(function(){
  var s = window.teaShell;
  return { id: s ? s.id : '', ended: s ? s.ended : true, cursor: s ? s.cursor : 0,
           cols: s && s.term ? s.term.cols : 0, rows: s && s.term ? s.term.rows : 0 };
})()`);
const navVisible = () => evalJs(`(function(){
  var n = document.getElementById('nav-shell');
  return !!n && !n.classList.contains('hidden');
})()`);
const openTerminal = () => evalJs(`(function(){
  document.getElementById('nav-shell').click();
  return true;
})()`);

try {
  const started = await startAgent();
  agent = started.proc;
  const agentPort = started.port;

  // ---- the server's answer about the terminal --------------------------
  const cap = JSON.parse((await getJson(agentPort, '/api/shell')).body);
  check('the server says a shell is available', cap.available === true, JSON.stringify(cap));
  check('it names the shell program', !!cap.shell, JSON.stringify(cap));

  // ---- the browser ----------------------------------------------------
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
  await sleep(1500);

  check('the terminal entry is in the sidebar', await navVisible());
  await openTerminal();
  await sleep(600);
  const viewOpen = await evalJs(`!document.getElementById('view-shell').classList.contains('hidden')`);
  check('the terminal view opens', viewOpen === true);

  const started2 = await waitFor(async () => (await shellState()).id !== '');
  check('a shell session is started', started2, JSON.stringify(await shellState()));

  // ---- the shell really runs ------------------------------------------
  await evalJs(`document.getElementById('shell-input').focus(); true`);
  const marker = 'tea-ui-marker';
  // The command *builds* the marker, so the echoed line does not contain it:
  // what the test looks for is what the shell printed, not the echo (the echo
  // scrolls out of the screen when the output is long).
  const cmd = process.platform === 'win32'
    ? "Write-Output ('tea' + '-ui-marker')"
    : "echo tea'-'ui-marker";
  await typeText(cmd);
  await pressKey('Enter', { code: 'Enter', vk: 13, text: '\r' });
  // The typed line is echoed by the shell, and then the command prints it
  // again: wait for both, otherwise the test would race with the output.
  const countMarker = async () => {
    const text = await screenText();
    return text.split(marker).length - 1;
  };
  const printed = await waitFor(async () => (await countMarker()) >= 1, 30000, 400);
  check('the shell prints what was typed', printed,
    JSON.stringify((await screenText()).slice(-200)));

  // ---- the keys are translated the way a terminal does -----------------
  const keys = await evalJs(`(function(){
    var f = window.teaShellKeyBytes;
    if (!f) return null;
    function ev(o){ return { key: o.key, ctrlKey: !!o.ctrl, altKey: !!o.alt, metaKey: !!o.meta }; }
    return {
      enter: f(ev({key:'Enter'})),
      backspace: f(ev({key:'Backspace'})),
      tab: f(ev({key:'Tab'})),
      up: f(ev({key:'ArrowUp'})),
      ctrlC: f(ev({key:'c', ctrl:true})),
      ctrlD: f(ev({key:'d', ctrl:true})),
      altB: f(ev({key:'b', alt:true})),
      plain: f(ev({key:'x'}))
    };
  })()`);
  check('Enter becomes a carriage return', keys && keys.enter === '\r', JSON.stringify(keys));
  check('Backspace becomes DEL', keys && keys.backspace === '\x7f', JSON.stringify(keys));
  check('Tab becomes a tab', keys && keys.tab === '\t', JSON.stringify(keys));
  check('the arrow keys become escape sequences',
    keys && keys.up === '\x1b[A', JSON.stringify(keys));
  check('Ctrl+C becomes 0x03', keys && keys.ctrlC === '\x03', JSON.stringify(keys));
  check('Ctrl+D becomes 0x04', keys && keys.ctrlD === '\x04', JSON.stringify(keys));
  check('Alt+key is ESC plus the key', keys && keys.altB === '\x1bb', JSON.stringify(keys));
  check('a plain key is itself', keys && keys.plain === 'x', JSON.stringify(keys));

  // ---- the emulator draws what a terminal would draw -------------------
  const drawn = await evalJs(`(function(){
    var T = window.teaTermScreen;
    if (!T) return null;
    var t = new T(20, 4);
    t.feed('\\x1b[31mred\\x1b[0m plain');
    var line = t.lines[0].map(function (c) { return c.c; }).join('');
    var color = t.lines[0][0].s.fg;
    // a cursor move, then a write
    t.feed('\\r\\nX');
    return { line: line, fg: color, second: t.lines[1][0].c };
  })()`);
  check('the emulator writes the text', drawn && drawn.line.indexOf('red plain') === 0,
    JSON.stringify(drawn));
  check('the emulator keeps the colour', drawn && drawn.fg === 1, JSON.stringify(drawn));
  check('the emulator moves the cursor', drawn && drawn.second === 'X', JSON.stringify(drawn));

  // ---- files in and out ------------------------------------------------
  // The upload goes through the real file input, so the whole path is
  // exercised: pick a file, the bytes go to the machine, the path comes back.
  const uploadName = 'tea-ui-upload.txt';
  const uploadPath = path.join(tmp, uploadName);
  fs.writeFileSync(uploadPath, 'tea-ui-upload-content');
  await send('DOM.enable');
  const doc = await send('DOM.getDocument');
  const node = await send('DOM.querySelector',
    { nodeId: doc.result.root.nodeId, selector: '#shell-file' });
  await send('DOM.setFileInputFiles', { files: [uploadPath], nodeId: node.result.nodeId });
  const arrived = await waitFor(() => fs.existsSync(path.join(tmp, uploadName)), 20000, 300);
  check('the picked file lands on the machine', arrived,
    fs.existsSync(path.join(tmp, uploadName)) ? '' : fs.readdirSync(tmp).join(','));
  if (arrived) {
    check('it has the bytes it was given',
      fs.readFileSync(path.join(tmp, uploadName), 'utf-8') === 'tea-ui-upload-content');
  }

  // Downloading: the browser has to end up with the file.
  const dlDir = path.join(tmp, 'downloads');
  fs.mkdirSync(dlDir, { recursive: true });
  try {
    await send('Browser.setDownloadBehavior',
      { behavior: 'allow', downloadPath: dlDir, eventsEnabled: true });
  } catch (e) { /* the check below tells us whether it worked */ }
  await evalJs(`(function(){
    document.getElementById('shell-path').value = ${JSON.stringify(uploadName)};
    document.getElementById('shell-download').click();
    return true;
  })()`);
  const downloaded = await waitFor(() => fs.existsSync(path.join(dlDir, uploadName)), 20000, 300);
  check('the browser downloads the file', downloaded, fs.readdirSync(dlDir).join(','));
  if (downloaded) {
    check('the downloaded file has the right bytes',
      fs.readFileSync(path.join(dlDir, uploadName), 'utf-8') === 'tea-ui-upload-content');
  }

  // ---- clear and end --------------------------------------------------
  await evalJs(`document.getElementById('shell-clear').click(); true`);
  await sleep(1500);
  const afterClear = await screenText();
  check('"clear" wipes the screen', afterClear.indexOf(marker) < 0, afterClear.slice(-120));

  await evalJs(`document.getElementById('shell-stop').click(); true`);
  await sleep(400);
  const stopped = await shellState();
  check('"end" closes the session', stopped.id === '', JSON.stringify(stopped));

  // ---- a server without a terminal hides the entry ---------------------
  const off = await startAgent({ TEA_AGENT_SHELL: 'off' });
  agentOff = off.proc;
  const capOff = JSON.parse((await getJson(off.port, '/api/shell')).body);
  check('TEA_AGENT_SHELL=off turns the terminal off', capOff.available === false,
    JSON.stringify(capOff));
  check('it says why', !!capOff.reason, JSON.stringify(capOff));

  await send('Page.navigate', { url: `http://127.0.0.1:${off.port}/` });
  await sleep(1500);
  check('the terminal entry is hidden when there is none', (await navVisible()) === false);
} catch (e) {
  check('the test ran to the end', false, e && e.message ? e.message : String(e));
} finally {
  for (const p of [agent, agentOff]) {
    if (!p) continue;
    try { p.kill(); } catch {}
  }
  if (ws) { try { ws.close(); } catch {} }
  if (chrome) { try { await chrome.kill(); } catch {} }
}

console.log();
console.log(`${results.filter((r) => r.ok).length}/${results.length} checks passed`);
const failed = results.filter((r) => !r.ok).map((r) => r.name);
if (failed.length) {
  console.log('FAILED: ' + failed.join(', '));
  process.exit(1);
}
