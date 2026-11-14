// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_desktop_service.cpp -- the device side of the desktop tunnel.
//
// A thread takes pictures of the screen a few times a second and hands the
// newest one to whoever asks. The mouse and the keyboard go the other way:
// the events that arrive are put into this machine's input.
//
// The service listens on the loopback only. The tunnel is what carries it.
#include "tunnel_desktop_service.h"

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

#include "tunnel_desktop.h"
#include "tunnel_video.h"

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

}  // namespace

struct DesktopService::Impl {
  Desktop desktop;
  httplib::Server server;
  std::thread server_thread;

  std::mutex mu;
  std::string picture;          // the newest picture (as it is encoded)
  std::deque<StreamPicture> stream;   // the last pictures, in order
  uint32_t next_seq = 1;               // the number the next picture gets
  int frame_width = 0;
  int frame_height = 0;
  int64_t frame_at_ms = 0;
  int64_t frames = 0;

  double fps = 2.0;
  int bitrate = 2000000;        // bits per second
  int keyint_seconds = 10;      // a key frame every this many seconds
  H264Encoder encoder;          // the H.264 encoder (x264)
  int encoder_width = 0;
  int encoder_height = 0;
  std::atomic<bool> stopping{false};

  /// Encode one picture the way the other side asked for it.
  bool encode(const ScreenFrame& frame, std::string* out, std::string* err);
};


bool DesktopService::Impl::encode(const ScreenFrame& frame, std::string* out,
                                   std::string* err) {
  double fps = 2.0;
  int bitrate = 2000000;
  int keyint = 10;
  {
    std::lock_guard<std::mutex> lock(mu);
    fps = this->fps;
    bitrate = this->bitrate;
    keyint = this->keyint_seconds;
  }
  // H.264 (x264): the screen is BGRA, x264 wants I420.
  if (!encoder.running() || frame.width != encoder_width || frame.height != encoder_height) {
    encoder.stop();
    if (!encoder.start(frame.width, frame.height, fps, bitrate, keyint, err)) return false;
    encoder_width = frame.width;
    encoder_height = frame.height;
  }
  const int64_t started = now_ms();
  YuvFrame yuv;
  bgra_to_yuv(frame.bgra.data(), frame.width, frame.height, frame.width * 4, &yuv);
  bool is_key = true;
  const bool ok = encoder.encode(yuv, started, out, &is_key, err);
  // x264 holds the first picture back until the rate control has settled:
  // that one comes out empty. An empty picture must not go into the stream
  // (the browser would try to decode it).
  if (ok && !out->empty()) {
    std::lock_guard<std::mutex> lock(mu);
    StreamPicture picture;
    picture.seq = next_seq++;
    picture.key = is_key;
    picture.bytes = *out;
    stream.push_back(picture);
    // Keep the last ones only: the other side may be away for a while.
    while (stream.size() > 32) stream.pop_front();
  }
  std::fprintf(stderr, "desktop: the picture is %zu bytes (%.2f MB/s at %d a second)\n",
               out->size(), out->size() * static_cast<double>(fps) / 1048576.0, fps);
  std::fflush(stderr);
  return ok;
}


// ---------------------------------------------------------------------------
// The stream: the pictures, packed
// ---------------------------------------------------------------------------
namespace {

void put_u32(std::string* out, uint32_t value) {
  out->push_back(static_cast<char>((value >> 24) & 0xff));
  out->push_back(static_cast<char>((value >> 16) & 0xff));
  out->push_back(static_cast<char>((value >> 8) & 0xff));
  out->push_back(static_cast<char>(value & 0xff));
}

bool get_u32(const std::string& body, std::size_t at, uint32_t* value) {
  if (at + 4 > body.size()) return false;
  *value = (static_cast<uint32_t>(static_cast<unsigned char>(body[at])) << 24) |
           (static_cast<uint32_t>(static_cast<unsigned char>(body[at + 1])) << 16) |
           (static_cast<uint32_t>(static_cast<unsigned char>(body[at + 2])) << 8) |
           static_cast<uint32_t>(static_cast<unsigned char>(body[at + 3]));
  return true;
}

}  // namespace

std::string pack_pictures(const std::vector<StreamPicture>& pictures) {
  std::string out;
  put_u32(&out, static_cast<uint32_t>(pictures.size()));
  for (const StreamPicture& picture : pictures) {
    put_u32(&out, picture.seq);
    out.push_back(picture.key ? 1 : 0);
    put_u32(&out, static_cast<uint32_t>(picture.bytes.size()));
    out.append(picture.bytes);
  }
  return out;
}

