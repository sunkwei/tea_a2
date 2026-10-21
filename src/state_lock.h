// state_lock.h -- the process-wide lock for the on-disk state files.
//
// The runtime runs one worker thread per in-flight turn, so several sessions of
// one workspace can rewrite the same files at the same time. memory.json,
// sessions/index.json, agent.md, settings.yaml and the task files are all
// "read the file, change one thing, write the whole thing back"; without a lock
// the last writer wins and the other turn's change disappears (measured before
// this existed: 24 concurrent memory adds were all answered 200 and left exactly
// one note on disk).
//
// One lock for the whole process rather than one per Workspace: Workspace
// objects are cheap and created per request, and two of them legitimately point
// at the same directory, so a per-object lock would serialise nothing. The
// critical sections touch a few KB of JSON - irrelevant next to a model request.
//
// Recursive because the entry points compose: fork_session creates a child
// through save_session, rename_session loads then saves, a task update writes
// the task file and then the session index.
//
// Deliberately leaked: a mutex destroyed during static teardown could still be
// locked by a worker that is winding down.
#pragma once

#include <mutex>

namespace tea {

inline std::recursive_mutex& state_mutex() {
  static std::recursive_mutex* mu = new std::recursive_mutex();
  return *mu;
}

using StateLock = std::lock_guard<std::recursive_mutex>;

}  // namespace tea
