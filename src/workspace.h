// workspace.h -- multi-workspace support.
//
// * Every workspace owns a `.tea_agent_run/` directory that holds its buffer,
//   logs, sessions and memory.
// * `$HOME/.tea_agent/workspaces.json` indexes every known workspace so that a
//   freshly started agent can list them all.
#pragma once

#include <cstddef>
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

/// One hit of a workspace search.
struct SearchHit {
  std::string path;    // workspace-relative, forward slashes
  std::string name;    // the file/directory name
  bool is_dir = false;
  uint64_t size = 0;
  std::string mtime;   // ISO-8601 UTC
  int hits = 0;        // content hits (0 when only the path matched)
  bool name_hit = false;  // the path matched the query
  std::vector<int> lines;      // 1-based line numbers of the content hits
  std::vector<std::string> samples;  // the matching lines (trimmed, capped)
  std::string preview;         // the first matching line, trimmed

  nlohmann::json to_json() const;
};

/// What to look for, and how.
///
/// The query is a **literal string**: no regular expression, no glob, no
/// fuzzy matching. What the user types is what gets compared, byte for byte
/// (modulo case folding and the `whole` flag below).
struct SearchQuery {
  std::string text;        // the literal query; empty => nothing to look for
  std::string subpath;     // search only here (workspace-relative); empty => the whole tree
  bool names = true;       // match the path of a file/directory
  bool content = true;     // match the text inside a file
  /// "完全匹配": the whole string has to be equal. For a path that means the
  /// relative path (or the file name) equals the query; for content it means
  /// the whole line equals it. Off by default: a search that only finds the
  /// exact spelling is useless for anything but a known name.
  bool whole = false;
  bool case_sensitive = false;
  std::size_t max_results = 100;
  /// Files bigger than this are not read (their path can still match).
  std::size_t max_file_bytes = 2u * 1024u * 1024u;
};

struct SearchResults {
  std::vector<SearchHit> hits;
  std::size_t files_scanned = 0;
  bool truncated = false;      // the result cap hid hits
  bool files_skipped = false;  // some files were too big or binary
};

/// One place inside a conversation where the query was found.
struct HistorySnippet {
  /// "title" | "message" | "summary" | "tool_call" | "trajectory"
  std::string kind;
  std::string role;   // "user" | "assistant" | "tool" (empty for the others)
  int index = -1;     // index of the message in the transcript (-1 = none)
  std::string tool;   // the tool name (a tool call or a trajectory step)
  std::string text;   // the matching fragment, with the context around it

  nlohmann::json to_json() const;
};

/// One session of the workspace's history that matched.
struct HistoryHit {
  std::string session_id;
  std::string title;
  std::string created_at;
  std::string updated_at;
  int message_count = 0;
  int hits = 0;
  bool title_hit = false;
  std::vector<HistorySnippet> snippets;

  nlohmann::json to_json() const;
};

/// What to look for in the history, and where.
struct HistoryQuery {
  std::string text;         // the literal query; empty => nothing to look for
  bool titles = true;       // match the session title
  bool messages = true;     // match the transcript (what was said, thought, called)
  bool trajectory = true;   // match the trajectory (tool calls and their output)
  /// "完全匹配": the whole string has to be equal (the whole title, or one
  /// whole line of the message).
  bool whole = false;
  bool case_sensitive = false;
  std::size_t max_results = 50;
  std::size_t max_sessions = 200;      // how many sessions are read
  std::size_t max_bytes_per_session = 4u * 1024u * 1024u;
  std::size_t max_snippets = 5;        // per session
};

struct HistoryResults {
  std::vector<HistoryHit> hits;
  std::size_t sessions_scanned = 0;
  bool truncated = false;        // the caps hid sessions
  bool sessions_skipped = false;  // some sessions were too big to read
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

