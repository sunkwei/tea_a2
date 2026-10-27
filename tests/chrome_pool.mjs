// One browser for the whole test run - or a private one per test.
//
// A fresh Chrome pays a big penalty on its FIRST page load in some Linux
// containers (WSL2): the renderer has to come up from a cold cache, and the
// first navigation does not answer for ~25 s. With 29 UI tests that is more
// than ten minutes of waiting for nothing.
//
// So the runner starts ONE browser and exports TEA_TEST_CDP=host:port; every
// test then opens its own tab in it. Without that variable each test gets a
// private browser, exactly as before.
//
//   TEA_TEST_CDP=127.0.0.1:9222 node tests/ui_usage.mjs
import { spawn } from 'node:child_process';
import http from 'node:http';
import net from 'node:net';
import { wsConnect } from './mini_ws.mjs';

const CHROME = process.env.CHROME_BIN || '/usr/bin/google-chrome';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const getJson = (port, path, host = '127.0.0.1') => new Promise((res, rej) => {
  http.get({ host, port, path }, (r) => {
    let b = '';
    r.on('data', (d) => (b += d));
    r.on('end', () => res({ status: r.statusCode, body: b }));
  }).on('error', rej);
});

const freePort = () => new Promise((res) => {
  const s = net.createServer();
  s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); });
});

/// Open a new tab in a running browser. Chrome wants PUT for /json/new (GET
/// was the old way); try both.
function requestTab(port, host, method) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (tab) => { if (!done) { done = true; resolve(tab); } };
    let req;
    try {
      req = http.request({ host, port, path: '/json/new?about:blank', method }, (r) => {
        let b = '';
        r.on('data', (d) => (b += d));
        r.on('end', () => {
          try {
            const tab = JSON.parse(b);
            finish(tab && tab.webSocketDebuggerUrl ? tab : null);
          } catch (e) { finish(null); }
        });
      });
    } catch (e) { finish(null); return; }
    req.on('error', () => finish(null));
    req.end();
  });
}

/// Give a tab the viewport a test asked for. In a shared browser the window
/// belongs to whoever started it, so the size is set per target instead - which
/// is also what the media queries see.
async function setViewport(wsUrl, windowSize) {
  const m = /^(\d+),(\d+)$/.exec(String(windowSize || ''));
  if (!m) return;
  const sock = wsConnect(wsUrl);
  await new Promise((r) => sock.on('open', r));
  let id = 0;
  const pending = new Map();
  sock.on('message', (raw) => {
    const msg = JSON.parse(raw);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  });
  const send = (method, params) => new Promise((res) => {
    const mid = ++id; pending.set(mid, res); sock.send({ id: mid, method, params });
  });
  try {
    await send('Emulation.setDeviceMetricsOverride', {
      width: Number(m[1]), height: Number(m[2]), deviceScaleFactor: 1, mobile: false,
    });
  } catch (e) { /* the test does not care about the exact size */ }
  try { sock.close(); } catch (e) { /* ignore */ }
}

async function newTab(port, host) {
  for (const method of ['PUT', 'GET']) {
    const tab = await requestTab(port, host, method);
    if (tab && tab.webSocketDebuggerUrl) return tab;
  }
  throw new Error('cannot open a tab in the browser on port ' + port);
}

async function closeTab(port, tabId, host) {
  if (!tabId) return;
  try {
    await getJson(port, '/json/close/' + tabId, host);
  } catch (e) { /* the browser is going away anyway */ }
}

/// A browser for one test. `kill()` closes it (the tab, or the whole process
/// when the browser was private).
export async function acquireBrowser(tmp, name = 'chrome', opts = {}) {
  const shared = (process.env.TEA_TEST_CDP || '').trim();
  if (shared) {
    const [host, portText] = shared.split(':');
    const port = Number(portText);
    const tab = await newTab(port, host);
    if (opts.windowSize) await setViewport(tab.webSocketDebuggerUrl, opts.windowSize);
    return {
      cdpPort: port,
      webSocketDebuggerUrl: tab.webSocketDebuggerUrl,
      kill() { closeTab(port, tab.id, host); },
    };
  }

  const cdpPort = await freePort();
  const flags = [
    '--headless=new', '--disable-gpu', '--no-sandbox',
    '--disable-features=RendererCodeIntegrity',
    // A container has no audio device, so getUserMedia would fail. Chrome's
    // fake device hands out a synthetic tone instead - that is what lets the
    // voice-input test record and decode real audio.
    '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
  ];
  // A test that needs a wide window (the turn index rail hides itself below
  // 900px) says so.
  if (opts.windowSize) flags.push('--window-size=' + opts.windowSize);
  // A test that wants the fake microphone to play a real recording says so.
  if (opts.fakeAudioFile) {
    flags.push('--use-file-for-fake-audio-capture=' + opts.fakeAudioFile);
  }
  flags.push(`--remote-debugging-port=${cdpPort}`, `--user-data-dir=${tmp}/${name}`, 'about:blank');
  const proc = spawn(CHROME, flags, { stdio: 'ignore' });

  let target = null;
  for (let i = 0; i < 120; i++) {
    try {
      const list = JSON.parse((await getJson(cdpPort, '/json/list')).body);
      target = list.find((t) => t.type === 'page');
      if (target) break;
    } catch {}
    await sleep(150);
  }
  if (!target) {
    try { proc.kill('SIGKILL'); } catch {}
    throw new Error('no chrome page target');
  }
  return {
    cdpPort,
    webSocketDebuggerUrl: target.webSocketDebuggerUrl,
    kill() { try { proc.kill('SIGKILL'); } catch {} },
  };
}
