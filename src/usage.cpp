#include "usage.h"

#include <algorithm>
#include <vector>

#include "http.h"
#include "log.h"
#include "util.h"

namespace tea {
namespace {

struct UsageType {
  const char* name;
  const char* default_url;
};

// The parsers tea_a2 knows. A new provider endpoint needs a new entry here
// (and a parser below) - there is no "guess the shape" mode on purpose.
const UsageType kUsageTypes[] = {
    {"minimax_token_plan", "https://www.minimax.cn/v1/token_plan/remains"},
    // No default endpoint: the config has to say where to call.
    {"json_fields", ""},
};

std::string trim_error(const std::string& s, size_t max_len = 300) {
  std::string out = trim(s);
  // On a character boundary: the text ends up in a JSON response.
  if (out.size() > max_len) out = truncate_utf8(out, max_len) + "...";
  return out;
}

double jnum(const nlohmann::json& j, const char* key) {
  if (!j.is_object()) return -1.0;
  auto it = j.find(key);
  if (it == j.end() || !it->is_number()) return -1.0;
  return it->get<double>();
}

int64_t jint(const nlohmann::json& j, const char* key) {
  if (!j.is_object()) return 0;
  auto it = j.find(key);
  if (it == j.end() || !it->is_number()) return 0;
  if (it->is_number_integer()) return it->get<int64_t>();
  return static_cast<int64_t>(it->get<double>());
}

int jstatus(const nlohmann::json& j, const char* key) {
  if (!j.is_object()) return -1;
  auto it = j.find(key);
  if (it == j.end() || !it->is_number()) return -1;
  return it->get<int>();
}

// MiniMax M Plan: model_remains[] with one entry per model group ("general").
// The 5-hour window is "interval", the weekly one is "weekly".
bool parse_minimax_token_plan(const nlohmann::json& j, UsageQuota* out, std::string* err) {
  if (j.is_object()) {
    auto base = j.find("base_resp");
    if (base != j.end() && base->is_object()) {
      const int code = jstatus(*base, "status_code");
      if (code != 0 && code != -1) {
        const std::string msg = base->value("status_msg", std::string(""));
        *err = "provider answered status_code " + std::to_string(code) +
               (msg.empty() ? "" : (": " + msg));
        return false;
      }
    }
  }

  const nlohmann::json* rows = nullptr;
  if (j.is_object()) {
    auto it = j.find("model_remains");
    if (it != j.end() && it->is_array()) rows = &(*it);
  }
  if (!rows || rows->empty()) {
    // A pay-as-you-go key has no subscription: the endpoint answers with an
    // empty list rather than an error. Say so instead of "no data".
    *err = "该 key 没有订阅额度（model_remains 为空）";
    return false;
  }

  const nlohmann::json* pick = nullptr;
  for (std::size_t i = 0; i < rows->size(); ++i) {
    const nlohmann::json& row = (*rows)[i];
    if (!row.is_object()) continue;
    if (row.value("model_name", std::string()) == "general") {
      pick = &row;
      break;
    }
    if (!pick) pick = &row;
  }
  if (!pick) {
    *err = "model_remains 里没有可解析的条目";
    return false;
  }

  out->model_name = pick->value("model_name", std::string());

  UsageWindow w;
  w.present = true;
  w.name = "5h";
  w.remaining_percent = jnum(*pick, "current_interval_remaining_percent");
  w.start_ms = jint(*pick, "start_time");
  w.end_ms = jint(*pick, "end_time");
  w.remains_ms = jint(*pick, "remains_time");
  w.status = jstatus(*pick, "current_interval_status");
  out->window = w;

  UsageWindow week;
  week.present = true;
  week.name = "weekly";
  week.remaining_percent = jnum(*pick, "current_weekly_remaining_percent");
  week.start_ms = jint(*pick, "weekly_start_time");
  week.end_ms = jint(*pick, "weekly_end_time");
  week.remains_ms = jint(*pick, "weekly_remains_time");
  week.status = jstatus(*pick, "current_weekly_status");
  out->weekly = week;

  return true;
}

// Walk a dotted path ("data.quota.remaining", "list.0.used") through a JSON
// document. Returns false when any step is missing or has the wrong shape.
bool json_path(const nlohmann::json& root, const std::string& path, nlohmann::json* out) {
  if (!out) return false;
  const nlohmann::json* cur = &root;
  size_t pos = 0;
  while (pos <= path.size()) {
    const size_t dot = path.find('.', pos);
    const std::string key = path.substr(pos, dot == std::string::npos ? std::string::npos : dot - pos);
    if (key.empty()) return false;
    if (cur->is_array()) {
      // An array index.
      bool numeric = true;
      for (std::size_t i = 0; i < key.size(); ++i) {
        if (key[i] < '0' || key[i] > '9') { numeric = false; break; }
      }
      if (!numeric) return false;
      const std::size_t idx = static_cast<std::size_t>(std::stoull(key));
      if (idx >= cur->size()) return false;
      cur = &(*cur)[idx];
    } else if (cur->is_object()) {
      auto it = cur->find(key);
      if (it == cur->end()) return false;
      cur = &(*it);
    } else {
      return false;
    }
    if (dot == std::string::npos) break;
    pos = dot + 1;
  }
  *out = *cur;
  return true;
}

double field_number(const nlohmann::json& root, const std::map<std::string, std::string>& fields,
                    const std::string& name, bool* found) {
  if (found) *found = false;
  auto it = fields.find(name);
  if (it == fields.end()) return -1.0;
  nlohmann::json v;
  if (!json_path(root, it->second, &v) || !v.is_number()) return -1.0;
  if (found) *found = true;
  return v.get<double>();
}

std::string field_string(const nlohmann::json& root,
                          const std::map<std::string, std::string>& fields, const std::string& name) {
  auto it = fields.find(name);
  if (it == fields.end()) return std::string();
  nlohmann::json v;
  if (!json_path(root, it->second, &v) || !v.is_string()) return std::string();
  return v.get<std::string>();
}

// A provider-specific quota endpoint whose JSON shape we do not know in
// advance: the config says which dotted path holds which number.
//
//   usage_type: json_fields
//   usage_fields:
//     remaining_percent: data.quota.remainingPercent
//     used: data.quota.used
//     total: data.quota.total
//     reset_at_ms: data.quota.resetAt
bool parse_json_fields(const nlohmann::json& j, const std::map<std::string, std::string>& fields,
                       UsageQuota* out, std::string* err) {
  if (fields.empty()) {
    *err = "usage_type 'json_fields' needs usage_fields (which JSON path holds which number)";
    return false;
  }

  UsageWindow w;
  w.present = true;
  w.name = field_string(j, fields, "label");
  if (w.name.empty()) w.name = "quota";

  bool has_pct = false;
  w.remaining_percent = field_number(j, fields, "remaining_percent", &has_pct);
  bool has_used = false, has_total = false;
  const double used = field_number(j, fields, "used", &has_used);
  const double total = field_number(j, fields, "total", &has_total);
  if (!has_pct && has_used && has_total && total > 0) {
    w.remaining_percent = (total - used) / total * 100.0;
    has_pct = true;
  }
  if (!has_pct) {
    *err = "json_fields: no remaining_percent and no used/total pair matched";
    return false;
  }
  bool has_reset = false;
  const double reset_at = field_number(j, fields, "reset_at_ms", &has_reset);
  bool has_reset_in = false;
  const double reset_in = field_number(j, fields, "reset_in_ms", &has_reset_in);
  if (has_reset) {
    w.end_ms = static_cast<int64_t>(reset_at);
    w.start_ms = w.end_ms;
  }
  if (has_reset_in) w.remains_ms = static_cast<int64_t>(reset_in);
  out->window = w;

  bool has_weekly = false;
  UsageWindow week;
  week.present = true;
  week.name = "weekly";
  week.remaining_percent = field_number(j, fields, "weekly_remaining_percent", &has_weekly);
  if (!has_weekly) {
    bool has_w_used = false, has_w_total = false;
    const double w_used = field_number(j, fields, "weekly_used", &has_w_used);
    const double w_total = field_number(j, fields, "weekly_total", &has_w_total);
    if (has_w_used && has_w_total && w_total > 0) {
      week.remaining_percent = (w_total - w_used) / w_total * 100.0;
      has_weekly = true;
    }
  }
  if (has_weekly) {
    bool has_w_reset = false;
    const double w_reset = field_number(j, fields, "weekly_reset_at_ms", &has_w_reset);
    if (has_w_reset) week.end_ms = static_cast<int64_t>(w_reset);
    bool has_w_reset_in = false;
    const double w_reset_in = field_number(j, fields, "weekly_reset_in_ms", &has_w_reset_in);
    if (has_w_reset_in) week.remains_ms = static_cast<int64_t>(w_reset_in);
    out->weekly = week;
  }
  return true;
}

}  // namespace

std::string usage_type_default_url(const std::string& type) {
  const std::string t = to_lower(trim(type));
  for (const UsageType& u : kUsageTypes) {
    if (t == u.name) return u.default_url;
  }
  return std::string();
}

bool usage_type_known(const std::string& type) {
  const std::string t = to_lower(trim(type));
  for (const UsageType& u : kUsageTypes) {
    if (t == u.name) return true;
  }
  return false;
}

std::string usage_type_list() {
  std::string out;
  for (const UsageType& u : kUsageTypes) {
    if (!out.empty()) out += ", ";
    out += u.name;
  }
  return out;
}

nlohmann::json UsageQuota::to_json() const {
  nlohmann::json j = nlohmann::json::object();
  j["ok"] = ok;
  if (!error.empty()) j["error"] = error;
  j["provider"] = provider;
  j["type"] = type;
  if (!model_name.empty()) j["model_name"] = model_name;

  auto window_json = [](const UsageWindow& w) {
    nlohmann::json o = nlohmann::json::object();
    o["present"] = w.present;
    o["name"] = w.name;
    o["remaining_percent"] = w.remaining_percent;
    o["start_ms"] = w.start_ms;
    o["end_ms"] = w.end_ms;
    o["remains_ms"] = w.remains_ms;
    o["status"] = w.status;
    return o;
  };
  j["window"] = window_json(window);
  j["weekly"] = window_json(weekly);
  return j;
}

bool query_usage(const ProviderInfo& provider, UsageQuota* out) {
  if (!out) return false;
  *out = UsageQuota();
  out->provider = provider.name;
  out->type = provider.usage_type;

  if (!provider.supports_usage) {
    out->error = "provider '" + provider.name + "' does not declare supports_usage";
    return false;
  }
  if (!usage_type_known(provider.usage_type)) {
    out->error = "unknown usage_type '" + provider.usage_type + "'";
    return false;
  }
  if (!provider.has_key()) {
    out->error = "provider '" + provider.name + "' has no api_key";
    return false;
  }

  std::string url = trim(provider.usage_url);
  if (url.empty()) url = usage_type_default_url(provider.usage_type);
  if (url.empty()) {
    out->error = "usage_url is required for usage_type '" + provider.usage_type + "'";
    return false;
  }

  HttpRequest req;
  req.method = "GET";
  req.url = url;
  // How the key is presented. "bearer" is the OpenAI-ish default; "api-key" is
  // what MiMo's Token Plan documents (api-key: $MIMO_API_KEY); "none" is for
  // endpoints that live behind a session cookie instead of the key.
  const std::string auth_kind = provider.usage_auth.empty() ? "bearer" : provider.usage_auth;
  if (auth_kind == "bearer") {
    HttpHeader auth;
    auth.name = "Authorization";
    auth.value = "Bearer " + provider.api_key;
    req.headers.push_back(auth);
  } else if (auth_kind == "api-key") {
    HttpHeader auth;
    auth.name = "api-key";
    auth.value = provider.api_key;
    req.headers.push_back(auth);
  }
  for (std::size_t i = 0; i < provider.usage_headers.size(); ++i) {
    HttpHeader h;
    h.name = provider.usage_headers[i].first;
    h.value = provider.usage_headers[i].second;
    req.headers.push_back(h);
  }
  HttpHeader accept;
  accept.name = "Accept";
  accept.value = "application/json";
  req.headers.push_back(accept);
  // A quota endpoint is a tiny answer; a slow one is a broken one. Keep the
  // budget small so a wrong URL cannot hang the UI.
  req.connect_timeout_ms = 10000;
  req.read_timeout_ms = 20000;
  req.body_limit = 512 * 1024;

  const HttpResponse res = http_perform(req);
  if (!res.ok) {
    out->error = "usage request failed: " + trim_error(res.error.empty() ? "no response" : res.error);
    return false;
  }
  if (res.status != 200) {
    out->error = "usage endpoint answered HTTP " + std::to_string(res.status) + ": " +
                 trim_error(res.body, 200);
    return false;
  }

  nlohmann::json j = nlohmann::json::parse(res.body, nullptr, false);
  if (j.is_discarded()) {
    out->error = "usage endpoint did not answer JSON: " + trim_error(res.body, 200);
    return false;
  }

  std::string err;
  bool ok = false;
  if (provider.usage_type == "minimax_token_plan") {
    ok = parse_minimax_token_plan(j, out, &err);
  } else if (provider.usage_type == "json_fields") {
    ok = parse_json_fields(j, provider.usage_fields, out, &err);
  } else {
    err = "no parser for usage_type '" + provider.usage_type + "'";
  }
  if (!ok) {
    out->ok = false;
    out->error = err.empty() ? "usage response could not be parsed" : err;
    return false;
  }
  out->ok = true;
  out->error.clear();
  return true;
}

}  // namespace tea
