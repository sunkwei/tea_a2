// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// workspace.h -- multi-workspace support.
//
// * Every workspace owns a `.tea_agent_run/` directory that holds its buffer,
//   logs, sessions and memory.
// * `$HOME/.tea_agent/workspaces.json` indexes every known workspace so that a
//   freshly started agent can list them all.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "config.h"
#include "history_gc.h"
#include "message.h"
#include "role.h"

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
  /// Can the process really write into this workspace? A workspace can be
  /// perfectly readable and still unusable: a sandbox (or a read-only mount,
  /// or a deny ACE) lets the agent read the project but not create the
  /// session files in `<workspace>/.tea_agent_run/`. The list says so up
  /// front instead of failing on the first request with a raw OS error.
  bool writable = true;
  /// Why it is not writable (empty when it is). Shown to the user verbatim.
  std::string write_error;

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
/// By default the query is a **literal string**: no glob, no fuzzy matching.
/// What the user types is what gets compared, byte for byte (modulo case
/// folding and the `whole` flag below).
///
/// With `regex` on, the query is an ECMAScript regular expression and it is
/// matched against the **content** of the files only - a path is never
/// matched by a pattern. That is the one place where the comparison stops
/// being byte-for-byte, so it is opt-in.
struct SearchQuery {
  std::string text;        // the literal query (or the pattern, when regex is on); empty => nothing to look for
  std::string subpath;     // search only here (workspace-relative); empty => the whole tree
  bool names = true;       // match the path of a file/directory
  bool content = true;     // match the text inside a file
  /// "完全匹配": the whole string has to be equal. For a path that means the
  /// relative path (or the file name) equals the query; for content it means
  /// the whole line equals it. Off by default: a search that only finds the
  /// exact spelling is useless for anything but a known name.
  bool whole = false;
  bool case_sensitive = false;
  /// The query is a regular expression (ECMAScript syntax) instead of a
  /// literal string. It matches file **content** only; names stay literal.
  /// The pattern is limited in length and the number of lines a regex search
  /// may look at is capped, because std::regex has no timeout: a
  /// pathological pattern would otherwise hang the search.
  bool regex = false;
  std::size_t max_results = 100;
  /// Files bigger than this are not read (their path can still match).
  std::size_t max_file_bytes = 2u * 1024u * 1024u;
};

struct SearchResults {
  std::vector<SearchHit> hits;
  std::size_t files_scanned = 0;
  bool truncated = false;      // the result cap hid hits
  bool files_skipped = false;  // some files were too big or binary
  /// Why the search could not run (empty = it ran). A pattern that does not
  /// compile says so here instead of quietly finding nothing.
  std::string error;
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

  /// Point a workspace's runtime directory somewhere else. The runtime data
  /// (sessions, log, memory, agent.md) normally lives inside the project, but
  /// a project the process may not write to is still worth using as a
  /// workspace - then the data lives in the tea home instead. The choice is
  /// written to the index, so it survives a restart.
  bool set_run_dir(const std::string& id, const std::string& dir);

  /// Note that a workspace was used, and remember when.
  ///
  /// The list of workspaces is ordered by "most recently used": the project
  /// you just worked in is the one you want next - not the one you added
  /// first. The index file keeps the registration order (it is the order the
  /// user built the list in), so the stamp is what the order is computed
  /// from.
  ///
  /// Returns false when the id is unknown.
  bool touch(const std::string& id);

  std::string index_path() const { return index_path_; }
  void refresh_flags();

  /// The last failure of save(): the index could not be written. Empty when
  /// the last save worked. A workspace that is not in the index does not
  /// survive a restart, so the HTTP layer reports this instead of quietly
  /// losing the registration.
  const std::string& last_save_error() const { return last_save_error_; }

 private:
  std::string index_path_;
  std::vector<WorkspaceEntry> entries_;
  mutable std::string last_save_error_;
};

/// One memory note with the weight rules applied.
///
/// `weight_base` is what the file stores; `weight_effective` is that number
/// decayed by the note's absolute age (14-day half-life). A note a person wrote
/// down is permanent and always injected; a note the agent wrote fades. The tier
/// decides how it is injected: strong every turn, medium when it matches the
/// conversation, weak as a deterministic sample (see select_memory_for_prompt).
struct MemoryNote {
  nlohmann::json raw;         ///< the entry as the file holds it (unknown fields kept)
  std::string id;
  std::string text;
  std::string source;         ///< "user" (a person wrote it) | "agent"
  std::string created_at;      ///< when it was first written
  std::string decay_anchor;    ///< the age is counted from here (moves on a confirmation)
  std::string last_merge_at;   ///< the last time a similar note confirmed this one
  std::string last_write;      ///< max(created_at, last_merge_at); what eviction orders on
  int merge_count = 0;
  int renew_count = 0;         ///< how often the anchor was reset (capped)
  double weight_base = 0.0;
  double weight_effective = 0.0;
  double age_days = 0.0;
  std::string tier;            ///< manual | strong | medium | weak
};

