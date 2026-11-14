// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
#include "html_export.h"

#include <algorithm>
#include <cstdint>
#include <map>
#include <set>
#include <sstream>
#include <vector>

#include "util.h"

namespace tea {
namespace {

// ---------------------------------------------------------------------------
// escaping
// ---------------------------------------------------------------------------
std::string esc(const std::string& s) {
  // A session written by an older version (or by a tool result that was never
  // cleaned) can carry bytes that are not valid UTF-8. The export is a
  // document; it has to be readable, so those bytes become U+FFFD here.
  const std::string in = sanitize_utf8(s);
  std::string out;
  out.reserve(in.size() + 16);
  for (std::size_t i = 0; i < in.size(); ++i) {
    switch (in[i]) {
      case '&': out += "&amp;"; break;
      case '<': out += "&lt;"; break;
      case '>': out += "&gt;"; break;
      case '"': out += "&quot;"; break;
      case '\'': out += "&#39;"; break;
      default: out += in[i];
    }
  }
  return out;
}

/// Only http(s) and mailto links survive. The export is a document somebody may
/// mail around; a `javascript:` link in a model answer must not become a live
/// one just because it was rendered as HTML.
bool safe_url(const std::string& url) {
  const std::string u = to_lower(trim(url));
  if (u.empty()) return false;
  if (starts_with(u, "https://") || starts_with(u, "http://") || starts_with(u, "mailto:")) {
    return true;
  }
  // Fragment/relative links are useless in a single file; anchors still work.
  return starts_with(u, "#");
}

// ---------------------------------------------------------------------------
// inline markdown
//
// Everything is escaped *first* (like the UI does), then the handful of inline
// constructs are matched on the escaped text. That ordering is what makes it
// impossible for a model answer to inject markup: the only HTML that survives
// is what this file generates.
// ---------------------------------------------------------------------------
std::string inline_md(const std::string& escaped);

std::string link_or_text(const std::string& label, const std::string& href) {
  if (!safe_url(href)) return label;
  return "<a href=\"" + href + "\" rel=\"noreferrer noopener\">" + label + "</a>";
}

/// Rewrite [text](url) and ![alt](url) in an already-escaped string.
std::string rewrite_links(const std::string& s) {
  std::string out;
  std::size_t i = 0;
  while (i < s.size()) {
    const std::size_t open = s.find('[', i);
    if (open == std::string::npos) {
      out.append(s, i, std::string::npos);
      break;
    }
    out.append(s, i, open - i);
    const bool image = open > 0 && s[open - 1] == '!';
    const std::size_t close = s.find(']', open + 1);
    if (close == std::string::npos || close + 1 >= s.size() || s[close + 1] != '(') {
      out += s[open];
      i = open + 1;
      continue;
    }
    // Find the matching ')' - URLs such as `javascript:alert(1)` contain one,
    // and stopping at the first would leave a stray ')' in the output.
    std::size_t paren = close + 2;
    {
      int depth = 0;
      bool closed = false;
      for (; paren < s.size(); ++paren) {
        if (s[paren] == '(') ++depth;
        else if (s[paren] == ')') {
          if (depth == 0) { closed = true; break; }
          --depth;
        }
      }
      if (!closed) {
        out += s[open];
        i = open + 1;
        continue;
      }
    }
    const std::string label = s.substr(open + 1, close - open - 1);
    const std::string href = trim(s.substr(close + 2, paren - close - 2));
    if (image) {
      // A remote <img> would make the file depend on the network, which is
      // exactly what an export must not do: keep the reference as a link.
      out += "<span class=\"md-img\">[图片: " +
             (label.empty() ? href : label) + "]</span>";
      if (safe_url(href)) {
        out += " <a href=\"" + href + "\" rel=\"noreferrer noopener\">链接</a>";
      }
    } else {
      out += link_or_text(label, href);
    }
    i = paren + 1;
    if (image && open > 0) {
      // Remove the '!' that was already emitted.
      const std::size_t bang = out.rfind('!');
      if (bang != std::string::npos) out.erase(bang, 1);
    }
  }
  return out;
}

/// Bold / italic / strikethrough. Deliberately conservative: a single `*` only
/// starts emphasis at a word boundary, which keeps `2*3*4` and paths such as
/// `src/*.cpp` from turning into italics.
std::string inline_md(const std::string& t) {
  // The inline code spans are lifted out first and put back at the end.
  //
  // Why not just render them where they are: markup AROUND a code span
  // (`**bold `code` bold**`) is one pair of markers, and a pass that worked on
  // the text BETWEEN the code spans would never find the second one - the
  // `**` would come out as two stray asterisks. The content of a code span
  // stays literal all the same: it is not in the text while the markup passes
  // run.
  const char kMarkStart = '\x01';
  const char kMarkEnd = '\x02';
  std::vector<std::string> code_spans;
  std::string text;
  {
    std::size_t i = 0;
    while (i < t.size()) {
      const std::size_t tick = t.find('`', i);
      if (tick == std::string::npos) {
        text += t.substr(i);
        break;
      }
      const std::size_t end = t.find('`', tick + 1);
      if (end == std::string::npos) {
        text += t.substr(i);
        break;
      }
      text += t.substr(i, tick - i);
      text += kMarkStart;
      text += std::to_string(code_spans.size());
      text += kMarkEnd;
      code_spans.push_back(t.substr(tick + 1, end - tick - 1));
      i = end + 1;
    }
  }

  text = rewrite_links(text);

  // **bold** and ~~strike~~ are unambiguous.
  std::string with_bold;
  std::size_t p = 0;
  while (p < text.size()) {
    if (text.compare(p, 2, "**") == 0) {
      const std::size_t close = text.find("**", p + 2);
      if (close != std::string::npos) {
        with_bold += "<strong>" + text.substr(p + 2, close - p - 2) + "</strong>";
        p = close + 2;
        continue;
      }
    }
    if (text.compare(p, 2, "~~") == 0) {
      const std::size_t close = text.find("~~", p + 2);
      if (close != std::string::npos) {
        with_bold += "<del>" + text.substr(p + 2, close - p - 2) + "</del>";
        p = close + 2;
        continue;
      }
    }
    with_bold += text[p];
    ++p;
  }

  // *italic* / _italic_ at word boundaries. A code span counts as one: the
  // text after it may start an emphasis, as it did before.
  std::string with_em;
  for (std::size_t k = 0; k < with_bold.size(); ++k) {
    const char c = with_bold[k];
    const bool boundary = k == 0 || with_bold[k - 1] == ' ' || with_bold[k - 1] == '(' ||
                          with_bold[k - 1] == '\n' || with_bold[k - 1] == kMarkEnd;
    if ((c == '*' || c == '_') && boundary && k + 1 < with_bold.size() &&
        with_bold[k + 1] != c && with_bold[k + 1] != ' ') {
      const std::size_t close = with_bold.find(std::string(1, c), k + 1);
      if (close != std::string::npos && close > k + 1 && with_bold[close - 1] != ' ') {
        with_em += "<em>" + with_bold.substr(k + 1, close - k - 1) + "</em>";
        k = close;
        continue;
      }
    }
    with_em += c;
  }

  // The code spans go back where they were.
  std::string out;
  std::size_t i = 0;
  while (i < with_em.size()) {
    const std::size_t start = with_em.find(kMarkStart, i);
    if (start == std::string::npos) {
      out += with_em.substr(i);
      break;
    }
    out += with_em.substr(i, start - i);
    const std::size_t end = with_em.find(kMarkEnd, start + 1);
    if (end == std::string::npos) {
      out += with_em.substr(start);
      break;
    }
    const std::string index = with_em.substr(start + 1, end - start - 1);
    const std::size_t n = static_cast<std::size_t>(std::atoi(index.c_str()));
    if (n < code_spans.size()) {
      out += "<code class=\"md-inline\">" + code_spans[n] + "</code>";
    }
    i = end + 1;
  }
  return out;
}

bool starts_with_any(const std::string& s, const char* const* prefixes, std::size_t n) {
  for (std::size_t i = 0; i < n; ++i) {
    if (starts_with(s, prefixes[i])) return true;
  }
  return false;
}

bool is_ul(const std::string& s) {
  static const char* const k[] = {"- ", "* ", "+ "};
  std::size_t i = 0;
  while (i < s.size() && (s[i] == ' ' || s[i] == '\t')) ++i;
  return starts_with_any(s.substr(i), k, sizeof(k) / sizeof(k[0]));
}

bool is_ol(const std::string& s) {
  std::size_t i = 0;
  while (i < s.size() && (s[i] == ' ' || s[i] == '\t')) ++i;
  std::size_t digits = 0;
  while (i + digits < s.size() && s[i + digits] >= '0' && s[i + digits] <= '9') ++digits;
  if (digits == 0) return false;
  const std::size_t after = i + digits;
  return after + 1 < s.size() && (s[after] == '.' || s[after] == ')') && s[after + 1] == ' ';
}

/// Already-escaped text, so a quote marker is `&gt;`.
bool is_quote(const std::string& s) {
  std::size_t i = 0;
  while (i < s.size() && (s[i] == ' ' || s[i] == '\t')) ++i;
  return starts_with(s.substr(i), "&gt;");
}

bool is_hr(const std::string& s) {
  const std::string t = trim(s);
  if (t.size() < 3) return false;
  const char c = t[0];
  if (c != '-' && c != '*' && c != '_') return false;
  for (std::size_t i = 0; i < t.size(); ++i) {
    if (t[i] != c) return false;
  }
  return true;
}

std::vector<std::string> split_cells(const std::string& line) {
  std::string t = trim(line);
  if (!t.empty() && t.front() == '|') t.erase(0, 1);
  if (!t.empty() && t.back() == '|') t.pop_back();
  std::vector<std::string> cells;
  std::string cur;
  for (std::size_t i = 0; i < t.size(); ++i) {
    if (t[i] == '|') {
      cells.push_back(trim(cur));
      cur.clear();
    } else {
      cur += t[i];
    }
  }
  cells.push_back(trim(cur));
  return cells;
}

/// `|---|:--:|` — the line that turns the row above it into a table header.
bool is_table_sep(const std::string& line) {
  const std::string t = trim(line);
  if (t.size() < 3 || t.find('-') == std::string::npos || t.find('|') == std::string::npos) {
    return false;
  }
  for (std::size_t i = 0; i < t.size(); ++i) {
    const char c = t[i];
    if (c != '|' && c != '-' && c != ':' && c != ' ') return false;
  }
  return true;
}

std::string align_of(const std::string& cell) {
  const std::string t = trim(cell);
  const bool left = !t.empty() && t.front() == ':';
  const bool right = !t.empty() && t.back() == ':';
  if (left && right) return "center";
  if (right) return "right";
  if (left) return "left";
  return std::string();
}

std::string md_code_block(const std::string& lang, const std::vector<std::string>& lines) {
  std::string body;
  for (std::size_t i = 0; i < lines.size(); ++i) {
    if (i) body += "\n";
    body += lines[i];
  }
  return "<div class=\"code-block\"><div class=\"code-banner\">" +
         esc(lang.empty() ? "code" : to_lower(lang)) + "</div><pre><code>" + body +
         "</code></pre></div>";
}

}  // namespace

std::string markdown_to_html(const std::string& source) {
  const std::string escaped = esc(source);
  std::vector<std::string> lines;
  {
    std::string cur;
    for (std::size_t i = 0; i < escaped.size(); ++i) {
      if (escaped[i] == '\n') {
        if (!cur.empty() && cur.back() == '\r') cur.pop_back();
        lines.push_back(cur);
        cur.clear();
      } else {
        cur += escaped[i];
      }
    }
    if (!cur.empty() && cur.back() == '\r') cur.pop_back();
    lines.push_back(cur);
  }

  std::string out;
  std::size_t i = 0;
  while (i < lines.size()) {
    const std::string& line = lines[i];

    // Fenced code block.
    {
      const std::string t = trim(line);
      if (t.size() >= 3 && (starts_with(t, "```") || starts_with(t, "~~~"))) {
        const char marker = t[0];
        std::string lang = trim(t.substr(3));
        std::vector<std::string> body;
        ++i;
        while (i < lines.size()) {
          const std::string f = trim(lines[i]);
          if (f.size() >= 3 && f[0] == marker && f.find_first_not_of(marker) == std::string::npos) {
            ++i;
            break;
          }
          body.push_back(lines[i]);
          ++i;
        }
        out += md_code_block(lang, body);
        continue;
      }
    }

    if (trim(line).empty()) {
      ++i;
      continue;
    }

    // Heading.
    {
      std::size_t hashes = 0;
      while (hashes < line.size() && line[hashes] == '#') ++hashes;
      if (hashes >= 1 && hashes <= 6 && hashes < line.size() && line[hashes] == ' ') {
        const std::string level = std::to_string(hashes);
        out += "<h" + level + ">" + inline_md(trim(line.substr(hashes + 1))) + "</h" + level + ">";
        ++i;
        continue;
      }
    }

    if (is_hr(line)) {
      out += "<hr>";
      ++i;
      continue;
    }

    if (is_quote(line)) {
      std::string joined;
      while (i < lines.size() && is_quote(lines[i])) {
        std::string body = trim(lines[i]);
        if (starts_with(body, "&gt;")) body = trim(body.substr(4));
        if (!joined.empty()) joined += "\n";
        joined += body;
        ++i;
      }
      out += "<blockquote>" + inline_md(joined) + "</blockquote>";
      continue;
    }

    if (is_ul(line)) {
      out += "<ul>";
      while (i < lines.size() && is_ul(lines[i])) {
        std::string body = trim(lines[i]);
        body = trim(body.substr(1));
        out += "<li>" + inline_md(body) + "</li>";
        ++i;
      }
      out += "</ul>";
      continue;
    }

    if (is_ol(line)) {
      const std::string t = trim(line);
      std::size_t digits = 0;
      while (digits < t.size() && t[digits] >= '0' && t[digits] <= '9') ++digits;
      const std::string first = t.substr(0, digits);
      out += "<ol" + (first != "1" ? " start=\"" + first + "\"" : std::string()) + ">";
      while (i < lines.size() && is_ol(lines[i])) {
        std::string body = trim(lines[i]);
        const std::size_t dot = body.find_first_of(".)");
        body = trim(body.substr(dot + 1));
        out += "<li>" + inline_md(body) + "</li>";
        ++i;
      }
      out += "</ol>";
      continue;
    }

    // Table: a row containing '|' followed by a separator row.
    if (i + 1 < lines.size() && line.find('|') != std::string::npos &&
        is_table_sep(lines[i + 1])) {
      const std::vector<std::string> head = split_cells(line);
      const std::vector<std::string> aligns = split_cells(lines[i + 1]);
      i += 2;
      out += "<div class=\"md-table-wrap\"><table class=\"md-table\"><thead><tr>";
      for (std::size_t c = 0; c < head.size(); ++c) {
        const std::string al = c < aligns.size() ? align_of(aligns[c]) : std::string();
        out += "<th" + (al.empty() ? std::string() : " style=\"text-align:" + al + "\"") + ">" +
               inline_md(head[c]) + "</th>";
      }
      out += "</tr></thead><tbody>";
      while (i < lines.size() && !trim(lines[i]).empty() &&
             lines[i].find('|') != std::string::npos) {
        const std::vector<std::string> row = split_cells(lines[i]);
        out += "<tr>";
        for (std::size_t c = 0; c < head.size(); ++c) {
          const std::string al = c < aligns.size() ? align_of(aligns[c]) : std::string();
          out += "<td" + (al.empty() ? std::string() : " style=\"text-align:" + al + "\"") + ">" +
                 inline_md(c < row.size() ? row[c] : std::string()) + "</td>";
        }
        out += "</tr>";
        ++i;
      }
      out += "</tbody></table></div>";
      continue;
    }

    // Paragraph: consecutive plain lines, line breaks preserved.
    std::string para;
    while (i < lines.size()) {
      const std::string& l = lines[i];
      if (trim(l).empty() || is_quote(l) || is_ul(l) || is_ol(l) || is_hr(l)) break;
      if (is_table_sep(l)) break;
      if (starts_with(trim(l), "```") || starts_with(trim(l), "~~~")) break;
      {
        std::size_t hashes = 0;
        while (hashes < l.size() && l[hashes] == '#') ++hashes;
        if (hashes >= 1 && hashes <= 6 && hashes < l.size() && l[hashes] == ' ') break;
      }
      if (!para.empty()) para += "\n";
      para += l;
      ++i;
    }
    if (para.empty()) {  // nothing matched: emit the line so text is never lost
      para = lines[i];
      ++i;
    }
    std::string with_breaks;
    for (std::size_t k = 0; k < para.size(); ++k) {
      if (para[k] == '\n') with_breaks += "<br>";
      else with_breaks += para[k];
    }
    out += "<p>" + inline_md(with_breaks) + "</p>";
  }
  return out;
}

namespace {

/// Inline one image as a data: URL, or explain why it is only a caption.
/// `json_budget` is the *extra* copy kept for the JSON block (used by import);
/// when it runs out the picture is still in the HTML, just not restorable.
std::string image_block(const Workspace& ws, const ImageAttachment& att,
                        std::size_t* budget, const ExportLimits& limits,
                        std::size_t* json_budget, std::map<std::string, std::string>* image_data,
                        bool* images_omitted) {
  const std::string caption = esc(att.path.empty() ? std::string("(image)") : att.path);
  const std::string absolute = ws.resolve_path(att.path, nullptr);
  if (absolute.empty() || !path_is_file(absolute)) {
    return "<figure class=\"img missing\"><figcaption>图片已不在工作区：" + caption +
           "</figcaption></figure>";
  }
  const uint64_t size = file_size(absolute);
  if (size > limits.image_bytes) {
    return "<figure class=\"img missing\"><figcaption>" + caption + "（" + human_size(size) +
           "，超过单张 " + human_size(limits.image_bytes) + " 的导出上限，未内联）</figcaption></figure>";
  }
  if (*budget < size) {
    return "<figure class=\"img missing\"><figcaption>" + caption +
           "（导出文件的图片总量已达上限，未内联）</figcaption></figure>";
  }
  std::string bytes;
  if (!read_file_binary(absolute, &bytes)) {
    return "<figure class=\"img missing\"><figcaption>图片无法读取：" + caption +
           "</figcaption></figure>";
  }
  const char* mime = sniff_image_mime(bytes);
  if (!mime) {
    return "<figure class=\"img missing\"><figcaption>不是可显示的图片：" + caption +
           "</figcaption></figure>";
  }
  *budget -= bytes.size();
  const std::string data_url =
      std::string("data:") + mime + ";base64," + base64_encode(bytes);
  if (image_data) {
    if (data_url.size() <= *json_budget) {
      *json_budget -= data_url.size();
      (*image_data)[att.path] = data_url;
    } else if (images_omitted) {
      *images_omitted = true;
    }
  }
  return "<figure class=\"img\"><img alt=\"" + caption + "\" src=\"" + data_url +
         "\"><figcaption>" + caption + " · " + human_size(bytes.size()) +
         "</figcaption></figure>";
}

/// The stylesheet of a Markdown page. The colours are variables on :root and
/// the dark mode only overrides :root - a media query that re-declared
/// individual rules would lose to any rule written after it (same specificity,
/// later wins), which is how a page ends up dark with white cards.
const char* const kDocStyle = R"CSS(
:root{
  color-scheme:light dark;
  --bg:#f6f7f9; --fg:#1f2328; --muted:#6b7480;
  --card:#fff; --card-border:#e3e6ea; --hairline:#e3e6ea;
  --code-bg:#f6f8fa; --code-banner:#f6f8fa; --inline-bg:#f1f3f5;
  --table-head:#f6f8fa; --link:#1a63d8; --quote:#57606a;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#16181c; --fg:#e6e8eb; --muted:#9aa4b2;
    --card:#1d2025; --card-border:#2b2f36; --hairline:#2b2f36;
    --code-bg:#14161a; --code-banner:#191c21; --inline-bg:#2a2f37;
    --table-head:#191c21; --link:#7ab7ff; --quote:#a7b0bd;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC","PingFang SC",
  "Microsoft YaHei",sans-serif}
main{max-width:1080px;margin:0 auto;padding:36px 24px 96px}
h1{font-size:26px;line-height:1.35;margin:0 0 20px}
h2{font-size:20px;margin:38px 0 12px;padding-bottom:7px;border-bottom:1px solid var(--hairline)}
h3{font-size:17px;margin:28px 0 8px}
h4{font-size:15px;margin:22px 0 6px;color:var(--muted)}
p{margin:10px 0}
ul,ol{margin:10px 0;padding-left:24px}
li{margin:3px 0}
a{color:var(--link);text-decoration:none}
a:hover{text-decoration:underline}
hr{border:0;border-top:1px solid var(--hairline);margin:26px 0}
blockquote{margin:12px 0;padding:2px 0 2px 14px;border-left:3px solid var(--card-border);
  color:var(--quote)}
/* The tables fit the page and wrap: a reference that has to be scrolled
   sideways to read a column is a reference nobody reads. Long unbroken
   strings (a JSON body, a URL) break instead of pushing the page wide. */
table{border-collapse:collapse;margin:14px 0;width:100%;font-size:14px}
th,td{border:1px solid var(--card-border);padding:7px 10px;text-align:left;
  vertical-align:top;overflow-wrap:anywhere}
th{background:var(--table-head);font-weight:600}
code{background:var(--inline-bg);padding:1px 5px;border-radius:4px;font-size:.92em;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
pre{background:var(--code-bg);border:1px solid var(--card-border);border-radius:8px;
  padding:12px 14px;overflow-x:auto}
pre code{background:none;padding:0}
.code-block{margin:14px 0}
.code-banner{background:var(--code-banner);border:1px solid var(--card-border);
  border-bottom:0;border-radius:8px 8px 0 0;padding:4px 12px;font-size:12px;color:var(--muted)}
.code-banner + pre{border-radius:0 0 8px 8px;margin-top:0}
strong{font-weight:650}
)CSS";

const char* const kStyle = R"CSS(
/* Everything is driven by these variables, so dark mode only has to override
   :root - a media query that instead re-declared individual rules would lose to
   any rule written after it (same specificity, later wins), which is exactly how
   this file used to end up with a dark page and white cards. */
:root{
  color-scheme:light dark;
  --bg:#f6f7f9; --fg:#1f2328; --muted:#6b7480;
  --card:#fff; --card-border:#e3e6ea; --hairline:#e3e6ea;
  --code-bg:#f6f8fa; --code-banner:#f6f8fa; --inline-bg:#f1f3f5;
  --table-head:#f6f8fa; --link:#1a63d8; --quote:#57606a;
  --user:#4a7dff; --assistant:#2ea043; --tool:#8b949e; --system:#d29922;
  --ok:#2ea043; --fail:#cf222e; --shadow:none;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#16181c; --fg:#e6e8eb; --muted:#9aa4b2;
    --card:#1d2025; --card-border:#2b2f36; --hairline:#2b2f36;
    --code-bg:#14161a; --code-banner:#191c21; --inline-bg:#2a2f37;
    --table-head:#191c21; --link:#7ab7ff; --quote:#a7b0bd;
    --user:#6f9bff; --assistant:#4ac26b; --tool:#8b949e; --system:#e3b341;
    --ok:#4ac26b; --fail:#ff7b72; --shadow:0 1px 2px #0006;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC","PingFang SC",
  "Microsoft YaHei",sans-serif}
main{max-width:900px;margin:0 auto;padding:32px 20px 80px}
header.top{margin-bottom:26px;border-bottom:1px solid var(--hairline);padding-bottom:18px}
h1{font-size:22px;line-height:1.35;margin:0 0 12px}
.meta{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;margin:0;font-size:13px}
.meta dt{color:var(--muted)}
.meta dd{margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  word-break:break-all}
.lede{font-size:13px;color:var(--muted);margin:14px 0 0}
.artifacts{margin:18px 0 0;padding:12px 14px;border:1px solid var(--card-border);
  border-radius:12px;background:var(--card);box-shadow:var(--shadow)}
.artifacts-head{display:flex;align-items:baseline;gap:10px}
.artifacts-head h2{margin:0;font-size:14px}
.artifacts-count{margin-left:auto;font:11px/1.5 ui-monospace,monospace;color:var(--muted)}
.artifacts-list{list-style:none;margin:8px 0 0;padding:0;display:flex;flex-direction:column;gap:2px}
.artifact{display:flex;gap:10px;align-items:baseline;font-size:13px}
.artifact .kind{flex:0 0 auto;font:10px/16px ui-monospace,monospace;padding:0 6px;
  border-radius:8px;border:1px solid var(--card-border);color:var(--muted)}
.artifact .name{flex:1 1 auto;font:12px/1.6 ui-monospace,monospace;color:var(--fg);
  word-break:break-all}
.artifact .size{flex:0 0 auto;font:11px/1.6 ui-monospace,monospace;color:var(--muted)}
.artifacts-note{margin:8px 0 0;font-size:12px;color:var(--muted)}
.todos{margin:18px 0 0;padding:12px 14px;border:1px solid var(--card-border);
  border-radius:12px;background:var(--card);box-shadow:var(--shadow)}
.todos-head{display:flex;align-items:baseline;gap:10px}
.todos-head h2{margin:0;font-size:14px}
.todos-count{margin-left:auto;font:11px/1.5 ui-monospace,monospace;color:var(--muted)}
.todos-bar{height:4px;border-radius:2px;background:var(--inline-bg);overflow:hidden;margin:8px 0 10px}
.todos-bar i{display:block;height:100%;background:var(--assistant);border-radius:2px}
.todos-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:3px}
.todo{display:flex;gap:8px;align-items:baseline;font-size:13px;color:var(--fg)}
.todo .mark{flex:0 0 auto;width:13px;text-align:center;font-size:11px;color:var(--muted)}
.todo.done{color:var(--muted)}
.todo.done .txt{text-decoration:line-through}
.todo.done .mark{color:var(--ok)}
.todo.active{font-weight:600}
.todo.active .mark{color:var(--link)}
.turns{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:14px}
.msg{background:var(--card);color:var(--fg);border:1px solid var(--card-border);
  border-radius:12px;padding:14px 16px;box-shadow:var(--shadow)}
.msg .who{font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);
  margin-bottom:8px;display:flex;gap:8px;align-items:baseline}
