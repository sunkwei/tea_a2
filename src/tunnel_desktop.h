// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_desktop.h -- this machine's screen, and the mouse and keyboard on it.
//
// The desktop role of the tunnel hands out two things: pictures of the
// screen (a few a second, compressed) and the ability to move the mouse
// and press the keys.
//
// Two backends do that:
//   * Windows (tunnel_desktop_win.cpp): GDI takes the picture, SendInput
//     moves the pointer.
//   * Linux (tunnel_desktop_linux.cpp): the compositor's own screenshot
//     program takes the picture, a virtual device in the kernel (/dev/uinput)
//     moves the pointer.
//
// The Linux one is the same way the extension layer works on Linux (see
// sched/a2_ext_py/desktop_linux.py): a Wayland desktop only lets the
// compositor take the picture, and the only way to reach a *native*
// Wayland program with the keyboard is a virtual input device - XTest
// stops at the XWayland boundary.
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

  /// Get ready. `max_width` and `max_height` are the size of the picture
  /// to hand out (with both of them the screen is stretched to exactly
  /// that, with only the width the height follows); 0 means "the screen as
  /// it is".
  bool start(int max_width, int max_height, std::string* err);
  void stop();
  bool running() const { return running_; }

  /// Take one picture.
  bool grab(ScreenFrame* out, std::string* err);

  /// Keep the machine awake: no sleep, no screen off, no lock screen.
  ///
  /// The whole point of the remote desktop is that somebody is looking at
  /// this screen from the other side. If the machine sleeps or locks, the
  /// picture stops changing (Windows locks the session and the screen goes
  /// to the lock screen, which GDI cannot even photograph). So while the
  /// desktop is on, the machine must stay awake.
  ///
  /// Returns false with a reason when the platform cannot do it - the
  /// desktop still works, but the machine may fall asleep under it.
  bool hold_awake(std::string* err);

  /// Give the machine back to its own sleep settings (called on stop()).
  void release_awake();

  /// Is the session locked right now? A locked session cannot be
  /// photographed and cannot be driven, so the viewer has to be told
  /// instead of being shown a frozen picture.
  bool session_locked() const;

  /// Why the screen cannot be shown (empty when it can). This is the one
  /// line the operator sees.
  std::string unavailable_reason() const;

  /// The size of the whole (virtual) screen: the coordinates the mouse is
  /// given are on it.
  int screen_width() const { return screen_width_; }
  /// Where the virtual screen starts (a monitor to the left of the first
  /// one makes this negative).
  int screen_x() const { return screen_x_; }
  int screen_y() const { return screen_y_; }
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
  int screen_x_ = 0;
  int screen_y_ = 0;
  int screen_height_ = 0;
};

}  // namespace tea_tunnel
