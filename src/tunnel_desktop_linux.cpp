// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_desktop_linux.cpp -- this machine's screen (Linux).
//
// The picture: the compositor's own screenshot program. On Wayland only
// the compositor knows what is on the screen - a rootless XWayland server
// sees the X11 programs and nothing else - so the picture has to come
// from there. Which program that is depends on the compositor:
//
//   spectacle       KDE (Plasma)
//   grim            wlroots (Sway, Hyprland, ...)
//   gnome-screenshot  GNOME
//   maim            the ones that have it
//
// All of them write a PNG, which tunnel_png.cpp reads.
//
// The mouse and the keyboard: a virtual device in the kernel
// (/dev/uinput). Its events are *real* input events, so every program
// sees them: the compositor hands them to the client that has the focus,
// Wayland programs included. XTest would stop at the XWayland boundary
// and reach nothing but the X11 programs.
//
// The keyboard uses the kernel's key codes, not the Windows virtual key
// codes the browser sends: the letters come out as the text (the layout
// of this machine is read from the X server, which gets it from the
// compositor), and the named keys are translated one by one.
#include "tunnel_desktop.h"

#ifndef _WIN32

#include <fcntl.h>
#include <linux/input.h>
#include <linux/uinput.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "tunnel_png.h"

namespace tea_tunnel {

namespace linux_backend {

// --- the screenshot programs ----------------------------------------------

struct ScreenshotTool {
  const char* program;
  const char* arguments;   // the file name is added at the end
};

// The first one that is installed wins. Every program writes the file
// where it is told to, so the name goes at the end of the arguments.
const ScreenshotTool kTools[] = {
    {"spectacle", "-b -n -f -p -o"},            // KDE: background, no notice, full screen
    {"grim", "-"},                               // wlroots
    {"gnome-screenshot", "-f -o"},               // GNOME
    {"maim", "-"},                               // the ones that have it
    {"scrot", "-o"},                             // X11
};

bool program_exists(const char* program) {
  const char* path = std::getenv("PATH");
  if (path == nullptr) return false;
  const std::string paths(path);
  std::size_t at = 0;
  while (at <= paths.size()) {
    const std::size_t end = paths.find(':', at);
    const std::string folder = paths.substr(at, end == std::string::npos ? std::string::npos : end - at);
    if (!folder.empty()) {
      const std::string full = folder + "/" + program;
      if (::access(full.c_str(), X_OK) == 0) return true;
    }
    if (end == std::string::npos) break;
    at = end + 1;
  }
  return false;
}

/// The compositor's screenshot program needs to know which Wayland display
/// to talk to. When the sidecar is started from a service (systemd, cron)
/// that variable is often not set, and the program then aborts instead of
/// taking the picture - so it is looked up here: the socket in the
/// runtime directory is the display.
void ensure_wayland_display() {
  if (std::getenv("WAYLAND_DISPLAY") != nullptr) return;
  const char* runtime = std::getenv("XDG_RUNTIME_DIR");
  if (runtime == nullptr || *runtime == '\0') return;
  const std::string folder(runtime);
  // wayland-0, wayland-1, ... : the first one that is there wins.
  for (int index = 0; index < 16; index++) {
    const std::string name = folder + "/wayland-" + std::to_string(index);
    if (::access(name.c_str(), F_OK) != 0) continue;
    ::setenv("WAYLAND_DISPLAY", ("wayland-" + std::to_string(index)).c_str(), 1);
    return;
  }
}

/// Take a picture with the compositor's program. The picture lands in
/// `path` as a PNG.
bool take_picture(const ScreenshotTool& tool, const std::string& path, std::string* err) {
  ensure_wayland_display();
  // The file of the last picture is removed first: a program that fails
  // leaves the old one behind, and that would look like a picture.
  ::unlink(path.c_str());
  // The program is run without a shell, so the file name needs no quoting.
  std::vector<std::string> parts;
  const std::string args(tool.arguments);
  std::size_t at = 0;
  while (at < args.size()) {
    const std::size_t end = args.find(' ', at);
    parts.push_back(args.substr(at, end == std::string::npos ? std::string::npos : end - at));
    if (end == std::string::npos) break;
    at = end + 1;
  }

  pid_t child = ::fork();
  if (child < 0) {
    if (err) *err = "cannot start the screenshot program";
    return false;
  }
  if (child == 0) {
    // The child: run the program with the file name at the end.
    std::vector<std::string> words;
    words.emplace_back(tool.program);
    for (std::string& part : parts) words.push_back(part);
    words.push_back(path);
    std::vector<char*> argv;
    for (std::string& word : words) argv.push_back(&word[0]);
    argv.push_back(nullptr);
    // A program that writes to the terminal would mess the output up.
    ::freopen("/dev/null", "w", stderr);
    ::execvp(tool.program, argv.data());
    ::_exit(127);
  }

  int status = 0;
  while (::waitpid(child, &status, 0) < 0) {
    if (errno != EINTR) {
      if (err) *err = "the screenshot program did not finish";
      return false;
    }
  }
  // Some of these programs abort (a core dump) when they cannot talk to
  // the compositor, so the exit code alone does not tell whether there is
  // a picture: the file is what counts.
  if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
    struct stat info {};
    if (::stat(path.c_str(), &info) != 0 || info.st_size == 0) {
      if (err) {
        *err = std::string("the screenshot program (") + tool.program + ") did not work";
      }
      return false;
    }
  }
  return true;
}

// --- the keyboard ---------------------------------------------------------

/// The kernel's key codes for the keys the browser names. The browser
/// sends the Windows virtual key code (13 = Enter, 9 = Tab, ...), which
/// is what the numbers here are.
struct NamedKey {
  int vk;          // the Windows virtual key code
  int code;        // the kernel's code
};

const NamedKey kNamedKeys[] = {
    {13, KEY_ENTER},    {9, KEY_TAB},        {27, KEY_ESC},
    {8, KEY_BACKSPACE}, {46, KEY_DELETE},   {36, KEY_HOME},
    {35, KEY_END},      {33, KEY_PAGEUP},   {34, KEY_PAGEDOWN},
    {38, KEY_UP},       {40, KEY_DOWN},     {37, KEY_LEFT},
    {39, KEY_RIGHT},    {16, KEY_LEFTSHIFT},{17, KEY_LEFTCTRL},
    {18, KEY_LEFTALT},
};

/// The US layout: what a key produces without and with shift. It is the
/// fallback when the X server cannot tell us the layout.
const char* kUsLetters = "qwertyuiopasdfghjklzxcvbnm";
const char* kUsLettersShift = "QWERTYUIOPASDFGHJKLZXCVBNM";

/// The key code of a character, and whether shift has to be held down.
bool key_for_char(char c, int* code, bool* shift) {
  if (c >= 'a' && c <= 'z') {
    *code = KEY_A + (c - 'a');
    *shift = false;
    return true;
  }
  if (c >= 'A' && c <= 'Z') {
    *code = KEY_A + (c - 'A');
    *shift = true;
    return true;
  }
  if (c >= '1' && c <= '9') {
    *code = KEY_1 + (c - '1');
    *shift = false;
    return true;
  }
  static const struct {
    char plain;
    char shifted;
    int code;
  } kPunctuation[] = {
      {'-', '_', KEY_MINUS}, {'=', '+', KEY_EQUAL},  {'[', '{', KEY_LEFTBRACE},
      {']', '}', KEY_RIGHTBRACE}, {'\\', '|', KEY_BACKSLASH}, {';', ':', KEY_SEMICOLON},
      {'\'', '"', KEY_APOSTROPHE}, {'`', '~', KEY_GRAVE}, {',', '<', KEY_COMMA},
      {'.', '>', KEY_DOT},    {'/', '?', KEY_SLASH}, {' ', ' ', KEY_SPACE},
  };
  for (const auto& entry : kPunctuation) {
    if (c == entry.plain) {
      *code = entry.code;
      *shift = entry.plain != entry.shifted;
      return true;
    }
    if (c == entry.shifted) {
      *code = entry.code;
      *shift = true;
      return true;
    }
  }
  return false;
}

// --- the virtual device ----------------------------------------------------

/// A virtual input device in the kernel (/dev/uinput).
///
/// It is created when it is needed and destroyed again right after: the
/// kernel forgets it, the compositor notices the new device and takes it
/// into account. The pointer is an **absolute** device (the axes are the
/// pixels of the screen), so a click on a pixel of the picture lands on
/// that pixel.
class VirtualDevice {
 public:
  ~VirtualDevice() { close(); }

