// Browser test for toolkit_load_image: an image the agent loads from disk must
// become something you can *see* in the conversation, both live and after a
// reload.
//
// The contract pinned here:
//   * during the turn, the injected follow-up message appears as its own bubble
//     with the picture in it (served by /raw, so it must actually load:
//     naturalWidth > 0) instead of only being in the request body;
//   * the tool row above it reports the stored copy;
//   * after a reload the same picture is still there (it is part of the
//     session, not a transient stream artefact);
//   * the assistant's answer lands below it, in order.
//
//   node tests/ui_tool_images.mjs [path-to-tea_agent-binary]
//
// Requires google-chrome (or CHROME_BIN) and node >= 18.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';
import { wsConnect } from './mini_ws.mjs';
import { acquireBrowser } from './chrome_pool.mjs';

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
const waitFor = async (fn, timeout = 20000, step = 150) => {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await fn()) return true;
    await sleep(step);
  }
  return false;
};

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || !detail ? '' : '  ' + detail));
};

/// A tiny valid PNG: the browser must decode it, so it cannot be a stub.
function makePng(width, height, rgb) {
  const raw = Buffer.concat(Array.from({ length: height },
    () => Buffer.concat([Buffer.from([0]), Buffer.concat(Array.from({ length: width },
      () => Buffer.from(rgb)))])));
  const chunk = (tag, data) => {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(tag), data]);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32(body));
    return Buffer.concat([len, body, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; ihdr[9] = 2; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr), chunk('IDAT', zlib.deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]);
}
function crc32(buf) {
  let c = ~0;
  for (const b of buf) {
    c ^= b;
    for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xedb88320 & -(c & 1));
  }
  return (~c) >>> 0;
}

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_toolimg_ui_'));
const proj = path.join(tmp, 'proj');
fs.mkdirSync(proj, { recursive: true });
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
const png = makePng(800, 600, [220, 40, 40]);
fs.writeFileSync(path.join(proj, 'shot.png'), png);

let agent, chrome, ws, mockServer;
try {
  let turnStarted = false;
  mockServer = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      if (!req.url.endsWith('/chat/completions')) {
        res.writeHead(404).end();
        return;
      }
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      const msgs = (JSON.parse(body || '{}').messages) || [];
      const last = msgs[msgs.length - 1] || {};
      const afterTool = last.role === 'tool' ||
        (last.role === 'user' && Array.isArray(last.content));
      if (turnStarted && !afterTool) {
        res.write('data: ' + JSON.stringify({
          id: 'c', choices: [{ index: 0, delta: { content: '这张图是红色的方块。' },
            finish_reason: null }],
        }) + '\n\n');
        res.write('data: ' + JSON.stringify({
          id: 'c', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
        }) + '\n\n');
        res.write('data: [DONE]\n\n');
        res.end();
        return;
      }
      if (!turnStarted) {
        turnStarted = true;
        res.write('data: ' + JSON.stringify({
          id: 'c',
          choices: [{
            index: 0,
            delta: {
              role: 'assistant',
              tool_calls: [{
                index: 0, id: 'call_img', type: 'function',
                function: { name: 'toolkit_load_image',
                  arguments: JSON.stringify({ path: 'shot.png', note: 'look at this' }) },
              }],
            },
            finish_reason: null,
          }],
        }) + '\n\n');
        res.write('data: ' + JSON.stringify({
          id: 'c', choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }],
        }) + '\n\n');
        res.write('data: [DONE]\n\n');
        res.end();
        return;
      }
      res.write('data: ' + JSON.stringify({
        id: 'c', choices: [{ index: 0, delta: { content: '这张图是红色的方块。' },
          finish_reason: null }],
      }) + '\n\n');
      res.write('data: [DONE]\n\n');
      res.end();
    });
  });
  await new Promise((r) => mockServer.listen(0, '127.0.0.1', r));

  fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:${mockServer.address().port}
    api_key: sk-test
    default_model: vision
    models:
      vision:
        max_context_tokens: 32768
        max_output_tokens: 4096
        supports_tools: true
        supports_vision: true
