// Browser test for 「更新模型」 in the settings view.
//
// The contract pinned here:
//   * every provider in the settings page carries a 更新模型 button;
//   * clicking it asks the provider for its model list (the server does the
//     query with the provider's own key) and lists every model with the
//     configuration tea_a2 would write;
//   * a model the provider does not offer any more is listed as well, and is
//     NOT selected by default;
//   * the numbers can be changed before the write;
//   * confirming writes the list into provider.yaml - only this provider's
//     models block - and the running agent picks it up without a restart;
//   * a provider without a model list endpoint says so instead of failing
//     silently.
//
//   node tests/ui_provider_models.mjs [path-to-tea_agent-binary]
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

const tmp = fs.mkdtempSync(path.join(process.env.TMP || os.tmpdir(), 'tea_models_ui_'));
const home = path.join(tmp, 'home');
const proj = path.join(tmp, 'proj');
fs.mkdirSync(home, { recursive: true });
fs.mkdirSync(proj, { recursive: true });
const configPath = path.join(home, 'provider.yaml');

// The mock provider: the OpenAI model list, and one provider without the
// endpoint at all.
const mock = http.createServer((req, res) => {
  if (req.url.startsWith('/v1/models')) {
    const body = JSON.stringify({
      object: 'list',
      data: [
        { id: 'acme-pro', object: 'model', context_length: 200000, max_completion_tokens: 32000,
          supported_parameters: ['tools', 'reasoning'] },
        { id: 'acme-mini', object: 'model' },
      ],
    });
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(body);
    return;
  }
  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end('{"error":"no model list here"}');
});

const CONFIG = `version: 1

providers:
  acme:
    api_url: http://127.0.0.1:MOCKPORT
    api_key: sk-acme
    # a comment that has to survive the update
    models:
      acme-old:
        max_context_tokens: 4096
  nomodels:
    api_url: http://127.0.0.1:MOCKPORT/nope
    api_key: sk-nope
    models:
      nomodels-old:
        max_context_tokens: 4096
`;

