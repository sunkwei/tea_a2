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

#ifdef _WIN32
BOOL WINAPI console_handler(DWORD signal) {
  if (signal == CTRL_C_EVENT || signal == CTRL_BREAK_EVENT || signal == CTRL_CLOSE_EVENT) {
    g_stop.store(true);
    return TRUE;
  }
  return FALSE;
}
void install_signal_handlers() { SetConsoleCtrlHandler(console_handler, TRUE); }
#else
void posix_signal_handler(int) { g_stop.store(true); }
void install_signal_handlers() {
  std::signal(SIGINT, posix_signal_handler);
  std::signal(SIGTERM, posix_signal_handler);
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
                               "allow-outside-workspace", "no-color", "insecure"};

bool is_boolean_flag(const std::string& name) {
  for (std::size_t i = 0; i < sizeof(kBooleanFlags) / sizeof(kBooleanFlags[0]); ++i) {
    if (name == kBooleanFlags[i]) return true;
  }
  return false;
}

void print_usage() {
  console_write_line(
      std::string(TEA_AGENT_NAME " " TEA_AGENT_VERSION_STRING
                  " -- a minimal C++ LLM agent with tools, workspaces and a web UI.\n"
                  "\n"
                  "Usage: tea_agent [options]\n"
                  "\n"
                  "Server\n"
                  "  --host <addr>              bind address (default 127.0.0.1)\n"
                  "  --port <n>                 listen port (default 8282)\n"
                  "  --web-dir <path>           serve the UI from disk instead of the embedded copy\n"
                  "  --open                     open the web UI in the default browser\n"
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
                  "Workspaces\n"
                  "  --workspace <path>         register a workspace (repeatable)\n"
                  "  --workspace-root <dir>     register every immediate subdirectory of <dir>\n"
                  "  --allow-outside-workspace  let file tools touch paths outside the workspace\n"
                  "\n"
                  "Diagnostics\n"
                  "  --log-level <level>        trace|debug|info|warn|error|off (default info)\n"
                  "  --log-file <path>          global log file (default $HOME/.tea_agent/logs/tea_agent.log)\n"
                  "  --no-console               do not echo log lines to the console\n"
                  "  --help                     show this help\n"
                  "  --version                  show the version\n"
                  "  --selftest                 run built-in checks and exit\n"
                  "\n"
                  "Environment\n"
                  "  TEA_AGENT_HOME             overrides the agent home directory\n"
                  "  HTTP_PROXY / HTTPS_PROXY   used by the WinHTTP backend\n"));
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

int main(int argc, char** argv) {
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
    console_write_line(TEA_AGENT_NAME " " TEA_AGENT_VERSION_STRING);
    return 0;
  }

  const std::string tea_home =
      args.get("tea-home", tea_home_dir());
  if (!make_dirs(tea_home)) {
    console_write_line("error: cannot create agent home directory: " + tea_home + " (" +
                       last_os_error() + ")");
    return 1;
  }

  const LogLevel level = log_level_from_string(args.get("log-level", "info"));
  const bool console = !args.has("no-console");
  const std::string log_file = args.get("log-file", path_join(path_join(tea_home, "logs"),
                                                              "tea_agent.log"));
  Logger::instance().configure(log_file, level, console);

  LOG_INFO("=== ", TEA_AGENT_NAME, " ", TEA_AGENT_VERSION_STRING, " starting ===");
  LOG_INFO("tea home: ", tea_home);
  LOG_INFO("log file: ", log_file);
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
      LOG_ERROR("configuration error: ", config.error);
      console_write_line("error: " + config.error);
      console_write_line("hint: create " + config_path +
                         " with a `providers:` mapping (see README.md)");
      return 1;
    }
    // Setup mode: serve the UI so it can upload provider.yaml, then reload.
    LOG_WARN("configuration missing: ", config.error);
    LOG_WARN("starting in setup mode: the web UI will ask for ", config_path);
    console_write_line("warning: " + config.error);
    console_write_line("         starting in setup mode; upload the file at " +
                       std::string("http://") + args.get("host", "127.0.0.1") + ":" +
                       args.get("port", "8282") + "/");
  }
  if (config_ok) {
    std::string err;
    if (args.has("provider") || args.has("model")) {
      if (!config.set_active(args.get("provider"), args.get("model"), &err) && !err.empty()) {
        LOG_WARN("config: ", err);
      }
    }
  }
  if (args.has("allow-outside-workspace")) config.settings.allow_outside_workspace = true;

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

  // ---- tools + server ----------------------------------------------------
  ToolRegistry tools;
  ServerOptions options;
  options.host = args.get("host", "127.0.0.1");
  options.web_dir = args.get("web-dir");
  try {
    options.port = std::stoi(args.get("port", "8282"));
  } catch (const std::exception&) {
    console_write_line("error: --port must be a number");
    return 1;
  }
  if (options.port <= 0 || options.port > 65535) {
    console_write_line("error: --port must be between 1 and 65535");
    return 1;
  }

  ApiServer server(&config, &tools, &registry);
  std::string start_error;
  if (!server.start(options, &start_error)) {
    LOG_ERROR("server: ", start_error);
    console_write_line("error: " + start_error);
    return 1;
  }

  // ---- banner ------------------------------------------------------------
  {
    std::string banner;
    banner += "\n";
    banner += "  " TEA_AGENT_NAME " " TEA_AGENT_VERSION_STRING "\n";
    banner += "  ------------------------------------------------------------\n";
    banner += "  Web UI      : " + server.url() + "\n";
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
    const nlohmann::json advertised = tools.openai_tools(config.active_provider_info());
    for (std::size_t i = 0; i < advertised.size(); ++i) {
      if (i) banner += ", ";
      banner += advertised[i]["function"]["name"].get<std::string>();
    }
    if (advertised.size() < tools.defs().size()) {
      banner += "  (" + std::to_string(tools.defs().size() - advertised.size()) +
                " provider-scoped tool(s) hidden for this provider)";
    }
    banner += "\n";
    banner += "  Workspaces  : " + std::to_string(registry.entries().size()) + " registered (index: " +
              registry.index_path() + ")\n";
    for (std::size_t i = 0; i < registry.entries().size(); ++i) {
      const WorkspaceEntry& e = registry.entries()[i];
      banner += "                [" + e.id + "] " + e.name + "  " + e.path +
                (e.exists ? "" : "  (missing!)") + "\n";
    }
    banner += "  ------------------------------------------------------------\n";
    banner += "  Press Ctrl+C to stop.\n\n";
    console_write_line(banner);
  }
  if (args.has("open")) open_browser(server.url());

  install_signal_handlers();
  while (!g_stop.load()) {
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    if (!server.is_running()) {
      LOG_ERROR("server: the listener stopped unexpectedly");
      break;
    }
  }

  LOG_INFO("shutting down");
  console_write_line("stopping...");
  server.stop();
  LOG_INFO("=== ", TEA_AGENT_NAME, " stopped ===");
  return 0;
}
