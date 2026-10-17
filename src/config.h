// config.h -- loads the OpenAI-compatible provider configuration from
// $HOME/.tea_agent/provider.yaml (same schema the existing tea_agent uses),
// optionally refined by $HOME/.tea_agent/tea_agent.yaml.
#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace tea {

struct ModelInfo {
  std::string name;
  int64_t max_context_tokens = 0;
  int64_t max_output_tokens = 0;
  bool supports_vision = false;
  bool supports_reasoning = false;
  bool supports_tools = true;
  std::string reasoning_effort = "auto";
  bool has_temperature = false;
  double temperature = 0.7;
  bool has_top_p = false;
  double top_p = 0.9;
  std::string note;
};

struct ProviderInfo {
  std::string name;
  std::string api_url;
  std::string api_key;
  std::string default_model;
  std::string description;
  std::vector<std::string> api_keys;
  std::vector<std::string> models;  // ordered model names
  std::map<std::string, ModelInfo> model_map;
  /// Extra request headers sent with every completion call. Values may contain
  /// the placeholder ${session_id}, which is replaced per conversation.
  std::vector<std::pair<std::string, std::string> > headers;

  bool has_key() const { return !api_key.empty(); }
  const ModelInfo* find_model(const std::string& name) const;
};

struct Settings {
  int max_iterations = 50;
  int64_t max_tool_output = 131072;
  int64_t max_assistant_content = 131072;
  int keep_turns = 10;
  bool enable_thinking = false;
  std::string reasoning_effort = "auto";
  double temperature = -1.0;  // < 0 => not set
  double top_p = -1.0;        // < 0 => not set
  int64_t max_tokens = 0;     // 0 => provider/model default
  int command_timeout_ms = 180000;
  int http_connect_timeout_ms = 30000;
  int http_read_timeout_ms = 600000;
  bool allow_outside_workspace = false;
  bool stream = true;
  bool stream_include_usage = false;
  bool utf8_console = true;
  std::string shell;  // "cmd" | "powershell" | "bash" | "" => auto
  std::string system_prompt;
};

struct AppConfig {
  std::string path;      // provider.yaml that was loaded
  std::string extra_path;  // tea_agent.yaml, if present
  std::string tea_home;  // $HOME/.tea_agent
  std::vector<std::string> provider_order;
  std::map<std::string, ProviderInfo> providers;
  Settings settings;
  std::string active_provider;
  std::string active_model;
  std::vector<std::string> warnings;
  std::string error;
  bool loaded = false;

  bool load(const std::string& config_path, const std::string& tea_home);

  const ProviderInfo* provider(const std::string& name) const;
  const ProviderInfo* active_provider_info() const;
  const ModelInfo* active_model_info() const;

  /// Choose the active provider/model. Empty arguments keep the current value.
  bool set_active(const std::string& provider, const std::string& model, std::string* err);

  std::string default_system_prompt() const;
};

}  // namespace tea
