// The MCP servers in the settings page.
//
// The `mcp.yaml` next to provider.yaml lists the MCP servers the agent talks
// to. Their tools join the agent's tool set, so the settings page has to show
// what they are: a server that is not there must say why, otherwise "the model
// does not call the tool" looks like a model problem.
//
// This test pins:
//
//   * the card says which file the configuration comes from;
//   * a running server is listed with its status and its tools;
//   * a server that cannot be started is listed with the reason;
//   * the MCP tools show up in the list of available tools;
//   * "刷新" re-reads the file and reconnects the servers.
//
//   node tests/ui_mcp.mjs [path-to-tea_agent-binary]
//
// Requires google-chrome (or CHROME_BIN) and node >= 18.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { wsConnect } from './mini_ws.mjs';
import { acquireBrowser } from './chrome_pool.mjs';

const REPO_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const BIN = process.argv[2] || path.join(REPO_ROOT, 'build', 'bin', 'tea_agent');
const CHROME = process.env.CHROME_BIN || '/usr/bin/google-chrome';
const MOCK_MCP = path.join(REPO_ROOT, 'tests', 'mock_mcp_server.py');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const freePort = () => new Promise((res) => {
  const s = net.createServer();
  s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => res(p)); });
});

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? '  ' + detail : ''));
};

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_ui_mcp_'));
fs.mkdirSync(path.join(tmp, 'ws'), { recursive: true });
fs.mkdirSync(path.join(tmp, 'home'), { recursive: true });

let agent, chrome, ws;
try {
  const agentPort = await freePort();
  fs.writeFileSync(path.join(tmp, 'provider.yaml'), `version: 1
providers:
  mock:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-test
    default_model: mock-model
    models:
      mock-model: {}
`);
  // The MCP configuration is a file of its own, next to provider.yaml.
  fs.writeFileSync(path.join(tmp, 'mcp.yaml'), `servers:
  mock:
    command: "python3"
    args: ["${MOCK_MCP}"]
  broken:
    command: /nonexistent/mcp-server-that-is-not-there
`);

  agent = spawn(BIN, ['--config', path.join(tmp, 'provider.yaml'), '--tea-home', path.join(tmp, 'home'),
    '--port', String(agentPort), '--no-console', '--log-file', path.join(tmp, 'agent.log'),
    '--workspace', path.join(tmp, 'ws')], { stdio: 'ignore' });

  let up = false;
  for (let i = 0; i < 100; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${agentPort}/api/health`);
      if (r.status === 200) { up = true; break; }
    } catch {}
    await sleep(150);
  }
  if (!up) throw new Error('the agent did not come up');

  chrome = await acquireBrowser(tmp);
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
  const waitFor = async (expr, tries = 80) => {
    for (let i = 0; i < tries; i++) {
      const v = await evalJs(expr);
      if (v) return v;
      await sleep(150);
    }
    return null;
  };

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: `http://127.0.0.1:${agentPort}/` });
  await sleep(1200);

  // ---- 1. the settings page lists the servers ----------------------------
  await evalJs(`document.querySelector('[data-view="settings"]').click()`);
  await sleep(400);
  const cards = await waitFor(`(function(){
    var box = document.getElementById('settings-mcp');
    if (!box) return null;
    var out = Array.from(box.querySelectorAll('.tool-card')).map(function (c) {
      return { text: c.textContent, badge: (c.querySelector('.badge') || {}).textContent || '' };
    });
    if (!out.length) return null;
    return out;
  })()`);
  check('the settings page lists the MCP servers', !!cards && cards.length === 2, JSON.stringify(cards));
  const mcpPath = await evalJs(`(document.getElementById('mcp-path') || {}).textContent || ''`);
  check('the card says which file the configuration comes from',
    /mcp\.yaml$/.test(mcpPath), mcpPath);
  const good = (cards || []).find((c) => c.text.indexOf('mock') === 0);
  const bad = (cards || []).find((c) => c.text.indexOf('broken') === 0);
  check('a running server is marked as connected', !!good && good.badge.indexOf('已连接') >= 0,
    good ? good.badge : 'no card');
  check('a running server shows its tools',
    !!good && good.text.indexOf('echo') >= 0 && good.text.indexOf('add') >= 0,
    good ? good.text.slice(0, 200) : 'no card');
  check('a server that cannot be started is marked as not connected',
    !!bad && bad.badge.indexOf('未连接') >= 0, bad ? bad.badge : 'no card');
  check('a server that cannot be started says why',
    !!bad && bad.text.indexOf('nonexistent') >= 0, bad ? bad.text.slice(0, 200) : 'no card');

  // ---- 2. the tools list has the MCP tools ------------------------------
  const toolNames = await waitFor(`(function(){
    var names = Array.from(document.querySelectorAll('#settings-tools .tool-card h4'))
      .map(function (h) { return h.textContent; });
    if (!names.length) return null;
    return names;
  })()`);
  check('the MCP tools are in the list of available tools',
    !!(toolNames && toolNames.indexOf('mcp__mock__echo') >= 0), JSON.stringify(toolNames));

  // ---- 3. 刷新 reconnects ------------------------------------------------
  await evalJs(`document.getElementById('mcp-refresh').click()`);
  await sleep(2500);
  const after = await waitFor(`(function(){
    var box = document.getElementById('settings-mcp');
    if (!box) return null;
    var cards = Array.from(box.querySelectorAll('.tool-card'));
    if (!cards.length) return null;
    return cards.map(function (c) {
      return { text: c.textContent, badge: (c.querySelector('.badge') || {}).textContent || '' };
    });
  })()`);
  const good2 = (after || []).find((c) => c.text.indexOf('mock') === 0);
  check('after a refresh the server is connected again',
    !!good2 && good2.badge.indexOf('已连接') >= 0, JSON.stringify(after));
} catch (e) {
  check('the test ran to the end', false, String(e && e.message ? e.message : e));
} finally {
  try { if (ws) ws.close(); } catch {}
  try { if (agent) agent.kill(); } catch {}
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch {}
}

const failed = results.filter((r) => !r.ok);
console.log('');
console.log(`${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
