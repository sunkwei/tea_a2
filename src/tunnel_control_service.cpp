// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_control_service.cpp -- the outside side of the desktop tunnel.
//
// The browser talks to this service on the loopback. This service talks to
// the other side of the tunnel through the local proxy (the same proxy the
// browser's own requests would go through), so the tunnel stays the only
// way between the two machines.
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

#include "tunnel_desktop_service.h"
#include "tunnel_video.h"
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
  std::string proxy_host = "127.0.0.1";
  int proxy_port = 0;

  std::mutex mu;
  std::string picture;          // the newest picture (as it came)
  std::deque<StreamPicture> stream;   // the pictures, in order
  uint32_t last_seq = 0;               // the last number the other side sent
  int frame_width = 0;
  int frame_height = 0;
  int screen_width = 0;
  int screen_height = 0;
  int screen_x = 0;
  int screen_y = 0;
  int64_t frame_at_ms = 0;
  int64_t frames = 0;
  int64_t decoded = 0;
  std::string last_error;

  std::atomic<bool> stopping{false};
};

ControlService::ControlService() : impl_(new Impl()) {}

ControlService::~ControlService() { stop(); }

bool ControlService::start(const std::string& proxy_host, int proxy_port, int viewer_port,
                           std::string* err) {
  if (running_) return true;
  if (proxy_port <= 0) {
    if (err) *err = "the tunnel has no local proxy to go through";
    return false;
  }
  impl_->proxy_host = proxy_host.empty() ? "127.0.0.1" : proxy_host;
  impl_->proxy_port = proxy_port;

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
    std::vector<StreamPicture> wanted;
    {
      std::lock_guard<std::mutex> lock(impl_->mu);
      for (const StreamPicture& picture : impl_->stream) {
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
    res.set_content(pack_pictures(wanted), "application/octet-stream");
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


  // What the browser needs to know: the size of the picture and the size of
  // the screen it shows (the mouse is placed in the screen's coordinates).
  server.Get("/api/control/info", [this](const httplib::Request&, httplib::Response& res) {
    std::lock_guard<std::mutex> lock(impl_->mu);
    json body = json::object();
    body["ok"] = true;
    body["frame_width"] = impl_->frame_width;
    body["frame_height"] = impl_->frame_height;
    body["screen_width"] = impl_->screen_width;
    body["screen_height"] = impl_->screen_height;
    body["screen_x"] = impl_->screen_x;
    body["screen_y"] = impl_->screen_y;
    body["frames"] = impl_->frames;
    body["codec"] = "h264";
    body["decoded"] = impl_->decoded;
    body["frame_at_ms"] = impl_->frame_at_ms;
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
    int frame_width = 0;
    int frame_height = 0;
    int screen_width = 0;
    int screen_height = 0;
    int screen_x = 0;
    int screen_y = 0;
    {
      std::lock_guard<std::mutex> lock(impl_->mu);
      frame_width = impl_->frame_width;
      frame_height = impl_->frame_height;
      screen_width = impl_->screen_width;
      screen_height = impl_->screen_height;
      screen_x = impl_->screen_x;
      screen_y = impl_->screen_y;
    }
    const double scale_x =
        (frame_width > 0 && screen_width > 0) ? static_cast<double>(screen_width) / frame_width : 1.0;
    const double scale_y =
        (frame_height > 0 && screen_height > 0) ? static_cast<double>(screen_height) / frame_height : 1.0;

    json events = json::array();
    if (body.contains("events") && body["events"].is_array()) {
      for (const json& ev : body["events"]) {
        if (!ev.is_object()) continue;
        json out = ev;
        if (ev.contains("x") && ev.contains("y")) {
          // The picture's pixels -> the screen's pixels. The screen may
          // start left of (0,0) (a second monitor), so the origin counts.
          out["x"] = screen_x + static_cast<int>(ev.value("x", 0) * scale_x);
          out["y"] = screen_y + static_cast<int>(ev.value("y", 0) * scale_y);
        }
        events.push_back(out);
      }
    }
    json forward = json::object();
    forward["events"] = events;

    // Through the tunnel: the local proxy carries it to the other side.
    httplib::Client cli(impl_->proxy_host, impl_->proxy_port);
    cli.set_connection_timeout(5, 0);
    cli.set_read_timeout(10, 0);
    cli.set_keep_alive(false);
    auto res2 = cli.Post(DesktopService::input_path(), forward.dump(), "application/json");
    json out = json::object();
    if (!res2 || res2->status != 200) {
      out["ok"] = false;
      out["error"] = res2 ? ("the other side answered " + std::to_string(res2->status))
                          : std::string("the other side did not answer");
      send_json(res, out, 502);
      return;
    }
    try {
      out = json::parse(res2->body);
    } catch (const std::exception&) {
      out = json::object();
      out["ok"] = true;
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
  pull_thread_ = std::thread([this]() { pull_loop(); });
  return true;
}

void ControlService::pull_loop() {
  httplib::Client cli(impl_->proxy_host, impl_->proxy_port);
  cli.set_connection_timeout(5, 0);
  cli.set_read_timeout(10, 0);
  // Every request gets its own connection: the connection through the proxy
  // is closed from time to time (the far end keeps it only a few seconds),
  // and a client that reuses a dead socket just fails from then on.
  cli.set_keep_alive(false);
  int64_t last_info = 0;
  while (!impl_->stopping.load()) {
    // The pictures. They come through the tunnel, so it is the other
    // machine's screen that is on it. They have to keep their order and
    // come without gaps: a missing picture means the decoder has to wait
    // for the next key frame.
    uint32_t since = 0;
    {
      std::lock_guard<std::mutex> lock(impl_->mu);
      since = impl_->last_seq;
    }
    std::string url = std::string(DesktopService::frames_path()) + "?since=" +
                      std::to_string(since);
    auto res = cli.Get(url);
    if (!res) {
      std::fprintf(stderr, "desktop: the pictures did not come through\n");
      std::fflush(stderr);
    } else if (res->status != 200) {
      std::fprintf(stderr, "desktop: the pictures came back with %d\n", res->status);
      std::fflush(stderr);
    }
    if (res && res->status == 200 && !res->body.empty()) {
      // The pictures are handed over as they are: the sizes and the codec
      // come with them in the headers, so nothing has to be decoded here.
      int width = 0;
      int height = 0;
      int screen_width = 0;
      int screen_height = 0;
      int screen_x = 0;
      int screen_y = 0;
      if (res->has_header("X-Frame-Width")) {
        width = std::atoi(res->get_header_value("X-Frame-Width").c_str());
      }
      if (res->has_header("X-Frame-Height")) {
        height = std::atoi(res->get_header_value("X-Frame-Height").c_str());
      }
      if (res->has_header("X-Screen-Width")) {
        screen_width = std::atoi(res->get_header_value("X-Screen-Width").c_str());
      }
      if (res->has_header("X-Screen-Height")) {
        screen_height = std::atoi(res->get_header_value("X-Screen-Height").c_str());
      }
      if (res->has_header("X-Screen-X")) {
        screen_x = std::atoi(res->get_header_value("X-Screen-X").c_str());
      }
      if (res->has_header("X-Screen-Y")) {
        screen_y = std::atoi(res->get_header_value("X-Screen-Y").c_str());
      }
      std::vector<StreamPicture> pictures;
      if (!unpack_pictures(res->body, &pictures) || pictures.empty()) {
        std::lock_guard<std::mutex> lock(impl_->mu);
        impl_->last_error = "the pictures from the other side could not be read";
      } else {
        std::lock_guard<std::mutex> lock(impl_->mu);
        for (const StreamPicture& picture : pictures) {
          impl_->stream.push_back(picture);
          impl_->last_seq = picture.seq;
        }
        while (impl_->stream.size() > 64) impl_->stream.pop_front();
        impl_->picture = impl_->stream.back().bytes;
        impl_->frame_width = width;
        impl_->frame_height = height;
        if (screen_width > 0) impl_->screen_width = screen_width;
        if (screen_height > 0) impl_->screen_height = screen_height;
        impl_->screen_x = screen_x;
        impl_->screen_y = screen_y;
        impl_->frame_at_ms = now_ms();
        impl_->frames++;
        impl_->decoded += static_cast<int64_t>(pictures.size());
        impl_->last_error.clear();
      }
    }
    // The sizes of the screen come with the picture (its headers), so
    // there is no second request to make.
    // A fifth of a second between the pictures: the other side sends two a
    // second, so there is nothing to gain from asking faster.
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
  }
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
