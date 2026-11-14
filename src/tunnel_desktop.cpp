// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_desktop.cpp -- the screen of this machine (Windows).
//
// The picture: GDI. The whole virtual screen (all the monitors) is copied
// into a memory bitmap and scaled down to the width that was asked for.
//
// The mouse and the keyboard: SendInput, the same way the remote control
// tools of Windows do it.
#include "tunnel_desktop.h"

#include <cstdio>
#include <cstring>

#ifdef _WIN32
#include <windows.h>

#include <algorithm>
#endif

namespace tea_tunnel {

#ifdef _WIN32

namespace {

/// Windows hands a process that is not DPI aware the **logical** pixels of
/// the screen: on a 150% screen 2194x1234 becomes 1463x823, and the picture
/// we take has only that many pixels — it looks like a cropped desktop.
/// Saying "I am DPI aware" gets the real pixels.
void make_dpi_aware() {
  static bool done = false;
  if (done) return;
  done = true;
  using SetContextFn = BOOL(WINAPI*)(HANDLE);
  HMODULE user32 = ::GetModuleHandleW(L"user32.dll");
  if (user32 != nullptr) {
    SetContextFn set_context = reinterpret_cast<SetContextFn>(
        reinterpret_cast<void*>(::GetProcAddress(user32, "SetProcessDpiAwarenessContext")));
    if (set_context != nullptr) {
      // DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = (HANDLE)-4
      if (set_context(reinterpret_cast<HANDLE>(static_cast<LONG_PTR>(-4)))) return;
    }
  }
  ::SetProcessDPIAware();
}

/// The area the pointer is given in: the whole virtual screen, with its
/// origin (a second monitor to the left of the first one has negative
/// coordinates).
struct VirtualScreen {
  int x = 0;
  int y = 0;
  int width = 0;
  int height = 0;
};

/// The primary display (the one the user looks at). Its top left is always
/// (0,0) in the virtual screen's coordinates, and the mouse is placed
/// relative to it.
VirtualScreen virtual_screen() {
  VirtualScreen out;
  out.x = 0;
  out.y = 0;
  out.width = GetSystemMetrics(SM_CXSCREEN);
  out.height = GetSystemMetrics(SM_CYSCREEN);
  return out;
}

/// A bitmap in memory that GDI can draw into.
class Dib {
 public:
  bool create(int width, int height) {
    release();
    width_ = width;
    height_ = height;
    screen_dc_ = ::GetDC(nullptr);
    if (screen_dc_ == nullptr) return false;
    memory_dc_ = ::CreateCompatibleDC(screen_dc_);
    if (memory_dc_ == nullptr) {
      release();
      return false;
    }
    BITMAPINFO info;
    std::memset(&info, 0, sizeof(info));
    info.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    info.bmiHeader.biWidth = width;
    // A negative height means "the first row is the top one", which is the
    // order the picture is in.
    info.bmiHeader.biHeight = -height;
    info.bmiHeader.biPlanes = 1;
    info.bmiHeader.biBitCount = 32;
    info.bmiHeader.biCompression = BI_RGB;
    void* bits = nullptr;
    bitmap_ = ::CreateDIBSection(memory_dc_, &info, DIB_RGB_COLORS, &bits, nullptr, 0);
    if (bitmap_ == nullptr || bits == nullptr) {
      release();
      return false;
    }
    bits_ = static_cast<uint8_t*>(bits);
    old_ = ::SelectObject(memory_dc_, bitmap_);
    return true;
  }

  void release() {
    if (memory_dc_ != nullptr) {
      if (old_ != nullptr) ::SelectObject(memory_dc_, old_);
      ::DeleteDC(memory_dc_);
      memory_dc_ = nullptr;
    }
    if (bitmap_ != nullptr) {
      ::DeleteObject(bitmap_);
      bitmap_ = nullptr;
    }
    if (screen_dc_ != nullptr) {
      ::ReleaseDC(nullptr, screen_dc_);
      screen_dc_ = nullptr;
    }
    bits_ = nullptr;
    old_ = nullptr;
  }

  ~Dib() { release(); }

  HDC memory() const { return memory_dc_; }
  uint8_t* bits() const { return bits_; }
  int width() const { return width_; }
  int height() const { return height_; }
  size_t stride() const { return static_cast<size_t>(width_) * 4; }

