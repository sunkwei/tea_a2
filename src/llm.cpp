// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
#include "llm.h"

#include <chrono>
#include <map>
#include <thread>

#include "http.h"
#include "log.h"
#include "util.h"

namespace tea {
namespace {

/// The known reasoning levels. "auto" means "do not send the field at all" -
/// the provider then uses its own default, which is the only safe choice for
/// a gateway that does not know `reasoning_effort` (it answers 400).
///
/// The names are the ones OpenAI uses; gateways that spell them differently
/// (xhigh, max) are accepted as well - the value goes on the wire unchanged,
/// so what the provider understands is what it gets.
const char* const kEfforts[] = {"auto", "off", "minimal", "low", "medium",
                                "high", "xhigh", "max"};
const int kEffortCount = static_cast<int>(sizeof(kEfforts) / sizeof(kEfforts[0]));


std::string strip_trailing_slashes(std::string s) {
  while (!s.empty() && s.back() == '/') s.pop_back();
  return s;
}

bool url_has_path(const std::string& url) {
  const std::size_t scheme_end = url.find("://");
  if (scheme_end == std::string::npos) return false;
  return url.find('/', scheme_end + 3) != std::string::npos;
}

std::string endpoint(const std::string& api_url, const char* suffix) {
  std::string base = strip_trailing_slashes(trim(api_url));
  if (base.empty()) return std::string();
  if (ends_with(base, suffix)) return base;
  if (ends_with(base, "/v1")) return base + suffix;
  if (!url_has_path(base)) return base + "/v1" + suffix;
  return base + suffix;
}

/// The provider's own text. It ends up in an error event, so it has to be
/// valid UTF-8 - a gateway that re-encodes can put a stray byte in there.
std::string extract_error_message(const nlohmann::json& j) {
  if (!j.is_object()) return std::string();
  auto err = j.find("error");
  if (err != j.end() && !err->is_null()) {
    if (err->is_string()) return sanitize_utf8(err->get<std::string>());
    if (err->is_object()) {
      auto m = err->find("message");
      if (m != err->end() && m->is_string()) return sanitize_utf8(m->get<std::string>());
      return dump_json(*err);
    }
  }
  const char* keys[] = {"message", "detail", "msg"};
  for (std::size_t i = 0; i < sizeof(keys) / sizeof(keys[0]); ++i) {
    auto it = j.find(keys[i]);
    if (it != j.end() && it->is_string()) return sanitize_utf8(it->get<std::string>());
  }
  return std::string();
}

bool retryable_status(int status) {
  return status == 408 || status == 409 || status == 425 || status == 429 || status == 500 ||
         status == 502 || status == 503 || status == 504 || status == 529;
}

/// What kind of failure a request hit. The two retryable kinds are waited out
/// very differently: a hiccup is worth a second, a busy provider is worth
/// minutes (it says so itself: "通常 1-5 分钟内恢复").
enum class FailureKind {
  Fatal,      // the request itself is wrong; trying it again says the same
  Transient,  // a hiccup: a dropped connection, a 502 - retry in a second
  Overloaded  // the provider is busy: HTTP 529 / "overloaded_error"
};

/// Does the failure say "the provider is too busy right now"?
///
/// HTTP 529 is the shape Anthropic-style gateways use for it, and the body
/// carries the reason: "overloaded_error", or (MiniMax) "当前为整点高峰时段，
/// 服务器短暂繁忙". The wording is matched as well as the status, because a
/// gateway may report the same thing with a 503.
bool says_overloaded(const ChatResult& result) {
  if (result.http_status == 529) return true;
  const std::string haystack = to_lower(result.error + " " + result.detail);
  if (haystack.find("overload") != std::string::npos) return true;  // overloaded_error
  if (haystack.find("server is busy") != std::string::npos) return true;
  if (haystack.find("繁忙") != std::string::npos) return true;      // 服务器短暂繁忙
  if (haystack.find("高峰") != std::string::npos) return true;      // 整点高峰时段
  return false;
}

FailureKind classify_failure(const ChatResult& result) {
  if (result.http_status == 0) return FailureKind::Transient;  // the request never arrived
  if (!retryable_status(result.http_status)) return FailureKind::Fatal;
  if (says_overloaded(result)) return FailureKind::Overloaded;
  return FailureKind::Transient;
}

/// The wait before the n-th retry of a busy provider: the base doubled every
/// time, capped so a long outage does not turn into an hour of waiting.
int overload_delay_ms(const ChatRequest& req, int attempt) {
  long long delay = req.overload_backoff_ms > 0 ? req.overload_backoff_ms : 60000;
  for (int i = 0; i < attempt; ++i) {
    if (req.overload_max_backoff_ms > 0 && delay >= req.overload_max_backoff_ms) break;
    delay *= 2;
  }
  if (req.overload_max_backoff_ms > 0 && delay > req.overload_max_backoff_ms) {
    delay = req.overload_max_backoff_ms;
  }
  return static_cast<int>(delay);
}

/// Sleep, but wake up as soon as the turn is cancelled - a wait of minutes
/// must not outlive the user's "stop". Returns false when the wait was cut
/// short.
bool sleep_interruptible(int delay_ms, const std::atomic<bool>* cancel) {
  for (int slept = 0; slept < delay_ms; slept += 50) {
    if (cancel && cancel->load()) return false;
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }
  return !(cancel && cancel->load());
}

/// The HTTP status an error object carries, if any: "http_code": 529 or
/// "http_code": "529". Gateways that report the failure as a stream event
/// put it there - without it the failure looks like a plain HTTP 200 and would
/// never be retried.
int error_http_code(const nlohmann::json& error) {
  if (!error.is_object()) return 0;
  auto it = error.find("http_code");
  if (it == error.end()) return 0;
  if (it->is_number()) return it->get<int>();
  if (it->is_string()) {
    const std::string text = trim(it->get<std::string>());
    if (text.empty()) return 0;
    for (std::size_t i = 0; i < text.size(); ++i) {
      if (text[i] < '0' || text[i] > '9') return 0;
    }
    long long value = 0;
    for (std::size_t i = 0; i < text.size(); ++i) value = value * 10 + (text[i] - '0');
    return static_cast<int>(value);
  }
  return 0;
}

/// `Retry-After: 30` - how long the provider wants us to stay away. Only the
/// numeric form is understood; the HTTP-date form is rare and the default
/// wait is close enough.
int parse_retry_after(const std::string& value) {
  const std::string text = trim(value);
  if (text.empty()) return 0;
  for (std::size_t i = 0; i < text.size(); ++i) {
    if (text[i] < '0' || text[i] > '9') return 0;
  }
  long long seconds = 0;
  for (std::size_t i = 0; i < text.size(); ++i) seconds = seconds * 10 + (text[i] - '0');
  if (seconds <= 0) return 0;
  if (seconds > 3600) seconds = 3600;
  return static_cast<int>(seconds * 1000);
}

void parse_usage_into(ChatUsage* usage, const nlohmann::json& u) {
  if (!u.is_object()) return;
  usage->present = true;
  usage->raw = u;
  auto get = [&](const char* key) -> long long {
    auto it = u.find(key);
    if (it == u.end() || !it->is_number()) return 0;
    return it->get<long long>();
  };
  usage->prompt_tokens = get("prompt_tokens");
  usage->completion_tokens = get("completion_tokens");
  usage->total_tokens = get("total_tokens");
  if (usage->total_tokens == 0) usage->total_tokens = usage->prompt_tokens + usage->completion_tokens;

  // Prefix-cache accounting. DeepSeek reports prompt_cache_hit_tokens; the
  // OpenAI shape nests it under prompt_tokens_details.cached_tokens.
  usage->cached_tokens = get("prompt_cache_hit_tokens");
  if (usage->cached_tokens == 0) {
    auto details = u.find("prompt_tokens_details");
    if (details != u.end() && details->is_object()) {
      auto cached = details->find("cached_tokens");
      if (cached != details->end() && cached->is_number()) {
        usage->cached_tokens = cached->get<long long>();
      }
    }
  }
}

/// Accumulates one streamed delta (or a whole non-streamed message object).
struct DeltaAccumulator {
  ChatResult* result;
  const DeltaCallback* on_delta;
  std::map<int, ToolCall> calls;
  std::string finish_reason;
  /// Request start (ms) and whether this is a streamed response. Used to record
  /// time-to-first-token and the generation window, which is what tokens/second
  /// should be measured over.
  const int64_t* started = nullptr;
  bool streaming = false;

