// The VT engine behind the web terminal (web/term.js) has to behave like a
// terminal, not like a text box.
//
// The bug this pins down: tmux paints its status line across the whole
// width of the screen. That leaves the cursor in the last column with a
// "pending wrap" (the terminal waits to see whether the next character
// wraps to the next line). A real terminal drops that pending wrap as soon
// as the cursor moves; the engine kept it, so the next write landed one
// line too low - and when that happened on the bottom row the whole screen
// scrolled and tmux's green status bar was pushed away.
//
// The same file pins the alternate screen (tmux draws on it and gives the
// shell's screen back when it exits), the scroll region, autowrap and what
// a resize does to the screen.
//
//   node tests/smoke_term_vt.mjs
//
// No browser, no server, no account: it loads web/term.js and feeds it
// escape sequences.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const src = fs.readFileSync(path.join(REPO_ROOT, 'web', 'term.js'), 'utf8');
const win = {};
new Function('window', src)(win);
const TermScreen = win.TeaTerm.TermScreen;

const results = [];
const check = (name, ok, detail) => {
  results.push({ name, ok });
  console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok || !detail ? '' : '  ' + detail));
};

/// The text of one row (trailing blanks cut off).
function rowText(term, row) {
  return term.lines[row].map((c) => (c && c.c != null ? c.c : ' ')).join('').replace(/\s+$/, '');
}

/// The style of one cell.
function cellStyle(term, row, col) {
  const cell = term.lines[row][col];
  return cell ? cell.s : null;
}

/// tmux: clear the screen, draw a pane, paint the status line green across
/// the whole width (that is what tmux does with status-style bg=green).
function tmuxFrame(term, pane, bar) {
  term.feed('\x1b[H\x1b[2J');
  term.feed('\x1b[1;1H\x1b[31m' + pane);
  term.feed('\x1b[24;1H\x1b[2K\x1b[42m' + (bar || 'S').repeat(term.cols) + '\x1b[0m');
}

// ---- 1. the status line must not push the next write off its row -----------
{
  const t = new TermScreen(80, 24);
  tmuxFrame(t, 'PANE-ONE');
  check('the status line fills the last column', t.wrapNext === true, 'wrapNext=' + t.wrapNext);
  t.feed('\x1b[1;1H\x1b[32mPANE-TWO');       // tmux redraws the pane
  check('a cursor move drops the pending wrap', t.cy === 0 && t.cx === 8,
    'cursor ' + t.cy + ',' + t.cx);
  check('the redrawn pane lands on its own row', rowText(t, 0) === 'PANE-TWO',
    JSON.stringify(rowText(t, 0)));
}

// ---- 2. a write on the bottom row must not scroll the screen away ---------
{
  const t = new TermScreen(80, 24);
  tmuxFrame(t, 'PANE-ONE');
  const bar = rowText(t, 23);
  t.feed('\x1b[24;1H\x1b[33mX');              // something else writes down there
  check('the screen did not scroll', t.back.length === 0, 'back=' + t.back.length);
  check('the status bar is still the last row', rowText(t, 23) === 'X' + bar.slice(1),
    JSON.stringify(rowText(t, 23)));
}

// ---- 3. the status bar keeps its green background -------------------------
{
  const t = new TermScreen(80, 24);
  tmuxFrame(t, 'PANE-ONE');
  const st = cellStyle(t, 23, 0);
  check('the status bar is green', !!st && st.bg === 2, JSON.stringify(st));
}

// ---- 4. the alternate screen gives the shell's screen back ----------------
{
  const t = new TermScreen(80, 24);
  t.feed('SHELL PROMPT\r\n');
  t.feed('\x1b[?1049h\x1b[H\x1b[2JTMUX\r\n');
  check('tmux draws on its own screen', rowText(t, 0) === 'TMUX', JSON.stringify(rowText(t, 0)));
  t.feed('\x1b[?1049l');
  check('the shell is back after tmux exits', rowText(t, 0) === 'SHELL PROMPT',
    JSON.stringify(rowText(t, 0)));
}

// ---- 5. the alternate screen keeps its own scrollback ---------------------
{
  const t = new TermScreen(80, 24);
  t.feed('SHELL LINE\r\n');
  t.feed('\x1b[?1049h');
  for (let i = 0; i < 40; i++) t.feed('TMUX LINE ' + i + '\r\n');
  check('the alternate screen scrolls on its own', t.back.length > 0, 'back=' + t.back.length);
  t.feed('\x1b[?1049l');
  check('the shell scrollback is untouched', t.back.length === 0, 'back=' + t.back.length);
  check('the shell screen is untouched', rowText(t, 0) === 'SHELL LINE',
    JSON.stringify(rowText(t, 0)));
}

