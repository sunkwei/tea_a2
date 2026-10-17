// llm.h -- OpenAI-compatible chat completions client with streaming and tool
// calling.
#pragma once

#include <atomic>
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
  long long max_tokens = 0;
  std::string reasoning_effort;
  int connect_timeout_ms = 30000;
  int read_timeout_ms = 600000;
  int max_retries = 2;
};

enum class DeltaKind { Content, Reasoning };
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
  long long elapsed_ms = 0;
};

/// Turn a provider base URL into the chat-completions endpoint. Handles
/// ".../v1", ".../chat/completions" and bare hosts such as
/// "https://api.deepseek.com".
std::string chat_completions_url(const std::string& api_url);
std::string models_url(const std::string& api_url);

ChatResult llm_chat(const ChatRequest& req, const DeltaCallback& on_delta,
                    const std::atomic<bool>* cancel);

}  // namespace tea
