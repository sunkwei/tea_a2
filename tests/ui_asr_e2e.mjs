// End-to-end proof: the real browser pipeline against the REAL SenseVoice
// model (not a mock).
//
//   browser (records, decodes, resamples to 16 kHz, encodes WAV)
//     -> tea_a2  POST /api/asr/transcribe
//     -> asr_server.py  POST /v1/audio/transcriptions
//     -> sherpa-onnx + SenseVoiceSmall int8 ONNX
//
// The fake audio device plays a tone, not speech, so the transcript will be
// empty or nonsense - what this proves is that the audio really travels the
// whole way and the model really runs.
//
//   node tests/ui_asr_e2e.mjs <tea_agent> <asr_server.py> <python> <model-dir>
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
const ASR_PY = process.argv[3] || path.join(REPO_ROOT, 'sched', 'asr_server.py');
const PYTHON = process.argv[4] || 'python3';
const MODEL_DIR = process.argv[5] || path.join(os.homedir(), '.tea_agent', 'asr');
// Optional: a WAV file Chrome plays into the fake microphone, so the pipeline
// can be checked with real speech instead of a tone.
const SPEECH = process.argv[6] || '';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const freePort = () => new Promise((res) => {
  const s = net.createServer();
  s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); });
});
const getJson = (port, p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port, path: p }, (r) => {
    let b = ''; r.on('data', (d) => (b += d));
    r.on('end', () => res({ status: r.statusCode, body: b }));
  }).on('error', rej);
});

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? '  ' + detail : ''));
};

// This test needs the real model and the real runtime. Without them it has
// nothing to prove, so it SKIPS (exit 0) instead of failing the suite.
//   pip install sherpa-onnx
//   python3 sched/asr_server.py --download
const hasModel = fs.existsSync(path.join(MODEL_DIR, 'model.int8.onnx')) &&
                 fs.existsSync(path.join(MODEL_DIR, 'tokens.txt'));
if (!hasModel) {
  console.log('SKIP: no SenseVoice model in ' + MODEL_DIR +
              ' (python3 sched/asr_server.py --download)');
  process.exit(0);
}

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_asr_e2e_'));
fs.mkdirSync(path.join(tmp, 'ws'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });

let agent, asr, chrome, ws;
try {
  const asrPort = await freePort();
  const agentPort = await freePort();
  const mockPort = await freePort();

  const mockServer = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      if (!req.url.endsWith('/chat/completions')) { res.writeHead(404); res.end(); return; }
      const sse = 'data: ' + JSON.stringify({ id: 'c', choices: [{ index: 0, delta: { content: '好' }, finish_reason: 'stop' }] }) + '\n\ndata: [DONE]\n\n';
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Content-Length': Buffer.byteLength(sse) });
      res.end(sse);
    });
  });
  await new Promise((r) => mockServer.listen(mockPort, '127.0.0.1', r));

  // The real transcription service, with the real model.
  asr = spawn(PYTHON, [ASR_PY, '--model-dir', MODEL_DIR, '--port', String(asrPort)],
    { stdio: ['ignore', 'pipe', 'pipe'] });
  let asrLog = '';
  asr.stdout.on('data', (d) => { asrLog += d; });
  asr.stderr.on('data', (d) => { asrLog += d; });

  let asrUp = false;
  for (let i = 0; i < 120; i++) {
    try { if ((await getJson(asrPort, '/health')).status === 200) { asrUp = true; break; } } catch {}
    await sleep(250);
  }
  if (!asrUp && /sherpa-onnx is not installed/.test(asrLog)) {
    console.log('SKIP: sherpa-onnx is not installed (pip install sherpa-onnx)');
    process.exit(0);
  }
  check('the SenseVoice service comes up with the real model', asrUp, asrLog.slice(-300));
  if (!asrUp) throw new Error('asr service did not start');

  const cfg = path.join(tmp, 'provider.yaml');
  fs.writeFileSync(cfg, `version: 1
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

  agent = spawn(BIN, ['--config', cfg, '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'ws')], { stdio: 'ignore' });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  chrome = await acquireBrowser(tmp, 'e2e', SPEECH ? { fakeAudioFile: SPEECH } : {});
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

  const shown = await evalJs(`(function(){
    var b=document.getElementById('mic');
    return { hidden: b ? b.classList.contains('hidden') : null };
  })()`);
  check('the microphone button is there', shown.hidden === false, JSON.stringify(shown));

  await evalJs(`document.getElementById('mic').click()`);
  await sleep(2000);
  await evalJs(`document.getElementById('mic').click()`);
  await sleep(8000);

  const value = await evalJs(`document.getElementById('input').value`);
  const toastText = await evalJs(`(function(){
    var t=document.getElementById('toast');
    return t ? t.textContent : '';
  })()`);
  check('the real model answered (no error toast)',
    !/转写失败/.test(toastText || ''), JSON.stringify(toastText));
  check('the service really decoded the audio',
    /transcribed/.test(asrLog), asrLog.split('\n').filter((l) => /transcribed/.test(l)).pop());

  console.log('\ntranscript: ' + JSON.stringify(value));
  if (SPEECH) {
    // Real speech in, real words out - the whole point of the feature.
    check('the real speech came out as text',
      /开饭时间/.test(value || ''), JSON.stringify(value));
  }
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  if (agent) agent.kill('SIGTERM');
  if (asr) asr.kill('SIGTERM');
  await sleep(300);
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (e) {}
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
