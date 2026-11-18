// Browser test for the 并行中的轮次 panel.
//
// A workspace can run several turns at once - different sessions are
// different agents. The subagents of a plan are such turns, and their
// trajectories are NOT in the conversation on the screen (each of them is a
// session of its own). The panel is where they are visible: one card per
// turn, with its state and its process, live.
//
//   node tests/ui_parallel_turns.mjs [path-to-tea_agent-binary]
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
  s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); });
});
const getJson = (port, p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port, path: p }, (r) => {
    let b = ''; r.on('data', (d) => (b += d)); r.on('end', () => res({ status: r.statusCode, body: b }));
  }).on('error', rej);
});
const postJson = (port, p, body) => new Promise((res, rej) => {
  const data = JSON.stringify(body || {});
  const req = http.request({ host: '127.0.0.1', port, path: p, method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) } },
    (r) => {
      let b = ''; r.on('data', (d) => (b += d)); r.on('end', () => res({ status: r.statusCode, body: b }));
    });
  req.on('error', rej);
  req.write(data); req.end();
});

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? '  ' + detail : ''));
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_par_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj'), { recursive: true });

let agent, chrome, ws, mockServer;
const pending = new Map();
let msgId = 0;
const send = (method, params) => new Promise((res) => {
  const mid = ++msgId; pending.set(mid, res);
  ws.send({ id: mid, method, params });
});
const evalJs = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.result && r.result.exceptionDetails) {
    throw new Error(JSON.stringify(r.result.exceptionDetails.exception || r.result.exceptionDetails));
  }
  return r.result && r.result.result ? r.result.result.value : undefined;
};
const waitState = async (expr, tries = 80) => {
  for (let i = 0; i < tries; i++) {
    const v = await evalJs(expr);
    if (v) return v;
    await sleep(200);
  }
  return null;
};

try {
  // ---- a provider that keeps the turn running for a moment ---------------
  const providerPort = await freePort();
  mockServer = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      if (!req.url.endsWith('/chat/completions')) { res.writeHead(404).end(); return; }
      const msgs = JSON.parse(body || '{}').messages || [];
      const afterTool = msgs.length && msgs[msgs.length - 1].role === 'tool';
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      const send2 = (o) => res.write('data: ' + JSON.stringify(o) + '\n\n');
      // The first answer asks for a tool; the follow-up answers with text.
      // Both come late, so the turn is still running while the test looks.
      setTimeout(() => {
        if (!afterTool) {
          send2({
            id: 'c', choices: [{
              index: 0,
              delta: {
                role: 'assistant',
                tool_calls: [{
                  index: 0, id: 'call_q', type: 'function',
                  function: {
                    name: 'toolkit_workspace_search',
                    arguments: JSON.stringify({ query: 'PARALLELMARKER' }),
                  },
                }],
              },
              finish_reason: null,
            }],
          });
          send2({ id: 'c', choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] });
        } else {
          send2({ id: 'c', choices: [{ index: 0, delta: { content: '子 agent 找到了 PARALLELMARKER' },
            finish_reason: null }] });
          send2({ id: 'c', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] });
        }
        res.end();
      }, 2500);
    });
  });
  await new Promise((r) => mockServer.listen(providerPort, '127.0.0.1', r));

  fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:${providerPort}
    api_key: sk-test
    default_model: m
    models: {m: {max_context_tokens: 32768, max_output_tokens: 4096, supports_tools: true}}