  void mark_delta() {
    if (!streaming || !started) return;
    const int64_t now = now_ms();
    if (result->ttft_ms == 0) result->ttft_ms = now - *started;
    result->generation_ms = now - (*started + result->ttft_ms);
  }

  void apply(const nlohmann::json& delta) {
    if (!delta.is_object()) return;

    auto content = delta.find("content");
    if (content != delta.end() && content->is_string()) {
      const std::string text = content->get<std::string>();
      if (!text.empty()) {
        result->content += text;
        mark_delta();
        if (*on_delta) (*on_delta)(DeltaKind::Content, text);
      }
    }

    const char* reasoning_keys[] = {"reasoning_content", "reasoning"};
    for (std::size_t i = 0; i < sizeof(reasoning_keys) / sizeof(reasoning_keys[0]); ++i) {
      auto r = delta.find(reasoning_keys[i]);
      if (r == delta.end() || !r->is_string()) continue;
      const std::string text = r->get<std::string>();
      if (!text.empty()) {
        result->reasoning += text;
        mark_delta();
        if (*on_delta) (*on_delta)(DeltaKind::Reasoning, text);
      }
    }

    auto tool_calls = delta.find("tool_calls");
    if (tool_calls == delta.end() || !tool_calls->is_array()) return;
    int position = 0;
    for (auto it = tool_calls->begin(); it != tool_calls->end(); ++it, ++position) {
      if (!it->is_object()) continue;
      int index = position;
      auto idx = it->find("index");
      if (idx != it->end() && idx->is_number()) index = idx->get<int>();

      ToolCall& call = calls[index];
      auto id = it->find("id");
      if (id != it->end() && id->is_string()) {
        const std::string v = sanitize_utf8(id->get<std::string>());
        if (!v.empty()) call.id = v;
      }
      auto name = it->find("name");
      if (name != it->end() && name->is_string() && call.name.empty()) {
        call.name = sanitize_utf8(name->get<std::string>());
      }
      auto fn = it->find("function");
      if (fn != it->end() && fn->is_object()) {
        auto fname = fn->find("name");
        if (fname != fn->end() && fname->is_string()) {
          const std::string v = sanitize_utf8(fname->get<std::string>());
          if (!v.empty()) call.name = v;
        }
        auto args = fn->find("arguments");
        if (args != fn->end() && args->is_string()) {
          const std::string piece = args->get<std::string>();
          call.raw_arguments += piece;
          // Streamed tool-call arguments are output tokens too. Forwarding them
          // lets a client count them (the Web UI uses this for the live t/s
          // figure); they are never rendered as message content.
          if (!piece.empty()) {
            mark_delta();
            if (*on_delta) (*on_delta)(DeltaKind::ToolArgs, piece);
          }
        }
      }
    }
  }
};

ChatResult attempt_once(const ChatRequest& req, const std::string& url, const std::string& payload,
                        const DeltaCallback& on_delta, const std::atomic<bool>* cancel) {
  ChatResult result;
  const int64_t started = now_ms();

  HttpRequest http;
  http.method = "POST";
  http.url = url;
  http.body = payload;
  http.content_type = "application/json";
  http.connect_timeout_ms = req.connect_timeout_ms;
  http.read_timeout_ms = req.read_timeout_ms;
  http.body_limit = req.stream ? 65536u : 0u;
  http.headers.push_back(HttpHeader{"Authorization", "Bearer " + req.api_key});
  http.headers.push_back(HttpHeader{"Accept", req.stream ? "text/event-stream" : "application/json"});
  http.headers.push_back(HttpHeader{"Accept-Encoding", "identity"});
  for (std::size_t i = 0; i < req.headers.size(); ++i) {
    if (to_lower(req.headers[i].first) == "authorization") continue;
    http.headers.push_back(HttpHeader{req.headers[i].first, req.headers[i].second});
  }

  DeltaAccumulator acc;
  acc.result = &result;
  acc.on_delta = &on_delta;
  acc.started = &started;
  acc.streaming = req.stream;

  std::string sse_buf;
  bool saw_stream_error = false;
  std::string stream_error;
  int stream_error_status = 0;

  HttpChunkCallback on_chunk;
  if (req.stream) {
    on_chunk = [&](const char* data, std::size_t len) -> bool {
      if (cancel && cancel->load()) {
        result.cancelled = true;
        return false;
      }
      sse_buf.append(data, len);
      if (sse_buf.find('\r') != std::string::npos) {
        sse_buf = replace_all(sse_buf, "\r\n", "\n");
      }
      for (;;) {
        const std::size_t pos = sse_buf.find("\n\n");
        if (pos == std::string::npos) break;
        const std::string block = sse_buf.substr(0, pos);
        sse_buf.erase(0, pos + 2);

        std::string event_payload;
        const std::vector<std::string> lines = split_lines(block);
        for (std::size_t i = 0; i < lines.size(); ++i) {
          const std::string& line = lines[i];
          if (!starts_with(line, "data:")) continue;
          std::string piece = line.substr(5);
          if (!piece.empty() && piece.front() == ' ') piece.erase(0, 1);
          if (!event_payload.empty()) event_payload += "\n";
          event_payload += piece;
        }
        if (event_payload.empty() || event_payload == "[DONE]") continue;

        nlohmann::json ev;
        try {
          // The parser refuses bytes that are not valid UTF-8, so a provider
          // that re-encodes (or a gateway in between) would cost us the whole
          // event - the answer would silently lose a piece. Clean the bytes
          // first: one odd byte becomes U+FFFD and the rest of the event
          // survives.
          ev = nlohmann::json::parse(sanitize_utf8(event_payload));
        } catch (const std::exception&) {
          continue;
        }
        auto err = ev.find("error");
        if (err != ev.end() && !err->is_null()) {
          saw_stream_error = true;
          stream_error = extract_error_message(ev);
          if (stream_error.empty()) stream_error = "provider reported an error";
          // The status may sit inside the error object ("http_code": 529).
          stream_error_status = error_http_code(*err);
          return false;
        }
        auto usage = ev.find("usage");
        if (usage != ev.end() && !usage->is_null()) parse_usage_into(&result.usage, *usage);

        auto choices = ev.find("choices");
        if (choices == ev.end() || !choices->is_array() || choices->empty()) continue;
        const nlohmann::json& choice = (*choices)[0];
        auto finish = choice.find("finish_reason");
        if (finish != choice.end() && finish->is_string()) {
          acc.finish_reason = finish->get<std::string>();
        }
        auto delta = choice.find("delta");
        if (delta != choice.end()) acc.apply(*delta);
      }
      return true;
    };
  }

  const HttpResponse res = http_perform(http, on_chunk);
  result.http_status = res.status;
  result.elapsed_ms = now_ms() - started;
  // The provider may say when to come back; honour it instead of guessing.
  result.retry_after_ms = parse_retry_after(res.header("Retry-After"));

  if (result.cancelled) {
    result.error = "cancelled";
    return result;
  }
  if (!res.ok) {
    result.error = res.error.empty() ? "HTTP request failed" : res.error;
    return result;
  }
  if (res.status < 200 || res.status >= 300) {
    result.error = "HTTP " + std::to_string(res.status);
    std::string detail = res.body;
    if (!detail.empty()) {
      try {
        const nlohmann::json j = nlohmann::json::parse(sanitize_utf8(detail));
        const std::string message = extract_error_message(j);
        if (!message.empty()) result.error += ": " + message;
      } catch (const std::exception&) {
        // Not JSON; keep the raw body as detail only.
      }
    }
    result.detail = truncate_utf8(sanitize_utf8(detail), 4096);
    return result;
  }

  if (saw_stream_error) {
    result.error = stream_error;
    result.detail = truncate_utf8(sanitize_utf8(sse_buf), 2048);
    // A gateway that reports the failure inside the stream still knows the
    // status it would have sent ("http_code": 529). Keep it, so the caller
    // can tell a busy provider from a broken request.
    if (stream_error_status > 0) result.http_status = stream_error_status;
    return result;
  }

  if (!req.stream) {
    try {
      // Same as above: clean the bytes before the parser sees them.
      const nlohmann::json j = nlohmann::json::parse(sanitize_utf8(res.body));
      auto choices = j.find("choices");
      if (choices != j.end() && choices->is_array() && !choices->empty()) {
        const nlohmann::json& choice = (*choices)[0];
        auto message = choice.find("message");
        if (message != choice.end()) acc.apply(*message);
        auto finish = choice.find("finish_reason");
        if (finish != choice.end() && finish->is_string()) {
          acc.finish_reason = finish->get<std::string>();
        }
      }
      auto usage = j.find("usage");
      if (usage != j.end() && !usage->is_null()) parse_usage_into(&result.usage, *usage);
    } catch (const std::exception& e) {
      result.error = std::string("cannot parse provider response: ") + e.what();
      result.detail = truncate_utf8(sanitize_utf8(res.body), 4096);
      return result;
    }
  } else if (!sse_buf.empty()) {
    // Some servers omit the final blank line; salvage a trailing JSON event.
    const std::string trimmed = trim(sse_buf);
    if (starts_with(trimmed, "data:")) {
      const std::string piece = trim(trimmed.substr(5));
      if (!piece.empty() && piece != "[DONE]") {
        try {
          const nlohmann::json ev = nlohmann::json::parse(piece);
          auto choices = ev.find("choices");
          if (choices != ev.end() && choices->is_array() && !choices->empty()) {
            auto delta = (*choices)[0].find("delta");
            if (delta != (*choices)[0].end()) acc.apply(*delta);
          }
        } catch (const std::exception&) {
        }
      }
    }
  }

  // The provider's own bytes are not guaranteed to be UTF-8 (a gateway that
  // re-encodes, a model that emits a raw byte inside a string). The JSON
  // parser accepts them, the serialiser does not - so they are cleaned here,
  // before they reach a document that has to be serialised.
  result.content = sanitize_utf8(result.content);
  result.reasoning = sanitize_utf8(result.reasoning);

  for (auto it = acc.calls.begin(); it != acc.calls.end(); ++it) {
    ToolCall call = it->second;
    if (call.id.empty()) call.id = "call_" + std::to_string(it->first + 1);
    if (call.name.empty()) call.name = "unknown";
    if (call.raw_arguments.empty()) call.raw_arguments = "{}";
    call.raw_arguments = sanitize_utf8(call.raw_arguments);
    try {
      call.arguments = nlohmann::json::parse(call.raw_arguments);
    } catch (const std::exception&) {
      call.arguments = nlohmann::json::object();
    }
    result.tool_calls.push_back(call);
  }
  result.finish_reason = acc.finish_reason;
  result.ok = true;
  return result;
}

}  // namespace

std::string chat_completions_url(const std::string& api_url) {
  return endpoint(api_url, "/chat/completions");
}

std::string models_url(const std::string& api_url) { return endpoint(api_url, "/models"); }

void apply_default_knobs(ChatRequest* req, const Settings& st, const ModelInfo* model) {
  if (!req) return;
  // max_tokens: the machine setting wins, the model's own output limit is
  // the fallback. Zero means "the provider decides".
  if (req->max_tokens <= 0) {
    if (st.max_tokens > 0) {
      req->max_tokens = st.max_tokens;
    } else if (model && model->max_output_tokens > 0) {
      req->max_tokens = model->max_output_tokens;
    }
  }
  // The sampling knobs: the global setting wins, the model value is the
  // fallback. A knob that was never set stays out of the request - some
  // gateways answer 400 for a field they do not know.
  if (st.temperature >= 0) {
    req->has_temperature = true;
    req->temperature = st.temperature;
  } else if (model && model->has_temperature) {
    req->has_temperature = true;
    req->temperature = model->temperature;
  }
  if (st.top_p >= 0) {
    req->has_top_p = true;
    req->top_p = st.top_p;
  } else if (model && model->has_top_p) {
    req->has_top_p = true;
    req->top_p = model->top_p;
  }
  if (st.top_k > 0) {
    req->has_top_k = true;
    req->top_k = st.top_k;
  } else if (model && model->has_top_k) {
    req->has_top_k = true;
    req->top_k = model->top_k;
  }
  if (st.frequency_penalty >= 0) {
    req->has_frequency_penalty = true;
    req->frequency_penalty = st.frequency_penalty;
  } else if (model && model->has_frequency_penalty) {
    req->has_frequency_penalty = true;
    req->frequency_penalty = model->frequency_penalty;
  }
  if (st.presence_penalty >= 0) {
    req->has_presence_penalty = true;
    req->presence_penalty = st.presence_penalty;
  } else if (model && model->has_presence_penalty) {
    req->has_presence_penalty = true;
    req->presence_penalty = model->presence_penalty;
  }
  if (st.seed != 0) {
    req->has_seed = true;
    req->seed = st.seed;
  } else if (model && model->has_seed) {
    req->has_seed = true;
    req->seed = model->seed;
  }
  // How hard the model should think. The model's own value wins; "auto" (and
  // the empty string) fall back to the machine setting, and "auto" there as
  // well means "say nothing" - a gateway that does not know `reasoning_effort`
  // answers 400 when it sees the field.
  std::string effort = model ? model->reasoning_effort : std::string();
  if (effort.empty() || effort == "auto") effort = st.reasoning_effort;
  req->reasoning_effort = effort;
  req->send_reasoning_effort = false;
  if (effort != "auto" && !effort.empty()) {
    // A model that does not declare reasoning support is not asked to think:
    // the field would be rejected by the gateway. The settings page ties the
    // effort field to the same checkbox, so this can only happen in a
    // hand-edited file.
    if (!model || model->supports_reasoning) {
      req->send_reasoning_effort = true;
    }
  }
}

bool apply_knob_overrides(ChatRequest* req, const nlohmann::json& body,
                          std::string* err) {
  if (!req || !body.is_object()) return true;
  auto fail = [&](const std::string& msg) {
    if (err) *err = msg;
    return false;
  };
  // A number knob: a number sets it, null clears it (the field leaves the
  // request), anything else is a mistake worth reporting - a typo that is
  // silently ignored is how "I set temperature and it did nothing" happens.
  auto num_knob = [&](const char* key, bool* has, double* value, double lo,
                       double hi, const char* range) -> bool {
    if (!body.contains(key)) return true;
    const nlohmann::json& v = body[key];
    if (v.is_null()) {
      *has = false;
      return true;
    }
    if (!v.is_number()) {
      return fail(std::string("'") + key + "' must be a number");
    }
    const double d = v.get<double>();
    if (d < lo || d > hi) {
      return fail(std::string("'") + key + "' must be between " + range);
    }
    *has = true;
    *value = d;
    return true;
  };
  if (!num_knob("temperature", &req->has_temperature, &req->temperature, 0.0, 2.0,
                "0 and 2")) return false;
  if (!num_knob("top_p", &req->has_top_p, &req->top_p, 0.0, 1.0, "0 and 1")) return false;
  if (!num_knob("frequency_penalty", &req->has_frequency_penalty,
                &req->frequency_penalty, -2.0, 2.0, "-2 and 2")) return false;
  if (!num_knob("presence_penalty", &req->has_presence_penalty,
                &req->presence_penalty, -2.0, 2.0, "-2 and 2")) return false;

  // top_k: 0 would mean "no token at all", so it is the same as "not set".
  if (body.contains("top_k")) {
    const nlohmann::json& v = body["top_k"];
    if (v.is_null()) {
      req->has_top_k = false;
      req->top_k = 0;
    } else if (!v.is_number_integer()) {
      return fail("'top_k' must be a whole number");
    } else {
      const int64_t k = v.get<int64_t>();
      if (k < 0 || k > 1000000) return fail("'top_k' must be between 0 and 1000000");
      req->has_top_k = k > 0;
      req->top_k = static_cast<int>(k);
    }
  }
  // seed: 0 is a real seed (a deterministic call), so only null means "unset".
  if (body.contains("seed")) {
    const nlohmann::json& v = body["seed"];
    if (v.is_null()) {
      req->has_seed = false;
      req->seed = 0;
    } else if (!v.is_number_integer()) {
      return fail("'seed' must be a whole number");
    } else {
      const int64_t sd = v.get<int64_t>();
      if (sd < 0 || sd > 2147483647LL) {
        return fail("'seed' must be between 0 and 2147483647");
      }
      req->has_seed = true;
      req->seed = sd;
    }
  }
  if (body.contains("max_tokens")) {
    const nlohmann::json& v = body["max_tokens"];
    if (v.is_null()) {
      req->max_tokens = 0;
    } else if (!v.is_number_integer()) {
      return fail("'max_tokens' must be a whole number");
    } else {
      const int64_t mt = v.get<int64_t>();
      if (mt < 0 || mt > 100000000LL) {
        return fail("'max_tokens' must be between 0 and 100000000");
      }
      req->max_tokens = mt;
    }
  }
  // The thinking level. "auto" (and null) mean "do not send the field"; any
  // other value is sent as it is - the caller knows their gateway, and "off"
  // is exactly what a caller who wants no thinking asks for.
  if (body.contains("reasoning_effort")) {
    const nlohmann::json& v = body["reasoning_effort"];
    if (v.is_null()) {
      req->reasoning_effort = "auto";
      req->send_reasoning_effort = false;
    } else if (!v.is_string()) {
      return fail("'reasoning_effort' must be a string");
    } else {
      const std::string effort = trim(v.get<std::string>());
      if (effort.empty() || effort == "auto") {
        req->reasoning_effort = "auto";
        req->send_reasoning_effort = false;
      } else {
        req->reasoning_effort = effort;
        req->send_reasoning_effort = true;
        // The value goes on the wire unchanged, so a gateway-specific spelling
        // works. A typo does not: it would be ignored and the model would keep
        // thinking. Say so in the log - the response tells the caller what was
        // sent, the log tells the operator it looks wrong.
        bool known = false;
        for (int i = 0; i < kEffortCount; ++i) {
          if (effort == kEfforts[i]) { known = true; break; }
        }
        if (!known) {
          LOG_WARN("llm: unusual reasoning_effort '", effort,
                   "' (known: auto/off/minimal/low/medium/high/xhigh/max) - "
                   "it goes on the wire unchanged");
        }
      }
    }
  }
  return true;
}

nlohmann::json knobs_to_json(const ChatRequest& req) {
  nlohmann::json j = nlohmann::json::object();
  // "auto" means the field was not sent; the flag says so explicitly, because
  // "the gateway decided" and "we asked for off" are different things.
  j["reasoning_effort"] = req.send_reasoning_effort ? req.reasoning_effort : "auto";
  j["reasoning_effort_sent"] = req.send_reasoning_effort;
  j["temperature"] = req.has_temperature ? nlohmann::json(req.temperature)
                                          : nlohmann::json(nullptr);
  j["top_p"] = req.has_top_p ? nlohmann::json(req.top_p) : nlohmann::json(nullptr);
  j["top_k"] = req.has_top_k ? nlohmann::json(req.top_k) : nlohmann::json(nullptr);
  j["frequency_penalty"] = req.has_frequency_penalty
                               ? nlohmann::json(req.frequency_penalty)
                               : nlohmann::json(nullptr);
  j["presence_penalty"] = req.has_presence_penalty
                               ? nlohmann::json(req.presence_penalty)
                               : nlohmann::json(nullptr);
  j["seed"] = req.has_seed ? nlohmann::json(req.seed) : nlohmann::json(nullptr);
  j["max_tokens"] = req.max_tokens > 0 ? nlohmann::json(req.max_tokens)
                                        : nlohmann::json(nullptr);
  return j;
}

nlohmann::json ChatUsage::to_json() const {
  nlohmann::json j = nlohmann::json::object();
  j["prompt_tokens"] = prompt_tokens;
  j["completion_tokens"] = completion_tokens;
  j["total_tokens"] = total_tokens;
  j["cached_tokens"] = cached_tokens;
  return j;
}

ChatResult llm_chat(const ChatRequest& req, const DeltaCallback& on_delta,
                    const std::atomic<bool>* cancel) {
  ChatResult result;
  const int64_t started = now_ms();
  const std::string url = chat_completions_url(req.api_url);
  if (url.empty()) {
    result.error = "provider api_url is empty";
    return result;
  }

  nlohmann::json body = nlohmann::json::object();
  body["model"] = req.model;
  body["messages"] = req.messages;
  if (!req.tools.empty()) {
    body["tools"] = req.tools;
    body["tool_choice"] = "auto";
  }
  if (req.stream) body["stream"] = true;
  if (req.has_temperature) body["temperature"] = req.temperature;
  if (req.has_top_p) body["top_p"] = req.top_p;
  if (req.has_top_k && req.top_k > 0) body["top_k"] = req.top_k;
  if (req.has_frequency_penalty) body["frequency_penalty"] = req.frequency_penalty;
  if (req.has_presence_penalty) body["presence_penalty"] = req.presence_penalty;
  if (req.has_seed) body["seed"] = req.seed;
  if (req.max_tokens > 0) body["max_tokens"] = req.max_tokens;
  if (req.send_reasoning_effort && !req.reasoning_effort.empty() &&
      req.reasoning_effort != "auto") {
    body["reasoning_effort"] = req.reasoning_effort;
  }
  if (req.stream && req.include_usage) {
    nlohmann::json stream_options = nlohmann::json::object();
    stream_options["include_usage"] = true;
    body["stream_options"] = stream_options;
  }
  const std::string payload = dump_json(body);

  LOG_DEBUG("llm: POST ", url, " model=", req.model, " bytes=", payload.size(),
            " tools=", req.tools.size(), " stream=", req.stream ? 1 : 0);

  // Two budgets, because the two kinds of failure need very different waits:
  // a hiccup is retried in a second or two, a busy provider in minutes.
  const int transient_budget = req.max_retries < 0 ? 0 : req.max_retries;
  int transient_attempts = 0;
  int overload_attempts = 0;
  long long overload_waited_ms = 0;

  for (int attempt = 0;; ++attempt) {
    if (cancel && cancel->load()) {
      result.cancelled = true;
      result.error = "cancelled";
      result.elapsed_ms = now_ms() - started;
      return result;
    }
    result = attempt_once(req, url, payload, on_delta, cancel);
    result.elapsed_ms = now_ms() - started;

    if (result.ok || result.cancelled) break;

    // A request that already produced output is not repeated: the provider
    // would answer twice, and the answer would be pasted twice.
    const bool produced_output =
        !result.content.empty() || !result.reasoning.empty() || !result.tool_calls.empty();
    if (produced_output) break;

    const FailureKind kind = classify_failure(result);
    if (kind == FailureKind::Fatal) break;

    int delay_ms = 0;
    int budget = 0;
    int used = 0;
    if (kind == FailureKind::Overloaded) {
      budget = req.overload_retries < 0 ? 0 : req.overload_retries;
      if (overload_attempts >= budget) break;
      delay_ms = overload_delay_ms(req, overload_attempts);
      // `Retry-After` may only make the wait longer, never shorter: the
      // provider's own "come back in 90 seconds" is worth obeying, but a short
      // one must not cut the wait the provider asked for ("1-5 minutes").
      if (result.retry_after_ms > delay_ms) delay_ms = result.retry_after_ms;
      // The total time we are willing to spend waiting for a busy provider.
      if (req.overload_max_total_ms > 0 &&
          overload_waited_ms + delay_ms > req.overload_max_total_ms) {
        LOG_WARN("llm: the provider is still busy and the wait budget (",
                 req.overload_max_total_ms, "ms) is used up; giving up");
        break;
      }
      ++overload_attempts;
      overload_waited_ms += delay_ms;
      used = overload_attempts;
    } else {
      budget = transient_budget;
      if (transient_attempts >= budget) break;
      delay_ms = 400 * (transient_attempts + 1);
      // The provider may say how long to stay away; that beats our guess.
      if (result.retry_after_ms > delay_ms) delay_ms = result.retry_after_ms;
      ++transient_attempts;
      used = transient_attempts;
    }

    RetryNotice notice;
    notice.kind = (kind == FailureKind::Overloaded) ? "overloaded" : "transient";
    notice.http_status = result.http_status;
    notice.delay_ms = delay_ms;
    notice.attempt = used;
    notice.max_attempts = budget;
    notice.error = result.error;
    if (req.on_retry) req.on_retry(notice);

    LOG_WARN("llm: attempt ", attempt + 1, " failed (", result.error, "); retrying in ", delay_ms,
             "ms (", notice.kind, " ", used, "/", budget, ")");
    if (!sleep_interruptible(delay_ms, cancel)) {
      result.cancelled = true;
      result.error = "cancelled";
      result.elapsed_ms = now_ms() - started;
      return result;
    }
  }

  result.elapsed_ms = now_ms() - started;
  if (result.ok) {
    std::string cache_note;
    if (result.usage.present && result.usage.prompt_tokens > 0) {
      const long long cached = result.usage.cached_tokens;
      const long long prompt = result.usage.prompt_tokens;
      const int pct = static_cast<int>((cached * 100) / (prompt > 0 ? prompt : 1));
      cache_note = " cache=" + std::to_string(cached) + "/" + std::to_string(prompt) + " (" +
                   std::to_string(pct) + "%)";
    }
    LOG_INFO("llm: ", req.provider, "/", req.model, " ok in ", result.elapsed_ms, "ms content=",
             result.content.size(), "b reasoning=", result.reasoning.size(), "b tool_calls=",
             result.tool_calls.size(),
             result.usage.present ? (" tokens=" + std::to_string(result.usage.total_tokens)) : "",
             cache_note);
  } else if (!result.cancelled) {
    // The provider's own words go into the log as well: the status alone
    // ("HTTP 400: invalid params, 400 (2013)") says nothing about the cause,
    // while the body carries the reason and the request id to quote.
    LOG_WARN("llm: ", req.provider, "/", req.model, " failed in ", result.elapsed_ms,
             "ms: ", result.error,
             result.detail.empty() ? "" : (" | the provider said: " + truncate_utf8(result.detail, 400)));
  }
  return result;
}

}  // namespace tea
