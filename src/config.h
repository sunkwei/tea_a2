// config.h -- loads the OpenAI-compatible provider configuration from
// $HOME/.tea_agent/provider.yaml (same schema the existing tea_agent uses),
// optionally refined by $HOME/.tea_agent/tea_agent.yaml.
#pragma once

#include <cstdint>
#include <map>
#include <mutex>
#include <string>
#include <vector>

namespace tea {

/// Whether assistant `reasoning_content` is echoed back on the follow-up
/// request. Providers disagree, and both behaviours are fatal if guessed wrong:
/// thinking-mode gateways (vLLM/SGLang/GLM/Kimi/opencode) *require* it, while
/// api.deepseek.com answers 400 if it is present.
enum class ReasoningEcho {
  Auto,   // decide from the endpoint, then learn from the provider's reply
  Keep,   // always echo reasoning_content
  Strip   // never echo it
};

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
  /// How to treat assistant reasoning_content on the wire.
  ReasoningEcho reasoning_echo = ReasoningEcho::Auto;

  /// Provider-scoped web tools (DeepSeek-style /v1/tools/{search,fetch}).
  /// These endpoints are not OpenAI-standard, so their presence cannot be
  /// probed safely; the config declares them. Account-level, hence not a
  /// per-model flag. Both default to off: every call is billed per request.
  bool supports_search = false;
  bool supports_fetch = false;
  /// Optional endpoint overrides (path relative to api_url, or a full URL).
  /// Empty => "/v1/tools/search" and "/v1/tools/fetch".
  std::string search_path;
  std::string fetch_path;

  bool has_key() const { return !api_key.empty(); }
  const ModelInfo* find_model(const std::string& name) const;
};

struct Settings {
  int max_iterations = 50;
  int64_t max_tool_output = 131072;
  int64_t max_assistant_content = 131072;
  int keep_turns = 10;
  std::string reasoning_effort = "auto";
  double temperature = -1.0;  // < 0 => not set
  double top_p = -1.0;        // < 0 => not set
  int64_t max_tokens = 0;     // 0 => provider/model default
  int command_timeout_ms = 180000;
  int http_connect_timeout_ms = 30000;
  int http_read_timeout_ms = 600000;
  /// Web tools (toolkit_search / toolkit_fetch) get their own budget. The
  /// 600s read timeout above exists for LLM streaming; reusing it meant one
  /// slow search endpoint could stall a turn for ten minutes, and the request
  /// was not cancellable either.
  int web_tool_connect_timeout_ms = 10000;
  int web_tool_timeout_ms = 60000;
  /// Hard cap on billed web-tool calls per user turn. A model that keeps
  /// retrying would otherwise burn the whole max_iterations budget, which is
  /// both slow and (the provider bills per call) expensive.
  int web_tool_max_calls_per_turn = 10;
  bool allow_outside_workspace = false;
  bool stream = true;
  bool stream_include_usage = false;
  bool utf8_console = true;
  std::string shell;  // "cmd" | "powershell" | "bash" | "" => auto
  std::string system_prompt;

  // ---- tool output ------------------------------------------------------
  /// Hard cap on retained lines per tool result, applied on top of
  /// max_tool_output. Content-aware filtering runs before this.
  int tool_output_max_lines = 400;

  // ---- context management ----------------------------------------------
  /// Summarise the oldest part of the history instead of dropping it silently.
  bool enable_compaction = true;
  /// Compact when the live history reaches this fraction of the message budget.
  /// Compacting exactly at the hard boundary would re-trigger on the very next
  /// turn; borrowing dsh's 0.8 leaves hysteresis.
  double compaction_trigger_ratio = 0.8;
  /// Fold history until the verbatim tail is at most this fraction of the budget.
  /// dsh uses 0.16: compacting "just enough" makes every following turn compact
  /// again, so we aim for a small tail instead.
  double compaction_retain_ratio = 0.16;
  /// Extra verbatim messages kept regardless of the token target.
  int compaction_retries = 1;
  /// Token budget for the generated summary.
  int64_t compaction_summary_max_tokens = 800;
  /// Keep at least this many recent messages no matter what.
  int compaction_keep_recent = 4;

  // ---- turn trajectories (the per-turn audit trail) ---------------------
  /// Record one structured trajectory per turn in
  /// `<run_dir>/trajectories/<session_id>.jsonl`. Off writes nothing at all.
  bool enable_trajectory = true;
  /// Cap on one tool output body inside a trajectory step, in bytes.
  int64_t trajectory_step_max_bytes = 8192;
  /// Retention: at most this many turns and this many bytes per session file.
  /// The oldest turns are dropped first.
  int trajectory_max_turns = 200;
  int64_t trajectory_max_bytes = 8 * 1024 * 1024;
};

/// Overlay a `settings:` mapping from a YAML file on top of `settings`.
/// Used for the optional `$TEA_AGENT_HOME/tea_agent.yaml` and for the
/// per-workspace `<run_dir>/settings.yaml`. Returns false with `err` set when
/// the file cannot be read or parsed; `settings` is then left untouched.
bool apply_settings_yaml(Settings* settings, const std::string& yaml_path, std::string* err);

struct AppConfig {  std::string path;      // provider.yaml that was loaded
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
  /// True when `load()` failed because the file does not exist (as opposed to
  /// being unreadable or malformed). The server uses this to come up in setup
  /// mode and let the UI upload the file instead of refusing to start.
  bool file_missing = false;

  bool load(const std::string& config_path, const std::string& tea_home);

  const ProviderInfo* provider(const std::string& name) const;
  const ProviderInfo* active_provider_info() const;
  const ModelInfo* active_model_info() const;

  /// Choose the active provider/model. Empty arguments keep the current value.
  bool set_active(const std::string& provider, const std::string& model, std::string* err);

  std::string default_system_prompt() const;

  // ---- learned runtime behaviour -----------------------------------------
  // A provider's opinion on reasoning_content can only be discovered by asking
  // it, so the first wrong guess is remembered here for the whole process.
  bool lookup_reasoning_echo(const std::string& provider_name, bool* out) const;
  void remember_reasoning_echo(const std::string& provider_name, bool keep);

 private:
  mutable std::mutex reasoning_mu_;
  std::map<std::string, bool> reasoning_echo_learned_;
};

}  // namespace tea
