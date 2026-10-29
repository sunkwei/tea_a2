#include "tools.h"

#include "output_filter.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <sstream>

#include "command.h"
#include "http.h"
#include "log.h"
#include "mcp.h"
#include "util.h"

namespace tea {
namespace {

std::string arg_str(const nlohmann::json& args, const char* key, const std::string& def = "") {
  if (!args.is_object()) return def;
  auto it = args.find(key);
  if (it == args.end() || it->is_null()) return def;
  if (it->is_string()) return it->get<std::string>();
  if (it->is_number() || it->is_boolean()) return it->dump();
  return def;
}

long long arg_int(const nlohmann::json& args, const char* key, long long def) {
  if (!args.is_object()) return def;
  auto it = args.find(key);
  if (it == args.end() || it->is_null()) return def;
  if (it->is_number()) return it->get<long long>();
  if (it->is_string()) {
    try {
      return std::stoll(it->get<std::string>());
    } catch (const std::exception&) {
      return def;
    }
  }
  return def;
}

double arg_double(const nlohmann::json& args, const char* key, double def) {
  if (!args.is_object()) return def;
  auto it = args.find(key);
  if (it == args.end() || it->is_null()) return def;
  if (it->is_number()) return it->get<double>();
  if (it->is_string()) {
    try {
      return std::stod(it->get<std::string>());
    } catch (const std::exception&) {
      return def;
    }
  }
  return def;
}

bool arg_bool(const nlohmann::json& args, const char* key, bool def) {
  if (!args.is_object()) return def;
  auto it = args.find(key);
  if (it == args.end() || it->is_null()) return def;
  if (it->is_boolean()) return it->get<bool>();
  return def;
}

std::string rel_or_abs(const Workspace& ws, const std::string& absolute) {
  return ws.display_path(absolute);
}

std::string limit_output(const std::string& text, std::size_t limit, bool* truncated) {
  if (limit == 0 || text.size() <= limit) {
    if (truncated) *truncated = false;
    return text;
  }
  if (truncated) *truncated = true;
  return truncate_utf8(text, limit) + "\n... [output truncated at " + std::to_string(limit) +
         " bytes]";
}

// ---------------------------------------------------------------------------
// toolkit_exec
// ---------------------------------------------------------------------------
ToolResult tool_exec(const nlohmann::json& args, const ToolContext& ctx) {
  ToolResult result;
  const std::string command = arg_str(args, "command");
  if (trim(command).empty()) {
    result.output = "error: 'command' is required";
    return result;
  }
  if (!ctx.workspace || !ctx.config) {
    result.output = "error: no workspace is active";
    return result;
  }

  CommandOptions options;
  options.command = command;
  // The trajectory records the command (never a guessed path list) as what this
  // tool touched; `command` is also the only thing an exec result can honestly
  // report.
  result.meta["command"] = command;
  options.tmp_dir = ctx.workspace->tmp_dir();
  options.shell = arg_str(args, "shell", ctx.workspace->settings().shell);
  options.timeout_ms = static_cast<int>(arg_int(args, "timeout_ms", ctx.workspace->settings().command_timeout_ms));
  if (options.timeout_ms < 1000) options.timeout_ms = 1000;
  options.max_output_bytes = static_cast<std::size_t>(ctx.workspace->settings().max_tool_output);
  options.utf8_console = ctx.workspace->settings().utf8_console;
  // The command can be interrupted: the user asking to stop the turn must not
  // have to wait for a long build to finish.
  options.cancel = ctx.cancel;

  const std::string cwd_arg = arg_str(args, "cwd");
  if (!cwd_arg.empty()) {
    std::string err;
    const std::string resolved = ctx.workspace->resolve_path(cwd_arg, &err);
    if (resolved.empty()) {
      result.output = "error: " + err;
      return result;
    }
    options.cwd = resolved;
  } else {
    options.cwd = ctx.workspace->root();
  }

  LOG_INFO("toolkit_exec: ", sanitize_for_terminal(command));
  const CommandResult run = run_command(options);

  std::ostringstream out;
  if (!run.ok) {
    out << "error: could not start the command: " << run.error;
    result.output = out.str();
    return result;
  }
  out << "[shell: " << run.shell_used << "]";
  if (run.cancelled) out << " [interrupted by the user]";
  if (run.timed_out) out << " [timed out after " << options.timeout_ms << "ms]";
  out << " [exit code: " << run.exit_code << "]\n";
  if (run.output.empty()) {
    out << "(no output)";
  } else {
    out << run.output;
  }

  result.ok = (run.exit_code == 0) && !run.timed_out && !run.cancelled;
  result.truncated = run.output_truncated;

  // Reduce the output according to what it is: a build log keeps its diagnostics
  // and loses the progress noise, everything else keeps head and tail.
  const Settings& st = ctx.workspace->settings();
  OutputFilterOptions fopts;
  fopts.max_bytes = static_cast<std::size_t>(st.max_tool_output);
  if (st.tool_output_max_lines > 0) {
    fopts.max_lines = static_cast<std::size_t>(st.tool_output_max_lines);
  }
  const FilteredOutput filtered = filter_tool_output(command, out.str(), fopts);
  result.output = filtered.text;
  result.truncated = result.truncated || filtered.changed;
  if (filtered.changed) {
    result.meta["output_filter"] = filtered.strategy;
    result.meta["dropped_lines"] = static_cast<long long>(filtered.dropped_lines);
    LOG_DEBUG("toolkit_exec: output reduced (", filtered.strategy, "), dropped ",
              filtered.dropped_lines, " lines");
  }
  if (run.cancelled) {
    // An interrupted command is not a failed command: it may have done half of
    // its work. Say so, so the next turn does not assume the work is done -
    // and does not assume it is not done either.
    result.cancelled = true;
    result.output =
        "The command was interrupted by the user after " + std::to_string(run.elapsed_ms) +
        "ms. It may only have run part of the way: whatever it already wrote or changed is "
        "still there, and the rest did not happen.\n" + result.output;
    result.meta["cancelled"] = true;
  }
  result.meta["exit_code"] = run.exit_code;
  result.meta["timed_out"] = run.timed_out;
  result.meta["elapsed_ms"] = run.elapsed_ms;
  return result;
}

// ---------------------------------------------------------------------------
// toolkit_file
// ---------------------------------------------------------------------------
ToolResult tool_read(const std::string& path, const ToolContext& ctx, const nlohmann::json& args) {
  ToolResult result;
  if (!ctx.workspace || !ctx.config) {
    result.output = "error: no workspace is active";
    return result;
  }
  if (!path_is_file(path)) {
    result.output = path_exists(path) ? "error: not a regular file: " + path
                                      : "error: file not found: " + path;
    return result;
  }
  const long long requested = arg_int(args, "max_bytes", 0);
  std::size_t limit = requested > 0 ? static_cast<std::size_t>(requested)
                                    : static_cast<std::size_t>(ctx.workspace->settings().max_tool_output);
  std::string content;
  bool truncated = false;
  if (!read_file(path, &content, limit, &truncated)) {
    result.output = "error: cannot read " + path;
    return result;
  }
  result.ok = true;
  result.truncated = truncated;
  result.output = content;
  if (truncated) {
    result.output += "\n... [file truncated at " + std::to_string(limit) + " bytes; total " +
                     std::to_string(file_size(path)) + " bytes]";
  }
  result.meta["size"] = file_size(path);
  result.meta["truncated"] = truncated;
  return result;
}

ToolResult tool_write(const std::string& path, const ToolContext& ctx, const nlohmann::json& args,
                      bool append) {
  ToolResult result;
  if (!ctx.workspace) {
    result.output = "error: no workspace is active";
    return result;
  }
  const std::string content = arg_str(args, "content");
  std::string err;
  const std::string parent = path_parent(path);
  if (!parent.empty() && !make_dirs(parent)) {
    result.output = "error: cannot create directory " + parent;
    return result;
  }
  if (path_exists(path) && path_is_dir(path)) {
    result.output = "error: target is a directory: " + path;
    return result;
  }
  if (!write_file(path, content, append, &err)) {
    result.output = "error: " + err;
    return result;
  }
  result.ok = true;
  const std::string verb = append ? "appended" : "wrote";
  result.output = verb + " " + std::to_string(content.size()) + " bytes to " +
                  rel_or_abs(*ctx.workspace, path) + " (" + std::to_string(file_size(path)) +
                  " bytes total)";
  result.meta["bytes"] = static_cast<long long>(content.size());
  result.meta["size"] = file_size(path);
  // Structured attribution for the turn trajectory: the exact path the tool
  // wrote and the byte count it reported, not something inferred later.
  result.meta["file_op"] = append ? "append" : "write";
  result.meta["path"] = rel_or_abs(*ctx.workspace, path);
  return result;
}

ToolResult tool_list(const std::string& path, const ToolContext& ctx) {
  ToolResult result;
  if (!path_is_dir(path)) {
    result.output = path_exists(path) ? "error: not a directory: " + path
                                      : "error: directory not found: " + path;
    return result;
  }
  std::vector<DirEntryInfo> entries;
  std::string err;
  if (!list_directory(path, &entries, &err)) {
    result.output = "error: " + err;
    return result;
  }
  std::ostringstream out;
  out << entries.size() << " entr" << (entries.size() == 1 ? "y" : "ies") << " in "
      << (ctx.workspace ? rel_or_abs(*ctx.workspace, path) : path) << "\n";
  for (std::size_t i = 0; i < entries.size(); ++i) {
    const DirEntryInfo& e = entries[i];
    char line[512];
    if (e.is_dir) {
      std::snprintf(line, sizeof(line), "[dir ]          %s", e.name.c_str());
    } else {
      std::snprintf(line, sizeof(line), "[file] %9llu  %s", static_cast<unsigned long long>(e.size),
                    e.name.c_str());
    }
    out << line << "\n";
  }
  std::size_t limit =
      ctx.workspace ? static_cast<std::size_t>(ctx.workspace->settings().max_tool_output) : 131072u;
  bool truncated = false;
  result.output = limit_output(out.str(), limit, &truncated);
  result.truncated = truncated;
  result.ok = true;
  result.meta["count"] = static_cast<long long>(entries.size());
  return result;
}

ToolResult tool_stat(const std::string& path) {
  ToolResult result;
  if (!path_exists(path)) {
    result.output = "error: path not found: " + path;
    return result;
  }
  nlohmann::json j = nlohmann::json::object();
  j["path"] = path;
  j["type"] = path_is_dir(path) ? "dir" : "file";
  j["size"] = file_size(path);
  j["mtime"] = file_mtime_iso(path);
  result.output = dump_json(j, 2);
  result.ok = true;
  return result;
}

ToolResult tool_file(const nlohmann::json& args, const ToolContext& ctx) {
  ToolResult result;
  if (!ctx.workspace) {
    result.output = "error: no workspace is active";
    return result;
  }
  const std::string op = to_lower(trim(arg_str(args, "op")));
  const std::string raw_path = arg_str(args, "path");
  if (op.empty()) {
    result.output = "error: 'op' is required (read|write|append|list|mkdir|delete|move|stat)";
    return result;
  }
  if (raw_path.empty()) {
    result.output = "error: 'path' is required";
    return result;
  }
  std::string err;
  const std::string path = ctx.workspace->resolve_path(raw_path, &err);
  if (path.empty()) {
    result.output = "error: " + err;
    return result;
  }

  if (op == "read") return tool_read(path, ctx, args);
  if (op == "write") return tool_write(path, ctx, args, false);
  if (op == "append") return tool_write(path, ctx, args, true);
  if (op == "list") return tool_list(path, ctx);
  if (op == "stat") return tool_stat(path);

  if (op == "mkdir") {
    if (path_exists(path)) {
      result.ok = path_is_dir(path);
      result.output = result.ok ? "directory already exists: " + rel_or_abs(*ctx.workspace, path)
                                : "error: a file with that name exists: " + path;
      return result;
    }
    if (!make_dirs(path)) {
      result.output = "error: cannot create directory " + path;
      return result;
    }
    result.ok = true;
    result.output = "created directory " + rel_or_abs(*ctx.workspace, path);
    return result;
  }

  if (op == "delete") {
    if (!path_exists(path)) {
      result.output = "error: path not found: " + path;
      return result;
    }
    if (path_is_within(ctx.workspace->root(), path) && path != ctx.workspace->root()) {
      result.output = "error: refusing to delete a directory that contains the workspace root";
      return result;
    }
    const bool recursive = arg_bool(args, "recursive", false);
    if (path_is_dir(path) && !recursive) {
      std::vector<DirEntryInfo> entries;
      std::string list_err;
      if (list_directory(path, &entries, &list_err) && !entries.empty()) {
        result.output = "error: directory is not empty; pass recursive=true to delete it";
        return result;
      }
    }
    if (!remove_path(path, path_is_dir(path) && recursive, &err)) {
      result.output = "error: cannot delete " + path + ": " + err;
      return result;
    }
    result.ok = true;
    result.output = "deleted " + rel_or_abs(*ctx.workspace, path);
    result.meta["file_op"] = "delete";
    result.meta["path"] = rel_or_abs(*ctx.workspace, path);
    return result;
  }

  if (op == "move") {
    const std::string raw_dest = arg_str(args, "dest");
    if (raw_dest.empty()) {
      result.output = "error: 'dest' is required for op=move";
      return result;
    }
    const std::string dest = ctx.workspace->resolve_path(raw_dest, &err);
    if (dest.empty()) {
      result.output = "error: " + err;
      return result;
    }
    if (!path_exists(path)) {
      result.output = "error: path not found: " + path;
      return result;
    }
    if (!move_path(path, dest, &err)) {
      result.output = "error: " + err;
      return result;
    }
    result.ok = true;
    result.output = "moved " + rel_or_abs(*ctx.workspace, path) + " -> " +
                    rel_or_abs(*ctx.workspace, dest);
    result.meta["file_op"] = "move";
    result.meta["path"] = rel_or_abs(*ctx.workspace, path);
    result.meta["dest"] = rel_or_abs(*ctx.workspace, dest);
    return result;
  }

  result.output = "error: unsupported op '" + op + "'";
  return result;
}

// ---------------------------------------------------------------------------
// toolkit_workspace_search
//
// A literal-string search over the workspace: file names and file content.
// The query is compared byte for byte - no regular expression, no glob, no
// fuzzy matching. `exact` switches to whole-string equality (完全匹配).
// ---------------------------------------------------------------------------
ToolResult tool_workspace_search(const nlohmann::json& args, const ToolContext& ctx) {
  ToolResult result;
  if (!ctx.workspace) {
    result.output = "error: no workspace is active";
    return result;
  }
  const std::string query_text = trim(arg_str(args, "query"));
  if (query_text.empty()) {
    result.output = "error: 'query' is required";
    return result;
  }

  SearchQuery query;
  query.text = query_text;
  query.subpath = trim(arg_str(args, "path"));
  const std::string scope = to_lower(trim(arg_str(args, "scope", "both")));
  if (scope == "name" || scope == "names" || scope == "path") {
    query.names = true;
    query.content = false;
  } else if (scope == "content" || scope == "text" || scope == "body") {
    query.names = false;
    query.content = true;
  } else if (scope == "both" || scope.empty()) {
    query.names = true;
    query.content = true;
  } else {
    // An unknown scope is a mistake, not something to guess: say so instead of
    // quietly searching something else than what was asked for.
    result.output = "error: scope must be one of: name, content, both (got '" + scope + "')";
    return result;
  }
  query.whole = arg_bool(args, "exact", false);
  query.case_sensitive = arg_bool(args, "case_sensitive", false);
  const long long wanted = arg_int(args, "max_results", 50);
  if (wanted > 0) {
    query.max_results = static_cast<std::size_t>(wanted > 200 ? 200 : wanted);
  }

  const SearchResults found = ctx.workspace->search(query);
  std::ostringstream out;
  out << found.hits.size() << (found.hits.size() == 1 ? " match" : " matches") << " for \""
      << query_text << "\" in " << ctx.workspace->root() << " (scope="
      << (query.names && query.content ? "both" : (query.names ? "name" : "content"))
      << ", " << (query.whole ? "exact" : "substring") << ", "
      << (query.case_sensitive ? "case-sensitive" : "case-insensitive") << "; scanned "
      << found.files_scanned << " files)";
  if (found.truncated) out << "\n[more results exist: the search hit its limit]";
  if (found.files_skipped) out << "\n[some files were skipped: too big or binary]";
  for (std::size_t i = 0; i < found.hits.size(); ++i) {
    const SearchHit& hit = found.hits[i];
    out << "\n\n" << hit.path;
    if (hit.hits > 0) out << "  (" << hit.hits << (hit.hits == 1 ? " line)" : " lines)");
    for (std::size_t k = 0; k < hit.samples.size(); ++k) {
      const int line_no = (k < hit.lines.size()) ? hit.lines[k] : 0;
      out << "\n  " << line_no << ": " << hit.samples[k];
    }
  }
  result.ok = true;
  result.meta["matches"] = static_cast<long long>(found.hits.size());
  result.meta["truncated"] = found.truncated;
  std::size_t limit =
      ctx.workspace ? static_cast<std::size_t>(ctx.workspace->settings().max_tool_output) : 131072u;
  bool truncated = false;
  result.output = limit_output(out.str(), limit, &truncated);
  result.truncated = truncated;
  return result;
}

// ---------------------------------------------------------------------------
// toolkit_memory
// ---------------------------------------------------------------------------
ToolResult tool_memory(const nlohmann::json& args, const ToolContext& ctx) {
  ToolResult result;
  if (!ctx.workspace) {
    result.output = "error: no workspace is active";
    return result;
  }
  const std::string op = to_lower(trim(arg_str(args, "op", "list")));
  std::string err;

  if (op == "add") {
    const nlohmann::json note = ctx.workspace->add_memory_note(arg_str(args, "text"), &err);
    if (note.is_null()) {
      result.output = "error: " + err;
      return result;
    }
    result.ok = true;
    result.output = "stored memory note " + note.value("id", std::string());
    return result;
  }
  if (op == "delete") {
    if (!ctx.workspace->delete_memory_note(arg_str(args, "id"), &err)) {
      result.output = "error: " + err;
      return result;
    }
    result.ok = true;
    result.output = "deleted memory note";
    return result;
  }
  if (op == "list") {
    const std::vector<nlohmann::json> notes = ctx.workspace->load_memory(&err);
    if (notes.empty()) {
      result.ok = true;
      result.output = "(no memory notes)";
      return result;
    }
    std::ostringstream out;
    for (std::size_t i = 0; i < notes.size(); ++i) {
      out << "- [" << notes[i].value("id", std::string()) << "] "
          << notes[i].value("text", std::string()) << "\n";
    }
    result.ok = true;
    result.output = out.str();
    return result;
  }
  result.output = "error: unsupported op '" + op + "' (use add|list|delete)";
  return result;
}

// ---------------------------------------------------------------------------
// toolkit_agent_doc
//
// agent.md is the workspace's experience log: what the agent learned about THIS
// project and would want to know next session (pitfalls, non-obvious commands,
// scripts it wrote, conventions). It is injected into the system prompt on every
// turn, which is why appends are capped: a runaway log would silently eat the
// context window. op=replace exists so the model can compact the file instead of
// growing it forever.
// ---------------------------------------------------------------------------
ToolResult tool_agent_doc(const nlohmann::json& args, const ToolContext& ctx) {
  ToolResult result;
  if (!ctx.workspace) {
    result.output = "error: no workspace is active";
    return result;
  }
  const std::string op = to_lower(trim(arg_str(args, "op", "read")));
  std::string err;

  if (op == "read") {
    bool truncated = false;
    const std::string doc = ctx.workspace->load_agent_doc(&truncated);
    result.ok = true;
    result.output = doc.empty() ? std::string("(agent.md is empty)") : doc;
    if (truncated) result.output += "\n... [truncated at the file size limit]";
    result.meta["path"] = ctx.workspace->agent_doc_file();
    result.meta["bytes"] = static_cast<long long>(doc.size());
    return result;
  }
  if (op == "append") {
    const std::string text = arg_str(args, "text");
    if (trim(text).empty()) {
      result.output = "error: 'text' is required for op=append";
      return result;
    }
    if (!ctx.workspace->append_agent_doc(text, &err)) {
      result.output = "error: " + err;
      return result;
    }
    result.ok = true;
    result.meta["path"] = ctx.workspace->agent_doc_file();
    result.meta["bytes"] = static_cast<long long>(file_size(ctx.workspace->agent_doc_file()));
    result.output = "appended to agent.md (" + std::to_string(file_size(ctx.workspace->agent_doc_file())) +
                    " bytes)";
    return result;
  }
  if (op == "replace") {
    const std::string text = arg_str(args, "content");
    if (trim(text).empty()) {
      // Wiping the file is a real operation, but it should be spelled out
      // rather than triggered by a missing/empty argument.
      result.output = "error: 'content' is required for op=replace (use op=read first, then send the "
                      "compacted document)";
      return result;
    }
    if (!ctx.workspace->save_agent_doc(text, &err)) {
      result.output = "error: " + err;
      return result;
    }
    result.ok = true;
    result.meta["path"] = ctx.workspace->agent_doc_file();
    result.meta["bytes"] = static_cast<long long>(file_size(ctx.workspace->agent_doc_file()));
    result.output = "replaced agent.md (" + std::to_string(file_size(ctx.workspace->agent_doc_file())) +
                    " bytes)";
    return result;
  }
  if (op == "scripts") {
    // The reusable half of "reuse what a previous session built": what is in
    // .tea_agent_run/scripts/ right now, newest first.
    const std::string dir = ctx.workspace->scripts_dir();
    std::vector<DirEntryInfo> entries;
    if (!list_directory(dir, &entries, &err)) {
      result.output = "error: cannot list " + dir + ": " + err;
      return result;
    }
    if (entries.empty()) {
      result.ok = true;
      result.output = "(no scripts yet in .tea_agent_run/scripts/)";
      return result;
    }
    std::sort(entries.begin(), entries.end(),
              [](const DirEntryInfo& a, const DirEntryInfo& b) { return a.mtime > b.mtime; });
    std::ostringstream out;
    for (std::size_t i = 0; i < entries.size(); ++i) {
      out << (entries[i].is_dir ? "dir  " : "file ") << entries[i].name << "  ("
          << entries[i].size << " bytes, " << entries[i].mtime << ")\n";
    }
    out << "Run them with toolkit_exec, e.g. `sh .tea_agent_run/scripts/<name>`.";
    result.ok = true;
    result.output = out.str();
    result.meta["dir"] = dir;
    result.meta["count"] = static_cast<long long>(entries.size());
    return result;
  }
  result.output = "error: unsupported op '" + op + "' (use read|append|replace|scripts)";
  return result;
}

// ---------------------------------------------------------------------------
// toolkit_todo
//
// A long job is easier to follow - for the model and for the person watching -
// when it is written down as steps. The list lives on the session (persisted,
// re-injected into the prompt every turn, pushed to the UI on every change), so
// it survives compaction, a restart and "what was it doing again?".
// ---------------------------------------------------------------------------
constexpr std::size_t kMaxTodoItems = 50;
constexpr std::size_t kMaxTodoTextBytes = 300;

std::string todo_status_glyph(const std::string& status) {
  if (status == "done") return "[x]";
  if (status == "active") return "[~]";
  return "[ ]";
}

/// "3/7 done" plus a per-status breakdown; also the text the model reads back
/// after every mutation, so it can see its own progress.
std::string todo_render(const std::vector<TodoItem>& todos, const std::string& headline) {
  int total = 0, done = 0;
  todo_counts(todos, &total, &done);
  int active = 0;
  for (std::size_t i = 0; i < todos.size(); ++i) {
    if (todos[i].status == "active") ++active;
  }
  std::ostringstream out;
  if (!headline.empty()) out << headline << "\n";
  out << "todo list: " << done << "/" << total << " done";
  if (active > 0) out << ", " << active << " in progress";
  out << "\n";
  if (todos.empty()) {
    out << "(empty)\n";
    return out.str();
  }
  for (std::size_t i = 0; i < todos.size(); ++i) {
    out << "  " << (i + 1) << ". " << todo_status_glyph(todos[i].status) << " "
        << todos[i].text;
    if (todos[i].status == "active") out << "   <- in progress";
    out << "\n";
  }
  if (total > 0 && done == total) {
    out << "All steps are done. Say what was accomplished; add follow-up steps if the work "
           "continues.\n";
  } else if (active == 0) {
    out << "No step is in progress: mark the next one active before working on it.\n";
  }
  return out.str();
}

/// Find an item by "id" first, then by 1-based "index".
std::size_t todo_find(const std::vector<TodoItem>& todos, const nlohmann::json& args,
                      bool* found) {
  *found = false;
  const std::string id = trim(arg_str(args, "id"));
  if (!id.empty()) {
    for (std::size_t i = 0; i < todos.size(); ++i) {
      if (todos[i].id == id) {
        *found = true;
        return i;
      }
    }
  }
  const long long index = arg_int(args, "index", 0);
  if (index >= 1 && static_cast<std::size_t>(index) <= todos.size()) {
    *found = true;
    return static_cast<std::size_t>(index - 1);
  }
  return 0;
}

/// Normalise one incoming entry: {"text": "...", "status": "..."} or a bare
/// string (=> pending). Long text is clipped rather than rejected: a truncated
/// step is still usable, a failed call is not.
TodoItem todo_from_arg(const nlohmann::json& value, std::size_t ordinal) {
  TodoItem item;
  item.id = "t" + std::to_string(ordinal);
  item.status = "pending";
  if (value.is_string()) {
    item.text = trim(value.get<std::string>());
  } else if (value.is_object()) {
    if (value.contains("text") && value["text"].is_string()) {
      item.text = trim(value["text"].get<std::string>());
    } else if (value.contains("content") && value["content"].is_string()) {
      item.text = trim(value["content"].get<std::string>());
    }
    if (value.contains("status") && value["status"].is_string()) {
      const std::string st = to_lower(trim(value["status"].get<std::string>()));
      if (TodoItem::valid_status(st)) item.status = st;
    }
  }
  if (item.text.size() > kMaxTodoTextBytes) item.text = truncate_utf8(item.text, kMaxTodoTextBytes);
  return item;
}

ToolResult tool_todo(const nlohmann::json& args, const ToolContext& ctx) {
  ToolResult result;
  if (!ctx.session) {
    result.output = "error: no session is active";
    return result;
  }
  const std::string op = to_lower(trim(arg_str(args, "op", "list")));
  std::vector<TodoItem>& todos = ctx.session->todos;

  if (op == "list") {
    result.ok = true;
    result.output = todo_render(todos, "");
    return result;
  }

  if (op == "clear") {
    todos.clear();
    result.ok = true;
    result.todos_changed = true;
    result.output = "todo list cleared";
    return result;
  }

  if (op == "set" || op == "replace") {
    nlohmann::json items;
    if (args.is_object()) {
      if (args.contains("items")) items = args["items"];
      else if (args.contains("todos")) items = args["todos"];
    }
    if (!items.is_array()) {
      result.output = "error: op=set needs 'items': an array of steps (strings, or "
                      "{text, status})";
      return result;
    }
    if (items.size() > kMaxTodoItems) {
      result.output = "error: at most " + std::to_string(kMaxTodoItems) +
                      " steps; group the small ones together";
      return result;
    }
    std::vector<TodoItem> next;
    for (std::size_t i = 0; i < items.size(); ++i) {
      TodoItem item = todo_from_arg(items[i], next.size() + 1);
      if (item.text.empty()) continue;
      next.push_back(item);
    }
    int active = 0;
    for (std::size_t i = 0; i < next.size(); ++i) {
      if (next[i].status == "active") ++active;
    }
    if (active > 1) {
      result.output = "error: only one step may be 'active' at a time (the work in progress); "
                      "mark the others pending or done";
      return result;
    }
    todos = next;
    result.ok = true;
    result.todos_changed = true;
    result.output = todo_render(todos, "todo list set (" + std::to_string(todos.size()) +
                                            " step(s))");
    return result;
  }

  if (op == "add") {
    if (todos.size() >= kMaxTodoItems) {
      result.output = "error: the list already has " + std::to_string(kMaxTodoItems) +
                      " steps; merge or drop finished ones with op=set";
      return result;
    }
    TodoItem item = todo_from_arg(args, todos.size() + 1);
    if (item.text.empty()) {
      // Accept the common shorthand {"op":"add","text":"..."} only; anything
      // else means the caller meant something else.
      item = todo_from_arg(args.is_object() && args.contains("items") ? args["items"]
                                                                     : nlohmann::json(),
                           todos.size() + 1);
    }
    if (item.text.empty()) {
      result.output = "error: op=add needs 'text' (the step to add)";
      return result;
    }
    if (item.status == "active") {
      for (std::size_t i = 0; i < todos.size(); ++i) {
        if (todos[i].status == "active") todos[i].status = "pending";
      }
    }
    todos.push_back(item);
    result.ok = true;
    result.todos_changed = true;
    result.output = todo_render(todos, "added step " + std::to_string(todos.size()));
    return result;
  }

  if (op == "update") {
    bool found = false;
    const std::size_t at = todo_find(todos, args, &found);
    if (!found) {
      result.output = "error: no such step; pass 'index' (1-based, from op=list) or 'id'";
      return result;
    }
    const std::string status = to_lower(trim(arg_str(args, "status")));
    const std::string text = trim(arg_str(args, "text"));
    if (status.empty() && text.empty()) {
      result.output = "error: op=update needs 'status' and/or 'text'";
      return result;
    }
    if (!status.empty()) {
      if (!TodoItem::valid_status(status)) {
        result.output = "error: status must be pending, active or done";
        return result;
      }
      if (status == "active") {
        // Exactly one step in progress, always: that is what makes the list
        // readable as "where is it now".
        for (std::size_t i = 0; i < todos.size(); ++i) {
          if (i != at && todos[i].status == "active") todos[i].status = "pending";
        }
      }
      todos[at].status = status;
    }
    if (!text.empty()) {
      todos[at].text = text.size() > kMaxTodoTextBytes
                           ? truncate_utf8(text, kMaxTodoTextBytes)
                           : text;
    }
    result.ok = true;
    result.todos_changed = true;
    result.output = todo_render(todos, "updated step " + std::to_string(at + 1));
    return result;
  }

  result.output = "error: unsupported op '" + op +
                  "' (use list | set | add | update | clear)";
  return result;
}

// ---------------------------------------------------------------------------
// toolkit_load_image
//
// The only way an image used to enter a conversation was a human pasting one:
// toolkit_file returns bytes as text, which a vision model cannot read. This
// tool takes a path, verifies the file really is an image (by magic bytes, not
// by extension), copies it under .tea_agent_run/artifacts/images/ - so later
// edits or deletions of the source cannot break the stored session, and /raw
// can serve it back to the UI - and hands the attachment to the agent loop,
// which appends it as a follow-up user message. Tool results themselves are
// text-only in the OpenAI wire format, so that follow-up is the only place an
// image can ride on.
// ---------------------------------------------------------------------------
constexpr std::size_t kMaxLoadedImageBytes = 8u * 1024u * 1024u;

// toolkit_task -- report on the task this turn belongs to.
//
// A task is work that was queued for later (the queue itself lives in the task
// server, sched/task_server.py). This side does not keep task records: the
// agent only says three things about the work - how far it has come, what it
// just did, and whether a human has to confirm something before it goes on -
// and the task server reads that from the turn's event stream.

ToolResult tool_task(const nlohmann::json& args, const ToolContext& ctx) {
  ToolResult result;
  const std::string op = to_lower(trim(arg_str(args, "op", "update")));

  if (op == "list" || op == "get") {
    // The queue lives in the task server (sched/task_server.py); this process
    // does not keep task records - it only says what it is doing.
    result.output =
        "this agent does not keep the task queue: it only reports on the task it is working on. "
        "The queue (enqueue, status, results) is served by the task server.";
    return result;
  }
  if (op != "update") {
    result.output = "error: op must be update";
    return result;
  }

  const bool has_progress = args.contains("progress");
  int progress = 0;
  if (has_progress) {
    if (!args["progress"].is_number()) {
      result.output = "error: 'progress' must be a number (0-100)";
      return result;
    }
    progress = args["progress"].get<int>();
    if (progress < 0 || progress > 100) {
      result.output = "error: 'progress' must be between 0 and 100";
      return result;
    }
  }
  const std::string note = trim(arg_str(args, "note"));
  const std::string question = trim(arg_str(args, "question"));
  bool needs_confirmation = false;
  if (args.contains("needs_confirmation")) {
    if (!args["needs_confirmation"].is_boolean()) {
      result.output = "error: 'needs_confirmation' must be true or false";
      return result;
    }
    needs_confirmation = args["needs_confirmation"].get<bool>();
  }

  // The report is the arguments of this very call: the task server reads them
  // from the turn's event stream and puts them in the task record. This side
  // only says it out loud, so the transcript shows what was reported.
  std::string label;
  if (ctx.task_id && !ctx.task_id->empty()) label = *ctx.task_id + ": ";
  if (has_progress) label += "progress " + std::to_string(progress) + "%";
  if (!note.empty()) label += (label.empty() ? "" : " - ") + note;
  if (needs_confirmation) {
    label += (label.empty() ? "" : " - ");
    label += question.empty() ? "waiting for the user" : ("waiting for the user: " + question);
  }
  result.ok = true;
  result.output = label.empty() ? "reported (nothing new)" : label;
  return result;
}

ToolResult tool_load_image(const nlohmann::json& args, const ToolContext& ctx) {
  ToolResult result;
  if (!ctx.workspace) {
    result.output = "error: no workspace is active";
    return result;
  }
  if (ctx.model && !ctx.model->supports_vision) {
    result.output =
        "error: the active model does not declare vision support (supports_vision: false), so "
        "an attached image would be dropped. Describe what you need in text, or switch to a "
        "vision-capable model first.";
    return result;
  }
  const std::string requested = arg_str(args, "path");
  if (trim(requested).empty()) {
    result.output = "error: 'path' is required";
    return result;
  }
  std::string err;
  const std::string path = ctx.workspace->resolve_path(requested, &err);
  if (path.empty()) {
    result.output = "error: " + err;
    return result;
  }
  if (!path_is_file(path)) {
    result.output = path_exists(path) ? "error: not a regular file: " + requested
                                      : "error: file not found: " + requested;
    return result;
  }
  std::string bytes;
  bool too_big = false;
  if (!read_file(path, &bytes, kMaxLoadedImageBytes + 1, &too_big) || too_big ||
      bytes.size() > kMaxLoadedImageBytes) {
    result.output = "error: " + requested + " is larger than " +
                    human_size(kMaxLoadedImageBytes) +
                    ", which is the per-image limit; downscale it first (e.g. with the "
                    "`convert`/`magick` or `sips` command, or ffmpeg) and load that.";
    return result;
  }
  if (bytes.empty()) {
    result.output = "error: " + requested + " is empty";
    return result;
  }
  const char* mime = sniff_image_mime(bytes);
  if (!mime) {
    result.output = "error: " + requested +
                    " is not a PNG, JPEG, GIF or WebP image (checked by content, not by name). "
                    "Use toolkit_file to read text files.";
    return result;
  }

  // Copy, do not reference: the source is the user's file and it may be
  // regenerated, moved or deleted while the session still refers to it.
  const std::string dir = path_join(ctx.workspace->artifacts_dir(), "images");
  if (!make_dirs(dir)) {
    result.output = "error: cannot create " + dir;
    return result;
  }
  const std::string file = path_join(dir, "img_" + random_hex(8) + image_extension_for(mime));
  if (!write_file(file, bytes, false, &err)) {
    result.output = "error: cannot store the image: " + err;
    return result;
  }

  ImageAttachment att;
  att.mime = mime;
  // Workspace-relative: the session file stays portable and /raw can serve it.
  att.path = ctx.workspace->display_path(file);

  result.ok = true;
  result.images.push_back(att);
  result.meta = nlohmann::json::object();
  result.meta["path"] = att.path;
  result.meta["source"] = ctx.workspace->display_path(path);
  result.meta["mime"] = att.mime;
  result.meta["bytes"] = static_cast<long long>(bytes.size());
  const std::string note = arg_str(args, "note");
  result.output = "loaded image " + att.path + " (" + mime + ", " + human_size(bytes.size()) +
                  ") from " + ctx.workspace->display_path(path) +
                  "; it is attached to the conversation as an image you can see.";
  if (!trim(note).empty()) result.output += " note: " + trim(note);
  return result;
}

// ---------------------------------------------------------------------------
// toolkit_search / toolkit_fetch
//
// Provider-scoped web tools. The protocol (DeepSeek's /v1/tools/{search,
// fetch}) is not OpenAI-standard, so support is declared per provider in
// provider.yaml rather than probed at runtime, and both tools only appear in
// the advertised tool list when the turn's provider opts in. The provider
// bills every call per request, which is why the config default is off.
// ---------------------------------------------------------------------------

std::string strip_trailing_slashes(std::string s) {
  while (!s.empty() && s.back() == '/') s.pop_back();
  return s;
}

/// Join a provider base URL with a web-tool endpoint. `override_path` may be
/// empty (=> `default_path`), a path relative to api_url, or a full URL.
std::string tool_endpoint_url(const ProviderInfo& provider, const std::string& override_path,
                              const char* default_path) {
  std::string path = trim(override_path);
  if (path.empty()) path = default_path;
  if (path.empty()) return std::string();
  if (starts_with(path, "http://") || starts_with(path, "https://")) return path;
  if (path[0] != '/') path = "/" + path;
  std::string base = strip_trailing_slashes(trim(provider.api_url));
  if (base.empty()) return std::string();
  if (ends_with(base, path)) return base;
  return base + path;
}

/// Count Unicode code points (roughly: bytes that are not UTF-8 continuation).
std::size_t utf8_char_count(const std::string& s) {
  std::size_t n = 0;
  for (std::size_t i = 0; i < s.size(); ++i) {
    if ((static_cast<unsigned char>(s[i]) & 0xC0) != 0x80) ++n;
  }
  return n;
}

/// `localization` is geolocation, not language: the provider rejects anything
/// that is not an IP literal ("'localization' must be a valid IP address"). Models
/// reach for a language tag here constantly, so the value is checked before a
/// billed call is sent and the error explains where language filtering lives.
bool looks_like_ip_literal(const std::string& value) {
  if (value.empty() || value.size() > 45) return false;
  bool has_colon = false, has_dot = false;
  for (std::size_t i = 0; i < value.size(); ++i) {
    const char c = value[i];
    if (c == ':') {
      has_colon = true;
      continue;
    }
    if (c == '.') {
      has_dot = true;
      continue;
    }
    const bool hexish = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
    if (!hexish) return false;  // a letter past 'f', a '-', a space, a slash...
  }
  if (has_colon) return true;              // IPv6 (possibly with an embedded IPv4)
  if (!has_dot) return false;              // "en", "203", "1.2" ...
  int parts = 1;
  for (std::size_t i = 0; i < value.size(); ++i) {
    if (value[i] == '.') ++parts;
  }
  if (parts != 4) return false;
  // Dotted quad: every octet must parse to 0..255.
  std::size_t i = 0;
  while (i <= value.size()) {
    const std::size_t dot = value.find('.', i);
    const std::string octet = value.substr(i, dot == std::string::npos ? std::string::npos
                                                                     : dot - i);
    if (octet.empty() || octet.size() > 3) return false;
    int n = 0;
    for (std::size_t k = 0; k < octet.size(); ++k) {
      if (octet[k] < '0' || octet[k] > '9') return false;
      n = n * 10 + (octet[k] - '0');
    }
    if (n > 255) return false;
    if (dot == std::string::npos) break;
    i = dot + 1;
  }
  return true;
}

/// Null-safe field accessors: web-tool responses mark missing values as JSON
/// null, which nlohmann's value() would reject with a type error.
std::string json_field_str(const nlohmann::json& j, const char* key,
                           const std::string& def = "") {
  if (!j.is_object()) return def;
  nlohmann::json::const_iterator it = j.find(key);
  if (it == j.end() || !it->is_string()) return def;
  return it->get<std::string>();
}

double json_field_num(const nlohmann::json& j, const char* key, double def) {
  if (!j.is_object()) return def;
  nlohmann::json::const_iterator it = j.find(key);
  if (it == j.end() || !it->is_number()) return def;
  return it->get<double>();
}

std::string web_tool_status_hint(int status) {
  switch (status) {
    case 401:
      return "API key missing or invalid; check provider.api_key";
    case 402:
      return "insufficient balance; top up the account, then retry";
    case 403:
      return "blocked by the provider's security policy";
    case 404:
      return "no usable page content (URL unreachable, blocked by the site, unsupported "
             "content type, or no extractable text)";
    case 429:
      return "rate limit exceeded (account QPS, or for fetch the target domain's QPS); "
             "wait before retrying";
    case 408:
    case 500:
    case 502:
    case 503:
    case 504:
      return "transient failure; safe to retry";
    default:
      return std::string();
  }
}

std::string web_tool_error_message(const nlohmann::json& body) {
  if (!body.is_object()) return std::string();
  nlohmann::json::const_iterator err = body.find("error");
  if (err == body.end() || err->is_null()) return std::string();
  if (err->is_string()) return err->get<std::string>();
  if (err->is_object()) {
    const std::string msg = json_field_str(*err, "message");
    return msg.empty() ? err->dump() : msg;
  }
  return err->dump();
}

struct WebToolResponse {
  bool ok = false;
  int status = 0;
  long long elapsed_ms = 0;
  nlohmann::json body;
  std::string error;
};

/// POST a JSON payload to a web-tool endpoint with the provider's Bearer auth.
WebToolResponse web_tool_post(const std::string& url, const std::string& api_key,
                              const nlohmann::json& payload, const Settings& st,
                              std::atomic<bool>* cancel) {
  WebToolResponse out;
  if (cancel && cancel->load()) {
    out.error = "cancelled by user";
    return out;
  }
  HttpRequest req;
  req.method = "POST";
  req.url = url;
  HttpHeader auth;
  auth.name = "Authorization";
  auth.value = "Bearer " + api_key;
  req.headers.push_back(auth);
  req.body = dump_json(payload);
  req.content_type = "application/json";
  // Dedicated budget: the LLM read timeout is minutes long on purpose, and
  // inheriting it made one slow endpoint stall the whole turn.
  req.connect_timeout_ms =
      st.web_tool_connect_timeout_ms > 0 ? st.web_tool_connect_timeout_ms : 10000;
  req.read_timeout_ms = st.web_tool_timeout_ms > 0 ? st.web_tool_timeout_ms : 60000;

  // Abort mid-transfer when the user stops the turn. Returning false makes the
  // HTTP layer close the connection instead of draining the body.
  const std::atomic<bool>* cancel_flag = cancel;
  HttpChunkCallback stop_on_cancel = [cancel_flag](const char*, std::size_t) {
    return !(cancel_flag && cancel_flag->load());
  };
  const HttpResponse resp = http_perform(req, stop_on_cancel);
  out.status = resp.status;
  out.elapsed_ms = resp.elapsed_ms;
  if (resp.aborted) {
    out.error = "cancelled by user";
    return out;
  }
  if (!resp.ok) {
    out.error = resp.error.empty() ? ("no HTTP response from " + url) : resp.error;
    const std::string lower = to_lower(out.error);
    if (lower.find("timed out") != std::string::npos ||
        lower.find("timeout") != std::string::npos) {
      // The HTTP layer already names the budget; just make it actionable.
      out.error += "; the endpoint may be slow - retry once, or continue without it";
    }
    return out;
  }
  if (!resp.body.empty()) {
    try {
      out.body = nlohmann::json::parse(resp.body);
    } catch (const std::exception& e) {
      out.error = std::string("response is not valid JSON (") + e.what() +
                  "); body starts with: " + truncate_utf8(resp.body, 300);
      return out;
    }
  }
  if (resp.status != 200) {
    out.error = "HTTP " + std::to_string(resp.status);
    const std::string msg = web_tool_error_message(out.body);
    if (!msg.empty()) out.error += ": " + msg;
    const std::string hint = web_tool_status_hint(resp.status);
    if (!hint.empty()) out.error += " [" + hint + "]";
    return out;
  }
  out.ok = true;
  return out;
}

/// The fetch endpoint returns Markdown with every punctuation character
/// backslash-escaped ("\/", "\,", "\#", "\.", ...). That is roughly 8% of the
/// body and pure noise for the model, so undo the escapes a Markdown
/// serialiser produces. Fenced code blocks are left alone: a regex like
/// `\.` or `\d` inside a sample must keep its backslash.
std::string unescape_markdown(const std::string& in) {
  std::string out;
  out.reserve(in.size());
  bool in_fence = false;
  std::size_t i = 0;
  while (i < in.size()) {
    if (i == 0 || in[i - 1] == '\n') {
      std::size_t j = i;
      while (j < in.size() && in[j] == ' ') ++j;
      if (in.compare(j, 3, "```") == 0) in_fence = !in_fence;
    }
    const char c = in[i];
    if (c == '\\' && !in_fence && i + 1 < in.size()) {
      const char next = in[i + 1];
      if (next == '\\') {
        out.push_back('\\');
        i += 2;
        continue;
      }
      if (std::strchr("`*_{}[]()#+-.!|>~/,:;=?", next) != nullptr) {
        out.push_back(next);
        i += 2;
        continue;
      }
    }
    out.push_back(c);
    ++i;
  }
  return out;
}

/// Charge one call against the per-turn budget. Returns an error message when
/// the budget is exhausted, empty string when the call may proceed.
std::string web_tool_charge(const ToolContext& ctx) {
  if (!ctx.web_tool_calls) return std::string();
  const int limit = ctx.config ? ctx.config->settings.web_tool_max_calls_per_turn : 10;
  if (*ctx.web_tool_calls >= limit) {
    return "error: the web-tool budget for this turn is exhausted (" + std::to_string(limit) +
           " calls). Each call is billed per request, so answer with what you already have "
           "or tell the user what is still missing.";
  }
  ++*ctx.web_tool_calls;
  return std::string();
}

int64_t web_tool_output_limit(const ToolContext& ctx) {
  if (ctx.workspace) return ctx.workspace->settings().max_tool_output;
  if (ctx.config) return ctx.config->settings.max_tool_output;
  return 131072;
}

/// Which provider serves one web-tool call.
///
/// The turn's own provider when it can serve the tool, otherwise the
/// independent search provider from the `search:` section of provider.yaml -
/// that is the whole point of the section: the conversation can run on a
/// provider without web access and still search the web.
///
/// `out` receives a copy of that provider (with the effective endpoint paths
/// applied), so the caller never has to know where the call ends up.
bool resolve_web_provider(const ToolContext& ctx, bool want_search, ProviderInfo* out,
                          bool* via_search_provider, std::string* err) {
  if (ctx.config) {
    return ctx.config->resolve_web_tool(ctx.provider, want_search, out, via_search_provider, err);
  }
  // No configuration loaded: the turn's provider is all there is.
  if (!ctx.provider) {
    if (err) *err = "no provider is active for this turn";
    return false;
  }
  const bool capable =
      want_search ? ctx.provider->supports_search : ctx.provider->supports_fetch;
  if (!capable) {
    if (err) {
      *err = "provider '" + ctx.provider->name + "' does not support " +
             (want_search ? "web search" : "page fetching") + " (set " +
             (want_search ? "supports_search" : "supports_fetch") + ": true in provider.yaml)";
    }
    return false;
  }
  *out = *ctx.provider;
  return true;
}

/// Can a web tool be offered to the model at all? The turn's own provider can
/// serve it, or the independent search provider (the `search:` section of
/// provider.yaml) can.
bool web_tool_available(const ProviderInfo* provider, const AppConfig* config, bool want_search) {
  if (provider && (want_search ? provider->supports_search : provider->supports_fetch)) return true;
  if (!config) return false;
  const ProviderInfo* sp = config->search_provider_info();
  if (!sp) return false;
  if (!(want_search ? config->search.supports_search : config->search.supports_fetch)) return false;
  // Without a key every call would fail with a 401, so the tool stays out of
  // the schema (the load-time warning says why).
  return sp->has_key();
}

ToolResult tool_search(const nlohmann::json& args, const ToolContext& ctx) {
  ToolResult result;
  // The turn's own provider, or - when it cannot search - the independent
  // search provider from the `search:` section of provider.yaml.
  ProviderInfo wp;
  bool via_search = false;
  std::string err;
  if (!resolve_web_provider(ctx, true, &wp, &via_search, &err)) {
    result.output = "error: " + err;
    return result;
  }
  if (!wp.has_key()) {
    result.output = "error: provider '" + wp.name + "' has no api_key configured";
    return result;
  }
  const std::string budget_error = web_tool_charge(ctx);
  if (!budget_error.empty()) {
    result.output = budget_error;
    return result;
  }
  const std::string query = trim(arg_str(args, "query"));
  if (query.empty()) {
    result.output = "error: 'query' is required (1-128 characters, not all whitespace)";
    return result;
  }
  if (utf8_char_count(query) > 128) {
    result.output = "error: 'query' must be at most 128 characters";
    return result;
  }
  const long long count = arg_int(args, "count", 10);
  if (count < 1 || count > 50) {
    result.output = "error: 'count' must be between 1 and 50";
    return result;
  }
  const std::string ori_question = trim(arg_str(args, "ori_question"));
  if (!ori_question.empty() && utf8_char_count(ori_question) > 128) {
    result.output = "error: 'ori_question' must be at most 128 characters";
    return result;
  }
  const std::string localization = trim(arg_str(args, "localization"));
  if (!localization.empty() && !looks_like_ip_literal(localization)) {
    result.output = "error: 'localization' must be an IP address (an IPv4/IPv6 literal such as "
                    "203.0.113.5) used to prioritise geographically close results; '" +
                    localization +
                    "' is not one. To filter by language put lang:zh (or lang:en, ...) inside "
                    "'query' instead. No request was sent.";
    return result;
  }
  const bool need_content = arg_bool(args, "need_content", false);

  nlohmann::json payload = nlohmann::json::object();
  payload["query"] = query;
  payload["count"] = count;
  if (!ori_question.empty()) payload["ori_question"] = ori_question;
  if (!localization.empty()) payload["localization"] = localization;
  if (need_content) payload["need_content"] = true;

  const std::string url = tool_endpoint_url(wp, wp.search_path, "/v1/tools/search");
  if (url.empty()) {
    result.output = "error: provider '" + wp.name + "' has no usable api_url";
    return result;
  }
  LOG_INFO("toolkit_search: ", sanitize_for_terminal(query), " (count=", count, ")",
           via_search ? (" via " + wp.name) : "");
  const Settings st = ctx.config ? ctx.config->settings : Settings();
  const WebToolResponse resp = web_tool_post(url, wp.api_key, payload, st, ctx.cancel);
  // Which provider actually served the call: the turn's own one, or the
  // independent search provider from the `search:` section.
  result.meta["provider"] = wp.name;
  result.meta["url"] = url;
  result.meta["status"] = resp.status;
  result.meta["elapsed_ms"] = resp.elapsed_ms;
  if (!resp.ok) {
    if (resp.error == "cancelled by user") {
      // The turn was stopped while the request was in flight. That is an
      // interrupt, not a failure: say so, so the row is not painted red and
      // the next turn knows the search never happened.
      result.cancelled = true;
      result.output =
          "cancelled by user: the turn was interrupted while the request was in flight";
      return result;
    }
    result.output = "error: " + resp.error;
    return result;
  }
  nlohmann::json::const_iterator results_it = resp.body.find("results");
  if (results_it == resp.body.end() || !results_it->is_array()) {
    result.output = "error: unexpected response shape (missing 'results' array): " +
                    truncate_utf8(dump_json(resp.body), 2000);
    return result;
  }
  const nlohmann::json& results = *results_it;
  result.meta["results"] = static_cast<long long>(results.size());
  // The provider bills a search only when it produced at least one result.
  result.meta["billable"] = !results.empty();
  if (results.empty()) {
    result.ok = true;
    result.output = "no results for: " + query;
    return result;
  }

  std::ostringstream out;
  out << results.size() << " result(s) for: " << query << "\n";
  for (std::size_t i = 0; i < results.size(); ++i) {
    const nlohmann::json& r = results[i];
    out << "\n[" << (i + 1) << "] " << json_field_str(r, "title") << "\n";
    out << "    url: " << json_field_str(r, "url") << "\n";
    const std::string date = json_field_str(r, "date");
    if (!date.empty()) out << "    date: " << date << "\n";
    const double score = json_field_num(r, "rerank_score", -1.0);
    const double authority = json_field_num(r, "authority", -1.0);
    if (score >= 0.0) {
      char buf[64];
      std::snprintf(buf, sizeof(buf), "%.2f", score);
      out << "    score: " << buf << "\n";
    }
    if (authority >= 0.0) out << "    authority: " << static_cast<int>(authority) << "\n";
    // summary is written for model consumption; snippet is the short fallback.
    const std::string summary = json_field_str(r, "summary", json_field_str(r, "snippet"));
    if (!summary.empty()) out << "    summary: " << summary << "\n";
    if (need_content) {
      const std::string content = unescape_markdown(json_field_str(r, "content"));
      if (!content.empty()) out << "    content:\n" << content << "\n";
    }
  }

  bool truncated = false;
  result.output =
      limit_output(out.str(), static_cast<std::size_t>(web_tool_output_limit(ctx)), &truncated);
  result.truncated = truncated;
  result.ok = true;
  return result;
}

ToolResult tool_fetch(const nlohmann::json& args, const ToolContext& ctx) {
  ToolResult result;
  // The turn's own provider, or - when it cannot fetch - the independent
  // search provider from the `search:` section of provider.yaml.
  ProviderInfo wp;
  bool via_search = false;
  std::string err;
  if (!resolve_web_provider(ctx, false, &wp, &via_search, &err)) {
    result.output = "error: " + err;
    return result;
  }
  if (!wp.has_key()) {
    result.output = "error: provider '" + wp.name + "' has no api_key configured";
    return result;
  }
  const std::string budget_error = web_tool_charge(ctx);
  if (!budget_error.empty()) {
    result.output = budget_error;
    return result;
  }
  const std::string url = trim(arg_str(args, "url"));
  if (url.empty()) {
    result.output = "error: 'url' is required";
    return result;
  }
  if (url.size() > 8192) {
    result.output = "error: 'url' must be at most 8192 bytes";
    return result;
  }
  if (!starts_with(url, "http://") && !starts_with(url, "https://")) {
    result.output = "error: 'url' must be an http:// or https:// URL";
    return result;
  }
  // Reject embedded credentials (user:pass@host), like the API does.
  const std::size_t scheme_end = url.find("://");
  const std::size_t authority_end = url.find_first_of("/?#", scheme_end + 3);
  const std::string authority =
      url.substr(scheme_end + 3, authority_end == std::string::npos
                                     ? std::string::npos
                                     : authority_end - scheme_end - 3);
  if (authority.find('@') != std::string::npos) {
    result.output = "error: 'url' must not contain a username or password";
    return result;
  }

  nlohmann::json payload = nlohmann::json::object();
  payload["url"] = url;
  const std::string endpoint = tool_endpoint_url(wp, wp.fetch_path, "/v1/tools/fetch");
  if (endpoint.empty()) {
    result.output = "error: provider '" + wp.name + "' has no usable api_url";
    return result;
  }
  LOG_INFO("toolkit_fetch: ", sanitize_for_terminal(url),
           via_search ? (" via " + wp.name) : "");
  const Settings st = ctx.config ? ctx.config->settings : Settings();
  const WebToolResponse resp = web_tool_post(endpoint, wp.api_key, payload, st, ctx.cancel);
  result.meta["provider"] = wp.name;
  result.meta["url"] = url;
  result.meta["endpoint"] = endpoint;
  result.meta["status"] = resp.status;
  result.meta["elapsed_ms"] = resp.elapsed_ms;
  if (!resp.ok) {
    if (resp.error == "cancelled by user") {
      // The turn was stopped while the request was in flight. That is an
      // interrupt, not a failure: say so, so the row is not painted red and
      // the next turn knows the search never happened.
      result.cancelled = true;
      result.output =
          "cancelled by user: the turn was interrupted while the request was in flight";
      return result;
    }
    result.output = "error: " + resp.error;
    return result;
  }
  const std::string raw_content = json_field_str(resp.body, "content");
  // The provider bills a fetch only when it returned page content.
  result.meta["billable"] = !raw_content.empty();
  result.meta["bytes"] = static_cast<long long>(raw_content.size());
  const std::string title = unescape_markdown(json_field_str(resp.body, "title"));
  const std::string content = unescape_markdown(raw_content);
  if (content.empty()) {
    result.ok = true;
    result.output = "the provider returned no extractable content for this URL (the page may "
                    "require JavaScript or a login, or be blocked by site policy)" +
                    (title.empty() ? std::string() : "; title: " + title);
    return result;
  }
  std::ostringstream out;
  if (!title.empty()) out << title << "\n\n";
  out << content;
  bool truncated = false;
  result.output =
      limit_output(out.str(), static_cast<std::size_t>(web_tool_output_limit(ctx)), &truncated);
  result.truncated = truncated;
  result.ok = true;
  return result;
}

// ---------------------------------------------------------------------------
// toolkit_fleet -- the school server's hands on its 40 devices
// ---------------------------------------------------------------------------

/// One JSON request to the fleet service. The fleet service holds the device
/// inventory, the daily collection and the batch jobs; this tool only asks.
ToolResult tool_fleet(const nlohmann::json& args, const ToolContext& ctx) {
  ToolResult result;
  if (!ctx.config) {
    result.output = "error: no configuration is active";
    return result;
  }
  const Settings& st = ctx.config->settings;
  if (st.fleet_url.empty()) {
    result.output =
        "error: no fleet service is configured (set fleet_url in the settings, or "
        "TEA_AGENT_FLEET_URL)";
    return result;
  }

  std::string base = st.fleet_url;
  while (!base.empty() && base.back() == '/') base.pop_back();

  const std::string action = arg_str(args, "action", "status");
  nlohmann::json payload = nlohmann::json::object();
  std::string path = "/api/fleet/status";
  std::string method = "GET";

  if (action == "status") {
    path = "/api/fleet/status";
  } else if (action == "devices") {
    path = "/api/fleet/devices";
  } else if (action == "report") {
    path = "/api/fleet/report";
  } else if (action == "collect") {
    // Collect now. It is a fleet-wide sweep, so it takes a while - the
    // fleet service answers when the devices have all answered.
    path = "/api/fleet/collect";
    method = "POST";
    payload = nlohmann::json::object();
  } else if (action == "exec" || action == "upgrade") {
    path = action == "exec" ? "/api/fleet/exec" : "/api/fleet/upgrade";
    method = "POST";
    payload = nlohmann::json::object();
    const std::string package = arg_str(args, "package");
    const std::string command = arg_str(args, "command");
    if (action == "upgrade") {
      if (package.empty()) {
        result.output = "error: 'package' is required for an upgrade";
        return result;
      }
      payload["package"] = package;
    } else {
      if (command.empty()) {
        result.output = "error: 'command' is required";
        return result;
      }
      payload["command"] = command;
    }
    // The selectors: which devices the job touches.
    if (args.contains("groups") && args["groups"].is_array()) {
      payload["groups"] = args["groups"];
    }
    if (args.contains("devices") && args["devices"].is_array()) {
      payload["devices"] = args["devices"];
    }
    if (args.contains("labels") && args["labels"].is_object()) {
      payload["labels"] = args["labels"];
    }
    // The rollout policy. The defaults are the careful ones: one canary
    // device, small batches, and a pause between them.
    payload["canary"] = arg_int(args, "canary", action == "upgrade" ? 1 : 0);
    payload["batch_size"] = arg_int(args, "batch_size", action == "upgrade" ? 5 : 0);
    payload["pause_s"] = arg_int(args, "pause_s", action == "upgrade" ? 30 : 0);
    payload["max_failure_rate"] = arg_double(args, "max_failure_rate", 0.0);
    payload["timeout_s"] = arg_int(args, "timeout_s", action == "upgrade" ? 600 : 120);
    const std::string verify = arg_str(args, "verify");
    if (!verify.empty()) payload["verify"] = verify;
    const std::string rollback = arg_str(args, "rollback");
    if (!rollback.empty()) payload["rollback"] = rollback;
    if (arg_bool(args, "dry_run", false)) payload["dry_run"] = true;
  } else if (action == "ask") {
    // The multi-level path: the DEVICE's own agent answers, with the
    // device's own provider and its own context.
    const std::string prompt = arg_str(args, "prompt");
    if (prompt.empty()) {
      result.output = "error: 'prompt' is required";
      return result;
    }
    path = "/api/fleet/ask";
    method = "POST";
    payload = nlohmann::json::object();
    payload["prompt"] = prompt;
    if (args.contains("groups") && args["groups"].is_array()) payload["groups"] = args["groups"];
    if (args.contains("devices") && args["devices"].is_array()) payload["devices"] = args["devices"];
    payload["timeout_s"] = arg_int(args, "timeout_s", 300);
  } else {
    result.output = "error: unknown action '" + action +
                    "' (status, devices, report, collect, exec, upgrade, ask)";
    return result;
  }

  HttpRequest req;
  req.method = method;
  req.url = base + path;
  if (method == "POST") {
    req.body = dump_json(payload);
    req.content_type = "application/json";
  }
  if (!st.fleet_token.empty()) {
    req.headers.push_back(HttpHeader{"Authorization", "Bearer " + st.fleet_token});
  }
  // A fleet job on 40 devices is not a web request: it needs minutes, not
  // the seconds an LLM call gets.
  req.connect_timeout_ms = 10000;
  req.read_timeout_ms = 900000;

  LOG_INFO("toolkit_fleet: ", method, " ", path);
  const HttpResponse resp = http_perform(req);
  result.meta["url"] = req.url;
  result.meta["status"] = resp.status;
  result.meta["elapsed_ms"] = resp.elapsed_ms;
  if (!resp.ok) {
    result.output = "error: the fleet service did not answer: " +
                    (resp.error.empty() ? ("HTTP " + std::to_string(resp.status)) : resp.error);
    return result;
  }
  if (resp.status != 200) {
    // The service answers errors in the envelope shape; show the message.
    std::string message = resp.body;
    try {
      nlohmann::json parsed = nlohmann::json::parse(resp.body);
      if (parsed.contains("message") && parsed["message"].is_string()) {
        message = parsed["message"].get<std::string>();
      }
    } catch (const std::exception&) {
      // not JSON: the raw body is the message
    }
    result.output = "error: the fleet service refused the request (HTTP " +
                    std::to_string(resp.status) + "): " + truncate_utf8(message, 2000);
    return result;
  }
  // The envelope's data is what the model wants; the wrapper is noise.
  std::string body = resp.body;
  try {
    nlohmann::json parsed = nlohmann::json::parse(resp.body);
    if (parsed.contains("data")) {
      body = dump_json(parsed["data"]);
    }
  } catch (const std::exception&) {
    // not JSON: pass it through as it is
  }
  bool truncated = false;
  result.output = limit_output(body, static_cast<std::size_t>(st.max_tool_output), &truncated);
  result.truncated = truncated;
  result.ok = true;
  return result;
}

// ---------------------------------------------------------------------------
// toolkit_peer -- one tea_a2 driving another
//
// The cascade. A peer is another tea_a2, on another machine, with a brain of
// its own: asking it a question runs ITS agent loop, with its provider, its
// tools and its context. The upper level decides *what* to ask; the lower
// level decides *how* to do it.
//
// The transport is the HTTP/SSE API this agent already serves - no new
// protocol. What is new is that one agent can address another by name, that
// the peer's secret stays in the settings instead of in a prompt, and that
// the answer comes back with a trace of what the peer actually did.
// ---------------------------------------------------------------------------

/// The peer this call is about: resolved from the settings (by name) or from
/// an address the model typed in.
struct PeerTarget {
  /// The name the model used ("lab-pc"), or the address for an ad-hoc peer.
  std::string name;
  /// Base url, no trailing '/'.
  std::string url;
  /// The peer's secret. Empty => the peer has no peer channel.
  std::string token;
  /// The workspace a chat runs in, when the settings say so.
  std::string workspace;
  /// What the peer is.
  std::string description;
  /// True when this peer came from the settings (and not from an address the
  /// model typed in).
  bool configured = false;
};

/// The peers of this agent. The workspace's settings win over the global ones
/// (a project may know machines the machine does not).
std::vector<PeerConfig> peer_settings(const ToolContext& ctx) {
  if (ctx.workspace) return ctx.workspace->settings().peers;
  if (ctx.config) return ctx.config->settings.peers;
  return std::vector<PeerConfig>();
}

/// Find the peer the call is about.
bool resolve_peer(const nlohmann::json& args, const ToolContext& ctx, PeerTarget* out,
                  std::string* err) {
  const std::vector<PeerConfig> peers = peer_settings(ctx);
  const std::string name = trim(arg_str(args, "peer"));
  const std::string raw_url = trim(arg_str(args, "url"));

  if (!name.empty()) {
    for (std::size_t i = 0; i < peers.size(); ++i) {
      if (peers[i].name != name) continue;
      out->name = peers[i].name;
      out->url = peers[i].url;
      out->token = peers[i].token;
      out->workspace = peers[i].workspace;
      out->description = peers[i].description;
      out->configured = true;
      return true;
    }
    std::string known;
    for (std::size_t i = 0; i < peers.size(); ++i) {
      if (!known.empty()) known += ", ";
      known += peers[i].name;
    }
    *err = "there is no peer called '" + name + "'" +
           (known.empty() ? " (no peers are configured)"
                          : " (the configured ones are: " + known + ")");
    return false;
  }

  if (!raw_url.empty()) {
    out->url = normalize_base_url(raw_url);
    if (out->url.empty()) {
      *err = "'" + raw_url + "' is not an http(s) address";
      return false;
    }
    out->name = out->url;
    // An address the model typed in is still the same machine as a configured
    // peer with that address - so it keeps the secret and the workspace. The
    // secret never comes from the model, only from the settings.
    for (std::size_t i = 0; i < peers.size(); ++i) {
      if (peers[i].url != out->url) continue;
      out->token = peers[i].token;
      out->workspace = peers[i].workspace;
      out->description = peers[i].description;
      out->configured = true;
      break;
    }
    return true;
  }

  *err = "which peer? pass peer=\"<name>\" (one of the configured peers) or "
         "url=\"http://host:port\"";
  return false;
}

/// One HTTP request to a peer, with the peer's secret when we have one.
HttpResponse peer_http(const PeerTarget& peer, const std::string& method,
                       const std::string& path, const std::string& body,
                       int connect_timeout_ms, int read_timeout_ms,
                       const HttpChunkCallback& on_chunk) {
  HttpRequest req;
  req.method = method;
  req.url = peer.url + path;
  if (method == "POST") {
    req.body = body;
    req.content_type = "application/json";
  }
  if (!peer.token.empty()) {
    req.headers.push_back(HttpHeader{"Authorization", "Bearer " + peer.token});
  }
  req.connect_timeout_ms = connect_timeout_ms;
  req.read_timeout_ms = read_timeout_ms;
  LOG_INFO("toolkit_peer: ", method, " ", req.url);
  return http_perform(req, on_chunk);
}

/// The peer channel's path for an ordinary route: /api/chat -> /api/peer/chat.
std::string peer_channel_path(const std::string& plain_path) {
  const std::string prefix = "/api/";
  if (starts_with(plain_path, prefix)) {
    return "/api/peer/" + plain_path.substr(prefix.size());
  }
  return "/api/peer" + plain_path;
}

/// A request to a peer, through the peer channel when there is one.
///
/// The peer channel (/api/peer/chat) is the token-guarded mirror of the
/// ordinary route. A peer that does not know it - an older build, or one
/// without a token - answers 404, and then the ordinary route is used. A 401
/// is NOT a reason to fall back: the peer refused the secret, and quietly
/// taking the open route instead would defeat the guard.
///
/// `before_request` runs before every attempt: a stream parser has to start
/// from an empty buffer, or the leftovers of a failed attempt are glued to the
/// first frame of the next one (and that frame is then lost).
HttpResponse peer_call(const PeerTarget& peer, const std::string& method,
                       const std::string& plain_path, const std::string& body,
                       int connect_timeout_ms, int read_timeout_ms,
                       const HttpChunkCallback& on_chunk = HttpChunkCallback(),
                       const std::function<void()>& before_request = std::function<void()>()) {
  if (!peer.token.empty()) {
    if (before_request) before_request();
    HttpResponse guarded =
        peer_http(peer, method, peer_channel_path(plain_path), body, connect_timeout_ms,
                  read_timeout_ms, on_chunk);
    if (guarded.status != 404) return guarded;
  }
  if (before_request) before_request();
  return peer_http(peer, method, plain_path, body, connect_timeout_ms, read_timeout_ms,
                   on_chunk);
}

/// The text of a JSON value that may be a string or a whole object. The
/// peer's event stream carries both shapes ("arguments" is a string on one
/// build and an object on another), and a tool that throws on the wire is
/// worse than one that prints it.
std::string peer_json_text(const nlohmann::json& value) {
  if (value.is_string()) return value.get<std::string>();
  if (value.is_null()) return "";
  if (value.is_object() || value.is_array()) return dump_json(value);
  return value.dump();
}

/// What the peer's agent did, read off its event stream.
struct PeerTurn {
  /// The answer, as the peer wrote it.
  std::string content;
  /// The peer's session id - the handle a later `stop` needs.
  std::string session_id;
  /// One line per tool call on the peer: what it called, and how it went.
  std::vector<std::string> steps;
  /// The peer's error, if it failed.
  std::string error;
  /// Whether the turn finished.
  bool done = false;
  /// A line that has not been seen completely yet.
  std::string buffer;

