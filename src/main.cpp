// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// main.cpp -- command line entry point for tea_agent.
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <string>
#include <thread>
#include <vector>

#include "agent.h"
#include "ca_bundle.h"
#include "config.h"
#include "http.h"
#include "log.h"
#include "output_filter.h"
#include "server.h"
#include "tls.h"
#include "mcp.h"
#include "mqtt_client.h"
#include "tools.h"
#include "util.h"
#include "version.h"
#include "workspace.h"

#ifdef _WIN32
#include <winsock2.h>
#include <windows.h>
#include <shellapi.h>
#else
#include <unistd.h>
#endif

using namespace tea;

namespace {

std::atomic<bool> g_stop(false);
/// How many stop signals arrived. The second one means "not waiting any
/// longer": the first one starts the drain, the second cuts it short.
std::atomic<int> g_stop_signals(0);

#ifdef _WIN32
BOOL WINAPI console_handler(DWORD signal) {
  if (signal == CTRL_C_EVENT || signal == CTRL_BREAK_EVENT || signal == CTRL_CLOSE_EVENT) {
    g_stop.store(true);
    g_stop_signals.fetch_add(1);
    return TRUE;
  }
  return FALSE;
}
void install_signal_handlers() { SetConsoleCtrlHandler(console_handler, TRUE); }
#else
void posix_signal_handler(int) {
  g_stop.store(true);
  g_stop_signals.fetch_add(1);
}
void install_signal_handlers() {
  std::signal(SIGINT, posix_signal_handler);
  std::signal(SIGTERM, posix_signal_handler);
}
#endif

#ifdef _WIN32
/// A Windows application is not attached to a console. Started from Explorer, a
/// shortcut or the autostart entry it must not pop a black window - that is the
/// point of being one. But started from a terminal (`a2.exe --version`) it would
/// print nothing at all, because its stdout is connected to nothing. So attach
/// to the console of whoever started us - **unless** stdout/stderr were already
/// redirected (a pipe from the tray host, a log file, the test runner): those
/// are valid handles, and reopening CONOUT$ would throw their output away.
void attach_parent_console() {
  const HANDLE out = GetStdHandle(STD_OUTPUT_HANDLE);
  const HANDLE err = GetStdHandle(STD_ERROR_HANDLE);
  const bool have_out = out != nullptr && out != INVALID_HANDLE_VALUE;
  const bool have_err = err != nullptr && err != INVALID_HANDLE_VALUE;
  if (have_out && have_err) return;
  if (!AttachConsole(ATTACH_PARENT_PROCESS)) return;  // nobody to talk to
  if (!have_out) {
#if defined(_MSC_VER)
    FILE* reopened = nullptr;
    freopen_s(&reopened, "CONOUT$", "w", stdout);
#else
    std::freopen("CONOUT$", "w", stdout);
#endif
  }
  if (!have_err) {
#if defined(_MSC_VER)
    FILE* reopened = nullptr;
    freopen_s(&reopened, "CONOUT$", "w", stderr);
#else
    std::freopen("CONOUT$", "w", stderr);
#endif
  }
}
#else
void attach_parent_console() {}
#endif

#ifdef _WIN32
/// The file this process was started from - the *path*, not the image handle.
std::string self_exe_path() {
  wchar_t buffer[MAX_PATH * 4];
  const DWORD length =
      GetModuleFileNameW(nullptr, buffer, static_cast<DWORD>(sizeof(buffer) / sizeof(buffer[0])));
  if (length == 0 || length >= sizeof(buffer) / sizeof(buffer[0])) return std::string();
  return wide_to_utf8(std::wstring(buffer, length));
}

/// (mtime, size) of a file: the cheap way to see "this is a different file
/// now". An empty string means "no such file" - a build that renames the binary
/// aside leaves a gap between the rename and the new link.
std::string file_stamp(const std::string& path) {
  WIN32_FILE_ATTRIBUTE_DATA data;
  const std::wstring wide = utf8_to_wide(path);
  if (!GetFileAttributesExW(wide.c_str(), GetFileExInfoStandard, &data)) return std::string();
  const unsigned long long size =
      (static_cast<unsigned long long>(data.nFileSizeHigh) << 32) | data.nFileSizeLow;
  const unsigned long long mtime =
      (static_cast<unsigned long long>(data.ftLastWriteTime.dwHighDateTime) << 32) |
      data.ftLastWriteTime.dwLowDateTime;
  return std::to_string(mtime) + ":" + std::to_string(size);
}

/// Quote one argument for a Windows command line (the rules the CRT uses when
/// it splits one: a space or a quote in the argument needs the quotes around
/// it, and a quote inside needs a backslash in front of it).
std::string quote_cmdline_arg(const std::string& arg) {
  if (!arg.empty() && arg.find_first_of(" \t\"") == std::string::npos) return arg;
  std::string out = "\"";
  for (std::size_t i = 0; i < arg.size(); ++i) {
    std::size_t backslashes = 0;
    while (i < arg.size() && arg[i] == '\\') { ++backslashes; ++i; }
    if (i == arg.size()) {
      out.append(backslashes * 2, '\\');
      break;
    }
    if (arg[i] == '"') {
      out.append(backslashes * 2 + 1, '\\');
      out.push_back('"');
    } else {
      out.append(backslashes, '\\');
      out.push_back(arg[i]);
    }
  }
  out.push_back('"');
  return out;
}

/// Start this program again - the new build - and let it take the port over.
/// The new instance is told to wait for the port; this one stops right after,
/// so the two never serve at the same time.
bool spawn_takeover(const std::string& exe_path, const std::vector<std::string>& args) {
  std::string command = quote_cmdline_arg(exe_path);
  for (std::size_t i = 0; i < args.size(); ++i) {
    if (args[i] == "--takeover") continue;   // exactly one, at the end
    command += " " + quote_cmdline_arg(args[i]);
  }
  command += " --takeover";
  const std::wstring wide_command = utf8_to_wide(command);
  std::vector<wchar_t> buffer(wide_command.begin(), wide_command.end());
  buffer.push_back(L'\0');
  STARTUPINFOW startup = {};
  startup.cb = sizeof(startup);
  PROCESS_INFORMATION info = {};
  // CREATE_NO_WINDOW: the new instance is a Windows application, it must not
  // flash a console window on the way in.
  if (!CreateProcessW(nullptr, &buffer[0], nullptr, nullptr, FALSE, CREATE_NO_WINDOW, nullptr,
                      nullptr, &startup, &info)) {
    return false;
  }
  CloseHandle(info.hThread);
  CloseHandle(info.hProcess);
  return true;
}

/// Watch our own binary: when it changes on disk (a rebuild replaced it), the
/// new build takes this one's port over and this instance goes away.
///
/// The rule that matters: the file has to exist and to have *stopped* changing
/// before we act. The linker writes the new exe in pieces, and starting half a
/// binary is worse than staying on the old one.
void watch_own_binary(std::string exe_path, std::string start_stamp,
                       const std::vector<std::string>& args) {
  std::string previous = start_stamp;
  int settled = 0;
  for (;;) {
    std::this_thread::sleep_for(std::chrono::seconds(2));
    const std::string now = file_stamp(exe_path);
    if (now.empty() || now == start_stamp) {
      previous = now;
      settled = 0;
      continue;                       // no file (a build in progress), or the same one
    }
    if (now != previous) {
      previous = now;
      settled = 0;
      continue;                       // still being written
    }
    if (++settled < 2) continue;      // one more poll before we believe it
    LOG_INFO("main: the binary changed on disk - the new build takes over");
    if (!spawn_takeover(exe_path, args)) {
      LOG_ERROR("main: could not start the new build; keeping this one");
      start_stamp = now;              // do not try again on every poll
      previous = now;
      settled = 0;
      continue;
    }
    g_stop.store(true);               // the drain runs, then this instance exits
    return;
  }
}
#endif

struct Args {
  std::map<std::string, std::vector<std::string> > values;

