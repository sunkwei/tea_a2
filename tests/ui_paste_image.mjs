// Browser test for pasted-image input, driven over CDP against headless Chrome.
//
// Checks the parts a backend test cannot: the paste handler, the thumbnail
// strip, the remove button, and the rendered user bubble.
//
//   node tests/ui_paste_image.mjs [path-to-tea_agent-binary]
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

// zlib.crc32 only exists from Node 20.15; compute it here so the test also runs
// on the Node 18 that many CI images still have.
function crc32(buf) {
  let c = ~0;
  for (const b of buf) {
    c ^= b;
    for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xedb88320 & -(c & 1));
  }
  return (~c) >>> 0;
}

function makePng(width = 4, height = 4) {
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
  const rows = [];
  for (let y = 0; y < height; y++) {
    rows.push(Buffer.from([0]));
    rows.push(Buffer.from(Array.from({ length: width }, () => [200, 60, 60]).flat()));
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(Buffer.concat(rows))),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

function freePort() {
  return new Promise((res) => {
    const s = net.createServer();
    s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); });
  });
}
const getJson = (port, p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port, path: p }, (r) => {
    let b = ''; r.on('data', (d) => (b += d)); r.on('end', () => res({ status: r.statusCode, body: b }));
  }).on('error', rej);
});

const PNG = makePng();
const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? '  ' + detail : ''));
};

// --- mock provider (records every chat body) -------------------------------
const chats = [];
const mockServer = http.createServer((req, res) => {
  let body = '';
  req.on('data', (d) => (body += d));
  req.on('end', () => {
    if (!req.url.endsWith('/chat/completions')) { res.writeHead(404); res.end(); return; }
    chats.push(JSON.parse(body || '{}'));
    const sse = [
      { id: 'c', choices: [{ index: 0, delta: { content: '收到图片' }, finish_reason: null }] },
      { id: 'c', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] },
    ].map((c) => 'data: ' + JSON.stringify(c) + '\n\n').join('') + 'data: [DONE]\n\n';
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Content-Length': Buffer.byteLength(sse) });
    res.end(sse);
  });
});

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_paste_'));
fs.mkdirSync(path.join(tmp, 'ws'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });

