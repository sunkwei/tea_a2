// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
#include "recovery.h"

#include "trajectory.h"
#include "util.h"
#include "workspace.h"

namespace tea {
namespace {

/// How much of the user's request is quoted back. Enough to recognise it,
/// short enough to keep the answer small.
constexpr std::size_t kPendingMessageMax = 2000;

/// Copy what the trajectory knows about a turn. Only the small fields: a
/// trajectory line also carries every tool output, and the whole file can be
/// megabytes.
void fill_from_record(const nlohmann::json& record, const std::string& workspace_id,
                      RecoveredTurn* out) {
  out->turn_id = record.value("turn_id", std::string());
  out->session_id = record.value("session_id", std::string());
  out->workspace_id = record.value("workspace_id", std::string());
  if (out->workspace_id.empty()) out->workspace_id = workspace_id;
  out->provider = record.value("provider", std::string());
  out->model = record.value("model", std::string());
  out->started_at = record.value("started_at", std::string());
  out->finished_at = record.value("finished_at", std::string());
  out->partial = record.value("partial", false);
  out->cancelled = record.value("cancelled", false);
  out->error = record.value("error", std::string());
  out->detail = record.value("detail", std::string());
  out->stop_reason = record.value("stop_reason", std::string());
  out->task_id = record.value("task_id", std::string());
  out->iterations = record.value("iterations", 0);
  out->steps = (record.contains("steps") && record["steps"].is_array())
                   ? static_cast<int>(record["steps"].size())
                   : 0;
  if (record.contains("usage") && record["usage"].is_object()) out->usage = record["usage"];
  if (record.contains("files_changed") && record["files_changed"].is_array()) {
    out->files_changed = record["files_changed"];
  }
}

/// The state of a turn, as the record describes it.
std::string state_of_record(const nlohmann::json& record) {
  if (record.value("partial", false)) return "interrupted";
  if (record.value("cancelled", false)) return "stopped";
  if (!record.value("ok", false)) return "failed";
  const std::string reason = record.value("stop_reason", std::string());
  if (reason == "budget") return "budget";
  if (reason == "repeated_output") return "stopped";
  return "done";
}

std::string clip(const std::string& text, std::size_t max) {
  if (text.size() <= max) return text;
  return text.substr(0, max) + "…";
}

}  // namespace

nlohmann::json RecoveredTurn::to_json() const {
  nlohmann::json j = nlohmann::json::object();
  j["turn_id"] = turn_id;
  j["session_id"] = session_id;
  j["workspace_id"] = workspace_id;
  j["state"] = state;
  // The turn is not running any more: it is what the records say about a turn
  // that is over. `recovered` says where the answer came from.
  j["running"] = false;
  j["recovered"] = true;
  j["partial"] = partial;
  j["cancelled"] = cancelled;
  j["started_at"] = started_at;
  if (!finished_at.empty()) j["finished_at"] = finished_at;
  j["provider"] = provider;
  j["model"] = model;
  j["iterations"] = iterations;
  j["steps"] = steps;
  j["usage"] = usage;
  j["files_changed"] = files_changed;
  if (!error.empty()) j["error"] = error;
  if (!detail.empty()) j["detail"] = detail;
  if (!stop_reason.empty()) j["stop_reason"] = stop_reason;
  if (!task_id.empty()) j["task_id"] = task_id;
  if (!pending_message.empty()) j["pending_message"] = pending_message;
  j["sources"] = sources;
  return j;
}

bool read_trajectory_states(const std::string& path, const std::string& workspace_id,
                             std::vector<RecoveredTurn>* out, std::string* err) {
  out->clear();
  std::vector<nlohmann::json> records;
  if (!load_trajectories(path, &records, err)) return false;
  // The records come newest first; the list is handed back oldest first, so
  // "the last one" is "the newest one".
  for (std::size_t i = records.size(); i-- > 0;) {
    RecoveredTurn turn;
    fill_from_record(records[i], workspace_id, &turn);
    turn.state = state_of_record(records[i]);
    turn.sources.push_back("trajectory");
    out->push_back(turn);
  }
  return true;
}

bool read_transcript_state(const Workspace& workspace, const std::string& session_id,
                           TranscriptState* out, std::string* err) {
  *out = TranscriptState();
  Session session;
  if (!workspace.load_session(session_id, &session, err)) return false;
  out->message_count = session.messages.size();
  for (std::size_t i = session.messages.size(); i-- > 0;) {
    if (session.messages[i].role != "user") continue;
    out->last_user_message = clip(session.messages[i].content, kPendingMessageMax);
    break;
  }
  if (!session.messages.empty()) {
    // The transcript ends with a request nobody answered.
    out->awaits_answer = session.messages.back().role == "user";
  }
  return true;
}

bool recover_session(const Workspace& workspace, const std::string& session_id,
                     RecoveredTurn* out, std::string* err) {
  *out = RecoveredTurn();
  out->session_id = session_id;
  out->workspace_id = workspace.id();

  // The trajectory: the newest turn of the session.
  const std::string path = trajectory_file_path(workspace.run_dir(), session_id);
  std::vector<nlohmann::json> records;
  std::string ignored;
  bool have_newest = false;
  nlohmann::json newest;
  if (load_trajectories(path, &records, &ignored) && !records.empty()) {
    newest = records[0];
    have_newest = true;
  }

  // The transcript: what was asked, and whether it got an answer.
  TranscriptState transcript;
  const bool have_transcript =
      read_transcript_state(workspace, session_id, &transcript, &ignored);

  bool recovered = false;
  const bool newest_cancelled = have_newest && newest.value("cancelled", false);
  if (have_newest) {
    const bool partial = newest.value("partial", false);
    const bool ok = newest.value("ok", false);
    if (partial) {
      // The turn never wrote its final line: the process went away while it
      // was working.
      fill_from_record(newest, workspace.id(), out);
      out->state = "interrupted";
      out->sources.push_back("trajectory");
      recovered = true;
    } else if (!ok && !newest_cancelled) {
      // The turn gave up - the provider, most of the time.
      fill_from_record(newest, workspace.id(), out);
      out->state = "failed";
      out->sources.push_back("trajectory");
      recovered = true;
    }
  }

  // The transcript knows something the trajectory does not: a turn that was
  // cut off before it wrote a single line still left the request behind,
  // unanswered.
  if (have_transcript && transcript.awaits_answer && !newest_cancelled) {
    if (!recovered) {
      // There is no trajectory for the turn that was cut off (it never got
      // to write one) - the transcript is all there is.
      *out = RecoveredTurn();
      out->session_id = session_id;
      out->workspace_id = workspace.id();
      out->state = "interrupted";
      out->sources.push_back("transcript");
      recovered = true;
    } else {
      out->sources.push_back("transcript");
    }
    out->pending_message = transcript.last_user_message;
  }

  return recovered;
}

}  // namespace tea
