// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tools.h -- the tools the agent can call.
//
// Minimal viable set required by the task:
//   * toolkit_exec  -- run a system command
//   * toolkit_file  -- read / write files (plus list/mkdir/delete/move/stat)
// Plus toolkit_memory so the per-workspace memory directory is useful.
// Plus provider-scoped toolkit_search / toolkit_fetch, which only exist when
// the active provider declares them in provider.yaml.
#pragma once

#include <atomic>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "config.h"
#include "workspace.h"

namespace tea {

class McpRegistry;

struct ToolDef {
  std::string name;
  std::string description;
  nlohmann::json parameters = nlohmann::json::object();

  nlohmann::json to_openai() const;
};

struct ToolResult {
  bool ok = false;
  std::string output;
  bool truncated = false;
  /// The tool was interrupted (the turn was stopped while it ran). The tool
  /// may have done half of its work: whatever it wrote or changed is still
  /// there, and the next turn has to know that.
  bool cancelled = false;
  nlohmann::json meta = nlohmann::json::object();
  /// Images this tool made part of the conversation (toolkit_load_image). The
  /// wire format only allows images on *user* messages, so the agent loop turns
  /// these into a follow-up user message; the text `output` alone is what
  /// reaches the model as the tool result.
  std::vector<ImageAttachment> images;
  /// True when the tool mutated the session's task list (toolkit_todo). The agent
  /// loop then persists the session and pushes a `todo` event, so the UI and the
  /// next request both see the new state.
  bool todos_changed = false;
};

/// Is this command allowed by the fence? `why` says why not (empty when it
/// is allowed).
///
/// A pattern is a prefix: "npm *" matches "npm install" and "npm", "git
/// status" matches exactly that. The pattern is matched against the whole
/// command, so "npm *" does not match "sudo npm install".
bool exec_command_allowed(const ExecPolicy& policy, const std::string& command,
                          std::string* why);

struct ToolContext {
  const AppConfig* config = nullptr;
  /// The provider this turn resolved (TurnOptions may override the active one).
  /// Web tools need its base URL / API key / capability flags.
  const ProviderInfo* provider = nullptr;
  /// The model this turn resolved. toolkit_load_image checks supports_vision
  /// here so a stale/over-eager call explains the problem instead of silently
  /// attaching an image the model cannot read.
  const ModelInfo* model = nullptr;
  Workspace* workspace = nullptr;
  /// The session this turn belongs to. toolkit_todo reads and rewrites its task
  /// list; null means the tool refuses rather than inventing a list somewhere.
  Session* session = nullptr;
  std::atomic<bool>* cancel = nullptr;
  /// Per-turn counter of billed web-tool calls (null => no budget enforced).
  int* web_tool_calls = nullptr;
  /// The task this turn belongs to (null => an ordinary chat, no task). The
  /// toolkit_task tool writes the progress, the intermediate state and the
  /// "the user has to confirm this" flag into it.
  const std::string* task_id = nullptr;
  /// The MCP servers (null => no mcp.yaml is configured). The
  /// `mcp__<server>__<tool>` tools are dispatched through it.
  McpRegistry* mcp = nullptr;
  /// The exec fence of this turn (null => no fence). See ExecPolicy.
  const ExecPolicy* exec_policy = nullptr;
  /// The fence of the stage profile this turn runs under (see role.h). It
  /// applies on top of the turn's own fence: a command has to pass both.
  const ExecPolicy* exec_policy_role = nullptr;
  /// A read-only stage (see role.h): the file tools may read and list, but
  /// not write, delete, move or create anything.
  bool read_only = false;
  /// The turn's deadline (absolute epoch milliseconds; 0 => no deadline). The
  /// turn's wall clock is a ceiling for the whole turn, the tools included: a
  /// command that would run past it is cut short instead of keeping the turn
  /// alive past its budget.
  long long deadline_ms = 0;
};

class ToolRegistry {
 public:
  ToolRegistry();

  const std::vector<ToolDef>& defs() const { return defs_; }

  /// The web tools this provider can serve (toolkit_search / toolkit_fetch),
  /// as OpenAI tool schemas. Empty when neither the provider nor the
  /// independent search provider can search - then a model would only learn
  /// about the tool by calling it and failing.
  nlohmann::json web_tool_schemas(const ProviderInfo* provider,
                                  const AppConfig* config) const;

  /// The web tools, by name: "toolkit_search" and "toolkit_fetch". These are
  /// the two tools that need no workspace and no model - they talk to the
  /// provider's own search endpoint - so a script can call them directly.
  static bool is_web_tool(const std::string& name);
  /// Tools advertised to the model. Provider-scoped extras (toolkit_search /
  /// toolkit_fetch) only appear when `provider` declares support for them;
  /// a null pointer yields the portable core set.
  /// `settings` decides whether the fleet tool is advertised at all: a
  /// device that is not part of a fleet has no fleet_url, and a model that
  /// is shown a tool it cannot use only wastes a turn on it.
  /// `config` carries the independent search provider (the `search:` section
  /// of provider.yaml): when the turn's own provider cannot search, the web
  /// tools are still advertised as long as that one can.
  /// `include_mcp` = false leaves the MCP tools out. They are dynamic: asking
  /// for them starts the MCP servers, and the start-up banner must not wait
  /// for a handshake (a server that is not there would delay the start-up by
  /// its whole timeout).
  nlohmann::json openai_tools(const ProviderInfo* provider = nullptr,
                              const Settings* settings = nullptr,
                              const AppConfig* config = nullptr,
                              bool include_mcp = true) const;
  bool has(const std::string& name) const;

  /// The time budget of one call of this tool, in milliseconds.
  /// 0 means the tool has no deadline of its own (a local file read, say).
  ///
  /// The agent announces the budget before the call starts, so the UI can say
  /// how long a slow tool may still take instead of only "running". It is the
  /// same number the tool itself enforces - not a guess.
  int timeout_ms(const std::string& name, const nlohmann::json& arguments,
                 const ToolContext& ctx) const;

  /// The MCP servers whose tools join the agent's tool set. Null => no MCP
  /// tools (no mcp.yaml is configured).
  void set_mcp(McpRegistry* mcp) { mcp_ = mcp; }
  McpRegistry* mcp() const { return mcp_; }

  ToolResult execute(const std::string& name, const nlohmann::json& arguments,
                     const ToolContext& ctx) const;

 private:
  std::vector<ToolDef> defs_;
  McpRegistry* mcp_ = nullptr;
};

}  // namespace tea
