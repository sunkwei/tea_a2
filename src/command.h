// command.h -- run a shell command and capture merged stdout/stderr.
#pragma once

#include <cstddef>
#include <string>

namespace tea {

struct CommandOptions {
  std::string command;
  std::string cwd;          // empty => inherit
  std::string tmp_dir;      // where the generated script file is written
  std::string shell;        // "cmd" | "powershell" | "pwsh" | "bash" | "sh" | "" => auto
  int timeout_ms = 180000;
  size_t max_output_bytes = 131072;
  bool utf8_console = true;
};

struct CommandResult {
  bool ok = false;               // process started and produced a result
  bool timed_out = false;
  bool output_truncated = false;
  int exit_code = -1;
  std::string output;            // stdout + stderr, merged, UTF-8
  long long elapsed_ms = 0;
  std::string error;             // spawn/pipe level failure
  std::string shell_used;
};

CommandResult run_command(const CommandOptions& opts);

/// Platform default interactive shell: "cmd" on Windows, "sh" elsewhere.
std::string default_shell();

/// Directory suitable for short-lived scratch files (always exists).
std::string system_temp_dir();

}  // namespace tea