bool unpack_pictures(const std::string& body, std::vector<StreamPicture>* out) {
  if (out == nullptr) return false;
  out->clear();
  uint32_t count = 0;
  if (!get_u32(body, 0, &count)) return false;
  std::size_t at = 4;
  for (uint32_t i = 0; i < count; i++) {
    uint32_t seq = 0;
    uint32_t length = 0;
    if (!get_u32(body, at, &seq)) return false;
    at += 4;
    if (at >= body.size()) return false;
    const bool key = body[at++] != 0;
    if (!get_u32(body, at, &length)) return false;
    at += 4;
    if (at + length > body.size()) return false;
    StreamPicture picture;
    picture.seq = seq;
    picture.key = key;
    picture.bytes = body.substr(at, length);
    at += length;
    out->push_back(picture);
  }
  return true;
}

DesktopService::DesktopService() : impl_(new Impl()) {}

DesktopService::~DesktopService() { stop(); }

bool DesktopService::start(int max_width, int max_height, double fps, int bitrate,
                            int keyint_seconds, std::string* err) {
  if (running_) return true;
  (void)max_height;
  impl_->fps = fps > 0.0 ? fps : 2.0;
  impl_->bitrate = bitrate > 0 ? bitrate : 2000000;
  impl_->keyint_seconds = keyint_seconds > 0 ? keyint_seconds : 10;
  if (!impl_->desktop.start(max_width, max_height, err)) return false;

  // The pictures: take one right now so the first request does not wait.
  ScreenFrame frame;
  std::string why;
  if (!impl_->desktop.grab(&frame, &why)) {
    if (err) *err = why;
    impl_->desktop.stop();
    return false;
  }
  std::string encoded;
  if (!impl_->encode(frame, &encoded, &why)) {
    if (err) *err = why;
    impl_->desktop.stop();
    return false;
  }
  {
    std::lock_guard<std::mutex> lock(impl_->mu);
    impl_->picture.swap(encoded);
    impl_->frame_width = frame.width;
    impl_->frame_height = frame.height;
    impl_->frame_at_ms = now_ms();
    impl_->frames = 1;
  }

  httplib::Server& server = impl_->server;
  // One request per connection: a keep-alive connection holds a worker
  // thread for seconds, and the other side opens a new connection for
  // every picture. The pool would be full of idle connections.
  server.set_keep_alive_max_count(1);

  // Somebody opened this service in a browser (it is the tunnel's own
  // address, so that is easy to do). Say what this is instead of a 404.
  server.Get("/", [this](const httplib::Request&, httplib::Response& res) {
    res.status = 200;
    res.set_header("Cache-Control", "no-store");
    res.set_content(
        "This is the desktop side of the tunnel: this machine's screen.\n"
        "\n"
        "The picture is decoded and shown on the control side - open the\n"
        "viewer's address (the control side prints it when it starts).\n",
        "text/plain; charset=utf-8");
  });

  server.Get(DesktopService::frame_path(), [this](const httplib::Request&, httplib::Response& res) {
    std::lock_guard<std::mutex> lock(impl_->mu);
    if (impl_->picture.empty()) {
      res.status = 204;
      return;
    }
    res.status = 200;
    res.set_header("Cache-Control", "no-store");
    res.set_header("Access-Control-Allow-Origin", "*");
    // The sizes travel with the picture: the other side does not have to
    // ask for them in a second request (that one kept failing).
    res.set_header("X-Screen-Width", std::to_string(impl_->desktop.screen_width()));
    res.set_header("X-Screen-Height", std::to_string(impl_->desktop.screen_height()));
    res.set_header("X-Frame-Width", std::to_string(impl_->frame_width));
    res.set_header("X-Frame-Height", std::to_string(impl_->frame_height));
    // Which codec the bytes are in: the other side has to know.
    res.set_header("X-Codec", "h264");
    res.set_content(impl_->picture, "video/h264");
  });

  // The pictures of the stream: everything after the number the other
  // side already has. They have to arrive in order and without gaps (a
  // missing picture means the decoder has to wait for the next key frame).
  server.Get(DesktopService::frames_path(), [this](const httplib::Request& req,
                                                   httplib::Response& res) {
    uint32_t since = 0;
    if (req.has_param("since")) {
      since = static_cast<uint32_t>(
          std::strtoul(req.get_param_value("since").c_str(), nullptr, 10));
    }
    std::vector<StreamPicture> wanted;
    int frame_width = 0;
    int frame_height = 0;
    {
      std::lock_guard<std::mutex> lock(impl_->mu);
      frame_width = impl_->frame_width;
      frame_height = impl_->frame_height;
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
    // The sizes travel with the pictures: the other side does not have to
    // ask for them in a second request.
    res.set_header("X-Screen-Width", std::to_string(impl_->desktop.screen_width()));
    res.set_header("X-Screen-Height", std::to_string(impl_->desktop.screen_height()));
    res.set_header("X-Frame-Width", std::to_string(frame_width));
    res.set_header("X-Frame-Height", std::to_string(frame_height));
    res.set_content(pack_pictures(wanted), "application/octet-stream");
  });

  server.Get(DesktopService::info_path(), [this](const httplib::Request&, httplib::Response& res) {
    std::lock_guard<std::mutex> lock(impl_->mu);
    json body = json::object();
    body["ok"] = true;
    body["screen_width"] = impl_->desktop.screen_width();
    body["screen_height"] = impl_->desktop.screen_height();
    body["frame_width"] = impl_->frame_width;
    body["frame_height"] = impl_->frame_height;
    body["fps"] = impl_->fps;
    body["codec"] = "h264";
    body["bitrate"] = impl_->bitrate;
    body["keyint"] = impl_->keyint_seconds;
    body["frames"] = impl_->frames;
    body["frame_at_ms"] = impl_->frame_at_ms;
    send_json(res, body);
  });

  server.Post(DesktopService::input_path(), [this](const httplib::Request& req,
                                                   httplib::Response& res) {
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
    int applied = 0;
    int failed = 0;
    std::string why;
    if (body.contains("events") && body["events"].is_array()) {
      for (const json& ev : body["events"]) {
        if (!ev.is_object()) continue;
        const std::string type = ev.value("t", std::string());
        bool ok = true;
        if (type == "move") {
          ok = impl_->desktop.move_mouse(ev.value("x", 0), ev.value("y", 0), &why);
        } else if (type == "down" || type == "up") {
          const int button = ev.value("b", 0);
          ok = impl_->desktop.mouse_button(button, type == "down", &why);
          if (ok && ev.contains("x") && ev.contains("y")) {
            ok = impl_->desktop.move_mouse(ev.value("x", 0), ev.value("y", 0), &why);
          }
        } else if (type == "wheel") {
          ok = impl_->desktop.wheel(ev.value("d", 0), &why);
        } else if (type == "key") {
          const bool down = ev.value("down", true);
          const int vk = ev.value("vk", 0);
          const std::string text = ev.value("text", std::string());
          ok = impl_->desktop.key(vk, text, down, &why);
        } else {
          continue;
        }
        if (ok) {
          applied++;
        } else {
          failed++;
        }
      }
    }
    json out = json::object();
    out["ok"] = failed == 0;
    out["applied"] = applied;
    out["failed"] = failed;
    if (failed > 0) out["error"] = why;
    send_json(res, out, failed > 0 ? 409 : 200);
  });

  // The port: the loopback, a free one.
  const int port = server.bind_to_any_port("127.0.0.1");
  if (port <= 0) {
    if (err) *err = "cannot listen on the loopback";
    impl_->desktop.stop();
    return false;
  }
  port_ = port;
  impl_->server_thread = std::thread([this]() { impl_->server.listen_after_bind(); });
  running_ = true;
  capture_thread_ = std::thread([this]() { capture_loop(); });
  return true;
}

void DesktopService::capture_loop() {
  int interval = static_cast<int>(1000.0 / (impl_->fps > 0.0 ? impl_->fps : 2.0));
  if (interval < 1) interval = 1;
  while (!impl_->stopping.load()) {
    const int64_t started = now_ms();
    ScreenFrame frame;
    std::string why;
    if (impl_->desktop.grab(&frame, &why)) {
      std::string encoded;
      if (impl_->encode(frame, &encoded, &why)) {
        std::lock_guard<std::mutex> lock(impl_->mu);
        impl_->picture.swap(encoded);
        impl_->frame_width = frame.width;
        impl_->frame_height = frame.height;
        impl_->frame_at_ms = now_ms();
        impl_->frames++;
      }
    }
    // Sleep the rest of the interval (a picture that takes long does not
    // make the next one come sooner).
    const int64_t spent = now_ms() - started;
    if (spent < interval) std::this_thread::sleep_for(std::chrono::milliseconds(interval - spent));
  }
}

void DesktopService::stop() {
  if (!running_ && !capture_thread_.joinable()) return;
  impl_->stopping.store(true);
  if (capture_thread_.joinable()) capture_thread_.join();
  impl_->server.stop();
  if (impl_->server_thread.joinable()) impl_->server_thread.join();
  impl_->desktop.stop();
  running_ = false;
}

}  // namespace tea_tunnel
