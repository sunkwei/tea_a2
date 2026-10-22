// Regression test for streaming render cost ("this page is slowing down Firefox").
//
// The original loop rebuilt the whole live bubble for every SSE delta:
// assistantNode() -> every markdown block re-parsed, every tool row rebuilt, the
// node replaced under #messages. A turn that streams N deltas therefore did N
// full rebuilds, and the markdown work grows with the answer, so the page ends
// up spending whole seconds of scripting time - which is what makes Firefox
// offer to stop the page.
//
// What is pinned here:
//   * during a pure text stream the live bubble is NOT replaced per delta
//     (a small constant per turn instead of ~one per delta);
//   * blocks that are already complete stay the same DOM nodes while the next
//     block streams (a marker property survives);
//   * the same holds across a tool call in the middle of the turn;
//   * the turn still renders correctly (final text, tool row, caret gone).
//
//   node tests/ui_render_perf.mjs [path-to-tea_agent-binary]
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
    let b = ''; r.on('data', (d) => (b += d)); r.on('end', () => res({ status: r.statusCode, body: b }));
  }).on('error', rej);
});

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || !detail ? '' : '  ' + detail));
};

// The mock streams: N1 deltas of text, a tool call, then N2 more deltas.
const N1 = 120;
const N2 = 200;
const DELTA_MS = 8;

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_render_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });

