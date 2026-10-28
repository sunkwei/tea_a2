// mcp.h -- Model Context Protocol: the agent as an MCP client.
//
// An MCP server is a separate program that offers tools over JSON-RPC. The
// agent starts one, asks it what it can do, and publishes those tools as
// `mcp__<server>__<tool>`, so a model can use them like any other tool.
//
// Only the stdio transport is implemented. That is what the servers that
// matter use (uiautomation-mcp, the filesystem server, ...), and it needs no
// port, no token and no network: the agent starts the program and talks to it
// over its stdin/stdout.
//
// The wire protocol is the MCP one: newline-delimited JSON-RPC 2.0 messages,
// `initialize` -> `notifications/initialized` -> `tools/list` -> `tools/call`.
#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

#include "config.h"

namespace tea {

/// One tool an MCP server offers.
struct McpTool {
  /// The server it belongs to (the name from mcp.yaml).
  std::string server;
  /// The name the server calls it by.
  std::string name;
  /// How the model sees it: `mcp__<server>__<name>`.
  std::string full_name;
  std::string description;
  /// The input schema (JSON Schema), as the server wrote it.
  nlohmann::json schema = nlohmann::json::object();
};

/// The answer to a tool call.
struct McpCallResult {
  /// The call itself worked (the server may still report a tool error).
  bool ok = false;
  /// All the text blocks of the answer, joined.
  std::string text;
  /// The server said the tool failed (`isError`).
  bool is_error = false;
  /// Images the server returned: base64 payload + mime type.
  std::vector<std::pair<std::string, std::string> > images;
  /// The raw JSON result, for the log and for the API.
  nlohmann::json raw = nlohmann::json::object();
};

/// One MCP server: the child process, the JSON-RPC session and its tools.
class McpClient {
 public:
  McpClient();
  ~McpClient();

  McpClient(const McpClient&) = delete;
  McpClient& operator=(const McpClient&) = delete;

  /// Start the server and do the handshake (initialize + tools/list).
  /// False with `err` set when the server cannot be reached or does not
  /// answer; nothing is left running in that case.
  bool connect(const McpServerConfig& cfg, std::string* err);

  /// Stop the server. The process is asked to quit and killed if it does not.
  /// Safe to call more than once.
  void disconnect();

  bool connected() const { return connected_.load(); }

  const std::vector<McpTool>& tools() const { return tools_; }
  const McpServerConfig& config() const { return cfg_; }
  /// What the server called itself (from the initialize answer).
  const std::string& server_name() const { return server_name_; }
  const std::string& server_version() const { return server_version_; }
  /// The protocol version the two sides agreed on.
  const std::string& protocol_version() const { return protocol_version_; }
  /// The last lines the server wrote to stderr. Servers log there, and it is
  /// the only place a failure usually explains itself.
  std::string stderr_tail() const;

  /// Call one tool of this server.
  bool call_tool(const std::string& tool, const nlohmann::json& arguments,
                 McpCallResult* out, std::string* err);

  /// The tool list as the model sees it (OpenAI function definitions).
  nlohmann::json openai_tools() const;

 private:
  struct Pending {
    bool done = false;
    nlohmann::json result = nlohmann::json::object();
    nlohmann::json error = nlohmann::json::object();
  };

  bool spawn(std::string* err);
  void reader_loop();
  void stderr_loop();
  /// Send a request and wait for its answer.
  bool request(const std::string& method, const nlohmann::json& params, int timeout_ms,
               nlohmann::json* result, nlohmann::json* error, std::string* err);
  /// Send a notification (no answer expected).
  bool notify(const std::string& method, const nlohmann::json& params);
  /// Write one JSON message (newline-delimited).
  bool write_message(const std::string& text);
  void handle_message(const nlohmann::json& message);
  void handle_raw(const std::string& text);
  void mark_dead(const std::string& why);

  McpServerConfig cfg_;
  std::vector<McpTool> tools_;
  std::string server_name_;
  std::string server_version_;
  std::string protocol_version_;

  std::atomic<bool> connected_{false};
  std::atomic<bool> stopping_{false};

  // The JSON-RPC bookkeeping.
  std::mutex mu_;
  std::condition_variable cv_;
  std::map<int64_t, std::shared_ptr<Pending> > pending_;
  int64_t next_id_ = 1;

  // The child's stderr, kept (capped) for the error messages.
  mutable std::mutex stderr_mu_;
  std::string stderr_;

  // The platform half: the process and its pipes. Shared with the reader
  // threads, so they can keep reading while the client is being torn down.
  struct Impl;
  std::shared_ptr<Impl> impl_;
  std::thread reader_thread_;
  std::thread stderr_thread_;
};

/// Every MCP server the agent knows about.
///
/// The servers are started on demand: a turn that needs a tool starts the
/// server, and a server that cannot be reached is not retried for a while (a
/// broken server must not slow every turn down).
class McpRegistry {
 public:
  McpRegistry();
  ~McpRegistry();

  McpRegistry(const McpRegistry&) = delete;
  McpRegistry& operator=(const McpRegistry&) = delete;

  /// Apply a configuration. Everything that was running is stopped first.
  void configure(const McpConfig& cfg);

  bool configured() const;
  bool enabled() const;

  /// The tools of every server that is up. Servers that cannot be reached are
  /// skipped; their error is in status_json().
  std::vector<McpTool> tools();

  /// The same tools as the model sees them (OpenAI function definitions).
  nlohmann::json openai_tools();

  /// Call a tool by its full name (`mcp__<server>__<tool>`).
  bool call(const std::string& full_name, const nlohmann::json& arguments,
            McpCallResult* out, std::string* err);

  /// The state of every server: for the UI and for /api/mcp/servers.
  nlohmann::json status_json();

  /// Stop and start every server again (the "refresh" button).
  nlohmann::json refresh();

  /// Connect every server that is not up yet, in the background of the
  /// caller's thread. Used at start-up so the first turn does not have to
  /// wait for the handshake.
  void warm_up();

  std::size_t server_count() const;

 private:
  struct Entry {
    McpServerConfig cfg;
    std::shared_ptr<McpClient> client;
    /// Why the server is not usable (empty when it is fine).
    std::string error;
    /// Do not try again before this moment (a broken server must not slow
    /// every turn down).
    int64_t retry_after_ms = 0;
  };

  /// Connect one server if it is not up yet. False with `err` set on failure.
  /// The handshake happens outside the lock (it takes time); `connect_mu_`
  /// makes sure only one handshake runs at a time.
  bool ensure_connected(std::size_t index, std::shared_ptr<McpClient>* client, std::string* err);

  mutable std::mutex mu_;
  /// Held while a server is being started, so two turns cannot start it twice.
  std::mutex connect_mu_;
  std::vector<Entry> entries_;
  McpConfig cfg_;
};

/// The name a tool is published under: `mcp__<server>__<tool>`.
///
/// OpenAI-compatible function names are limited to 64 characters of
/// `[A-Za-z0-9_-]`, so the parts are sanitised and, if the whole thing does
/// not fit, shortened with a hash that keeps it unique.
std::string mcp_tool_full_name(const std::string& server, const std::string& tool);

/// The tool description as the model should see it: the server's own text,
/// shortened to something that does not drown the request.
std::string mcp_tool_description(const McpTool& tool);

}  // namespace tea