  /// A device with absolute axes (the pointer) and buttons.
  bool create_pointer(int width, int height, std::string* err) {
    name_ = "a2-virtual-mouse";
    // The axes are the pixels of the screen.
    abs_max_[ABS_X] = width > 0 ? width - 1 : 0;
    abs_max_[ABS_Y] = height > 0 ? height - 1 : 0;
    has_abs_ = true;
    rels_ = {REL_WHEEL};
    buttons_ = {BTN_LEFT, BTN_RIGHT, BTN_MIDDLE};
    return create(err);
  }

  /// A device with the keys.
  bool create_keyboard(std::string* err) {
    name_ = "a2-virtual-keyboard";
    // Every key the layout can produce, so that any of them works.
    for (int code = KEY_ESC; code <= KEY_MAX; code++) {
      if (code == KEY_LEFTALT || code == KEY_LEFTCTRL || code == KEY_LEFTSHIFT ||
          code == KEY_CAPSLOCK || code == KEY_NUMLOCK || code == KEY_SCROLLLOCK ||
          code == KEY_RIGHTALT || code == KEY_RIGHTCTRL || code == KEY_RIGHTSHIFT ||
          code == KEY_MENU || code == KEY_COMPOSE || code == KEY_PAUSE ||
          code == KEY_SYSRQ || code == KEY_HOME || code == KEY_UP || code == KEY_PAGEUP ||
          code == KEY_LEFT || code == KEY_RIGHT || code == KEY_END || code == KEY_DOWN ||
          code == KEY_PAGEDOWN || code == KEY_INSERT || code == KEY_DELETE ||
          code == KEY_RIGHTBRACE || code == KEY_LEFTBRACE || code == KEY_DOWN ||
          code == KEY_F1 || code == KEY_F12) {
        continue;
      }
      keys_.push_back(code);
    }
    for (const NamedKey& named : kNamedKeys) keys_.push_back(named.code);
    for (int code = KEY_A; code <= KEY_Z; code++) keys_.push_back(code);
    for (int code = KEY_1; code <= KEY_0; code++) keys_.push_back(code);
    for (int code = KEY_F1; code <= KEY_F10; code++) keys_.push_back(code);
    for (int code = KEY_NUMLOCK; code <= KEY_KPDOT; code++) keys_.push_back(code);
    keys_.push_back(KEY_MINUS);
    keys_.push_back(KEY_EQUAL);
    keys_.push_back(KEY_LEFTBRACE);
    keys_.push_back(KEY_RIGHTBRACE);
    keys_.push_back(KEY_BACKSLASH);
    keys_.push_back(KEY_SEMICOLON);
    keys_.push_back(KEY_APOSTROPHE);
    keys_.push_back(KEY_GRAVE);
    keys_.push_back(KEY_COMMA);
    keys_.push_back(KEY_DOT);
    keys_.push_back(KEY_SLASH);
    keys_.push_back(KEY_SPACE);
    return create(err);
  }

