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
#include "http.h"
#include "log.h"
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
  std::atomic<bool> cancel{false};
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

  void prune_turns_locked() {
    for (auto it = turns.begin(); it != turns.end();) {
      if (it->second && it->second->finished() && it->second->worker.joinable()) {
        it = turns.erase(it);
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
    res.set_content(std::string(asset->data, asset->size), asset->mime);
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

  svr.Post("/api/workspaces", [this](const httplib::Request& req, httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;
    const std::string path = json_string(body, "path");
    if (path.empty()) {
      send_error(res, 400, "bad_request", "'path' is required");
      return;
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

    const std::string message = json_string(body, "message");
    if (trim(message).empty()) {
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

    {
      std::lock_guard<std::mutex> lock(mu);
      prune_turns_locked();
      auto it = turns.find(turn->session_id);
      if (it != turns.end() && it->second) it->second->cancel.store(true);
      turns[turn->session_id] = turn;
    }

    AppConfig* cfg = config;
    ToolRegistry* reg = tools;
    turn->worker = std::thread([turn, cfg, reg, message, provider, model]() {
      Agent agent(cfg, reg);
      TurnOptions options;
      options.provider = provider;
      options.model = model;
      options.stream = true;
      EventEmitter emit = [turn](const nlohmann::json& event) { turn->emit_event(event); };
      try {
        agent.run_turn(turn->workspace, &turn->session, message, options, emit, &turn->cancel);
      } catch (const std::exception& e) {
        nlohmann::json event = nlohmann::json::object();
        event["type"] = "error";
        event["message"] = std::string("agent crashed: ") + e.what();
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
