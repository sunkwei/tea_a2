#include "server.h"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <map>
#include <mutex>
#include <thread>

#include "httplib.h"

#include "agent.h"
#include "file_dialog.h"
#include "http.h"
#include "log.h"
#include "trajectory.h"
#include "util.h"
#include "version.h"
#include "web_assets.h"

namespace tea {

namespace {

constexpr size_t kFileReadLimit = 2u * 1024u * 1024u;

/// Images and PDFs are fetched verbatim by the browser, so the raw route needs a
/// much higher ceiling than the JSON `/file` view. 16 MB is generous for a
/// screenshot yet small enough that one request cannot exhaust the device.
constexpr uint64_t kRawReadLimit = 16u * 1024u * 1024u;

/// A session trajectory file is already bounded by `trajectory_max_bytes`
/// (8 MB by default); this is only a guard against an operator raising that cap
/// on a device that cannot serve it.
constexpr size_t kTrajectoryReadLimit = 32u * 1024u * 1024u;

/// Content type for the raw route. A fixed allow-list instead of sniffing: an
/// unknown extension is served as opaque bytes, never as HTML, so a crafted file
/// cannot execute script in the UI's origin.
const char* raw_mime_for(const std::string& file_path) {
  const std::string ext = path_extension(file_path);  // lower-case, includes the dot
  if (ext == ".png") return "image/png";
  if (ext == ".jpg" || ext == ".jpeg") return "image/jpeg";
  if (ext == ".gif") return "image/gif";
  if (ext == ".webp") return "image/webp";
  if (ext == ".svg") return "image/svg+xml";
  if (ext == ".bmp") return "image/bmp";
  if (ext == ".ico") return "image/x-icon";
  if (ext == ".pdf") return "application/pdf";
  if (ext == ".txt") return "text/plain; charset=utf-8";
  if (ext == ".md" || ext == ".markdown") return "text/markdown; charset=utf-8";
  return "application/octet-stream";
}

/// Limits for pasted images. A screenshot is well under 1 MB; the caps exist so
/// one request cannot exhaust memory or fill the artifacts directory.
constexpr std::size_t kMaxImagesPerMessage = 4;
constexpr std::size_t kMaxImageBytes = 8u * 1024u * 1024u;

/// Identify the image by its magic bytes rather than trusting the client's
/// declared MIME type: the value ends up in a data: URL that the provider
/// parses, and the extension decides how /raw serves the file back.
const char* sniff_image_mime(const std::string& bytes) {
  const unsigned char* b = reinterpret_cast<const unsigned char*>(bytes.data());
  const std::size_t n = bytes.size();
  if (n >= 8 && b[0] == 0x89 && b[1] == 'P' && b[2] == 'N' && b[3] == 'G') return "image/png";
  if (n >= 3 && b[0] == 0xFF && b[1] == 0xD8 && b[2] == 0xFF) return "image/jpeg";
  if (n >= 6 && bytes.compare(0, 4, "GIF8") == 0) return "image/gif";
  if (n >= 12 && bytes.compare(0, 4, "RIFF") == 0 && bytes.compare(8, 4, "WEBP") == 0) {
    return "image/webp";
  }
  return nullptr;
}

std::string image_extension_for(const std::string& mime) {
  if (mime == "image/png") return ".png";
  if (mime == "image/jpeg") return ".jpg";
  if (mime == "image/gif") return ".gif";
  if (mime == "image/webp") return ".webp";
  return ".bin";
}

/// Split a `data:<mime>;base64,<payload>` URL. Also accepts a bare base64
/// payload (the UI always sends the full URL, but curl users often do not).
bool decode_image_payload(const std::string& raw, std::string* bytes, std::string* err) {
  std::string payload = trim(raw);
  const std::string marker = ";base64,";
  if (starts_with(payload, "data:")) {
    const std::size_t at = payload.find(marker);
    if (at == std::string::npos) {
      *err = "image data URL must be base64-encoded (expected ';base64,')";
      return false;
    }
    payload = payload.substr(at + marker.size());
  }
  if (payload.empty()) {
    *err = "image payload is empty";
    return false;
  }
  if (!base64_decode(payload, bytes)) {
    *err = "image payload is not valid base64";
    return false;
  }
  return true;
}

void send_json(httplib::Response& res, const nlohmann::json& body, int status = 200) {
  res.status = status;
  res.set_header("Access-Control-Allow-Origin", "*");
  res.set_header("Cache-Control", "no-store");
  res.set_content(body.dump(), "application/json; charset=utf-8");
}

void send_error(httplib::Response& res, int status, const std::string& code,
                const std::string& message) {
  nlohmann::json body = nlohmann::json::object();
  nlohmann::json error = nlohmann::json::object();
  error["code"] = code;
  error["message"] = message;
  body["error"] = error;
  send_json(res, body, status);
}

void send_ok(httplib::Response& res) {
  nlohmann::json body = nlohmann::json::object();
  body["ok"] = true;
  send_json(res, body);
}

bool parse_json_body(const httplib::Request& req, nlohmann::json* out, httplib::Response& res) {
  if (req.body.empty()) {
    *out = nlohmann::json::object();
    return true;
  }
  try {
    *out = nlohmann::json::parse(req.body);
  } catch (const std::exception& e) {
    send_error(res, 400, "bad_json", std::string("request body is not valid JSON: ") + e.what());
    return false;
  }
  if (!out->is_object()) {
    send_error(res, 400, "bad_json", "request body must be a JSON object");
    return false;
  }
  return true;
}

std::string json_string(const nlohmann::json& j, const char* key, const std::string& def = "") {
  if (!j.is_object()) return def;
  auto it = j.find(key);
  if (it == j.end() || !it->is_string()) return def;
  return it->get<std::string>();
}

// ---------------------------------------------------------------------------
// One streaming chat turn, shared between the connection thread that pumps the
// SSE response and the worker thread that runs the agent loop.
// ---------------------------------------------------------------------------
struct ChatTurn {
  std::string session_id;
  std::string workspace_id;
  Workspace workspace;
  Session session;

  mutable std::mutex mu;
  std::condition_variable cv;
  std::deque<std::string> queue;
  bool closed = false;
  /// False once the worker has finished the loop and is winding down: after that
  /// a late `enqueue_prompt` must be refused, otherwise it would sit in the inbox
  /// of a turn that will never read it again.
  bool accepting = true;
  std::atomic<bool> cancel{false};
  /// Prompts the user typed while this turn is running. Adopted one per agent
  /// iteration; see prompt_queue.h.
  PromptInbox inbox;
  std::thread worker;

  ~ChatTurn() {
    if (worker.joinable()) worker.join();
  }

  void push(std::string chunk) {
    {
      std::lock_guard<std::mutex> lock(mu);
      queue.push_back(std::move(chunk));
    }
    cv.notify_all();
  }

  void emit_event(const nlohmann::json& event) { push("data: " + event.dump() + "\n\n"); }

  void close() {
    {
      std::lock_guard<std::mutex> lock(mu);
      closed = true;
    }
    cv.notify_all();
  }

  bool finished() const {
    std::lock_guard<std::mutex> lock(mu);
    return closed;
  }

  /// Queue one prompt. Returns false when the turn has already stopped accepting
  /// them (finished, or the worker is winding down). The check and the push share
  /// `mu` with close()/begin_shutdown(), so a prompt cannot slip into a turn that
  /// nobody will ever drain.
  bool enqueue_prompt(const QueuedPrompt& prompt) {
    std::lock_guard<std::mutex> lock(mu);
    if (!accepting) return false;
    inbox.push(prompt);
    return true;
  }