  bool valid() const { return fd_ >= 0; }

  /// One input event (the kernel's struct input_event).
  void emit(int type, int code, int value) {
    if (fd_ < 0) return;
    input_event event{};
    event.type = static_cast<__u16>(type);
    event.code = static_cast<__u16>(code);
    event.value = static_cast<__s32>(value);
    const ssize_t written = ::write(fd_, &event, sizeof(event));
    (void)written;
  }

  /// End the frame: the kernel sends the events to the clients then.
  void sync() { emit(EV_SYN, SYN_REPORT, 0); }

 private:
  bool create(std::string* err) {
    fd_ = ::open("/dev/uinput", O_WRONLY | O_NONBLOCK | O_CLOEXEC);
    if (fd_ < 0) {
      if (err) {
        *err = "cannot open /dev/uinput (the virtual device needs write permission on it)";
      }
      return false;
    }
    // struct uinput_user_dev: the name, the ids, and the four arrays of
    // the axes.
    uinput_user_dev dev{};
    std::memset(&dev, 0, sizeof(dev));
    std::snprintf(dev.name, sizeof(dev.name), "%s", name_.c_str());
    dev.id.bustype = BUS_USB;
    dev.id.vendor = 0x1234;
    dev.id.product = 0x5678;
    dev.id.version = 1;
    for (int code = 0; code < ABS_CNT; code++) {
      dev.absmax[code] = abs_max_[code];
    }
    if (::write(fd_, &dev, sizeof(dev)) != static_cast<ssize_t>(sizeof(dev))) {
      if (err) *err = "cannot write the virtual device";
      close();
      return false;
    }
    if (has_abs_) {
      ::ioctl(fd_, UI_SET_EVBIT, EV_ABS);
      ::ioctl(fd_, UI_SET_ABSBIT, ABS_X);
      ::ioctl(fd_, UI_SET_ABSBIT, ABS_Y);
    }
    if (!rels_.empty()) {
      ::ioctl(fd_, UI_SET_EVBIT, EV_REL);
      for (int rel : rels_) ::ioctl(fd_, UI_SET_RELBIT, rel);
    }
    if (!buttons_.empty() || !keys_.empty()) {
      ::ioctl(fd_, UI_SET_EVBIT, EV_KEY);
      for (int button : buttons_) ::ioctl(fd_, UI_SET_KEYBIT, button);
      for (int key : keys_) ::ioctl(fd_, UI_SET_KEYBIT, key);
    }
    if (::ioctl(fd_, UI_DEV_CREATE) < 0) {
      if (err) *err = "cannot create the virtual device";
      close();
      return false;
    }
    // The compositor needs a moment to notice the new device - otherwise
    // the first events land nowhere.
    ::usleep(200000);
    return true;
  }

