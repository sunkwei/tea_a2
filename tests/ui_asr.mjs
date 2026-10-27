// Browser test for voice input (the microphone button next to the composer).
//
// The contract pinned here:
//   * without an `asr:` section in provider.yaml there is NO microphone button
//     (a dead button is worse than no button);
//   * with one, the button appears and records when clicked;
//   * the recording is encoded in the browser as 16 kHz mono WAV - the mock
//     service checks the WAV header, so a wrong sample rate fails the test;
//   * the transcript lands in the INPUT BOX, not in a sent message: voice
//     input is a draft, and a wrong transcript must not be sent by itself;
//   * a failing transcription service leaves the input box alone and says why.
//
// The microphone is faked: Chrome has no audio device in a container, so
// getUserMedia is replaced with a Web Audio oscillator. Everything after that
// (MediaRecorder, decodeAudioData, the resampler, the WAV writer, the POST)
// is the real code.
//
//   node tests/ui_asr.mjs [path-to-tea_agent-binary]
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
const CHROME = process.env.CHROME_BIN || '/usr/bin/google-chrome';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const freePort = () => new Promise((res) => {
  const s = net.createServer();
  s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); });
});
const getJson = (port, p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port, path: p }, (r) => {
    let b = '';
    r.on('data', (d) => (b += d));
    r.on('end', () => res({ status: r.statusCode, body: b }));
  }).on('error', rej);
});

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? '  ' + detail : ''));
};

// --- the fake transcription service ---------------------------------------
// It records the WAV it was sent (and what its header says) and answers with
// a fixed transcript.
const TRANSCRIPT = '这是语音输入的测试';
const asrCalls = [];
let asrFail = false;

const asrServer = http.createServer((req, res) => {
  let body = Buffer.alloc(0);
  req.on('data', (d) => { body = Buffer.concat([body, d]); });
  req.on('end', () => {
    const text = body.toString('latin1');
    // The audio part of a multipart body: find the WAV header inside it.
    const at = body.indexOf(Buffer.from('RIFF'));
    const wav = at >= 0 ? body.slice(at) : Buffer.alloc(0);
    let rate = 0, channels = 0, bits = 0;
    if (wav.length >= 44) {
      channels = wav.readUInt16LE(22);
      rate = wav.readUInt32LE(24);
      bits = wav.readUInt16LE(34);
    }
    asrCalls.push({ url: req.url, bytes: wav.length, rate, channels, bits, body: text });
    if (asrFail) {
      const payload = JSON.stringify({ error: { message: 'model not loaded' } });
      res.writeHead(503, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) });
      res.end(payload);
      return;
    }
    const payload = JSON.stringify({ text: TRANSCRIPT });
    res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) });
    res.end(payload);
  });
});

// --- the fake chat provider (must not be called) ---------------------------
const chats = [];
const mockServer = http.createServer((req, res) => {
  let body = '';
  req.on('data', (d) => (body += d));
  req.on('end', () => {
    if (!req.url.endsWith('/chat/completions')) { res.writeHead(404); res.end(); return; }
    chats.push(JSON.parse(body || '{}'));
    const sse = [
      { id: 'c', choices: [{ index: 0, delta: { content: '收到' }, finish_reason: null }] },
      { id: 'c', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] },
    ].map((c) => 'data: ' + JSON.stringify(c) + '\n\n').join('') + 'data: [DONE]\n\n';
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Content-Length': Buffer.byteLength(sse) });
    res.end(sse);
  });
});

// The microphone comes from Chrome's fake device (--use-fake-device-for-media-stream,
// set in tests/chrome_pool.mjs and scripts/test-all.sh): a container has no
// audio hardware, and the fake device hands out a synthetic tone that
// MediaRecorder records for real. Everything after that is the app's own code.
//
// The page is served from 127.0.0.1, which is a secure context - the same
// reason a real deployment on localhost works. (An http:// LAN address is
// NOT a secure context, and browsers refuse the microphone there; the app
// hides the button in that case.)

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_asr_ui_'));
fs.mkdirSync(path.join(tmp, 'ws'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });

