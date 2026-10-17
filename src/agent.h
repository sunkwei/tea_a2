// agent.h -- the multi-turn tool-calling loop.
#pragma once

#include <atomic>
#include <functional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "config.h"
#include "llm.h"
#include "message.h"
#include "tools.h"
#include "workspace.h"

namespace tea {

/// Receives streaming events; must be safe to call from the agent thread.
using EventEmitter = std::function<void(const nlohmann::json&)>;

struct TurnOptions {
  std::string provider;  // empty => config default
  std::string model;     // empty => config default
  bool stream = true;
};

/// How much of the stored history actually made it into the model context.
struct ContextStats {
  std::size_t kept = 0;
  std::size_t total = 0;
  long long budget = 0;
  long long used = 0;
  bool trimmed = false;
};

struct TurnResult {
  bool ok = false;
  bool cancelled = false;
  bool hit_iteration_limit = false;
  int iterations = 0;
  std::string content;
  std::string provider;
  std::string model;
  std::string error;
  std::string detail;
  ChatUsage usage;
  long long elapsed_ms = 0;
};

class Agent {
 public:
  Agent(AppConfig* config, ToolRegistry* tools) : config_(config), tools_(tools) {}

  TurnResult run_turn(Workspace& workspace, Session* session, const std::string& user_text,
                      const TurnOptions& options, const EventEmitter& emit,
                      std::atomic<bool>* cancel);

  /// Rough token estimate used for history trimming.
  static long long estimate_tokens(const std::string& text);
  static long long estimate_tokens(const std::vector<Message>& messages);

  /// Fold the oldest part of the history into a rolling summary when the context
  /// is about to overflow. Never deletes messages: only advances
  /// session->summary_upto so they stop being sent.
  bool maybe_compact(Workspace& workspace, Session* session, const std::string& provider,
                     const std::string& model, const ModelInfo* model_info,
                     std::atomic<bool>* cancel, const EventEmitter& emit);

  /// Exposed so the HTTP layer can show exactly what the model receives.
  std::string build_system_prompt(const Workspace& workspace, const Session& session) const;

 private:
  void build_context(const Workspace& workspace, const Session& session, const ModelInfo* model,
                     std::vector<Message>* out, ContextStats* stats = nullptr) const;
  bool compact_history(Workspace& workspace, Session* session, const std::string& provider,
                       const std::string& model, const ModelInfo* model_info,
                       std::atomic<bool>* cancel);

  AppConfig* config_;
  ToolRegistry* tools_;
};

}  // namespace tea
