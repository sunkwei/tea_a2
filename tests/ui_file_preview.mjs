// Browser test for the file preview (the 「文件」 page, right-hand pane).
//
// The contract pinned here:
//   * the pane next to the file tree is actually there (it used to be 2px
//     wide because of a duplicated <div class="files-grid">);
//   * clicking a text file shows a PREVIEW on the right - the content, with
//     the code highlighted - not an empty box;
//   * 「编辑」 switches that preview to the editor and back;
//   * clicking a picture shows the picture, with its size;
//   * a binary file says so instead of showing a screen of garbage;
//   * the preview follows the edits: what you typed is what the preview shows.
//
//   node tests/ui_file_preview.mjs [path-to-tea_agent-binary]
//
// Requires google-chrome (or CHROME_BIN) and node >= 18.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
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

const tmp = fs.mkdtempSync(path.join(process.env.TMP || os.tmpdir(), 'tea_fprev_ui_'));
const home = path.join(tmp, 'home');
const proj = path.join(tmp, 'proj');
fs.mkdirSync(home, { recursive: true });
fs.mkdirSync(proj, { recursive: true });
const configPath = path.join(home, 'provider.yaml');

const CPP = `#include <stdio.h>

// 打个招呼
int main(int argc, char** argv) {
  const char* name = "world";
  printf("hello, %s!\\n", name);
  return 0;
}
`;
const MD = '# 标题\n\n正文 **粗体**。\n';
// A 64x64 PNG (transparent) - enough to check that the browser can decode it.
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAAV0lEQVR42u3OMQEAAAgDoK1/aM3g4QcFqCXVVAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADg1vSRAAB0aGVsbG8AAAAASUVORK5CYII=',
  'base64');

const CONFIG = `version: 1

providers:
  acme:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-acme
    models:
      acme-pro: {}
`;