 private:
  HDC screen_dc_ = nullptr;
  HDC memory_dc_ = nullptr;
  HBITMAP bitmap_ = nullptr;
  HGDIOBJ old_ = nullptr;
  uint8_t* bits_ = nullptr;
  int width_ = 0;
  int height_ = 0;
};

}  // namespace

struct Desktop::Impl {
  Dib scratch;          // the whole screen, at its own size
  Dib scaled;           // the picture we hand out
  VirtualScreen area;
  int max_width = 0;
  int max_height = 0;
};

Desktop::Desktop() : impl_(new Impl()) {}
Desktop::~Desktop() { stop(); }

bool Desktop::start(int max_width, int max_height, std::string* err) {
  make_dpi_aware();
  impl_->area = virtual_screen();
  if (impl_->area.width <= 0 || impl_->area.height <= 0) {
    if (err) *err = "this machine has no screen";
    return false;
  }
  screen_width_ = impl_->area.width;
  screen_x_ = impl_->area.x;
  screen_y_ = impl_->area.y;
  screen_height_ = impl_->area.height;
  impl_->max_width = max_width > 0 ? max_width : impl_->area.width;
  impl_->max_height = max_height > 0 ? max_height : 0;

  if (!impl_->scratch.create(impl_->area.width, impl_->area.height)) {
    if (err) *err = "cannot make a bitmap for the screen";
    return false;
  }
  running_ = true;
  return true;
}

void Desktop::stop() {
  running_ = false;
  impl_->scratch.release();
  impl_->scaled.release();
}

bool Desktop::grab(ScreenFrame* out, std::string* err) {
  if (!running_) {
    if (err) *err = "the screen is not open";
    return false;
  }
  const VirtualScreen& area = impl_->area;
  HDC screen = ::GetDC(nullptr);
  if (screen == nullptr) {
    if (err) *err = "cannot reach the screen";
    return false;
  }
  const BOOL copied = ::BitBlt(impl_->scratch.memory(), 0, 0, area.width, area.height, screen, area.x,
                               area.y, SRCCOPY | CAPTUREBLT);
  ::ReleaseDC(nullptr, screen);
  if (!copied) {
    if (err) *err = "cannot copy the screen";
    return false;
  }

  // The picture we hand out: the screen, scaled to the size that was asked
  // for (--resize WxH stretches it, --width only limits the width). The
  // sides are even (the H.264 encoder wants that).
  int width = area.width;
  int height = area.height;
  if (impl_->max_width > 0 && impl_->max_height > 0 && area.width > 0 && area.height > 0) {
    // --resize: exactly that size. The picture is stretched if the shape
    // is different (the mouse coordinates are scaled on each side, so it
    // still lines up).
    width = impl_->max_width;
    height = impl_->max_height;
  } else if (impl_->max_width > 0 && area.width > impl_->max_width) {
    // Only a width was asked for: the height follows.
    width = impl_->max_width;
    height = static_cast<int>(static_cast<long long>(area.height) * impl_->max_width / area.width);
  }
  width = std::max(2, (width / 2) * 2);    // even numbers please
  height = std::max(2, (height / 2) * 2);
  if (impl_->scaled.width() != width || impl_->scaled.height() != height) {
    if (!impl_->scaled.create(width, height)) {
      if (err) *err = "cannot make a bitmap for the picture";
      return false;
    }
  }
  if (width == area.width && height == area.height) {
    std::memcpy(impl_->scaled.bits(), impl_->scratch.bits(),
                impl_->scratch.stride() * static_cast<size_t>(area.height));
  } else {
    ::SetStretchBltMode(impl_->scaled.memory(), HALFTONE);
    ::StretchBlt(impl_->scaled.memory(), 0, 0, width, height, impl_->scratch.memory(), 0, 0,
                 area.width, area.height, SRCCOPY);
  }

  out->width = width;
  out->height = height;
  out->bgra.assign(impl_->scaled.bits(), impl_->scaled.bits() + impl_->scaled.stride() *
                                                                static_cast<size_t>(height));
  out->captured_at_ms = 0;
  return true;
}

bool Desktop::move_mouse(int x, int y, std::string* err) {
  if (!running_) {
    if (err) *err = "the screen is not open";
    return false;
  }
  const VirtualScreen& area = impl_->area;
  // SendInput wants the position in a 0..65535 grid over the whole screen.
  const int nx = area.width > 1 ? (x - area.x) * 65535 / (area.width - 1) : 0;
  const int ny = area.height > 1 ? (y - area.y) * 65535 / (area.height - 1) : 0;
  INPUT input;
  std::memset(&input, 0, sizeof(input));
  input.type = INPUT_MOUSE;
  input.mi.dx = nx;
  input.mi.dy = ny;
  // Without MOUSEEVENTF_VIRTUALDESK the position is relative to the
  // primary display - that is the one we are showing.
  input.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE;
  if (::SendInput(1, &input, sizeof(input)) != 1) {
    if (err) *err = "the mouse did not move";
    return false;
  }
  return true;
}

bool Desktop::mouse_button(int button, bool down, std::string* err) {
  if (!running_) {
    if (err) *err = "the screen is not open";
    return false;
  }
  DWORD flags = 0;
  switch (button) {
    case 0: flags = down ? MOUSEEVENTF_LEFTDOWN : MOUSEEVENTF_LEFTUP; break;
    case 1: flags = down ? MOUSEEVENTF_MIDDLEDOWN : MOUSEEVENTF_MIDDLEUP; break;
    case 2: flags = down ? MOUSEEVENTF_RIGHTDOWN : MOUSEEVENTF_RIGHTUP; break;
    default:
      if (err) *err = "there is no such mouse button";
      return false;
  }
  INPUT input;
  std::memset(&input, 0, sizeof(input));
  input.type = INPUT_MOUSE;
  input.mi.dwFlags = flags;
  if (::SendInput(1, &input, sizeof(input)) != 1) {
    if (err) *err = "the mouse button did not go through";
    return false;
  }
  return true;
}

bool Desktop::wheel(int delta, std::string* err) {
  if (!running_) {
    if (err) *err = "the screen is not open";
    return false;
  }
  if (delta == 0) return true;
  INPUT input;
  std::memset(&input, 0, sizeof(input));
  input.type = INPUT_MOUSE;
  input.mi.dwFlags = MOUSEEVENTF_WHEEL;
  input.mi.mouseData = static_cast<DWORD>(delta);
  if (::SendInput(1, &input, sizeof(input)) != 1) {
    if (err) *err = "the wheel did not turn";
    return false;
  }
  return true;
}

bool Desktop::key(int vk, const std::string& text, bool down, std::string* err) {
  if (!running_) {
    if (err) *err = "the screen is not open";
    return false;
  }
  INPUT input;
  std::memset(&input, 0, sizeof(input));
  input.type = INPUT_KEYBOARD;
  if (vk > 0) {
    // A named key (Enter, Tab, an arrow...): the virtual key code is what
    // the keyboard driver wants.
    input.ki.wVk = static_cast<WORD>(vk);
    input.ki.wScan = static_cast<WORD>(MapVirtualKeyW(static_cast<UINT>(vk), MAPVK_VK_TO_VSC));
    input.ki.dwFlags = down ? (KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY)
                             : (KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP);
    if (input.ki.wScan == 0) input.ki.dwFlags = down ? 0 : KEYEVENTF_KEYUP;
  } else {
    // A character: the text itself goes in (this is how you type a letter
    // that is not on the keyboard of the other machine).
    if (text.empty()) return true;
    const UINT16 ch = static_cast<UINT16>(static_cast<unsigned char>(text[0]));
    input.ki.wScan = ch;
    input.ki.dwFlags = KEYEVENTF_UNICODE | (down ? 0 : KEYEVENTF_KEYUP);
  }
  if (::SendInput(1, &input, sizeof(input)) != 1) {
    if (err) *err = "the key did not go through";
    return false;
  }
  return true;
}

#else  // !_WIN32

struct Desktop::Impl {};

Desktop::Desktop() : impl_(new Impl()) {}
Desktop::~Desktop() { stop(); }

bool Desktop::start(int max_width, int max_height, std::string* err) {
  (void)max_width;
  (void)max_height;
  if (err) *err = "there is no desktop on this platform";
  return false;
}

void Desktop::stop() { running_ = false; }

bool Desktop::grab(ScreenFrame* out, std::string* err) {
  (void)out;
  if (err) *err = "there is no desktop on this platform";
  return false;
}

bool Desktop::move_mouse(int x, int y, std::string* err) {
  (void)x;
  (void)y;
  if (err) *err = "there is no desktop on this platform";
  return false;
}

bool Desktop::mouse_button(int button, bool down, std::string* err) {
  (void)button;
  (void)down;
  if (err) *err = "there is no desktop on this platform";
  return false;
}

bool Desktop::wheel(int delta, std::string* err) {
  (void)delta;
  if (err) *err = "there is no desktop on this platform";
  return false;
}

bool Desktop::key(int vk, const std::string& text, bool down, std::string* err) {
  (void)vk;
  (void)text;
  (void)down;
  if (err) *err = "there is no desktop on this platform";
  return false;
}

#endif  // _WIN32

}  // namespace tea_tunnel
