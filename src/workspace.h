// workspace.h -- multi-workspace support.
//
// * Every workspace owns a `.tea_agent_run/` directory that holds its buffer,
//   logs, sessions and memory.
// * `$HOME/.tea_agent/workspaces.json` indexes every known workspace so that a
//   freshly started agent can list them all.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "config.h"
#include "message.h"

namespace tea {

struct WorkspaceEntry {
  std::string id;
  std::string name;
  std::string path;
  std::string created_at;
  std::string last_used_at;
  std::string last_session_id;
  bool exists = false;
  int sessions = 0;
  std::string run_dir;

  nlohmann::json to_json() const;
  static WorkspaceEntry from_json(const nlohmann::json& j);
};

class WorkspaceRegistry {
 public:
  bool load(const std::string& tea_home, std::string* err);
  bool save(std::string* err) const;

  const std::vector<WorkspaceEntry>& entries() const { return entries_; }
  std::vector<WorkspaceEntry>& mutable_entries() { return entries_; }

  WorkspaceEntry* find(const std::string& id);
  const WorkspaceEntry* find(const std::string& id) const;
  WorkspaceEntry* find_by_path(const std::string& path);

  /// Register `path` (creating `.tea_agent_run/`). Returns the entry.
  WorkspaceEntry* add(const std::string& path, const std::string& name, std::string* err);
  bool remove(const std::string& id, std::string* err);

  std::string index_path() const { return index_path_; }
  void refresh_flags();

 private:
  std::string index_path_;
  std::vector<WorkspaceEntry> entries_;
};

class Workspace {
 public:
  /// Layering: `base_settings` (builtin defaults + tea_agent.yaml + CLI) is
  /// overridden by `<run_dir>/settings.yaml`, so a project can pin its own
  /// shell, timeouts and system prompt without touching the global config.
  bool open(const WorkspaceEntry& entry, const Settings& base_settings, std::string* err);

  const WorkspaceEntry& entry() const { return entry_; }
  const std::string& id() const { return entry_.id; }
  const std::string& root() const { return root_; }
  const std::string& run_dir() const { return run_dir_; }
  const Settings& settings() const { return settings_; }
  /// Contents of `<run_dir>/instructions.md` (empty when absent). Stable for the
  /// process lifetime, so it does not destabilise the prompt prefix.
  const std::string& instructions() const { return instructions_; }
  /// Contents of `<run_dir>/system_prompt.md` (empty when absent): the
  /// workspace's replacement for the built-in persona. Read fresh on every call
  /// so an edit from the web UI applies to the next turn without a restart.
  std::string load_system_prompt(bool* custom = nullptr) const;
  /// Replace `<run_dir>/system_prompt.md`. Empty/whitespace-only content clears
  /// the override (the file is truncated), which falls back to settings.yaml's
  /// `system_prompt` and then to the built-in default.
  bool save_system_prompt(const std::string& content, std::string* err) const;
  /// Contents of `<run_dir>/agent.md`: the workspace's accumulated experience,
  /// written by the agent itself (pitfalls, reusable commands/scripts, project
  /// conventions). Read fresh on every call - unlike instructions() it changes
  /// while the process runs, because the agent appends to it mid-turn - and
  /// capped at `max_bytes` (0 => the on-disk limit).
  std::string load_agent_doc(bool* truncated = nullptr) const;
  /// Free bytes on the workspace volume, sampled when the workspace was opened.
  uint64_t free_bytes() const { return free_bytes_; }
  std::string settings_file() const;
  std::string instructions_file() const;
  std::string system_prompt_file() const;
  std::string agent_doc_file() const;
  std::string scripts_dir() const;
  std::string sessions_dir() const;
  std::string memory_file() const;
  std::string log_file() const;
  std::string tmp_dir() const;
  std::string meta_file() const;
  std::string artifacts_dir() const;
  /// Hard cap for `<run_dir>/system_prompt.md`; exposed so the HTTP layer can
  /// reject an oversized edit with the real number.
  std::size_t system_prompt_max_bytes() const;

  /// Map a user/model supplied path to an absolute one, enforcing the
  /// workspace boundary unless allow_outside_workspace is set.
  std::string resolve_path(const std::string& path, std::string* err) const;
  /// Make a path relative to the workspace root when possible (for display).
  std::string display_path(const std::string& absolute) const;

  std::vector<SessionInfo> list_sessions() const;
  bool load_session(const std::string& id, Session* out, std::string* err) const;
  bool save_session(const Session& session, std::string* err) const;
  bool create_session(const std::string& title, Session* out, std::string* err) const;
  bool delete_session(const std::string& id, std::string* err) const;
  bool rename_session(const std::string& id, const std::string& title, std::string* err) const;

  /// Create a new session that starts as a copy of `id`'s transcript through the
  /// end of the turn containing `message_index` (an index into the parent's
  /// messages whose role must be "user"; the assistant/tool messages that
  /// follow it are part of the same turn and are copied too, so the child never
  /// begins mid-turn). `message_index < 0` copies the whole session. The parent
  /// is left untouched, and `out->info.forked_from` / `fork_turn` record the
  /// lineage. The child's title defaults to "分叉 <turn> · <parent title>".
  bool fork_session(const std::string& id, long long message_index, const std::string& title,
                    Session* out, std::string* err) const;

  /// Replace agent.md wholesale. The caller is expected to have compacted it;
  /// a hard byte cap keeps a runaway append from bloating every later prompt.
  bool save_agent_doc(const std::string& content, std::string* err) const;
  /// Append one entry to agent.md, stamped with the current UTC time. Creates
  /// the file (and its template header) when it does not exist yet.
  bool append_agent_doc(const std::string& text, std::string* err) const;

  std::vector<nlohmann::json> load_memory(std::string* err) const;
  nlohmann::json add_memory_note(const std::string& text, std::string* err) const;
  bool delete_memory_note(const std::string& note_id, std::string* err) const;

  std::vector<std::string> tail_log(std::size_t max_lines) const;

  void log(const std::string& level, const std::string& message) const;

  static std::string derive_title(const std::string& text);

 private:
  WorkspaceEntry entry_;
  std::string root_;
  std::string run_dir_;
  Settings settings_;
  std::string instructions_;
  uint64_t free_bytes_ = 0;
};

}  // namespace tea
