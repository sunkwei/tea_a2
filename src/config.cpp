#include "config.h"

#include <yaml-cpp/yaml.h>

#include <algorithm>

#include "log.h"
#include "usage.h"
#include "util.h"

namespace tea {
namespace {

std::string ystr(const YAML::Node& node, const std::string& key, const std::string& def = "") {
  if (!node || !node.IsMap()) return def;
  const YAML::Node v = node[key];
  if (!v || !v.IsScalar()) return def;
  try {
    return v.as<std::string>();
  } catch (const std::exception&) {
    return def;
  }
}

bool ybool(const YAML::Node& node, const std::string& key, bool def) {
  if (!node || !node.IsMap()) return def;
  const YAML::Node v = node[key];
  if (!v || !v.IsScalar()) return def;
  try {
    return v.as<bool>();
  } catch (const std::exception&) {
    const std::string s = to_lower(trim(v.as<std::string>()));
    if (s == "true" || s == "yes" || s == "on" || s == "1") return true;
    if (s == "false" || s == "no" || s == "off" || s == "0") return false;
    return def;
  }
}

bool yhas(const YAML::Node& node, const std::string& key) {
  if (!node || !node.IsMap()) return false;
  const YAML::Node v = node[key];
  return static_cast<bool>(v) && v.IsDefined() && !v.IsNull();
}

int64_t yint(const YAML::Node& node, const std::string& key, int64_t def) {
  if (!yhas(node, key)) return def;
  const YAML::Node v = node[key];
  if (!v.IsScalar()) return def;
  try {
    return v.as<int64_t>();
  } catch (const std::exception&) {
    try {
      return static_cast<int64_t>(v.as<double>());
    } catch (const std::exception&) {
      return def;
    }
  }
}

double ydouble(const YAML::Node& node, const std::string& key, double def) {
  if (!yhas(node, key)) return def;
  const YAML::Node v = node[key];
  if (!v.IsScalar()) return def;
  try {
    return v.as<double>();
  } catch (const std::exception&) {
    return def;
  }
}

void apply_settings(Settings& s, const YAML::Node& node) {
  if (!node || !node.IsMap()) return;
  if (yhas(node, "max_iterations")) s.max_iterations = static_cast<int>(yint(node, "max_iterations", s.max_iterations));
  if (yhas(node, "max_tool_output")) s.max_tool_output = yint(node, "max_tool_output", s.max_tool_output);
  if (yhas(node, "max_assistant_content"))
    s.max_assistant_content = yint(node, "max_assistant_content", s.max_assistant_content);
  if (yhas(node, "keep_turns")) s.keep_turns = static_cast<int>(yint(node, "keep_turns", s.keep_turns));
  if (yhas(node, "reasoning_effort")) s.reasoning_effort = ystr(node, "reasoning_effort", s.reasoning_effort);
  if (yhas(node, "temperature")) s.temperature = ydouble(node, "temperature", s.temperature);
  if (yhas(node, "top_p")) s.top_p = ydouble(node, "top_p", s.top_p);
  if (yhas(node, "max_tokens")) s.max_tokens = yint(node, "max_tokens", s.max_tokens);
  if (yhas(node, "http_engine")) s.http_engine = ystr(node, "http_engine", s.http_engine);
  if (yhas(node, "command_timeout_ms"))
    s.command_timeout_ms = static_cast<int>(yint(node, "command_timeout_ms", s.command_timeout_ms));
  if (yhas(node, "http_connect_timeout_ms"))
    s.http_connect_timeout_ms = static_cast<int>(yint(node, "http_connect_timeout_ms", s.http_connect_timeout_ms));
  if (yhas(node, "http_read_timeout_ms"))
    s.http_read_timeout_ms = static_cast<int>(yint(node, "http_read_timeout_ms", s.http_read_timeout_ms));
  if (yhas(node, "web_tool_connect_timeout_ms"))
    s.web_tool_connect_timeout_ms =
        static_cast<int>(yint(node, "web_tool_connect_timeout_ms", s.web_tool_connect_timeout_ms));
  if (yhas(node, "web_tool_timeout_ms"))
    s.web_tool_timeout_ms =
        static_cast<int>(yint(node, "web_tool_timeout_ms", s.web_tool_timeout_ms));
  if (yhas(node, "web_tool_max_calls_per_turn"))
    s.web_tool_max_calls_per_turn =
        static_cast<int>(yint(node, "web_tool_max_calls_per_turn", s.web_tool_max_calls_per_turn));
  if (yhas(node, "fleet_url")) s.fleet_url = ystr(node, "fleet_url", s.fleet_url);
  if (yhas(node, "fleet_token")) s.fleet_token = ystr(node, "fleet_token", s.fleet_token);
  if (yhas(node, "allow_outside_workspace"))
    s.allow_outside_workspace = ybool(node, "allow_outside_workspace", s.allow_outside_workspace);
  if (yhas(node, "stream")) s.stream = ybool(node, "stream", s.stream);
  if (yhas(node, "overload_retries"))
    s.overload_retries = static_cast<int>(yint(node, "overload_retries", s.overload_retries));
  if (yhas(node, "overload_backoff_ms"))
    s.overload_backoff_ms =
        static_cast<int>(yint(node, "overload_backoff_ms", s.overload_backoff_ms));
  if (yhas(node, "overload_max_backoff_ms"))
    s.overload_max_backoff_ms =
        static_cast<int>(yint(node, "overload_max_backoff_ms", s.overload_max_backoff_ms));
  if (yhas(node, "overload_max_total_ms"))
    s.overload_max_total_ms = yint(node, "overload_max_total_ms", s.overload_max_total_ms);
  if (yhas(node, "stream_include_usage"))
    s.stream_include_usage = ybool(node, "stream_include_usage", s.stream_include_usage);
  if (yhas(node, "utf8_console")) s.utf8_console = ybool(node, "utf8_console", s.utf8_console);
  if (yhas(node, "shell")) s.shell = ystr(node, "shell", s.shell);
  if (yhas(node, "system_prompt")) s.system_prompt = ystr(node, "system_prompt", s.system_prompt);
  if (yhas(node, "tool_output_max_lines"))
    s.tool_output_max_lines = static_cast<int>(yint(node, "tool_output_max_lines", s.tool_output_max_lines));
  if (yhas(node, "enable_compaction"))
    s.enable_compaction = ybool(node, "enable_compaction", s.enable_compaction);
  if (yhas(node, "compaction_summary_max_tokens"))
    s.compaction_summary_max_tokens =
        yint(node, "compaction_summary_max_tokens", s.compaction_summary_max_tokens);
  if (yhas(node, "compaction_trigger_ratio"))
    s.compaction_trigger_ratio = ydouble(node, "compaction_trigger_ratio", s.compaction_trigger_ratio);
  if (yhas(node, "compaction_retain_ratio"))
    s.compaction_retain_ratio = ydouble(node, "compaction_retain_ratio", s.compaction_retain_ratio);
  if (yhas(node, "compaction_retries"))
    s.compaction_retries = static_cast<int>(yint(node, "compaction_retries", s.compaction_retries));
  if (yhas(node, "compaction_keep_recent"))
    s.compaction_keep_recent =
        static_cast<int>(yint(node, "compaction_keep_recent", s.compaction_keep_recent));
  if (yhas(node, "enable_trajectory"))
    s.enable_trajectory = ybool(node, "enable_trajectory", s.enable_trajectory);
  if (yhas(node, "trajectory_step_max_bytes"))
    s.trajectory_step_max_bytes =
        yint(node, "trajectory_step_max_bytes", s.trajectory_step_max_bytes);
  if (yhas(node, "trajectory_max_turns"))
    s.trajectory_max_turns =
        static_cast<int>(yint(node, "trajectory_max_turns", s.trajectory_max_turns));
  if (yhas(node, "trajectory_max_bytes"))
    s.trajectory_max_bytes = yint(node, "trajectory_max_bytes", s.trajectory_max_bytes);
  if (s.keep_turns < 0) s.keep_turns = 0;
  if (s.trajectory_step_max_bytes < 0) s.trajectory_step_max_bytes = 0;
  if (s.trajectory_step_max_bytes > 1024 * 1024) s.trajectory_step_max_bytes = 1024 * 1024;
  if (s.trajectory_max_turns < 1) s.trajectory_max_turns = 1;
  if (s.trajectory_max_bytes < 1024) s.trajectory_max_bytes = 1024;
  if (s.compaction_trigger_ratio < 0.3) s.compaction_trigger_ratio = 0.3;
  if (s.compaction_trigger_ratio > 0.95) s.compaction_trigger_ratio = 0.95;
  if (s.compaction_retain_ratio < 0.02) s.compaction_retain_ratio = 0.02;
  if (s.compaction_retain_ratio >= s.compaction_trigger_ratio)
    s.compaction_retain_ratio = s.compaction_trigger_ratio / 2;
  if (s.compaction_retries < 0) s.compaction_retries = 0;
  if (s.max_iterations < 1) s.max_iterations = 1;
  if (s.command_timeout_ms < 1000) s.command_timeout_ms = 1000;
  if (s.web_tool_connect_timeout_ms < 1000) s.web_tool_connect_timeout_ms = 1000;
  if (s.web_tool_timeout_ms < 1000) s.web_tool_timeout_ms = 1000;
  if (s.web_tool_timeout_ms > 600000) s.web_tool_timeout_ms = 600000;
  if (s.web_tool_max_calls_per_turn < 1) s.web_tool_max_calls_per_turn = 1;
  if (s.overload_retries < 0) s.overload_retries = 0;
  if (s.overload_retries > 20) s.overload_retries = 20;
  if (s.overload_backoff_ms < 0) s.overload_backoff_ms = 0;
  if (s.overload_max_backoff_ms < 0) s.overload_max_backoff_ms = 0;
  if (s.overload_max_total_ms < 0) s.overload_max_total_ms = 0;
}

}  // namespace

bool apply_settings_yaml(Settings* settings, const std::string& yaml_path, std::string* err) {
  if (!settings) return false;
  if (!path_exists(yaml_path)) {
    if (err) *err = "not found: " + yaml_path;
    return false;
  }
  YAML::Node root;
  try {
    root = YAML::LoadFile(yaml_path);
  } catch (const std::exception& e) {
    if (err) *err = std::string("cannot parse ") + yaml_path + ": " + e.what();
    return false;
  }
  if (!root || !root.IsMap()) {
    if (err) *err = "not a YAML mapping: " + yaml_path;
    return false;
  }
  // Accept both a nested `settings:` block (same shape as tea_agent.yaml) and a
  // flat file whose keys are the settings themselves.
  const YAML::Node block = root["settings"] ? root["settings"] : root;
  apply_settings(*settings, block);
  return true;
}

const ModelInfo* ProviderInfo::find_model(const std::string& n) const {
  auto it = model_map.find(n);
  return it == model_map.end() ? nullptr : &it->second;
}

bool AppConfig::load(const std::string& config_path, const std::string& tea_home_in) {
  tea_home = tea_home_in.empty() ? tea_home_dir() : tea_home_in;
  path = config_path.empty() ? path_join(tea_home, "provider.yaml") : config_path;
  warnings.clear();
  error.clear();
  providers.clear();
  provider_order.clear();
  search = SearchProviderConfig();
  loaded = false;
  file_missing = false;

  if (!path_exists(path)) {
    error = "provider config not found: " + path;
    file_missing = true;
    return false;
  }

  YAML::Node root;
  try {
    root = YAML::LoadFile(path);
  } catch (const std::exception& e) {
    error = std::string("failed to parse ") + path + ": " + e.what();
    return false;
  }
  if (!root || !root.IsMap()) {
    error = "provider config is not a YAML mapping: " + path;
    return false;
  }

  const YAML::Node providers_node = root["providers"];
  if (providers_node && providers_node.IsMap()) {
    for (auto it = providers_node.begin(); it != providers_node.end(); ++it) {
      std::string name;
      try {
        name = it->first.as<std::string>();
      } catch (const std::exception&) {
        continue;
      }
      const YAML::Node pn = it->second;
      if (!pn || !pn.IsMap()) continue;

      ProviderInfo info;
      info.name = name;
      info.api_url = trim(ystr(pn, "api_url"));
      info.api_key = trim(ystr(pn, "api_key"));
      info.default_model = trim(ystr(pn, "default_model"));
      info.description = trim(ystr(pn, "description"));

      const YAML::Node keys = pn["api_keys"];
      if (keys && keys.IsSequence()) {
        for (const auto& k : keys) {
          if (!k.IsScalar()) continue;
          try {
            const std::string v = trim(k.as<std::string>());
            if (!v.empty()) info.api_keys.push_back(v);
          } catch (const std::exception&) {
          }
        }
      }
      if (info.api_key.empty() && !info.api_keys.empty()) info.api_key = info.api_keys.front();

      const YAML::Node models = pn["models"];
      if (models && models.IsMap()) {
        for (auto mit = models.begin(); mit != models.end(); ++mit) {
          std::string mname;
          try {
            mname = mit->first.as<std::string>();
          } catch (const std::exception&) {
            continue;
          }
          ModelInfo mi;
          mi.name = mname;
          const YAML::Node mn = mit->second;
          if (mn && mn.IsMap()) {
            mi.max_context_tokens = yint(mn, "max_context_tokens", 0);
            mi.max_output_tokens = yint(mn, "max_output_tokens", 0);
            mi.supports_vision = ybool(mn, "supports_vision", false);
            mi.supports_reasoning = ybool(mn, "supports_reasoning", false);
            mi.supports_tools = ybool(mn, "supports_tools", true);
            mi.reasoning_effort = ystr(mn, "reasoning_effort", "auto");
            mi.note = ystr(mn, "note");
            if (yhas(mn, "temperature")) {
              mi.has_temperature = true;
              mi.temperature = ydouble(mn, "temperature", 0.7);
            }
            if (yhas(mn, "top_p")) {
              mi.has_top_p = true;
              mi.top_p = ydouble(mn, "top_p", 0.9);
            }
          }
          info.models.push_back(mname);
          info.model_map[mname] = mi;
        }
      }

      // Optional per-provider request headers (some gateways need routing ids).
      const YAML::Node headers_node = pn["headers"];
      if (headers_node && headers_node.IsMap()) {
        for (auto hit = headers_node.begin(); hit != headers_node.end(); ++hit) {
          std::string header_name;
          try {
            header_name = trim(hit->first.as<std::string>());
          } catch (const std::exception&) {
            continue;
          }
          if (header_name.empty() || !hit->second.IsScalar()) continue;
          std::string header_value;
          try {
            header_value = hit->second.as<std::string>();
          } catch (const std::exception&) {
            continue;
          }
          info.headers.push_back(std::make_pair(header_name, header_value));
        }
      }

      // Whether the assistant's reasoning_content must be echoed back to this
      // endpoint on the follow-up request.
      if (yhas(pn, "reasoning_content")) {
        const std::string echo = to_lower(trim(ystr(pn, "reasoning_content")));
        if (echo == "keep" || echo == "true" || echo == "always") {
          info.reasoning_echo = ReasoningEcho::Keep;
        } else if (echo == "strip" || echo == "false" || echo == "never") {
          info.reasoning_echo = ReasoningEcho::Strip;
        } else if (echo == "auto" || echo.empty()) {
          info.reasoning_echo = ReasoningEcho::Auto;
        } else {
          warnings.push_back("provider '" + name + "': unknown reasoning_content '" + echo +
                             "' (use auto|keep|strip); falling back to auto");
        }
      }

      // Provider-scoped web tools (DeepSeek opens /v1/tools/{search,fetch} to
      // every API user; the protocol is not OpenAI-standard, so support is
      // declared rather than probed). Off by default: every call is billed
      // per request by the provider.
      //
      // The documented place is the provider block. A model block is accepted
      // too, because that is where people put it first (the capability always
      // travels with a specific model in the docs), and silently ignoring it
      // hides the tools with no diagnostic at all - the model simply never gets
      // them. A warning names the canonical spot so the file can be tidied.
      info.supports_search = ybool(pn, "supports_search", false);
      info.supports_fetch = ybool(pn, "supports_fetch", false);
      if (!info.supports_search || !info.supports_fetch) {
        bool model_search = false, model_fetch = false;
        const YAML::Node models_node = pn["models"];
        if (models_node && models_node.IsMap()) {
          for (auto mit = models_node.begin(); mit != models_node.end(); ++mit) {
            const YAML::Node mn = mit->second;
            if (!mn || !mn.IsMap()) continue;
            if (ybool(mn, "supports_search", false)) model_search = true;
            if (ybool(mn, "supports_fetch", false)) model_fetch = true;
          }
        }
        if (model_search) info.supports_search = true;
        if (model_fetch) info.supports_fetch = true;
        if (model_search || model_fetch) {
          warnings.push_back("provider '" + name +
                             "': supports_search/supports_fetch are declared on a model; they "
                             "are provider-scoped, so they were promoted. Move them up to the "
                             "provider block.");
        }
      }
      if (yhas(pn, "supports_featch")) {
        // A typo that looks right and does nothing: say so instead of leaving the
        // operator wondering why toolkit_fetch never shows up.
        warnings.push_back("provider '" + name +
                           "': unknown key 'supports_featch' (did you mean supports_fetch?)");
      }
      info.search_path = trim(ystr(pn, "search_path"));
      info.fetch_path = trim(ystr(pn, "fetch_path"));

      // `stream_include_usage` is provider-scoped: whether a gateway reports
      // token usage (and the prefix-cache hit count) in a streaming response is
      // a property of that gateway, not of the whole installation. Unset means
      // "inherit settings.stream_include_usage".
      if (yhas(pn, "stream_include_usage")) {
        const YAML::Node node = pn["stream_include_usage"];
        if (node.IsNull() || (node.IsScalar() && node.Scalar() == "")) {
          info.stream_include_usage = -1;
        } else {
          info.stream_include_usage = ybool(pn, "stream_include_usage", true) ? 1 : 0;
        }
      }

      // A quota endpoint ("how much of my subscription is left"). Provider
      // scoped and NOT OpenAI-standard, so it is declared, not probed.
      //
      // The flag lives next to `stream_include_usage`, which means something
      // else entirely (asking for token counts *in a chat response*). The
      // difference is spelled out in the README, because mixing the two up
      // would either hide the quota or send a field the gateway rejects.
      info.supports_usage = ybool(pn, "supports_usage", false);
      info.usage_type = trim(ystr(pn, "usage_type"));
      info.usage_url = trim(ystr(pn, "usage_url"));

      // The details imply the flag: writing `usage_type:` alone is the natural
      // thing to do, and silently doing nothing would be a bad afternoon.
      if (!info.supports_usage && (!info.usage_type.empty() || !info.usage_url.empty())) {
        info.supports_usage = true;
        warnings.push_back("provider '" + name +
                           "': usage_type/usage_url were set but supports_usage is missing; "
                           "the quota query was enabled. Add 'supports_usage: true' to make it explicit.");
      }
      // Declared on a model? The quota belongs to the account, not to a model.
      if (!info.supports_usage) {
        const YAML::Node models_node = pn["models"];
        if (models_node && models_node.IsMap()) {
          for (auto mit = models_node.begin(); mit != models_node.end(); ++mit) {
            const YAML::Node mn = mit->second;
            if (!mn || !mn.IsMap()) continue;
            if (ybool(mn, "supports_usage", false) || yhas(mn, "usage_type") || yhas(mn, "usage_url")) {
              info.supports_usage = true;
              if (info.usage_type.empty()) info.usage_type = trim(ystr(mn, "usage_type"));
              if (info.usage_url.empty()) info.usage_url = trim(ystr(mn, "usage_url"));
              warnings.push_back("provider '" + name +
                                 "': supports_usage/usage_type are declared on a model; the quota is "
                                 "account wide, so they were promoted. Move them up to the provider block.");
              break;
            }
          }
        }
      }
      // How the key reaches the usage endpoint, and any extra headers it needs
      // (a session cookie, a custom auth header, ...).
      info.usage_auth = to_lower(trim(ystr(pn, "usage_auth", "bearer")));
      if (info.usage_auth.empty()) info.usage_auth = "bearer";
      if (info.usage_auth != "bearer" && info.usage_auth != "api-key" &&
          info.usage_auth != "none") {
        warnings.push_back("provider '" + name + "': unknown usage_auth '" + info.usage_auth +
                           "' (use bearer|api-key|none); falling back to bearer");
        info.usage_auth = "bearer";
      }
      {
        const YAML::Node uh = pn["usage_headers"];
        if (uh && uh.IsMap()) {
          for (auto hit = uh.begin(); hit != uh.end(); ++hit) {
            std::string name_h, value_h;
            try {
              name_h = trim(hit->first.as<std::string>());
              value_h = hit->second.IsScalar() ? hit->second.as<std::string>() : std::string();
            } catch (const std::exception&) {
              continue;
            }
            if (name_h.empty()) continue;
            info.usage_headers.push_back(std::make_pair(name_h, value_h));
          }
        }
      }
      {
        const YAML::Node uf = pn["usage_fields"];
        if (uf && uf.IsMap()) {
          for (auto hit = uf.begin(); hit != uf.end(); ++hit) {
            std::string k, v;
            try {
              k = trim(hit->first.as<std::string>());
              v = hit->second.IsScalar() ? trim(hit->second.as<std::string>()) : std::string();
            } catch (const std::exception&) {
              continue;
            }
            if (k.empty() || v.empty()) continue;
            info.usage_fields[k] = v;
          }
        }
      }
      // The console page a human opens to see the quota. It stands on its own:
      // a provider with no quota API at all can still point at its dashboard.
      info.usage_console_url = trim(ystr(pn, "usage_console_url"));
      if (!info.usage_console_url.empty()) {
        const std::string u = to_lower(info.usage_console_url);
        if (u.rfind("http://", 0) != 0 && u.rfind("https://", 0) != 0) {
          warnings.push_back("provider '" + name + "': usage_console_url must be an http(s) URL, got '" +
                             info.usage_console_url + "'; the link was dropped");
          info.usage_console_url.clear();
        }
      }
      if (info.supports_usage) {
        if (info.usage_type.empty()) info.usage_type = "minimax_token_plan";
        if (!usage_type_known(info.usage_type)) {
          warnings.push_back("provider '" + name + "': unknown usage_type '" + info.usage_type +
                             "' (known: " + usage_type_list() + "); the quota query stays off");
          info.supports_usage = false;
          info.usage_type.clear();
          info.usage_url.clear();
        }
      }

      if (info.api_url.empty()) {
        warnings.push_back("provider '" + name + "' has no api_url; skipped");
        continue;
      }
      if (info.default_model.empty() && !info.models.empty()) {
        info.default_model = info.models.front();
      }
      provider_order.push_back(name);
      providers[name] = std::move(info);
    }
  }

  // ---- the independent search provider ----------------------------------
  // The `search:` section names the provider that serves the web tools
  // (toolkit_search / toolkit_fetch), independently of the provider the
  // conversation runs on. It exists because the two are usually different
  // accounts: the chat may run on a local model with no web access at all,
  // while the searches are billed to the DeepSeek account.
  {
    // `search_provider:` is accepted as a spelling of the same thing; the
    // section is short enough that people reach for either name.
    const YAML::Node search_node = root["search"] ? root["search"] : root["search_provider"];
    if (search_node && search_node.IsMap()) {
      search.configured = true;
      search.provider = trim(ystr(search_node, "provider"));
      if (search.provider.empty()) {
        search.error =
            "the 'search:' section needs a 'provider:' naming an entry of the 'providers:' list";
        warnings.push_back(search.error);
      } else {
        auto it = providers.find(search.provider);
        if (it == providers.end()) {
          search.error = "the 'search:' section names provider '" + search.provider +
                         "', which is not defined in this file";
          warnings.push_back(search.error);
        } else {
          const ProviderInfo& p = it->second;
          // The section may declare the capabilities itself; otherwise they
          // are inherited from the provider entry it names. A section that
          // says nothing and names a provider without the flags is a mistake
          // worth a warning, not a silent "no web tools".
          search.supports_search = yhas(search_node, "supports_search")
                                       ? ybool(search_node, "supports_search", false)
                                       : p.supports_search;
          search.supports_fetch = yhas(search_node, "supports_fetch")
                                      ? ybool(search_node, "supports_fetch", false)
                                      : p.supports_fetch;
          search.always = ybool(search_node, "always", false);
          // Endpoint overrides: the section wins over the provider entry.
          search.search_path = trim(ystr(search_node, "search_path"));
          if (search.search_path.empty()) search.search_path = p.search_path;
          search.fetch_path = trim(ystr(search_node, "fetch_path"));
          if (search.fetch_path.empty()) search.fetch_path = p.fetch_path;
          if (!search.supports_search && !search.supports_fetch) {
            search.error = "the search provider '" + search.provider +
                           "' declares neither supports_search nor supports_fetch";
            warnings.push_back(search.error +
                               " (add 'supports_search: true' to the 'search:' section)");
          } else if (!p.has_key()) {
            search.error = "the search provider '" + search.provider + "' has no api_key";
            warnings.push_back(search.error + "; the web tools cannot be used");
          }
        }
      }
    }
  }

  apply_settings(settings, root["settings"]);

  // Optional C++-specific overrides that do not belong in provider.yaml.
  extra_path = path_join(tea_home, "tea_agent.yaml");
  if (path_exists(extra_path)) {
    try {
      YAML::Node extra = YAML::LoadFile(extra_path);
      if (extra && extra.IsMap()) {
        apply_settings(settings, extra["settings"]);
        if (extra["server"] && extra["server"].IsMap()) {
          // Reserved for future use; server flags come from the command line.
        }
      }
    } catch (const std::exception& e) {
      warnings.push_back(std::string("ignoring ") + extra_path + ": " + e.what());
    }
  }

  // Resolve the default provider/model: the FIRST provider in the file and the
  // FIRST model in its `models:` map. Order in the file is the switch - put a
  // provider or a model higher to make it the default, or pass --provider/--model.
  //
  // `default_model:` deliberately does not take part: a knob that quietly
  // disagreed with the visible order (and with what a fresh `provider.yaml`
  // implies) caused more "why is it using that model?" than it solved. It still
  // works as a fallback for a provider that declares no model list.
  //
  // The old `roles:` block is not read at all: it came from the Python
  // implementation, where a "role" meant per-role model routing that this
  // runtime does not have, so a `cheap:`/`interruption:` entry looked meaningful
  // while doing nothing.
  if (!provider_order.empty()) {
    active_provider = provider_order.front();
    const ProviderInfo* first = provider(active_provider);
    active_model = first ? first->primary_model() : std::string();
  }
  if (root["roles"]) {
    std::string ignored;
    const YAML::Node roles_root = root["roles"];
    if (roles_root.IsMap()) {
      for (auto it = roles_root.begin(); it != roles_root.end(); ++it) {
        std::string role;
        try {
          role = it->first.as<std::string>();
        } catch (const std::exception&) {
          continue;
        }
        if (!ignored.empty()) ignored += ", ";
        ignored += role;
      }
    }
    warnings.push_back(
        "the 'roles:' block is ignored (found: " + (ignored.empty() ? "main" : ignored) +
        "). The default is the FIRST provider in this file and the FIRST model in its "
        "'models:' map; move them up, or pass --provider/--model. Roles came from the Python "
        "implementation and never mapped onto anything here.");
  }
  if (!set_active(active_provider, active_model, nullptr)) {
    if (provider_order.empty()) {
      error = "no usable provider found in " + path;
      return false;
    }
    set_active(provider_order.front(), std::string(), nullptr);
  }

  loaded = true;
  LOG_INFO("config: loaded ", providers.size(), " provider(s) from ", path,
           " active=", active_provider, "/", active_model);
  for (const auto& w : warnings) LOG_WARN("config: ", w);
  return true;
}

const ProviderInfo* AppConfig::provider(const std::string& name) const {
  auto it = providers.find(name);
  return it == providers.end() ? nullptr : &it->second;
}

const ProviderInfo* AppConfig::active_provider_info() const {
  return provider(active_provider);
}

const ModelInfo* AppConfig::active_model_info() const {
  const ProviderInfo* p = active_provider_info();
  if (!p) return nullptr;
  return p->find_model(active_model);
}

const ProviderInfo* AppConfig::search_provider_info() const {
  if (!search.configured || search.provider.empty()) return nullptr;
  return provider(search.provider);
}

bool AppConfig::resolve_web_tool(const ProviderInfo* turn_provider, bool want_search,
                                 ProviderInfo* out, bool* via_search_provider,
                                 std::string* err) const {
  if (via_search_provider) *via_search_provider = false;
  if (!out) return false;
  const char* what = want_search ? "web search" : "page fetching";
  const char* flag = want_search ? "supports_search" : "supports_fetch";

  const ProviderInfo* sp = search_provider_info();
  const bool sp_capable =
      sp != nullptr && (want_search ? search.supports_search : search.supports_fetch);
  // Can the turn's own provider serve the tool? That is the cheap, local
  // case, and the behaviour everybody had before the search: section existed.
  const bool turn_can =
      turn_provider && (want_search ? turn_provider->supports_search : turn_provider->supports_fetch);

  // The search provider serves the call when it is the only one that can
  // (the conversation runs on a provider without web access - the case the
  // section exists for), or when the operator asked for it explicitly with
  // `always: true` (one account, one bill, one set of credentials).
  if (sp_capable && (search.always || !turn_can)) {
    if (!sp->has_key()) {
      if (err) *err = "the search provider '" + sp->name + "' has no api_key configured";
      return false;
    }
    *out = *sp;
    if (want_search) {
      out->supports_search = true;
      out->search_path = search.search_path;
    } else {
      out->supports_fetch = true;
      out->fetch_path = search.fetch_path;
    }
    if (via_search_provider) *via_search_provider = true;
    return true;
  }

  if (turn_can) {
    *out = *turn_provider;
    return true;
  }

  if (err) {
    if (turn_provider) {
      *err = "provider '" + turn_provider->name + "' does not support " + what + " (set " + flag +
             ": true in provider.yaml, or point the 'search:' section at a provider that does)";
    } else {
      *err = "no provider is active for this turn";
    }
  }
  return false;
}

bool AppConfig::set_active(const std::string& provider_name, const std::string& model_name,
                           std::string* err) {
  if (providers.empty()) {
    if (err) *err = "no providers configured";
    return false;
  }
  std::string pname = provider_name.empty() ? active_provider : provider_name;
  const ProviderInfo* p = nullptr;
  if (!pname.empty()) p = provider(pname);
  if (!p) {
    p = provider(provider_order.front());
    pname = p ? p->name : std::string();
  }
  if (!p) {
    if (err) *err = "provider not found: " + provider_name;
    return false;
  }

  std::string mname = model_name;
  if (mname.empty() && pname == active_provider) mname = active_model;
  // The first model in the list is the default; `default_model:` only matters
  // for a provider that has no list at all.
  if (mname.empty()) mname = p->primary_model();
  if (p->find_model(mname) == nullptr) {
    if (!p->models.empty()) {
      if (err) *err = "model '" + mname + "' not found for provider '" + p->name + "'; using '" +
                      p->models.front() + "'";
      mname = p->models.front();
    } else {
      // Provider without a declared model list: accept whatever was asked for.
      if (mname.empty()) {
        if (err) *err = "provider '" + p->name + "' has no models";
        return false;
      }
    }
  }
  active_provider = pname;
  active_model = mname;
  return true;
}

bool AppConfig::lookup_reasoning_echo(const std::string& provider_name, bool* out) const {
  std::lock_guard<std::mutex> lock(reasoning_mu_);
  std::map<std::string, bool>::const_iterator it = reasoning_echo_learned_.find(provider_name);
  if (it == reasoning_echo_learned_.end()) return false;
  if (out) *out = it->second;
  return true;
}

void AppConfig::remember_reasoning_echo(const std::string& provider_name, bool keep) {
  std::lock_guard<std::mutex> lock(reasoning_mu_);
  reasoning_echo_learned_[provider_name] = keep;
}

bool AppConfig::lookup_usage_flag_supported(const std::string& provider_name, bool* out) const {
  std::lock_guard<std::mutex> lock(reasoning_mu_);
  auto it = usage_flag_learned_.find(provider_name);
  if (it == usage_flag_learned_.end()) return false;
  if (out) *out = it->second;
  return true;
}

void AppConfig::remember_usage_flag_supported(const std::string& provider_name, bool supported) {
  std::lock_guard<std::mutex> lock(reasoning_mu_);
  usage_flag_learned_[provider_name] = supported;
}

std::string AppConfig::default_system_prompt() const {
  if (!settings.system_prompt.empty()) return settings.system_prompt;
  return
      "You are tea_agent, a pragmatic software-engineering agent running locally on the "
      "user's machine.\n"
      "You work inside a single workspace directory and you can act on it through tools.\n\n"
      "Rules:\n"
      "1. Prefer doing over describing: use tools to inspect and change files instead of "
      "guessing.\n"
      "2. Run `toolkit_exec` to inspect the environment (list files, build, run tests).\n"
      "3. Use `toolkit_file` to read and write files. Paths are relative to the workspace "
      "root.\n"
      "4. Chain multiple tool calls when needed; keep going until the task is actually "
      "done.\n"
      "5. When you are finished, reply with a short summary of what you changed and how you "
      "verified it.\n"
      "6. Never invent tool output. If a command fails, read the error and adapt.\n"
      "7. Answer in the same language the user writes in.";
}

}  // namespace tea