.msg .who .t{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  text-transform:none;letter-spacing:0}
.msg.user{border-left:4px solid var(--user)}
.msg.assistant{border-left:4px solid var(--assistant)}
.msg.tool{border-left:4px solid var(--tool);background:transparent;border-style:dashed}
.msg.system{border-left:4px solid var(--system)}
.msg .body>:first-child{margin-top:0}
.msg .body>:last-child{margin-bottom:0}
p{margin:0 0 10px}
h1,h2,h3,h4,h5,h6{margin:18px 0 8px;line-height:1.35}
ul,ol{margin:0 0 10px;padding-left:24px}
li{margin:2px 0}
a{color:var(--link);text-decoration:none}
a:hover{text-decoration:underline}
hr{border:0;border-top:1px solid var(--hairline);margin:16px 0}
blockquote{margin:0 0 10px;padding:2px 0 2px 12px;border-left:3px solid var(--card-border);
  color:var(--quote)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
.md-inline{background:var(--inline-bg);border-radius:4px;padding:1px 5px}
.code-block{border:1px solid var(--card-border);border-radius:8px;overflow:hidden;
  margin:0 0 12px}
.code-banner{font:11px/1 ui-monospace,monospace;color:var(--muted);padding:7px 10px;
  border-bottom:1px solid var(--card-border);background:var(--code-banner)}
.code-block pre{margin:0;padding:12px;overflow:auto;background:var(--code-bg);border:0}
pre{background:var(--code-bg);border:1px solid var(--card-border);border-radius:8px;
  padding:12px;overflow:auto;
  font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;
  word-break:break-word}
.md-table-wrap{overflow:auto;margin:0 0 12px}
.md-table{border-collapse:collapse;font-size:14px}
.md-table th,.md-table td{border:1px solid var(--card-border);padding:6px 10px}
.md-table th{background:var(--table-head);font-weight:600}
details{margin:0 0 10px;border:1px solid var(--card-border);border-radius:8px;padding:8px 12px}
details>summary{cursor:pointer;font-size:13px;color:var(--quote);font-weight:600}
details[open]>summary{margin-bottom:8px}
details.reasoning{border-style:dashed}
details.call,details.tool{font-size:13px}
details.tool.fail>summary{color:var(--fail)}
/* Collapsed blocks keep a right-aligned one-line preview (tool output, call
   arguments). It is elided rather than wrapped: a summary that grows to three
   lines defeats the point of collapsing. */
details>summary{display:flex;gap:8px;align-items:baseline;min-width:0}
details>summary .pv{margin-left:auto;max-width:62%;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;font-weight:400;color:var(--muted);
  font:400 11px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
details.tool.fail>summary .pv{color:var(--fail)}
.badge{font:11px/1 ui-monospace,monospace;padding:2px 7px;border-radius:999px;
  border:1px solid currentColor}
.badge.ok{color:var(--ok)}
.badge.fail{color:var(--fail)}
.badge.info{color:var(--muted)}
.img{margin:10px 0 0}
.img img{max-width:100%;height:auto;border-radius:8px;border:1px solid var(--card-border);
  display:block}
.img figcaption{font:11px/1.5 ui-monospace,monospace;color:var(--muted);margin-top:6px;
  word-break:break-all}
.img.missing figcaption{color:var(--fail)}
.md-img{color:var(--muted)}
footer{margin-top:34px;padding-top:16px;border-top:1px solid var(--hairline);
  font-size:12px;color:var(--muted)}
)CSS";

/// One-line preview for a collapsed <summary>: the first non-empty line, with
/// runs of whitespace flattened and the tail elided. Collapsed blocks still have
/// to say *something*, or a reader has to open every one of them to find out
/// which command produced what.
std::string preview_line(const std::string& text, std::size_t max_chars) {
  std::string first;
  std::size_t i = 0;
  while (i < text.size()) {
    const std::size_t end = text.find('\n', i);
    const std::string line = trim(text.substr(i, end == std::string::npos ? std::string::npos
                                                                        : end - i));
    if (!line.empty()) {
      first = line;
      break;
    }
    if (end == std::string::npos) break;
    i = end + 1;
  }
  std::string flat;
  bool space = false;
  for (std::size_t k = 0; k < first.size(); ++k) {
    const char c = first[k];
    if (c == ' ' || c == '\t' || c == '\r') {
      space = true;
      continue;
    }
    if (space && !flat.empty()) flat += ' ';
    space = false;
    flat += c;
  }
  // Count characters, not bytes, so a CJK preview is not cut mid-glyph.
  std::size_t chars = 0;
  for (std::size_t k = 0; k < flat.size();) {
    const unsigned char c = static_cast<unsigned char>(flat[k]);
    const std::size_t step = (c < 0x80) ? 1 : ((c >> 5) == 0x6 ? 2 : ((c >> 4) == 0xE ? 3 : 4));
    k += step;
    ++chars;
  }
  if (chars <= max_chars) return esc(flat);
  // Cut on a character boundary, not a byte one.
  std::size_t bytes = 0, seen = 0;
  while (bytes < flat.size() && seen < max_chars) {
    const unsigned char c = static_cast<unsigned char>(flat[bytes]);
    const std::size_t step = (c < 0x80) ? 1 : ((c >> 5) == 0x6 ? 2 : ((c >> 4) == 0xE ? 3 : 4));
    bytes += step;
    ++seen;
  }
  // On a character boundary: the export is a document, and half a character
  // in it is not readable.
  return esc(truncate_utf8(flat, bytes)) + "…";
}

std::string message_html(const Workspace& ws, const Message& m, std::size_t* image_budget,
                         const ExportLimits& limits, std::size_t* json_budget,
                         std::map<std::string, std::string>* image_data, bool* images_omitted) {
  const std::string role = m.role.empty() ? "unknown" : m.role;
  std::ostringstream out;
  out << "<li class=\"msg " << esc(role) << "\">";
  out << "<div class=\"who\">";
  if (role == "user") out << "用户";
  else if (role == "assistant") out << "助手";
  else if (role == "tool") out << "工具";
  else if (role == "system") out << "系统";
  else out << esc(role);
  if (!m.name.empty()) out << " <span class=\"t\">" << esc(m.name) << "</span>";
  if (!m.created_at.empty()) out << " <span class=\"t\">" << esc(m.created_at) << "</span>";
  if (role == "tool") {
    out << " <span class=\"badge " << (m.ok ? "ok" : "fail") << "\">"
        << (m.ok ? "ok" : "失败") << "</span>";
    if (m.elapsed_ms > 0) out << " <span class=\"t\">" << m.elapsed_ms << " ms</span>";
  }
  out << "</div>";

  if (!m.reasoning.empty()) {
    out << "<details class=\"reasoning\"><summary>思考过程</summary><pre>"
        << esc(m.reasoning) << "</pre></details>";
  }

  if (role == "tool") {
    std::string body = m.content;
    bool cut = m.truncated;
    if (limits.tool_output_bytes > 0 && body.size() > limits.tool_output_bytes) {
      body = truncate_utf8(body, limits.tool_output_bytes, &cut) +
             "\n... [导出时截断：原始输出 " + std::to_string(m.content.size()) + " 字节]";
    }
    // Collapsed by default: a transcript is read, not scrolled through - the
    // summary carries the size and the first line so a reader can tell which
    // block to open.
    out << "<details class=\"tool " << (m.ok ? "ok" : "fail") << "\"><summary>"
        << (m.ok ? "输出" : "输出（失败）") << " · " << human_size(m.content.size())
        << (cut ? " · 已截断" : "");
    const std::string pv = preview_line(m.content, 90);
    if (!pv.empty()) out << "<span class=\"pv\">" << pv << "</span>";
    out << "</summary><pre>" << esc(body) << "</pre></details>";
  } else if (!m.content.empty()) {
    out << "<div class=\"body\">" << markdown_to_html(m.content) << "</div>";
  }

  for (std::size_t i = 0; i < m.tool_calls.size(); ++i) {
    const ToolCall& call = m.tool_calls[i];
    const std::string args = call.raw_arguments.empty() ? dump_json(call.arguments, 2)
                                                       : call.raw_arguments;
    out << "<details class=\"call\"><summary>调用 " << esc(call.name);
    // The interesting part of a call is usually one field ("command", "path").
    // Show it in the summary so a collapsed call still reads as a sentence.
    const std::string pv = preview_line(args, 80);
    if (!pv.empty()) out << "<span class=\"pv\">" << pv << "</span>";
    out << "</summary><pre>" << esc(args) << "</pre></details>";
  }

  for (std::size_t i = 0; i < m.images.size(); ++i) {
    out << image_block(ws, m.images[i], image_budget, limits, json_budget, image_data,
                       images_omitted);
  }
  out << "</li>";
  return out.str();
}

}  // namespace