  /// Called by the worker after run_turn returned, before the final drain: from
  /// here on new prompts are refused and the drain below is authoritative.
  void begin_shutdown() {
    std::lock_guard<std::mutex> lock(mu);
    accepting = false;
  }

  /// Called repeatedly by httplib. Returns false to end the response.
  bool pump(httplib::DataSink& sink) {
    std::unique_lock<std::mutex> lock(mu);
    while (queue.empty() && !closed) {
      if (cv.wait_for(lock, std::chrono::seconds(15)) == std::cv_status::timeout) {
        if (!queue.empty() || closed) break;
        lock.unlock();
        if (!sink.is_writable()) {
          cancel.store(true);
          return false;
        }
        char heartbeat[80];
        std::snprintf(heartbeat, sizeof(heartbeat), "data: {\"type\":\"ping\",\"t\":%lld}\n\n",
                      static_cast<long long>(now_ms() / 1000));
        if (!sink.write(heartbeat, std::strlen(heartbeat))) {
          cancel.store(true);
          return false;
        }
        lock.lock();
      }
    }
    if (queue.empty()) {
      // The turn is finished and everything has been flushed. A chunked body is
      // only terminated by done(): returning false here is treated as a
      // cancellation and aborts the transfer *without* the terminating
      // "0\r\n\r\n" chunk, which browsers report as "Error in input stream"
      // (curl silently tolerates it).
      sink.done();
      return true;
    }
    const std::string chunk = queue.front();
    queue.pop_front();
    lock.unlock();
    if (!sink.write(chunk.data(), chunk.size())) {
      cancel.store(true);
      return false;
    }
    return true;
  }
};

const WebAsset* find_embedded_asset(const std::string& path) {
  for (std::size_t i = 0; i < kWebAssetCount; ++i) {
    if (path == kWebAssets[i].path) return &kWebAssets[i];
  }
  return nullptr;
}

}  // namespace

// ---------------------------------------------------------------------------
// Impl
// ---------------------------------------------------------------------------
struct ApiServer::Impl {
  AppConfig* config = nullptr;
  ToolRegistry* tools = nullptr;
  WorkspaceRegistry* registry = nullptr;

  httplib::Server server;
  std::thread listen_thread;
  std::atomic<bool> listen_returned{false};
  bool listen_ok = false;
  int64_t started_ms = 0;
  std::string web_dir;

  std::mutex mu;  // guards registry access and `turns`
  std::map<std::string, std::shared_ptr<ChatTurn>> turns;
  /// Turns displaced by a newer turn on the same session. ~ChatTurn joins its
  /// worker, so these are kept alive here and only destroyed outside `mu`:
  /// joining under the lock would freeze every other request for as long as
  /// the old worker is still stuck (e.g. in a long tool call).
  std::vector<std::shared_ptr<ChatTurn>> retired;
  // ---- helpers -----------------------------------------------------------
  void install_routes();

  bool open_workspace(const std::string& id, Workspace* out, std::string* err) {
    WorkspaceEntry copy;
    {
      std::lock_guard<std::mutex> lock(mu);
      const WorkspaceEntry* entry = registry->find(id);
      if (!entry) {
        *err = "workspace not found: " + id;
        return false;
      }
      copy = *entry;
    }
    return out->open(copy, config->settings, err);
  }

  bool find_session_workspace(const std::string& session_id, Workspace* out, std::string* err) {
    WorkspaceEntry copy;
    bool found = false;
    {
      std::lock_guard<std::mutex> lock(mu);
      for (std::size_t i = 0; i < registry->entries().size(); ++i) {
        const WorkspaceEntry& e = registry->entries()[i];
        const std::string file = path_join(path_join(path_join(e.path, ".tea_agent_run"), "sessions"),
                                           session_id + ".json");
        if (path_exists(file)) {
          copy = e;
          found = true;
          break;
        }
      }
    }
    if (!found) {
      *err = "session not found: " + session_id;
      return false;
    }
    return out->open(copy, config->settings, err);
  }

  /// With `mu` held: move finished turns into `graveyard`. The caller destroys
  /// them after unlocking, because the destructor joins the worker thread.
  void prune_turns_locked(std::vector<std::shared_ptr<ChatTurn>>* graveyard) {
    for (auto it = turns.begin(); it != turns.end();) {
      if (it->second && it->second->finished()) {
        graveyard->push_back(it->second);
        it = turns.erase(it);
      } else {
        ++it;
      }
    }
    for (auto it = retired.begin(); it != retired.end();) {
      if (!*it || (*it)->finished()) {
        if (*it) graveyard->push_back(*it);
        it = retired.erase(it);
      } else {
        ++it;
      }
    }
  }

