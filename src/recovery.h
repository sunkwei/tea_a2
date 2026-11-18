// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// recovery.h -- what was cut off, recovered from the durable records.
//
// A turn can end in ways the process does not control:
//
//   * the client drops the SSE stream (a reload, a sleeping laptop, a tunnel
//     that blinks) - the turn keeps working, the client re-attaches to it;
//   * the provider answers with an error - the turn fails and says so;
//   * the machine goes away (a restart, a crash, a power cut) - the turn is
//     gone, and with it everything the process knew about it.
//
// The first two leave a trace right away. The third leaves three: the
// trajectory file (the per-turn audit trail), the session transcript (what was
// said) and the log. This module reads the first two and reports what can be
// recovered - "as much as possible", and never more than the records know.
//
// What it deliberately does NOT do: continue the work. A turn that was cut in
// half may have done half of it, and re-running it is the operator's call.
#pragma once

#include <cstddef>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace tea {

class Workspace;

/// One turn, as the durable records describe it.
///
/// The state is one of:
///   "done"        - the model answered and the turn ended normally;
///   "failed"      - the turn gave up (the provider, the agent);
///   "interrupted" - the turn was cut off while it was working;
///   "stopped"     - the user stopped it (not a failure);
///   "budget"      - a ceiling was reached (not a failure either).
struct RecoveredTurn {
  std::string turn_id;
  std::string session_id;
  std::string workspace_id;
  std::string provider;
  std::string model;
  std::string started_at;
  std::string finished_at;
  std::string state;
  /// The trajectory never got a final line for this turn: the process went
  /// away while it was working.
  bool partial = false;
  /// The user stopped it. Not a failure, and not reported as one.
  bool cancelled = false;
  std::string error;
  std::string detail;
  std::string stop_reason;
  std::string task_id;
  int iterations = 0;
  /// How many steps the turn got through (model calls, tool calls).
  int steps = 0;
  nlohmann::json usage = nlohmann::json::object();
  nlohmann::json files_changed = nlohmann::json::array();
  /// What the user asked for in the turn that never got an answer.
  std::string pending_message;
  /// Where the facts came from: "trajectory" and/or "transcript".
  std::vector<std::string> sources;

  /// True when the turn did not end normally: it was cut off or it failed.
  /// Those are the ones worth recovering; the others are just history.
  bool abnormal() const { return state == "interrupted" || state == "failed"; }

  /// The shape `GET /api/turns/<id>` answers with, so a caller does not have
  /// to care whether the turn is still in memory or was read back from disk.
  nlohmann::json to_json() const;
};

/// Read one trajectory file and report the turns in it, oldest first.
///
/// The state of each turn comes from the record itself: a turn that never got
/// a final line was interrupted (the process went away while it was working),
/// a turn that ended with ok=false failed, a turn the user stopped was
/// stopped.
bool read_trajectory_states(const std::string& path, const std::string& workspace_id,
                             std::vector<RecoveredTurn>* out, std::string* err);

/// What the transcript of a session says about its last turn.
struct TranscriptState {
  /// The last thing the user said (clipped).
  std::string last_user_message;
  /// The transcript ends with a user message: nothing answered it.
  bool awaits_answer = false;
  /// How many messages the transcript holds.
  std::size_t message_count = 0;
};

bool read_transcript_state(const Workspace& workspace, const std::string& session_id,
                           TranscriptState* out, std::string* err);

/// Merge what the trajectory and the transcript say about the last turn of a
/// session.
///
/// False when there is nothing to recover: the last turn ended normally, or
/// the user stopped it. True when the last turn was cut off (the process went
/// away) or failed (the provider gave up) - `out` then says which, and how
/// far it got.
bool recover_session(const Workspace& workspace, const std::string& session_id,
                     RecoveredTurn* out, std::string* err);

}  // namespace tea
