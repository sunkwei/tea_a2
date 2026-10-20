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
#include "archive.h"
#include "env.h"
#include "http.h"
#include "log.h"
#include "trajectory.h"
#include "util.h"
#include "version.h"
#include "web_assets.h"

namespace tea {

std::vector<std::string> reachable_urls(const std::string& bind_host, int port) {
  std::vector<std::string> urls;
  const std::string port_text = std::to_string(port);
  const std::string host = bind_host.empty() ? std::string("127.0.0.1") : bind_host;
  const bool wildcard = host == "0.0.0.0" || host == "::" || host == "[::]" || host == "*";
  if (!wildcard) {
    // An explicit bind address is exactly what a browser has to type (an IPv6
    // literal needs brackets in a URL).
    const bool ipv6 = host.find(':') != std::string::npos;
    urls.push_back("http://" + (ipv6 ? "[" + host + "]" : host) + ":" + port_text);
    return urls;
  }
  // Wildcard: every interface is bound, so the useful answer is this machine's
  // address on the LAN, not the loopback the banner used to print. Loopback is
  // still listed last, because it is the one that always works.
  const std::vector<std::string> addresses = local_ipv4_addresses();
  for (std::size_t i = 0; i < addresses.size(); ++i) {
    urls.push_back("http://" + addresses[i] + ":" + port_text);
  }
  urls.push_back("http://127.0.0.1:" + port_text);
  return urls;
}

namespace {

constexpr size_t kFileReadLimit = 2u * 1024u * 1024u;

/// Replay buffer for one running turn. A client that disconnected comes back and
/// asks for everything after the last frame it saw, so the frames have to be
/// kept for as long as the turn runs; 8 MB is far more than a long agent turn
/// streams, and the oldest frames are dropped past it (the client is then told
/// to resync from the persisted session rather than shown a silent gap).
constexpr std::size_t kTurnLogMaxBytes = 8u * 1024u * 1024u;

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
// One streaming chat turn.
//
// The turn outlives the connection that started it. Every event is kept in a
// bounded log with a sequence number, so a browser that is closed, put to sleep
// or reopened on another machine can ask whether the session still has a turn
// running (GET /api/chat/turn) and re-attach (POST /api/chat/attach): it gets
// the frames it missed and then follows live. A disconnect therefore *detaches*
// one subscriber; only /api/chat/stop, a newer turn on the same session, or
// shutting the server down cancels the work.
// ---------------------------------------------------------------------------
struct ChatTurn {
  std::string session_id;
  std::string workspace_id;
  std::string started_at;
  Workspace workspace;
  Session session;

  mutable std::mutex mu;
  std::condition_variable cv;
  /// Every frame this turn produced, verbatim (`data: {...}\n\n`), oldest first.
  /// This is both the write buffer for attached clients and the replay log.
  std::deque<std::string> log;
  /// Sequence number of `log.front()`. Dropping frames from the front raises it.
  long long log_base = 1;
  std::size_t log_bytes = 0;
  /// Frames dropped from the front. A client that asks for one of them is told to
  /// resync from the persisted session instead of being shown a silent gap.
  long long log_dropped = 0;
  /// Sequence number the next event will carry.
  long long next_seq = 1;
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

  /// One attached HTTP response: which frame it wants next.
  struct Subscriber {
    long long cursor = 1;
  };

  ~ChatTurn() {
    if (worker.joinable()) worker.join();
  }

  void emit_event(const nlohmann::json& event) {
    nlohmann::json stamped = event;
    std::string frame;
    {
      std::lock_guard<std::mutex> lock(mu);
      // The sequence number is what makes a reconnect lossless: the client tells
      // us the last one it saw and we replay everything after it.
      stamped["seq"] = next_seq++;
      frame = "data: " + stamped.dump() + "\n\n";
      log.push_back(frame);
      log_bytes += frame.size();
      // One turn cannot grow without bound: a long agent run streams a lot of
      // text. Drop the oldest frames once the cap is hit; `log_dropped` turns
      // that into an explicit resync for whoever asks for them.
      while (log_bytes > kTurnLogMaxBytes && log.size() > 1) {
        log_bytes -= log.front().size();
        log.pop_front();
        ++log_base;
        ++log_dropped;
      }
    }
    cv.notify_all();
  }

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

