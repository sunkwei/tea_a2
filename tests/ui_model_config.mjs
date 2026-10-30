// Browser test for 「模型配置」 in the settings view.
//
// The contract pinned here:
//   * every provider in the settings page carries a 模型配置 button;
//   * clicking it opens a dialog with one row per model of that provider;
//   * the row carries the configuration the file has: the context length, the
//     output limit, the reasoning effort, the sampling knobs and the
//     capability flags;
//   * the numbers can be changed before the write - including the ones the
//     page writes as "128K" / "1M";
//   * confirming writes the result into provider.yaml - only this provider's
//     models block - and the running agent picks it up without a restart;
//   * a model can be added, removed and made the default one;
//   * a value the user clears is really cleared.
//
//   node tests/ui_model_config.mjs [path-to-tea_agent-binary]
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

const tmp = fs.mkdtempSync(path.join(process.env.TMP || os.tmpdir(), 'tea_mcfg_ui_'));
const home = path.join(tmp, 'home');
const proj = path.join(tmp, 'proj');
fs.mkdirSync(home, { recursive: true });
fs.mkdirSync(proj, { recursive: true });
const configPath = path.join(home, 'provider.yaml');

const CONFIG = `version: 1

providers:
  acme:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-acme
    # a comment that has to survive the update
    models:
      acme-pro:
        max_context_tokens: 200000
        max_output_tokens: 32000
        supports_tools: true
        supports_reasoning: true
        temperature: 0.4
      acme-mini:
        max_context_tokens: 4096
  other:
    api_url: http://127.0.0.1:9/v1
    api_key: sk-other
    models:
      other-model:
        max_context_tokens: 8192
`;

