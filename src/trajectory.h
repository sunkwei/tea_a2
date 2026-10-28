// trajectory.h -- the structured, machine-readable record of ONE agent turn.
//
// A trajectory answers "what did the agent actually do this turn?" without
// replaying the chat transcript: which model iterations ran, which tools were
// called with which arguments and what came back, how much was compacted, and
// which files the tools actually touched. The operator of an embedded device
// that is driven by a larger agent over HTTP needs that audit trail.
//
// Storage: one JSONL file per session,
//
//     <workspace>/.tea_agent_run/trajectories/<session_id>.jsonl
//
// one line per turn. A turn's line is written when the turn starts and rewritten
// in place after every step (same byte offset, then truncate), so a process that
// is killed mid-turn still leaves the last complete snapshot on flash. Readers
// must therefore tolerate a torn trailing line and take the LAST record for a
// given turn_id (see load_trajectories()).
//
// The format is append-only in spirit: lines are ordered oldest -> newest, and
// retention (trajectory_max_turns / trajectory_max_bytes) drops whole lines from
// the head, logging what it pruned.
#pragma once

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "config.h"
#include "llm.h"
#include "message.h"
#include "tools.h"

namespace tea {

class Workspace;

/// Hard bounds applied to a trajectory. Embedded devices have very little flash,
/// so both the per-step body and the whole per-session file are capped.
struct TrajectoryLimits {
  bool enabled = true;
  long long step_max_bytes = 8192;
  int max_turns = 200;
  long long max_bytes = 8ll * 1024ll * 1024ll;
};

/// Read the trajectory knobs from a workspace's effective settings.
TrajectoryLimits trajectory_limits_from_settings(const Settings& settings);

/// `<run_dir>/trajectories/<session_id>.jsonl`.
std::string trajectory_file_path(const std::string& run_dir, const std::string& session_id);

/// True when `id` is usable as a file name (and therefore as a route parameter):
/// [A-Za-z0-9_-]{1,128}. Rejects "." / ".." / separators, which is what keeps the
/// trajectory routes inside the workspace.
bool trajectory_id_is_safe(const std::string& id);

/// Load one JSONL file as turn objects, newest first. Records are de-duplicated
/// by turn_id (last one wins, which is the in-place-updated line), and a torn or
/// otherwise unparsable line is skipped rather than failing the whole file.
bool load_trajectories(const std::string& path, std::vector<nlohmann::json>* out,
                       std::string* err);

/// The compact form returned by `GET /api/sessions/:id/trajectory`:
/// turn_id, started_at, elapsed_ms, ok, iterations, steps (count), usage,
/// files_changed (count), partial.
nlohmann::json trajectory_summary(const nlohmann::json& turn);

/// Records one turn. Not thread safe by itself (it is owned by one turn), but
/// every file write is serialised process-wide so two turns in the same session
/// cannot interleave their rewrites.
class TrajectoryRecorder {
 public:
  TrajectoryRecorder() {}

  /// Start recording. Returns false (and stays inactive) when recording is
  /// disabled in the settings, the session id is unusable, or the directory
  /// cannot be created -- callers keep working either way.
  bool begin(Workspace& workspace, const std::string& session_id,
             const std::string& workspace_id, const std::string& provider,
             const std::string& model);

  bool active() const { return active_; }
  const std::string& turn_id() const { return turn_id_; }
  const std::string& path() const { return path_; }

  /// A compaction folded `covered_messages` messages into a summary of
  /// `summary_bytes` bytes. Recorded from the emitted event, so both the
  /// pre-turn and the overflow-recovery compaction paths are covered.
  void record_compaction(long long covered_messages, long long summary_bytes);

  /// One model iteration: what came back and what it cost.
  void record_model(long long started_ms, const std::string& reasoning, const std::string& content,
                    const std::vector<ToolCall>& tool_calls, const ChatUsage& usage,
                    bool usage_estimated, long long ttft_ms);

  /// One executed tool call, with its raw (unparsed) arguments and the result.
  /// File paths and byte counts are taken from `result.meta`, i.e. only what the
  /// tool actually reported -- nothing is inferred from the arguments.
  void record_tool(const std::string& name, const std::string& raw_arguments,
                   const ToolResult& result, long long elapsed_ms);

  /// Close the turn: stamp finished_at/ok/usage, write the final line, prune.
  void finish(bool ok, const std::string& error, const std::string& detail, int iterations,
              const ChatUsage& usage, bool usage_estimated);

  /// The turn was interrupted (the user stopped it). Not a failure: the record
  /// says so, so the UI does not paint a stopped turn as a broken one.
  void set_cancelled(bool cancelled) { cancelled_ = cancelled; }

  /// The turn ran out of iterations (max_iterations) while the model was still
  /// calling tools: the work was not finished. The record says so, so the UI can
  /// tell "cut short" from "answered".
  void set_iteration_limit(bool hit) { iteration_limit_ = hit; }

  /// The model gave the same output three times in a row and the turn was
  /// stopped: it was going in circles, not working.
  void set_repeated_output(bool hit) { repeated_output_ = hit; }

 private:
  nlohmann::json build() const;
  void flush();
  void prune();

  bool active_ = false;
  Workspace* workspace_ = nullptr;
  TrajectoryLimits limits_;
  std::string path_;
  std::string session_id_;
  std::string turn_id_;
  std::string workspace_id_;
  std::string provider_;
  std::string model_;
  std::string started_at_;
  std::string finished_at_;
  long long started_ms_ = 0;
  int iterations_ = 0;
  bool finished_ = false;
  bool ok_ = false;
  bool cancelled_ = false;
  bool iteration_limit_ = false;
  bool repeated_output_ = false;
  bool usage_estimated_ = false;
  std::string error_;
  std::string detail_;
  ChatUsage usage_;

  nlohmann::json steps_ = nlohmann::json::array();
  nlohmann::json files_changed_ = nlohmann::json::array();

  // Byte range of this turn's line while it is the tail of the file. When the
  // range no longer matches (another writer appended), flush() appends a fresh
  // line instead of clobbering theirs.
  bool range_valid_ = false;
  uint64_t range_offset_ = 0;
  uint64_t range_len_ = 0;
};

}  // namespace tea
