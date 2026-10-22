// Browser test for exporting a session as one offline HTML file, and importing
// it back.
//
// The contract pinned here:
//   * the topbar has an export button whenever a session is open (hidden for a
//     draft), with a tooltip naming the session;
//   * one click hands the browser an HTML blob (not JSON, not a redirect), and
//     the toast reports the file name and size;
//   * the exported file really is self-contained: it contains the conversation
//     text, an inline <style>, and the image as a data: URL - so it can be
//     copied to another machine and opened by double-clicking;
//   * the sidebar's import button takes such a file and opens the restored
//     session, with its text and its picture.
//
//   node tests/ui_session_export.mjs [path-to-tea_agent-binary]
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

// 1x1 red PNG: small, valid, and recognisable in the export.
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64');

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_export_ui_'));
const proj = path.join(tmp, 'proj');
const run = path.join(proj, '.tea_agent_run');
const sessions = path.join(run, 'sessions');
fs.mkdirSync(path.join(run, 'artifacts', 'images'), { recursive: true });
fs.mkdirSync(sessions, { recursive: true });
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });
fs.writeFileSync(path.join(run, 'artifacts', 'images', 'shot.png'), PNG);

const SID = 's_export_ui';
const TITLE = '导出测试 会话';
const messages = [
  { role: 'user', content: '第一问：请解释 **导出的含义**', created_at: '2026-01-01T00:00:00Z' },
  { role: 'assistant', content: '## 回答\n\n- 离线\n- 自包含\n\n```sh\necho hi\n```\n',
    created_at: '2026-01-01T00:00:01Z' },
  { role: 'user', content: '看看这张截图', created_at: '2026-01-01T00:00:02Z',
    images: [{ path: '.tea_agent_run/artifacts/images/shot.png', mime: 'image/png' }] },
];
fs.writeFileSync(path.join(sessions, SID + '.json'), JSON.stringify({
  id: SID, title: TITLE, created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:02Z', messages,
}, null, 2));
fs.writeFileSync(path.join(sessions, 'index.json'), JSON.stringify([{
  id: SID, title: TITLE, created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:02Z', message_count: messages.length,
}], null, 2));

