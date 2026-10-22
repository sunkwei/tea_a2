#include "llm.h"

#include <chrono>
#include <map>
#include <thread>

#include "http.h"
#include "log.h"
#include "util.h"

namespace tea {
namespace {

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

std::string extract_error_message(const nlohmann::json& j) {
  if (!j.is_object()) return std::string();
  auto err = j.find("error");
  if (err != j.end() && !err->is_null()) {
    if (err->is_string()) return err->get<std::string>();
    if (err->is_object()) {
      auto m = err->find("message");
      if (m != err->end() && m->is_string()) return m->get<std::string>();
      return err->dump();
    }
  }
  const char* keys[] = {"message", "detail", "msg"};
  for (std::size_t i = 0; i < sizeof(keys) / sizeof(keys[0]); ++i) {
    auto it = j.find(keys[i]);
    if (it != j.end() && it->is_string()) return it->get<std::string>();
  }
  return std::string();
}

bool retryable_status(int status) {
  return status == 408 || status == 409 || status == 425 || status == 429 || status == 500 ||
         status == 502 || status == 503 || status == 504 || status == 529;
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
        const std::string v = id->get<std::string>();
        if (!v.empty()) call.id = v;
      }
      auto name = it->find("name");
      if (name != it->end() && name->is_string() && call.name.empty()) {
        call.name = name->get<std::string>();
      }
      auto fn = it->find("function");
      if (fn != it->end() && fn->is_object()) {
        auto fname = fn->find("name");
        if (fname != fn->end() && fname->is_string()) {
          const std::string v = fname->get<std::string>();
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
          ev = nlohmann::json::parse(event_payload);
        } catch (const std::exception&) {
          continue;
        }
        auto err = ev.find("error");
        if (err != ev.end() && !err->is_null()) {
          saw_stream_error = true;
          stream_error = extract_error_message(ev);
          if (stream_error.empty()) stream_error = "provider reported an error";
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
        const nlohmann::json j = nlohmann::json::parse(detail);
        const std::string message = extract_error_message(j);
        if (!message.empty()) result.error += ": " + message;
      } catch (const std::exception&) {
        // Not JSON; keep the raw body as detail only.
      }
    }
    result.detail = truncate_utf8(detail, 4096);
    return result;
  }

  if (saw_stream_error) {
    result.error = stream_error;
    result.detail = truncate_utf8(sse_buf, 2048);
    return result;
  }

  if (!req.stream) {
    try {
      const nlohmann::json j = nlohmann::json::parse(res.body);
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
      result.detail = truncate_utf8(res.body, 4096);
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

  const int max_attempts = req.max_retries < 0 ? 0 : req.max_retries;
  for (int attempt = 0; attempt <= max_attempts; ++attempt) {
    if (cancel && cancel->load()) {
      result.cancelled = true;
      result.error = "cancelled";
      result.elapsed_ms = now_ms() - started;
      return result;
    }
    result = attempt_once(req, url, payload, on_delta, cancel);
    result.elapsed_ms = now_ms() - started;

    if (result.ok || result.cancelled) break;

    const bool produced_output =
        !result.content.empty() || !result.reasoning.empty() || !result.tool_calls.empty();
    const bool retryable = (result.http_status == 0 || retryable_status(result.http_status));
    if (!retryable || produced_output || attempt == max_attempts) break;

    const int delay_ms = 400 * (attempt + 1);
    LOG_WARN("llm: attempt ", attempt + 1, " failed (", result.error, "); retrying in ", delay_ms,
             "ms");
    for (int slept = 0; slept < delay_ms; slept += 50) {
      if (cancel && cancel->load()) break;
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
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
    LOG_WARN("llm: ", req.provider, "/", req.model, " failed in ", result.elapsed_ms,
             "ms: ", result.error);
  }
  return result;
}

}  // namespace tea