  /// Forget everything read so far. Used between two attempts at the same
  /// peer: the leftovers of a failed one would otherwise be glued to the
  /// first frame of the next one.
  void reset() {
    buffer.clear();
    content.clear();
    steps.clear();
    error.clear();
    session_id.clear();
    done = false;
  }

  void feed(const char* data, std::size_t len) {
    buffer.append(data, len);
    std::size_t pos;
    while ((pos = buffer.find('\n')) != std::string::npos) {
      const std::string line = buffer.substr(0, pos);
      buffer.erase(0, pos + 1);
      handle_line(line);
    }
  }

  void handle_line(const std::string& raw) {
    const std::string line = trim(raw);
    if (!starts_with(line, "data:")) return;
    const std::string payload = trim(line.substr(5));
    if (payload.empty() || payload == "[DONE]") return;
    nlohmann::json frame;
    try {
      frame = nlohmann::json::parse(payload);
    } catch (const std::exception&) {
      return;
    }
    if (!frame.is_object()) return;
    const std::string type = frame.value("type", std::string());
    if (type == "start") {
      session_id = peer_json_text(frame["session_id"]);
    } else if (type == "content_delta") {
      const std::string text = peer_json_text(frame["text"]);
      if (!text.empty()) content += text;
    } else if (type == "tool_call") {
      const std::string tool = peer_json_text(frame["name"]);
      std::string args = peer_json_text(frame["arguments"]);
      args = truncate_utf8(args, 160);
      steps.push_back("called " + tool + (args.empty() ? "" : "(" + args + ")"));
    } else if (type == "tool_result") {
      const std::string tool = peer_json_text(frame["name"]);
      const bool ok = frame.value("ok", false);
      const long long elapsed = frame.value("elapsed_ms", static_cast<long long>(0));
      std::string output = trim(peer_json_text(frame["output"]));
      const std::size_t nl = output.find('\n');
      if (nl != std::string::npos) output = output.substr(0, nl);
      output = truncate_utf8(output, 160);
      steps.push_back(tool + ": " + (ok ? "ok" : "FAILED") + " (" + std::to_string(elapsed) +
                      "ms)" + (output.empty() ? "" : " - " + output));
    } else if (type == "error") {
      error = peer_json_text(frame["message"]);
      if (error.empty()) error = "the peer failed";
    } else if (type == "done") {
      done = true;
      // The finished event carries the whole answer. The deltas are the
      // stream; this is the truth (a turn that was cut short has none).
      const std::string whole = peer_json_text(frame["content"]);
      if (!whole.empty()) content = whole;
    }
  }
};

/// The first line of a peer's answer, for the header line of the result.
std::string peer_header(const PeerTarget& peer, const std::string& what) {
  std::string out = "[" + what;
  if (!peer.name.empty()) out += " " + peer.name;
  out += " @ " + peer.url + "]";
  return out;
}

/// The error message of a peer's answer, if it is one.
std::string peer_error_message(const HttpResponse& resp) {
  std::string message = resp.body;
  try {
    nlohmann::json parsed = nlohmann::json::parse(resp.body);
    if (parsed.contains("error")) {
      const nlohmann::json& err = parsed["error"];
      if (err.is_string()) {
        message = err.get<std::string>();
      } else if (err.is_object() && err.contains("message")) {
        message = err.value("message", std::string());
      }
    } else if (parsed.contains("message") && parsed["message"].is_string()) {
      message = parsed["message"].get<std::string>();
    }
  } catch (const std::exception&) {
    // not JSON: the raw body is the message
  }
  return truncate_utf8(message, 2000);
}

/// The workspace a chat on the peer runs in: what the caller said, else what
/// the settings say, else the peer's first workspace.
bool peer_workspace(const PeerTarget& peer, const std::string& wanted, std::string* out,
                    std::string* err) {
  if (!wanted.empty()) {
    *out = wanted;
    return true;
  }
  if (!peer.workspace.empty()) {
    *out = peer.workspace;
    return true;
  }
  // The workspace is registered on the PEER, and a peer may have several, so
  // the peer is the one who says.
  HttpResponse resp = peer_call(peer, "GET", "/api/workspaces", "", 10000, 30000);
  if (resp.status != 200) {
    *err = "cannot ask the peer for its workspaces (HTTP " + std::to_string(resp.status) +
           "): " + peer_error_message(resp);
    return false;
  }
  try {
    nlohmann::json parsed = nlohmann::json::parse(resp.body);
    if (parsed.contains("workspaces") && parsed["workspaces"].is_array()) {
      for (std::size_t i = 0; i < parsed["workspaces"].size(); ++i) {
        const nlohmann::json& ws = parsed["workspaces"][i];
        if (ws.is_object() && ws.contains("id") && ws["id"].is_string()) {
          *out = ws["id"].get<std::string>();
          return true;
        }
      }
    }
  } catch (const std::exception&) {
  }
  *err = "the peer has no workspace to work in (register one on it, or name one with "
         "workspace_id)";
  return false;
}

/// The peers this agent knows, as text.
std::string peer_list_text(const ToolContext& ctx) {
  const std::vector<PeerConfig> peers = peer_settings(ctx);
  if (peers.empty()) {
    return "no peers are configured. Add them to the 'peers:' section of the settings "
           "(provider.yaml, tea_agent.yaml or the workspace's settings.yaml):\n"
           "  peers:\n"
           "    lab-pc:\n"
           "      url: http://172.16.1.31:8383\n"
           "      token: <the peer's --api-token>\n"
           "      workspace: <the workspace id on the peer>   # optional\n"
           "      description: the projector in room 302        # optional\n"
           "A peer can also be addressed directly with url=\"http://host:port\".";
  }
  std::string out = "peers (" + std::to_string(peers.size()) + "):\n";
  for (std::size_t i = 0; i < peers.size(); ++i) {
    const PeerConfig& p = peers[i];
    out += "- " + p.name + "  " + p.url;
    out += p.token.empty() ? "  (no token: only the open routes)" : "  (token: yes)";
    if (!p.workspace.empty()) out += "  workspace: " + p.workspace;
    if (!p.description.empty()) out += "  - " + p.description;
    out += "\n";
  }
  return out;
}

ToolResult tool_peer(const nlohmann::json& args, const ToolContext& ctx) {
  ToolResult result;
  const std::string action = to_lower(trim(arg_str(args, "action", "list")));

  if (action == "list") {
    result.output = peer_list_text(ctx);
    result.ok = true;
    return result;
  }

  PeerTarget peer;
  std::string err;
  if (!resolve_peer(args, ctx, &peer, &err)) {
    result.output = "error: " + err;
    return result;
  }

  const Settings& st = ctx.config ? ctx.config->settings : Settings();
  const std::size_t out_limit = static_cast<std::size_t>(st.max_tool_output);
  const int timeout_s = static_cast<int>(arg_int(args, "timeout_s", 900));
  const int connect_timeout_ms = 10000;

  if (action == "health" || action == "status" || action == "tools" ||
      action == "workspaces") {
    const std::string path = action == "health"    ? "/api/health"
                             : action == "status" ? "/api/status"
                             : action == "tools"  ? "/api/tools"
                                                 : "/api/workspaces";
    LOG_INFO("toolkit_peer: ", action, " ", peer.url);
    HttpResponse resp = peer_call(peer, "GET", path, "", connect_timeout_ms, 60000);
    result.meta["peer"] = peer.name;
    result.meta["url"] = peer.url;
    result.meta["status"] = resp.status;
    if (!resp.ok) {
      result.output = "error: cannot reach the peer at " + peer.url + ": " +
                      (resp.error.empty() ? ("HTTP " + std::to_string(resp.status)) : resp.error);
      return result;
    }
    if (resp.status != 200) {
      result.output = "error: the peer refused the request (HTTP " +
                      std::to_string(resp.status) + "): " + peer_error_message(resp);
      return result;
    }
    bool truncated = false;
    result.output = limit_output(resp.body, out_limit, &truncated);
    result.truncated = truncated;
    result.ok = true;
    return result;
  }

  if (action == "exec") {
    const std::string command = arg_str(args, "command");
    if (trim(command).empty()) {
      result.output = "error: 'command' is required";
      return result;
    }
    nlohmann::json payload = nlohmann::json::object();
    payload["command"] = command;
    payload["timeout_ms"] = static_cast<long long>(timeout_s) * 1000;
    const std::string cwd = arg_str(args, "cwd");
    if (!cwd.empty()) payload["cwd"] = cwd;
    LOG_INFO("toolkit_peer: exec on ", peer.url, ": ", sanitize_for_terminal(command));
    HttpResponse resp = peer_call(peer, "POST", "/api/exec", dump_json(payload),
                                   connect_timeout_ms, timeout_s * 1000 + 30000);
    result.meta["peer"] = peer.name;
    result.meta["url"] = peer.url;
    result.meta["status"] = resp.status;
    if (!resp.ok) {
      result.output = "error: cannot reach the peer at " + peer.url + ": " +
                      (resp.error.empty() ? ("HTTP " + std::to_string(resp.status)) : resp.error);
      return result;
    }
    if (resp.status != 200) {
      result.output = "error: the peer refused the command (HTTP " +
                      std::to_string(resp.status) + "): " + peer_error_message(resp);
      return result;
    }
    nlohmann::json answer;
    bool parsed_ok = false;
    try {
      answer = nlohmann::json::parse(resp.body);
      parsed_ok = true;
    } catch (const std::exception&) {
    }
    std::string body = resp.body;
    if (parsed_ok && answer.contains("output")) {
      body = answer.value("output", std::string());
      const int exit_code = answer.value("exit_code", 0);
      result.ok = answer.value("ok", exit_code == 0);
      result.meta["exit_code"] = static_cast<long long>(exit_code);
    }
    bool truncated = false;
    result.output = peer_header(peer, "exec") + "\n" + limit_output(body, out_limit, &truncated);
    result.truncated = truncated;
    return result;
  }

  if (action == "stop") {
    const std::string session_id = arg_str(args, "session_id");
    if (session_id.empty()) {
      result.output = "error: 'session_id' is required (the peer's session)";
      return result;
    }
    nlohmann::json payload = nlohmann::json::object();
    payload["session_id"] = session_id;
    HttpResponse resp = peer_call(peer, "POST", "/api/chat/stop", dump_json(payload),
                                   connect_timeout_ms, 30000);
    if (!resp.ok || resp.status != 200) {
      result.output = "error: the peer did not stop that turn (HTTP " +
                      std::to_string(resp.status) + "): " + peer_error_message(resp);
      return result;
    }
    result.output = "asked the peer to stop session " + session_id;
    result.ok = true;
    return result;
  }

  if (action == "chat") {
    std::string prompt = arg_str(args, "prompt");
    if (prompt.empty()) prompt = arg_str(args, "message");
    if (trim(prompt).empty()) {
      result.output = "error: 'prompt' is required";
      return result;
    }
    std::string workspace_id;
    if (!peer_workspace(peer, arg_str(args, "workspace_id"), &workspace_id, &err)) {
      result.output = "error: " + err;
      return result;
    }
    nlohmann::json payload = nlohmann::json::object();
    payload["message"] = prompt;
    payload["workspace_id"] = workspace_id;
    const std::string session_id = arg_str(args, "session_id");
    if (!session_id.empty()) payload["session_id"] = session_id;

    PeerTurn turn;
    const std::atomic<bool>* cancel = ctx.cancel;
    // The peer answers in its own time: a long job on the other machine is
    // not a reason to give up here. The user can still stop the turn, and
    // then the transfer is cut.
    HttpChunkCallback on_chunk = [&turn, cancel](const char* data, std::size_t len) -> bool {
      if (cancel && cancel->load()) return false;
      turn.feed(data, len);
      return true;
    };
    LOG_INFO("toolkit_peer: chat on ", peer.url, " (workspace ", workspace_id, ")");
    HttpResponse resp = peer_call(peer, "POST", "/api/chat", dump_json(payload),
                                   connect_timeout_ms, timeout_s * 1000, on_chunk,
                                   [&turn]() { turn.reset(); });
    result.meta["peer"] = peer.name;
    result.meta["url"] = peer.url;
    result.meta["status"] = resp.status;
    result.meta["workspace_id"] = workspace_id;
    if (!turn.session_id.empty()) result.meta["session_id"] = turn.session_id;

    if (cancel && cancel->load()) {
      result.cancelled = true;
      result.output = "The call to the peer was interrupted. The peer may still be working on "
                      "it: ask it again with the same session_id to see what it has.";
      return result;
    }
    if (!resp.ok) {
      result.output = "error: cannot reach the peer at " + peer.url + ": " +
                      (resp.error.empty() ? ("HTTP " + std::to_string(resp.status)) : resp.error);
      return result;
    }
    // The peer answered with a JSON envelope instead of a stream: that happens
    // when the turn was queued or refused. Say what it said.
    if (resp.status != 200 && resp.body.find("data:") == std::string::npos) {
      result.output = "error: the peer refused the request (HTTP " +
                      std::to_string(resp.status) + "): " + peer_error_message(resp);
      return result;
    }
    if (!turn.error.empty()) {
      result.output = "error: the peer's agent failed: " + turn.error;
      return result;
    }
    if (turn.content.empty() && turn.steps.empty() && !turn.done) {
      result.output = "error: the peer answered nothing (HTTP " + std::to_string(resp.status) +
                      "). It may have queued the message instead of running it.";
      return result;
    }

    std::string out = peer_header(peer, "chat") + " (" + std::to_string(resp.elapsed_ms) +
                       "ms, " + std::to_string(turn.steps.size()) + " tool call(s) on the peer)\n";
    if (!turn.content.empty()) {
      out += turn.content + "\n";
    }
    if (!turn.steps.empty()) {
      out += "\n--- what the peer did ---\n";
      for (std::size_t i = 0; i < turn.steps.size(); ++i) {
        out += "- " + turn.steps[i] + "\n";
      }
    }
    if (!turn.session_id.empty()) {
      out += "\n(the peer's session is " + turn.session_id +
             " - use it to stop the turn or to continue the conversation)";
    }
    bool truncated = false;
    result.output = limit_output(out, out_limit, &truncated);
    result.truncated = truncated;
    result.ok = true;
    return result;
  }

  result.output = "error: unknown action '" + action +
                  "' (list, health, status, chat, exec, tools, workspaces, stop)";
  return result;
}

// ---------------------------------------------------------------------------
// the MCP tools
//
// The tools of an MCP server are published as `mcp__<server>__<tool>`, so the
// model sees them as one more tool of the agent. The result is the text the
// server returned; an image block becomes an attachment of the conversation,
// exactly like the one toolkit_load_image produces.
// ---------------------------------------------------------------------------
ToolResult tool_mcp(const std::string& name, const nlohmann::json& args, const ToolContext& ctx) {
  ToolResult result;
  if (!ctx.mcp) {
    result.ok = false;
    result.output = "error: no MCP server is configured";
    return result;
  }
  McpCallResult call;
  std::string err;
  if (!ctx.mcp->call(name, args, &call, &err)) {
    result.ok = false;
    result.output = "error: " + err;
    return result;
  }
  result.ok = !call.is_error;
  result.output = call.text;
  if (call.is_error) {
    result.output = "the MCP tool reported an error: " + result.output;
  }
  result.meta = nlohmann::json::object();
  result.meta["mcp_tool"] = name;

  // Images the server sent along: they are stored in the workspace and
  // attached to the conversation, so a model that can see them gets them.
  for (std::size_t i = 0; i < call.images.size(); ++i) {
    std::string bytes;
    if (!base64_decode(call.images[i].first, &bytes) || bytes.empty()) {
      result.output += "\n(the MCP server sent an image that could not be decoded)";
      continue;
    }
    const char* sniffed = sniff_image_mime(bytes);
    if (!sniffed) {
      result.output += "\n(the MCP server sent an image in a format this agent cannot show)";
      continue;
    }
    if (!ctx.workspace) {
      result.output +=
          "\n(the MCP server sent an image, but this turn has no workspace to store it in)";
      continue;
    }
    const std::string dir = path_join(ctx.workspace->artifacts_dir(), "images");
    if (!make_dirs(dir)) {
      result.output += "\n(cannot create " + dir + " to store the image the MCP server sent)";
      continue;
    }
    const std::string file = path_join(dir, "mcp_" + random_hex(8) + image_extension_for(sniffed));
    std::string write_error;
    if (!write_file(file, bytes, false, &write_error)) {
      result.output += "\n(cannot store the image the MCP server sent: " + write_error + ")";
      continue;
    }
    ImageAttachment att;
    att.mime = sniffed;
    att.path = ctx.workspace->display_path(file);
    result.images.push_back(att);
    result.meta["path"] = att.path;
    result.output += "\n(the MCP server also sent an image: " + att.path + ")";
  }
  return result;
}

}  // namespace

nlohmann::json ToolDef::to_openai() const {
  nlohmann::json j = nlohmann::json::object();
  j["type"] = "function";
  nlohmann::json fn = nlohmann::json::object();
  fn["name"] = name;
  fn["description"] = description;
  fn["parameters"] = parameters;
  j["function"] = fn;
  return j;
}

ToolRegistry::ToolRegistry() {
  {
    ToolDef def;
    def.name = "toolkit_exec";
    def.description =
        "Run a shell command on the host machine and return its merged stdout/stderr and exit "
        "code. On Windows the command runs through cmd.exe (or PowerShell when shell=\"powershell\"); "
        "on Linux/macOS through sh. Use this for inspecting the environment, building, running tests "
        "and any other system command. 执行系统命令并返回输出。";
    def.parameters = nlohmann::json::parse(R"JSON({
      "type": "object",
      "properties": {
        "command": {
          "type": "string",
          "description": "The command line to execute, e.g. \"cmake --build build\" or \"dir /b\"."
        },
        "cwd": {
          "type": "string",
          "description": "Working directory. Relative paths resolve against the workspace root. Defaults to the workspace root."
        },
        "timeout_ms": {
          "type": "integer",
          "description": "Kill the command after this many milliseconds. Default 180000."
        },
        "shell": {
          "type": "string",
          "enum": ["cmd", "powershell", "pwsh", "bash", "sh"],
          "description": "Shell to use. Defaults to the platform default (cmd on Windows, sh elsewhere)."
        }
      },
      "required": ["command"]
    })JSON");
    defs_.push_back(def);
  }
  {
    ToolDef def;
    def.name = "toolkit_file";
    def.description =
        "Read, write and manage files inside the workspace. Operations: read (return file "
        "content), write (create/overwrite), append, list (directory listing), mkdir, delete, move, "
        "stat. Relative paths resolve against the workspace root. Binary files (images) come back "
        "as bytes as text - use toolkit_load_image when you need to actually see a picture. "
        "文件读写与目录操作。";
    def.parameters = nlohmann::json::parse(R"JSON({
      "type": "object",
      "properties": {
        "op": {
          "type": "string",
          "enum": ["read", "write", "append", "list", "mkdir", "delete", "move", "stat"],
          "description": "The operation to perform."
        },
        "path": {
          "type": "string",
          "description": "Target path, relative to the workspace root (or absolute inside it)."
        },
        "content": {
          "type": "string",
          "description": "Content to write. Required for op=write and op=append."
        },
        "dest": {
          "type": "string",
          "description": "Destination path. Required for op=move."
        },
        "recursive": {
          "type": "boolean",
          "description": "For op=delete: allow deleting a non-empty directory."
        },
        "max_bytes": {
          "type": "integer",
          "description": "For op=read: maximum number of bytes to return."
        }
      },
      "required": ["op", "path"]
    })JSON");
    defs_.push_back(def);
  }
  {
    ToolDef def;
    def.name = "toolkit_workspace_search";
    def.description =
        "Search the workspace for a literal string: file names and file content. The query is "
        "compared byte for byte - no regular expression, no glob, no fuzzy matching. scope=name "
        "matches the path, scope=content matches the text inside the files, scope=both (default) "
        "does both. exact=true asks for a whole-string match (完全匹配): the path (or file name) "
        "has to equal the query, or the whole line has to equal it. Use it when you know the "
        "exact name you are looking for; leave it off for a normal search. "
        "在工作区里搜文件与内容（完全字符串匹配）。";
    def.parameters = nlohmann::json::parse(R"JSON({
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "The literal string to look for (compared byte for byte)."
        },
        "scope": {
          "type": "string",
          "enum": ["both", "name", "content"],
          "description": "What to match: file names, file content, or both (default both)."
        },
        "exact": {
          "type": "boolean",
          "description": "true => whole-string match (完全匹配) instead of 'contains'."
        },
        "case_sensitive": {
          "type": "boolean",
          "description": "true => case matters (default: it does not)."
        },
        "path": {
          "type": "string",
          "description": "Search only this subdirectory of the workspace (relative path)."
        },
        "max_results": {
          "type": "integer",
          "description": "Maximum number of hits to return (default 50)."
        }
      },
      "required": ["query"]
    })JSON");
    defs_.push_back(def);
  }
  {
    ToolDef def;
    def.name = "toolkit_memory";
    def.description =
        "Store and recall durable notes about this workspace (kept in "
        ".tea_agent_run/memory/memory.json). Use op=add to remember user preferences or project "
        "facts, and op=list to recall them. 工作区长期记忆的读写。";
    def.parameters = nlohmann::json::parse(R"JSON({
      "type": "object",
      "properties": {
        "op": { "type": "string", "enum": ["add", "list", "delete"] },
        "text": { "type": "string", "description": "Note content for op=add." },
        "id": { "type": "string", "description": "Note id for op=delete." }
      },
      "required": ["op"]
    })JSON");
    defs_.push_back(def);
  }
  {
    ToolDef def;
    def.name = "toolkit_todo";
    def.description =
        "Break a multi-step job into an explicit task list and keep it current: the list is "
        "shown to the user as live progress and injected into your prompt every turn. Use "
        "op=set (or replace) with the whole plan when you start a non-trivial task, then "
        "op=update to mark the step you are working on 'active' before starting it and 'done' "
        "immediately after finishing it. Exactly one step may be active at a time. Keep "
        "steps short and outcome-shaped (\"add the todo tool\", not \"work on stuff\"). A "
        "single trivial edit does not need a list. A plan whose steps are all done is cleared "
        "when the turn ends - you do not have to clear it yourself. "
        "复杂任务分解为阶段任务列表，并记录完成情况。";
    def.parameters = nlohmann::json::parse(R"JSON({
      "type": "object",
      "properties": {
        "op": {
          "type": "string",
          "enum": ["list", "set", "replace", "add", "update", "clear"],
          "description": "list: read it back; set/replace: write the whole plan; add: append one step; update: change one step's status/text; clear: drop the list."
        },
        "items": {
          "type": "array",
          "description": "For op=set/replace: the ordered steps. Each item is a string or {\"text\": \"...\", \"status\": \"pending|active|done\"}.",
          "items": {}
        },
        "text": {
          "type": "string",
          "description": "For op=add: the new step. For op=update: the replacement text."
        },
        "index": {
          "type": "integer",
          "description": "1-based step number, as printed by op=list."
        },
        "id": {
          "type": "string",
          "description": "Step id (t1, t2, ...) instead of index."
        },
        "status": {
          "type": "string",
          "enum": ["pending", "active", "done"],
          "description": "New status for op=update (or the initial status for op=add)."
        }
      },
      "required": ["op"]
    })JSON");
    defs_.push_back(def);
  }
  {
    ToolDef def;
    def.name = "toolkit_task";
    def.description =
        "Report on the task this turn belongs to. A task is work that was queued to be done "
        "later; the task server started it and watches it, and the report is what it sees. "
        "Use op=update to report the progress (0-100) and what you just did (note); use "
        "op=update with needs_confirmation=true and a question when the work cannot go on "
        "without the user - the task then waits for an answer instead of being marked done. "
        "汇报任务进度、中间状态，或请求用户确认。";
    def.parameters = nlohmann::json::parse(R"JSON({
      "type": "object",
      "properties": {
        "op": {
          "type": "string",
          "enum": ["update"],
          "description": "update: report the progress and the intermediate state."
        },
        "progress": {
          "type": "integer",
          "description": "How far the task is, 0-100."
        },
        "note": {
          "type": "string",
          "description": "What you just did - the intermediate state, one line."
        },
        "needs_confirmation": {
          "type": "boolean",
          "description": "true when the work cannot go on without the user. The task then waits for an answer."
        },
        "question": {
          "type": "string",
          "description": "What the user has to answer, when needs_confirmation is true."
        }
      },
      "required": ["op"]
    })JSON");
    defs_.push_back(def);
  }
  {
    ToolDef def;
    def.name = "toolkit_load_image";
    def.description =
        "Load an image file from disk (PNG, JPEG, GIF or WebP) and attach it to the conversation "
        "so you can actually look at it. Use this whenever the answer depends on seeing a "
        "picture: a screenshot, a UI mock, a chart, a photo, the output of an image-based test. "
        "toolkit_file cannot do this - it returns bytes as text. The file is copied into "
        ".tea_agent_run/artifacts/images/ (so later edits or deletions of the source do not "
        "break the session) and appears in the next request as an image. Requires a model with "
        "vision support. 从磁盘加载图片并附加到对话中，让模型真正“看到”它。";
    def.parameters = nlohmann::json::parse(R"JSON({
      "type": "object",
      "properties": {
        "path": {
          "type": "string",
          "description": "Image file to load. Relative paths resolve against the workspace root."
        },
        "note": {
          "type": "string",
          "description": "Optional reason to show next to the image, e.g. what to look for."
        }
      },
      "required": ["path"]
    })JSON");
    defs_.push_back(def);
  }
  {
    ToolDef def;
    def.name = "toolkit_agent_doc";
    def.description =
        "Read and maintain THIS workspace's experience log at .tea_agent_run/agent.md. It is "
        "injected into your system prompt every turn and survives across sessions, so it is where "
        "durable, project-specific knowledge belongs: pitfalls you hit, non-obvious build/test "
        "commands, scripts you wrote under .tea_agent_run/scripts/, conventions to follow next "
        "time. Not for user preferences or one-off task state - use toolkit_memory for those. "
        "op=read returns the document; op=append adds one dated entry; op=replace rewrites the "
        "whole document (use it to merge/drop stale entries when appending complains about the "
        "size limit); op=scripts lists reusable scripts. 读取/维护工作区经验文件 agent.md。";
    def.parameters = nlohmann::json::parse(R"JSON({
      "type": "object",
      "properties": {
        "op": { "type": "string", "enum": ["read", "append", "replace", "scripts"] },
        "text": {
          "type": "string",
          "description": "For op=append: one terse entry (a pitfall, a command, a script path). Multi-line is allowed but keep it short."
        },
        "content": {
          "type": "string",
          "description": "For op=replace: the complete new document, usually the result of compacting what op=read returned."
        }
      },
      "required": ["op"]
    })JSON");
    defs_.push_back(def);
  }
  {
    ToolDef def;
    def.name = "toolkit_search";
    def.description =
        "Search the web through the provider's own search endpoint and get structured results "
        "(title, url, date, summary). Prefer this over scraping search engines with curl. Query "
        "syntax: site:example.com and -site:example.com to restrict/exclude sites, \"exact "
        "phrase\" for phrase matching, -word or -\"phrase\" to exclude, lang:zh,en for "
        "language filters, after:2025-01-01 / before:2026-06-01 for date filters; operators "
        "combine. The provider bills every call, so keep count small. Only available when the "
        "current provider declares supports_search, or when a search provider is configured for "
        "this installation. 按查询词搜索网页并返回结构化结果。";
    def.parameters = nlohmann::json::parse(R"JSON({
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "Search query, 1-128 characters. Supports site:, -site:, \"phrase\", -word, lang:xx and after:/before: date operators."
        },
        "count": {
          "type": "integer",
          "description": "Results to return, 1-50, default 10. Price per call does not depend on count."
        },
        "ori_question": {
          "type": "string",
          "description": "Optional: the user's original question behind this search (1-128 chars); helps intent understanding."
        },
        "localization": {
          "type": "string",
          "description": "Optional geolocation hint: an IPv4/IPv6 literal (e.g. \"203.0.113.5\") used to prioritise nearby results. NOT a language code - for language use the lang:zh / lang:en operator inside query."
        },
        "need_content": {
          "type": "boolean",
          "description": "Optional: also return raw page text per result. Default false."
        }
      },
      "required": ["query"]
    })JSON");
    defs_.push_back(def);
  }
  {
    ToolDef def;
    def.name = "toolkit_fetch";
    def.description =
        "Fetch one web page and return its title and Markdown body. Use after toolkit_search "
        "picked a URL, or when a specific page must be read in full. The provider bills every "
        "call. Only available when the current provider declares supports_fetch, or when a search "
        "provider is configured for this installation. 抓取单个网页,返回 Markdown 正文。";
    def.parameters = nlohmann::json::parse(R"JSON({
      "type": "object",
      "properties": {
        "url": {
          "type": "string",
          "description": "The page to fetch: an http(s) URL without credentials, at most 8192 characters."
        }
      },
      "required": ["url"]
    })JSON");
    defs_.push_back(def);
  }
  {
    ToolDef def;
    def.name = "toolkit_fleet";
    def.description =
        "Manage the group of devices this server looks after (the school fleet). Actions: "
        "status (how every device is), devices (the inventory), report (today's report), "
        "collect (ask every device how it is, now), exec (run a command on the selected "
        "devices), upgrade (upgrade a package on them), ask (let each device's OWN agent "
        "answer a prompt - they think with their own context). The exec and upgrade actions "
        "take a rollout policy: canary (try this many devices first and stop if they fail), "
        "batch_size (devices per batch), pause_s (seconds between batches), max_failure_rate "
        "(stop the run when the failure rate is over this), verify (a command that must "
        "succeed afterwards) and rollback (a command that undoes the change). "
        "运维机群：查看设备状态、下发批量命令、滚动升级。";
    def.parameters = nlohmann::json::parse(R"JSON({
      "type": "object",
      "properties": {
        "action": {
          "type": "string",
          "enum": ["status", "devices", "report", "collect", "exec", "upgrade", "ask"],
          "description": "What to do."
        },
        "command": {
          "type": "string",
          "description": "The command to run on the devices (action=exec)."
        },
        "package": {
          "type": "string",
          "description": "The package to upgrade (action=upgrade). The package manager is detected on the device."
        },
        "prompt": {
          "type": "string",
          "description": "The question to ask the devices' own agents (action=ask)."
        },
        "groups": {
          "type": "array",
          "items": {"type": "string"},
          "description": "Only the devices in these groups. Empty = every device."
        },
        "devices": {
          "type": "array",
          "items": {"type": "string"},
          "description": "Only these devices, by name."
        },
        "labels": {
          "type": "object",
          "description": "Only the devices with these labels, e.g. room=A."
        },
        "canary": {
          "type": "integer",
          "description": "Try this many devices first; if they fail, leave the rest alone. Default 1 for an upgrade, 0 otherwise."
        },
        "batch_size": {
          "type": "integer",
          "description": "How many devices per batch. Default 5 for an upgrade, 0 (all at once) otherwise."
        },
        "pause_s": {
          "type": "integer",
          "description": "Seconds to wait between batches. Default 30 for an upgrade."
        },
        "max_failure_rate": {
          "type": "number",
          "description": "Stop the run when the failure rate is over this (0 = never stop)."
        },
        "timeout_s": {
          "type": "integer",
          "description": "How long one command may take on one device. Default 600 for an upgrade, 120 otherwise."
        },
        "verify": {
          "type": "string",
          "description": "A command that must succeed after the change; a device that fails it counts as failed."
        },
        "rollback": {
          "type": "string",
          "description": "A command that undoes the change, run on the devices that failed."
        },
        "dry_run": {
          "type": "boolean",
          "description": "Show what would happen without touching a device."
        }
      },
      "required": ["action"]
    })JSON");
    defs_.push_back(def);
  }
  {
    ToolDef def;
    def.name = "toolkit_peer";
    def.description =
        "Drive another tea_a2 (a peer) on another machine. Actions: list (the peers this "
        "installation knows), health (is it up, which version), status (its snapshot), "
        "chat (ask ITS agent to do something - it thinks with its own provider, its own "
        "tools and its own context, and the answer comes back with a trace of what it did), "
        "exec (run one command there, no model involved), tools (what that machine can do), "
        "workspaces (its workspaces) and stop (end a turn running there). Address a peer by "
        "name (peer=\"lab-pc\") or by address (url=\"http://172.16.1.31:8383\"); the secret "
        "stays in the settings, never in the call. "
        "级联：让另一台机器上的 tea_a2 干活。";
    def.parameters = nlohmann::json::parse(R"JSON({
      "type": "object",
      "properties": {
        "action": {
          "type": "string",
          "enum": ["list", "health", "status", "chat", "exec", "tools", "workspaces", "stop"],
          "description": "What to do."
        },
        "peer": {
          "type": "string",
          "description": "The name of a configured peer (see the peers: section of the settings)."
        },
        "url": {
          "type": "string",
          "description": "The address of a peer that is not configured, e.g. http://172.16.1.31:8383. It gets no secret unless a configured peer has the same address."
        },
        "prompt": {
          "type": "string",
          "description": "What the peer's own agent should do (action=chat). It answers in its own words."
        },
        "command": {
          "type": "string",
          "description": "The command to run on the peer (action=exec). No model is involved."
        },
        "cwd": {
          "type": "string",
          "description": "The directory to run the command in (action=exec)."
        },
        "workspace_id": {
          "type": "string",
          "description": "Which workspace on the peer the chat runs in. Default: the one the settings name, else the peer's first."
        },
        "session_id": {
          "type": "string",
          "description": "The peer's session: continue a conversation in it (action=chat), or end the turn running in it (action=stop)."
        },
        "timeout_s": {
          "type": "integer",
          "description": "How long to wait for the peer. Default 900 seconds."
        }
      },
      "required": ["action"]
    })JSON");
    defs_.push_back(def);
  }
}

