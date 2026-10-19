#include "command.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#include "log.h"
#include "util.h"

#ifdef _WIN32
#include <winsock2.h>
#include <windows.h>

#include <io.h>
#else
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#endif

namespace tea {

std::string default_shell() {
#ifdef _WIN32
  return "cmd";
#else
  return "sh";
#endif
}

std::string system_temp_dir() {
#ifdef _WIN32
  wchar_t buffer[MAX_PATH + 2];
  const DWORD n = GetTempPathW(MAX_PATH + 1, buffer);
  if (n > 0 && n <= MAX_PATH) {
    std::string utf8 = wide_to_utf8(std::wstring(buffer, n));
    while (!utf8.empty() && (utf8.back() == '\\' || utf8.back() == '/')) utf8.pop_back();
    if (!utf8.empty()) return utf8;
  }
  return ".";
#else
  const char* tmp = std::getenv("TMPDIR");
  if (tmp && *tmp) return tmp;
  return "/tmp";
#endif
}

namespace {

void trim_trailing_newlines(std::string* s) {
  // Keep the transcript tidy; the UI re-adds spacing.
  while (!s->empty() && (s->back() == '\n' || s->back() == '\r')) s->pop_back();
}

void strip_utf8_bom(std::string* s) {
  if (s->size() >= 3 && static_cast<unsigned char>((*s)[0]) == 0xEF &&
      static_cast<unsigned char>((*s)[1]) == 0xBB &&
      static_cast<unsigned char>((*s)[2]) == 0xBF) {
    s->erase(0, 3);
  }
}

std::string script_extension(const std::string& shell) {
  if (shell == "powershell" || shell == "pwsh") return ".ps1";
#ifdef _WIN32
  // Everything else is executed by cmd.exe on Windows (see run_command), and
  // cmd.exe only runs a batch file when it is named .bat/.cmd: handed a .sh it
  // silently does nothing and reports exit 0, so the command looks like it
  // succeeded with empty output. Never let the name come from the requested
  // shell here - `shell: "sh"` still means a cmd batch on this platform.
  (void)shell;
  return ".cmd";
#else
  if (shell == "bash" || shell == "sh") return ".sh";
  return ".cmd";
#endif
}

/// Build the script body that is written to a temporary file. Running a script
/// file instead of passing the command on the command line sidesteps every
/// cmd.exe/PowerShell quoting pitfall.
std::string script_body(const std::string& shell, const CommandOptions& opts) {
  const bool is_ps = (shell == "powershell" || shell == "pwsh");
  std::string body;
  if (is_ps) {
    if (opts.utf8_console) {
      body += "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false\n";
      body += "$OutputEncoding = [Console]::OutputEncoding\n";
    }
    body += opts.command;
    body += "\n";
    body += "exit $LASTEXITCODE\n";
  } else {
    body += "@echo off\r\n";
    if (opts.utf8_console) body += "chcp 65001 >nul 2>nul\r\n";
    body += "setlocal\r\n";
    body += opts.command;
    body += "\r\n";
    body += "exit /b %ERRORLEVEL%\r\n";
  }
  return body;
}

}  // namespace

#ifdef _WIN32

CommandResult run_command(const CommandOptions& opts) {
  CommandResult res;
  const int64_t started = now_ms();

  const std::string shell = opts.shell.empty() ? default_shell() : to_lower(opts.shell);
  res.shell_used = shell;
  const bool is_ps = (shell == "powershell" || shell == "pwsh");

  std::string tmp_dir = opts.tmp_dir.empty() ? system_temp_dir() : opts.tmp_dir;
  make_dirs(tmp_dir);
  const std::string script_path =
      path_join(tmp_dir, "tea_cmd_" + random_hex(6) + script_extension(shell));

  if (!write_file(script_path, script_body(shell, opts), false, &res.error)) {
    return res;
  }

  std::wstring cmdline;
  if (is_ps) {
    const std::wstring exe = utf8_to_wide(shell == "pwsh" ? "pwsh.exe" : "powershell.exe");
    cmdline = L"\"" + exe +
              L"\" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"" +
              utf8_to_wide(script_path) + L"\"";
  } else {
    cmdline = L"cmd.exe /d /s /c \"" + utf8_to_wide(script_path) + L"\"";
  }

  SECURITY_ATTRIBUTES sa;
  std::memset(&sa, 0, sizeof(sa));
  sa.nLength = sizeof(sa);
  sa.bInheritHandle = TRUE;

  HANDLE pipe_read = nullptr;
  HANDLE pipe_write = nullptr;
  if (!CreatePipe(&pipe_read, &pipe_write, &sa, 1 << 20)) {
    res.error = "CreatePipe failed";
    return res;
  }
  SetHandleInformation(pipe_read, HANDLE_FLAG_INHERIT, 0);

  HANDLE nul_in = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa,
                              OPEN_EXISTING, 0, nullptr);

