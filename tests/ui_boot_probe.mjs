// Headless boot probe for web/app.js.
//
// The asset checks in e2e.ps1 only assert that the served HTML/JS *contains*
// certain strings: they never execute the page. That makes a throw inside
// init() completely invisible -- the browser keeps showing the placeholder
// text ("连接中…") baked into index.html, while the API stays perfectly fine.
//
// This probe executes the real web/app.js against a minimal DOM stub built by
// parsing the real web/index.html, feeds it the same JSON the server returns,
// fires the DOMContentLoaded path exactly like a browser would, and fails on
// any exception (sync or unhandled rejection) or on a status element that never
// reaches its connected state.
//
//   node tests/ui_boot_probe.mjs                          # canned fixtures
//   node tests/ui_boot_probe.mjs --base http://127.0.0.1:8399   # served page
//   node tests/ui_boot_probe.mjs --base ... --live-api    # + the server's JSON
//   node tests/ui_boot_probe.mjs --state noworkspace      # first-run variants
//   node tests/ui_boot_probe.mjs --all                    # every state
//   node tests/ui_boot_probe.mjs --app some/app.js        # boot a given file
//
// --base also fetches index.html and app.js *from the server* instead of from
// web/, which is what catches a broken asset embedder: a corrupted app.js is a
// syntax error, so nothing runs and the status never leaves its placeholder.
//
// Exits 0 when every state boots cleanly.

import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const WEB_DIR = path.join(REPO_ROOT, 'web');

/* ------------------------------------------------------------------ *
 * 1) Minimal DOM: elements, classList, style, dataset, a real tree.
 * ------------------------------------------------------------------ */

const VOID_TAGS = new Set(['meta', 'link', 'br', 'img', 'input', 'hr',
  'path', 'circle', 'rect', 'line', 'polyline', 'use', 'source', 'area', 'col']);

class ClassList {
  constructor(el) { this.el = el; }
  _list() { return this.el._classes; }
  _has(c) { return this._list().indexOf(c) >= 0; }
  add(...names) { for (const n of names) if (n && !this._has(n)) this._list().push(n); }
  remove(...names) { for (const n of names) this.el._classes = this._list().filter((c) => c !== n); }
  contains(n) { return this._has(n); }
  toggle(n, force) {
    const want = force === undefined ? !this._has(n) : !!force;
    if (want) this.add(n); else this.remove(n);
    return want;
  }
  get value() { return this._list().join(' '); }
}

let doc; // forward declaration; Element needs the shared id map

