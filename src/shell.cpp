// shell.cpp -- the portable half of the interactive shell: the output buffer,
// the session registry and the capability probe.
//
// The platform halves (src/shell_posix.cpp, src/shell_win32.cpp) only have to
// spawn a process on a terminal, move bytes in and out, and resize it.
#include "shell.h"

#include <chrono>
#include <cstdlib>

#include "log.h"
#include "util.h"

namespace tea {

namespace shell_detail {
const char* platform_name();
const char* backend_name();
std::string default_shell();
}  // namespace shell_detail

ShellCapability shell_capability() {
  ShellCapability cap;
  cap.platform = shell_detail::platform_name();
  cap.backend = shell_detail::backend_name();
  cap.shell = shell_detail::default_shell();

  // The escape hatch: a machine that must not hand out a shell sets
  // TEA_AGENT_SHELL=off. Anything else (including "on") leaves it alone.
  const char* env = std::getenv("TEA_AGENT_SHELL");
  if (env && *env) {
    const std::string value = to_lower(trim(env));
    if (value == "0" || value == "off" || value == "false" || value == "no") {
      cap.available = false;
      cap.reason = "the interactive shell is disabled (TEA_AGENT_SHELL=" + std::string(env) + ")";
      return cap;
    }
  }
  if (cap.shell.empty()) {
    cap.available = false;
    cap.reason = "no shell program was found on this machine";
    return cap;
  }
  cap.available = true;
  return cap;
}

// ---------------------------------------------------------------------------
// ShellSession
// ---------------------------------------------------------------------------

ShellSession::ShellSession() { last_activity_ms_.store(now_ms()); }

ShellSession::~ShellSession() { close(); }

bool ShellSession::start(const std::string& shell, const std::string& cwd, int cols, int rows,
                         std::string* err) {
  // A terminal that is too small confuses every program that asks the tty for
  // its size, and one that is too wide turns every line into a puzzle.
  if (cols < 20) cols = 20;
  if (cols > 500) cols = 500;
  if (rows < 5) rows = 5;
  if (rows > 200) rows = 200;

  shell_ = shell.empty() ? shell_detail::default_shell() : shell;
  cwd_ = cwd;
  cols_ = cols;
  rows_ = rows;

  std::string e;
  if (!spawn(shell_, cwd_, cols, rows, &e)) {
    if (err) *err = e;
    return false;
  }
  running_.store(true);
  last_activity_ms_.store(now_ms());
  return true;
}

bool ShellSession::write(const std::string& data, std::string* err) {
  if (data.empty()) return true;
  if (!running_.load()) {
    if (err) *err = "the shell is not running";
    return false;
  }
  std::string e;
  if (!send_raw(data, &e)) {
    if (err) *err = e;
    return false;
  }
  last_activity_ms_.store(now_ms());
  return true;
}

bool ShellSession::read_since(std::size_t cursor, std::string* out, std::size_t* next,
                              bool* resync) const {
  std::lock_guard<std::mutex> lock(mu_);
  bool dropped = false;
  std::size_t from = cursor;
  if (from < base_) {
    // The buffer already threw these bytes away: the caller would see a hole
    // in the middle of the output, so it is told to repaint instead.
    from = base_;
    dropped = true;
  }
  if (from > total_) from = total_;
  if (out) out->assign(buf_, from - base_, total_ - from);
  if (next) *next = total_;
  if (resync) *resync = dropped;
  return true;
}

bool ShellSession::wait_for_output(std::size_t cursor, int timeout_ms) {
  std::unique_lock<std::mutex> lock(mu_);
  const auto ready = [this, cursor]() { return total_ > cursor || ended_; };
  if (ready()) return true;
  if (timeout_ms <= 0) return false;
  cv_.wait_for(lock, std::chrono::milliseconds(timeout_ms), ready);
  return ready();
}

bool ShellSession::resize(int cols, int rows, std::string* err) {
  if (cols < 20) cols = 20;
  if (cols > 500) cols = 500;
  if (rows < 5) rows = 5;
  if (rows > 200) rows = 200;
  std::string e;
  if (!set_size_raw(cols, rows, &e)) {
    if (err) *err = e;
    return false;
  }
  {
    std::lock_guard<std::mutex> lock(mu_);
    cols_ = cols;
    rows_ = rows;
  }
  last_activity_ms_.store(now_ms());
  return true;
}

void ShellSession::close() {
  if (closed_.exchange(true)) return;
  terminate_raw();
  running_.store(false);
  {
    std::lock_guard<std::mutex> lock(mu_);
    ended_ = true;
  }
  cv_.notify_all();
}

bool ShellSession::ended() const {
  std::lock_guard<std::mutex> lock(mu_);
  return ended_;
}

int ShellSession::cols() const {
  std::lock_guard<std::mutex> lock(mu_);
  return cols_;
}

int ShellSession::rows() const {
  std::lock_guard<std::mutex> lock(mu_);
  return rows_;
}

std::size_t ShellSession::total_output() const {
  std::lock_guard<std::mutex> lock(mu_);
  return total_;
}

void ShellSession::mark_ended() {
  running_.store(false);
  {
    std::lock_guard<std::mutex> lock(mu_);
    ended_ = true;
  }
  cv_.notify_all();
}

void ShellSession::push_output(const std::string& bytes) {
  if (bytes.empty()) return;
  {
    std::lock_guard<std::mutex> lock(mu_);
    buf_ += bytes;
    total_ += bytes.size();
    if (buf_.size() > kShellBufferBytes) {
      const std::size_t excess = buf_.size() - kShellBufferBytes;
      buf_.erase(0, excess);
      base_ += excess;
    }
    last_activity_ms_.store(now_ms());
  }
  cv_.notify_all();
}

nlohmann::json ShellSession::status_json() const {
  nlohmann::json j = nlohmann::json::object();
  j["shell"] = shell_;
  j["cwd"] = cwd_;
  j["cols"] = cols();
  j["rows"] = rows();
  j["running"] = running_.load();
  j["pid"] = static_cast<long long>(pid_.load());
  j["exit_code"] = exit_code_.load();
  j["output_bytes"] = static_cast<long long>(total_output());
  j["ended"] = ended();
  return j;
}

// ---------------------------------------------------------------------------
// ShellRegistry
// ---------------------------------------------------------------------------

/// How many shells one server hands out at the same time. Each one is a
/// process plus a reader thread, and a forgotten tab must not be able to
/// spawn a hundred of them.
constexpr std::size_t kMaxShellSessions = 8;

ShellRegistry::ShellRegistry() {}

ShellRegistry::~ShellRegistry() { close_all(); }

bool ShellRegistry::create(const std::string& shell, const std::string& cwd, int cols, int rows,
                            std::string* id, std::string* err) {
  const ShellCapability cap = shell_capability();
  if (!cap.available) {
    if (err) {
      *err = cap.reason.empty() ? "the interactive shell is not available on this platform"
                                : cap.reason;
    }
    return false;
  }
  {
    std::lock_guard<std::mutex> lock(mu_);
    if (sessions_.size() >= kMaxShellSessions) {
      if (err) {
        *err = "too many shell sessions are open (limit " + std::to_string(kMaxShellSessions) +
               "); close one first";
      }
      return false;
    }
  }

  std::unique_ptr<ShellSession> session = make_shell_session(err);
  if (!session) {
    if (err && err->empty()) *err = "the interactive shell could not be started";
    return false;
  }
  std::string e;
  if (!session->start(shell, cwd, cols, rows, &e)) {
    if (err) *err = e;
    return false;
  }

  const std::string sid = make_id("sh_");
  const long pid = session->pid();
  {
    std::lock_guard<std::mutex> lock(mu_);
    sessions_[sid] = std::shared_ptr<ShellSession>(session.release());
  }
  if (id) *id = sid;
  LOG_INFO("shell: session ", sid, " started (", shell, ", pid ", pid, ")");
  return true;
}

std::shared_ptr<ShellSession> ShellRegistry::find(const std::string& id) {
  std::lock_guard<std::mutex> lock(mu_);
  auto it = sessions_.find(id);
  return it == sessions_.end() ? nullptr : it->second;
}

bool ShellRegistry::close(const std::string& id) {
  std::shared_ptr<ShellSession> session;
  {
    std::lock_guard<std::mutex> lock(mu_);
    auto it = sessions_.find(id);
    if (it == sessions_.end()) return false;
    session = it->second;
    sessions_.erase(it);
  }
  session->close();
  return true;
}

void ShellRegistry::close_all() {
  std::map<std::string, std::shared_ptr<ShellSession>> sessions;
  {
    std::lock_guard<std::mutex> lock(mu_);
    sessions.swap(sessions_);
  }
  for (auto it = sessions.begin(); it != sessions.end(); ++it) it->second->close();
}

nlohmann::json ShellRegistry::list_json() const {
  nlohmann::json list = nlohmann::json::array();
  {
    std::lock_guard<std::mutex> lock(mu_);
    for (auto it = sessions_.begin(); it != sessions_.end(); ++it) {
      nlohmann::json j = it->second->status_json();
      j["id"] = it->first;
      list.push_back(j);
    }
  }
  return list;
}

void ShellRegistry::reap(int max_idle_ms) {
  std::vector<std::shared_ptr<ShellSession>> dead;
  {
    std::lock_guard<std::mutex> lock(mu_);
    const int64_t now = now_ms();
    for (auto it = sessions_.begin(); it != sessions_.end();) {
      const bool finished = it->second->ended();
      const bool idle = max_idle_ms > 0 &&
                        (now - it->second->last_activity_ms()) > static_cast<int64_t>(max_idle_ms);
      if (finished || idle) {
        dead.push_back(it->second);
        it = sessions_.erase(it);
      } else {
        ++it;
      }
    }
  }
  // Closing outside the lock: it waits for the reader thread.
  for (std::size_t i = 0; i < dead.size(); ++i) dead[i]->close();
}

}  // namespace tea
