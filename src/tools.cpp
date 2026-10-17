#include "tools.h"

#include <sstream>

#include "command.h"
#include "log.h"
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
  std::size_t limit = static_cast<std::size_t>(ctx.workspace->settings().max_tool_output);
  bool truncated_again = false;
  std::string text = out.str();
  text = limit_output(text, limit, &truncated_again);
  result.truncated = result.truncated || truncated_again;
  result.output = text;
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
        "stat. Relative paths resolve against the workspace root. 文件读写与目录操作。";
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
}

nlohmann::json ToolRegistry::openai_tools() const {
  nlohmann::json arr = nlohmann::json::array();
  for (std::size_t i = 0; i < defs_.size(); ++i) arr.push_back(defs_[i].to_openai());
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