let agent, chrome;
let ws;
try {
  await new Promise((r) => mockServer.listen(0, '127.0.0.1', r));
  const mockPort = mockServer.address().port;
  fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:${mockPort}
    api_key: sk-test
    default_model: see
    models:
      see:
        supports_tools: true
        supports_vision: true
`);

  const agentPort = await freePort();
  const cdpPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'ws')], { stdio: 'ignore' });

  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
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
  await sleep(2500);

  // Paste a File built in the page. Chrome's ClipboardEventInit accepts
  // clipboardData, but fall back to defining the property if it is ignored.
  const pasteExpr = (b64) => `(function(){
    var bin = atob(${JSON.stringify(b64)});
    var bytes = new Uint8Array(bin.length);
    for (var i=0;i<bin.length;i++) bytes[i]=bin.charCodeAt(i);
    var file = new File([bytes], 'red.png', {type:'image/png'});
    var dt = new DataTransfer();
    dt.items.add(file);
    var ta = document.getElementById('input');
    ta.focus();
    var ev = new ClipboardEvent('paste', {clipboardData: dt, bubbles:true, cancelable:true});
    if (!ev.clipboardData || !ev.clipboardData.files.length) {
      Object.defineProperty(ev, 'clipboardData', {value: dt});
    }
    ta.dispatchEvent(ev);
    return true;
  })()`;

  await evalJs(pasteExpr(PNG.toString('base64')));
  await sleep(900);

  const afterPaste = await evalJs(`(function(){
    var strip=document.getElementById('attach-strip');
    var items=strip.querySelectorAll('.attach-item');
    var img=items.length?items[0].querySelector('img'):null;
    return { hidden: strip.classList.contains('hidden'), count: items.length,
             src: img?img.src.slice(0,22):'', title: img?img.title:'' };
  })()`);
  check('paste adds a thumbnail', afterPaste && afterPaste.count === 1 && !afterPaste.hidden,
    JSON.stringify(afterPaste));
  check('thumbnail is a data: URL', afterPaste && afterPaste.src.startsWith('data:image/png'),
    afterPaste && afterPaste.src);

  await evalJs(`document.querySelector('.attach-item .attach-del').click()`);
  await sleep(300);
  const afterRemove = await evalJs(`(function(){
    var strip=document.getElementById('attach-strip');
    return { hidden: strip.classList.contains('hidden'), count: strip.querySelectorAll('.attach-item').length };
  })()`);
  check('remove button clears the thumbnail', afterRemove && afterRemove.count === 0 && afterRemove.hidden,
    JSON.stringify(afterRemove));

  // Paste again, then send with text.
  await evalJs(pasteExpr(PNG.toString('base64')));
  await sleep(700);
  await evalJs(`(function(){
    document.getElementById('input').value = '这张图是什么颜色？';
    document.getElementById('input').dispatchEvent(new Event('input'));
    document.getElementById('send').click();
  })()`);
  await sleep(3000);

  const sent = chats[chats.length - 1];
  const userMsg = sent && sent.messages ? sent.messages.filter((m) => m.role === 'user').pop() : null;
  const content = userMsg && userMsg.content;
  check('request carries a multimodal user message',
    Array.isArray(content) && content[0] && content[0].type === 'text' &&
    content[1] && content[1].type === 'image_url',
    JSON.stringify(Array.isArray(content) ? content.map((p) => p.type) : content));

  const sentUrl = Array.isArray(content) && content[1] ? content[1].image_url.url : '';
  check('image travels as a png data URL', sentUrl.startsWith('data:image/png;base64,'),
    sentUrl.slice(0, 30));
  check('image bytes survive the round trip',
    Buffer.from(sentUrl.split(',')[1] || '', 'base64').equals(PNG));

  const afterSend = await evalJs(`(function(){
    var strip=document.getElementById('attach-strip');
    var imgs=document.querySelectorAll('.msg.user .msg-images img');
    var first=imgs.length?imgs[0]:null;
    return { stripHidden: strip.classList.contains('hidden'),
             stripCount: strip.querySelectorAll('.attach-item').length,
             bubbleImgs: imgs.length,
             isRaw: !!first && first.src.indexOf('/raw?path=') >= 0,
             src: first?first.src.slice(-40):'' };
  })()`);
  check('attachment strip cleared after send',
    afterSend && afterSend.stripHidden === true && afterSend.stripCount === 0,
    JSON.stringify(afterSend));
  check('user bubble renders the image through /raw',
    afterSend && afterSend.bubbleImgs === 1 && afterSend.isRaw === true,
    afterSend && afterSend.src);

  const imgOk = await evalJs(`(function(){
    var img=document.querySelector('.msg.user .msg-images img');
    if (!img) return 'missing';
    return img.complete && img.naturalWidth > 0 ? 'ok' : ('broken:'+img.src);
  })()`);
  check('served image loads in the browser', imgOk === 'ok', String(imgOk));

  // Text-only turns must still work (no regression).
  const before = chats.length;
  await evalJs(`(function(){
    document.getElementById('input').value = 'plain text';
    document.getElementById('input').dispatchEvent(new Event('input'));
    document.getElementById('send').click();
  })()`);
  await sleep(2500);
  const last = chats[chats.length - 1];
  const lastUser = last && last.messages ? last.messages.filter((m) => m.role === 'user').pop() : null;
  check('text-only message stays a string',
    chats.length === before + 1 && typeof lastUser.content === 'string',
    typeof (lastUser && lastUser.content));
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  if (agent) agent.kill('SIGTERM');
  mockServer.close();
  await sleep(300);
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
