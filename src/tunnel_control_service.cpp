// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_control_service.cpp -- the outside side of the desktop tunnel.
//
// The browser talks to this service on the loopback. The pictures come
// from the tunnel's screen channel (the sidecar hands them over) and the
// mouse and the keyboard go back the same way.
#include "tunnel_control_service.h"

#include <httplib.h>
#include <nlohmann/json.hpp>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "web_assets.h"

namespace tea_tunnel {

namespace {

using json = nlohmann::json;

int64_t now_ms() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}

void send_json(httplib::Response& res, const json& body, int status = 200) {
  res.status = status;
  res.set_header("Access-Control-Allow-Origin", "*");
  res.set_header("Cache-Control", "no-store");
  res.set_content(body.dump(), "application/json; charset=utf-8");
}

/// The page and its script are compiled into the binary (there is no file
/// to read on a device).
const tea::WebAsset* find_asset(const std::string& name) {
  for (std::size_t i = 0; i < tea::kWebAssetCount; i++) {
    if (name == tea::kWebAssets[i].path) return &tea::kWebAssets[i];
  }
  return nullptr;
}

}  // namespace

struct ControlService::Impl {
  httplib::Server server;
  std::thread server_thread;

  std::mutex mu;
  std::string picture;                  // the newest picture (as it came)
  std::deque<ScreenPicture> stream;     // the pictures, in order
  uint32_t last_seq = 0;                // the last number the other side sent
  ScreenGeometry geometry;
  int64_t frame_at_ms = 0;
  int64_t frames = 0;
  int64_t decoded = 0;
  std::string last_error;
  // The state of the machine on the other side: locked (or asleep), and
  // why there is no picture.
  bool locked = false;
  std::string note;

  PictureSource source;
  InputSender sender;

  std::atomic<bool> stopping{false};
};

ControlService::ControlService() : impl_(new Impl()) {}

ControlService::~ControlService() { stop(); }

