// Browser test for "本轮产出" - the download links under the final answer.
//
// The contract pinned here:
//   * a session whose last turn produced files shows a strip under the final
//     assistant message (not under earlier turns, not while the turn is live);
//   * each row names the file, its kind and its size, and gives a download link
//     pointing at /download (an attachment), plus a "查看" for image/video/audio;
//   * the link really works: the browser gets the exact bytes with
//     Content-Disposition: attachment;
//   * files from an OLDER turn are not listed under the latest answer;
//   * the strip appears live: while a turn runs the agent emits `artifacts`, and
//     once it ends the strip is there without a reload;
//   * the strip is absent when the turn produced nothing.
//
//   node tests/ui_artifacts.mjs [path-to-tea_agent-binary]
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

const REPORT = '# 报告\n\n结论：链接都通。\n';
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_artifacts_ui_'));
const proj = path.join(tmp, 'proj');
const sessions = path.join(proj, '.tea_agent_run', 'sessions');
fs.mkdirSync(sessions, { recursive: true });
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.writeFileSync(path.join(proj, 'report.md'), REPORT);
fs.writeFileSync(path.join(proj, 'old.csv'), 'a,b\n1,2\n');

// Turn 1 produced old.csv; turn 2 (the last one) produced report.md. The strip
// must only list report.md.
const SID = 's_artifacts_ui';
fs.writeFileSync(path.join(sessions, SID + '.json'), JSON.stringify({
  id: SID, title: '产出与下载', created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:03Z',
  messages: [
    { role: 'user', content: '先导出旧数据', created_at: '2026-01-01T00:00:00Z' },
    { role: 'assistant', content: '已导出 old.csv', created_at: '2026-01-01T00:00:01Z' },
    { role: 'user', content: '再写个报告', created_at: '2026-01-01T00:00:02Z' },
    { role: 'assistant', content: '报告写好了，见下方链接。', created_at: '2026-01-01T00:00:03Z' },
  ],
  artifacts: [
    { path: 'old.csv', bytes: 8, kind: 'data', tool: 'toolkit_file',
      created_at: '2026-01-01T00:00:01Z', turn: 1 },
    { path: 'report.md', bytes: REPORT.length, kind: 'document', tool: 'toolkit_file',
      created_at: '2026-01-01T00:00:03Z', turn: 2 },
  ],
}, null, 2));
fs.writeFileSync(path.join(sessions, 'index.json'), JSON.stringify([{
  id: SID, title: '产出与下载', created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:03Z', message_count: 4,
}], null, 2));

let agent, chrome, ws, mockServer;
try {
  // The live part: the first request asks toolkit_exec to create a PNG; the
  // follow-up answers with text.
  let phase = 0;
  mockServer = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => (body += d));
    req.on('end', () => {
      if (!req.url.endsWith('/chat/completions')) { res.writeHead(404).end(); return; }
      const msgs = JSON.parse(body || '{}').messages || [];
      const afterTool = msgs.length && msgs[msgs.length - 1].role === 'tool';
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      const send = (o) => res.write('data: ' + JSON.stringify(o) + '\n\n');
      if (!afterTool && phase === 0) {
        phase = 1;
        send({
          id: 'c', choices: [{
            index: 0,
            delta: {
              role: 'assistant',
              tool_calls: [{
                index: 0, id: 'call_gen', type: 'function',
                function: {
                  name: 'toolkit_exec',
                  arguments: JSON.stringify({
                    command: "python3 -c \"import base64;" +
                      "open('chart.png','wb').write(base64.b64decode(" +
                      "'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='))\"",
                    timeout_ms: 30000,
                  }),
                },
              }],
            },
            finish_reason: null,
          }],
        });
        send({ id: 'c', choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] });
      } else {
        send({ id: 'c', choices: [{ index: 0, delta: { content: '图生成好了，见下方。' },
          finish_reason: null }] });
        send({ id: 'c', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] });
      }
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
    default_model: m
    models: {m: {max_context_tokens: 32768, supports_tools: true}}
