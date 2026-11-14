// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_desktop.h -- this machine's screen, and the mouse and keyboard on it.
//
// The desktop role of the tunnel hands out two things: pictures of the
// screen (a few a second, compressed) and the ability to move the mouse and
// press the keys. This file is the Windows half of that: GDI takes the
// picture, SendInput moves the pointer.
//
// Everywhere else there is no desktop here, and the service says so.
#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace tea_tunnel {

/// One picture of the screen.
struct ScreenFrame {
  int width = 0;
  int height = 0;
  int64_t captured_at_ms = 0;
  std::vector<uint8_t> bgra;   // width * height * 4, blue first
};

/// The screen of this machine.
class Desktop {
 public:
  Desktop();
  ~Desktop();
  Desktop(const Desktop&) = delete;
  Desktop& operator=(const Desktop&) = delete;

  /// Get ready. `max_width` is the widest picture to hand out (the screen
  /// is scaled down to that); 0 means "the screen as it is".
  bool start(int max_width, std::string* err);
  void stop();
  bool running() const { return running_; }

  /// Take one picture.
  bool grab(ScreenFrame* out, std::string* err);

  /// The size of the whole (virtual) screen: the coordinates the mouse is
  /// given are on it.
  int screen_width() const { return screen_width_; }
  int screen_height() const { return screen_height_; }

  /// Put the pointer where it is told.
  bool move_mouse(int x, int y, std::string* err);
  /// Press or let go of a button (0 = left, 1 = middle, 2 = right).
  bool mouse_button(int button, bool down, std::string* err);
  /// Turn the wheel (positive = away from the user).
  bool wheel(int delta, std::string* err);
  /// Press or let go of a key. `vk` is the Windows virtual key code; when it
  /// is 0 the text (the character the user typed) is what goes in.
  bool key(int vk, const std::string& text, bool down, std::string* err);

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
  bool running_ = false;
  int screen_width_ = 0;
  int screen_height_ = 0;
};

}  // namespace tea_tunnel
