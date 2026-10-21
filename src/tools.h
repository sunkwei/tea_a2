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
};

class ToolRegistry {
 public:
  ToolRegistry();

  const std::vector<ToolDef>& defs() const { return defs_; }
  /// Tools advertised to the model. Provider-scoped extras (toolkit_search /
  /// toolkit_fetch) only appear when `provider` declares support for them;
  /// a null pointer yields the portable core set.
  nlohmann::json openai_tools(const ProviderInfo* provider = nullptr) const;
  bool has(const std::string& name) const;

  ToolResult execute(const std::string& name, const nlohmann::json& arguments,
                     const ToolContext& ctx) const;

 private:
  std::vector<ToolDef> defs_;
};

}  // namespace tea
