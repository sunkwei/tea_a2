// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_desktop_win.cpp -- this machine's screen (Windows).
//
// The picture: GDI. The whole virtual screen (all the monitors) is copied
// into a memory bitmap and scaled down to the width that was asked for.
//
// The mouse and the keyboard: SendInput, the same way the remote control
// tools of Windows do it.
#ifdef _WIN32

#include "tunnel_desktop.h"

#include <cstdio>
#include <cstring>

#include <windows.h>
#include <wtsapi32.h>

#include <algorithm>

namespace tea_tunnel {


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

/// The session's lock state, as Windows reports it.
///
/// WTSQuerySessionInformation(WTSSessionInfoEx) answers with a buffer
/// whose layout is not the same in every SDK, so the fields are read by
/// hand instead of through a struct. The bytes are
///
///   0  u32  Level (= 1)
///   8  u32  SessionId
///  12  u32  SessionState
///  16  u32  SessionFlags   (0 = locked, 1 = unlocked)
///  20  ...  WinStationName, UserName, ...
struct SessionState {
  bool known = false;
  bool locked = false;
};

/// Ask Windows whether this session is locked.
///
/// Two sources, because neither is enough on its own:
///
///  * the input desktop. While the session is locked (or a UAC prompt is
///    up) the input desktop is the secure one ("Winlogon"), not
///    "Default";
///  * the session flags (WTS_SESSIONSTATE_LOCK). That is the direct
///    answer, but it only exists on Windows 8 and later.
///
/// Either one saying "locked" is enough.
SessionState query_session_state() {
  SessionState out;

  HDESK input = ::OpenInputDesktop(0, FALSE, DESKTOP_READOBJECTS);
  if (input != nullptr) {
    wchar_t name[256] = {0};
    DWORD needed = 0;
    if (::GetUserObjectInformationW(input, UOI_NAME, name, sizeof(name), &needed)) {
      out.known = true;
      if (::lstrcmpiW(name, L"Default") != 0) out.locked = true;
    }
    ::CloseDesktop(input);
  }

  // The flags: 0 = locked, 1 = unlocked. (On Windows 7 the two are the
  // other way round - a documented defect in the OS.)
  PWTSINFOEXW info = nullptr;
  DWORD bytes = 0;
  if (::WTSQuerySessionInformationW(WTS_CURRENT_SERVER_HANDLE, WTS_CURRENT_SESSION,
                                   WTSSessionInfoEx, reinterpret_cast<LPWSTR*>(&info),
                                   &bytes)) {
    out.known = true;
    const unsigned char* raw = reinterpret_cast<const unsigned char*>(info);
    if (bytes >= 20) {
      uint32_t flags = 0;
      std::memcpy(&flags, raw + 16, sizeof(flags));
      if (flags == WTS_SESSIONSTATE_LOCK) out.locked = true;
    }
    ::WTSFreeMemory(info);
  }
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
  // The thread that holds the "stay awake" request, and the event that
  // tells it to stop (see hold_awake).
  HANDLE awake_thread = nullptr;
  HANDLE awake_stop = nullptr;
  // Why the screen cannot be shown, in words for the operator.
  std::string unavailable;
};

/// The thread that asks Windows to keep the machine awake.
///
/// SetThreadExecutionState works per thread: the request lasts only as
/// long as the thread that made it. So it has to be a thread of its own.
///
/// ES_CONTINUOUS      the request stays until we say otherwise
/// ES_SYSTEM_REQUIRED  the machine must not sleep
/// ES_DISPLAY_REQUIRED  the screen must not go off
///
/// The request is asked for again every 20 seconds: another program
/// (a media player, a presentation) can take it away, and the machine
/// must not fall asleep under the viewer.
DWORD WINAPI awake_thread_proc(LPVOID param) {
  while (true) {
    ::SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED);
    // Wait for the stop event, but not longer than 20 seconds: the
    // request has to be renewed.
    if (param != nullptr) ::WaitForSingleObject(param, 20 * 1000);
    else ::Sleep(20 * 1000);
    if (param != nullptr && ::WaitForSingleObject(param, 0) == WAIT_OBJECT_0) break;
  }
  // Let the machine sleep again when we are done.
  ::SetThreadExecutionState(ES_CONTINUOUS);
  return 0;
}

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
  release_awake();
  impl_->scratch.release();
  impl_->scaled.release();
}

bool Desktop::hold_awake(std::string* err) {
  if (!running_) {
    if (err) *err = "the screen is not open";
    return false;
  }
  // The event tells the thread to stop; it is closed in release_awake.
  HANDLE stop_event = ::CreateEventW(nullptr, TRUE, FALSE, nullptr);
  if (stop_event == nullptr) {
    if (err) *err = "cannot create the event that ends the awake thread";
    return false;
  }
  HANDLE thread = ::CreateThread(nullptr, 0, awake_thread_proc, stop_event, 0, nullptr);
  if (thread == nullptr) {
    ::CloseHandle(stop_event);
    if (err) *err = "cannot start the thread that keeps the machine awake";
    return false;
  }
  // The request itself is made **in the new thread**: it only counts
  // for the thread that asked for it, so asking here would be pointless
  // (it would be gone the moment this function returns).
  impl_->awake_thread = thread;
  impl_->awake_stop = stop_event;
  return true;
}

void Desktop::release_awake() {
  // The event is closed **after** the thread is gone: the thread waits on
  // that handle, and closing it under its feet would be a race.
  if (impl_->awake_stop != nullptr) ::SetEvent(impl_->awake_stop);
  if (impl_->awake_thread != nullptr) {
    ::WaitForSingleObject(impl_->awake_thread, 5000);
    ::CloseHandle(impl_->awake_thread);
    impl_->awake_thread = nullptr;
  }
  if (impl_->awake_stop != nullptr) {
    ::CloseHandle(impl_->awake_stop);
    impl_->awake_stop = nullptr;
  }
}

bool Desktop::session_locked() const {
  return query_session_state().locked;
}

std::string Desktop::unavailable_reason() const {
  return impl_->unavailable;
}

bool Desktop::grab(ScreenFrame* out, std::string* err) {
  if (!running_) {
    if (err) *err = "the screen is not open";
    return false;
  }
  const VirtualScreen& area = impl_->area;
  // A locked session cannot be photographed: Windows has put the secure
  // desktop on the screen, and GDI can only see the user's own desktop.
  // Say so instead of handing out a picture of nothing.
  const SessionState state = query_session_state();
  impl_->unavailable.clear();
  if (state.locked) {
    // The secure desktop is up: either the session is locked, or a system
    // dialog (UAC) has the input. Both mean the same thing to the viewer:
    // the screen cannot be photographed and the mouse cannot reach it.
    impl_->unavailable =
        "this machine's screen is not available (locked, or a system dialog has the input)";
    if (err) *err = impl_->unavailable;
    return false;
  }
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

}  // namespace tea_tunnel

#endif  // _WIN32