  /// What GET /api/chat/turn reports. `running` is the only field the UI needs in
  /// order to decide whether to re-attach; the rest explains the replay window.
  nlohmann::json status_json() const {
    std::lock_guard<std::mutex> lock(mu);
    nlohmann::json j = nlohmann::json::object();
    j["running"] = !closed;
    j["session_id"] = session_id;
    j["workspace_id"] = workspace_id;
    j["started_at"] = started_at;
    j["last_seq"] = next_seq - 1;
    j["oldest_seq"] = log_base;
    j["buffered"] = static_cast<long long>(log.size());
    j["dropped"] = log_dropped;
    return j;
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

  /// Called repeatedly by httplib for one attached response; returns false to end
  /// it. Frames are copied out under the lock and written outside it, so a slow
  /// client cannot stall the worker or the other subscribers.
  bool pump(httplib::DataSink& sink, const std::shared_ptr<Subscriber>& sub) {
    for (;;) {
      std::string frame;
      std::string resync_frame;
      bool done = false;
      {
        std::unique_lock<std::mutex> lock(mu);
        // Wait for the next frame, but never longer than the heartbeat interval:
        // writing a ping is also how a client that vanished without a FIN is
        // noticed. `closed` ends the wait for good.
        for (;;) {
          const long long end_seq = log_base + static_cast<long long>(log.size());
          if (closed || sub->cursor < end_seq) break;
          if (cv.wait_for(lock, std::chrono::seconds(15)) == std::cv_status::timeout) break;
        }
        if (sub->cursor < log_base) {
          // It missed frames that are already gone. Say so, then continue from
          // the oldest one still held: the client reloads the persisted session
          // and picks the live stream up from here.
          nlohmann::json ev = nlohmann::json::object();
          ev["type"] = "resync";
          ev["reason"] = "event_buffer_overflow";
          ev["dropped"] = log_dropped;
          ev["oldest_seq"] = log_base;
          resync_frame = "data: " + ev.dump() + "\n\n";
          sub->cursor = log_base;
        }
        const long long end_seq = log_base + static_cast<long long>(log.size());
        if (sub->cursor >= log_base && sub->cursor < end_seq) {
          frame = log[static_cast<std::size_t>(sub->cursor - log_base)];
          ++sub->cursor;
        }
        done = closed && sub->cursor >= end_seq;
      }
      if (!resync_frame.empty() && !sink.write(resync_frame.data(), resync_frame.size())) {
        return false;
      }
      if (!frame.empty()) {
        if (!sink.write(frame.data(), frame.size())) return false;  // this client is gone
        continue;
      }
      if (done) {
        // A chunked body is only terminated by done(): returning false here is
        // treated as a cancellation and aborts the transfer *without* the
        // terminating "0\r\n\r\n" chunk, which browsers report as "Error in
        // input stream" (curl silently tolerates it).
        sink.done();
        return true;
      }
      if (!sink.is_writable()) return false;
      char heartbeat[80];
      const int n = std::snprintf(heartbeat, sizeof(heartbeat),
                                  "data: {\"type\":\"ping\",\"t\":%lld}\n\n",
                                  static_cast<long long>(now_ms() / 1000));
      if (n <= 0 || !sink.write(heartbeat, static_cast<std::size_t>(n))) return false;
    }
  }
};

/// A download name that survives every browser, shell and filesystem: ASCII only,
/// no separators, and a UTC stamp so two downloads do not collide.
std::string ascii_archive_name(const std::string& workspace, const std::string& rel) {
  std::string out;
  for (std::size_t i = 0; i < workspace.size(); ++i) {
    const unsigned char c = static_cast<unsigned char>(workspace[i]);
    const bool plain = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                       (c >= '0' && c <= '9') || c == '-' || c == '_';
    out += plain ? static_cast<char>(c) : '_';
  }
  if (out.empty()) out = "workspace";
  if (out.size() > 48) out.resize(48);

  std::string dir;
  for (std::size_t i = 0; i < rel.size(); ++i) {
    const unsigned char c = static_cast<unsigned char>(rel[i]);
    const bool plain = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                       (c >= '0' && c <= '9') || c == '-' || c == '_';
    if (c == '.' && dir.empty()) continue;             // no leading dot
    dir += plain ? static_cast<char>(c) : '_';
  }
  if (dir.empty()) dir = "workspace";
  if (dir.size() > 48) dir.resize(48);

  // 2026-10-19T09:12:33Z -> 20261019-091233
  std::string digits;
  const std::string stamp = now_iso8601();
  for (std::size_t i = 0; i < stamp.size(); ++i) {
    if (stamp[i] >= '0' && stamp[i] <= '9') digits += stamp[i];
  }
  const std::string when =
      digits.size() >= 14 ? (digits.substr(0, 8) + "-" + digits.substr(8, 6)) : digits;
  return out + "-" + dir + (when.empty() ? "" : "-" + when) + ".zip";
}

/// Percent-encode for `filename*=UTF-8''...`, so a non-ASCII workspace name still
/// arrives readable instead of as a row of underscores (RFC 5987/6266).
std::string rfc5987_name(const std::string& name) {
  static const char* kHex = "0123456789ABCDEF";
  std::string out;
  for (std::size_t i = 0; i < name.size(); ++i) {
    const unsigned char c = static_cast<unsigned char>(name[i]);
    const bool plain = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                       (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.';
    if (plain) {
      out += static_cast<char>(c);
    } else {
      out += '%';
      out += kHex[c >> 4];
      out += kHex[c & 0x0F];
    }
  }
  return out;
}

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
  // NOTE: inside a bracket expression write the '-' LAST and unescaped:
  // libstdc++ up to GCC 6 throws std::regex_error("Invalid range in bracket
  // expression") for "\\-", which aborted the agent at startup on the
  // aarch64 target (Linaro GCC 6.5 runtime). The character set is identical.
  svr.Get(R"(/([A-Za-z0-9_.-]+))", [this](const httplib::Request& req, httplib::Response& res) {
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

  svr.Post("/api/workspaces", [this](const httplib::Request& req, httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;
    const std::string path = json_string(body, "path");
    if (path.empty()) {
      send_error(res, 400, "bad_request", "'path' is required");
      return;
    }
    // The UI asks the user for a complete path. A relative one would silently
    // resolve against the server's cwd (and `create` would happily mkdir there),
    // which is not something the person typing it can see, so refuse it up front
    // with a message that shows the expected shape.
    if (!path_is_absolute(expand_user(trim(path)))) {
      send_error(res, 400, "bad_request",
                 "workspace path must be a full path, e.g. /home/me/project or "
                 "C:\\Users\\me\\project (got '" + trim(path) + "')");
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

  // ---- forking a session -------------------------------------------------
  // A branch starts as a copy of the parent's transcript through the end of the
  // selected turn; the next message sent to it becomes that branch's next turn
  // while the parent keeps going its own way. `message_index` addresses a user
  // message of the parent (the child also gets the assistant/tool messages that
  // answered it), omitted or negative forks at the end, and `title` overrides
  // the generated "分叉 <turn> · <parent title>".
  svr.Post(R"(/api/sessions/([^/]+)/fork)",
           [this](const httplib::Request& req, httplib::Response& res) {
             nlohmann::json body;
             if (!parse_json_body(req, &body, res)) return;
             const std::string session_id = req.matches[1].str();
             Workspace ws;
             std::string err;
             if (!find_session_workspace(session_id, &ws, &err)) {
               send_error(res, 404, "not_found", err);
               return;
             }
             long long message_index = -1;
             auto idx_it = body.find("message_index");
             if (idx_it != body.end() && !idx_it->is_null()) {
               if (idx_it->is_number_unsigned()) {
                 const unsigned long long v = idx_it->get<unsigned long long>();
                 if (v > 1000000000ull) {
                   send_error(res, 400, "bad_request", "'message_index' is out of range");
                   return;
                 }
                 message_index = static_cast<long long>(v);
               } else if (idx_it->is_number_integer()) {
                 message_index = idx_it->get<long long>();
               } else {
                 send_error(res, 400, "bad_request", "'message_index' must be an integer");
                 return;
               }
             }
             // Copying a transcript that a running turn is still appending to
             // would freeze a half-finished answer into the branch. The UI does
             // not arm a fork point while streaming; this covers direct callers.
             {
               std::lock_guard<std::mutex> lock(mu);
               auto it = turns.find(session_id);
               if (it != turns.end() && it->second && !it->second->finished()) {
                 send_error(res, 409, "turn_running",
                            "this session has a turn in progress; it must finish before it "
                            "can be forked");
                 return;
               }
             }
             Session child;
             if (!ws.fork_session(session_id, message_index, json_string(body, "title"), &child,
                                  &err)) {
               send_error(res, 400, "fork_failed", err);
               return;
             }
             nlohmann::json messages = nlohmann::json::array();
             for (std::size_t i = 0; i < child.messages.size(); ++i) {
               messages.push_back(child.messages[i].to_store());
             }
             nlohmann::json j = nlohmann::json::object();
             j["ok"] = true;
             j["workspace_id"] = ws.id();
             j["session"] = child.info.to_json();
             j["messages"] = messages;
             send_json(res, j);
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

  // ---- workspace archive download ----------------------------------------
  // One ZIP with a whole directory of *this device*, so an operator can take the
  // run data (sessions, trajectories, logs, memory, agent.md) with them without
  // shelling into the machine. Assembled in memory by src/archive.cpp; the limits
  // there are what keeps a single request from exhausting the device, and files
  // that do not fit are reported in X-Archive-Skipped instead of failing the
  // whole download.
  svr.Get(R"(/api/workspaces/([^/]+)/archive)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            // The default is the run directory: that is what "download this
            // workspace's tea_agent data" means. Any other directory inside the
            // workspace works the same way - resolve_path is what refuses to
            // leave the sandbox, so `../..` never reaches the zipper.
            std::string rel = req.has_param("path") ? trim(req.get_param_value("path")) : "";
            if (rel.empty()) rel = ".tea_agent_run";
            const std::string target = ws.resolve_path(rel, &err);
            if (target.empty()) {
              send_error(res, 403, "forbidden", err);
              return;
            }
            if (!path_exists(target)) {
              send_error(res, 404, "not_found", "no such directory: " + rel);
              return;
            }
            if (!path_is_dir(target)) {
              send_error(res, 400, "not_a_directory",
                         "not a directory: " + rel + " (a single file is /raw)");
              return;
            }
            const ArchiveResult archive = zip_directory(target, rel);
            if (!archive.ok) {
              send_error(res, 500, "archive_failed", archive.error);
              return;
            }
            const std::string ascii = ascii_archive_name(path_filename(ws.root()), rel);
            res.set_header("Content-Disposition",
                           "attachment; filename=\"" + ascii + "\"; filename*=UTF-8''" +
                               rfc5987_name(ascii));
            res.set_header("Cache-Control", "no-store");
            res.set_header("Access-Control-Allow-Origin", "*");
            // Lets a caller say what it got without unpacking: how much went in
            // and how much the limits left out.
            res.set_header("Access-Control-Expose-Headers",
                           "Content-Disposition, X-Archive-Entries, X-Archive-Skipped, "
                           "X-Archive-Uncompressed");
            res.set_header("X-Archive-Entries", std::to_string(archive.entries));
            res.set_header("X-Archive-Skipped", std::to_string(archive.skipped.size()));
            res.set_header("X-Archive-Uncompressed", std::to_string(archive.uncompressed));
            LOG_INFO("archive: ", ws.display_path(target), " -> ", ascii, " (", archive.bytes.size(),
                     " B, ", archive.entries, " entries, ", archive.skipped.size(), " skipped)");
            res.set_content(archive.bytes, "application/zip");
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
    turn->started_at = now_iso8601();
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
    // No resource releaser here on purpose: the response ending (tab closed,
    // laptop asleep, network dropped) must not cancel the work. Only
    // /api/chat/stop, a newer turn on the same session, or shutting the server
    // down does that. Whoever comes back re-attaches below.
    auto sub = std::make_shared<ChatTurn::Subscriber>();
    res.set_chunked_content_provider(
        "text/event-stream; charset=utf-8",
        [turn, sub](std::size_t, httplib::DataSink& sink) { return turn->pump(sink, sub); });
  });

  // ---- re-attaching to a turn that is still running -----------------------
  // "Is this session's turn still running, and from which sequence number can I
  // replay it?" The UI asks when it opens a session - a reload, a second tab, a
  // laptop waking up - so a turn that outlived its connection is picked up
  // instead of being shown as a conversation that stopped mid-sentence.
  svr.Get("/api/chat/turn", [this](const httplib::Request& req, httplib::Response& res) {
    const std::string session_id =
        req.has_param("session_id") ? req.get_param_value("session_id") : std::string();
    if (session_id.empty()) {
      send_error(res, 400, "bad_request", "'session_id' is required");
      return;
    }
    std::shared_ptr<ChatTurn> turn;
    std::vector<std::shared_ptr<ChatTurn>> graveyard;
    {
      std::lock_guard<std::mutex> lock(mu);
      // Reap turns whose worker already finished. With no client left to notice
      // them they would otherwise hold their replay log until the next /api/chat.
      prune_turns_locked(&graveyard);
      auto it = turns.find(session_id);
      if (it != turns.end()) turn = it->second;
    }
    graveyard.clear();  // ~ChatTurn joins the worker: never under `mu`
    nlohmann::json j = turn ? turn->status_json() : nlohmann::json::object();
    if (turn) {
      j["pending"] = static_cast<long long>(turn->inbox.size());
    } else {
      j["running"] = false;
      j["session_id"] = session_id;
    }
    j["ok"] = true;
    send_json(res, j);
  });

  // Every turn that is still running, newest first. A browser that opens cold -
  // another machine, another profile, no localStorage - has no idea what it was
  // looking at, so this is how it finds work in progress instead of landing on
  // the first workspace in the index and showing an empty chat.
  svr.Get("/api/chat/turns", [this](const httplib::Request&, httplib::Response& res) {
    std::vector<std::shared_ptr<ChatTurn>> live;
    std::vector<std::shared_ptr<ChatTurn>> graveyard;
    {
      std::lock_guard<std::mutex> lock(mu);
      prune_turns_locked(&graveyard);
      for (auto it = turns.begin(); it != turns.end(); ++it) {
        if (it->second) live.push_back(it->second);
      }
    }
    graveyard.clear();  // ~ChatTurn joins the worker: never under `mu`
    nlohmann::json list = nlohmann::json::array();
    for (std::size_t i = 0; i < live.size(); ++i) {
      nlohmann::json t = live[i]->status_json();
      t["pending"] = static_cast<long long>(live[i]->inbox.size());
      list.push_back(t);
    }
    nlohmann::json j = nlohmann::json::object();
    j["ok"] = true;
    j["turns"] = list;
    send_json(res, j);
  });

  // Re-attach: replay everything after `since` (0 or omitted = the whole buffer),
  // then follow live. This is what a reopened browser calls.
  svr.Post("/api/chat/attach", [this](const httplib::Request& req, httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;
    const std::string session_id = json_string(body, "session_id");
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
    if (!turn) {
      send_error(res, 404, "no_active_turn", "this session has no turn to attach to");
      return;
    }
    auto sub = std::make_shared<ChatTurn::Subscriber>();
    // `since` is the last seq the client saw. A seq older than the buffer (or no
    // seq at all) replays from the oldest frame still held; when frames were
    // dropped, pump() sends a `resync` event first so the client reloads the
    // persisted session instead of showing a hole.
    nlohmann::json::const_iterator since_it = body.find("since");
    if (since_it != body.end() && since_it->is_number_integer()) {
      const long long since = since_it->get<long long>();
      if (since > 0) sub->cursor = since + 1;
    }
    LOG_INFO("chat: re-attaching to the running turn of session ", session_id, " (replay from seq ",
             sub->cursor, ")");
    res.set_header("Cache-Control", "no-cache");
    res.set_header("X-Accel-Buffering", "no");
    res.set_header("Access-Control-Allow-Origin", "*");
    res.set_chunked_content_provider(
        "text/event-stream; charset=utf-8",
        [turn, sub](std::size_t, httplib::DataSink& sink) { return turn->pump(sink, sub); });
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
  // Route registration builds std::regex objects; on an old libstdc++ (GCC <= 6)
  // one of those used to throw and take the whole process down with no
  // diagnostic. Report it as a start error instead.
  try {
    impl_->install_routes();
  } catch (const std::exception& e) {
    if (err) *err = std::string("installing the routes threw: ") + e.what();
    return false;
  }

  const std::string host = options.host.empty() ? "127.0.0.1" : options.host;
  impl_->listen_thread = std::thread([this, host]() {
    // An exception escaping this thread used to call std::terminate straight
    // away ("terminate called without an active exception"), leaving nothing
    // but a core dump to debug with. Turn it into a reported start failure.
    try {
      impl_->listen_ok = impl_->server.listen(host, options_.port);
    } catch (const std::exception& e) {
      LOG_ERROR("server: listen threw: ", e.what());
      impl_->listen_ok = false;
    } catch (...) {
      LOG_ERROR("server: listen threw an unknown exception");
      impl_->listen_ok = false;
    }
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
  return "http://127.0.0.1:" + std::to_string(options_.port);
}

std::vector<std::string> ApiServer::urls() const {
  return reachable_urls(options_.host, options_.port);
}

}  // namespace tea
