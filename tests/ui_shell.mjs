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

// Where on the screen is this text? (the rectangle of the substring)
const rectOfText = (needle) => evalJs(`(function(){
  var rows = document.getElementById('shell-rows');
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

const clipboardText = () => evalJs(`(async function(){
  try { return await navigator.clipboard.readText(); }
  catch (e) { return 'ERR:' + (e && e.message ? e.message : e); }
})()`);

// A mouse drag (the buttons flag matters: without it the browser does not
// treat the move as a drag).
const dragMouse = async (from, to) => {
  await send('Input.dispatchMouseEvent',
    { type: 'mousePressed', x: from.x, y: from.y, button: 'left', buttons: 1, clickCount: 1 });
  await send('Input.dispatchMouseEvent',
    { type: 'mouseMoved', x: to.x, y: to.y, button: 'left', buttons: 1 });
  await send('Input.dispatchMouseEvent',
    { type: 'mouseReleased', x: to.x, y: to.y, button: 'left', buttons: 0, clickCount: 1 });
};

// The right mouse button (the browser turns it into a contextmenu event).
const rightClick = async (x, y) => {
  await send('Input.dispatchMouseEvent',
    { type: 'mousePressed', x, y, button: 'right', buttons: 2, clickCount: 1 });
  await send('Input.dispatchMouseEvent',
    { type: 'mouseReleased', x, y, button: 'right', buttons: 0, clickCount: 1 });
};

const menuItems = () => evalJs(`(function(){
  var m = document.getElementById('shell-menu');
  if (!m || m.classList.contains('hidden')) return null;
  var out = [];
  for (var i = 0; i < m.children.length; i++) out.push(m.children[i].textContent);
  return out;
})()`);

// One click (count = how many clicks: 2 = double click, 3 = triple click).
const clickMouse = async (x, y, count = 1) => {
  for (let i = 1; i <= count; i++) {
    await send('Input.dispatchMouseEvent',
      { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: i });
    await send('Input.dispatchMouseEvent',
      { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: i });
  }
};

// Drag over a piece of text and wait until the selection really is that
// text (the shell prints on its own, so the picture can move under the
// pointer between measuring and dragging).
const dragSelect = async (needle) => {
  for (let attempt = 0; attempt < 5; attempt++) {
    const r = await rectOfText(needle);
    if (!r) { await sleep(300); continue; }
    const y = Math.round(r.y + r.h / 2);
    await dragMouse({ x: Math.round(r.x + 1), y }, { x: Math.round(r.x + r.w - 1), y });
    await sleep(200);
    const st = await selState();
    if (st.text === needle) return st;
    await sleep(400);
  }
  return await selState();
};

// The same, but with clicks (2 = double click, 3 = triple click).
const clickSelect = async (needle, count) => {
  for (let attempt = 0; attempt < 5; attempt++) {
    const r = await rectOfText(needle);
    if (!r) { await sleep(300); continue; }
    const y = Math.round(r.y + r.h / 2);
    await clickMouse(Math.round(r.x + r.w / 2), y, count);
    await sleep(200);
    const st = await selState();
    if (st.text.indexOf(needle) >= 0) return st;
    await sleep(400);
  }
  return await selState();
};

const selState = () => evalJs(`(function(){
  var s = window.teaShell.sel;
  var hl = document.querySelectorAll('#shell-rows .term-sel');
  var marked = [];
  for (var i = 0; i < hl.length; i++) marked.push(hl[i].textContent);
  return { active: !!(s && s.active), marked: marked.join('|'),
           text: window.teaShellSelText ? window.teaShellSelText() : '' };
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

  // ---- mouse selection and copy ----------------------------------------
  // The terminal draws its own picture and rebuilds it on every bit of
  // output, so the browser's own selection cannot be used (it is gone the
  // moment the picture is rebuilt). The app draws the selection itself and
  // copies what is selected when the mouse button is released.
  await send('Browser.grantPermissions', {
    origin: `http://127.0.0.1:${agentPort}`,
    permissions: ['clipboardReadWrite', 'clipboardSanitizedWrite']
  });
  const selMarker = 'tea-sel-marker';
  await evalJs(`document.getElementById('shell-input').focus(); true`);
  const selCmd = process.platform === 'win32'
    ? "Write-Output ('tea' + '-sel-marker')"
    : "echo tea'-'sel-marker";
  await typeText(selCmd);
  await pressKey('Enter', { code: 'Enter', vk: 13, text: '\r' });
  const markerShown = await waitFor(async () => !!(await rectOfText(selMarker)), 30000, 400);
  check('the text to select is on the screen', markerShown);

  const mrect = markerShown ? await rectOfText(selMarker) : null;
  if (mrect) {
    const st = await dragSelect(selMarker);
    check('dragging over the text makes a selection', st.active === true, JSON.stringify(st));
    check('the selection holds exactly the dragged text', st.text === selMarker,
      JSON.stringify(st.text));
    check('the selected text is highlighted on the screen',
      st.marked.indexOf(selMarker) >= 0, JSON.stringify(st.marked));

    const copied = await waitFor(async () => {
      const t = await clipboardText();
      return t === selMarker;
    }, 6000, 200);
    check('releasing the mouse copies the selection', copied === true,
      JSON.stringify(await clipboardText()));

    // A double click takes the word under the pointer.
    const word = await clickSelect(selMarker, 2);
    check('a double click selects the word under the pointer', word.text === selMarker,
      JSON.stringify(word.text));

    // A triple click takes the whole line.
    const line = await clickSelect(selMarker, 3);
    check('a triple click selects the whole line',
      line.text.indexOf(selMarker) >= 0 && line.text.length >= selMarker.length,
      JSON.stringify(line.text));

    // The selection survives new output (the picture is rebuilt all the time).
    await typeText('echo tea-after-selection');
    await pressKey('Enter', { code: 'Enter', vk: 13, text: '\r' });
    await sleep(1200);
    const after = await selState();
    check('the selection survives new output', after.text === selMarker,
      JSON.stringify(after.text));

    // The right-click menu: with a selection it offers to copy that.
    const menuRect = await rectOfText(selMarker);
    const menuAt = { x: Math.round(menuRect.x + menuRect.w / 2),
                     y: Math.round(menuRect.y + menuRect.h / 2) };
    await rightClick(menuAt.x, menuAt.y);
    await sleep(250);
    const menu = await menuItems();
    check('the right-click menu opens', !!menu, JSON.stringify(menu));
    check('it offers to copy the selection',
      menu && menu.length === 3 && menu[0] === '复制', JSON.stringify(menu));

    // Escape throws the selection away.
    await pressKey('Escape', { code: 'Escape', vk: 27 });
    await sleep(250);
    const gone = await selState();
    check('Escape clears the selection', gone.active === false && !gone.text,
      JSON.stringify(gone));
  }

  // Without a selection the menu copies the whole picture.
  const menuRect2 = await rectOfText(selMarker);
  await rightClick(Math.round(menuRect2.x + menuRect2.w / 2),
                   Math.round(menuRect2.y + menuRect2.h / 2));
  await sleep(250);
  const menu2 = await menuItems();
  check('without a selection the menu offers to copy the picture',
    menu2 && menu2.length === 3 && menu2[0] === '复制整个画面', JSON.stringify(menu2));
  const itemBox = await evalJs(`(function(){
    var b = document.getElementById('shell-menu').children[0];
    var r = b.getBoundingClientRect();
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
  })()`);
  await clickMouse(itemBox.x, itemBox.y, 1);
  await sleep(500);
  const menuClip = await clipboardText();
  check('the menu copies the picture',
    menuClip.indexOf(selMarker) >= 0 && menuClip.length > selMarker.length,
    JSON.stringify(menuClip.slice(0, 160)));

  // The copy button: with a selection it copies that, without one the whole
  // picture.
  const copyBox = await evalJs(`(function(){
    var r = document.getElementById('shell-copy').getBoundingClientRect();
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
  })()`);
  await clickMouse(copyBox.x, copyBox.y, 1);
  await sleep(500);
  const screenClip = await clipboardText();
  check('the copy button copies the picture when nothing is selected',
    screenClip.indexOf(selMarker) >= 0 && screenClip.length > selMarker.length,
    JSON.stringify(screenClip.slice(0, 160)));

  // ---- the keyboard stays with the terminal ---------------------------
  // A click in the screen used to take the focus away (the browser's default
  // action of a mousedown runs after ours), and every key typed afterwards went
  // nowhere: the terminal showed the prompt and swallowed the keyboard.
  const screenBox = await evalJs(`(function(){
    var r = document.getElementById('shell-screen').getBoundingClientRect();
    return { x: Math.round(r.left + 40), y: Math.round(r.top + 60) };
  })()`);
  for (const kind of ['mousePressed', 'mouseReleased']) {
    await send('Input.dispatchMouseEvent',
      { type: kind, x: screenBox.x, y: screenBox.y, button: 'left', clickCount: 1 });
  }
  await sleep(250);
  const focused = await evalJs(`document.activeElement ? document.activeElement.id : ''`);
  check('a click in the terminal keeps the keyboard there', focused === 'shell-input', focused);
  await typeText('Write-Output tea-focus-probe');
  await pressKey('Enter', { code: 'Enter', vk: 13, text: '\r' });
  const probe = await waitFor(async () => (await screenText()).indexOf('tea-focus-probe') >= 0,
    20000, 400);
  check('typing after that click reaches the shell', probe, (await screenText()).slice(-200));

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

  // ---- the cursor -------------------------------------------------------
  const cursor = await evalJs(`(function(){
    var c = document.getElementById('term-cursor');
    var t = window.teaShell.term;
    if (!c || !t) return null;
    return { hidden: c.classList.contains('hidden'), visible: t.cursorVisible,
             cx: t.cx, cy: t.cy, left: c.style.left, top: c.style.top };
  })()`);
  check('the terminal shows a cursor', cursor && !cursor.hidden, JSON.stringify(cursor));
  check('the cursor sits where the terminal says', cursor && cursor.visible === true,
    JSON.stringify(cursor));
  const hiddenByProgram = await evalJs(`(function(){
    var t = window.teaShell.term;
    t.feed('\\x1b[?25l');
    return t.cursorVisible;
  })()`);
  check('the program can hide the cursor (CSI ?25l)', hiddenByProgram === false, hiddenByProgram);
  const shownAgain = await evalJs(`(function(){
    var t = window.teaShell.term;
    t.feed('\\x1b[?25h');
    return t.cursorVisible;
  })()`);
  check('and show it again (CSI ?25h)', shownAgain === true, shownAgain);

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
  await evalJs(`document.getElementById('shell-path').value = ${JSON.stringify(uploadName)}; true`);
  // A *real* click: a download that is not started by a user gesture can be
  // blocked by the browser, and then nothing lands in the download directory.
  const dlBox = await evalJs(`(function(){
    var r = document.getElementById('shell-download').getBoundingClientRect();
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
  })()`);
  for (const kind of ['mousePressed', 'mouseReleased']) {
    await send('Input.dispatchMouseEvent',
      { type: kind, x: dlBox.x, y: dlBox.y, button: 'left', clickCount: 1 });
  }
  // Chrome may number a repeated download ("name (1).txt"), and while it is
  // being written the file is called "name.txt.crdownload".
  const downloadedFile = () => {
    const names = fs.readdirSync(dlDir).filter((n) => n.indexOf(uploadName) === 0 &&
      n.slice(-10) !== '.crdownload');
    return names.length ? path.join(dlDir, names[0]) : '';
  };
  const downloaded = await waitFor(() => downloadedFile() !== '', 40000, 500);
  check('the browser downloads the file', downloaded, fs.readdirSync(dlDir).join(','));
  if (downloaded) {
    check('the downloaded file has the right bytes',
      fs.readFileSync(downloadedFile(), 'utf-8') === 'tea-ui-upload-content');
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
