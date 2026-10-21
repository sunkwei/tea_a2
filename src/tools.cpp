#include "tools.h"

#include "output_filter.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <sstream>

#include "command.h"
#include "http.h"
#include "log.h"
#include "task.h"
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
  if (run.timed_out) out << " [timed out after " << options.timeout_ms << "ms]";
  out << " [exit code: " << run.exit_code << "]\n";
  if (run.output.empty()) {
    out << "(no output)";
  } else {
    out << run.output;
  }

  result.ok = (run.exit_code == 0) && !run.timed_out;
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
  result.output = j.dump(2);
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
// A task is work that was queued for later (see /api/tasks and sched/). The
// agent owns three things about it: how far it has come, what it just did, and
// whether a human has to confirm something before it goes on. The scheduler and
// the task list read them from the task record.

ToolResult tool_task(const nlohmann::json& args, const ToolContext& ctx) {
  ToolResult result;
  if (!ctx.workspace) {
    result.output = "error: no workspace is active";
    return result;
  }
  const std::string op = to_lower(trim(arg_str(args, "op", "update")));

  if (op == "list") {
    const std::vector<Task> tasks = TaskStore::list(*ctx.workspace);
    if (tasks.empty()) {
      result.ok = true;
      result.output = "no tasks in this workspace";
      return result;
    }
    std::string out;
    for (std::size_t i = 0; i < tasks.size(); ++i) {
      const Task& t = tasks[i];
      out += t.id + "  " + t.status + "  " + std::to_string(t.progress) + "%  " + t.title;
      if (t.needs_confirmation) out += "  [needs confirmation]";
      if (!t.note.empty()) out += "  - " + t.note;
      out += "\n";
    }
    result.ok = true;
    result.output = out;
    return result;
  }

  std::string id = trim(arg_str(args, "task_id"));
  if (id.empty() && ctx.task_id) id = *ctx.task_id;
  if (id.empty()) {
    result.output = "error: this turn is not part of a task, so there is nothing to report";
    return result;
  }

  if (op == "get") {
    Task t;
    std::string err;
    if (!TaskStore::load(*ctx.workspace, id, &t, &err)) {
      result.output = "error: " + err;
      return result;
    }
    result.ok = true;
    result.output = t.id + ": " + t.status + " (" + std::to_string(t.progress) + "%) " + t.title;
    if (!t.note.empty()) result.output += "\nlatest: " + t.note;
    if (t.needs_confirmation) result.output += "\nwaiting for the user: " + t.question;
    return result;
  }

  if (op != "update") {
    result.output = "error: op must be update, get or list";
    return result;
  }

  // ---- update -------------------------------------------------------------
  const bool has_progress = args.is_object() && args.contains("progress");
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
  bool wants_confirmation = false;
  bool has_confirmation = false;
  if (args.is_object() && args.contains("needs_confirmation")) {
    if (!args["needs_confirmation"].is_boolean()) {
      result.output = "error: 'needs_confirmation' must be true or false";
      return result;
    }
    wants_confirmation = args["needs_confirmation"].get<bool>();
    has_confirmation = true;
  }
  const std::string question = trim(arg_str(args, "question"));

  Task updated;
  std::string err;
  const bool ok = TaskStore::update(
      *ctx.workspace, id,
      [&](Task* t) {
        if (has_progress) t->progress = progress;
        if (!note.empty()) t->note = note;
        if (has_confirmation) t->needs_confirmation = wants_confirmation;
        if (!question.empty()) t->question = question;
        // The history of what happened, in order - this is the "intermediate
        // state" the task list shows.
        nlohmann::json step = nlohmann::json::object();
        step["at"] = now_iso8601();
        step["progress"] = t->progress;
        if (!note.empty()) step["note"] = note;
        if (has_confirmation) step["needs_confirmation"] = wants_confirmation;
        if (!question.empty()) step["question"] = question;
        t->steps.push_back(step);
        // Keep the history bounded: a long task would otherwise grow the file
        // without limit.
        while (t->steps.size() > 100) t->steps.erase(t->steps.begin());
      },
      &updated, &err);
  if (!ok) {
    result.output = "error: " + err;
    return result;
  }
  result.ok = true;
  result.output = "task " + updated.id + ": " + updated.status + " (" +
                  std::to_string(updated.progress) + "%)";
  if (updated.needs_confirmation) {
    result.output += " - waiting for the user";
    if (!updated.question.empty()) result.output += ": " + updated.question;
  }
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
  req.body = payload.dump();
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

ToolResult tool_search(const nlohmann::json& args, const ToolContext& ctx) {
  ToolResult result;
  if (!ctx.provider) {
    result.output = "error: no provider is active for this turn";
    return result;
  }
  if (!ctx.provider->supports_search) {
    result.output = "error: provider '" + ctx.provider->name +
                    "' does not support web search (set supports_search: true in provider.yaml)";
    return result;
  }
  if (!ctx.provider->has_key()) {
    result.output = "error: provider '" + ctx.provider->name + "' has no api_key configured";
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

  const std::string url =
      tool_endpoint_url(*ctx.provider, ctx.provider->search_path, "/v1/tools/search");
  if (url.empty()) {
    result.output = "error: provider '" + ctx.provider->name + "' has no usable api_url";
    return result;
  }
  LOG_INFO("toolkit_search: ", sanitize_for_terminal(query), " (count=", count, ")");
  const Settings st = ctx.config ? ctx.config->settings : Settings();
  const WebToolResponse resp = web_tool_post(url, ctx.provider->api_key, payload, st, ctx.cancel);
  result.meta["url"] = url;
  result.meta["status"] = resp.status;
  result.meta["elapsed_ms"] = resp.elapsed_ms;
  if (!resp.ok) {
    result.output = "error: " + resp.error;
    return result;
  }
  nlohmann::json::const_iterator results_it = resp.body.find("results");
  if (results_it == resp.body.end() || !results_it->is_array()) {
    result.output = "error: unexpected response shape (missing 'results' array): " +
                    truncate_utf8(resp.body.dump(), 2000);
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
  if (!ctx.provider) {
    result.output = "error: no provider is active for this turn";
    return result;
  }
  if (!ctx.provider->supports_fetch) {
    result.output = "error: provider '" + ctx.provider->name +
                    "' does not support page fetching (set supports_fetch: true in provider.yaml)";
    return result;
  }
  if (!ctx.provider->has_key()) {
    result.output = "error: provider '" + ctx.provider->name + "' has no api_key configured";
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
  const std::string endpoint =
      tool_endpoint_url(*ctx.provider, ctx.provider->fetch_path, "/v1/tools/fetch");
  if (endpoint.empty()) {
    result.output = "error: provider '" + ctx.provider->name + "' has no usable api_url";
    return result;
  }
  LOG_INFO("toolkit_fetch: ", sanitize_for_terminal(url));
  const Settings st = ctx.config ? ctx.config->settings : Settings();
  const WebToolResponse resp =
      web_tool_post(endpoint, ctx.provider->api_key, payload, st, ctx.cancel);
  result.meta["url"] = url;
  result.meta["endpoint"] = endpoint;
  result.meta["status"] = resp.status;
  result.meta["elapsed_ms"] = resp.elapsed_ms;
  if (!resp.ok) {
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
        "single trivial edit does not need a list. 复杂任务分解为阶段任务列表，并记录完成情况。";
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
        "Report on the task this turn belongs to. A task is work that was queued for "
        "later (POST /api/tasks): the scheduler starts it when the provider still has "
        "quota, and the task list shows how far it got. Use op=update to report the "
        "progress (0-100) and what you just did (note); use op=update with "
        "needs_confirmation=true and a question when the work cannot go on without the "
        "user - the task then waits for an answer instead of being marked done. op=get "
        "reads the task, op=list shows all of them. Without a task_id it refers to the "
        "task of this turn. 汇报任务进度、中间状态，或请求用户确认。";
    def.parameters = nlohmann::json::parse(R"JSON({
      "type": "object",
      "properties": {
        "op": {
          "type": "string",
          "enum": ["update", "get", "list"],
          "description": "update: report progress; get: read the task; list: all tasks of this workspace."
        },
        "task_id": {
          "type": "string",
          "description": "Which task. Optional: it defaults to the task of this turn."
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
        "current provider declares supports_search. 按查询词搜索网页并返回结构化结果。";
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
        "call. Only available when the current provider declares supports_fetch. 抓取单个网页,"
        "返回 Markdown 正文。";
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
}

nlohmann::json ToolRegistry::openai_tools(const ProviderInfo* provider) const {
  nlohmann::json arr = nlohmann::json::array();
  for (std::size_t i = 0; i < defs_.size(); ++i) {
    const ToolDef& def = defs_[i];
    // Provider-scoped web tools are opt-in: without the provider that can
    // actually serve them they would only produce "unknown tool" round trips.
    if (def.name == "toolkit_search" && !(provider && provider->supports_search)) continue;
    if (def.name == "toolkit_fetch" && !(provider && provider->supports_fetch)) continue;
    arr.push_back(def.to_openai());
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
    if (name == "toolkit_exec") return tool_exec(arguments, ctx);
    if (name == "toolkit_file") return tool_file(arguments, ctx);
    if (name == "toolkit_memory") return tool_memory(arguments, ctx);
    if (name == "toolkit_todo") return tool_todo(arguments, ctx);
    if (name == "toolkit_load_image") return tool_load_image(arguments, ctx);
    if (name == "toolkit_task") return tool_task(arguments, ctx);
    if (name == "toolkit_agent_doc") return tool_agent_doc(arguments, ctx);
    if (name == "toolkit_search") return tool_search(arguments, ctx);
    if (name == "toolkit_fetch") return tool_fetch(arguments, ctx);
  } catch (const std::exception& e) {
    result.ok = false;
    result.output = std::string("error: tool threw an exception: ") + e.what();
    return result;
  }
  result.ok = false;
  result.output = "error: unknown tool '" + name + "'";
  return result;
}

}  // namespace tea
