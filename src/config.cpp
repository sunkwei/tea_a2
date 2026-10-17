#include "config.h"

#include <yaml-cpp/yaml.h>

#include <algorithm>

#include "log.h"
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
  if (yhas(node, "enable_thinking")) s.enable_thinking = ybool(node, "enable_thinking", s.enable_thinking);
  if (yhas(node, "reasoning_effort")) s.reasoning_effort = ystr(node, "reasoning_effort", s.reasoning_effort);
  if (yhas(node, "temperature")) s.temperature = ydouble(node, "temperature", s.temperature);
  if (yhas(node, "top_p")) s.top_p = ydouble(node, "top_p", s.top_p);
  if (yhas(node, "max_tokens")) s.max_tokens = yint(node, "max_tokens", s.max_tokens);
  if (yhas(node, "command_timeout_ms"))
    s.command_timeout_ms = static_cast<int>(yint(node, "command_timeout_ms", s.command_timeout_ms));
  if (yhas(node, "http_connect_timeout_ms"))
    s.http_connect_timeout_ms = static_cast<int>(yint(node, "http_connect_timeout_ms", s.http_connect_timeout_ms));
  if (yhas(node, "http_read_timeout_ms"))
    s.http_read_timeout_ms = static_cast<int>(yint(node, "http_read_timeout_ms", s.http_read_timeout_ms));
  if (yhas(node, "allow_outside_workspace"))
    s.allow_outside_workspace = ybool(node, "allow_outside_workspace", s.allow_outside_workspace);
  if (yhas(node, "stream")) s.stream = ybool(node, "stream", s.stream);
  if (yhas(node, "stream_include_usage"))
    s.stream_include_usage = ybool(node, "stream_include_usage", s.stream_include_usage);
  if (yhas(node, "utf8_console")) s.utf8_console = ybool(node, "utf8_console", s.utf8_console);
  if (yhas(node, "shell")) s.shell = ystr(node, "shell", s.shell);
  if (yhas(node, "system_prompt")) s.system_prompt = ystr(node, "system_prompt", s.system_prompt);
  if (s.max_iterations < 1) s.max_iterations = 1;
  if (s.command_timeout_ms < 1000) s.command_timeout_ms = 1000;
}

}  // namespace

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

  if (!path_exists(path)) {
    error = "provider config not found: " + path;
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

  // Resolve the active provider/model: roles.main > first provider default.
  const YAML::Node main_role = root["roles"] ? root["roles"]["main"] : YAML::Node();
  if (main_role && main_role.IsMap()) {
    active_provider = trim(ystr(main_role, "provider"));
    active_model = trim(ystr(main_role, "model"));
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
  if (mname.empty()) mname = p->default_model;
  if (mname.empty() && !p->models.empty()) mname = p->models.front();
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
