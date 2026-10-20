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
const png = makePng(40, 30, [220, 40, 40]);
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
roles: {main: {provider: mock, model: vision}}
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

  chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
    '--window-size=1280,900',
    `--remote-debugging-port=${cdpPort}`, `--user-data-dir=${path.join(tmp, 'chrome')}`,
    'about:blank'], { stdio: 'ignore' });
  let target = null;
  for (let i = 0; i < 200; i++) {
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
  check('the browser really decoded it', withImage[0] && withImage[0].width === 40,
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
    reloaded.length === 1 && reloaded[0].width === 40, JSON.stringify(reloaded));

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
