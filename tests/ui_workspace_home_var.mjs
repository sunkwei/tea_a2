// Browser test: the add-workspace form takes a $HOME path.
//
// A path people have in front of them is often written `$HOME/projects/foo` -
// it comes out of a `.bashrc`, a README or a ticket. The agent expands it to
// **its own** home directory (the machine the agent runs on), so the form has
// to accept that spelling instead of calling it a relative path.
//
//   node tests/ui_workspace_home_var.mjs [path-to-tea_agent-binary]
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

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? '  ' + detail : ''));
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_homevar_'));
const home = path.join(tmp, 'home');
const projA = path.join(tmp, 'proj_a');
const projC = path.join(home, 'proj_c');
fs.mkdirSync(home, { recursive: true });
fs.mkdirSync(projA, { recursive: true });
fs.mkdirSync(projC, { recursive: true });
const config = path.join(tmp, 'provider.yaml');
fs.writeFileSync(config, `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:1
    api_key: sk-test
    default_model: m
    models: {m: {supports_tools: true}}
`);

let agent, chrome, ws;

async function startAgent() {
  const agentPort = await freePort();
  // HOME is what the agent expands - so it is pointed at the temp dir, and the
  // test never touches the real home directory of the person running it.
  agent = spawn(BIN, ['--config', config, '--tea-home', path.join(home, '.tea_agent'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', projA],
    { stdio: 'ignore', env: Object.assign({}, process.env, { HOME: home }) });
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
           error: e ? e.textContent : '' };
})()`);

try {
  const agentPort = await startAgent();

  const cdpPort = await freePort();
  chrome = await acquireBrowser(tmp);
  const target = { webSocketDebuggerUrl: chrome.webSocketDebuggerUrl };
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

  // ---- 1) $HOMEDIR is not $HOME: still refused in the form -------------
  await evalJs(openAdd);
  await sleep(400);
  await fill('$HOMEDIR/proj', 'proj');
  await evalJs(submit);
  await sleep(400);
  let st = await dialogState();
  check('$HOMEDIR is refused in the form (it is another variable)',
    st && st.open === true && st.error.indexOf('绝对路径') >= 0, JSON.stringify(st));

  // ---- 2) $HOME is accepted ---------------------------------------------
  await fill('$HOME/proj_c', 'proj_c');
  await evalJs(submit);
  await sleep(1500);
  check('the $HOME path is accepted', (await chip()) === 'proj_c', await chip());

  const listed = JSON.parse((await getJson(agentPort, '/api/workspaces')).body).workspaces.map((w) => w.path);
  check('the server registered the expanded path',
    listed.indexOf(projC) >= 0, listed.join(', '));
  check('the raw $HOME does not survive into the index',
    listed.every((p) => p.indexOf('$HOME') < 0), listed.join(', '));

  // ---- 3) ${HOME} and ~ are accepted too ---------------------------------
  const projD = path.join(home, 'proj_d');
  fs.mkdirSync(projD, { recursive: true });
  await evalJs(openAdd);
  await sleep(400);
  await fill('${HOME}/proj_d', 'proj_d');
  await evalJs(submit);
  await sleep(1500);
  check('the ${HOME} path is accepted', (await chip()) === 'proj_d', await chip());

  const projE = path.join(home, 'proj_e');
  fs.mkdirSync(projE, { recursive: true });
  await evalJs(openAdd);
  await sleep(400);
  await fill('~/proj_e', 'proj_e');
  await evalJs(submit);
  await sleep(1500);
  check('the ~ path is accepted', (await chip()) === 'proj_e', await chip());

  const listed2 = JSON.parse((await getJson(agentPort, '/api/workspaces')).body).workspaces.map((w) => w.path);
  check('every workspace points at a real directory',
    listed2.indexOf(projC) >= 0 && listed2.indexOf(projD) >= 0 && listed2.indexOf(projE) >= 0,
    listed2.join(', '));
} catch (e) {
  check('the test ran to the end', false, String(e && e.message ? e.message : e));
} finally {
  try { if (ws) ws.close(); } catch {}
  try { if (chrome) chrome.kill(); } catch {}
  await stopAgent();
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log('');
console.log(`${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) {
  console.log('FAILED: ' + failed.map((r) => r.name).join(', '));
  process.exit(1);
}