  bool serve_asset(const std::string& relative, httplib::Response& res) {
    // 1. explicit --web-dir override (handy while developing the UI)
    if (!web_dir.empty()) {
      const std::string file = path_join(web_dir, relative);
      if (path_is_file(file)) {
        std::string content;
        bool truncated = false;
        if (read_file(file, &content, 0, &truncated)) {
          const WebAsset* asset = find_embedded_asset(relative);
          res.set_content(content, asset ? asset->mime : "application/octet-stream");
          res.set_header("Cache-Control", "no-store");
          return true;
        }
      }
    }
    // 2. assets embedded at build time
    const WebAsset* asset = find_embedded_asset(relative);
    if (!asset) return false;
    // kAssetData_N is NUL-terminated, so never trust a size that runs past the
    // terminator: a build where they disagree used to append the next asset to
    // app.js, which is a JS syntax error and left the UI stuck on 连接中.
    std::size_t n = asset->size;
    const std::size_t term = std::strlen(asset->data);
    if (n > term) n = term;
    res.set_content(std::string(asset->data, n), asset->mime);
    res.set_header("Cache-Control", "no-store");
    return true;
  }
};

void ApiServer::Impl::install_routes() {
  httplib::Server& svr = server;

  svr.set_payload_max_length(64ull * 1024ull * 1024ull);
  svr.set_read_timeout(60, 0);
  svr.set_write_timeout(600, 0);
  svr.set_idle_interval(0, 200000);
  svr.set_keep_alive_max_count(1024);
  svr.set_tcp_nodelay(true);
  svr.new_task_queue = [] { return new httplib::ThreadPool(32); };

  // ---- static ------------------------------------------------------------
  svr.Get("/", [this](const httplib::Request&, httplib::Response& res) {
    if (!serve_asset("index.html", res)) {
      send_error(res, 500, "no_assets", "the embedded web UI is missing");
    }
  });
  svr.Get(R"(/([A-Za-z0-9_\-\.]+))", [this](const httplib::Request& req, httplib::Response& res) {
    const std::string name = req.matches[1].str();
    if (name == "favicon.ico") {
      res.status = 204;
      return;
    }
    if (!serve_asset(name, res)) {
      send_error(res, 404, "not_found", "no such asset: " + name);
    }
  });

  // ---- CORS preflight ----------------------------------------------------
  svr.Options(R"(/.*)", [](const httplib::Request&, httplib::Response& res) {
    res.status = 204;
    res.set_header("Access-Control-Allow-Origin", "*");
    res.set_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS");
    res.set_header("Access-Control-Allow-Headers", "Content-Type, Authorization");
  });

  // ---- meta --------------------------------------------------------------
  svr.Get("/api/health", [this](const httplib::Request&, httplib::Response& res) {
    nlohmann::json j = nlohmann::json::object();
    j["ok"] = true;
    j["version"] = TEA_AGENT_VERSION_STRING;
    j["uptime_s"] = static_cast<double>(now_ms() - started_ms) / 1000.0;
    j["provider"] = config->active_provider;
    j["model"] = config->active_model;
    j["http_engine"] = http_engine();
    // Hosts where WinHTTP was given up on and the bundled TLS backend took over
    // (Windows only, empty otherwise). Makes the fallback visible from outside
    // instead of only in the log.
    const std::vector<std::string> fallback = http_socket_fallback_hosts();
    if (!fallback.empty()) j["http_socket_fallback"] = fallback;
    // The UI hides the "browse" button when the server has no desktop to draw
    // a dialog on (headless box, no zenity/kdialog, no override).
    j["dir_picker"] = directory_picker_available();
    // The UI blocks on this: without a config there is nothing to chat with.
    j["config_loaded"] = config->loaded;
    if (!config->loaded) j["config_error"] = config->error;
    send_json(res, j);
  });

  // ---- first-run configuration -------------------------------------------
  // A missing provider.yaml is not fatal any more: the server comes up in setup
  // mode, the UI uploads the file, and the config is reloaded in place.
  svr.Get("/api/config/status", [this](const httplib::Request&, httplib::Response& res) {
    nlohmann::json j = nlohmann::json::object();
    j["loaded"] = config->loaded;
    j["path"] = config->path;
    j["tea_home"] = config->tea_home;
    if (!config->loaded) {
      j["needs_setup"] = config->file_missing;
      j["error"] = config->error;
    }
    j["providers"] = config->provider_order;
    send_json(res, j);
  });

  svr.Post("/api/config", [this](const httplib::Request& req, httplib::Response& res) {
    // Body is the YAML text itself, or {"yaml": "..."} for JSON-only clients.
    // Decide by content rather than Content-Type: a browser File upload and a
    // curl --data-binary send the raw text, while the bundled UI wraps it.
    std::string yaml_text = req.body;
    if (!trim(req.body).empty() && trim(req.body)[0] == '{') {
      nlohmann::json body = nlohmann::json::parse(req.body, nullptr, false);
      if (body.is_object()) {
        const std::string wrapped = json_string(body, "yaml");
        if (!wrapped.empty()) yaml_text = wrapped;
        else {
          const std::string alt = json_string(body, "content");
          if (!alt.empty()) yaml_text = alt;
        }
      }
      // No wrapper key: the body is a YAML flow mapping, keep it verbatim.
    }
    if (trim(yaml_text).empty()) {
      send_error(res, 400, "bad_request", "the request body is empty; send provider.yaml");
      return;
    }
    if (yaml_text.size() > 4u * 1024u * 1024u) {
      send_error(res, 413, "too_large", "provider.yaml must be smaller than 4 MB");
      return;
    }

    // Reject the upload while a turn is running: reloading the config in place
    // would pull the provider out from under the agent thread.
    {
      std::lock_guard<std::mutex> lock(mu);
      for (auto it = turns.begin(); it != turns.end(); ++it) {
        if (it->second && !it->second->finished()) {
          send_error(res, 409, "turn_running",
                     "a chat turn is still running; stop it before replacing the config");
          return;
        }
      }
    }

    const std::string dir = path_parent(config->path);
    if (!dir.empty() && !make_dirs(dir)) {
      send_error(res, 500, "io_error", "cannot create " + dir);
      return;
    }
    // Validate into a scratch config first, so a bad upload never overwrites a
    // working file (and never leaves the server without a config).
    const std::string probe_path = config->path + ".upload.tmp";
    std::string io_err;
    if (!write_file(probe_path, yaml_text, false, &io_err)) {
      send_error(res, 500, "io_error", "cannot write " + probe_path + ": " + io_err);
      return;
    }
    AppConfig probe;
    if (!probe.load(probe_path, config->tea_home)) {
      remove_path(probe_path, false, nullptr);
      send_error(res, 400, "invalid_config", probe.error);
      return;
    }
    if (probe.providers.empty()) {
      remove_path(probe_path, false, nullptr);
      send_error(res, 400, "invalid_config",
                 "the file parsed, but declares no usable provider (needs a `providers:` "
                 "mapping with at least one api_url)");
      return;
    }
    if (!move_path(probe_path, config->path, &io_err)) {
      remove_path(probe_path, false, nullptr);
      send_error(res, 500, "io_error", "cannot replace " + config->path + ": " + io_err);
      return;
    }
    if (!config->load(config->path, config->tea_home)) {
      send_error(res, 500, "reload_failed", config->error);
      return;
    }
    LOG_INFO("config: provider.yaml replaced via the web UI (", config->providers.size(),
             " provider(s), active=", config->active_provider, "/", config->active_model, ")");
    nlohmann::json j = nlohmann::json::object();
    j["ok"] = true;
    j["path"] = config->path;
    j["providers"] = config->provider_order;
    j["active"] = { {"provider", config->active_provider}, {"model", config->active_model} };
    send_json(res, j);
  });

  svr.Get("/api/providers", [this](const httplib::Request&, httplib::Response& res) {
    nlohmann::json j = nlohmann::json::object();
    j["config_path"] = config->path;
    if (!config->extra_path.empty() && path_exists(config->extra_path)) {
      j["extra_config_path"] = config->extra_path;
    }
    nlohmann::json active = nlohmann::json::object();
    active["provider"] = config->active_provider;
    active["model"] = config->active_model;
    // The UI divides prompt_tokens by the window to show "上下文 12%", so the
    // window has to be reachable without the client guessing it.
    const ModelInfo* active_info = config->active_model_info();
    active["max_context_tokens"] = active_info ? active_info->max_context_tokens : 0;
    j["active"] = active;

    nlohmann::json list = nlohmann::json::array();
    for (std::size_t i = 0; i < config->provider_order.size(); ++i) {
      const ProviderInfo* p = config->provider(config->provider_order[i]);
      if (!p) continue;
      nlohmann::json pj = nlohmann::json::object();
      pj["name"] = p->name;
      pj["api_url"] = p->api_url;
      pj["default_model"] = p->default_model;
      pj["models"] = p->models;
      // `models` stays a plain array of names (existing clients read it that
      // way); per-model capabilities live in a parallel map keyed by model name.
      nlohmann::json model_info = nlohmann::json::object();
      for (std::size_t m = 0; m < p->models.size(); ++m) {
        const ModelInfo* info = p->find_model(p->models[m]);
        nlohmann::json mi = nlohmann::json::object();
        mi["max_context_tokens"] = info ? info->max_context_tokens : 0;
        if (info) {
          mi["max_output_tokens"] = info->max_output_tokens;
          mi["supports_tools"] = info->supports_tools;
          mi["supports_reasoning"] = info->supports_reasoning;
          mi["supports_vision"] = info->supports_vision;
        }
        model_info[p->models[m]] = mi;
      }
      pj["model_info"] = model_info;
      pj["has_key"] = p->has_key();  // never expose the key itself
      pj["supports_search"] = p->supports_search;
      pj["supports_fetch"] = p->supports_fetch;
      if (!p->description.empty()) pj["description"] = p->description;
      list.push_back(pj);
    }
    j["providers"] = list;

    nlohmann::json settings = nlohmann::json::object();
    settings["max_iterations"] = config->settings.max_iterations;
    settings["max_tool_output"] = config->settings.max_tool_output;
    settings["command_timeout_ms"] = config->settings.command_timeout_ms;
    settings["allow_outside_workspace"] = config->settings.allow_outside_workspace;
    j["settings"] = settings;
    j["warnings"] = config->warnings;
    send_json(res, j);
  });

  svr.Get("/api/tools", [this](const httplib::Request&, httplib::Response& res) {
    nlohmann::json list = nlohmann::json::array();
    const std::vector<ToolDef>& defs = tools->defs();
    for (std::size_t i = 0; i < defs.size(); ++i) {
      nlohmann::json t = nlohmann::json::object();
      t["name"] = defs[i].name;
      t["description"] = defs[i].description;
      t["parameters"] = defs[i].parameters;
      list.push_back(t);
    }
    nlohmann::json j = nlohmann::json::object();
    j["tools"] = list;
    send_json(res, j);
  });

  // ---- workspaces --------------------------------------------------------
  svr.Get("/api/workspaces", [this](const httplib::Request&, httplib::Response& res) {
    nlohmann::json list = nlohmann::json::array();
    {
      std::lock_guard<std::mutex> lock(mu);
      registry->refresh_flags();
      for (std::size_t i = 0; i < registry->entries().size(); ++i) {
        list.push_back(registry->entries()[i].to_json());
      }
    }
    nlohmann::json j = nlohmann::json::object();
    j["index_path"] = registry->index_path();
    j["tea_home"] = config->tea_home;
    j["workspaces"] = list;
    send_json(res, j);
  });

  // ---- native directory picker -------------------------------------------
  // The browser cannot return an absolute path (showDirectoryPicker gives a
  // handle; <input webkitdirectory> gives relative names), so the dialog is
  // opened on the server's desktop and the path comes back over HTTP. The
  // request blocks until the dialog closes; httplib's pool has enough workers
  // that one open dialog does not stall the rest of the API.
  svr.Post("/api/dialog/directory", [this](const httplib::Request& req, httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;
    std::string initial = json_string(body, "initial");
    if (!initial.empty()) {
      // Never hand the picker a path that escaped the workspace sandbox by
      // accident: an unreadable start directory just makes the dialog open at
      // the default location.
      if (!path_is_dir(initial)) initial.clear();
    }
    const int timeout_ms = 300000;  // 5 minutes: people read their filesystem
    const DirectoryPickResult pick = pick_directory(initial, "Select a project directory", timeout_ms);
    nlohmann::json j = nlohmann::json::object();
    j["tool"] = pick.tool;
    if (pick.ok) {
      LOG_INFO("dialog: directory selected via ", pick.tool, ": ", pick.path);
      j["ok"] = true;
      j["path"] = pick.path;
      send_json(res, j);
      return;
    }
    if (pick.cancelled) {
      j["ok"] = false;
      j["cancelled"] = true;
      send_json(res, j);
      return;
    }
    LOG_WARN("dialog: ", pick.tool, " failed: ", pick.error);
    j["ok"] = false;
    j["error"] = pick.error;
    if (pick.unavailable) {
      j["unavailable"] = true;
      send_json(res, j, 501);
      return;
    }
    send_json(res, j, 500);
  });

  svr.Post("/api/workspaces", [this](const httplib::Request& req, httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;
    const std::string path = json_string(body, "path");
    if (path.empty()) {
      send_error(res, 400, "bad_request", "'path' is required");
      return;
    }
    // `create: true` lets the UI offer "the directory does not exist, create
    // it?" instead of dead-ending on a typo-free new project path.
    nlohmann::json::const_iterator create_it = body.find("create");
    if (create_it != body.end() && create_it->is_boolean() && create_it->get<bool>()) {
      const std::string expanded = path_absolute(expand_user(trim(path)));
      if (!path_exists(expanded)) {
        if (!make_dirs(expanded)) {
          send_error(res, 400, "workspace_error", "cannot create directory " + expanded);
          return;
        }
        LOG_INFO("workspace: created directory ", expanded);
      }
    }
    std::string err;
    WorkspaceEntry* entry = nullptr;
    {
      std::lock_guard<std::mutex> lock(mu);
      entry = registry->add(path, json_string(body, "name"), &err);
      if (!entry) {
        send_error(res, 400, "workspace_error", err);
        return;
      }
      registry->refresh_flags();
      // Materialise .tea_agent_run/ right away, like the CLI does.
      Workspace ws;
      std::string open_err;
      if (!ws.open(*entry, config->settings, &open_err)) {
        LOG_WARN("workspace: ", open_err);
      }
      send_json(res, entry->to_json());
    }
  });

  svr.Delete(R"(/api/workspaces/([^/]+))",
             [this](const httplib::Request& req, httplib::Response& res) {
               const std::string id = req.matches[1].str();
               std::string err;
               {
                 std::lock_guard<std::mutex> lock(mu);
                 if (!registry->remove(id, &err)) {
                   send_error(res, 404, "not_found", err);
                   return;
                 }
               }
               send_ok(res);
             });

  svr.Get(R"(/api/workspaces/([^/]+)/tree)",
          [this](const httplib::Request& req, httplib::Response& res) {
            const std::string id = req.matches[1].str();
            Workspace ws;
            std::string err;
            if (!open_workspace(id, &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            const std::string rel = req.has_param("path") ? req.get_param_value("path") : "";
            const std::string target = rel.empty() ? ws.root() : ws.resolve_path(rel, &err);
            if (target.empty()) {
              send_error(res, 400, "bad_path", err);
              return;
            }
            if (!path_is_dir(target)) {
              send_error(res, 400, "not_a_directory", "not a directory: " + rel);
              return;
            }
            std::vector<DirEntryInfo> entries;
            if (!list_directory(target, &entries, &err)) {
              send_error(res, 400, "list_failed", err);
              return;
            }
            nlohmann::json list = nlohmann::json::array();
            for (std::size_t i = 0; i < entries.size(); ++i) {
              nlohmann::json e = nlohmann::json::object();
              e["name"] = entries[i].name;
              e["path"] = ws.display_path(entries[i].path);
              e["type"] = entries[i].is_dir ? "dir" : "file";
              e["size"] = entries[i].size;
              e["mtime"] = entries[i].mtime;
              list.push_back(e);
            }
            nlohmann::json j = nlohmann::json::object();
            j["path"] = (target == ws.root()) ? std::string() : ws.display_path(target);
            j["root"] = ws.root();
            j["entries"] = list;
            send_json(res, j);
          });

  svr.Get(R"(/api/workspaces/([^/]+)/file)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            const std::string rel = req.has_param("path") ? req.get_param_value("path") : "";
            if (rel.empty()) {
              send_error(res, 400, "bad_request", "'path' is required");
              return;
            }
            const std::string target = ws.resolve_path(rel, &err);
            if (target.empty()) {
              send_error(res, 400, "bad_path", err);
              return;
            }
            if (!path_is_file(target)) {
              send_error(res, 404, "not_found",
                         path_exists(target) ? "not a regular file" : "file not found");
              return;
            }
            std::string content;
            bool truncated = false;
            if (!read_file(target, &content, kFileReadLimit, &truncated)) {
              send_error(res, 400, "read_failed", "cannot read " + rel);
              return;
            }
            nlohmann::json j = nlohmann::json::object();
            j["path"] = ws.display_path(target);
            j["size"] = file_size(target);
            j["truncated"] = truncated;
            j["content"] = content;
            send_json(res, j);
          });

  // ---- raw file bytes ----------------------------------------------------
  // Images/PDFs referenced from assistant markdown are loaded by the browser
  // directly, so they need a binary route; `/file` stays JSON-only.
  svr.Get(R"(/api/workspaces/([^/]+)/raw)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            const std::string rel = req.has_param("path") ? req.get_param_value("path") : "";
            if (rel.empty()) {
              send_error(res, 400, "bad_request", "'path' is required");
              return;
            }
            const std::string target = ws.resolve_path(rel, &err);
            if (target.empty()) {
              // resolve_path enforces the workspace boundary: ../.. is refused
              // here and never reaches the read below.
              send_error(res, 403, "forbidden", err);
              return;
            }
            if (!path_is_file(target)) {
              send_error(res, 404, "not_found",
                         path_exists(target) ? "not a regular file" : "file not found");
              return;
            }
            const uint64_t size = file_size(target);
            if (size > kRawReadLimit) {
              send_error(res, 413, "too_large",
                         "file is larger than 16 MB; use /file for a truncated view");
              return;
            }
            std::string bytes;
            if (!read_file(target, &bytes)) {
              send_error(res, 400, "read_failed", "cannot read " + rel);
              return;
            }
            res.set_header("Cache-Control", "no-store");
            res.set_header("Access-Control-Allow-Origin", "*");
            res.set_content(bytes, raw_mime_for(target));
          });

  svr.Put(R"(/api/workspaces/([^/]+)/file)",
          [this](const httplib::Request& req, httplib::Response& res) {
            nlohmann::json body;
            if (!parse_json_body(req, &body, res)) return;
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            const std::string rel = json_string(body, "path");
            if (rel.empty()) {
              send_error(res, 400, "bad_request", "'path' is required");
              return;
            }
            const std::string target = ws.resolve_path(rel, &err);
            if (target.empty()) {
              send_error(res, 400, "bad_path", err);
              return;
            }
            const std::string content = json_string(body, "content");
            if (!write_file(target, content, false, &err)) {
              send_error(res, 400, "write_failed", err);
              return;
            }
            nlohmann::json j = nlohmann::json::object();
            j["ok"] = true;
            j["bytes"] = static_cast<long long>(content.size());
            j["size"] = file_size(target);
            send_json(res, j);
          });

  // ---- prompt preview ----------------------------------------------------
  // Shows exactly the system prompt this workspace produces, which is what
  // operators need in order to reason about device probing and cache stability.
  svr.Get(R"(/api/workspaces/([^/]+)/prompt)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            Agent agent(config, tools);
            Session session;
            session.info.created_at = now_iso8601();
            const std::string prompt = agent.build_system_prompt(ws, session);
            nlohmann::json j = nlohmann::json::object();
            j["system_prompt"] = prompt;
            j["bytes"] = static_cast<long long>(prompt.size());
            j["estimated_tokens"] = Agent::estimate_tokens(prompt);
            j["settings_file"] = ws.settings_file();
            j["instructions_file"] = ws.instructions_file();
            j["has_instructions"] = !ws.instructions().empty();
            send_json(res, j);
          });
  // ---- sessions ----------------------------------------------------------
  svr.Get(R"(/api/workspaces/([^/]+)/sessions)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            const std::vector<SessionInfo> sessions = ws.list_sessions();
            nlohmann::json list = nlohmann::json::array();
            for (std::size_t i = 0; i < sessions.size(); ++i) list.push_back(sessions[i].to_json());
            nlohmann::json j = nlohmann::json::object();
            j["sessions"] = list;
            send_json(res, j);
          });

  svr.Post(R"(/api/workspaces/([^/]+)/sessions)",
           [this](const httplib::Request& req, httplib::Response& res) {
             nlohmann::json body;
             if (!parse_json_body(req, &body, res)) return;
             Workspace ws;
             std::string err;
             if (!open_workspace(req.matches[1].str(), &ws, &err)) {
               send_error(res, 404, "not_found", err);
               return;
             }
             Session session;
             if (!ws.create_session(json_string(body, "title"), &session, &err)) {
               send_error(res, 400, "session_error", err);
               return;
             }
             send_json(res, session.info.to_json());
           });

  svr.Get(R"(/api/sessions/([^/]+))",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!find_session_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            Session session;
            if (!ws.load_session(req.matches[1].str(), &session, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            nlohmann::json messages = nlohmann::json::array();
            for (std::size_t i = 0; i < session.messages.size(); ++i) {
              messages.push_back(session.messages[i].to_store());
            }
            nlohmann::json j = nlohmann::json::object();
            j["session"] = session.info.to_json();
            j["workspace_id"] = ws.id();
            j["messages"] = messages;
            send_json(res, j);
          });

  svr.Delete(R"(/api/sessions/([^/]+))",
             [this](const httplib::Request& req, httplib::Response& res) {
               Workspace ws;
               std::string err;
               if (!find_session_workspace(req.matches[1].str(), &ws, &err)) {
                 send_error(res, 404, "not_found", err);
                 return;
               }
               if (!ws.delete_session(req.matches[1].str(), &err)) {
                 send_error(res, 400, "session_error", err);
                 return;
               }
               send_ok(res);
             });

  // Renaming is what the sidebar's session index calls; the title is display-only.
  svr.Patch(R"(/api/sessions/([^/]+))",
            [this](const httplib::Request& req, httplib::Response& res) {
              nlohmann::json body;
              if (!parse_json_body(req, &body, res)) return;
              Workspace ws;
              std::string err;
              if (!find_session_workspace(req.matches[1].str(), &ws, &err)) {
                send_error(res, 404, "not_found", err);
                return;
              }
              const std::string title = json_string(body, "title");
              if (trim(title).empty()) {
                send_error(res, 400, "bad_request", "'title' is required");
                return;
              }
              if (!ws.rename_session(req.matches[1].str(), title, &err)) {
                send_error(res, 400, "session_error", err);
                return;
              }
              send_ok(res);
            });

  // ---- trajectories (the per-turn audit trail) ---------------------------
  // One JSONL file per session (`<run_dir>/trajectories/<session_id>.jsonl`,
  // one line per turn). The default view is the newest turn summaries first;
  // `?full=1` adds steps and file changes, `?format=jsonl` hands the raw bytes
  // to a controlling agent, and `/api/turns/<id>/trajectory` addresses exactly
  // the turn the SSE `done` event announced as `trajectory_id`.
  svr.Get(R"(/api/sessions/([^/]+)/trajectory)",
          [this](const httplib::Request& req, httplib::Response& res) {
            const std::string session_id = req.matches[1].str();
            // The id becomes a file name further down; refuse anything that is
            // not a plain id so a crafted path cannot escape the run directory.
            if (!trajectory_id_is_safe(session_id)) {
              send_error(res, 400, "bad_request", "invalid session id");
              return;
            }
            Workspace ws;
            std::string err;
            if (!find_session_workspace(session_id, &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            const std::string path = trajectory_file_path(ws.run_dir(), session_id);
            if (!path_is_file(path)) {
              send_error(res, 404, "not_found",
                         "no trajectory recorded for session " + session_id);
              return;
            }
            if (req.has_param("format") &&
                to_lower(req.get_param_value("format")) == "jsonl") {
              std::string bytes;
              if (!read_file(path, &bytes, kTrajectoryReadLimit)) {
                send_error(res, 400, "read_failed", "cannot read " + path);
                return;
              }
              res.status = 200;
              res.set_header("Access-Control-Allow-Origin", "*");
              res.set_header("Cache-Control", "no-store");
              res.set_content(bytes, "application/x-ndjson; charset=utf-8");
              return;
            }
            std::vector<nlohmann::json> turns;
            if (!load_trajectories(path, &turns, &err)) {
              send_error(res, 400, "trajectory_error", err);
              return;
            }
            const bool full = req.has_param("full") && req.get_param_value("full") != "0";
            nlohmann::json list = nlohmann::json::array();
            for (std::size_t i = 0; i < turns.size(); ++i) {
              list.push_back(full ? turns[i] : trajectory_summary(turns[i]));
            }
            send_json(res, list);
          });

  svr.Get(R"(/api/turns/([^/]+)/trajectory)",
          [this](const httplib::Request& req, httplib::Response& res) {
            const std::string turn_id = req.matches[1].str();
            if (!trajectory_id_is_safe(turn_id)) {
              send_error(res, 400, "bad_request", "invalid turn id");
              return;
            }
            // Turn ids are not indexed, so scan the registered workspaces'
            // session files and stop at the first hit (which is what a caller
            // polling right after `done` gets).
            std::vector<WorkspaceEntry> entries;
            {
              std::lock_guard<std::mutex> lock(mu);
              for (std::size_t i = 0; i < registry->entries().size(); ++i) {
                entries.push_back(registry->entries()[i]);
              }
            }
            for (std::size_t i = 0; i < entries.size(); ++i) {
              if (entries[i].path.empty() || !path_is_dir(entries[i].path)) continue;
              Workspace ws;
              std::string err;
              if (!ws.open(entries[i], config->settings, &err)) continue;
              const std::string dir = path_join(ws.run_dir(), "trajectories");
              if (!path_is_dir(dir)) continue;
              std::vector<DirEntryInfo> files;
              if (!list_directory(dir, &files, &err)) continue;
              for (std::size_t k = 0; k < files.size(); ++k) {
                if (files[k].is_dir) continue;
                if (path_extension(files[k].name) != ".jsonl") continue;
                std::vector<nlohmann::json> turns;
                if (!load_trajectories(files[k].path, &turns, &err)) continue;
                for (std::size_t t = 0; t < turns.size(); ++t) {
                  if (turns[t].value("turn_id", std::string()) != turn_id) continue;
                  send_json(res, turns[t]);
                  return;
                }
              }
            }
            send_error(res, 404, "not_found", "no trajectory recorded for turn " + turn_id);
          });

  // ---- memory ------------------------------------------------------------
  svr.Get(R"(/api/workspaces/([^/]+)/memory)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            const std::vector<nlohmann::json> notes = ws.load_memory(&err);
            nlohmann::json list = nlohmann::json::array();
            for (std::size_t i = 0; i < notes.size(); ++i) list.push_back(notes[i]);
            nlohmann::json j = nlohmann::json::object();
            j["notes"] = list;
            j["path"] = ws.memory_file();
            send_json(res, j);
          });

  svr.Post(R"(/api/workspaces/([^/]+)/memory)",
           [this](const httplib::Request& req, httplib::Response& res) {
             nlohmann::json body;
             if (!parse_json_body(req, &body, res)) return;
             Workspace ws;
             std::string err;
             if (!open_workspace(req.matches[1].str(), &ws, &err)) {
               send_error(res, 404, "not_found", err);
               return;
             }
             const nlohmann::json note = ws.add_memory_note(json_string(body, "text"), &err);
             if (note.is_null()) {
               send_error(res, 400, "bad_request", err);
               return;
             }
             send_json(res, note);
           });

  svr.Delete(R"(/api/workspaces/([^/]+)/memory/([^/]+))",
             [this](const httplib::Request& req, httplib::Response& res) {
               Workspace ws;
               std::string err;
               if (!open_workspace(req.matches[1].str(), &ws, &err)) {
                 send_error(res, 404, "not_found", err);
                 return;
               }
               if (!ws.delete_memory_note(req.matches[2].str(), &err)) {
                 send_error(res, 404, "not_found", err);
                 return;
               }
               send_ok(res);
             });

  // ---- agent.md (workspace experience log) -------------------------------
  // The file is injected into the system prompt on every turn and maintained by
  // the agent through toolkit_agent_doc; these routes let a human read and fix it
  // from the web UI without shelling into the device.
  svr.Get(R"(/api/workspaces/([^/]+)/agent-doc)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            bool truncated = false;
            const std::string content = ws.load_agent_doc(&truncated);
            std::vector<DirEntryInfo> scripts;
            list_directory(ws.scripts_dir(), &scripts, nullptr);
            nlohmann::json list = nlohmann::json::array();
            for (std::size_t i = 0; i < scripts.size(); ++i) {
              if (scripts[i].is_dir) continue;
              list.push_back({{"name", scripts[i].name},
                              {"size", static_cast<long long>(scripts[i].size)},
                              {"mtime", scripts[i].mtime}});
            }
            nlohmann::json j = nlohmann::json::object();
            j["path"] = ws.agent_doc_file();
            j["relative_path"] = ws.display_path(ws.agent_doc_file());
            j["scripts_dir"] = ws.display_path(ws.scripts_dir());
            j["content"] = content;
            j["bytes"] = static_cast<long long>(content.size());
            j["truncated"] = truncated;
            j["scripts"] = list;
            send_json(res, j);
          });

  svr.Put(R"(/api/workspaces/([^/]+)/agent-doc)",
          [this](const httplib::Request& req, httplib::Response& res) {
            nlohmann::json body;
            if (!parse_json_body(req, &body, res)) return;
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            const std::string content = json_string(body, "content");
            if (!ws.save_agent_doc(content, &err)) {
              send_error(res, 400, "bad_request", err);
              return;
            }
            LOG_INFO("agent.md: rewritten from the web UI (", content.size(), " bytes)");
            nlohmann::json j = nlohmann::json::object();
            j["ok"] = true;
            j["path"] = ws.agent_doc_file();
            j["bytes"] = static_cast<long long>(file_size(ws.agent_doc_file()));
            send_json(res, j);
          });

  // ---- logs --------------------------------------------------------------
  svr.Get(R"(/api/workspaces/([^/]+)/logs)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            std::size_t tail = 300;
            if (req.has_param("tail")) {
              try {
                const long long v = std::stoll(req.get_param_value("tail"));
                if (v > 0) tail = static_cast<std::size_t>(v > 5000 ? 5000 : v);
              } catch (const std::exception&) {
              }
            }
            const std::vector<std::string> lines = ws.tail_log(tail);
            nlohmann::json list = nlohmann::json::array();
            for (std::size_t i = 0; i < lines.size(); ++i) list.push_back(lines[i]);
            nlohmann::json j = nlohmann::json::object();
            j["path"] = ws.log_file();
            j["lines"] = list;
            send_json(res, j);
          });

  // ---- chat (SSE) --------------------------------------------------------
  svr.Post("/api/chat", [this](const httplib::Request& req, httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;

    if (!config->loaded) {
      send_error(res, 409, "config_missing",
                 "no provider.yaml is configured yet; upload it in the web UI");
      return;
    }
    const std::string message = json_string(body, "message");
    // A message may be image-only ("look at this"), so emptiness is checked
    // after the attachments are parsed below.
    nlohmann::json::const_iterator images_it = body.find("images");
    const nlohmann::json images_json =
        images_it != body.end() ? *images_it : nlohmann::json();
    // An empty "images": [] must not turn into an empty turn.
    if (trim(message).empty() && (!images_json.is_array() || images_json.empty())) {
      send_error(res, 400, "bad_request", "'message' is required");
      return;
    }
    const std::string workspace_id = json_string(body, "workspace_id");
    if (workspace_id.empty()) {
      send_error(res, 400, "bad_request", "'workspace_id' is required");
      return;
    }

    auto turn = std::make_shared<ChatTurn>();
    std::string err;
    if (!open_workspace(workspace_id, &turn->workspace, &err)) {
      send_error(res, 404, "not_found", err);
      return;
    }
    turn->workspace_id = turn->workspace.id();

    const std::string requested_session = json_string(body, "session_id");
    if (!requested_session.empty()) {
      // Unknown session ids simply start a fresh conversation.
      turn->workspace.load_session(requested_session, &turn->session, nullptr);
    }
    if (turn->session.info.id.empty()) {
      Session created;
      if (!turn->workspace.create_session("", &created, &err)) {
        send_error(res, 500, "session_error", err);
        return;
      }
      turn->session = created;
    }
    turn->session_id = turn->session.info.id;

    const std::string provider = json_string(body, "provider");
    const std::string model = json_string(body, "model");

    // ---- image attachments ------------------------------------------------
    std::vector<ImageAttachment> images;
    if (images_json.is_array() && !images_json.empty()) {
      if (images_json.size() > kMaxImagesPerMessage) {
        send_error(res, 400, "too_many_images",
                   "at most " + std::to_string(kMaxImagesPerMessage) + " images per message");
        return;
      }
      // Reject before doing any work if the target model cannot see images:
      // otherwise the provider answers with a confusing 400, or silently
      // ignores them and the user believes the model looked at the picture.
      const ProviderInfo* pinfo =
          config->provider(provider.empty() ? config->active_provider : provider);
      std::string model_name = model.empty() ? (pinfo ? pinfo->default_model : std::string()) : model;
      const ModelInfo* minfo = pinfo ? pinfo->find_model(model_name) : nullptr;
      // Mirror the agent's fallback: an unknown model name resolves to the
      // provider's default, so a stale id in the UI must not look like "this
      // model cannot see images".
      if (pinfo && !minfo && !pinfo->models.empty()) {
        model_name = pinfo->default_model.empty() ? pinfo->models.front() : pinfo->default_model;
        minfo = pinfo->find_model(model_name);
      }
      if (!pinfo || !minfo || !minfo->supports_vision) {
        send_error(res, 400, "vision_unsupported",
                   "model '" + (pinfo ? pinfo->name : std::string("?")) + " / " + model_name +
                       "' does not declare supports_vision; set it in provider.yaml or pick a "
                       "vision model before attaching images");
        return;
      }
      const std::string artifacts = turn->workspace.artifacts_dir();
      if (!make_dirs(artifacts)) {
        send_error(res, 500, "io_error", "cannot create " + artifacts);
        return;
      }
      for (std::size_t i = 0; i < images_json.size(); ++i) {
        const nlohmann::json& item = images_json[i];
        std::string raw;
        if (item.is_string()) raw = item.get<std::string>();
        else if (item.is_object()) raw = json_string(item, "data");
        if (raw.empty()) {
          send_error(res, 400, "bad_request", "image " + std::to_string(i + 1) + " has no data");
          return;
        }
        std::string bytes;
        std::string decode_err;
        if (!decode_image_payload(raw, &bytes, &decode_err)) {
          send_error(res, 400, "bad_image", "image " + std::to_string(i + 1) + ": " + decode_err);
          return;
        }
        if (bytes.size() > kMaxImageBytes) {
          send_error(res, 413, "image_too_large",
                     "image " + std::to_string(i + 1) + " is " + human_size(bytes.size()) +
                         "; the limit is " + human_size(kMaxImageBytes));
          return;
        }
        const char* mime = sniff_image_mime(bytes);
        if (!mime) {
          send_error(res, 400, "bad_image",
                     "image " + std::to_string(i + 1) +
                         " is not a PNG, JPEG, GIF or WebP (identified by content, not by name)");
          return;
        }
        const std::string file = path_join(artifacts, "img_" + random_hex(8) + image_extension_for(mime));
        std::string io_err;
        if (!write_file(file, bytes, false, &io_err)) {
          send_error(res, 500, "io_error", "cannot store the image: " + io_err);
          return;
        }
        ImageAttachment att;
        att.mime = mime;
        // Workspace-relative, so the session file stays portable and the /raw
        // route can serve it back to the UI.
        att.path = turn->workspace.display_path(file);
        images.push_back(att);
      }
      LOG_INFO("chat: ", images.size(), " image attachment(s) stored under ", artifacts);
    }

    std::vector<std::shared_ptr<ChatTurn>> graveyard;
    {
      std::lock_guard<std::mutex> lock(mu);
      prune_turns_locked(&graveyard);
      auto it = turns.find(turn->session_id);
      if (it != turns.end() && it->second) {
        it->second->cancel.store(true);
        retired.push_back(it->second);  // destroyed after the lock is released
        turns.erase(it);
      }
      turns[turn->session_id] = turn;
    }
    // Joining a finished worker is immediate; doing it here keeps `mu` free.
    graveyard.clear();

    AppConfig* cfg = config;
    ToolRegistry* reg = tools;
    turn->worker = std::thread([turn, cfg, reg, message, images, provider, model]() {
      Agent agent(cfg, reg);
      TurnOptions options;
      options.provider = provider;
      options.model = model;
      options.stream = true;
      options.inbox = &turn->inbox;
      EventEmitter emit = [turn](const nlohmann::json& event) { turn->emit_event(event); };
      try {
        agent.run_turn(turn->workspace, &turn->session, message, images, options, emit,
                       &turn->cancel);
      } catch (const std::exception& e) {
        nlohmann::json event = nlohmann::json::object();
        event["type"] = "error";
        event["message"] = std::string("agent crashed: ") + e.what();
        turn->emit_event(event);
      }
      // run_turn drains the queue when it finishes, but a prompt can still land
      // in the microseconds between that drain and the worker stopping. Refuse
      // new ones first, then the drain below is final. Anything left is handed
      // back to the browser, which restores it to the composer; leaking it here
      // would make a queued message vanish without a trace.
      turn->begin_shutdown();
      std::vector<QueuedPrompt> leftover = turn->inbox.drain();
      if (!leftover.empty()) {
        nlohmann::json items = nlohmann::json::array();
        for (std::size_t i = 0; i < leftover.size(); ++i) {
          nlohmann::json item = nlohmann::json::object();
          item["id"] = leftover[i].id;
          item["content"] = leftover[i].text;
          item["created_at"] = leftover[i].created_at;
          items.push_back(item);
        }
        nlohmann::json event = nlohmann::json::object();
        event["type"] = "queue_unconsumed";
        event["items"] = items;
        turn->emit_event(event);
      }
      turn->close();
    });

    res.set_header("Cache-Control", "no-cache");
    res.set_header("X-Accel-Buffering", "no");
    res.set_header("Access-Control-Allow-Origin", "*");
    res.set_chunked_content_provider(
        "text/event-stream; charset=utf-8",
        [turn](std::size_t, httplib::DataSink& sink) { return turn->pump(sink); },
        [turn](bool) { turn->cancel.store(true); });
  });

  svr.Post("/api/chat/stop", [this](const httplib::Request& req, httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;
    const std::string session_id = json_string(body, "session_id");
    bool found = false;
    {
      std::lock_guard<std::mutex> lock(mu);
      auto it = turns.find(session_id);
      if (it != turns.end() && it->second) {
        it->second->cancel.store(true);
        found = true;
      }
    }
    nlohmann::json j = nlohmann::json::object();
    j["ok"] = true;
    j["cancelled"] = found;
    send_json(res, j);
  });

  // ---- queued prompts ----------------------------------------------------
  // A prompt typed while a turn is already running does not start a second turn:
  // it is appended to the running conversation at the next agent iteration. The
  // queue belongs to that turn, so stopping or replacing the turn throws the
  // remaining prompts back to the caller (`queue_unconsumed` on the SSE stream)
  // instead of firing them later in an unrelated turn.
  svr.Post("/api/chat/queue", [this](const httplib::Request& req, httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;
    const std::string session_id = json_string(body, "session_id");
    const std::string message = json_string(body, "message");
    if (session_id.empty()) {
      send_error(res, 400, "bad_request", "'session_id' is required");
      return;
    }
    if (trim(message).empty()) {
      send_error(res, 400, "bad_request", "'message' is required");
      return;
    }
    // Queued prompts are text-only. An image must be decoded, size-checked and
    // written into the workspace before it can be referenced, which /api/chat
    // does for the request that starts a turn; silently dropping them here would
    // be worse than refusing.
    auto images_it = body.find("images");
    if (images_it != body.end() && images_it->is_array() && !images_it->empty()) {
      send_error(res, 400, "images_not_queued",
                 "a queued prompt cannot carry images; wait for the running turn to finish");
      return;
    }

    // No pruning here: this endpoint must stay fast, and destroying a retired
    // turn joins its worker (which can be stuck in a long tool call). A finished
    // turn refuses `enqueue_prompt` on its own, and the dead entry is reaped by
    // the next /api/chat for that session.
    std::shared_ptr<ChatTurn> turn;
    {
      std::lock_guard<std::mutex> lock(mu);
      auto it = turns.find(session_id);
      if (it != turns.end()) turn = it->second;
    }

    if (!turn) {
      send_error(res, 409, "no_active_turn",
                 "this session has no running turn; send the message directly");
      return;
    }

    QueuedPrompt prompt;
    prompt.id = make_id("q_");
    prompt.text = trim(message);
    prompt.created_at = now_iso8601();
    if (!turn->enqueue_prompt(prompt)) {
      send_error(res, 409, "no_active_turn",
                 "the running turn just finished; send the message directly");
      return;
    }

    LOG_INFO("chat: queued prompt ", prompt.id, " for session ", session_id, " (",
             turn->inbox.size(), " pending)");
    nlohmann::json j = nlohmann::json::object();
    j["ok"] = true;
    j["queued"] = {{"id", prompt.id}, {"content", prompt.text}, {"created_at", prompt.created_at}};
    j["pending"] = static_cast<int>(turn->inbox.size());
    send_json(res, j);
  });

  svr.Post("/api/chat/queue/cancel", [this](const httplib::Request& req,
                                            httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;
    const std::string session_id = json_string(body, "session_id");
    const std::string id = json_string(body, "id");
    if (session_id.empty()) {
      send_error(res, 400, "bad_request", "'session_id' is required");
      return;
    }
    std::shared_ptr<ChatTurn> turn;
    {
      std::lock_guard<std::mutex> lock(mu);
      auto it = turns.find(session_id);
      if (it != turns.end()) turn = it->second;
    }
    int cancelled = 0;
    int pending = 0;
    if (turn) {
      if (id.empty()) {
        cancelled = static_cast<int>(turn->inbox.drain().size());
      } else {
        cancelled = turn->inbox.remove(id) ? 1 : 0;
      }
      pending = static_cast<int>(turn->inbox.size());
    }
    nlohmann::json j = nlohmann::json::object();
    j["ok"] = true;
    j["cancelled"] = cancelled;
    j["pending"] = pending;
    send_json(res, j);
  });

  svr.Get("/api/chat/queue", [this](const httplib::Request& req, httplib::Response& res) {
    const std::string session_id =
        req.has_param("session_id") ? req.get_param_value("session_id") : std::string();
    if (session_id.empty()) {
      send_error(res, 400, "bad_request", "'session_id' is required");
      return;
    }
    std::shared_ptr<ChatTurn> turn;
    {
      std::lock_guard<std::mutex> lock(mu);
      auto it = turns.find(session_id);
      if (it != turns.end()) turn = it->second;
    }
    std::vector<QueuedPrompt> items;
    if (turn) items = turn->inbox.snapshot();
    nlohmann::json arr = nlohmann::json::array();
    for (std::size_t i = 0; i < items.size(); ++i) {
      arr.push_back({{"id", items[i].id},
                     {"content", items[i].text},
                     {"created_at", items[i].created_at}});
    }
    nlohmann::json j = nlohmann::json::object();
    j["ok"] = true;
    j["pending"] = static_cast<int>(items.size());
    j["items"] = arr;
    send_json(res, j);
  });

  // ---- fallback for unknown API routes ----------------------------------
  svr.Get(R"(/api/.*)", [](const httplib::Request& req, httplib::Response& res) {
    send_error(res, 404, "not_found", "no such endpoint: " + req.path);
  });
  svr.Post(R"(/api/.*)", [](const httplib::Request& req, httplib::Response& res) {
    send_error(res, 404, "not_found", "no such endpoint: " + req.path);
  });
  svr.Put(R"(/api/.*)", [](const httplib::Request& req, httplib::Response& res) {
    send_error(res, 404, "not_found", "no such endpoint: " + req.path);
  });
  svr.Delete(R"(/api/.*)", [](const httplib::Request& req, httplib::Response& res) {
    send_error(res, 404, "not_found", "no such endpoint: " + req.path);
  });

  svr.set_exception_handler([](const httplib::Request&, httplib::Response& res,
                               std::exception_ptr ep) {
    std::string what = "unknown error";
    try {
      if (ep) std::rethrow_exception(ep);
    } catch (const std::exception& e) {
      what = e.what();
    } catch (...) {
    }
    LOG_ERROR("server: unhandled exception: ", what);
    send_error(res, 500, "internal_error", what);
  });
}

