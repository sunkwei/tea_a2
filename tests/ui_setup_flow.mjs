// Real-browser test of the first-run setup flow, driven over CDP.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

// First-run flow: a server started without provider.yaml must serve the UI,
// show a blocking upload dialog, and reload itself once the file is accepted.
//
// Drives a real headless Chrome over CDP, so it covers the JS/CSS too.
// Requires google-chrome (or CHROME_BIN) and node >= 18.
//   node tests/ui_setup_flow.mjs [path-to-tea_agent-binary]
import { fileURLToPath } from 'node:url';
const REPO_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const BIN = process.argv[2] || path.join(REPO_ROOT, 'build', 'bin', 'tea_agent');
const CHROME = process.env.CHROME_BIN || '/usr/bin/google-chrome';

function freePort() {
  return new Promise((res) => {
    const s = net.createServer();
    s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); });
  });
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function getJson(port, p) {
  return new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port, path: p }, (r) => {
      let b = ''; r.on('data', (d) => (b += d)); r.on('end', () => res({ status: r.statusCode, body: b }));
    }).on('error', rej);
  });
}

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_ui_'));
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
const agentPort = await freePort();
const cdpPort = await freePort();

const agent = spawn(BIN, ['--tea-home', path.join(tmp, 'home'), '--port', String(agentPort),
  '--no-console', '--log-file', path.join(tmp, 'agent.log')], { stdio: 'ignore' });

let chrome;
let ws = null;
const results = [];
const check = (name, ok, detail) => { results.push({ name, ok, detail }); console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? '  ' + detail : '')); };