  void close() {
    if (fd_ < 0) return;
    ::ioctl(fd_, UI_DEV_DESTROY);
    ::close(fd_);
    fd_ = -1;
  }

  int fd_ = -1;
  std::string name_;
  bool has_abs_ = false;
  int abs_max_[ABS_CNT] = {0};
  std::vector<int> keys_;
  std::vector<int> rels_;
  std::vector<int> buttons_;
};

}  // namespace linux_backend

using linux_backend::ScreenshotTool;
using linux_backend::VirtualDevice;

struct Desktop::Impl {
  int max_width = 0;
  int max_height = 0;
  int screen_width = 0;
  int screen_height = 0;
  linux_backend::ScreenshotTool tool{};
  bool have_tool = false;
  std::string picture_path;
  linux_backend::VirtualDevice pointer;
  linux_backend::VirtualDevice keyboard;
};

Desktop::Desktop() : impl_(new Impl()) {}
Desktop::~Desktop() { stop(); }

bool Desktop::start(int max_width, int max_height, std::string* err) {
  // The picture comes from the compositor's program.
  for (const linux_backend::ScreenshotTool& candidate : linux_backend::kTools) {
    if (!linux_backend::program_exists(candidate.program)) continue;
    impl_->tool = candidate;
    impl_->have_tool = true;
    break;
  }
  if (!impl_->have_tool) {
    if (err) {
      *err = "there is no desktop on this platform (no screenshot program found: "
             "spectacle, grim, gnome-screenshot or maim)";
    }
    return false;
  }

  // The size of the screen: the picture itself says how big it is.
  // The file the compositor writes into. The name has the process id in
  // it: two tunnels on one machine (or one that is restarted while the
  // old one is still going) must not fight over the same file.
  impl_->picture_path = "/tmp/a2-tunnel-screen-" + std::to_string(::getpid()) + ".png";
  PngImage probe;
  std::string why;
  if (!linux_backend::take_picture(impl_->tool, impl_->picture_path, &why) ||
      !png_read_file(impl_->picture_path, &probe, &why)) {
    if (err) *err = why.empty() ? "cannot take a picture of the screen" : why;
    return false;
  }
  impl_->screen_width = probe.width;
  impl_->screen_height = probe.height;
  if (impl_->screen_width <= 0 || impl_->screen_height <= 0) {
    if (err) *err = "the screenshot came back empty";
    return false;
  }

  screen_width_ = impl_->screen_width;
  screen_height_ = impl_->screen_height;
  screen_x_ = 0;
  screen_y_ = 0;
  impl_->max_width = max_width > 0 ? max_width : impl_->screen_width;
  impl_->max_height = max_height > 0 ? max_height : 0;

  // The pointer: absolute, so a click on a pixel lands on that pixel.
  if (!impl_->pointer.create_pointer(impl_->screen_width, impl_->screen_height, err)) {
    return false;
  }
  if (!impl_->keyboard.create_keyboard(err)) {
    return false;
  }
  running_ = true;
  return true;
}

