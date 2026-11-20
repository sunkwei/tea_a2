// Browser test for the 并行中的轮次 hot zones.
//
// A workspace can run several turns at once - different sessions are different
// agents. The subagents of a plan are such turns, and their trajectories are NOT
// in the conversation on the screen (each of them is a session of its own).
//
// 2026-11-20 the panel changed shape: it is no longer a strip above the chat
// (that pushed the conversation down by ~150px). It is now **one hot zone per
// running turn** in the bottom-right corner, above the composer. Hovering a hot
// zone for 180ms opens the panel (7 lines of process), the mouse leaving does
// NOT close it - only the 「关闭」 button, a click outside, or hovering another
// hot zone does. Finished turns disappear at once (no 「完成」 state, no 60s).
//
// That is driven by a real 180ms timer + real mouseenter, so this test has to
// dispatch REAL mouse events (Input.dispatchMouseEvent). Synthesised DOM events
// from Runtime.evaluate would not exercise the same path.
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
const sendJson = (port, method, p, body) => new Promise((res, rej) => {
  const data = JSON.stringify(body || {});
  const req = http.request({ host: '127.0.0.1', port, path: p, method,
    headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) } },
    (r) => {
      let b = ''; r.on('data', (d) => (b += d)); r.on('end', () => res({ status: r.statusCode, body: b }));
    });
  req.on('error', rej);
  req.write(data); req.end();
});
const postJson = (port, p, body) => sendJson(port, 'POST', p, body);
const patchJson = (port, p, body) => sendJson(port, 'PATCH', p, body);

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
const waitState = async (expr, tries = 80, every = 200) => {
  for (let i = 0; i < tries; i++) {
    const v = await evalJs(expr);
    if (v) return v;
    await sleep(every);
  }
  return null;
};

// ---- real mouse ------------------------------------------------------------
const mouseTo = (x, y) => send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none' });
const mouseClick = async (x, y) => {
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 });
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1 });
};
const centerOf = async (sel) => evalJs(`(function(){
  var el = document.querySelector(${JSON.stringify(sel)});
  if (!el) return null;
  var b = el.getBoundingClientRect();
  if (!b.width || !b.height) return null;
  return { x: Math.round(b.left + b.width / 2), y: Math.round(b.top + b.height / 2) };
})()`);
const mouseAway = () => mouseTo(12, 120);          // 顶栏下面一点，别碰热区也别点东西
const hoverSel = async (sel) => {
  const at = await centerOf(sel);
  if (!at) return false;
  await mouseTo(at.x, at.y);
  return true;
};
const panelState = () => evalJs(`(function(){
  var p = document.getElementById('parallel-pop');
  if (!p || p.classList.contains('hidden')) return null;
  var log = p.querySelector('.parallel-log');
  return {
    name: (p.querySelector('.pz-name') || {}).textContent || '',
    text: (log && log.textContent) || '',
    maxH: log ? parseFloat(getComputedStyle(log).maxHeight) : 0,
    more: p.querySelectorAll('.pz-more-item').length,
    moreText: p.querySelector('.pz-more-list') ? p.querySelector('.pz-more-list').textContent : ''
  };
})()`);
/// 悬停之后等浮层出现，返回等了多少毫秒（超时返回 -1）。
const waitPanel = async (maxMs = 2000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < maxMs) {
    const v = await panelState();
    if (v) return Date.now() - t0;
    await sleep(30);
  }
  return -1;
};
const panelHidden = () => evalJs(`(function(){
  var p = document.getElementById('parallel-pop');
  return !p || p.classList.contains('hidden');
})()`);
const chips = () => evalJs(`(function(){
  var box = document.getElementById('parallel-hot');
  if (!box) return [];
  return Array.prototype.map.call(box.querySelectorAll('.pz-chip[data-turn-id]'), function (c) {
    var b = c.getBoundingClientRect();
    return { id: c.dataset.turnId, top: Math.round(b.top), left: Math.round(b.left),
             width: Math.round(b.width), height: Math.round(b.height) };
  });
})()`);

