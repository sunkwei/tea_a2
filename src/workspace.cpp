// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
#include "workspace.h"

#include <yaml-cpp/yaml.h>

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <iterator>
#include <map>
#include <regex>
#include <set>
#include <sstream>
#include <utility>

#include "env.h"
#include "gzip.h"
#include "log.h"
#include "state_lock.h"
#include "trajectory.h"
#include "util.h"

namespace tea {
namespace {

const char* const kRunDirName = ".tea_agent_run";

/// The format of the workspace index (workspaces.json).
///
///   1 - "last_used_at" was stamped when the workspace was **added**;
///   2 - "last_used_at" means "when it was used" (a turn ran there, or
///       somebody switched to it in the UI).
///
/// An index of version 1 is migrated on load: those stamps are registration
/// times, not usage times.
const int kIndexVersion = 2;
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

/// Read a JSON file that may have been gzipped by the history cleaner. The
/// compressed copy is the same JSON - the cleaner only changes how the bytes
/// are stored, never what they mean.
nlohmann::json read_json_file_maybe_gzip(const std::string& path, bool* ok) {
  *ok = false;
  std::string text;
  std::string err;
  // read_file_maybe_gzip() finds the compressed twin when the plain file is
  // gone, so the existence check is its job, not ours.
  if (!read_file_maybe_gzip(path, &text, &err)) return nlohmann::json();
  try {
    nlohmann::json j = nlohmann::json::parse(text);
    *ok = true;
    return j;
  } catch (const std::exception&) {
    return nlohmann::json();
  }
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
  // The index format:
  //   1 - "last_used_at" was stamped when the workspace was **added** (and
  //       again when it was registered a second time), so the field said
  //       "when it was registered", not "when it was used";
  //   2 - "last_used_at" means what it says: the last time a turn ran there
  //       or somebody switched to it.
  //
  // An index of version 1 therefore carries registration times in that field.
  // They are dropped on load (the registration time is in `created_at`
  // anyway), and the index is written back as version 2.
  int version = 1;
  bool migrate = false;
  try {
    nlohmann::json j = nlohmann::json::parse(text);
    const nlohmann::json* list = nullptr;
    if (j.is_object()) {
      if (j.contains("version") && j["version"].is_number_integer()) {
        version = j["version"].get<int>();
      }
      auto it = j.find("workspaces");
      if (it != j.end() && it->is_array()) list = &(*it);
    } else if (j.is_array()) {
      list = &j;
    }
    migrate = version < kIndexVersion;
    if (list) {
      for (auto it = list->begin(); it != list->end(); ++it) {
        WorkspaceEntry e = WorkspaceEntry::from_json(*it);
        if (e.id.empty() && !e.path.empty()) e.id = make_id("ws_");
        if (migrate) e.last_used_at.clear();
        if (!e.path.empty()) entries_.push_back(e);
      }
    }
  } catch (const std::exception& e) {
    if (err) *err = std::string("cannot parse workspace index ") + index_path_ + ": " + e.what();
    return false;
  }
  refresh_flags();
  LOG_INFO("workspaces: loaded ", entries_.size(), " entr(ies) from ", index_path_);
  if (migrate) {
    LOG_INFO("workspaces: the index is from an older version - the \"last used\" stamps in it "
             "were registration times; they are dropped");
    std::string save_err;
    if (!save(&save_err)) LOG_WARN("workspaces: cannot write the migrated index: ", save_err);
  }
  return true;
}

bool WorkspaceRegistry::save(std::string* err) const {
  StateLock lock(state_mutex());
  nlohmann::json root = nlohmann::json::object();
  root["version"] = kIndexVersion;
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
      if (!probe_dir_writable(e.run_dir, &reason)) {
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
    // Registering a workspace is **not** using it: the "last used" stamp is
    // for the people who work in it (see touch()). Overwriting it here would
    // put every freshly added project on top of the list - the opposite of
    // what the list is for.
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
  // Empty until it is used: a workspace nobody worked in yet keeps its place
  // in the registration order.
  e.last_used_at.clear();
  e.exists = true;
  entries_.push_back(e);
  refresh_flags();
  std::string save_err;
  if (!save(&save_err)) LOG_WARN("workspaces: cannot update the index: ", save_err);
  return &entries_.back();
}

bool WorkspaceRegistry::touch(const std::string& id) {
  // The state lock covers the change **and** the write: the index is one file
  // that every workspace shares, and a half-written one loses a registration.
  // (save() takes the same lock; it is recursive on purpose.)
  StateLock lock(state_mutex());
  WorkspaceEntry* e = find(id);
  if (e == nullptr) return false;
  const std::string stamp = now_iso8601_ms();
  if (e->last_used_at == stamp) return true;
  e->last_used_at = stamp;
  std::string save_err;
  if (!save(&save_err)) LOG_WARN("workspaces: cannot update the index: ", save_err);
  return true;
}

bool WorkspaceRegistry::set_run_dir(const std::string& id, const std::string& dir) {
  WorkspaceEntry* e = find(id);
  if (!e) return false;
  e->run_dir = dir;
  std::string save_err;
  if (!save(&save_err)) LOG_WARN("workspaces: cannot update the index: ", save_err);
  return true;
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

/// The compressed twin of a session file: what the history cleaner leaves
/// behind for a session nobody has touched for a while.
std::string session_gzip_file_for(const std::string& dir, const std::string& id) {
  return session_file_for(dir, id) + ".gz";
}

/// Does the session exist at all - plain or compressed?
bool session_file_exists(const std::string& dir, const std::string& id) {
  return path_exists(session_file_for(dir, id)) || path_exists(session_gzip_file_for(dir, id));
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
  // The runtime data normally lives inside the project, but it may point
  // elsewhere: a project the process may not write to is still a usable
  // workspace, with its sessions, log and memory in the tea home.
  run_dir_ = entry_.run_dir.empty() ? path_join(root_, kRunDirName) : entry_.run_dir;

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
  dirs.push_back(roles_dir());
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

std::string Workspace::roles_dir() const {
  return path_join(run_dir_, "roles");
}

std::string Workspace::role_file(const std::string& id) const {
  return path_join(roles_dir(), id + ".yaml");
}

RoleSet Workspace::load_roles() const {
  RoleSet set = RoleSet::load(roles_dir());
  // The built-in templates are part of the program, not files here: the
  // workspace holds the roles the user made. A workspace file with the same
  // id wins - it is the user's own version of that role, and deleting the
  // file brings the template back.
  const std::vector<std::pair<std::string, std::string>>& builtins = builtin_roles();
  for (std::size_t i = 0; i < builtins.size(); ++i) {
    if (set.find(builtins[i].first) != nullptr) continue;
    Role role;
    std::string err;
    if (!builtin_role(builtins[i].first, &role, &err)) {
      LOG_WARN("workspace ", entry_.id, ": ", err);
      continue;
    }
    set.add(role);
  }
  return set;
}

bool Workspace::role_has_file(const std::string& id) const {
  return path_exists(role_file(id));
}

std::string Workspace::current_role_id() const {
  // Fresh on purpose: the pointer is what the user switches, and the switch
  // has to reach the next turn without reopening the workspace. The settings
  // of the workspace itself are read once (the prompt prefix must stay
  // stable), so this one file is parsed again here.
  std::string text;
  if (path_exists(settings_file()) && read_file(settings_file(), &text, 1024u * 1024u)) {
    try {
      const YAML::Node root = YAML::Load(text);
      if (root && root.IsMap()) {
        const YAML::Node block = root["settings"] ? root["settings"] : root;
        if (block && block.IsMap()) {
          const YAML::Node value = block["role"];
          if (value && value.IsScalar()) return trim(value.as<std::string>());
        }
      }
    } catch (const std::exception&) {
      // A settings.yaml that does not parse keeps the value it had when the
      // workspace was opened; the workspace already logged the reason.
    }
  }
  return trim(settings_.role);
}

bool Workspace::set_current_role(const std::string& id, std::string* err) {
  const std::string clean = trim(id);
  if (!clean.empty()) {
    if (!valid_role_id(clean)) {
      if (err) *err = "'" + clean + "' is not a usable role name";
      return false;
    }
    // The role must exist: a file of this workspace, or one of the built-in
    // templates (they live in the program, not in a file).
    if (!role_has_file(clean) && builtin_role_text(clean).empty()) {
      if (err) *err = "no such role in this workspace: " + clean;
      return false;
    }
  }
  if (!write_setting_string("role", clean, err)) return false;
  settings_.role = clean;
  LOG_INFO("workspace ", entry_.id, ": the role is now ", clean.empty() ? "(none)" : clean,
           " (applies from the next turn on)");
  return true;
}

bool Workspace::write_setting_bool(const std::string& key, bool value, std::string* err) {
  return write_setting_line(key, value ? "true" : "false", err);
}

bool Workspace::write_setting_string(const std::string& key, const std::string& value,
                                     std::string* err) {
  // YAML-quoted: an id may hold characters a bare scalar would read as
  // something else (a colon, a hash, a leading quote).
  std::string quoted = "\"";
  for (std::size_t i = 0; i < value.size(); ++i) {
    const char c = value[i];
    if (c == '"' || c == '\\') quoted += '\\';
    quoted += c;
  }
  quoted += "\"";
  return write_setting_line(key, quoted, err);
}

bool Workspace::write_setting_line(const std::string& key, const std::string& value_literal,
                                   std::string* err) {
  StateLock lock(state_mutex());
  const std::string path = settings_file();
  const std::string literal = std::string(key) + ": " + value_literal;
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
  LOG_INFO("workspace ", entry_.id, ": ", key, " = ", value_literal, " written to ",
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

bool Workspace::agent_doc_is_pristine() const {
  std::string text;
  if (!read_file(agent_doc_file(), &text)) return true;  // no file at all
  return trim(text) == trim(kAgentDocTemplate);
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
        if (!session_file_exists(dir, s.id)) continue;
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
        // A compressed session is "<id>.json.gz": the id is the stem without
        // the ".json" part.
        std::string id;
        const std::string ext = path_extension(files[i].name);
        if (ext == ".json") {
          id = path_stem(files[i].name);
        } else if (files[i].name.size() > 8 &&
                   files[i].name.compare(files[i].name.size() - 8, 8, ".json.gz") == 0) {
          id = files[i].name.substr(0, files[i].name.size() - 8);
        } else {
          continue;
        }
        bool ok = false;
        nlohmann::json j = read_json_file_maybe_gzip(files[i].path, &ok);
        if (!ok || !j.is_object()) continue;
        SessionInfo s;
        s.id = id;
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
  nlohmann::json j = read_json_file_maybe_gzip(path, &ok);
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
  // The cleaner may have compressed this session while it was cold. It is hot
  // again now, so the plain file is the truth and the compressed twin goes.
  {
    const std::string gz_path = session_path + ".gz";
    if (path_exists(gz_path)) remove_path(gz_path, false, nullptr);
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
  if (!session_file_exists(sessions_dir(), session_id)) {
    if (err) *err = "session not found: " + session_id;
    return false;
  }
  bool ok = false;
  const nlohmann::json j = read_json_file_maybe_gzip(path, &ok);
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
  if (!session_file_exists(dir, session_id)) {
    if (err) *err = "session not found: " + session_id;
    return false;
  }
  bool ok = false;
  nlohmann::json j = read_json_file_maybe_gzip(path, &ok);
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
  {
    const std::string gz_path = path + ".gz";
    if (path_exists(gz_path)) remove_path(gz_path, false, nullptr);
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
  const std::string gz_path = session_gzip_file_for(dir, id);
  if (!path_exists(path) && !path_exists(gz_path)) {
    if (err) *err = "session not found: " + id;
    return false;
  }
  remove_path(path, false, nullptr);
  remove_path(gz_path, false, nullptr);
  // The trajectory is the audit trail of the very same session. Deleting the
  // conversation and leaving its turns behind on disk would keep the history
  // (and the search index) pointing at a conversation that no longer exists.
  const std::string traj = trajectory_file_path(run_dir_, id);
  remove_path(traj, false, nullptr);
  remove_path(traj + ".gz", false, nullptr);
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

namespace {
/// One file the cleaner may act on.
struct GcEntry {
  std::string id;
  std::string path;
  std::string mtime;   // ISO-8601 UTC; empty when the file has no timestamp
  int64_t size = 0;
  bool compressed = false;
};

/// The session id behind a transcript file name ("<id>.json" or
/// "<id>.json.gz"). Empty for anything else (index.json included).
std::string session_id_from_file(const std::string& name) {
  if (name.size() > 8 && name.compare(name.size() - 8, 8, ".json.gz") == 0) {
    return name.substr(0, name.size() - 8);
  }
  if (name != "index.json" && path_extension(name) == ".json") return path_stem(name);
  return std::string();
}

/// The session id behind a trajectory file name ("<id>.jsonl" or
/// "<id>.jsonl.gz").
std::string trajectory_id_from_file(const std::string& name) {
  if (name.size() > 10 && name.compare(name.size() - 10, 10, ".jsonl.gz") == 0) {
    return name.substr(0, name.size() - 10);
  }
  if (path_extension(name) == ".jsonl") return path_stem(name);
  return std::string();
}
}  // namespace

bool Workspace::gc_history(const HistoryGcLimits& limits, HistoryGcReport* report,
                            std::string* err) const {
  // The state lock for the whole run: the compression replaces a file with
  // its compressed twin, and a turn writing that session at the same moment
  // would lose its newest words ("the old file was compressed, the new one
  // was removed"). The lock is the same one the session writes use, so the
  // two cannot overlap. It is recursive, so delete_session() below may take
  // it again.
  StateLock lock(state_mutex());
  HistoryGcReport local;
  if (!report) report = &local;
  report->dry_run = limits.dry_run;

  const std::string dir = sessions_dir();
  const std::string traj_dir = path_join(run_dir_, "trajectories");
  const int64_t now_s = now_ms() / 1000;
  const bool do_it = !limits.dry_run;

  // ---- what is there ----------------------------------------------------
  // One entry per session: the plain file wins over a stale compressed copy.
  std::map<std::string, GcEntry> by_id;
  std::vector<DirEntryInfo> files;
  std::string list_err;
  if (list_directory(dir, &files, &list_err)) {
    for (std::size_t i = 0; i < files.size(); ++i) {
      if (files[i].is_dir) continue;
      const std::string id = session_id_from_file(files[i].name);
      if (id.empty()) continue;
      GcEntry e;
      e.id = id;
      e.path = files[i].path;
      e.mtime = files[i].mtime;
      e.size = static_cast<int64_t>(files[i].size);
      e.compressed = path_is_gzip(files[i].path);
      std::map<std::string, GcEntry>::iterator it = by_id.find(id);
      if (it == by_id.end() || (!it->second.compressed && e.compressed)) by_id[id] = e;
    }
  }
  std::vector<GcEntry> sessions;
  for (std::map<std::string, GcEntry>::const_iterator it = by_id.begin(); it != by_id.end(); ++it) {
    sessions.push_back(it->second);
  }
  // Newest first: the first `keep_recent` ones are the ones being worked on.
  std::sort(sessions.begin(), sessions.end(), [](const GcEntry& a, const GcEntry& b) {
    if (a.mtime != b.mtime) return a.mtime > b.mtime;
    return a.id < b.id;
  });

  std::map<std::string, GcEntry> traj_by_id;
  files.clear();
  if (list_directory(traj_dir, &files, &list_err)) {
    for (std::size_t i = 0; i < files.size(); ++i) {
      if (files[i].is_dir) continue;
      const std::string id = trajectory_id_from_file(files[i].name);
      if (id.empty()) continue;
      GcEntry e;
      e.id = id;
      e.path = files[i].path;
      e.mtime = files[i].mtime;
      e.size = static_cast<int64_t>(files[i].size);
      e.compressed = path_is_gzip(files[i].path);
      std::map<std::string, GcEntry>::iterator it = traj_by_id.find(id);
      if (it == traj_by_id.end() || (!it->second.compressed && e.compressed)) traj_by_id[id] = e;
    }
  }

  report->bytes_before = tree_size(run_dir_);

  // ---- 1. what is too old ----------------------------------------------
  const std::string age_cutoff =
      limits.max_age_days > 0
          ? iso8601_from_unix(now_s - static_cast<int64_t>(limits.max_age_days) * 86400)
          : std::string();
  const std::string cold_cutoff =
      limits.compress_after_days > 0
          ? iso8601_from_unix(now_s - static_cast<int64_t>(limits.compress_after_days) * 86400)
          : std::string();

  // What the age pass takes away: the compression pass must not count the
  // same file twice (a dry run would say "1 removed, 1 compressed" for one
  // file).
  std::vector<std::string> removed_ids;
  if (!age_cutoff.empty()) {
    // The age limit is an explicit statement about what is stale, so it does
    // not spare the newest sessions: a session nobody touched for 40 days is
    // 40 days old, whether it is the last one or not. (A session in use has a
    // fresh mtime - every save rewrites the file.)
    for (std::size_t i = 0; i < sessions.size(); ++i) {
      const GcEntry& e = sessions[i];
      if (e.mtime.empty() || e.mtime >= age_cutoff) continue;
      if (do_it) {
        std::string del_err;
        if (!delete_session(e.id, &del_err)) {
          report->notes.push_back("cannot delete session " + e.id + ": " + del_err);
          continue;
        }
      }
      report->sessions_removed++;
      report->removed_sessions.push_back(e.id);
      removed_ids.push_back(e.id);
      if (traj_by_id.count(e.id)) {
        report->trajectories_removed++;
        removed_ids.push_back(e.id);
      }
    }
    // A trajectory whose session is gone is an orphan: it belongs to a
    // conversation that was deleted, and it is pure disk.
    for (std::map<std::string, GcEntry>::const_iterator it = traj_by_id.begin();
         it != traj_by_id.end(); ++it) {
      if (by_id.count(it->first)) continue;
      const GcEntry& e = it->second;
      if (e.mtime.empty() || e.mtime >= age_cutoff) continue;
      if (do_it) remove_path(e.path, false, nullptr);
      report->trajectories_removed++;
      removed_ids.push_back(e.id);
    }
  }

  // ---- 2. what is cold: gzip it, do not lose it ------------------------
  if (!cold_cutoff.empty()) {
    for (std::size_t i = 0; i < sessions.size(); ++i) {
      const GcEntry& e = sessions[i];
      if (e.compressed) continue;
      if (e.mtime.empty() || e.mtime >= cold_cutoff) continue;
      if (std::find(removed_ids.begin(), removed_ids.end(), e.id) != removed_ids.end()) continue;
      if (!do_it) {
        report->sessions_compressed++;
        report->compressed_sessions.push_back(e.id);
        continue;
      }
      int64_t saved = 0;
      std::string gz_err;
      if (!gzip_file_in_place(e.path, &gz_err, &saved)) continue;
      report->sessions_compressed++;
      report->compressed_sessions.push_back(e.id);
      report->compressed_saved += saved;
    }
    for (std::map<std::string, GcEntry>::const_iterator it = traj_by_id.begin();
         it != traj_by_id.end(); ++it) {
      const GcEntry& e = it->second;
      if (e.compressed) continue;
      if (e.mtime.empty() || e.mtime >= cold_cutoff) continue;
      if (std::find(removed_ids.begin(), removed_ids.end(), e.id) != removed_ids.end()) continue;
      if (!do_it) {
        report->trajectories_compressed++;
        continue;
      }
      int64_t saved = 0;
      std::string gz_err;
      if (!gzip_file_in_place(e.path, &gz_err, &saved)) continue;
      report->trajectories_compressed++;
      report->compressed_saved += saved;
    }
  }

  // ---- 3. over budget: the oldest sessions go --------------------------
  if (limits.max_bytes > 0) {
    // The budget is about the *history*: the roles are the user's own files
    // (like agent.md and settings.yaml), the cleaner never removes them, so
    // counting them here would only make it delete sessions it cannot make
    // room for.
    int64_t size = tree_size(run_dir_);
    const int64_t roles_size = tree_size(roles_dir());
    if (roles_size > 0) size -= roles_size;
    if (size < 0) size = 0;
    for (std::size_t i = sessions.size(); i > static_cast<std::size_t>(limits.keep_recent); --i) {
      if (size <= limits.max_bytes) break;
      const GcEntry& e = sessions[i - 1];
      bool already_removed = false;
      for (std::size_t k = 0; k < report->removed_sessions.size(); ++k) {
        if (report->removed_sessions[k] == e.id) already_removed = true;
      }
      if (already_removed) continue;
      if (do_it) {
        std::string del_err;
        if (!delete_session(e.id, &del_err)) {
          report->notes.push_back("cannot delete session " + e.id + ": " + del_err);
          continue;
        }
      }
      report->sessions_removed++;
      report->removed_sessions.push_back(e.id);
      size -= e.size;
    }
    if (size > limits.max_bytes) {
      report->notes.push_back(
          "the run_dir is still over its budget: the rest is not history (logs, artifacts, "
          "indexes) and was left alone");
    }
  }

  report->bytes_after = do_it ? tree_size(run_dir_) : report->bytes_before;
  report->freed_bytes = report->bytes_before - report->bytes_after;
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

// A regular expression is the one place where the comparison is not
// byte-for-byte, so it is bounded on both ends: the pattern may not be
// longer than this, and a regex search may not look at more than
// kMaxRegexLines lines. std::regex has no timeout - without these caps a
// pathological pattern ("(a+)+b") would hang the agent instead of
// answering.
constexpr std::size_t kMaxRegexPattern = 256;
constexpr std::size_t kMaxRegexLines = 200000;

/// The part of the line that matched, with a little context around it.
/// A pattern match says *where* the hit is; showing the whole line hides it.
std::string search_preview_regex(const std::string& line, const std::smatch& m) {
  const std::size_t kMax = 200;
  const std::size_t kContext = 40;
  std::size_t from = 0;
  std::size_t to = line.size();
  if (m.size() > 0 && m[0].matched) {
    const std::size_t start = static_cast<std::size_t>(m.position(0));
    const std::size_t len = static_cast<std::size_t>(m.length(0));
    from = start > kContext ? start - kContext : 0;
    to = start + len + kContext;
    if (to > line.size()) to = line.size();
  }
  std::string out = trim(line.substr(from, to - from));
  if (from > 0) out = "…" + out;
  if (to < line.size()) out += "…";
  if (out.size() > kMax) {
    out = out.substr(0, kMax);
    out += "…";
  }
  return out;
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

  // The pattern is compiled once, here: the walk below compares every line
  // of every file against it.
  std::regex pattern;
  const bool use_regex = query.regex;
  if (use_regex) {
    if (needle.size() > kMaxRegexPattern) {
      out.error = "the regular expression is too long (" +
                  std::to_string(needle.size()) + " > " +
                  std::to_string(kMaxRegexPattern) + " characters)";
      return out;
    }
    std::regex::flag_type flags = std::regex::ECMAScript;
    if (!query.case_sensitive) flags |= std::regex::icase;
    try {
      pattern = std::regex(needle, flags);
    } catch (const std::regex_error& exc) {
      out.error = std::string("invalid regular expression: ") + exc.what();
      return out;
    }
  }
  // How many lines a regex search may still look at (0 = no limit).
  std::size_t regex_lines = 0;

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

      // A regular expression matches file content only: a path is never
      // matched by a pattern (a path is not a line of text, and pretending
      // otherwise would surprise whoever wrote the pattern).
      if (query.names && !use_regex) {
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
                bool matched = false;
                std::string preview;
                if (use_regex) {
                  if (regex_lines >= kMaxRegexLines) {
                    // The cap is reached: the rest of the tree is left alone
                    // and the caller is told the answer is partial.
                    out.truncated = true;
                    break;
                  }
                  ++regex_lines;
                  if (query.whole) {
                    matched = std::regex_match(trim(line), pattern);
                    preview = search_preview(line);
                  } else {
                    std::smatch m;
                    matched = std::regex_search(line, m, pattern);
                    if (matched) preview = search_preview_regex(line, m);
                  }
                } else {
                  matched = query.whole
                                ? search_equals(trim(line), needle, query.case_sensitive)
                                : search_contains(line, needle, query.case_sensitive);
                  preview = search_preview(line);
                }
                if (matched) {
                  if (hit.hits == 0) hit.preview = preview;
                  if (hit.lines.size() < kMaxHitsPerFile) hit.lines.push_back(line_no);
                  if (hit.samples.size() < kMaxSamplesPerFile) {
                    hit.samples.push_back(preview);
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

namespace {

// --- memory weights --------------------------------------------------------
//
// A memory note is a fact or a preference that is injected into the system
// prompt. Left alone the file would grow without bound and the oldest line
// would still be there years later, so a note carries a weight that decays with
// its **absolute age** (14-day half-life - a memory that is never confirmed
// fades, like a human one) plus a hard cap on how many notes of each kind may
// exist.
constexpr double kMemoryHalfLifeDays = 14.0;
/// `weight_effective >= 0.75`: injected every turn.
constexpr double kMemoryTierStrong = 0.75;
/// `weight_effective > 0.50`: injected when it matches the conversation.
constexpr double kMemoryTierMedium = 0.50;
/// What the agent's fresh note starts at (a person's note starts at 1, on
/// purpose: "what I wrote down stays").
constexpr double kMemoryAgentWeight = 0.90;
/// A confirmation of an existing note raises its weight instead of duplicating
/// it; the boost is what makes remembering something twice worth doing.
constexpr double kMemoryMergeBonus = 0.20;
constexpr std::size_t kMemoryManualCap = 20;
constexpr std::size_t kMemoryAgentCap = 100;
constexpr std::size_t kMemoryStrongBudget = 24;
constexpr std::size_t kMemoryMediumBudget = 14;
constexpr std::size_t kMemoryWeakPick = 2;
/// How often a confirmation may reset the age. Past it the note keeps the
/// weight boost but keeps ageing: repeating it forever is not a way to live
/// forever.
constexpr int kMemoryRenewMax = 6;
constexpr int64_t kMemoryRenewWindowMs = 24LL * 60 * 60 * 1000;
/// Similarity: after normalisation an exact match always merges. Otherwise the
/// texts have to be long enough to be compared and share enough 2-grams. The
/// length guard is not cosmetic: "note-00" and "note-01" share 5 of 7 bigrams
/// (Jaccard 0.71), and a batch of such notes would collapse into one.
constexpr double kMemorySimilarJaccard = 0.60;
constexpr std::size_t kMemorySimilarMinChars = 12;
constexpr std::size_t kMemorySimilarMinShared = 6;
/// How much of a medium note the conversation has to mention for it to count as
/// relevant.
constexpr double kMemoryRelevanceMin = 0.30;
/// A merge keeps what the note used to say; that trace is bounded.
constexpr std::size_t kMemoryMergeTraceMax = 20;
/// The eviction audit keeps the newest lines only.
constexpr std::size_t kMemoryEvictedKeep = 200;
const char* const kMemoryAuditFileName = "evicted.jsonl";

/// ISO-8601 -> unix ms (0 when the string is not a timestamp).
int64_t memory_iso_ms(const std::string& s) {
  int64_t ms = 0;
  if (!s.empty() && iso8601_to_unix_ms(s, &ms)) return ms;
  return 0;
}

std::string memory_later(const std::string& a, const std::string& b) {
  if (b.empty()) return a;
  if (a.empty()) return b;
  return memory_iso_ms(b) > memory_iso_ms(a) ? b : a;
}

std::string memory_str(const nlohmann::json& j, const char* key) {
  auto it = j.find(key);
  if (it != j.end() && it->is_string()) return it->get<std::string>();
  return std::string();
}

int memory_int(const nlohmann::json& j, const char* key) {
  auto it = j.find(key);
  if (it != j.end() && it->is_number()) return it->get<int>();
  return 0;
}

/// UTF-8 -> code points. An invalid byte becomes U+FFFD one byte at a time: a
/// note somebody pasted must never make the comparison throw.
std::vector<char32_t> memory_code_points(const std::string& s) {
  std::vector<char32_t> out;
  out.reserve(s.size());
  for (std::size_t i = 0; i < s.size();) {
    const unsigned char c = static_cast<unsigned char>(s[i]);
    char32_t cp = 0xFFFD;
    std::size_t len = 1;
    if (c < 0x80) {
      cp = static_cast<char32_t>(c);
    } else if ((c & 0xE0) == 0xC0 && i + 1 < s.size()) {
      cp = (static_cast<char32_t>(c & 0x1F) << 6) |
           static_cast<char32_t>(static_cast<unsigned char>(s[i + 1]) & 0x3F);
      len = 2;
    } else if ((c & 0xF0) == 0xE0 && i + 2 < s.size()) {
      cp = (static_cast<char32_t>(c & 0x0F) << 12) |
           (static_cast<char32_t>(static_cast<unsigned char>(s[i + 1]) & 0x3F) << 6) |
           static_cast<char32_t>(static_cast<unsigned char>(s[i + 2]) & 0x3F);
      len = 3;
    } else if ((c & 0xF8) == 0xF0 && i + 3 < s.size()) {
      cp = (static_cast<char32_t>(c & 0x07) << 18) |
           (static_cast<char32_t>(static_cast<unsigned char>(s[i + 1]) & 0x3F) << 12) |
           (static_cast<char32_t>(static_cast<unsigned char>(s[i + 2]) & 0x3F) << 6) |
           static_cast<char32_t>(static_cast<unsigned char>(s[i + 3]) & 0x3F);
      len = 4;
    }
    out.push_back(cp);
    i += len;
  }
  return out;
}

/// What the comparison sees: full-width folded to half-width, ASCII lower-cased,
/// whitespace collapsed. The stored text is never rewritten - only compared.
std::vector<char32_t> memory_normalized(const std::string& text) {
  std::vector<char32_t> out;
  out.reserve(text.size());
  bool space_pending = false;
  for (char32_t cp : memory_code_points(text)) {
    if (cp >= 0xFF01 && cp <= 0xFF5E) {
      cp -= 0xFEE0;
    } else if (cp == 0x3000) {
      cp = ' ';
    }
    if (cp == ' ' || cp == '\t' || cp == '\r' || cp == '\n') {
      space_pending = !out.empty();
      continue;
    }
    if (cp >= 'A' && cp <= 'Z') cp = cp - 'A' + 'a';
    if (space_pending) {
      out.push_back(' ');
      space_pending = false;
    }
    out.push_back(cp);
  }
  return out;
}

std::set<uint64_t> memory_bigrams(const std::vector<char32_t>& cps) {
  std::set<uint64_t> out;
  for (std::size_t i = 0; i + 1 < cps.size(); ++i) {
    out.insert((static_cast<uint64_t>(cps[i]) << 32) |
               static_cast<uint64_t>(cps[i + 1]));
  }
  return out;
}

/// The numbers in a note are usually the fact itself (a port, a path, a
/// version, a budget). `本项目默认端口用 8080` and `... 9090` are 85% the same
/// string and two different facts, so they must not merge.
std::vector<std::string> memory_numbers(const std::vector<char32_t>& cps) {
  std::vector<std::string> out;
  std::string run;
  for (const char32_t cp : cps) {
    if (cp >= U'0' && cp <= U'9') {
      run += static_cast<char>(cp);
      continue;
    }
    if (!run.empty()) {
      out.push_back(run);
      run.clear();
    }
  }
  if (!run.empty()) out.push_back(run);
  std::sort(out.begin(), out.end());
  return out;
}

/// 0 when the two notes are not the same note; otherwise the Jaccard score
/// (1.0 for an exact match after normalisation).
double memory_similarity(const std::vector<char32_t>& a,
                         const std::vector<char32_t>& b) {
  if (a.empty() || b.empty()) return 0.0;
  if (a == b) return 1.0;
  if (std::min(a.size(), b.size()) < kMemorySimilarMinChars) return 0.0;
  if (memory_numbers(a) != memory_numbers(b)) return 0.0;
  const std::set<uint64_t> ga = memory_bigrams(a);
  const std::set<uint64_t> gb = memory_bigrams(b);
  if (ga.empty() || gb.empty()) return 0.0;
  std::size_t shared = 0;
  for (const uint64_t g : ga) {
    if (gb.count(g)) ++shared;
  }
  if (shared < kMemorySimilarMinShared) return 0.0;
  const std::size_t uni = ga.size() + gb.size() - shared;
  const double jaccard = static_cast<double>(shared) / static_cast<double>(uni);
  return jaccard >= kMemorySimilarJaccard ? jaccard : 0.0;
}

/// How much of the note the focus text covers (shared bigrams / the note's own
/// bigrams). A one-character note covers nothing - there is nothing to compare.
double memory_relevance(const std::vector<char32_t>& note,
                        const std::vector<char32_t>& focus) {
  const std::set<uint64_t> note_grams = memory_bigrams(note);
  if (note_grams.empty()) return 0.0;
  const std::set<uint64_t> focus_grams = memory_bigrams(focus);
  if (focus_grams.empty()) return 0.0;
  std::size_t shared = 0;
  for (const uint64_t g : note_grams) {
    if (focus_grams.count(g)) ++shared;
  }
  return static_cast<double>(shared) / static_cast<double>(note_grams.size());
}

uint64_t memory_seed_hash(const std::string& s) {
  uint64_t h = 1469598103934665603ull;  // FNV-1a
  for (const unsigned char c : s) {
    h ^= c;
    h *= 1099511628211ull;
  }
  return h ? h : 1;
}

/// xorshift64: the weak tier is sampled, but the sample has to be reproducible
/// (same seed -> same notes), or the prompt changes every turn for no reason.
struct MemoryRng {
  uint64_t state;
  uint64_t next() {
    state ^= state << 13;
    state ^= state >> 7;
    state ^= state << 17;
    return state;
  }
};

double memory_decay(double base, int64_t anchor_ms, int64_t now) {
  if (anchor_ms <= 0 || now <= anchor_ms) return base;
  const double days = static_cast<double>(now - anchor_ms) / 86400000.0;
  return base * std::pow(0.5, days / kMemoryHalfLifeDays);
}

/// Recompute everything that is derived: effective weight, tier, age, and the
/// "last confirmed" stamp the caps sort on.
void memory_refresh(MemoryNote* n, int64_t now) {
  if (n->created_at.empty()) n->created_at = n->decay_anchor;
  if (n->created_at.empty()) n->created_at = now_iso8601();
  if (n->decay_anchor.empty()) n->decay_anchor = n->created_at;
  const int64_t anchor_ms = memory_iso_ms(n->decay_anchor);
  n->age_days = (anchor_ms > 0 && now > anchor_ms)
                    ? static_cast<double>(now - anchor_ms) / 86400000.0
                    : 0.0;
  if (n->source == "user") {
    // A person's note does not fade: its weight is the promise, not a score.
    n->weight_base = 1.0;
    n->weight_effective = 1.0;
    n->tier = "manual";
  } else {
    if (n->weight_base <= 0.0) n->weight_base = kMemoryAgentWeight;
    n->weight_effective = memory_decay(n->weight_base, anchor_ms, now);
    n->tier = n->weight_effective >= kMemoryTierStrong
                  ? "strong"
                  : (n->weight_effective > kMemoryTierMedium ? "medium" : "weak");
  }
  n->last_write = memory_later(n->created_at, n->last_merge_at);
}

/// The note as the file holds it. Unknown fields survive, because memory.json
/// is also hand-edited and read by other tools.
nlohmann::json memory_note_json(const MemoryNote& n) {
  nlohmann::json j = n.raw.is_object() ? n.raw : nlohmann::json::object();
  j["id"] = n.id;
  j["text"] = n.text;
  j["source"] = n.source;
  j["weight_base"] = n.weight_base;
  j["created_at"] = n.created_at;
  j["decay_anchor"] = n.decay_anchor;
  if (n.last_merge_at.empty()) {
    j.erase("last_merge_at");
  } else {
    j["last_merge_at"] = n.last_merge_at;
  }
  if (n.merge_count > 0) j["merge_count"] = n.merge_count; else j.erase("merge_count");
  if (n.renew_count > 0) j["renew_count"] = n.renew_count; else j.erase("renew_count");
  return j;
}

/// A note is one bullet: a multi-line note must not break the list shape.
std::string memory_bullet(const std::string& text) {
  std::string out;
  out.reserve(text.size() + 8);
  for (const char c : text) {
    out += c;
    if (c == '\n') out += "  ";
  }
  return out;
}

}  // namespace

std::string Workspace::memory_audit_file() const {
  return path_join(path_join(run_dir_, "memory"), kMemoryAuditFileName);
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

std::vector<MemoryNote> Workspace::load_memory_notes(std::string* err) const {
  const std::vector<nlohmann::json> raw = load_memory(err);
  const int64_t now = now_ms();
  std::vector<MemoryNote> out;
  out.reserve(raw.size());
  for (std::size_t i = 0; i < raw.size(); ++i) {
    MemoryNote n;
    n.raw = raw[i].is_object() ? raw[i] : nlohmann::json::object();
    n.id = memory_str(n.raw, "id");
    n.text = memory_str(n.raw, "text");
    n.created_at = memory_str(n.raw, "created_at");
    n.decay_anchor = memory_str(n.raw, "decay_anchor");
    n.last_merge_at = memory_str(n.raw, "last_merge_at");
    n.merge_count = memory_int(n.raw, "merge_count");
    n.renew_count = memory_int(n.raw, "renew_count");
    // An entry without a source predates the weight rules. It is read as a
    // person's note: that is the only reading which cannot silently weaken
    // something a user wrote down.
    n.source = memory_str(n.raw, "source") == "agent" ? "agent" : "user";
    double base = 0.0;
    auto weight = n.raw.find("weight_base");
    if (weight != n.raw.end() && weight->is_number()) base = weight->get<double>();
    if (!(base > 0.0)) base = n.source == "user" ? 1.0 : kMemoryAgentWeight;
    if (base > 1.0) base = 1.0;
    n.weight_base = base;
    memory_refresh(&n, now);
    out.push_back(n);
  }
  return out;
}

bool Workspace::save_memory_notes(const std::vector<MemoryNote>& notes,
                                  std::string* err) const {
  nlohmann::json arr = nlohmann::json::array();
  for (const MemoryNote& n : notes) arr.push_back(memory_note_json(n));
  nlohmann::json mem = nlohmann::json::object();
  mem["notes"] = arr;
  mem["updated_at"] = now_iso8601();
  return write_file_atomic(memory_file(), dump_json(mem, 2) + "\n", err);
}

MemoryWrite Workspace::add_memory_note(const std::string& text,
                                       const std::string& source,
                                       std::string* err) const {
  // Read-load-change-write under the process-wide lock: two turns remembering
  // something at the same time must both survive.
  MemoryWrite result;
  StateLock lock(state_mutex());
  const std::string trimmed = trim(text);
  if (trimmed.empty()) {
    if (err) *err = "memory text is empty";
    return result;
  }
  const std::string who = source == "user" ? "user" : "agent";
  const int64_t now = now_ms();
  const std::string now_s = now_iso8601();
  std::vector<MemoryNote> notes = load_memory_notes(nullptr);
  const std::vector<char32_t> incoming = memory_normalized(trimmed);

  std::size_t best = 0;
  double best_score = 0.0;
  bool merge = false;
  for (std::size_t i = 0; i < notes.size(); ++i) {
    const double score = memory_similarity(incoming, memory_normalized(notes[i].text));
    if (score > best_score) {
      best_score = score;
      best = i;
      merge = true;
    }
  }

  std::string target_id;
  if (merge) {
    MemoryNote& note = notes[best];
    if (!note.raw.is_object()) note.raw = nlohmann::json::object();
    nlohmann::json trace = nlohmann::json::object();
    trace["id"] = note.id;
    trace["at"] = now_s;
    bool traced = false;
    if (note.source == "user") {
      // A person's wording wins. An agent restating the note must not rewrite
      // what the user wrote: the restatement is only kept as a trace.
      if (memory_normalized(trimmed) != memory_normalized(note.text)) {
        trace["text"] = trimmed;
        trace["source"] = who;
        traced = true;
      }
    } else {
      trace["text"] = note.text;
      trace["source"] = note.source;
      traced = true;
      note.text = trimmed;
      if (who == "user") note.source = "user";  // what a person writes is permanent
    }
    if (traced) {
      nlohmann::json& history = note.raw["merged_from"];
      if (!history.is_array()) history = nlohmann::json::array();
      history.push_back(trace);
      while (history.size() > kMemoryMergeTraceMax) history.erase(history.begin());
    }
    const double base = who == "user" ? 1.0 : kMemoryAgentWeight;
    note.weight_base = std::min(1.0, std::max(note.weight_base, base) + kMemoryMergeBonus);
    note.merge_count += 1;
    note.last_merge_at = now_s;
    // A confirmation also renews the note - the one thing the absolute age
    // would otherwise never forgive. Capped, and at most once a day: repeating
    // a note is not a way to keep it alive forever.
    if (note.source != "user" && note.renew_count < kMemoryRenewMax) {
      const int64_t anchor = memory_iso_ms(note.decay_anchor);
      if (anchor <= 0 || now - anchor >= kMemoryRenewWindowMs) {
        note.decay_anchor = now_s;
        note.renew_count += 1;
      }
    }
    memory_refresh(&note, now);
    target_id = note.id;
    result.merged_into = note.id;
  } else {
    MemoryNote note;
    note.id = make_id("n_");
    note.text = trimmed;
    note.source = who;
    note.weight_base = who == "user" ? 1.0 : kMemoryAgentWeight;
    note.created_at = now_s;
    note.decay_anchor = now_s;
    note.raw = nlohmann::json::object();
    memory_refresh(&note, now);
    target_id = note.id;
    notes.push_back(note);
  }

  // The caps are enforced after every write, so a hand-edited file that grew
  // past them is corrected by the next note rather than never.
  result.evicted = enforce_memory_caps(&notes, now);

  std::string write_err;
  if (!save_memory_notes(notes, &write_err)) {
    if (err) *err = write_err.empty() ? "cannot write memory file" : write_err;
    result.evicted.clear();
    return result;
  }
  for (const MemoryNote& n : notes) {
    if (n.id != target_id) continue;
    result.note = memory_note_json(n);
    result.note["tier"] = n.tier;
    result.note["weight_effective"] = n.weight_effective;
    break;
  }
  return result;
}

std::vector<nlohmann::json> Workspace::enforce_memory_caps(
    std::vector<MemoryNote>* notes, int64_t now) const {
  std::vector<nlohmann::json> evicted;
  for (MemoryNote& n : *notes) memory_refresh(&n, now);

  auto drop = [&](std::size_t index, const char* reason) {
    const MemoryNote& n = (*notes)[index];
    nlohmann::json e = nlohmann::json::object();
    e["id"] = n.id;
    e["text"] = n.text;
    e["source"] = n.source;
    e["created_at"] = n.created_at;
    e["last_write"] = n.last_write;
    e["weight_base"] = n.weight_base;
    e["weight_effective"] = n.weight_effective;
    e["evicted_at"] = now_iso8601();
    e["reason"] = reason;
    evicted.push_back(e);
    notes->erase(notes->begin() + static_cast<std::ptrdiff_t>(index));
  };

  // Manual notes: the cap is about how many a person is willing to keep, so the
  // one that was confirmed longest ago goes first. A note restated yesterday is
  // still current; one nobody looked at since it was written is not.
  for (;;) {
    std::size_t count = 0;
    std::size_t oldest = 0;
    int64_t oldest_ms = 0;
    for (std::size_t i = 0; i < notes->size(); ++i) {
      if ((*notes)[i].source != "user") continue;
      const int64_t ms = memory_iso_ms((*notes)[i].last_write);
      if (count == 0 || ms < oldest_ms) {
        oldest = i;
        oldest_ms = ms;
      }
      ++count;
    }
    if (count <= kMemoryManualCap) break;
    drop(oldest, "manual_cap");
  }

  // Agent notes: the weakest goes first, oldest among equals - the same order
  // the prompt uses, so what nobody ever sees is what is dropped.
  for (;;) {
    std::size_t count = 0;
    std::size_t weakest = 0;
    double weakest_weight = 0.0;
    int64_t weakest_ms = 0;
    for (std::size_t i = 0; i < notes->size(); ++i) {
      const MemoryNote& n = (*notes)[i];
      if (n.source == "user") continue;
      const int64_t ms = memory_iso_ms(n.last_write);
      const bool worse = count == 0 || n.weight_effective < weakest_weight ||
                         (n.weight_effective == weakest_weight && ms < weakest_ms);
      if (worse) {
        weakest = i;
        weakest_weight = n.weight_effective;
        weakest_ms = ms;
      }
      ++count;
    }
    if (count <= kMemoryAgentCap) break;
    drop(weakest, "agent_cap");
  }

  // The audit goes first: a crash after this line leaves a note that is still
  // in memory.json but already listed as evicted (harmless, and the next
  // eviction logs it again), while losing the line would mean losing the note
  // without a trace.
  if (!evicted.empty()) append_memory_audit(evicted);
  return evicted;
}

bool Workspace::append_memory_audit(const std::vector<nlohmann::json>& evicted) const {
  if (evicted.empty()) return true;
  const std::string path = memory_audit_file();
  std::string existing;
  read_file(path, &existing);
  std::vector<std::string> lines;
  for (const std::string& line : split_lines(existing)) {
    if (!trim(line).empty()) lines.push_back(line);
  }
  for (const nlohmann::json& e : evicted) lines.push_back(dump_json(e));
  if (lines.size() > kMemoryEvictedKeep) {
    lines.erase(lines.begin(), lines.begin() + static_cast<std::ptrdiff_t>(lines.size() - kMemoryEvictedKeep));
  }
  std::string data;
  for (const std::string& line : lines) {
    data += line;
    data += "\n";
  }
  make_dirs(path_parent(path));
  return write_file_atomic(path, data);
}

int Workspace::import_memory_notes(const std::vector<nlohmann::json>& incoming,
                                    std::vector<std::string>* conflicts,
                                    std::string* err) const {
  StateLock lock(state_mutex());
  // The raw notes are kept as they are: a note that came from another device
  // can carry fields this build does not know, and a restore must not drop
  // them on the way in.
  std::vector<nlohmann::json> notes = load_memory(nullptr);
  std::set<std::string> ids;
  for (std::size_t i = 0; i < notes.size(); ++i) {
    const std::string id = memory_str(notes[i], "id");
    if (!id.empty()) ids.insert(id);
  }
  int added = 0;
  for (std::size_t i = 0; i < incoming.size(); ++i) {
    if (!incoming[i].is_object()) continue;
    nlohmann::json note = incoming[i];
    std::string id = memory_str(note, "id");
    if (id.empty()) {
      id = make_id("m");
      note["id"] = id;
    }
    if (ids.count(id) > 0) {
      // The local note wins: it is the one this device has been using.
      if (conflicts != nullptr) conflicts->push_back(id);
      continue;
    }
    ids.insert(id);
    notes.push_back(note);
    ++added;
  }
  if (added == 0) return 0;

  nlohmann::json mem = nlohmann::json::object();
  mem["notes"] = notes;
  mem["updated_at"] = now_iso8601();
  if (!write_file_atomic(memory_file(), dump_json(mem, 2) + "\n", err)) return 0;

  // The caps are the same ones a normal write enforces: too many manual notes
  // lose the oldest confirmation first, too many agent notes lose the
  // weakest. What is dropped lands in the audit.
  std::vector<MemoryNote> loaded = load_memory_notes(nullptr);
  enforce_memory_caps(&loaded, now_ms());
  // Written back in their raw form, so the fields this build does not know
  // stay in the file.
  std::vector<nlohmann::json> kept;
  kept.reserve(loaded.size());
  for (std::size_t i = 0; i < loaded.size(); ++i) {
    kept.push_back(loaded[i].raw.is_object() ? loaded[i].raw : memory_note_json(loaded[i]));
  }
  nlohmann::json mem2 = nlohmann::json::object();
  mem2["notes"] = kept;
  mem2["updated_at"] = now_iso8601();
  if (!write_file_atomic(memory_file(), dump_json(mem2, 2) + "\n", err)) return 0;
  return added;
}

bool Workspace::delete_memory_note(const std::string& note_id, std::string* err) const {
  StateLock lock(state_mutex());
  const std::vector<MemoryNote> notes = load_memory_notes(nullptr);
  std::vector<MemoryNote> kept;
  kept.reserve(notes.size());
  bool removed = false;
  for (const MemoryNote& n : notes) {
    if (n.id == note_id) {
      removed = true;
      continue;
    }
    kept.push_back(n);
  }
  if (!removed) {
    if (err) *err = "memory note not found: " + note_id;
    return false;
  }
  return save_memory_notes(kept, err);
}

MemoryPrompt Workspace::select_memory_for_prompt(const std::string& focus,
                                                 const std::string& seed) const {
  MemoryPrompt out;
  const std::vector<MemoryNote> notes = load_memory_notes(nullptr);
  const std::vector<char32_t> focus_cps = memory_normalized(focus);

  std::vector<std::size_t> manual, strong, medium, weak;
  for (std::size_t i = 0; i < notes.size(); ++i) {
    const std::string& tier = notes[i].tier;
    if (tier == "manual") manual.push_back(i);
    else if (tier == "strong") strong.push_back(i);
    else if (tier == "medium") medium.push_back(i);
    else weak.push_back(i);
  }

  auto by_last_write = [&notes](std::size_t a, std::size_t b) {
    const int64_t am = memory_iso_ms(notes[a].last_write);
    const int64_t bm = memory_iso_ms(notes[b].last_write);
    if (am != bm) return am > bm;
    return notes[a].id < notes[b].id;
  };
  auto by_weight = [&notes, &by_last_write](std::size_t a, std::size_t b) {
    if (notes[a].weight_effective != notes[b].weight_effective) {
      return notes[a].weight_effective > notes[b].weight_effective;
    }
    return by_last_write(a, b);
  };

  std::sort(manual.begin(), manual.end(), by_last_write);
  out.manual = static_cast<int>(manual.size());

  std::sort(strong.begin(), strong.end(), by_weight);
  out.strong_total = static_cast<int>(strong.size());
  if (strong.size() > kMemoryStrongBudget) {
    out.truncated_strong = static_cast<int>(strong.size() - kMemoryStrongBudget);
    strong.resize(kMemoryStrongBudget);
  }
  out.strong = static_cast<int>(strong.size());

  out.medium_total = static_cast<int>(medium.size());
  std::vector<std::pair<double, std::size_t>> matched;
  for (const std::size_t idx : medium) {
    const double relevance = memory_relevance(memory_normalized(notes[idx].text), focus_cps);
    if (relevance >= kMemoryRelevanceMin) matched.push_back(std::make_pair(relevance, idx));
  }
  out.medium_matched = static_cast<int>(matched.size());
  std::vector<std::size_t> chosen_medium;
  if (matched.empty()) {
    // Nothing in this conversation touches the medium notes: fall back to the
    // strongest ones instead of injecting an arbitrary slice.
    std::sort(medium.begin(), medium.end(), by_weight);
    for (std::size_t i = 0; i < medium.size() && i < kMemoryMediumBudget; ++i) {
      chosen_medium.push_back(medium[i]);
    }
  } else {
    std::sort(matched.begin(), matched.end(),
              [&by_weight](const std::pair<double, std::size_t>& a,
                           const std::pair<double, std::size_t>& b) {
                if (a.first != b.first) return a.first > b.first;
                return by_weight(a.second, b.second);
              });
    for (std::size_t i = 0; i < matched.size() && i < kMemoryMediumBudget; ++i) {
      chosen_medium.push_back(matched[i].second);
    }
  }
  out.medium = static_cast<int>(chosen_medium.size());

  out.weak_total = static_cast<int>(weak.size());
  std::vector<std::size_t> chosen_weak;
  {
    // Sorted by id first so the walk is reproducible, then weighted by the
    // effective weight: a note that is only just fading beats one that is
    // nearly gone.
    std::sort(weak.begin(), weak.end(), [&notes](std::size_t a, std::size_t b) {
      return notes[a].id < notes[b].id;
    });
    MemoryRng rng{memory_seed_hash(seed)};
    std::vector<std::size_t> pool = weak;
    while (!pool.empty() && chosen_weak.size() < kMemoryWeakPick) {
      double total = 0.0;
      for (const std::size_t idx : pool) total += notes[idx].weight_effective;
      const bool weighted = total > 0.0;
      if (!weighted) total = static_cast<double>(pool.size());
      const double r =
          (static_cast<double>(rng.next() % 1000000ull) / 1000000.0) * total;
      std::size_t pick = pool.size() - 1;
      double run = 0.0;
      for (std::size_t i = 0; i < pool.size(); ++i) {
        run += weighted ? notes[pool[i]].weight_effective : 1.0;
        if (r < run) {
          pick = i;
          break;
        }
      }
      chosen_weak.push_back(pool[pick]);
      out.weak_ids.push_back(notes[pool[pick]].id);
      pool.erase(pool.begin() + static_cast<std::ptrdiff_t>(pick));
    }
  }
  out.weak = static_cast<int>(chosen_weak.size());

  // The tiers are emitted in a fixed order (manual, strong, medium, weak). It is
  // the order the model should trust them in, and a stable order keeps the
  // prompt prefix cacheable when only one tier changes.
  std::ostringstream body;
  auto emit = [&body, &notes](const char* title, const std::vector<std::size_t>& indexes) {
    if (indexes.empty()) return;
    body << title << "\n";
    for (const std::size_t idx : indexes) {
      body << "- " << memory_bullet(notes[idx].text) << "\n";
    }
  };
  emit("Written down by the user (always in force):", manual);
  emit("Confirmed here:", strong);
  emit("Possibly relevant to this conversation:", chosen_medium);
  emit("Older notes (kept, not confirmed recently):", chosen_weak);
  if (!body.str().empty()) {
    out.text = "\nWorkspace memory (durable notes previously stored for this workspace)\n" +
               body.str();
  }
  return out;
}

std::vector<nlohmann::json> Workspace::recent_evictions(std::size_t max) const {
  std::vector<nlohmann::json> out;
  std::string data;
  if (!read_file(memory_audit_file(), &data)) return out;
  std::vector<nlohmann::json> all;
  for (const std::string& line : split_lines(data)) {
    const std::string trimmed = trim(line);
    if (trimmed.empty()) continue;
    nlohmann::json j = nlohmann::json::parse(trimmed, nullptr, false);
    if (!j.is_discarded()) all.push_back(j);
  }
  const std::size_t start = all.size() > max ? all.size() - max : 0;
  for (std::size_t i = start; i < all.size(); ++i) out.push_back(all[i]);
  return out;
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
