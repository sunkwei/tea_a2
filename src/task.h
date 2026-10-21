// task.h -- the work queue a scheduler can pick tasks from.
//
// A task is "something to do later": a prompt, the session it runs in, and the
// state of that work. The scheduler (sched/task_runner.py) lists the queued ones,
// starts them while the provider still has quota, and watches their progress; a
// human can do the same through the HTTP API.
//
// Storage: one file per task, <workspace>/.tea_agent_run/tasks/<id>.json. One
// file per task means two writers never collide on the same file, and the
// read-modify-write of a single task is under the process-wide state lock (see
// state_lock.h) - the same lock the session index and memory.json use.
#pragma once

#include <functional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "workspace.h"

namespace tea {

/// Where a task is in its life.
namespace task_status {
extern const char* const kQueued;             // waiting to be picked up
extern const char* const kRunning;            // a turn is working on it
extern const char* const kWaitingConfirmation; // the agent needs the user
extern const char* const kDone;               // finished
extern const char* const kFailed;             // the turn failed
extern const char* const kCancelled;          // stopped by a human
}  // namespace task_status

struct Task {
  std::string id;
  std::string workspace_id;
  /// The session the task runs in. Empty until the task is started.
  std::string session_id;
  /// Short label for a list; the prompt is the real content.
  std::string title;
  /// What the agent should do.
  std::string prompt;
  std::string status = task_status::kQueued;
  /// 0..100, as reported by the agent.
  int progress = 0;
  /// The latest intermediate state, in one line.
  std::string note;
  /// The history of intermediate states (what it did, in order).
  nlohmann::json steps = nlohmann::json::array();
  /// The agent asked the user something and stopped.
  bool needs_confirmation = false;
  /// What it is waiting for.
  std::string question;
  std::string provider;
  std::string model;
  std::string created_at;
  std::string updated_at;
  std::string started_at;
  std::string finished_at;
  /// Why it failed, if it did.
  std::string error;
  /// Free-form data for whoever owns the task. A script that keeps its own
  /// bookkeeping (a ticket id, a retry count, a deadline) puts it here instead of
  /// asking the server for a new field.
  nlohmann::json meta = nlohmann::json::object();

  nlohmann::json to_json() const;
  static Task from_json(const nlohmann::json& j);
};

/// The task store of one workspace.
class TaskStore {
 public:
  /// <workspace>/.tea_agent_run/tasks
  static std::string dir_for(const Workspace& ws);

  /// Create a task (status queued) and write it out. The caller fills in
  /// what the task is - prompt, title, session, provider - the store owns the id
  /// and the timestamps.
  static bool create(const Workspace& ws, Task* task, std::string* err);

  /// Read one task. `err` says why when it fails.
  static bool load(const Workspace& ws, const std::string& id, Task* out, std::string* err);

  /// Write a task out (atomic replace).
  static bool save(const Workspace& ws, const Task& task, std::string* err);

  /// Read-modify-write one task under the state lock. `fn` may change the task;
  /// the result is written back. This is the only way to change a task, so two
  /// writers (the agent and an API caller) cannot lose each other's update.
  static bool update(const Workspace& ws, const std::string& id,
                     const std::function<void(Task*)>& fn, Task* out, std::string* err);

  /// Every task of the workspace, newest first.
  static std::vector<Task> list(const Workspace& ws);

  /// The tasks of one status, oldest first (that is the order work is done in).
  static std::vector<Task> list_by_status(const Workspace& ws, const std::string& status);
};

}  // namespace tea
