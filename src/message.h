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

/// What `repair_tool_calls` had to change. The agent logs it: a history that
/// had to be fixed is worth a warning, because it means something upstream (an
/// interrupted turn, a provider that reused a tool-call id) left the
/// conversation in a shape that no provider accepts.
struct ToolCallRepair {
  std::size_t orphan_results = 0;     // a tool result without a waiting call
  std::size_t duplicate_results = 0;  // a second answer to the same call
  std::size_t duplicate_calls = 0;    // the same call id declared twice
  std::size_t unanswered_calls = 0;   // a call that never got an answer
  std::size_t nameless_calls = 0;     // a call without a function name
  std::size_t idless_calls = 0;       // a call without an id
  bool changed() const;
  /// "2 duplicate tool results, 1 unanswered tool call" - for the log.
  std::string summary() const;
};

/// Make the tool-call structure of a wire message list acceptable to a
/// provider.
///
/// A provider maps every tool result back to the call it answers, and the
/// mapping has to be one-to-one. MiniMax rejects the *whole* request when it
/// is not:
///     HTTP 400 {"error":{"message":"invalid params, 400 (2013)"}}
/// (verified 2026-10-23 against api.minimax.cn) - an error that says nothing
/// about the cause, so the turn dies with a mystery. The other providers word
/// the same problem differently ("tool call id is invalid", "tool result's
/// tool id(x) not found", "tool call and result not match"), but they reject
/// the request as well.
///
/// The rules the repair enforces:
///   * a tool call has an id and a function name;
///   * an id is declared at most once;
///   * every tool result answers a call that is still waiting for an answer;
///   * a call that never got an answer is dropped from its assistant message.
///
/// Nothing else is touched: text, reasoning, images and the order of the
/// messages stay as they are.
ToolCallRepair repair_tool_calls(nlohmann::json* messages);

/// One entry of a session's task list. A complex job is decomposed into steps,
/// the agent keeps this list current with `toolkit_todo`, and the same list is
/// what the Web UI (and the exported HTML) shows as progress - so "where is it
/// now?" is answered by looking, not by reading the transcript.
struct TodoItem {
  std::string id;    // stable within a session ("t1", "t2", ...)
  std::string text;  // the step, short enough to read at a glance
  std::string status;  // "pending" | "active" | "done"

  static bool valid_status(const std::string& s);
  nlohmann::json to_json() const;
  static TodoItem from_json(const nlohmann::json& j);
};

struct SessionInfo {
  std::string id;
  std::string title;
  std::string created_at;
  std::string updated_at;
  int message_count = 0;
  /// Progress of the session's task list. Kept in the index (not recomputed by
  /// the sidebar) so the session list can show "3/7" without loading every
  /// session file.
  int todo_total = 0;
  int todo_done = 0;
  /// Lineage of a forked session: the id it was copied from and the 1-based
  /// user-turn ordinal in that parent. Empty / 0 for a root session. Stored in
  /// the session file and in the index, so a branch of a branch keeps it and
  /// the sidebar can say where the branch came from.
  std::string forked_from;
  int fork_turn = 0;

  nlohmann::json to_json() const;
  static SessionInfo from_json(const nlohmann::json& j);
};

/// A file the session produced (a document the agent wrote, a chart, a video a
/// command rendered, an image it loaded). The Web UI offers these as downloads
/// next to the final answer, and the exported HTML lists them - so "where did
/// that report go?" is answered by looking.
struct SessionArtifact {
  std::string path;    // workspace-relative (served by /download and /raw)
  long long bytes = 0;
  std::string kind;    // image | video | audio | document | data | other
  std::string tool;    // which tool produced it ("toolkit_file", "toolkit_exec", ...)
  std::string created_at;
  /// 1-based user turn it was produced in, so the UI can show "this turn's
  /// output" under the final message. 0 = unknown (e.g. imported).
  int turn = 0;

  nlohmann::json to_json() const;
  static SessionArtifact from_json(const nlohmann::json& j);
};

/// Map a file name to one of the kinds above (by extension).
std::string artifact_kind_for(const std::string& path);
/// True when the file is build noise rather than a result: object files,
/// dependency stamps, caches. Those are excluded from "本轮产出" because a list
/// containing them is noise, not an answer.
///
/// Everything else counts - including a .c or .cpp the agent wrote, which an
/// allow-list of "interesting" extensions would have dropped.
bool artifact_is_build_noise(const std::string& path);

struct Session {
  SessionInfo info;
  std::vector<Message> messages;
  /// Rolling summary of the oldest `summary_upto` messages. Compaction never
  /// deletes the originals (the UI and the audit trail still see them); it only
  /// keeps them out of the model's context.
  std::string summary;
  std::size_t summary_upto = 0;
  /// The task list for this conversation: what the user asked for, broken into
  /// steps, with their progress. Persisted with the session, injected into the
  /// system prompt (so a compacted or restarted turn still knows the plan) and
  /// pushed to clients on every change.
  std::vector<TodoItem> todos;
  /// Files produced during the conversation, newest last. Deduplicated by path
  /// (a file rewritten three times appears once, with its latest size).
  std::vector<SessionArtifact> artifacts;

};

/// "3/7" style progress for a task list.
void todo_counts(const std::vector<TodoItem>& todos, int* total, int* done);
/// True when the list has steps and every one of them is done - a finished
/// plan. Such a list is not carried into the next turn: it is dead weight in
/// the prompt and a stale panel in the UI.
bool todos_finished(const std::vector<TodoItem>& todos);

}  // namespace tea
