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
  std::string jpeg;             // the newest picture
  int frame_width = 0;
  int frame_height = 0;
  int64_t frame_at_ms = 0;
  int64_t frames = 0;

  int fps = 2;
  int quality = 0;
  std::atomic<bool> stopping{false};
};

DesktopService::DesktopService() : impl_(new Impl()) {}

DesktopService::~DesktopService() { stop(); }

bool DesktopService::start(int max_width, int fps, int quality, std::string* err) {
  if (running_) return true;
  impl_->fps = fps > 0 ? fps : 2;
  impl_->quality = quality;
  if (!impl_->desktop.start(max_width, err)) return false;

  // The pictures: take one right now so the first request does not wait.
  ScreenFrame frame;
  std::string why;
  if (!impl_->desktop.grab(&frame, &why)) {
    if (err) *err = why;
    impl_->desktop.stop();
    return false;
  }
  std::string jpeg;
  if (!jpeg_encode(frame.bgra.data(), frame.width, frame.height, frame.width * 4, impl_->quality,
                   &jpeg, &why)) {
    if (err) *err = why;
    impl_->desktop.stop();
    return false;
  }
  {
    std::lock_guard<std::mutex> lock(impl_->mu);
    impl_->jpeg.swap(jpeg);
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
    if (impl_->jpeg.empty()) {
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
    res.set_content(impl_->jpeg, "image/jpeg");
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
    body["codec"] = "jpeg";
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
  const int interval = 1000 / (impl_->fps > 0 ? impl_->fps : 2);
  while (!impl_->stopping.load()) {
    const int64_t started = now_ms();
    ScreenFrame frame;
    std::string why;
    if (impl_->desktop.grab(&frame, &why)) {
      std::string jpeg;
      if (jpeg_encode(frame.bgra.data(), frame.width, frame.height, frame.width * 4, impl_->quality,
                      &jpeg, &why)) {
        std::lock_guard<std::mutex> lock(impl_->mu);
        impl_->jpeg.swap(jpeg);
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
