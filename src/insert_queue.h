// insert_queue.h -- messages the user typed while a turn is still running.
//
// A turn can run for minutes: a long tool call, several model iterations. The
// composer stays usable, and a message typed in that state does not start a
// second turn - it is parked here and takes effect later.
//
// Two timings, and the difference matters:
//
//   * "after" (the default): the message waits for the running turn to end and
//     then starts a **new** turn of its own. The running turn is not disturbed
//     at all.
//   * "now": the running turn adopts the message before its next model request,
//     so the tool calls the model decides next already know about it.
//
// The queue belongs to the **session**, not to the turn: a message that waits
// for the turn to end has to outlive it. The agent loop only ever adopts the
// "now" ones; the server hands the others to the next turn.
#pragma once

#include <cstddef>
#include <deque>
#include <mutex>
#include <string>
#include <vector>

namespace tea {

/// When a queued message takes effect.
enum class InsertMode {
  /// After the running turn ends: the message starts a new turn.
  kAfter = 0,
  /// Now: the running turn adopts it before its next model request.
  kNow = 1,
};

/// The wire name of a mode ("after" / "now"). Used in the API and in the log.
inline const char* insert_mode_name(InsertMode mode) {
  return mode == InsertMode::kNow ? "now" : "after";
}

/// Read a mode from the wire. Returns false for anything else - a misspelled
/// mode is an error, never a silent default.
inline bool parse_insert_mode(const std::string& text, InsertMode* out) {
  if (out == nullptr) return false;
  if (text == "now") {
    *out = InsertMode::kNow;
    return true;
  }
  if (text == "after") {
    *out = InsertMode::kAfter;
    return true;
  }
  return false;
}

/// One message waiting for its turn. `id` is what the UI sends back to cancel it.
struct QueuedPrompt {
  std::string id;
  std::string text;
  std::string created_at;
  InsertMode mode = InsertMode::kAfter;
};

/// Thread-safe FIFO. The HTTP thread pushes/removes, the agent worker pops;
/// no other synchronisation is needed.
class InsertQueue {
 public:
  void push(const QueuedPrompt& prompt) {
    std::lock_guard<std::mutex> lock(mu_);
    items_.push_back(prompt);
  }

  /// Take the oldest prompt that asks to be inserted *now*. Returns false when
  /// there is none - the ordinary "wait for the turn to end" messages stay.
  bool pop_now(QueuedPrompt* out) {
    std::lock_guard<std::mutex> lock(mu_);
    for (auto it = items_.begin(); it != items_.end(); ++it) {
      if (it->mode != InsertMode::kNow) continue;
      if (out) *out = *it;
      items_.erase(it);
      return true;
    }
    return false;
  }

  /// Is there a prompt waiting to be inserted now?
  bool has_now() const {
    std::lock_guard<std::mutex> lock(mu_);
    for (std::size_t i = 0; i < items_.size(); ++i) {
      if (items_[i].mode == InsertMode::kNow) return true;
    }
    return false;
  }

  /// Take the oldest prompt, whatever its timing. This is how the next turn
  /// gets its message.
  bool pop_next(QueuedPrompt* out) {
    std::lock_guard<std::mutex> lock(mu_);
    if (items_.empty()) return false;
    if (out) *out = items_.front();
    items_.pop_front();
    return true;
  }

  /// Put a prompt back at the front: it was taken out but could not be used
  /// (the turn it was meant for could not be started).
  void push_front(const QueuedPrompt& prompt) {
    std::lock_guard<std::mutex> lock(mu_);
    items_.push_front(prompt);
  }

  /// Turn every "insert now" prompt into an ordinary one. A turn that ends
  /// before it could adopt them leaves them behind: they wait for the next
  /// turn like any other message.
  void downgrade_now() {
    std::lock_guard<std::mutex> lock(mu_);
    for (std::size_t i = 0; i < items_.size(); ++i) {
      items_[i].mode = InsertMode::kAfter;
    }
  }

  /// Drop one prompt by id. Returns false when it was already adopted.
  bool remove(const std::string& id) {
    std::lock_guard<std::mutex> lock(mu_);
    for (auto it = items_.begin(); it != items_.end(); ++it) {
      if (it->id == id) {
        items_.erase(it);
        return true;
      }
    }
    return false;
  }

  /// Remove and return everything (turn end / cancel).
  std::vector<QueuedPrompt> drain() {
    std::lock_guard<std::mutex> lock(mu_);
    std::vector<QueuedPrompt> out(items_.begin(), items_.end());
    items_.clear();
    return out;
  }

  std::vector<QueuedPrompt> snapshot() const {
    std::lock_guard<std::mutex> lock(mu_);
    return std::vector<QueuedPrompt>(items_.begin(), items_.end());
  }

  bool empty() const {
    std::lock_guard<std::mutex> lock(mu_);
    return items_.empty();
  }

  std::size_t size() const {
    std::lock_guard<std::mutex> lock(mu_);
    return items_.size();
  }

 private:
  mutable std::mutex mu_;
  std::deque<QueuedPrompt> items_;
};

}  // namespace tea
