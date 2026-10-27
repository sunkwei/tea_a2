#include "trajectory.h"

#include <algorithm>
#include <cstdio>
#include <exception>
#include <map>

#include "log.h"
#include "util.h"
#include "workspace.h"

namespace tea {
namespace {

/// Never read more than this much of one session file into memory. The default
/// trajectory_max_bytes is 8 MB, so this only matters when an operator raises
/// the cap far beyond what an embedded device should hold.
constexpr size_t kTrajectoryReadCap = 32u * 1024u * 1024u;

/// Serialises every trajectory file write in this process. A turn owns its own
/// recorder, but two turns in the same session (a cancelled turn finishing while
/// the next one starts) must not interleave their in-place rewrites.
std::mutex& trajectory_file_mutex() {
  static std::mutex mu;
  return mu;
}

nlohmann::json usage_object(const ChatUsage& usage, bool estimated) {
  nlohmann::json j = nlohmann::json::object();
  j["prompt_tokens"] = usage.present ? usage.prompt_tokens : 0;
  j["completion_tokens"] = usage.present ? usage.completion_tokens : 0;
  j["total_tokens"] = usage.present ? usage.total_tokens : 0;
  j["cached_tokens"] = usage.present ? usage.cached_tokens : 0;
  // true => the provider sent no usage; these numbers are local estimates.
  j["estimated"] = estimated || !usage.present;
  return j;
}

std::string step_started_at(long long started_ms) {
  return iso8601_from_unix(started_ms / 1000);
}

long long meta_int(const nlohmann::json& meta, const char* key, bool* found = nullptr) {
  if (found) *found = false;
  if (!meta.is_object()) return 0;
  auto it = meta.find(key);
  if (it == meta.end() || !it->is_number_integer()) return 0;
  if (found) *found = true;
  return it->get<long long>();
}

const std::string kOutputDroppedMarker = "\n... [trajectory: tool output dropped to stay within "
                                         "trajectory_step_max_bytes]";

}  // namespace

TrajectoryLimits trajectory_limits_from_settings(const Settings& settings) {
  TrajectoryLimits limits;
  limits.enabled = settings.enable_trajectory;
  limits.step_max_bytes = settings.trajectory_step_max_bytes;
  limits.max_turns = settings.trajectory_max_turns;
  limits.max_bytes = settings.trajectory_max_bytes;
  // Clamp defensively: a negative or nonsensical value must not delete the turn
  // that was just written, and one step must not be able to fill the flash.
  if (limits.step_max_bytes < 0) limits.step_max_bytes = 0;
  if (limits.step_max_bytes > 1024ll * 1024ll) limits.step_max_bytes = 1024ll * 1024ll;
  if (limits.max_turns < 1) limits.max_turns = 1;
  if (limits.max_bytes < 1024) limits.max_bytes = 1024;
  return limits;
}

std::string trajectory_file_path(const std::string& run_dir, const std::string& session_id) {
  return path_join(path_join(run_dir, "trajectories"), session_id + ".jsonl");
}

bool trajectory_id_is_safe(const std::string& id) {
  if (id.empty() || id.size() > 128) return false;
  for (std::size_t i = 0; i < id.size(); ++i) {
    const char c = id[i];
    const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
                    c == '_' || c == '-';
    if (!ok) return false;
  }
  return true;
}

bool load_trajectories(const std::string& path, std::vector<nlohmann::json>* out,
                       std::string* err) {
  // Take the writer's lock: the recorder refreshes its newest line *in place* (to
  // keep the file append-only instead of rewriting it every step), so a reader
  // that raced it would see a half-written last record. Readers are HTTP requests
  // and writers are turns, so the wait is a file read long.
  std::lock_guard<std::mutex> lock(trajectory_file_mutex());
  out->clear();
  if (!path_is_file(path)) {
    if (err) *err = "trajectory file not found: " + path;
    return false;
  }
  std::string data;
  const uint64_t size = file_size(path);
  if (size > kTrajectoryReadCap) {
    if (!read_file_tail(path, kTrajectoryReadCap, &data)) {
      if (err) *err = "cannot read " + path;
      return false;
    }
    // The tail starts mid-line; drop it so parsing begins at a record boundary.
    const std::size_t nl = data.find('\n');
    if (nl == std::string::npos) data.clear();
    else data.erase(0, nl + 1);
  } else if (!read_file(path, &data)) {
    if (err) *err = "cannot read " + path;
    return false;
  }

  std::vector<nlohmann::json> records;
  std::map<std::string, std::size_t> by_turn;
  std::size_t pos = 0;
  while (pos < data.size()) {
    std::size_t nl = data.find('\n', pos);
    if (nl == std::string::npos) nl = data.size();
    const std::string line = trim(data.substr(pos, nl - pos));
    pos = nl + 1;
    if (line.empty()) continue;
    nlohmann::json record;
    try {
      record = nlohmann::json::parse(line);
    } catch (const std::exception&) {
      // A crash can leave the last line half written; a reader must still be
      // able to use everything before it.
      LOG_DEBUG("trajectory: skipping an unparsable line in ", path);
      continue;
    }
    if (!record.is_object()) continue;
    const std::string id = record.value("turn_id", std::string());
    if (id.empty()) continue;
    std::map<std::string, std::size_t>::iterator it = by_turn.find(id);
    if (it == by_turn.end()) {
      by_turn[id] = records.size();
      records.push_back(record);
    } else {
      // The recorder rewrites a turn's line in place; if a fallback append
      // happened, the later line is the newer snapshot.
      records[it->second] = record;
    }
  }

  out->assign(records.rbegin(), records.rend());  // newest first
  return true;
}

nlohmann::json trajectory_summary(const nlohmann::json& turn) {
  nlohmann::json s = nlohmann::json::object();
  if (!turn.is_object()) return s;
  s["turn_id"] = turn.value("turn_id", std::string());
  s["started_at"] = turn.value("started_at", std::string());
  s["elapsed_ms"] = turn.value("elapsed_ms", static_cast<long long>(0));
  s["ok"] = turn.value("ok", false);
  s["cancelled"] = turn.value("cancelled", false);
  s["iterations"] = turn.value("iterations", 0);
  s["steps"] = (turn.contains("steps") && turn["steps"].is_array())
                   ? static_cast<int>(turn["steps"].size())
                   : 0;
  s["usage"] = (turn.contains("usage") && turn["usage"].is_object()) ? turn["usage"]
                                                                    : nlohmann::json::object();
  // Only real paths count as a file change; exec entries carry a command instead.
  long long files = 0;
  if (turn.contains("files_changed") && turn["files_changed"].is_array()) {
    for (nlohmann::json::const_iterator it = turn["files_changed"].begin();
         it != turn["files_changed"].end(); ++it) {
      if (!it->is_object()) continue;
      if (!it->contains("path") || !(*it)["path"].is_string()) continue;
      if ((*it)["path"].get<std::string>().empty()) continue;
      ++files;
    }
  }
  s["files_changed"] = files;
  s["partial"] = turn.value("partial", false);
  return s;
}

// ---------------------------------------------------------------------------
// TrajectoryRecorder
// ---------------------------------------------------------------------------
bool TrajectoryRecorder::begin(Workspace& workspace, const std::string& session_id,
                               const std::string& workspace_id, const std::string& provider,
                               const std::string& model) {
  const TrajectoryLimits limits = trajectory_limits_from_settings(workspace.settings());
  if (!limits.enabled) {
    LOG_DEBUG("trajectory: disabled by settings");
    return false;
  }
  if (!trajectory_id_is_safe(session_id)) {
    LOG_WARN("trajectory: unusable session id, not recording: ", session_id);
    return false;
  }

  workspace_ = &workspace;
  limits_ = limits;
  session_id_ = session_id;
  workspace_id_ = workspace_id;
  provider_ = provider;
  model_ = model;
  path_ = trajectory_file_path(workspace.run_dir(), session_id);
  turn_id_ = make_id("t_");
  started_ms_ = now_ms();
  started_at_ = now_iso8601_ms();
  finished_ = false;
  ok_ = false;
  error_.clear();
  detail_.clear();
  usage_ = ChatUsage();
  usage_estimated_ = false;
  steps_ = nlohmann::json::array();
  files_changed_ = nlohmann::json::array();
  range_valid_ = false;
  range_offset_ = 0;
  range_len_ = 0;
  active_ = true;

  // Write the header line immediately: a device that loses power mid-turn still
  // leaves proof that the turn started and how far it got.
  flush();
  return true;
}

nlohmann::json TrajectoryRecorder::build() const {
  nlohmann::json turn = nlohmann::json::object();
  turn["version"] = 1;
  turn["turn_id"] = turn_id_;
  turn["session_id"] = session_id_;
  turn["workspace_id"] = workspace_id_;
  turn["provider"] = provider_;
  turn["model"] = model_;
  turn["started_at"] = started_at_;
  turn["elapsed_ms"] = now_ms() - started_ms_;
  turn["ok"] = finished_ ? ok_ : false;
  turn["cancelled"] = cancelled_;
  turn["iterations"] = iterations_;
  turn["usage"] = usage_object(usage_, usage_estimated_);
  turn["steps"] = steps_;
  turn["files_changed"] = files_changed_;
  if (finished_) turn["finished_at"] = finished_at_;
  else turn["partial"] = true;
  if (!error_.empty()) turn["error"] = error_;
  if (!detail_.empty()) turn["detail"] = detail_;
  return turn;
}

void TrajectoryRecorder::flush() {
  if (!active_) return;

  nlohmann::json turn = build();
  std::string line = dump_json(turn);

  // Whole-turn byte cap: shed the oldest tool output bodies (the biggest part)
  // until the record fits, and say so instead of silently losing context.
  if (static_cast<long long>(line.size()) > limits_.max_bytes) {
    bool shed = false;
    for (std::size_t i = 0; i < steps_.size() && static_cast<long long>(line.size()) > limits_.max_bytes;
         ++i) {
      nlohmann::json& step = steps_[i];
      if (!step.is_object() || step.value("kind", std::string()) != "tool") continue;
      if (!step.contains("output") || !step["output"].is_string()) continue;
      const std::string body = step["output"].get<std::string>();
      if (body.empty() || body == kOutputDroppedMarker) continue;
      step["output"] = kOutputDroppedMarker;
      step["output_truncated"] = true;
      shed = true;
      turn = build();
      line = dump_json(turn);
    }
    if (shed) {
      turn["turn_truncated"] = true;
      line = dump_json(turn);
      LOG_WARN("trajectory: turn ", turn_id_, " exceeded trajectory_max_bytes; tool output shed");
    }
  }

  std::lock_guard<std::mutex> lock(trajectory_file_mutex());
  const uint64_t size = file_size(path_);
  if (range_valid_ && range_offset_ + range_len_ == size) {
    // Our line is still the tail: refresh it in place and truncate the rest.
    if (write_file_at(path_, range_offset_, line + "\n")) {
      range_len_ = static_cast<uint64_t>(line.size()) + 1;
      return;
    }
    LOG_WARN("trajectory: in-place update failed for ", path_, "; appending instead");
    range_valid_ = false;
  }
  range_offset_ = size;
  range_len_ = static_cast<uint64_t>(line.size()) + 1;
  range_valid_ = write_file(path_, line + "\n", true, nullptr);
}

void TrajectoryRecorder::record_compaction(long long covered_messages, long long summary_bytes) {
  if (!active_) return;
  nlohmann::json step = nlohmann::json::object();
  step["index"] = static_cast<int>(steps_.size());
  step["kind"] = "compaction";
  const long long now = now_ms();
  step["started_at"] = step_started_at(now);
  step["elapsed_ms"] = 0;
  step["covered_messages"] = covered_messages;
  step["summary_bytes"] = summary_bytes;
  steps_.push_back(step);
  flush();
}

void TrajectoryRecorder::record_model(long long started_ms, const std::string& reasoning,
                                      const std::string& content,
                                      const std::vector<ToolCall>& tool_calls,
                                      const ChatUsage& usage, bool usage_estimated,
                                      long long ttft_ms) {
  if (!active_) return;
  const long long now = now_ms();
  nlohmann::json step = nlohmann::json::object();
  step["index"] = static_cast<int>(steps_.size());
  step["kind"] = "model";
  step["started_at"] = step_started_at(started_ms);
  step["elapsed_ms"] = now > started_ms ? now - started_ms : 0;
  step["reasoning_chars"] = static_cast<long long>(utf8_length(reasoning));
  step["content_chars"] = static_cast<long long>(utf8_length(content));
  nlohmann::json names = nlohmann::json::array();
  for (std::size_t i = 0; i < tool_calls.size(); ++i) names.push_back(tool_calls[i].name);
  step["tool_calls"] = names;
  nlohmann::json tokens = nlohmann::json::object();
  tokens["prompt"] = usage.present ? usage.prompt_tokens : 0;
  tokens["completion"] = usage.present ? usage.completion_tokens : 0;
  tokens["cached"] = usage.present ? usage.cached_tokens : 0;
  step["tokens"] = tokens;
  step["usage_estimated"] = usage_estimated || !usage.present;
  if (ttft_ms > 0) step["ttft_ms"] = ttft_ms;
  steps_.push_back(step);
  flush();
}

void TrajectoryRecorder::record_tool(const std::string& name, const std::string& raw_arguments,
                                     const ToolResult& result, long long elapsed_ms) {
  if (!active_) return;
  const long long now = now_ms();
  nlohmann::json step = nlohmann::json::object();
  step["index"] = static_cast<int>(steps_.size());
  step["kind"] = "tool";
  step["started_at"] = step_started_at(now - (elapsed_ms > 0 ? elapsed_ms : 0));
  step["elapsed_ms"] = elapsed_ms > 0 ? elapsed_ms : 0;
  step["name"] = name;
  // Exactly what the model emitted (may be invalid JSON), so the audit trail
  // shows the real call rather than a re-serialised guess.
  step["arguments"] = raw_arguments;
  step["ok"] = result.ok;
  bool has_exit_code = false;
  const long long exit_code = meta_int(result.meta, "exit_code", &has_exit_code);
  if (has_exit_code) step["exit_code"] = exit_code;
  step["truncated"] = result.truncated;

  step["output_bytes"] = static_cast<long long>(result.output.size());
  std::string body = result.output;
  bool body_truncated = false;
  const long long cap = limits_.step_max_bytes;
  if (cap <= 0) {
    if (!body.empty()) {
      body_truncated = true;
      body.clear();
      body = "[trajectory: tool output not recorded (trajectory_step_max_bytes=0)]";
    }
  } else if (static_cast<long long>(body.size()) > cap) {
    const long long dropped = static_cast<long long>(body.size()) - cap;
    body = truncate_utf8(body, static_cast<std::size_t>(cap));
    body_truncated = true;
    body += "\n... [trajectory: " + std::to_string(dropped) + " bytes of tool output dropped]";
  }
  step["output"] = body;
  step["output_truncated"] = body_truncated;

  if (result.meta.is_object()) {
    if (result.meta.contains("output_filter") && result.meta["output_filter"].is_string()) {
      step["output_filter"] = result.meta["output_filter"];
      const long long dropped = meta_int(result.meta, "dropped_lines");
      if (dropped > 0) step["dropped_lines"] = dropped;
    }
  }
  steps_.push_back(step);

  // --- file changes: only what the tool itself reported -------------------
  const nlohmann::json& meta = result.meta;
  const bool has_file_op = meta.is_object() && meta.contains("file_op") && meta["file_op"].is_string();
  if (has_file_op) {
    const std::string op = meta["file_op"].get<std::string>();
    nlohmann::json fc = nlohmann::json::object();
    fc["op"] = op;
    if (meta.contains("path") && meta["path"].is_string()) fc["path"] = meta["path"];
    if (op == "write" || op == "append") {
      fc["bytes"] = meta_int(meta, "bytes");
      fc["size"] = meta_int(meta, "size");
    }
    if (op == "move" && meta.contains("dest") && meta["dest"].is_string()) {
      fc["dest"] = meta["dest"];
    }
    fc["tool"] = name;
    fc["ok"] = result.ok;
    files_changed_.push_back(fc);
  }
  // toolkit_exec cannot know which paths it touched, so record the command --
  // nothing more, and never a guessed path list.
  if (meta.is_object() && meta.contains("command") && meta["command"].is_string()) {
    nlohmann::json fc = nlohmann::json::object();
    fc["op"] = "exec";
    fc["command"] = meta["command"];
    bool has_code = false;
    const long long code = meta_int(meta, "exit_code", &has_code);
    if (has_code) fc["exit_code"] = code;
    fc["ok"] = result.ok;
    files_changed_.push_back(fc);
  }

  flush();
}

void TrajectoryRecorder::finish(bool ok, const std::string& error, const std::string& detail,
                                int iterations, const ChatUsage& usage, bool usage_estimated) {
  if (!active_) return;
  finished_ = true;
  finished_at_ = now_iso8601_ms();
  ok_ = ok;
  error_ = error;
  detail_ = detail;
  iterations_ = iterations;
  usage_ = usage;
  usage_estimated_ = usage_estimated;
  flush();
  prune();
}

void TrajectoryRecorder::prune() {
  // Refuse to rewrite a file we cannot read completely: read_file() would leave
  // the tail out and dropping the newest turn is worse than skipping retention
  // for one turn (which the next turn will retry).
  if (file_size(path_) > static_cast<uint64_t>(kTrajectoryReadCap)) {
    LOG_WARN("trajectory: ", path_, " is larger than ", kTrajectoryReadCap,
             " bytes; skipping retention");
    return;
  }
  std::string data;
  if (!read_file(path_, &data)) return;

  // Collapse the file into one record per turn (last occurrence wins), dropping
  // lines a crash left unparsable.
  std::vector<std::string> lines;
  std::vector<nlohmann::json> records;
  std::map<std::string, std::size_t> by_turn;
  std::size_t pos = 0;
  while (pos < data.size()) {
    std::size_t nl = data.find('\n', pos);
    if (nl == std::string::npos) nl = data.size();
    const std::string line = trim(data.substr(pos, nl - pos));
    pos = nl + 1;
    if (line.empty()) continue;
    nlohmann::json record;
    try {
      record = nlohmann::json::parse(line);
    } catch (const std::exception&) {
      continue;
    }
    if (!record.is_object()) continue;
    const std::string id = record.value("turn_id", std::string());
    if (id.empty()) continue;
    std::map<std::string, std::size_t>::iterator it = by_turn.find(id);
    if (it == by_turn.end()) {
      by_turn[id] = lines.size();
      lines.push_back(line);
      records.push_back(record);
    } else {
      lines[it->second] = line;
      records[it->second] = record;
    }
  }
  if (records.empty()) return;

  uint64_t total = 0;
  for (std::size_t i = 0; i < lines.size(); ++i) total += lines[i].size() + 1;

  std::size_t first = 0;
  while (records.size() - first > 1) {
    const bool too_many_turns =
        static_cast<long long>(records.size() - first) > static_cast<long long>(limits_.max_turns);
    const bool too_many_bytes = total > static_cast<uint64_t>(limits_.max_bytes);
    if (!too_many_turns && !too_many_bytes) break;
    total -= lines[first].size() + 1;
    ++first;
  }
  if (first == 0) return;

  std::string out;
  for (std::size_t i = first; i < lines.size(); ++i) {
    out += lines[i];
    out += '\n';
  }
  {
    std::lock_guard<std::mutex> lock(trajectory_file_mutex());
    // Atomic: the pruning rewrite is the one time the whole file is replaced, and
    // a reader that is already holding the (previous) contents must not be able to
    // open a truncated file in between.
    if (!write_file_atomic(path_, out, nullptr)) {
      LOG_WARN("trajectory: cannot rewrite ", path_, " after pruning");
      return;
    }
  }
  range_valid_ = false;
  const std::size_t pruned = first;
  const std::size_t kept = records.size() - first;
  LOG_INFO("trajectory: pruned ", pruned, " turn(s) from ", path_, " (kept ", kept, " turn(s), ",
           total, " bytes; limits: ", limits_.max_turns, " turns / ", limits_.max_bytes, " bytes)");
  if (workspace_) {
    workspace_->log("info", "trajectory: pruned " + std::to_string(pruned) + " turn(s) from " +
                                path_filename(path_) + " (kept " + std::to_string(kept) +
                                " turn(s), " + std::to_string(total) + " bytes; limits " +
                                std::to_string(limits_.max_turns) + " turns / " +
                                std::to_string(limits_.max_bytes) + " bytes)");
  }
}

}  // namespace tea