`);

  const agentPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj')], { stdio: 'ignore' });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  // ---- two sessions: one on the screen, one running in the background ---
  const list = await getJson(agentPort, '/api/workspaces');
  const wsId = JSON.parse(list.body).workspaces[0].id;
  // 两个会话：一个空着（页面上看的就是它），一个在后台跑。
  const madeA = await postJson(agentPort, `/api/workspaces/${wsId}/sessions`, {});
  const sessionA = JSON.parse(madeA.body).id || (JSON.parse(madeA.body).session || {}).id;
  const madeB = await postJson(agentPort, `/api/workspaces/${wsId}/sessions`, {});
  const sessionB = JSON.parse(madeB.body).id || (JSON.parse(madeB.body).session || {}).id;
  check('two sessions were created', !!sessionA && !!sessionB,
    String(madeA.body).slice(0, 120) + ' / ' + String(madeB.body).slice(0, 120));

  const turn = await postJson(agentPort, '/api/chat', {
    workspace_id: wsId, session_id: sessionB, message: '子 agent 的活儿', async: true,
  });
  const turnBody = JSON.parse(turn.body);
  check('the background turn started', !!(turnBody.turn_id || (turnBody.data || {}).turn_id),
    String(turn.body).slice(0, 200));

  // ---- the browser -------------------------------------------------------
  chrome = await acquireBrowser(tmp);
  const target = { webSocketDebuggerUrl: chrome.webSocketDebuggerUrl };
  if (!target) throw new Error('no chrome page target');
  ws = wsConnect(target.webSocketDebuggerUrl);
  await new Promise((r) => ws.on('open', r));
  ws.on('message', (raw) => {
    const m = JSON.parse(raw);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  await send('Page.enable');
  await send('Runtime.enable');
  // 页面里的错误要看得见：不然只能看到「面板是空的」。
  await send('Page.addScriptToEvaluateOnNewDocument', {
    source: "window.__errs=[];window.addEventListener('error',function(e){window.__errs.push(String(e.message)+' @'+e.filename+':'+e.lineno);});",
  });
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await waitState('window.teaState && window.teaState.workspace ? true : false');

  // The page opens the newest session - the one that is running. Open the
  // other one, so the running turn is somebody else's turn.
  await evalJs(`(function(){
    var rows = document.querySelectorAll('#session-list .sess-row');
    for (var i = 0; i < rows.length; i++) {
      if ((rows[i].textContent || '').indexOf('子 agent 的活儿') < 0) { rows[i].click(); return true; }
    }
    return false;
  })()`);
  await sleep(1000);

  // ---- the panel shows the other turn ------------------------------------
  const errs = await evalJs('(window.__errs || []).join(" | ")');
  if (errs) console.log('PAGE ERRORS: ' + errs);

  const panel = await waitState(`(function(){
    var box = document.getElementById('parallel');
    if (!box || box.classList.contains('hidden')) return null;
    var cards = box.querySelectorAll('.parallel-card');
    if (!cards.length) return null;
    var card = cards[0];
    return {
      count: cards.length,
      title: (card.querySelector('.parallel-card-title') || {}).textContent || '',
      state: (card.querySelector('.parallel-state') || {}).textContent || '',
      log: (card.querySelector('.parallel-log') || {}).textContent || '',
    };
  })()`);
  check('the panel is there', !!panel, JSON.stringify(panel));
  check('it has one card for the other turn', panel && panel.count === 1, JSON.stringify(panel));
  check('the card names the turn', panel && (panel.title || '').indexOf('子 agent') >= 0,
    JSON.stringify(panel));
  check('the card says it is running', panel && (panel.state || '').indexOf('进行中') >= 0,
    JSON.stringify(panel));

  // ---- the process shows up live -----------------------------------------
  const live = await waitState(`(function(){
    var box = document.getElementById('parallel');
    if (!box) return null;
    var log = box.querySelector('.parallel-log');
    var txt = (log && log.textContent) || '';
    if (txt.indexOf('toolkit_workspace_search') < 0) return null;
    return { text: txt.slice(0, 300) };
  })()`, 100);
  check('the card shows the tools the turn called', !!live, JSON.stringify(live));

  // ---- and the turn's own answer ----------------------------------------
  const answered = await waitState(`(function(){
    var box = document.getElementById('parallel');
    if (!box) return null;
    var txt = (box.querySelector('.parallel-log') || {}).textContent || '';
    if (txt.indexOf('PARALLELMARKER') < 0) return null;
    return { text: txt.slice(0, 400) };
  })()`, 120);
  check('the card shows what the turn said', !!answered, JSON.stringify(answered));

  // ---- the card's state follows the turn ---------------------------------
  const done = await waitState(`(function(){
    var box = document.getElementById('parallel');
    if (!box) return null;
    var st = (box.querySelector('.parallel-state') || {}).textContent || '';
    if (st.indexOf('完成') < 0) return null;
    return { state: st };
  })()`, 120);
  check('the card says the turn is done', !!done, JSON.stringify(done));
} catch (e) {
  check('the test ran to the end', false, String((e && e.stack) || e));
} finally {
  try { if (ws) ws.close(); } catch {}
  try { if (chrome) await chrome.close(); } catch {}
  try { if (agent) agent.kill(); } catch {}
  try { if (mockServer) mockServer.close(); } catch {}
}

const failed = results.filter((r) => !r.ok);
console.log('');
console.log(`${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) {
  console.log('FAILED: ' + failed.map((f) => f.name).join(', '));
  process.exit(1);
}
