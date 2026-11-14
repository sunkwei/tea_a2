// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_desktop_service.h -- the service the device side of the desktop
// tunnel hands out.
//
// It runs on the machine whose screen is being watched. The tunnel carries
// the bytes like everything else, so the browser on the other side reaches
// this service through the tunnel:
//
//   GET  /api/desktop/frame      the newest picture (H.264, or JPEG if the
//                                 other side asks for it)
//   GET  /api/desktop/info       the size of the screen and of the picture
//   POST /api/desktop/codec      "h264" or "jpeg": which one to send
//   POST /api/desktop/input      the mouse and the keyboard
//
// The pictures are taken a few times a second (two by default): a screen
// that changes slowly does not need more, and the tunnel stays quiet.
#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <thread>
#include <vector>

namespace tea_tunnel {

/// One picture of the stream: its number, whether it is a key frame, and
/// the bytes.
struct StreamPicture {
  uint32_t seq = 0;
  bool key = false;
  std::string bytes;
};

/// The pictures of the stream, packed into one body (this is what travels
/// through the tunnel):
///
///   4 bytes   how many pictures
///   per picture: 4 bytes the number, 1 byte (1 = a key frame),
///                4 bytes the length, then the bytes
std::string pack_pictures(const std::vector<StreamPicture>& pictures);

/// Unpack what pack_pictures wrote. False when the body is broken.
bool unpack_pictures(const std::string& body, std::vector<StreamPicture>* out);

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

  /// The paths the other side calls. They are part of the contract between
  /// the two halves, so both of them use these names.
  static const char* frame_path() { return "/api/desktop/frame"; }
  static const char* frames_path() { return "/api/desktop/frames"; }
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
