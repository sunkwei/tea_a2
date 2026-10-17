// message.h -- conversation model shared by the agent loop, the session store
// and the HTTP API.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace tea {

struct ToolCall {
  std::string id;
  std::string name;
  nlohmann::json arguments = nlohmann::json::object();
  std::string raw_arguments;  // exactly what the model emitted (may be invalid JSON)
};

struct Message {
  std::string role;  // "system" | "user" | "assistant" | "tool"
  std::string content;
  std::string reasoning;
  std::vector<ToolCall> tool_calls;
  std::string tool_call_id;
  std::string name;
  bool ok = true;
  long long elapsed_ms = 0;
  bool truncated = false;
  std::string created_at;

  /// OpenAI wire format (what we POST to the provider).
  /// `include_reasoning` echoes `reasoning_content` back on assistant turns.
  /// "Thinking mode" gateways (vLLM/SGLang/GLM/Kimi/opencode...) require it on
  /// the follow-up request; api.deepseek.com rejects it outright.
  nlohmann::json to_wire(bool include_reasoning = false) const;
  /// Storage format: a superset that also keeps reasoning/timing metadata.
  nlohmann::json to_store() const;
  static Message from_store(const nlohmann::json& j);
  static Message make(const std::string& role, const std::string& content);
};

struct SessionInfo {
  std::string id;
  std::string title;
  std::string created_at;
  std::string updated_at;
  int message_count = 0;

  nlohmann::json to_json() const;
  static SessionInfo from_json(const nlohmann::json& j);
};

struct Session {
  SessionInfo info;
  std::vector<Message> messages;
  /// Rolling summary of the oldest `summary_upto` messages. Compaction never
  /// deletes the originals (the UI and the audit trail still see them); it only
  /// keeps them out of the model's context.
  std::string summary;
  std::size_t summary_upto = 0;
};

}  // namespace tea