namespace {

// ---------------------------------------------------------------------------
// the download menu: one turn ("会话") or the whole topic ("主题")
// ---------------------------------------------------------------------------

/// A turn is one question and everything that came after it until the next
/// question. The UI counts them the same way (the right-hand jump index
/// numbers the user messages), so "turn 3" means the same on both sides.
struct ExportTurn {
  std::size_t user_index = 0;       // the question itself
  std::vector<std::size_t> indices;  // every message of the turn, in order
};

std::vector<ExportTurn> split_turns(const Session& session) {
  std::vector<ExportTurn> turns;
  for (std::size_t i = 0; i < session.messages.size(); ++i) {
    const Message& m = session.messages[i];
    if (m.role == "user") {
      ExportTurn t;
      t.user_index = i;
      t.indices.push_back(i);
      turns.push_back(t);
      continue;
    }
    // Whatever came before the first question is not part of a turn.
    if (turns.empty()) continue;
    turns.back().indices.push_back(i);
  }
  return turns;
}

/// The answer of a turn: the last thing the assistant said before the next
/// question. A turn that ended in a tool call has no answer - the transcript
/// then shows the call itself.
std::size_t final_answer_index(const Session& session, const ExportTurn& turn) {
  for (std::size_t k = turn.indices.size(); k > 0; --k) {
    const std::size_t idx = turn.indices[k - 1];
    const Message& m = session.messages[idx];
    if (m.role == "assistant" && !trim(m.content).empty()) return idx;
  }
  return static_cast<std::size_t>(-1);
}

/// The tool results of a turn, by the id of the call they answer, plus the
/// ids the assistant actually declared: a result whose call is missing has
/// nowhere to go otherwise.
struct TurnCalls {
  std::map<std::string, const Message*> results;
  std::set<std::string> declared;
};

TurnCalls turn_calls(const Session& session, const ExportTurn& turn) {
  TurnCalls out;
  for (std::size_t i = 0; i < turn.indices.size(); ++i) {
    const Message& m = session.messages[turn.indices[i]];
    if (m.role == "tool") {
      if (!m.tool_call_id.empty()) out.results[m.tool_call_id] = &m;
    } else if (m.role == "assistant") {
      for (std::size_t k = 0; k < m.tool_calls.size(); ++k) {
        out.declared.insert(m.tool_calls[k].id);
      }
    }
  }
  return out;
}

/// Was the call answered, and how did it go? The output itself is not in the
/// file: a transcript says what was called and whether it worked.
std::string call_status_html(const TurnCalls& calls, const ToolCall& call) {
  std::map<std::string, const Message*>::const_iterator it = calls.results.find(call.id);
  if (it == calls.results.end() || it->second == NULL) {
    return "<span class=\"badge info\">未返回</span>";
  }
  if (it->second->cancelled) return "<span class=\"badge info\">已中断</span>";
  if (it->second->ok) return "<span class=\"badge ok\">成功</span>";
  return "<span class=\"badge fail\">失败</span>";
}

std::string who_line(const std::string& label, const Message& m) {
  std::string out = "<div class=\"who\">" + label;
  if (!m.created_at.empty()) out += " <span class=\"t\">" + esc(m.created_at) + "</span>";
  out += "</div>";
  return out;
}

/// The question of a turn: what the user wrote, and the pictures they pasted.
std::string question_html(const Workspace& ws, const Message& m, std::size_t* image_budget,
                          const ExportLimits& limits, std::size_t* json_budget,
                          std::map<std::string, std::string>* image_data,
                          bool* images_omitted) {
  std::ostringstream out;
  out << "<li class=\"msg user\">" << who_line("用户", m);
  if (!trim(m.content).empty()) {
    out << "<div class=\"body\">" << markdown_to_html(m.content) << "</div>";
  }
  for (std::size_t i = 0; i < m.images.size(); ++i) {
    out << image_block(ws, m.images[i], image_budget, limits, json_budget, image_data,
                       images_omitted);
  }
  out << "</li>\n";
  return out.str();
}

/// One answer of the assistant: what it thought, what it said, what it called.
std::string answer_html(const Workspace& ws, const Message& m, const TurnCalls& calls,
                        std::size_t* image_budget, const ExportLimits& limits,
                        std::size_t* json_budget,
                        std::map<std::string, std::string>* image_data, bool* images_omitted) {
  std::ostringstream out;
  out << "<li class=\"msg assistant\">" << who_line("助手", m);
  if (!m.reasoning.empty()) {
    out << "<details class=\"reasoning\"><summary>思考过程</summary><pre>"
        << esc(m.reasoning) << "</pre></details>";
  }
  if (!trim(m.content).empty()) {
    out << "<div class=\"body\">" << markdown_to_html(m.content) << "</div>";
  }
  for (std::size_t i = 0; i < m.tool_calls.size(); ++i) {
    const ToolCall& call = m.tool_calls[i];
    const std::string args = call.raw_arguments.empty() ? dump_json(call.arguments, 2)
                                                       : call.raw_arguments;
    out << "<details class=\"call\"><summary>调用 " << esc(call.name) << " "
        << call_status_html(calls, call);
    const std::string pv = preview_line(args, 80);
    if (!pv.empty()) out << "<span class=\"pv\">" << pv << "</span>";
    out << "</summary><pre>" << esc(args) << "</pre></details>\n";
  }
  for (std::size_t i = 0; i < m.images.size(); ++i) {
    out << image_block(ws, m.images[i], image_budget, limits, json_budget, image_data,
                       images_omitted);
  }
  out << "</li>\n";
  return out.str();
}

}  // namespace