try {
  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }

  chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
    `--remote-debugging-port=${cdpPort}`, `--user-data-dir=${path.join(tmp, 'chrome')}`,
    'about:blank'], { stdio: 'ignore' });

  let target = null;
  for (let i = 0; i < 100; i++) {
    try {
      const r = await getJson(cdpPort, '/json/list');
      const list = JSON.parse(r.body);
      target = list.find((t) => t.type === 'page');
      if (target) break;
    } catch {}
    await sleep(150);
  }
  if (!target) throw new Error('no chrome page target');

  const { wsConnect } = await import('./mini_ws.mjs');
  ws = wsConnect(target.webSocketDebuggerUrl);
  await new Promise((r) => ws.on('open', r));
  let id = 0;
  const pending = new Map();
  ws.on('message', (raw) => {
    const msg = JSON.parse(raw);
    if (process.env.WSDEBUG) console.log('[ws]', raw.slice(0, 100));
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  });
  const send = (method, params) => new Promise((res) => {
    const mid = ++id; pending.set(mid, res);
    ws.send({ id: mid, method, params });
  });
  const evalJs = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.result && r.result.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
    return r.result && r.result.result ? r.result.result.value : undefined;
  };

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(2500);

  // 1) the gate is visible and blocks the UI
  const gate = await evalJs(`(function(){
    var d=document.getElementById('dialog');
    return { text: d ? d.textContent : '', overlayHidden: document.getElementById('overlay').classList.contains('hidden'),
             statusText: document.getElementById('status-text').textContent };
  })()`);
  check('setup dialog shown', gate && !gate.overlayHidden && gate.text.includes('provider.yaml'),
    JSON.stringify(gate && gate.text.slice(0, 60)));
  check('status pill says 未配置', gate && gate.statusText === '未配置', gate && gate.statusText);

  // 2) Esc / overlay click must not dismiss it
  await evalJs(`document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}))`);
  await sleep(200);
  const afterEsc = await evalJs(`document.getElementById('overlay').classList.contains('hidden')`);
  check('Esc does not close the gate', afterEsc === false);

  await evalJs(`document.getElementById('overlay').click()`);
  await sleep(200);
  const afterClick = await evalJs(`document.getElementById('overlay').classList.contains('hidden')`);
  check('overlay click does not close the gate', afterClick === false);

  // 3) an invalid upload shows an error and keeps the gate open
  await evalJs(`(function(){
    var ta=document.getElementById('setup-yaml'); ta.value='providers: [broken';
    var btns=document.querySelectorAll('#dialog .actions .btn');
    btns[btns.length-1].click();
  })()`);
  await sleep(1200);
  const badUpload = await evalJs(`(function(){
    var e=document.getElementById('setup-error');
    return { shown: e && !e.classList.contains('hidden'), text: e ? e.textContent : '',
             overlayHidden: document.getElementById('overlay').classList.contains('hidden') };
  })()`);
  check('invalid upload reports an error', badUpload && badUpload.shown && /上传失败/.test(badUpload.text),
    badUpload && badUpload.text.slice(0, 90));
  check('invalid upload keeps the gate open', badUpload && badUpload.overlayHidden === false);

  // 3b) upload through the real file input (what a click on the drop zone does)
  const yamlFile = path.join(tmp, 'provider.yaml');
  fs.writeFileSync(yamlFile, `version: 1
providers:
  FileUp:
    api_url: https://api.deepseek.com
    api_key: sk-test
    default_model: deepseek-chat
    models:
      deepseek-chat:
        supports_tools: true
roles:
  main: {provider: FileUp, model: deepseek-chat}
`);
  const doc = await send('DOM.getDocument', { depth: -1 });
  const node = await send('DOM.querySelector', { nodeId: doc.result.root.nodeId, selector: '#setup-file' });
  await send('DOM.setFileInputFiles', { files: [yamlFile], nodeId: node.result.nodeId });
  await sleep(700);
  const picked = await evalJs(`(function(){
    var ta=document.getElementById('setup-yaml');
    return { len: ta.value.length, head: ta.value.slice(0,20),
             label: document.querySelector('.setup-drop-main').textContent };
  })()`);
  check('file picker fills the textarea', picked && picked.len > 50 && /FileUp/.test(picked.len ? 'FileUp' : '') || (picked && picked.label.indexOf('provider.yaml') >= 0),
    JSON.stringify(picked));

  // 4) a valid upload applies the config and reloads the page
  const yaml = `version: 1
providers:
  DeepSeek:
    api_url: https://api.deepseek.com
    api_key: sk-test
    default_model: deepseek-chat
    models:
      deepseek-chat:
        supports_tools: true
roles:
  main: {provider: DeepSeek, model: deepseek-chat}
`;
  await send('Page.addScriptToEvaluateOnNewDocument', { source: `window.__reloaded=true;` });
  await evalJs(`(function(){
    var ta=document.getElementById('setup-yaml');
    ta.value=${JSON.stringify(yaml)};
    var btns=document.querySelectorAll('#dialog .actions .btn');
    btns[btns.length-1].click();
  })()`);
  await sleep(4000);

  const after = await evalJs(`(function(){
    return { reloaded: !!window.__reloaded,
             setupGone: document.querySelector('.setup') === null,
             overlayHidden: document.getElementById('overlay').classList.contains('hidden'),
             statusText: document.getElementById('status-text').textContent,
             bannerHidden: document.getElementById('banner').classList.contains('hidden'),
             dialogText: document.getElementById('dialog').textContent };
  })()`);
  check('page auto-reloaded after upload', after && after.reloaded === true, JSON.stringify(after));
  check('setup gate gone after reload', after && after.setupGone === true);
  check('offline/needs-config banner cleared', after && after.bannerHidden === true);
  check('status pill back to 已连接', after && /已连接/.test(after.statusText), after && after.statusText);

  const cfg = fs.existsSync(path.join(tmp, 'home', 'provider.yaml'));
  check('provider.yaml written to disk', cfg);

  const providers = JSON.parse((await getJson(agentPort, '/api/providers')).body);
  check('server reloaded the config', providers.active.provider === 'DeepSeek', JSON.stringify(providers.active));
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  agent.kill('SIGTERM');
  await sleep(300);
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
