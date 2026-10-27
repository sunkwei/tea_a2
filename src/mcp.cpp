// mcp.cpp -- the MCP client: start a server, speak JSON-RPC to it, call its
// tools.
//
// The transport is stdio, the way the MCP servers that matter do it: the agent
// starts the program and writes newline-delimited JSON-RPC messages to its
// stdin; the answers come back on stdout. Everything the server writes to
// stderr is kept aside - that is where a .NET or Node server explains
// itself when it dies.
//
// The protocol handshake is the standard one:
//
//   -> initialize            (protocolVersion, capabilities, clientInfo)
//   <- result                (protocolVersion, serverInfo, capabilities)
//   -> notifications/initialized
//   -> tools/list            (what can this server do?)
//   <- result.tools[]
//   -> tools/call            (do this)
//   <- result.content[]      (text and image blocks)
//
// Only the client side of the protocol is implemented. A server that asks the
// client for something (sampling, roots) gets a "method not found" answer
// instead of silence: a server that waits forever for an answer it will
// never get is worse than one that knows the client cannot do it.
#include "mcp.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sstream>

#include "log.h"
#include "util.h"
#include "version.h"

#ifdef _WIN32
#include <windows.h>
#else
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#endif

namespace tea {
namespace {

/// The protocol version this client asks for.
///
/// 2024-11-05 is the version every implementation knows, including the .NET
/// SDK the Windows UI-automation server is built on. A server that only
/// speaks a newer one says so in the initialize answer, and the version it
/// answers with is what the session uses.
const char* kDefaultProtocolVersion = "2024-11-05";

/// The versions to try when the server refuses the one above.
const char* kFallbackProtocolVersions[] = {"2025-06-18", "2025-03-26", "2024-11-05"};

/// How much of a tool description the model gets. The UI-automation server
/// writes a paragraph per tool; a few of them would eat the request.
const std::size_t kMaxDescriptionBytes = 1200;

/// How much stderr is kept for the error messages.
const std::size_t kMaxStderrBytes = 16 * 1024;

/// The longest function name an OpenAI-compatible provider accepts.
const std::size_t kMaxToolNameBytes = 64;

std::string sanitize_name_part(const std::string& in) {
  std::string out;
  out.reserve(in.size());
  for (std::size_t i = 0; i < in.size(); ++i) {
    const unsigned char c = static_cast<unsigned char>(in[i]);
    const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
                    c == '_' || c == '-';
    out.push_back(ok ? static_cast<char>(c) : '_');
  }
  if (out.empty()) out = "x";
  return out;
}

/// A short, stable hash of a string (FNV-1a, 8 hex digits).
std::string short_hash(const std::string& in) {
  uint64_t h = 1469598103934665603ull;
  for (std::size_t i = 0; i < in.size(); ++i) {
    h ^= static_cast<unsigned char>(in[i]);
    h *= 1099511628211ull;
  }
  char buf[16];
  std::snprintf(buf, sizeof(buf), "%08x", static_cast<unsigned>(h & 0xffffffffull));
  return std::string(buf);
}

/// Case-insensitive comparison of the first `n` characters (portable: the
/// Windows build has no strncasecmp).
bool starts_with_ci(const std::string& s, const char* prefix, std::size_t n) {
  if (s.size() < n) return false;
  for (std::size_t i = 0; i < n; ++i) {
    char a = s[i];
    char b = prefix[i];
    if (a >= 'A' && a <= 'Z') a = static_cast<char>(a - 'A' + 'a');
    if (b >= 'A' && b <= 'Z') b = static_cast<char>(b - 'A' + 'a');
    if (a != b) return false;
  }
  return true;
}

/// Take one message out of the receive buffer.
///
/// The MCP stdio transport is newline-delimited JSON. Some servers speak the
/// LSP framing instead (`Content-Length: n\r\n\r\n<body>`), so both are
/// accepted - a server that frames its messages differently is still a server
/// the agent can use.
bool extract_message(std::string* buffer, std::string* out) {
  if (buffer->empty()) return false;
  // LSP-style framing?
  if (starts_with_ci(*buffer, "Content-Length:", 15)) {
    const std::size_t header_end = buffer->find("\r\n\r\n");
    const std::size_t alt_end = buffer->find("\n\n");
    std::size_t sep = std::string::npos;
    std::size_t sep_len = 0;
    if (header_end != std::string::npos) {
      sep = header_end;
      sep_len = 4;
    } else if (alt_end != std::string::npos) {
      sep = alt_end;
      sep_len = 2;
    }
    if (sep == std::string::npos) return false;  // the header is not complete yet
    const std::string header = buffer->substr(0, sep);
    const std::size_t pos = header.find("Content-Length:");
    if (pos == std::string::npos) {
      // A header we do not understand: drop it and try again.
      buffer->erase(0, sep + sep_len);
      return extract_message(buffer, out);
    }
    const std::size_t length = static_cast<std::size_t>(std::atoll(header.c_str() + pos + 15));
    if (buffer->size() < sep + sep_len + length) return false;  // the body is not here yet
    *out = buffer->substr(sep + sep_len, length);
    buffer->erase(0, sep + sep_len + length);
    return true;
  }
  const std::size_t nl = buffer->find('\n');
  if (nl == std::string::npos) return false;
  std::string line = buffer->substr(0, nl);
  buffer->erase(0, nl + 1);
  while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
  if (line.empty()) return extract_message(buffer, out);
  *out = line;
  return true;
}

/// The JSON of a JSON-RPC error, as the MCP servers report them.
std::string describe_rpc_error(const nlohmann::json& error) {
  if (!error.is_object()) return "the server reported an error";
  if (error.contains("message") && error["message"].is_string()) {
    return error["message"].get<std::string>();
  }
  return dump_json(error);
}

#ifdef _WIN32

/// Quote one argument the way a Windows program expects it (the rules of the
/// Microsoft C runtime: a backslash is only special in front of a quote).
std::string quote_windows_arg(const std::string& arg) {
  if (!arg.empty() && arg.find_first_of(" \t\n\v\"") == std::string::npos) return arg;
  std::string out = "\"";
  std::size_t backslashes = 0;
  for (std::size_t i = 0; i < arg.size(); ++i) {
    const char c = arg[i];
    if (c == '\\') {
      ++backslashes;
      continue;
    }
    if (c == '"') {
      out.append(backslashes * 2 + 1, '\\');
      backslashes = 0;
    } else {
      out.append(backslashes, '\\');
      backslashes = 0;
    }
    out.push_back(c);
  }
  out.append(backslashes * 2, '\\');
  out.push_back('"');
  return out;
}

/// Build the environment block for the child: the environment of this
/// process, with the configured variables added or replaced.
std::wstring build_environment(const std::map<std::string, std::string>& extra) {
  std::map<std::wstring, std::wstring> vars;
  LPWCH block = GetEnvironmentStringsW();
  if (block) {
    const wchar_t* p = block;
    while (*p) {
      const wchar_t* eq = wcschr(p, L'=');
      if (eq && eq != p) {
        vars[std::wstring(p, eq - p)] = std::wstring(eq + 1);
      }
      p += wcslen(p) + 1;
    }
    FreeEnvironmentStringsW(block);
  }
  for (std::map<std::string, std::string>::const_iterator it = extra.begin(); it != extra.end();
       ++it) {
    vars[utf8_to_wide(it->first)] = utf8_to_wide(it->second);
  }
  // The block must be sorted by name and end with an extra NUL.
  std::vector<std::wstring> keys;
  for (std::map<std::wstring, std::wstring>::const_iterator it = vars.begin(); it != vars.end();
       ++it) {
    keys.push_back(it->first);
  }
  std::sort(keys.begin(), keys.end());
  std::wstring out;
  for (std::size_t i = 0; i < keys.size(); ++i) {
    out += keys[i];
    out.push_back(L'=');
    out += vars[keys[i]];
    out.push_back(L'\0');
  }
  out.push_back(L'\0');
  return out;
}

#endif  // _WIN32

}  // namespace

// ---------------------------------------------------------------------------
// the platform half: the child process and its pipes
// ---------------------------------------------------------------------------

struct McpClient::Impl {
#ifdef _WIN32
  HANDLE process = nullptr;
  HANDLE in_write = nullptr;   // we write here, the child reads it (stdin)
  HANDLE out_read = nullptr;   // we read here, the child writes it (stdout)
  HANDLE err_read = nullptr;
  DWORD pid = 0;
#else
  int pid = -1;
  int in_fd = -1;
  int out_fd = -1;
  int err_fd = -1;
#endif
};

McpClient::McpClient() : impl_(new Impl()) {}

McpClient::~McpClient() { disconnect(); }

std::string McpClient::stderr_tail() const {
  std::lock_guard<std::mutex> lock(stderr_mu_);
  return stderr_;
}

// ---------------------------------------------------------------------------
// starting the server
// ---------------------------------------------------------------------------

bool McpClient::spawn(std::string* err) {
  const McpServerConfig& cfg = cfg_;
  if (cfg.command.empty()) {
    if (err) *err = "no command to run";
    return false;
  }

#ifdef _WIN32
  SECURITY_ATTRIBUTES sa;
  sa.nLength = sizeof(sa);
  sa.lpSecurityDescriptor = nullptr;
  sa.bInheritHandle = TRUE;

  HANDLE child_in = nullptr;    // the child reads this
  HANDLE child_out = nullptr;   // the child writes this
  HANDLE child_err = nullptr;
  if (!CreatePipe(&child_in, &impl_->in_write, &sa, 0) ||
      !CreatePipe(&impl_->out_read, &child_out, &sa, 0) ||
      !CreatePipe(&impl_->err_read, &child_err, &sa, 0)) {
    if (err) *err = "cannot create the pipes for the MCP server: " + last_os_error();
    return false;
  }
  // The parent's ends must not be inherited by the child, or the child would
  // hold them open and the reader would never see the end of the output.
  SetHandleInformation(impl_->in_write, HANDLE_FLAG_INHERIT, 0);
  SetHandleInformation(impl_->out_read, HANDLE_FLAG_INHERIT, 0);
  SetHandleInformation(impl_->err_read, HANDLE_FLAG_INHERIT, 0);

  std::string command_line = quote_windows_arg(cfg.command);
  for (std::size_t i = 0; i < cfg.args.size(); ++i) {
    command_line += " ";
    command_line += quote_windows_arg(cfg.args[i]);
  }
  std::wstring wide_command = utf8_to_wide(command_line);

  STARTUPINFOW si;
  ZeroMemory(&si, sizeof(si));
  si.cb = sizeof(si);
  si.dwFlags = STARTF_USESTDHANDLES;
  si.hStdInput = child_in;
  si.hStdOutput = child_out;
  si.hStdError = child_err;
  si.wShowWindow = SW_HIDE;

  std::wstring env_block = build_environment(cfg.env);
  PROCESS_INFORMATION pi;
  ZeroMemory(&pi, sizeof(pi));
  const std::wstring workdir = cfg.cwd.empty() ? std::wstring() : utf8_to_wide(cfg.cwd);
  const BOOL ok = CreateProcessW(
      nullptr, &wide_command[0], nullptr, nullptr, TRUE, CREATE_NO_WINDOW,
      const_cast<wchar_t*>(env_block.c_str()),
      workdir.empty() ? nullptr : workdir.c_str(), &si, &pi);
  // The child has its own copies of the pipe ends now.
  CloseHandle(child_in);
  CloseHandle(child_out);
  CloseHandle(child_err);
  if (!ok) {
    if (err) *err = "cannot start '" + cfg.command + "': " + last_os_error();
    CloseHandle(impl_->in_write);
    CloseHandle(impl_->out_read);
    CloseHandle(impl_->err_read);
    impl_->in_write = nullptr;
    impl_->out_read = nullptr;
    impl_->err_read = nullptr;
    return false;
  }
  impl_->process = pi.hProcess;
  impl_->pid = pi.dwProcessId;
  CloseHandle(pi.hThread);
#else
  int in_pipe[2] = {-1, -1};
  int out_pipe[2] = {-1, -1};
  int err_pipe[2] = {-1, -1};
  if (pipe(in_pipe) != 0 || pipe(out_pipe) != 0 || pipe(err_pipe) != 0) {
    if (err) *err = "cannot create the pipes for the MCP server: " + last_os_error();
    for (int i = 0; i < 2; ++i) {
      if (in_pipe[i] >= 0) close(in_pipe[i]);
      if (out_pipe[i] >= 0) close(out_pipe[i]);
      if (err_pipe[i] >= 0) close(err_pipe[i]);
    }
    return false;
  }
  const pid_t pid = fork();
  if (pid < 0) {
    if (err) *err = "cannot fork for the MCP server: " + last_os_error();
    for (int i = 0; i < 2; ++i) {
      close(in_pipe[i]);
      close(out_pipe[i]);
      close(err_pipe[i]);
    }
    return false;
  }
  if (pid == 0) {
    // The child: stdin from the pipe, stdout and stderr to the pipes.
    // Its own process group, so the whole tree can be killed later.
    setpgid(0, 0);
    dup2(in_pipe[0], STDIN_FILENO);
    dup2(out_pipe[1], STDOUT_FILENO);
    dup2(err_pipe[1], STDERR_FILENO);
    close(in_pipe[0]);
    close(in_pipe[1]);
    close(out_pipe[0]);
    close(out_pipe[1]);
    close(err_pipe[0]);
    close(err_pipe[1]);
    if (!cfg.cwd.empty()) {
      if (chdir(cfg.cwd.c_str()) != 0) _exit(126);
    }
    for (std::map<std::string, std::string>::const_iterator it = cfg.env.begin();
         it != cfg.env.end(); ++it) {
      setenv(it->first.c_str(), it->second.c_str(), 1);
    }
    std::vector<std::string> args;
    args.push_back(cfg.command);
    for (std::size_t i = 0; i < cfg.args.size(); ++i) args.push_back(cfg.args[i]);
    std::vector<char*> argv;
    for (std::size_t i = 0; i < args.size(); ++i) {
      argv.push_back(const_cast<char*>(args[i].c_str()));
    }
    argv.push_back(nullptr);
    execvp(argv[0], argv.data());
    _exit(127);
  }
  // The parent.
  setpgid(pid, pid);   // the child may not have run yet; ignoring the error is fine
  close(in_pipe[0]);
  close(out_pipe[1]);
  close(err_pipe[1]);
  impl_->pid = pid;
  impl_->in_fd = in_pipe[1];
  impl_->out_fd = out_pipe[0];
  impl_->err_fd = err_pipe[0];
#endif
  return true;
}

// ---------------------------------------------------------------------------
// the reader threads
// ---------------------------------------------------------------------------

void McpClient::reader_loop() {
  // The shared pointer keeps the pipes alive while this thread reads them.
  std::shared_ptr<Impl> impl = impl_;
  std::string buffer;
  char chunk[8192];
  for (;;) {
#ifdef _WIN32
    DWORD got = 0;
    if (!ReadFile(impl->out_read, chunk, sizeof(chunk), &got, nullptr)) break;
    if (got == 0) break;
    buffer.append(chunk, got);
#else
    const ssize_t got = read(impl->out_fd, chunk, sizeof(chunk));
    if (got <= 0) break;
    buffer.append(chunk, static_cast<std::size_t>(got));
#endif
    std::string message;
    while (extract_message(&buffer, &message)) handle_raw(message);
  }
  mark_dead("the MCP server closed its output");
}

void McpClient::stderr_loop() {
  std::shared_ptr<Impl> impl = impl_;
  char chunk[4096];
  for (;;) {
#ifdef _WIN32
    DWORD got = 0;
    if (!ReadFile(impl->err_read, chunk, sizeof(chunk), &got, nullptr)) break;
    if (got == 0) break;
    std::lock_guard<std::mutex> lock(stderr_mu_);
    stderr_.append(chunk, got);
#else
    const ssize_t got = read(impl->err_fd, chunk, sizeof(chunk));
    if (got <= 0) break;
    std::lock_guard<std::mutex> lock(stderr_mu_);
    stderr_.append(chunk, static_cast<std::size_t>(got));
#endif
    if (stderr_.size() > kMaxStderrBytes) {
      stderr_.erase(0, stderr_.size() - kMaxStderrBytes);
    }
  }
}

void McpClient::mark_dead(const std::string& why) {
  {
    std::lock_guard<std::mutex> lock(mu_);
    // Every request that is still waiting has to be released: the caller must
    // not sit on its timeout when the server is already gone.
    for (std::map<int64_t, std::shared_ptr<Pending> >::iterator it = pending_.begin();
         it != pending_.end(); ++it) {
      if (!it->second->done) {
        it->second->done = true;
        it->second->error = nlohmann::json::object();
        it->second->error["code"] = -32000;
        it->second->error["message"] = why;
      }
    }
  }
  cv_.notify_all();
  connected_.store(false);
}

// ---------------------------------------------------------------------------
// JSON-RPC
// ---------------------------------------------------------------------------

bool McpClient::write_message(const std::string& text) {
  if (stopping_.load()) return false;
  const std::string line = text + "\n";
#ifdef _WIN32
  if (!impl_->in_write) return false;
  DWORD written = 0;
  if (!WriteFile(impl_->in_write, line.data(), static_cast<DWORD>(line.size()), &written, nullptr)) {
    return false;
  }
  return written == line.size();
#else
  if (impl_->in_fd < 0) return false;
  std::size_t written = 0;
  while (written < line.size()) {
    const ssize_t n = write(impl_->in_fd, line.data() + written, line.size() - written);
    if (n <= 0) {
      if (n < 0 && errno == EINTR) continue;
      return false;
    }
    written += static_cast<std::size_t>(n);
  }
  return true;
#endif
}

bool McpClient::notify(const std::string& method, const nlohmann::json& params) {
  nlohmann::json message = nlohmann::json::object();
  message["jsonrpc"] = "2.0";
  message["method"] = method;
  if (!params.is_null()) message["params"] = params;
  return write_message(dump_json(message));
}

bool McpClient::request(const std::string& method, const nlohmann::json& params, int timeout_ms,
                        nlohmann::json* result, nlohmann::json* error, std::string* err) {
  std::shared_ptr<Pending> pending(new Pending());
  int64_t id = 0;
  {
    std::lock_guard<std::mutex> lock(mu_);
    id = next_id_++;
    pending_[id] = pending;
  }

  nlohmann::json message = nlohmann::json::object();
  message["jsonrpc"] = "2.0";
  message["id"] = id;
  message["method"] = method;
  if (!params.is_null()) message["params"] = params;
  if (!write_message(dump_json(message))) {
    {
      std::lock_guard<std::mutex> lock(mu_);
      pending_.erase(id);
    }
    if (err) *err = "cannot write to the MCP server (it is gone?)";
    return false;
  }

  std::unique_lock<std::mutex> lock(mu_);
  const bool answered = cv_.wait_for(lock, std::chrono::milliseconds(timeout_ms),
                                     [&pending]() { return pending->done; });
  pending_.erase(id);
  if (!answered) {
    if (err) {
      *err = "the MCP server did not answer '" + method + "' within " +
             std::to_string(timeout_ms) + " ms";
    }
    return false;
  }
  if (pending->error.is_object() && !pending->error.empty()) {
    if (error) *error = pending->error;
    if (err) *err = describe_rpc_error(pending->error);
    return false;
  }
  if (result) *result = pending->result;
  return true;
}

void McpClient::handle_raw(const std::string& text) {
  nlohmann::json message;
  try {
    message = nlohmann::json::parse(sanitize_utf8(text));
  } catch (const std::exception& e) {
    LOG_WARN("mcp: cannot parse a message from ", cfg_.name, ": ", e.what());
    return;
  }
  handle_message(message);
}

void McpClient::handle_message(const nlohmann::json& message) {
  if (!message.is_object()) return;

  // A request from the server (sampling, roots, ...). We do not implement
  // those, but the server is waiting for an answer: give it one that says so.
  if (message.contains("method") && message.contains("id") && !message.contains("result")) {
    nlohmann::json reply = nlohmann::json::object();
    reply["jsonrpc"] = "2.0";
    reply["id"] = message["id"];
    nlohmann::json error = nlohmann::json::object();
    error["code"] = -32601;
    error["message"] = "the tea_agent MCP client does not implement this request";
    if (message["method"].is_string()) {
      error["message"] += " (" + message["method"].get<std::string>() + ")";
    }
    reply["error"] = error;
    write_message(dump_json(reply));
    return;
  }

  if (!message.contains("id")) return;   // a notification: nothing to do
  if (!message["id"].is_number_integer()) return;

  const int64_t id = message["id"].get<int64_t>();
  std::shared_ptr<Pending> pending;
  {
    std::lock_guard<std::mutex> lock(mu_);
    std::map<int64_t, std::shared_ptr<Pending> >::iterator it = pending_.find(id);
    if (it == pending_.end()) return;   // an answer to a request that timed out
    pending = it->second;
    pending->done = true;
    if (message.contains("result")) pending->result = message["result"];
    if (message.contains("error")) pending->error = message["error"];
  }
  cv_.notify_all();
}

// ---------------------------------------------------------------------------
// connect / disconnect
// ---------------------------------------------------------------------------

bool McpClient::connect(const McpServerConfig& cfg, std::string* err) {
  disconnect();
  cfg_ = cfg;
  stopping_.store(false);
  if (!cfg.enabled) {
    if (err) *err = "the server is disabled";
    return false;
  }
  if (!spawn(err)) return false;

  reader_thread_ = std::thread(&McpClient::reader_loop, this);
  stderr_thread_ = std::thread(&McpClient::stderr_loop, this);

  // The handshake. A server that refuses the protocol version we asked for
  // says so; the versions below are tried in turn before giving up.
  const std::string wanted =
      cfg.protocol_version.empty() ? kDefaultProtocolVersion : cfg.protocol_version;
  nlohmann::json result;
  nlohmann::json error;
  std::string last_error;
  bool ok = false;
  for (int attempt = 0; attempt < 4; ++attempt) {
    std::string version = wanted;
    if (attempt > 0) {
      const char* fallback = kFallbackProtocolVersions[attempt - 1];
      if (wanted == fallback) continue;
      version = fallback;
    }
    nlohmann::json params = nlohmann::json::object();
    params["protocolVersion"] = version;
    params["capabilities"] = nlohmann::json::object();
    nlohmann::json client_info = nlohmann::json::object();
    client_info["name"] = TEA_AGENT_NAME;
    client_info["version"] = tea::version_string();
    params["clientInfo"] = client_info;

    result = nlohmann::json::object();
    error = nlohmann::json::object();
    last_error.clear();
    if (request("initialize", params, cfg.startup_timeout_ms, &result, &error, &last_error)) {
      ok = true;
      break;
    }
    // Only a complaint about the version is worth another try.
    const std::string lowered = to_lower(last_error);
    if (lowered.find("version") == std::string::npos) break;
    LOG_WARN("mcp: ", cfg.name, " refused protocol version ", version, ": ", last_error);
  }
  if (!ok) {
    const std::string tail = stderr_tail();
    if (err) {
      *err = "the MCP server '" + cfg.name + "' did not initialize: " + last_error;
      if (!tail.empty()) *err += " (stderr: " + tail.substr(0, 400) + ")";
    }
    disconnect();
    return false;
  }

  if (result.contains("protocolVersion") && result["protocolVersion"].is_string()) {
    protocol_version_ = result["protocolVersion"].get<std::string>();
  } else {
    protocol_version_ = wanted;
  }
  if (result.contains("serverInfo") && result["serverInfo"].is_object()) {
    const nlohmann::json& info = result["serverInfo"];
    if (info.contains("name") && info["name"].is_string()) {
      server_name_ = info["name"].get<std::string>();
    }
    if (info.contains("version") && info["version"].is_string()) {
      server_version_ = info["version"].get<std::string>();
    }
  }

  // The server is only ready for calls after this notification.
  notify("notifications/initialized", nlohmann::json::object());

  // What can it do?
  nlohmann::json list;
  std::string list_error;
  if (!request("tools/list", nlohmann::json::object(), cfg.startup_timeout_ms, &list, nullptr,
               &list_error)) {
    if (err) {
      *err = "the MCP server '" + cfg.name + "' did not list its tools: " + list_error;
      const std::string tail = stderr_tail();
      if (!tail.empty()) *err += " (stderr: " + tail.substr(0, 400) + ")";
    }
    disconnect();
    return false;
  }

  tools_.clear();
  if (list.contains("tools") && list["tools"].is_array()) {
    const nlohmann::json& list_tools = list["tools"];
    for (std::size_t i = 0; i < list_tools.size(); ++i) {
      const nlohmann::json& t = list_tools[i];
      if (!t.is_object() || !t.contains("name") || !t["name"].is_string()) continue;
      McpTool tool;
      tool.server = cfg_.name;
      tool.name = t["name"].get<std::string>();
      tool.full_name = mcp_tool_full_name(cfg_.name, tool.name);
      if (t.contains("description") && t["description"].is_string()) {
        tool.description = t["description"].get<std::string>();
      }
      if (t.contains("inputSchema") && t["inputSchema"].is_object()) {
        tool.schema = t["inputSchema"];
      }
      tools_.push_back(tool);
    }
  }
  connected_.store(true);
  LOG_INFO("mcp: server '", cfg_.name, "' is up (", tools_.size(), " tool(s), protocol ",
           protocol_version_, ", server ", server_name_, " ", server_version_, ")");
  return true;
}

void McpClient::disconnect() {
  connected_.store(false);
  stopping_.store(true);
#ifdef _WIN32
  if (impl_->in_write) {
    CloseHandle(impl_->in_write);
    impl_->in_write = nullptr;
  }
  if (impl_->process) {
    // The server is asked to quit by closing its stdin; if it does not go in
    // a moment it is killed.
    WaitForSingleObject(impl_->process, 200);
    TerminateProcess(impl_->process, 1);
    CloseHandle(impl_->process);
    impl_->process = nullptr;
  }
  // The read handles are closed *after* the reader threads are done: closing a
  // handle another thread is blocked on is a race (the value can be reused).
#else
  if (impl_->in_fd >= 0) {
    close(impl_->in_fd);
    impl_->in_fd = -1;
  }
  if (impl_->pid > 0) {
    // The whole process group, so a server that spawned helpers goes too.
    kill(-impl_->pid, SIGTERM);
    int status = 0;
    for (int i = 0; i < 20; ++i) {
      const pid_t r = waitpid(impl_->pid, &status, WNOHANG);
      if (r == impl_->pid || r < 0) break;
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    kill(-impl_->pid, SIGKILL);
    waitpid(impl_->pid, &status, WNOHANG);
    impl_->pid = -1;
  }
#endif
  // Wake up everyone who is waiting for an answer that will never come.
  {
    std::lock_guard<std::mutex> lock(mu_);
    for (std::map<int64_t, std::shared_ptr<Pending> >::iterator it = pending_.begin();
         it != pending_.end(); ++it) {
      if (!it->second->done) {
        it->second->done = true;
        it->second->error = nlohmann::json::object();
        it->second->error["code"] = -32000;
        it->second->error["message"] = "the MCP server was stopped";
      }
    }
  }
  cv_.notify_all();
  // The reader threads end when the server is gone (the pipes reach their
  // end); joining them here keeps the destructor from pulling the ground from
  // under a running thread - and lets the pipes be closed safely afterwards.
  if (reader_thread_.joinable()) reader_thread_.join();
  if (stderr_thread_.joinable()) stderr_thread_.join();
#ifdef _WIN32
  if (impl_->out_read) {
    CloseHandle(impl_->out_read);
    impl_->out_read = nullptr;
  }
  if (impl_->err_read) {
    CloseHandle(impl_->err_read);
    impl_->err_read = nullptr;
  }
#else
  if (impl_->out_fd >= 0) {
    close(impl_->out_fd);
    impl_->out_fd = -1;
  }
  if (impl_->err_fd >= 0) {
    close(impl_->err_fd);
    impl_->err_fd = -1;
  }
#endif
}

// ---------------------------------------------------------------------------
// calling a tool
// ---------------------------------------------------------------------------

bool McpClient::call_tool(const std::string& tool, const nlohmann::json& arguments,
                           McpCallResult* out, std::string* err) {
  if (!connected()) {
    if (err) *err = "the MCP server '" + cfg_.name + "' is not connected";
    return false;
  }
  nlohmann::json params = nlohmann::json::object();
  params["name"] = tool;
  params["arguments"] = arguments.is_null() ? nlohmann::json::object() : arguments;

  nlohmann::json result;
  nlohmann::json error;
  std::string request_error;
  if (!request("tools/call", params, cfg_.timeout_ms, &result, &error, &request_error)) {
    if (err) *err = request_error;
    return false;
  }

  McpCallResult call;
  call.ok = true;
  call.raw = result;
  if (result.contains("isError") && result["isError"].is_boolean()) {
    call.is_error = result["isError"].get<bool>();
  }
  std::string text;
  if (result.contains("content") && result["content"].is_array()) {
    const nlohmann::json& content = result["content"];
    for (std::size_t i = 0; i < content.size(); ++i) {
      const nlohmann::json& block = content[i];
      if (!block.is_object()) continue;
      const std::string type = block.value("type", std::string("text"));
      if (type == "text" && block.contains("text") && block["text"].is_string()) {
        if (!text.empty()) text += "\n";
        text += block["text"].get<std::string>();
      } else if (type == "image" && block.contains("data") && block["data"].is_string()) {
        const std::string mime = block.value("mimeType", std::string("image/png"));
        call.images.push_back(std::make_pair(block["data"].get<std::string>(), mime));
      } else if (type == "resource" && block.contains("resource")) {
        // An embedded resource: show its text, if it has any.
        const nlohmann::json& res = block["resource"];
        if (res.is_object() && res.contains("text") && res["text"].is_string()) {
          if (!text.empty()) text += "\n";
          text += res["text"].get<std::string>();
        }
      }
    }
  }
  if (text.empty() && !result.contains("content")) {
    // Some servers answer with a plain object instead of content blocks.
    text = dump_json(result);
  }
  call.text = text;
  if (out) *out = call;
  return true;
}

nlohmann::json McpClient::openai_tools() const {
  nlohmann::json arr = nlohmann::json::array();
  for (std::size_t i = 0; i < tools_.size(); ++i) {
    const McpTool& tool = tools_[i];
    nlohmann::json fn = nlohmann::json::object();
    fn["name"] = tool.full_name;
    fn["description"] = mcp_tool_description(tool);
    nlohmann::json params = tool.schema.is_object() ? tool.schema : nlohmann::json::object();
    if (!params.contains("type")) params["type"] = "object";
    if (!params.contains("properties")) params["properties"] = nlohmann::json::object();
    fn["parameters"] = params;
    nlohmann::json entry = nlohmann::json::object();
    entry["type"] = "function";
    entry["function"] = fn;
    arr.push_back(entry);
  }
  return arr;
}

// ---------------------------------------------------------------------------
// the registry
// ---------------------------------------------------------------------------

McpRegistry::McpRegistry() {}

McpRegistry::~McpRegistry() {
  std::vector<std::shared_ptr<McpClient> > old;
  {
    std::lock_guard<std::mutex> lock(mu_);
    for (std::size_t i = 0; i < entries_.size(); ++i) {
      if (entries_[i].client) old.push_back(entries_[i].client);
    }
    entries_.clear();
  }
  for (std::size_t i = 0; i < old.size(); ++i) old[i]->disconnect();
}

void McpRegistry::configure(const McpConfig& cfg) {
  std::vector<std::shared_ptr<McpClient> > old;
  {
    std::lock_guard<std::mutex> lock(mu_);
    cfg_ = cfg;
    for (std::size_t i = 0; i < entries_.size(); ++i) {
      if (entries_[i].client) old.push_back(entries_[i].client);
    }
    entries_.clear();
    for (std::size_t i = 0; i < cfg.servers.size(); ++i) {
      Entry entry;
      entry.cfg = cfg.servers[i];
      entries_.push_back(entry);
    }
  }
  // Stop the old servers outside the lock: a disconnect waits for a process.
  for (std::size_t i = 0; i < old.size(); ++i) old[i]->disconnect();
}

bool McpRegistry::configured() const {
  std::lock_guard<std::mutex> lock(mu_);
  return cfg_.configured;
}

bool McpRegistry::enabled() const {
  std::lock_guard<std::mutex> lock(mu_);
  return cfg_.configured && cfg_.enabled;
}

std::size_t McpRegistry::server_count() const {
  std::lock_guard<std::mutex> lock(mu_);
  return entries_.size();
}

bool McpRegistry::ensure_connected(std::size_t index, std::shared_ptr<McpClient>* client,
                                   std::string* err) {
  McpServerConfig cfg;
  {
    std::lock_guard<std::mutex> lock(mu_);
    if (index >= entries_.size()) {
      if (err) *err = "there is no such MCP server";
      return false;
    }
    Entry& entry = entries_[index];
    if (entry.client && entry.client->connected()) {
      if (client) *client = entry.client;
      return true;
    }
    if (!entry.cfg.enabled) {
      if (err) *err = "the MCP server '" + entry.cfg.name + "' is disabled";
      entry.error = *err;
      return false;
    }
    if (!entry.cfg.error.empty()) {
      if (err) *err = entry.cfg.error;
      return false;
    }
    if (entry.retry_after_ms > now_ms()) {
      if (err) {
        *err = entry.error.empty() ? std::string("the MCP server '") + entry.cfg.name +
                                         "' is not available"
                                   : entry.error;
      }
      return false;
    }
    cfg = entry.cfg;
  }

  // The handshake takes time, so it happens outside the lock - and only one
  // at a time, so two turns cannot start the same server twice.
  std::lock_guard<std::mutex> connect_lock(connect_mu_);
  {
    std::lock_guard<std::mutex> lock(mu_);
    if (index < entries_.size() && entries_[index].client &&
        entries_[index].client->connected()) {
      if (client) *client = entries_[index].client;
      return true;
    }
  }

  std::shared_ptr<McpClient> fresh(new McpClient());
  std::string connect_error;
  if (!fresh->connect(cfg, &connect_error)) {
    {
      std::lock_guard<std::mutex> lock(mu_);
      if (index < entries_.size()) {
        entries_[index].error = connect_error;
        // Do not try again for a minute: a server that is not there must not
        // delay every turn.
        entries_[index].retry_after_ms = now_ms() + 60000;
      }
    }
    if (err) *err = connect_error;
    LOG_WARN("mcp: ", connect_error);
    return false;
  }
  {
    std::lock_guard<std::mutex> lock(mu_);
    if (index < entries_.size()) {
      entries_[index].client = fresh;
      entries_[index].error.clear();
      entries_[index].retry_after_ms = 0;
    }
  }
  if (client) *client = fresh;
  return true;
}

std::vector<McpTool> McpRegistry::tools() {
  std::vector<McpTool> all;
  if (!enabled()) return all;
  std::size_t count = server_count();
  for (std::size_t i = 0; i < count; ++i) {
    std::shared_ptr<McpClient> client;
    std::string err;
    if (!ensure_connected(i, &client, &err)) continue;
    if (!client) continue;
    const std::vector<McpTool>& tools = client->tools();
    for (std::size_t k = 0; k < tools.size(); ++k) all.push_back(tools[k]);
  }
  return all;
}

nlohmann::json McpRegistry::openai_tools() {
  nlohmann::json arr = nlohmann::json::array();
  const std::vector<McpTool> all = tools();
  for (std::size_t i = 0; i < all.size(); ++i) {
    const McpTool& tool = all[i];
    nlohmann::json fn = nlohmann::json::object();
    fn["name"] = tool.full_name;
    fn["description"] = mcp_tool_description(tool);
    nlohmann::json params = tool.schema.is_object() ? tool.schema : nlohmann::json::object();
    if (!params.contains("type")) params["type"] = "object";
    if (!params.contains("properties")) params["properties"] = nlohmann::json::object();
    fn["parameters"] = params;
    nlohmann::json entry = nlohmann::json::object();
    entry["type"] = "function";
    entry["function"] = fn;
    arr.push_back(entry);
  }
  return arr;
}

bool McpRegistry::call(const std::string& full_name, const nlohmann::json& arguments,
                        McpCallResult* out, std::string* err) {
  if (!enabled()) {
    if (err) *err = "no MCP server is configured";
    return false;
  }
  const std::size_t count = server_count();
  for (std::size_t i = 0; i < count; ++i) {
    std::shared_ptr<McpClient> client;
    std::string connect_error;
    if (!ensure_connected(i, &client, &connect_error)) continue;
    if (!client) continue;
    const std::vector<McpTool>& tools = client->tools();
    for (std::size_t k = 0; k < tools.size(); ++k) {
      if (tools[k].full_name != full_name) continue;
      return client->call_tool(tools[k].name, arguments, out, err);
    }
  }
  if (err) *err = "there is no MCP tool called '" + full_name + "'";
  return false;
}

nlohmann::json McpRegistry::status_json() {
  nlohmann::json j = nlohmann::json::object();
  std::lock_guard<std::mutex> lock(mu_);
  j["configured"] = cfg_.configured;
  j["enabled"] = cfg_.configured && cfg_.enabled;
  if (!cfg_.error.empty()) j["error"] = cfg_.error;
  nlohmann::json servers = nlohmann::json::array();
  for (std::size_t i = 0; i < entries_.size(); ++i) {
    Entry& entry = entries_[i];
    nlohmann::json s = nlohmann::json::object();
    s["name"] = entry.cfg.name;
    s["command"] = entry.cfg.command;
    s["args"] = entry.cfg.args;
    s["enabled"] = entry.cfg.enabled;
    s["connected"] = entry.client && entry.client->connected();
    if (!entry.error.empty()) s["error"] = entry.error;
    if (!entry.cfg.error.empty()) s["config_error"] = entry.cfg.error;
    nlohmann::json tools = nlohmann::json::array();
    if (entry.client && entry.client->connected()) {
      s["protocol_version"] = entry.client->protocol_version();
      s["server_name"] = entry.client->server_name();
      s["server_version"] = entry.client->server_version();
      const std::vector<McpTool>& list = entry.client->tools();
      for (std::size_t k = 0; k < list.size(); ++k) {
        nlohmann::json t = nlohmann::json::object();
        t["name"] = list[k].name;
        t["full_name"] = list[k].full_name;
        t["description"] = mcp_tool_description(list[k]);
        tools.push_back(t);
      }
    }
    s["tools"] = tools;
    s["tool_count"] = static_cast<int>(tools.size());
    servers.push_back(s);
  }
  j["servers"] = servers;
  return j;
}

nlohmann::json McpRegistry::refresh() {
  {
    std::lock_guard<std::mutex> lock(mu_);
    for (std::size_t i = 0; i < entries_.size(); ++i) {
      if (entries_[i].client) entries_[i].client->disconnect();
      entries_[i].client.reset();
      entries_[i].error.clear();
      entries_[i].retry_after_ms = 0;
    }
  }
  // Connect them one by one: the handshake takes time and the status of the
  // other servers must not wait for it.
  const std::size_t count = server_count();
  for (std::size_t i = 0; i < count; ++i) {
    std::shared_ptr<McpClient> client;
    std::string err;
    ensure_connected(i, &client, &err);
  }
  return status_json();
}

void McpRegistry::warm_up() {
  if (!enabled()) return;
  const std::size_t count = server_count();
  for (std::size_t i = 0; i < count; ++i) {
    std::shared_ptr<McpClient> client;
    std::string err;
    ensure_connected(i, &client, &err);
  }
}

// ---------------------------------------------------------------------------
// names
// ---------------------------------------------------------------------------

std::string mcp_tool_full_name(const std::string& server, const std::string& tool) {
  std::string server_part = sanitize_name_part(server);
  if (server_part.size() > 32) {
    server_part = server_part.substr(0, 23) + "_" + short_hash(server).substr(0, 8);
  }
  const std::string prefix = "mcp__" + server_part + "__";
  std::string name = sanitize_name_part(tool);
  // OpenAI-compatible function names are at most 64 characters long.
  if (prefix.size() + name.size() > kMaxToolNameBytes) {
    const std::size_t room =
        kMaxToolNameBytes > prefix.size() + 10 ? kMaxToolNameBytes - prefix.size() - 9 : 8;
    name = name.substr(0, room) + "_" + short_hash(tool).substr(0, 8);
  }
  return prefix + name;
}

std::string mcp_tool_description(const McpTool& tool) {
  std::string description = trim(tool.description);
  if (description.empty()) {
    description = "The MCP tool '" + tool.name + "' of the server '" + tool.server + "'.";
  } else {
    description = "MCP tool '" + tool.name + "' of the server '" + tool.server + "': " +
                  description;
  }
  bool truncated = false;
  const std::string shortened = truncate_utf8(description, kMaxDescriptionBytes, &truncated);
  if (truncated) return shortened + " ... (the description was shortened)";
  return shortened;
}

}  // namespace tea