  bool has(const std::string& key) const { return values.find(key) != values.end(); }
  std::string get(const std::string& key, const std::string& def = std::string()) const {
    std::map<std::string, std::vector<std::string> >::const_iterator it = values.find(key);
    if (it == values.end() || it->second.empty()) return def;
    return it->second.back();
  }
  std::vector<std::string> all(const std::string& key) const {
    std::map<std::string, std::vector<std::string> >::const_iterator it = values.find(key);
    if (it == values.end()) return std::vector<std::string>();
    return it->second;
  }
};

const char* kBooleanFlags[] = {"help", "version", "selftest", "open", "no-console", "quiet",
                               "allow-outside-workspace", "no-color", "insecure",
                               "gc", "gc-dry-run", "takeover", "no-reload-on-rebuild"};

bool is_log_level_name(const std::string& value) {
  const std::string v = to_lower(trim(value));
  return v == "trace" || v == "debug" || v == "info" || v == "warn" || v == "warning" ||
         v == "error" || v == "off" || v == "none";
}

bool is_boolean_flag(const std::string& name) {
  for (std::size_t i = 0; i < sizeof(kBooleanFlags) / sizeof(kBooleanFlags[0]); ++i) {
    if (name == kBooleanFlags[i]) return true;
  }
  return false;
}

void print_usage() {
  console_write_line(
      std::string(TEA_AGENT_NAME " " + std::string(tea::version_string()) + "\n"
                  " -- a minimal C++ LLM agent with tools, workspaces and a web UI.\n"
                  "\n"
                  "Usage: tea_agent [options]\n"
                  "\n"
                  "Server\n"
                  "  --host <addr>              bind address (default 127.0.0.1)\n"
                  "  --port <n>                 listen port (default 8383)\n"
                  "  --web-dir <path>           serve the UI from disk instead of the embedded copy\n"
                  "  --api-token <secret>       enable the fleet channel (/api/exec, /api/status) and\n"
                  "                             require this token for it (env: TEA_AGENT_API_TOKEN)\n"
                  "  --open                     open the web UI in the default browser\n"
                  "                             (each SSE stream keeps one HTTP thread; the pool grows\n"
                  "                              on demand - see the environment variables below)\n"
                  "\n"
                  "Model\n"
                  "  --config <path>            provider config (default $HOME/.tea_agent/provider.yaml)\n"
                  "  --tea-home <path>          agent home directory (default $HOME/.tea_agent)\n"
                  "  --provider <name>          provider to use\n"
                  "  --model <name>             model to use\n"
                  "  --http-engine <name>       auto | socket | winhttp\n"
                  "  --ca-bundle <path>         PEM file with trusted roots (default: the bundle\n"
                  "                             compiled into this binary)\n"
                  "  --insecure                 do not verify TLS certificates (testing only)\n"
                  "\n"
                  "MQTT control channel (opt-in; docs/mqtt_control.md)\n"
                  "  --mqtt <host:port>         register with an a2_broker and accept\n"
                  "                             tunnel.start / tunnel.stop / status\n"
                  "  --mqtt_user <user>         broker account (mqtt_accounts, role=device)\n"
                  "  --mqtt_pass <pass>         its password (visible in ps: keep the unit 0600)\n"
                  "  --mqtt_name <name>         base name; the registration is <name>@<ipv4>-<mac>\n"
                  "  --tunnel-broker <host:port> where the sidecar connects (default\n"
                  "                             103.232.92.54:8382; may differ from the MQTT broker)\n"
                  "  --print-registration       print the registration name and exit\n"
                  "\n"
                  "Workspaces\n"
                  "  --workspace <path>         register a workspace (repeatable)\n"
                  "  --workspace-root <dir>     register every immediate subdirectory of <dir>\n"
                  "  --allow-outside-workspace  let file tools touch paths outside the workspace\n"
                  "\n"
                  "Diagnostics\n"
                  "  --log-level <level>        what goes into the log file:\n"
                  "                             trace|debug|info|warn|error|off (default info)\n"
                  "  --console-level <level>    what is echoed to the console:\n"
                  "                             trace|debug|info|warn|error|off (default warn)\n"
                  "  --log-file <path>          global log file (default $HOME/.tea_agent/logs/tea_agent.log)\n"
                  "  --no-console               do not echo log lines to the console at all\n"
"  --no-reload-on-rebuild     do not hand the port over when the binary changes\n"
"                            (Windows: by default a rebuilt binary takes over)\n"
                  "  --quiet                    same as --no-console\n"
                  "  --help                     show this help\n"
                  "  --version                  show the version\n"
                  "  --selftest                 run built-in checks and exit\n"
                  "\n"
                  "History\n"
                  "  --gc                      clean the history of every workspace and exit\n"
                  "                            (sessions and trajectories): what is too old is\n"
                  "                            removed, what is cold is gzipped, and a run_dir\n"
                  "                            over its budget loses its oldest sessions\n"
                  "  --gc-dry-run              with --gc: only report what would happen\n"
                  "  --max-age-days <n>        override history_max_age_days for this run\n"
                  "  --max-bytes <n>           override history_max_bytes for this run\n"
                  "  --compress-after-days <n> override history_compress_after_days\n"
                  "  --keep-recent <n>         never touch the newest N sessions (default 5)\n"
                  "\n"
                  "Environment\n"
                  "  TEA_AGENT_HOME             overrides the agent home directory\n"
                  "  HTTP_PROXY / HTTPS_PROXY   used by the WinHTTP backend\n"
                  "  TEA_AGENT_HTTP_THREADS_MAX HTTP worker threads ceiling (default 128, min 16)\n"
                  "  TEA_AGENT_MAX_SUBSCRIBERS  how many browsers may follow one turn (default 8)\n"
                  "  TEA_AGENT_MAX_ACTIVE_TURNS turns that may run at once (default 24)\n"));
}

void open_browser(const std::string& url) {
#ifdef _WIN32
  ShellExecuteW(nullptr, L"open", utf8_to_wide(url).c_str(), nullptr, nullptr, SW_SHOWNORMAL);
#else
  const std::string cmd = "xdg-open '" + url + "' >/dev/null 2>&1 &";
  if (std::system(cmd.c_str()) != 0) {
    // Nothing sensible to do; the URL is printed anyway.
  }
#endif
}

/// How long the shutdown waits for the running turns to finish.
///
/// 0 means "do not wait" (the old behaviour: everything dies at once).
/// The default is 10 minutes: long enough for a turn that is nearly done,
/// short enough that a forgotten turn does not hold the service hostage.
/// The unit has to agree with it (TimeoutStopSec), otherwise systemd kills
/// the process in the middle of the wait.
int drain_seconds_from_env() {
  const char* raw = std::getenv("TEA_AGENT_DRAIN_SECONDS");
  if (!raw || !*raw) return 600;
  char* end = nullptr;
  const long parsed = std::strtol(raw, &end, 10);
  if (end == raw || parsed < 0 || parsed > 86400) {
    LOG_WARN("main: ignoring TEA_AGENT_DRAIN_SECONDS=", raw,
             " (needs an integer 0..86400)");
    return 600;
  }
  return static_cast<int>(parsed);
}

/// Look for a CA bundle file next to the binary, in the repository, or in the
/// agent home directory. Only consulted when the build embedded no bundle (or
/// when an operator wants to override the embedded one with --ca-bundle).
std::string find_ca_bundle(const std::string& tea_home) {
  std::vector<std::string> candidates;
  const std::string exe_dir = executable_dir();
  if (!exe_dir.empty()) {
    candidates.push_back(path_join(path_join(exe_dir, "ca-bundle"), "cacert.pem"));
    candidates.push_back(path_join(exe_dir, "cacert.pem"));
    candidates.push_back(path_join(path_join(exe_dir, ".."), "3rd/ca-bundle/cacert.pem"));
  }
  candidates.push_back("3rd/ca-bundle/cacert.pem");
  candidates.push_back(path_join(path_join(tea_home, "ca-bundle"), "cacert.pem"));
  for (std::size_t i = 0; i < candidates.size(); ++i) {
    if (path_is_file(candidates[i])) return path_absolute(candidates[i]);
  }
  return std::string();
}

}  // namespace

