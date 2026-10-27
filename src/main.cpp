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
                  "  --port <n>                 listen port (default 8282)\n"
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
                  "  --quiet                    same as --no-console\n"
                  "  --help                     show this help\n"
                  "  --version                  show the version\n"
                  "  --selftest                 run built-in checks and exit\n"
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
    const std::string setup_port = args.get("port", "8282");
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

  // ---- tools + server ----------------------------------------------------
  ToolRegistry tools;
  // The MCP servers of the `mcp:` section. Their tools join the agent's tool
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
    options.port = std::stoi(args.get("port", "8282"));
  } catch (const std::exception&) {
    console_write_line("error: --port must be a number");
    return 1;
  }
  if (options.port <= 0 || options.port > 65535) {
    console_write_line("error: --port must be between 1 and 65535");
    return 1;
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
    // The MCP servers of the `mcp:` section: what they are and whether they
    // answered. A server that is not there is a line in the log, not a reason
    // to refuse to start.
    if (config.mcp.configured) {
      banner += "  MCP         : ";
      bool first = true;
      for (std::size_t i = 0; i < config.mcp.servers.size(); ++i) {
        const McpServerConfig& s = config.mcp.servers[i];
        if (!first) banner += ", ";
        first = false;
        banner += s.name;
        if (!s.enabled) {
          banner += " (disabled)";
        } else if (!s.error.empty()) {
          banner += " (unusable: " + s.error + ")";
        }
      }
      if (first) banner += "(none)";
      banner += "\n";
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

  install_signal_handlers();
  while (!g_stop.load()) {
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    if (!server.is_running()) {
      LOG_ERROR("server: the listener stopped unexpectedly");
      break;
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