let agent, chrome, ws;
try {
  fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    default_model: m
    models: {m: {max_context_tokens: 32768}}
`);

  const agentPort = await freePort();
  const cdpPort = await freePort();
  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', proj], { stdio: 'ignore' });
  for (let i = 0; i < 120; i++) {
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
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(2000);

  // ---- the button -------------------------------------------------------
  const btn = await evalJs(`(function(){
    var b = document.getElementById('session-export');
    return b ? { hidden: b.classList.contains('hidden'), title: b.title,
                 disabled: b.disabled, inTopbar: !!document.querySelector('#topbar #session-export') }
             : null;
  })()`);
  check('the topbar has an export button', !!btn && btn.inTopbar === true, JSON.stringify(btn));
  check('it is shown for an open session', btn && btn.hidden === false, JSON.stringify(btn));
  check('the tooltip names the session', btn && btn.title.indexOf(TITLE) >= 0, btn && btn.title);

  // ---- capture the download --------------------------------------------
  await evalJs(`(function(){
    window.__dl = null;
    var orig = URL.createObjectURL.bind(URL);
    URL.createObjectURL = function (blob) {
      window.__dl = { size: blob.size, type: blob.type, text: '' };
      blob.text().then(function (t) { window.__dl.text = t; });
      return orig(blob);
    };
    window.__clicked = null;
    var rc = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function () {
      if (this.download) window.__clicked = this.download;
      return rc.apply(this, arguments);
    };
    return true;
  })()`);
  await evalJs(`document.getElementById('session-export').click(); true`);
  let dl = null;
  for (let i = 0; i < 60; i++) {
    await sleep(300);
    dl = await evalJs(`window.__dl && window.__dl.text ? window.__dl : null`);
    if (dl) break;
  }
  check('a file was handed to the browser', !!dl, JSON.stringify(dl && dl.size));
  check('it is served as HTML', dl && dl.type.indexOf('text/html') === 0, dl && dl.type);
  check('the download name is an .html file',
    (await evalJs('window.__clicked')) && /\.html$/.test(await evalJs('window.__clicked')),
    await evalJs('window.__clicked'));

  const html = (dl && dl.text) || '';
  check('the exported HTML has the conversation',
    html.indexOf('请解释') >= 0 && html.indexOf('离线') >= 0, html.length);
  check('it renders markdown (heading and list)',
    html.indexOf('<h2>回答</h2>') >= 0 && html.indexOf('<li>离线</li>') >= 0);
  check('it carries its own stylesheet', html.indexOf('<style>') >= 0
    && html.indexOf('prefers-color-scheme') > 0);
  check('the image is inlined', html.indexOf('src="data:image/png;base64,') > 0);
  check('nothing points at this server', html.indexOf('127.0.0.1:' + agentPort) < 0);
  check('the session JSON rides along',
    html.indexOf('id="tea-session-data"') > 0 && html.indexOf('"format": "tea_agent.session"') > 0);

  // The toast reports what happened.
  const toast = await evalJs(`document.getElementById('toast').textContent`);
  check('a toast reports the exported file', toast.indexOf('.html') >= 0, toast);

  // ---- importing the file back -----------------------------------------
  // A real file input cannot be opened by a test; the click handler is exercised
  // by feeding the same bytes through the code path it uses.
  const wsId = JSON.parse((await getJson(agentPort, '/api/workspaces')).body).workspaces[0].id;
  const importResult = await evalJs(`(function(){
    return fetch('api/workspaces/' + encodeURIComponent('${wsId}') + '/sessions/import', {
      method: 'POST', headers: { 'Content-Type': 'text/html; charset=utf-8' }, body: window.__dl.text
    }).then(function (r) { return r.json(); });
  })()`);
  check('the exported file imports back', importResult && importResult.ok === true,
    JSON.stringify(importResult && importResult.error));
  check('it becomes a new session',
    importResult && importResult.session && importResult.session.id !== '${SID}',
    JSON.stringify(importResult && importResult.session));

  const newSid = importResult && importResult.session && importResult.session.id;
  const detail = JSON.parse((await getJson(agentPort, '/api/sessions/' + newSid)).body);
  check('the restored session has every message',
    (detail.messages || []).length === messages.length, (detail.messages || []).length);
  const withImage = (detail.messages || []).filter((m) => (m.images || []).length);
  check('the restored session has its picture', withImage.length === 1, withImage.length);
  check('the picture is a fresh copy in this workspace',
    withImage.length === 1 && withImage[0].images[0].path !==
      '.tea_agent_run/artifacts/images/shot.png', withImage.length && withImage[0].images[0].path);

  // And through the UI: the sidebar import button exists and is wired.
  const importBtn = await evalJs(`(function(){
    var b = document.getElementById('session-import');
    return b ? { title: b.title, inSidebar: !!document.querySelector('#sidebar #session-import') }
             : null;
  })()`);
  check('the sidebar offers an import button', !!importBtn && importBtn.inSidebar === true,
    JSON.stringify(importBtn));
  check('it explains what it accepts', importBtn && importBtn.title.indexOf('.html') >= 0,
    importBtn && importBtn.title);

  // Clicking the sidebar button really opens a file picker with the right filter
  // (the picker itself cannot be driven, so intercept the element it creates).
  const picker = await evalJs(`(function(){
    var captured = null;
    var orig = document.createElement.bind(document);
    document.createElement = function (tag) {
      var node = orig(tag);
      if (String(tag).toLowerCase() === 'input') captured = node;
      return node;
    };
    document.getElementById('session-import').click();
    document.createElement = orig;
    return captured ? { accept: captured.accept, type: captured.type } : null;
  })()`);
  check('the import button opens a file picker that accepts .html', picker
    && picker.type === 'file' && picker.accept.indexOf('.html') >= 0, JSON.stringify(picker));

  // After a reload the sidebar lists both sessions.
  await send('Page.reload');
  await sleep(2500);
  const listed = await evalJs(`Array.prototype.map.call(
    document.querySelectorAll('#session-list .sess-title'), function (n) { return n.textContent; })`);
  check('the restored session is listed in the sidebar',
    (listed || []).length >= 2, JSON.stringify(listed));

  // ---- the real promise: open it as a local file, server not involved -----
  // Written to a scratch directory and loaded over file://, i.e. exactly what
  // happens after copying it to another machine and double-clicking it.
  const offline = path.join(tmp, 'exported.html');
  fs.writeFileSync(offline, html, 'utf8');
  await send('Page.navigate', { url: 'file://' + offline });
  await sleep(1500);
  const page = await evalJs(`(function(){
    var body = getComputedStyle(document.body);
    var img = document.querySelector('.img img');
    var h2 = document.querySelector('h2');
    return {
      title: document.title,
      bg: body.backgroundColor,
      font: body.fontFamily,
      text: document.body.textContent,
      h2Color: h2 ? getComputedStyle(h2).fontSize : '',
      imgSrc: img ? img.getAttribute('src').slice(0, 22) : '',
      imgLoaded: img ? (img.complete && img.naturalWidth > 0) : false,
      userBubbles: document.querySelectorAll('.msg.user').length,
      toolish: document.querySelectorAll('.msg.assistant').length,
      jsonBlocks: document.querySelectorAll('script[type="application/json"]').length,
      external: Array.prototype.some.call(document.querySelectorAll('[src],[href]'),
        function (n) { var v = n.getAttribute('src') || n.getAttribute('href') || '';
                       return v.indexOf('http') === 0 || v.indexOf('//') === 0; })
    };
  })()`);
  check('the exported file opens from file:// (no server)', page && page.title.indexOf(TITLE) >= 0,
    JSON.stringify(page && page.title));
  check('the inline stylesheet is applied', page && page.bg !== 'rgba(0, 0, 0, 0)'
    && page.font.length > 0, JSON.stringify(page && [page.bg, page.font]));
  check('the conversation is rendered', page && page.text.indexOf('请解释') >= 0
    && parseFloat(page.h2Color) > 12, JSON.stringify(page && page.h2Color));
  check('the messages are there as bubbles',
    page && page.userBubbles === 2 && page.toolish === 1,
    JSON.stringify(page && [page.userBubbles, page.toolish]));
  check('the inlined image really loads offline', page && page.imgSrc.indexOf('data:image/png') === 0
    && page.imgLoaded === true, JSON.stringify(page && [page.imgSrc, page.imgLoaded]));
  check('the session JSON is still there for a restore', page && page.jsonBlocks === 1,
    JSON.stringify(page && page.jsonBlocks));
  check('nothing is fetched from the network', page && page.external === false,
    JSON.stringify(page && page.external));

  // ---- colours in both schemes ------------------------------------------
  // Regression: the dark rules used to be written before the light ones, so at
  // equal specificity the later light declarations won - a dark page with white
  // cards and near-invisible grey text. Pin the computed colours and the
  // contrast ratio in BOTH schemes, in the exported file itself.
  const palette = () => evalJs(`(function(){
    // Read the three channel numbers rather than parsing a specific syntax:
    // Chrome answers legacy "rgb(1, 2, 3)" and modern "rgb(1 2 3)" depending on
    // the version, and a split(',') silently yields NaN for the latter.
    function rgb(el, prop) {
      var nums = (getComputedStyle(el)[prop].match(/[0-9.]+/g) || []).map(Number);
      if (nums.length < 3) return null;
      return { r: nums[0], g: nums[1], b: nums[2] };
    }
    function lum(c) {
      function ch(x) { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); }
      return 0.2126 * ch(c.r) + 0.7152 * ch(c.g) + 0.0722 * ch(c.b);
    }
    function contrast(a, b) {
      var la = lum(a), lb = lum(b);
      return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
    }
    var body = document.body;
    var card = document.querySelector('.msg.user');
    var pre = document.querySelector('pre');
    var out = {
      bodyBg: rgb(body, 'backgroundColor'), bodyFg: rgb(body, 'color'),
      cardBg: rgb(card, 'backgroundColor'), cardFg: rgb(card, 'color'),
      preBg: rgb(pre, 'backgroundColor'),
      bodyLum: lum(rgb(body, 'backgroundColor')),
      cardLum: lum(rgb(card, 'backgroundColor')),
      bodyContrast: contrast(rgb(body, 'backgroundColor'), rgb(body, 'color')),
      cardContrast: contrast(rgb(card, 'backgroundColor'), rgb(card, 'color')),
      // In dark mode card vs page is a subtle lift plus a border, so compare
      // luminance with a small epsilon (a visible step, not an exact match).
      cardVisible: Math.abs(lum(rgb(card, 'backgroundColor')) -
                           lum(rgb(body, 'backgroundColor'))) > 0.002
    };
    return out;
  })()`);

  // Pin the scheme instead of relying on the browser's default: headless
  // Chrome reports prefers-color-scheme: dark, so the "light" measurements were
  // taken from the dark palette and failed for a reason that had nothing to do
  // with the exported file.
  await send('Emulation.setEmulatedMedia',
    { features: [{ name: 'prefers-color-scheme', value: 'light' }] });
  await sleep(400);
  const light = await palette();
  check('light scheme: the page is light', light && light.bodyLum > 0.7, JSON.stringify(light));
  check('light scheme: cards are light too (not an inverted page)',
    light && light.cardLum > 0.7, JSON.stringify(light && [light.cardLum, light.cardBg]));
  check('light scheme: text contrasts with its background',
    light && light.bodyContrast > 7 && light.cardContrast > 7,
    JSON.stringify(light && [light.bodyContrast, light.cardContrast]));

  await send('Emulation.setEmulatedMedia',
    { features: [{ name: 'prefers-color-scheme', value: 'dark' }] });
  await sleep(500);
  const dark = await palette();
  check('dark scheme is honoured by the exported file', dark && dark.bodyLum < 0.2,
    JSON.stringify(dark && dark.bodyBg));
  check('dark scheme: cards follow the page (this was the bug)',
    dark && dark.cardLum < 0.35 && Math.abs(dark.cardLum - dark.bodyLum) < 0.1,
    JSON.stringify(dark && [dark.cardLum, dark.bodyLum, dark.cardBg]));
  check('dark scheme: cards stay distinguishable from the page',
    dark && dark.cardVisible === true, JSON.stringify(dark && dark.cardBg));
  check('dark scheme: text is readable on the card',
    dark && dark.cardContrast > 7, JSON.stringify(dark && dark.cardContrast));
  check('dark scheme: code blocks are dark as well',
    dark && dark.preBg && dark.preBg.r < 60 && dark.preBg.g < 60,
    JSON.stringify(dark && dark.preBg));
  check('dark scheme: the body text is light, not grey-on-white',
    dark && dark.bodyFg.r > 150, JSON.stringify(dark && dark.bodyFg));
  await send('Emulation.setEmulatedMedia', { features: [] });
} catch (e) {
  console.error('harness error:', e.message);
  results.push({ name: 'harness', ok: false });
} finally {
  if (ws) ws.close();
  if (chrome) chrome.kill('SIGKILL');
  if (agent) agent.kill('SIGTERM');
  await sleep(300);
  fs.rmSync(tmp, { recursive: true, force: true });
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
