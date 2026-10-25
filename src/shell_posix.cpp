// shell_posix.cpp -- the POSIX half of the interactive shell.
//
// Linux (and macOS) get a real pseudo terminal: posix_openpt() + fork() +
// exec(). The child gets the pty as its controlling terminal, so bash behaves
// exactly as it does in a terminal: line editing, job control, colours, and
// programs that ask the tty for its size.
//
// The Windows half lives in src/shell_win32.cpp (ConPTY + PowerShell).
#ifndef _WIN32

// The pty helpers (ptsname_r, ...) are POSIX 2008; ask for everything the
// platform has before the first include pulls in features.h.
#ifndef _GNU_SOURCE
#define _GNU_SOURCE 1
#endif

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include <chrono>
#include <cstring>
#include <thread>

#include "log.h"
#include "shell.h"
#include "util.h"

namespace tea {

namespace shell_detail {

const char* platform_name() {
#if defined(__linux__)
  return "linux";
#elif defined(__APPLE__)
  return "macos";
#else
  return "posix";
#endif
}

const char* backend_name() { return "pty"; }

std::string default_shell() {
  const char* env = std::getenv("SHELL");
  if (env && *env && access(env, X_OK) == 0) return env;
  if (access("/bin/bash", X_OK) == 0) return "/bin/bash";
  if (access("/usr/bin/bash", X_OK) == 0) return "/usr/bin/bash";
  if (access("/bin/sh", X_OK) == 0) return "/bin/sh";
  if (access("/usr/bin/sh", X_OK) == 0) return "/usr/bin/sh";
  return "";
}

}  // namespace shell_detail

namespace {

std::string errno_text() { return std::string(std::strerror(errno)); }

/// A shell on a pseudo terminal.
class PtySession : public ShellSession {
 public:
  ~PtySession() override { close(); }

 protected:
  bool spawn(const std::string& shell, const std::string& cwd, int cols, int rows,
             std::string* err) override;
  bool send_raw(const std::string& data, std::string* err) override;
  bool set_size_raw(int cols, int rows, std::string* err) override;
  void terminate_raw() override;

 private:
  void read_loop();
  void reap_child();