nlohmann::json ToolRegistry::openai_tools(const ProviderInfo* provider,
                                           const Settings* settings,
                                           const AppConfig* config,
                                           bool include_mcp) const {
  nlohmann::json arr = nlohmann::json::array();
  for (std::size_t i = 0; i < defs_.size(); ++i) {
    const ToolDef& def = defs_[i];
    // Provider-scoped web tools are opt-in: without a provider that can
    // actually serve them they would only produce "unknown tool" round trips.
    // The turn's own provider counts, and so does the independent search
    // provider from the `search:` section - that is how a chat on a provider
    // without web access still gets the web tools.
    if (def.name == "toolkit_search" && !web_tool_available(provider, config, true)) continue;
    if (def.name == "toolkit_fetch" && !web_tool_available(provider, config, false)) continue;
    // The fleet tool needs a fleet service to talk to. Without one the model
    // would only learn about it by calling it and failing.
    if (def.name == "toolkit_fleet" && !(settings && !settings->fleet_url.empty())) continue;
    arr.push_back(def.to_openai());
  }
  // The tools of the MCP servers join the agent's own tool set. A server that
  // is not reachable contributes nothing: a model that is shown a tool that
  // cannot work only wastes a turn on it.
  if (mcp_ && include_mcp) {
    const nlohmann::json mcp_tools = mcp_->openai_tools();
    for (std::size_t i = 0; i < mcp_tools.size(); ++i) arr.push_back(mcp_tools[i]);
  }
  return arr;
}