std::size_t session_turn_count(const Session& session) {
  std::size_t n = 0;
  for (std::size_t i = 0; i < session.messages.size(); ++i) {
    if (session.messages[i].role == "user") ++n;
  }
  return n;
}

std::string export_conversation_html(const Workspace& workspace, const Session& session,
                                     const std::string& version,
                                     const ExportSelection& selection,
                                     const ExportLimits& limits,
                                     std::map<std::string, std::string>* image_data,
                                     std::size_t* message_count) {
  const std::string title = session.info.title.empty() ? "会话" : session.info.title;
  std::size_t budget = limits.total_image_bytes;
  std::size_t json_budget = limits.json_image_bytes;
  bool images_omitted = false;
  std::map<std::string, std::string> own_images;
  std::map<std::string, std::string>* images = image_data ? image_data : &own_images;

  const std::vector<ExportTurn> turns = split_turns(session);
  const bool brief = selection.detail == ExportSelection::Detail::kBrief;

  // Which turns are in the file. "turn 0" means the last one: the newest
  // thing said, which is what "the current turn" means to somebody who did
  // not pick a number.
  std::size_t first = 0;
  std::size_t count = turns.size();
  int number = selection.turn;
  if (selection.scope == ExportSelection::Scope::kTurn) {
    if (number < 1) number = static_cast<int>(turns.size());
    if (number < 1 || static_cast<std::size_t>(number) > turns.size()) {
      first = 0;
      count = 0;
    } else {
      first = static_cast<std::size_t>(number) - 1;
      count = 1;
    }
  }

  // The messages that end up in the file - and therefore the messages the
  // JSON block carries. The tool output is not in the file, so it is not in
  // the JSON either: what the reader sees is what the file carries.
  Session shown;
  shown.info = session.info;
  shown.info.message_count = 0;
  if (selection.scope == ExportSelection::Scope::kTurn && number >= 1) {
    shown.info.title = title + "（第 " + std::to_string(number) + " 条）";
  }

  std::ostringstream body;
  for (std::size_t t = first; t < first + count && t < turns.size(); ++t) {
    const ExportTurn& turn = turns[t];
    const TurnCalls calls = turn_calls(session, turn);
    body << "<li class=\"msg system\"><div class=\"who\">第 " << (t + 1) << " 条会话</div></li>\n";

    const Message& q = session.messages[turn.user_index];
    body << question_html(workspace, q, &budget, limits, &json_budget, images, &images_omitted);
    shown.messages.push_back(q);

    if (brief) {
      // Only the question and the answer: the reader wants the exchange, not
      // the machinery behind it.
      const std::size_t ans = final_answer_index(session, turn);
      if (ans != static_cast<std::size_t>(-1)) {
        const Message& a = session.messages[ans];
        body << "<li class=\"msg assistant\">" << who_line("助手", a);
        if (!trim(a.content).empty()) {
          body << "<div class=\"body\">" << markdown_to_html(a.content) << "</div>";
        }
        body << "</li>\n";
        // The brief version shows the answer and nothing else: the thinking
        // and the calls behind it are not in the file, so they are not in the
        // JSON block either.
        Message brief = a;
        brief.reasoning.clear();
        brief.tool_calls.clear();
        shown.messages.push_back(brief);
      }
      continue;
    }

    for (std::size_t i = 0; i < turn.indices.size(); ++i) {
      const std::size_t idx = turn.indices[i];
      if (idx == turn.user_index) continue;  // the question is already out
      const Message& m = session.messages[idx];
      if (m.role == "assistant") {
        body << answer_html(workspace, m, calls, &budget, limits, &json_budget, images,
                            &images_omitted);
        shown.messages.push_back(m);
      } else if (m.role == "tool") {
        // The result is already on the call it answers (the badge). A result
        // without a call would vanish - say so instead.
        if (m.tool_call_id.empty() ||
            calls.declared.find(m.tool_call_id) == calls.declared.end()) {
          body << "<li class=\"msg tool\">" << who_line("工具", m)
               << "<div class=\"body\">（没有对应的调用记录）</div></li>\n";
        }
        Message trimmed = m;
        trimmed.content = "（本次下载不含工具输出）";
        shown.messages.push_back(trimmed);
      }
    }
  }

  std::ostringstream out;
  out << "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
         "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n<title>"
      << esc(title) << " · " << esc(workspace.entry().name) << "</title>\n<style>"
      << kStyle << "</style>\n</head>\n<body>\n<main>\n<header class=\"top\">\n<h1>"
      << esc(title) << "</h1>\n<dl class=\"meta\">\n";
  out << "<dt>工作区</dt><dd>" << esc(workspace.entry().name) << " · "
      << esc(workspace.root()) << "</dd>\n";
  out << "<dt>主题</dt><dd>" << esc(session.info.id) << "</dd>\n";
  if (selection.scope == ExportSelection::Scope::kTurn) {
    out << "<dt>范围</dt><dd>当前会话（第 " << number << " 条，共 " << turns.size()
        << " 条）</dd>\n";
  } else {
    out << "<dt>范围</dt><dd>整个主题（" << turns.size() << " 条会话）</dd>\n";
  }
  out << "<dt>内容</dt><dd>"
      << (brief ? "用户输入与最终回答" : "用户输入、思考、工具调用与最终回答") << "</dd>\n";
  out << "<dt>时间</dt><dd>" << esc(session.info.created_at);
  if (!session.info.updated_at.empty() && session.info.updated_at != session.info.created_at) {
    out << " → " << esc(session.info.updated_at);
  }
  out << "</dd>\n";
  out << "<dt>导出</dt><dd>tea_agent " << esc(version) << " · " << esc(now_iso8601())
      << "</dd>\n</dl>\n";
  out << "<p class=\"lede\">这是 tea_agent 导出的静态快照：不依赖服务器、不请求网络，"
         "直接双击打开即可阅读。工具输出没有包含在本次下载里（只记录了调用参数与成功与否）。"
         "页末的 <code>tea-session-data</code> 块保存了本次导出的会话 JSON，"
         "可用 <code>POST /api/workspaces/{id}/sessions/import</code> 恢复成一个新会话。</p>\n"
         "</header>\n<ol class=\"turns\">\n";
  out << body.str();
  out << "</ol>\n<footer>由 tea_agent " << esc(version) << " 于 " << esc(now_iso8601())
      << " 导出，共 " << shown.messages.size()
      << " 条消息。此文件不含任何外部资源。</footer>\n</main>\n";

  // The JSON block is the restore path. "</" is escaped so a message containing
  // "</script>" cannot end the block early.
  const std::string payload =
      dump_json(export_session_json(workspace, shown, version, images, images_omitted), 2);
  std::string safe_payload;
  for (std::size_t i = 0; i < payload.size(); ++i) {
    if (payload[i] == '<' && i + 1 < payload.size() && payload[i + 1] == '/') {
      safe_payload += "<\\/";
      ++i;
    } else {
      safe_payload += payload[i];
    }
  }
  out << "<script type=\"application/json\" id=\"tea-session-data\">\n" << safe_payload
      << "\n</script>\n</body>\n</html>\n";
  if (message_count) *message_count = shown.messages.size();
  return out.str();
}
nlohmann::json export_session_json(const Workspace& workspace, const Session& session,
                                   const std::string& version,
                                   const std::map<std::string, std::string>* image_data,
                                   bool images_omitted) {
  nlohmann::json messages = nlohmann::json::array();
  for (std::size_t i = 0; i < session.messages.size(); ++i) {
    messages.push_back(session.messages[i].to_store());
  }
  nlohmann::json j = nlohmann::json::object();
  j["format"] = "tea_agent.session";
  j["format_version"] = 1;
  j["exported_at"] = now_iso8601();
  j["exported_by"] = std::string("tea_agent/") + version;
  j["workspace"] = {{"id", workspace.id()}, {"name", workspace.entry().name},
                    {"path", workspace.root()}};
  j["session"] = session.info.to_json();
  j["messages"] = messages;
  if (!session.summary.empty()) {
    j["summary"] = session.summary;
    j["summary_upto"] = static_cast<long long>(session.summary_upto);
  }
  if (!session.artifacts.empty()) {
    nlohmann::json arts = nlohmann::json::array();
    for (std::size_t i = 0; i < session.artifacts.size(); ++i) {
      arts.push_back(session.artifacts[i].to_json());
    }
    j["artifacts"] = arts;  // what the session produced, for a restore
  }
  // The plan of the session: the live list, or - between the turns - the
  // steps the last turn left behind. Progress travels with the conversation.
  const std::vector<TodoItem>& plan =
      session.todos.empty() ? session.todo_carry : session.todos;
  if (!plan.empty()) {
    nlohmann::json todos = nlohmann::json::array();
    for (std::size_t i = 0; i < plan.size(); ++i) {
      todos.push_back(plan[i].to_json());
    }
    j["todos"] = todos;
  }
  if (image_data && !image_data->empty()) {
    nlohmann::json images = nlohmann::json::object();
    for (std::map<std::string, std::string>::const_iterator it = image_data->begin();
         it != image_data->end(); ++it) {
      images[it->first] = it->second;
    }
    j["images"] = images;  // path -> data: URL, written back on import
  }
  if (images_omitted) j["images_omitted"] = true;
  return j;
}