/// What one memory write did.
struct MemoryWrite {
  nlohmann::json note;                  ///< the stored note (null on error)
  std::string merged_into;              ///< id it was merged into (empty = new note)
  std::vector<nlohmann::json> evicted;  ///< entries the caps dropped
  bool ok() const { return !note.is_null(); }
};

/// The "Workspace memory" block for the system prompt, and how much was left
/// out of it.
struct MemoryPrompt {
  std::string text;
  int manual = 0;
  int strong = 0;
  int strong_total = 0;
  int truncated_strong = 0;
  int medium = 0;
  int medium_total = 0;
  int medium_matched = 0;
  int weak = 0;
  int weak_total = 0;
  std::vector<std::string> weak_ids;
};

/// The four memory layers (docs/memory-design.md).
///
///   L1 conversation - the sessions and their trajectories
///   L2 journal      - journal/YYYY-MM-DD.md, one page per day
///   L3 knowledge    - knowledge/<slug>.md, one page per thing understood
///   L4 index        - knowledge/index.md, the catalog of L3
///
/// One search covers all of them. The layers differ in where the text lives,
/// not in how it is matched: the match is literal, byte for byte, because
/// "the words that were actually written" is the only thing a search can
/// honestly offer - and a rewritten note is not the same note.
struct MemoryLayerQuery {
  std::string text;                   ///< the literal query; empty => browse by time
  std::vector<std::string> kinds;     ///< conversation | journal | knowledge; empty => all
  std::string from;                   ///< ISO-8601, inclusive
  std::string to;                     ///< ISO-8601, exclusive
  std::string source_id;              ///< session id, journal file name or page slug
  std::string order;                  ///< "newest" (default) | "oldest"
  std::size_t limit = 8;
  std::string cursor;                 ///< where the last search stopped
  bool case_sensitive = true;         ///< the match is literal
};

/// One hit: where it is, when it was, and a short excerpt of it.
struct MemoryLayerItem {
  std::string ref;    ///< conv:<sid>#m<idx> | journal:<file>#c<off> | knowledge:<slug>#c<off>
  std::string kind;   ///< conversation | journal | knowledge
  std::string time;   ///< ISO-8601 UTC
  std::string title;  ///< the page title, or the session title
  std::string who;    ///< "user" | "assistant" (empty for the written layers)
  std::string text;   ///< the excerpt
  std::string source; ///< the session id, the file name or the slug
};

/// What one search found - and what it could not read.
struct MemoryLayerResults {
  std::vector<MemoryLayerItem> items;
  std::string next_cursor;
  bool partial = false;               ///< some source could not be read
  std::vector<std::string> failures;  ///< what could not be read
  bool truncated = false;             ///< the caps hid results
  std::string error;                  ///< why the search could not run
};

/// The catalog of the knowledge pages: one line per page.
struct KnowledgeCatalog {
  std::string text;
  int pages = 0;   ///< how many pages there are
  int listed = 0;  ///< how many made it into the text
};

/// The original text behind a ref, or a knowledge page and its relations.
struct MemoryLayerRead {
  std::string ref;
  std::string kind;
  std::string title;
  std::string time;
  std::string who;
  std::string content;
  std::size_t offset = 0;
  std::size_t total_chars = 0;
  bool has_next = false;
  nlohmann::json related;  ///< the links of a knowledge page
  bool ok = false;
  std::string error;
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
  /// The directory the workspace's roles live in: <run_dir>/roles.
  std::string roles_dir() const;
  /// The file of one role: <run_dir>/roles/<id>.yaml.
  std::string role_file(const std::string& id) const;
  /// The roles of this workspace: the files in `roles/` **and** the built-in
  /// templates that ship with the agent. A file with the same id as a built-in
  /// wins (it is the user's own version of it). A file that cannot be parsed
  /// is skipped and reported by the set, so one broken role file does not
  /// hide the others.
  RoleSet load_roles() const;
  /// Is this role a file of this workspace (as opposed to a built-in
  /// template that has not been copied yet)?
  bool role_has_file(const std::string& id) const;
  /// The role this workspace is in right now (its id), or "" when there is
  /// none. Read fresh from settings.yaml: the pointer is what the user
  /// switches, and the switch has to reach the next turn without reopening
  /// the workspace.
  std::string current_role_id() const;
  /// Point the workspace at a role ("" clears it). The pointer is written to
  /// <run_dir>/settings.yaml as `role:`.
  bool set_current_role(const std::string& id, std::string* err);
  std::string sessions_dir() const;
  /// The journal directory (L2): one page per day.
  std::string journal_dir() const;
  /// The knowledge directory (L3 and L4): one page per thing understood, and
  /// the catalog of them.
  std::string knowledge_dir() const;
  /// Search all four layers at once. The match is literal (byte for byte);
  /// `query` empty means "browse by time".
  MemoryLayerResults memory_find_layers(const MemoryLayerQuery& query) const;
  /// Read the original text behind a ref (the text, not a summary).
  MemoryLayerRead memory_read_layer(const std::string& ref, std::size_t offset,
                                    std::size_t limit) const;
  /// Open a knowledge page by its name, with the pages it links to and the
  /// pages that link to it.
  MemoryLayerRead knowledge_open(const std::string& title, std::size_t offset,
                                 std::size_t limit) const;
  /// The catalog of the knowledge pages (L4): one line per page, the ones
  /// that matter for this conversation first. `max_lines` is the budget;
  /// what did not fit is reported, not silently dropped.
  KnowledgeCatalog knowledge_catalog(const std::string& focus,
                                     std::size_t max_lines) const;
  /// The knowledge pages of this workspace: one file per thing understood.
  std::vector<nlohmann::json> knowledge_pages() const;
  /// Read one page. The slug is the file name without the extension.
  bool knowledge_read(const std::string& slug, std::string* content,
                      std::string* title, std::string* err) const;
  /// Write (or create) one page. The slug is the file name without the
  /// extension; the content is Markdown.
  bool knowledge_write(const std::string& slug, const std::string& content,
                       std::string* err) const;
  /// Delete one page.
  bool knowledge_delete(const std::string& slug, std::string* err) const;
  /// The days that have a journal page (L2), newest first.
  std::vector<nlohmann::json> journal_days() const;
  /// Read one day's page. The date is `YYYY-MM-DD`.
  bool journal_read(const std::string& date, std::string* content, std::string* err) const;
  /// Write one day's page.
  bool journal_write(const std::string& date, const std::string& content,
                     std::string* err) const;
  std::string memory_file() const;
  /// Where an evicted note is kept (`memory/evicted.jsonl`): the note itself is
  /// gone from memory.json, but a person can still see what was dropped.
  std::string memory_audit_file() const;
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

