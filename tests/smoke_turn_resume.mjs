// A turn outlives the connection that started it.
//
// Closing the browser used to cancel the running turn: the SSE response's
// resource releaser set `cancel`, so the agent stopped at its next checkpoint and
// the half-finished answer was thrown away. Reopening the page then showed a
// conversation that stopped mid-sentence, with no way to tell whether it was
// still working.
//
// Now a disconnect only detaches one subscriber. Every event is kept in a bounded
// log with a sequence number, so this test can:
//
//   * hang up mid-turn and see the turn (and the upstream provider request) keep
//     going, with events still accumulating;
//   * ask GET /api/chat/turn and get `running: true` plus the replay window;
//   * POST /api/chat/attach and receive the whole turn from seq 1 - contiguous
//     sequence numbers, the frames seen before the hangup included - and then
//     follow it live to `done`;
//   * read the finished answer back from the session file;
//   * confirm /api/chat/stop still cancels a turn nobody is watching.
//
//   node tests/smoke_turn_resume.mjs [path-to-tea_agent-binary]
//
// Requires node >= 18. No browser, no provider account: the model is a local
// stream of one character every 300ms.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const BIN = process.argv[2] || path.join(REPO_ROOT, 'build', 'bin', 'tea_agent');

const CHUNKS = 40;          // ~12s of streaming: enough time to walk away and come back
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
const postJson = (port, p, body) => new Promise((res, rej) => {
  const payload = JSON.stringify(body || {});
  const req = http.request({ host: '127.0.0.1', port, path: p, method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) } },
    (r) => { let b = ''; r.on('data', (d) => (b += d)); r.on('end', () => res({ status: r.statusCode, body: b })); });
  req.on('error', rej);
  req.end(payload);
});

/// POST an SSE request and collect its frames. Returns an object whose `frames`
/// keeps growing until `hangup()` (client goes away) or the stream ends.
function sseRequest(port, p, body) {
  const out = { frames: [], ended: null, hangup: () => {} };
  const req = http.request({ host: '127.0.0.1', port, path: p, method: 'POST',
    headers: { 'Content-Type': 'application/json' } }, (res) => {
    res.on('data', (d) => {
      for (const line of d.toString().split('\n')) {
        if (line.indexOf('data:') !== 0) continue;
        try { out.frames.push(JSON.parse(line.slice(5).trim())); } catch {}
      }
    });
    res.on('end', () => { out.ended = 'end'; });
  });
  req.on('error', () => { out.ended = out.ended || 'error'; });
  out.hangup = () => { req.destroy(); };
  req.end(JSON.stringify(body));
  return out;
}

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? '  ' + detail : ''));
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_resume_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'ws'), { recursive: true });

// The mock provider also reports whether *its* response was aborted: a cancelled
// turn closes the upstream request, a detached one does not.
let upstream = { open: false, chunks: 0, finished: false };
const mock = http.createServer((req, res) => {
  let body = '';
  req.on('data', (d) => (body += d));
  req.on('end', () => {
    upstream = { open: true, chunks: 0, finished: false };
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    const timer = setInterval(() => {
      upstream.chunks++;
      res.write('data: ' + JSON.stringify({ id: 'c', choices: [{ index: 0, delta: { content: '字' }, finish_reason: null }] }) + '\n\n');
      if (upstream.chunks >= CHUNKS) {
        clearInterval(timer);
        res.write('data: ' + JSON.stringify({ id: 'c', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }) + '\n\n');
        res.write('data: [DONE]\n\n');
        res.end();
        upstream.open = false; upstream.finished = true;
      }
    }, 300);
    res.on('close', () => { clearInterval(timer); upstream.open = false; });
  });
});
await new Promise((r) => mock.listen(0, '127.0.0.1', r));

fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:${mock.address().port}
    api_key: sk-test
    default_model: m
    models: {m: {supports_tools: true}}