  STARTUPINFOW si;
  std::memset(&si, 0, sizeof(si));
  si.cb = sizeof(si);
  si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
  si.wShowWindow = SW_HIDE;
  si.hStdInput = nul_in;
  si.hStdOutput = pipe_write;
  si.hStdError = pipe_write;

  PROCESS_INFORMATION pi;
  std::memset(&pi, 0, sizeof(pi));

  const std::wstring cwd_w = utf8_to_wide(opts.cwd);
  const wchar_t* cwd_ptr = cwd_w.empty() ? nullptr : cwd_w.c_str();

  std::vector<wchar_t> mutable_cmd(cmdline.begin(), cmdline.end());
  mutable_cmd.push_back(L'\0');

  const BOOL created = CreateProcessW(nullptr, mutable_cmd.data(), nullptr, nullptr, TRUE,
                                      CREATE_NO_WINDOW | CREATE_SUSPENDED, nullptr, cwd_ptr, &si,
                                      &pi);
  if (!created) {
    const DWORD e = GetLastError();
    res.error = "CreateProcess failed (error " + std::to_string(e) + ")";
    CloseHandle(pipe_read);
    CloseHandle(pipe_write);
    if (nul_in != INVALID_HANDLE_VALUE) CloseHandle(nul_in);
    remove_path(script_path, false, nullptr);
    return res;
  }
  CloseHandle(pipe_write);
  if (nul_in != INVALID_HANDLE_VALUE) CloseHandle(nul_in);

  // A job object lets us kill the whole process tree on timeout.
  HANDLE job = CreateJobObjectW(nullptr, nullptr);
  if (job) {
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION info;
    std::memset(&info, 0, sizeof(info));
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    SetInformationJobObject(job, JobObjectExtendedLimitInformation, &info, sizeof(info));
    AssignProcessToJobObject(job, pi.hProcess);
  }
  ResumeThread(pi.hThread);

  std::string raw;
  raw.reserve(4096);
  bool process_done = false;
  int grace_loops = 0;
  char buffer[16384];

  for (;;) {
    if (!process_done) {
      process_done = (WaitForSingleObject(pi.hProcess, 0) == WAIT_OBJECT_0);
    }
    DWORD available = 0;
    if (!PeekNamedPipe(pipe_read, nullptr, 0, nullptr, &available, nullptr)) break;
    if (available > 0) {
      DWORD got = 0;
      const DWORD want = available < sizeof(buffer) ? available : static_cast<DWORD>(sizeof(buffer));
      if (!ReadFile(pipe_read, buffer, want, &got, nullptr) || got == 0) break;
      if (raw.size() < opts.max_output_bytes) {
        const size_t room = opts.max_output_bytes - raw.size();
        raw.append(buffer, got < room ? got : room);
        if (got > room) res.output_truncated = true;
      } else {
        res.output_truncated = true;
      }
      grace_loops = 0;
      continue;
    }
    if (process_done) {
      // Give the pipe a moment to flush whatever the child wrote last.
      if (++grace_loops > 40) break;
      Sleep(5);
      continue;
    }
    if (now_ms() - started > opts.timeout_ms) {
      res.timed_out = true;
      if (job) {
        TerminateJobObject(job, 1);
      } else {
        TerminateProcess(pi.hProcess, 1);
      }
      WaitForSingleObject(pi.hProcess, 5000);
      process_done = true;
      // Drain whatever is left, then stop.
      for (int i = 0; i < 200; ++i) {
        DWORD left = 0;
        if (!PeekNamedPipe(pipe_read, nullptr, 0, nullptr, &left, nullptr) || left == 0) break;
        DWORD got = 0;
        const DWORD want = left < sizeof(buffer) ? left : static_cast<DWORD>(sizeof(buffer));
        if (!ReadFile(pipe_read, buffer, want, &got, nullptr) || got == 0) break;
        if (raw.size() < opts.max_output_bytes) raw.append(buffer, got);
      }
      break;
    }
    Sleep(10);
  }

  DWORD exit_code = 0;
  if (res.timed_out) {
    exit_code = 124;  // conventional timeout code, matches GNU timeout(1)
  } else if (!GetExitCodeProcess(pi.hProcess, &exit_code)) {
    exit_code = static_cast<DWORD>(-1);
  }

  CloseHandle(pipe_read);
  CloseHandle(pi.hThread);
  CloseHandle(pi.hProcess);
  if (job) CloseHandle(job);
  remove_path(script_path, false, nullptr);

  strip_utf8_bom(&raw);
  res.output = native_to_utf8(raw);
  trim_trailing_newlines(&res.output);
  res.exit_code = static_cast<int>(exit_code);
  res.ok = true;
  res.elapsed_ms = now_ms() - started;