let agent, chrome, ws;
try {
  fs.writeFileSync(configPath, CONFIG);
  fs.writeFileSync(path.join(proj, 'hello.cpp'), CPP);
  fs.writeFileSync(path.join(proj, 'note.md'), MD);
  fs.writeFileSync(path.join(proj, 'pic.png'), PNG);
  fs.writeFileSync(path.join(proj, 'blob.bin'), Buffer.from([0, 1, 2, 3, 0, 255, 0, 7]));

  const agentPort = await freePort();
  agent = spawn(BIN, ['--tea-home', home, '--port', String(agentPort), '--no-console',
    '--log-file', path.join(home, 'logs', 'tea_agent.log'),
    '--workspace', proj], { stdio: 'ignore' });

  for (let i = 0; i < 100; i++) {
    try { if ((await getJson(agentPort, '/api/health')).status === 200) break; } catch {}
    await sleep(150);
  }
  const health = JSON.parse((await getJson(agentPort, '/api/health')).body);
  check('the agent starts with the existing configuration',
    health.config_loaded === true, JSON.stringify(health));

  const chromeTmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_fprev_chrome_'));
  chrome = await acquireBrowser(chromeTmp, 'chrome', { windowSize: '1400,900' });
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
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(2500);

  // The files page.
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('.nav-row'),
      function (x) { return x.dataset.view === 'files'; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(900);

  const clickFile = async (name) => {
    await evalJs(`(function(){
      var rows = document.querySelectorAll('#files-list .file-row');
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].textContent.indexOf(${JSON.stringify(name)}) >= 0) { rows[i].click(); return true; }
      }
      return false;
    })()`);
    await sleep(900);
  };

  // What the right-hand pane shows right now.
  const pane = async () => evalJs(`(function(){
    function info(id) {
      var e = document.getElementById(id);
      if (!e) return null;
      var r = e.getBoundingClientRect();
      // A textarea holds its text in .value, not in .textContent.
      var txt = (e.tagName === 'TEXTAREA') ? (e.value || '') : (e.textContent || '');
      return { hidden: e.classList.contains('hidden'), w: Math.round(r.width),
               h: Math.round(r.height), text: txt.slice(0, 400),
               toks: e.querySelectorAll('span[class^=tok-]').length };
    }
    var p = document.querySelector('.file-pane');
    var pr = p ? p.getBoundingClientRect() : null;
    var toggle = document.getElementById('files-preview-toggle');
    return {
      head: info('files-preview-head'), pre: info('files-pre'), media: info('files-media'),
      editor: info('files-editor'), md: info('files-md'), blank: info('files-blank'),
      toggle: { hidden: toggle.classList.contains('hidden'), text: toggle.textContent },
      pane: pr ? { w: Math.round(pr.width), h: Math.round(pr.height) } : null
    };
  })()`);

  // ---- a text file: the preview shows up on the right --------------------
  await clickFile('hello.cpp');
  const cpp = await pane();
  check('the pane next to the tree is wide enough to show something',
    cpp && cpp.pane && cpp.pane.w > 300, JSON.stringify(cpp && cpp.pane));
  check('the head names the file',
    cpp && cpp.head && cpp.head.text.indexOf('hello.cpp') >= 0, JSON.stringify(cpp && cpp.head));
  check('a text file is shown as a preview, not as an editor',
    cpp && cpp.pre && cpp.pre.hidden === false && cpp.editor && cpp.editor.hidden === true,
    JSON.stringify(cpp && { pre: cpp.pre, editor: cpp.editor }));
  check('the preview has the content of the file',
    cpp && cpp.pre && cpp.pre.text.indexOf('printf') >= 0, JSON.stringify(cpp && cpp.pre));
  check('the code is highlighted',
    cpp && cpp.pre && cpp.pre.toks > 5, JSON.stringify(cpp && cpp.pre && cpp.pre.toks));
  check('the toggle offers to edit',
    cpp && cpp.toggle && cpp.toggle.hidden === false && cpp.toggle.text === '编辑',
    JSON.stringify(cpp && cpp.toggle));

  // ---- 「编辑」 switches to the editor ------------------------------------
  await evalJs(`(function(){ document.getElementById('files-preview-toggle').click(); return true; })()`);
  await sleep(500);
  const editing = await pane();
  check('「编辑」 shows the editor',
    editing && editing.editor && editing.editor.hidden === false &&
    editing.pre && editing.pre.hidden === true, JSON.stringify(editing && { pre: editing.pre, editor: editing.editor }));
  check('the editor holds the content of the file',
    editing && editing.editor && editing.editor.text.indexOf('printf') >= 0,
    JSON.stringify(editing && editing.editor));
  check('the toggle now offers the preview',
    editing && editing.toggle && editing.toggle.text === '预览', JSON.stringify(editing && editing.toggle));

  // ---- back to the preview: it follows the edits --------------------------
  await evalJs(`(function(){
    var e = document.getElementById('files-editor');
    e.value = e.value.replace('hello, %s!', 'hi, %s!');
    e.dispatchEvent(new Event('input', { bubbles: true }));
    document.getElementById('files-preview-toggle').click();
    return true;
  })()`);
  await sleep(500);
  const edited = await pane();
  check('the preview shows what was typed',
    edited && edited.pre && edited.pre.text.indexOf('hi, %s!') >= 0,
    JSON.stringify(edited && edited.pre));

  // ---- a picture: the picture is shown ------------------------------------
  await clickFile('pic.png');
  const pic = await pane();
  check('a picture is shown',
    pic && pic.media && pic.media.hidden === false, JSON.stringify(pic && pic.media));
  check('the picture has its size',
    pic && pic.media && pic.media.text.indexOf('64 × 64 px') >= 0, JSON.stringify(pic && pic.media));
  check('a picture cannot be edited', pic && pic.toggle && pic.toggle.hidden === true,
    JSON.stringify(pic && pic.toggle));

  // ---- a binary file: it says so ------------------------------------------
  await clickFile('blob.bin');
  const bin = await pane();
  check('a binary file says it cannot be previewed',
    bin && bin.blank && bin.blank.hidden === false &&
    bin.blank.text.indexOf('二进制') >= 0, JSON.stringify(bin && bin.blank));
  check('a binary file offers a download',
    bin && bin.blank && bin.blank.text.indexOf('下载') >= 0, JSON.stringify(bin && bin.blank));

  // ---- the server says which files are binary ----------------------------
  const wsList = JSON.parse((await getJson(agentPort, '/api/workspaces')).body);
  const wsId = (wsList.workspaces || [])[0].id;
  const binFile = JSON.parse((await getJson(agentPort,
    `/api/workspaces/${wsId}/file?path=blob.bin`)).body);
  check('the server marks a binary file as binary', binFile.binary === true, JSON.stringify(binFile));
  const cppFile = JSON.parse((await getJson(agentPort,
    `/api/workspaces/${wsId}/file?path=hello.cpp`)).body);
  check('the server does not mark a text file as binary', cppFile.binary === false,
    JSON.stringify(cppFile));
  check('the file content comes back whole',
    cppFile.content === CPP, JSON.stringify(String(cppFile.content).slice(0, 40)));
} catch (e) {
  check('no exception', false, e && e.stack ? e.stack : String(e));
} finally {
  try { if (ws) ws.close(); } catch {}
  for (const p of [chrome, agent]) {
    try { if (p) p.kill(); } catch {}
  }
  await sleep(300);
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch {}
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length === 0 ? 0 : 1);