void Desktop::stop() {
  running_ = false;
  impl_->pointer = VirtualDevice();
  impl_->keyboard = VirtualDevice();
  if (!impl_->picture_path.empty()) {
    ::unlink(impl_->picture_path.c_str());
  }
}

bool Desktop::grab(ScreenFrame* out, std::string* err) {
  if (!running_) {
    if (err) *err = "the screen is not open";
    return false;
  }
  std::string why;
  if (!linux_backend::take_picture(impl_->tool, impl_->picture_path, &why)) {
    if (err) *err = why;
    return false;
  }
  PngImage image;
  if (!png_read_file(impl_->picture_path, &image, &why)) {
    if (err) *err = why;
    return false;
  }

  // The picture we hand out: the screen, scaled to the size that was
  // asked for (--resize WxH stretches it, --width only limits the width).
  // The sides are even (the H.264 encoder wants that).
  int width = image.width;
  int height = image.height;
  if (impl_->max_width > 0 && impl_->max_height > 0) {
    width = impl_->max_width;
    height = impl_->max_height;
  } else if (impl_->max_width > 0 && image.width > impl_->max_width) {
    width = impl_->max_width;
    height = static_cast<int>(static_cast<long long>(image.height) * impl_->max_width / image.width);
  }
  width = std::max(2, (width / 2) * 2);
  height = std::max(2, (height / 2) * 2);

  out->width = width;
  out->height = height;
  out->captured_at_ms = 0;
  if (width == image.width && height == image.height) {
    out->bgra = std::move(image.bgra);
    return true;
  }

  // Scale it down. The picture is a simple box filter: every pixel of the
  // small picture is the average of the ones it stands for.
  out->bgra.assign(static_cast<std::size_t>(width) * static_cast<std::size_t>(height) * 4, 0);
  for (int y = 0; y < height; y++) {
    const int y0 = static_cast<int>(static_cast<long long>(y) * image.height / height);
    int y1 = static_cast<int>(static_cast<long long>(y + 1) * image.height / height);
    if (y1 <= y0) y1 = y0 + 1;
    if (y1 > image.height) y1 = image.height;
    for (int x = 0; x < width; x++) {
      const int x0 = static_cast<int>(static_cast<long long>(x) * image.width / width);
      int x1 = static_cast<int>(static_cast<long long>(x + 1) * image.width / width);
      if (x1 <= x0) x1 = x0 + 1;
      if (x1 > image.width) x1 = image.width;
      int sums[4] = {0, 0, 0, 0};
      int count = 0;
      for (int sy = y0; sy < y1; sy++) {
        for (int sx = x0; sx < x1; sx++) {
          const uint8_t* pixel = image.bgra.data() +
                                (static_cast<std::size_t>(sy) * image.width + sx) * 4;
          sums[0] += pixel[0];
          sums[1] += pixel[1];
          sums[2] += pixel[2];
          sums[3] += pixel[3];
          count++;
        }
      }
      if (count == 0) count = 1;
      uint8_t* target = out->bgra.data() +
                        (static_cast<std::size_t>(y) * width + x) * 4;
      for (int channel = 0; channel < 4; channel++) {
        target[channel] = static_cast<uint8_t>(sums[channel] / count);
      }
    }
  }
  return true;
}

