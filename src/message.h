// message.h -- conversation model shared by the agent loop, the session store
// and the HTTP API.
#pragma once

#include <map>
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

/// One image attached to a user message (a pasted screenshot, typically).
/// Only the file reference is stored: sessions stay small and readable, and the
/// bytes live once in the workspace's artifacts directory. `path` is relative to
/// the workspace root, which is also what the /raw route serves.
struct ImageAttachment {
  std::string path;
  std::string mime;  // image/png, image/jpeg, image/webp, image/gif
};

struct Message {
  std::string role;  // "system" | "user" | "assistant" | "tool"
  std::string content;
  /// Vision input for user messages. Empty for every other role.
  std::vector<ImageAttachment> images;
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
  /// When the message has images, `content` becomes the multimodal array
  /// (text part + image_url parts). `image_urls` maps attachment path to a
  /// ready data: URL; the caller builds it once per turn (encoding is not free)
  /// and passes an empty map to drop the images.
  nlohmann::json to_wire(bool include_reasoning = false,
                         const std::map<std::string, std::string>* image_urls = nullptr) const;
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
  /// Lineage of a forked session: the id it was copied from and the 1-based
  /// user-turn ordinal in that parent. Empty / 0 for a root session. Stored in
  /// the session file and in the index, so a branch of a branch keeps it and
  /// the sidebar can say where the branch came from.
  std::string forked_from;
  int fork_turn = 0;

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
