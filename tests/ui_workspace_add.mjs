// Browser test for adding a workspace (a different project directory).
//
// There is exactly one entry path now: "＋ 添加工作区…" opens a form and the user
// types a complete path. No native directory dialog is involved (that needed a
// process and a temp script on the server's desktop, per click), so this test
// asserts the form is what appears, that a bad path is reported in the form
// itself (the dialog stays open), and that the good paths still work - including
// the "the directory does not exist, create it?" offer.
//
//   node tests/ui_workspace_add.mjs [path-to-tea_agent-binary]
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
const postJson = (port, p, body) => new Promise((res, rej) => {
  const payload = JSON.stringify(body || {});
  const req = http.request({ host: '127.0.0.1', port, path: p, method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) } },
    (r) => {
      let b = ''; r.on('data', (d) => (b += d));
      r.on('end', () => res({ status: r.statusCode, body: b }));
    });
  req.on('error', rej);
  req.end(payload);
});

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? '  ' + detail : ''));
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_wsadd_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj_a'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'proj_b'), { recursive: true });
fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:1
    api_key: sk-test
    default_model: m
    models: {m: {supports_tools: true}}
roles: {main: {provider: mock, model: m}}
`);

let agent, chrome, ws;

async function startAgent() {
  const agentPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'proj_a')], { stdio: 'ignore' });
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) return agentPort; } catch {}
    await sleep(150);
  }
  throw new Error('agent did not start');
}

async function stopAgent() {
  if (!agent) return;
  agent.kill('SIGTERM');
  await sleep(300);
  agent = null;
}

let id = 0;
const pending = new Map();
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

const chip = () => evalJs(`document.getElementById('ws-chip-text').textContent`);
const openAdd = `(function(){
  document.getElementById('ws-chip').click();
  var d = document.getElementById('ws-dropdown');
  var b = Array.from(d.querySelectorAll('.dd-item'))
    .filter(function (x) { return x.textContent.indexOf('添加工作区') >= 0; })[0];
  if (!b) return false;
  b.click();
  return true;
})()`;
const submit = `(function(){
  var bs = document.querySelectorAll('#dialog .actions .btn');
  if (!bs.length) return false;
  bs[bs.length - 1].click();
  return true;
})()`;
const fill = (dir, name) => evalJs(`(function(){
  var i = document.querySelectorAll('#dialog .field .ta');
  if (i.length < 1) return false;
  i[0].value = ${JSON.stringify(dir)};
  i[0].dispatchEvent(new Event('input'));
  if (i.length > 1 && ${JSON.stringify(name)}) i[1].value = ${JSON.stringify(name)};
  return true;
})()`);
const dialogState = () => evalJs(`(function(){
  var d = document.getElementById('dialog');
  var e = d.querySelector('.form-error');
  return { open: !document.getElementById('overlay').classList.contains('hidden'),
           title: (d.querySelector('h3') || {}).textContent,
           error: e ? e.textContent : '' };
})()`);

try {
  const agentPort = await startAgent();

  // ---- 1) the server no longer offers a directory picker ----------------
  const health = JSON.parse((await getJson(agentPort, '/api/health')).body);
  check('/api/health no longer advertises dir_picker', health.dir_picker === undefined,
    String(health.dir_picker));
  const gone = await postJson(agentPort, '/api/dialog/directory', {});
  check('/api/dialog/directory is gone', gone.status === 404, 'HTTP ' + gone.status);

  // ---- 2) the browser ------------------------------------------------
  const cdpPort = await freePort();
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
  ws.on('message', (raw) => {
    const msg = JSON.parse(raw);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  });
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(1600);

  const menu = await evalJs(`(function(){
    document.getElementById('ws-chip').click();
    var d = document.getElementById('ws-dropdown');
    return { hidden: d.classList.contains('hidden'),
             items: Array.from(d.children).map(function (c) { return c.textContent.trim(); }) };
  })()`);
  check('workspace chip opens a menu', menu && menu.hidden === false);
  check('menu offers 添加工作区', menu && menu.items.some((t) => t.indexOf('添加工作区') >= 0),
    menu && menu.items.join(' | '));
  await evalJs(`document.getElementById('ws-chip').click(); true`);

  // "添加工作区…" opens the form directly: no dialog process, no wait.
  await evalJs(openAdd);
  await sleep(400);
  const form = await evalJs(`(function(){
    var d = document.getElementById('dialog');
    var inputs = d.querySelectorAll('.field .ta');
    return { open: !document.getElementById('overlay').classList.contains('hidden'),
             title: (d.querySelector('h3') || {}).textContent,
             fields: inputs.length,
             browseButtons: d.querySelectorAll('.field .btn').length,
             focused: document.activeElement === inputs[0],
             placeholder: inputs.length ? inputs[0].placeholder : '' };
  })()`);
  check('the add form opens straight away',
    form && form.open === true && form.title === '添加工作区', JSON.stringify(form));
  check('the form has a path field and no browse button',
    form && form.fields === 2 && form.browseButtons === 0, JSON.stringify(form));
  check('the path field shows an example path', form && form.placeholder.length > 0, form && form.placeholder);
  check('the path field takes focus', form && form.focused === true);

  // ---- 3) an empty path is refused in the form -------------------------
  await evalJs(submit);
  await sleep(200);
  let st = await dialogState();
  check('an empty path is reported in the form',
    st && st.open === true && st.error.indexOf('请输入') >= 0, JSON.stringify(st));

  // ---- 4) a relative path is refused before it reaches the server ------
  await fill('proj_rel', 'proj_rel');
  await evalJs(submit);
  await sleep(400);
  st = await dialogState();
  check('a relative path is refused in the form',
    st && st.open === true && st.error.indexOf('绝对路径') >= 0, JSON.stringify(st));
  check('the refused relative path was not created', !fs.existsSync(path.join(tmp, 'proj_rel')));

  // ---- 5) a missing directory offers to be created ---------------------
  const newDir = path.join(tmp, 'proj_new');
  await fill(newDir, 'proj_new');
  await evalJs(submit);
  await sleep(900);
  const offer = await evalJs(`document.getElementById('dialog').textContent.replace(/\\s+/g, ' ')`);
  check('missing directory offers to create it',
    offer && offer.indexOf('目录不存在') >= 0 && offer.indexOf('创建并添加') >= 0,
    offer && offer.slice(0, 70));
  await evalJs(submit);
  await sleep(1500);
  check('directory is created on confirm', fs.existsSync(newDir));
  check('UI switches to the new workspace', (await chip()) === 'proj_new', await chip());
  // 标签页标题跟着工作区走：同时开着多个页面时靠它区分。
  const title = await evalJs(`document.title`);
  check('browser title names the new workspace', title === 'proj_new · TEA AGENT', JSON.stringify(title));

  // ---- 6) an existing directory is added as typed ----------------------
  await evalJs(openAdd);
  await sleep(400);
  await fill(path.join(tmp, 'proj_b'), 'proj_b');
  await evalJs(submit);
  await sleep(1500);
  check('an existing directory is added', (await chip()) === 'proj_b', await chip());
  const listed = JSON.parse((await getJson(agentPort, '/api/workspaces')).body).workspaces.map((w) => w.path);
  check('both directories are registered',
    listed.indexOf(path.join(tmp, 'proj_new')) >= 0 && listed.indexOf(path.join(tmp, 'proj_b')) >= 0,
    listed.join(', '));

  // ---- 7) a file is refused, and the dialog stays open -----------------
  const filePath = path.join(tmp, 'not_a_dir.txt');
  fs.writeFileSync(filePath, 'x');
  await evalJs(openAdd);
  await sleep(400);
  await fill(filePath, 'nope');
  await evalJs(submit);
  await sleep(900);
  st = await dialogState();
  check('a file path is refused with a message',
    st && st.open === true && st.error.indexOf('添加失败') >= 0, JSON.stringify(st));
  check('a file path is not registered',
    JSON.parse((await getJson(agentPort, '/api/workspaces')).body).workspaces
      .every((w) => w.path !== filePath));

  // ---- 8) cancel leaves nothing behind --------------------------------
  await evalJs(`(function(){ var bs = document.querySelectorAll('#dialog .actions .btn'); if (bs.length) bs[0].click(); return true; })()`);
  await sleep(400);
  check('cancel keeps the current workspace', (await chip()) === 'proj_b', await chip());

  // ---- 9) the server enforces the same rule ---------------------------
  const rel = await postJson(agentPort, '/api/workspaces', { path: 'proj_rel', name: 'rel' });
  check('POST /api/workspaces rejects a relative path', rel.status === 400, 'HTTP ' + rel.status);
  check('the rejection says what a full path looks like',
    rel.body.indexOf('full path') >= 0, rel.body.slice(0, 120));
  check('the rejected relative path was not created', !fs.existsSync(path.join(tmp, 'proj_rel')));
  const abs = await postJson(agentPort, '/api/workspaces', { path: path.join(tmp, 'proj_a'), name: 'a' });
  check('POST /api/workspaces still accepts a full path', abs.status === 200, 'HTTP ' + abs.status);
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  await stopAgent();
  await sleep(200);
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