let agent, chrome, ws, mock;
try {
  const chunkOf = (delta, finish = null) => ({
    id: 'c', object: 'chat.completion.chunk',
    choices: [{ index: 0, delta, finish_reason: finish }],
  });
  const sse = (res, payload) => {
    if (res.writableEnded) return;
    res.write('data: ' + JSON.stringify(payload) + '\n\n');
  };
  /// Stream `n` content deltas. With `then`, hand the (still open) response to
  /// the caller instead of finishing the answer - used to emit a tool call.
  const streamText = (res, n, prefix, then) => {
    let i = 0;
    const timer = setInterval(() => {
      i++;
      sse(res, chunkOf({ content: prefix + i + ' ' }));
      if (i >= n) {
        clearInterval(timer);
        if (then) { then(); return; }
        sse(res, chunkOf({}, 'stop'));
        if (!res.writableEnded) { res.write('data: [DONE]\n\n'); res.end(); }
      }
    }, DELTA_MS);
    res.on('close', () => clearInterval(timer));
  };

  mock = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      if (!req.url.endsWith('/chat/completions')) { res.writeHead(404).end(); return; }
      let payload = {};
      try { payload = JSON.parse(body); } catch {}
      const tools = (payload.messages || []).filter((m) => m.role === 'tool');
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      if (tools.length) {
        // Second iteration: a plain (but chunky) answer, no tool calls.
        streamText(res, N2, '后续段落 ');
        return;
      }
      streamText(res, N1, '第一段 ', () => {
        // Middle of the turn: one tool call, so the live bubble has to grow a
        // tool row between two text blocks.
        sse(res, chunkOf({
          role: 'assistant',
          tool_calls: [{
            index: 0, id: 'call_1', type: 'function',
            function: { name: 'toolkit_exec', arguments: JSON.stringify({ command: 'echo perf' }) },
          }],
        }));
        sse(res, chunkOf({}, 'tool_calls'));
        res.write('data: [DONE]\n\n');
        res.end();
      });
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
`);

  const agentPort = await freePort();
  const cdpPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj')], { stdio: 'ignore' });
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
  await send('Performance.enable');
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(1800);

  // Count how often the live bubble is *replaced* under #messages, and tag the
  // first markdown block so we can prove it is not rebuilt per delta.
  await evalJs(`(function(){
    window.__perf = { liveReplaced: 0, mutations: 0 };
    var box = document.getElementById('messages');
    window.__perf.observer = new MutationObserver(function (records) {
      window.__perf.mutations += records.length;
      records.forEach(function (rec) {
        for (var i = 0; i < rec.addedNodes.length; i++) {
          var n = rec.addedNodes[i];
          if (n.nodeType === 1 && n.dataset && n.dataset.live === '1') window.__perf.liveReplaced++;
        }
      });
    });
    window.__perf.observer.observe(box, { childList: true, subtree: true });
    return true;
  })()`);

  await evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = 'perf please';
    ta.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);

  let tagged = false;
  for (let i = 0; i < 100 && !tagged; i++) {
    tagged = await evalJs(`(function(){
      var md = document.querySelector('#messages .msg.assistant .md');
      if (!md) return false;
      md.dataset.probe = 'first-md';
      return true;
    })()`);
    if (!tagged) await sleep(100);
  }
  check('the first markdown block rendered', tagged);

  let toolSeen = false;
  for (let i = 0; i < 200 && !toolSeen; i++) {
    toolSeen = await evalJs(`(function(){
      var row = document.querySelector('#messages .tool-row:not(.reasoning):not(.traj-row)');
      if (!row) return false;
      row.dataset.probe = 'tool-row';
      return true;
    })()`);
    if (!toolSeen) await sleep(100);
  }
  check('the tool row rendered mid-turn', toolSeen);

  const midCounts = await evalJs(`(function(){ return {
    replaced: window.__perf.liveReplaced, mutations: window.__perf.mutations };
  })()`);

  let finished = false;
  for (let i = 0; i < 300; i++) {
    if (await evalJs(`document.getElementById('send').className.indexOf('stop') < 0`)) { finished = true; break; }
    await sleep(150);
  }
  check('the turn finished', finished);

  await sleep(900);  // let finalizeStream reload and re-render

  const counts = await evalJs(`(function(){
    window.__perf.observer.disconnect();
    return { replaced: window.__perf.liveReplaced, mutations: window.__perf.mutations };
  })()`);

  const totalDeltas = N1 + N2;
  // Reported for the record; not asserted, because wall-clock numbers depend on
  // the machine. The rebuild count above is the deterministic signal.
  const perf = await send('Performance.getMetrics');
  const metric = (name) => {
    const m = (perf.result.metrics || []).find((x) => x.name === name);
    return m ? m.value : 0;
  };
  console.log(`  ${totalDeltas} deltas -> ${counts.replaced} live-bubble rebuild(s), ` +
    `${counts.mutations} mutation record(s), ` +
    `scripting ${(metric('ScriptDuration') * 1000).toFixed(0)} ms, ` +
    `style+layout ${((metric('RecalcStyleDuration') + metric('LayoutDuration')) * 1000).toFixed(0)} ms`);
  // Old behaviour: one whole-bubble replacement per delta (~320). New: only when
  // the structure changes (the second text block starts, the tool row appears and
  // then settles, the turn ends) - a small constant per turn.
  check('the live bubble is not rebuilt per delta',
    counts.replaced > 0 && counts.replaced <= 12,
    `replaced=${counts.replaced} for ${totalDeltas} deltas (mid-turn: ${midCounts.replaced})`);
  check('DOM mutations stay far below one-per-delta',
    counts.mutations < totalDeltas * 2,
    `mutations=${counts.mutations} for ${totalDeltas} deltas`);
  // The growing paragraph is not re-rendered per delta either: a plain-text
  // paragraph is updated by writing into its text node (no DOM rebuild at all),
  // and a markdown one is re-rendered at most every 100ms. Before this, every
  // delta re-ran the markdown parser over the whole answer - quadratic, and the
  // reason Firefox kept saying "this page is slowing down your browser".
  check('the growing paragraph is not rebuilt per delta',
    counts.mutations <= 80,
    `mutations=${counts.mutations} for ${totalDeltas} deltas`);

  const finalState = await evalJs(`(function(){
    var msgs = document.querySelectorAll('#messages .msg.assistant');
    var last = msgs[msgs.length - 1];
    return {
      assistantBubbles: msgs.length,
      hasSecond: last ? last.textContent.indexOf('后续段落') >= 0 : false,
      toolRows: document.querySelectorAll('#messages .tool-row:not(.reasoning):not(.traj-row)').length,
      toolProbe: (function(){ var r = document.querySelector('#messages .tool-row:not(.reasoning):not(.traj-row)');
                             return r ? (r.dataset.probe || '') : 'missing'; })(),
      caret: document.querySelectorAll('#messages .stream-caret').length,
      streamingClass: document.querySelectorAll('#messages .msg.assistant.streaming').length,
      text: last ? last.textContent.slice(0, 80) : ''
    };
  })()`);
  check('the final answer is rendered after the reload',
    finalState && finalState.hasSecond === true, JSON.stringify(finalState && finalState.text));
  check('the tool row survives the final render',
    finalState && finalState.toolRows === 1, JSON.stringify(finalState && finalState.toolRows));
  check('no streaming caret is left behind',
    finalState && finalState.caret === 0, JSON.stringify(finalState && finalState.caret));
  check('no bubble is left in the streaming state',
    finalState && finalState.streamingClass === 0, JSON.stringify(finalState && finalState.streamingClass));
  check('the first stream alone did not rebuild the bubble per delta',
    midCounts && midCounts.replaced <= 12,
    `mid-turn replacements=${midCounts && midCounts.replaced} for ${N1} deltas`);
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  if (agent) agent.kill('SIGTERM');
  if (mock) mock.close();
  await sleep(300);
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
