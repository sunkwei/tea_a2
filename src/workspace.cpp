#include "workspace.h"

#include <yaml-cpp/yaml.h>

#include <algorithm>
#include <cstddef>
#include <fstream>
#include <iterator>

#include "env.h"
#include "log.h"
#include "state_lock.h"
#include "trajectory.h"
#include "util.h"

namespace tea {
namespace {

const char* const kRunDirName = ".tea_agent_run";
const char* const kMemoryFileName = "memory.json";

/// Hard ceiling for `<run_dir>/agent.md`. The file is injected into the system
/// prompt of every turn, so an unbounded append would silently eat the context
/// window (and invalidate the prompt cache with every byte). 32 KB is far more
/// experience than a single project needs; past it the tool tells the model to
/// compact the file with op=replace.
constexpr std::size_t kAgentDocMaxBytes = 32u * 1024u;

/// Hard ceiling for `<run_dir>/system_prompt.md`. The file replaces the built-in
/// persona in every request, so a runaway paste would inflate every turn (and
/// swallow the context budget) instead of failing once. 64 KB is far more than
/// a real persona needs.
constexpr std::size_t kSystemPromptMaxBytes = 64u * 1024u;

/// What a brand-new agent.md starts as. The headings are a suggestion for both
/// the model and the human editing it; the guidance block above them explains
/// what belongs here (and what belongs in memory.json instead).
const char* const kAgentDocTemplate =
    "# agent.md - workspace experience\n"
    "\n"
    "Accumulated by the agent for THIS workspace. Maintained with `toolkit_agent_doc`;\n"
    "edited by hand is fine too. Keep it short: one line per entry, delete what is no\n"
    "longer true instead of piling up history. Reusable scripts live in `.tea_agent_run/scripts/`.\n"
    "\n"
    "## Overview\n"
    "\n"
    "## Commands & scripts worth reusing\n"
    "\n"
    "## Pitfalls\n"
    "\n"
    "## Conventions\n";

nlohmann::json read_json_file(const std::string& path, bool* ok) {
  *ok = false;
  std::string text;
  if (!path_exists(path)) return nlohmann::json();
  if (!read_file(path, &text)) return nlohmann::json();
  try {
    nlohmann::json j = nlohmann::json::parse(text);
    *ok = true;
    return j;
  } catch (const std::exception&) {
    return nlohmann::json();
  }
}

/// Can the process really write into this workspace? A real write, because
/// "readable" and "usable" are different things here: the sessions, the log
/// and the memory all live in `<workspace>/.tea_agent_run/`. A sandbox, a
/// read-only mount or a deny ACE leaves the project readable and the
/// workspace unusable, and the only honest way to know is to try.
///
/// The probe writes into the run directory (the agent's own space), never
/// into the project tree, and takes the file away again.
bool probe_writable(const std::string& run_dir, std::string* err) {
  std::string reason;
  if (!make_dirs(run_dir, &reason)) {
    if (err) *err = "cannot create " + run_dir + (reason.empty() ? "" : ": " + reason);
    return false;
  }
  std::string stamp = now_iso8601();
  for (std::size_t i = 0; i < stamp.size(); ++i) {
    if (stamp[i] == ':') stamp[i] = '-';
  }
  const std::string probe = path_join(run_dir, ".tea_agent_write_probe-" + stamp);
  std::string write_err;
  if (!write_file(probe, "probe\n", false, &write_err)) {
    if (err) *err = write_err.empty() ? ("cannot write into " + run_dir) : write_err;
    return false;
  }
  remove_path(probe, false, nullptr);
  return true;
}

}  // namespace

// ---------------------------------------------------------------------------
// WorkspaceEntry
// ---------------------------------------------------------------------------
nlohmann::json WorkspaceEntry::to_json() const {
  nlohmann::json j = nlohmann::json::object();
  j["id"] = id;
  j["name"] = name;
  j["path"] = path;
  j["run_dir"] = run_dir.empty() ? path_join(path, kRunDirName) : run_dir;
  j["created_at"] = created_at;
  j["last_used_at"] = last_used_at;
  if (!last_session_id.empty()) j["last_session_id"] = last_session_id;
  j["exists"] = exists;
  j["sessions"] = sessions;
  j["writable"] = writable;
  if (!write_error.empty()) j["write_error"] = write_error;
  return j;
}

WorkspaceEntry WorkspaceEntry::from_json(const nlohmann::json& j) {
  WorkspaceEntry e;
  if (!j.is_object()) return e;
  auto str_of = [&](const char* key) -> std::string {
    auto it = j.find(key);
    if (it == j.end() || !it->is_string()) return std::string();
    return it->get<std::string>();
  };
  e.id = str_of("id");
  e.name = str_of("name");
  e.path = str_of("path");
  e.run_dir = str_of("run_dir");
  e.created_at = str_of("created_at");
  e.last_used_at = str_of("last_used_at");
  e.last_session_id = str_of("last_session_id");
  e.exists = path_is_dir(e.path);
  if (e.run_dir.empty() && !e.path.empty()) e.run_dir = path_join(e.path, kRunDirName);
  // The flags on disk are only a cache: refresh_flags() re-probes them, so a
  // workspace that was read-only yesterday and is writable today is reported
  // as writable.
  auto writable_it = j.find("writable");
  if (writable_it != j.end() && writable_it->is_boolean()) e.writable = writable_it->get<bool>();
  e.write_error = str_of("write_error");
  return e;
}

// ---------------------------------------------------------------------------
// WorkspaceRegistry
// ---------------------------------------------------------------------------
bool WorkspaceRegistry::load(const std::string& tea_home, std::string* err) {
  index_path_ = path_join(tea_home, "workspaces.json");
  entries_.clear();
  if (!path_exists(index_path_)) return true;

  std::string text;
  if (!read_file(index_path_, &text)) {
    if (err) *err = "cannot read workspace index: " + index_path_;
    return false;
  }
  try {
    nlohmann::json j = nlohmann::json::parse(text);
    const nlohmann::json* list = nullptr;
    if (j.is_object()) {
      auto it = j.find("workspaces");
      if (it != j.end() && it->is_array()) list = &(*it);
    } else if (j.is_array()) {
      list = &j;
    }
    if (list) {
      for (auto it = list->begin(); it != list->end(); ++it) {
        WorkspaceEntry e = WorkspaceEntry::from_json(*it);
        if (e.id.empty() && !e.path.empty()) e.id = make_id("ws_");
        if (!e.path.empty()) entries_.push_back(e);
      }
    }
  } catch (const std::exception& e) {
    if (err) *err = std::string("cannot parse workspace index ") + index_path_ + ": " + e.what();
    return false;
  }
  refresh_flags();
  LOG_INFO("workspaces: loaded ", entries_.size(), " entr(ies) from ", index_path_);
  return true;
}

bool WorkspaceRegistry::save(std::string* err) const {
  StateLock lock(state_mutex());
  nlohmann::json root = nlohmann::json::object();
  root["version"] = 1;
  root["updated_at"] = now_iso8601();
  nlohmann::json list = nlohmann::json::array();
  for (std::size_t i = 0; i < entries_.size(); ++i) list.push_back(entries_[i].to_json());
  root["workspaces"] = list;
  const std::string parent = path_parent(index_path_);
  if (!parent.empty()) make_dirs(parent);
  std::string write_err;
  if (!write_file_atomic(index_path_, dump_json(root, 2) + "\n", &write_err)) {
    // Remember it: the caller has to tell the user that the registration is
    // not on disk, because it is gone after a restart.
    last_save_error_ = write_err;
    if (err) *err = write_err;
    return false;
  }
  last_save_error_.clear();
  return true;
}

void WorkspaceRegistry::refresh_flags() {
  for (std::size_t i = 0; i < entries_.size(); ++i) {
    WorkspaceEntry& e = entries_[i];
    e.exists = path_is_dir(e.path);
    if (e.run_dir.empty()) e.run_dir = path_join(e.path, kRunDirName);
    e.writable = true;
    e.write_error.clear();
    if (!e.exists) {
      e.writable = false;
      e.write_error = "workspace directory does not exist: " + e.path;
    } else {
      // A real write, so the answer is the truth and not a guess.
      std::string reason;
      if (!probe_writable(e.run_dir, &reason)) {
        e.writable = false;
        e.write_error = reason;
      }
    }
    e.sessions = 0;
    const std::string sessions_dir = path_join(e.run_dir, "sessions");
    if (path_is_dir(sessions_dir)) {
      std::vector<DirEntryInfo> files;
      std::string err;
      if (list_directory(sessions_dir, &files, &err)) {
        for (std::size_t k = 0; k < files.size(); ++k) {
          if (files[k].is_dir) continue;
          if (files[k].name == "index.json") continue;
          if (path_extension(files[k].name) != ".json") continue;
          ++e.sessions;
        }
      }
    }
  }
}

WorkspaceEntry* WorkspaceRegistry::find(const std::string& id) {
  for (std::size_t i = 0; i < entries_.size(); ++i) {
    if (entries_[i].id == id) return &entries_[i];
  }
  return nullptr;
}

const WorkspaceEntry* WorkspaceRegistry::find(const std::string& id) const {
  for (std::size_t i = 0; i < entries_.size(); ++i) {
    if (entries_[i].id == id) return &entries_[i];
  }
  return nullptr;
}

WorkspaceEntry* WorkspaceRegistry::find_by_path(const std::string& path) {
  const std::string want = path_canonical(path);
#ifdef _WIN32
  const std::string want_l = to_lower(want);
#else
  const std::string& want_l = want;
#endif
  for (std::size_t i = 0; i < entries_.size(); ++i) {
    const std::string have = path_canonical(entries_[i].path);
#ifdef _WIN32
    if (to_lower(have) == want_l) return &entries_[i];
#else
    if (have == want_l) return &entries_[i];
#endif
  }
  return nullptr;
}

WorkspaceEntry* WorkspaceRegistry::add(const std::string& path_in, const std::string& name,
                                       std::string* err) {
  std::string path = expand_user(trim(path_in));
  if (path.empty()) {
    if (err) *err = "workspace path is empty";
    return nullptr;
  }
  path = path_absolute(path);
  if (!path_exists(path)) {
    if (err) *err = "workspace path does not exist: " + path;
    return nullptr;
  }
  if (!path_is_dir(path)) {
    if (err) *err = "workspace path is not a directory: " + path;
    return nullptr;
  }
  path = path_canonical(path);

  if (WorkspaceEntry* existing = find_by_path(path)) {
    if (!name.empty()) existing->name = name;
    if (existing->id.empty()) existing->id = make_id("ws_");
    if (existing->created_at.empty()) existing->created_at = now_iso8601();
    existing->last_used_at = now_iso8601();
    refresh_flags();
    std::string save_err;
    if (!save(&save_err)) LOG_WARN("workspaces: cannot update the index: ", save_err);
    return existing;
  }

  WorkspaceEntry e;
  e.id = make_id("ws_");
  e.name = name.empty() ? path_filename(path) : name;
  if (e.name.empty()) e.name = path;
  e.path = path;
  e.run_dir = path_join(path, kRunDirName);
  e.created_at = now_iso8601();
  e.last_used_at = e.created_at;
  e.exists = true;
  entries_.push_back(e);
  refresh_flags();
  std::string save_err;
  if (!save(&save_err)) LOG_WARN("workspaces: cannot update the index: ", save_err);
  return &entries_.back();
}

bool WorkspaceRegistry::remove(const std::string& id, std::string* err) {
  for (std::size_t i = 0; i < entries_.size(); ++i) {
    if (entries_[i].id != id) continue;
    entries_.erase(entries_.begin() + static_cast<std::ptrdiff_t>(i));
    return save(err);
  }
  if (err) *err = "workspace not found: " + id;
  return false;
}

// ---------------------------------------------------------------------------
// Workspace
// ---------------------------------------------------------------------------
std::string Workspace::sessions_dir() const { return path_join(run_dir_, "sessions"); }
std::string Workspace::memory_file() const {
  return path_join(path_join(run_dir_, "memory"), kMemoryFileName);
}
std::string Workspace::log_file() const {
  return path_join(path_join(run_dir_, "logs"), "agent.log");
}
std::string Workspace::tmp_dir() const { return path_join(run_dir_, "tmp"); }
std::string Workspace::meta_file() const { return path_join(run_dir_, "meta.json"); }
std::string Workspace::artifacts_dir() const { return path_join(run_dir_, "artifacts"); }

namespace {
std::string session_file_for(const std::string& dir, const std::string& id) {
  return path_join(dir, id + ".json");
}
}  // namespace

bool Workspace::open(const WorkspaceEntry& entry, const Settings& base_settings, std::string* err) {
  // Seeding the run directory is also a read-modify-write ("create it if missing");
  // two threads opening the same workspace at once would otherwise race on it.
  StateLock lock(state_mutex());
  entry_ = entry;
  settings_ = base_settings;
  root_ = path_canonical(entry.path);
  if (root_.empty() || !path_is_dir(root_)) {
    if (err) *err = "workspace directory does not exist: " + entry.path;
    return false;
  }
  entry_.path = root_;
  run_dir_ = path_join(root_, kRunDirName);

  // NOTE: keep these as owned strings - taking c_str() from temporaries here
  // used to leave the array full of dangling pointers.
  std::vector<std::string> dirs;
  dirs.push_back(run_dir_);
  dirs.push_back(sessions_dir());
  dirs.push_back(tmp_dir());
  dirs.push_back(artifacts_dir());
  dirs.push_back(path_parent(log_file()));
  dirs.push_back(path_parent(memory_file()));
  dirs.push_back(scripts_dir());
  for (std::size_t i = 0; i < dirs.size(); ++i) {
    if (dirs[i].empty()) continue;
    std::string reason;
    if (!make_dirs(dirs[i], &reason)) {
      // The OS reason travels with the message: "Access is denied" points at
      // the sandbox or the read-only mount, a bare path does not.
      if (err) *err = "cannot create " + dirs[i] + (reason.empty() ? "" : ": " + reason);
      return false;
    }
  }

  if (!path_exists(meta_file())) {
    nlohmann::json meta = nlohmann::json::object();
    meta["schema"] = 1;
    meta["id"] = entry_.id;
    meta["name"] = entry_.name;
    meta["path"] = root_;
    meta["created_at"] = entry_.created_at.empty() ? now_iso8601() : entry_.created_at;
    write_file_atomic(meta_file(), dump_json(meta, 2) + "\n");
  }
  if (!path_exists(memory_file())) {
    nlohmann::json mem = nlohmann::json::object();
    mem["notes"] = nlohmann::json::array();
    write_file_atomic(memory_file(), dump_json(mem, 2) + "\n");
  }
  // agent.md is the workspace's experience log (see load_agent_doc). Seeded so
  // the file is discoverable - by the model (it is injected into the prompt)
  // and by whoever opens .tea_agent_run/ - instead of appearing only after the
  // first append.
  if (!path_exists(agent_doc_file())) {
    write_file_atomic(agent_doc_file(), kAgentDocTemplate);
  }
  // Per-workspace overrides land on top of the machine-level settings, so a
  // project can pin its own shell/timeouts/system prompt. Loaded once here and
  // kept for the process lifetime to keep the prompt prefix stable.
  if (path_exists(settings_file())) {
    std::string load_err;
    if (!apply_settings_yaml(&settings_, settings_file(), &load_err)) {
      LOG_WARN("workspace ", entry_.id, ": ignoring ", settings_file(), ": ", load_err);
    } else {
      LOG_DEBUG("workspace ", entry_.id, ": applied ", settings_file());
    }
  }
  // The CLI flag is a floor: a workspace settings.yaml may tighten the boundary
  // but not re-open a lift the operator asked for on the command line.
  if (settings_.force_allow_outside_workspace) settings_.allow_outside_workspace = true;

  instructions_.clear();
  if (path_exists(instructions_file())) {
    std::string text;
    if (read_file(instructions_file(), &text, 64u * 1024u)) {
      instructions_ = trim(text);
      if (!instructions_.empty()) {
        LOG_INFO("workspace ", entry_.id, ": loaded ", instructions_file(), " (",
                 instructions_.size(), " bytes)");
      }
    }
  }

  free_bytes_ = free_space_bytes(root_);

  Logger::instance().add_sink(entry_.id, log_file());
  return true;
}

std::string Workspace::settings_file() const {
  return path_join(run_dir_, "settings.yaml");
}

std::string Workspace::instructions_file() const {
  return path_join(run_dir_, "instructions.md");
}

std::string Workspace::system_prompt_file() const {
  return path_join(run_dir_, "system_prompt.md");
}

std::string Workspace::agent_doc_file() const {
  return path_join(run_dir_, "agent.md");
}

std::size_t Workspace::system_prompt_max_bytes() const {
  return kSystemPromptMaxBytes;
}

bool Workspace::write_setting_bool(const std::string& key, bool value, std::string* err) {
  StateLock lock(state_mutex());
  const std::string path = settings_file();
  const std::string literal = std::string(key) + ": " + (value ? "true" : "false");
  const std::string indent_unit = "  ";

  std::string text;
  if (path_exists(path)) {
    if (!read_file(path, &text, 1024u * 1024u)) {
      if (err) *err = "cannot read " + path;
      return false;
    }
  }

  // Refuse to edit something we cannot understand: a hand-written settings.yaml
  // with a syntax error should keep failing loudly (the workspace already logs a
  // WARN for it) instead of being quietly replaced by a file we generated.
  bool has_settings_block = false;
  if (!trim(text).empty()) {
    try {
      const YAML::Node root = YAML::Load(text);
      if (!root || !root.IsMap()) {
        if (err) *err = path + " is not a YAML mapping";
        return false;
      }
      const YAML::Node block = root["settings"];
      if (block) {
        if (!block.IsMap()) {
          if (err) *err = path + ": 'settings' is not a mapping";
          return false;
        }
        has_settings_block = true;
      }
    } catch (const std::exception& e) {
      if (err) *err = std::string("cannot parse ") + path + ": " + e.what();
      return false;
    }
  }

  const bool crlf = text.find("\r\n") != std::string::npos;
  const std::string nl = crlf ? "\r\n" : "\n";
  std::vector<std::string> lines;
  {
    std::string cur;
    for (std::size_t i = 0; i < text.size(); ++i) {
      if (text[i] == '\n') {
        if (!cur.empty() && cur.back() == '\r') cur.pop_back();
        lines.push_back(cur);
        cur.clear();
      } else {
        cur.push_back(text[i]);
      }
    }
    if (!cur.empty()) lines.push_back(cur);
  }

  // Indentation of a line, tabs counted as one column (every settings.yaml in the
  // wild uses spaces, and a mixed file is not worth being clever about).
  // Returns npos for blank and comment-only lines, which take no part in the
  // structure but must be kept.
  const auto content_indent = [](const std::string& line) -> std::size_t {
    std::size_t k = 0;
    while (k < line.size() && (line[k] == ' ' || line[k] == '\t')) ++k;
    if (k >= line.size() || line[k] == '#') return std::string::npos;
    return k;
  };
  const auto trimmed = [](const std::string& line) -> std::string {
    std::size_t a = 0;
    while (a < line.size() && (line[a] == ' ' || line[a] == '\t')) ++a;
    std::size_t b = line.size();
    while (b > a && (line[b - 1] == ' ' || line[b - 1] == '\r' || line[b - 1] == '\t')) --b;
    return line.substr(a, b - a);
  };

  bool written = false;

  if (has_settings_block) {
    // Locate the top-level `settings:` line, then walk its direct children.
    std::size_t start = lines.size();
    std::size_t child_indent = std::string::npos;
    for (std::size_t i = 0; i < lines.size(); ++i) {
      if (content_indent(lines[i]) != 0) continue;
      if (trimmed(lines[i]) == "settings:") {
        start = i;
        break;
      }
    }
    if (start == lines.size()) {
      // Parsed as a block but the key is spelled differently (flow style, or
      // `settings :`); refuse rather than guess where to put the line.
      if (err) *err = path + ": cannot locate the 'settings:' line to edit";
      return false;
    }
    std::size_t end = lines.size();
    for (std::size_t i = start + 1; i < lines.size(); ++i) {
      const std::size_t ind = content_indent(lines[i]);
      if (ind == std::string::npos) continue;                 // blank / comment
      if (ind == 0) { end = i; break; }                       // next top-level key
      if (child_indent == std::string::npos) child_indent = ind;
    }
    if (child_indent == std::string::npos) child_indent = indent_unit.size();

    std::size_t replace_at = lines.size();
    for (std::size_t i = start + 1; i < end; ++i) {
      const std::size_t ind = content_indent(lines[i]);
      if (ind != child_indent) continue;                      // nested deeper
      const std::string t = trimmed(lines[i]);
      if (t.compare(0, key.size(), key) != 0) continue;
      if (t.size() <= key.size() || t[key.size()] != ':') continue;
      replace_at = i;
      break;
    }

    const std::string body = std::string(child_indent, ' ') + literal;
    if (replace_at != lines.size()) {
      lines[replace_at] = body;
    } else {
      // Insert after the last non-blank line of the block so a trailing comment
      // stays attached to whatever it was commenting on.
      std::size_t insert_at = end;
      while (insert_at > start + 1 && content_indent(lines[insert_at - 1]) == std::string::npos) {
        --insert_at;
      }
      lines.insert(lines.begin() + static_cast<std::ptrdiff_t>(insert_at), body);
    }
    written = true;
  } else if (trim(text).empty()) {
    lines.clear();
    lines.push_back("# Per-workspace overrides; see README「按工作区覆盖设置」.");
    lines.push_back("settings:");
    lines.push_back(indent_unit + literal);
    written = true;
  } else {
    // A flat settings file: apply_settings_yaml accepts `key: value` at the top
    // level too, so edit it in place rather than reshaping the file into a
    // `settings:` block the operator never wrote.
    std::size_t replace_at = lines.size();
    for (std::size_t i = 0; i < lines.size(); ++i) {
      if (content_indent(lines[i]) != 0) continue;
      const std::string t = trimmed(lines[i]);
      if (t.compare(0, key.size(), key) != 0) continue;
      if (t.size() <= key.size() || t[key.size()] != ':') continue;
      replace_at = i;
      break;
    }
    if (replace_at != lines.size()) {
      lines[replace_at] = literal;
    } else {
      std::size_t insert_at = lines.size();
      while (insert_at > 0 && content_indent(lines[insert_at - 1]) == std::string::npos) {
        --insert_at;
      }
      lines.insert(lines.begin() + static_cast<std::ptrdiff_t>(insert_at), literal);
    }
    written = true;
  }

  if (!written) {
    if (err) *err = "could not locate a place for '" + std::string(key) + "' in " + path;
    return false;
  }

  std::string out;
  for (std::size_t i = 0; i < lines.size(); ++i) {
    out += lines[i];
    out += nl;
  }
  if (!write_file_atomic(path, out, err)) return false;

  // Reflect the change in this object so the caller does not have to reopen the
  // workspace to see it. Everything the file does not mention keeps whatever the
  // CLI / machine-level settings gave it.
  std::string apply_err;
  if (!apply_settings_yaml(&settings_, path, &apply_err)) {
    if (err) *err = apply_err;
    return false;
  }
  if (settings_.force_allow_outside_workspace) settings_.allow_outside_workspace = true;
  LOG_INFO("workspace ", entry_.id, ": ", key, " = ", value ? "true" : "false", " written to ",
           display_path(path));
  return true;
}

std::string Workspace::load_system_prompt(bool* custom) const {
  if (custom) *custom = false;
  if (!path_exists(system_prompt_file())) return std::string();
  std::string text;
  if (!read_file(system_prompt_file(), &text, kSystemPromptMaxBytes)) return std::string();
  if (custom) *custom = !trim(text).empty();
  return text;
}

bool Workspace::save_system_prompt(const std::string& content, std::string* err) const {
  StateLock lock(state_mutex());
  if (content.size() > kSystemPromptMaxBytes) {
    if (err) {
      *err = "system_prompt.md would be " + std::to_string(content.size()) + " bytes; the limit is " +
             std::to_string(kSystemPromptMaxBytes) + ". Shorten it and write it again.";
    }
    return false;
  }
  // An empty file is the documented "use the default" state, so whitespace-only
  // input is normalised to an empty file rather than a file full of blanks.
  std::string body = trim(content);
  if (!body.empty()) body += "\n";
  return write_file_atomic(system_prompt_file(), body, err);
}

std::string Workspace::scripts_dir() const {
  return path_join(run_dir_, "scripts");
}

std::string Workspace::load_agent_doc(bool* truncated) const {
  if (truncated) *truncated = false;
  std::string text;
  bool cut = false;
  if (!read_file(agent_doc_file(), &text, kAgentDocMaxBytes, &cut)) return std::string();
  if (truncated) *truncated = cut;
  return text;
}

bool Workspace::save_agent_doc(const std::string& content, std::string* err) const {
  StateLock lock(state_mutex());
  if (content.size() > kAgentDocMaxBytes) {
    if (err) {
      *err = "agent.md would be " + std::to_string(content.size()) + " bytes; the limit is " +
             std::to_string(kAgentDocMaxBytes) + ". Compact it (merge or drop stale entries) " +
             "and write it again.";
    }
    return false;
  }
  // Normalise the tail: exactly one trailing newline, and a whitespace-only
  // document becomes an empty file instead of a lone blank line.
  std::string body = content;
  while (!body.empty() && (body.back() == '\n' || body.back() == '\r' || body.back() == ' ' ||
                           body.back() == '\t')) {
    body.pop_back();
  }
  if (!body.empty()) body += "\n";
  return write_file_atomic(agent_doc_file(), body, err);
}

bool Workspace::append_agent_doc(const std::string& text, std::string* err) const {
  // Read-modify-write on a file the model appends to from several turns at once.
  StateLock lock(state_mutex());
  const std::string body = trim(text);
  if (body.empty()) {
    if (err) *err = "text is empty";
    return false;
  }
  std::string current;
  bool existing = path_exists(agent_doc_file());
  if (existing) {
    bool cut = false;
    if (!read_file(agent_doc_file(), &current, 0, &cut)) {
      if (err) *err = "cannot read " + agent_doc_file();
      return false;
    }
  } else {
    current = std::string(kAgentDocTemplate);
  }
  if (!current.empty() && current.back() != '\n') current += "\n";
  // A dated bullet, not a heading: entries stay one glance apart and the file
  // does not grow a new section for every single observation.
  current += "- " + now_iso8601() + " " + replace_all(body, "\n", "\n  ") + "\n";
  if (current.size() > kAgentDocMaxBytes) {
    if (err) {
      *err = "agent.md is at its " + std::to_string(kAgentDocMaxBytes) + " byte limit; read it, " +
             "merge or drop stale entries, and rewrite it with toolkit_agent_doc op=replace";
    }
    return false;
  }
  return write_file_atomic(agent_doc_file(), current, err);
}

void Workspace::set_allowed_roots(const std::vector<std::string>& roots) {
  allowed_roots_.clear();
  for (std::size_t i = 0; i < roots.size(); ++i) {
    const std::string trimmed = trim(roots[i]);
    if (trimmed.empty()) continue;
    allowed_roots_.push_back(path_absolute(expand_user(trimmed)));
  }
}

std::string Workspace::resolve_path(const std::string& path_in, std::string* err) const {
  std::string p = trim(path_in);
  if (p.size() >= 2 && ((p.front() == '"' && p.back() == '"') || (p.front() == '\'' && p.back() == '\''))) {
    p = p.substr(1, p.size() - 2);
  }
  p = expand_user(p);
  if (p.empty()) {
    if (err) *err = "path is empty";
    return std::string();
  }
  const std::string full =
      path_is_absolute(p) ? path_absolute(p) : path_absolute(path_join(root_, p));
  if (!settings_.allow_outside_workspace && !path_is_within(full, root_)) {
    // A turn may be granted extra directories (the task queue does it per
    // task). They count as inside the boundary.
    for (std::size_t i = 0; i < allowed_roots_.size(); ++i) {
      if (path_is_within(full, allowed_roots_[i])) return full;
    }
    if (err) {
      std::string extra;
      for (std::size_t i = 0; i < allowed_roots_.size(); ++i) {
        extra += (extra.empty() ? "" : ", ");
        extra += allowed_roots_[i];
      }
      *err = "path escapes the workspace: '" + p + "' (workspace root: " + root_ +
             (extra.empty() ? "" : ("; allowed in this turn: " + extra)) + ")";
    }
    return std::string();
  }
  return full;
}

std::string Workspace::display_path(const std::string& absolute) const {
  if (path_is_within(absolute, root_)) {
    std::string rel = absolute.substr(root_.size());
    while (!rel.empty() && (rel.front() == '/' || rel.front() == '\\')) rel.erase(0, 1);
    if (rel.empty()) return ".";
    std::string out;
    for (std::size_t i = 0; i < rel.size(); ++i) out += (rel[i] == '\\') ? '/' : rel[i];
    return out;
  }
  return absolute;
}

std::vector<SessionInfo> Workspace::list_sessions() const {
  std::vector<SessionInfo> out;
  const std::string dir = sessions_dir();
  const std::string index = path_join(dir, "index.json");
  bool from_index = false;
  if (path_exists(index)) {
    bool ok = false;
    nlohmann::json j = read_json_file(index, &ok);
    if (ok && j.is_array()) {
      for (auto it = j.begin(); it != j.end(); ++it) {
        SessionInfo s = SessionInfo::from_json(*it);
        if (s.id.empty()) continue;
        if (!path_exists(session_file_for(dir, s.id))) continue;
        out.push_back(s);
      }
      from_index = true;
    }
  }
  if (!from_index) {
    out.clear();
    std::vector<DirEntryInfo> files;
    std::string err;
    if (list_directory(dir, &files, &err)) {
      for (std::size_t i = 0; i < files.size(); ++i) {
        if (files[i].is_dir) continue;
        if (files[i].name == "index.json") continue;
        if (path_extension(files[i].name) != ".json") continue;
        bool ok = false;
        nlohmann::json j = read_json_file(files[i].path, &ok);
        if (!ok || !j.is_object()) continue;
        SessionInfo s;
        s.id = path_stem(files[i].name);
        auto title_it = j.find("title");
        if (title_it != j.end() && title_it->is_string()) s.title = title_it->get<std::string>();
        auto created_it = j.find("created_at");
        if (created_it != j.end() && created_it->is_string())
          s.created_at = created_it->get<std::string>();
        auto updated_it = j.find("updated_at");
        if (updated_it != j.end() && updated_it->is_string())
          s.updated_at = updated_it->get<std::string>();
        auto msgs = j.find("messages");
        if (msgs != j.end() && msgs->is_array()) s.message_count = static_cast<int>(msgs->size());
        auto from_it = j.find("forked_from");
        if (from_it != j.end() && from_it->is_string()) {
          s.forked_from = from_it->get<std::string>();
        }
        auto turn_it = j.find("fork_turn");
        if (turn_it != j.end() && turn_it->is_number()) s.fork_turn = turn_it->get<int>();
        if (s.title.empty()) s.title = "会话 " + s.id;
        if (s.updated_at.empty()) s.updated_at = files[i].mtime;
        if (s.created_at.empty()) s.created_at = s.updated_at;
        out.push_back(s);
      }
    }
  }
  std::sort(out.begin(), out.end(), [](const SessionInfo& a, const SessionInfo& b) {
    return a.updated_at > b.updated_at;
  });
  return out;
}

bool Workspace::load_session(const std::string& id, Session* out, std::string* err) const {
  const std::string path = session_file_for(sessions_dir(), id);
  bool ok = false;
  nlohmann::json j = read_json_file(path, &ok);
  if (!ok) {
    if (err) *err = "session not found: " + id;
    return false;
  }
  out->info = SessionInfo();
  out->messages.clear();
  out->summary.clear();
  out->summary_upto = 0;
  out->todos.clear();
  out->todo_carry.clear();
  out->artifacts.clear();
  out->info.id = id;
  auto title_it = j.find("title");
  if (title_it != j.end() && title_it->is_string()) out->info.title = title_it->get<std::string>();
  auto created_it = j.find("created_at");
  if (created_it != j.end() && created_it->is_string())
    out->info.created_at = created_it->get<std::string>();
  auto updated_it = j.find("updated_at");
  if (updated_it != j.end() && updated_it->is_string())
    out->info.updated_at = updated_it->get<std::string>();
  auto from_it = j.find("forked_from");
  if (from_it != j.end() && from_it->is_string()) {
    out->info.forked_from = from_it->get<std::string>();
  }
  auto turn_it = j.find("fork_turn");
  if (turn_it != j.end() && turn_it->is_number()) out->info.fork_turn = turn_it->get<int>();
  auto summary_it = j.find("summary");
  if (summary_it != j.end() && summary_it->is_string()) {
    out->summary = summary_it->get<std::string>();
  }
  auto upto_it = j.find("summary_upto");
  if (upto_it != j.end() && upto_it->is_number_integer()) {
    out->summary_upto = upto_it->get<std::size_t>();
  }
  if (out->summary_upto > 0 && out->summary.empty()) out->summary_upto = 0;
  auto artifacts_it = j.find("artifacts");
  if (artifacts_it != j.end() && artifacts_it->is_array()) {
    for (auto it = artifacts_it->begin(); it != artifacts_it->end(); ++it) {
      SessionArtifact a = SessionArtifact::from_json(*it);
      if (a.path.empty()) continue;
      out->artifacts.push_back(a);
    }
  }
  auto carry_it = j.find("todo_carry");
  if (carry_it != j.end() && carry_it->is_array()) {
    for (auto it = carry_it->begin(); it != carry_it->end(); ++it) {
      TodoItem item = TodoItem::from_json(*it);
      if (trim(item.text).empty()) continue;
      out->todo_carry.push_back(item);
    }
  }
  auto todos_it = j.find("todos");
  if (todos_it != j.end() && todos_it->is_array()) {
    for (auto it = todos_it->begin(); it != todos_it->end(); ++it) {
      TodoItem item = TodoItem::from_json(*it);
      if (trim(item.text).empty()) continue;
      out->todos.push_back(item);
    }
  }
  auto msgs = j.find("messages");
  if (msgs != j.end() && msgs->is_array()) {
    for (auto it = msgs->begin(); it != msgs->end(); ++it) {
      out->messages.push_back(Message::from_store(*it));
    }
  }
  out->info.message_count = static_cast<int>(out->messages.size());
  return true;
}

bool Workspace::save_session(const Session& session, std::string* err) const {
  // The transcript write plus the index.json read-modify-write below have to be
  // one critical section: two turns finishing at once would otherwise each build
  // the index from their own snapshot and drop the other's entry.
  StateLock lock(state_mutex());
  nlohmann::json j = nlohmann::json::object();
  j["id"] = session.info.id;
  j["title"] = session.info.title;
  j["created_at"] = session.info.created_at;
  j["updated_at"] = session.info.updated_at;
  if (!session.info.forked_from.empty()) j["forked_from"] = session.info.forked_from;
  if (session.info.fork_turn > 0) j["fork_turn"] = session.info.fork_turn;
  nlohmann::json msgs = nlohmann::json::array();
  for (std::size_t i = 0; i < session.messages.size(); ++i) {
    msgs.push_back(session.messages[i].to_store());
  }
  j["messages"] = msgs;
  if (!session.summary.empty()) j["summary"] = session.summary;
  if (session.summary_upto > 0) j["summary_upto"] = session.summary_upto;
  // The steps the last turn left open: the next turn takes them back as its
  // plan and continues where the last one stopped.
  if (!session.todo_carry.empty()) {
    nlohmann::json carry = nlohmann::json::array();
    for (std::size_t i = 0; i < session.todo_carry.size(); ++i) {
      carry.push_back(session.todo_carry[i].to_json());
    }
    j["todo_carry"] = carry;
  }
  if (!session.artifacts.empty()) {
    nlohmann::json arts = nlohmann::json::array();
    for (std::size_t i = 0; i < session.artifacts.size(); ++i) {
      arts.push_back(session.artifacts[i].to_json());
    }
    j["artifacts"] = arts;
  }
  // The task list belongs to whoever changed it (save_todos), not to the
  // session save: a turn writes its own copy of the session back at the end,
  // and that copy can be older than the list on disk. So the list is taken from
  // the file, not from this copy. A brand new session has no list yet - there
  // the copy is the only one there is.
  const std::string dir = sessions_dir();
  const std::string session_path = session_file_for(dir, session.info.id);
  const bool fresh_session = !path_exists(session_path);
  if (fresh_session) {
    if (!session.todos.empty()) {
      nlohmann::json todos = nlohmann::json::array();
      for (std::size_t i = 0; i < session.todos.size(); ++i) {
        todos.push_back(session.todos[i].to_json());
      }
      j["todos"] = todos;
    }
  } else {
    bool existing_ok = false;
    const nlohmann::json existing = read_json_file(session_path, &existing_ok);
    if (existing_ok && existing.is_object()) {
      auto todos_it = existing.find("todos");
      if (todos_it != existing.end()) j["todos"] = *todos_it;
    }
  }
  if (!write_file_atomic(session_path, dump_json(j, 2) + "\n", err)) {
    return false;
  }

  // Keep the lightweight index in sync. Progress is part of it: the session
  // list shows "3/7" without opening every session file.
  SessionInfo info = session.info;
  info.message_count = static_cast<int>(session.messages.size());
  if (fresh_session) {
    todo_counts(session.todos, &info.todo_total, &info.todo_done);
  }
  const std::string index_path = path_join(dir, "index.json");
  bool ok = false;
  nlohmann::json index = read_json_file(index_path, &ok);
  nlohmann::json list = nlohmann::json::array();
  if (ok && index.is_array()) list = index;
  bool replaced = false;
  for (auto it = list.begin(); it != list.end(); ++it) {
    if (it->is_object()) {
      auto id_it = it->find("id");
      if (id_it != it->end() && id_it->is_string() && id_it->get<std::string>() == info.id) {
        // The progress in the index belongs to the task list, and the task list
        // is written elsewhere (save_todos). Carry the counts over instead of
        // resetting them to zero.
        nlohmann::json entry = info.to_json();
        if (!fresh_session) {
          auto total_it = it->find("todo_total");
          auto done_it = it->find("todo_done");
          if (total_it != it->end() && total_it->is_number()) entry["todo_total"] = *total_it;
          if (done_it != it->end() && done_it->is_number()) entry["todo_done"] = *done_it;
        }
        *it = entry;
        replaced = true;
        break;
      }
    }
  }
  if (!replaced) list.push_back(info.to_json());
  return write_file_atomic(index_path, dump_json(list, 2) + "\n", err);
}

bool Workspace::load_todos(const std::string& session_id, std::vector<TodoItem>* out,
                           std::string* err) const {
  StateLock lock(state_mutex());
  out->clear();
  const std::string path = session_file_for(sessions_dir(), session_id);
  if (!path_exists(path)) {
    if (err) *err = "session not found: " + session_id;
    return false;
  }
  bool ok = false;
  const nlohmann::json j = read_json_file(path, &ok);
  if (!ok || !j.is_object()) {
    if (err) *err = "cannot read the session file: " + path;
    return false;
  }
  auto todos_it = j.find("todos");
  if (todos_it != j.end() && todos_it->is_array()) {
    for (auto it = todos_it->begin(); it != todos_it->end(); ++it) {
      TodoItem item = TodoItem::from_json(*it);
      if (trim(item.text).empty()) continue;
      out->push_back(item);
    }
  }
  return true;
}

bool Workspace::save_todos(const std::string& session_id, const std::vector<TodoItem>& todos,
                            std::string* err) const {
  StateLock lock(state_mutex());
  const std::string dir = sessions_dir();
  const std::string path = session_file_for(dir, session_id);
  if (!path_exists(path)) {
    if (err) *err = "session not found: " + session_id;
    return false;
  }
  bool ok = false;
  nlohmann::json j = read_json_file(path, &ok);
  if (!ok || !j.is_object()) {
    if (err) *err = "cannot read the session file: " + path;
    return false;
  }
  nlohmann::json items = nlohmann::json::array();
  for (std::size_t i = 0; i < todos.size(); ++i) {
    items.push_back(todos[i].to_json());
  }
  j["todos"] = items;
  if (!write_file_atomic(path, dump_json(j, 2) + "\n", err)) {
    return false;
  }

  // The index carries the progress ("3/7" in the session list).
  int total = 0, done = 0;
  todo_counts(todos, &total, &done);
  const std::string index_path = path_join(dir, "index.json");
  bool index_ok = false;
  nlohmann::json index = read_json_file(index_path, &index_ok);
  nlohmann::json list = nlohmann::json::array();
  if (index_ok && index.is_array()) list = index;
  bool replaced = false;
  for (auto it = list.begin(); it != list.end(); ++it) {
    if (!it->is_object()) continue;
    auto id_it = it->find("id");
    if (id_it != it->end() && id_it->is_string() && id_it->get<std::string>() == session_id) {
      (*it)["todo_total"] = total;
      (*it)["todo_done"] = done;
      replaced = true;
      break;
    }
  }
  if (!replaced) return true;   // not in the index (a foreign file): leave it alone
  return write_file_atomic(index_path, dump_json(list, 2) + "\n", err);
}

bool Workspace::create_session(const std::string& title, Session* out, std::string* err) const {
  StateLock lock(state_mutex());
  Session s;
  s.info.id = make_id("s_");
  s.info.title = title.empty() ? "新会话" : title;
  s.info.created_at = now_iso8601();
  s.info.updated_at = s.info.created_at;
  s.info.message_count = 0;
  if (!save_session(s, err)) return false;
  *out = s;
  return true;
}

bool Workspace::delete_session(const std::string& id, std::string* err) const {
  StateLock lock(state_mutex());
  const std::string dir = sessions_dir();
  const std::string path = session_file_for(dir, id);
  if (!path_exists(path)) {
    if (err) *err = "session not found: " + id;
    return false;
  }
  remove_path(path, false, nullptr);
  const std::string index_path = path_join(dir, "index.json");
  bool ok = false;
  nlohmann::json index = read_json_file(index_path, &ok);
  if (ok && index.is_array()) {
    nlohmann::json list = nlohmann::json::array();
    for (auto it = index.begin(); it != index.end(); ++it) {
      if (!it->is_object()) continue;
      auto id_it = it->find("id");
      if (id_it != it->end() && id_it->is_string() && id_it->get<std::string>() == id) continue;
      list.push_back(*it);
    }
    write_file_atomic(index_path, dump_json(list, 2) + "\n");
  }
  return true;
}

bool Workspace::rename_session(const std::string& id, const std::string& title,
                               std::string* err) const {
  StateLock lock(state_mutex());
  Session s;
  if (!load_session(id, &s, err)) return false;
  s.info.title = title;
  s.info.updated_at = now_iso8601();
  return save_session(s, err);
}

bool Workspace::fork_session(const std::string& id, long long message_index,
                             const std::string& title, Session* out, std::string* err) const {
  // Snapshot the parent and write the child as one step: a turn appending to the
  // parent meanwhile would otherwise be half-copied or lost from the index.
  StateLock lock(state_mutex());
  Session parent;
  if (!load_session(id, &parent, err)) return false;
  if (parent.messages.empty()) {
    if (err) *err = "session has no messages to fork: " + id;
    return false;
  }

  // A fork point is a *turn*, not a bare message: everything through the
  // assistant (and tool) messages that answer the selected user message is
  // copied, so the child starts with a complete exchange instead of an
  // assistant turn whose tool calls have no results.
  std::size_t end = parent.messages.size();
  if (message_index >= 0) {
    // Compare as unsigned before narrowing: a huge index must not wrap into a
    // valid slot on a 32-bit build.
    const unsigned long long at_wide = static_cast<unsigned long long>(message_index);
    if (at_wide >= parent.messages.size()) {
      if (err) {
        *err = "message_index " + std::to_string(message_index) +
               " is past the end of the session (" +
               std::to_string(parent.messages.size()) + " messages)";
      }
      return false;
    }
    const std::size_t at = static_cast<std::size_t>(at_wide);
    if (parent.messages[at].role != "user") {
      if (err) {
        *err = "message " + std::to_string(message_index) + " is not a user message (role: " +
               parent.messages[at].role + ")";
      }
      return false;
    }
    end = parent.messages.size();
    for (std::size_t i = at + 1; i < parent.messages.size(); ++i) {
      if (parent.messages[i].role == "user") {
        end = i;
        break;
      }
    }
  }

  int turn = 0;
  for (std::size_t i = 0; i < end; ++i) {
    if (parent.messages[i].role == "user") ++turn;
  }

  Session child;
  child.info.id = make_id("s_");
  child.info.created_at = now_iso8601();
  child.info.updated_at = child.info.created_at;
  child.info.forked_from = id;
  child.info.fork_turn = turn;
  std::string chosen = trim(title);
  if (chosen.empty()) {
    std::string parent_title = parent.info.title.empty() ? std::string("会话") : parent.info.title;
    bool shortened = false;
    parent_title = truncate_utf8(parent_title, 48, &shortened);
    if (shortened) parent_title += "…";
    chosen = "分叉 " + std::to_string(turn) + " · " + parent_title;
  }
  child.info.title = derive_title(chosen);
  child.messages.assign(parent.messages.begin(), parent.messages.begin() + end);
  // A rolling summary that reaches past the fork describes messages the child
  // does not have, so it is dropped rather than replayed as a partial truth.
  if (parent.summary_upto > 0 && parent.summary_upto <= end) {
    child.summary = parent.summary;
    child.summary_upto = parent.summary_upto;
  }
  child.info.message_count = static_cast<int>(child.messages.size());
  if (!save_session(child, err)) return false;
  if (out) *out = child;
  return true;
}

bool Workspace::record_artifact(Session* session, const std::string& relative_path,
                                const std::string& tool, int turn) const {
  if (!session) return false;
  std::string rel = trim(relative_path);
  if (rel.empty()) return false;
  // Accept an absolute path inside the workspace too (tools report in both forms).
  if (!rel.empty() && (rel[0] == '/' || rel.find(':') == 1)) rel = display_path(rel);
  while (!rel.empty() && rel[0] == '.') {
    if (starts_with(rel, "./")) rel = rel.substr(2);
    else break;
  }
  // Internal state is not an output; listing sessions and trajectories would
  // bury the one report the user actually asked for.
  if (starts_with(rel, ".tea_agent_run/")) return false;
  if (artifact_is_build_noise(rel)) return false;

  const std::string absolute = resolve_path(rel, nullptr);
  if (absolute.empty() || !path_is_file(absolute)) return false;
  const uint64_t size = file_size(absolute);
  if (size == 0) return false;

  for (std::size_t i = 0; i < session->artifacts.size(); ++i) {
    SessionArtifact& a = session->artifacts[i];
    if (a.path != rel) continue;
    a.bytes = static_cast<long long>(size);
    a.created_at = now_iso8601();
    if (!tool.empty()) a.tool = tool;
    if (turn > 0) a.turn = turn;
    if (a.kind.empty()) a.kind = artifact_kind_for(rel);
    return true;
  }
  SessionArtifact a;
  a.path = rel;
  a.bytes = static_cast<long long>(size);
  a.kind = artifact_kind_for(rel);
  a.tool = tool;
  a.created_at = now_iso8601();
  a.turn = turn;
  session->artifacts.push_back(a);
  return true;
}

std::vector<std::string> Workspace::recent_artifacts(long long since_ms,
                                                    bool* truncated) const {
  if (truncated) *truncated = false;
  // A command cannot report what it wrote, so after one runs we look for files
  // that appeared or changed. The walk is bounded and filters by extension, so
  // a build (thousands of .o/.a/.d) produces nothing here.
  constexpr int kMaxDepth = 8;
  constexpr std::size_t kMaxVisited = 20000;
  // A command can produce far more than a handful of files (a render, an
  // export, a test run). 20 was low enough to silently hide results, so the
  // cap is higher; when it bites, the caller is told (see `truncated`).
  constexpr std::size_t kMaxResults = 200;
  static const char* const kSkipDirs[] = {
      ".git",      ".tea_agent_run", "node_modules", ".venv",        "venv",
      "__pycache__", "target",       "dist",         ".cache",       ".idea",
      ".vscode",   "build",          "build-arm64",  "build-win64",  "build-mingw",
      "build-msvc", "cmake-build-debug", "obj",      ".gradle",      "3rd",
  };

  std::vector<std::string> out;
  std::size_t visited = 0;
  // DirEntryInfo carries an ISO-8601 string; those sort lexicographically, so
  // converting the cut-off once is cheaper than parsing every mtime.
  const std::string since_iso = iso8601_from_unix(since_ms / 1000);

  struct Frame {
    std::string dir;
    int depth;
  };
  std::vector<Frame> stack;
  stack.push_back({root_, 0});
  while (!stack.empty() && visited < kMaxVisited && out.size() < kMaxResults) {
    const Frame frame = stack.back();
    stack.pop_back();
    std::vector<DirEntryInfo> entries;
    if (!list_directory(frame.dir, &entries, nullptr)) continue;
    for (std::size_t i = 0; i < entries.size() && visited < kMaxVisited; ++i) {
      ++visited;
      const DirEntryInfo& e = entries[i];
      if (e.is_dir) {
        if (frame.depth + 1 > kMaxDepth) continue;
        bool skip = false;
        for (std::size_t k = 0; k < sizeof(kSkipDirs) / sizeof(kSkipDirs[0]); ++k) {
          if (e.name == kSkipDirs[k]) {
            skip = true;
            break;
          }
        }
        if (!skip) stack.push_back({e.path, frame.depth + 1});
        continue;
      }
      if (artifact_is_build_noise(e.name)) continue;
      if (!e.mtime.empty() && e.mtime < since_iso) continue;
      out.push_back(display_path(e.path));
      if (out.size() >= kMaxResults) {
        // More may be hiding: the caller must say "and more" rather than
        // presenting a truncated list as the whole truth.
        if (truncated) *truncated = true;
        break;
      }
    }
  }
  return out;
}

// workspace search
// ---------------------------------------------------------------------------
namespace {

/// Directories the search never descends into. They hold thousands of files
/// that nobody is looking for (dependencies, build output, VCS data) and their
/// content is generated rather than written by hand.
bool search_skip_dir(const std::string& name) {
  static const char* const kSkip[] = {
      ".git",       ".hg",      ".svn",         "node_modules", "__pycache__",
      ".venv",      "venv",     ".cache",       ".idea",        ".vscode",
      ".gradle",    ".tox",     ".mypy_cache",  ".pytest_cache", ".tea_agent_run",
  };
  for (std::size_t i = 0; i < sizeof(kSkip) / sizeof(kSkip[0]); ++i) {
    if (name == kSkip[i]) return true;
  }
  return false;
}

/// Extensions whose files are binary by construction. Checking the name first
/// saves reading a 40 MB video into memory just to find a NUL byte in it.
bool search_binary_ext(const std::string& name) {
  static const char* const kBin[] = {
      ".png", ".jpg",  ".jpeg", ".gif",  ".webp", ".bmp",  ".ico",  ".tif",  ".tiff",
      ".pdf", ".zip",  ".gz",   ".bz2",  ".xz",   ".7z",   ".rar",  ".tar",  ".tgz",
      ".exe", ".dll",  ".so",   ".dylib", ".o",   ".obj",  ".a",    ".lib",  ".class",
      ".jar", ".pyc",  ".pyo",  ".wasm", ".mp3",  ".mp4",  ".wav",  ".avi",  ".mkv",
      ".mov", ".woff", ".woff2", ".ttf", ".otf", ".eot",  ".db",   ".sqlite", ".bin",
      ".dat", ".pack", ".idx",  ".iso",  ".dmg",  ".deb",  ".rpm",
  };
  const std::string ext = path_extension(name);
  for (std::size_t i = 0; i < sizeof(kBin) / sizeof(kBin[0]); ++i) {
    if (ext == kBin[i]) return true;
  }
  return false;
}

/// ASCII case folding. The query is compared byte for byte otherwise: folding
/// non-ASCII would need a locale, and a wrong fold is worse than no fold at
/// all (it would report a hit that is not there).
std::string search_fold(const std::string& s) {
  std::string out = s;
  for (std::size_t i = 0; i < out.size(); ++i) {
    const char c = out[i];
    if (c >= 'A' && c <= 'Z') out[i] = static_cast<char>(c - 'A' + 'a');
  }
  return out;
}

/// "the query appears in the haystack" - a plain substring test, no regex.
bool search_contains(const std::string& haystack, const std::string& needle,
                     bool case_sensitive) {
  if (needle.empty()) return false;
  if (case_sensitive) return haystack.find(needle) != std::string::npos;
  return search_fold(haystack).find(search_fold(needle)) != std::string::npos;
}

/// "the query is the whole string" - the 完全匹配 mode.
bool search_equals(const std::string& value, const std::string& needle, bool case_sensitive) {
  if (needle.empty()) return false;
  if (case_sensitive) return value == needle;
  return search_fold(value) == search_fold(needle);
}

/// A NUL byte in the first chunk means "this is not text". Anything else is
/// treated as text: a file with a stray control character is still worth
/// searching.
bool search_looks_binary(const std::string& text) {
  const std::size_t limit = std::min<std::size_t>(text.size(), 8192);
  return text.substr(0, limit).find('\0') != std::string::npos;
}

/// Cut a matching line down to something that fits on a screen.
std::string search_preview(const std::string& line) {
  std::string s = trim(line);
  const std::size_t kMax = 200;
  if (s.size() > kMax) {
    s = s.substr(0, kMax);
    s += "…";
  }
  return s;
}

}  // namespace

nlohmann::json SearchHit::to_json() const {
  nlohmann::json j = nlohmann::json::object();
  j["path"] = path;
  j["name"] = name;
  j["type"] = is_dir ? "dir" : "file";
  j["size"] = size;
  j["mtime"] = mtime;
  j["hits"] = hits;
  j["name_hit"] = name_hit;
  if (!lines.empty()) j["lines"] = lines;
  if (!samples.empty()) j["samples"] = samples;
  if (!preview.empty()) j["preview"] = preview;
  return j;
}

SearchResults Workspace::search(const SearchQuery& query) const {
  SearchResults out;
  const std::string needle = trim(query.text);
  if (needle.empty()) return out;
  if (!query.names && !query.content) return out;

  // Where the walk starts: the workspace root, or a subdirectory of it. A
  // subdirectory that leaves the workspace is refused - the search never
  // reaches outside the root.
  std::string start = root_;
  if (!query.subpath.empty()) {
    std::string err;
    const std::string sub = resolve_path(query.subpath, &err);
    if (sub.empty() || !path_is_dir(sub) || !path_is_within(sub, root_)) return out;
    start = sub;
  }

  constexpr int kMaxDepth = 12;
  constexpr std::size_t kMaxVisited = 10000;
  constexpr std::size_t kMaxHitsPerFile = 20;
  constexpr std::size_t kMaxSamplesPerFile = 5;
  // Total text this one search is allowed to read. Without it a repository
  // full of big files would turn a search into a full disk scan.
  constexpr std::size_t kMaxContentBytes = 32u * 1024u * 1024u;
  std::size_t content_bytes = 0;
  std::size_t visited = 0;

  struct Frame {
    std::string dir;
    int depth;
  };
  std::vector<Frame> stack;
  stack.push_back({start, 0});

  while (!stack.empty() && visited < kMaxVisited && out.hits.size() < query.max_results) {
    const Frame frame = stack.back();
    stack.pop_back();
    std::vector<DirEntryInfo> entries;
    if (!list_directory(frame.dir, &entries, nullptr)) continue;
    for (std::size_t i = 0; i < entries.size(); ++i) {
      if (visited >= kMaxVisited || out.hits.size() >= query.max_results) {
        // More may be hiding: the caller must say "and more" rather than
        // presenting a truncated list as the whole truth.
        out.truncated = true;
        break;
      }
      const DirEntryInfo& e = entries[i];
      ++visited;
      if (e.is_dir) {
        if (frame.depth + 1 > kMaxDepth) continue;
        if (search_skip_dir(e.name)) continue;
        stack.push_back({e.path, frame.depth + 1});
        continue;
      }
      ++out.files_scanned;

      const std::string rel = display_path(e.path);
      SearchHit hit;
      hit.path = rel;
      hit.name = e.name;
      hit.size = e.size;
      hit.mtime = e.mtime;

      if (query.names) {
        // The whole-string mode accepts the file name as well as the whole
        // relative path: "main.cpp" should find "src/main.cpp".
        hit.name_hit = query.whole
                           ? (search_equals(rel, needle, query.case_sensitive) ||
                              search_equals(e.name, needle, query.case_sensitive))
                           : search_contains(rel, needle, query.case_sensitive);
      }

      if (query.content) {
        if (e.size > query.max_file_bytes) {
          out.files_skipped = true;
        } else if (search_binary_ext(e.name)) {
          out.files_skipped = true;
        } else if (content_bytes + e.size > kMaxContentBytes) {
          out.files_skipped = true;
        } else {
          std::string text;
          if (read_file(e.path, &text, query.max_file_bytes, nullptr)) {
            content_bytes += text.size();
            if (search_looks_binary(text)) {
              out.files_skipped = true;
            } else {
              int line_no = 0;
              std::size_t pos = 0;
              while (pos <= text.size()) {
                const std::size_t nl = text.find('\n', pos);
                std::string line =
                    (nl == std::string::npos) ? text.substr(pos) : text.substr(pos, nl - pos);
                ++line_no;
                if (!line.empty() && line.back() == '\r') line.pop_back();
                const bool matched =
                    query.whole ? search_equals(trim(line), needle, query.case_sensitive)
                                 : search_contains(line, needle, query.case_sensitive);
                if (matched) {
                  if (hit.hits == 0) hit.preview = search_preview(line);
                  if (hit.lines.size() < kMaxHitsPerFile) hit.lines.push_back(line_no);
                  if (hit.samples.size() < kMaxSamplesPerFile) {
                    hit.samples.push_back(search_preview(line));
                  }
                  ++hit.hits;
                }
                if (nl == std::string::npos) break;
                pos = nl + 1;
              }
            }
          } else {
            out.files_skipped = true;
          }
        }
      }

      if (hit.name_hit || hit.hits > 0) {
        out.hits.push_back(hit);
      }
    }
  }
  if (!stack.empty()) out.truncated = true;

  // Order: the file whose name matches comes first, then a file whose content
  // matches. Inside a group, alphabetical - a stable order the UI and the
  // model can rely on.
  std::stable_sort(out.hits.begin(), out.hits.end(),
                   [](const SearchHit& a, const SearchHit& b) {
                     const int ra = a.name_hit ? 0 : 1;
                     const int rb = b.name_hit ? 0 : 1;
                     if (ra != rb) return ra < rb;
                     return a.path < b.path;
                   });
  return out;
}

// ---------------------------------------------------------------------------
// history search (conversations + trajectories)
// ---------------------------------------------------------------------------
namespace {

/// Where in the history a hit came from. The UI says it out loud, because
/// "the agent said it" and "a tool printed it" are different things.
const char* const kHitTitle = "title";
const char* const kHitMessage = "message";
const char* const kHitSummary = "summary";
const char* const kHitToolCall = "tool_call";
const char* const kHitTrajectory = "trajectory";

/// The offset of `needle` in `haystack`, or npos.
///
/// Case folding only touches ASCII letters, so the folded string has the same
/// length as the original and the offset is valid for both.
std::size_t search_find(const std::string& haystack, const std::string& needle,
                         bool case_sensitive) {
  if (needle.empty() || haystack.empty()) return std::string::npos;
  if (case_sensitive) {
    const std::size_t at = haystack.find(needle);
    return at;
  }
  const std::string folded = search_fold(haystack);
  return folded.find(search_fold(needle));
}

/// True when `text` contains the query - or, in the whole-string mode, when
/// one of its lines *is* the query.
bool history_matches(const std::string& text, const std::string& needle, bool whole,
                     bool case_sensitive, std::size_t* offset) {
  if (offset) *offset = std::string::npos;
  if (text.empty() || needle.empty()) return false;
  if (!whole) {
    const std::size_t at = search_find(text, needle, case_sensitive);
    if (offset) *offset = at;
    return at != std::string::npos;
  }
  // 完全匹配：整行相等。
  std::size_t pos = 0;
  int line_no = 0;
  while (pos <= text.size()) {
    const std::size_t nl = text.find('\n', pos);
    std::string line = (nl == std::string::npos) ? text.substr(pos) : text.substr(pos, nl - pos);
    ++line_no;
    if (!line.empty() && line.back() == '\r') line.pop_back();
    if (search_equals(trim(line), needle, case_sensitive)) {
      if (offset) *offset = pos;
      return true;
    }
    if (nl == std::string::npos) break;
    pos = nl + 1;
  }
  return false;
}

/// A window of `text` around the match, so the reader sees the hit in context.
/// The window is cut on UTF-8 boundaries: a half character in the snippet
/// would be a broken character in the browser.
std::string history_snippet(const std::string& text, std::size_t offset, std::size_t hit_len) {
  const std::size_t kBefore = 40;
  const std::size_t kAfter = 120;
  const std::size_t kMax = 200;
  std::size_t start = offset > kBefore ? offset - kBefore : 0;
  std::size_t end = std::min(text.size(), offset + hit_len + kAfter);
  if (end - start > kMax) end = start + kMax;
  // Move `start` forward to a character boundary (a UTF-8 continuation byte
  // cannot start a character).
  while (start > 0 && start < text.size() &&
         (static_cast<unsigned char>(text[start]) & 0xC0) == 0x80) {
    ++start;
  }
  std::string out = text.substr(start, end - start);
  // And `end` back to one.
  while (end > start && end < text.size() &&
         (static_cast<unsigned char>(text[end]) & 0xC0) == 0x80) {
    --end;
    out = text.substr(start, end - start);
  }
  if (start > 0) out = "…" + out;
  if (end < text.size()) out += "…";
  return trim(out);
}

}  // namespace

nlohmann::json HistorySnippet::to_json() const {
  nlohmann::json j = nlohmann::json::object();
  j["kind"] = kind;
  if (!role.empty()) j["role"] = role;
  if (index >= 0) j["index"] = index;
  if (!tool.empty()) j["tool"] = tool;
  j["text"] = text;
  return j;
}

nlohmann::json HistoryHit::to_json() const {
  nlohmann::json j = nlohmann::json::object();
  j["session_id"] = session_id;
  j["title"] = title;
  j["created_at"] = created_at;
  j["updated_at"] = updated_at;
  j["message_count"] = message_count;
  j["hits"] = hits;
  j["title_hit"] = title_hit;
  nlohmann::json arr = nlohmann::json::array();
  for (std::size_t i = 0; i < snippets.size(); ++i) arr.push_back(snippets[i].to_json());
  j["snippets"] = arr;
  return j;
}

HistoryResults Workspace::search_history(const HistoryQuery& query) const {
  HistoryResults out;
  const std::string needle = trim(query.text);
  if (needle.empty()) return out;
  if (!query.titles && !query.messages && !query.trajectory) return out;

  const std::vector<SessionInfo> sessions = list_sessions();
  for (std::size_t s = 0; s < sessions.size(); ++s) {
    if (out.hits.size() >= query.max_results || out.sessions_scanned >= query.max_sessions) {
      // More may be hiding: say so instead of pretending this is everything.
      out.truncated = true;
      break;
    }
    const SessionInfo& info = sessions[s];
    ++out.sessions_scanned;

    HistoryHit hit;
    hit.session_id = info.id;
    hit.title = info.title;
    hit.created_at = info.created_at;
    hit.updated_at = info.updated_at;
    hit.message_count = info.message_count;

    // ---- the title --------------------------------------------------------
    if (query.titles) {
      std::size_t at = 0;
      if (history_matches(info.title, needle, query.whole, query.case_sensitive, &at)) {
        hit.title_hit = true;
        ++hit.hits;
        HistorySnippet snip;
        snip.kind = kHitTitle;
        snip.text = info.title;
        hit.snippets.push_back(snip);
      }
    }

    // ---- the transcript ---------------------------------------------------
    if (query.messages) {
      const std::string file = session_file_for(sessions_dir(), info.id);
      const uint64_t size = file_size(file);
      if (size > query.max_bytes_per_session) {
        out.sessions_skipped = true;
      } else {
        Session session;
        std::string err;
        if (load_session(info.id, &session, &err)) {
          for (std::size_t m = 0; m < session.messages.size(); ++m) {
            if (hit.snippets.size() >= query.max_snippets) break;
            const Message& msg = session.messages[m];
            // What is worth searching in a message: what was said, what was
            // thought, and what was called.
            struct Part {
              const char* kind;
              const std::string* text;
            };
            const Part parts[] = {
                {kHitMessage, &msg.content},
                {kHitToolCall, &msg.reasoning},
            };
            for (std::size_t p = 0; p < 2; ++p) {
              if (parts[p].text->empty()) continue;
              std::size_t at = 0;
              if (!history_matches(*parts[p].text, needle, query.whole, query.case_sensitive,
                                   &at)) {
                continue;
              }
              HistorySnippet snip;
              snip.kind = parts[p].kind;
              snip.role = msg.role;
              snip.index = static_cast<int>(m);
              snip.text = history_snippet(*parts[p].text, at, needle.size());
              hit.snippets.push_back(snip);
              ++hit.hits;
              if (hit.snippets.size() >= query.max_snippets) break;
            }
            // The tool calls the model made are part of the conversation too.
            for (std::size_t t = 0; t < msg.tool_calls.size(); ++t) {
              if (hit.snippets.size() >= query.max_snippets) break;
              const ToolCall& call = msg.tool_calls[t];
              const std::string args =
                  call.raw_arguments.empty() ? dump_json(call.arguments) : call.raw_arguments;
              std::size_t at = 0;
              if (!history_matches(args, needle, query.whole, query.case_sensitive, &at)) continue;
              HistorySnippet snip;
              snip.kind = kHitToolCall;
              snip.role = msg.role;
              snip.index = static_cast<int>(m);
              snip.tool = call.name;
              snip.text = history_snippet(args, at, needle.size());
              hit.snippets.push_back(snip);
              ++hit.hits;
            }
          }
          // The compacted summary is what the model remembers of the old
          // messages; a hit there is still a hit.
          if (!session.summary.empty() && hit.snippets.size() < query.max_snippets) {
            std::size_t at = 0;
            if (history_matches(session.summary, needle, query.whole, query.case_sensitive, &at)) {
              HistorySnippet snip;
              snip.kind = kHitSummary;
              snip.text = history_snippet(session.summary, at, needle.size());
              hit.snippets.push_back(snip);
              ++hit.hits;
            }
          }
        }
      }
    }

    // ---- the trajectory ---------------------------------------------------
    // The trajectory is the audit trail: which tools ran, with which
    // arguments, and what they printed. A word that only ever appeared in a
    // command's output is worth finding.
    if (query.trajectory) {
      std::vector<nlohmann::json> turns;
      std::string err;
      const std::string tpath = trajectory_file_path(run_dir_, info.id);
      if (load_trajectories(tpath, &turns, &err)) {
        for (std::size_t t = 0; t < turns.size(); ++t) {
          if (hit.snippets.size() >= query.max_snippets) break;
          if (!turns[t].is_object()) continue;
          auto steps = turns[t].find("steps");
          if (steps == turns[t].end() || !steps->is_array()) continue;
          for (std::size_t k = 0; k < steps->size(); ++k) {
            if (hit.snippets.size() >= query.max_snippets) break;
            const nlohmann::json& step = (*steps)[k];
            if (!step.is_object()) continue;
            const std::string kind = step.value("kind", std::string());
            if (kind != "tool") continue;
            const std::string tool = step.value("name", std::string());
            const std::string args = step.value("arguments", std::string());
            const std::string output = step.value("output", std::string());
            struct Field {
              const std::string* text;
            };
            const Field fields[] = {{&args}, {&output}};
            for (std::size_t f = 0; f < 2; ++f) {
              if (fields[f].text->empty()) continue;
              std::size_t at = 0;
              if (!history_matches(*fields[f].text, needle, query.whole, query.case_sensitive,
                                   &at)) {
                continue;
              }
              HistorySnippet snip;
              snip.kind = kHitTrajectory;
              snip.tool = tool;
              snip.text = history_snippet(*fields[f].text, at, needle.size());
              hit.snippets.push_back(snip);
              ++hit.hits;
              if (hit.snippets.size() >= query.max_snippets) break;
            }
          }
        }
      }
    }

    if (hit.hits > 0) out.hits.push_back(hit);
  }

  // Newest first - the same order the session list uses.
  std::stable_sort(out.hits.begin(), out.hits.end(),
                   [](const HistoryHit& a, const HistoryHit& b) {
                     if (a.updated_at != b.updated_at) return a.updated_at > b.updated_at;
                     return a.session_id < b.session_id;
                   });
  return out;
}

std::vector<nlohmann::json> Workspace::load_memory(std::string* err) const {
  std::vector<nlohmann::json> notes;
  bool ok = false;
  nlohmann::json j = read_json_file(memory_file(), &ok);
  if (!ok) {
    if (path_exists(memory_file()) && err) *err = "cannot read memory file";
    return notes;
  }
  auto it = j.find("notes");
  if (it != j.end() && it->is_array()) {
    for (auto n = it->begin(); n != it->end(); ++n) notes.push_back(*n);
  }
  return notes;
}

nlohmann::json Workspace::add_memory_note(const std::string& text, std::string* err) const {
  // Read-load-append-write: the whole point of the lock. Two turns remembering
  // something at the same time must both survive.
  StateLock lock(state_mutex());
  const std::string trimmed = trim(text);
  if (trimmed.empty()) {
    if (err) *err = "memory text is empty";
    return nlohmann::json();
  }
  std::vector<nlohmann::json> notes = load_memory(nullptr);
  nlohmann::json note = nlohmann::json::object();
  note["id"] = make_id("n_");
  note["text"] = trimmed;
  note["created_at"] = now_iso8601();
  notes.push_back(note);

  nlohmann::json mem = nlohmann::json::object();
  nlohmann::json arr = nlohmann::json::array();
  for (std::size_t i = 0; i < notes.size(); ++i) arr.push_back(notes[i]);
  mem["notes"] = arr;
  mem["updated_at"] = now_iso8601();
  if (!write_file_atomic(memory_file(), dump_json(mem, 2) + "\n", err)) return nlohmann::json();
  return note;
}

bool Workspace::delete_memory_note(const std::string& note_id, std::string* err) const {
  StateLock lock(state_mutex());
  std::vector<nlohmann::json> notes = load_memory(nullptr);
  nlohmann::json arr = nlohmann::json::array();
  bool removed = false;
  for (std::size_t i = 0; i < notes.size(); ++i) {
    auto id_it = notes[i].find("id");
    if (id_it != notes[i].end() && id_it->is_string() && id_it->get<std::string>() == note_id) {
      removed = true;
      continue;
    }
    arr.push_back(notes[i]);
  }
  if (!removed) {
    if (err) *err = "memory note not found: " + note_id;
    return false;
  }
  nlohmann::json mem = nlohmann::json::object();
  mem["notes"] = arr;
  mem["updated_at"] = now_iso8601();
  return write_file_atomic(memory_file(), dump_json(mem, 2) + "\n", err);
}

std::vector<std::string> Workspace::tail_log(std::size_t max_lines) const {
  std::vector<std::string> lines;
  const std::string path = log_file();
  if (!path_exists(path)) return lines;
  std::string data;
  if (!read_file_tail(path, 512u * 1024u, &data)) return lines;
  lines = split_lines(data);
  // The first line is probably cut in half when we started mid-file.
  if (data.size() >= 512u * 1024u && !lines.empty()) lines.erase(lines.begin());
  if (max_lines > 0 && lines.size() > max_lines) {
    lines.erase(lines.begin(),
                lines.begin() + static_cast<std::ptrdiff_t>(lines.size() - max_lines));
  }
  return lines;
}

void Workspace::log(const std::string& level, const std::string& message) const {
  const std::string tag = entry_.id.empty() ? entry_.name : entry_.id;
  Logger::instance().write(log_level_from_string(level), tag, message);
}

std::string Workspace::derive_title(const std::string& text) {
  std::string t = trim(text);
  const std::size_t nl = t.find_first_of("\r\n");
  if (nl != std::string::npos) t = t.substr(0, nl);
  t = trim(t);
  bool truncated = false;
  std::string out = truncate_utf8(t, 80, &truncated);
  if (truncated) out += "...";
  if (out.empty()) out = "新会话";
  return out;
}

}  // namespace tea
