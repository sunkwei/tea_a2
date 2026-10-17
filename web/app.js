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
    trajectory: [], trajFull: {}, lastTrajectoryId: null,
    stats: null, liveTokens: 0, liveStartAt: 0, metricsTimer: null,
    turnStartAt: 0, turnSteps: 0, mdBase: '',
    files: { dir: '', entries: [], path: null, content: '', original: '', truncated: false, readOnly: false, preview: false, size: 0 },
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
  function setMetric(id, text, title, cls) {
    var v = $(id + '-v');
    if (v) v.textContent = text;
    var bar = $(id + '-bar');
    if (bar && cls && cls.width != null) bar.style.width = cls.width;
    var box = $(id);
    if (!box) return;
    box.classList.toggle('warn', !!(cls && cls.warn));
    box.classList.toggle('bad', !!(cls && cls.bad));
    box.classList.toggle('est', !!(cls && cls.est));
    if (title) box.title = title;
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
        ? 'provider 未返回缓存字段（DeepSeek: prompt_cache_hit_tokens；OpenAI: prompt_tokens_details.cached_tokens）'
        : '前缀缓存命中 ' + fmtTokens(s.cached) + ' / ' + fmtTokens(s.prompt) + ' prompt tokens',
      { width: (cachePct == null ? 0 : Math.min(100, cachePct)) + '%' });

    var ctxPct = s.max > 0 ? Math.round(s.context * 100 / s.max) : null;
    setMetric('metric-context',
      s.context ? (est ? '≈' : '') + fmtTokens(s.context) + (s.max > 0 ? ' / ' + fmtTokens(s.max) : '') +
        (ctxPct == null ? '' : ' · ' + ctxPct + '%') : '—',
      (s.max > 0 ? '上下文窗口 ' + s.context + ' / ' + s.max + ' tokens（' + ctxPct + '%）'
                 : '本次请求 prompt tokens：' + s.context + '（模型未配置 max_context_tokens）') +
        (est ? '\n本地估算：provider 未返回 usage' : ''),
      { width: (ctxPct == null ? 0 : Math.min(100, ctxPct)) + '%', warn: ctxPct != null && ctxPct >= 70, bad: ctxPct != null && ctxPct >= 90 });

    var parts = [];
    if (s.output) parts.push('输出 ' + fmtTokens(s.output) + ' tok');
    if (s.total) parts.push('合计 ' + fmtTokens(s.total) + ' tok');
    if (s.ttft != null && s.ttft > 0) parts.push('首字 ' + fmtSec(s.ttft));
    setMetric('metric-tokens', parts.length ? parts.join(' · ') : '—',
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
  function handleEvent(ev) {
    if (ev.type === 'ping') return;                              // 心跳：保持连接存活即可
    if (!state.live) return;
    if (ev.type === 'start') {
      if (ev.session_id && (!state.session || state.session.id !== ev.session_id)) {
        state.session = { id: ev.session_id, title: '新会话' };
        saveLS();
      }
      state.stats = emptyStats();                 // 新一轮：指标栏先清空，等 metrics 到达再填
      state.liveTokens = 0; state.liveStartAt = 0;
      state.turnSteps = 0; state.turnStartAt = Date.now();
      renderMetrics();
      return;
    }
    if (ev.type === 'reasoning_delta') {
      state.live.reasoning += ev.text || '';
      noteLiveTokens(ev.text);
      renderLive(); scrollMaybe(); return;
    }
    if (ev.type === 'content_delta') {
      var last = state.live.blocks[state.live.blocks.length - 1];
      if (!last || last.type !== 'md') { last = { type: 'md', text: '' }; state.live.blocks.push(last); }
      last.text += ev.text || '';
      noteLiveTokens(ev.text);
      renderLive(); scrollMaybe(); return;
    }
    if (ev.type === 'tool_call' || ev.type === 'tool_start') {
      if (ev.type === 'tool_start') { state.turnSteps++; renderMetrics(); }
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
      renderLive(); scrollMaybe(); return;
    }
    if (ev.type === 'done' || ev.type === 'stopped') {
      state.ended = true;
      state.liveTokens = 0;                    // 服务端数字到了，丢弃本地估算
      if (ev.trajectory_id) state.lastTrajectoryId = ev.trajectory_id;
      if (ev.type === 'done') applyMetrics(ev.metrics, ev.usage);
      renderMetrics();
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
    updateComposer();
    if (on) {
      startMetricsTicker();
    } else {
      stopMetricsTicker();
      state.liveTokens = 0; state.liveStartAt = 0;
      renderMetrics();
    }
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
    var finish = function () { renderChat(); renderMetrics(); scrollMaybe(); };
    if (!sid) { finish(); return; }
    // 结束后重新拉取，保证渲染状态与服务器一致；轨迹是这一轮结束后才落盘的，
    // 所以必须和会话一起重新取回来才能挂到对应的 assistant 消息上。
    api('api/sessions/' + encodeURIComponent(sid)).then(function (d) {
      if (state.session && state.session.id === sid) {
        state.session = d.session || state.session;
        state.messages = d.messages || [];
      }
    }, function () { }).then(loadTrajectories).then(function () {
      attachTrajectories();
    }).then(finish, finish);
    loadSessions();
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

      var row = el('button', 'sess-row' + (active ? ' active' : ''));
      row.type = 'button';
      row.disabled = state.streaming;
      row.appendChild(el('span', 'sess-title', s.title || s.id));
      var meta = el('span', 'sess-meta');
      meta.appendChild(el('span', null, fmtRelTime(s.updated_at || s.created_at) || '—'));
      if (s.message_count != null) {
        meta.appendChild(el('span', null, '·'));
        meta.appendChild(el('span', null, s.message_count + ' 条'));
      }
      row.appendChild(meta);
      row.title = (s.title || s.id) + '\n' + fmtTime(s.updated_at || s.created_at) +
        ' · ' + (s.message_count || 0) + ' 条消息\n点击切换会话';
      row.onclick = function () { if (!state.streaming) openSession(s.id); };
      item.appendChild(row);

      var tools = el('div', 'sess-tools');
      var ren = btn('sess-tool', '改名', function (e) {
        e.stopPropagation();
        if (!state.streaming) renameSessionDialog(s);
      });
      ren.title = '重命名会话';
      ren.disabled = state.streaming;
      var del = btn('sess-tool danger', '删除', function (e) {
        e.stopPropagation();
        if (!state.streaming) deleteSessionDialog(s);
      });
      del.title = '删除会话';
      del.disabled = state.streaming;
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
            .then(function () { toast('已重命名'); return loadSessions(); },
              function (e) { toast('重命名失败：' + e.message); });
        }
      }
    ]);
    setTimeout(function () { input.focus(); input.select(); }, 0);
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
        state.trajectory = []; state.lastTrajectoryId = null;
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
      state.stats = statsFor(sid);                 // 恢复该会话上一轮的指标
      state.turnSteps = num(state.stats.steps);
      state.turnStartAt = 0;
      state.liveTokens = 0; state.liveStartAt = 0;
      state.lastTrajectoryId = null;
      // 轨迹按需单独取：摘要用于折叠行，展开时再按 turn_id 拉完整记录。
      return loadTrajectories().then(function () {
        attachTrajectories();
        saveLS(); renderSessions(); renderChat(); renderMetrics(); scrollMaybe(true);
      });
    }, function (e) { toast('加载会话失败：' + e.message); });
  }
  function newSessionDraft() {
    state.session = null; state.messages = []; state.live = null; state.streamError = null;
    state.stats = emptyStats(); state.liveTokens = 0; state.liveStartAt = 0;
    state.turnSteps = 0; state.turnStartAt = 0;
    state.trajectory = []; state.lastTrajectoryId = null;
    saveLS(); renderSessions(); renderChat(); renderMetrics(); showView('chat'); $('input').focus();
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
    $('files-save').disabled = !f.path || f.readOnly || !dirty || isImagePath(f.path);
    $('files-revert').disabled = !f.path || !dirty || isImagePath(f.path);
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
      if (line) {
        var row = line.parentNode;
        state.expanded[row.dataset.key] = row.classList.toggle('open');
        // 轨迹面板内容按需加载：折叠时只有一行摘要，展开才请求完整记录。
        if (row.classList.contains('traj-row') && row.classList.contains('open')) {
          loadTrajectoryPanel(row);
        }
      }
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
    $('files-preview-toggle').onclick = function () {
      state.files.preview = !state.files.preview;
      renderFiles();
    };
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
    state.stats = emptyStats();
    bind();
    updateComposer();
    renderMetrics();
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