bool extract_session_json_from_html(const std::string& html, nlohmann::json* out,
                                    std::string* err) {
  const std::string marker = "id=\"tea-session-data\"";
  std::size_t at = html.find(marker);
  if (at == std::string::npos) {
    // Also accept the payload on its own (a caller that pasted just the JSON).
    try {
      nlohmann::json parsed = nlohmann::json::parse(html);
      if (parsed.is_object() && parsed.contains("messages")) {
        *out = parsed;
        return true;
      }
    } catch (const std::exception&) {
    }
    if (err) *err = "not a tea_agent export: no tea-session-data block (and not a session JSON)";
    return false;
  }
  const std::size_t open = html.find('>', at);
  const std::size_t close = open == std::string::npos ? std::string::npos
                                                     : html.find("</script>", open);
  if (open == std::string::npos || close == std::string::npos) {
    if (err) *err = "the tea-session-data block is truncated";
    return false;
  }
  std::string body = html.substr(open + 1, close - open - 1);
  // The writer escaped "</" as "<\/" so a message cannot end the block early.
  body = replace_all(body, "<\\/", "</");
  try {
    *out = nlohmann::json::parse(body);
  } catch (const std::exception& e) {
    if (err) *err = std::string("the embedded session JSON does not parse: ") + e.what();
    return false;
  }
  return true;
}

