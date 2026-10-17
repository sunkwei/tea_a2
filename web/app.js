/* TEA AGENT — 离线单页 UI（原生 ES2020，经典脚本；无构建、无外部请求） */
(function () {
  'use strict';

  /* ---------- 1) 状态与本地持久化 ---------- */
  var LS_KEY = 'tea_agent_ui_v1';
  var API_BASE = new URL('.', document.baseURI).href; // 相对页面目录，兼容任意挂载前缀
  var VIEWS = ['chat', 'files', 'memory', 'logs', 'settings'];
  var VIEW_LABEL = { chat: '对话', files: '文件', memory: '记忆', logs: '日志', settings: '设置' };

  var state = {
    view: 'chat', health: null, providers: null, tools: null, indexPath: '',
    workspaces: [], workspace: null, sessions: [], session: null, messages: [],
    model: null, models: [], modelMenu: false,
    live: null, liveNode: null, streaming: false, abort: null, stopTimer: null,
    ended: false, streamError: null, expanded: {}, stickBottom: true,
    files: { dir: '', entries: [], path: null, content: '', original: '', truncated: false, readOnly: false },
    memory: [], logsTimer: null, logsAutoScroll: true
  };

  function saveLS() {
    try {
      localStorage.setItem(LS_KEY, JSON.stringify({
        view: state.view, workspace_id: state.workspace ? state.workspace.id : null,
        session_id: state.session ? state.session.id : null, model: state.model,
        logs_autoscroll: state.logsAutoScroll
      }));
    } catch (e) { /* 隐私模式下忽略 */ }
  }
  function loadLS() {
    try { return JSON.parse(localStorage.getItem(LS_KEY) || '{}') || {}; } catch (e) { return {}; }
  }

  /* ---------- 2) 通用小工具 ---------- */
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function btn(cls, text, fn) { var b = el('button', cls, text); b.type = 'button'; b.onclick = fn; return b; }
  var ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return ESC_MAP[c]; }); }
  function apiUrl(p) { return API_BASE + String(p).replace(/^\/+/, ''); }
  function api(p, opts) {
    opts = opts || {};
    var init = { method: opts.method || 'GET' };
    if (opts.body !== undefined) { init.headers = { 'Content-Type': 'application/json' }; init.body = JSON.stringify(opts.body); }
    return fetch(apiUrl(p), init).then(function (r) {
      return r.text().then(function (t) {
        var data = null;
        try { data = t ? JSON.parse(t) : null; } catch (e) { data = null; }
        if (!r.ok) {
          var e2 = new Error((data && data.error && (data.error.message || data.error.code)) || ('HTTP ' + r.status));
          e2.status = r.status; throw e2;
        }
        return data;
      });
    });
  }
  function fmtBytes(n) {
    if (n == null) return '';
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    return (n / 1048576).toFixed(1) + ' MB';
  }
  function fmtMs(ms) { return ms == null ? '' : (ms < 1000 ? ms + ' ms' : (ms / 1000).toFixed(1) + ' s'); }
  function fmtTime(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    var p = function (x) { return (x < 10 ? '0' : '') + x; };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  var toastTimer = null;
  function toast(msg) {
    var t = $('toast');
    t.textContent = msg; t.classList.remove('hidden');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.add('hidden'); }, 2200);
  }
  function legacyCopy(text) {
    try {
      var ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(ta); return ok;
    } catch (e) { return false; }
  }
  function copyText(text, b) {
    var done = function (ok) {
      b.textContent = ok ? '已复制' : '复制失败';
      setTimeout(function () { b.textContent = '复制'; }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(legacyCopy(text)); });
    } else done(legacyCopy(text));
  }

  /* ---------- 3) Markdown 渲染（先转义再解析，绝不注入原始 HTML） ---------- */
  function inlineMd(t) {
    return t.split(/(`[^`]+`)/g).map(function (p) {
      if (p.length > 1 && p.charAt(0) === '`' && p.charAt(p.length - 1) === '`') {
        return '<code class="md-inline">' + p.slice(1, -1) + '</code>';
      }
      var s = p.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, function (m, label, href) {
        var safe = /^(https?:|mailto:|#|\/|\.)/i.test(href) ? href : '#';
        return '<a href="' + safe + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
      });
      s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
        .replace(/(^|[\s(（])\*([^*\n]+)\*/g, '$1<em>$2</em>')
        .replace(/(^|[\s(（])_([^_\n]+)_/g, '$1<em>$2</em>')
        .replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
      return s;
    }).join('');
  }
  function codeBlock(lang, lines) {
    return '<div class="code-block"><div class="code-banner"><span class="lang">' +
      esc((lang || '代码').toLowerCase()) + '</span><button class="code-copy" type="button">复制</button></div>' +
      '<pre><code>' + lines.join('\n') + '</code></pre></div>';
  }
  function renderMarkdown(src) {
    var lines = esc(src == null ? '' : src).split('\n');
    var out = [], i = 0;
    var isUl = function (s) { return /^\s*[-*+]\s+/.test(s); };
    var isOl = function (s) { return /^\s*\d+[.)]\s+/.test(s); };
    var isQ = function (s) { return /^\s*>\s?/.test(s); };
    var isHr = function (s) { return /^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(s); };
    while (i < lines.length) {
      var line = lines[i], m;
      // 围栏代码块
      if ((m = line.match(/^\s*(```+|~~~+)\s*([^\s`]*)\s*$/))) {
        var marker = m[1].charAt(0), buf = [];
        i++;
        while (i < lines.length) {
          var f2 = lines[i].match(/^\s*(```+|~~~+)\s*$/);
          if (f2 && f2[1].charAt(0) === marker) { i++; break; }
          buf.push(lines[i]); i++;
        }
        out.push(codeBlock(m[2], buf));
        continue;
      }
      if (!line.trim()) { i++; continue; }
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {                       // 标题
        out.push('<h' + m[1].length + '>' + inlineMd(m[2].trim()) + '</h' + m[1].length + '>'); i++; continue;
      }
      if (isHr(line)) { out.push('<hr>'); i++; continue; }               // 分隔线
      if (isQ(line)) {                                                   // 引用
        var q = [];
        while (i < lines.length && isQ(lines[i])) { q.push(lines[i].replace(/^\s*>\s?/, '')); i++; }
        out.push('<blockquote>' + inlineMd(q.join('<br>')) + '</blockquote>'); continue;
      }
      if (isUl(line)) {                                                  // 无序列表
        var ul = [];
        while (i < lines.length && isUl(lines[i])) { ul.push('<li>' + inlineMd(lines[i].replace(/^\s*[-*+]\s+/, '')) + '</li>'); i++; }
        out.push('<ul>' + ul.join('') + '</ul>'); continue;
      }
      if (isOl(line)) {                                                  // 有序列表
        var ol = [], first = parseInt(line.match(/^\s*(\d+)/)[1], 10);
        while (i < lines.length && isOl(lines[i])) { ol.push('<li>' + inlineMd(lines[i].replace(/^\s*\d+[.)]\s+/, '')) + '</li>'); i++; }
        out.push('<ol' + (first > 1 ? ' start="' + first + '"' : '') + '>' + ol.join('') + '</ol>'); continue;
      }
      var p = [];                                                        // 段落（保留换行）
      while (i < lines.length && lines[i].trim() && !/^\s*(```+|~~~+)/.test(lines[i]) &&
        !/^(#{1,6})\s+/.test(lines[i]) && !isQ(lines[i]) && !isUl(lines[i]) && !isOl(lines[i]) && !isHr(lines[i])) {
        p.push(lines[i]); i++;
      }
      out.push('<p>' + inlineMd(p.join('<br>')) + '</p>');
    }
    return out.join('');
  }

  /* ---------- 4) 对话渲染 ---------- */
  var TOOL_META = {
    toolkit_exec: { title: '执行命令', icon: 'term' },
    toolkit_file: { title: '文件操作', icon: 'file' }
  };
  var ICONS = {
    term: '<svg viewBox="0 0 16 16"><path d="M2.6 3.4h10.8v9.2H2.6z"/><path d="M5 7l1.6 1.6L5 10.2M8.4 10.4h3"/></svg>',
    file: '<svg viewBox="0 0 16 16"><path d="M4 2.4h5l3 3v8.2H4z"/><path d="M9 2.4v3h3"/></svg>',
    gear: '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="2"/><path d="M8 2.4v2M8 11.6v2M2.4 8h2M11.6 8h2"/></svg>',
    spark: '<svg viewBox="0 0 16 16"><path d="M8 2.6l1.3 3.2 3.2 1.2-3.2 1.3L8 11.5 6.7 8.3 3.5 7l3.2-1.2z"/></svg>',
    check: '<svg viewBox="0 0 16 16"><path d="M3.4 8.4l3 3 6.2-6.6"/></svg>',
    cross: '<svg viewBox="0 0 16 16"><path d="M4.2 4.2l7.6 7.6M11.8 4.2l-7.6 7.6"/></svg>'
  };
  function toolSummary(args) {
    args = args || {};
    if (typeof args === 'string') return args;
    if (args.command) return String(args.command);
    if (args.path) return [args.op || args.action || args.operation || '', args.path].join(' ').trim();
    if (!Object.keys(args).length) return '';
    try { return JSON.stringify(args).slice(0, 160); } catch (e) { return ''; }
  }
  function toolIconSvg(name, status) {
    if (status === 'running') return '<span class="spinner"></span>';
    if (status === 'ok') return ICONS.check;                       // 成功 → 对勾
    if (status === 'fail') return ICONS.cross;                     // 失败 → 叉号
    return ICONS[(TOOL_META[name] || {}).icon || 'gear'];
  }
  function toolRowView(tc, result, key, running) {
    var meta = TOOL_META[tc.name] || { title: tc.name || '工具', icon: 'gear' };
    var status = running ? 'running' : (result ? (result.ok === false ? 'fail' : 'ok') : 'idle');
    var row = el('div', 'tool-row');
    row.dataset.key = key;
    if ((key in state.expanded) ? state.expanded[key] : (status === 'running')) row.classList.add('open');

    var line = el('div', 'tool-line');
    var ic = el('span', 'tool-icon' + (status === 'ok' ? ' ok' : status === 'fail' ? ' fail' : ''));
    ic.innerHTML = toolIconSvg(tc.name, status);
    line.appendChild(ic);
    line.appendChild(el('span', 'tool-title', meta.title));
    line.appendChild(el('span', 'dot'));
    line.appendChild(el('span', 'tool-summary', toolSummary(tc.arguments)));
    if (result && result.elapsed_ms != null) line.appendChild(el('span', 'tool-time', fmtMs(result.elapsed_ms)));
    else if (status === 'running') line.appendChild(el('span', 'tool-time', '运行中'));
    line.appendChild(el('span', 'chev', '›'));
    row.appendChild(line);

    var panel = el('div', 'tool-panel'), kv1 = el('div', 'kv'), kv2 = el('div', 'kv');
    var input = '';
    try { input = JSON.stringify(tc.arguments || {}, null, 2); } catch (e) { input = String(tc.arguments); }
    kv1.appendChild(el('div', 'k', '输入'));
    kv1.appendChild(el('div', 'v', input));
    var outText = result ? (result.output == null ? '' : String(result.output)) : (status === 'running' ? '等待输出…' : '');
    if (status === 'fail' && result && result.output == null) outText = '';
    if (result && result.truncated) outText += '\n…（输出已截断）';
    kv2.appendChild(el('div', 'k', '输出'));
    kv2.appendChild(el('div', 'v', outText));
    panel.appendChild(kv1); panel.appendChild(kv2);
    row.appendChild(panel);
    return row;
  }
  function reasoningRow(text, key, expand) {
    var row = el('div', 'tool-row reasoning');
    row.dataset.key = key;
    if ((key in state.expanded) ? state.expanded[key] : expand) row.classList.add('open');
    var line = el('div', 'tool-line');
    var ic = el('span', 'tool-icon');
    ic.innerHTML = ICONS.spark;
    line.appendChild(ic);
    line.appendChild(el('span', 'tool-title', '深度思考'));
    line.appendChild(el('span', 'dot'));
    line.appendChild(el('span', 'tool-summary pulse', text.length + ' 字'));
    line.appendChild(el('span', 'chev', '›'));
    row.appendChild(line);
    row.appendChild(el('div', 'reason-panel', text));
    return row;
  }
  function assistantNode(msg, outputs, keyPrefix, isLive) {
    var wrap = el('div', 'msg assistant' + (isLive ? ' streaming' : ''));
    if (isLive) wrap.dataset.live = '1';
    if (msg.reasoning) wrap.appendChild(reasoningRow(msg.reasoning, keyPrefix + ':reason', isLive && !msg.content));
    var blocks = msg.blocks;
    if (!blocks) {
      blocks = [];
      if (msg.content) blocks.push({ type: 'md', text: msg.content });
      (msg.tool_calls || []).forEach(function (tc) { blocks.push({ type: 'tool', tc: tc }); });
    }
    blocks.forEach(function (b, idx) {
      if (b.type === 'md') {
        if (!b.text) return;
        var md = el('div', 'md');
        md.innerHTML = renderMarkdown(b.text);
        if (isLive && !state.ended && idx === blocks.length - 1) md.appendChild(el('span', 'stream-caret'));
        wrap.appendChild(md);
      } else {
        var tc = b.tc;
        wrap.appendChild(toolRowView(tc, outputs[tc.id] || null, keyPrefix + ':t:' + tc.id, tc.status === 'running' && !outputs[tc.id]));
      }
    });
    return wrap;
  }
  function renderChat() {
    var box = $('messages');
    clear(box);
    state.liveNode = null;
    if (!state.workspaces.length) return;                       // 空状态由添加工作区弹窗接管
    var outputs = {};
    state.messages.forEach(function (m) {
      if (m.role === 'tool' && m.tool_call_id) {
        outputs[m.tool_call_id] = { output: m.content, ok: m.ok !== false, elapsed_ms: m.elapsed_ms };
      }
    });
    state.messages.forEach(function (m, i) {
      if (m.role === 'tool') return;                            // 工具消息只作为输出附加
      if (m.role === 'user') box.appendChild(el('div', 'msg user', m.content));
      else if (m.role === 'system') box.appendChild(el('div', 'msg system', m.content));
      else box.appendChild(assistantNode(m, outputs, 'm' + i, false));
    });
    if (state.live) {
      state.liveNode = assistantNode(state.live, state.live.outputs, 'live', true);
      box.appendChild(state.liveNode);
    }
    if (state.streamError) box.appendChild(el('div', 'msg error', state.streamError));
    else if (!state.messages.length && !state.live) {
      var empty = el('div', 'chat-empty');
      empty.appendChild(el('div', 'big', state.session ? '还没有消息' : '开始一个新会话'));
      empty.appendChild(el('div', null, state.session ? '在下方输入内容开始对话。' : '直接输入内容，发送时会自动创建会话。'));
      box.appendChild(empty);
    }
  }
  function renderLive() {
    var box = $('messages');
    var hint = box.querySelector('.chat-empty');
    if (hint) hint.remove();
    if (!state.live) {
      if (state.liveNode && state.liveNode.parentNode) state.liveNode.parentNode.removeChild(state.liveNode);
      state.liveNode = null; return;
    }
    var node = assistantNode(state.live, state.live.outputs, 'live', true);
    if (state.liveNode && state.liveNode.parentNode === box) box.replaceChild(node, state.liveNode);
    else box.appendChild(node);
    state.liveNode = node;
    scrollMaybe();
  }
  function nearBottom() { var s = $('chat-scroll'); return s.scrollHeight - s.scrollTop - s.clientHeight < 80; }
  function scrollMaybe(force) { var s = $('chat-scroll'); if (force || state.stickBottom) s.scrollTop = s.scrollHeight; }

  /* ---------- 5) 流式对话（fetch + ReadableStream 解析 SSE） ---------- */
  function liveBlockForTool(id) {
    var found = null;
    state.live.blocks.forEach(function (b) { if (b.type === 'tool' && b.tc.id === id) found = b; });
    return found;
  }
  function handleEvent(ev) {
    if (ev.type === 'ping') return;                              // 心跳：保持连接存活即可
    if (!state.live) return;
    if (ev.type === 'start') {
      if (ev.session_id && (!state.session || state.session.id !== ev.session_id)) {
        state.session = { id: ev.session_id, title: '新会话' };
        saveLS();
      }
      return;
    }
    if (ev.type === 'reasoning_delta') { state.live.reasoning += ev.text || ''; renderLive(); scrollMaybe(); return; }
    if (ev.type === 'content_delta') {
      var last = state.live.blocks[state.live.blocks.length - 1];
      if (!last || last.type !== 'md') { last = { type: 'md', text: '' }; state.live.blocks.push(last); }
      last.text += ev.text || '';
      renderLive(); scrollMaybe(); return;
    }
    if (ev.type === 'tool_call' || ev.type === 'tool_start') {
      var block = liveBlockForTool(ev.id);
      if (block) block.tc.status = 'running';
      else state.live.blocks.push({ type: 'tool', tc: { id: ev.id, name: ev.name, arguments: ev.arguments, status: 'running' } });
      renderLive(); scrollMaybe(); return;
    }
    if (ev.type === 'tool_result') {
      state.live.outputs[ev.id] = { output: ev.output, ok: ev.ok !== false, elapsed_ms: ev.elapsed_ms, truncated: !!ev.truncated };
      var b2 = liveBlockForTool(ev.id);
      if (b2) b2.tc.status = ev.ok === false ? 'fail' : 'ok';
      renderLive(); scrollMaybe(); return;
    }
    if (ev.type === 'iteration_end') return;                     // 仅统计，不渲染
    if (ev.type === 'error') {
      state.ended = true;
      state.streamError = '出错了：' + (ev.message || '未知错误') + (ev.detail ? '\n' + ev.detail : '');
      renderLive(); scrollMaybe(); return;
    }
    if (ev.type === 'done' || ev.type === 'stopped') state.ended = true;
  }
  function parseSseChunk(raw) {
    raw.split('\n').forEach(function (line) {
      if (line.charAt(0) === '\r') line = line.slice(1);
      if (line.indexOf('data:') !== 0) return;
      var payload = line.slice(5);
      if (payload.charAt(0) === ' ') payload = payload.slice(1);
      if (!payload || payload === '[DONE]') return;
      var ev = null;
      try { ev = JSON.parse(payload); } catch (e) { return; }
      if (ev && typeof ev === 'object') handleEvent(ev);
    });
  }
  function setStreaming(on) {
    state.streaming = on;
    updateComposer();
    $('ws-chip').disabled = on;
    $('new-session').disabled = on;
    Array.prototype.forEach.call(document.querySelectorAll('.sess-row'), function (b) { b.disabled = on; });
  }
  function finalizeStream() {
    var sid = state.session ? state.session.id : null;
    state.live = null; state.liveNode = null; state.ended = false;
    if (state.stopTimer) { clearTimeout(state.stopTimer); state.stopTimer = null; }
    state.abort = null;
    setStreaming(false);
    var finish = function () { renderChat(); scrollMaybe(); };
    if (!sid) { finish(); return; }
    // 结束后重新拉取，保证渲染状态与服务器一致
    api('api/sessions/' + encodeURIComponent(sid)).then(function (d) {
      state.session = d.session || state.session;
      state.messages = d.messages || [];
      finish(); loadSessions();
    }, finish);
  }
  function sendMessage() {
    if (state.streaming) return;
    var ta = $('input');
    var text = ta.value.replace(/\s+$/, '');
    if (!text) return;
    if (!state.workspace) { toast('请先选择工作区'); return; }
    ta.value = ''; autoGrow();

    var ready = state.session ? Promise.resolve(state.session)
      : api('api/workspaces/' + encodeURIComponent(state.workspace.id) + '/sessions',
        { method: 'POST', body: { title: text.slice(0, 24) || '新会话' } })
        .then(function (s) { state.session = s; saveLS(); loadSessions(); return s; });

    ready.then(function (s) {
      state.messages.push({ role: 'user', content: text, created_at: new Date().toISOString() });
      state.live = { role: 'assistant', reasoning: '', blocks: [], outputs: {} };
      state.ended = false; state.streamError = null; state.stickBottom = true;
      renderChat(); scrollMaybe(true); setStreaming(true);
      var ctrl = new AbortController();
      state.abort = ctrl;
      return fetch(apiUrl('api/chat'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          workspace_id: state.workspace.id, session_id: s.id, message: text,
          provider: state.model ? state.model.provider : undefined,
          model: state.model ? state.model.model : undefined
        }),
        signal: ctrl.signal
      }).then(function (res) {
        if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);
        var reader = res.body.getReader(), dec = new TextDecoder('utf-8'), buf = '';
        var pump = function () {
          return reader.read().then(function (r) {
            if (r.done) { if (buf.indexOf('data:') === 0) parseSseChunk(buf); return; }
            buf += dec.decode(r.value, { stream: true });
            buf = buf.replace(/\r\n/g, '\n');
            var idx;
            while ((idx = buf.indexOf('\n\n')) >= 0) { parseSseChunk(buf.slice(0, idx)); buf = buf.slice(idx + 2); }
            return pump();
          });
        };
        return pump();
      });
    }).then(finalizeStream, function (err) {
      if (!err || err.name !== 'AbortError') {
        state.streamError = '请求失败：' + (err && err.message ? err.message : String(err));
        loadHealth().catch(function () {});
      }
      finalizeStream();                                          // 仍然重拉会话，保持与服务端一致
    });
  }
  function stopStream() {
    var sid = state.session ? state.session.id : null;
    if (sid) api('api/chat/stop', { method: 'POST', body: { session_id: sid } }).catch(function () {});
    state.stopTimer = setTimeout(function () { if (state.streaming && state.abort) state.abort.abort(); }, 3000);
  }
  function updateComposer() {
    var b = $('send');
    b.classList.toggle('stop', state.streaming);
    b.title = state.streaming ? '停止生成' : '发送';
    b.innerHTML = state.streaming
      ? '<svg viewBox="0 0 16 16"><rect x="4.5" y="4.5" width="7" height="7" rx="1.2" fill="currentColor" stroke="none"/></svg>'
      : '<svg viewBox="0 0 16 16"><path d="M8 12.6V3.6M4.2 7.4L8 3.6l3.8 3.8"/></svg>';
  }
  function autoGrow() {
    var ta = $('input');
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 168) + 'px';
    ta.style.overflowY = ta.scrollHeight > 168 ? 'auto' : 'hidden';
  }

  /* ---------- 6) 顶栏 / 状态 / 模型与工作区选择 ---------- */
  function setCrumbs(items) {
    var c = $('crumbs');
    clear(c);
    items.forEach(function (it, i) {
      if (i) c.appendChild(el('span', 'sep', '/'));
      var last = i === items.length - 1;
      if (last) { c.appendChild(el('span', 'crumb cur', it.label)); return; }
      c.appendChild(btn('crumb', it.label, it.onClick || null));
    });
  }
  function markOnline() {
    $('status-dot').className = 'status-dot ok';
    $('status-text').textContent = '已连接' + (state.health && state.health.version ? ' v' + state.health.version : '');
    $('banner').classList.add('hidden');
  }
  function markOffline(err) {
    $('status-dot').className = 'status-dot bad';
    $('status-text').textContent = '未连接';
    $('banner-text').textContent = '无法连接到本地服务：' + (err && err.message ? err.message : '未知错误');
    $('banner').classList.remove('hidden');
  }
  function closeDropdowns() {
    $('model-dropdown').classList.add('hidden');
    $('ws-dropdown').classList.add('hidden');
    $('model-chip').setAttribute('aria-expanded', 'false');
    $('ws-chip').setAttribute('aria-expanded', 'false');
  }
  function buildModels() {
    state.models = [];
    ((state.providers && state.providers.providers) || []).forEach(function (p) {
      (p.models || []).forEach(function (m) { state.models.push({ provider: p.name, model: m }); });
    });
    if (!state.model && state.providers && state.providers.active) {
      state.model = { provider: state.providers.active.provider, model: state.providers.active.model };
    }
    if (!state.model && state.models.length) state.model = state.models[0];
  }
  function renderModelChip() {
    $('model-chip-text').textContent = state.model ? state.model.model : '模型';
    $('model-chip').title = state.model ? (state.model.provider + ' · ' + state.model.model) : '选择模型';
  }
  function renderModelMenu() {
    var dd = $('model-dropdown');
    clear(dd);
    var ps = (state.providers && state.providers.providers) || [];
    if (!ps.length) { dd.appendChild(el('div', 'dd-group', '无可用模型')); return; }
    ps.forEach(function (p) {
      dd.appendChild(el('div', 'dd-group', p.name + (p.has_key ? '' : ' · 未配置密钥')));
      (p.models || []).forEach(function (m) {
        var active = state.model && state.model.provider === p.name && state.model.model === m;
        var it = btn('dd-item' + (active ? ' active' : ''), m, function () {
          state.model = { provider: p.name, model: m };
          saveLS(); renderModelChip(); renderModelMenu(); closeDropdowns(); toast('已切换模型：' + m);
        });
        if (active) it.appendChild(el('span', 'check', '✓'));
        dd.appendChild(it);
      });
    });
  }
  function renderWorkspaceChip() {
    $('ws-chip-text').textContent = state.workspace ? state.workspace.name : '选择工作区';
    $('ws-chip').title = state.workspace ? state.workspace.path : '';
  }
  function renderWorkspaceMenu() {
    var dd = $('ws-dropdown');
    clear(dd);
    state.workspaces.forEach(function (w) {
      var it = btn('dd-item' + (state.workspace && state.workspace.id === w.id ? ' active' : ''), w.name, function () {
        closeDropdowns();
        if (!state.workspace || state.workspace.id !== w.id) selectWorkspace(w.id);
      });
      it.appendChild(el('span', 'sub', w.exists === false ? '路径不存在' : (w.sessions || 0) + ' 会话'));
      dd.appendChild(it);
    });
    dd.appendChild(el('div', 'dd-sep'));
    dd.appendChild(btn('dd-item', '＋ 添加工作区…', function () { closeDropdowns(); openAddWorkspace(false); }));
  }
  function renderSessions() {
    var box = $('session-list');
    clear(box);
    if (!state.workspace) { box.appendChild(el('div', 'empty-note', '请先选择工作区')); return; }
    if (!state.sessions.length) { box.appendChild(el('div', 'empty-note', '暂无会话，点击 ＋ 新建')); return; }
    state.sessions.forEach(function (s) {
      var b = btn('sess-row' + (state.session && state.session.id === s.id ? ' active' : ''), s.title || s.id,
        function () { if (!state.streaming) openSession(s.id); });
      b.title = (s.title || '') + ' · ' + (s.message_count || 0) + ' 条消息 · ' + fmtTime(s.updated_at || s.created_at);
      b.disabled = state.streaming;
      box.appendChild(b);
    });
  }

  /* ---------- 7) 数据加载 ---------- */
  function loadHealth() {
    return api('api/health').then(function (h) { state.health = h; markOnline(); return h; },
      function (e) { markOffline(e); throw e; });
  }
  function loadProviders() {
    return api('api/providers').then(function (p) {
      state.providers = p;
      var saved = state.model;
      buildModels();
      if (saved && state.models.some(function (m) { return m.provider === saved.provider && m.model === saved.model; })) state.model = saved;
      renderModelChip(); renderModelMenu(); renderSettings();
    }, function () {});
  }
  function loadTools() {
    return api('api/tools').then(function (t) { state.tools = t; renderSettings(); }, function () {});
  }
  function loadWorkspaces() {
    return api('api/workspaces').then(function (d) {
      state.workspaces = d.workspaces || [];
      state.indexPath = d.index_path || '';
      renderWorkspaceMenu();
      if (!state.workspaces.length) {
        state.workspace = null; state.sessions = []; state.session = null; state.messages = [];
        renderWorkspaceChip(); renderSessions(); renderChat(); renderSettings();
        openAddWorkspace(true);
        return;
      }
      var saved = loadLS().workspace_id;
      var w = state.workspaces.filter(function (x) { return x.id === saved; })[0] || state.workspaces[0];
      return selectWorkspace(w.id, true);
    });
  }
  function selectWorkspace(id, silent) {
    var w = state.workspaces.filter(function (x) { return x.id === id; })[0];
    if (!w) return Promise.resolve();
    var changed = !state.workspace || state.workspace.id !== w.id;
    state.workspace = w;
    if (changed) {
      state.session = null; state.messages = []; state.live = null; state.streamError = null;
      state.files = { dir: '', entries: [], path: null, content: '', original: '', truncated: false, readOnly: false };
    }
    renderWorkspaceChip(); renderChat(); saveLS();
    return loadSessions().then(function () {
      if (state.view === 'files') loadTree(state.files.dir);
      if (state.view === 'memory') loadMemory();
      if (state.view === 'logs') loadLogs();
      if (state.view === 'settings') renderSettings();
      if (!silent) toast('已切换工作区：' + w.name);
    });
  }
  function loadSessions() {
    if (!state.workspace) return Promise.resolve();
    return api('api/workspaces/' + encodeURIComponent(state.workspace.id) + '/sessions').then(function (d) {
      state.sessions = (d.sessions || []).slice().sort(function (a, b) {
        return String(b.updated_at || b.created_at || '').localeCompare(String(a.updated_at || a.created_at || ''));
      });
      if (state.session) {
        var still = state.sessions.filter(function (s) { return s.id === state.session.id; })[0];
        if (still) state.session = still;
      }
      renderSessions();
      if (!state.session && state.sessions.length) return openSession(state.sessions[0].id);
      if (!state.session) renderChat();
    }, function () { renderSessions(); });
  }
  function openSession(sid) {
    return api('api/sessions/' + encodeURIComponent(sid)).then(function (d) {
      state.session = d.session || { id: sid };
      state.messages = d.messages || [];
      state.live = null; state.streamError = null;
      saveLS(); renderSessions(); renderChat(); scrollMaybe(true);
    }, function (e) { toast('加载会话失败：' + e.message); });
  }
  function newSessionDraft() {
    state.session = null; state.messages = []; state.live = null; state.streamError = null;
    saveLS(); renderSessions(); renderChat(); showView('chat'); $('input').focus();
  }
  function openAddWorkspace(isEmpty) {
    var body = el('div');
    body.appendChild(el('div', 'desc', isEmpty ? '还没有任何工作区。请输入一个本地目录路径来创建（例如 C:\\work\\my_project）。'
      : '输入本地目录路径以添加到工作区索引。'));
    var pathInput = el('input', 'ta'), nameInput = el('input', 'ta');
    pathInput.type = 'text'; pathInput.placeholder = 'C:\\Users\\me\\project';
    nameInput.type = 'text'; nameInput.placeholder = 'project';
    [['路径', pathInput], ['名称（可选）', nameInput]].forEach(function (pair) {
      var f = el('div', 'field');
      f.appendChild(el('label', null, pair[0]));
      f.appendChild(pair[1]);
      body.appendChild(f);
    });
    openDialog('添加工作区', body, [
      { label: '取消', onClick: closeDialog },
      {
        label: '添加', primary: true, onClick: function () {
          var p = pathInput.value.trim();
          if (!p) { toast('请输入路径'); return; }
          var nm = nameInput.value.trim() || p.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || 'workspace';
          closeDialog();
          api('api/workspaces', { method: 'POST', body: { path: p, name: nm } }).then(function () {
            toast('已添加工作区：' + nm);
            return loadWorkspaces();
          }, function (e) { toast('添加失败：' + e.message); });
        }
      }
    ]);
    setTimeout(function () { pathInput.focus(); }, 0);
  }
  function openDialog(title, bodyNode, actions) {
    var d = $('dialog');
    clear(d);
    d.appendChild(el('h3', null, title));
    d.appendChild(bodyNode);
    var bar = el('div', 'actions');
    (actions || []).forEach(function (a) { bar.appendChild(btn('btn' + (a.primary ? ' primary' : ''), a.label, a.onClick)); });
    d.appendChild(bar);
    $('overlay').classList.remove('hidden');
  }
  function closeDialog() { $('overlay').classList.add('hidden'); clear($('dialog')); }

  /* ---------- 8) 视图切换 ---------- */
  function showView(name) {
    state.view = name;
    saveLS();
    VIEWS.forEach(function (v) { $('view-' + v).classList.toggle('hidden', v !== name); });
    Array.prototype.forEach.call(document.querySelectorAll('.nav-row'), function (b) { b.classList.toggle('active', b.dataset.view === name); });
    renderCrumbs();
    if (name === 'chat') { scrollMaybe(); setTimeout(autoGrow, 0); }
    if (name === 'files') loadTree(state.files.dir);
    if (name === 'memory') loadMemory();
    if (name === 'logs') loadLogs();
    if (name === 'settings') renderSettings();
  }
  function renderCrumbs() {
    var ws = state.workspace ? state.workspace.name : '无工作区';
    if (state.view === 'chat') setCrumbs([{ label: ws }, { label: state.session ? (state.session.title || state.session.id) : '新会话' }]);
    else setCrumbs([{ label: ws }, { label: VIEW_LABEL[state.view] || state.view }]);
  }

  /* ---------- 9) 文件视图 ---------- */
  function loadTree(dir) {
    if (!state.workspace) return Promise.resolve();
    state.files.dir = dir || '';
    return api('api/workspaces/' + encodeURIComponent(state.workspace.id) + '/tree?path=' + encodeURIComponent(state.files.dir))
      .then(function (d) { state.files.entries = d.entries || []; renderFiles(); },
        function (e) { toast('读取目录失败：' + e.message); });
  }
  function renderFiles() {
    var f = state.files;
    var c = $('files-crumbs');
    clear(c);
    var parts = f.dir ? f.dir.split('/') : [];
    c.appendChild(btn('crumb', state.workspace ? state.workspace.name : '工作区', function () { loadTree(''); }));
    parts.forEach(function (p, i) {
      c.appendChild(el('span', 'sep', '/'));
      var sub = parts.slice(0, i + 1).join('/');
      if (i === parts.length - 1) c.appendChild(el('span', 'crumb cur', p));
      else c.appendChild(btn('crumb', p, function () { loadTree(sub); }));
    });
    var list = $('files-list');
    clear(list);
    if (f.dir) {
      var up = el('button', 'file-row');
      up.type = 'button';
      up.innerHTML = '<span class="ico2">↑</span><span class="nm">返回上级</span>';
      up.onclick = function () { loadTree(parts.slice(0, -1).join('/')); };
      list.appendChild(up);
    }
    if (!f.entries.length) list.appendChild(el('div', 'empty-note', '空目录'));
    f.entries.forEach(function (en) {
      var b = el('button', 'file-row' + (f.path === en.path ? ' active' : ''));
      b.type = 'button';
      b.innerHTML = '<span class="ico2">' + (en.type === 'dir' ? '▸' : '·') + '</span><span class="nm"></span>' +
        '<span class="sz">' + (en.type === 'dir' ? '目录' : fmtBytes(en.size)) + '</span>';
      b.querySelector('.nm').textContent = en.name;
      b.onclick = function () { if (en.type === 'dir') loadTree(en.path); else openFile(en.path); };
      list.appendChild(b);
    });
    var dirty = f.content !== f.original;
    $('files-note').textContent = f.path ? (f.truncated ? '内容已截断，只读显示' : (dirty ? '有未保存修改' : '未修改')) : '';
    $('files-save').disabled = !f.path || f.readOnly || !dirty;
    $('files-revert').disabled = !f.path || !dirty;
  }
  function openFile(p) {
    if (!state.workspace) return;
    api('api/workspaces/' + encodeURIComponent(state.workspace.id) + '/file?path=' + encodeURIComponent(p)).then(function (d) {
      var f = state.files;
      f.path = d.path || p;
      f.content = d.content == null ? '' : d.content;
      f.original = f.content;
      f.truncated = !!d.truncated;
      f.readOnly = f.truncated;
      $('files-preview-head').textContent = f.path + '  ·  ' + fmtBytes(d.size);
      $('files-editor').classList.toggle('hidden', f.readOnly);
      $('files-pre').classList.toggle('hidden', !f.readOnly);
      $('files-editor').value = f.content;
      $('files-pre').textContent = f.content;
      renderFiles();
    }, function (e) { toast('读取文件失败：' + e.message); });
  }
  function saveFile() {
    var f = state.files;
    if (!f.path || !state.workspace) return;
    api('api/workspaces/' + encodeURIComponent(state.workspace.id) + '/file',
      { method: 'PUT', body: { path: f.path, content: $('files-editor').value } }).then(function (r) {
        f.content = $('files-editor').value;
        f.original = f.content;
        renderFiles();
        toast('已保存' + (r && r.bytes != null ? ' ' + r.bytes + ' 字节' : ''));
      }, function (e) { toast('保存失败：' + e.message); });
  }

  /* ---------- 10) 记忆视图 ---------- */
  function loadMemory() {
    if (!state.workspace) return Promise.resolve();
    return api('api/workspaces/' + encodeURIComponent(state.workspace.id) + '/memory').then(function (d) {
      state.memory = d.notes || [];
      renderMemory();
    }, function (e) { toast('读取记忆失败：' + e.message); });
  }
  function renderMemory() {
    var box = $('memory-list');
    clear(box);
    if (!state.memory.length) { box.appendChild(el('div', 'empty-note', '还没有记忆条目')); return; }
    state.memory.forEach(function (n) {
      var row = el('div', 'note');
      var left = el('div', 'txt');
      left.appendChild(el('div', null, n.text));
      left.appendChild(el('div', 'meta', fmtTime(n.created_at) + ' · ' + n.id));
      row.appendChild(left);
      row.appendChild(btn('del', '删除', function () {
        api('api/workspaces/' + encodeURIComponent(state.workspace.id) + '/memory/' + encodeURIComponent(n.id), { method: 'DELETE' })
          .then(function () { loadMemory(); toast('已删除'); }, function (e) { toast('删除失败：' + e.message); });
      }));
      box.appendChild(row);
    });
  }
  function addMemory() {
    var ta = $('memory-input');
    var text = ta.value.trim();
    if (!text || !state.workspace) return;
    api('api/workspaces/' + encodeURIComponent(state.workspace.id) + '/memory', { method: 'POST', body: { text: text } })
      .then(function () { ta.value = ''; loadMemory(); toast('已添加'); }, function (e) { toast('添加失败：' + e.message); });
  }

  /* ---------- 11) 日志视图 ---------- */
  function loadLogs() {
    if (!state.workspace) return Promise.resolve();
    return api('api/workspaces/' + encodeURIComponent(state.workspace.id) + '/logs?tail=300').then(function (d) {
      var pre = $('logs-pre');
      pre.textContent = (d.lines || []).join('\n');
      if (state.logsAutoScroll) pre.scrollTop = pre.scrollHeight;
    }, function (e) { $('logs-pre').textContent = '读取日志失败：' + e.message; });
  }
  function startLogsTimer() {
    setInterval(function () {
      if (state.view === 'logs' && !document.hidden) loadLogs();   // 仅可见时每 3 秒刷新
    }, 3000);
  }

  /* ---------- 12) 设置视图（只展示是否已配置密钥，绝不回显任何密钥） ---------- */
  function renderSettings() {
    var paths = $('settings-paths');
    clear(paths);
    var row = function (k, v) {
      var d = el('div');
      d.appendChild(el('span', 'k', k));
      d.appendChild(el('span', 'mono', v || '—'));
      paths.appendChild(d);
    };
    var cfg = state.providers || {};
    row('配置文件', cfg.config_path);
    row('工作区索引', state.indexPath);
    if (cfg.active) row('当前模型', cfg.active.provider + ' · ' + cfg.active.model);
    if (cfg.settings) {
      row('最大迭代', String(cfg.settings.max_iterations));
      row('工具输出上限', cfg.settings.max_tool_output + ' 字节');
    }
    var pv = $('settings-providers');
    clear(pv);
    var ps = cfg.providers || [];
    if (!ps.length) pv.appendChild(el('div', 'empty-note', '无提供方信息'));
    ps.forEach(function (p) {
      var card = el('div', 'prov');
      var h = el('h4', null, p.name);
      h.appendChild(el('span', 'badge ' + (p.has_key ? 'yes' : 'no'), p.has_key ? '已配置密钥' : '未配置密钥'));
      card.appendChild(h);
      [['API 地址', p.api_url], ['默认模型', p.default_model], ['可用模型', (p.models || []).join(', ')]].forEach(function (pair) {
        var r = el('div', 'row');
        r.appendChild(el('span', 'k', pair[0]));
        r.appendChild(el('span', 'mono', pair[1] || '—'));
        card.appendChild(r);
      });
      pv.appendChild(card);
    });
    var tl = $('settings-tools');
    clear(tl);
    var ts = (state.tools && state.tools.tools) || [];
    if (!ts.length) tl.appendChild(el('div', 'empty-note', '无工具信息'));
    ts.forEach(function (t) {
      var card = el('div', 'tool-card');
      card.appendChild(el('h4', null, t.name));
      card.appendChild(el('div', 'desc', t.description || ''));
      var pre = el('pre');
      try { pre.textContent = JSON.stringify(t.parameters || {}, null, 2); } catch (e) { pre.textContent = String(t.parameters); }
      card.appendChild(pre);
      tl.appendChild(card);
    });
  }

  /* ---------- 13) 事件绑定与初始化 ---------- */
  function bind() {
    Array.prototype.forEach.call(document.querySelectorAll('.nav-row'), function (b) {
      b.onclick = function () { showView(b.dataset.view); };
    });
    $('chat-scroll').addEventListener('scroll', function () { state.stickBottom = nearBottom(); });
    $('input').addEventListener('input', autoGrow);
    $('input').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMessage(); }
    });
    $('send').onclick = function () { if (state.streaming) stopStream(); else sendMessage(); };
    $('new-session').onclick = function () { if (!state.streaming) newSessionDraft(); };
    $('banner-retry').onclick = function () {
      $('status-dot').className = 'status-dot wait';
      $('status-text').textContent = '连接中…';
      loadHealth().catch(function () {});
    };
    $('ws-chip').onclick = function (e) {
      e.stopPropagation();
      var open = $('ws-dropdown').classList.contains('hidden');
      closeDropdowns();
      if (open) { renderWorkspaceMenu(); $('ws-dropdown').classList.remove('hidden'); $('ws-chip').setAttribute('aria-expanded', 'true'); }
    };
    $('model-chip').onclick = function (e) {
      e.stopPropagation();
      var open = $('model-dropdown').classList.contains('hidden');
      closeDropdowns();
      if (open) { renderModelMenu(); $('model-dropdown').classList.remove('hidden'); $('model-chip').setAttribute('aria-expanded', 'true'); }
    };
    document.addEventListener('click', function (e) {
      if (!e.target.closest || !e.target.closest('.dd-wrap')) closeDropdowns();
    });
    $('messages').addEventListener('click', function (e) {
      var copy = e.target.closest && e.target.closest('.code-copy');
      if (copy) {
        var code = copy.closest('.code-block');
        code = code && code.querySelector('code');
        if (code) copyText(code.textContent, copy);
        return;
      }
      var line = e.target.closest && e.target.closest('.tool-line');
      if (line) state.expanded[line.parentNode.dataset.key] = line.parentNode.classList.toggle('open');
    });
    $('overlay').onclick = function (e) { if (e.target === $('overlay')) closeDialog(); };
    $('files-save').onclick = saveFile;
    $('files-revert').onclick = function () {
      state.files.content = state.files.original;
      $('files-editor').value = state.files.original;
      renderFiles();
    };
    $('files-editor').addEventListener('input', function () {
      state.files.content = $('files-editor').value;
      renderFiles();
    });
    $('memory-add').onclick = addMemory;
    $('memory-refresh').onclick = loadMemory;
    $('logs-refresh').onclick = loadLogs;
    $('logs-autoscroll').onchange = function () {
      state.logsAutoScroll = $('logs-autoscroll').checked;
      saveLS();
      if (state.logsAutoScroll) $('logs-pre').scrollTop = $('logs-pre').scrollHeight;
    };
    $('settings-refresh').onclick = function () { loadProviders(); loadTools(); loadHealth().catch(function () {}); };
    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {   // Ctrl/Cmd+K 聚焦输入框
        e.preventDefault();
        if (state.view !== 'chat') showView('chat');
        $('input').focus();
      }
      if (e.key === 'Escape') { closeDropdowns(); closeDialog(); }          // Esc 关闭浮层
    });
  }

  function init() {
    var ls = loadLS();
    state.view = VIEWS.indexOf(ls.view) >= 0 ? ls.view : 'chat';
    state.model = ls.model || null;
    state.logsAutoScroll = ls.logs_autoscroll !== false;
    $('logs-autoscroll').checked = state.logsAutoScroll;
    bind();
    updateComposer();
    renderWorkspaceChip(); renderModelChip(); renderSessions(); renderChat();
    showView(state.view);
    var boot = function () { return Promise.all([loadProviders(), loadTools(), loadWorkspaces()]); };
    loadHealth().then(boot, boot).catch(function () {});
    startLogsTimer();
    autoGrow();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
