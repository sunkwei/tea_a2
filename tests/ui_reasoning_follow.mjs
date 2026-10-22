// When the turn is running and "深度思考" is open, the panel must follow the
// newest reasoning text.
//
// The reasoning panel is its own scroll box (.reason-panel: max-height 240px,
// overflow:auto). Its text grows while the model streams, and the scroll
// position stays where it was - so the panel used to sit at the top and show
// the *oldest* reasoning, with everything new hidden below the fold. Opening
// the panel had the same effect: it showed the beginning of the reasoning, not
// the output the user just asked for.
//
// What is pinned here:
//   * while the reasoning streams, the open panel sticks to the bottom;
//   * clicking the row open jumps to the newest output;
//   * a user who scrolled up inside the panel to read an earlier part is not
//     yanked back down by the next delta.
//
//   node tests/ui_reasoning_follow.mjs [path-to-tea_agent-binary]
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

// The mock streams reasoning in small pieces, slowly enough that the panel
// really overflows its 240px box before the answer starts.
const DELTAS = 60;
const DELTA_MS = 25;
const TEXT = '这是一段推理内容，用来把面板撑到超过最大高度，这样面板内部才会出现滚动。';

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_reason_'));
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

  mock = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      if (!req.url.endsWith('/chat/completions')) { res.writeHead(404).end(); return; }
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      let i = 0;
      const timer = setInterval(() => {
        i++;
        sse(res, chunkOf({ reasoning_content: TEXT + ' ' + i + ' ' }));
        if (i >= DELTAS) {
          clearInterval(timer);
          sse(res, chunkOf({ content: '最终答案' }, 'stop'));
          res.write('data: [DONE]\n\n');
          res.end();
        }
      }, DELTA_MS);
      res.on('close', () => clearInterval(timer));
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
  // The state of the live reasoning panel, as the browser sees it.
  const panelState = () => evalJs(`(function(){
    var row = document.querySelector('#messages .msg.assistant .tool-row.reasoning');
    if (!row) return null;
    var panel = row.querySelector('.reason-panel');
    if (!panel) return null;
    return {
      open: row.classList.contains('open'),
      scrollTop: Math.round(panel.scrollTop),
      scrollHeight: panel.scrollHeight,
      clientHeight: panel.clientHeight,
      gap: panel.scrollHeight - panel.scrollTop - panel.clientHeight,
      textLen: panel.textContent.length
    };
  })()`);

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(1800);

  await evalJs(`(function(){
    var ta = document.getElementById('input');
    ta.value = 'think please';
    ta.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);

  // Poll until the panel satisfies a condition. Sampling once would be a race:
  // the panel is updated by the render loop, which under load can lag behind
  // the stream by a frame.
  // Returns {ok, state}: `ok` says whether the condition was ever met. Without
  // it a timeout hands back the last sample, which can look fine by accident
  // (an empty panel has a gap of 0) and would turn a failure into a pass.
  const waitPanel = async (ok, tries = 120) => {
    for (let i = 0; i < tries; i++) {
      const st = await panelState();
      if (st && ok(st)) return { ok: true, state: st };
      await sleep(100);
    }
    return { ok: false, state: await panelState() };
  };

  // ---- 1. the panel follows the newest reasoning while it streams ----------
  const seen = await waitPanel((st) => st.open && st.scrollHeight > st.clientHeight + 20);
  check('the reasoning panel is open and overflowing while the turn runs',
    seen.ok, JSON.stringify(seen.state));
  const atBottom = await waitPanel((st) => st.open && st.scrollHeight > st.clientHeight + 20 &&
    st.gap <= 4);
  check('the open panel sits at the newest output, not at the top',
    atBottom.ok, JSON.stringify(atBottom.state));

  // ---- 2. clicking the row open jumps to the newest output ----------------
  const clickRow = `(function(){
    var row = document.querySelector('#messages .msg.assistant .tool-row.reasoning');
    if (!row) return false;
    var line = row.querySelector('.tool-line');
    line.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    return true;
  })()`;
  // The row collapses by itself once the answer starts streaming (the panel is
  // open by default only while there is no content yet), so open it again
  // before the collapse/open sequence - the run should not depend on timing.
  let now = await panelState();
  if (now && !now.open) {
    await evalJs(clickRow);
    await sleep(250);
  }
  // Collapse it first (the click handler scrolls on open), then open it again
  // and look at where the panel ends up.
  await evalJs(clickRow);
  await sleep(150);
  const collapsed = await panelState();
  check('the row can be collapsed', collapsed && !collapsed.open, JSON.stringify(collapsed));

  await evalJs(clickRow);
  const reopened = await waitPanel((st) => st.open && st.gap <= 4, 40);
  check('clicking it open jumps to the newest output',
    reopened.ok, JSON.stringify(reopened.state));

  // ---- 3. a user who scrolled up is not yanked back down ------------------
  await evalJs(`(function(){
    var panel = document.querySelector('#messages .msg.assistant .tool-row.reasoning .reason-panel');
    if (!panel) return false;
    panel.scrollTop = 0;
    panel.dispatchEvent(new Event('scroll'));
    return true;
  })()`);
  await sleep(600);   // more reasoning arrives in the meantime
  const held = await panelState();
  check('a panel the user scrolled up stays where they put it',
    held && held.scrollTop <= 4 && held.gap > 4, JSON.stringify(held));

  // ---- 4. and it is still the newest text when the turn is over -----------
  let finished = false;
  for (let i = 0; i < 300; i++) {
    if (await evalJs(`document.getElementById('send').className.indexOf('stop') < 0`)) { finished = true; break; }
    await sleep(150);
  }
  check('the turn finished', finished);
  await sleep(900);   // the final reload re-renders the bubbles
  const final = await panelState();
  check('the reasoning text is complete in the panel',
    final && final.textLen > TEXT.length, JSON.stringify(final));
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