bool import_session_payload(const Workspace& workspace, const nlohmann::json& payload,
                            Session* out, std::string* err) {
  if (!payload.is_object()) {
    if (err) *err = "the payload is not a JSON object";
    return false;
  }
  const nlohmann::json msgs = payload.contains("messages") ? payload["messages"]
                                                          : nlohmann::json::array();
  if (!msgs.is_array()) {
    if (err) *err = "the payload has no 'messages' array";
    return false;
  }
  if (msgs.empty()) {
    if (err) *err = "the payload contains no messages";
    return false;
  }

  Session session;
  session.info.id = make_id("s_");
  std::string base_title = "导入的会话";
  if (payload.contains("session") && payload["session"].is_object()) {
    const nlohmann::json& meta = payload["session"];
    if (meta.contains("title") && meta["title"].is_string()) {
      base_title = trim(meta["title"].get<std::string>());
      if (base_title.empty()) base_title = "导入的会话";
    }
  }
  session.info.title = base_title;
  session.info.created_at = now_iso8601();
  session.info.updated_at = session.info.created_at;

  // Image data URLs travel with the payload so a conversation restored on
  // another machine still shows its pictures. Files are named afresh: the same
  // bytes may already be here under another workspace's path.
  std::map<std::string, std::string> rewrites;
  if (payload.contains("images") && payload["images"].is_object()) {
    const std::string dir = path_join(workspace.artifacts_dir(), "images");
    if (!make_dirs(dir)) {
      if (err) *err = "cannot create " + dir;
      return false;
    }
    for (auto it = payload["images"].begin(); it != payload["images"].end(); ++it) {
      const std::string path = it.key();
      if (!it.value().is_string()) continue;
      const std::string url = it.value().get<std::string>();
      const std::size_t comma = url.find(";base64,");
      if (comma == std::string::npos || !starts_with(url, "data:")) continue;
      const std::string mime = url.substr(5, comma - 5);
      std::string bytes;
      if (!base64_decode(url.substr(comma + 8), &bytes) || bytes.empty()) continue;
      const char* sniffed = sniff_image_mime(bytes);
      if (!sniffed) continue;
      const std::string file =
          path_join(dir, "img_" + random_hex(8) + image_extension_for(sniffed));
      if (!write_file(file, bytes, false, nullptr)) continue;
      rewrites[path] = workspace.display_path(file);
    }
  }

  for (std::size_t i = 0; i < msgs.size(); ++i) {
    Message m = Message::from_store(msgs[i]);
    if (m.role.empty()) continue;
    for (std::size_t k = 0; k < m.images.size(); ++k) {
      std::map<std::string, std::string>::const_iterator it = rewrites.find(m.images[k].path);
      if (it != rewrites.end()) m.images[k].path = it->second;
    }
    session.messages.push_back(m);
  }
  if (session.messages.empty()) {
    if (err) *err = "the payload contains no usable messages";
    return false;
  }
  session.info.message_count = static_cast<int>(session.messages.size());
  if (payload.contains("summary") && payload["summary"].is_string()) {
    session.summary = payload["summary"].get<std::string>();
    if (payload.contains("summary_upto") && payload["summary_upto"].is_number_integer()) {
      session.summary_upto = static_cast<std::size_t>(payload["summary_upto"].get<long long>());
      if (session.summary_upto > session.messages.size()) {
        session.summary_upto = session.messages.size();
      }
    }
  }
  if (payload.contains("todos") && payload["todos"].is_array()) {
    for (auto it = payload["todos"].begin(); it != payload["todos"].end(); ++it) {
      TodoItem item = TodoItem::from_json(*it);
      if (trim(item.text).empty()) continue;
      if (item.id.empty()) item.id = "t" + std::to_string(session.todos.size() + 1);
      session.todos.push_back(item);
    }
  }
  if (!workspace.save_session(session, err)) return false;
  *out = session;
  return true;
}