`);

  const agentPort = await freePort();
  const cdpPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'),
    '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', proj], { stdio: 'ignore' });
  for (let i = 0; i < 200; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  chrome = await acquireBrowser(tmp, 'chrome', { windowSize: '1280,900' });
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

  // Send one message; the mock answers with a toolkit_load_image call, then text.
  await evalJs(`(function(){
    var i = document.getElementById('input');
    i.value = '看看 shot.png 里是什么颜色';
    i.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);

  const bubbles = () => evalJs(`(function(){
    var userWithImage = Array.prototype.map.call(document.querySelectorAll('.msg.user'), function (n) {
      var img = n.querySelector('.msg-images img');
      return {
        text: n.textContent,
        hasImage: !!img,
        src: img ? img.getAttribute('src') : '',
        loaded: img ? (img.complete && img.naturalWidth > 0) : false,
        width: img ? img.naturalWidth : 0
      };
    });
    return {
      userWithImage: userWithImage,
      toolLines: Array.prototype.map.call(document.querySelectorAll('.tool-line'),
        function (n) { return n.textContent; }),
      assistant: Array.prototype.map.call(document.querySelectorAll('.msg.assistant'),
        function (n) { return n.textContent; })
    };
  })()`);

  const appeared = await waitFor(async () => {
    const b = await bubbles();
    return b.userWithImage.some((u) => u.hasImage && u.loaded);
  }, 25000);
  check('the loaded image shows up in the conversation', appeared);

  let b = await bubbles();
  const withImage = b.userWithImage.filter((u) => u.hasImage);
  check('exactly one bubble carries the image', withImage.length === 1,
    JSON.stringify(b.userWithImage.map((u) => [u.text, u.hasImage])));
  check('the bubble explains where the image came from',
    withImage[0] && withImage[0].text.indexOf('toolkit_load_image') >= 0,
    JSON.stringify(withImage[0] && withImage[0].text));
  check('the image is served by the /raw route',
    withImage[0] && withImage[0].src.indexOf('/raw?path=') > 0,
    JSON.stringify(withImage[0] && withImage[0].src));
  check('the browser really decoded it', withImage[0] && withImage[0].width === 800,
    JSON.stringify(withImage[0] && [withImage[0].width, withImage[0].loaded]));
  // The tool row is collapsed by default; its panel carries the real output.
  await evalJs(`(function(){
    var lines = document.querySelectorAll('.tool-line');
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].textContent.indexOf('toolkit_load_image') >= 0) { lines[i].click(); return true; }
    }
    return false;
  })()`);
  await sleep(400);
  const panelText = await evalJs(`(function(){
    var out = [];
    Array.prototype.forEach.call(document.querySelectorAll('.tool-row.open .tool-panel'),
      function (n) { out.push(n.textContent); });
    return out.join('\\n');
  })()`);
  check('the tool row reports the stored copy',
    panelText.indexOf('artifacts/images') >= 0 && panelText.indexOf('image/png') >= 0,
    JSON.stringify(panelText.slice(0, 300)));

  const answered = await waitFor(async () => {
    const now = await bubbles();
    return now.assistant.some((t) => t.indexOf('红色的方块') >= 0);
  }, 25000);
  check('the answer arrives after the image', answered);

  // Reload: the picture is part of the session, not of the stream.
  await send('Page.reload');
  await sleep(2500);
  const afterReload = await waitFor(async () => {
    const now = await bubbles();
    return now.userWithImage.some((u) => u.hasImage && u.loaded);
  }, 15000);
  check('the image survives a reload', afterReload);
  b = await bubbles();
  const reloaded = b.userWithImage.filter((u) => u.hasImage);
  check('only one bubble after the reload (no duplicate)',
    b.userWithImage.length === 2, JSON.stringify(b.userWithImage.map((u) => u.text)));
  check('the session keeps the image in place',
    reloaded.length === 1 && reloaded[0].width === 800, JSON.stringify(reloaded));

  // ---- double-click opens the original size ------------------------------
  // The bubble thumbnail is capped at 240x180 to keep the transcript readable;
  // that is useless for reading text inside a screenshot, so a double-click must
  // show the image at its own pixel size (800x600 here), not at 240.
  const thumb = await evalJs(`(function(){
    var img = document.querySelector('.msg.user .msg-images img');
    if (!img) return null;
    var r = img.getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height),
             natural: img.naturalWidth, title: img.title,
             cursor: getComputedStyle(img).cursor, tabIndex: img.tabIndex };
  })()`);
  check('the bubble thumbnail is scaled down', thumb && thumb.w <= 240 && thumb.natural === 800,
    JSON.stringify(thumb));
  check('the thumbnail advertises the zoom', thumb && thumb.title.indexOf('双击') >= 0
    && thumb.cursor === 'zoom-in', JSON.stringify(thumb && [thumb.title, thumb.cursor]));

  const viewer = () => evalJs(`(function(){
    var box = document.getElementById('image-viewer');
    var img = document.getElementById('image-viewer-img');
    var r = img.getBoundingClientRect();
    return {
      open: !box.classList.contains('hidden'),
      natural: img.naturalWidth,
      w: Math.round(r.width), h: Math.round(r.height),
      loaded: img.complete && img.naturalWidth > 0,
      meta: document.getElementById('image-viewer-meta').textContent,
      scrollable: box.scrollHeight >= box.clientHeight,
      zIndex: getComputedStyle(box).zIndex
    };
  })()`);

  await evalJs(`(function(){
    var img = document.querySelector('.msg.user .msg-images img');
    img.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, cancelable: true }));
    return true;
  })()`);
  await sleep(600);
  let v = await viewer();
  check('double-clicking a thumbnail opens the viewer', v && v.open === true, JSON.stringify(v));
  check('the viewer loads the picture', v && v.loaded === true && v.natural === 800,
    JSON.stringify(v && [v.loaded, v.natural]));
  check('it is shown at the original pixel size (not the thumbnail size)',
    v && v.w === 800 && v.h === 600, JSON.stringify(v && [v.w, v.h]));
  check('the viewer says the size and how to close it',
    v && v.meta.indexOf('800 × 600') === 0 && v.meta.indexOf('Esc') > 0, v && v.meta);
  check('the viewer sits above the page', v && Number(v.zIndex) >= 60,
    JSON.stringify(v && v.zIndex));

  // Esc closes it and the big bitmap is released.
  await evalJs(`document.dispatchEvent(new KeyboardEvent('keydown', {
    key: 'Escape', bubbles: true })); true`);
  await sleep(300);
  v = await viewer();
  check('Esc closes the viewer', v && v.open === false, JSON.stringify(v && v.open));
  const released = await evalJs(
    `document.getElementById('image-viewer-img').getAttribute('src')`);
  check('closing releases the full-size bitmap', released === null,
    JSON.stringify(released && released.slice(0, 40)));

  // Reopen (keyboard: Enter on the focused thumbnail) and close by clicking the
  // backdrop - both are documented affordances.
  await evalJs(`(function(){
    var img = document.querySelector('.msg.user .msg-images img');
    img.focus();
    img.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    return true;
  })()`);
  await sleep(500);
  v = await viewer();
  check('Enter on the thumbnail opens it too', v && v.open === true, JSON.stringify(v && v.open));
  await evalJs(`(function(){
    var box = document.getElementById('image-viewer');
    box.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    return true;
  })()`);
  await sleep(300);
  v = await viewer();
  check('clicking the backdrop closes it', v && v.open === false, JSON.stringify(v && v.open));

  const health = JSON.parse((await getJson(agentPort, '/api/health')).body);
  check('the server is still healthy', health.ok === true, JSON.stringify(health));
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