// ---- 6. the cursor may leave the scroll region ---------------------------
{
  const t = new TermScreen(80, 24);
  t.feed('\x1b[5;10r');                       // the scrolling region is rows 5..10
  t.feed('\x1b[12;1H');
  t.feed('\x1b[3A');                          // three lines up
  check('cursor up is bounded by the screen, not the region', t.cy === 8, 'cy=' + t.cy);
  t.feed('\x1b[1A');
  check('the cursor can go above the region', t.cy === 7, 'cy=' + t.cy);
}

// ---- 7. autowrap off (CSI ?7l) does not wrap ------------------------------
// The engine keeps at least 20 columns, so the line below is 20 wide.
{
  const t = new TermScreen(20, 5);
  t.feed('\x1b[?7l');
  t.feed('ABCDEFGHIJKLMNOPQRSTU');
  check('no wrap: the cursor stays in the last column', t.cx === 19 && t.cy === 0,
    'cursor ' + t.cy + ',' + t.cx);
  check('no wrap: the text stays on one row', rowText(t, 0) === 'ABCDEFGHIJKLMNOPQRSU',
    JSON.stringify(rowText(t, 0)));
  t.feed('\x1b[?7h');
  t.feed('\r\n');
  t.feed('ABCDEFGHIJKLMNOPQRSTU');
  check('autowrap back on: the text wraps', t.cy === 2 && rowText(t, 1) === 'ABCDEFGHIJKLMNOPQRST',
    'cy=' + t.cy + ' row1=' + JSON.stringify(rowText(t, 1)));
}

// ---- 8. ordinary wrapping still works -------------------------------------
{
  const t = new TermScreen(20, 5);
  t.feed('ABCDEFGHIJKLMNOPQRSTU');
  check('a full line wraps to the next row', t.cy === 1 && rowText(t, 0) === 'ABCDEFGHIJKLMNOPQRST',
    'cy=' + t.cy + ' row0=' + JSON.stringify(rowText(t, 0)));
  check('the wrapped part is on the next row', rowText(t, 1) === 'U', JSON.stringify(rowText(t, 1)));
}

// ---- 9. a resize leaves the screen alone ----------------------------------
{
  const t = new TermScreen(80, 24);
  for (let i = 0; i < 30; i++) t.feed('line' + i + '\r\n');
  const back = t.back.length;
  check('the overflow went into the scrollback', back > 0, 'back=' + back);
  t.resize(80, 40);                          // the window got taller
  check('growing keeps the history in the scrollback', t.back.length === back,
    'back=' + t.back.length);
  check('growing adds blank rows at the bottom', t.lines.length === 40, 'lines=' + t.lines.length);
  check('the content did not move', rowText(t, 22) === 'line29', JSON.stringify(rowText(t, 22)));
}

// ---- 10. a resize keeps the cursor on its content -------------------------
{
  const t = new TermScreen(80, 24);
  for (let i = 0; i < 20; i++) t.feed('row' + i + '\r\n');
  check('the cursor is below the last line', t.cy === 20, 'cy=' + t.cy);
  t.resize(80, 10);                          // the window got shorter
  check('the cursor follows the content', t.cy === 6, 'cy=' + t.cy);
  check('the content under the cursor is still there', rowText(t, 5) === 'row19',
    JSON.stringify(rowText(t, 5)));
}

// ---- 11. a resize drops the pending wrap ----------------------------------
{
  const t = new TermScreen(20, 5);
  t.feed('ABCDEFGHIJKLMNOPQRST');            // exactly one line: the wrap is pending
  check('the line is full and the wrap is pending', t.wrapNext === true, 'wrapNext=' + t.wrapNext);
  t.resize(30, 5);
  check('a resize drops the pending wrap', t.wrapNext === false, 'wrapNext=' + t.wrapNext);
  t.feed('X');
  check('the next character stays on the line', t.cy === 0 && t.cx === 20,
    'cursor ' + t.cy + ',' + t.cx);
}

// ---- 12. the alternate screen is resized too ------------------------------
{
  const t = new TermScreen(80, 24);
  t.feed('SHELL\r\n');
  t.feed('\x1b[?1049h');
  t.resize(60, 12);
  check('the alternate screen got the new size', t.cols === 60 && t.rows === 12,
    t.cols + 'x' + t.rows);
  t.feed('\x1b[?1049l');
  check('the shell screen got the new size as well', t.cols === 60 && t.rows === 12 &&
    t.lines.length === 12, t.cols + 'x' + t.rows + ' lines=' + t.lines.length);
  // 屏幕变矮，最上面那几行进了回滚缓冲（没丢）。
  const all = t.back.concat(t.lines).map((line) =>
    line.map((c) => (c && c.c != null ? c.c : ' ')).join('').replace(/\s+$/, ''));
  check('the shell text is not lost', all.indexOf('SHELL') >= 0,
    JSON.stringify(all.slice(0, 3)));
}

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\n${failed.length} FAILED` : '\nALL PASS');
process.exit(failed.length ? 1 : 0);