let agent, chrome, ws;
try {
  await new Promise((r) => asrServer.listen(0, '127.0.0.1', r));
  const asrPort = asrServer.address().port;
  await new Promise((r) => mockServer.listen(0, '127.0.0.1', r));
  const mockPort = mockServer.address().port;

  // First: no asr: section at all -> no microphone button.
  const noAsr = path.join(tmp, 'no-asr.yaml');
  fs.writeFileSync(noAsr, `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:${mockPort}
    api_key: sk-test
    default_model: see
    models:
      see: {}
`);

  // Second: with an asr: section -> the button shows up.
  const withAsr = path.join(tmp, 'with-asr.yaml');
  fs.writeFileSync(withAsr, `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:${mockPort}
    api_key: sk-test
    default_model: see
    models:
      see: {}
asr:
  url: http://127.0.0.1:${asrPort}
  model: sense-voice
`);

  const agentPort = await freePort();
  agent = spawn(BIN, ['--config', noAsr, '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'ws')], { stdio: 'ignore' });

  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  chrome = await acquireBrowser(tmp, 'asr');
  ws = wsConnect(chrome.webSocketDebuggerUrl);
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

  const micSetup = await evalJs(`(function(){
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return { ok: false, why: 'no mediaDevices (not a secure context)' };
    }
    return { ok: typeof window.MediaRecorder === 'function', why: 'MediaRecorder missing' };
  })()`);
  check('the browser offers a microphone on this page',
    micSetup && micSetup.ok === true, JSON.stringify(micSetup));

  // ---- without an asr: section there is no button ------------------------
  const noButton = await evalJs(`(function(){
    var b=document.getElementById('mic');
    return { exists: !!b, hidden: b ? b.classList.contains('hidden') : null };
  })()`);
  check('the microphone button exists in the composer', noButton.exists === true, JSON.stringify(noButton));
  check('without an asr: section the button stays hidden',
    noButton.hidden === true, JSON.stringify(noButton));

  // ---- with an asr: section the button appears --------------------------
  await fetch(`http://127.0.0.1:${agentPort}/api/config`, {
    method: 'POST',
    headers: { 'Content-Type': 'text/yaml' },
    body: fs.readFileSync(withAsr, 'utf8'),
  });
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(2500);

  const withButton = await evalJs(`(function(){
    var b=document.getElementById('mic');
    return { exists: !!b, hidden: b ? b.classList.contains('hidden') : null,
             title: b ? b.title : '' };
  })()`);
  check('with an asr: section the button is visible',
    withButton.hidden === false, JSON.stringify(withButton));
  check('the button says what it does',
    /语音输入/.test(withButton.title || ''), withButton.title);

  // ---- record, stop, transcribe -----------------------------------------
  const before = chats.length;
  await evalJs(`document.getElementById('mic').click()`);
  await sleep(1200);
  const recording = await evalJs(`(function(){
    var b=document.getElementById('mic');
    return { rec: b.classList.contains('rec'), title: b.title };
  })()`);
  check('clicking the button starts recording', recording.rec === true, JSON.stringify(recording));
  check('the button says it is recording', /录音/.test(recording.title || ''), recording.title);

  await evalJs(`document.getElementById('mic').click()`);
  // Give the browser time to decode, resample and post.
  await sleep(4000);

  check('the transcription service was called', asrCalls.length === 1,
    'calls: ' + asrCalls.length);
  const call = asrCalls[0] || {};
  check('the service is called at the transcription path',
    call.url === '/v1/audio/transcriptions', call.url);
  check('the browser sends 16 kHz audio', call.rate === 16000, String(call.rate));
  check('the browser sends mono audio', call.channels === 1, String(call.channels));
  check('the browser sends 16-bit samples', call.bits === 16, String(call.bits));
  check('the clip has samples in it', call.bytes > 1000, String(call.bytes));

  const inputValue = await evalJs(`document.getElementById('input').value`);
  check('the transcript lands in the input box', inputValue === TRANSCRIPT, JSON.stringify(inputValue));

  // ---- the transcript goes where the cursor is --------------------------
  await evalJs(`(function(){
    var ta=document.getElementById('input');
    ta.value='前面后面'; ta.focus(); ta.selectionStart=2; ta.selectionEnd=2;
  })()`);
  await evalJs(`document.getElementById('mic').click()`);
  await sleep(1000);
  await evalJs(`document.getElementById('mic').click()`);
  await sleep(4000);
  const inserted = await evalJs(`(function(){
    var ta=document.getElementById('input');
    return { value: ta.value, caret: ta.selectionStart };
  })()`);
  // Inserted where the cursor was, with a space on each side so the words do
  // not get glued together.
  check('the transcript is inserted at the cursor, not at the end',
    inserted.value === '前面 ' + TRANSCRIPT + ' 后面', JSON.stringify(inserted));
  check('the caret ends up after the inserted text',
    inserted.caret === 3 + TRANSCRIPT.length, JSON.stringify(inserted));

  await evalJs(`(function(){
    var ta=document.getElementById('input');
    ta.value=''; ta.dispatchEvent(new Event('input'));
  })()`);

  const notSent = await evalJs(`(function(){
    return { chats: 0, focused: document.activeElement && document.activeElement.id };
  })()`);
  check('the transcript is NOT sent by itself', chats.length === before,
    'chats: ' + (chats.length - before));
  check('the input box keeps the focus after transcribing',
    notSent.focused === 'input', JSON.stringify(notSent));

  // ---- a failing service changes nothing --------------------------------
  await evalJs(`(function(){
    var ta=document.getElementById('input');
    ta.value=''; ta.dispatchEvent(new Event('input'));
  })()`);
  asrFail = true;
  await evalJs(`document.getElementById('mic').click()`);
  await sleep(1000);
  await evalJs(`document.getElementById('mic').click()`);
  await sleep(3500);

  const afterFail = await evalJs(`(function(){
    var ta=document.getElementById('input');
    var t=document.getElementById('toast');
    return { value: ta.value, toast: t ? t.textContent : '', shown: t ? !t.classList.contains('hidden') : false };
  })()`);
  check('a failed transcription leaves the input box empty',
    afterFail.value === '', JSON.stringify(afterFail.value));
  check('a failed transcription says why', /转写失败/.test(afterFail.toast || ''),
    JSON.stringify(afterFail.toast));
  check('nothing was sent either', chats.length === before,
    'chats: ' + (chats.length - before));
  asrFail = false;
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  if (agent) agent.kill('SIGTERM');
  asrServer.close();
  mockServer.close();
  await sleep(300);
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