  LOG_DEBUG("exec[", shell, "] exit=", res.exit_code, " elapsed=", res.elapsed_ms,
            "ms bytes=", res.output.size());
  return res;
}

#else  // ------------------------------------------------------------------

CommandResult run_command(const CommandOptions& opts) {
  CommandResult res;
  const int64_t started = now_ms();

  const std::string shell = opts.shell.empty() ? default_shell() : to_lower(opts.shell);
  res.shell_used = shell;
  const bool use_bash = (shell == "bash");

  std::string tmp_dir = opts.tmp_dir.empty() ? system_temp_dir() : opts.tmp_dir;
  make_dirs(tmp_dir);
  const std::string script_path =
      path_join(tmp_dir, "tea_cmd_" + random_hex(6) + (use_bash ? ".sh" : ".sh"));

  std::string body;
  if (opts.utf8_console) {
    body += "export LANG=${LANG:-C.UTF-8}\n";
    body += "export LC_ALL=${LC_ALL:-$LANG}\n";
  }
  body += opts.command;
  body += "\n";
  if (!write_file(script_path, body, false, &res.error)) return res;

  int fds[2];
  if (pipe(fds) != 0) {
    res.error = "pipe() failed";
    remove_path(script_path, false, nullptr);
    return res;
  }

  const pid_t pid = fork();
  if (pid < 0) {
    res.error = "fork() failed";
    close(fds[0]);
    close(fds[1]);
    remove_path(script_path, false, nullptr);
    return res;
  }

  if (pid == 0) {
    setsid();
    const int devnull = open("/dev/null", O_RDONLY);
    if (devnull >= 0) {
      dup2(devnull, STDIN_FILENO);
      close(devnull);
    }
    dup2(fds[1], STDOUT_FILENO);
    dup2(fds[1], STDERR_FILENO);
    close(fds[0]);
    close(fds[1]);
    if (!opts.cwd.empty()) {
      if (chdir(opts.cwd.c_str()) != 0) _exit(126);
    }
    const char* argv[4];
    if (use_bash) {
      argv[0] = "bash";
      argv[1] = script_path.c_str();
      argv[2] = nullptr;
    } else {
      argv[0] = "sh";
      argv[1] = script_path.c_str();
      argv[2] = nullptr;
    }
    execvp(argv[0], const_cast<char* const*>(argv));
    _exit(127);
  }

  close(fds[1]);
  std::string raw;
  raw.reserve(4096);
  bool child_done = false;
  int status = 0;
  int grace = 0;

  for (;;) {
    if (!child_done && waitpid(pid, &status, WNOHANG) == pid) child_done = true;

    pollfd pfd;
    pfd.fd = fds[0];
    pfd.events = POLLIN;
    pfd.revents = 0;
    const int pr = poll(&pfd, 1, child_done ? 20 : 30);

    if (pr > 0 && (pfd.revents & (POLLIN | POLLHUP | POLLERR))) {
      char buffer[16384];
      const ssize_t got = read(fds[0], buffer, sizeof(buffer));
      if (got > 0) {
        if (raw.size() < opts.max_output_bytes) {
          const size_t room = opts.max_output_bytes - raw.size();
          raw.append(buffer, static_cast<size_t>(got) < room ? static_cast<size_t>(got) : room);
          if (static_cast<size_t>(got) > room) res.output_truncated = true;
        } else {
          res.output_truncated = true;
        }
        grace = 0;
        continue;
      }
      if (got == 0) break;  // EOF
      if (errno == EINTR) continue;
      break;
    }

    if (child_done) {
      if (++grace > 20) break;
      continue;
    }
    if (now_ms() - started > opts.timeout_ms) {
      res.timed_out = true;
      kill(-pid, SIGKILL);
      waitpid(pid, &status, 0);
      child_done = true;
      break;
    }
  }

  close(fds[0]);
  if (!child_done) {
    kill(-pid, SIGKILL);
    waitpid(pid, &status, 0);
  }
  remove_path(script_path, false, nullptr);

  int exit_code = -1;
  if (res.timed_out) {
    exit_code = 124;
  } else if (WIFEXITED(status)) {
    exit_code = WEXITSTATUS(status);
  } else if (WIFSIGNALED(status)) {
    exit_code = 128 + WTERMSIG(status);
  }

  strip_utf8_bom(&raw);
  res.output = native_to_utf8(raw);
  trim_trailing_newlines(&res.output);
  res.exit_code = exit_code;
  res.ok = true;
  res.elapsed_ms = now_ms() - started;
  LOG_DEBUG("exec[", shell, "] exit=", res.exit_code, " elapsed=", res.elapsed_ms,
            "ms bytes=", res.output.size());
  return res;
}

#endif

}  // namespace tea