let agent, chrome, ws;
try {
  const mockPort = await freePort();
  await new Promise((r) => mock.listen(mockPort, '127.0.0.1', r));
  fs.writeFileSync(configPath, CONFIG.replace(/MOCKPORT/g, String(mockPort)));

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

  const cdpPort = await freePort();
  chrome = await acquireBrowser(tmp, 'chrome', { windowSize: '1200,900' });

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

  // The settings page: every provider carries a 更新模型 button.
  await evalJs(`(function(){
    var b = Array.prototype.filter.call(document.querySelectorAll('.nav-row'),
      function (x) { return x.dataset.view === 'settings'; })[0];
    if (b) b.click();
    return true;
  })()`);
  await sleep(900);
  const buttons = await evalJs(`(function(){
    var cards = document.querySelectorAll('#settings-providers .prov');
    var out = [];
    for (var i = 0; i < cards.length; i++) {
      var bs = cards[i].querySelectorAll('button');
      var names = [];
      for (var j = 0; j < bs.length; j++) names.push(bs[j].textContent);
      out.push({ provider: cards[i].querySelector('h4') ? cards[i].querySelector('h4').textContent : '',
                 buttons: names });
    }
    return out;
  })()`);
  check('every provider in the settings page has a 更新模型 button',
    buttons.length === 2 && buttons.every((b) => b.buttons.indexOf('更新模型') >= 0),
    JSON.stringify(buttons));

  // Clicking it lists the models of the provider.
  await evalJs(`(function(){
    var card = document.querySelectorAll('#settings-providers .prov')[0];
    var bs = card.querySelectorAll('button');
    for (var i = 0; i < bs.length; i++) {
      if (bs[i].textContent.indexOf('更新模型') >= 0) { bs[i].click(); return true; }
    }
    return false;
  })()`);
  await sleep(1500);
  const dialog = await evalJs(`(function(){
    var d = document.getElementById('dialog');
    var rows = d.querySelectorAll('.mdl-row');
    var out = [];
    for (var i = 0; i < rows.length; i++) {
      var pick = rows[i].querySelector('.mdl-pick');
      var nums = rows[i].querySelectorAll('.mdl-num');
      out.push({
        name: rows[i].querySelector('.mdl-name') ? rows[i].querySelector('.mdl-name').textContent : '',
        picked: pick ? pick.checked : null,
        ctx: nums.length ? nums[0].value : '',
        out: nums.length > 1 ? nums[1].value : ''
      });
    }
    return {
      open: !document.getElementById('overlay').classList.contains('hidden'),
      title: d.querySelector('h3') ? d.querySelector('h3').textContent : '',
      rows: out,
      actions: Array.prototype.map.call(d.querySelectorAll('.actions .btn'), function (b) { return b.textContent; })
    };
  })()`);
  check('the dialog opens with the model list',
    dialog && dialog.open === true && dialog.title.indexOf('更新模型') >= 0, JSON.stringify(dialog && dialog.title));
  check('every model of the provider is listed',
    dialog && dialog.rows.length === 3, JSON.stringify(dialog && dialog.rows));
  const pro = (dialog.rows || []).find((r) => r.name.indexOf('acme-pro') === 0);
  const mini = (dialog.rows || []).find((r) => r.name.indexOf('acme-mini') === 0);
  const old = (dialog.rows || []).find((r) => r.name.indexOf('acme-old') === 0);
  check('the configuration of a model is shown',
    pro && pro.ctx === '200000' && pro.out === '32000', JSON.stringify(pro));
  check('a new model is selected by default', mini && mini.picked === true, JSON.stringify(mini));
  check('a model the provider no longer offers is not selected',
    old && old.picked === false && old.name.indexOf('提供方已不再提供') >= 0, JSON.stringify(old));
  check('the dialog offers to write the list',
    dialog && (dialog.actions || []).some((a) => a.indexOf('写入') >= 0), JSON.stringify(dialog && dialog.actions));

  // Change a number and write.
  await evalJs(`(function(){
    var rows = document.querySelectorAll('#dialog .mdl-row');
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].querySelector('.mdl-name').textContent.indexOf('acme-mini') === 0) {
        var nums = rows[i].querySelectorAll('.mdl-num');
        nums[0].value = '65536';
        return true;
      }
    }
    return false;
  })()`);
  await evalJs(`(function(){
    var bs = document.querySelectorAll('#dialog .actions .btn');
    for (var i = 0; i < bs.length; i++) {
      if (bs[i].textContent.indexOf('写入') >= 0) { bs[i].click(); return true; }
    }
    return false;
  })()`);
  await sleep(2500);

  const text = fs.readFileSync(configPath, 'utf8');
  check('the confirmed list was written into provider.yaml',
    text.indexOf('acme-pro:') >= 0 && text.indexOf('acme-mini:') >= 0, text);
  check('the changed number was written',
    text.indexOf('65536') >= 0, text);
  check('the model the provider no longer offers was dropped',
    text.indexOf('acme-old:') < 0, text);
  check('the comment survived', text.indexOf('# a comment that has to survive the update') >= 0, text);
  check('the other provider survived', text.indexOf('nomodels:') >= 0, text);
  check('the key survived', text.indexOf('sk-acme') >= 0, text);

  const providers = JSON.parse((await getJson(agentPort, '/api/providers')).body);
  const acme = (providers.providers || []).find((p) => p.name === 'acme');
  check('the running agent picked the new list up without a restart',
    acme && acme.models.join(',') === 'acme-pro,acme-mini', JSON.stringify(acme && acme.models));

  // A provider without the endpoint says so instead of failing silently.
  await evalJs(`(function(){
    var card = document.querySelectorAll('#settings-providers .prov')[1];
    var bs = card.querySelectorAll('button');
    for (var i = 0; i < bs.length; i++) {
      if (bs[i].textContent.indexOf('更新模型') >= 0) { bs[i].click(); return true; }
    }
    return false;
  })()`);
  await sleep(1500);
  const failed = await evalJs(`(function(){
    var d = document.getElementById('dialog');
    return { text: d.textContent || '', open: !document.getElementById('overlay').classList.contains('hidden') };
  })()`);
  check('a provider without a model list says so',
    failed && failed.open === true && failed.text.indexOf('查询失败') >= 0, JSON.stringify(failed && failed.text.slice(0, 120)));
} catch (e) {
  check('no exception', false, e && e.stack ? e.stack : String(e));
} finally {
  try { if (ws) ws.close(); } catch {}
  for (const p of [chrome, agent]) {
    try { if (p) p.kill(); } catch {}
  }
  try { mock.close(); } catch {}
  await sleep(300);
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch {}
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length === 0 ? 0 : 1);
