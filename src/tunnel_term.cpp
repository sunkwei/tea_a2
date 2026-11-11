// tunnel_term.cpp -- the HTTP terminal that the tunnel can carry.
//
// The sidecar normally forwards bytes between the two ends of a tunnel. This
// file adds a service on the far end: a real shell (a PTY) with a small JSON
// API and a web page that drives it.
//
// The wire protocol does not change. The agent side still dials a TCP address
// when the peer opens a connection; in terminal mode that address is this
// service, so the browser on the operator's machine reaches it through the
// tunnel like any other HTTP service.
//
// The API mirrors the agent's /api/shell endpoints in shape, so the two are
// easy to tell apart in a log but easy to use in the same way.
#include "tunnel_term.h"

#include <httplib.h>
#include <nlohmann/json.hpp>

#include <atomic>
#include <cstdio>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "shell.h"
#include "util.h"
#include "web_assets.h"

namespace tea_tunnel {

namespace {

using json = nlohmann::json;

/// How big a file the terminal may upload. The same limit the agent uses.
constexpr std::size_t kUploadLimit = 32u * 1024u * 1024u;

/// A shell nobody touched for an hour is closed: a forgotten browser tab must
/// not keep a process (and its memory) alive forever.
constexpr int kIdleMillis = 60 * 60 * 1000;

/// The longest a browser may wait for new output. Long enough to keep the
/// traffic quiet, short enough that a dead connection is noticed.
constexpr int kMaxWaitMillis = 30000;

/// The page and its scripts are compiled into the binary (there is no file to
/// read on a device). A build without them still serves the API, so the page
/// says what is missing instead of the tunnel failing to start.
const tea::WebAsset* find_asset(const std::string& name) {
  for (std::size_t i = 0; i < tea::kWebAssetCount; i++) {
    if (name == tea::kWebAssets[i].path) return &tea::kWebAssets[i];
  }
  return nullptr;
}

void send_json(httplib::Response& res, const json& body, int status = 200) {
  res.status = status;
  res.set_header("Access-Control-Allow-Origin", "*");
  res.set_header("Cache-Control", "no-store");
  res.set_content(tea::dump_json(body), "application/json; charset=utf-8");
}

void send_error(httplib::Response& res, int status, const std::string& code,
                const std::string& message) {
  json body = json::object();
  json error = json::object();
  error["code"] = code;
  error["message"] = message;
  body["error"] = error;
  send_json(res, body, status);
}

/// Read a request body as JSON. Returns false and answers 400 when it is not
/// JSON at all.
bool parse_json_body(const httplib::Request& req, json* out, httplib::Response& res) {
  try {
    *out = json::parse(req.body);
  } catch (const std::exception& e) {
    send_error(res, 400, "bad_request", std::string("not JSON: ") + e.what());
    return false;
  }
  return true;
}

std::string json_string(const json& body, const std::string& key) {
  if (!body.contains(key) || !body[key].is_string()) return "";
  return body[key].get<std::string>();
}

int json_int(const json& body, const std::string& key, int fallback) {
  if (!body.contains(key) || !body[key].is_number_integer()) return fallback;
  return body[key].get<int>();
}

/// The content type for a file the browser downloads. A fixed list instead of
/// sniffing: an unknown extension is served as opaque bytes, so a crafted file
/// cannot run script in the browser's origin.
std::string mime_for(const std::string& path) {
  const std::string ext = tea::path_extension(path);
  if (ext == ".png") return "image/png";
  if (ext == ".jpg" || ext == ".jpeg") return "image/jpeg";
  if (ext == ".gif") return "image/gif";
  if (ext == ".webp") return "image/webp";
  if (ext == ".svg") return "image/svg+xml";
  if (ext == ".pdf") return "application/pdf";
  if (ext == ".txt") return "text/plain; charset=utf-8";
  if (ext == ".md" || ext == ".markdown") return "text/markdown; charset=utf-8";
  return "application/octet-stream";
}

/// A download name that survives every browser and filesystem: ASCII only,
/// with the extension kept (it decides what the receiving system does with it).
std::string download_name(const std::string& name) {
  std::string out;
  for (std::size_t i = 0; i < name.size(); ++i) {
    const unsigned char c = static_cast<unsigned char>(name[i]);
    const bool plain = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                       (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.';
    out += plain ? static_cast<char>(c) : '_';
  }
  if (out.empty() || out == "." || out == "..") out = "download";
  if (out.size() > 96) {
    const std::string ext = tea::path_extension(out);
    out = out.substr(0, 96 - ext.size()) + ext;
  }
  return out;
}

/// The same name, percent-encoded (RFC 5987), so a name with non-ASCII
/// characters survives the trip.
std::string download_name_utf8(const std::string& name) {
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

}  // namespace

// ---------------------------------------------------------------------------
// TerminalService
// ---------------------------------------------------------------------------

struct TerminalService::Impl {
  httplib::Server svr;
  tea::ShellRegistry shells;
  std::thread worker;
  std::atomic<bool> running{false};
  /// Where a new session starts. The shell has to live somewhere; the home
  /// directory is the one place that always exists and is writable.
  std::string base_dir;

  void install_routes();
  bool serve_asset(httplib::Response& res, const std::string& name);
};

bool TerminalService::Impl::serve_asset(httplib::Response& res, const std::string& name) {
  const tea::WebAsset* asset = find_asset(name);
  if (asset == nullptr) {
    send_error(res, 404, "not_found", "no such asset: " + name);
    return false;
  }
  res.set_content(std::string(asset->data, asset->size), asset->mime);
  res.set_header("Cache-Control", "no-store");
  return true;
}

void TerminalService::Impl::install_routes() {
  // ---- the page ----------------------------------------------------------
  svr.Get("/", [this](const httplib::Request&, httplib::Response& res) {
    serve_asset(res, "tunnel_terminal.html");
  });
  svr.Get("/term.js", [this](const httplib::Request&, httplib::Response& res) {
    serve_asset(res, "term.js");
  });
  svr.Get("/tunnel_terminal.js", [this](const httplib::Request&, httplib::Response& res) {
    serve_asset(res, "tunnel_terminal.js");
  });

  // ---- what this machine can do ------------------------------------------
  svr.Get("/api/term", [this](const httplib::Request&, httplib::Response& res) {
    const tea::ShellCapability cap = tea::shell_capability();
    json body = json::object();
    body["ok"] = true;
    body["available"] = cap.available;
    body["platform"] = cap.platform;
    body["backend"] = cap.backend;
    body["shell"] = cap.shell;
    body["reason"] = cap.reason;
    body["base_dir"] = base_dir;
    send_json(res, body);
  });

  // ---- a new session ------------------------------------------------------
  svr.Post("/api/term/session", [this](const httplib::Request& req, httplib::Response& res) {
    json body;
    if (!parse_json_body(req, &body, res)) return;
    const int cols = json_int(body, "cols", 80);
    const int rows = json_int(body, "rows", 24);
    // A session may ask for a different directory, but only a real one: the
    // shell runs with the sidecar's rights anyway, so this is a convenience,
    // not a privilege.
    std::string cwd = base_dir;
    const std::string want = tea::trim(json_string(body, "cwd"));
    if (!want.empty()) {
      const std::string expanded = tea::expand_user(want);
      if (tea::path_is_dir(expanded)) {
        cwd = tea::path_absolute(expanded);
      } else {
        send_error(res, 400, "bad_request", "no such directory: " + want);
        return;
      }
    }
    std::string id;
    std::string err;
    if (!shells.create("", cwd, cols, rows, &id, &err)) {
      send_error(res, 400, "shell_unavailable", err);
      return;
    }
    std::shared_ptr<tea::ShellSession> session = shells.find(id);
    json out = json::object();
    out["ok"] = true;
    out["id"] = id;
    if (session) out["session"] = session->status_json();
    send_json(res, out);
  });

  svr.Get(R"(/api/term/session/([^/]+))",
           [this](const httplib::Request& req, httplib::Response& res) {
    const std::string id = req.matches[1].str();
    std::shared_ptr<tea::ShellSession> session = shells.find(id);
    if (!session) {
      send_error(res, 404, "not_found", "no such session: " + id);
      return;
    }
    json out = json::object();
    out["ok"] = true;
    out["id"] = id;
    out["session"] = session->status_json();
    send_json(res, out);
  });

  // ---- the output, since a cursor ----------------------------------------
  // A browser asks for "what is new since byte N". When there is nothing new
  // the request waits a little, so an idle terminal costs no traffic at all.
  svr.Get(R"(/api/term/session/([^/]+)/output)",
           [this](const httplib::Request& req, httplib::Response& res) {
    const std::string id = req.matches[1].str();
    std::shared_ptr<tea::ShellSession> session = shells.find(id);
    if (!session) {
      send_error(res, 404, "not_found", "no such session: " + id);
      return;
    }
    shells.reap(kIdleMillis);
    std::size_t cursor = 0;
    if (req.has_param("cursor")) {
      const std::string raw = req.get_param_value("cursor");
      try {
        cursor = static_cast<std::size_t>(std::stoull(raw));
      } catch (...) {
        cursor = 0;
      }
    }
    int wait_ms = 0;
    if (req.has_param("wait")) {
      const std::string raw = req.get_param_value("wait");
      try {
        wait_ms = std::stoi(raw);
      } catch (...) {
        wait_ms = 0;
      }
    }
    if (wait_ms < 0) wait_ms = 0;
    if (wait_ms > kMaxWaitMillis) wait_ms = kMaxWaitMillis;
    if (wait_ms > 0) session->wait_for_output(cursor, wait_ms);

    std::string data;
    std::size_t next = cursor;
    bool resync = false;
    session->read_since(cursor, &data, &next, &resync);
    json out = json::object();
    out["ok"] = true;
    out["id"] = id;
    out["data_b64"] = tea::base64_encode(data);
    out["cursor"] = static_cast<long long>(next);
    out["resync"] = resync;
    out["ended"] = session->ended();
    out["running"] = session->running();
    out["exit_code"] = session->exit_code();
    send_json(res, out);
  });

  // ---- what the user typed ------------------------------------------------
  svr.Post(R"(/api/term/session/([^/]+)/input)",
           [this](const httplib::Request& req, httplib::Response& res) {
    const std::string id = req.matches[1].str();
    std::shared_ptr<tea::ShellSession> session = shells.find(id);
    if (!session) {
      send_error(res, 404, "not_found", "no such session: " + id);
      return;
    }
    json body;
    if (!parse_json_body(req, &body, res)) return;
    std::string data;
    const std::string b64 = json_string(body, "data_b64");
    if (!b64.empty()) {
      if (!tea::base64_decode(b64, &data)) {
        send_error(res, 400, "bad_request", "'data_b64' is not valid base64");
        return;
      }
    } else {
      data = json_string(body, "data");
    }
    std::string err;
    if (!session->write(data, &err)) {
      send_error(res, 409, "write_failed", err);
      return;
    }
    json out = json::object();
    out["ok"] = true;
    send_json(res, out);
  });

  // ---- the window changed size --------------------------------------------
  svr.Post(R"(/api/term/session/([^/]+)/resize)",
           [this](const httplib::Request& req, httplib::Response& res) {
    const std::string id = req.matches[1].str();
    std::shared_ptr<tea::ShellSession> session = shells.find(id);
    if (!session) {
      send_error(res, 404, "not_found", "no such session: " + id);
      return;
    }
    json body;
    if (!parse_json_body(req, &body, res)) return;
    const int cols = json_int(body, "cols", 0);
    const int rows = json_int(body, "rows", 0);
    if (cols <= 0 || rows <= 0) {
      send_error(res, 400, "bad_request", "'cols' and 'rows' are required");
      return;
    }
    std::string err;
    if (!session->resize(cols, rows, &err)) {
      send_error(res, 409, "resize_failed", err);
      return;
    }
    json out = json::object();
    out["ok"] = true;
    send_json(res, out);
  });

  // ---- end the session ------------------------------------------------------
  svr.Delete(R"(/api/term/session/([^/]+))",
              [this](const httplib::Request& req, httplib::Response& res) {
    const std::string id = req.matches[1].str();
    if (!shells.close(id)) {
      send_error(res, 404, "not_found", "no such session: " + id);
      return;
    }
    json out = json::object();
    out["ok"] = true;
    send_json(res, out);
  });

  // ---- files in and out ------------------------------------------------------
  // The upload lands in the session's directory, or in "dir" when the caller
  // names one. The name is reduced to its last component, so "../../etc/passwd"
  // cannot walk out.
  svr.Post(R"(/api/term/session/([^/]+)/upload)",
           [this](const httplib::Request& req, httplib::Response& res) {
    const std::string id = req.matches[1].str();
    std::shared_ptr<tea::ShellSession> session = shells.find(id);
    if (!session) {
      send_error(res, 404, "not_found", "no such session: " + id);
      return;
    }
    std::string name = req.has_param("name") ? req.get_param_value("name") : "";
    name = tea::path_filename(name);
    if (name.empty() || name == "." || name == "..") {
      send_error(res, 400, "bad_request", "'name' is required");
      return;
    }
    if (req.body.size() > kUploadLimit) {
      send_error(res, 413, "too_large",
                 "the file is larger than " + std::to_string(kUploadLimit / (1024u * 1024u)) +
                     " MB");
      return;
    }
    std::string dir = session->cwd();
    if (dir.empty()) dir = base_dir;
    const std::string want = req.has_param("dir") ? tea::trim(req.get_param_value("dir")) : "";
    if (!want.empty()) {
      dir = tea::path_is_absolute(want) ? tea::path_absolute(want)
                                        : tea::path_absolute(tea::path_join(dir, want));
    }
    if (!tea::make_dirs(dir)) {
      send_error(res, 500, "io_error", "cannot create " + dir);
      return;
    }
    const std::string target = tea::path_join(dir, name);
    std::string err;
    if (!tea::write_file_atomic(target, req.body, &err)) {
      send_error(res, 500, "io_error", "cannot write " + target + ": " + err);
      return;
    }
    json out = json::object();
    out["ok"] = true;
    out["path"] = target;
    out["name"] = name;
    out["dir"] = dir;
    out["bytes"] = static_cast<long long>(req.body.size());
    send_json(res, out);
  });

  // The download: the file the path names, sent as an attachment. A relative
  // path is resolved against the session's directory.
  svr.Get(R"(/api/term/session/([^/]+)/file)",
          [this](const httplib::Request& req, httplib::Response& res) {
    const std::string id = req.matches[1].str();
    std::shared_ptr<tea::ShellSession> session = shells.find(id);
    if (!session) {
      send_error(res, 404, "not_found", "no such session: " + id);
      return;
    }
    const std::string raw = req.has_param("path") ? req.get_param_value("path") : "";
    if (raw.empty()) {
      send_error(res, 400, "bad_request", "'path' is required");
      return;
    }
    std::string base = session->cwd();
    if (base.empty()) base = base_dir;
    const std::string target =
        tea::path_is_absolute(raw) ? tea::path_absolute(raw)
                                    : tea::path_absolute(tea::path_join(base, raw));
    if (!tea::path_is_file(target)) {
      send_error(res, 404, "not_found",
                 tea::path_exists(target) ? "not a regular file" : "file not found: " + target);
      return;
    }
    // The file is streamed, not read into memory: a device may have a big log
    // file, and the tunnel is slow enough without copying it twice.
    auto file = std::make_shared<std::FILE*>(nullptr);
    *file = std::fopen(target.c_str(), "rb");
    if (*file == nullptr) {
      send_error(res, 500, "io_error", "cannot open " + target);
      return;
    }
    const std::string name = tea::path_filename(target);
    res.set_header("Content-Disposition",
                   "attachment; filename=\"" + download_name(name) +
                       "\"; filename*=UTF-8''" + download_name_utf8(name));
    res.set_header("Cache-Control", "no-store");
    res.set_header("Access-Control-Allow-Origin", "*");
    res.set_header("Access-Control-Expose-Headers", "Content-Disposition, Content-Length");
    res.set_content_provider(
        static_cast<size_t>(tea::file_size(target)), mime_for(target),
        [file](size_t offset, size_t length, httplib::DataSink& sink) {
          if (std::fseek(*file, static_cast<long>(offset), SEEK_SET) != 0) return false;
          const size_t want = length < 65536 ? length : 65536;
          std::vector<char> buf(want);
          const size_t got = std::fread(buf.data(), 1, want, *file);
          if (got == 0) {
            sink.done();               // the file ended earlier than promised
            return true;
          }
          if (!sink.write(buf.data(), got)) return false;
          if (got < want) sink.done();
          return true;
        },
        [file](bool) {
          // The response is done with the file (success or not).
          if (*file) {
            std::fclose(*file);
            *file = nullptr;
          }
        });
  });
}

TerminalService::TerminalService() : impl_(new Impl()) {}

TerminalService::~TerminalService() { stop(); }

bool TerminalService::available() {
  return tea::shell_capability().available;
}

bool TerminalService::start(std::string* err) {
  if (impl_->running.load()) return true;

  const tea::ShellCapability cap = tea::shell_capability();
  if (!cap.available) {
    if (err) {
      *err = cap.reason.empty() ? "this machine cannot run a shell" : cap.reason;
    }
    return false;
  }

  // The shell starts in the home directory: it is the one place that always
  // exists and is writable, on a device as much as on a desktop.
  impl_->base_dir = tea::home_dir();
  if (impl_->base_dir.empty() || !tea::path_is_dir(impl_->base_dir)) {
    impl_->base_dir = tea::path_absolute(".");
  }

  impl_->install_routes();
  impl_->svr.set_payload_max_length(kUploadLimit);
  // A browser may wait half a minute for new output, so the write timeout
  // has to be longer than that.
  impl_->svr.set_read_timeout(60, 0);
  impl_->svr.set_write_timeout(120, 0);
  impl_->svr.set_keep_alive_max_count(1024);

  // The service is reached through the tunnel, never directly: it listens on
  // the loopback only.
  const int port = impl_->svr.bind_to_any_port("127.0.0.1");
  if (port <= 0) {
    if (err) *err = "cannot listen on 127.0.0.1";
    return false;
  }
  port_ = static_cast<uint16_t>(port);
  impl_->running.store(true);
  impl_->worker = std::thread([this]() {
    impl_->svr.listen_after_bind();
    impl_->running.store(false);
  });
  return true;
}

void TerminalService::stop() {
  if (impl_ && impl_->running.load()) {
    impl_->svr.stop();
    if (impl_->worker.joinable()) impl_->worker.join();
    impl_->running.store(false);
  }
  if (impl_) impl_->shells.close_all();
  port_ = 0;
}

}  // namespace tea_tunnel