std::string render_markdown_page(const std::string& title, const std::string& markdown) {
  std::ostringstream out;
  out << "<!doctype html>\n<html lang=\"zh\">\n<head>\n"
      << "<meta charset=\"utf-8\">\n"
      << "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
      << "<title>" << esc(title) << "</title>\n"
      << "<style>" << kDocStyle << "</style>\n"
      << "</head>\n<body>\n<main>\n"
      << markdown_to_html(markdown)
      << "\n</main>\n</body>\n</html>\n";
  return out.str();
}

std::string export_file_name_for(const std::string& session_title, const std::string& tag) {
  std::string base;
  for (std::size_t i = 0; i < session_title.size(); ++i) {
    const unsigned char c = static_cast<unsigned char>(session_title[i]);
    const bool plain = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                       (c >= '0' && c <= '9') || c == '-' || c == '_';
    base += plain ? static_cast<char>(c) : '-';
  }
  while (!base.empty() && (base.front() == '-' || base.back() == '-')) {
    if (base.front() == '-') base.erase(0, 1);
    if (!base.empty() && base.back() == '-') base.pop_back();
  }
  if (base.size() > 48) base = truncate_utf8(base, 48);
  if (base.empty()) base = "session";

  std::string digits;
  const std::string stamp = now_iso8601();
  for (std::size_t i = 0; i < stamp.size(); ++i) {
    if (stamp[i] >= '0' && stamp[i] <= '9') digits += stamp[i];
  }
  const std::string when =
      digits.size() >= 14 ? (digits.substr(0, 8) + "-" + digits.substr(8, 6)) : digits;
  // The tag ("turn-3-brief") tells the four files of one session apart.
  return "tea_agent-" + base + (tag.empty() ? "" : "-" + tag) +
         (when.empty() ? "" : "-" + when) + ".html";
}