namespace tea {

/// Ask a running agent to stop (the Android service calls this when the
/// app goes away). The signal handlers do the same thing; this is the
/// programmatic version of the same two signals.
void agent_request_stop() {
  g_stop.store(true);
  g_stop_signals.fetch_add(1);
}

int tea_agent_main(int argc, char** argv) {
  attach_parent_console();
  Args args;
  for (int i = 1; i < argc; ++i) {
    std::string token = argv[i];
    if (token.size() > 2 && token.compare(0, 2, "--") == 0) {
      std::string name = token.substr(2);
      std::string value;
      const std::size_t eq = name.find('=');
      if (eq != std::string::npos) {
        value = name.substr(eq + 1);
        name = name.substr(0, eq);
      } else if (!is_boolean_flag(name) && i + 1 < argc) {
        const std::string next = argv[i + 1];
        if (!(next.size() > 2 && next.compare(0, 2, "--") == 0)) {
          value = next;
          ++i;
        }
      }
      if (value.empty() && is_boolean_flag(name)) value = "true";
      args.values[name].push_back(value);
    } else if (token == "-h") {
      args.values["help"].push_back("true");
    } else if (token == "-V") {
      args.values["version"].push_back("true");
    } else {
      console_write_line("warning: ignoring unexpected argument '" + token + "'");
    }
  }

  if (args.has("help")) {
    print_usage();
    return 0;
  }
  if (args.has("selftest")) {
    std::string report;
    const bool ok = output_filter_selftest(&report);
    console_write_line(report);
    return ok ? 0 : 1;
  }

  if (args.has("version")) {
    console_write_line(TEA_AGENT_NAME " " + std::string(tea::version_string()));
    return 0;
  }

  const std::string tea_home =
      args.get("tea-home", tea_home_dir());
  if (!make_dirs(tea_home)) {
    console_write_line("error: cannot create agent home directory: " + tea_home + " (" +
                       last_os_error() + ")");
    return 1;
  }

  // Can this process really write into the tea home? The user's home
  // directory is theirs - but a sandboxed (or low-integrity) process may read
  // it and still not be allowed to write there, and then the workspace index
  // and the global log fail later and silently. Find out now, say it out
  // loud, and offer the two ways out.
  std::string tea_home_error;
  const bool tea_home_writable = probe_dir_writable(tea_home, &tea_home_error);

  // Two thresholds on purpose: the log file keeps the full story (INFO by
  // default) while the terminal stays quiet unless something is actually wrong.
  // A server left running in a console tab should not scroll per-request INFO
  // lines past the operator; `--console-level info` brings them back.
  const bool console = !args.has("no-console") && !args.has("quiet");
  const std::string log_level_arg = args.get("log-level", "info");
  const std::string console_level_arg = args.get("console-level", "warn");
  const LogLevel level = log_level_from_string(log_level_arg);
  LogLevel console_level = log_level_from_string(console_level_arg);
  // log_level_from_string() is deliberately lenient (it never fails), so a typo
  // would silently widen the console back to INFO. Say so instead.
  if (!is_log_level_name(console_level_arg)) {
    console_write_line("warning: --console-level '" + console_level_arg +
                       "' is not a level; using warn");
    console_level = LogLevel::Warn;
  }
  if (!is_log_level_name(log_level_arg)) {
    console_write_line("warning: --log-level '" + log_level_arg + "' is not a level; using info");
  }
  const std::string log_file = args.get("log-file", path_join(path_join(tea_home, "logs"),
                                                              "tea_agent.log"));
  Logger::instance().configure(log_file, level, console);
  Logger::instance().set_console_level(console_level);

  LOG_INFO("=== ", TEA_AGENT_NAME, " ", std::string(tea::version_string()), " starting ===");
  LOG_INFO("console: ", console ? log_level_name(console_level) : "off", " (log file: ",
           log_level_name(level), ")");
  LOG_INFO("tea home: ", tea_home);
  LOG_INFO("log file: ", log_file);
  if (!tea_home_writable) {
    LOG_WARN("the tea home is not writable: ", tea_home, " (", tea_home_error, ")");
    console_write_line("warning: the tea home is not writable: " + tea_home + " (" +
                       tea_home_error + ")");
    console_write_line("         the workspace index and the global log cannot be written;");
    console_write_line("         point --tea-home at a writable directory, or start the agent");
    console_write_line("         with normal integrity (a low-integrity process may not write there).");
  }
  LOG_INFO("http engines available: ", http_available_engines());

  // ---- TLS ---------------------------------------------------------------
  TlsOptions tls;
  tls.verify_peer = !args.has("insecure");
  // Root resolution, in order: --ca-bundle (explicit) > embedded bundle >
  // a PEM found on disk. The embedded one is the default so that copying a
  // single ELF to a device is enough for https:// to work; the file search
  // remains for builds without a vendored bundle and for operators who want to
  // refresh the roots without rebuilding.
  tls.ca_bundle = args.get("ca-bundle");
  if (tls.ca_bundle.empty()) {
    const char* pem = embedded_ca_bundle_pem();
    if (pem != nullptr && embedded_ca_bundle_size() > 0) {
      tls.ca_pem.assign(pem, embedded_ca_bundle_size());
    } else {
      tls.ca_bundle = find_ca_bundle(tea_home);
    }
  }
  if (!tls.ca_bundle.empty()) tls.ca_bundle = path_absolute(tls.ca_bundle);
  http_set_tls_config(tls);
  if (!tls.verify_peer) {
    LOG_WARN("tls: certificate verification is DISABLED (--insecure)");
  } else if (!tls.ca_bundle.empty()) {
    LOG_INFO("tls: CA bundle ", tls.ca_bundle);
  } else if (!tls.ca_pem.empty()) {
    LOG_INFO("tls: CA bundle embedded in the binary (", tls.ca_pem.size(), " bytes)");
  } else {
    LOG_WARN(
        "tls: no CA bundle found; https:// through the bundled TLS backend will fail "
        "(pass --ca-bundle <pem> or --insecure)");
  }

  // ---- configuration -----------------------------------------------------
  AppConfig config;
  const std::string config_path = args.get("config", path_join(tea_home, "provider.yaml"));
  const bool config_ok = config.load(config_path, tea_home);
  if (!config_ok) {
    if (!config.file_missing) {
      // Malformed or unreadable config: fail loudly. Only a *missing* file is
      // recoverable, because the web UI can then ask for it.
      // The stamped ERROR line already reaches the console; only the hint is
      // extra (it is meant for a human, not for the log file).
      LOG_ERROR("configuration error: ", config.error);
      console_write_line("hint: create " + config_path +
                         " with a `providers:` mapping (see README.md)");
      return 1;
    }
    // Setup mode: serve the UI so it can upload provider.yaml, then reload.
    LOG_WARN("configuration missing: ", config.error);
    LOG_WARN("starting in setup mode: the web UI will ask for ", config_path);
    // The two WARN lines above already explain the situation (and reach the
    // console by default); here only the actionable hint for a human. Same URL
    // expansion as the banner: with `--host 0.0.0.0` the address to type is the
    // machine's, not its loopback.
    const std::string setup_port = args.get("port", "8383");
    const std::vector<std::string> setup_urls =
        reachable_urls(args.get("host", "127.0.0.1"), std::atoi(setup_port.c_str()));
    console_write_line("hint: open " +
                       (setup_urls.empty() ? "http://127.0.0.1:" + setup_port : setup_urls.front()) +
                       "/ to upload " + config_path);
  }
  if (config_ok) {
    std::string err;
    if (args.has("provider") || args.has("model")) {
      if (!config.set_active(args.get("provider"), args.get("model"), &err) && !err.empty()) {
        LOG_WARN("config: ", err);
      }
    }
  }
  if (args.has("allow-outside-workspace")) {
    config.settings.allow_outside_workspace = true;
    // A floor, not a default: Workspace::open re-applies it after the workspace
    // settings.yaml, so the web UI can tighten this but never loosen it, and the
    // switch reports itself as CLI-fixed instead of silently doing nothing.
    config.settings.force_allow_outside_workspace = true;
    config.cli_allow_outside_workspace = true;
  }

  // The fleet channel's endpoint. The settings files decide (tea_agent.yaml,
  // a workspace's settings.yaml); the environment overrides them, so a unit
  // file does not have to carry the URL.
  if (const char* env = std::getenv("TEA_AGENT_FLEET_URL")) {
    if (*env) config.settings.fleet_url = env;
  }
  if (const char* env = std::getenv("TEA_AGENT_FLEET_TOKEN")) {
    if (*env) config.settings.fleet_token = env;
  }
  if (!config.settings.fleet_url.empty()) {
    LOG_INFO("fleet: endpoint ", config.settings.fleet_url,
             (config.settings.fleet_token.empty() ? " (no token)" : " (with token)"));
  }

  // HTTP backend: the CLI flag wins, otherwise the settings files decide
  // (tea_agent.yaml, plus a workspace's settings.yaml). Persisting
  // `http_engine: socket` is the cure for a machine where WinHTTP cannot reach
  // the provider: it skips the probe instead of failing it once per process.
  if (args.has("http-engine")) {
    http_set_engine(args.get("http-engine"));
  } else if (!config.settings.http_engine.empty() && config.settings.http_engine != "auto") {
    http_set_engine(config.settings.http_engine);
    LOG_INFO("http: engine pinned to '", config.settings.http_engine, "' by the settings file");
  }

  // ---- workspaces --------------------------------------------------------
  WorkspaceRegistry registry;
  {
    std::string err;
    if (!registry.load(tea_home, &err)) {
      LOG_WARN("workspaces: ", err);
    }
  }

  std::vector<std::string> to_register = args.all("workspace");
  const std::string workspace_root = args.get("workspace-root");
  if (!workspace_root.empty()) {
    std::vector<tea::DirEntryInfo> entries;
    std::string err;
    if (list_directory(workspace_root, &entries, &err)) {
      for (std::size_t i = 0; i < entries.size(); ++i) {
        if (entries[i].is_dir) to_register.push_back(entries[i].path);
      }
    } else {
      LOG_WARN("workspace-root: ", err);
    }
  }
  for (std::size_t i = 0; i < to_register.size(); ++i) {
    std::string err;
    WorkspaceEntry* entry = registry.add(to_register[i], std::string(), &err);
    if (!entry) {
      LOG_WARN("workspace: ", err);
    } else {
      // Materialise .tea_agent_run/ immediately so the workspace has its
      // buffer/log/memory storage on disk even before the first request.
      Workspace ws;
      std::string open_err;
      if (!ws.open(*entry, config.settings, &open_err)) {
        LOG_WARN("workspace: ", open_err);
      }
      LOG_INFO("workspace registered: ", entry->id, " -> ", entry->path);
    }
  }
  registry.refresh_flags();

  // ---- history cleaner (--gc) -------------------------------------------
  // The history of a workspace only ever grows, so it needs a cleaner - and
  // the cleaner has to be reachable without the server: on a device the
  // service may not even be running, and "the disk is full" is not the moment
  // to start a server.
  if (args.has("gc")) {
    int64_t freed = 0;
    int touched = 0;
    for (std::size_t i = 0; i < registry.entries().size(); ++i) {
      const WorkspaceEntry& entry = registry.entries()[i];
      Workspace ws;
      std::string open_err;
      if (!ws.open(entry, config.settings, &open_err)) {
        console_write_line("skip " + entry.path + ": " + open_err);
        continue;
      }
      // The workspace's own settings.yaml decides; the command line wins.
      const Settings& ws_settings = ws.settings();
      HistoryGcLimits limits;
      limits.max_age_days = ws_settings.history_max_age_days;
      limits.max_bytes = ws_settings.history_max_bytes;
      limits.compress_after_days = ws_settings.history_compress_after_days;
      limits.keep_recent = ws_settings.history_keep_recent;
      if (args.has("max-age-days")) limits.max_age_days = std::atoi(args.get("max-age-days").c_str());
      if (args.has("max-bytes")) limits.max_bytes = std::atoll(args.get("max-bytes").c_str());
      if (args.has("compress-after-days"))
        limits.compress_after_days = std::atoi(args.get("compress-after-days").c_str());
      if (args.has("keep-recent")) limits.keep_recent = std::atoi(args.get("keep-recent").c_str());
      limits.dry_run = args.has("gc-dry-run");
      HistoryGcReport report;
      std::string err;
      if (!ws.gc_history(limits, &report, &err)) {
        console_write_line("skip " + entry.path + ": " + err);
        continue;
      }
      ++touched;
      freed += report.freed_bytes;
      console_write_line(
          entry.path + ": " + std::to_string(report.freed_bytes) + " byte(s) freed" +
          (report.dry_run ? " (dry run)" : "") + " - " +
          std::to_string(report.sessions_removed) + " session(s), " +
          std::to_string(report.trajectories_removed) + " trajectory file(s), " +
          std::to_string(report.sessions_compressed + report.trajectories_compressed) +
          " compressed");
      for (std::size_t k = 0; k < report.notes.size(); ++k) {
        console_write_line("  note: " + report.notes[k]);
      }
    }
    console_write_line("history cleaner: " + std::to_string(touched) + " workspace(s), " +
                       std::to_string(freed) + " byte(s) freed");
    return 0;
  }

  // ---- tools + server ----------------------------------------------------
  ToolRegistry tools;
  // The MCP servers of mcp.yaml. Their tools join the agent's tool
  // set, and the web UI shows whether they are up.
  McpRegistry mcp;
  mcp.configure(config.mcp);
  tools.set_mcp(&mcp);
  ServerOptions options;
  options.host = args.get("host", "127.0.0.1");
  options.web_dir = args.get("web-dir");
  // The fleet channel's secret. The flag wins; the environment is what a
  // systemd unit sets, so the token does not have to sit in a unit file.
  options.api_token = args.get("api-token");
  if (options.api_token.empty()) {
    const char* env = std::getenv("TEA_AGENT_API_TOKEN");
    if (env && *env) options.api_token = env;
  }
  try {
    options.port = std::stoi(args.get("port", "8383"));
  } catch (const std::exception&) {
    console_write_line("error: --port must be a number");
    return 1;
  }
  if (options.port <= 0 || options.port > 65535) {
    console_write_line("error: --port must be between 1 and 65535");
    return 1;
  }
  // A handover: the previous instance is still holding the port and is about to
  // let it go (the binary was rebuilt under it). Wait for it instead of
  // refusing to start - and wait longer than that instance's drain, or the
  // handover would give up while the old one is still finishing a turn.
  if (args.has("takeover")) options.wait_for_port_seconds = drain_seconds_from_env() + 60;

  // ---- the MQTT control channel (opt-in) --------------------------------
  // All four values are needed. A partial set is reported and the channel
  // stays off: a device that half-registers is worse than one that does not.
  {
    MqttSettings& mqtt = config.mqtt;
    mqtt.host = args.get("mqtt");
    mqtt.user = args.get("mqtt_user");
    if (mqtt.user.empty()) mqtt.user = args.get("mqtt-user");
    mqtt.password = args.get("mqtt_pass");
    if (mqtt.password.empty()) mqtt.password = args.get("mqtt-pass");
    mqtt.name = args.get("mqtt_name");
    if (mqtt.name.empty()) mqtt.name = args.get("mqtt-name");
    const std::string tunnel_broker = args.get("tunnel-broker");
    if (!tunnel_broker.empty()) mqtt.tunnel_broker = tunnel_broker;
    const std::string ca_bundle = args.get("ca-bundle");
    if (!ca_bundle.empty()) mqtt.ca_bundle = ca_bundle;

    const bool asked = !mqtt.host.empty() || !mqtt.user.empty() || !mqtt.password.empty() ||
                       !mqtt.name.empty();
    if (asked) {
      bool ok = true;
      const std::size_t colon = mqtt.host.rfind(':');
      if (colon == std::string::npos) {
        console_write_line("error: --mqtt needs HOST:PORT");
        ok = false;
      } else {
        const std::string host = mqtt.host.substr(0, colon);
        int port = 0;
        try {
          port = std::stoi(mqtt.host.substr(colon + 1));
        } catch (const std::exception&) {
          port = 0;
        }
        if (host.empty() || port <= 0 || port > 65535) {
          console_write_line("error: --mqtt needs HOST:PORT");
          ok = false;
        } else {
          mqtt.host = host;
          mqtt.port = port;
        }
      }
      if (mqtt.user.empty() || mqtt.password.empty() || mqtt.name.empty()) {
        console_write_line("error: --mqtt needs --mqtt_user, --mqtt_pass and --mqtt_name");
        ok = false;
      }
      for (std::size_t i = 0; ok && i < mqtt.name.size(); ++i) {
        const char c = mqtt.name[i];
        const bool good = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                          (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-';
        if (!good) {
          console_write_line("error: --mqtt_name may only use A-Z a-z 0-9 . _ -");
          ok = false;
        }
      }
      if (ok) {
        const std::string ip = tea::primary_ipv4();
        const std::string mac = tea::primary_mac();
        mqtt.ip = ip;
        mqtt.mac = mac;
        // The IPv4 address is only a label: two customer sites may both be
        // 192.168.1.5. The MAC keeps two devices apart, and it survives a
        // DHCP lease change.
        mqtt.client_name = mqtt.name + "@" + (ip.empty() ? std::string("unknown") : ip);
        if (!mac.empty()) mqtt.client_name += "-" + mac;
        mqtt.enabled = true;
        // The sidecar joins the same broker unless the operator says
        // otherwise: on the default deployment a2_broker is MQTT *and* the
        // tunnel rendezvous, on one port.
        if (mqtt.tunnel_broker.empty()) {
          mqtt.tunnel_broker = mqtt.host + ":" + std::to_string(mqtt.port);
        }
      }
    }
    // Deployment helper: print the name the broker account must use, then
    // exit. No broker connection, no side effects.
    if (args.has("print-registration") && mqtt.enabled) {
      console_write_line(mqtt.client_name);
      return 0;
    }
  }

  ApiServer server(&config, &tools, &registry, &mcp);
  std::string start_error;
  if (!server.start(options, &start_error)) {
    LOG_ERROR("server: ", start_error);   // also echoed to the console
    return 1;
  }

  // The MCP servers are started in the background: the handshake of a server
  // that is not there takes seconds, and the agent must not wait for it at
  // start-up. A turn that needs a tool starts the server itself.
  std::thread mcp_warmup([&mcp]() { mcp.warm_up(); });

  // ---- banner ------------------------------------------------------------
  {
    std::string banner;
    banner += "\n";
    banner += "  " TEA_AGENT_NAME " " + std::string(tea::version_string());
    // The commit comes from tea_agent.buildinfo next to the binary. A single-file
    // copy has no such file, and then there is no provenance to show - the
    // banner says so instead of embedding a hash in the binary.
    const std::string git_hash = tea::version_git_hash();
    if (!git_hash.empty()) banner += ", git hash: " + git_hash;
    banner += "\n";
    banner += "  ------------------------------------------------------------\n";
    // `--host 0.0.0.0` binds every interface, so printing 127.0.0.1 there is
    // useless: the user is starting it precisely so another device can open it.
    // Show the address that carries the default route first (that is the one a
    // phone on the same LAN types), then any other interface, then loopback.
    const std::vector<std::string> urls = server.urls();
    for (std::size_t i = 0; i < urls.size(); ++i) {
      banner += (i == 0 ? "  Web UI      : " : "                ") + urls[i];
      // Only label the loopback line when other addresses came first; with a
      // specific bind it is the only line and needs no explanation.
      if (urls.size() > 1 && urls[i].find("//127.0.0.1:") != std::string::npos) {
        banner += "   (this machine only)";
      }
      banner += "\n";
    }
    // Where the mutable state lives (the workspace index, the global log) -
    // and whether this process may write there at all.
    banner += "  Tea home    : " + tea_home;
    if (!tea_home_writable) banner += "   NOT WRITABLE: " + tea_home_error;
    banner += "\n";
    banner += "  Config      : " + config.path +
              (config.loaded ? std::string("\n")
                             : std::string("  (missing - upload it in the web UI)\n"));
    banner += "  Provider    : " +
              (config.loaded ? config.active_provider + " / " + config.active_model
                             : std::string("not configured yet")) +
              "\n";
    banner += "  HTTP engine : " + http_engine() + " (available: " + http_available_engines() +
              ")\n";
    // List what the active provider actually advertises: web tools are
    // provider-scoped, and showing the unfiltered set here misleads debugging.
    banner += "  Tools       : ";
    // The MCP tools are left out here: asking for them starts the MCP servers,
    // and the banner must not wait for a handshake that may never come.
    const nlohmann::json advertised =
        tools.openai_tools(config.active_provider_info(), &config.settings, &config, false);
    for (std::size_t i = 0; i < advertised.size(); ++i) {
      if (i) banner += ", ";
      banner += advertised[i]["function"]["name"].get<std::string>();
    }
    if (advertised.size() < tools.defs().size()) {
      banner += "  (" + std::to_string(tools.defs().size() - advertised.size()) +
                " provider-scoped tool(s) hidden for this provider)";
    }
    banner += "\n";
    // The independent search provider: the web tools can be served by another
    // provider than the one the chat runs on (the `search:` section).
    if (config.search.configured) {
      banner += "  Search      : " + config.search.provider;
      if (!config.search.error.empty()) {
        banner += " (unusable: " + config.search.error + ")";
      } else {
        banner += std::string(" (web tools, ") + (config.search.always ? "always" : "fallback") +
                  ")";
      }
      banner += "\n";
    }
    // The MCP servers of mcp.yaml: what they are and whether they answered.
    // A server that is not there is a line in the log, not a reason to refuse
    // to start. The file is named too - "why is nothing there" usually means
    // "the file is somewhere else".
    if (config.mcp.file_exists) {
      banner += "  MCP         : " + config.mcp_path + "\n";
      if (config.mcp.servers.empty()) {
        banner += "                (no servers)\n";
      }
      for (std::size_t i = 0; i < config.mcp.servers.size(); ++i) {
        const McpServerConfig& s = config.mcp.servers[i];
        banner += "                " + s.name;
        if (!s.enabled) {
          banner += " (disabled)";
        } else if (!s.error.empty()) {
          banner += " (unusable: " + s.error + ")";
        }
        banner += "\n";
      }
    }
    banner += "  Workspaces  : " + std::to_string(registry.entries().size()) + " registered (index: " +
              registry.index_path() + ")\n";
    for (std::size_t i = 0; i < registry.entries().size(); ++i) {
      const WorkspaceEntry& e = registry.entries()[i];
      banner += "                [" + e.id + "] " + e.name + "  " + e.path +
                (e.exists ? "" : "  (missing!)") + "\n";
    }
    banner += "  ------------------------------------------------------------\n";
    banner += "  Press Ctrl+C to stop.\n\n";
    if (!args.has("quiet")) console_write_line(banner);
  }
  // The banner is written straight to the console (not through the logger), so
  // --quiet has to skip it explicitly; it is a greeting, not a diagnostic.
  if (args.has("open")) open_browser(server.url());

  // ---- watch our own binary ----------------------------------------------
  //
  // A rebuild replaces the exe while this instance is running (the build moves
  // the old file aside so the linker can write a new one). The new build is
  // started, waits for the port, and this instance drains and exits - so
  // "compile and it takes effect" works on a machine that runs the agent
  // straight from the build tree. --no-reload-on-rebuild turns it off.
#ifdef _WIN32
  // (Windows only: on Linux the service manager restarts the agent, and the
  // helpers above - self_exe_path / file_stamp / spawn_takeover - do not
  // exist there. --no-reload-on-rebuild is still accepted, it just has
  // nothing to turn off.)
  if (!args.has("no-reload-on-rebuild")) {
    const std::string exe_path = self_exe_path();
    if (!exe_path.empty()) {
      const std::string stamp = file_stamp(exe_path);
      std::vector<std::string> own_args;
      for (int i = 0; i < argc; ++i) own_args.push_back(argv[i] ? argv[i] : "");
      std::thread watcher([exe_path, stamp, own_args]() {
        watch_own_binary(exe_path, stamp, own_args);
      });
      watcher.detach();
    }
  }
#endif

  install_signal_handlers();
  while (!g_stop.load()) {
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    if (!server.is_running()) {
      LOG_ERROR("server: the listener stopped unexpectedly");
      break;
    }
  }

  // ---- the shutdown drains first ----------------------------------------
  //
  // `systemctl --user restart a2` is how a new build gets picked up, and it
  // is usually run from another window: the person who asked for the restart
  // is often in the middle of a conversation with this very agent. Killing
  // that conversation on the spot loses the work - the turn is interrupted
  // mid-way and the answer never arrives.
  //
  // So the running turns are given a chance to finish first. New turns are
  // refused while the drain lasts (they would die with the process anyway),
  // and a second signal stops the wait.
  const int drain_seconds = drain_seconds_from_env();
  if (drain_seconds > 0) {
    server.begin_drain();
    int active = server.active_turns();
    if (active > 0) {
      LOG_INFO("shutdown: ", active, " turn(s) are running; they are given up to ",
               drain_seconds, " s to finish");
      if (!args.has("quiet")) {
        console_write_line("shutting down after the running turn(s) finish (" +
                           std::to_string(active) + " running, up to " +
                           std::to_string(drain_seconds) + " s)");
      }
      const int64_t deadline = now_ms() + static_cast<int64_t>(drain_seconds) * 1000;
      while (now_ms() < deadline) {
        if (g_stop_signals.load() > 1) {
          LOG_INFO("shutdown: second signal - not waiting any longer");
          break;
        }
        active = server.active_turns();
        if (active <= 0) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
      }
      active = server.active_turns();
      if (active > 0) {
        LOG_WARN("shutdown: ", active, " turn(s) did not finish in time; stopping anyway");
        if (!args.has("quiet")) {
          console_write_line("stopping now (" + std::to_string(active) +
                             " turn(s) did not finish in time)");
        }
      }
    }
  }

  LOG_INFO("shutting down");
  if (!args.has("quiet")) console_write_line("stopping...");
  server.stop();
  // The warm-up thread holds the MCP servers; it has to be finished before the
  // registry (and the servers) go away.
  if (mcp_warmup.joinable()) mcp_warmup.join();
  LOG_INFO("=== ", TEA_AGENT_NAME, " stopped ===");
  return 0;
}

}  // namespace tea

// The CLI entry point. The Android build compiles this out
// (TEA_AGENT_NO_MAIN): there the agent is a shared library and the APK
// calls tea_agent_main() from JNI.
#ifndef TEA_AGENT_NO_MAIN
#ifdef _WIN32
// The agent is a Windows application (CMakeLists: add_executable(... WIN32)):
// there is no `main`, and the command line the CRT offers in its place is the
// **ANSI** one - a `--log-file C:\用户\...` would arrive mangled. Take the wide
// command line, split it the way the shell does, and hand over UTF-8, which is
// what the rest of the program speaks.
int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
  int wide_count = 0;
  LPWSTR* wide = CommandLineToArgvW(GetCommandLineW(), &wide_count);
  std::vector<std::string> args;
  if (wide) {
    for (int i = 0; i < wide_count; ++i) args.push_back(wide_to_utf8(wide[i]));
    LocalFree(wide);
  }
  std::vector<char*> argv;
  argv.reserve(args.size() + 1);
  for (std::size_t i = 0; i < args.size(); ++i) {
    argv.push_back(const_cast<char*>(args[i].c_str()));
  }
  argv.push_back(nullptr);
  return tea::tea_agent_main(static_cast<int>(args.size()), argv.data());
}
#else
int main(int argc, char** argv) { return tea::tea_agent_main(argc, argv); }
#endif
#endif
