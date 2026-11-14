/* TEA TERM -- the VT100 engine, shared by two pages.
 *
 * The browser terminal of the agent (web/app.js) and the terminal that the
 * tunnel carries (web/tunnel_terminal.js) draw their screen with the *same*
 * code: one emulator, so both pages look and behave the same.
 *
 * Classic script (no build step, no modules) -- it defines window.TeaTerm.
 *
 *   var t = new TeaTerm.TermScreen(80, 24);
 *   t.feed('\x1b[31mred\x1b[0m');
 *   TeaTerm.termLineHtml(t.lines[0], -1, -1);
 */
(function (global) {
  'use strict';

  var ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return ESC_MAP[c]; }); }

  /// How many lines the scrollback keeps. Past it the oldest line is dropped
  /// (and `dropped` counts how many, so the selection can still be placed).
  /// A thousand lines is a lot of history and still small enough to keep in
  /// the browser without thinking about it.
  var TERM_MAX_BACK_LINES = 1000;
    var TERM_BASE_COLORS = [
      '#1c1f24', '#f25a5a', '#4ed17e', '#f7ad31', '#7aaaff', '#bc3fbc', '#11a8cd', '#d7dce3',
      '#5b6472', '#ff7b72', '#7ee787', '#ffa657', '#79c0ff', '#d2a8ff', '#56d4dd', '#f0f6fc'
    ];

    function termColor(v) {
      if (v == null || v < 0) return null;
      if (typeof v === 'string') return v;                       // rgb(r,g,b)
      if (v < 16) return TERM_BASE_COLORS[v];
      if (v < 232) {
        var i = v - 16;
        var steps = [0, 95, 135, 175, 215, 255];
        return 'rgb(' + steps[Math.floor(i / 36)] + ',' + steps[Math.floor((i % 36) / 6)] + ',' +
          steps[i % 6] + ')';
      }
      var g = 8 + (v - 232) * 10;
      return 'rgb(' + g + ',' + g + ',' + g + ')';
    }
    function termDefaultStyle() {
      return {
        fg: -1, bg: -1, bold: false, dim: false,
        italic: false, underline: false, inverse: false
      };
    }
    function termCloneStyle(s) {
      return {
        fg: s.fg, bg: s.bg, bold: s.bold, dim: s.dim,
        italic: s.italic, underline: s.underline, inverse: s.inverse
      };
    }

    /* ---- 屏幕：一块二维的字符格子 ---- */
    function TermScreen(cols, rows) {
      this.reply = null;          // 终端问 cursor 在哪时，用它把回答写回去
      this.cols = 80;
      this.rows = 24;
      this.reset();
      this.resize(cols, rows);
    }

    TermScreen.prototype.reset = function () {
      this.lines = [];
      for (var i = 0; i < this.rows; i++) this.lines.push(this.blank());
      this.back = [];             // 滚上去的行（可以往回翻）
      this.dropped = 0;           // 回滚缓冲满了被丢掉的老行数（行号要补偿）
      this.cx = 0;
      this.cy = 0;
      this.top = 0;
      this.bot = this.rows - 1;
      this.style = termDefaultStyle();
      this.saved = null;
      this.wrapNext = false;      // 到了最右边，下一个字符换行
      this.state = 'normal';
      this.params = '';
      this.viewOffset = 0;        // 往回翻了多少行
      this.cursorVisible = true;  // 程序可以用 CSI ?25l 把它藏起来
      this.autowrap = true;       // DECAWM (?7)：到最右边要不要换行
      this.origin = false;        // DECOM (?6)：光标定位是不是相对滚动区
      this.alt = null;            // 备用屏幕开着的时候，主屏幕存在这儿
      this.altActive = false;
      this.dirty = true;
    };

    // 一行空白（cols 个格子）。
    function blankLine(cols) {
      var line = [];
      var st = termDefaultStyle();
      for (var i = 0; i < cols; i++) line.push({ c: ' ', s: st });
      return line;
    }

    // 把一行裁成新的宽度：多的丢掉，少的补空白（真终端不重排）。
    function fitLine(line, cols) {
      var st = termDefaultStyle();
      while (line.length < cols) line.push({ c: ' ', s: st });
      if (line.length > cols) line.length = cols;
      return line;
    }

    // 一块屏幕（若干行 + 回滚缓冲 + 光标）缩到新的尺寸。
    // 变矮：最下面几行挤进回滚缓冲，光标跟着内容一起往上挪。
    // 变高：下面补空白行，回滚缓冲里的历史不要拉回屏幕。
    function fitBlock(block, cols, rows) {
      var oldRows = block.lines.length;
      for (var i = 0; i < block.lines.length; i++) fitLine(block.lines[i], cols);
      for (var j = 0; j < block.back.length; j++) fitLine(block.back[j], cols);
      if (rows < oldRows) {
        var drop = oldRows - rows;
        var head = block.lines.splice(0, drop);   // 最上面几行挤进回滚缓冲
        block.back = head.concat(block.back);
        block.cy = Math.max(0, block.cy - drop);
      } else {
        while (block.lines.length < rows) block.lines.push(blankLine(cols));
      }
      block.cx = Math.min(block.cx, cols - 1);
      block.cy = Math.min(block.cy, rows - 1);
      return block;
    }

    TermScreen.prototype.blank = function () {
      return blankLine(this.cols);
    };

    TermScreen.prototype.resize = function (cols, rows) {
      cols = Math.max(20, Math.min(500, cols || this.cols));
      rows = Math.max(5, Math.min(200, rows || this.rows));
      if (cols === this.cols && rows === this.rows) return;
      this.cols = cols;
      this.rows = rows;
      fitBlock(this, cols, rows);
      // 备用屏幕开着的时候主屏幕藏在 alt 里，也得跟着改尺寸，
      // 不然 tmux 退出以后主屏幕的大小跟当前对不上。
      if (this.alt) fitBlock(this.alt, cols, rows);
      this.top = 0;
      this.bot = rows - 1;
      this.wrapNext = false;   // 宽度变了，挂着的那次换行不作数
      this.dirty = true;
    };

    /* ---- 解析：把一段文本变成终端的动作 ---- */
    TermScreen.prototype.feed = function (text) {
      for (var i = 0; i < text.length; i++) {
        var ch = text.charAt(i);
        if (this.state === 'esc') {
          if (ch === '[') { this.state = 'csi'; this.params = ''; continue; }
          if (ch === ']') { this.state = 'osc'; continue; }
          // 字符集选择（ESC ( B 之类）：跳掉后面那一个字符
          if (ch === '(' || ch === ')' || ch === '*' || ch === '+' || ch === '%' || ch === '#') {
            this.state = 'skip';
            continue;
          }
          if (ch === '7') { this.saveCursor(); this.state = 'normal'; continue; }
          if (ch === '8') { this.restoreCursor(); this.state = 'normal'; continue; }
          if (ch === 'D') { this.index(); this.state = 'normal'; continue; }
          if (ch === 'E') { this.nextLine(); this.state = 'normal'; continue; }
          if (ch === 'M') { this.reverseIndex(); this.state = 'normal'; continue; }
          if (ch === 'c') { this.reset(); this.state = 'normal'; continue; }
          this.state = 'normal';
          continue;
        }
        if (this.state === 'skip') { this.state = 'normal'; continue; }
        if (this.state === 'osc') {
          // OSC（窗口标题之类）：一直忽略到 BEL 或者 ST
          if (ch === '\x07') { this.state = 'normal'; continue; }
          if (ch === '\x1b') { this.state = 'oscEnd'; continue; }
          continue;
        }
        if (this.state === 'oscEnd') { this.state = 'normal'; continue; }
        if (this.state === 'csi') {
          if (ch >= '@' && ch <= '~') { this.csi(this.params, ch); this.state = 'normal'; continue; }
          this.params += ch;
          continue;
        }
        // 普通状态
        if (ch === '\x1b') { this.state = 'esc'; continue; }
        if (ch === '\r') { this.cx = 0; this.wrapNext = false; this.dirty = true; continue; }
        if (ch === '\n') { this.lineFeed(); continue; }
        if (ch === '\b') {
          if (this.cx > 0) this.cx--;
          this.wrapNext = false;
          this.dirty = true;
          continue;
        }
        if (ch === '\t') {
          this.cx = Math.min(this.cols - 1, (Math.floor(this.cx / 8) + 1) * 8);
          this.wrapNext = false;
          this.dirty = true;
          continue;
        }
        if (ch < ' ') continue;                       // 其它控制字符：忽略
        this.putChar(ch);
      }
    };

    TermScreen.prototype.putChar = function (ch) {
      if (this.wrapNext) {
        this.cx = 0;
        this.lineFeed();
        this.wrapNext = false;
      }
      var line = this.lines[this.cy];
      if (!line) return;
      line[this.cx] = { c: ch, s: this.style };
      if (this.cx < this.cols - 1) {
        this.cx++;
      } else if (this.autowrap) {
        // 到了最右边：光标先不动，挂着一次换行，等下一个字符再换。
        // 真终端就是这样，tmux 画满宽的状态条靠的也是它。
        this.cx = this.cols - 1;
        this.wrapNext = true;
      }
      this.dirty = true;
    };

    // 上下挪光标（LF / IND / RI）：挂着的那次换行就此作废。
    TermScreen.prototype.lineFeed = function () {
      if (this.cy === this.bot) this.scrollUp(1);
      else if (this.cy < this.rows - 1) this.cy++;
      this.wrapNext = false;
      this.dirty = true;
    };

    TermScreen.prototype.index = function () {
      if (this.cy === this.bot) this.scrollUp(1);
      else if (this.cy < this.rows - 1) this.cy++;
      this.wrapNext = false;
      this.dirty = true;
    };

    TermScreen.prototype.reverseIndex = function () {
      if (this.cy === this.top) this.scrollDown(1);
      else if (this.cy > 0) this.cy--;
      this.wrapNext = false;
      this.dirty = true;
    };

    TermScreen.prototype.nextLine = function () {
      this.cx = 0;
      this.index();
    };

    TermScreen.prototype.scrollUp = function (n) {
      for (var i = 0; i < n; i++) {
        if (this.top === 0) {
          this.back.push(this.lines[this.top]);
          if (this.back.length > TERM_MAX_BACK_LINES) { this.back.shift(); this.dropped++; }
        }
        this.lines.splice(this.top, 1);
        this.lines.splice(this.bot, 0, this.blank());
      }
      this.dirty = true;
    };

    TermScreen.prototype.scrollDown = function (n) {
      for (var i = 0; i < n; i++) {
        this.lines.splice(this.bot, 1);
        this.lines.splice(this.top, 0, this.blank());
      }
      this.dirty = true;
    };

    TermScreen.prototype.saveCursor = function () {
      this.saved = { cx: this.cx, cy: this.cy, style: this.style };
    };

    TermScreen.prototype.restoreCursor = function () {
      if (!this.saved) return;
      this.cx = Math.min(this.saved.cx, this.cols - 1);
      this.cy = Math.min(this.saved.cy, this.rows - 1);
      this.style = this.saved.style;
      this.wrapNext = false;
      this.dirty = true;
    };

    /* ---- 备用屏幕（DECSET 1049）----
     * tmux 启动时切到备用屏幕画自己的界面，退出时切回来。
     * 没有这块的话，tmux 的界面会把 shell 的屏幕冲掉，
     * 退出 tmux 以后连原来的提示符都找不回来。 */
    TermScreen.prototype.enterAltScreen = function () {
      if (this.altActive) return;
      this.alt = {
        lines: this.lines,
        back: this.back,
        dropped: this.dropped,
        cx: this.cx,
        cy: this.cy,
        top: this.top,
        bot: this.bot,
        style: this.style,
        saved: this.saved,
        wrapNext: this.wrapNext
      };
      this.lines = [];
      for (var i = 0; i < this.rows; i++) this.lines.push(this.blank());
      this.back = [];             // 备用屏幕的回滚缓冲跟主屏幕分开
      this.dropped = 0;
      this.cx = 0;
      this.cy = 0;
      this.top = 0;
      this.bot = this.rows - 1;
      this.style = termDefaultStyle();
      this.saved = null;
      this.wrapNext = false;
      this.altActive = true;
      this.dirty = true;
    };

    TermScreen.prototype.leaveAltScreen = function () {
      if (!this.altActive) return;
      if (this.alt) {
        this.lines = this.alt.lines;
        this.back = this.alt.back;
        this.dropped = this.alt.dropped;
        this.cx = this.alt.cx;
        this.cy = this.alt.cy;
        this.top = this.alt.top;
        this.bot = this.alt.bot;
        this.style = this.alt.style;
        this.saved = this.alt.saved;
        this.wrapNext = this.alt.wrapNext;
      }
      this.alt = null;
      this.altActive = false;
      this.dirty = true;
    };

    TermScreen.prototype.eraseInLine = function (mode) {
      var line = this.lines[this.cy];
      if (!line) return;
      var from = 0;
      var to = this.cols;
      if (mode === 0) from = this.cx;
      else if (mode === 1) to = this.cx + 1;
      for (var i = from; i < to && i < line.length; i++) line[i] = { c: ' ', s: this.style };
      this.dirty = true;
    };

    TermScreen.prototype.eraseInDisplay = function (mode) {
      if (mode === 2 || mode === 3) {
        for (var r = 0; r < this.rows; r++) this.lines[r] = this.blank();
      } else if (mode === 0) {
        this.eraseInLine(0);
        for (var r2 = this.cy + 1; r2 < this.rows; r2++) this.lines[r2] = this.blank();
      } else if (mode === 1) {
        this.eraseInLine(1);
        for (var r3 = 0; r3 < this.cy; r3++) this.lines[r3] = this.blank();
      }
      this.dirty = true;
    };

    TermScreen.prototype.insertLines = function (n) {
      for (var i = 0; i < n; i++) {
        if (this.cy > this.bot) break;
        this.lines.splice(this.bot, 1);
        this.lines.splice(this.cy, 0, this.blank());
      }
      this.dirty = true;
    };

    TermScreen.prototype.deleteLines = function (n) {
      for (var i = 0; i < n; i++) {
        if (this.cy > this.bot) break;
        this.lines.splice(this.cy, 1);
        this.lines.splice(this.bot, 0, this.blank());
      }
      this.dirty = true;
    };

    TermScreen.prototype.deleteChars = function (n) {
      var line = this.lines[this.cy];
      if (!line) return;
      line.splice(this.cx, n);
      var st = this.style;
      while (line.length < this.cols) line.push({ c: ' ', s: st });
      if (line.length > this.cols) line.length = this.cols;
      this.dirty = true;
    };

    TermScreen.prototype.insertChars = function (n) {
      var line = this.lines[this.cy];
      if (!line) return;
      var st = this.style;
      for (var i = 0; i < n; i++) line.splice(this.cx, 0, { c: ' ', s: st });
      if (line.length > this.cols) line.length = this.cols;
      this.dirty = true;
    };

    TermScreen.prototype.eraseChars = function (n) {
      var line = this.lines[this.cy];
      if (!line) return;
      var st = this.style;
      for (var i = 0; i < n && this.cx + i < line.length; i++) {
        line[this.cx + i] = { c: ' ', s: st };
      }
      this.dirty = true;
    };

    /* ---- CSI：带参数的转义序列 ---- */
    TermScreen.prototype.csi = function (params, final) {
      var priv = params.charAt(0) === '?' || params.charAt(0) === '>' || params.charAt(0) === '!';
      var body = priv ? params.slice(1) : params;
      var nums = [];
      var parts = body.split(';');
      for (var i = 0; i < parts.length; i++) {
        var v = parseInt(parts[i], 10);
        nums.push(isNaN(v) ? 0 : v);
      }
      var arg = function (idx, def) {
        if (nums.length > idx && nums[idx] > 0) return nums[idx];
        return def;
      };
      if (priv && (final === 'h' || final === 'l')) {
        var on = (final === 'h');
        // 光标的可见性（?25）
        if (body === '25') {
          this.cursorVisible = on;
          this.dirty = true;
          return;
        }
        // 自动换行（?7）：关掉以后写到最右边就停在那儿反复覆盖。
        if (body === '7') {
          this.autowrap = on;
          if (!on) this.wrapNext = false;
          this.dirty = true;
          return;
        }
        // 原点模式（?6）：光标定位变成相对滚动区。
        if (body === '6') {
          this.origin = on;
          this.dirty = true;
          return;
        }
        // 备用屏幕（?47 / ?1047 / ?1049）：tmux 就靠它画自己的界面，
        // 退出的时候把原来的屏幕原样还回来。
        if (body === '47' || body === '1047' || body === '1049') {
          if (on) this.enterAltScreen();
          else this.leaveAltScreen();
          return;
        }
        // 只存/取光标（?1048）
        if (body === '1048') {
          if (on) this.saveCursor();
          else this.restoreCursor();
          return;
        }
        return;
      }
      if (priv && final !== 'm') return;             // 私有模式：先不管
      // 光标一动，挂着的那次换行就作废（真终端的规矩）。
      // 不然写到最右边之后再定位一次，下一个字符会莫名其妙地换行：
      // tmux 画满宽的绿色状态条时，整屏会被顶歪，状态条也就散了。
      switch (final) {
        // 上下左右相对整屏挪，不是相对滚动区（滚动区只管滚不滚）。
        case 'A': this.cy = Math.max(0, this.cy - arg(0, 1)); this.wrapNext = false; break;
        case 'B': this.cy = Math.min(this.rows - 1, this.cy + arg(0, 1)); this.wrapNext = false; break;
        case 'C': this.cx = Math.min(this.cols - 1, this.cx + arg(0, 1)); this.wrapNext = false; break;
        case 'D': this.cx = Math.max(0, this.cx - arg(0, 1)); this.wrapNext = false; break;
        case 'E': this.cy = Math.min(this.rows - 1, this.cy + arg(0, 1)); this.cx = 0; this.wrapNext = false; break;
        case 'F': this.cy = Math.max(0, this.cy - arg(0, 1)); this.cx = 0; this.wrapNext = false; break;
        case 'G': this.cx = Math.max(0, Math.min(this.cols - 1, arg(0, 1) - 1)); this.wrapNext = false; break;
        case 'H':
        case 'f':
          this.cy = this.origin
            ? Math.max(this.top, Math.min(this.bot, this.top + arg(0, 1) - 1))
            : Math.max(0, Math.min(this.rows - 1, arg(0, 1) - 1));
          this.cx = Math.max(0, Math.min(this.cols - 1, arg(1, 1) - 1));
          this.wrapNext = false;
          break;
        case 'J': this.eraseInDisplay(nums.length ? nums[0] : 0); break;
        case 'K': this.eraseInLine(nums.length ? nums[0] : 0); break;
        case 'L': this.insertLines(arg(0, 1)); break;
        case 'M': this.deleteLines(arg(0, 1)); break;
        case 'P': this.deleteChars(arg(0, 1)); break;
        case '@': this.insertChars(arg(0, 1)); break;
        case 'S': this.scrollUp(arg(0, 1)); break;
        case 'T': this.scrollDown(arg(0, 1)); break;
        case 'X': this.eraseChars(arg(0, 1)); break;
        case 'd':
          this.cy = this.origin
            ? Math.max(this.top, Math.min(this.bot, this.top + arg(0, 1) - 1))
            : Math.max(0, Math.min(this.rows - 1, arg(0, 1) - 1));
          this.wrapNext = false;
          break;
        case 'm': this.sgr(nums); break;
        case 'r':
          this.top = Math.max(0, arg(0, 1) - 1);
          this.bot = Math.min(this.rows - 1, arg(1, this.rows) - 1);
          if (this.bot <= this.top) { this.top = 0; this.bot = this.rows - 1; }
          this.cx = 0;
          this.cy = this.top;
          this.wrapNext = false;
          break;
        case 's': this.saveCursor(); break;
        case 'u': this.restoreCursor(); break;
        case 'n':
          // 光标位置报告：有些程序（readline、PSReadLine）会问，得回答
          if (nums[0] === 6 && this.reply) {
            this.reply('\x1b[' + (this.cy + 1) + ';' + (this.cx + 1) + 'R');
          } else if (nums[0] === 5 && this.reply) {
            this.reply('\x1b[0n');
          }
          break;
        default:
          break;
      }
      this.dirty = true;
    };

    /* ---- SGR：颜色和字形 ---- */
    TermScreen.prototype.sgr = function (nums) {
      if (!nums.length) nums = [0];
      var s = termCloneStyle(this.style);
      for (var i = 0; i < nums.length; i++) {
        var n = nums[i];
        if (n === 0) s = termDefaultStyle();
        else if (n === 1) s.bold = true;
        else if (n === 2) s.dim = true;
        else if (n === 3) s.italic = true;
        else if (n === 4) s.underline = true;
        else if (n === 7) s.inverse = true;
        else if (n === 22) { s.bold = false; s.dim = false; }
        else if (n === 23) s.italic = false;
        else if (n === 24) s.underline = false;
        else if (n === 27) s.inverse = false;
        else if (n >= 30 && n <= 37) s.fg = n - 30;
        else if (n === 39) s.fg = -1;
        else if (n >= 40 && n <= 47) s.bg = n - 40;
        else if (n === 49) s.bg = -1;
        else if (n >= 90 && n <= 97) s.fg = n - 90 + 8;
        else if (n >= 100 && n <= 107) s.bg = n - 100 + 8;
        else if (n === 38 || n === 48) {
          var key = n === 38 ? 'fg' : 'bg';
          if (nums[i + 1] === 5) {
            s[key] = nums[i + 2] || 0;
            i += 2;
          } else if (nums[i + 1] === 2) {
            s[key] = 'rgb(' + (nums[i + 2] || 0) + ',' + (nums[i + 3] || 0) + ',' +
              (nums[i + 4] || 0) + ')';
            i += 4;
          }
        }
      }
      this.style = s;
    };

    /* ---- 画出来 ---- */
    function termSpan(style, text, sel) {
      var s = style || termDefaultStyle();
      var fg = s.fg;
      var bg = s.bg;
      if (s.inverse) {
        var t = fg;
        fg = bg;
        bg = t;
      }
      var css = '';
      var fgColor = termColor(fg);
      var bgColor = termColor(bg);
      if (fgColor) css += 'color:' + fgColor + ';';
      if (bgColor) css += 'background-color:' + bgColor + ';';
      if (s.bold) css += 'font-weight:700;';
      if (s.dim) css += 'opacity:.7;';
      if (s.italic) css += 'font-style:italic;';
      if (s.underline) css += 'text-decoration:underline;';
      // 选中的部分要有一层底色。行内样式里的 background-color 是终端自己写的，
      // 所以这里用 !important 盖住它（字色不动，还是终端原来的颜色）。
      var cls = sel ? ' class="term-sel"' : '';
      if (sel) css += 'background-color:rgba(96,156,255,.42) !important;';
      if (!css) return esc(text);
      return '<span' + cls + ' style="' + css + '">' + esc(text) + '</span>';
    }

    // 一行画成 HTML。selFrom/selTo 是这一行里被选中的格子范围（半开区间，
    // 没有选中就传 -1）：选中的部分单独包一层，选区边界正好落在样式段中间时
    // 把那段切开。
    function termLineHtml(line, selFrom, selTo) {
      var hasSel = selFrom != null && selTo != null && selTo > selFrom;
      var segs = [];
      var cur = null;
      var start = 0;
      for (var i = 0; i < line.length; i++) {
        var cell = line[i];
        var st = cell ? cell.s : cur;         // 没格子的地方算上一段的样子
        if (cur === null && i === 0) { cur = st; start = 0; continue; }
        if (st !== cur) { segs.push({ s: cur, from: start, to: i }); cur = st; start = i; }
      }
      if (cur !== null) segs.push({ s: cur, from: start, to: line.length });
      var out = '';
      for (var k = 0; k < segs.length; k++) {
        var seg = segs[k];
        var text = '';
        for (var j = seg.from; j < seg.to; j++) {
          var c = line[j];
          text += (c && c.c != null) ? c.c : ' ';
        }
        if (!hasSel) { out += termSpan(seg.s, text, false); continue; }
        var a = Math.max(seg.from, selFrom);
        var b = Math.min(seg.to, selTo);
        if (b <= a) { out += termSpan(seg.s, text, false); continue; }
        if (a > seg.from) out += termSpan(seg.s, text.slice(0, a - seg.from), false);
        out += termSpan(seg.s, text.slice(a - seg.from, b - seg.from), true);
        if (b < seg.to) out += termSpan(seg.s, text.slice(b - seg.from), false);
      }
      return out;
    }

    var SHELL_KEYS = {
      Enter: '\r', Backspace: '\x7f', Tab: '\t', Escape: '\x1b',
      ArrowUp: '\x1b[A', ArrowDown: '\x1b[B', ArrowRight: '\x1b[C', ArrowLeft: '\x1b[D',
      Home: '\x1b[H', End: '\x1b[F', PageUp: '\x1b[5~', PageDown: '\x1b[6~',
      Insert: '\x1b[2~', Delete: '\x1b[3~',
      F1: '\x1bOP', F2: '\x1bOQ', F3: '\x1bOR', F4: '\x1bOS',
      F5: '\x1b[15~', F6: '\x1b[17~', F7: '\x1b[18~', F8: '\x1b[19~',
      F9: '\x1b[20~', F10: '\x1b[21~', F11: '\x1b[23~', F12: '\x1b[24~'
    };

    function shellKeyBytes(e) {
      var key = e.key;
      if (e.ctrlKey && !e.altKey && !e.metaKey) {
        if (key && key.length === 1) {
          var up = key.toUpperCase();
          var code = up.charCodeAt(0);
          if (code >= 65 && code <= 90) return String.fromCharCode(code - 64);
          if (key === ' ') return '\x00';
          if (key >= '[' && key <= '_') return String.fromCharCode(key.charCodeAt(0) - 64);
        }
        if (SHELL_KEYS[key]) return SHELL_KEYS[key];
        return null;
      }
      if (e.altKey && !e.ctrlKey && !e.metaKey) {
        if (key && key.length === 1) return '\x1b' + key;
        return null;
      }
      if (e.metaKey) return null;                    // 留给浏览器自己的快捷键
      if (SHELL_KEYS[key]) return SHELL_KEYS[key];
      if (key && key.length === 1) return key;
      return null;
    }

  global.TeaTerm = {
    TermScreen: TermScreen,
    termColor: termColor,
    termDefaultStyle: termDefaultStyle,
    termCloneStyle: termCloneStyle,
    termSpan: termSpan,
    termLineHtml: termLineHtml,
    SHELL_KEYS: SHELL_KEYS,
    keyBytes: shellKeyBytes,
    esc: esc,
    MAX_BACK_LINES: TERM_MAX_BACK_LINES
  };
})(typeof window !== 'undefined' ? window : this);