  /// Set one string key in `<run_dir>/settings.yaml`. The value is written
  /// YAML-quoted, so an id with a colon or a hash in it stays a string.
  bool write_setting_string(const std::string& key, const std::string& value, std::string* err);

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
  /// Clean the history of this workspace: drop what is too old, gzip what is
  /// cold, and - when the run_dir is over its budget - drop the oldest
  /// sessions until it fits. `report` says what happened (or, with
  /// `limits.dry_run`, what would happen).
  bool gc_history(const HistoryGcLimits& limits, HistoryGcReport* report, std::string* err) const;
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

  /// Is agent.md still the untouched template?
  ///
  /// A fresh workspace gets one, and it says nothing - so a restore may put
  /// the other device's experience in its place without losing anything. A
  /// file with content of its own is not pristine.
  bool agent_doc_is_pristine() const;
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
  /// The notes with the weight rules applied (effective weight, tier, age).
  std::vector<MemoryNote> load_memory_notes(std::string* err) const;
  /// Add a note for `source`: "user" is what a person wrote down and is
  /// permanent; anything else is the agent's ("agent"). A similar existing
  /// note is merged into instead of duplicated, then the caps are enforced
  /// (20 manual / 100 agent, oldest confirmation first / weakest first).
  MemoryWrite add_memory_note(const std::string& text, const std::string& source,
                              std::string* err) const;
  bool delete_memory_note(const std::string& note_id, std::string* err) const;
  /// Merge notes that came from another device into this workspace's memory.
  ///
  /// A note whose id is already here is a **conflict**: the local one wins and
  /// the other device's version is dropped (not merged in - a restore must not
  /// rewrite what this device has been using). The caps are enforced
  /// afterwards, exactly as when a note is added here, and whatever is evicted
  /// lands in the audit.
  ///
  /// Returns how many notes were added; `conflicts` (optional) gets the ids
  /// that were kept.
  int import_memory_notes(const std::vector<nlohmann::json>& notes,
                          std::vector<std::string>* conflicts, std::string* err) const;
  /// The memory block of the system prompt: manual notes first, then strong,
  /// medium (matched against `focus`) and a deterministic sample of the weak
  /// ones. `seed` is stable for a whole UTC day, so a re-run sees the same
  /// sample and the prompt prefix does not churn every turn.
  MemoryPrompt select_memory_for_prompt(const std::string& focus,
                                        const std::string& seed) const;
  /// The newest eviction-audit entries, oldest first (max `max`).
  std::vector<nlohmann::json> recent_evictions(std::size_t max) const;

  std::vector<std::string> tail_log(std::size_t max_lines) const;

  void log(const std::string& level, const std::string& message) const;

  static std::string derive_title(const std::string& text);

 private:
  /// The shared body of write_setting_bool / write_setting_string: a targeted
  /// line edit in `<run_dir>/settings.yaml` (the file belongs to the operator,
  /// so comments, key order and quoting survive byte for byte).
  bool write_setting_line(const std::string& key, const std::string& value_literal,
                          std::string* err);

  bool save_memory_notes(const std::vector<MemoryNote>& notes, std::string* err) const;
  /// Drop the excess notes (manual > 20, agent > 100) and write the audit.
  std::vector<nlohmann::json> enforce_memory_caps(std::vector<MemoryNote>* notes,
                                                  int64_t now) const;
  bool append_memory_audit(const std::vector<nlohmann::json>& evicted) const;

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
