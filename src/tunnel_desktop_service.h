// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_desktop_service.h -- the service the device side of the desktop
// tunnel hands out.
//
// It runs on the machine whose screen is being watched. The picture is
// taken a few times a second and pushed to the tunnel (the screen data
// channel), and the mouse and the keyboard go the other way.
//
// The service also keeps an HTTP interface on the loopback. It is not
// the way the picture travels any more (the tunnel carries it directly),
// but it is handy on the machine itself: a person there can look at what
// the other side sees, and the tunnel's own port answers with a page
// that says so instead of a 404.
#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "tunnel_screen.h"

namespace tea_tunnel {

class DesktopService {
 public:
  DesktopService();
  ~DesktopService();
  DesktopService(const DesktopService&) = delete;
  DesktopService& operator=(const DesktopService&) = delete;

  /// Start the service. `max_width` and `max_height` are the biggest
  /// picture (0 = the screen as it is), `fps` how many pictures a second,
  /// `bitrate` the bits per second the encoder may use (0 = the default),
  /// `keyint_seconds` how often a key frame comes (0 = every picture is
  /// one).
  bool start(int max_width, int max_height, double fps, int bitrate, int keyint_seconds,
             std::string* err);
  void stop();

  bool running() const { return running_; }
  int port() const { return port_; }

  /// Where the pictures go. The tunnel sets this: it hands every new
  /// picture to the screen data channel. Without it the pictures are only
  /// kept in memory (the HTTP interface serves them).
  using PictureSink = std::function<void(const std::vector<ScreenPicture>&, const ScreenGeometry&)>;
  void set_picture_sink(PictureSink sink);

  /// The mouse and the keyboard, from the tunnel. The body is the JSON
  /// the other side sends ({"events": [...]}) and the answer is the same
  /// JSON the HTTP interface returns ({"ok": true, "applied": 2, ...}).
  /// The tunnel sets this.
  using InputHandler = std::function<std::string(const std::string& body)>;
  void set_input_handler(InputHandler handler);

  /// The paths the HTTP interface answers. They are part of the contract
  /// between the two halves, so both of them use these names.
  static const char* frame_path() { return "/api/desktop/frame"; }
  static const char* frames_path() { return "/api/desktop/frames"; }
  static const char* info_path() { return "/api/desktop/info"; }
  static const char* input_path() { return "/api/desktop/input"; }

  /// Apply the events of a body (the same JSON the HTTP interface takes)
  /// and answer it. The tunnel's screen channel uses this: the events
  /// come over the channel and the answer goes back the same way.
  std::string apply_input(const std::string& body);

 private:
  void capture_loop();

  struct Impl;
  std::unique_ptr<Impl> impl_;
  std::thread capture_thread_;
  int port_ = 0;
  bool running_ = false;
};

}  // namespace tea_tunnel
