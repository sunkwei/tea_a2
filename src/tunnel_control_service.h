// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_control_service.h -- the service the outside side of the desktop
// tunnel hands out.
//
// It stands between the browser and the machine whose screen is watched:
//
//   * the pictures come in over the tunnel's screen channel (the sidecar
//     hands them over) and are kept here;
//   * the browser gets the newest picture and the sizes;
//   * what the browser says about the mouse and the keyboard goes back
//     over the same channel, with the coordinates put into the screen of
//     the other machine.
//
// The browser's own requests never leave this machine: they are answered
// from what came over the channel.
#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "tunnel_screen.h"

namespace tea_tunnel {

class ControlService {
 public:
  ControlService();
  ~ControlService();
  ControlService(const ControlService&) = delete;
  ControlService& operator=(const ControlService&) = delete;

  /// Start the service. `viewer_port` is the port the browser opens
  /// (0 = a free one).
  bool start(int viewer_port, std::string* err);
  void stop();

  bool running() const { return running_; }
  int port() const { return port_; }

  /// Where the pictures come from: the sidecar hands over what the
  /// screen channel brought.
  using PictureSource = std::function<void(const std::vector<ScreenPicture>&, const ScreenGeometry&)>;
  void set_picture_source(PictureSource source);

  /// Where the mouse and the keyboard go: the sidecar puts them on the
  /// screen channel. It returns the answer (the JSON the device side
  /// sent back).
  using InputSender = std::function<std::string(const std::string& body)>;
  void set_input_sender(InputSender sender);

  /// The pictures came in over the screen channel: keep them, so the
  /// browser can ask for the ones it has not seen yet.
  void on_pictures(const std::vector<ScreenPicture>& pictures, const ScreenGeometry& geometry);

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
  std::thread pull_thread_;
  int port_ = 0;
  bool running_ = false;
};

}  // namespace tea_tunnel