try {
  // ---- a provider that keeps a turn running for a while -------------------
  // 「慢任务」：工具调用 2.5s 后回来，最后那句回答再等 45s —— 这一轮能跑近一分钟，
  // 足够我们把出现、悬停、移开、关闭、切换、+N 全走一遍。其它消息 2.5s + 2.5s。
  const providerPort = await freePort();
  mockServer = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      if (!req.url.endsWith('/chat/completions')) { res.writeHead(404).end(); return; }
      const msgs = JSON.parse(body || '{}').messages || [];
      const afterTool = msgs.length && msgs[msgs.length - 1].role === 'tool';
      const said = msgs.filter((m) => m.role === 'user')
        .map((m) => (typeof m.content === 'string' ? m.content : JSON.stringify(m.content || ''))).join(' ');
      const delay = afterTool && /慢任务/.test(said) ? 45000 : 2500;
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      const send2 = (o) => res.write('data: ' + JSON.stringify(o) + '\n\n');
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
      }, delay);
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

  // ---- the sessions -------------------------------------------------------
  const list = await getJson(agentPort, '/api/workspaces');
  const wsId = JSON.parse(list.body).workspaces[0].id;
  const mkSession = async () => {
    const r = await postJson(agentPort, `/api/workspaces/${wsId}/sessions`, {});
    const b = JSON.parse(r.body);
    return b.id || (b.session || {}).id;
  };
  const sessionA = await mkSession();   // 页面上看的就是它（空会话）
  const sessionB = await mkSession();   // 页面打开之前就跑完的那一轮
  const sessionC = await mkSession();   // 慢任务-甲
  const sessionD = await mkSession();   // 慢任务-乙
  const sessionE = await mkSession();   // 短任务-丙
  const sessionF = await mkSession();   // 慢任务-丁
  const sessionG = await mkSession();   // 慢任务-戊
  const sessionH = await mkSession();   // 慢任务-己
  check('eight sessions were created',
    !!sessionA && !!sessionB && !!sessionC && !!sessionD && !!sessionE && !!sessionF && !!sessionG && !!sessionH,
    [sessionA, sessionB, sessionC, sessionD].join(','));
  // 会话的标题就是它的第一句话。A 是空会话，给它一个能认出来的名字。
  await patchJson(agentPort, `/api/sessions/${sessionA}`, { title: '屏幕上这个会话' });

  // ---- 先跑完一轮：它在页面打开之前就结束了 ------------------------------
  // 只列**进行中**的轮次：早结束的不该留下任何热区（1.6.1 那套「结束后再留
  // 一分钟」已经取消——否则一个用过几十次的工作区会把右下角堆满）。
  const oldTurn = await postJson(agentPort, '/api/chat', {
    workspace_id: wsId, session_id: sessionB, message: '早就跑完的活儿', async: true,
  });
  const oldBody = JSON.parse(oldTurn.body);
  const oldId = oldBody.turn_id || (oldBody.data || {}).turn_id;
  check('an earlier turn started', !!oldId, String(oldTurn.body).slice(0, 200));
  let oldState = '';
  for (let i = 0; i < 160; i++) {
    await sleep(250);
    const st = await getJson(agentPort, '/api/turns/' + oldId);
    if (st.status !== 200) continue;
    oldState = JSON.parse(st.body).state || '';
    if (oldState && oldState !== 'running') break;
  }
  check('the earlier turn is over', !!oldState && oldState !== 'running', oldState);

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
  // 页面里的错误要看得见：不然只能看到「热区不见了」。
  await send('Page.addScriptToEvaluateOnNewDocument', {
    source: "window.__errs=[];window.addEventListener('error',function(e){window.__errs.push(String(e.message)+' @'+e.filename+':'+e.lineno);});",
  });
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await waitState('window.teaState && window.teaState.workspace ? true : false');

  // 打开那个空会话（A）：这样后台跑的轮次都算「别人的」。
  await waitState(`(function(){
    var rows = document.querySelectorAll('#session-list .sess-row');
    for (var i = 0; i < rows.length; i++) {
      if ((rows[i].textContent || '').indexOf('屏幕上这个会话') >= 0) { rows[i].click(); return true; }
    }
    return false;
  })()`, 40);
  const onA = await waitState(
    `window.teaState.session && window.teaState.session.id === ${JSON.stringify(sessionA)}`, 40);
  check('the empty session is the one on the screen', !!onA);

  // ---- 布局：对话区上面不该再有任何东西 ----------------------------------
  const strip = await evalJs(`(function(){
    var top = document.getElementById('topbar').getBoundingClientRect();
    var view = document.getElementById('view-chat').getBoundingClientRect();
    return { gap: Math.round(view.top - top.bottom) };
  })()`);
  check('nothing is pushed between the topbar and the chat view',
    !!strip && strip.gap >= -1 && strip.gap <= 8, JSON.stringify(strip));

  // ---- 早就结束的那一轮不该留下热区 --------------------------------------
  await sleep(1500);
  const stale = await chips();
  check('a turn that was over before the page opened leaves no hot zone',
    stale && stale.length === 0, JSON.stringify(stale));

  // ---- 现在起一轮真正在跑的（甲）------------------------------------------
  const turnJia = await postJson(agentPort, '/api/chat', {
    workspace_id: wsId, session_id: sessionC, message: '慢任务-甲', async: true,
  });
  const jiaBody = JSON.parse(turnJia.body);
  const jiaId = jiaBody.turn_id || (jiaBody.data || {}).turn_id;
  check('the background turn started', !!jiaId, String(turnJia.body).slice(0, 200));

  const one = await waitState(`(function(){
    var box = document.getElementById('parallel-hot');
    if (!box || box.classList.contains('hidden')) return null;
    var cs = box.querySelectorAll('.pz-chip[data-turn-id]');
    if (cs.length !== 1) return null;
    var c = cs[0], b = c.getBoundingClientRect();
    var send = document.getElementById('send').getBoundingClientRect();
    var col = document.querySelector('.composer-wrap .chat-col').getBoundingClientRect();
    return {
      id: c.dataset.turnId, height: Math.round(b.height),
      pos: getComputedStyle(box).position,
      rightGap: Math.round(Math.abs(b.right - send.right)),
      aboveComposer: b.bottom <= col.top + 1,
      overlapsSend: !(b.right < send.left || b.left > send.right || b.bottom < send.top || b.top > send.bottom)
    };
  })()`, 60);
  check('the hot zone shows up for the running turn', one && one.id === jiaId, JSON.stringify(one));
  check('the hot zone is a fixed overlay (it does not take layout space)',
    one && one.pos === 'fixed', JSON.stringify(one));
  check('the hot zone is one line high', one && one.height > 0 && one.height <= 32, JSON.stringify(one));
  check('the hot zone sits above the composer, right-aligned with the send button, and clear of it',
    one && one.rightGap <= 2 && one.aboveComposer && !one.overlapsSend, JSON.stringify(one));

  // ---- 悬停：180ms 后弹出 ------------------------------------------------
  if (!(await hoverSel('.pz-chip[data-turn-id]'))) throw new Error('the hot zone cannot be hovered');
  const waited = await waitPanel();
  check('hovering the hot zone opens the panel', waited >= 0 && waited <= 700, waited + ' ms');
  const panel = await panelState();
  check('the panel shows the tools the turn called',
    !!panel && panel.text.indexOf('toolkit_workspace_search') >= 0,
    JSON.stringify(panel && panel.text.slice(0, 120)));
  check('the log is 6-7 lines tall', !!panel && panel.maxH >= 96 && panel.maxH <= 120,
    JSON.stringify(panel && panel.maxH));

  // ---- 鼠标移开：**不收起**（要能读 7 行、要能点「展开」）-----------------
  await mouseAway();
  await sleep(1200);
  check('the panel stays open after the mouse leaves', !(await panelHidden()));

  // ---- 「关闭」：收起，热区还在 ------------------------------------------
  const clicked = await evalJs(`(function(){
    var b = document.querySelector('#parallel-pop .pz-close');
    if (!b) return false;
    b.click();
    return true;
  })()`);
  await sleep(200);
  const afterClose = await chips();
  check('the 「关闭」 button collapses the panel and keeps the hot zone',
    !!clicked && (await panelHidden()) && afterClose.length === 1, JSON.stringify(afterClose));

  // ---- 点浮层外也收起 ----------------------------------------------------
  await hoverSel('.pz-chip[data-turn-id]');
  const reopened = await waitPanel();
  await mouseClick(300, 320);
  await sleep(250);
  check('clicking outside collapses it too', reopened >= 0 && (await panelHidden()));

  // ---- 第二轮：两个热区，最新的那个在最下面 ------------------------------
  const turnYi = await postJson(agentPort, '/api/chat', {
    workspace_id: wsId, session_id: sessionD, message: '慢任务-乙', async: true,
  });
  const yiBody = JSON.parse(turnYi.body);
  const yiId = yiBody.turn_id || (yiBody.data || {}).turn_id;
  const two = await waitState(`(function(){
    var cs = document.querySelectorAll('#parallel-hot .pz-chip[data-turn-id]');
    if (cs.length !== 2) return null;
    var out = [];
    for (var i = 0; i < cs.length; i++) {
      var b = cs[i].getBoundingClientRect();
      out.push({ id: cs[i].dataset.turnId, y: Math.round(b.top) });
    }
    return out;
  })()`, 40);
  check('a second running turn adds a second hot zone', !!two && two.length === 2, JSON.stringify(two));
  check('the newest hot zone is the lowest one',
    !!two && two[1].id === yiId && two[1].y > two[0].y, JSON.stringify(two));

  // ---- 悬停另一个热区：切换（同时只有一个浮层）---------------------------
  await mouseAway();
  await hoverSel(`.pz-chip[data-turn-id="${jiaId}"]`);
  await waitPanel();
  await hoverSel(`.pz-chip[data-turn-id="${yiId}"]`);
  await sleep(500);
  const switched = await panelState();
  const popCount = await evalJs(`document.querySelectorAll('#parallel-pop').length`);
  check('hovering another hot zone switches the panel',
    !!switched && switched.name.indexOf('慢任务-乙') >= 0 && popCount === 1,
    JSON.stringify(switched && switched.name) + ' / popovers=' + popCount);

  // ---- 五个在跑：四个热区 + 「+N」----------------------------------------
  for (const [sid, text] of [[sessionF, '慢任务-丁'], [sessionG, '慢任务-戊'], [sessionH, '慢任务-己']]) {
    await postJson(agentPort, '/api/chat', { workspace_id: wsId, session_id: sid, message: text, async: true });
  }
  const many = await waitState(`(function(){
    var box = document.getElementById('parallel-hot');
    if (!box) return null;
    var cs = box.querySelectorAll('.pz-chip[data-turn-id]');
    var more = box.querySelector('.pz-more');
    if (cs.length !== 4 || !more) return null;
    return { chips: cs.length, more: more.textContent };
  })()`, 40);
  check('five running turns collapse into four hot zones plus 「+N」',
    !!many && many.chips === 4 && many.more === '+1', JSON.stringify(many));

  await mouseAway();
  await hoverSel('.pz-more');
  const morePanel = (await waitPanel()) >= 0 ? await panelState() : null;
  check('「+N」 lists the turns that were merged away',
    !!morePanel && morePanel.more === 1 && morePanel.moreText.indexOf('慢任务-甲') >= 0,
    JSON.stringify(morePanel && morePanel.moreText));

  // ---- 短任务：出现，然后**结束即消失** ----------------------------------
  const turnBing = await postJson(agentPort, '/api/chat', {
    workspace_id: wsId, session_id: sessionE, message: '短任务-丙', async: true,
  });
  const bingBody = JSON.parse(turnBing.body);
  const bingId = bingBody.turn_id || (bingBody.data || {}).turn_id;
  const bingShown = await waitState(
    `!!document.querySelector('#parallel-hot .pz-chip[data-turn-id="${bingId}"]')`, 40);
  check('a short turn gets its own hot zone', !!bingShown, String(bingId));
  const bingGone = await waitState(
    `!document.querySelector('#parallel-hot .pz-chip[data-turn-id="${bingId}"]')`, 80, 250);
  check('a turn that ends takes its hot zone with it (no 「完成」 state, no 60s)', !!bingGone);

  // ---- 别的页面：一个热区都不显示 ---------------------------------------
  await mouseAway();
  await evalJs(`(function(){
    var b = document.querySelector('.nav-row[data-view="files"]');
    if (b) b.click();
    return !!b;
  })()`);
  await sleep(500);
  const onFiles = await evalJs(`(function(){
    var box = document.getElementById('parallel-hot');
    return { hidden: box.classList.contains('hidden'), view: window.teaState.view };
  })()`);
  check('the hot zones are hidden on the other pages',
    !!onFiles && onFiles.hidden && onFiles.view === 'files', JSON.stringify(onFiles));
  await evalJs(`(function(){
    var b = document.querySelector('.nav-row[data-view="chat"]');
    if (b) b.click();
    return !!b;
  })()`);
  const backChat = await waitState(`(function(){
    var box = document.getElementById('parallel-hot');
    return !!box && !box.classList.contains('hidden') && box.querySelectorAll('.pz-chip').length > 0;
  })()`, 40);
  check('they come back on the chat page', !!backChat);

  const errs = await evalJs('(window.__errs || []).join(" | ")');
  check('the page logged no errors', !errs, errs || '');
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
}
// 一定要退出：mock 服务端的 keep-alive 连接还开着，事件循环不会自己结束。
process.exit(failed.length ? 1 : 0);