class Element {
  constructor(tagName) {
    this.tagName = String(tagName || 'div').toLowerCase();
    this.nodeType = 1;
    this.parentNode = null;
    this.children = [];
    this._text = '';
    this._classes = [];
    this._attrs = Object.create(null);
    this._listeners = Object.create(null);
    this.style = {};
    this.dataset = {};
    this.classList = new ClassList(this);
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.title = '';
    this.type = '';
    this.rows = 0;
    this.placeholder = '';
    this.files = null;
    this.scrollTop = 0;
    this.scrollHeight = 48;
    this.clientHeight = 32;
    this.scrollWidth = 320;
    this.offsetWidth = 320;
    this.hidden = false;
    this.id = '';
  }
  get id() { return this._id || ''; }
  set id(v) {
    this._id = String(v || '');
    if (doc && this._id) doc._byId.set(this._id, this);
  }
  get className() { return this._classes.join(' '); }
  set className(v) {
    this._classes = String(v == null ? '' : v).split(/\s+/).filter(Boolean);
    if (doc) {
      for (const c of this._classes) {
        if (!doc._classIndex.has(c)) doc._classIndex.set(c, new Set());
        doc._classIndex.get(c).add(this);
      }
    }
  }
  get firstChild() { return this.children.length ? this.children[0] : null; }
  get childNodes() { return this.children; }
  get textContent() {
    if (!this.children.length) return this._text;
    return this._text + this.children.map((c) => c.textContent).join('');
  }
  set textContent(v) {
    this.children = [];
    this._text = String(v == null ? '' : v);
  }
  get innerHTML() { return this._html || ''; }
  set innerHTML(v) {
    this._html = String(v == null ? '' : v);
    this.children = [];
    const frag = parseHTML(this._html);
    for (const child of frag.children.slice()) this.appendChild(child);
  }
  appendChild(node) {
    if (!node) return node;
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.children.push(node);
    if (node.id && doc) doc._byId.set(node.id, node);
    return node;
  }
  insertBefore(node, ref) {
    if (!ref) return this.appendChild(node);
    const i = this.children.indexOf(ref);
    if (i < 0) return this.appendChild(node);
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.children.splice(i, 0, node);
    return node;
  }
  removeChild(node) {
    const i = this.children.indexOf(node);
    if (i >= 0) this.children.splice(i, 1);
    if (node) node.parentNode = null;
    return node;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  append(node) { this.appendChild(node); }
  setAttribute(name, value) {
    this._attrs[name] = String(value);
    if (name === 'id') this.id = value;
    if (name === 'class') this.className = value;
    if (name.startsWith('data-')) this.dataset[camel(name.slice(5))] = String(value);
    if (name === 'checked') this.checked = true;
    if (name === 'disabled') this.disabled = true;
    if (name === 'title') this.title = String(value);
    if (name === 'value') this.value = String(value);
  }
  getAttribute(name) { return name in this._attrs ? this._attrs[name] : null; }
  removeAttribute(name) { delete this._attrs[name]; }
  hasAttribute(name) { return name in this._attrs; }
  addEventListener(type, fn) {
    (this._listeners[type] || (this._listeners[type] = [])).push(fn);
  }
  removeEventListener(type, fn) {
    this._listeners[type] = (this._listeners[type] || []).filter((f) => f !== fn);
  }
  dispatchEvent(ev) {
    ev = ev || {};
    if (!ev.target) ev.target = this;
    if (!ev.preventDefault) ev.preventDefault = () => { ev.defaultPrevented = true; };
    if (!ev.stopPropagation) ev.stopPropagation = () => { ev.stopped = true; };
    const handler = this['on' + (ev.type || '')];
    if (typeof handler === 'function') handler.call(this, ev);
    for (const fn of (this._listeners[ev.type] || []).slice()) fn.call(this, ev);
    return !ev.defaultPrevented;
  }
  click() { this.dispatchEvent({ type: 'click' }); }
  focus() { if (doc) doc.activeElement = this; }
  blur() {}
  select() {}
  closest(sel) {
    let n = this;
    while (n && n.nodeType === 1) {
      if (matches(n, sel)) return n;
      n = n.parentNode;
    }
    return null;
  }
  matches(sel) { return matches(this, sel); }
  querySelector(sel) { return queryAll(this, sel, true)[0] || null; }
  querySelectorAll(sel) { return queryAll(this, sel, false); }
  getBoundingClientRect() { return { width: 320, height: 24, top: 0, left: 0, right: 320, bottom: 24 }; }
}

function camel(s) { return s.replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }

const SELECTOR_CACHE = new Map();
function parseSelector(sel) {
  if (SELECTOR_CACHE.has(sel)) return SELECTOR_CACHE.get(sel);
  const steps = String(sel).trim().split(/\s+/).filter(Boolean).map((part) => {
    const m = { tag: null, id: null, classes: [] };
    let rest = part;
    if (rest[0] !== '.' && rest[0] !== '#') {
      const t = /^[a-zA-Z0-9-]+/.exec(rest);
      if (t) { m.tag = t[0].toLowerCase(); rest = rest.slice(t[0].length); }
    }
    const re = /([.#])([a-zA-Z0-9_-]+)/g;
    let mm;
    while ((mm = re.exec(rest))) {
      if (mm[1] === '.') m.classes.push(mm[2]);
      else m.id = mm[2];
    }
    return m;
  });
  SELECTOR_CACHE.set(sel, steps);
  return steps;
}
function matchesStep(el, step) {
  if (el.nodeType !== 1) return false;
  if (step.tag && el.tagName !== step.tag) return false;
  if (step.id && el.id !== step.id) return false;
  for (const c of step.classes) if (!el.classList.contains(c)) return false;
  return true;
}
function matches(el, sel) {
  const steps = parseSelector(sel);
  if (!steps.length) return false;
  if (!matchesStep(el, steps[steps.length - 1])) return false;
  let i = steps.length - 2;
  let n = el.parentNode;
  while (i >= 0 && n && n.nodeType === 1) {
    if (matchesStep(n, steps[i])) i--;
    n = n.parentNode;
  }
  return i < 0;
}
function queryAll(root, sel, firstOnly) {
  const out = [];
  const walk = (node) => {
    for (const child of node.children) {
      if (matches(child, sel)) {
        out.push(child);
        if (firstOnly) return true;
      }
      if (walk(child)) return true;
    }
    return false;
  };
  // A fragment/root matches nothing itself; the document searches from body.
  walk(root);
  return out;
}

/* ------------------------------------------------------------------ *
 * 2) A tiny HTML parser, so the probe uses the *real* index.html.
 *    Missing element ids then throw exactly like a browser would.
 * ------------------------------------------------------------------ */

function findTagEnd(html, start) {
  let quote = null;
  for (let i = start + 1; i < html.length; i++) {
    const c = html[i];
    if (quote) { if (c === quote) quote = null; continue; }
    if (c === '"' || c === "'") { quote = c; continue; }
    if (c === '>') return i;
  }
  return -1;
}

function applyAttrs(el, raw) {
  const re = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>`]+)))?/g;
  let m;
  while ((m = re.exec(raw))) {
    const name = m[1].toLowerCase();
    const value = m[3] !== undefined ? m[3] : (m[4] !== undefined ? m[4] : (m[5] !== undefined ? m[5] : ''));
    el.setAttribute(name, value);
  }
}

function parseHTML(html) {
  const frag = new Element('#fragment');
  const stack = [frag];
  let i = 0;
  while (i < html.length) {
    const lt = html.indexOf('<', i);
    if (lt < 0) break;
    if (html.startsWith('<!--', lt)) {
      const end = html.indexOf('-->', lt);
      i = end < 0 ? html.length : end + 3;
      continue;
    }
    if (html.startsWith('<!', lt) || html.startsWith('<?', lt)) {
      const end = html.indexOf('>', lt);
      i = end < 0 ? html.length : end + 1;
      continue;
    }
    const gt = findTagEnd(html, lt);
    if (gt < 0) break;
    const raw = html.slice(lt + 1, gt).trim();
    if (!raw) { i = gt + 1; continue; }
    if (raw[0] === '/') {
      const name = raw.slice(1).trim().toLowerCase();
      for (let k = stack.length - 1; k > 0; k--) {
        if (stack[k].tagName === name) { stack.length = k; break; }
      }
      i = gt + 1;
      continue;
    }
    const selfClose = raw.endsWith('/');
    const name = (/^[a-zA-Z0-9-]+/.exec(raw) || ['div'])[0].toLowerCase();
    const el = new Element(name);
    applyAttrs(el, raw.slice(name.length).replace(/\/$/, ''));
    stack[stack.length - 1].appendChild(el);
    if (name === 'script') {
      const close = html.indexOf('</script>', gt);
      i = close < 0 ? html.length : close + '</script>'.length;
      continue;
    }
    if (!selfClose && !VOID_TAGS.has(name)) stack.push(el);
    i = gt + 1;
  }
  return frag;
}

/* ------------------------------------------------------------------ *
 * 3) Canned responses, copied from the live server's actual JSON.
 * ------------------------------------------------------------------ */

const CANNED = {
  '/api/health': {
    config_loaded: true, http_engine: 'auto', model: 'mock-model',
    ok: true, provider: 'mock', uptime_s: 1.588, version: '0.1.3',
  },
  // Asked on every boot: is a turn still running for the session we restored,
  // and is any turn running at all (a cold boot jumps to the work in progress)?
  '/api/chat/turn': { ok: true, running: false, session_id: 's_349631df65d5' },
  '/api/chat/turns': { ok: true, turns: [] },
  '/api/providers': {
    active: { max_context_tokens: 128000, model: 'mock-model', provider: 'mock' },
    config_path: 'C:\\tmp\\home\\provider.yaml',
    providers: [{
      api_url: 'http://127.0.0.1:8793/v1',
      default_model: 'mock-model',
      description: 'local mock used by the end-to-end test',
      has_key: true,
      model_info: { 'mock-model': { max_context_tokens: 128000, max_output_tokens: 8192, supports_tools: true } },
      models: ['mock-model'],
      name: 'mock',
    }],
    settings: { max_tool_output: 65536 },
    warnings: [],
  },
  '/api/tools': {
    tools: [{
      name: 'toolkit_exec',
      description: 'Run a shell command on the host machine.',
      parameters: { type: 'object', properties: { command: { type: 'string' } }, required: ['command'] },
    }],
  },
  '/api/workspaces': {
    index_path: 'C:\\tmp\\home\\workspaces.json',
    tea_home: 'C:\\tmp\\home',
    workspaces: [{
      created_at: '2026-10-18T11:51:00Z', exists: true, id: 'ws_7b216557eb08',
      last_used_at: '2026-10-18T11:51:04Z', name: 'probe-ws',
      path: 'C:\\tmp\\workspace', sessions: 1,
    }],
  },
  '/api/workspaces/ws_7b216557eb08/sessions': {
    sessions: [{
      created_at: '2026-10-18T11:51:04Z', id: 's_349631df65d5',
      message_count: 8, title: 'hi', updated_at: '2026-10-18T11:51:04Z',
    }],
  },
  '/api/sessions/s_349631df65d5': {
    session: { id: 's_349631df65d5', title: 'hi', created_at: '2026-10-18T11:51:04Z', updated_at: '2026-10-18T11:51:04Z' },
    messages: [
      { content: 'hi', created_at: '2026-10-18T11:51:04Z', role: 'user' },
      { content: 'Done.', created_at: '2026-10-18T11:51:05Z', role: 'assistant' },
    ],
  },
  '/api/sessions/s_349631df65d5/trajectory': [],
  '/api/config/status': { loaded: true, path: 'C:\\tmp\\home\\provider.yaml', providers: ['mock'], tea_home: 'C:\\tmp\\home' },
};

/* ------------------------------------------------------------------ *
 * 4) Boot one state in its own vm context.
 * ------------------------------------------------------------------ */

function makeDocument(html, baseURI) {
  doc = {
    readyState: 'loading',
    baseURI,
    hidden: false,
    activeElement: null,
    _byId: new Map(),
    _classIndex: new Map(),
    _listeners: Object.create(null),
    title: '',
  };
  const frag = parseHTML(html);
  // index.html: <html><head>..</head><body>..</body></html>
  const htmlEl = frag.children.find((c) => c.tagName === 'html') || frag;
  const body = htmlEl.children.find((c) => c.tagName === 'body') || htmlEl;
  doc.documentElement = htmlEl;
  doc.body = body;
  doc.head = htmlEl.children.find((c) => c.tagName === 'head') || htmlEl;

  doc.getElementById = (id) => doc._byId.get(id) || null;
  doc.createElement = (tag) => new Element(tag);
  doc.createTextNode = (text) => {
    const n = new Element('#text');
    n.nodeType = 3;
    n.textContent = text;
    return n;
  };
  doc.querySelector = (sel) => queryAll(body, sel, true)[0] || null;
  doc.querySelectorAll = (sel) => queryAll(body, sel, false);
  doc.addEventListener = (type, fn) => {
    (doc._listeners[type] || (doc._listeners[type] = [])).push(fn);
  };
  doc.removeEventListener = (type, fn) => {
    doc._listeners[type] = (doc._listeners[type] || []).filter((f) => f !== fn);
  };
  doc.dispatchEvent = (ev) => {
    for (const fn of (doc._listeners[ev.type] || []).slice()) fn.call(doc, ev);
    return !ev.defaultPrevented;
  };
  doc.execCommand = () => true;
  return doc;
}

function makeLocalStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(String(k)) ? map.get(String(k)) : null),
    setItem: (k, v) => { map.set(String(k), String(v)); },
    removeItem: (k) => { map.delete(String(k)); },
    clear: () => map.clear(),
    key: (i) => [...map.keys()][i] ?? null,
    get length() { return map.size; },
  };
}

async function bootState(state, opts) {
  // With --base, use the *served* assets: that is what the browser executes,
  // and it is where a broken asset embedder shows up (app.js with the next
  // asset appended is a syntax error, so nothing runs and the status stays on
  // the 连接中 placeholder while every string-based check still passes).
  const loadAsset = async (rel) => {
    if (rel === '/app.js' && opts.app) return fs.readFileSync(opts.app, 'utf8');
    if (rel === '/' && opts.html) return fs.readFileSync(opts.html, 'utf8');
    if (!opts.base) {
      const name = rel === '/' ? 'index.html' : rel.replace(/^\//, '');
      return fs.readFileSync(path.join(WEB_DIR, name), 'utf8');
    }
    const url = opts.base.replace(/\/*$/, '') + rel;
    const r = await globalThis.fetch(url);
    const body = await r.text();
    if (!r.ok) throw new Error('GET ' + rel + ' -> HTTP ' + r.status);
    return body;
  };
  const indexHtml = await loadAsset('/');
  const appJs = await loadAsset('/app.js');

  const baseURI = opts.base ? opts.base.replace(/\/*$/, '/') : 'http://127.0.0.1:0/';
  const document = makeDocument(indexHtml, baseURI);

  const errors = [];
  const requests = [];
  const timers = [];
  let timerSeq = 1;
  let inFlight = 0;

  const recordError = (where, err) => {
    errors.push({ where, message: err && err.message, stack: err && err.stack });
  };

  // ---- fetch: canned fixtures unless --live-api asked for the real server --
  const canned = structuredClone(CANNED);
  if (state === 'noworkspace') {
    canned['/api/workspaces'] = { index_path: 'C:\\tmp\\home\\workspaces.json', tea_home: 'C:\\tmp\\home', workspaces: [] };
  }
  if (state === 'noprovider') {
    canned['/api/health'] = { ...CANNED['/api/health'], config_loaded: false, provider: '', model: '' };
    canned['/api/providers'] = { active: null, config_path: '', providers: [], settings: {}, warnings: [] };
    canned['/api/config/status'] = { loaded: false, path: 'C:\\tmp\\home\\provider.yaml', providers: [], tea_home: 'C:\\tmp\\home' };
  }

  // The served page is always what we execute when --base is given. API
  // responses default to the canned fixtures so the assertions are
  // deterministic (a workspace with no sessions yet is a legitimate state).
  // --live-api replays the real server's JSON instead.
  const useLiveApi = !!opts.base && opts.liveApi;

  const respond = async (url) => {
    const u = new URL(url, baseURI);
    const pathname = u.pathname;
    requests.push(pathname);
    if (useLiveApi) {
      // Real server: exactly the bytes the browser would get.
      const r = await globalThis.fetch(baseURI.replace(/\/*$/, '') + pathname + u.search);
      const body = await r.text();
      return { ok: r.ok, status: r.status, body };
    }
    if (pathname in canned) {
      return { ok: true, status: 200, body: JSON.stringify(canned[pathname]) };
    }
    if (pathname.startsWith('/api/')) {
      return { ok: false, status: 404, body: JSON.stringify({ error: { code: 'not_found', message: 'no fixture for ' + pathname } }) };
    }
    return { ok: true, status: 200, body: '' };
  };

  const fetchStub = async (input, init) => {
    const url = typeof input === 'string' ? input : String(input && input.url);
    const method = (init && init.method) || 'GET';
    inFlight++;
    let res;
    try {
      res = await respond(url);
    } catch (e) {
      recordError('fetch ' + method + ' ' + url, e);
      return { ok: false, status: 0, statusText: 'network error', text: async () => '', json: async () => null };
    } finally {
      inFlight--;
    }
    return {
      ok: res.ok,
      status: res.status,
      statusText: res.ok ? 'OK' : 'ERROR',
      headers: { get: () => 'application/json' },
      text: async () => res.body,
      json: async () => JSON.parse(res.body),
    };
  };

  // ---- fake timers: only zero-ish timeouts are drained --------------------
  const fakeSetTimeout = (fn, ms) => {
    const id = timerSeq++;
    timers.push({ id, fn, at: Date.now() + (Number(ms) || 0), repeat: false, fired: false });
    return id;
  };
  const fakeSetInterval = (fn, ms) => {
    const id = timerSeq++;
    timers.push({ id, fn, at: Date.now() + (Number(ms) || 1), repeat: true, fired: false });
    return id;
  };
  const fakeClear = (id) => {
    const t = timers.find((x) => x.id === id);
    if (t) t.cancelled = true;
  };

  const location = {
    href: baseURI, reload: () => { location.reloaded = true; },
    origin: baseURI.replace(/\/$/, ''),
  };

  const sandbox = {
    document,
    window: {},
    location,
    localStorage: makeLocalStorage(),
    navigator: { userAgent: 'ui-boot-probe', clipboard: null },
    fetch: fetchStub,
    URL,
    URLSearchParams,
    setTimeout: fakeSetTimeout,
    clearTimeout: fakeClear,
    setInterval: fakeSetInterval,
    clearInterval: fakeClear,
    console: { log() {}, warn() {}, error() {} },
    FileReader: class { readAsText() {} },
    File: class {},
    crypto: { randomUUID: () => '00000000-0000-0000-0000-000000000000' },
    // init() binds window-level events (resize -> the turn index) and schedules
    // animation frames. Without these the whole page throws before it renders,
    // which is exactly the kind of breakage this probe exists to catch.
    addEventListener() {},
    removeEventListener() {},
    requestAnimationFrame: (fn) => fakeSetTimeout(fn, 16),
    cancelAnimationFrame: fakeClear,
    innerWidth: 1440,
    innerHeight: 900,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;

  const unhandled = [];
  const onUnhandled = (reason) => {
    unhandled.push(reason);
    recordError('unhandledRejection', reason instanceof Error ? reason : new Error(String(reason)));
  };
  process.on('unhandledRejection', onUnhandled);

  const context = vm.createContext(sandbox);

  // Load the page script. A throw here is a parse/eval error, reported as-is.
  let evalError = null;
  try {
    vm.runInContext(appJs, context, { filename: 'web/app.js' });
  } catch (e) {
    evalError = e;
  }

  // Browser order: <script src> at end of body runs while readyState is
  // "loading", then DOMContentLoaded fires.
  let initError = null;
  if (!evalError) {
    try {
      document.readyState = 'interactive';
      document.dispatchEvent({ type: 'DOMContentLoaded' });
    } catch (e) {
      initError = e;
    }
  }

  // Drain microtasks, timer callbacks and in-flight responses, with a hard
  // time budget. A request that has not settled yet is *not* idle.
  const started = Date.now();
  let idle = 0;
  let lastActivity = requests.length;
  while (Date.now() - started < 6000 && idle < 20) {
    await new Promise((r) => setImmediate(r));
    let fired = 0;
    for (const t of timers) {
      if (t.repeat || t.fired || t.cancelled) continue;
      if (t.at <= Date.now()) {
        t.fired = true;
        fired++;
        try { t.fn(); } catch (e) { recordError('setTimeout', e); }
      }
    }
    const busy = fired > 0 || inFlight > 0 || requests.length !== lastActivity;
    if (busy) { idle = 0; lastActivity = requests.length; } else { idle++; }
  }
  if (inFlight > 0) recordError('settle', new Error('a request never settled (in-flight=' + inFlight + ')'));
  process.removeListener('unhandledRejection', onUnhandled);

  const el = (id) => document.getElementById(id);
  const statusText = el('status-text');
  const statusDot = el('status-dot');
  const banner = el('banner');

  const result = {
    state,
    evalError,
    initError,
    errors,
    unhandled: unhandled.length,
    requests: [...new Set(requests)],
    statusText: statusText ? statusText.textContent : '(missing #status-text)',
    statusDotClass: statusDot ? statusDot.className : '(missing #status-dot)',
    bannerHidden: banner ? banner.classList.contains('hidden') : null,
    sessions: el('session-list') ? el('session-list').children.length : -1,
    sessionText: el('session-list') ? el('session-list').textContent.replace(/\s+/g, ' ').trim() : '',
    metrics: {
      context: el('metric-context-v') && el('metric-context-v').textContent,
      turns: el('metric-turns-v') && el('metric-turns-v').textContent,
      elapsed: el('metric-elapsed-v') && el('metric-elapsed-v').textContent,
      cache: el('metric-cache-v') && el('metric-cache-v').textContent,
      contextTitle: el('metric-context') && el('metric-context').title,
    },
    dialogOpen: el('overlay') ? !el('overlay').classList.contains('hidden') : null,
    navOff: (() => {
      const o = {};
      document.querySelectorAll('.nav-row').forEach((b) => { o[b.dataset.view] = b.classList.contains('off'); });
      return o;
    })(),
    openedSession: requests.some((p) => /^\/api\/sessions\/[^/]+$/.test(p)),
    dialogText: el('dialog') ? el('dialog').textContent.replace(/\s+/g, ' ').trim().slice(0, 120) : '',
  };
  return result;
}

/* ------------------------------------------------------------------ *
 * 5) Assertions + reporting.
 * ------------------------------------------------------------------ */

const results = [];
const perState = [];

function check(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? '  ' + detail : ''));
}

function reportBoot(r) {
  if (r.evalError) {
    console.log('EXCEPTION while evaluating web/app.js:');
    console.log(r.evalError.stack || String(r.evalError));
    return false;
  }
  if (r.initError) {
    console.log('EXCEPTION inside the init/DOMContentLoaded path:');
    console.log(r.initError.stack || String(r.initError));
  }
  for (const e of r.errors) {
    console.log('EXCEPTION (' + e.where + '): ' + e.message);
    if (e.stack) console.log(e.stack);
  }
  return true;
}

const argv = process.argv.slice(2);
const argOf = (name) => {
  const i = argv.indexOf(name);
  return i >= 0 ? argv[i + 1] : null;
};
const base = argOf('--base') || (argv[0] && argv[0].startsWith('http') ? argv[0] : null);
const appOverride = argOf('--app');
const htmlOverride = argOf('--html');
const wantLiveApi = argv.includes('--live-api');
const wantAll = argv.includes('--all');
const single = argOf('--state');
const states = wantAll ? ['normal', 'noworkspace', 'noprovider'] : [single || 'normal'];

console.log('ui_boot_probe: ' + (base ? 'live server ' + base : 'canned fixtures') +
  ', states=' + states.join(','));

for (const state of states) {
  console.log('\n-- state: ' + state + ' --');
  let r;
  try {
    r = await bootState(state, { base, app: appOverride, html: htmlOverride, liveApi: wantLiveApi });
  } catch (e) {
    console.log('HARNESS ERROR: ' + (e.stack || e));
    check(state + ': harness ran', false);
    continue;
  }
  const clean = reportBoot(r);
  perState.push(r);
  check(state + ': init completes without throwing',
    clean && !r.evalError && !r.initError && r.errors.length === 0,
    clean ? '' : 'see stack above');
  check(state + ': status element left the 连接中 placeholder',
    r.statusText !== '连接中…' && r.statusText !== '连接中', 'status=' + JSON.stringify(r.statusText));

  if (state === 'normal') {
    check(state + ': status shows the connected state', /^已连接/.test(r.statusText), 'status=' + JSON.stringify(r.statusText));
    check(state + ': status dot is ok', /\bok\b/.test(r.statusDotClass), 'class=' + JSON.stringify(r.statusDotClass));
    check(state + ': server banner stays hidden', r.bannerHidden === true);
    check(state + ': session list rendered instead of the placeholder',
      !/请先选择工作区/.test(r.sessionText) && /条|暂无会话/.test(r.sessionText),
      'text=' + JSON.stringify(r.sessionText));
    if (r.openedSession) {
      check(state + ': metrics rendered the turn count',
        /^[1-9][0-9]*$/.test(String(r.metrics.turns)), 'turns=' + JSON.stringify(r.metrics.turns));
    }
    check(state + ': metrics resolved the context window from providers',
      /128000/.test(String(r.metrics.contextTitle)),
      'title=' + JSON.stringify(r.metrics.contextTitle));
  }
  if (state === 'noworkspace') {
    check(state + ': first-run workspace prompt opens', r.dialogOpen === true, 'dialog=' + JSON.stringify(r.dialogText));
    check(state + ': session list explains the missing workspace',
      /请先选择工作区/.test(r.sessionText), 'text=' + JSON.stringify(r.sessionText));
  }
  if (state === 'noprovider') {
    // 没有 provider.yaml 时不再有「挡住整个应用」的对话框：对话那几项置灰，
    // 终端和设置照常，配置入口在设置页里。
    check(state + ': no dialog blocks the app', r.dialogOpen === false,
      'dialog=' + JSON.stringify(r.dialogText));
    check(state + ': the chat view is greyed out', r.navOff && r.navOff.chat === true,
      'nav=' + JSON.stringify(r.navOff));
    check(state + ': the terminal stays enabled', r.navOff && r.navOff.shell === false,
      'nav=' + JSON.stringify(r.navOff));
    check(state + ': status reports the unconfigured state', r.statusText === '未配置',
      'status=' + JSON.stringify(r.statusText));
  }
  console.log('  api paths requested: ' + r.requests.join(' '));
  console.log('  session-list text: ' + JSON.stringify(r.sessionText));
  console.log('  metrics: ' + JSON.stringify(r.metrics) + ' dialog=' + JSON.stringify(r.dialogText));
}

const failed = results.filter((r) => !r.ok);
console.log('\n' + (results.length - failed.length) + '/' + results.length + ' boot checks passed');
if (failed.length) {
  console.log('FAILED: ' + failed.map((f) => f.name).join('; '));
  process.exit(1);
}
// ASCII markers so a PowerShell caller does not have to match Chinese text.
for (const r of perState) {
  console.log('UI_BOOT_OK state=' + r.state + ' status=' +
    (r.statusText === '已连接' || r.statusText.startsWith('已连接') ? 'connected' : r.statusText === '未配置' ? 'needs-setup' : 'other') +
    ' api_paths=' + r.requests.length);
}
console.log('ui_boot_probe: OK');
process.exit(0);
