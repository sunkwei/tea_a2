#include "model_discovery.h"

#include <algorithm>
#include <initializer_list>

#include <nlohmann/json.hpp>

#include "http.h"
#include "llm.h"
#include "log.h"
#include "util.h"

namespace tea {
namespace {

// --------------------------------------------------------------------------
// reading a number / a flag out of a provider's JSON
// --------------------------------------------------------------------------

/// The objects a gateway may hide the details in. OpenRouter puts the real
/// numbers under "top_provider", Ollama under "details".
const char* kMetaObjects[] = {"top_provider", "details", "meta", "metadata", "limits"};

int64_t json_int(const nlohmann::json& j) {
  if (j.is_number_integer()) return j.get<int64_t>();
  if (j.is_number()) return static_cast<int64_t>(j.get<double>());
  if (j.is_string()) {
    // Some gateways answer "128000" as a string.
    try {
      return static_cast<int64_t>(std::stoll(j.get<std::string>()));
    } catch (const std::exception&) {
      return 0;
    }
  }
  return 0;
}

/// The first of `keys` that carries a number, in the object itself or in one
/// of the nested metadata objects.
int64_t find_int(const nlohmann::json& j, std::initializer_list<const char*> keys, int depth = 0) {
  if (!j.is_object()) return 0;
  for (const char* key : keys) {
    auto it = j.find(key);
    if (it == j.end() || it->is_null()) continue;
    const int64_t v = json_int(*it);
    if (v > 0) return v;
  }
  if (depth >= 2) return 0;
  for (const char* sub : kMetaObjects) {
    auto sit = j.find(sub);
    if (sit == j.end() || !sit->is_object()) continue;
    const int64_t v = find_int(*sit, keys, depth + 1);
    if (v > 0) return v;
  }
  return 0;
}

std::string find_str(const nlohmann::json& j, std::initializer_list<const char*> keys) {
  if (!j.is_object()) return std::string();
  for (const char* key : keys) {
    auto it = j.find(key);
    if (it == j.end() || !it->is_string()) continue;
    return trim(it->get<std::string>());
  }
  return std::string();
}

/// The capability flags a provider may state directly.
bool find_bool(const nlohmann::json& j, const char* key) {
  if (!j.is_object()) return false;
  auto it = j.find(key);
  if (it == j.end()) return false;
  if (it->is_boolean()) return it->get<bool>();
  if (it->is_string()) {
    const std::string s = to_lower(trim(it->get<std::string>()));
    return s == "true" || s == "yes" || s == "1";
  }
  return false;
}

/// The capability flags, read from the many shapes gateways use:
///
///   "supported_parameters": ["tools","reasoning",...]   (OpenRouter)
///   "capabilities": ["tools","vision","thinking",...]    (Ollama)
///   "modalities": ["text","image"]                       (OpenRouter)
///   "architecture": {"input_modalities": ["text","image"]}
///   "supports_tools": true                              (plain flags)
struct Caps {
  bool tools = false;
  bool reasoning = false;
  bool vision = false;
};

Caps find_caps(const nlohmann::json& j) {
  Caps caps;
  if (!j.is_object()) return caps;

  const char* array_keys[] = {"supported_parameters", "capabilities", "modalities",
                              "input_modalities", "features", "supported_features"};
  for (const char* key : array_keys) {
    auto it = j.find(key);
    if (it == j.end() || !it->is_array()) continue;
    for (const auto& v : *it) {
      if (!v.is_string()) continue;
      const std::string s = to_lower(trim(v.get<std::string>()));
      if (s.empty()) continue;
      if (s == "tools" || s == "tool_choice" || s == "tool_use" || s == "function_calling") {
        caps.tools = true;
      } else if (s == "reasoning" || s == "include_reasoning" || s == "thinking" ||
                 s == "reasoning_content") {
        caps.reasoning = true;
      } else if (s == "image" || s == "vision" || s == "multimodal" || s == "image_input") {
        caps.vision = true;
      }
    }
  }
  // Ollama: "architecture": {"input_modalities": ["text", "image"]}
  auto arch = j.find("architecture");
  if (arch != j.end() && arch->is_object()) {
    auto mods = arch->find("input_modalities");
    if (mods != arch->end() && mods->is_array()) {
      for (const auto& v : *mods) {
        if (!v.is_string()) continue;
        const std::string s = to_lower(trim(v.get<std::string>()));
        if (s == "image" || s == "vision") caps.vision = true;
      }
    }
  }
  // Plain boolean flags.
  if (find_bool(j, "supports_tools") || find_bool(j, "tool_use")) {
    caps.tools = true;
  }
  if (find_bool(j, "supports_reasoning") || find_bool(j, "thinking")) {
    caps.reasoning = true;
  }
  if (find_bool(j, "supports_vision") || find_bool(j, "vision")) {
    caps.vision = true;
  }
  return caps;
}

// --------------------------------------------------------------------------
// the model list, whatever shape the gateway uses
// --------------------------------------------------------------------------

/// Pull the model entries out of the answer. The shapes seen in the wild:
///   {"data": [{"id": "..."}]}            (OpenAI and friends)
///   {"models": [{"name": "..."}]}         (Ollama)
///   {"models": ["a", "b"]}                (a plain list of names)
///   [{"id": "..."}]                       (a bare array)
///   {"model-name": {...}}                 (a bare map)
void collect_entries(const nlohmann::json& root, std::vector<nlohmann::json>* out) {
  if (!out) return;
  if (root.is_array()) {
    for (const auto& e : root) out->push_back(e);
    return;
  }
  if (!root.is_object()) return;

  const char* list_keys[] = {"data", "models", "result", "results", "items", "model_list"};
  for (const char* key : list_keys) {
    auto it = root.find(key);
    if (it == root.end() || !it->is_array()) continue;
    for (const auto& e : *it) out->push_back(e);
    return;
  }
  // A bare map: {"gpt-4o": {...}, "gpt-4o-mini": {...}}
  for (auto it = root.begin(); it != root.end(); ++it) {
    const std::string key = it.key();
    nlohmann::json entry = nlohmann::json::object();
    entry["id"] = key;
    if (it.value().is_object()) {
      // The key is the model name; the value carries what is known about it.
      entry = it.value();
      entry["id"] = key;
    }
    out->push_back(entry);
  }
}

/// The model id of one entry: a string entry is its own name, an object
/// carries it under one of the usual keys.
std::string entry_id(const nlohmann::json& entry) {
  if (entry.is_string()) return trim(entry.get<std::string>());
  if (!entry.is_object()) return std::string();
  std::string id = find_str(entry, {"id", "name", "model", "model_name", "slug"});
  if (id.empty()) return std::string();
  // Ollama appends the tag: "llama3:latest". Keep it - it is the model name.
  return id;
}

// --------------------------------------------------------------------------
// what tea_a2 knows about a model name
// --------------------------------------------------------------------------

struct BuiltinModel {
  const char* name;  // exact match, lower-cased
  int64_t max_context_tokens;
  int64_t max_output_tokens;
  bool tools;
  bool reasoning;
  bool vision;
};

// A short table of the models this project actually talks to. It is a
// *fallback*: the provider's own answer always wins, and a value that is
// already in provider.yaml wins over the table (a name means different things
// on different gateways).
const BuiltinModel kBuiltinModels[] = {
    {"deepseek-chat", 128000, 8192, true, false, false},
    {"deepseek-reasoner", 128000, 65536, true, true, false},
    {"deepseek-v3", 128000, 8192, true, false, false},
    {"deepseek-v3.1", 128000, 8192, true, false, false},
    {"deepseek-v3.2", 128000, 8192, true, false, false},
    {"minimax-m3.1-flash", 1000000, 128000, true, true, true},
    {"minimax-m2", 1000000, 128000, true, true, true},
    {"minimax-m2.1", 1000000, 128000, true, true, true},
    {"gpt-4o", 128000, 16384, true, false, true},
    {"gpt-4o-mini", 128000, 16384, true, false, true},
    {"gpt-4.1", 1047576, 32768, true, false, true},
    {"gpt-4.1-mini", 1047576, 32768, true, false, true},
    {"gpt-5", 400000, 128000, true, true, true},
    {"o3", 200000, 100000, true, true, true},
    {"o3-mini", 200000, 100000, true, true, false},
    {"o4-mini", 200000, 100000, true, true, false},
    {"claude-3-5-sonnet", 200000, 8192, true, false, true},
    {"claude-3-7-sonnet", 200000, 64000, true, true, true},
    {"claude-sonnet-4", 200000, 64000, true, true, true},
    {"claude-opus-4", 200000, 32000, true, true, true},
    {"glm-4.5", 128000, 16384, true, false, false},
    {"glm-4.6", 200000, 32768, true, false, false},
    {"kimi-k2", 128000, 16384, true, false, false},
    {"qwen3-235b", 262144, 65536, true, false, false},
};

}  // namespace

bool builtin_model_defaults(const std::string& model_id, ModelInfo* out) {
  if (!out) return false;
  const std::string id = to_lower(trim(model_id));
  if (id.empty()) return false;
  for (std::size_t i = 0; i < sizeof(kBuiltinModels) / sizeof(kBuiltinModels[0]); ++i) {
    if (id == kBuiltinModels[i].name) {
      out->name = model_id;
      out->max_context_tokens = kBuiltinModels[i].max_context_tokens;
      out->max_output_tokens = kBuiltinModels[i].max_output_tokens;
      out->supports_tools = kBuiltinModels[i].tools;
      out->supports_reasoning = kBuiltinModels[i].reasoning;
      out->supports_vision = kBuiltinModels[i].vision;
      return true;
    }
  }
  return false;
}

namespace {

/// What the model *name* gives away. Not much, but better than nothing: a
/// "-reasoner" model reasons, a "-vl" model sees.
void guess_from_name(const std::string& model_id, ModelInfo* out) {
  const std::string id = to_lower(model_id);
  if (id.find("reasoner") != std::string::npos || id.find("thinking") != std::string::npos ||
      id.find("qwq") != std::string::npos || id.find("-r1") != std::string::npos) {
    out->supports_reasoning = true;
  }
  if (id.find("-vl") != std::string::npos || id.find("vision") != std::string::npos ||
      id.find("omni") != std::string::npos || id.find("-v-") != std::string::npos) {
    out->supports_vision = true;
  }
}

std::string truncate_note(const std::string& s) {
  std::string out = trim(s);
  if (out.size() > 160) out = truncate_utf8(out, 160) + "…";
  return out;
}

}  // namespace

bool discover_provider_models(const ProviderInfo& provider, ModelDiscovery* out) {
  if (!out) return false;
  *out = ModelDiscovery();

  const std::string url = models_url(provider.api_url);
  out->url = url;
  if (url.empty()) {
    out->error = "provider '" + provider.name + "' has no api_url";
    return false;
  }

  // What provider.yaml holds today, so the answer can say what is new and
  // what would be dropped.
  for (std::size_t i = 0; i < provider.models.size(); ++i) {
    const ModelInfo* info = provider.find_model(provider.models[i]);
    DiscoveredModel m;
    m.id = provider.models[i];
    m.in_config = true;
    m.source = "config";
    if (info) {
      m.max_context_tokens = info->max_context_tokens;
      m.max_output_tokens = info->max_output_tokens;
      m.supports_tools = info->supports_tools;
      m.supports_reasoning = info->supports_reasoning;
      m.supports_vision = info->supports_vision;
      m.note = info->note;
    }
    out->current.push_back(m);
  }

  HttpRequest req;
  req.method = "GET";
  req.url = url;
  if (provider.has_key()) {
    HttpHeader auth;
    auth.name = "Authorization";
    auth.value = "Bearer " + provider.api_key;
    req.headers.push_back(auth);
  }
  // Some gateways need a routing header even for the model list.
  for (std::size_t i = 0; i < provider.headers.size(); ++i) {
    HttpHeader h;
    h.name = provider.headers[i].first;
    h.value = replace_all(provider.headers[i].second, "${session_id}", "");
    req.headers.push_back(h);
  }
  HttpHeader accept;
  accept.name = "Accept";
  accept.value = "application/json";
  req.headers.push_back(accept);
  // A model list is small and comes back fast; a slow one is a broken one.
  req.connect_timeout_ms = 10000;
  req.read_timeout_ms = 30000;
  req.body_limit = 4 * 1024 * 1024;

  const HttpResponse res = http_perform(req);
  if (!res.ok) {
    out->error = "the model list request failed: " +
                 (res.error.empty() ? std::string("no response") : res.error);
    return false;
  }
  if (res.status != 200) {
    std::string body = trim(res.body);
    if (body.size() > 200) body = truncate_utf8(body, 200) + "...";
    out->error = "the model list endpoint answered HTTP " + std::to_string(res.status) +
                 (body.empty() ? "" : ": " + body);
    return false;
  }

  const nlohmann::json root = nlohmann::json::parse(res.body, nullptr, false);
  if (root.is_discarded()) {
    std::string body = trim(res.body);
    if (body.size() > 200) body = truncate_utf8(body, 200) + "...";
    out->error = "the model list endpoint did not answer JSON: " + body;
    return false;
  }

  std::vector<nlohmann::json> entries;
  collect_entries(root, &entries);
  if (entries.empty()) {
    out->error = "the model list endpoint answered, but no model was in it";
    return false;
  }

  std::vector<std::string> seen;
  for (std::size_t i = 0; i < entries.size(); ++i) {
    const std::string id = entry_id(entries[i]);
    if (id.empty()) continue;
    if (std::find(seen.begin(), seen.end(), id) != seen.end()) continue;
    seen.push_back(id);

    const nlohmann::json& entry = entries[i];
    DiscoveredModel m;
    m.id = id;

    // 1. what the provider says
    const int64_t ctx = find_int(entry, {"max_context_tokens", "context_length", "context_window",
                                         "max_context_length", "max_input_tokens", "max_model_len",
                                         "max_prompt_tokens", "n_ctx"});
    const int64_t out_tokens = find_int(entry, {"max_output_tokens", "max_completion_tokens",
                                                "max_tokens", "output_token_limit",
                                                "max_new_tokens"});
    const Caps caps = find_caps(entry);
    const std::string description = find_str(entry, {"description", "display_name", "note"});

    // 2. what provider.yaml already says (the user's own numbers win over the
    //    static table: on a local gateway "gpt-4o" can be anything)
    const ModelInfo* configured = provider.find_model(id);
    ModelInfo info;
    info.name = id;
    if (configured) {
      info.max_context_tokens = configured->max_context_tokens;
      info.max_output_tokens = configured->max_output_tokens;
      info.supports_tools = configured->supports_tools;
      info.supports_reasoning = configured->supports_reasoning;
      info.supports_vision = configured->supports_vision;
      info.reasoning_effort = configured->reasoning_effort;
      info.note = configured->note;
      info.has_temperature = configured->has_temperature;
      info.temperature = configured->temperature;
      info.has_top_p = configured->has_top_p;
      info.top_p = configured->top_p;
    }

    ModelInfo builtin;
    const bool has_builtin = builtin_model_defaults(id, &builtin);

    if (ctx > 0) {
      info.max_context_tokens = ctx;
      m.source = "provider";
    } else if (info.max_context_tokens > 0) {
      m.source = "config";
    } else if (has_builtin) {
      info.max_context_tokens = builtin.max_context_tokens;
      m.source = "builtin";
    } else {
      m.source = "default";
    }
    if (out_tokens > 0) {
      info.max_output_tokens = out_tokens;
      if (m.source == "default") m.source = "provider";
    } else if (info.max_output_tokens == 0 && has_builtin) {
      info.max_output_tokens = builtin.max_output_tokens;
      if (m.source == "default") m.source = "builtin";
    }
    if (caps.tools) {
      info.supports_tools = true;
      if (m.source == "default") m.source = "provider";
    }
    if (caps.reasoning) {
      info.supports_reasoning = true;
      if (m.source == "default") m.source = "provider";
    }
    if (caps.vision) {
      info.supports_vision = true;
      if (m.source == "default") m.source = "provider";
    }
    if (m.source == "default" || m.source == "config") {
      // Nothing came from the endpoint - the name may still give something.
      const bool before = info.supports_reasoning || info.supports_vision;
      guess_from_name(id, &info);
      if (!before && (info.supports_reasoning || info.supports_vision) &&
          m.source != "builtin") {
        m.source = "guess";
      }
    }
    if (info.note.empty() && !description.empty()) info.note = truncate_note(description);

    m.max_context_tokens = info.max_context_tokens;
    m.max_output_tokens = info.max_output_tokens;
    m.supports_tools = info.supports_tools;
    m.supports_reasoning = info.supports_reasoning;
    m.supports_vision = info.supports_vision;
    m.note = info.note;
    m.in_config = configured != nullptr;
    out->models.push_back(m);
  }

  if (out->models.empty()) {
    out->error = "the model list endpoint answered, but no model name was in it";
    return false;
  }

  // What is new, and what would be dropped.
  for (std::size_t i = 0; i < out->models.size(); ++i) {
    if (!out->models[i].in_config) out->added.push_back(out->models[i].id);
  }

  // The order is the switch: the FIRST model of a provider is its default.
  // So the models that are already in provider.yaml keep their place, and the
  // new ones are appended - otherwise a provider that lists its models
  // alphabetically would quietly change the default of every conversation.
  {
    std::vector<DiscoveredModel> ordered;
    ordered.reserve(out->models.size());
    for (std::size_t i = 0; i < out->current.size(); ++i) {
      const std::string& id = out->current[i].id;
      for (std::size_t m = 0; m < out->models.size(); ++m) {
        if (out->models[m].id == id) {
          ordered.push_back(out->models[m]);
          break;
        }
      }
    }
    for (std::size_t m = 0; m < out->models.size(); ++m) {
      if (!out->models[m].in_config) ordered.push_back(out->models[m]);
    }
    out->models.swap(ordered);
  }
  for (std::size_t i = 0; i < out->current.size(); ++i) {
    const std::string& id = out->current[i].id;
    if (std::find(seen.begin(), seen.end(), id) == seen.end()) out->removed.push_back(id);
  }
  if (!out->removed.empty()) {
    std::string list;
    for (std::size_t i = 0; i < out->removed.size(); ++i) {
      if (i) list += ", ";
      list += out->removed[i];
    }
    out->warnings.push_back("provider.yaml lists models the provider does not offer: " + list);
  }

  out->ok = true;
  out->error.clear();
  LOG_INFO("model discovery: provider '", provider.name, "' offers ", out->models.size(),
           " model(s) via ", url);
  return true;
}

}  // namespace tea
