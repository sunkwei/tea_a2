// prompt_queue.h -- prompts the user typed while a turn is already running.
//
// A turn can run for minutes: a long tool call, several model iterations. The
// composer stays usable, and Enter in that state enqueues the text instead of
// starting a second turn. The agent loop adopts exactly one queued prompt per
// iteration, so each one gets its own model round and the conversation keeps its
// normal shape (user -> assistant -> ...) instead of collapsing a burst of
// prompts into a single request.
//
// The queue lives inside the ChatTurn, i.e. exactly as long as one running turn.
// A finished or cancelled turn therefore cannot leave prompts behind that would
// fire in some later, unrelated turn. Anything not adopted by the time the loop
// ends is handed back to the caller (`queue_unconsumed`), which puts it in the
// composer again rather than silently dropping the user's text.
#pragma once

#include <cstddef>
#include <deque>
#include <mutex>
#include <string>
#include <vector>

namespace tea {

/// One prompt waiting for its turn. `id` is what the UI sends back to cancel it.
struct QueuedPrompt {
  std::string id;
  std::string text;
  std::string created_at;
};

/// Thread-safe FIFO. The HTTP thread pushes/removes, the agent worker pops; no
/// other synchronisation is needed.
class PromptInbox {
 public:
  void push(const QueuedPrompt& prompt) {
    std::lock_guard<std::mutex> lock(mu_);
    items_.push_back(prompt);
  }

  /// Take the oldest prompt. Returns false when the queue is empty.
  bool pop(QueuedPrompt* out) {
    std::lock_guard<std::mutex> lock(mu_);
    if (items_.empty()) return false;
    if (out) *out = items_.front();
    items_.pop_front();
    return true;
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
