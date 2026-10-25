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

  /// The model a fresh conversation starts with: the FIRST entry of `models:`,
  /// i.e. the order in provider.yaml is the switch. `default_model` is only a
  /// fallback for a provider that declares no model list at all - keeping a
  /// separate "default" knob that silently disagreed with the visible order was
  /// more confusing than useful.
  std::string primary_model() const {
    if (!models.empty()) return models.front();
    return default_model;
  }
  /// Extra request headers sent with every completion call. Values may contain
  /// the placeholder ${session_id}, which is replaced per conversation.
  std::vector<std::pair<std::string, std::string> > headers;
  /// How to treat assistant reasoning_content on the wire.
  ReasoningEcho reasoning_echo = ReasoningEcho::Auto;

  /// Whether to ask for token usage in a streaming response
  /// (`stream_options: {include_usage: true}`). -1 = not declared (inherit the
  /// global `settings.stream_include_usage`), 0 = never, 1 = always.
  ///
  /// Why it matters: OpenAI-compatible gateways only report usage - and with it
  /// the prefix-cache hit count - when that flag is set. MiniMax documents it
  /// as "When streaming, set to true to include token usage in the stream"; with
  /// the flag missing it answers with an empty `usage` object, and the metrics
  /// strip then shows local estimates instead of the provider's numbers.
  int stream_include_usage = -1;
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

  /// The provider exposes a *quota* endpoint: "how much of my subscription is
  /// left" (MiniMax M Plan: a 5-hour window and a weekly window).
  ///
  /// NOT the same thing as `stream_include_usage` above, which asks the
  /// provider to include token counts in a chat response. The two are easy to
  /// confuse, so the names are kept apart on purpose: one is about the
  /// *response*, this one is about a *separate account endpoint*.
  ///
  /// The endpoint is not OpenAI-standard, so support is declared, not probed.
  /// Off by default - a wrong flag would send the key to an endpoint that does
  /// not exist.
  bool supports_usage = false;
  /// Which parser understands the response ("minimax_token_plan"). Empty when
  /// `supports_usage` is off.
  std::string usage_type;
  /// Full URL of the usage endpoint. Empty => the default of `usage_type`
  /// (the M Plan endpoint lives on another host than the chat API, so it cannot
  /// be derived from `api_url`).
  std::string usage_url;
  /// How the key is presented to the usage endpoint:
  ///   "bearer"  (default) Authorization: Bearer <key>
  ///   "api-key" api-key: <key>            (MiMo's Token Plan style)
  ///   "none"    no auth header at all      (endpoints behind a session cookie)
  std::string usage_auth = "bearer";
  /// Extra headers for the usage request only (a session cookie, a routing id,
  /// ...). The provider's own `headers:` are for the chat call and are NOT
  /// replayed here - a usage endpoint is a different service.
  std::vector<std::pair<std::string, std::string> > usage_headers;
  /// For `usage_type: json_fields`: dotted paths into the JSON answer
  /// ("data.remainingPercent", "list.0.used", ...). See src/usage.cpp.
  std::map<std::string, std::string> usage_fields;
  /// The page a HUMAN opens to see the quota - the provider's console, which
  /// usually needs a login the API key cannot stand in for (MiMo's Token Plan
  /// is the example: its credits live in the web console only).
  ///
  /// Independent of `supports_usage`: a provider can have this link without an
  /// API at all, and the UI then offers "open the console" instead of a number.
  std::string usage_console_url;

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
  /// "auto" | "socket" | "winhttp". Same meaning as `--http-engine`, but
  /// persistable: on a machine where WinHTTP cannot connect, setting
  /// `http_engine: socket` in tea_agent.yaml (or a workspace's settings.yaml)
  /// saves a failing probe. The command line flag still wins.
  std::string http_engine = "auto";
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
  /// Where the fleet manager lives (the server-side service that keeps the
  /// device inventory and runs the batch jobs). Empty => the `toolkit_fleet`
  /// tool is not advertised at all, so a device that is not part of a fleet
  /// never shows the model a tool it cannot use.
  std::string fleet_url;
  /// Bearer token for that service. Kept next to the URL so one place holds
  /// everything the fleet channel needs.
  std::string fleet_token;
  bool allow_outside_workspace = false;
  /// `--allow-outside-workspace` was given on the command line. Not a YAML key:
  /// it is a floor applied *after* every settings file is layered, so a workspace
  /// settings.yaml cannot quietly take the boundary back from an operator who
  /// asked for it to be lifted. The web UI reports it as `fixed_by_cli`.
  bool force_allow_outside_workspace = false;
  bool stream = true;
  /// Ask for token usage in streaming responses. On by default: without it most
  /// OpenAI-compatible gateways return an empty `usage` object, and the UI has
  /// to fall back to local estimates (no cache hit rate, no real token counts).
  /// A provider that rejects the field is detected at runtime and remembered
  /// (see AppConfig::remember_usage_flag_supported).
  bool stream_include_usage = true;
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
  /// turn, so the trigger sits well below it: 0.65 leaves enough room for the
  /// next few turns, which is also what keeps the request inside the provider's
  /// real window when the estimate is a little optimistic.
  double compaction_trigger_ratio = 0.65;
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
  /// `--allow-outside-workspace` was passed on the command line. This is an
  /// operator decision with security weight, so it acts as a floor: the web UI
  /// may switch the restriction *on*, but never off again while the process runs
  /// with the flag.
  bool cli_allow_outside_workspace = false;

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

  // Same idea for `stream_options.include_usage`: a gateway that does not know
  // the field answers 400, and we then stop sending it (for that provider, for
  // the rest of the process) instead of failing every turn.
  bool lookup_usage_flag_supported(const std::string& provider_name, bool* out) const;
  void remember_usage_flag_supported(const std::string& provider_name, bool supported);

 private:
  mutable std::mutex reasoning_mu_;
  std::map<std::string, bool> reasoning_echo_learned_;
  std::map<std::string, bool> usage_flag_learned_;
};

}  // namespace tea
