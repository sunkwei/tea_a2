// shell.h -- an interactive shell (a real terminal) that the browser drives.
//
// This is deliberately independent of the agent: no model, no tools, no
// sessions. It is a terminal emulator in the browser plus a pty (Linux) or a
// ConPTY (Windows) on this machine, and nothing else.
//
// The platform parts live in two files:
//   * src/shell_posix.cpp  - Linux/macOS: posix_openpt + fork + exec
//   * src/shell_win32.cpp  - Windows: ConPTY (CreatePseudoConsole) + PowerShell
// Both provide the same factory, make_shell_session().
#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

namespace tea {

/// How much output one session keeps for a browser that comes back late.
/// 4 MB is a lot of terminal output; past it the oldest bytes are dropped and
/// the browser is told to repaint (a shell that prints forever must not eat
/// the server's memory).
constexpr std::size_t kShellBufferBytes = 4u * 1024u * 1024u;

/// What this build can do about shells.
struct ShellCapability {
  bool available = false;
  std::string platform;   ///< "linux", "windows", ...
  std::string backend;    ///< "pty" (Linux) / "conpty" (Windows)
  std::string shell;      ///< the program that would run
  std::string reason;     ///< why not, when it is not available
};

/// Probe the platform. Never fails: an unavailable platform is a normal
/// answer, not an error.
ShellCapability shell_capability();

/// One shell running on this machine.
///
/// The output buffer and the cursor arithmetic live here (portable); the
/// platform subclass only has to spawn the process, move bytes into it and
/// take them out again.
class ShellSession {
 public:
  ShellSession();
  virtual ~ShellSession();

  ShellSession(const ShellSession&) = delete;
  ShellSession& operator=(const ShellSession&) = delete;

  /// Start the shell. `shell` is the program to run (empty = the platform
  /// default), `cwd` the directory to start in (empty = the server's own).
  /// On failure `err` says why and nothing is running.
  bool start(const std::string& shell, const std::string& cwd, int cols, int rows,
             std::string* err);

  /// Send what the user typed to the terminal.
  bool write(const std::string& data, std::string* err);

  /// Everything the terminal produced since `cursor`. `next` is the new
  /// cursor. `resync` is set when the buffer had already dropped the bytes
  /// the caller had not read yet - the browser then repaints from scratch
  /// instead of showing a hole.
  bool read_since(std::size_t cursor, std::string* out, std::size_t* next,
                  bool* resync) const;

  /// Block until there is something to read after `cursor`, until the shell
  /// ends, or until the timeout expires. This is what keeps the browser's
  /// poll cheap: no output means no request traffic at all.
  bool wait_for_output(std::size_t cursor, int timeout_ms);

  /// Tell the terminal that its size changed (the pty then re-wraps the
  /// output and the shell redraws its prompt).
  bool resize(int cols, int rows, std::string* err);

  /// End the session: the shell is asked to quit and its process is killed if
  /// it does not. Safe to call more than once.
  void close();

  bool running() const { return running_.load(); }
  int exit_code() const { return exit_code_.load(); }
  long pid() const { return pid_.load(); }
  const std::string& shell() const { return shell_; }
  const std::string& cwd() const { return cwd_; }
  int cols() const;
  int rows() const;
  std::size_t total_output() const;
  int64_t last_activity_ms() const { return last_activity_ms_.load(); }
  bool ended() const;

  nlohmann::json status_json() const;

 protected:
  /// Platform hooks, implemented by shell_posix.cpp / shell_win32.cpp.
  virtual bool spawn(const std::string& shell, const std::string& cwd, int cols, int rows,
                     std::string* err) = 0;
  virtual bool send_raw(const std::string& data, std::string* err) = 0;
  virtual bool set_size_raw(int cols, int rows, std::string* err) = 0;
  virtual void terminate_raw() = 0;

  /// Called by the platform's reader thread with whatever the terminal
  /// produced.
  void push_output(const std::string& bytes);

  /// The platform half reports the pid and the exit code it saw.
  void set_pid(long pid) { pid_.store(pid); }
  void set_exit_code(int code) { exit_code_.store(code); }

  /// The platform half calls this when the shell process is gone (the user
  /// typed "exit", or the shell was killed). Without it the browser would
  /// keep polling a session that will never say anything again.
  void mark_ended();

 private:
  mutable std::mutex mu_;
  std::condition_variable cv_;
  std::string buf_;        ///< pending output; buf_[0] sits at absolute offset base_
  std::size_t base_ = 0;   ///< absolute offset of buf_[0]
  std::size_t total_ = 0;  ///< bytes produced so far
  bool ended_ = false;

  std::string shell_;
  std::string cwd_;
  int cols_ = 80;
  int rows_ = 24;

  std::atomic<int> exit_code_{-1};
  std::atomic<long> pid_{0};
  std::atomic<bool> running_{false};
  std::atomic<bool> closed_{false};
  std::atomic<int64_t> last_activity_ms_{0};
};

/// Create a session for this platform. Returns null and fills `err` when the
/// platform cannot provide a terminal.
std::unique_ptr<ShellSession> make_shell_session(std::string* err);

/// Every shell this server is running.
class ShellRegistry {
 public:
  ShellRegistry();
  ~ShellRegistry();

  /// Start a session. `id` receives its id.
  bool create(const std::string& shell, const std::string& cwd, int cols, int rows,
              std::string* id, std::string* err);

  std::shared_ptr<ShellSession> find(const std::string& id);

  /// End one session (and forget it).
  bool close(const std::string& id);

  /// End every session. Called when the server stops.
  void close_all();

  nlohmann::json list_json() const;

  /// Forget the sessions that ended on their own, and end the ones that have
  /// been idle for `max_idle_ms`.
  void reap(int max_idle_ms);

 private:
  mutable std::mutex mu_;
  std::map<std::string, std::shared_ptr<ShellSession>> sessions_;
};

}  // namespace tea