std::string export_file_name(const std::string& session_title) {
  return export_file_name_for(session_title, "");
}

std::string export_session_html(const Workspace& workspace, const Session& session,
                                const std::string& version, const ExportLimits& limits,
                                std::map<std::string, std::string>* image_data) {
  const std::string title = session.info.title.empty() ? "会话" : session.info.title;
  std::size_t image_budget = limits.total_image_bytes;
  std::size_t json_budget = limits.json_image_bytes;
  bool images_omitted = false;

  std::ostringstream out;
  out << "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
         "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
         "<title>"
      << esc(title) << " · " << esc(workspace.entry().name) << "</title>\n<style>"
      << kStyle << "</style>\n</head>\n<body>\n<main>\n<header class=\"top\">\n<h1>"
      << esc(title) << "</h1>\n<dl class=\"meta\">\n";
  out << "<dt>工作区</dt><dd>" << esc(workspace.entry().name) << " · "
      << esc(workspace.root()) << "</dd>\n";
  out << "<dt>会话</dt><dd>" << esc(session.info.id) << "</dd>\n";
  out << "<dt>时间</dt><dd>" << esc(session.info.created_at);
  if (!session.info.updated_at.empty() && session.info.updated_at != session.info.created_at) {
    out << " → " << esc(session.info.updated_at);
  }
  out << "</dd>\n";
  out << "<dt>消息</dt><dd>" << session.messages.size() << " 条</dd>\n";
  if (!session.info.forked_from.empty()) {
    out << "<dt>分叉自</dt><dd>" << esc(session.info.forked_from) << " 第 "
        << session.info.fork_turn << " 轮</dd>\n";
  }
  out << "<dt>导出</dt><dd>tea_agent " << esc(version) << " · " << esc(now_iso8601())
      << "</dd>\n</dl>\n";
  if (!session.artifacts.empty()) {
    out << "<section class=\"artifacts\">\n<div class=\"artifacts-head\"><h2>本轮产出</h2>"
           "<span class=\"artifacts-count\">"
        << session.artifacts.size() << " 个文件</span></div>\n"
        << "<ul class=\"artifacts-list\">\n";
    for (std::size_t i = 0; i < session.artifacts.size(); ++i) {
      const SessionArtifact& a = session.artifacts[i];
      out << "<li class=\"artifact\"><span class=\"kind\">" << esc(a.kind) << "</span>"
          << "<span class=\"name\">" << esc(a.path) << "</span>"
          << "<span class=\"size\">" << human_size(static_cast<uint64_t>(a.bytes))
          << "</span></li>\n";
    }
    out << "</ul>\n<p class=\"artifacts-note\">这些文件留在原工作区里，本 HTML 是静态快照，"
           "不含文件本体。</p>\n</section>\n";
  }

  // The plan of the session: the live list, or the steps the last turn left.
  const std::vector<TodoItem>& plan =
      session.todos.empty() ? session.todo_carry : session.todos;
  if (!plan.empty()) {
    int total = 0, done = 0;
    todo_counts(plan, &total, &done);
    const int pct = total > 0 ? static_cast<int>(done * 100 / total) : 0;
    out << "<section class=\"todos\">\n<div class=\"todos-head\"><h2>任务</h2>"
           "<span class=\"todos-count\">"
        << done << "/" << total << " 完成 · " << pct << "%</span></div>\n"
        << "<div class=\"todos-bar\"><i style=\"width:" << pct << "%\"></i></div>\n"
        << "<ol class=\"todos-list\">\n";
    for (std::size_t i = 0; i < plan.size(); ++i) {
      const TodoItem& t = plan[i];
      out << "<li class=\"todo " << esc(t.status) << "\"><span class=\"mark\">"
          << (t.status == "done" ? "✓" : (t.status == "active" ? "●" : "○")) << "</span>"
          << "<span class=\"txt\">" << esc(t.text) << "</span></li>\n";
    }
    out << "</ol>\n</section>\n";
  }

  out << "<p class=\"lede\">这是 tea_agent 导出的静态快照：不依赖服务器、不请求网络，"
         "直接双击打开即可阅读。页末的 <code>tea-session-data</code> 块保存了完整会话 JSON，"
         "可用 <code>POST /api/workspaces/{id}/sessions/import</code> 恢复成一个新会话。</p>\n"
         "</header>\n<ol class=\"turns\">\n";

  if (!session.summary.empty()) {
    out << "<li class=\"msg system\"><div class=\"who\">早期对话摘要</div>"
           "<details class=\"reasoning\" open><summary>压缩摘要</summary><pre>"
        << esc(session.summary) << "</pre></details></li>\n";
  }

  for (std::size_t i = 0; i < session.messages.size(); ++i) {
    out << message_html(workspace, session.messages[i], &image_budget, limits, &json_budget,
                        image_data, &images_omitted)
        << "\n";
  }

  out << "</ol>\n<footer>由 tea_agent " << esc(version) << " 于 " << esc(now_iso8601())
      << " 导出，共 " << session.messages.size()
      << " 条消息。此文件不含任何外部资源。</footer>\n</main>\n";

  // The JSON block is the restore path. "</" is escaped so a message containing
  // "</script>" cannot end the block early (HTML parsers only look for the
  // literal sequence, which is why the backslash form is enough).
  std::string payload = dump_json(export_session_json(workspace, session, version, image_data,
                                                      images_omitted),
                                  2);
  std::string safe_payload;
  for (std::size_t i = 0; i < payload.size(); ++i) {
    if (payload[i] == '<' && i + 1 < payload.size() && payload[i + 1] == '/') {
      safe_payload += "<\\/";
      ++i;
    } else {
      safe_payload += payload[i];
    }
  }
  out << "<script type=\"application/json\" id=\"tea-session-data\">\n" << safe_payload
      << "\n</script>\n</body>\n</html>\n";
  return out.str();
}

}  // namespace tea
