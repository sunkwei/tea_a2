// server.h -- the HTTP server that exposes the agent to the browser.
#pragma once

#include <memory>
#include <string>

#include "config.h"
#include "tools.h"
#include "workspace.h"

namespace tea {

struct ServerOptions {
  std::string host = "127.0.0.1";
  int port = 8282;
  /// Optional directory holding index.html/app.js/app.css/tokens.css. When
  /// empty (or when a file is missing) the assets embedded in the binary are
  /// used instead.
  std::string web_dir;
};

class ApiServer {
 public:
  ApiServer(AppConfig* config, ToolRegistry* tools, WorkspaceRegistry* registry);
  ~ApiServer();

  ApiServer(const ApiServer&) = delete;
  ApiServer& operator=(const ApiServer&) = delete;

  bool start(const ServerOptions& options, std::string* err);
  void stop();
  bool is_running() const;
  int port() const;
  std::string url() const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
  ServerOptions options_;
};

}  // namespace tea
