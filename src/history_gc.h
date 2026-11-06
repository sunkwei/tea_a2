// history_gc.h -- what the history cleaner is allowed to do, and what it did.
//
// The history of a workspace (session transcripts + turn trajectories) is the
// one thing in a run_dir that only ever grows. The per-file caps
// (trajectory_max_turns / trajectory_max_bytes) bound a *single* file; they
// say nothing about fifty sessions, and the session transcripts - which are
// bigger than the trajectories - had no cap at all.
//
// So there is a cleaner with three knobs:
//   * max_age_days       - drop what is older than this
//   * max_bytes          - the run_dir may not grow past this
//   * compress_after_days- gzip what is cold (it stays readable)
//
// and it can also be asked to only *report* what it would do (dry run), which
// is the mode a human should use first.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace tea {

struct HistoryGcLimits {
  /// Sessions and trajectories older than this are removed. 0 = keep forever.
  int max_age_days = 0;
  /// The run_dir may not grow past this many bytes. When it does, the oldest
  /// sessions (and their trajectories) go until it fits. 0 = no budget.
  int64_t max_bytes = 0;
  /// Data untouched for this long is gzipped instead of deleted. 0 = never.
  int compress_after_days = 0;
  /// Never touch the newest N sessions: the ones the user is working in right
  /// now are not history yet.
  int keep_recent = 5;
  /// Only report what would happen; change nothing.
  bool dry_run = false;
};

/// The total size of everything under `dir` (files and subdirectories).
int64_t tree_size(const std::string& dir);

struct HistoryGcReport {
  bool dry_run = false;
  int64_t bytes_before = 0;
  int64_t bytes_after = 0;
  int64_t freed_bytes = 0;
  /// How much the compression alone saved (already counted in freed_bytes).
  int64_t compressed_saved = 0;
  int sessions_removed = 0;
  int trajectories_removed = 0;
  int sessions_compressed = 0;
  int trajectories_compressed = 0;
  std::vector<std::string> removed_sessions;
  std::vector<std::string> compressed_sessions;
  /// Human-readable remarks (what was skipped and why).
  std::vector<std::string> notes;

  nlohmann::json to_json() const;
};

}  // namespace tea
