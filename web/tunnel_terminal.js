/* The terminal that the tunnel carries.
 *
 * The sidecar (a2_tunnel) hands out a real shell on the machine it runs on;
 * this page is the browser half of it. The page is served *through the
 * tunnel* - the operator's browser talks to 127.0.0.1:<port> on their own
 * machine, the sidecar forwards the bytes, and the far end answers. So every
 * request here is a relative URL: it goes the same way as the page itself.
 *
 * The VT engine (the screen model, the colours, the key mapping) is shared
 * with the agent's own terminal - web/term.js. Only the plumbing around it
 * lives here: the session, the long poll, the selection, the scrollback and
 * the files in and out.
 *
 * Classic script, no build step: it defines nothing global.
 */
(function () {
  'use strict';

  var TeaTerm = window.TeaTerm || {};
  var TermScreen = TeaTerm.TermScreen;
  var termLineHtml = TeaTerm.termLineHtml;
  var keyBytes = TeaTerm.keyBytes;

  function $(id) { return document.getElementById(id); }

  /* ---- 与服务端说话 --------------------------------------------------- */

  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || 'GET', headers: {} };
    if (opts.body !== undefined && opts.body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    if (opts.raw !== undefined && opts.raw !== null) {
      init.headers['Content-Type'] = 'application/octet-stream';
      init.body = opts.raw;
    }
    return fetch(path, init).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        if (text) {
          try { data = JSON.parse(text); } catch (e) { data = null; }
        }
        if (!res.ok) {
          var message = 'HTTP ' + res.status;
          if (data && data.error && data.error.message) message = data.error.message;
          var err = new Error(message);
          err.status = res.status;
          throw err;
        }
        return data || {};
      });
    });
  }

  function bytesFromBase64(b64) {
    var bin = atob(b64 || '');
    var out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  // UTF-8 decoding that survives a character split across two chunks.
  function makeDecoder() {
    if (typeof TextDecoder !== 'undefined') {
      var td = new TextDecoder('utf-8');
      return function (bytes) { return td.decode(bytes, { stream: true }); };
    }
    var pending = [];
    return function (bytes) {
      var buf = pending.concat(Array.prototype.slice.call(bytes));
      var out = '';
      var i = 0;
      while (i < buf.length) {
        var b = buf[i];
        var need = 0;
        var code = 0;
        if (b < 0x80) { code = b; need = 0; }
        else if ((b & 0xE0) === 0xC0) { code = b & 0x1F; need = 1; }
        else if ((b & 0xF0) === 0xE0) { code = b & 0x0F; need = 2; }
        else if ((b & 0xF8) === 0xF0) { code = b & 0x07; need = 3; }
        else { out += '�'; i++; continue; }
        if (i + need >= buf.length) break;          // half a character: wait
        var ok = true;
        for (var k = 1; k <= need; k++) {
          var c = buf[i + k];
          if ((c & 0xC0) !== 0x80) { ok = false; break; }
          code = (code << 6) | (c & 0x3F);
        }
        if (!ok) { out += '�'; i++; continue; }
        if (code > 0xFFFF) {
          code -= 0x10000;
          out += String.fromCharCode(0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF));
        } else {
          out += String.fromCharCode(code);
        }
        i += need + 1;
      }
      pending = buf.slice(i);
      return out;
    };
  }

  /* ---- 状态 ------------------------------------------------------------ */

  var state = {
    id: '',            // the session on the far end
    cursor: 0,         // how many bytes we have read
    ended: false,
    pumping: false,
    term: null,
    decoder: null,
    cap: null,
    size: { cols: 80, rows: 24 },
    charW: 8,
    lineH: 18,
    sel: {
      active: false,
      dragging: false,
      anchor: { id: 0, caret: 0 },
      head: { id: 0, caret: 0 },
      from: 0,
      to: 0
    }
  };

  function note(text, warn) {
    var node = $('note');
    if (!node) return;
    node.textContent = text || '';
    node.classList.toggle('warn', !!warn);
  }

  function meta(text) {
    var node = $('meta');
    if (node) node.textContent = text || '';
  }

  function focusInput() {
    var input = $('input');
    if (!input) return;
    try { input.focus({ preventScroll: true }); } catch (e) { input.focus(); }
  }

  /* ---- 会话 ------------------------------------------------------------ */

  function measure() {
    var screen = $('screen');
    var fallback = { cols: 80, rows: 24 };
    if (!screen || !screen.clientWidth) return fallback;
    var probe = document.createElement('span');
    probe.className = 'term-row';
    probe.style.position = 'absolute';
    probe.style.visibility = 'hidden';
    probe.textContent = new Array(81).join('M');
    screen.appendChild(probe);
    var rect = probe.getBoundingClientRect();
    var style = window.getComputedStyle(probe);
    var lineHeight = parseFloat(style.lineHeight);
    if (!lineHeight || isNaN(lineHeight)) lineHeight = parseFloat(style.fontSize) * 1.5;
    screen.removeChild(probe);
    var cw = rect.width / 80;
    if (!cw) return fallback;
    state.charW = cw;
    state.lineH = lineHeight;
    var cols = Math.max(20, Math.min(500, Math.floor((screen.clientWidth - 26) / cw)));
    var rows = Math.max(5, Math.min(200, Math.floor((screen.clientHeight - 22) / lineHeight)));
    return { cols: cols, rows: rows };
  }

  function start() {
    api('api/term').then(function (cap) {
      state.cap = cap;
      if (!cap || !cap.available) {
        meta('这台机器上没有可用的终端');
        note((cap && cap.reason) ? cap.reason : '这台机器不能开 shell', true);
        return;
      }
      meta((cap.shell || 'shell') + ' · ' + (cap.base_dir || ''));
      return ensureSession();
    }, function (e) {
      meta('连不上远端');
      note('隧道没有就绪：' + (e && e.message ? e.message : e), true);
    });
  }

  function ensureSession() {
    if (state.id && !state.ended) {
      pump();
      return Promise.resolve(state.id);
    }
    if (!state.cap || !state.cap.available) return Promise.resolve('');
    var size = measure();
    return api('api/term/session', {
      method: 'POST',
      body: { cols: size.cols, rows: size.rows }
    }).then(function (d) {
      state.id = d.id || '';
      state.cursor = 0;
      state.ended = false;
      state.size = size;
      if (!state.decoder) state.decoder = makeDecoder();
      state.term = new TermScreen(size.cols, size.rows);
      state.term.reply = function (text) { send(text); };
      var info = d.session || {};
      meta((state.cap.shell || 'shell') + (info.cwd ? ' · ' + info.cwd : ''));
      note('这是远端机器上的真实 shell：它能碰那台机器上的任何东西。');
      render();
      pump();
      focusInput();
      return state.id;
    }, function (e) {
      note('开不了 shell：' + (e && e.message ? e.message : e), true);
      return '';
    });
  }

  // Ask for what is new; the far end holds the request until there is
  // something to say, so an idle terminal costs no traffic at all.
  function pump() {
    if (!state.id || state.ended || state.pumping) return;
    state.pumping = true;
    var id = state.id;
    api('api/term/session/' + encodeURIComponent(id) + '/output?cursor=' + state.cursor +
        '&wait=20000')
      .then(function (d) {
        state.pumping = false;
        if (state.id !== id) return;
        state.cursor = d.cursor || 0;
        if (d.resync && state.term) state.term.reset();
        if (d.data_b64 && state.term && state.decoder) {
          var text = state.decoder(bytesFromBase64(d.data_b64));
          if (text) {
            state.term.feed(text);
            render();
          }
        }
        if (d.ended) {
          state.ended = true;
          note('这个 shell 已经退出了（退出码 ' + (d.exit_code != null ? d.exit_code : '?') +
               '）。开一个新会话继续。');
          return;
        }
        pump();
      })
      .catch(function (e) {
        state.pumping = false;
        if (e && e.status === 404) {
          // The session is gone: the shell ended (and the far end forgot it),
          // or the sidecar restarted. Retrying would only produce 404s.
          state.ended = true;
          note('这个会话已经不在了（shell 退出了，或侧载重启了）。开一个新会话继续。', true);
          return;
        }
        setTimeout(pump, 1000);
      });
  }

  function send(data) {
    if (!data || !state.id || state.ended) return;
    api('api/term/session/' + encodeURIComponent(state.id) + '/input', {
      method: 'POST',
      body: { data: data }
    }).catch(function () {
      note('输入没有送到 shell。', true);
    });
  }

  function resize() {
    if (!state.id || state.ended) return;
    var size = measure();
    if (size.cols === state.size.cols && size.rows === state.size.rows) return;
    state.size = size;
    if (state.term) state.term.resize(size.cols, size.rows);
    clearSel();
    render();
    api('api/term/session/' + encodeURIComponent(state.id) + '/resize', {
      method: 'POST',
      body: { cols: size.cols, rows: size.rows }
    }).catch(function () {});
  }

  function newSession() {
    if (state.id) {
      var old = state.id;
      state.id = '';
      state.ended = true;
      api('api/term/session/' + encodeURIComponent(old), { method: 'DELETE' }).catch(function () {});
    }
    state.term = new TermScreen(state.size.cols, state.size.rows);
    state.cursor = 0;
    render();
    ensureSession();
  }

  /* ---- 画面 ------------------------------------------------------------ */

  // Which part of the buffer is on screen: the scrollback plus the last rows
  // (viewOffset says how far back we scrolled).
  function viewWindow() {
    var t = state.term;
    if (!t) return { start: 0, end: 0 };
    var total = t.back.length + t.lines.length;
    var end = Math.max(0, total - t.viewOffset);
    var start = Math.max(0, end - t.rows);
    return { start: start, end: end };
  }

  function render() {
    var t = state.term;
    if (!t) return;
    var rows = $('rows');
    if (!rows) return;
    var all = t.back.concat(t.lines);
    var win = viewWindow();
    var sel = state.sel.active ? state.sel : null;
    var html = [];
    for (var i = win.start; i < win.end; i++) {
      var from = -1, to = -1;
      if (sel) {
        // The selection is an absolute position (row * cols + column); here it
        // becomes a range inside this row.
        var rowFrom = (t.dropped + i) * t.cols;
        var a = Math.max(sel.from, rowFrom) - rowFrom;
        var b = Math.min(sel.to, rowFrom + t.cols) - rowFrom;
        if (b > a) { from = a; to = b; }
      }
      html.push('<div class="term-row">' + termLineHtml(all[i], from, to) + '</div>');
    }
    rows.innerHTML = html.join('');
    var screen = $('screen');
    if (screen && t.viewOffset === 0) screen.scrollTop = screen.scrollHeight;
    cursorUpdate();
  }

  function cursorUpdate() {
    var cursor = $('cursor');
    var screen = $('screen');
    var rows = $('rows');
    var t = state.term;
    if (!cursor || !screen || !rows || !t) return;
    if (!t.cursorVisible) {
      cursor.classList.add('hidden');
      return;
    }
    var all = t.back.concat(t.lines);
    var win = viewWindow();
    var row = t.back.length + t.cy;
    if (row < win.start || row >= win.end) {
      cursor.classList.add('hidden');
      return;
    }
    var line = all[row];
    var cell = line ? line[t.cx] : null;
    cursor.textContent = (cell && cell.c) ? cell.c : ' ';
    var sRect = screen.getBoundingClientRect();
    var rRect = rows.getBoundingClientRect();
    cursor.style.left = Math.round(rRect.left - sRect.left + t.cx * state.charW) + 'px';
    cursor.style.top = Math.round(rRect.top - sRect.top + (row - win.start) * state.lineH) + 'px';
    cursor.style.width = Math.round(state.charW) + 'px';
    cursor.style.height = Math.round(state.lineH) + 'px';
    cursor.classList.remove('hidden');
  }

  /* ---- 选区 ------------------------------------------------------------
     The browser's own selection cannot be used here: the screen is rebuilt
     (innerHTML) on every output, so the native selection is gone the moment
     there is new output. So it is drawn by hand:
       * the selection remembers an absolute position (row * cols + column);
       * rows keep their number when they scroll into the scrollback, so new
         output does not move the selection (only the oldest row falling out
         of the scrollback does, and `dropped` compensates for that);
       * the hit test measures the DOM (a Chinese character takes two cells in
         a monospace font, so counting characters would be off);
       * the highlight is a span inside the row, so it follows the text. */

  function selUpdate(anchor, head) {
    var s = state.sel;
    var cols = state.term.cols;
    s.anchor = anchor;
    s.head = head;
    var p1 = anchor.id * cols + anchor.caret;
    var p2 = head.id * cols + head.caret;
    s.from = Math.min(p1, p2);
    s.to = Math.max(p1, p2);
    s.active = s.to > s.from;
  }

  function caretOf(id, caret) {
    var cols = state.term.cols;
    if (caret < 0) caret = 0;
    if (caret > cols) caret = cols;
    return { id: id, caret: caret };
  }

  function clearSel() {
    var s = state.sel;
    if (!s) return;
    var had = s.active;
    s.active = false;
    s.dragging = false;
    s.from = 0;
    s.to = 0;
    if (had) render();
  }

  function selectedText() {
    var t = state.term;
    var s = state.sel;
    if (!t || !s || !s.active || s.to <= s.from) return '';
    var all = t.back.concat(t.lines);
    var cols = t.cols;
    var out = [];
    var firstId = Math.floor(s.from / cols);
    var lastId = Math.floor((s.to - 1) / cols);
    for (var id = firstId; id <= lastId; id++) {
      var k = id - t.dropped;
      if (k < 0 || k >= all.length) continue;
      var line = all[k] || [];
      var rowFrom = id * cols;
      var a = Math.max(s.from - rowFrom, 0);
      var b = Math.min(s.to - rowFrom, cols);
      var text = '';
      for (var c = a; c < b; c++) {
        var cell = line[c];
        text += (cell && cell.c != null) ? cell.c : ' ';
      }
      out.push(text.replace(/[ \t]+$/, ''));
    }
    return out.join('\n');
  }

  // Which cell is the pointer on?
  function cellAt(clientX, clientY) {
    var rows = $('rows');
    if (!rows || !state.term) return null;
    var rowEls = rows.children;
    if (!rowEls.length) return null;
    var idx = -1;
    var el = document.elementFromPoint(clientX, clientY);
    while (el && el.parentNode && el.parentNode !== rows) el = el.parentNode;
    if (el && el.parentNode === rows) {
      for (var i = 0; i < rowEls.length; i++) {
        if (rowEls[i] === el) { idx = i; break; }
      }
    }
    if (idx < 0) {
      // Between two rows: count the rows.
      var rowsRect = rows.getBoundingClientRect();
      var lineH = state.lineH > 0 ? state.lineH : 18;
      idx = Math.floor((clientY - rowsRect.top) / lineH);
    }
    if (idx < 0) idx = 0;
    if (idx >= rowEls.length) idx = rowEls.length - 1;
    var cols = state.term.cols;
    var hit = colInRow(rowEls[idx], clientX);
    var col = hit.col;
    if (col > cols) col = cols;
    if (col < 0) col = 0;
    var win = viewWindow();
    return { id: state.term.dropped + win.start + idx, col: col, after: hit.after,
             rowIndex: idx };
  }

  // Which character of a row is the pointer on? Measured with the DOM: a
  // Chinese character takes two cells in a monospace font, so counting
  // characters would be off. `after` says whether the pointer is on the right
  // half of that character - the caret then goes after it, not before.
  function colInRow(el, clientX) {
    var text = el.textContent || '';
    if (!text.length) return { col: 0, after: false };
    var parts = [];
    var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
    var node, base = 0;
    while ((node = walker.nextNode())) {
      parts.push({ node: node, base: base, len: node.nodeValue.length });
      base += node.nodeValue.length;
    }
    for (var p = 0; p < parts.length; p++) {
      var it = parts[p];
      var range = document.createRange();
      range.selectNodeContents(it.node);
      var rect = range.getBoundingClientRect();
      if (!rect.width && !rect.height) continue;
      if (clientX < rect.left) return { col: it.base, after: false };
      if (clientX <= rect.right) {
        var lo = 0, hi = it.len;
        while (lo < hi) {
          var mid = (lo + hi + 1) >> 1;
          range.setStart(it.node, 0);
          range.setEnd(it.node, mid);
          var r2 = range.getBoundingClientRect();
          if (r2.right <= clientX) lo = mid; else hi = mid - 1;
        }
        var after = true;
        if (lo < it.len) {
          range.setStart(it.node, lo);
          range.setEnd(it.node, lo + 1);
          var cr = range.getBoundingClientRect();
          after = clientX >= (cr.left + cr.right) / 2;
        }
        return { col: it.base + lo, after: after };
      }
    }
    return { col: text.length, after: true };
  }

  // A word: letters, digits, underscore, hyphen, dot (like a browser).
  var WORD_RE = /[A-Za-z0-9_.\-À-￿]/;

  function wordRange(line, col) {
    var len = line.length;
    if (!len) return { from: 0, to: 0 };
    if (col >= len) col = len - 1;
    if (col < 0) col = 0;
    var isWord = function (c) { return !!c && WORD_RE.test(c); };
    var head = line[col] ? line[col].c : ' ';
    if (!isWord(head)) return { from: col, to: col + 1 };
    var a = col, b = col;
    while (a > 0 && isWord(line[a - 1] ? line[a - 1].c : '')) a--;
    while (b < len - 1 && isWord(line[b + 1] ? line[b + 1].c : '')) b++;
    return { from: a, to: b + 1 };
  }

  var clickCount = { count: 0, time: 0, id: -1, col: -1 };

  function onMouseDown(e) {
    if (e.button !== 0 || !state.term) return;
    e.preventDefault();               // no native selection; we draw our own
    focusInput();
    var cell = cellAt(e.clientX, e.clientY);
    if (!cell) return;
    var now = Date.now();
    if (clickCount.id === cell.id && clickCount.col === cell.col && now - clickCount.time < 450) {
      clickCount.count++;
    } else {
      clickCount.count = 1;
    }
    clickCount.time = now;
    clickCount.id = cell.id;
    clickCount.col = cell.col;

    var t = state.term;
    if (clickCount.count >= 3) {
      // Three clicks: the whole line.
      selUpdate(caretOf(cell.id, 0), caretOf(cell.id, t.cols));
    } else if (clickCount.count === 2) {
      // Two clicks: the word under the pointer.
      var all = t.back.concat(t.lines);
      var line = all[cell.id - t.dropped] || [];
      var w = wordRange(line, cell.col);
      selUpdate(caretOf(cell.id, w.from), caretOf(cell.id, w.to));
    } else {
      // One click: a caret. Which side of the character it lands on depends
      // on which half of it the pointer is.
      var at = cell.after ? cell.col + 1 : cell.col;
      selUpdate(caretOf(cell.id, at), caretOf(cell.id, at));
    }
    state.sel.dragging = true;
    render();
  }

  function onMouseMove(e) {
    if (!state.sel.dragging || !state.term) return;
    var cell = cellAt(e.clientX, e.clientY);
    if (!cell) return;
    var cols = state.term.cols;
    // Dragging forwards: the character under the pointer is part of the
    // selection. Dragging backwards: the selection stops before it.
    var anchorRaw = state.sel.anchor.id * cols + state.sel.anchor.caret;
    var headRaw = cell.id * cols + cell.col;
    var caret = headRaw >= anchorRaw ? cell.col + 1 : cell.col;
    if (state.sel.head.id === cell.id && state.sel.head.caret === caret) return;
    selUpdate(state.sel.anchor, caretOf(cell.id, caret));
    render();
  }

  function onMouseUp() {
    if (!state.sel.dragging) return;
    state.sel.dragging = false;
    var text = selectedText();
    if (text) note('选中了 ' + text.length + ' 个字符（按「复制」拿走）');
  }

  /* ---- 复制 / 上传 / 下载 ------------------------------------------------ */

  function copyText(text) {
    if (!text) {
      note('没有选中任何文字。', true);
      return;
    }
    var done = function () { note('已复制 ' + text.length + ' 个字符。'); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text, done); });
      return;
    }
    fallbackCopy(text, done);
  }

  // A page served over plain http has no navigator.clipboard, so there is a
  // fallback: a hidden textarea and execCommand.
  function fallbackCopy(text, done) {
    var box = document.createElement('textarea');
    box.value = text;
    box.setAttribute('readonly', 'readonly');
    box.style.position = 'fixed';
    box.style.left = '-1000px';
    document.body.appendChild(box);
    box.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(box);
    if (ok) done();
    else note('这个浏览器不让网页复制：用「下载」把文件拿走，或者手动选中。', true);
  }

  function upload(file) {
    if (!state.id) {
      note('还没有会话。', true);
      return;
    }
    var dir = ($('path').value || '').trim();
    var url = 'api/term/session/' + encodeURIComponent(state.id) + '/upload?name=' +
              encodeURIComponent(file.name);
    if (dir) url += '&dir=' + encodeURIComponent(dir);
    api(url, { method: 'POST', raw: file }).then(function (d) {
      note('已上传：' + (d.path || file.name) + '（' + d.bytes + ' 字节）');
    }, function (e) {
      note('上传失败：' + (e && e.message ? e.message : e), true);
    });
  }

  function download() {
    if (!state.id) {
      note('还没有会话。', true);
      return;
    }
    var path = ($('path').value || '').trim();
    if (!path) {
      note('先在上面写上要下载的文件名。', true);
      return;
    }
    var url = 'api/term/session/' + encodeURIComponent(state.id) + '/file?path=' +
              encodeURIComponent(path);
    var a = document.createElement('a');
    a.href = url;
    a.setAttribute('download', '');
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    note('正在下载：' + path);
  }

  /* ---- 接线 ------------------------------------------------------------ */

  function wire() {
    $('screen').addEventListener('mousedown', onMouseDown);
    $('screen').addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
    $('screen').addEventListener('contextmenu', function (e) {
      e.preventDefault();
      focusInput();
    });
    // The wheel scrolls the scrollback, not the page: the rows are rebuilt on
    // every output, so the browser's own scrollbar has nothing to scroll.
    $('screen').addEventListener('wheel', function (e) {
      if (!state.term) return;
      var t = state.term;
      var max = t.back.length;
      var next = t.viewOffset + (e.deltaY > 0 ? 3 : -3);
      if (next < 0) next = 0;
      if (next > max) next = max;
      if (next === t.viewOffset) return;
      e.preventDefault();
      t.viewOffset = next;
      render();
      if (next > 0) note('往回翻了 ' + next + ' 行（滚回去回到最新）');
      else note('');
    }, { passive: false });

    $('btn-new').onclick = function () { newSession(); };
    $('btn-copy').onclick = function () { copyText(selectedText()); };
    $('btn-upload').onclick = function () { $('file').click(); };
    $('btn-download').onclick = download;
    $('file').onchange = function () {
      var f = $('file').files && $('file').files[0];
      $('file').value = '';             // the same file can be picked again
      if (f) upload(f);
    };

    // The keyboard goes to the invisible input: it has to be a real input so
    // that the IME has something to compose into.
    var input = $('input');
    input.addEventListener('keydown', function (e) {
      if (e.isComposing) return;
      var bytes = keyBytes(e);
      if (bytes === null) return;
      e.preventDefault();
      send(bytes);
    });
    input.addEventListener('input', function () {
      // The IME (and the mobile keyboard) put the text into the input.
      var text = input.value;
      if (!text) return;
      input.value = '';
      send(text.replace(/\r\n/g, '\r').replace(/\n/g, '\r'));
    });
    input.addEventListener('paste', function (e) {
      e.preventDefault();
      var text = '';
      try { text = (e.clipboardData || window.clipboardData).getData('text') || ''; } catch (err) { text = ''; }
      if (!text) return;
      send(text.replace(/\r\n/g, '\r').replace(/\n/g, '\r'));
    });
    // Paste also works when the input does not have the focus.
    document.addEventListener('paste', function (e) {
      if (!state.id || state.ended) return;
      var tag = (e.target && e.target.tagName) ? e.target.tagName.toLowerCase() : '';
      if (tag === 'input' || tag === 'textarea') return;
      e.preventDefault();
      var text = '';
      try { text = (e.clipboardData || window.clipboardData).getData('text') || ''; } catch (err) { text = ''; }
      if (!text) return;
      send(text.replace(/\r\n/g, '\r').replace(/\n/g, '\r'));
    });

    window.addEventListener('resize', function () {
      resize();
    });
  }

  wire();
  start();

  // The browser tests (and a curious operator) can look at the state.
  try {
    window.teaTunnelTerm = state;
    window.teaTunnelTermSelectedText = selectedText;
  } catch (e) { /* ignore */ }
})();