bool ToolRegistry::has(const std::string& name) const {
  for (std::size_t i = 0; i < defs_.size(); ++i) {
    if (defs_[i].name == name) return true;
  }
  return false;
}

ToolResult ToolRegistry::execute(const std::string& name, const nlohmann::json& arguments,
                                 const ToolContext& ctx) const {
  ToolResult result;
  try {
    if (name == "toolkit_exec") result = tool_exec(arguments, ctx);
    else if (name == "toolkit_file") result = tool_file(arguments, ctx);
    else if (name == "toolkit_workspace_search") result = tool_workspace_search(arguments, ctx);
    else if (name == "toolkit_memory") result = tool_memory(arguments, ctx);
    else if (name == "toolkit_todo") result = tool_todo(arguments, ctx);
    else if (name == "toolkit_load_image") result = tool_load_image(arguments, ctx);
    else if (name == "toolkit_task") result = tool_task(arguments, ctx);
    else if (name == "toolkit_agent_doc") result = tool_agent_doc(arguments, ctx);
    else if (name == "toolkit_search") result = tool_search(arguments, ctx);
    else if (name == "toolkit_fetch") result = tool_fetch(arguments, ctx);
    else if (name == "toolkit_fleet") result = tool_fleet(arguments, ctx);
    else if (name == "toolkit_peer") result = tool_peer(arguments, ctx);
    else if (mcp_ && starts_with(name, "mcp__")) result = tool_mcp(name, arguments, ctx);
    else {
      result.ok = false;
      result.output = "error: unknown tool '" + name + "'";
    }
  } catch (const std::exception& e) {
    result.ok = false;
    result.output = std::string("error: tool threw an exception: ") + e.what();
  }
  // Everything a tool hands back ends up in a JSON document: the model request,
  // the session file, the event stream. The JSON serialiser refuses bytes that
  // are not valid UTF-8, so a binary file, a command that prints raw bytes or
  // a Latin-1 file name used to kill the whole turn with
  // "invalid UTF-8 byte at index N". Replace what cannot be encoded instead.
  result.output = sanitize_utf8(result.output);
  return result;
}

}  // namespace tea