`);

  const agentPort = await freePort();
  const cdpPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'),
    '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', proj], { stdio: 'ignore' });
  for (let i = 0; i < 120; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
    '--window-size=1280,900',
    `--remote-debugging-port=${cdpPort}`, `--user-data-dir=${path.join(tmp, 'chrome')}`,
    'about:blank'], { stdio: 'ignore' });
  let target = null;
  for (let i = 0; i < 120; i++) {
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
  await sleep(2000);

  const strip = () => evalJs(`(function(){
    var box = document.querySelector('.artifacts');
    if (!box) return null;
    var afterAssistant = false;
    var prev = box.previousElementSibling;
    if (prev) afterAssistant = prev.classList.contains('msg') &&
      prev.classList.contains('assistant');
    return {
      title: box.querySelector('.a-title') ? box.querySelector('.a-title').textContent : '',
      count: box.querySelector('.a-count') ? box.querySelector('.a-count').textContent : '',
      afterAssistant: afterAssistant,
      rows: Array.prototype.map.call(box.querySelectorAll('.artifact'), function (li) {
        var dl = li.querySelector('.a-dl');
        return {
          name: li.querySelector('.a-name').textContent,
          meta: li.querySelector('.a-meta').textContent,
          href: dl ? dl.getAttribute('href') : '',
          download: dl ? dl.hasAttribute('download') : false,
          ext: !!li.querySelector('.a-view')
        };
      })
    };
  })()`);

  const shown = await waitFor(async () => {
    const q = await strip();
    return q && q.rows.length > 0;
  });
  check('the final answer shows this turn\'s output', shown, JSON.stringify(await strip()));

  let q = await strip();
  check('the strip sits right under the assistant message', q.afterAssistant === true,
    JSON.stringify(q.afterAssistant));
  check('it is titled 本轮产出', q.title === '本轮产出', q.title);
  check('only this turn\'s file is listed (not last turn\'s)',
    q.rows.length === 1 && q.rows[0].name === 'report.md',
    JSON.stringify(q.rows.map((r) => r.name)));
  check('the row names the kind and the size',
    q.rows[0].meta.indexOf('文档') === 0 && q.rows[0].meta.indexOf('B') > 0, q.rows[0].meta);
  check('the download link points at /download',
    q.rows[0].href.indexOf('/download?path=report.md') > 0, q.rows[0].href);
  check('the link asks the browser to save it', q.rows[0].download === true);
  check('a document has no preview button', q.rows[0].ext === false);

  // ---- the link really downloads the file --------------------------------
  const downloaded = await evalJs(`(function(){
    var href = document.querySelector('.artifact .a-dl').getAttribute('href');
    return fetch(href).then(function (r) {
      return r.text().then(function (t) {
        return { status: r.status, body: t,
                 disp: r.headers.get('Content-Disposition'),
                 type: r.headers.get('Content-Type') };
      });
    });
  })()`);
  check('the download URL serves the file',
    downloaded && downloaded.status === 200 && downloaded.body === REPORT,
    JSON.stringify(downloaded && [downloaded.status, downloaded.body && downloaded.body.slice(0, 20)]));
  check('it is served as an attachment',
    downloaded.disp && downloaded.disp.indexOf('attachment') === 0, downloaded.disp);
  check('with the right MIME type',
    downloaded.type && downloaded.type.indexOf('text/markdown') === 0, downloaded.type);

  // ---- a running turn adds a new file live -------------------------------
  await evalJs(`(function(){
    var i = document.getElementById('input');
    i.value = '生成一张图';
    i.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);
  const live = await waitFor(async () => {
    const s = await strip();
    return s && s.rows.some((r) => r.name === 'chart.png');
  }, 30000);
  check('a file produced during the turn appears after it', live,
    JSON.stringify((await strip() || {}).rows));
  q = await strip();
  check('the new image is listed with a preview', q.rows.some((r) => r.name === 'chart.png'
    && r.ext === true), JSON.stringify(q.rows));
  check('the earlier turn\'s file is still hidden', q.rows.every((r) => r.name !== 'old.csv'),
    JSON.stringify(q.rows.map((r) => r.name)));
  const live2 = await evalJs(`(function(){
    var rows = Array.prototype.map.call(document.querySelectorAll('.artifact'), function (li) {
      return { name: li.querySelector('.a-name').textContent,
               href: li.querySelector('.a-dl').getAttribute('href') };
    });
    return rows;
  })()`);
  check('the live one is downloadable too',
    live2.some((r) => r.name === 'chart.png' && r.href.indexOf('chart.png') > 0),
    JSON.stringify(live2));

  // ---- a turn with no output has no strip --------------------------------
  await evalJs(`(function(){
    var i = document.getElementById('input');
    i.value = '再聊一句';
    i.dispatchEvent(new Event('input'));
    document.getElementById('send').click();
    return true;
  })()`);
  // Wait for the turn to finish: the compose button goes back to "send".
  await waitFor(async () => (await evalJs(
    `document.getElementById('send').title.indexOf('发送') === 0`)), 40000);
  await sleep(800);
  const after = await evalJs(`(function(){
    var box = document.querySelector('.artifacts');
    return box ? box.querySelectorAll('.artifact').length : 0;
  })()`);
  check('a turn that produced nothing shows no strip', after === 0, after);
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