  /// Set one boolean key in `<run_dir>/settings.yaml`, then reload that file into
  /// this object's settings. A targeted line edit rather than a YAML round-trip:
  /// the file belongs to the operator, so comments, key order, quoting and the
  /// rest of the document survive byte for byte. Creates the file (with a
  /// `settings:` block) when it does not exist yet. Refuses to touch a file it
  /// cannot parse - silently rewriting a broken settings.yaml would hide the
  /// reason the workspace came up with the wrong settings.
  bool write_setting_bool(const std::string& key, bool value, std::string* err);

  /// Map a user/model supplied path to an absolute one, enforcing the
  /// workspace boundary unless allow_outside_workspace is set.
  std::string resolve_path(const std::string& path, std::string* err) const;

  /// Extra directories this turn may touch, on top of the workspace root.
  ///
  /// The boundary is normally the workspace: the file tools refuse anything
  /// outside it. A caller that drives its own policy (the task queue does) can
  /// say "this turn may also work in these directories" - the list belongs to
  /// the Workspace object, and a Workspace is one turn's copy, so the grant
  /// lasts exactly that turn. Empty => the workspace root is the only place
  /// the file tools may reach.
  void set_allowed_roots(const std::vector<std::string>& roots);
  const std::vector<std::string>& allowed_roots() const { return allowed_roots_; }
  /// Make a path relative to the workspace root when possible (for display).
  std::string display_path(const std::string& absolute) const;

  std::vector<SessionInfo> list_sessions() const;
  bool load_session(const std::string& id, Session* out, std::string* err) const;
  /// Write the whole session (transcript, summary, artifacts). The task list
  /// is NOT part of it - see save_todos.
  bool save_session(const Session& session, std::string* err) const;
  /// Write the task list of a session - and only that.
  ///
  /// The task list has its own write path on purpose: a turn holds its own copy
  /// of the session and writes the whole file back when it ends, so a list that
  /// somebody else changed meanwhile (the browser ticking an item off, a script
  /// writing the list) would be silently overwritten. The list therefore lives
  /// outside the session save, and whoever changes it writes it here.
  bool save_todos(const std::string& session_id, const std::vector<TodoItem>& todos,
                  std::string* err) const;
  /// Read the task list as it is on disk - the freshest one. A turn holds its
  /// own copy of the list, and somebody else (the browser ticking an item
  /// off, a script writing the list) may have changed it meanwhile.
  bool load_todos(const std::string& session_id, std::vector<TodoItem>* out,
                  std::string* err) const;
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

  /// Record a file this session produced (the tools call this). Internal paths
  /// (`.tea_agent_run/...`), uninteresting extensions and empty files are
  /// ignored; a path already listed keeps its first turn and gets the new size.
  /// Returns true when the list changed.
  bool record_artifact(Session* session, const std::string& relative_path,
                       const std::string& tool, int turn) const;
  /// Files under the workspace whose mtime is at or after `since_ms` (used after
  /// a command, which cannot know what it wrote). Build noise is excluded.
  /// Bounded: depth, entries visited and results are all capped, and the usual
  /// noise directories are skipped. `truncated` (optional) is set when the
  /// result cap hid files - the UI has to say so instead of pretending the
  /// list is complete.
  std::vector<std::string> recent_artifacts(long long since_ms, bool* truncated = nullptr) const;

  /// Search the workspace for a literal string (see SearchQuery).
  ///
  /// The walk is bounded (depth, files visited, bytes read, results) and never
  /// leaves the workspace root. `SearchResults::truncated` says when the cap
  /// hid hits, so the caller can say "and more" instead of pretending the
  /// list is the whole truth.
  SearchResults search(const SearchQuery& query) const;

  /// Search the workspace's **history**: the conversations that happened here
  /// (see HistoryQuery). Titles, what was said, what was thought, what was
  /// called - and the trajectory, which is where a command's output lives.
  ///
  /// The read is bounded (sessions, bytes, snippets) and `HistoryResults`
  /// says when a cap hid something.
  HistoryResults search_history(const HistoryQuery& query) const;

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
  /// Per-turn extra roots for the file tools (see set_allowed_roots).
  std::vector<std::string> allowed_roots_;
  Settings settings_;
  std::string instructions_;
  uint64_t free_bytes_ = 0;
};

}  // namespace tea
