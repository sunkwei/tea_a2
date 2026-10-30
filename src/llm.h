// llm.h -- OpenAI-compatible chat completions client with streaming and tool
// calling.
#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "config.h"
#include "message.h"

namespace tea {

struct ChatUsage {
  bool present = false;
  long long prompt_tokens = 0;
  long long completion_tokens = 0;
  long long total_tokens = 0;
  /// Prompt tokens served from the provider's prefix cache. Lets us actually
  /// measure whether the system prompt and message history stay cacheable.
  long long cached_tokens = 0;
  nlohmann::json raw = nlohmann::json::object();
  nlohmann::json to_json() const;
};

/// Told before every retry wait, so the caller can show what is going on
/// instead of leaving the user in front of a frozen screen.
struct RetryNotice {
  /// "transient" (a hiccup, retried in seconds) or "overloaded" (the provider
  /// is busy - HTTP 529 - and says it recovers in minutes).
  std::string kind;
  int http_status = 0;
  /// How long the wait before this retry is.
  int delay_ms = 0;
  /// Which retry this is (1-based) and how many this kind of failure gets.
  int attempt = 0;
  int max_attempts = 0;
  /// The provider's own words about the failure.
  std::string error;
};
using RetryCallback = std::function<void(const RetryNotice&)>;

struct ChatRequest {
  std::string provider;
  std::string model;
  std::string api_url;  // provider base URL
  std::string api_key;
  nlohmann::json messages = nlohmann::json::array();
  nlohmann::json tools = nlohmann::json::array();
  /// Extra headers from the provider config (already substituted).
  std::vector<std::pair<std::string, std::string> > headers;
  bool stream = true;
  bool include_usage = false;
  bool send_reasoning_effort = false;
  bool has_temperature = false;
  double temperature = 0.7;
  bool has_top_p = false;
  double top_p = 0.9;
  /// top_k / frequency_penalty / presence_penalty / seed. Only a few
  /// gateways take them, so each one is sent only when it was set - a
  /// gateway that does not know the field answers 400.
  bool has_top_k = false;
  int top_k = 0;
  bool has_frequency_penalty = false;
  double frequency_penalty = 0.0;
  bool has_presence_penalty = false;
  double presence_penalty = 0.0;
  bool has_seed = false;
  int64_t seed = 0;
  long long max_tokens = 0;
  std::string reasoning_effort;
  int connect_timeout_ms = 30000;
  int read_timeout_ms = 600000;
  /// Retries for a hiccup (a dropped connection, a 502). They are cheap and
  /// come quickly.
  int max_retries = 2;
  /// Retries for "the provider is busy" (HTTP 529 and friends). Those waits
  /// are minutes long - the provider itself says it recovers in 1-5 minutes -
  /// so they have their own budget, separate from the hiccup retries.
  int overload_retries = 3;
  /// The first wait after a busy answer; every further one doubles it.
  int overload_backoff_ms = 60000;
  /// The cap for a single wait.
  int overload_max_backoff_ms = 300000;
  /// The cap for all the waits of one request together. 0 => no cap.
  long long overload_max_total_ms = 600000;
  /// Told about every retry before the wait starts.
  RetryCallback on_retry;
};

enum class DeltaKind { Content, Reasoning, ToolArgs };
using DeltaCallback = std::function<void(DeltaKind kind, const std::string& text)>;

struct ChatResult {
  bool ok = false;
  bool cancelled = false;
  int http_status = 0;
  std::string content;
  std::string reasoning;
  std::vector<ToolCall> tool_calls;
  std::string finish_reason;
  ChatUsage usage;
  std::string error;
  std::string detail;
  /// What the provider asked for in `Retry-After` (milliseconds, 0 => none).
  int retry_after_ms = 0;
  long long elapsed_ms = 0;
  /// Time to first streamed delta, and the streaming window that follows it.
  /// `generation_ms` is 0 for a non-streaming response, so callers that want
  /// tokens/second fall back to `elapsed_ms` in that case.
  long long ttft_ms = 0;
  long long generation_ms = 0;
};

/// Turn a provider base URL into the chat-completions endpoint. Handles
/// ".../v1", ".../chat/completions" and bare hosts such as
/// "https://api.deepseek.com".
std::string chat_completions_url(const std::string& api_url);
std::string models_url(const std::string& api_url);

ChatResult llm_chat(const ChatRequest& req, const DeltaCallback& on_delta,
                    const std::atomic<bool>* cancel);

}  // namespace tea
