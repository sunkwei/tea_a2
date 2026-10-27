// server.h -- the HTTP server that exposes the agent to the browser.
#pragma once

#include <memory>
#include <string>
#include <vector>

#include "config.h"
#include "mcp.h"
#include "tools.h"
#include "workspace.h"

namespace tea {

/// `http://<host>:<port>` for a bind address, expanding a wildcard bind
/// (`0.0.0.0` / `::` / empty) to the machine's active IPv4 addresses. Free
/// function so the setup-mode hint can print the same list before the server
/// object exists.
std::vector<std::string> reachable_urls(const std::string& bind_host, int port);

struct ServerOptions {
  std::string host = "127.0.0.1";
  int port = 8282;
  /// Optional directory holding index.html/app.js/app.css/tokens.css. When
  /// empty (or when a file is missing) the assets embedded in the binary are
  /// used instead.
  std::string web_dir;
  /// Shared secret for the machine-level ops routes (/api/exec, /api/status).
  /// Empty means those routes are switched off entirely: an unauthenticated
  /// "run this command" endpoint on a device that may be reachable from the
  /// network is not something to ship by default.
  std::string api_token;
};

class ApiServer {
 public:
  /// `mcp` may be null: then the MCP routes report that no MCP server is
  /// configured instead of failing.
  ApiServer(AppConfig* config, ToolRegistry* tools, WorkspaceRegistry* registry,
            McpRegistry* mcp = nullptr);
  ~ApiServer();

  ApiServer(const ApiServer&) = delete;
  ApiServer& operator=(const ApiServer&) = delete;

  bool start(const ServerOptions& options, std::string* err);
  void stop();
  bool is_running() const;
  int port() const;
  /// The loopback URL. Always usable on this machine, which is why `--open`
  /// uses it even when the server is bound to every interface.
  std::string url() const;
  /// Every URL a browser can reach this server at, best first: a wildcard bind
  /// expands to the machine's active addresses (default route first) and ends
  /// with loopback; a specific bind yields just that address. Never empty: the
  /// loopback line is the fallback when the machine has no usable address.
  std::vector<std::string> urls() const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
  ServerOptions options_;
};

}  // namespace tea
