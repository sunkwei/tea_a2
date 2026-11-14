// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_desktop_service.h -- the service the device side of the desktop
// tunnel hands out.
//
// It runs on the machine whose screen is being watched. The tunnel carries
// the bytes like everything else, so the browser on the other side reaches
// this service through the tunnel:
//
//   GET  /api/desktop/frame.jpg  the newest picture (JPEG)
//   GET  /api/desktop/info       the size of the screen and of the picture
//   POST /api/desktop/input      the mouse and the keyboard
//
// The pictures are taken a few times a second (two by default): a screen
// that changes slowly does not need more, and the tunnel stays quiet.
#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <thread>

namespace tea_tunnel {

class DesktopService {
 public:
  DesktopService();
  ~DesktopService();
  DesktopService(const DesktopService&) = delete;
  DesktopService& operator=(const DesktopService&) = delete;

  /// Start the service. `max_width` is the widest picture (0 = the screen
  /// as it is), `fps` how many pictures a second, `quality` the JPEG
  /// quality (0 = the default).
  bool start(int max_width, int fps, int quality, std::string* err);
  void stop();

  bool running() const { return running_; }
  int port() const { return port_; }

  /// The paths the other side calls. They are part of the contract between
  /// the two halves, so both of them use these names.
  static const char* frame_path() { return "/api/desktop/frame.jpg"; }
  static const char* info_path() { return "/api/desktop/info"; }
  static const char* input_path() { return "/api/desktop/input"; }

 private:
  void capture_loop();

  struct Impl;
  std::unique_ptr<Impl> impl_;
  std::thread capture_thread_;
  int port_ = 0;
  bool running_ = false;
};

}  // namespace tea_tunnel