  int master_ = -1;
  /// The child's pid. It is also its process group id (the child called
  /// setsid), so the whole group can be signalled at once.
  std::atomic<long> child_pid_{-1};
  std::thread reader_;
  std::atomic<bool> stop_{false};
};

bool PtySession::spawn(const std::string& shell, const std::string& cwd, int cols, int rows,
                       std::string* err) {
  if (shell.empty()) {
    *err = "no shell program was found on this machine";
    return false;
  }

  const int master = posix_openpt(O_RDWR | O_NOCTTY);
  if (master < 0) {
    *err = "cannot open a pseudo terminal: " + errno_text();
    return false;
  }
  if (grantpt(master) != 0) {
    *err = "cannot grant access to the pseudo terminal: " + errno_text();
    ::close(master);
    return false;
  }
  if (unlockpt(master) != 0) {
    *err = "cannot unlock the pseudo terminal: " + errno_text();
    ::close(master);
    return false;
  }
  char slave_path[PATH_MAX];
  slave_path[0] = '\0';
  const int name_rc = ptsname_r(master, slave_path, sizeof(slave_path));
  if (name_rc != 0 || slave_path[0] == '\0') {
    *err = "cannot find the pseudo terminal's name: " + std::string(std::strerror(name_rc));
    ::close(master);
    return false;
  }

  // The size is set before the child starts, so the very first prompt is
  // already wrapped for the window the browser has.
  struct winsize ws;
  std::memset(&ws, 0, sizeof(ws));
  ws.ws_col = static_cast<unsigned short>(cols);
  ws.ws_row = static_cast<unsigned short>(rows);
  ioctl(master, TIOCSWINSZ, &ws);

  const pid_t child = fork();
  if (child < 0) {
    *err = "cannot start the shell: " + errno_text();
    ::close(master);
    return false;
  }
  if (child == 0) {
    // ---- the child ----
    // A new session, then the pty becomes its controlling terminal: that is
    // what makes the shell interactive (and gives it job control).
    if (setsid() < 0) _exit(127);
    const int slave = open(slave_path, O_RDWR);
    if (slave < 0) _exit(127);
    // Not fatal when it fails: opening the slave without O_NOCTTY already
    // made it the controlling terminal on most systems.
    ioctl(slave, TIOCSCTTY, 0);
    if (dup2(slave, STDIN_FILENO) < 0) _exit(127);
    if (dup2(slave, STDOUT_FILENO) < 0) _exit(127);
    if (dup2(slave, STDERR_FILENO) < 0) _exit(127);
    if (slave > STDERR_FILENO) ::close(slave);
    ::close(master);
    if (!cwd.empty() && chdir(cwd.c_str()) != 0) {
      // Staying in the old directory is better than not starting at all.
    }
    setenv("TERM", "xterm-256color", 1);
    // No arguments: a shell with a terminal on stdin is interactive by
    // itself, and this way it also works for shells we know nothing about.
    char* argv[2];
    argv[0] = const_cast<char*>(shell.c_str());
    argv[1] = nullptr;
    execvp(shell.c_str(), argv);
    _exit(127);
  }

  // ---- the parent ----
  master_ = master;
  child_pid_.store(static_cast<long>(child));
  set_pid(static_cast<long>(child));
  // Non-blocking: the reader thread must never sit in read() forever, or
  // closing the session would have to wait for the shell to say something.
  const int flags = fcntl(master, F_GETFL, 0);
  if (flags >= 0) fcntl(master, F_SETFL, flags | O_NONBLOCK);
  reader_ = std::thread([this]() { read_loop(); });
  return true;
}

void PtySession::read_loop() {
  char buffer[8192];
  while (!stop_.load()) {
    struct pollfd pfd;
    pfd.fd = master_;
    pfd.events = POLLIN;
    pfd.revents = 0;
    const int ready = poll(&pfd, 1, 200);
    if (ready < 0) {
      if (errno == EINTR) continue;
      break;
    }
    if (ready == 0) continue;
    if ((pfd.revents & (POLLIN | POLLHUP | POLLERR)) == 0) continue;
    bool eof = false;
    for (;;) {
      const ssize_t n = read(master_, buffer, sizeof(buffer));
      if (n > 0) {
        push_output(std::string(buffer, static_cast<std::size_t>(n)));
        continue;
      }
      if (n == 0) {
        eof = true;  // the shell closed the terminal: it is gone
        break;
      }
      if (errno == EINTR) continue;
      if (errno == EAGAIN || errno == EWOULDBLOCK) break;
      eof = true;
      break;
    }
    if (eof) break;
  }
  reap_child();
}

void PtySession::reap_child() {
  const long pid = child_pid_.load();
  if (pid <= 0) return;
  int status = 0;
  // Give the shell a moment to die on its own (it usually does, when the
  // terminal is closed), then insist.
  for (int i = 0; i < 25; ++i) {
    const pid_t r = waitpid(static_cast<pid_t>(pid), &status, WNOHANG);
    if (r == static_cast<pid_t>(pid)) {
      child_pid_.store(-1);
      set_exit_code(WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status));
      mark_ended();
      return;
    }
    if (r < 0) {
      child_pid_.store(-1);
      return;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  }
  kill(static_cast<pid_t>(pid), SIGKILL);
  waitpid(static_cast<pid_t>(pid), &status, 0);
  child_pid_.store(-1);
  set_exit_code(128 + SIGKILL);
  mark_ended();
}

bool PtySession::send_raw(const std::string& data, std::string* err) {
  if (master_ < 0) {
    *err = "the terminal is closed";
    return false;
  }
  std::size_t sent = 0;
  const int64_t deadline = now_ms() + 5000;
  while (sent < data.size()) {
    const ssize_t n = ::write(master_, data.data() + sent, data.size() - sent);
    if (n > 0) {
      sent += static_cast<std::size_t>(n);
      continue;
    }
    if (n < 0 && errno == EINTR) continue;
    if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
      if (now_ms() > deadline) {
        *err = "the terminal is not accepting input";
        return false;
      }
      struct pollfd pfd;
      pfd.fd = master_;
      pfd.events = POLLOUT;
      pfd.revents = 0;
      poll(&pfd, 1, 100);
      continue;
    }
    *err = "cannot write to the terminal: " + errno_text();
    return false;
  }
  return true;
}

bool PtySession::set_size_raw(int cols, int rows, std::string* err) {
  if (master_ < 0) {
    *err = "the terminal is closed";
    return false;
  }
  struct winsize ws;
  std::memset(&ws, 0, sizeof(ws));
  ws.ws_col = static_cast<unsigned short>(cols);
  ws.ws_row = static_cast<unsigned short>(rows);
  if (ioctl(master_, TIOCSWINSZ, &ws) != 0) {
    *err = "cannot resize the terminal: " + errno_text();
    return false;
  }
  return true;
}

void PtySession::terminate_raw() {
  stop_.store(true);
  const long pid = child_pid_.load();
  if (pid > 0) {
    // The whole process group, so a command the user started does not
    // outlive the session.
    kill(-static_cast<pid_t>(pid), SIGHUP);
    kill(static_cast<pid_t>(pid), SIGHUP);
  }
  if (reader_.joinable()) reader_.join();
  const long left = child_pid_.load();
  if (left > 0) {
    kill(-static_cast<pid_t>(left), SIGKILL);
    kill(static_cast<pid_t>(left), SIGKILL);
    int status = 0;
    waitpid(static_cast<pid_t>(left), &status, WNOHANG);
    child_pid_.store(-1);
  }
  if (master_ >= 0) {
    ::close(master_);
    master_ = -1;
  }
}

}  // namespace

std::unique_ptr<ShellSession> make_shell_session(std::string* err) {
  (void)err;
  return std::unique_ptr<ShellSession>(new PtySession());
}

}  // namespace tea

#endif  // _WIN32
