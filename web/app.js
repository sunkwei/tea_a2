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
    // 订阅额度（provider 声明 supports_usage 时才有）
    usage: null, usageError: '', usageAt: 0,
    workspaces: [], workspace: null, sessions: [], session: null, messages: [],
    model: null, models: [], modelMenu: false,
    live: null, liveNode: null, liveView: null, liveFrame: 0, liveRenderFallback: 0,
    liveMdAt: 0,        // 上一次重渲染「正在增长的段落」的时间（节流用）
    streaming: false, abort: null, stopTimer: null,
    attachments: [],
    queue: [], queueRestore: [], queueDone: {},
    sidebarHidden: false,
    ended: false, streamError: null, expanded: {}, stickBottom: true,
    trajectory: [], trajFull: {}, lastTrajectoryId: null,
    access: null,                                    // 文件访问范围（受限工作区 / 全局）
    stats: null, liveTokens: 0, liveStartAt: 0, metricsTimer: null,
    turnStartAt: 0, turnSteps: 0, mdBase: '',
    turnSeq: 0, attachRetries: 0,
    /// 每次 openSession 递增。开场会话的请求是异步的（而且 loadSessions 会自动打开
    /// 最近一个），用户点的那一下可能和自动打开撞在一起：晚到的旧请求如果把
    /// state.session/live 改回去，屏幕就会出现"刚接上的直播气泡又没了"。只有最后一次
    /// openSession 的结果算数。
    sessionEpoch: 0,
    /// 用户按了「＋ 新建会话」：这个标签页要的是草稿，不是"最近更新的那个会话"。
    /// loadSessions() 不能在后台把它替换成 sessions[0]（正在跑的那一轮结束后就会
    /// 触发一次刷新）。
    draft: false,
    // 你主动离开了哪个会话的轮次（它还在服务端跑）：别又把人拽回去
    detached: null,
    /// 服务端正在跑的轮次，按 session id 索引（GET /api/chat/turns）。侧栏据此
    /// 标出「进行中」，改名/删除也据此禁用——那些操作会被 worker 覆盖掉。
    running: {},
    /// 选中的分叉点：{ sid, mid, ord, preview }。下一条消息先 POST /fork 建一个
    /// 从这条轮次分出去的新会话，再发进那个会话；当前会话不动。null = 未选。
    fork: null,
    files: { dir: '', entries: [], path: null, content: '', original: '', truncated: false, readOnly: false, preview: false, size: 0 },
    memory: [], logsTimer: null, logsAutoScroll: true,
    /// 当前会话的任务列表（todo）：agent 用 toolkit_todo 维护，SSE 的 todo 事件
    /// 实时推送。done/total 用于「3/7」这样的进度显示。
    todos: [], todoDone: 0, todoTotal: 0, todoCollapsed: false,
    /// 本会话产出的文件（工具写出的文档、生成的图表/视频等）。agent 每发现一个新
    /// 文件就发一个 artifacts 事件；这里按 turn 分组，只在这一轮的**最终回答**下面
    /// 列出下载链接。
    artifacts: [], artifactsTruncated: false,
    agentDoc: {
      content: '', original: '', path: '', scriptsDir: '', scripts: [],
      loading: false, touched: false
    },
    // 「销毁」的预览结果（GET /api/destroy）：tea home + 每个工作区的
    // .tea_agent_run。只在打开确认框时取一次，不缓存。
    destroy: null,
    // 工作区级 system prompt（.tea_agent_run/system_prompt.md）。custom=false
    // 表示没有覆盖、实际用的是 defaultPrompt；编辑框始终显示 current（当前真正
    // 在用的提示词），保存后下一轮请求就生效。
    systemPrompt: {
      content: '', current: '', custom: false, path: '',
      defaultPrompt: '', defaultSource: 'builtin', maxBytes: 0,
      effectiveBytes: 0, estimatedTokens: 0, loading: false, touched: false
    }
  };

  /// 本标签页正在看的主题。**sessionStorage** 而不是 localStorage：两个窗口各自
  /// 记住自己的主题，另开一个窗口看别的主题不会被这一边覆盖掉。
  var TAB_KEY = 'tea_agent_tab_v1';
  var TOPIC_PARAM = 's';

  function saveLS() {
    try {
      // 只存"跨标签页共享的偏好"。当前会话属于**这个标签页**（见 setTabSession），
      // 写进 localStorage 会让最后关闭/最后刷新的窗口决定所有窗口看什么。
      localStorage.setItem(LS_KEY, JSON.stringify({
        view: state.view, workspace_id: state.workspace ? state.workspace.id : null,
        model: state.model,
        logs_autoscroll: state.logsAutoScroll, sidebar_hidden: state.sidebarHidden
      }));
    } catch (e) { /* 隐私模式下忽略 */ }
  }
  function loadLS() {
    try { return JSON.parse(localStorage.getItem(LS_KEY) || '{}') || {}; } catch (e) { return {}; }
  }
  function tabSession() {
    try { return sessionStorage.getItem(TAB_KEY) || ''; } catch (e) { return ''; }
  }
  function setTabSession(sid) {
    try {
      if (sid) sessionStorage.setItem(TAB_KEY, sid); else sessionStorage.removeItem(TAB_KEY);
    } catch (e) { /* 隐私模式下忽略 */ }
  }
  /// 主题的地址：`?s=<session_id>`。刷新、收藏、贴给另一个浏览器，打开的都是同一个
  /// 会话——而不是"索引里最近更新的那个"。两个窗口各带各的 `?s=`，互不干扰。
  function topicFromUrl() {
    try {
      var raw = new URLSearchParams(window.location.search).get(TOPIC_PARAM);
      return raw ? String(raw).trim() : '';
    } catch (e) { return ''; }
  }
  function setTopicUrl(sid) {
    var base = window.location.pathname;
    var next = base + (sid ? '?' + TOPIC_PARAM + '=' + encodeURIComponent(sid) : '');
    if (next === base + window.location.search) return;
    try { window.history.replaceState(null, '', next); } catch (e) { /* file:// 下忽略 */ }
  }

  // Debug handle for the browser tests: they assert on real state, not on a
  // DOM that could lag behind. Harmless in production (same closure, no I/O).
  try { window.teaState = state; } catch (e) { /* ignore */ }

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
        // 任何一个成功请求都证明服务活着：如果状态灯正处于「未连接」，立刻复核一次，
        // 而不是等下一次轮询（用户可能刚重启了服务端）。
        if (String(p).indexOf('api/health') !== 0) noteRequestOk();
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
  // 会话索引里的相对时间；超过一周就退回日期，避免「37 天前」这种噪音。
  function fmtRelTime(iso) {
    if (!iso) return '';
    var t = Date.parse(iso);
    if (isNaN(t)) return String(iso);
    var diff = Math.floor((Date.now() - t) / 1000);
    if (diff < 0) diff = 0;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    if (diff < 172800) return '昨天';
    if (diff < 604800) return Math.floor(diff / 86400) + ' 天前';
    return fmtTime(iso).slice(0, 10);
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

  /* ---------- 3) Markdown 渲染（先转义再解析，绝不注入原始 HTML） ----------
     renderMarkdown 先把整段源文本转义，之后所有内联处理都只在这份转义文本上做，
     所以原文里的 <script> / <img onerror> 永远只会变成可见文本。图片与链接另外
     做协议白名单：只有 http(s)、mailto、页内锚点和工作区相对路径能进入 src/href。 */
  var ESC_INV = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'" };
  function unesc(s) { return String(s).replace(/&(amp|lt|gt|quot|#39);/g, function (m) { return ESC_INV[m]; }); }
  function safeHref(raw, isImage) {
    var s = String(raw == null ? '' : raw).trim();
    if (!s) return '';
    if (/^https?:\/\//i.test(s)) return s;
    if (!isImage && /^mailto:/i.test(s)) return s;
    if (!isImage && s.charAt(0) === '#') return s;
    if (/^[a-z][a-z0-9+.\-]*:/i.test(s)) return '';      // javascript:、data:、vbscript: …
    if (s.indexOf('//') === 0) return '';                // 协议相对 URL 指向外部主机
    return s;                                            // 工作区相对路径
  }
  function rawAssetUrl(rel) {
    if (!state.workspace) return '';
    var clean = unesc(rel).replace(/^\.\//, '').replace(/^\/+/, '');
    if (!clean) return '';
    if (state.mdBase && !/^\.\.\//.test(clean)) clean = state.mdBase.replace(/\/+$/, '') + '/' + clean;
    return apiUrl('api/workspaces/' + encodeURIComponent(state.workspace.id) +
      '/raw?path=' + encodeURIComponent(clean));
  }
  function mdImage(alt, src) {
    var href = safeHref(src, true);
    if (!href) return alt;
    var url = /^https?:\/\//i.test(href) ? href : rawAssetUrl(href);
    if (!url) return alt;
    var cap = alt ? '<span class="md-cap">' + alt + '</span>' : '';
    return '<span class="md-figure"><img src="' + url + '" alt="' + alt + '"' +
      (alt ? ' title="' + alt + '"' : '') + ' loading="lazy" referrerpolicy="no-referrer">' + cap + '</span>';
  }
  function mdLink(label, href) {
    var h = safeHref(href, false);
    var url = '#';
    if (h) {
      if (/^(https?:|mailto:|#)/i.test(h) || h.charAt(0) === '/') url = h;
      else url = rawAssetUrl(h) || '#';
    }
    return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
  }
  // 表格：| a | b | 加一行 | --- | :---: | 分隔，分隔行决定每列对齐。
  function mdCells(line) {
    var s = String(line).trim();
    if (s.charAt(0) === '|') s = s.slice(1);
    if (s.charAt(s.length - 1) === '|') s = s.slice(0, -1);
    return s.split('|').map(function (c) { return c.trim(); });
  }
  function mdAlign(cell) {
    var left = cell.charAt(0) === ':', right = cell.charAt(cell.length - 1) === ':';
    if (left && right) return 'center';
    if (right) return 'right';
    if (left) return 'left';
    return '';
  }
  function isMdTableSep(line) {
    if (String(line).indexOf('|') < 0) return false;
    var cells = mdCells(line);
    if (!cells.length) return false;
    for (var i = 0; i < cells.length; i++) if (!/^:?-{2,}:?$/.test(cells[i])) return false;
    return true;
  }
  function isMdTableStart(lines, i) {
    return i + 1 < lines.length && String(lines[i]).indexOf('|') >= 0 && isMdTableSep(lines[i + 1]);
  }
  function inlineMd(t) {
    return t.split(/(`[^`]+`)/g).map(function (p) {
      if (p.length > 1 && p.charAt(0) === '`' && p.charAt(p.length - 1) === '`') {
        return '<code class="md-inline">' + p.slice(1, -1) + '</code>';
      }
      var s = p.replace(/!\[([^\]\n]*)\]\(([^)\s]+)\)/g, function (m, alt, src) { return mdImage(alt, src); });
      s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, function (m, label, href) { return mdLink(label, href); });
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
    // 注意：src 会先整体转义，所以引用符号到这里已经是 `&gt;`。
    var isQ = function (s) { return /^\s*(?:&gt;|>)\s?/.test(s); };
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
        while (i < lines.length && isQ(lines[i])) { q.push(lines[i].replace(/^\s*(?:&gt;|>)\s?/, '')); i++; }
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
      if (isMdTableStart(lines, i)) {                                     // 表格
        var head = mdCells(lines[i]), aligns = mdCells(lines[i + 1]).map(mdAlign), rows = [];
        i += 2;
        while (i < lines.length && lines[i].trim() && lines[i].indexOf('|') >= 0) {
          rows.push(mdCells(lines[i])); i++;
        }
        var alignAttr = function (idx) {
          return aligns[idx] ? ' style="text-align:' + aligns[idx] + '"' : '';
        };
        var thead = '<thead><tr>';
        head.forEach(function (c, k) { thead += '<th' + alignAttr(k) + '>' + inlineMd(c) + '</th>'; });
        thead += '</tr></thead>';
        var tbody = '<tbody>';
        rows.forEach(function (r) {
          tbody += '<tr>';
          head.forEach(function (c, k) {
            tbody += '<td' + alignAttr(k) + '>' + inlineMd(r[k] == null ? '' : r[k]) + '</td>';
          });
          tbody += '</tr>';
        });
        tbody += '</tbody>';
        out.push('<div class="md-table-wrap"><table class="md-table">' + thead + tbody + '</table></div>');
        continue;
      }
      var p = [];                                                        // 段落（保留换行）
      while (i < lines.length && lines[i].trim() && !/^\s*(```+|~~~+)/.test(lines[i]) &&
        !/^(#{1,6})\s+/.test(lines[i]) && !isQ(lines[i]) && !isUl(lines[i]) && !isOl(lines[i]) &&
        !isHr(lines[i]) && !isMdTableStart(lines, i)) {
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
    cross: '<svg viewBox="0 0 16 16"><path d="M4.2 4.2l7.6 7.6M11.8 4.2l-7.6 7.6"/></svg>',
    traj: '<svg viewBox="0 0 16 16"><path d="M3.2 4h3.2v3.2H3.2zM9.6 8.8h3.2V12H9.6z"/><path d="M6.4 5.6h3.2v4.8"/></svg>'
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

  /* ---------- 4b) 轨迹：每一轮的结构化审计记录 ----------
     后端在 <workspace>/.tea_agent_run/trajectories/<session>.jsonl 里一轮一行，
     记录模型迭代、工具调用（参数+实际输出）、压缩动作与工具真实回报的文件变更。
     折叠时只显示摘要（步数 / 耗时 / tokens）；展开才按 turn_id 拉完整记录，
     所以一个会话有多少轮都不会把页面拖慢。数据全部来自后端，UI 不做推测。 */
  function fmtSecSpaced(ms) {
    return ms == null ? '' : (num(ms) / 1000).toFixed(num(ms) < 10000 ? 1 : 0) + ' s';
  }
  function trajKindLabel(kind) {
    if (kind === 'model') return '模型';
    if (kind === 'tool') return '工具';
    if (kind === 'compaction') return '压缩';
    return String(kind || '步骤');
  }
  function fmtTrajSummary(t) {
    var parts = [];
    if (t.steps != null) parts.push(num(t.steps) + ' 步');
    if (t.elapsed_ms != null) parts.push(fmtSecSpaced(t.elapsed_ms));
    var u = t.usage || {}, tok = num(u.total_tokens);
    if (!tok) tok = num(u.prompt_tokens) + num(u.completion_tokens);
    if (tok) parts.push((u.estimated ? '≈' : '') + fmtTokens(tok) + ' tok');
    if (num(t.files_changed)) parts.push(num(t.files_changed) + ' 文件');
    return parts.join(' · ');
  }
  function trajStepView(step) {
    var li = el('div', 'traj-step ' + (step.kind || 'other'));
    var head = el('div', 'traj-step-head');
    head.appendChild(el('span', 'traj-idx', '#' + (num(step.index) + 1)));
    head.appendChild(el('span', 'traj-kind', trajKindLabel(step.kind)));
    if (step.name) head.appendChild(el('span', 'traj-name', step.name));
    head.appendChild(el('span', 'traj-ms', fmtMs(num(step.elapsed_ms))));
    if (step.kind === 'model') {
      head.appendChild(el('span', 'traj-info',
        '推理 ' + num(step.reasoning_chars) + ' 字 · 正文 ' + num(step.content_chars) + ' 字'));
      var tk = step.tokens || {};
      var tok = (step.usage_estimated ? '≈' : '') + fmtTokens(num(tk.prompt)) + ' → ' +
        fmtTokens(num(tk.completion)) + ' tok';
      if (num(tk.cached)) tok += ' · 缓存 ' + fmtTokens(num(tk.cached));
      head.appendChild(el('span', 'traj-tok', tok));
      if (num(step.ttft_ms)) head.appendChild(el('span', 'traj-tok', '首字 ' + fmtMs(num(step.ttft_ms))));
    } else if (step.kind === 'compaction') {
      head.appendChild(el('span', 'traj-info',
        '覆盖 ' + num(step.covered_messages) + ' 条消息 · 摘要 ' + fmtBytes(num(step.summary_bytes))));
    } else {
      var flags = [];
      if (step.exit_code != null) flags.push('exit ' + step.exit_code);
      if (step.truncated) flags.push('输出已截断');
      if (step.output_filter && step.output_filter !== 'none') {
        flags.push('过滤 ' + step.output_filter +
          (num(step.dropped_lines) ? ' -' + num(step.dropped_lines) + ' 行' : ''));
      }
      if (flags.length) head.appendChild(el('span', 'traj-flags', flags.join(' · ')));
    }
    li.appendChild(head);
    if (step.kind === 'tool') {
      if (step.arguments) li.appendChild(el('div', 'traj-args', step.arguments));
      if (step.output) li.appendChild(el('pre', 'traj-out', step.output));
      if (step.output_truncated) li.appendChild(el('div', 'traj-note', '输出已按轨迹上限截断'));
    }
    return li;
  }
  function trajFilesView(files) {
    var box = el('div', 'traj-files');
    box.appendChild(el('div', 'traj-files-title', '本轮改动'));
    files = files || [];
    if (!files.length) {
      box.appendChild(el('div', 'traj-note', '没有工具报告文件改动'));
      return box;
    }
    files.forEach(function (f) {
      var row = el('div', 'traj-file');
      row.appendChild(el('span', 'traj-op ' + (f.op || ''), f.op || '?'));
      if (f.path) row.appendChild(el('span', 'traj-path', f.path + (f.dest ? ' → ' + f.dest : '')));
      else if (f.command) row.appendChild(el('code', 'traj-cmd', f.command));
      var meta = [];
      if (f.op !== 'exec' && f.op !== 'delete') {
        if (f.bytes != null) meta.push(fmtBytes(num(f.bytes)));
        if (f.size != null) meta.push('共 ' + fmtBytes(num(f.size)));
      }
      if (f.exit_code != null) meta.push('exit ' + f.exit_code);
      if (f.ok === false) meta.push('失败');
      if (meta.length) row.appendChild(el('span', 'traj-file-meta', meta.join(' · ')));
      box.appendChild(row);
    });
    return box;
  }
  function renderTrajPanel(panel, turn) {
    clear(panel);
    var head = el('div', 'traj-head');
    var bits = [];
    if (turn.provider) bits.push(turn.provider);
    if (turn.model) bits.push(turn.model);
    bits.push(num(turn.iterations) + ' 轮迭代');
    if (turn.started_at) bits.push(fmtTime(turn.started_at));
    head.appendChild(el('span', 'traj-sum', bits.join(' · ')));
    head.appendChild(el('span', 'traj-id', turn.turn_id || ''));
    panel.appendChild(head);
    if (turn.error) {
      panel.appendChild(el('div', 'traj-error', turn.error + (turn.detail ? '\n' + turn.detail : '')));
    }
    var steps = turn.steps || [];
    if (!steps.length) panel.appendChild(el('div', 'traj-note', '没有步骤记录（可能仍在进行中）'));
    steps.forEach(function (s) { panel.appendChild(trajStepView(s)); });
    panel.appendChild(trajFilesView(turn.files_changed));
  }
  function trajectoryRow(turn, key) {
    var row = el('div', 'tool-row traj-row');
    row.dataset.key = key;
    row.dataset.turnId = turn.turn_id || '';
    if (key in state.expanded && state.expanded[key]) row.classList.add('open');
    var line = el('div', 'tool-line');
    var ic = el('span', 'tool-icon');
    ic.innerHTML = ICONS.traj;
    line.appendChild(ic);
    line.appendChild(el('span', 'tool-title', '轨迹'));
    line.appendChild(el('span', 'dot'));
    line.appendChild(el('span', 'tool-summary', fmtTrajSummary(turn)));
    if (turn.ok === false) line.appendChild(el('span', 'traj-badge', turn.partial ? '进行中' : '失败'));
    line.appendChild(el('span', 'chev', '›'));
    row.appendChild(line);
    row.appendChild(el('div', 'tool-panel traj-panel'));
    // 重新渲染时如果这一行本来就是展开的，面板要立即有内容（命中缓存时不发请求）。
    if (row.classList.contains('open')) loadTrajectoryPanel(row);
    return row;
  }
  function loadTrajectoryPanel(row) {
    var id = row.dataset.turnId;
    if (!id) return;
    var panel = row.querySelector('.traj-panel');
    if (!panel || panel.dataset.loaded === '1') return;
    var cached = state.trajFull[id];
    if (cached) { renderTrajPanel(panel, cached); panel.dataset.loaded = '1'; return; }
    panel.textContent = '载入中…';
    api('api/turns/' + encodeURIComponent(id) + '/trajectory').then(function (turn) {
      state.trajFull[id] = turn;
      renderTrajPanel(panel, turn);
      panel.dataset.loaded = '1';
    }, function (e) {
      panel.textContent = '轨迹加载失败：' + e.message;
    });
  }
  function loadTrajectories() {
    if (!state.workspace || !state.session) { state.trajectory = []; return Promise.resolve(); }
    var sid = state.session.id;
    return api('api/sessions/' + encodeURIComponent(sid) + '/trajectory').then(function (turns) {
      if (state.session && state.session.id === sid) state.trajectory = turns || [];
    }, function () { state.trajectory = []; });
  }
  // 把「第 N 轮」挂到该轮最后一条 assistant 消息上。从最新的一端对齐，
  // 这样即使早期轮次没有轨迹（比如功能上线前），最近几轮仍然能正确对应。
  function attachTrajectories() {
    var turns = state.trajectory || [];
    if (!turns.length || !state.messages.length) return;
    var userIdx = [];
    state.messages.forEach(function (m, i) { if (m.role === 'user') userIdx.push(i); });
    if (!userIdx.length) return;
    var newestUsers = userIdx.slice().reverse();
    for (var j = 0; j < turns.length && j < newestUsers.length; j++) {
      var start = newestUsers[j];
      var end = j === 0 ? state.messages.length : newestUsers[j - 1];
      for (var i = end - 1; i >= start; i--) {
        if (state.messages[i].role === 'assistant') { state.messages[i].trajectory = turns[j]; break; }
      }
    }
  }
  /* ---------- 4a-1) 流式渲染的成本 ----------
     一段回答在流式期间是「一个 md 块，text 一直在长」。以前每来一个 delta 就把
     整块重新跑一遍 markdown 解析、再 innerHTML 换掉：单次成本随回答长度线性增长，
     整轮下来就是 O(n²)——回答越长越卡，Firefox 尤其明显（它会直接提示
     「此网页拖慢了您的 firefox」，鼠标也跟着卡）。三个改动：
       * mdBlockHtml()：块级缓存。结构变化导致整块重建时，没变过的段落不用重解析。
       * 纯文本快路径：只有一段普通文字时直接改文本节点，不重跑 markdown、不重建
         DOM（光标动画也不会被重启）。
       * 节流：带 markdown 的段落最多每 100ms 重渲染一次；本轮结束时一定补上最终
         文本，所以不会少显示内容。
  */
  var MD_RENDER_MS = 100;
  var MD_INLINE_MARKERS = /[*_`\[\]()!~]/;
  /// 这段文字会不会被 markdown 改写？不会的话就能当成纯文本直接写进文本节点。
  function isPlainText(t) {
    if (!t) return true;
    if (MD_INLINE_MARKERS.test(t)) return false;
    if (t.indexOf('\n') >= 0) return false;
    if (/^\s*(#|[-+>]\s|\d+[.)]\s)/.test(t)) return false;
    return true;
  }
  /// 渲染结果里只有一个 <p>、且里面只有一个文本节点 => 可以直接改这个节点。
  function plainTextNode(mdEl) {
    if (!mdEl) return null;
    var ps = mdEl.querySelectorAll('p');
    if (ps.length !== 1) return null;
    var p = ps[0];
    if (p.childNodes.length !== 1) return null;
    var n = p.firstChild;
    return (n && n.nodeType === 3) ? n : null;
  }
  /// 段落渲染结果的缓存（挂在块对象上：流式期间块是同一个，只是 text 在长）。
  function mdBlockHtml(b) {
    if (b._mdText === b.text) return b._mdHtml;
    var html = renderMarkdown(b.text || '');
    b._mdText = b.text;
    b._mdHtml = html;
    return html;
  }
  function assistantNode(msg, outputs, keyPrefix, isLive) {
    var wrap = el('div', 'msg assistant' + (isLive ? ' streaming' : ''));
    if (isLive) wrap.dataset.live = '1';
    // 轨迹行默认折叠，放在最前面：先看到「这一轮做了什么」再看正文。
    if (msg.trajectory && !isLive) {
      wrap.appendChild(trajectoryRow(msg.trajectory, keyPrefix + ':traj'));
    }
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
        md.innerHTML = mdBlockHtml(b);
        if (isLive && !state.ended && idx === blocks.length - 1) md.appendChild(el('span', 'stream-caret'));
        wrap.appendChild(md);
      } else {
        var tc = b.tc;
        wrap.appendChild(toolRowView(tc, outputs[tc.id] || null, keyPrefix + ':t:' + tc.id, tc.status === 'running' && !outputs[tc.id]));
      }
    });
    return wrap;
  }
  /// A user bubble: text plus any attached images. Optimistic (just-sent)
  /// messages carry a data URL; reloaded ones carry a workspace path served by
  /// the /raw route.
  /* ---------- 本轮产出（下载链接） ----------
     工具写出的文档、渲染出的图表/视频，都记在会话的 artifacts 里（带 turn 编号）。
     只在这一轮的最终回答下面列出来：会话里攒了 50 个文件也不该铺满屏幕。 */
  var ARTIFACT_LABEL = {
    image: '图片', video: '视频', audio: '音频', document: '文档',
    data: '数据', archive: '压缩包', other: '文件'
  };
  function artifactIcon(kind) {
    if (kind === 'image') return '▣';
    if (kind === 'video') return '▶';
    if (kind === 'audio') return '♪';
    if (kind === 'document') return '▤';
    if (kind === 'data') return '▦';
    if (kind === 'archive') return '▧';
    return '▢';
  }
  function downloadUrl(path, inline) {
    return apiUrl('api/workspaces/' + encodeURIComponent(state.workspace.id) +
      '/download?path=' + encodeURIComponent(path) + (inline ? '&inline=1' : ''));
  }
  /// 这一轮的产出：turn 为 0（导入的历史记录）时按「全部」处理，但只在最后一条
  /// 助手消息下显示一次。
  function artifactsForTurn(turn) {
    return (state.artifacts || []).filter(function (a) {
      if (!a || !a.path) return false;
      return turn > 0 ? (a.turn || 0) === turn : true;
    });
  }
  function currentTurnNumber() {
    var n = 0;
    state.messages.forEach(function (m) { if (m.role === 'user') n++; });
    return n;
  }
  /// How many rows are shown before the list folds itself away. A turn that
  /// produced 40 screenshots should not push the conversation off the screen.
  var ARTIFACT_PREVIEW = 5;
  function artifactStrip(items, opts) {
    if (!items.length) return null;
    opts = opts || {};
    var box = el('div', 'artifacts');
    var head = el('div', 'artifacts-head');
    head.appendChild(el('span', 'a-title', '本轮产出'));
    head.appendChild(el('span', 'a-count', items.length + ' 个文件'));
    var toggle = null;
    if (items.length > ARTIFACT_PREVIEW) {
      toggle = btn('a-toggle', '展开全部 ' + items.length + ' 个', function () {
        var open = box.classList.toggle('open');
        toggle.textContent = open ? '收起' : '展开全部 ' + items.length + ' 个';
      });
      head.appendChild(toggle);
    }
    box.appendChild(head);
    var list = el('ul', 'artifacts-list');
    items.forEach(function (a) {
      var li = el('li', 'artifact');
      var kind = a.kind || 'other';
      li.appendChild(el('span', 'a-ico ' + kind, artifactIcon(kind)));
      var name = el('span', 'a-name', String(a.path).split('/').pop());
      name.title = a.path;
      li.appendChild(name);
      li.appendChild(el('span', 'a-meta',
        (ARTIFACT_LABEL[kind] || '文件') + ' · ' + fmtBytes(a.bytes || 0)));
      var link = document.createElement('a');
      link.className = 'a-dl';
      link.href = downloadUrl(a.path, false);
      link.setAttribute('download', '');
      link.textContent = '下载';
      link.title = '下载 ' + a.path;
      li.appendChild(link);
      // 图片和视频可以直接在浏览器里预览，省一次下载。
      if (kind === 'image' || kind === 'video' || kind === 'audio') {
        var view = btn('a-view', '查看', function () {
          if (kind === 'image') { openImageViewer(downloadUrl(a.path, true), a.path); return; }
          window.open(downloadUrl(a.path, true), '_blank', 'noopener');
        });
        view.title = '在浏览器里打开 ' + a.path;
        li.appendChild(view);
      }
      list.appendChild(li);
    });
    box.appendChild(list);
    if (items.length > ARTIFACT_PREVIEW) box.classList.add('folded');
    if (opts.truncated) {
      box.appendChild(el('div', 'a-note', '文件太多，只列出了部分；完整清单见会话目录'));
    }
    return box;
  }

  function userNode(m) {
    var node = el('div', 'msg user');
    if (m.content) node.appendChild(el('div', null, m.content));
    if (m.images && m.images.length) {
      var strip = el('div', 'msg-images');
      m.images.forEach(function (img) {
        var src = img.dataUrl;
        if (!src && img.path && state.workspace) {
          src = apiUrl('api/workspaces/' + encodeURIComponent(state.workspace.id) +
            '/raw?path=' + encodeURIComponent(img.path));
        }
        if (!src) return;
        var node2 = document.createElement('img');
        node2.src = src;
        node2.alt = '图片';
        // 缩略图只是索引：双击（或回车）按原图尺寸打开。单击不动，避免误触。
        node2.classList.add('zoomable');
        node2.tabIndex = 0;
        node2.title = '双击查看原图';
        node2.setAttribute('role', 'button');
        node2.ondblclick = function (e) {
          e.preventDefault();
          openImageViewer(src, '图片');
        };
        node2.onkeydown = function (e) {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            openImageViewer(src, '图片');
          }
        };
        strip.appendChild(node2);
      });
      if (strip.childNodes.length) node.appendChild(strip);
    }
    return node;
  }

  /* ---------- 图片查看器（双击缩略图看原图） ---------- */
  // 缩略图受 240x180 限制，看不清截图里的文字。双击后按**原图像素尺寸**显示
  // （width/height:auto + max-*:none），比视口大就滚动查看——缩放会让 1px 的
  // 分隔线糊掉，而看截图时那正是要找的东西。
  var imageViewer = { open: false };
  function openImageViewer(src, alt) {
    var box = $('image-viewer');
    var img = $('image-viewer-img');
    if (!box || !img || !src) return;
    box.classList.remove('hidden');
    imageViewer.open = true;
    img.src = src;
    img.alt = alt || '图片';
    $('image-viewer-meta').textContent = '加载中…';
    var describe = function () {
      if (!img.naturalWidth) return;
      var box = $('image-viewer');
      // 原图比视口大时不缩放（缩了就看不清 1px 的线），而是允许滚动，并把这件事说出来。
      var big = img.naturalWidth > box.clientWidth || img.naturalHeight > box.clientHeight;
      var meta = $('image-viewer-meta');
      meta.textContent = img.naturalWidth + ' × ' + img.naturalHeight + ' px · 原图尺寸' +
        (big ? ' · 可滚动查看' : '') + ' · 点击背景或按 Esc 关闭';
      meta.classList.toggle('large', big);
    };
    if (img.complete && img.naturalWidth) describe();
    else img.onload = describe;
    // 打开前把焦点放到图片上，Esc 才收得到（键盘用户从缩略图进来）。
    setTimeout(function () { img.focus(); }, 0);
  }
  function closeImageViewer() {
    if (!imageViewer.open) return false;
    var box = $('image-viewer');
    var img = $('image-viewer-img');
    box.classList.add('hidden');
    imageViewer.open = false;
    img.onload = null;
    img.removeAttribute('src');   // 释放可能很大的原图
    return true;
  }

  /* ---------- 产出文件（artifacts） ---------- */
  function setArtifacts(items, truncated) {
    state.artifacts = (items || []).filter(function (a) { return a && a.path; });
    state.artifactsTruncated = !!truncated;
    renderChat();
  }

  /* ---------- 4a) 任务列表（todo） ----------
     复杂任务由 agent 用 toolkit_todo 分解成阶段；这里只负责呈现和让人手动勾选。
     数据来自三处：打开会话时的 session.todos、SSE 的 todo 事件、以及本地勾选后的
     PUT 响应，三者都走 setTodos()，所以看到的状态永远是同一份。 */
  function setTodos(items, opts) {
    opts = opts || {};
    state.todos = (items || []).filter(function (t) { return t && String(t.text || '').trim(); });
    var done = 0;
    state.todos.forEach(function (t) { if (t.status === 'done') done++; });
    state.todoDone = done;
    state.todoTotal = state.todos.length;
    // 同步侧栏里的「3/7」：不改会话文件，只更新内存里的那份索引。
    if (state.session) {
      state.sessions.forEach(function (s) {
        if (s.id === state.session.id) {
          s.todo_total = state.todoTotal;
          s.todo_done = state.todoDone;
        }
      });
      if (!opts.noSidebar) renderSessions();
    }
    // 新任务开始（还有没做完的）就展开：进度是这一轮的重点，不该藏在折叠里。
    if (!opts.keepCollapsed && state.todos.length && done < state.todos.length) {
      state.todoCollapsed = false;
    }
    renderTodos();
  }
  function renderTodos() {
    var panel = $('todo-panel');
    if (!panel) return;
    // 全部做完就不再占地方了：任务面板贴在输入框上方，一整列删除线既占地方又没有
    // 下一步可看。做完就收起来，下一轮不再显示（侧栏的「6/6」和会话里的记录还在，
    // 想看历史随时能翻会话）。
    var allDone = state.todoTotal > 0 && state.todoDone === state.todoTotal;
    panel.classList.toggle('hidden', !state.todos.length || allDone);
    panel.classList.toggle('collapsed', !!state.todoCollapsed);
    $('todo-head').setAttribute('aria-expanded', state.todoCollapsed ? 'false' : 'true');
    $('todo-count').textContent = state.todoDone + '/' + state.todoTotal;
    $('todo-count').classList.toggle('all-done',
      state.todoTotal > 0 && state.todoDone === state.todoTotal);
    var pct = state.todoTotal ? Math.round(state.todoDone * 100 / state.todoTotal) : 0;
    $('todo-bar').style.width = pct + '%';
    $('todo-bar').parentNode.title = '已完成 ' + state.todoDone + ' / ' + state.todoTotal +
      '（' + pct + '%）';
    var list = $('todo-list');
    clear(list);
    state.todos.forEach(function (t, i) {
      var li = el('li', 'todo-item ' + (t.status || 'pending'));
      var mark = el('span', 'todo-mark',
        t.status === 'done' ? '✓' : (t.status === 'active' ? '●' : '○'));
      mark.setAttribute('aria-hidden', 'true');
      li.appendChild(mark);
      li.appendChild(el('span', 'todo-text', t.text));
      li.title = t.status === 'done' ? '已完成（点击改回未完成）'
        : (t.status === 'active' ? '进行中（点击标记完成）' : '未开始（点击标记完成）');
      li.tabIndex = 0;
      li.setAttribute('role', 'checkbox');
      li.setAttribute('aria-checked', t.status === 'done' ? 'true' : 'false');
      var toggle = function () { toggleTodo(i); };
      li.onclick = toggle;
      li.onkeydown = function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
      };
      list.appendChild(li);
    });
  }
  /// 手动勾选一条：先本地更新（点完立刻有反馈），再把整份列表写回服务端。
  function toggleTodo(index) {
    if (!state.session || !state.workspace) return;
    var t = state.todos[index];
    if (!t) return;
    var copy = state.todos.slice();
    copy[index] = { id: t.id, text: t.text, status: t.status === 'done' ? 'pending' : 'done' };
    setTodos(copy, { keepCollapsed: true });
    api('api/sessions/' + encodeURIComponent(state.session.id) + '/todos',
      { method: 'PUT', body: { items: copy } }).then(function (d) {
        setTodos(d.items, { keepCollapsed: true });
      }, function (e) {
        toast('更新任务失败：' + e.message);
        if (state.session) openSession(state.session.id);
      });
  }

  /* ---------- 4b) 对话跳转索引（右侧轮次刻度） ----------
     一条用户消息 = 一个编号。悬停/聚焦弹出该条输入的前 64 个字符，点击滚到那一条；
     滚动时高亮当前视口所在的轮次。索引完全由 state.messages 推导，不额外请求服务端。 */
  var TURN_PREVIEW_CHARS = 64;
  var turnSpyFrame = 0;

  /// 取用户输入的前 64 个字符。先去掉行首空白，再按 Unicode 码点截断，避免把
  /// 表情切一半；换行折成空格，让 tip 保持紧凑。纯图片消息没有文字，给一个说明。
  function turnPreview(m) {
    var text = String(m && m.content != null ? m.content : '').replace(/\r\n?/g, '\n').replace(/^\s+/, '');
    var chars = Array.from ? Array.from(text) : text.split('');
    var cut = chars.length > TURN_PREVIEW_CHARS;
    var head = chars.slice(0, TURN_PREVIEW_CHARS).join('').replace(/\s+/g, ' ').trim();
    if (!head) return (m && m.images && m.images.length) ? '（图片消息）' : '（空消息）';
    return head + (cut ? '…' : '');
  }
  function turnList() {
    var out = [];
    for (var i = 0; i < state.messages.length; i++) {
      var m = state.messages[i];
      if (m && m.role === 'user') out.push({ mid: i, ord: out.length + 1, msg: m, preview: turnPreview(m) });
    }
    return out;
  }
  /// 选中的分叉点：下一条消息会先建一个从这一轮分出去的新会话，再发进去。
  /// 点同一编号、点 × 或按 Esc 都能取消——它只影响下一条消息。
  function clearForkPoint(silent) {
    if (!state.fork) return;
    state.fork = null;
    renderForkStrip();
    renderTurnIndex();
    if (!silent) toast('已取消分叉点');
  }
  function forkPointFor(sid, mid) {
    return state.fork && state.fork.sid === sid && state.fork.mid === mid ? state.fork : null;
  }
  function setForkPoint(t) {
    if (!state.session) return;
    state.fork = { sid: state.session.id, mid: t.mid, ord: t.ord, preview: t.preview };
    renderForkStrip();
    renderTurnIndex();
  }
  function renderForkStrip() {
    var strip = $('fork-strip');
    if (!strip) return;
    clear(strip);
    var f = state.fork;
    if (!f || !state.session || state.session.id !== f.sid) {
      strip.classList.add('hidden');
      updatePlaceholder();
      return;
    }
    strip.classList.remove('hidden');
    strip.appendChild(el('span', 'f-tag', '分叉点'));
    var text = el('span', 'f-text', '第 ' + f.ord + ' 条：' + f.preview);
    text.title = '下一条消息会写进一个从第 ' + f.ord + ' 条分出来的新会话；当前会话保持原样';
    strip.appendChild(text);
    var del = btn('f-cancel', '×', function () { clearForkPoint(); });
    del.title = '取消分叉点（也可以再点一次索引里的这个编号，或按 Esc）';
    del.setAttribute('aria-label', '取消分叉点');
    strip.appendChild(del);
    updatePlaceholder();
  }
  function hideTurnTip() {
    var tip = $('turn-tip');
    if (tip) tip.classList.add('hidden');
  }
  function showTurnTip(dot, ord, preview) {
    var tip = $('turn-tip');
    if (!tip) return;
    clear(tip);
    tip.appendChild(el('span', 'tip-idx', '第 ' + ord + ' 条' +
      (dot.classList.contains('fork') ? ' · 分叉点（再点取消）' : '')));
    tip.appendChild(el('span', 'tip-text', preview));
    tip.dataset.turn = String(ord);
    tip.classList.remove('hidden');
    var r = dot.getBoundingClientRect();
    if (!r.width && !r.height) { hideTurnTip(); return; }   // 索引条被窄屏隐藏时不该弹
    var top = r.top + r.height / 2;
    var h = tip.offsetHeight;
    // 贴边时把 tip 夹回视口，别让第一条/最后一条的提示被窗口切掉。
    top = Math.max(h / 2 + 8, Math.min(window.innerHeight - h / 2 - 8, top));
    // 用 right 而不是 left 定位：left + max-width 会让浏览器把「left 到视口右缘」
    // 当成可用宽度，索引条本来就贴着右边，提示会被压成一列单字。right 定位下宽度
    // 由 CSS 给定，锚在索引条左缘往左长。
    tip.style.left = 'auto';
    tip.style.right = (window.innerWidth - r.left + 10) + 'px';
    tip.style.top = top + 'px';
  }
  function updateActiveTurn() {
    var rail = $('turn-index');
    if (!rail || rail.classList.contains('hidden')) return;
    var dots = rail.querySelectorAll('.turn-dot');
    if (!dots.length) return;
    var active = dots.length - 1;
    if (!state.stickBottom) {
      // 视口顶部（留 24px 余量）之上最后一条用户消息就是「当前在读」的轮次。
      var s = $('chat-scroll');
      var base = s.getBoundingClientRect().top + 24;
      var nodes = $('messages').querySelectorAll('.msg.user');
      active = 0;
      for (var i = 0; i < nodes.length; i++) {
        if (nodes[i].getBoundingClientRect().top <= base) active = i;
        else break;
      }
    }
    for (var j = 0; j < dots.length; j++) dots[j].classList.toggle('active', j === active);
    rail.dataset.active = String(active + 1);
    var ad = dots[active];
    if (ad && rail.scrollHeight > rail.clientHeight) {
      var rt = rail.getBoundingClientRect(), ar = ad.getBoundingClientRect();
      if (ar.top < rt.top + 2) rail.scrollTop -= (rt.top + 2 - ar.top);
      else if (ar.bottom > rt.bottom - 2) rail.scrollTop += (ar.bottom - rt.bottom + 2);
    }
  }
  function scheduleTurnSpy() {
    if (turnSpyFrame) return;
    var raf = window.requestAnimationFrame || function (fn) { return setTimeout(fn, 16); };
    turnSpyFrame = raf(function () { turnSpyFrame = 0; updateActiveTurn(); });
  }
  function jumpToTurn(mid) {
    var s = $('chat-scroll');
    var node = $('messages').querySelector('.msg.user[data-mid="' + mid + '"]');
    if (!node) return;
    hideTurnTip();
    var top = node.getBoundingClientRect().top - s.getBoundingClientRect().top + s.scrollTop - 16;
    state.stickBottom = false;
    s.scrollTop = Math.max(0, top);
    updateActiveTurn();
    node.classList.add('turn-hit');
    setTimeout(function () { node.classList.remove('turn-hit'); }, 900);
  }
  function renderTurnIndex() {
    var rail = $('turn-index');
    if (!rail) return;
    var turns = turnList();
    clear(rail);
    hideTurnTip();
    if (turns.length < 2) {
      rail.classList.add('hidden');
      rail.removeAttribute('data-count');
      rail.removeAttribute('data-active');
      return;
    }
    rail.classList.remove('hidden');
    rail.dataset.count = String(turns.length);
    rail.setAttribute('aria-label', '对话跳转索引：共 ' + turns.length + ' 条用户消息');
    var sid = state.session ? state.session.id : null;
    turns.forEach(function (t) {
      var armed = !!forkPointFor(sid, t.mid);
      var dot = btn('turn-dot', String(t.ord), function () {
        jumpToTurn(t.mid);
        // 生成中按 Enter 只会排队，排队消息不携带分叉；这时只做跳转，避免
        // 让用户以为排进去的消息会进新会话。
        if (state.streaming) return;
        if (forkPointFor(sid, t.mid)) clearForkPoint(); else setForkPoint(t);
      });
      dot.classList.toggle('fork', armed);
      dot.setAttribute('aria-pressed', armed ? 'true' : 'false');
      dot.dataset.mid = String(t.mid);
      dot.dataset.turn = String(t.ord);
      dot.title = '第 ' + t.ord + ' 条：' + t.preview;
      dot.setAttribute('aria-label', '跳到第 ' + t.ord + ' 条：' + t.preview);
      dot.onmouseenter = function () { showTurnTip(dot, t.ord, t.preview); };
      dot.onmouseleave = hideTurnTip;
      dot.onfocus = function () { showTurnTip(dot, t.ord, t.preview); };
      dot.onblur = hideTurnTip;
      rail.appendChild(dot);
    });
    updateActiveTurn();
  }

  function renderChat() {
    var box = $('messages');
    clear(box);
    state.liveNode = null;
    state.liveView = null;
    if (!state.workspaces.length) { renderTurnIndex(); return; } // 空状态由添加工作区弹窗接管
    var outputs = {};
    state.messages.forEach(function (m) {
      if (m.role === 'tool' && m.tool_call_id) {
        outputs[m.tool_call_id] = { output: m.content, ok: m.ok !== false, elapsed_ms: m.elapsed_ms };
      }
    });
    // The final answer of the latest turn is the natural place for "here are the
    // files I produced": find that message first so the strip can be attached
    // after the loop (a turn can contain several assistant messages).
    var lastAssistant = -1;
    for (var k = state.messages.length - 1; k >= 0; k--) {
      if (state.messages[k] && state.messages[k].role === 'assistant') { lastAssistant = k; break; }
    }
    state.messages.forEach(function (m, i) {
      if (m.role === 'tool') return;                            // 工具消息只作为输出附加
      if (m.role === 'user') {
        var un = userNode(m);
        un.dataset.mid = String(i);
        box.appendChild(un);
      }
      else if (m.role === 'system') box.appendChild(el('div', 'msg system', m.content));
      else {
        var an = assistantNode(m, outputs, 'm' + i, false);
        box.appendChild(an);
        if (i === lastAssistant && !state.live) {
          // Only once the turn is over (no live bubble): while it runs, files are
          // still being produced and the list would jump around.
          var strip = artifactStrip(artifactsForTurn(currentTurnNumber()),
                                   { truncated: state.artifactsTruncated });
          if (strip) box.appendChild(strip);
        }
      }
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
    renderTurnIndex();
    renderForkStrip();
  }
  /// 流式渲染只在「结构」变化时才重建气泡；纯文字增长只更新最后一个段落。
  /// 以前每来一个 delta 就 assistantNode(...) 重建整个气泡：历史段落会被重新
  /// 跑一遍 markdown 解析，工具行也全部重建，于是一轮长回答的 DOM 工作量是
  /// O(段落数 × delta 数)。Firefox 的「此网页拖慢了您的 firefox」就是这么来的。
  function liveStructure(live) {
    var parts = [live.reasoning ? 'r' : '-'];
    for (var i = 0; i < live.blocks.length; i++) {
      var b = live.blocks[i];
      if (b.type === 'md') { parts.push(b.text ? 'md' : 'md0'); continue; }
      var tc = b.tc || {};
      var out = live.outputs && live.outputs[tc.id];
      parts.push('t:' + tc.id + ':' + (tc.status || '') + ':' +
        (out ? (out.ok === false ? 'fail' : 'ok') : '-'));
    }
    parts.push(state.ended ? 'e' : 's');
    return parts.join('|');
  }
  /// Text of the last markdown block, plus how many markdown elements the block
  /// list should have produced - so an incremental update can tell whether the
  /// last .md element really is the block being streamed.
  function lastMdInfo(live) {
    var text = '';
    var rendered = 0;
    for (var i = 0; i < live.blocks.length; i++) {
      var b = live.blocks[i];
      if (b.type !== 'md') continue;
      if (b.text) { rendered++; text = b.text; }
    }
    return { text: text, rendered: rendered };
  }
  function lastBlockIsMd(live) {
    var n = live.blocks.length;
    return n > 0 && live.blocks[n - 1].type === 'md';
  }
  function buildLiveNode(live) {
    var box = $('messages');
    var node = assistantNode(live, live.outputs, 'live', true);
    if (state.liveNode && state.liveNode.parentNode === box) box.replaceChild(node, state.liveNode);
    else box.appendChild(node);
    state.liveNode = node;
    var mds = node.querySelectorAll('.md');
    var info = lastMdInfo(live);
    var lastMd = mds.length ? mds[mds.length - 1] : null;
    state.liveView = {
      node: node,
      sig: liveStructure(live),
      mdEl: lastMd,
      mdRendered: info.rendered,
      mdText: info.text,
      mdPlain: plainTextNode(lastMd),
      reasonEl: node.querySelector('.reasoning .reason-panel'),
      reasonText: live.reasoning || ''
    };
    state.liveMdAt = 0;      // 重建后下一帧就渲染一次
  }
  function renderLive() {
    var box = $('messages');
    var hint = box.querySelector('.chat-empty');
    if (hint) hint.remove();
    if (!state.live) {
      if (state.liveNode && state.liveNode.parentNode) state.liveNode.parentNode.removeChild(state.liveNode);
      state.liveNode = null; state.liveView = null; return;
    }
    var live = state.live;
    var sig = liveStructure(live);
    var view = state.liveView;
    var info = lastMdInfo(live);
    var canPatch = view && view.node && view.node.parentNode === box && view.sig === sig &&
      view.mdEl && info.rendered === view.mdRendered;

    if (!canPatch) {
      // 结构变了（新增段落 / 思考开始 / 工具状态变化 / 本轮结束）：整块重建。
      buildLiveNode(live);
      scrollMaybe();
      return;
    }

    // 结构没变：只有文字在增长。只碰最后一个段落与思考面板。
    if (info.text && info.text !== view.mdText) {
      var now = Date.now();
      var due = state.ended || !state.liveMdAt || (now - state.liveMdAt) >= MD_RENDER_MS;
      if (view.mdPlain && isPlainText(info.text)) {
        // 纯文本：直接改文本节点（不重跑 markdown、不重建 DOM、不重启光标动画）
        view.mdPlain.data = info.text;
        view.mdText = info.text;
      } else if (due) {
        view.mdEl.innerHTML = renderMarkdown(info.text);
        if (!state.ended && lastBlockIsMd(live)) view.mdEl.appendChild(el('span', 'stream-caret'));
        view.mdText = info.text;
        view.mdPlain = plainTextNode(view.mdEl);
        state.liveMdAt = now;
      }
    }
    // 本轮结束：光标（闪烁的那个小块）该收掉了。快路径不会重建 DOM，所以这里
    // 单独摘掉它。
    if (state.ended && view.mdEl) {
      var caret = view.mdEl.querySelector('.stream-caret');
      if (caret && caret.parentNode) caret.parentNode.removeChild(caret);
    }
    var reasoning = live.reasoning || '';
    if (view.reasonEl && reasoning !== view.reasonText) {
      view.reasonEl.textContent = reasoning;
      var summary = view.node.querySelector('.reasoning .tool-summary');
      if (summary) summary.textContent = reasoning.length + ' 字';
      view.reasonText = reasoning;
    }
    scrollMaybe();
  }

  /// 把一帧内的多次 DOM 变更合成一次：SSE 的 delta 经常比刷新率还密，
  /// 逐个渲染不仅慢，还会让浏览器判定页面「拖慢浏览器」。
  /// rAF 并不总是来：页面被遮挡 / 在后台标签页里时浏览器会挂起它（headless 下
  /// 导航回来也可能不再产生帧）。只靠 rAF 的话，事件还在收、state.live 还在长，
  /// 画面却一动不动——所以再挂一个 200ms 的看门狗：帧来了走帧（看门狗被取消），
  /// 帧不来也保证每 200ms 至少画一次。
  function scheduleLiveRender() {
    if (state.liveFrame) return;
    var ran = false;
    var run = function () {
      if (ran) return;
      ran = true;
      state.liveFrame = 0;
      if (state.liveRenderFallback) { clearTimeout(state.liveRenderFallback); state.liveRenderFallback = 0; }
      renderLive();
    };
    var raf = window.requestAnimationFrame || function (fn) { return setTimeout(fn, 16); };
    state.liveFrame = raf(run);
    state.liveRenderFallback = setTimeout(run, 200);
  }
  function cancelLiveRender() {
    if (state.liveRenderFallback) { clearTimeout(state.liveRenderFallback); state.liveRenderFallback = 0; }
    if (!state.liveFrame) return;
    var caf = window.cancelAnimationFrame || clearTimeout;
    caf(state.liveFrame);
    state.liveFrame = 0;
  }
  function nearBottom() { var s = $('chat-scroll'); return s.scrollHeight - s.scrollTop - s.clientHeight < 80; }
  /// 只在真的偏移时才写 scrollTop：贴底滚动是常态，每帧写一次同样的值同样会
  /// 触发滚动相关的布局工作。
  function scrollMaybe(force) {
    var s = $('chat-scroll');
    if (!force && !state.stickBottom) return;
    var target = s.scrollHeight - s.clientHeight;
    if (target > 0 && Math.abs(s.scrollTop - target) > 1) s.scrollTop = target;
  }

  /* ---------- 5b) 指标栏（输出速度 / 缓存命中率 / 上下文用量） ----------
     数据来源是 SSE 的 `metrics` 块（iteration_end / done 都带），配合 usage 里的
     prompt/completion/cached tokens。provider 没返回 usage 时后端会给出本地估算并
     在 metrics.estimated 里标记，UI 用「≈」和 tooltip 说明，不把估算当事实展示。 */
  var METRICS_KEY = 'tea_agent_ui_metrics_v1';
  function emptyStats() {
    return {
      output: 0, context: 0, max: 0, prompt: 0, cached: 0, total: 0, cachePct: null,
      speed: null, ttft: null, genMs: 0, elapsed: 0, estimated: false, at: 0,
      steps: 0, turns: 0
    };
  }
  function num(v) { return typeof v === 'number' && isFinite(v) ? v : 0; }
  // providers 端点会在 model_info 里给出窗口；metrics 没带 max_context_tokens 时用它兜底。
  function modelMaxContext() {
    if (!state.model) return 0;
    var found = 0;
    state.models.forEach(function (m) {
      if (m.provider === state.model.provider && m.model === state.model.model) found = num(m.max);
    });
    return found;
  }
  // 轮次 = 会话里的用户消息条数（与「模型迭代次数」区分开，后者在 metrics 里）。
  function countUserTurns() {
    var n = 0;
    for (var i = 0; i < state.messages.length; i++) {
      if (state.messages[i] && state.messages[i].role === 'user') n++;
    }
    return n;
  }
  // 与后端 Agent::estimate_tokens 同量级：CJK 约 1 token/字，ASCII 约 4 字符 1 token。
  function estTokens(text) {
    if (!text) return 0;
    var ascii = 0, wide = 0, other = 0;
    for (var i = 0; i < text.length; i++) {
      var c = text.charCodeAt(i);
      if (c < 0x80) { if (c !== 32 && c !== 10 && c !== 9 && c !== 13) ascii++; }
      else if (c >= 0x2e80) wide++;
      else other++;
    }
    return Math.round(ascii / 4) + wide + Math.round(other / 2);
  }
  function fmtTokens(n) {
    n = num(n);
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(n < 10000 ? 1 : 0) + 'k';
    return String(Math.round(n));
  }
  function fmtSec(ms) { return ms == null ? '—' : (num(ms) / 1000).toFixed(num(ms) < 10000 ? 1 : 0) + 's'; }
  function loadStatsMap() {
    try { return JSON.parse(localStorage.getItem(METRICS_KEY) || '{}') || {}; } catch (e) { return {}; }
  }
  function statsFor(sid) {
    var s = emptyStats();
    var saved = sid ? loadStatsMap()[sid] : null;
    if (!saved) return s;
    Object.keys(s).forEach(function (k) { if (k in saved) s[k] = saved[k]; });
    return s;
  }
  function saveStats() {
    var sid = state.session ? state.session.id : null;
    if (!sid) return;
    var map = loadStatsMap();
    map[sid] = state.stats;
    var ids = Object.keys(map);
    if (ids.length > 30) {                 // 只留最近 30 个会话，别把 localStorage 撑爆
      ids.sort(function (a, b) { return num(map[b] && map[b].at) - num(map[a] && map[a].at); });
      ids.slice(30).forEach(function (id) { delete map[id]; });
    }
    try { localStorage.setItem(METRICS_KEY, JSON.stringify(map)); } catch (e) { /* 配额满则忽略 */ }
  }
  /// 指标栏每 500ms 重画一次，但绝大多数时刻文字没有变化。逐项比较后只写真正
  /// 变了的节点：无谓的 textContent/style 写入同样会触发样式与布局重算。
  function setMetric(id, text, title, cls) {
    var v = $(id + '-v');
    if (v && v.textContent !== text) v.textContent = text;
    var bar = $(id + '-bar');
    if (bar && cls && cls.width != null && bar.style.width !== cls.width) bar.style.width = cls.width;
    var box = $(id);
    if (!box) return;
    box.classList.toggle('warn', !!(cls && cls.warn));
    box.classList.toggle('bad', !!(cls && cls.bad));
    box.classList.toggle('est', !!(cls && cls.est));
    if (title && box.title !== title) box.title = title;
  }
  function renderMetrics() {
    var s = state.stats || emptyStats();
    var live = state.streaming && state.liveTokens > 0 && state.liveStartAt > 0;
    var liveSec = live ? Math.max(0.2, (Date.now() - state.liveStartAt) / 1000) : 0;
    var speed = live ? state.liveTokens / liveSec : s.speed;
    var est = !live && s.estimated;
    if (!s.max) s.max = modelMaxContext();       // metrics 没带窗口时用 providers 兜底

    // 耗时：流式期间按本地时钟跳动，结束后用 done 的 elapsed_ms。
    var liveElapsed = state.streaming && state.turnStartAt ? Date.now() - state.turnStartAt : null;
    var elapsed = liveElapsed != null ? liveElapsed : num(s.elapsed);
    setMetric('metric-elapsed', elapsed > 0 ? (liveElapsed != null ? '≈' : '') + fmtSec(elapsed) : '—',
      liveElapsed != null
        ? '本轮已运行 ' + fmtSec(liveElapsed)
        : (s.elapsed > 0 ? '整轮耗时 ' + fmtSec(s.elapsed) +
          (s.output > 0 ? '\n按整轮耗时吞吐 ≈ ' + (s.output / (s.elapsed / 1000)).toFixed(1) + ' t/s' : '')
          : '还没有本轮数据'),
      { est: liveElapsed != null });

    // 轮次：会话内的用户消息数；步数：本轮 tool_start 次数。
    var turns = countUserTurns();
    setMetric('metric-turns', turns > 0 ? String(turns) : '—',
      '会话轮次：该会话里已发送的用户消息数');
    var steps = state.turnSteps > 0 ? state.turnSteps : num(s.steps);
    setMetric('metric-steps', (steps > 0 || state.streaming) ? String(steps) : '—',
      '步数：本轮执行的工具调用次数（tool_start 事件）');

    setMetric('metric-speed',
      speed == null ? (state.streaming ? '生成中…' : '—')
                    : (live || est ? '≈' : '') + speed.toFixed(speed >= 100 ? 0 : 1) + ' t/s',
      live
        ? '正在生成：本轮已输出约 ' + fmtTokens(state.liveTokens) + ' tokens / ' + liveSec.toFixed(1) + 's'
        : (speed == null ? (state.streaming ? '等待第一个增量…' : '还没有生成数据') :
          (est ? '本地估算（provider 未返回 usage）\n' : '') +
          '输出 ' + fmtTokens(s.output) + ' tokens / 生成 ' + fmtSec(s.genMs) + '（不含首字与工具执行）' +
          (s.elapsed > 0 ? '\n整轮吞吐 ≈ ' + (s.output / (s.elapsed / 1000)).toFixed(1) + ' t/s（含 TTFT 与工具执行，' + fmtSec(s.elapsed) + '）' : '') +
          '\n首字延迟 ' + fmtSec(s.ttft)),
      { est: est || live });

    var cachePct = s.cachePct;
    setMetric('metric-cache', cachePct == null ? '—' : cachePct + '%',
      cachePct == null
        ? 'provider 未返回缓存字段（DeepSeek: prompt_cache_hit_tokens；OpenAI: prompt_tokens_details.cached_tokens）\n' +
          '不少 OpenAI 兼容网关只有在请求带 stream_options.include_usage 时才报用量'
        : '前缀缓存命中 ' + fmtTokens(s.cached) + ' / ' + fmtTokens(s.prompt) + ' prompt tokens',
      { width: (cachePct == null ? 0 : Math.min(100, cachePct)) + '%' });

    var ctxPct = s.max > 0 ? Math.round(s.context * 100 / s.max) : null;
    setMetric('metric-context',
      s.context ? (est ? '≈' : '') + fmtTokens(s.context) + (s.max > 0 ? '/' + fmtTokens(s.max) : '') +
        (ctxPct == null ? '' : ' ' + ctxPct + '%') : '—',
      (s.max > 0 ? '上下文窗口 ' + s.context + ' / ' + s.max + ' tokens（' + ctxPct + '%）'
                 : '本次请求 prompt tokens：' + s.context + '（模型未配置 max_context_tokens）') +
        (est ? '\n本地估算：provider 未返回 usage' : ''),
      { width: (ctxPct == null ? 0 : Math.min(100, ctxPct)) + '%', warn: ctxPct != null && ctxPct >= 70, bad: ctxPct != null && ctxPct >= 90 });

    // 单行预算很紧：单位不进正文，tooltip 里已经写清楚是 tokens。
    var parts = [];
    if (s.output) parts.push('输出 ' + fmtTokens(s.output));
    if (s.total) parts.push('合计 ' + fmtTokens(s.total));
    if (s.ttft != null && s.ttft > 0) parts.push('首字 ' + fmtSec(s.ttft));
    setMetric('metric-tokens', parts.length ? parts.join('·') : '—',
      parts.length ? '本轮统计' + (est ? '（本地估算）' : '') + '\n' +
        '输出 ' + s.output + ' tokens，prompt ' + s.prompt + ' tokens，合计 ' + s.total +
        '\n生成窗口 ' + s.genMs + 'ms，整轮 ' + s.elapsed + 'ms' : '还没有本轮数据',
      { est: est });
  }
  function applyMetrics(m, usage) {
    if (!m) return;
    var s = state.stats;
    s.output = num(m.output_tokens);
    s.context = num(m.context_tokens);
    s.max = num(m.max_context_tokens);
    s.ttft = num(m.ttft_ms);
    s.genMs = num(m.generation_ms);
    s.elapsed = num(m.elapsed_ms);
    s.estimated = !!m.estimated;
    s.speed = s.genMs > 0 ? s.output / (s.genMs / 1000) : null;
    if (usage) {
      s.prompt = num(usage.prompt_tokens);
      s.cached = num(usage.cached_tokens);
      s.total = num(usage.total_tokens);
      s.cachePct = s.prompt > 0 ? Math.round(s.cached * 100 / s.prompt) : null;
    }
    s.at = Date.now();
    s.steps = state.turnSteps;
    s.turns = countUserTurns();
    saveStats();
    renderMetrics();
  }
  function startMetricsTicker() {
    if (state.metricsTimer) return;
    state.metricsTimer = setInterval(function () {
      if (!state.streaming) return;
      renderMetrics();
    }, 500);
  }
  function stopMetricsTicker() {
    if (!state.metricsTimer) return;
    clearInterval(state.metricsTimer);
    state.metricsTimer = null;
  }
  // 流式期间按字符估算已输出的 token，配合计时器给出「正在生成」的实时速度。
  function noteLiveTokens(text) {
    if (!text) return;
    if (!state.liveStartAt) state.liveStartAt = Date.now();
    state.liveTokens += estTokens(text);
  }

  /* ---------- 5) 流式对话（fetch + ReadableStream 解析 SSE） ---------- */
  function liveBlockForTool(id) {
    var found = null;
    state.live.blocks.forEach(function (b) { if (b.type === 'tool' && b.tc.id === id) found = b; });
    return found;
  }
  /// 采纳一条排队消息时，把当前 live 气泡定格成一条普通 assistant 消息，后续迭代的
  /// 输出落到新的气泡里。否则整轮（迭代 0 的回答 + 被采纳的 user 消息 + 迭代 1 的回答）
  /// 会挤在同一个 live 气泡里，模型「先回答后收到消息」的时序就看不出来了。
  function freezeLiveBeforeQueued() {
    var live = state.live;
    if (!live) return;
    var hasContent = !!(live.reasoning || live.blocks.length);
    if (!hasContent) return;
    state.messages.push({
      role: 'assistant', content: live.content || '', reasoning: live.reasoning || '',
      blocks: live.blocks.slice()
    });
    // 工具输出在 live 气泡里是旁路的；定格后要变成 tool 消息，renderChat 才能
    // 重新挂回对应的工具行。
    Object.keys(live.outputs || {}).forEach(function (id) {
      var o = live.outputs[id] || {};
      state.messages.push({
        role: 'tool', tool_call_id: id, content: o.output || '',
        ok: o.ok !== false, elapsed_ms: o.elapsed_ms, truncated: !!o.truncated
      });
    });
    state.live = { role: 'assistant', reasoning: '', blocks: [], outputs: {} };
    state.liveNode = null;
  }

  function handleEvent(ev) {
    if (ev.type === 'ping') return;                              // 心跳：保持连接存活即可
    // 每一帧都带序号：断线重连时服务端按它回放，客户端靠它知道自己看到了哪里。
    if (typeof ev.seq === 'number') state.turnSeq = ev.seq;
    if (ev.type === 'resync') { resyncFromServer(); return; }
    if (!state.live) return;
    if (ev.type === 'start') {
      if (ev.session_id && (!state.session || state.session.id !== ev.session_id)) {
        state.session = { id: ev.session_id, title: '新会话' };
        saveLS();
        renderCrumbs();
      }
      // 接回一个正在跑的轮次时，落盘消息里已经有这一轮前几次迭代的结果，而回放会把
      // 它们重新画进 live 气泡：先截掉重复的那截。正常发送时两者相等，是空操作。
      if (typeof ev.messages_before === 'number' && state.messages.length > ev.messages_before) {
        state.messages = state.messages.slice(0, ev.messages_before);
      }
      state.stats = emptyStats();                 // 新一轮：指标栏先清空，等 metrics 到达再填
      state.liveTokens = 0; state.liveStartAt = 0;
      state.turnSteps = 0; state.turnStartAt = Date.now();
      renderMetrics();
      renderChat();
      return;
    }
    if (ev.type === 'artifacts') {
      // agent 产出了文件：立刻把下载链接补到这一轮的回答下面。
      setArtifacts(ev.items, ev.truncated);
      return;
    }
    if (ev.type === 'todo') {
      // agent 改动了任务列表：实时更新进度条，不打断正在生成的回答。
      setTodos(ev.items);
      return;
    }
    if (ev.type === 'tool_images') {
      // toolkit_load_image 把图片作为一条 follow-up user 消息附加进会话（wire 协议
      // 只允许 user 消息带图片）。这里同步插进对话，图片走 /raw 显示；本轮结束时
      // 回读会话还会再确认一次。
      if (ev.content) {
        freezeLiveBeforeQueued();
        state.messages.push({
          role: 'user', content: ev.content,
          images: ev.images || [],
          created_at: new Date().toISOString()
        });
        renderChat(); scrollMaybe(true);
      }
      return;
    }
    if (ev.type === 'queue_adopted') {
      // 服务端在两次迭代之间采纳了队首消息：从排队条里移除，并把它作为一条普通
      // 用户消息插进对话（本轮结束后的会话回读会再确认一次）。
      // queueDone 记住已经处理的 id：POST /api/chat/queue 的响应可能比这条 SSE
      // 事件晚到，晚到时不能再补一个永远等不到采纳的排队条。
      if (ev.id) state.queueDone[ev.id] = true;
      state.queue = state.queue.filter(function (q) { return q.id !== ev.id; });
      renderQueue();
      if (ev.content) {
        freezeLiveBeforeQueued();
        state.messages.push({
          role: 'user', content: ev.content,
          created_at: ev.created_at || new Date().toISOString()
        });
        renderChat(); scrollMaybe(true);
        toast('已采纳排队消息，继续生成');
      }
      return;
    }
    if (ev.type === 'queue_unconsumed') {
      // 轮次在采纳之前就结束了（停止 / 报错 / 迭代上限）：先记下来，等流关闭后
      // 统一放回输入框，避免和用户正在敲的内容交错。
      var unconsumed = (ev.items || []);
      var dead = {};
      unconsumed.forEach(function (it) {
        if (!it) return;
        if (it.id) { dead[it.id] = true; state.queueDone[it.id] = true; }
        if (it.content) state.queueRestore.push(it.content);
      });
      state.queue = state.queue.filter(function (q) { return !dead[q.id]; });
      renderQueue();
      return;
    }
    if (ev.type === 'reasoning_delta') {
      state.live.reasoning += ev.text || '';
      noteLiveTokens(ev.text);
      scheduleLiveRender(); return;
    }
    if (ev.type === 'content_delta') {
      var last = state.live.blocks[state.live.blocks.length - 1];
      if (!last || last.type !== 'md') { last = { type: 'md', text: '' }; state.live.blocks.push(last); }
      last.text += ev.text || '';
      noteLiveTokens(ev.text);
      scheduleLiveRender(); return;
    }
    if (ev.type === 'tool_call' || ev.type === 'tool_start') {
      if (ev.type === 'tool_start') { state.turnSteps++; renderMetrics(); }
      var block = liveBlockForTool(ev.id);
      if (block) block.tc.status = 'running';
      else state.live.blocks.push({ type: 'tool', tc: { id: ev.id, name: ev.name, arguments: ev.arguments, status: 'running' } });
      scheduleLiveRender(); return;
    }
    if (ev.type === 'tool_result') {
      state.live.outputs[ev.id] = { output: ev.output, ok: ev.ok !== false, elapsed_ms: ev.elapsed_ms, truncated: !!ev.truncated };
      var b2 = liveBlockForTool(ev.id);
      if (b2) b2.tc.status = ev.ok === false ? 'fail' : 'ok';
      scheduleLiveRender(); return;
    }
    if (ev.type === 'tool_args_delta') { noteLiveTokens(ev.text); return; }   // 工具调用参数也是输出 token
    if (ev.type === 'iteration_end') {
      // 先清掉本轮的实时估算窗口再套用服务端数字：窗口按轮重置，工具执行的时间
      // 不会被算进生成时间，实时值与 done 的口径才一致。
      state.liveTokens = 0; state.liveStartAt = 0;
      applyMetrics(ev.metrics, ev.usage);
      return;
    }
    if (ev.type === 'error') {
      state.ended = true;
      state.streamError = '出错了：' + (ev.message || '未知错误') + (ev.detail ? '\n' + ev.detail : '');
      scheduleLiveRender(); return;
    }
    if (ev.type === 'done' || ev.type === 'stopped') {
      state.ended = true;
      state.liveTokens = 0;                    // 服务端数字到了，丢弃本地估算
      if (ev.trajectory_id) state.lastTrajectoryId = ev.trajectory_id;
      if (ev.type === 'done') applyMetrics(ev.metrics, ev.usage);
      renderMetrics();
      notifyFinished();                         // 后台跑完：让标签页闪一下
      // 节流可能把最后一段文字留在「该渲染但还没渲染」的状态：本轮结束，补一次。
      scheduleLiveRender();
    }
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
    updatePlaceholder();
    updateComposer();
    // 「进行中」标记只属于**没在跟随**的会话（见 renderSessions）：跟随状态一变，
    // 侧栏那一行就得重画，否则接上之后标记还挂着。
    renderSessions();
    if (on) {
      startMetricsTicker();
    } else {
      stopMetricsTicker();
      state.liveTokens = 0; state.liveStartAt = 0;
      renderMetrics();
    }
    // The chip stays clickable: the menu is still useful (it shows what exists)
    // and the refusal is explained on selection. The tooltip states the reason
    // up front.
    $('ws-chip').title = '切换工作区，或添加一个新的项目目录（正在进行的轮次会在服务端继续）';
  }
  function updatePlaceholder() {
    $('input').placeholder = state.streaming ? QUEUE_PLACEHOLDER
      : (state.fork && state.session && state.fork.sid === state.session.id
        ? FORK_PLACEHOLDER : INPUT_PLACEHOLDER);
  }
  function finalizeStream() {
    var sid = state.session ? state.session.id : null;
    cancelLiveRender();
    state.live = null; state.liveNode = null; state.liveView = null; state.ended = false;
    if (state.stopTimer) { clearTimeout(state.stopTimer); state.stopTimer = null; }
    state.abort = null;
    setStreaming(false);
    // 本轮结束时还没被采纳的消息：服务端已经把它们从队列里摘掉，这里放回输入框，
    // 而不是留在看不见的地方。停止/报错时同样成立——用户按了停止，就不该自动
    // 再发一轮，但打过的字必须还给他。
    if (state.queueRestore.length) {
      var restoredCount = state.queueRestore.length;
      var restored = state.queueRestore.join('\n\n');
      state.queueRestore = [];
      var ta = $('input');
      ta.value = ta.value.replace(/\s+$/, '');
      ta.value = ta.value ? (ta.value + '\n\n' + restored) : restored;
      autoGrow();
      state.queue = [];
      renderQueue();
      toast('本轮已结束，' + restoredCount + ' 条排队消息已放回输入框');
    }
    // 连接中断或事件丢失时，以服务端队列为准对账一次：已经不在队列里的条目
    // 不能继续挂在输入框上方（多半已经在另一条路径上被采纳或退回了）。
    if (sid && state.queue.length) {
      api('api/chat/queue?session_id=' + encodeURIComponent(sid)).then(function (d) {
        var live = {};
        (d.items || []).forEach(function (it) { if (it && it.id) live[it.id] = true; });
        var next = state.queue.filter(function (q) { return live[q.id]; });
        if (next.length !== state.queue.length) { state.queue = next; renderQueue(); }
      }, function () {});
    }
    var finish = function () { renderChat(); renderMetrics(); scrollMaybe(); };
    if (!sid) { finish(); return; }
    // 结束后重新拉取，保证渲染状态与服务器一致；轨迹是这一轮结束后才落盘的，
    // 所以必须和会话一起重新取回来才能挂到对应的 assistant 消息上。
    api('api/sessions/' + encodeURIComponent(sid)).then(function (d) {
      if (state.session && state.session.id === sid) {
        state.session = d.session || state.session;
        state.messages = d.messages || [];
        renderCrumbs();          // 标题由服务端在第一轮生成，回读后才拿得到
      }
    }, function () { }).then(loadTrajectories).then(function () {
      attachTrajectories();
    }).then(finish, finish);
    loadSessions();
  }
  /* ---------- 粘贴图片 ---------- */
  // 剪贴板里的图片读成 data URL 暂存在 state.attachments，发送时随消息一起提交；
  // 服务端按内容嗅探真实类型、限制大小，并落到工作区的 artifacts 目录。
  var ATTACH_MAX = 4;
  var ATTACH_MAX_BYTES = 8 * 1024 * 1024;

  function renderAttachments() {
    var strip = $('attach-strip');
    clear(strip);
    if (!state.attachments.length) { strip.classList.add('hidden'); return; }
    strip.classList.remove('hidden');
    state.attachments.forEach(function (a) {
      var item = el('div', 'attach-item');
      var img = document.createElement('img');
      img.src = a.dataUrl;
      img.alt = a.name || '粘贴的图片';
      img.title = (a.name || '图片') + ' · ' + fmtBytes(a.bytes);
      item.appendChild(img);
      var del = el('button', 'attach-del', '×');
      del.type = 'button';
      del.title = '移除这张图片';
      del.onclick = function () {
        state.attachments = state.attachments.filter(function (x) { return x.id !== a.id; });
        renderAttachments();
      };
      item.appendChild(del);
      strip.appendChild(item);
    });
  }

  /* ---------- 排队消息（会话进行中按 Enter） ---------- */
  // 一轮可能跑很久（长工具、多轮迭代）。这时按 Enter 不再被忽略或直接开第二轮，
  // 而是把消息放进该轮自己的队列：agent loop 每轮采纳一条，采纳前随时可以取消。
  // 队列随轮次存活——轮次结束仍未采纳的会被服务端退回（queue_unconsumed），
  // 这里再放回输入框，绝不悄悄丢掉用户已经打出来的字。
  var INPUT_PLACEHOLDER = '输入消息，Enter 发送，Shift+Enter 换行（可直接粘贴图片）';
  var QUEUE_PLACEHOLDER = '会话进行中：Enter 排队（下一轮采纳，采纳前可取消），Shift+Enter 换行';
  var FORK_PLACEHOLDER = '分叉模式：Enter 会新建一个从选中轮次分出的会话，当前会话保持原样';

  function renderQueue() {
    var strip = $('queue-strip');
    clear(strip);
    if (!state.queue.length) { strip.classList.add('hidden'); return; }
    strip.classList.remove('hidden');
    state.queue.forEach(function (q, i) {
      var row = el('div', 'queue-item');
      row.title = q.content || '';
      row.appendChild(el('span', 'q-tag', '排队 ' + (i + 1)));
      row.appendChild(el('span', 'q-text', q.content || ''));
      var del = btn('q-cancel', '×', function () { cancelQueued(q.id); });
      del.title = '取消这条排队消息（采纳前都可以取消）';
      del.setAttribute('aria-label', '取消排队');
      row.appendChild(del);
      strip.appendChild(row);
    });
  }

  function clearQueue() {
    state.queue = [];
    state.queueRestore = [];
    state.queueDone = {};
    renderQueue();
  }

  /// Enter while a turn is running. Returns true when the key press was handled,
  /// so the caller never falls through to starting a second turn.
  function queueMessage() {
    var ta = $('input');
    var text = ta.value.replace(/\s+$/, '');
    if (!state.streaming || !text) return false;
    if (state.attachments.length) {
      toast('排队消息暂不支持图片：等本轮结束后再发送');
      return true;
    }
    if (!state.session) { toast('请先选择工作区'); return true; }
    api('api/chat/queue', {
      method: 'POST',
      body: { session_id: state.session.id, message: text }
    }).then(function (res) {
      ta.value = ''; autoGrow();
      var q = (res && res.queued) || {};
      // 这轮可能在响应回来之前就已经采纳了它（SSE 走另一条连接），别再补一条。
      if (!q.id || !state.queueDone[q.id]) {
        state.queue.push({
          id: q.id || '', content: q.content || text, created_at: q.created_at || ''
        });
      }
      renderQueue();
      toast('已排队，agent 下一轮采纳；采纳前可取消');
    }, function (e) {
      toast('排队失败：' + (e && e.message ? e.message : String(e)));
    });
    return true;
  }

  function cancelQueued(id) {
    if (!id || !state.session) return;
    api('api/chat/queue/cancel', {
      method: 'POST',
      body: { session_id: state.session.id, id: id }
    }).then(function (res) {
      state.queue = state.queue.filter(function (q) { return q.id !== id; });
      renderQueue();
      toast(res && res.cancelled ? '已取消排队' : '这条消息已经被采纳，无法取消');
    }, function (e) {
      toast('取消失败：' + (e && e.message ? e.message : String(e)));
    });
  }

  function addAttachment(file) {
    if (!file || !/^image\//.test(file.type || '')) return;
    if (state.attachments.length >= ATTACH_MAX) {
      toast('一条消息最多 ' + ATTACH_MAX + ' 张图片');
      return;
    }
    if (file.size > ATTACH_MAX_BYTES) {
      toast('图片太大（' + fmtBytes(file.size) + '），上限 ' + fmtBytes(ATTACH_MAX_BYTES));
      return;
    }
    var reader = new FileReader();
    reader.onload = function () {
      var url = String(reader.result || '');
      var comma = url.indexOf(',');
      state.attachments.push({
        id: 'a' + Date.now() + '_' + Math.random().toString(36).slice(2, 7),
        dataUrl: url, mime: (url.slice(5, comma) || file.type || '').replace(';base64', ''),
        name: file.name || 'pasted-image', bytes: file.size
      });
      renderAttachments();
    };
    reader.onerror = function () { toast('读取图片失败'); };
    reader.readAsDataURL(file);
  }

  /// Collect images out of a paste/drop event. `items` is preferred because
  /// some platforms put screenshots there with an empty `files` list.
  function filesFromDataTransfer(dt) {
    var out = [];
    if (!dt) return out;
    if (dt.files && dt.files.length) {
      Array.prototype.forEach.call(dt.files, function (f) { out.push(f); });
    } else if (dt.items) {
      Array.prototype.forEach.call(dt.items, function (it) {
        if (it.kind === 'file') { var f = it.getAsFile(); if (f) out.push(f); }
      });
    }
    return out;
  }

  /// Read one SSE response body and feed every frame to handleEvent. Shared by
  /// "send a message" and "re-attach to a turn that is already running".
  function readTurnStream(res) {
    if (!res.ok) {
      return res.text().then(function (t) {
        var msg = 'HTTP ' + res.status;
        try {
          var j = JSON.parse(t);
          if (j && j.error && (j.error.message || j.error.code)) msg = j.error.message || j.error.code;
        } catch (e) { if (t) msg = t.slice(0, 300); }
        throw new Error(msg);
      });
    }
    if (!res.body) throw new Error('HTTP ' + res.status);
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
  }

  /// 接回一个还在跑的轮次：服务端从 seq 1 开始回放这一轮的全部事件，然后继续直播。
  /// 关掉页面、合上笔记本、换台机器打开，都不该让这一轮看起来"断在半句话"。
  /// 回放是完整的（不是从断点续），所以 live 气泡从零重建，和一直开着页面看到的一样。
  function attachTurn(sid) {
    state.turnSeq = 0;
    state.live = { role: 'assistant', reasoning: '', blocks: [], outputs: {} };
    state.ended = false; state.streamError = null; state.stickBottom = true;
    state.turnStartAt = Date.now();
    renderChat(); scrollMaybe(true); setStreaming(true);
    restoreQueue(sid);
    var ctrl = new AbortController();
    state.abort = ctrl;
    fetch(apiUrl('api/chat/attach'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sid, since: 0 }),
      signal: ctrl.signal
    }).then(function (res) {
      if (res.ok) state.attachRetries = 0;
      return readTurnStream(res);
    }).then(finalizeStream, function (err) {
      finalizeStream();
      if (err && err.name === 'AbortError') return;
      // 连接断了不等于那一轮结束了：过一会儿再问一次服务端，还在跑就再接回去。
      if (state.attachRetries < 3) {
        state.attachRetries++;
        setTimeout(function () {
          if (!state.streaming && state.session && state.session.id === sid) maybeAttach(sid);
        }, 2000);
      }
    });
  }

  /// 服务端上所有还在跑的轮次，最近开始的在前。可能是别的机器、别的标签页发起的。
  function runningTurns() {
    return api('api/chat/turns').then(function (d) {
      var t = (d && d.turns) || [];
      t.sort(function (a, b) {
        return String(b.started_at || '').localeCompare(String(a.started_at || ''));
      });
      return t;
    }, function () { return []; });
  }

  /// 这个会话有没有正在跑的轮次？有就接回去。打开会话、标签页重新可见时都会问一次。
  function maybeAttach(sid) {
    if (!sid || state.streaming) return Promise.resolve();
    return api('api/chat/turn?session_id=' + encodeURIComponent(sid)).then(function (d) {
      var running = !!(d && d.running);
      if (!running) {
        // 那一轮已经结束：侧栏上的「进行中」标记也该消失了。
        if (state.detached === sid) { state.detached = null; renderSessions(); }
        return;
      }
      if (state.detached === sid) return;      // 你自己离开的：不要又把你拽回去
      if (!state.session || state.session.id !== sid) return;   // 用户已经切走了
      attachTurn(sid);
    }, function () {});
  }

  /// 排队条跟着一起回来：那些消息还挂在服务端的队列里，等这一轮采纳。
  function restoreQueue(sid) {
    return api('api/chat/queue?session_id=' + encodeURIComponent(sid)).then(function (d) {
      if (!state.session || state.session.id !== sid) return;
      state.queue = (d.items || []).filter(function (it) { return it && it.id; });
      renderQueue();
    }, function () {});
  }

  /// 回放窗口溢出（一轮输出超过服务端 8MB 上限）时的兜底：重新拉一次会话，清空 live
  /// 气泡，之后的增量继续往上接。已经生成的那半截看不到了，但落盘的内容一条不少，
  /// 这一轮结束时还会再对账一次。
  function resyncFromServer() {
    var sid = state.session ? state.session.id : null;
    if (!sid) return;
    api('api/sessions/' + encodeURIComponent(sid)).then(function (d) {
      if (!state.session || state.session.id !== sid) return;
      state.messages = d.messages || [];
      state.live = { role: 'assistant', reasoning: '', blocks: [], outputs: {} };
      state.liveNode = null;
      renderChat(); scrollMaybe(true);
      toast('这轮输出太长，已按服务端记录重新同步');
    }, function () {});
  }

  function sendMessage() {
    if (state.streaming) return;
    var ta = $('input');
    var text = ta.value.replace(/\s+$/, '');
    var images = state.attachments.slice();
    if (!text && !images.length) return;
    if (!state.workspace) { toast('请先选择工作区'); return; }
    ta.value = ''; autoGrow();
    // 队列属于上一轮：新一轮开始时不可能还有可被采纳的旧条目。
    state.queue = []; state.queueDone = {}; renderQueue();
    state.turnSeq = 0; state.attachRetries = 0;
    // 你之前离开过这一轮（它还在服务端跑）：这条消息会排进它的队列（不是顶掉），
    // 之后跟着它一起直播，所以说清楚再发。
    if (state.detached && state.session && state.detached === state.session.id) {
      state.detached = null;
      renderSessions();
      toast('上一轮仍在进行：这条消息会排进它的队列，然后一起看直播');
    }

    // 分叉点只对「当前会话」有效：先把消息要去的会话建出来，再发进去。分叉
    // 失败就什么都不发（输入和图片还回输入框），绝不悄悄发进用户正想离开的会话。
    var fork = state.fork && state.session && state.fork.sid === state.session.id ? state.fork : null;
    var forkFailed = false;
    var ready;
    if (fork) {
      ready = api('api/sessions/' + encodeURIComponent(fork.sid) + '/fork',
        { method: 'POST', body: { message_index: fork.mid } })
        .then(function (d) {
          if (!d || !d.session || !d.session.id) throw new Error('服务端没有返回新会话');
          state.session = d.session;
          setTopicUrl(d.session.id); setTabSession(d.session.id);   // 分叉出来的是新主题
          state.messages = d.messages || [];
          state.live = null; state.liveNode = null; state.liveView = null;
          state.streamError = null; state.ended = false; state.stickBottom = true;
          clearQueue();
          state.trajectory = []; state.trajFull = {}; state.lastTrajectoryId = null;
          clearForkPoint(true);          // 分叉已经落地：别让它影响再下一条
          saveLS();
          renderCrumbs(); renderSessions(); renderChat(); renderMetrics();
          loadSessions();
          toast('已从第 ' + fork.ord + ' 条分叉：' + (state.session.title || ''));
          return state.session;
        }, function (e) {
          forkFailed = true;
          ta.value = text; autoGrow();
          state.attachments = images;
          renderAttachments();
          toast('分叉失败：' + e.message);
          throw e;
        });
    } else if (state.session) {
      ready = Promise.resolve(state.session);
    } else {
      ready = api('api/workspaces/' + encodeURIComponent(state.workspace.id) + '/sessions',
        { method: 'POST', body: { title: text.slice(0, 24) || '图片' } })
        .then(function (s) {
          state.session = s;
          state.draft = false;                      // 草稿落地成了真正的会话
          setTopicUrl(s.id); setTabSession(s.id);   // 第一条消息落地 = 这个主题有地址了
          saveLS(); loadSessions(); return s;
        });
    }

    // 缩略图条立刻清空；乐观消息自己带 data URL，回读会话时换成服务端路径。
    state.attachments = [];
    renderAttachments();

    ready.then(function (s) {
      state.messages.push({
        role: 'user', content: text, created_at: new Date().toISOString(),
        images: images.map(function (a) { return { path: '', mime: a.mime, dataUrl: a.dataUrl }; })
      });
      state.live = { role: 'assistant', reasoning: '', blocks: [], outputs: {} };
      state.ended = false; state.streamError = null; state.stickBottom = true;
      state.stats = emptyStats(); state.liveTokens = 0; state.liveStartAt = 0;
      state.turnSteps = 0; state.turnStartAt = Date.now();
      renderMetrics();
      renderChat(); scrollMaybe(true); setStreaming(true);
      var ctrl = new AbortController();
      state.abort = ctrl;
      return fetch(apiUrl('api/chat'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          workspace_id: state.workspace.id, session_id: s.id, message: text,
          images: images.length ? images.map(function (a) { return a.dataUrl; }) : undefined,
          provider: state.model ? state.model.provider : undefined,
          model: state.model ? state.model.model : undefined
        }),
        signal: ctrl.signal
      }).then(function (res) {
        // 服务端只有在**新开**一轮时才回 SSE。回 JSON 说明这个会话已经有一轮在跑，
        // 消息被排进了它的队列（两个窗口共用一个主题时的默认行为）。
        var ctype = res.headers.get('Content-Type') || '';
        if (res.ok && ctype.indexOf('text/event-stream') < 0) {
          return res.json().then(function (d) { return { queued: d || {} }; });
        }
        return readTurnStream(res).then(function () { return { queued: null }; });
      });
    }).then(function (r) {
      if (!r || !r.queued) { finalizeStream(); return; }
      var d = r.queued;
      var queuedSid = d.session_id || (state.session ? state.session.id : '');
      var lastMsg = state.messages[state.messages.length - 1];
      if (lastMsg && lastMsg.role === 'user' && lastMsg.content === text) state.messages.pop();
      state.live = null; state.liveNode = null; state.liveView = null;
      state.ended = false;
      setStreaming(false);
      if (d.queued && d.queued.id && !state.queueDone[d.queued.id]) {
        state.queue.push({
          id: d.queued.id, content: d.queued.content || text,
          created_at: d.queued.created_at || ''
        });
      }
      renderQueue(); renderChat();
      toast('这个会话的一轮还在跑：消息已排队，agent 下一步采纳（两个窗口看同一份输出）');
      // 接上那一轮：这个窗口从"只看历史"变成跟着别人正在跑的直播。
      return maybeAttach(queuedSid);
    }, function (err) {
      if (forkFailed) return;         // 分叉失败时输入已还回，别再报一次流错误
      if (!err || err.name !== 'AbortError') {
        state.streamError = '请求失败：' + (err && err.message ? err.message : String(err));
        loadHealth(true).catch(function () {});   // 请求都发不出去：立刻如实反映
      }
      finalizeStream();                                          // 仍然重拉会话，保持与服务端一致
    });
  }
  /// 停止跟随这一轮，但**不打断**它。任务可能是另一台机器（或另一个标签页）发起的，
  /// 你也可能只是想先去看别的工作区：断开本地这条 SSE 就够了，服务端照跑到底，
  /// 回到这个会话时会重新接上。和「停止」的区别就在这里——停止是取消所有人的这一轮。
  function detachStream() {
    var sid = state.session ? state.session.id : null;
    if (sid) state.detached = sid;
    if (state.stopTimer) { clearTimeout(state.stopTimer); state.stopTimer = null; }
    if (state.abort) state.abort.abort();      // AbortError 不报错，finalizeStream 收尾
    renderSessions();                          // 侧栏标出「还在跑，你已离开」
    toast('已停止跟随；这一轮仍在服务端继续，点会话可再接回');
  }

  function stopStream() {
    var sid = state.session ? state.session.id : null;
    if (sid) api('api/chat/stop', { method: 'POST', body: { session_id: sid } }).catch(function () {});
    state.stopTimer = setTimeout(function () { if (state.streaming && state.abort) state.abort.abort(); }, 3000);
  }
  function updateComposer() {
    var b = $('send');
    b.classList.toggle('stop', state.streaming);
    // 「离开」只在跟随一轮时出现：停止跟随但不打断它。
    var d = $('detach');
    if (d) d.classList.toggle('hidden', !state.streaming);
    b.title = state.streaming ? '停止生成（Enter 排队，采纳前可取消）' : '发送';
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
  /* ---------- 服务状态：只信任「刚刚」的事实 ----------
     以前 /api/health 只在启动时查一次，之后再也不刷新。于是任何一次瞬时失败
     （页面打开时服务还在重启、请求撞在一起、后台标签页被系统挂起）都会把
     「未连接」永久钉在右上角——即使之后每个请求都成功。现在：
       · 连续失败两次才显示未连接，单次抖动不算；
       · 前台每 10s 轮询一次，切回标签页时立刻复核；
       · 任意请求成功即触发复核（服务刚恢复时不用等下一个轮询周期）；
       · 状态本身可点击，点一下马上重查。 */
  var healthTimer = null;
  var healthFailures = 0;
  var healthRecheck = 0;
  var statusShown = '';          // '' | 'ok' | 'setup' | 'down' | 'wait'

  function applyStatus(kind, text) {
    if (statusShown === kind) return;      // 同样的状态不重复写 DOM
    statusShown = kind;
    var dot = $('status-dot');
    dot.className = 'status-dot ' + (kind === 'ok' ? 'ok' : kind === 'wait' ? 'wait' : 'bad');
    $('status-text').textContent = text;
    $('status').title = kind === 'down'
      ? '无法连接到本地服务（点击重新检测）'
      : '服务状态（点击重新检测）';
    var banner = $('banner');
    if (kind === 'setup') {
      $('banner-text').textContent = '尚未配置 provider.yaml，上传后才能开始对话。';
      banner.classList.remove('hidden');
    } else if (kind === 'down') {
      banner.classList.remove('hidden');
    } else if (kind === 'ok') {
      banner.classList.add('hidden');
    }
  }
  function markOnline() {
    var needsSetup = state.health && state.health.config_loaded === false;
    if (needsSetup) {
      applyStatus('setup', '未配置');
    } else {
      applyStatus('ok', '已连接' +
        (state.health && state.health.version ? ' v' + state.health.version : ''));
    }
  }
  function markOffline(err) {
    if (err && err.message) $('banner-text').textContent = '无法连接到本地服务：' + err.message;
    applyStatus('down', '未连接');
  }
  function markChecking() { applyStatus('wait', '连接中…'); }

  /// A non-health request succeeded. If the pill says "未连接", re-verify now
  /// (debounced) instead of waiting up to a full poll interval.
  function noteRequestOk() {
    if (statusShown !== 'down' || healthRecheck) return;
    healthRecheck = setTimeout(function () {
      healthRecheck = 0;
      if (statusShown === 'down') loadHealth(true).catch(function () {});
    }, 0);
  }

  function startHealthTimer() {
    if (healthTimer) return;
    // Clicking back into the window is as good as switching to the tab: the hint
    // has been seen, so it may stop blinking.
    window.addEventListener('focus', function () { stopTabBlink(); });
    healthTimer = setInterval(function () {
      if (document.hidden) return;              // 后台标签页不需要轮询
      loadHealth(false).catch(function () {});
    }, 10000);
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) return;
      stopTabBlink();                           // 回到这个标签页就不闪了
      loadHealth(false).catch(function () {});
      // 标签页回来时连接可能已经断了（合盖、断网、被系统挂起）：那一轮如果还在
      // 服务端跑着，就接回去继续直播，而不是让用户以为它停了。
      if (state.session && !state.streaming) maybeAttach(state.session.id);
    });
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
      var info = p.model_info || {};
      (p.models || []).forEach(function (m) {
        state.models.push({ provider: p.name, model: m, max: num((info[m] || {}).max_context_tokens) });
      });
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

  // ---- 订阅额度 ------------------------------------------------------------
  // provider.yaml 里写了 `supports_usage: true` 的 provider 才有额度接口
  // （MiniMax M Plan：一个 5 小时窗口 + 一个周窗口）。服务端拿着 provider 自己的
  // key 去查，浏览器永远看不到 key。
  var USAGE_REFRESH_MS = 60000;
  function usageProvider() {
    var ps = (state.providers && state.providers.providers) || [];
    var name = state.model && state.model.provider;
    for (var i = 0; i < ps.length; i++) if (ps[i].name === name) return ps[i];
    return null;
  }
  function fmtRemain(ms) {
    if (ms == null || ms < 0) return '';
    var s = Math.floor(ms / 1000);
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    if (h >= 24) return Math.floor(h / 24) + ' 天 ' + (h % 24) + ' 小时';
    if (h > 0) return h + ' 小时 ' + m + ' 分';
    return m + ' 分';
  }
  function fmtClock(ms) {
    if (!ms) return '';
    var d = new Date(ms);
    if (isNaN(d.getTime())) return '';
    var p = function (x) { return (x < 10 ? '0' : '') + x; };
    return p(d.getHours()) + ':' + p(d.getMinutes());
  }
  /// 窗口还剩多久：provider 给的是「查询那一刻」的剩余量，这里按时间差扣掉。
  function windowRemain(w) {
    if (!w || !w.remains_ms) return 0;
    var left = w.remains_ms - (Date.now() - (state.usageAt || Date.now()));
    return left > 0 ? left : 0;
  }
  /// 打开提供方的额度控制台（需要人登录），新标签页。
  function openConsole(url) {
    if (!url) return;
    var w = window.open(url, '_blank', 'noopener');
    if (!w) window.location.href = url;   // 弹窗被拦了就直接跳
  }
  function loadUsage() {
    var p = usageProvider();
    var chip = $('usage-chip');
    if (!p || (!p.supports_usage && !p.usage_console_url)) {
      state.usage = null; state.usageError = ''; state.usageAt = 0;
      if (chip) chip.classList.add('hidden');
      renderUsage();
      return Promise.resolve();
    }
    if (!p.supports_usage) {
      // 这个提供方的额度用 key 查不到（要登录控制台）：只给链接，不发请求。
      state.usage = null; state.usageError = ''; state.usageAt = 0;
      renderUsage();
      return Promise.resolve();
    }
    if (chip) { chip.classList.remove('hidden'); chip.classList.add('loading'); }
    return api('api/providers/' + encodeURIComponent(p.name) + '/usage').then(function (d) {
      state.usage = d; state.usageError = ''; state.usageAt = Date.now();
    }, function (e) {
      state.usage = null;
      state.usageError = (e && e.message) || '查询失败';
    }).then(function () {
      if (chip) chip.classList.remove('loading');
      renderUsage();
    });
  }
  function renderUsage() {
    var chip = $('usage-chip'), text = $('usage-chip-text');
    if (!chip || !text) return;
    var p = usageProvider();
    chip.classList.remove('low', 'err');
    chip.classList.toggle('link', !!(p && !p.supports_usage && p.usage_console_url));
    if (!p || (!p.supports_usage && !p.usage_console_url)) {
      chip.classList.add('hidden');
      return;
    }
    if (!p.supports_usage) {
      // 只有控制台链接：徽标变成「去控制台看」，不假装能报数字。
      chip.classList.remove('hidden');
      text.textContent = '额度 ↗';
      chip.title = '这个提供方的额度用 key 查不到（要登录控制台），点开自己看';
      return;
    }
    if (state.usageError) {
      text.textContent = '额度 ?';
      chip.classList.add('err');
      chip.title = '额度查询失败：' + state.usageError;
      return;
    }
    var u = state.usage, w = u && u.window;
    if (!w || !w.present) {
      text.textContent = '额度 —';
      chip.title = '该提供方没有返回额度信息';
      return;
    }
    var pct = (w.remaining_percent == null || w.remaining_percent < 0) ? -1 : Math.round(w.remaining_percent);
    text.textContent = '额度 ' + (pct >= 0 ? pct + '%' : '—');
    if (pct >= 0 && pct <= 20) chip.classList.add('low');
    var lines = ['5 小时窗口剩余 ' + (pct >= 0 ? pct + '%' : '未知')];
    if (w.remains_ms) {
      lines.push('窗口 ' + fmtRemain(windowRemain(w)) + '后重置（' + fmtClock(w.end_ms) + '）');
    }
    if (u.weekly && u.weekly.present && u.weekly.remaining_percent >= 0) {
      lines.push('周窗口剩余 ' + Math.round(u.weekly.remaining_percent) + '%');
    }
    if (state.usageAt) lines.push('查询于 ' + new Date(state.usageAt).toLocaleTimeString());
    chip.title = lines.join('\n');
  }
  /// 设置页里按 provider 单独查一次（不只当前 provider）。
  function queryUsageFor(p, out) {
    out.textContent = '查询中…';
    return api('api/providers/' + encodeURIComponent(p.name) + '/usage').then(function (d) {
      var w = (d && d.window) || {};
      var parts = [];
      if (w.present && w.remaining_percent >= 0) parts.push('5 小时窗口剩余 ' + Math.round(w.remaining_percent) + '%');
      if (d && d.weekly && d.weekly.present && d.weekly.remaining_percent >= 0) {
        parts.push('周窗口剩余 ' + Math.round(d.weekly.remaining_percent) + '%');
      }
      out.textContent = parts.join(' · ') || '该 key 没有订阅额度';
    }, function (e) {
      out.textContent = '查询失败：' + ((e && e.message) || '未知错误');
    });
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
          loadUsage();
        });
        if (active) it.appendChild(el('span', 'check', '✓'));
        dd.appendChild(it);
      });
    });
  }
  /// 浏览器标签页 / 窗口标题：带上当前工作区，同时在开着的多个页面之间一眼区分。
  /// 工作区名放在最前面，标签页被截断时先保住最能区分的那一段。
  var APP_NAME = 'TEA AGENT';

  /* ---------- 标签页闪烁：后台跑完了要让人知道 ---------- */
  // A turn that finishes while the tab is in the background is invisible: the
  // answer is there, the status pill says nothing, and the user only finds out
  // when they happen to look at the tab again. A blinking title is the oldest
  // trick there is and the only one that needs no notification permission.
  var TAB_BLINK_MS = 1000;
  var tabBlinkTimer = null;
  var tabBlinkMarked = false;
  var tabBaseTitle = APP_NAME;

  function applyTitle() {
    document.title = tabBlinkMarked ? ('\u25cf ' + tabBaseTitle) : tabBaseTitle;
  }
  function startTabBlink() {
    if (tabBlinkTimer) return;
    tabBlinkTimer = setInterval(function () {
      tabBlinkMarked = !tabBlinkMarked;
      applyTitle();
    }, TAB_BLINK_MS);
  }
  function stopTabBlink() {
    if (!tabBlinkTimer) return;
    clearInterval(tabBlinkTimer);
    tabBlinkTimer = null;
    tabBlinkMarked = false;
    applyTitle();
  }
  /// Call it when something finished that the user asked for.
  function notifyFinished() {
    // Only while the page is not in front of the user: a blinking title under
    // the eyes is noise, not a hint.
    if (!document.hidden && document.hasFocus()) return;
    startTabBlink();
  }

  // A task that finished *elsewhere* - the scheduler picked it up while this tab
  // was in the background - has to reach the tab too, otherwise the work is done
  // and nobody knows. The event stream is the one place that knows.
  var taskEventsSource = null;
  function subscribeTaskEvents() {
    if (taskEventsSource) return;
    try {
      taskEventsSource = new EventSource(apiUrl('api/events'));
    } catch (e) { return; }
    taskEventsSource.onmessage = function (ev) {
      var data = null;
      try { data = JSON.parse(ev.data); } catch (e) { return; }
      if (!data || !data.type) return;
      if (data.type === 'task_done' || data.type === 'task_failed' ||
          data.type === 'task_waiting_confirmation' || data.type === 'task_cancelled') {
        notifyFinished();
      }
    };
    taskEventsSource.onerror = function () {
      // The server restarted or the network hiccuped: close, wait, try again.
      if (taskEventsSource) { taskEventsSource.close(); taskEventsSource = null; }
      setTimeout(subscribeTaskEvents, 5000);
    };
  }

  function updateTitle() {
    var name = state.workspace ? state.workspace.name : '';
    tabBaseTitle = name ? (name + ' \u00b7 ' + APP_NAME) : APP_NAME;
    applyTitle();
  }
  function renderWorkspaceChip() {
    $('ws-chip-text').textContent = state.workspace ? state.workspace.name : '选择工作区';
    $('ws-chip').title = state.workspace ? state.workspace.path : '';
    updateTitle();
    // 面包屑曾经只在 showView() 里刷新，于是启动时（工作区/会话还没加载）画出来的
    // 「无工作区 / 新会话」会一直留着。工作区变了就重画，和标签页标题同一个时机。
    renderCrumbs();
  }
  function renderWorkspaceMenu() {
    var dd = $('ws-dropdown');
    clear(dd);
    state.workspaces.forEach(function (w) {
      var it = btn('dd-item' + (state.workspace && state.workspace.id === w.id ? ' active' : ''), w.name, function () {
        closeDropdowns();
        // 正在跟随一轮时切工作区：先离开（服务端照跑），别拦着人。
        if (!state.workspace || state.workspace.id !== w.id) selectWorkspace(w.id);
      });
      it.appendChild(el('span', 'sub', w.exists === false ? '路径不存在' : (w.sessions || 0) + ' 会话'));
      dd.appendChild(it);
    });
    dd.appendChild(el('div', 'dd-sep'));
    dd.appendChild(btn('dd-item', '＋ 添加工作区…', function () {
      closeDropdowns();
      openAddWorkspace(false);   // 只是打开一个对话框，不影响正在跑的轮次
    }));
  }
  /* 会话索引：标题 + 相对时间 + 消息条数，当前会话高亮，行内改名/删除。
     排序在 loadSessions 里做（最新的在前），这里只负责渲染。 */
  function renderSessions() {
    var box = $('session-list');
    clear(box);
    if (!state.workspace) { box.appendChild(el('div', 'empty-note', '请先选择工作区')); return; }
    if (!state.sessions.length) { box.appendChild(el('div', 'empty-note', '暂无会话，点击上方 ＋ 新建')); return; }
    state.sessions.forEach(function (s) {
      var active = state.session && state.session.id === s.id;
      var item = el('div', 'sess-item' + (active ? ' active' : ''));
      item.dataset.id = s.id;

      // 「进行中」只标在**你没在跟随**的会话上：正跟着的那一轮已经在屏幕上直播了，
      // 再挂一个标记只会让人以为有两处。
      var running = !!state.running[s.id] && !(active && state.streaming);
      var row = el('button', 'sess-row' + (active ? ' active' : ''));
      row.type = 'button';
      // 不再因为"这一页正在生成"就禁用整个列表：多主题并行时你要能随时看别的
      // 主题（服务端那一轮照跑，见 openSession 里的自动离开）。
      row.appendChild(el('span', 'sess-title', s.title || s.id));
      var meta = el('span', 'sess-meta');
      meta.appendChild(el('span', null, fmtRelTime(s.updated_at || s.created_at) || '—'));
      if (s.message_count != null) {
        meta.appendChild(el('span', null, '·'));
        meta.appendChild(el('span', null, s.message_count + ' 条'));
      }
      // 任务进度：侧栏一眼能看出哪个会话还没做完（3/7），不用点进去。
      if (s.todo_total > 0) {
        meta.appendChild(el('span', null, '·'));
        var chip = el('span', 'sess-todo' + (s.todo_done >= s.todo_total ? ' done' : ''),
          s.todo_done + '/' + s.todo_total);
        chip.title = '任务进度：' + s.todo_done + ' / ' + s.todo_total + ' 完成';
        meta.appendChild(chip);
      }
      // 服务端还在跑的那一轮：任何浏览器发起的都标出来，点一下才接上（不会自动
      // 把你从别的主题里拽走）。离开过的那一轮加一句"你已离开"。
      if (running) {
        meta.appendChild(el('span', 'sess-running',
          state.detached === s.id ? '· 进行中（你已离开）' : '· 进行中'));
      }
      row.appendChild(meta);
      row.title = (s.title || s.id) + '\n' + fmtTime(s.updated_at || s.created_at) +
        ' · ' + (s.message_count || 0) + ' 条消息\n' +
        (running ? '这一轮正在进行，点击接上直播' : '点击切换会话');
      row.onclick = function () {
        // 已经在这个会话上跟随直播：再点一下等于刷新，不打断。
        if (state.streaming && active) return;
        openSession(s.id);
      };
      item.appendChild(row);

      var tools = el('div', 'sess-tools');
      var ren = btn('sess-tool', '改名', function (e) {
        e.stopPropagation();
        if (!running) renameSessionDialog(s);
      });
      // 服务端会拒绝（409 turn_running）：worker 手里那份 Session 会把改名/删除
      // 覆盖掉。这里直接禁用，省掉一次"点了没反应"。
      ren.title = running ? '这一轮还在跑：结束或停止后再改名' : '重命名会话';
      ren.disabled = running;
      var del = btn('sess-tool danger', '删除', function (e) {
        e.stopPropagation();
        if (!running) deleteSessionDialog(s);
      });
      del.title = running ? '这一轮还在跑：结束或停止后再删除' : '删除会话';
      del.disabled = running;
      tools.appendChild(ren);
      tools.appendChild(del);
      item.appendChild(tools);
      box.appendChild(item);
    });
  }
  // 删除/改名后统一走 loadSessions()：它重排列表并保持当前会话高亮。
  function deleteSessionDialog(s) {
    var body = el('div', 'desc', '删除会话「' + (s.title || s.id) + '」及其全部消息？此操作不可撤销。');
    openDialog('删除会话', body, [
      { label: '取消', onClick: closeDialog },
      {
        label: '删除', primary: true, onClick: function () {
          closeDialog();
          api('api/sessions/' + encodeURIComponent(s.id), { method: 'DELETE' }).then(function () {
            toast('已删除会话');
            if (state.session && state.session.id === s.id) {
              state.session = null;
              state.messages = [];
              state.live = null;
              state.stats = emptyStats();
              renderChat();
              renderMetrics();
            }
            return loadSessions().then(function () {
              if (!state.session && state.sessions.length) return openSession(state.sessions[0].id);
              renderChat();
            });
          }, function (e) { toast('删除失败：' + e.message); });
        }
      }
    ]);
  }
  function renameSessionDialog(s) {
    var body = el('div');
    var input = el('input', 'ta');
    input.type = 'text';
    input.value = s.title || '';
    var f = el('div', 'field');
    f.appendChild(el('label', null, '会话标题'));
    f.appendChild(input);
    body.appendChild(f);
    openDialog('重命名会话', body, [
      { label: '取消', onClick: closeDialog },
      {
        label: '保存', primary: true, onClick: function () {
          var t = input.value.trim();
          if (!t) { toast('标题不能为空'); return; }
          closeDialog();
          api('api/sessions/' + encodeURIComponent(s.id), { method: 'PATCH', body: { title: t } })
            .then(function () {
              if (state.session && state.session.id === s.id) {
                state.session.title = t;
                renderCrumbs();
              }
              toast('已重命名');
              return loadSessions();
            },
              function (e) { toast('重命名失败：' + e.message); });
        }
      }
    ]);
    setTimeout(function () { input.focus(); input.select(); }, 0);
  }

  /* ---------- 6.5) 首次运行：上传 provider.yaml ---------- */
  // 服务在找不到 provider.yaml 时会以「配置模式」启动。此时除了上传配置之外什么都做不了，
  // 所以用一个不可关闭的对话框挡住界面，上传成功后直接刷新页面（配置在服务端已热加载）。
  var setupGate = { open: false, busy: false };

  function loadConfigStatus() {
    return api('api/config/status').then(function (st) { return st; }, function () { return null; });
  }

  function openSetupGate(status, opts) {
    // reset 模式：从设置页主动重置 provider。它复用同一个对话框（只填 key 的快捷
    // 通道 + 上传 + 粘贴），但**可以关闭**，而且先把风险摆在前面：现有
    // provider.yaml 会被覆盖，旧配置找不回来。
    var reset = !!(opts && opts.reset);
    setupGate.open = !reset;
    var body = el('div', 'setup');
    body.appendChild(el('p', 'setup-lead', reset
      ? '重置 provider：下面这份配置会覆盖当前的 provider.yaml。'
      : '服务已启动，但没有找到 provider.yaml。上传后即可开始对话，配置会立即生效。'));
    if (reset) {
      var warn = el('div', 'setup-warn');
      warn.appendChild(el('div', 'setup-warn-title', '⚠ 会覆盖现有配置，且无法撤销'));
      warn.appendChild(el('div', 'setup-warn-body',
        '当前 provider.yaml 里的 provider、模型和 API key 都会被替换掉。' +
        '新配置会先校验再落盘（不合法就拒绝写入，不会留下半个文件），' +
        '但一旦覆盖，旧的那份就没了——除非你另有备份。' +
        '想保留多个 provider，请直接编辑那个文件，而不是重置。'));
      body.appendChild(warn);
    }
    var pathRow = el('div', 'setup-path');
    pathRow.appendChild(el('span', 'k', reset ? '覆盖' : '保存到'));
    pathRow.appendChild(el('span', 'mono', (status && status.path) || '~/.tea_agent/provider.yaml'));
    body.appendChild(pathRow);

    // ---- 快捷通道：只填一个 API key ----
    // 完整 provider.yaml 有几行到几十行，在手机或没有编辑器的板子上手敲很痛苦。
    // 这里只问 key：服务端 /api/config/quickstart 生成一份配置（DeepSeek 默认模型
    // deepseek-flash，MiniMax 默认模型 MiniMax-M3.1-Flash）并按上传同样的流程
    // 校验、落盘、热加载。
    var QUICK = {
      deepseek: {
        label: 'DeepSeek（deepseek-flash）',
        hint: '选择 DeepSeek，粘贴 key，回车即可。会生成一份只含该 provider 的 provider.yaml' +
              '（默认模型 deepseek-flash），之后随时可以手工修改。',
        placeholder: 'sk-…'
      },
      minimax: {
        label: 'MiniMax（MiniMax-M3.1-Flash）',
        hint: '选择 MiniMax，粘贴 M Plan 的 key（sk-cp-…），回车即可。会生成一份只含该 provider 的' +
              'provider.yaml（默认模型 MiniMax-M3.1-Flash，1M 上下文），顺带打开顶栏的额度徽章。',
        placeholder: 'sk-cp-…'
      }
    };
    var quick = el('div', 'setup-quick');
    quick.appendChild(el('div', 'setup-quick-title', '快捷开始：只填 API key'));
    var quickHint = el('div', 'setup-quick-hint', QUICK.deepseek.hint);
    quickHint.id = 'setup-quick-hint';
    quick.appendChild(quickHint);
    var quickRow = el('div', 'setup-quick-row');
    var providerSel = document.createElement('select');
    providerSel.className = 'ta setup-provider';
    providerSel.id = 'setup-provider';
    var opt = document.createElement('option');
    opt.value = 'deepseek';
    opt.textContent = QUICK.deepseek.label;
    providerSel.appendChild(opt);
    var optMm = document.createElement('option');
    optMm.value = 'minimax';
    optMm.textContent = QUICK.minimax.label;
    providerSel.appendChild(optMm);
    quickRow.appendChild(providerSel);
    var keyInput = document.createElement('input');
    keyInput.type = 'password';
    keyInput.className = 'ta setup-key';
    keyInput.id = 'setup-key';
    keyInput.placeholder = QUICK.deepseek.placeholder;
    keyInput.autocomplete = 'off';
    keyInput.spellcheck = false;
    quickRow.appendChild(keyInput);
    var quickBtn = btn('btn primary setup-quick-btn', '用 key 创建配置', function () {
      quickApply();
    });
    quickBtn.id = 'setup-quick-apply';
    quickRow.appendChild(quickBtn);
    quick.appendChild(quickRow);
    body.appendChild(quick);
    // 换 provider 时提示文字和 key 的形状一起变：MiniMax 的 M Plan key 是 sk-cp-…
    providerSel.onchange = function () {
      var q = QUICK[providerSel.value] || QUICK.deepseek;
      quickHint.textContent = q.hint;
      keyInput.placeholder = q.placeholder;
    };
    body.appendChild(el('div', 'setup-or', '—— 或者上传 / 粘贴完整配置 ——'));

    // 文件选择 + 拖放
    var drop = el('label', 'setup-drop');
    drop.setAttribute('for', 'setup-file');
    drop.appendChild(el('span', 'setup-drop-main', '点击选择或拖入 provider.yaml'));
    drop.appendChild(el('span', 'setup-drop-hint', 'YAML 文件，通常只有几十行'));
    var input = document.createElement('input');
    input.type = 'file'; input.id = 'setup-file';
    input.accept = '.yaml,.yml,text/yaml,text/plain';
    input.className = 'hidden';
    drop.appendChild(input);
    body.appendChild(drop);

    // 也可以直接粘贴
    var ta = document.createElement('textarea');
    ta.className = 'ta mono setup-paste'; ta.id = 'setup-yaml'; ta.rows = 8;
    ta.placeholder = '或者把 provider.yaml 的内容粘贴到这里…';
    body.appendChild(ta);

    var err = el('div', 'setup-error hidden'); err.id = 'setup-error';
    body.appendChild(err);

    function showError(msg) {
      err.textContent = msg; err.classList.remove('hidden');
    }
    function setBusy(b) {
      setupGate.busy = b;
      applyBtn.disabled = b;
      applyBtn.textContent = b ? '正在应用…' : '上传并应用';
      quickBtn.disabled = b;
      keyInput.disabled = b;
      providerSel.disabled = b;
      quickBtn.textContent = b ? '正在应用…' : '用 key 创建配置';
    }
    function apply(text) {
      if (setupGate.busy) return;
      if (!text || !String(text).trim()) { showError('请先选择文件或粘贴配置内容。'); return; }
      setBusy(true); err.classList.add('hidden');
      fetch(apiUrl('api/config'), {
        method: 'POST',
        headers: { 'Content-Type': 'text/yaml' },
        body: String(text)
      }).then(function (r) {
        return r.text().then(function (t) {
          var data = null;
          try { data = t ? JSON.parse(t) : null; } catch (e) { data = null; }
          if (!r.ok) {
            var m = (data && data.error && (data.error.message || data.error.code)) || ('HTTP ' + r.status);
            throw new Error(m);
          }
          return data;
        });
      }).then(function () {
        // 配置已在服务端生效；刷新页面让界面按新配置重建。
        $('setup-ok').classList.remove('hidden');
        setTimeout(function () { location.reload(); }, 400);
      }, function (e) {
        setBusy(false);
        showError('上传失败：' + (e && e.message ? e.message : '未知错误'));
      });
    }

    /// 只用一个 key 建配置：服务端写 provider.yaml → 热加载 → 这里刷新页面。
    function quickApply() {
      if (setupGate.busy) return;
      var key = String(keyInput.value || '').trim();
      if (!key) {
        showError('请先粘贴 API key（DeepSeek 形如 sk-…，MiniMax M Plan 形如 sk-cp-…）');
        keyInput.focus();
        return;
      }
      setBusy(true);
      err.classList.add('hidden');
      api('api/config/quickstart', {
        method: 'POST',
        body: { provider: providerSel.value || 'deepseek', api_key: key }
      }).then(function (d) {
        $('setup-ok').classList.remove('hidden');
        // 用服务端回报的 provider/model，而不是这里写死的名字：换了快捷通道
        // 也不会显示成别的 provider。
        $('setup-ok').textContent = '已用 ' +
          ((d && d.provider) || (providerSel.value || 'deepseek')) + ' 创建配置（' +
          ((d && d.model) || '') + '），正在刷新…';
        setTimeout(function () { location.reload(); }, 400);
      }, function (e) {
        setBusy(false);
        showError('创建配置失败：' + (e && e.message ? e.message : '未知错误'));
      });
    }

    input.onchange = function () {
      var f = input.files && input.files[0];
      if (!f) return;
      var reader = new FileReader();
      reader.onload = function () {
        ta.value = String(reader.result || '');
        drop.classList.add('filled');
        drop.querySelector('.setup-drop-main').textContent = '已选择：' + f.name;
      };
      reader.readAsText(f);
    };
    ['dragenter', 'dragover'].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add('over'); });
    });
    ['dragleave', 'drop'].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove('over'); });
    });
    drop.addEventListener('drop', function (e) {
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (!f) return;
      var reader = new FileReader();
      reader.onload = function () {
        ta.value = String(reader.result || '');
        drop.classList.add('filled');
        drop.querySelector('.setup-drop-main').textContent = '已选择：' + f.name;
      };
      reader.readAsText(f);
    });

    var applyBtn = btn('btn primary', '上传并应用', function () { apply(ta.value); });
    var ok = el('div', 'setup-ok hidden', '配置已应用，正在刷新…'); ok.id = 'setup-ok';
    body.appendChild(ok);

    openDialog(reset ? '重置 provider 配置' : '需要配置 provider.yaml', body, [
      reset ? { label: '取消', onClick: closeDialog }
             : { label: '重新检测', onClick: function () { location.reload(); } },
      { label: '上传并应用', primary: true, onClick: function () { apply(ta.value); } }
    ]);
    // openDialog 会重建按钮，所以把禁用状态挂在真正的那个按钮上。
    var dialog = $('dialog');
    var buttons = dialog.querySelectorAll('.actions .btn');
    if (buttons.length >= 2) {
      applyBtn = buttons[buttons.length - 1];
      applyBtn.onclick = function () { apply(ta.value); };
    }
    keyInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); quickApply(); }
    });
    // 挡住关闭：overlay 点击与 Esc 都不能关掉它。重置模式除外——那里是可以
    // 取消的，不然「误点」就等于「覆盖配置」。
    if (!reset) $('overlay').classList.add('setup-locked');
    // 打开就把光标放在最省事的那条路上：粘贴 key、回车。
    setTimeout(function () { keyInput.focus(); }, 0);
  }

  /* ---------- 7) 数据加载 ---------- */
  /// `force` (manual click / recovery check) reports a failure immediately;
  /// the background poll tolerates one, so a single hiccup never turns the pill
  /// red while everything else keeps working.
  function loadHealth(force) {
    return api('api/health').then(function (h) {
      healthFailures = 0;
      state.health = h;
      markOnline();
      return h;
    }, function (e) {
      healthFailures++;
      if (force || healthFailures >= 2) markOffline(e);
      throw e;
    });
  }
  /// Manual re-check (status pill / banner button): show the waiting state, then
  /// report the outcome right away instead of after two poll failures.
  function recheckNow() {
    healthFailures = 0;
    markChecking();
    loadHealth(true).catch(function () {});
  }

  function loadProviders() {
    return api('api/providers').then(function (p) {
      state.providers = p;
      var saved = state.model;
      buildModels();
      if (saved && state.models.some(function (m) { return m.provider === saved.provider && m.model === saved.model; })) state.model = saved;
      renderModelChip(); renderModelMenu(); renderSettings(); renderMetrics();
      loadUsage();
      // Providers carry max_context_tokens, so the metrics strip can resolve the
      // context window here; without this it stays "模型未配置" until a stream or a
      // session switch happens to re-render it.
      // (dsh: metrics are re-rendered whenever the model catalogue changes.)
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
        clearForkPoint(true);
        state.workspace = null; state.sessions = []; state.session = null; state.messages = [];
        state.trajectory = []; state.lastTrajectoryId = null;
        clearQueue();
        renderWorkspaceChip(); renderSessions(); renderChat(); renderSettings();
        openAddWorkspace(true);
        return;
      }
      // 页面落在哪个主题？地址栏的 ?s= 说了算（刷新/收藏/贴给另一个浏览器都靠它），
      // 其次是这个标签页上次看的那个会话（sessionStorage），最后才是上次的工作区。
      //
      // 刻意**不**再"谁在跑就跳到谁"：多主题并行时，另开一个窗口/标签页想看点别的，
      // 一打开就被拽到别人正在跑的那个主题上，正是"多个浏览器本质是一个 session"
      // 的来源。正在跑的会话在侧栏标「进行中」，点一下才接上。
      var wanted = topicFromUrl();
      if (!wanted) wanted = tabSession();
      if (!wanted) wanted = loadLS().session_id || '';   // 旧版本留在 localStorage 里的
      return selectHomeTopic(wanted);
    });
  }

  /// 冷启动落点：先记下"服务器上谁在跑"（侧栏标记用），再按 wanted 找主题。
  /// wanted 失效（会话被删、来自另一台机器）时清掉地址，正常落回默认工作区。
  function selectHomeTopic(wanted) {
    var savedWs = loadLS().workspace_id;
    var home = function () {
      if (state.session && wanted && state.session.id === wanted) return Promise.resolve();
      var w = state.workspaces.filter(function (x) { return x.id === savedWs; })[0] || state.workspaces[0];
      return selectWorkspace(w.id, true);
    };
    var refresh = refreshRunning();
    if (!wanted) return refresh.then(home);
    return refresh.then(function () {
      return api('api/sessions/' + encodeURIComponent(wanted)).then(function (d) {
        var wid = d && d.workspace_id;
        var w = state.workspaces.filter(function (x) { return x.id === wid; })[0];
        if (!w || (d && d.session && d.session.id !== wanted)) {
          // 这个地址在本机打不开（工作区没登记 / 会话已被删）：别留着它，否则每次
          // 刷新都要先白试一次。
          setTopicUrl(null); setTabSession(null);
          return home();
        }
        return selectWorkspace(w.id, true).then(function () {
          if (state.session && state.session.id === wanted) return null;
          return openSession(wanted, { quiet: true });
        });
      }, function () {
        setTopicUrl(null); setTabSession(null);
        return null;
      }).then(home);
    });
  }

  /// 服务端正在跑的轮次 → state.running（按 session id）。侧栏据此标「进行中」，
  /// 会话行的改名/删除也因此禁用：worker 手里那份 Session 会把改动覆盖掉。
  function refreshRunning() {
    return runningTurns().then(function (list) {
      var map = {};
      list.forEach(function (t) { if (t && t.session_id) map[t.session_id] = t; });
      state.running = map;
      renderSessions();
      return list;
    }, function () { return []; });
  }
  function selectWorkspace(id, silent) {
    var w = state.workspaces.filter(function (x) { return x.id === id; })[0];
    if (!w) return Promise.resolve();
    var changed = !state.workspace || state.workspace.id !== w.id;
    // 跟随中的轮次属于别的工作区：先离开（服务端照跑），别再拒绝这一下。
    if (changed && state.streaming) detachStream();
    state.workspace = w;
    if (changed) {
      clearForkPoint(true);
      state.draft = false;
      state.sessionEpoch++;                   // 上一个工作区的 openSession 结果作废
      state.session = null; state.messages = []; state.live = null; state.streamError = null;
      clearQueue();
      state.stats = emptyStats(); state.liveTokens = 0; state.liveStartAt = 0;
      state.turnSteps = 0; state.turnStartAt = 0;
      state.trajectory = []; state.lastTrajectoryId = null;
      state.files = { dir: '', entries: [], path: null, content: '', original: '', truncated: false, readOnly: false, preview: false, size: 0 };
    }
    renderWorkspaceChip(); renderChat(); renderMetrics(); saveLS();
    return loadSessions().then(function () {
      if (state.view === 'files') loadTree(state.files.dir);
      if (state.view === 'memory') loadMemory();
      if (state.view === 'logs') loadLogs();
      if (state.view === 'settings') { renderSettings(); loadSystemPrompt(); loadAccess(); }
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
      // 自动打开"最近更新的会话"是便利，但不能覆盖用户明确的草稿。
      if (!state.session && !state.draft && state.sessions.length) return openSession(state.sessions[0].id);
      if (!state.session) renderChat();
      return refreshRunning();       // 侧栏的「进行中」标记跟着列表一起刷新
    }, function () { renderSessions(); });
  }
  function openSession(sid) {
    if (!sid) return Promise.resolve();
    // 正在跟随另一轮：先离开（服务端照跑），而不是拒绝点这一下。多主题并行时
    // "必须先按停止/离开才能看别的会话"是最别扭的地方，这里改成自动离开。
    if (state.streaming && (!state.session || state.session.id !== sid)) detachStream();
    // 主动点开会话 = 想重新跟上它：清掉「已离开」标记，下面的 maybeAttach 才会接回去。
    if (state.detached === sid) state.detached = null;
    clearForkPoint(true);
    var epoch = ++state.sessionEpoch;
    return api('api/sessions/' + encodeURIComponent(sid)).then(function (d) {
      // 更新的 openSession 已经开始：这次的结果已经过期，别去改 state。
      if (epoch !== state.sessionEpoch) return null;
      state.session = d.session || { id: sid };
      state.draft = false;
      state.messages = d.messages || [];
      setTodos(d.todos || []);
      state.artifacts = d.artifacts || [];
      state.live = null; state.streamError = null;
      state.stickBottom = true;                     // 打开会话默认落在最新一条
      clearQueue();
      renderCrumbs();
      state.stats = statsFor(sid);                 // 恢复该会话上一轮的指标
      state.turnSteps = num(state.stats.steps);
      state.turnStartAt = 0;
      state.liveTokens = 0; state.liveStartAt = 0;
      state.lastTrajectoryId = null;
      // 这个会话现在就是这个标签页的主题：地址栏与 sessionStorage 一起记住它。
      setTopicUrl(sid);
      setTabSession(sid);
      // 轨迹按需单独取：摘要用于折叠行，展开时再按 turn_id 拉完整记录。
      return loadTrajectories().then(function () {
        if (epoch !== state.sessionEpoch) return null;
        attachTrajectories();
        saveLS(); renderSessions(); renderChat(); renderMetrics(); scrollMaybe(true);
        // 这一轮可能还在服务端跑着（页面刚重新打开、或从别的标签页切回来）：接回去。
        return maybeAttach(sid);
      });
    }, function (e) { toast('加载会话失败：' + e.message); });
  }
  function newSessionDraft() {
    if (state.streaming) detachStream();      // 新会话不该被"先停止"挡住
    clearForkPoint(true);
    setTopicUrl(null); setTabSession(null);   // 草稿没有主题地址，发出第一条才生成会话
    state.draft = true;
    state.sessionEpoch++;                     // 在途的 openSession 结果作废，别把草稿顶掉

    state.session = null; state.messages = []; state.live = null; state.streamError = null;
    setTodos([]);
    state.artifacts = [];
    state.stickBottom = true;
    clearQueue();
    renderCrumbs();
    state.stats = emptyStats(); state.liveTokens = 0; state.liveStartAt = 0;
    state.turnSteps = 0; state.turnStartAt = 0;
    state.trajectory = []; state.lastTrajectoryId = null;
    saveLS(); renderSessions(); renderChat(); renderMetrics(); showView('chat'); $('input').focus();
  }
  /// Register a workspace and, on success, switch to it: adding a project and
  /// then staying on the old one looks like nothing happened.
  function addWorkspace(path, name, create, onError) {
    return api('api/workspaces', { method: 'POST', body: { path: path, name: name, create: !!create } })
      .then(function (entry) {
        toast('已添加工作区：' + (entry && entry.name ? entry.name : name));
        return loadWorkspaces().then(function () {
          if (entry && entry.id) return selectWorkspace(entry.id);
        });
      }, function (e) { onError(e); });
  }

  /// "＋ 添加工作区…" opens the path form. There is no directory browser: the
  /// dialog would have to be drawn on the server's desktop (one extra process
  /// and one temp script per click) and is invisible when the UI is opened from
  /// another machine, so the form asks for a complete path and says what is
  /// wrong when it is not one.
  function openAddWorkspace(isEmpty) { openAddWorkspaceForm(isEmpty); }

  function deriveWorkspaceName(path) {
    return String(path || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop() || 'workspace';
  }

  /// 完整路径的判定：Windows 盘符（C:\…）、UNC 共享（\\server\…）和以 / 开头的 POSIX
  /// 路径都算；其余都是相对路径，服务端会按自己的 cwd 解析，用户在浏览器里看不到那个
  /// cwd，所以这里先拦下来（服务端 /api/workspaces 也会再校验一次）。
  function looksAbsolutePath(p) {
    return /^[A-Za-z]:[\\/]/.test(p) || /^\\\\/.test(p) || p.charAt(0) === '/';
  }

  function openAddWorkspaceForm(isEmpty) {
    var body = el('div');
    body.appendChild(el('div', 'desc', isEmpty
      ? '输入项目目录的完整路径（绝对路径），例如 C:\\Users\\me\\project 或 /home/me/project。'
      : '输入项目目录的完整路径（绝对路径），例如 C:\\Users\\me\\project 或 /home/me/project；目录不存在时会问你是否创建。'));
    var pathInput = el('input', 'ta'), nameInput = el('input', 'ta');
    pathInput.type = 'text'; pathInput.placeholder = 'C:\\Users\\me\\project';
    pathInput.autocomplete = 'off'; pathInput.spellcheck = false;
    nameInput.type = 'text'; nameInput.placeholder = 'project';

    var pathField = el('div', 'field');
    pathField.appendChild(el('label', null, '路径'));
    pathField.appendChild(pathInput);
    body.appendChild(pathField);

    var nameField = el('div', 'field');
    nameField.appendChild(el('label', null, '名称（可选）'));
    nameField.appendChild(nameInput);
    body.appendChild(nameField);

    // 提示写在表单里，不用 toast：toast 会自己消失，用户来不及对照自己敲的路径，
    // 而且对话框要一直开着，改一个字符就能重试。
    var errorNode = el('div', 'form-error');
    body.appendChild(errorNode);
    var showError = function (msg) {
      errorNode.textContent = msg;
      pathInput.focus(); pathInput.select();
    };
    pathInput.addEventListener('input', function () { errorNode.textContent = ''; });

    openDialog('添加工作区', body, [
      { label: '取消', onClick: closeDialog },
      {
        label: '添加', primary: true, onClick: function () {
          var p = pathInput.value.trim();
          if (!p) { showError('请输入完整的项目目录路径'); return; }
          if (!looksAbsolutePath(p)) {
            showError('请写完整的目录路径（绝对路径），例如 C:\\Users\\me\\project 或 /home/me/project');
            return;
          }
          var nm = nameInput.value.trim() || deriveWorkspaceName(p);
          addWorkspace(p, nm, false, function (e) {
            var msg = (e && e.message) ? e.message : '未知错误';
            // 新项目目录是常见情况，不该卡死：问一句是否创建，而不是让用户去终端
            // 手动 mkdir 再回来。
            if (msg.indexOf('does not exist') >= 0) { openCreateWorkspaceDialog(p, nm); return; }
            showError('添加失败：' + msg);
          });
        }
      }
    ]);
    setTimeout(function () { pathInput.focus(); }, 0);
  }

  function openCreateWorkspaceDialog(path, name) {
    var body = el('div');
    body.appendChild(el('div', 'desc', '这个目录还不存在，是否创建后作为工作区？'));
    var row = el('div', 'field');
    row.appendChild(el('label', null, '将创建'));
    row.appendChild(el('span', 'mono', path));
    body.appendChild(row);
    openDialog('目录不存在', body, [
      { label: '取消', onClick: closeDialog },
      {
        label: '创建并添加', primary: true, onClick: function () {
          closeDialog();
          addWorkspace(path, name, true, function (e) { toast('创建失败：' + e.message); });
        }
      }
    ]);
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
    hideTurnTip();
    closeImageViewer();
    saveLS();
    VIEWS.forEach(function (v) { $('view-' + v).classList.toggle('hidden', v !== name); });
    Array.prototype.forEach.call(document.querySelectorAll('.nav-row'), function (b) { b.classList.toggle('active', b.dataset.view === name); });
    renderCrumbs();
    if (name === 'chat') { scrollMaybe(); scheduleTurnSpy(); setTimeout(autoGrow, 0); }
    if (name === 'files') loadTree(state.files.dir);
    if (name === 'memory') loadMemory();
    if (name === 'logs') loadLogs();
    if (name === 'settings') { renderSettings(); loadSystemPrompt(); loadAccess(); }
  }
  /* ---------- 侧边栏收起 / 展开 ---------- */
  // 窄屏或专心读回答时，左侧 260px 一直是白占的。收起后由顶栏左侧的同一个图标
  // 展开回来（Ctrl/Cmd+B 也可以），状态记在 localStorage 里，下次打开保持原样。
  function applySidebar() {
    var hidden = !!state.sidebarHidden;
    $('app').classList.toggle('side-collapsed', hidden);
    $('sidebar-show').classList.toggle('hidden', !hidden);
    $('sidebar-hide').setAttribute('aria-expanded', hidden ? 'false' : 'true');
    $('sidebar-show').setAttribute('aria-expanded', hidden ? 'true' : 'false');
  }
  function toggleSidebar(force) {
    state.sidebarHidden = typeof force === 'boolean' ? force : !state.sidebarHidden;
    applySidebar();
    saveLS();
    // 正文栏宽度变了：滚动位置和输入框高度都要按新宽度重算一次。
    scrollMaybe();
    scheduleTurnSpy();
    autoGrow();
  }

  function renderCrumbs() {
    updateExportButton();
    var ws = state.workspace ? state.workspace.name : '无工作区';
    if (state.view === 'chat') setCrumbs([{ label: ws }, { label: state.session ? (state.session.title || state.session.id) : '新会话' }]);
    else setCrumbs([{ label: ws }, { label: VIEW_LABEL[state.view] || state.view }]);
  }

  /* ---------- 9) 文件视图 ---------- */
  /// 从 Content-Disposition 里取文件名：优先 RFC 5987 的 filename*（UTF-8），
  /// 退回普通的 filename，都没有就用兜底名。
  function dispositionFilename(header, fallback) {
    var m = /filename\*=UTF-8''([^;]+)/i.exec(header || '');
    if (m && m[1]) { try { return decodeURIComponent(m[1]); } catch (e) { /* 用下一个 */ } }
    m = /filename="?([^";]+)"?/i.exec(header || '');
    if (m && m[1]) return m[1];
    return fallback;
  }

  /// 下载**这台设备上、这个工作区**的 `.tea_agent_run`（会话 / 轨迹 / 日志 / 记忆 /
  /// agent.md），服务端打成 zip 传回来。用 fetch + blob 而不是直接跳转：出错时能
  /// 说清原因，而不是让浏览器把一个 JSON 错误体当文件存下来。
  function downloadRunArchive() {
    if (!state.workspace) { toast('请先选择工作区'); return; }
    var b = $('files-download');
    var label = b.textContent;
    var restore = function () { b.disabled = false; b.textContent = label; };
    b.disabled = true; b.textContent = '打包中…';
    fetch(apiUrl('api/workspaces/' + encodeURIComponent(state.workspace.id) +
      '/archive?path=' + encodeURIComponent('.tea_agent_run'))).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (t) {
          var msg = 'HTTP ' + r.status;
          try {
            var j = JSON.parse(t);
            if (j && j.error && (j.error.message || j.error.code)) msg = j.error.message || j.error.code;
          } catch (e) { if (t) msg = t.slice(0, 200); }
          throw new Error(msg);
        });
      }
      var head = {
        name: dispositionFilename(r.headers.get('Content-Disposition'), 'tea_agent_run.zip'),
        entries: r.headers.get('X-Archive-Entries'),
        skipped: r.headers.get('X-Archive-Skipped')
      };
      return r.blob().then(function (blob) { return { blob: blob, head: head }; });
    }).then(function (d) {
      var objUrl = URL.createObjectURL(d.blob);
      var a = document.createElement('a');
      a.href = objUrl; a.download = d.head.name;
      document.body.appendChild(a);
      a.click();
      setTimeout(function () { URL.revokeObjectURL(objUrl); a.remove(); }, 4000);
      toast('已下载 ' + d.head.name + '（' + fmtBytes(d.blob.size) +
        (d.head.entries ? '，' + d.head.entries + ' 个条目' : '') +
        (d.head.skipped && d.head.skipped !== '0' ? '，跳过 ' + d.head.skipped + ' 个' : '') + '）');
    }, function (e) {
      toast('下载失败：' + (e && e.message ? e.message : String(e)));
    }).then(restore, restore);
  }

  /// 把一段 fetch 响应存成文件（导出的 HTML、归档 zip 都走这里）。用 blob 而不是让
  /// 浏览器直接打开 URL：服务端报错时能就地说明原因，而不是存下一个 JSON 错误体。
  function saveBlobResponse(r, fallbackName) {
    var head = {
      name: dispositionFilename(r.headers.get('Content-Disposition'), fallbackName),
      entries: r.headers.get('X-Archive-Entries'),
      skipped: r.headers.get('X-Archive-Skipped')
    };
    return r.blob().then(function (blob) {
      var objUrl = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = objUrl; a.download = head.name;
      document.body.appendChild(a);
      a.click();
      setTimeout(function () { URL.revokeObjectURL(objUrl); a.remove(); }, 4000);
      return { blob: blob, head: head };
    });
  }
  function httpError(r) {
    return r.text().then(function (t) {
      var msg = 'HTTP ' + r.status;
      try {
        var j = JSON.parse(t);
        if (j && j.error && (j.error.message || j.error.code)) msg = j.error.message || j.error.code;
      } catch (e) { if (t) msg = t.slice(0, 200); }
      throw new Error(msg);
    });
  }

  /* ---------- 导出 / 导入会话（单文件 HTML） ---------- */
  // 导出的 HTML 自带 CSS 与图片（data: URL），拷到任何机器上双击即可离线阅读；
  // 同一个文件里还带着整段会话 JSON，所以可以导入回来（甚至导入到另一个工作区）。
  function updateExportButton() {
    var b = $('session-export');
    if (b) {
      b.classList.toggle('hidden', !state.session);
      b.disabled = !state.session || !state.workspace;
      b.title = state.session
        ? '导出「' + (state.session.title || state.session.id) + '」为单个 HTML 文件（离线可读，含图片）'
        : '导出当前会话';
    }
    var link = $('session-link');
    if (link) {
      link.classList.toggle('hidden', !state.session);
      link.disabled = !state.session;
      link.title = state.session
        ? '复制主题链接（?s=…）：「' + (state.session.title || state.session.id) +
          '」可以在另一个浏览器/标签页打开同一个会话'
        : '复制主题链接';
    }
  }

  /// 主题链接：地址栏本来就跟着当前会话走（openSession → setTopicUrl），所以直接
  /// 取 location.href。贴到另一个浏览器打开，落在同一个会话上，两边看同一份输出。
  function copyTopicLink() {
    if (!state.session) { toast('请先打开一个会话'); return; }
    setTopicUrl(state.session.id);
    var url = window.location.href;
    var fallback = function () {
      var ta = document.createElement('textarea');
      ta.value = url;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      document.body.removeChild(ta);
      if (ok) toast('已复制主题链接：' + url);
      else window.prompt('复制这个主题的链接：', url);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(function () {
        toast('已复制主题链接：' + url);
      }, fallback);
    } else {
      fallback();
    }
  }
  function exportSession() {
    if (!state.session || !state.workspace) { toast('请先打开一个会话'); return; }
    var b = $('session-export');
    b.disabled = true;
    fetch(apiUrl('api/sessions/' + encodeURIComponent(state.session.id) + '/export'))
      .then(function (r) { return r.ok ? r : httpError(r); })
      .then(function (r) { return saveBlobResponse(r, 'tea_agent-session.html'); })
      .then(function (d) {
        toast('已导出 ' + d.head.name + '（' + fmtBytes(d.blob.size) + '，' +
          (d.head.entries || '含') + ' 条消息）');
      }, function (e) {
        toast('导出失败：' + (e && e.message ? e.message : String(e)));
      }).then(function () { b.disabled = false; });
  }
  function importSessionFile(file) {
    if (!file || !state.workspace) return;
    var wsid = state.workspace.id;
    toast('正在导入 ' + file.name + '…');
    var reader = new FileReader();
    reader.onload = function () {
      fetch(apiUrl('api/workspaces/' + encodeURIComponent(wsid) + '/sessions/import'), {
        method: 'POST',
        headers: { 'Content-Type': 'text/html; charset=utf-8' },
        body: String(reader.result || '')
      }).then(function (r) {
        return r.text().then(function (t) {
          var data = null;
          try { data = t ? JSON.parse(t) : null; } catch (e) { data = null; }
          if (!r.ok) {
            throw new Error((data && data.error && (data.error.message || data.error.code)) ||
              ('HTTP ' + r.status));
          }
          return data;
        });
      }).then(function (d) {
        toast('已导入 ' + (d && d.messages ? d.messages : '') + ' 条消息');
        return loadSessions().then(function () {
          if (d && d.session && d.session.id) return openSession(d.session.id);
        });
      }, function (e) { toast('导入失败：' + (e && e.message ? e.message : String(e))); });
    };
    reader.readAsText(file);
  }

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
    $('files-save').disabled = !f.path || f.readOnly || !dirty || isImagePath(f.path);
    $('files-revert').disabled = !f.path || !dirty || isImagePath(f.path);
    // 下载的是服务端那个工作区的 .tea_agent_run：没有工作区就没什么可打包。
    $('files-download').disabled = !state.workspace;
    renderFilePane();
  }
  function fileExt(p) { var m = /\.([A-Za-z0-9]+)\s*$/.exec(String(p || '')); return m ? m[1].toLowerCase() : ''; }
  var IMAGE_EXT = { png: 1, jpg: 1, jpeg: 1, gif: 1, webp: 1, svg: 1, bmp: 1, ico: 1 };
  function isImagePath(p) { return !!IMAGE_EXT[fileExt(p)]; }
  function isMarkdownPath(p) { var e = fileExt(p); return e === 'md' || e === 'markdown'; }
  function fileRawUrl(p) {
    return apiUrl('api/workspaces/' + encodeURIComponent(state.workspace.id) +
      '/raw?path=' + encodeURIComponent(p));
  }
  function fileEntry(p) {
    var found = null;
    state.files.entries.forEach(function (en) { if (en.path === p) found = en; });
    return found;
  }
  // 图片直接走 raw 路由显示；markdown 可以在「编辑 / 预览」之间切换。
  function renderFilePane() {
    var f = state.files;
    var image = !!f.path && isImagePath(f.path);
    var md = !!f.path && isMarkdownPath(f.path);
    var showMd = md && !!f.preview;
    var showPre = !image && !showMd && f.readOnly;
    var showEditor = !image && !showMd && !f.readOnly;
    $('files-media').classList.toggle('hidden', !image);
    $('files-md').classList.toggle('hidden', !showMd);
    $('files-pre').classList.toggle('hidden', !showPre);
    $('files-editor').classList.toggle('hidden', !showEditor);
    var toggle = $('files-preview-toggle');
    toggle.classList.toggle('hidden', !md);
    toggle.textContent = f.preview ? '编辑' : '预览';
    var media = $('files-media');
    if (image) {
      clear(media);
      var img = document.createElement('img');
      img.src = fileRawUrl(f.path);
      img.alt = f.path;
      media.appendChild(img);
      media.appendChild(el('span', 'hint', f.path + ' · ' + fmtBytes(f.size)));
    } else if (media.firstChild) {
      clear(media);
    }
    if (showMd) {
      // 预览里的相对图片路径以该文件所在目录为基准。
      state.mdBase = f.path.split('/').slice(0, -1).join('/');
      $('files-md').innerHTML = renderMarkdown($('files-editor').value);
      state.mdBase = '';
    }
  }
  function openFile(p) {
    if (!state.workspace) return;
    var f = state.files;
    if (isImagePath(p)) {
      // 图片是二进制，走 raw 路由；`/file` 的 JSON 文本视图对它没有意义。
      f.path = p; f.content = ''; f.original = ''; f.truncated = false; f.readOnly = true; f.preview = false;
      var entry = fileEntry(p);
      f.size = entry ? num(entry.size) : 0;
      $('files-preview-head').textContent = p + (f.size ? '  ·  ' + fmtBytes(f.size) : '');
      $('files-editor').value = '';
      $('files-pre').textContent = '';
      renderFiles();
      return;
    }
    api('api/workspaces/' + encodeURIComponent(state.workspace.id) + '/file?path=' + encodeURIComponent(p)).then(function (d) {
      f.path = d.path || p;
      f.content = d.content == null ? '' : d.content;
      f.original = f.content;
      f.truncated = !!d.truncated;
      f.readOnly = f.truncated;
      f.preview = false;
      f.size = num(d.size);
      $('files-preview-head').textContent = f.path + '  ·  ' + fmtBytes(d.size);
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
    loadAgentDoc();                       // 经验文件与记忆一起刷新
    return api('api/workspaces/' + encodeURIComponent(state.workspace.id) + '/memory').then(function (d) {
      state.memory = d.notes || [];
      renderMemory();
    }, function (e) { toast('读取记忆失败：' + e.message); });
  }

  /* ---------- 工作区经验 agent.md ---------- */
  // agent 自己维护、每轮注入 system prompt 的经验文件。这里只做「人也能看/改」：
  // 编辑框内容与 state.agentDoc.original 比较得出是否有未保存的修改。
  function loadAgentDoc() {
    if (!state.workspace) { renderAgentDoc(); return Promise.resolve(); }
    state.agentDoc.loading = true;
    renderAgentDoc();
    var wsid = state.workspace.id;
    return api('api/workspaces/' + encodeURIComponent(wsid) + '/agent-doc').then(function (d) {
      if (!state.workspace || state.workspace.id !== wsid) return;   // 期间切了工作区
      state.agentDoc = {
        content: d.content || '', original: d.content || '',
        path: d.relative_path || d.path || '',
        scriptsDir: d.scripts_dir || '', scripts: d.scripts || [],
        loading: false, touched: false
      };
      renderAgentDoc();
    }, function (e) {
      state.agentDoc.loading = false;
      renderAgentDoc();
      toast('读取 agent.md 失败：' + e.message);
    });
  }
  function renderAgentDoc() {
    var doc = state.agentDoc;
    var editor = $('agent-doc-editor');
    // touched 由输入事件置位：用户改过就别用服务端内容覆盖（他手里的更新）。
    // 读取完成、还原、保存成功都会把它复位，编辑器随之跟服务端同步。
    if (!doc.touched) editor.value = doc.content;
    $('agent-doc-path').textContent = doc.path;
    $('agent-doc-path').title = doc.path || '';
    var dirty = editor.value !== doc.original;
    var meta = dirty ? '未保存' : (doc.loading ? '读取中…' : fmtBytes(doc.original.length));
    $('agent-doc-meta').textContent = meta;
    $('agent-doc-meta').classList.toggle('dirty', dirty);
    var box = $('agent-doc-scripts');
    clear(box);
    if (doc.scripts && doc.scripts.length) {
      box.appendChild(el('span', null, '可复用脚本（' + (doc.scriptsDir || '.tea_agent_run/scripts/') + '）：'));
      doc.scripts.forEach(function (s) {
        var tag = el('span', 's-nm', s.name);
        tag.title = fmtBytes(s.size) + ' · ' + fmtTime(s.mtime);
        box.appendChild(tag);
      });
    }
  }
  function saveAgentDoc() {
    if (!state.workspace) return;
    var doc = state.agentDoc;
    var content = $('agent-doc-editor').value;
    api('api/workspaces/' + encodeURIComponent(state.workspace.id) + '/agent-doc',
      { method: 'PUT', body: { content: content } }).then(function () {
        doc.original = content;
        doc.content = content;
        doc.touched = false;
        renderAgentDoc();
        toast('agent.md 已保存');
      }, function (e) { toast('保存失败：' + e.message); });
  }
  /* ---------- 工作区 system prompt ---------- */
  // 每轮请求都会重新拼 system prompt：这里保存的内容下一次（或本轮下一次
  // 模型请求）就会生效，不需要重启，也不用改会话。
  //
  // 编辑框显示的是 `current`——服务端真正当作人设使用的那段文字：有工作区覆盖
  // 就是覆盖内容，没有就是默认提示词。空编辑框什么也说明不了，所以绝不显示空。
  // 改完保存，"与默认一致"会被服务端归一成「无覆盖（跟随默认）」。
  function applySystemPrompt(d) {
    var sp = state.systemPrompt;
    sp.content = d.content || '';
    sp.current = d.current_prompt != null ? d.current_prompt : (d.content || '');
    sp.custom = !!d.custom;
    sp.path = d.relative_path || d.path || sp.path;
    sp.defaultPrompt = d.default_prompt || sp.defaultPrompt;
    sp.defaultSource = d.default_source || sp.defaultSource;
    sp.maxBytes = num(d.max_bytes);
    sp.effectiveBytes = num(d.effective_bytes);
    sp.estimatedTokens = num(d.estimated_tokens);
    sp.loading = false;
    sp.touched = false;
  }
  function loadSystemPrompt() {
    if (!state.workspace) { renderSystemPrompt(); return Promise.resolve(); }
    var wsid = state.workspace.id;
    state.systemPrompt.loading = true;
    renderSystemPrompt();
    return api('api/workspaces/' + encodeURIComponent(wsid) + '/system-prompt').then(function (d) {
      if (!state.workspace || state.workspace.id !== wsid) return;   // 期间切了工作区
      applySystemPrompt(d);
      renderSystemPrompt();
    }, function (e) {
      state.systemPrompt.loading = false;
      renderSystemPrompt();
      toast('读取 system prompt 失败：' + e.message);
    });
  }
  /// 编辑框里是不是「当前生效的内容」。dirty 用 current 比，不用磁盘上的覆盖：
  /// 没有覆盖时编辑框里是默认提示词，那不算用户改过。
  function systemPromptDirty() {
    return $('system-prompt-editor').value !== state.systemPrompt.current;
  }
  function renderSystemPrompt() {
    var sp = state.systemPrompt;
    var editor = $('system-prompt-editor');
    if (!editor) return;
    // touched 由输入事件置位：用户正在改就不要被服务端内容覆盖。
    if (!sp.touched) editor.value = sp.current;
    $('system-prompt-path').textContent = sp.path;
    $('system-prompt-path').title = sp.path || '';
    var dirty = systemPromptDirty();
    var meta = dirty ? '未保存'
      : (sp.loading ? '读取中…' : (sp.custom ? '自定义 · ' + fmtBytes(sp.content.length) : '默认'));
    $('system-prompt-meta').textContent = meta;
    $('system-prompt-meta').classList.toggle('dirty', dirty);
    $('system-prompt-save').disabled = !dirty;
    $('system-prompt-revert').disabled = !dirty;
    var hint = $('system-prompt-hint');
    clear(hint);
    hint.appendChild(el('span', 's-nm',
      sp.custom ? '当前：工作区自定义，整体替换默认人格' :
        '当前：默认提示词（编辑框里就是它，改完保存即成为本工作区自定义）'));
    if (sp.effectiveBytes) {
      hint.appendChild(el('span', null, '整段提示词 ' + fmtBytes(sp.effectiveBytes) +
        ' · ≈' + fmtTokens(sp.estimatedTokens) + ' tokens（含环境、agent.md 等，点「预览」）'));
    }
    hint.appendChild(el('span', null, '保存后从下一轮开始生效'));
    if (sp.defaultSource === 'settings.yaml') {
      hint.appendChild(el('span', null, '默认来自 settings.yaml 的 system_prompt'));
    }
  }
  function saveSystemPrompt() {
    if (!state.workspace) return;
    var content = $('system-prompt-editor').value;
    api('api/workspaces/' + encodeURIComponent(state.workspace.id) + '/system-prompt',
      { method: 'PUT', body: { content: content } }).then(function (d) {
        applySystemPrompt(d);
        renderSystemPrompt();
        toast(d.custom ? 'system prompt 已保存，下一轮生效'
                       : '已恢复默认提示词，下一轮生效');
      }, function (e) { toast('保存失败：' + e.message); });
  }
  function previewSystemPrompt() {
    if (!state.workspace) { toast('请先选择工作区'); return; }
    var dirty = systemPromptDirty();
    api('api/workspaces/' + encodeURIComponent(state.workspace.id) + '/prompt').then(function (d) {
      var body = el('div');
      if (dirty) {
        body.appendChild(el('div', 'hint',
          '编辑框里有未保存的修改：下面是服务端当前生效的版本，保存后才会换成编辑框里的内容。'));
      }
      body.appendChild(el('div', 'hint', '下一轮实际发送的 system prompt · ' + fmtBytes(d.bytes) +
        ' · ≈' + fmtTokens(d.estimated_tokens) + ' tokens · ' +
        (d.has_system_prompt ? '工作区自定义' : '默认')));
      var pre = el('pre', 'prompt-preview', d.system_prompt || '');
      body.appendChild(pre);
      openDialog('完整 system prompt', body, [{ label: '关闭', primary: true, onClick: closeDialog }]);
    }, function (e) { toast('读取完整提示词失败：' + e.message); });
  }
  /* ---------- 销毁：删除 tea home 与所有工作区的 .tea_agent_run ---------- */
  // 不可逆操作，所以先 GET 列出将删除的路径（用户能看到自己这台机器上究竟是
  // 哪些目录），再在确认框里 POST。删除成功后本地保存的工作区/会话 id 也一并
  // 清掉，随后刷新页面 —— 服务端已经回到「没有 provider.yaml」的首次运行状态。
  function openDestroyDialog() {
    var btn = $('destroy-run');
    btn.disabled = true;
    api('api/destroy').then(function (d) {
      btn.disabled = false;
      state.destroy = d;
      var body = el('div');
      body.appendChild(el('p', 'desc', '是否真的彻底删除痕迹？此操作不可恢复。'));
      var home = el('div', 'destroy-row');
      home.appendChild(el('span', 'k', 'tea home'));
      home.appendChild(el('span', 'mono', d.tea_home || ''));
      body.appendChild(home);
      var targets = (d.targets || []).filter(function (t) { return t.kind === 'workspace'; });
      if (targets.length) {
        body.appendChild(el('div', 'destroy-sub', '以下工作区的 .tea_agent_run（' + targets.length + ' 个）：'));
        targets.forEach(function (t) {
          var row = el('div', 'destroy-row');
          row.appendChild(el('span', 'k', t.workspace || t.workspace_id || ''));
          row.appendChild(el('span', 'mono', t.path));
          if (t.exists === false) row.appendChild(el('span', 'gone', '目录已不存在'));
          body.appendChild(row);
        });
      } else {
        body.appendChild(el('div', 'destroy-sub', '当前没有已索引的工作区。'));
      }
      body.appendChild(el('div', 'desc',
        'provider.yaml、workspaces.json、日志、会话、轨迹、记忆、agent.md 都会被删除；' +
        '项目目录本身不受影响。删除后回到首次运行状态，需要重新上传 provider.yaml。'));
      var err = el('div', 'form-error');
      body.appendChild(err);
      var confirmBtn = null;
      var actions = [
        { label: '取消', onClick: closeDialog },
        {
          label: '彻底删除', primary: true, onClick: function () {
            if (confirmBtn.disabled) return;
            confirmBtn.disabled = true;
            confirmBtn.textContent = '正在删除…';
            api('api/destroy', { method: 'POST', body: { confirm: 'destroy' } }).then(function (r) {
              state.destroy = null;
              // 本地记录的 workspace_id / session_id 指向已经不存在的东西。
              try { localStorage.removeItem(LS_KEY); } catch (e) { /* 忽略 */ }
              var failed = (r && r.failed) || [];
              if (failed.length) {
                err.textContent = '部分路径删除失败：' +
                  failed.map(function (f) { return f.path + '（' + f.error + '）'; }).join('；');
                confirmBtn.disabled = false;
                confirmBtn.textContent = '彻底删除';
                return;
              }
              closeDialog();
              toast('已销毁：删除了 ' + ((r && r.removed) || []).length + ' 个目录');
              setTimeout(function () { location.reload(); }, 600);
            }, function (e) {
              err.textContent = '删除失败：' + (e && e.message ? e.message : '未知错误');
              confirmBtn.disabled = false;
              confirmBtn.textContent = '彻底删除';
            });
          }
        }
      ];
      openDialog('销毁', body, actions);
      var btns = document.querySelectorAll('#dialog .actions .btn');
      confirmBtn = btns[btns.length - 1];
    }, function (e) {
      btn.disabled = false;
      toast('读取待删除路径失败：' + e.message);
    });
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

  /* ---------- 12b) 文件访问范围（受限工作区 / 全局） ---------- */
  // `state.access` 是 GET /api/workspaces/{id}/settings 的最后一次结果。控件永远
  // 显示服务端回报的生效值，而不是本地猜测：切换失败时不会留下一个「看起来已经
  // 生效」的界面。
  function loadAccess() {
    if (!state.workspace) { state.access = null; renderAccess(); return Promise.resolve(); }
    return api('api/workspaces/' + encodeURIComponent(state.workspace.id) + '/settings')
      .then(function (d) { state.access = d; renderAccess(); },
        function (e) {
          state.access = { error: e.message };
          renderAccess();
        });
  }
  function renderAccess() {
    var a = state.access || {};
    var on = a.allow_outside_workspace === true;
    var fixed = a.fixed_by_cli === true;
    var wsBtn = $('access-workspace'), glBtn = $('access-global');
    if (!wsBtn || !glBtn) return;
    wsBtn.setAttribute('aria-checked', on ? 'false' : 'true');
    glBtn.setAttribute('aria-checked', on ? 'true' : 'false');
    // 命令行给了下限：只能收紧，不能在网页上放开；读不到设置时也不让点，
    // 免得两个状态互相打架。
    wsBtn.disabled = fixed || !!a.error;
    glBtn.disabled = fixed || !!a.error;
    $('access-card').classList.toggle('global', on);

    $('access-path').textContent = a.relative_settings_file || '';
    $('access-path').title = a.settings_file || '';
    $('access-meta').textContent = a.error ? '读取失败' : (fixed ? '命令行固定' : (on ? '全局' : '受限'));

    var hint = $('access-hint');
    hint.classList.toggle('warn', on || fixed);
    if (a.error) {
      hint.textContent = '读取设置失败：' + a.error;
    } else if (fixed) {
      hint.textContent = '本进程以 --allow-outside-workspace 启动，边界已解除且不能在网页上关闭；' +
        '要恢复限制，请去掉该参数后重启。';
    } else if (on) {
      hint.textContent = '⚠ 已取消工作区边界：toolkit_exec / toolkit_file 与「文件」页都能访问整台机器上的任意路径。' +
        '设置已写入上面的 settings.yaml，重启后仍然生效。';
    } else {
      hint.textContent = '工具与「文件」页只能在当前工作区内活动，越出根目录的路径会被拒绝。' +
        '切换后立即对新的请求生效；已经开始的那一轮仍按它开始时的设置执行。';
    }
  }
  function setAccessScope(scope) {
    if (!state.workspace) return;
    var a = state.access || {};
    if (a.error) { toast('设置还没读到，请先点「刷新」'); return; }
    var want = scope === 'global';
    if (want === (a.allow_outside_workspace === true)) return;      // 没有变化
    if (!want) { commitAccess(false); return; }                     // 收紧是安全动作
    // 放宽必须先确认：这一步不可逆地扩大了工具的权限，不该由一次误点完成。
    var body = el('div');
    body.appendChild(el('div', 'desc',
      '「全局」意味着 toolkit_file / toolkit_exec 以及「文件」页可以读写本机任意路径，不再局限于当前工作区。'));
    var row = el('div', 'field');
    row.appendChild(el('label', null, '写入'));
    row.appendChild(el('span', 'mono', a.relative_settings_file || '.tea_agent_run/settings.yaml'));
    body.appendChild(row);
    body.appendChild(el('div', 'desc', '重启后仍然生效。只在确实需要跨目录工作时打开，用完切回「受限工作区」。'));
    openDialog('解除工作区边界？', body, [
      { label: '取消', onClick: closeDialog },
      { label: '解除边界', primary: true, onClick: function () { closeDialog(); commitAccess(true); } }
    ]);
  }
  function commitAccess(want) {
    if (!state.workspace) return;
    api('api/workspaces/' + encodeURIComponent(state.workspace.id) + '/settings',
      { method: 'PUT', body: { allow_outside_workspace: want } }).then(function (d) {
        state.access = d;
        renderAccess();
        toast(want ? '已解除工作区边界（全局）' : '已恢复为受限工作区');
        loadSystemPrompt();          // system prompt 里那行说明也跟着变了
      }, function (e) {
        // 409 fixed_by_cli / 400 settings_error：显示真实原因，界面回到服务端状态
        toast('切换失败：' + e.message);
        loadAccess();
      });
  }

  /* ---------- 12) 设置视图（只展示是否已配置密钥，绝不回显任何密钥） ---------- */
  function renderSettings() {
    var home = $('destroy-home');
    if (home && state.health && state.health.tea_home) home.textContent = state.health.tea_home;
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
      if (p.supports_usage || p.usage_console_url) {
        var out = el('div', 'usage-result', '');
        var r2 = el('div', 'row');
        r2.appendChild(el('span', 'k', '额度查询'));
        if (p.supports_usage) {
          r2.appendChild(el('span', 'mono', p.usage_type || 'minimax_token_plan'));
          r2.appendChild(btn('btn', '查询', function () { queryUsageFor(p, out); }));
        } else {
          // 查不了的 provider：至少给一个「自己去看」的入口。
          r2.appendChild(el('span', 'mono', '该提供方只能用控制台查看'));
        }
        if (p.usage_console_url) {
          r2.appendChild(btn('btn', '打开控制台', function () { openConsole(p.usage_console_url); }));
        }
        card.appendChild(r2);
        card.appendChild(out);
      }
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
    $('chat-scroll').addEventListener('scroll', function () {
      state.stickBottom = nearBottom();
      scheduleTurnSpy();
    });
    // 窗口宽度变化会改变换行，进而改变每条消息的位置：重新算一次当前轮次。
    window.addEventListener('resize', function () { hideTurnTip(); scheduleTurnSpy(); });
    $('input').addEventListener('input', autoGrow);
    // 粘贴图片：只拦下含图片的剪贴板内容，普通文本照常粘贴。
    $('input').addEventListener('paste', function (e) {
      var files = filesFromDataTransfer(e.clipboardData).filter(function (f) {
        return /^image\//.test(f.type || '');
      });
      if (!files.length) return;
      e.preventDefault();
      files.forEach(addAttachment);
    });
    // 拖放图片到输入框，同样是常见操作。
    ['dragenter', 'dragover'].forEach(function (ev) {
      $('input').addEventListener(ev, function (e) { e.preventDefault(); });
    });
    $('input').addEventListener('drop', function (e) {
      var files = filesFromDataTransfer(e.dataTransfer).filter(function (f) {
        return /^image\//.test(f.type || '');
      });
      if (!files.length) return;
      e.preventDefault();
      files.forEach(addAttachment);
    });
    $('input').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        // 生成中：进入排队；空闲：正常发送。分工明确，不会因为按了 Enter 就
        // 在背后开第二轮把当前这一轮顶掉。
        if (state.streaming) queueMessage(); else sendMessage();
      }
    });
    $('send').onclick = function () { if (state.streaming) stopStream(); else sendMessage(); };
    var detachBtn = $('detach');
    if (detachBtn) detachBtn.onclick = detachStream;
    $('sidebar-hide').onclick = function () { toggleSidebar(true); };
    $('sidebar-show').onclick = function () { toggleSidebar(false); };
    // 点击背景（不是图片本身）或双击关闭；Esc 在下面的全局键盘处理里。
    $('image-viewer').onclick = function (e) {
      if (e.target === this) closeImageViewer();
    };
    $('image-viewer-img').ondblclick = function (e) {
      e.preventDefault();
      closeImageViewer();
    };
    $('todo-head').onclick = function () {
      state.todoCollapsed = !state.todoCollapsed;
      renderTodos();
    };
    $('session-export').onclick = exportSession;
    $('session-link').onclick = copyTopicLink;
    $('session-import').onclick = function () {
      if (!state.workspace) { toast('请先选择工作区'); return; }
      var input = document.createElement('input');
      input.type = 'file';
      input.accept = '.html,.htm,.json,text/html,application/json';
      input.onchange = function () {
        var f = input.files && input.files[0];
        if (f) importSessionFile(f);
      };
      input.click();
    };
    $('new-session').onclick = function () {
      newSessionDraft();          // 会先离开正在跟随的轮次（服务端照跑）
    };
    $('banner-retry').onclick = recheckNow;
    $('status').onclick = recheckNow;
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
      if (line) {
        var row = line.parentNode;
        state.expanded[row.dataset.key] = row.classList.toggle('open');
        // 轨迹面板内容按需加载：折叠时只有一行摘要，展开才请求完整记录。
        if (row.classList.contains('traj-row') && row.classList.contains('open')) {
          loadTrajectoryPanel(row);
        }
      }
    });
    $('overlay').onclick = function (e) {
      if (setupGate.open) return;                       // 首次配置必须完成，不能点掉
      if (e.target === $('overlay')) closeDialog();
    };
    $('files-save').onclick = saveFile;
    $('files-download').onclick = downloadRunArchive;
    $('files-revert').onclick = function () {
      state.files.content = state.files.original;
      $('files-editor').value = state.files.original;
      renderFiles();
    };
    $('files-editor').addEventListener('input', function () {
      state.files.content = $('files-editor').value;
      renderFiles();
    });
    $('files-preview-toggle').onclick = function () {
      state.files.preview = !state.files.preview;
      renderFiles();
    };
    $('memory-add').onclick = addMemory;
    $('memory-refresh').onclick = loadMemory;
    $('system-prompt-save').onclick = saveSystemPrompt;
    $('system-prompt-preview').onclick = previewSystemPrompt;
    // 「恢复默认」：把编辑框换回默认提示词（不是清空）。保存时服务端看到内容与
    // 默认一致，就会删掉工作区覆盖，于是后续默认变更仍然能影响到这里。
    $('system-prompt-default').onclick = function () {
      $('system-prompt-editor').value = state.systemPrompt.defaultPrompt;
      state.systemPrompt.touched = true;
      renderSystemPrompt();
    };
    $('system-prompt-revert').onclick = function () {
      state.systemPrompt.touched = false;
      renderSystemPrompt();
    };
    $('system-prompt-editor').addEventListener('input', function () {
      state.systemPrompt.touched = true;
      renderSystemPrompt();
    });
    $('agent-doc-save').onclick = saveAgentDoc;
    $('agent-doc-revert').onclick = function () {
      state.agentDoc.touched = false;
      $('agent-doc-editor').value = state.agentDoc.original;
      renderAgentDoc();
    };
    $('agent-doc-editor').addEventListener('input', function () {
      state.agentDoc.touched = true;
      renderAgentDoc();
    });
    $('logs-refresh').onclick = loadLogs;
    $('access-workspace').onclick = function () { setAccessScope('workspace'); };
    $('access-global').onclick = function () { setAccessScope('global'); };
    $('logs-autoscroll').onchange = function () {
      state.logsAutoScroll = $('logs-autoscroll').checked;
      saveLS();
      if (state.logsAutoScroll) $('logs-pre').scrollTop = $('logs-pre').scrollHeight;
    };
    $('usage-chip').onclick = function () {
      var p = usageProvider();
      if (p && !p.supports_usage && p.usage_console_url) { openConsole(p.usage_console_url); return; }
      loadUsage();
    };
    // 额度会随时间自己恢复，所以徽标上的倒计时要自己走，过期了再重新查一次。
    setInterval(function () {
      var p = usageProvider();
      if (!p || !p.supports_usage) return;
      if (!state.usageAt || Date.now() - state.usageAt > USAGE_REFRESH_MS) loadUsage();
      else renderUsage();
    }, 30000);

    $('destroy-run').onclick = openDestroyDialog;
    $('settings-refresh').onclick = function () {
      loadProviders(); loadTools(); loadSystemPrompt(); loadAccess();
      loadHealth(true).catch(function () {});
    };
    // 「重置 provider」：打开和首次运行同一个配置界面（快捷 key 通道 + 上传 + 粘贴），
    // 区别是它先摆出覆盖的风险，而且可以取消。真正的覆盖发生在服务端校验通过之后。
    $('provider-reset').onclick = function () {
      loadConfigStatus().then(function (st) { openSetupGate(st || {}, { reset: true }); });
    };
    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {   // Ctrl/Cmd+K 聚焦输入框
        e.preventDefault();
        if (state.view !== 'chat') showView('chat');
        $('input').focus();
      }
      if ((e.ctrlKey || e.metaKey) && (e.key === 'b' || e.key === 'B')) { // Ctrl/Cmd+B 收起侧栏
        e.preventDefault();
        toggleSidebar();
      }
      if (e.key === 'Escape') {                                          // Esc 关闭浮层
        if (closeImageViewer()) return;                                  // 先关图片查看器
        if (state.fork) clearForkPoint();                                // 先撤掉分叉点
        closeDropdowns();
        if (!setupGate.open) closeDialog();
      }
    });
  }

  function init() {
    var ls = loadLS();
    state.view = VIEWS.indexOf(ls.view) >= 0 ? ls.view : 'chat';
    state.model = ls.model || null;
    state.logsAutoScroll = ls.logs_autoscroll !== false;
    $('logs-autoscroll').checked = state.logsAutoScroll;
    state.sidebarHidden = ls.sidebar_hidden === true;
    applySidebar();
    updateTitle();                       // 加载工作区之前也先立住一个确定的标题
    state.stats = emptyStats();
    bind();
    updateComposer();
    renderMetrics();
    renderWorkspaceChip(); renderModelChip(); renderSessions(); renderChat();
    showView(state.view);
    subscribeTaskEvents();     // 后台跑完的任务也要能闪一下标签页
    var boot = function () { return Promise.all([loadProviders(), loadTools(), loadWorkspaces()]); };
    loadHealth().then(function (h) {
      if (h && h.config_loaded === false) {
        // Nothing else can work yet: ask for the file instead of booting.
        loadConfigStatus().then(function (st) { openSetupGate(st || {}); },
          function () { openSetupGate({}); });
        return null;
      }
      return boot();
    }, boot).catch(function () {});
    startLogsTimer();
    startHealthTimer();
    autoGrow();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
