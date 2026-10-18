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
};

struct ToolContext {
  const AppConfig* config = nullptr;
  /// The provider this turn resolved (TurnOptions may override the active one).
  /// Web tools need its base URL / API key / capability flags.
  const ProviderInfo* provider = nullptr;
  Workspace* workspace = nullptr;
  std::atomic<bool>* cancel = nullptr;
  /// Per-turn counter of billed web-tool calls (null => no budget enforced).
  int* web_tool_calls = nullptr;
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