roles: {main: {provider: mock, model: m}}
`);

const agentPort = await freePort();
const agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
  '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
  '--workspace', path.join(tmp, 'ws')], { stdio: 'ignore' });

const turnStatus = async (sid) =>
  JSON.parse((await getJson(agentPort, '/api/chat/turn?session_id=' + encodeURIComponent(sid))).body);

try {
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }
  const wsId = JSON.parse((await getJson(agentPort, '/api/workspaces')).body).workspaces[0].id;

  // ---- 1) start a turn, watch a few frames, then hang up -----------------
  const first = sseRequest(agentPort, '/api/chat', { workspace_id: wsId, message: 'answer slowly' });
  await sleep(2500);
  const sid = (first.frames.find((f) => f.session_id) || {}).session_id;
  const seenBefore = first.frames.length;
  check('the turn started and streamed', !!sid && seenBefore > 1, 'frames=' + seenBefore + ' session=' + sid);
  check('every frame carries a sequence number',
    first.frames.filter((f) => typeof f.seq === 'number').length === seenBefore,
    first.frames.map((f) => f.seq).join(','));
  first.hangup();                                    // <- the browser goes away
  await sleep(3000);

  // ---- 2) the turn keeps running with nobody attached -------------------
  let st = await turnStatus(sid);
  check('the turn is still running after the client left', st.running === true, JSON.stringify(st));
  check('the provider request was NOT aborted', upstream.open === true && upstream.finished === false,
    'open=' + upstream.open + ' finished=' + upstream.finished + ' chunks=' + upstream.chunks);
  const seqBefore = st.last_seq;
  await sleep(1500);
  st = await turnStatus(sid);
  check('events keep accumulating while nobody is watching', st.last_seq > seqBefore,
    seqBefore + ' -> ' + st.last_seq);
  check('the replay window still starts at seq 1', st.oldest_seq === 1 && st.dropped === 0,
    JSON.stringify(st));

  // A client that knows nothing about this session - another machine, a fresh
  // browser profile - finds the work in progress through the list endpoint.
  const all = JSON.parse((await getJson(agentPort, '/api/chat/turns')).body);
  const listed = (all.turns || []).filter((t) => t.session_id === sid)[0];
  check('GET /api/chat/turns lists the running turn with its workspace',
    !!listed && listed.running === true && listed.workspace_id === wsId, JSON.stringify(all.turns));

  // ---- 3) re-attach: full replay, then live to the end ------------------
  const second = sseRequest(agentPort, '/api/chat/attach', { session_id: sid, since: 0 });
  for (let i = 0; i < 80 && !second.frames.some((f) => f.type === 'done' || f.type === 'stopped'); i++) {
    await sleep(400);
  }
  const replay = second.frames;
  const done = replay.find((f) => f.type === 'done');
  check('the replay starts at the beginning of the turn', replay.length && replay[0].type === 'start',
    replay.length ? replay[0].type + ' seq=' + replay[0].seq : 'nothing');
  check('the replay includes the frames seen before the hangup', replay.length > seenBefore,
    'replayed=' + replay.length + ' seenBefore=' + seenBefore);
  check('sequence numbers are contiguous from 1',
    replay.every((f, i) => f.seq === i + 1), 'first=' + (replay[0] || {}).seq + ' last=' + (replay[replay.length - 1] || {}).seq);
  check('the re-attached stream reaches done', !!done, JSON.stringify(replay[replay.length - 1] || {}));
  check('the provider stream ran to completion', upstream.finished === true);
  check('the start event says where the persisted history ends',
    typeof replay[0].messages_before === 'number' && replay[0].messages_before === 1,
    String(replay[0].messages_before));
  second.hangup();

  // ---- 4) the finished answer is on disk --------------------------------
  const sess = JSON.parse((await getJson(agentPort, '/api/sessions/' + encodeURIComponent(sid))).body);
  const answers = sess.messages.filter((m) => m.role === 'assistant');
  check('the whole answer was persisted', answers.length === 1 && answers[0].content.length === CHUNKS,
    answers.map((m) => 'len=' + m.content.length).join(','));

  // ---- 5) a finished turn is reaped, not reported as running ------------
  await sleep(400);
  st = await turnStatus(sid);
  check('a finished turn reports running:false', st.running === false, JSON.stringify(st));
  const gone = await postJson(agentPort, '/api/chat/attach', { session_id: sid });
  check('attaching to a reaped turn is a clean 404', gone.status === 404, 'HTTP ' + gone.status);

  // ---- 6) `since` replays only the tail ---------------------------------
  const partial = sseRequest(agentPort, '/api/chat', { workspace_id: wsId, message: 'and again' });
  await sleep(2000);
  const sid2 = (partial.frames.find((f) => f.session_id) || {}).session_id;
  const cut = partial.frames[partial.frames.length - 1].seq;
  const tail = sseRequest(agentPort, '/api/chat/attach', { session_id: sid2, since: cut });
  await sleep(1200);
  check('since= starts right after the given seq',
    tail.frames.length > 0 && tail.frames[0].seq === cut + 1,
    'since=' + cut + ' first=' + (tail.frames[0] || {}).seq);
  partial.hangup();
  await postJson(agentPort, '/api/chat/stop', { session_id: sid2 });
  await sleep(800);
  tail.hangup();

  // ---- 7) stop still cancels a turn nobody is watching ------------------
  upstream = { open: false, chunks: 0, finished: false };
  const third = sseRequest(agentPort, '/api/chat', { workspace_id: wsId, message: 'one more' });
  await sleep(1500);
  const sid3 = (third.frames.find((f) => f.session_id) || {}).session_id;
  third.hangup();                                    // walk away again
  await sleep(500);
  st = await turnStatus(sid3);
  check('a detached turn is still running before stop', st.running === true, JSON.stringify(st));
  const stop = await postJson(agentPort, '/api/chat/stop', { session_id: sid3 });
  await sleep(1500);
  st = await turnStatus(sid3);
  check('/api/chat/stop cancels a detached turn', stop.status === 200 && st.running === false,
    JSON.stringify(st));
  check('stop aborted the provider request', upstream.open === false && upstream.finished === false,
    'chunks=' + upstream.chunks);
  const afterStop = JSON.parse((await getJson(agentPort, '/api/chat/turns')).body);
  check('nothing is listed as running after stop', (afterStop.turns || []).length === 0,
    JSON.stringify(afterStop.turns));
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  agent.kill('SIGTERM');
  mock.close();
  await sleep(300);
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