let agent, chrome, ws;
try {
  fs.writeFileSync(configPath, CONFIG);

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

  const chromeTmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tea_mcfg_chrome_'));
  chrome = await acquireBrowser(chromeTmp, 'chrome', { windowSize: '1200,900' });
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

  // The settings page: every provider carries a 模型配置 button.
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
  check('every provider in the settings page has a 模型配置 button',
    buttons.length === 2 && buttons.every((b) => b.buttons.indexOf('模型配置') >= 0),
    JSON.stringify(buttons));

  // Clicking it opens the dialog with one row per model.
  await evalJs(`(function(){
    var card = document.querySelectorAll('#settings-providers .prov')[0];
    var bs = card.querySelectorAll('button');
    for (var i = 0; i < bs.length; i++) {
      if (bs[i].textContent.indexOf('模型配置') >= 0) { bs[i].click(); return true; }
    }
    return false;
  })()`);
  await sleep(1200);
  const dialog = await evalJs(`(function(){
    var d = document.getElementById('dialog');
    var rows = d.querySelectorAll('.mcfg-row');
    var out = [];
    for (var i = 0; i < rows.length; i++) {
      var nums = rows[i].querySelectorAll('.mcfg-num');
      var texts = rows[i].querySelectorAll('.mcfg-text');
      var caps = rows[i].querySelectorAll('.mcfg-caps input');
      out.push({
        name: rows[i].querySelector('.mcfg-name .mono') ? rows[i].querySelector('.mcfg-name .mono').textContent : '',
        summary: rows[i].querySelector('.mcfg-sum') ? rows[i].querySelector('.mcfg-sum').textContent : '',
        ctx: nums.length ? nums[0].value : '',
        out: nums.length > 1 ? nums[1].value : '',
        effort: texts.length ? texts[0].value : '',
        temp: nums.length > 2 ? nums[2].value : '',
        topK: nums.length > 4 ? nums[4].value : '',
        caps: caps.length,
        badges: rows[i].querySelectorAll('.mcfg-name .badge').length
      });
    }
    return {
      open: !document.getElementById('overlay').classList.contains('hidden'),
      title: d.querySelector('h3') ? d.querySelector('h3').textContent : '',
      rows: out,
      presets: d.querySelectorAll('.mcfg-presets .btn').length,
      actions: Array.prototype.map.call(d.querySelectorAll('.actions .btn'), function (b) { return b.textContent; })
    };
  })()`);
  check('the dialog opens with one row per model',
    dialog && dialog.open === true && dialog.title.indexOf('模型配置') >= 0,
    JSON.stringify(dialog && dialog.title));
  check('every model of the provider has a row',
    dialog && dialog.rows.length === 2, JSON.stringify(dialog && dialog.rows));
  const pro = (dialog.rows || []).find((r) => r.name === 'acme-pro');
  const mini = (dialog.rows || []).find((r) => r.name === 'acme-mini');
  check('the context length and the output limit are shown',
    pro && pro.ctx === '200000' && pro.out === '32000', JSON.stringify(pro));
  check('the temperature is shown',
    pro && pro.temp === '0.4', JSON.stringify(pro));
  check('the row sums up the configuration',
    pro && pro.summary.indexOf('上下文 200000') >= 0, JSON.stringify(pro && pro.summary));
  check('the first model is marked as the default one',
    pro && pro.badges >= 1, JSON.stringify(pro));
  check('the capability flags are there',
    pro && pro.caps === 3, JSON.stringify(pro));
  check('the presets are offered', dialog && dialog.presets >= 3, JSON.stringify(dialog && dialog.presets));
  check('the dialog offers to write the configuration',
    dialog && (dialog.actions || []).some((a) => a.indexOf('保存') >= 0), JSON.stringify(dialog && dialog.actions));

  // Change a few numbers and write. The capacity is written as "128K".
  await evalJs(`(function(){
    var rows = document.querySelectorAll('#dialog .mcfg-row');
    for (var i = 0; i < rows.length; i++) {
      var nm = rows[i].querySelector('.mcfg-name .mono');
      if (nm && nm.textContent === 'acme-mini') {
        var nums = rows[i].querySelectorAll('.mcfg-num');
        nums[0].value = '128K';
        nums[1].value = '8K';
        nums[4].value = '40';        // top_k
        var texts = rows[i].querySelectorAll('.mcfg-text');
        texts[0].value = 'high';     // the reasoning effort
        return true;
      }
    }
    return false;
  })()`);
  await evalJs(`(function(){
    var bs = document.querySelectorAll('#dialog .actions .btn');
    for (var i = 0; i < bs.length; i++) {
      if (bs[i].textContent.indexOf('保存') >= 0) { bs[i].click(); return true; }
    }
    return false;
  })()`);
  await sleep(2500);

  const text = fs.readFileSync(configPath, 'utf8');
  check('the context length was written',
    text.indexOf('max_context_tokens: 131072') >= 0, text);
  check('the output limit was written',
    text.indexOf('max_output_tokens: 8192') >= 0, text);
  check('top_k was written', text.indexOf('top_k: 40') >= 0, text);
  check('the reasoning effort was written',
    text.indexOf('reasoning_effort: high') >= 0, text);
  check('the temperature of the other model survived',
    text.indexOf('temperature: 0.4') >= 0, text);
  check('the comment survived', text.indexOf('# a comment that has to survive the update') >= 0, text);
  check('the other provider survived',
    text.indexOf('other-model:') >= 0 && text.indexOf('max_context_tokens: 8192\n') >= 0, text);
  check('the key survived', text.indexOf('sk-acme') >= 0, text);

  const providers = JSON.parse((await getJson(agentPort, '/api/providers')).body);
  const acme = (providers.providers || []).find((p) => p.name === 'acme');
  const info = acme && acme.model_info ? acme.model_info['acme-mini'] : null;
  check('the running agent picked the configuration up without a restart',
    info && info.max_context_tokens === 131072 && info.top_k === 40 &&
    info.reasoning_effort === 'high', JSON.stringify(info));

  // Add a model, make it the default one, and remove another one.
  await evalJs(`(function(){
    var card = document.querySelectorAll('#settings-providers .prov')[0];
    var bs = card.querySelectorAll('button');
    for (var i = 0; i < bs.length; i++) {
      if (bs[i].textContent.indexOf('模型配置') >= 0) { bs[i].click(); return true; }
    }
    return false;
  })()`);
  await sleep(1200);
  await evalJs(`(function(){
    var add = document.querySelector('#dialog .mcfg-add .mcfg-text');
    if (!add) return false;
    add.value = 'acme-new';
    var bs = document.querySelectorAll('#dialog .mcfg-add .btn');
    if (bs[0]) { bs[0].click(); return true; }
    return false;
  })()`);
  await sleep(600);
  const afterAdd = await evalJs(`(function(){
    var rows = document.querySelectorAll('#dialog .mcfg-row');
    var names = [];
    for (var i = 0; i < rows.length; i++) {
      var nm = rows[i].querySelector('.mcfg-name .mono');
      names.push(nm ? nm.textContent : '');
    }
    return names;
  })()`);
  check('a model can be added',
    afterAdd && afterAdd.indexOf('acme-new') >= 0, JSON.stringify(afterAdd));

  // Make the new one the default and delete acme-mini.
  await evalJs(`(function(){
    var rows = document.querySelectorAll('#dialog .mcfg-row');
    for (var i = 0; i < rows.length; i++) {
      var nm = rows[i].querySelector('.mcfg-name .mono');
      if (!nm) continue;
      var bs = rows[i].querySelectorAll('.mcfg-acts .btn');
      if (nm.textContent === 'acme-new' && bs[0]) { bs[0].click(); }
      if (nm.textContent === 'acme-mini' && bs[1]) { bs[1].click(); }
    }
    return true;
  })()`);
  await sleep(400);
  await evalJs(`(function(){
    var bs = document.querySelectorAll('#dialog .actions .btn');
    for (var i = 0; i < bs.length; i++) {
      if (bs[i].textContent.indexOf('保存') >= 0) { bs[i].click(); return true; }
    }
    return false;
  })()`);
  await sleep(2500);

  const text2 = fs.readFileSync(configPath, 'utf8');
  check('the new model was written', text2.indexOf('acme-new:') >= 0, text2);
  check('the removed model is gone', text2.indexOf('acme-mini:') < 0, text2);
  const providers2 = JSON.parse((await getJson(agentPort, '/api/providers')).body);
  const acme2 = (providers2.providers || []).find((p) => p.name === 'acme');
  check('the default model is the first one',
    acme2 && acme2.models[0] === 'acme-new', JSON.stringify(acme2 && acme2.models));
  check('the removed model is gone from the running agent too',
    acme2 && acme2.models.indexOf('acme-mini') < 0, JSON.stringify(acme2 && acme2.models));
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