bool Desktop::move_mouse(int x, int y, std::string* err) {
  if (!running_) {
    if (err) *err = "the screen is not open";
    return false;
  }
  if (!impl_->pointer.valid()) {
    if (err) *err = "the virtual mouse is gone";
    return false;
  }
  // The pointer is absolute: the axes are the pixels of the screen.
  const int px = std::max(0, std::min(x, impl_->screen_width - 1));
  const int py = std::max(0, std::min(y, impl_->screen_height - 1));
  impl_->pointer.emit(EV_ABS, ABS_X, px);
  impl_->pointer.emit(EV_ABS, ABS_Y, py);
  impl_->pointer.sync();
  return true;
}

bool Desktop::mouse_button(int button, bool down, std::string* err) {
  if (!running_) {
    if (err) *err = "the screen is not open";
    return false;
  }
  if (!impl_->pointer.valid()) {
    if (err) *err = "the virtual mouse is gone";
    return false;
  }
  int code = 0;
  switch (button) {
    case 0: code = BTN_LEFT; break;
    case 1: code = BTN_MIDDLE; break;
    case 2: code = BTN_RIGHT; break;
    default:
      if (err) *err = "there is no such mouse button";
      return false;
  }
  impl_->pointer.emit(EV_KEY, code, down ? 1 : 0);
  impl_->pointer.sync();
  return true;
}

bool Desktop::wheel(int delta, std::string* err) {
  if (!running_) {
    if (err) *err = "the screen is not open";
    return false;
  }
  if (!impl_->pointer.valid()) {
    if (err) *err = "the virtual mouse is gone";
    return false;
  }
  if (delta == 0) return true;
  impl_->pointer.emit(EV_REL, REL_WHEEL, delta);
  impl_->pointer.sync();
  return true;
}

bool Desktop::key(int vk, const std::string& text, bool down, std::string* err) {
  if (!running_) {
    if (err) *err = "the screen is not open";
    return false;
  }
  if (!impl_->keyboard.valid()) {
    if (err) *err = "the virtual keyboard is gone";
    return false;
  }

  // A named key (Enter, Tab, an arrow...): the browser sends the Windows
  // virtual key code, which is translated to the kernel's code.
  if (vk > 0) {
    int code = 0;
    for (const linux_backend::NamedKey& named : linux_backend::kNamedKeys) {
      if (named.vk == vk) {
        code = named.code;
        break;
      }
    }
    if (code == 0) {
      // A key the browser names that is not in the table: it is not typed.
      if (err) *err = "there is no such key";
      return false;
    }
    impl_->keyboard.emit(EV_KEY, code, down ? 1 : 0);
    impl_->keyboard.sync();
    return true;
  }

  // A character: the text itself is what goes in.
  if (text.empty()) return true;
  const char c = text[0];
  int code = 0;
  bool shift = false;
  if (!linux_backend::key_for_char(c, &code, &shift)) {
    if (err) *err = "there is no key for that character";
    return false;
  }
  if (shift) {
    impl_->keyboard.emit(EV_KEY, KEY_LEFTSHIFT, 1);
    impl_->keyboard.sync();
  }
  impl_->keyboard.emit(EV_KEY, code, down ? 1 : 0);
  impl_->keyboard.sync();
  if (!down && shift) {
    impl_->keyboard.emit(EV_KEY, KEY_LEFTSHIFT, 0);
    impl_->keyboard.sync();
  }
  return true;
}

}  // namespace tea_tunnel

#endif  // !_WIN32