bool ControlService::start(int viewer_port, std::string* err) {
  if (running_) return true;

  httplib::Server& server = impl_->server;

  // The page the browser opens.
  server.Get("/", [this](const httplib::Request& req, httplib::Response& res) {
    const tea::WebAsset* asset = find_asset("tunnel_desktop.html");
    if (asset == nullptr) {
      res.status = 500;
      res.set_content("the viewer page is not in this build\n", "text/plain; charset=utf-8");
      return;
    }
    res.status = 200;
    res.set_header("Cache-Control", "no-store");
    res.set_content(std::string(asset->data, asset->size), "text/html; charset=utf-8");
  });
  server.Get("/tunnel_desktop.js", [this](const httplib::Request&, httplib::Response& res) {
    const tea::WebAsset* asset = find_asset("tunnel_desktop.js");
    if (asset == nullptr) {
      res.status = 404;
      return;
    }
    res.status = 200;
    res.set_header("Cache-Control", "no-store");
    res.set_content(std::string(asset->data, asset->size), "application/javascript; charset=utf-8");
  });

  // The pictures of the stream, from the number the browser already has.
  server.Get("/api/control/frames", [this](const httplib::Request& req,
                                            httplib::Response& res) {
    uint32_t since = 0;
    if (req.has_param("since")) {
      since = static_cast<uint32_t>(
          std::strtoul(req.get_param_value("since").c_str(), nullptr, 10));
    }
    std::vector<ScreenPicture> wanted;
    {
      std::lock_guard<std::mutex> lock(impl_->mu);
      for (const ScreenPicture& picture : impl_->stream) {
        if (picture.seq > since) wanted.push_back(picture);
      }
    }
    if (wanted.empty()) {
      res.status = 204;
      return;
    }
    res.status = 200;
    res.set_header("Cache-Control", "no-store");
    res.set_header("Access-Control-Allow-Origin", "*");
    res.set_header("X-Codec", "h264");
    res.set_content(pack_screen_pictures(wanted), "application/octet-stream");
  });

  // The newest picture. It is handed over as it came: the browser knows
  // how to read H.264 by itself (WebCodecs), and JPEG when it cannot.
  server.Get("/api/control/frame", [this](const httplib::Request&, httplib::Response& res) {
    std::lock_guard<std::mutex> lock(impl_->mu);
    if (impl_->picture.empty()) {
      res.status = 204;
      return;
    }
    res.status = 200;
    res.set_header("Cache-Control", "no-store");
    res.set_header("Access-Control-Allow-Origin", "*");
    res.set_header("X-Codec", "h264");
    res.set_content(impl_->picture, "video/h264");
  });

  // What the browser needs to know: the size of the picture and the size
  // of the screen it shows (the mouse is placed in the screen's
  // coordinates).
  server.Get("/api/control/info", [this](const httplib::Request&, httplib::Response& res) {
    std::lock_guard<std::mutex> lock(impl_->mu);
    json body = json::object();
    body["ok"] = true;
    body["frame_width"] = impl_->geometry.frame_width;
    body["frame_height"] = impl_->geometry.frame_height;
    body["screen_width"] = impl_->geometry.screen_width;
    body["screen_height"] = impl_->geometry.screen_height;
    body["screen_x"] = impl_->geometry.screen_x;
    body["screen_y"] = impl_->geometry.screen_y;
    body["frames"] = impl_->frames;
    body["codec"] = "h264";
    body["decoded"] = impl_->decoded;
    body["frame_at_ms"] = impl_->frame_at_ms;
    body["locked"] = impl_->locked;
    if (!impl_->note.empty()) body["note"] = impl_->note;
    if (!impl_->last_error.empty()) body["error"] = impl_->last_error;
    send_json(res, body);
  });

  // The mouse and the keyboard: they go back to the other side.
  server.Post("/api/control/input", [this](const httplib::Request& req, httplib::Response& res) {
    json body;
    try {
      body = json::parse(req.body);
    } catch (const std::exception& e) {
      json error = json::object();
      error["code"] = "bad_request";
      error["message"] = std::string("not JSON: ") + e.what();
      json out = json::object();
      out["error"] = error;
      send_json(res, out, 400);
      return;
    }

    // The browser sends the position in the picture's pixels; the other
    // machine wants the position on its own screen.
    ScreenGeometry geometry;
    InputSender sender;
    {
      std::lock_guard<std::mutex> lock(impl_->mu);
      geometry = impl_->geometry;
      sender = impl_->sender;
    }
    const double scale_x =
        (geometry.frame_width > 0 && geometry.screen_width > 0)
            ? static_cast<double>(geometry.screen_width) / geometry.frame_width
            : 1.0;
    const double scale_y =
        (geometry.frame_height > 0 && geometry.screen_height > 0)
            ? static_cast<double>(geometry.screen_height) / geometry.frame_height
            : 1.0;

    json events = json::array();
    if (body.contains("events") && body["events"].is_array()) {
      for (const json& ev : body["events"]) {
        if (!ev.is_object()) continue;
        json out = ev;
        if (ev.contains("x") && ev.contains("y")) {
          // The picture's pixels -> the screen's pixels. The screen may
          // start left of (0,0) (a second monitor), so the origin counts.
          out["x"] = geometry.screen_x + static_cast<int>(ev.value("x", 0) * scale_x);
          out["y"] = geometry.screen_y + static_cast<int>(ev.value("y", 0) * scale_y);
        }
        events.push_back(out);
      }
    }
    json forward = json::object();
    forward["events"] = events;

    if (!sender) {
      json out = json::object();
      out["ok"] = false;
      out["error"] = "the tunnel to the other side is not up";
      send_json(res, out, 502);
      return;
    }
    // Through the screen channel: the answer comes back the same way.
    const std::string answer = sender(forward.dump());
    json out;
    try {
      out = json::parse(answer);
    } catch (const std::exception&) {
      out = json::object();
      out["ok"] = false;
      out["error"] = "the other side did not answer";
      send_json(res, out, 502);
      return;
    }
    send_json(res, out);
  });

  int port = 0;
  if (viewer_port > 0) {
    if (!server.bind_to_port("127.0.0.1", viewer_port)) {
      if (err) *err = "cannot listen on 127.0.0.1:" + std::to_string(viewer_port);
      return false;
    }
    port = viewer_port;
  } else {
    port = server.bind_to_any_port("127.0.0.1");
    if (port <= 0) {
      if (err) *err = "cannot listen on the loopback";
      return false;
    }
  }
  port_ = port;
  impl_->server_thread = std::thread([this]() { impl_->server.listen_after_bind(); });
  running_ = true;
  return true;
}

void ControlService::set_picture_source(PictureSource source) {
  std::lock_guard<std::mutex> lock(impl_->mu);
  impl_->source = std::move(source);
}

/// The pictures came in over the screen channel: keep them, so the
/// browser can ask for the ones it has not seen yet.
void ControlService::on_pictures(const std::vector<ScreenPicture>& pictures,
                                 const ScreenGeometry& geometry) {
  std::lock_guard<std::mutex> lock(impl_->mu);
  for (const ScreenPicture& picture : pictures) {
    impl_->stream.push_back(picture);
    impl_->last_seq = picture.seq;
  }
  // Keep the last ones only: the browser may be away for a while.
  while (impl_->stream.size() > 64) impl_->stream.pop_front();
  if (!impl_->stream.empty()) impl_->picture = impl_->stream.back().bytes;
  impl_->geometry = geometry;
  impl_->frame_at_ms = now_ms();
  impl_->frames++;
  impl_->decoded += static_cast<int64_t>(pictures.size());
  impl_->last_error.clear();
}

void ControlService::on_status(bool locked, const std::string& note) {
  std::lock_guard<std::mutex> lock(impl_->mu);
  impl_->locked = locked;
  impl_->note = note;
}

void ControlService::set_input_sender(InputSender sender) {
  std::lock_guard<std::mutex> lock(impl_->mu);
  impl_->sender = std::move(sender);
}

void ControlService::stop() {
  if (!running_ && !pull_thread_.joinable()) return;
  impl_->stopping.store(true);
  if (pull_thread_.joinable()) pull_thread_.join();
  impl_->server.stop();
  if (impl_->server_thread.joinable()) impl_->server_thread.join();
  running_ = false;
}

}  // namespace tea_tunnel
