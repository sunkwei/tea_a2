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
  std::deque<ScreenPicture> stream;   // the last pictures, in order
  uint32_t next_seq = 1;               // the number the next picture gets
  bool last_key = false;               // whether the last picture was a key frame
  int frame_width = 0;
  int frame_height = 0;
  int64_t frame_at_ms = 0;
  int64_t frames = 0;
  // The state of the machine: locked, and why.
  bool locked = false;
  std::string note;

  double fps = 2.0;
  int bitrate = 2000000;        // bits per second
  int keyint_seconds = 10;      // a key frame every this many seconds
  H264Encoder encoder;          // the H.264 encoder (x264)
  int encoder_width = 0;
  int encoder_height = 0;
  std::atomic<bool> stopping{false};

  /// Where the new pictures go (the tunnel's screen channel).
  PictureSink sink;
  /// The state of the machine (locked or not) goes here.
  StatusSink status_sink;
  /// The input events from the tunnel.
  InputHandler input_handler;

  /// Encode one picture the way the other side asked for it.
  bool encode(const ScreenFrame& frame, std::string* out, std::string* err);

  /// Tell the tunnel that the machine is locked (or that it is not any
  /// more). The sink is called outside the lock.
  void report_status();
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
    ScreenPicture picture;
    picture.seq = next_seq++;
    picture.key = is_key;
    picture.bytes = *out;
    stream.push_back(picture);
    last_key = is_key;
    // Keep the last ones only: the other side may be away for a while.
    while (stream.size() > 32) stream.pop_front();
  }
  // The number of pictures a second is a double: it must not be printed
  // as an int (that is undefined behaviour and it showed up as a wild
  // number in the log).
  std::fprintf(stderr, "desktop: the picture is %zu bytes (%.2f MB/s at %.1f a second)\n",
               out->size(), out->size() * static_cast<double>(fps) / 1048576.0, fps);
  std::fflush(stderr);
  return ok;
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

  // The machine must stay awake while the screen is being watched: a
  // sleeping machine stops drawing, and a locked one shows its own lock
  // screen (which GDI cannot photograph at all). Not being able to do
  // this is not fatal - the desktop still works, it just may fall asleep
  // under the viewer.
  std::string awake_why;
  if (!impl_->desktop.hold_awake(&awake_why)) {
    std::fprintf(stderr, "desktop: cannot keep the machine awake: %s\n",
                 awake_why.c_str());
  }

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
    std::vector<ScreenPicture> wanted;
    int frame_width = 0;
    int frame_height = 0;
    {
      std::lock_guard<std::mutex> lock(impl_->mu);
      frame_width = impl_->frame_width;
      frame_height = impl_->frame_height;
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
    // The sizes travel with the pictures: the other side does not have to
    // ask for them in a second request.
    res.set_header("X-Screen-Width", std::to_string(impl_->desktop.screen_width()));
    res.set_header("X-Screen-Height", std::to_string(impl_->desktop.screen_height()));
    res.set_header("X-Screen-X", std::to_string(impl_->desktop.screen_x()));
    res.set_header("X-Screen-Y", std::to_string(impl_->desktop.screen_y()));
    res.set_header("X-Frame-Width", std::to_string(frame_width));
    res.set_header("X-Frame-Height", std::to_string(frame_height));
    res.set_content(pack_screen_pictures(wanted), "application/octet-stream");
  });

  server.Get(DesktopService::info_path(), [this](const httplib::Request&, httplib::Response& res) {
    std::lock_guard<std::mutex> lock(impl_->mu);
    json body = json::object();
    body["ok"] = true;
    body["screen_width"] = impl_->desktop.screen_width();
    body["screen_height"] = impl_->desktop.screen_height();
    body["screen_x"] = impl_->desktop.screen_x();
    body["screen_y"] = impl_->desktop.screen_y();
    body["frame_width"] = impl_->frame_width;
    body["frame_height"] = impl_->frame_height;
    body["fps"] = impl_->fps;
    body["codec"] = "h264";
    body["bitrate"] = impl_->bitrate;
    body["keyint"] = impl_->keyint_seconds;
    body["frames"] = impl_->frames;
    body["frame_at_ms"] = impl_->frame_at_ms;
    body["locked"] = impl_->locked;
    if (!impl_->note.empty()) body["note"] = impl_->note;
    send_json(res, body);
  });

  server.Post(DesktopService::input_path(), [this](const httplib::Request& req,
                                                   httplib::Response& res) {
    // The same work the tunnel's screen channel does: the events are put
    // into this machine's input either way.
    const std::string answer = apply_input(req.body);
    json out;
    try {
      out = json::parse(answer);
    } catch (const std::exception&) {
      json error = json::object();
      error["code"] = "bad_request";
      error["message"] = "the events are not JSON";
      out = json::object();
      out["error"] = error;
      send_json(res, out, 400);
      return;
    }
    send_json(res, out, out.value("failed", 0) > 0 ? 409 : 200);
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

/// Put the events of a body into this machine's input and say what
/// happened. The body is the JSON the other side sends
/// ({"events": [...]}), the answer is the same JSON the HTTP interface
/// returns.
std::string DesktopService::apply_input(const std::string& body) {
  json parsed;
  try {
    parsed = json::parse(body);
  } catch (const std::exception& e) {
    json error = json::object();
    error["code"] = "bad_request";
    error["message"] = std::string("not JSON: ") + e.what();
    json out = json::object();
    out["error"] = error;
    return out.dump();
  }
  int applied = 0;
  int failed = 0;
  int last_x = 0;
  int last_y = 0;
  bool moved = false;
  std::string why;
  if (parsed.contains("events") && parsed["events"].is_array()) {
    for (const json& ev : parsed["events"]) {
      if (!ev.is_object()) continue;
      const std::string type = ev.value("t", std::string());
      bool ok = true;
      if (type == "move") {
        last_x = ev.value("x", 0);
        last_y = ev.value("y", 0);
        moved = true;
        ok = impl_->desktop.move_mouse(last_x, last_y, &why);
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
  if (moved) {
    // Where the mouse ended up (the screen's own coordinates ✓).
    out["x"] = last_x;
    out["y"] = last_y;
  }
  if (failed > 0) out["error"] = why;
  return out.dump();
}

void DesktopService::Impl::report_status() {
  StatusSink sink;
  bool locked = false;
  std::string note;
  {
    std::lock_guard<std::mutex> lock(mu);
    sink = status_sink;
    locked = this->locked;
    note = this->note;
  }
  if (sink) sink(locked, note);
}

void DesktopService::set_picture_sink(PictureSink sink) {
  std::lock_guard<std::mutex> lock(impl_->mu);
  impl_->sink = std::move(sink);
}

void DesktopService::set_status_sink(StatusSink sink) {
  std::lock_guard<std::mutex> lock(impl_->mu);
  impl_->status_sink = std::move(sink);
}

void DesktopService::set_input_handler(InputHandler handler) {
  std::lock_guard<std::mutex> lock(impl_->mu);
  impl_->input_handler = std::move(handler);
}

void DesktopService::capture_loop() {
  int interval = static_cast<int>(1000.0 / (impl_->fps > 0.0 ? impl_->fps : 2.0));
  if (interval < 1) interval = 1;
  while (!impl_->stopping.load()) {
    const int64_t started = now_ms();
    ScreenFrame frame;
    std::string why;
    if (impl_->desktop.grab(&frame, &why)) {
      // The screen is there: the machine is awake and not locked.
      bool was_locked = false;
      {
        // The state is also read by the HTTP interface, so it is written
        // under the lock (a std::string that is being replaced while
        // somebody reads it is a crash waiting to happen).
        std::lock_guard<std::mutex> lock(impl_->mu);
        was_locked = impl_->locked;
        impl_->locked = false;
        impl_->note.clear();
      }
      // The other side is told only when the state changes.
      if (was_locked) impl_->report_status();
      std::string encoded;
      if (impl_->encode(frame, &encoded, &why)) {
        // The picture that was just made is handed to the tunnel (the
        // screen channel). The sink is called outside the lock: it writes
        // to the channel, and that must not hold up the next picture.
        PictureSink sink;
        ScreenGeometry geometry;
        ScreenPicture picture;
        {
          std::lock_guard<std::mutex> lock(impl_->mu);
          impl_->picture.swap(encoded);
          impl_->frame_width = frame.width;
          impl_->frame_height = frame.height;
          impl_->frame_at_ms = now_ms();
          impl_->frames++;
          sink = impl_->sink;
          picture.seq = impl_->next_seq - 1;
          picture.key = impl_->last_key;
          picture.bytes = impl_->picture;
          geometry.frame_width = impl_->frame_width;
          geometry.frame_height = impl_->frame_height;
          geometry.screen_width = impl_->desktop.screen_width();
          geometry.screen_height = impl_->desktop.screen_height();
          geometry.screen_x = impl_->desktop.screen_x();
          geometry.screen_y = impl_->desktop.screen_y();
        }
        if (sink) {
          std::vector<ScreenPicture> pictures;
          pictures.push_back(picture);
          sink(pictures, geometry);
        }
      }
    } else {
      // No picture. The usual reason is that the session is locked: the
      // viewer has to be told, otherwise it just sees a picture that
      // never changes and no explanation.
      const bool locked = impl_->desktop.session_locked();
      // The backend says why (it knows what it saw); when it has nothing
      // to say, the reason of the failed grab is used, and if even that is
      // empty, a plain sentence.
      std::string note = locked ? impl_->desktop.unavailable_reason() : why;
      if (note.empty() && locked) {
        note = "this machine's screen is not available (locked)";
      }
      if (note.empty()) note = "cannot take a picture of the screen";
      bool changed = false;
      {
        std::lock_guard<std::mutex> lock(impl_->mu);
        changed = (locked != impl_->locked || note != impl_->note);
        impl_->locked = locked;
        impl_->note = note;
      }
      if (changed) impl_->report_status();
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
  // The machine may sleep again now that nobody is watching.
  impl_->desktop.stop();
  running_ = false;
}

}  // namespace tea_tunnel
