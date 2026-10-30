#include "provider_yaml.h"

#include <cctype>
#include <cstring>
#include <sstream>

#include "util.h"

namespace tea {
namespace {

// --------------------------------------------------------------------------
// line handling
// --------------------------------------------------------------------------

/// The indentation of a line (tabs count as one column; YAML does not allow
/// them for indentation anyway).
int indent_of(const std::string& line) {
  int n = 0;
  for (std::size_t i = 0; i < line.size(); ++i) {
    if (line[i] == ' ') {
      ++n;
    } else if (line[i] == '\t') {
      ++n;
    } else {
      break;
    }
  }
  return n;
}

bool is_blank(const std::string& line) { return trim(line).empty(); }

/// A line that only carries a comment. YAML ignores it wherever it stands,
/// so it must not be mistaken for the end of a block.
bool is_comment(const std::string& line) {
  const std::string s = trim(line);
  return !s.empty() && s[0] == '#';
}

/// The key of a "key: value" line, without the quotes a YAML author may have
/// put around it. Empty when the line is not a mapping entry.
std::string parse_key(const std::string& line) {
  const std::string s = trim(line);
  if (s.empty() || s[0] == '#' || s[0] == '-') return std::string();
  // Find the ':' that ends the key: the first one outside quotes that is
  // followed by a space or ends the line.
  char quote = 0;
  for (std::size_t i = 0; i < s.size(); ++i) {
    const char c = s[i];
    if (quote) {
      if (c == quote) quote = 0;
      continue;
    }
    if (c == '"' || c == '\'') {
      quote = c;
      continue;
    }
    if (c == ':' && (i + 1 == s.size() || s[i + 1] == ' ')) {
      std::string key = trim(s.substr(0, i));
      if (key.size() >= 2 && ((key.front() == '"' && key.back() == '"') ||
                              (key.front() == '\'' && key.back() == '\''))) {
        key = key.substr(1, key.size() - 2);
      }
      return key;
    }
  }
  return std::string();
}

/// A YAML key that has to be quoted: anything that could be read as something
/// else (a number, a boolean, a mapping) or that carries a character YAML
/// gives a meaning to.
std::string yaml_key(const std::string& key) {
  if (key.empty()) return "\"\"";
  bool need_quotes = false;
  const std::string lower = to_lower(key);
  if (lower == "true" || lower == "false" || lower == "yes" || lower == "no" ||
      lower == "on" || lower == "off" || lower == "null" || lower == "~") {
    need_quotes = true;
  }
  // A plain scalar that is only digits would come back as a number.
  bool all_digits = !key.empty();
  for (std::size_t i = 0; i < key.size(); ++i) {
    if (!std::isdigit(static_cast<unsigned char>(key[i]))) {
      all_digits = false;
      break;
    }
  }
  if (all_digits) need_quotes = true;
  static const char* kSpecial = ":#{}[]&*!|>'\"%@`,\n\t";
  for (std::size_t i = 0; i < key.size(); ++i) {
    if (std::strchr(kSpecial, key[i]) != nullptr) {
      need_quotes = true;
      break;
    }
  }
  if (key.front() == ' ' || key.back() == ' ') need_quotes = true;
  if (key.front() == '-' || key.front() == '?') need_quotes = true;
  if (!need_quotes) return key;
  std::string out = "\"";
  for (std::size_t i = 0; i < key.size(); ++i) {
    if (key[i] == '"' || key[i] == '\\') out += '\\';
    out += key[i];
  }
  out += "\"";
  return out;
}

std::string yaml_scalar(const std::string& value) {
  if (value.empty()) return "\"\"";
  bool need_quotes = false;
  static const char* kSpecial = ":#{}[]&*!|>'\"%@`,\n\t";
  for (std::size_t i = 0; i < value.size(); ++i) {
    if (std::strchr(kSpecial, value[i]) != nullptr) {
      need_quotes = true;
      break;
    }
  }
  if (value.front() == ' ' || value.back() == ' ') need_quotes = true;
  if (!need_quotes) return value;
  std::string out = "\"";
  for (std::size_t i = 0; i < value.size(); ++i) {
    if (value[i] == '"' || value[i] == '\\') out += '\\';
    out += value[i];
  }
  out += "\"";
  return out;
}

/// The `models:` block, rendered.
std::string render_models_block(int models_indent, const std::vector<ModelInfo>& models) {
  const std::string pad(models_indent, ' ');
  const std::string pad2(models_indent + 2, ' ');
  const std::string pad3(models_indent + 4, ' ');
  std::string out;
  out += pad + "models:\n";
  for (std::size_t i = 0; i < models.size(); ++i) {
    const ModelInfo& m = models[i];
    out += pad2 + yaml_key(m.name) + ":\n";
    if (m.max_context_tokens > 0) {
      out += pad3 + "max_context_tokens: " + std::to_string(m.max_context_tokens) + "\n";
    }
    if (m.max_output_tokens > 0) {
      out += pad3 + "max_output_tokens: " + std::to_string(m.max_output_tokens) + "\n";
    }
    out += pad3 + "supports_tools: " + (m.supports_tools ? "true" : "false") + "\n";
    out += pad3 + "supports_reasoning: " + (m.supports_reasoning ? "true" : "false") + "\n";
    out += pad3 + "supports_vision: " + (m.supports_vision ? "true" : "false") + "\n";
    if (!m.reasoning_effort.empty() && m.reasoning_effort != "auto") {
      out += pad3 + "reasoning_effort: " + yaml_scalar(m.reasoning_effort) + "\n";
    }
    if (m.has_temperature) {
      std::ostringstream os;
      os.precision(6);
      os << m.temperature;
      out += pad3 + "temperature: " + os.str() + "\n";
    }
    if (m.has_top_p) {
      std::ostringstream os;
      os.precision(6);
      os << m.top_p;
      out += pad3 + "top_p: " + os.str() + "\n";
    }
    if (m.has_top_k && m.top_k > 0) {
      out += pad3 + "top_k: " + std::to_string(m.top_k) + "\n";
    }
    if (m.has_frequency_penalty) {
      std::ostringstream os;
      os.precision(6);
      os << m.frequency_penalty;
      out += pad3 + "frequency_penalty: " + os.str() + "\n";
    }
    if (m.has_presence_penalty) {
      std::ostringstream os;
      os.precision(6);
      os << m.presence_penalty;
      out += pad3 + "presence_penalty: " + os.str() + "\n";
    }
    if (m.has_seed) {
      out += pad3 + "seed: " + std::to_string(m.seed) + "\n";
    }
    if (!m.note.empty()) {
      out += pad3 + "note: " + yaml_scalar(m.note) + "\n";
    }
  }
  return out;
}

/// Split a rendered block into lines.
std::vector<std::string> split_block(const std::string& block) {
  std::vector<std::string> out;
  std::string cur;
  for (std::size_t i = 0; i < block.size(); ++i) {
    if (block[i] == '\n') {
      out.push_back(cur);
      cur.clear();
    } else {
      cur += block[i];
    }
  }
  if (!cur.empty()) out.push_back(cur);
  return out;
}

}  // namespace

bool patch_provider_models(const std::string& yaml_text, const std::string& provider,
                            const std::vector<ModelInfo>& models, std::string* out,
                            std::string* err) {
  if (!out) return false;
  const std::string provider_name = trim(provider);
  if (provider_name.empty()) {
    if (err) *err = "no provider name given";
    return false;
  }

  // Keep the line ending of the file: a Windows file stays CRLF.
  const bool crlf = yaml_text.find("\r\n") != std::string::npos;
  const std::string eol = crlf ? "\r\n" : "\n";
  std::vector<std::string> lines;
  {
    std::string cur;
    for (std::size_t i = 0; i < yaml_text.size(); ++i) {
      if (yaml_text[i] == '\n') {
        if (!cur.empty() && cur.back() == '\r') cur.pop_back();
        lines.push_back(cur);
        cur.clear();
      } else {
        cur += yaml_text[i];
      }
    }
    if (!cur.empty()) {
      if (!cur.empty() && cur.back() == '\r') cur.pop_back();
      lines.push_back(cur);
    }
  }
  if (lines.empty()) {
    if (err) *err = "provider.yaml is empty";
    return false;
  }

  // ---- the providers: mapping ------------------------------------------
  std::size_t providers_line = lines.size();
  for (std::size_t i = 0; i < lines.size(); ++i) {
    if (is_blank(lines[i]) || is_comment(lines[i])) continue;
    if (indent_of(lines[i]) != 0) continue;
    if (parse_key(lines[i]) == "providers") {
      providers_line = i;
      break;
    }
  }
  if (providers_line == lines.size()) {
    if (err) *err = "the file has no 'providers:' section";
    return false;
  }

  // ---- the provider -----------------------------------------------------
  std::size_t provider_line = lines.size();
  int provider_indent = 0;
  for (std::size_t i = providers_line + 1; i < lines.size(); ++i) {
    if (is_blank(lines[i])) continue;
    const int ind = indent_of(lines[i]);
    // A comment does not end the mapping; the next real top-level key does.
    if (ind == 0) {
      if (is_comment(lines[i])) continue;
      break;
    }
    if (parse_key(lines[i]) == provider_name) {
      provider_line = i;
      provider_indent = ind;
      break;
    }
  }
  if (provider_line == lines.size()) {
    if (err) *err = "provider '" + provider_name + "' is not in the file";
    return false;
  }

  // The block of the provider ends at the next top-level key.
  std::size_t provider_end = lines.size();
  for (std::size_t i = provider_line + 1; i < lines.size(); ++i) {
    if (is_blank(lines[i])) continue;
    if (indent_of(lines[i]) == 0) {
      if (is_comment(lines[i])) continue;
      provider_end = i;
      break;
    }
  }

  // The children of the provider live at one indent level.
  int child_indent = provider_indent + 2;
  for (std::size_t i = provider_line + 1; i < provider_end; ++i) {
    if (is_blank(lines[i])) continue;
    const int ind = indent_of(lines[i]);
    if (ind > provider_indent) {
      child_indent = ind;
      break;
    }
  }

  // ---- the models: block ------------------------------------------------
  std::size_t models_line = lines.size();
  for (std::size_t i = provider_line + 1; i < provider_end; ++i) {
    if (is_blank(lines[i])) continue;
    if (indent_of(lines[i]) != child_indent) continue;
    if (parse_key(lines[i]) == "models") {
      models_line = i;
      break;
    }
  }

  const std::string block = render_models_block(child_indent, models);
  if (models_line == lines.size()) {
    // No models: block yet - add one at the end of the provider.
    std::size_t insert_at = provider_end;
    while (insert_at > provider_line + 1 && is_blank(lines[insert_at - 1])) --insert_at;
    std::vector<std::string> out_lines;
    out_lines.reserve(lines.size() + 16);
    for (std::size_t i = 0; i < insert_at; ++i) out_lines.push_back(lines[i]);
    const std::vector<std::string> block_lines = split_block(block);
    for (std::size_t i = 0; i < block_lines.size(); ++i) out_lines.push_back(block_lines[i]);
    for (std::size_t i = insert_at; i < lines.size(); ++i) out_lines.push_back(lines[i]);
    std::string text;
    for (std::size_t i = 0; i < out_lines.size(); ++i) {
      text += out_lines[i];
      if (i + 1 < out_lines.size()) text += eol;
    }
    *out = text;
    return true;
  }

  // The block ends at the next line that is not deeper than the key.
  std::size_t models_end = lines.size();
  for (std::size_t i = models_line + 1; i < lines.size(); ++i) {
    if (is_blank(lines[i])) continue;
    if (indent_of(lines[i]) <= child_indent) {
      models_end = i;
      break;
    }
  }

  std::string text;
  for (std::size_t i = 0; i < models_line; ++i) {
    text += lines[i];
    text += eol;
  }
  // The rendered block already ends with a newline.
  text += block;
  for (std::size_t i = models_end; i < lines.size(); ++i) {
    text += lines[i];
    text += eol;
  }
  *out = text;
  return true;
}

}  // namespace tea
