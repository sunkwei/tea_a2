#include "file_dialog.h"

#include <cstdlib>

#include "command.h"
#include "util.h"

namespace tea {
namespace {

std::string env_var(const char* name) {
  const char* v = std::getenv(name);
  return v ? std::string(v) : std::string();
}

/// Single-quote for a POSIX shell, so a path with spaces or quotes survives.
std::string sh_quote(const std::string& s) {
  std::string out = "'";
  for (std::size_t i = 0; i < s.size(); ++i) {
    if (s[i] == '\'') out += "'\\''";
    else out += s[i];
  }
  out += "'";
  return out;
}

/// Escape a string for embedding in a double-quoted PowerShell literal.
std::string ps_quote(const std::string& s) {
  std::string out = "\"";
  for (std::size_t i = 0; i < s.size(); ++i) {
    if (s[i] == '"' || s[i] == '`' || s[i] == '$') out += '`';
    out += s[i];
  }
  out += "\"";
  return out;
}

/// Escape for an AppleScript string literal.
std::string as_quote(const std::string& s) {
  std::string out = "\"";
  for (std::size_t i = 0; i < s.size(); ++i) {
    if (s[i] == '"' || s[i] == '\\') out += '\\';
    out += s[i];
  }
  out += "\"";
  return out;
}

bool command_exists(const std::string& name) {
  CommandOptions opts;
  opts.command = "command -v " + name + " >/dev/null 2>&1";
  opts.shell = "sh";
  opts.timeout_ms = 5000;
  opts.max_output_bytes = 1024;
  const CommandResult r = run_command(opts);
  return r.ok && r.exit_code == 0;
}

/// Run a picker command and interpret its stdout as the chosen path.
DirectoryPickResult run_picker(const std::string& command, const std::string& tool, int timeout_ms) {
  DirectoryPickResult out;
  out.tool = tool;

  CommandOptions opts;
  opts.command = command;
  opts.shell = "sh";
  opts.timeout_ms = timeout_ms;
  opts.max_output_bytes = 64 * 1024;
  const CommandResult r = run_command(opts);

  if (r.timed_out) {
    out.error = "the directory dialog did not close within " + std::to_string(timeout_ms / 1000) +
                "s";
    return out;
  }
  if (!r.ok) {
    out.error = r.error.empty() ? "could not start " + tool : r.error;
    return out;
  }
  // 1 is "the user cancelled" for zenity, kdialog and osascript alike.
  if (r.exit_code == 1) {
    out.cancelled = true;
    return out;
  }
  std::string path = trim(r.output);
  // Only the last non-empty line is the path; some backends print warnings first.
  const std::size_t nl = path.find_last_of('\n');
  if (nl != std::string::npos) path = trim(path.substr(nl + 1));
  if (r.exit_code != 0 || path.empty()) {
    if (r.exit_code != 0) {
      out.error = "the " + tool + " dialog failed (exit " + std::to_string(r.exit_code) +
                  "): " + truncate_utf8(path, 300);
    } else {
      out.error = "the " + tool + " dialog returned nothing";
    }
    return out;
  }
  out.ok = true;
  out.path = path;
  return out;
}

std::string current_backend() {
  if (!env_var("TEA_AGENT_DIR_PICKER").empty()) return "env";
#ifdef _WIN32
  return "powershell";
#elif defined(__APPLE__)
  return "osascript";
#else
  // Prefer the desktop's own dialog: kdialog on KDE, zenity elsewhere. Both
  // speak the same contract (stdout = path, exit 1 = cancelled).
  const std::string desktop = to_lower(env_var("XDG_CURRENT_DESKTOP"));
  if (desktop.find("kde") != std::string::npos && command_exists("kdialog")) return "kdialog";
  if (command_exists("zenity")) return "zenity";
  if (command_exists("kdialog")) return "kdialog";
  return "";
#endif
}

}  // namespace

bool directory_picker_available() { return !current_backend().empty(); }

DirectoryPickResult pick_directory(const std::string& initial_dir, const std::string& title,
                                   int timeout_ms) {
  DirectoryPickResult out;
  const std::string backend = current_backend();
  out.tool = backend;
  if (backend.empty()) {
    out.unavailable = true;
    out.error =
        "no native directory dialog is available on the server (install zenity or kdialog, set "
        "TEA_AGENT_DIR_PICKER, or type the path)";
    return out;
  }
  const std::string t = title.empty() ? "Select a project directory" : title;

  if (backend == "env") {
    // The override is trusted verbatim: it is an operator-provided command.
    return run_picker(env_var("TEA_AGENT_DIR_PICKER"), "TEA_AGENT_DIR_PICKER", timeout_ms);
  }
  if (backend == "powershell") {
    // FolderBrowserDialog is the Win32 shell dialog; UTF-8 output keeps non-ASCII
    // paths intact, and -STA is required by the shell APIs.
    std::string script =
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description=" + ps_quote(t) + ";"
        "$d.ShowNewFolderButton=$true;";
    if (!initial_dir.empty()) script += "$d.SelectedPath=" + ps_quote(initial_dir) + ";";
    script += "if($d.ShowDialog() -eq 'OK'){Write-Output $d.SelectedPath}else{exit 1}";
    return run_picker("powershell -NoProfile -STA -Command " + ps_quote(script), "powershell",
                      timeout_ms);
  }
  if (backend == "osascript") {
    std::string script = "POSIX path of (choose folder with prompt " + as_quote(t);
    if (!initial_dir.empty()) script += " default location POSIX file " + as_quote(initial_dir);
    script += ")";
    return run_picker("osascript -e " + sh_quote(script), "osascript", timeout_ms);
  }
  if (backend == "kdialog") {
    std::string cmd = "kdialog --getexistingdirectory";
    if (!initial_dir.empty()) cmd += " " + sh_quote(initial_dir);
    cmd += " --title " + sh_quote(t);
    // kdialog writes errors to stderr; the path is the only thing on stdout.
    return run_picker(cmd + " 2>/dev/null", "kdialog", timeout_ms);
  }
  // zenity
  std::string cmd = "zenity --file-selection --directory --title=" + sh_quote(t);
  if (!initial_dir.empty()) cmd += " --filename=" + sh_quote(initial_dir + "/");
  return run_picker(cmd + " 2>/dev/null", "zenity", timeout_ms);
}

}  // namespace tea