// ---------------------------------------------------------------------------
// ApiServer
// ---------------------------------------------------------------------------
ApiServer::ApiServer(AppConfig* config, ToolRegistry* tools, WorkspaceRegistry* registry)
    : impl_(new Impl()) {
  impl_->config = config;
  impl_->tools = tools;
  impl_->registry = registry;
}

ApiServer::~ApiServer() { stop(); }

bool ApiServer::start(const ServerOptions& options, std::string* err) {
  options_ = options;
  impl_->web_dir = options.web_dir;
  impl_->started_ms = now_ms();
  impl_->install_routes();

  const std::string host = options.host.empty() ? "127.0.0.1" : options.host;
  impl_->listen_thread = std::thread([this, host]() {
    impl_->listen_ok = impl_->server.listen(host, options_.port);
    impl_->listen_returned.store(true);
  });

  // Wait for the listener to come up (or to fail fast).
  for (int i = 0; i < 500; ++i) {
    if (impl_->server.is_running()) break;
    if (impl_->listen_returned.load()) break;
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  if (!impl_->server.is_running()) {
    if (impl_->listen_thread.joinable()) impl_->listen_thread.join();
    if (err) {
      *err = "cannot listen on " + host + ":" + std::to_string(options_.port) +
             " (address already in use?)";
    }
    return false;
  }
  LOG_INFO("server: listening on http://", host, ":", options_.port);
  return true;
}

void ApiServer::stop() {
  if (!impl_) return;
  {
    std::lock_guard<std::mutex> lock(impl_->mu);
    for (auto it = impl_->turns.begin(); it != impl_->turns.end(); ++it) {
      if (it->second) it->second->cancel.store(true);
    }
  }
  impl_->server.stop();
  if (impl_->listen_thread.joinable()) impl_->listen_thread.join();
}

bool ApiServer::is_running() const { return impl_ && impl_->server.is_running(); }

int ApiServer::port() const { return options_.port; }

std::string ApiServer::url() const {
  const std::string host = options_.host.empty() ? "127.0.0.1" : options_.host;
  const std::string display = (host == "0.0.0.0" || host == "::") ? "127.0.0.1" : host;
  return "http://" + display + ":" + std::to_string(options_.port);
}

}  // namespace tea
