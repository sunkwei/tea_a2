// tunnel_control_service.h -- the service the outside side of the desktop
// tunnel hands out.
//
// It stands between the browser and the machine whose screen is watched:
//
//   * a thread pulls the pictures through the tunnel and decodes them
//     (the size of the picture is what the browser needs to place the mouse);
//   * the browser gets the newest picture and the sizes;
//   * what the browser says about the mouse and the keyboard goes back
//     through the tunnel, with the coordinates put into the screen of the
//     other machine.
#pragma once

#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

namespace tea_tunnel {

class ControlService {
 public:
  ControlService();
  ~ControlService();
  ControlService(const ControlService&) = delete;
  ControlService& operator=(const ControlService&) = delete;

  /// Start the service. `proxy_host`/`proxy_port` are the local proxy of the
  /// tunnel (the bytes go through it to the other side); `viewer_port` is
  /// the port the browser opens (0 = a free one).
  bool start(const std::string& proxy_host, int proxy_port, int viewer_port, std::string* err);
  void stop();

  bool running() const { return running_; }
  int port() const { return port_; }

 private:
  void pull_loop();

  struct Impl;
  std::unique_ptr<Impl> impl_;
  std::thread pull_thread_;
  int port_ = 0;
  bool running_ = false;
};

}  // namespace tea_tunnel
