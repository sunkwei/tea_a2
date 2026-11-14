// src/main.cpp -- appstub: a tray host for other programs.
//
// appstub does one thing: it starts the programs listed in its configuration
// file, keeps them alive, and gives them a place in the notification area.
//
// Why a tray app instead of a Windows service: a service lives in session 0 and
// runs as LocalSystem. There %USERPROFILE% is
// C:\Windows\System32\config\systemprofile, the files it creates belong to
// SYSTEM, and a service that runs as a *user* account needs that account's
// password. A tray app runs in the user's own session: the same profile, the
// same paths, the same rights as the tools it drives. That is the job a
// systemd *user* unit does on Linux.
//
// The configuration is one JSON file (see config/appstub.json):
//
//   {
//     "log_dir": "%LOCALAPPDATA%\\appstub",
//     "apps": [
//       {
//         "id": "agent",
//         "name": "tea_a2 agent",
//         "command": "C:\\...\\tea_agent.exe",
//         "args": ["--port", "8383"],
//         "cwd": "C:\\...\\tea_a2",
//         "env": {"TEA_AGENT_HOME": "C:\\Users\\Hetin\\.tea_agent"},
//         "port": 8383,
//         "url": "http://127.0.0.1:8383/",
//         "version_args": ["--version"],
//         "autostart": true,
//         "restart": "on-failure",
//         "restart_delay_ms": 3000
//       }
//     ]
//   }
//
// "%VAR%" inside any string is replaced by the environment variable of that
// name - the *user's* environment, which is the whole point of the app.

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif

#include <windows.h>
#include <shellapi.h>
#include <winsock2.h>
#include <ws2tcpip.h>

#include <nlohmann/json.hpp>

#include <algorithm>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

namespace {

using json = nlohmann::json;

// --- utf-8 <-> utf-16 -----------------------------------------------------

std::wstring widen(const std::string& utf8) {
  if (utf8.empty()) return std::wstring();
  int n = MultiByteToWideChar(CP_UTF8, 0, utf8.c_str(), static_cast<int>(utf8.size()), nullptr, 0);
  std::wstring out(static_cast<size_t>(n), L'\0');
  MultiByteToWideChar(CP_UTF8, 0, utf8.c_str(), static_cast<int>(utf8.size()), &out[0], n);
  return out;
}

std::wstring utf8_to_wide(const std::string& utf8) {
  if (utf8.empty()) return std::wstring();
  int n = MultiByteToWideChar(CP_UTF8, 0, utf8.c_str(), static_cast<int>(utf8.size()), nullptr, 0);
  std::wstring out(static_cast<size_t>(n), L'\0');
  MultiByteToWideChar(CP_UTF8, 0, utf8.c_str(), static_cast<int>(utf8.size()), &out[0], n);
  return out;
}

std::string narrow(const std::wstring& utf16) {
  if (utf16.empty()) return std::string();
  int n = WideCharToMultiByte(CP_UTF8, 0, utf16.c_str(), static_cast<int>(utf16.size()), nullptr, 0,
                              nullptr, nullptr);
  std::string out(static_cast<size_t>(n), '\0');
  WideCharToMultiByte(CP_UTF8, 0, utf16.c_str(), static_cast<int>(utf16.size()), &out[0], n, nullptr,
                      nullptr);
  return out;
}

// --- logging ---------------------------------------------------------------

std::string g_log_file;

// The version of this program. It lives here (and not in a header) because
// only one file needs it.
const char* const kAppstubVersion = "1.0.0";

void logf(const char* fmt, ...) {
  char buffer[2048];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buffer, sizeof(buffer), fmt, ap);
  va_end(ap);

  SYSTEMTIME st;
  GetLocalTime(&st);
  char line[2176];
  snprintf(line, sizeof(line), "%04d-%02d-%02d %02d:%02d:%02d %s\n", st.wYear, st.wMonth, st.wDay,
           st.wHour, st.wMinute, st.wSecond, buffer);

  if (!g_log_file.empty()) {
    FILE* f = nullptr;
    if (fopen_s(&f, g_log_file.c_str(), "a") == 0 && f) {
      fputs(line, f);
      fclose(f);
    }
  }
  OutputDebugStringA(line);
}

// --- paths and environment -------------------------------------------------

std::string env_or_empty(const char* name) {
  const char* v = getenv(name);
  return v ? std::string(v) : std::string();
}

std::string exe_dir() {
  std::vector<wchar_t> buffer(1024);
  for (;;) {
    const DWORD n = GetModuleFileNameW(nullptr, &buffer[0], static_cast<DWORD>(buffer.size()));
    if (n == 0) return std::string();
    if (n < buffer.size()) {
      std::wstring path(&buffer[0], n);
      const size_t slash = path.find_last_of(L"\\/");
      if (slash == std::wstring::npos) return std::string();
      return narrow(path.substr(0, slash));
    }
    buffer.resize(buffer.size() * 2);
    if (buffer.size() > (1u << 20)) return std::string();
  }
}

// "%VAR%" -> the value of the environment variable VAR.
std::string expand_env(const std::string& text) {
  std::string out;
  out.reserve(text.size());
  for (size_t i = 0; i < text.size();) {
    if (text[i] == '%') {
      const size_t end = text.find('%', i + 1);
      if (end != std::string::npos && end > i + 1) {
        const std::string name = text.substr(i + 1, end - i - 1);
        const char* value = getenv(name.c_str());
        if (value) {
          out += value;
          i = end + 1;
          continue;
        }
      }
    }
    out.push_back(text[i]);
    ++i;
  }
  return out;
}

std::string join_path(const std::string& dir, const std::string& leaf) {
  if (dir.empty()) return leaf;
  if (!leaf.empty() && (leaf[0] == '\\' || leaf[0] == '/')) return leaf;
  char last = dir[dir.size() - 1];
  if (last == '\\' || last == '/') return dir + leaf;
  return dir + "\\" + leaf;
}

bool file_exists(const std::string& path) {
  if (path.empty()) return false;
  const DWORD attrs = GetFileAttributesW(widen(path).c_str());
  return attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_DIRECTORY);
}

bool dir_exists(const std::string& path) {
  if (path.empty()) return false;
  const DWORD attrs = GetFileAttributesW(widen(path).c_str());
  return attrs != INVALID_FILE_ATTRIBUTES && (attrs & FILE_ATTRIBUTE_DIRECTORY);
}

bool port_in_use(int port) {
  SOCKET s = socket(AF_INET, SOCK_STREAM, 0);
  if (s == INVALID_SOCKET) return false;
  sockaddr_in addr;
  std::memset(&addr, 0, sizeof(addr));
  addr.sin_family = AF_INET;
  addr.sin_port = htons(static_cast<unsigned short>(port));
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  const int r = connect(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
  closesocket(s);
  return r == 0;
}

// --- the hosted programs ---------------------------------------------------

struct Child {
  // configuration
  std::string id;
  std::string name;
  std::string command;
  std::vector<std::string> args;
  std::string cwd;
  std::vector<std::pair<std::string, std::string>> env;
  int port = 0;
  std::string url;
  bool autostart = false;
  bool restart_on_failure = true;
  int restart_delay_ms = 3000;
  // Watch the program file: when it is newer than the running instance, the
  // program was rebuilt and the child is restarted to pick up the new code.
  bool watch = true;
  std::string watch_file;  // empty = the command itself
  // The flag(s) that ask the program for its version. The default is
  // "--version"; an empty list switches the question off.
  std::vector<std::string> version_args;
  // What the program said about itself ("tea_agent 1.4.1"). Empty when the
  // program does not answer that question.
  std::string version;
  // Whether the question was already asked, and for which build: a newer file
  // on disk means a new program, and the answer has to be asked again.
  bool version_asked = false;
  ULONGLONG version_image_time = 0;

  // state
  HANDLE process = nullptr;
  DWORD pid = 0;
  bool wanted = false;
  bool running = false;
  DWORD exit_code = 0;
  bool restart_pending = false;
  ULONGLONG restart_at = 0;
  // The modification time of the watched file when this child was started.
  ULONGLONG image_time = 0;
  std::string note;
};

// The file's modification time, in FILETIME units (100 ns since 1601).
ULONGLONG file_modified_time(const std::string& path) {
  if (path.empty()) return 0;
  WIN32_FILE_ATTRIBUTE_DATA data;
  if (!GetFileAttributesExW(widen(path).c_str(), GetFileExInfoStandard, &data)) return 0;
  ULONGLONG t = data.ftLastWriteTime.dwHighDateTime;
  t = (t << 32) | data.ftLastWriteTime.dwLowDateTime;
  return t;
}

ULONGLONG now_file_time() {
  FILETIME ft;
  GetSystemTimeAsFileTime(&ft);
  ULONGLONG t = ft.dwHighDateTime;
  t = (t << 32) | ft.dwLowDateTime;
  return t;
}

std::vector<Child> g_children;
HWND g_window = nullptr;
// The children live in a job with KILL_ON_JOB_CLOSE: when appstub dies (even
// when it is killed), they go with it. Without this a killed appstub leaves
// its programs behind, and the next instance refuses to start them because
// the ports are still taken.
HANDLE g_job = nullptr;
HINSTANCE g_instance = nullptr;
// The bitmaps the tray icon points at (see make_tray_icon).
HBITMAP g_icon_colour = nullptr;
HBITMAP g_icon_mask = nullptr;
NOTIFYICONDATAW g_tray = {};
HICON g_icon = nullptr;
UINT g_taskbar_created = 0;

// Command line quoting (the rules CreateProcess parses).
std::string quote_argument(const std::string& s) {
  if (!s.empty() && s.find_first_of(" \t\"") == std::string::npos) return s;
  std::string out = "\"";
  size_t backslashes = 0;
  for (char c : s) {
    if (c == '\\') {
      ++backslashes;
      continue;
    }
    if (c == '"') {
      out.append(backslashes * 2 + 1, '\\');
      backslashes = 0;
      out.push_back('"');
      continue;
    }
    out.append(backslashes, '\\');
    backslashes = 0;
    out.push_back(c);
  }
  out.append(backslashes * 2, '\\');
  out.push_back('"');
  return out;
}

// The child gets the user's environment plus the configured overrides. A
// Unicode environment block has to be sorted, case-insensitively.
std::wstring build_environment(const std::vector<std::pair<std::string, std::string>>& overrides) {
  std::vector<std::wstring> vars;
  LPWCH block = GetEnvironmentStringsW();
  if (block) {
    for (LPWCH p = block; *p;) {
      const size_t len = wcslen(p);
      vars.emplace_back(p, len);
      p += len + 1;
    }
    FreeEnvironmentStringsW(block);
  }
  for (const auto& kv : overrides) {
    const std::wstring key = widen(kv.first);
    const std::wstring entry = key + L"=" + widen(kv.second);
    bool replaced = false;
    for (auto& v : vars) {
      if (v.size() > key.size() && v.compare(0, key.size(), key) == 0 && v[key.size()] == L'=') {
        v = entry;
        replaced = true;
        break;
      }
    }
    if (!replaced) vars.push_back(entry);
  }
  std::sort(vars.begin(), vars.end(),
            [](const std::wstring& a, const std::wstring& b) { return _wcsicmp(a.c_str(), b.c_str()) < 0; });
  std::wstring out;
  for (const auto& v : vars) {
    out += v;
    out.push_back(L'\0');
  }
  out.push_back(L'\0');
  return out;
}

// --- asking a program for its version --------------------------------------

// How long the probe may take. A program that knows the flag answers at
// once; a program that ignores it and starts serving anyway is killed after
// this.
const ULONGLONG kVersionTimeoutMs = 5000;

// The first line of what the program printed, without the whitespace around
// it. That line is the version ("tea_agent 1.4.1").
std::string first_line(const std::string& text) {
  size_t start = 0;
  while (start < text.size() &&
         (text[start] == '\r' || text[start] == '\n' || text[start] == ' ' ||
          text[start] == '\t')) {
    ++start;
  }
  const size_t end = text.find_first_of("\r\n", start);
  std::string line =
      (end == std::string::npos) ? text.substr(start) : text.substr(start, end - start);
  while (!line.empty() && (line.back() == ' ' || line.back() == '\t')) line.pop_back();
  if (line.size() > 64) line.resize(64);
  return line;
}

// Ask the program for its version and keep the answer.
//
// The question is asked the way the program is normally run - the same
// arguments, the same environment, the same working directory - plus the
// version flag. A program that does not know the flag says so by failing,
// and then nothing is shown: a made up version is worse than no version.
bool ask_version(Child& child) {
  if (child.version_args.empty()) return false;

  std::string command_line = quote_argument(child.command);
  for (const auto& a : child.args) command_line += " " + quote_argument(a);
  for (const auto& a : child.version_args) command_line += " " + quote_argument(a);

  SECURITY_ATTRIBUTES sa;
  std::memset(&sa, 0, sizeof(sa));
  sa.nLength = sizeof(sa);
  sa.bInheritHandle = TRUE;
  HANDLE read_end = nullptr;
  HANDLE write_end = nullptr;
  if (!CreatePipe(&read_end, &write_end, &sa, 0)) return false;
  // The read end must not be inherited: the child would keep it open, and the
  // read below would never see the end of the output.
  SetHandleInformation(read_end, HANDLE_FLAG_INHERIT, 0);
  // An empty stdin: the program must not wait for somebody to type.
  HANDLE nul = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa,
                           OPEN_EXISTING, 0, nullptr);

  STARTUPINFOW si;
  std::memset(&si, 0, sizeof(si));
  si.cb = sizeof(si);
  si.dwFlags = STARTF_USESTDHANDLES;
  si.hStdInput = nul;
  si.hStdOutput = write_end;
  si.hStdError = write_end;

  PROCESS_INFORMATION pi;
  std::memset(&pi, 0, sizeof(pi));
  std::wstring wcmd = widen(command_line);
  std::wstring wcwd = widen(child.cwd);
  std::wstring env = build_environment(child.env);
  const DWORD flags = CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT;
  const BOOL ok = CreateProcessW(nullptr, &wcmd[0], nullptr, nullptr, TRUE, flags,
                                 env.empty() ? nullptr : &env[0],
                                 wcwd.empty() ? nullptr : wcwd.c_str(), &si, &pi);
  if (nul && nul != INVALID_HANDLE_VALUE) CloseHandle(nul);
  CloseHandle(write_end);
  if (!ok) {
    CloseHandle(read_end);
    return false;
  }

  // Read while it runs: a program that writes more than the pipe holds
  // would block, and then it would never end.
  std::string output;
  const ULONGLONG deadline = GetTickCount64() + kVersionTimeoutMs;
  bool timed_out = false;
  for (;;) {
    DWORD available = 0;
    if (PeekNamedPipe(read_end, nullptr, 0, nullptr, &available, nullptr) && available > 0) {
      char buffer[1024];
      DWORD got = 0;
      if (ReadFile(read_end, buffer, sizeof(buffer), &got, nullptr) && got > 0) {
        output.append(buffer, got);
      }
    }
    if (WaitForSingleObject(pi.hProcess, 50) == WAIT_OBJECT_0) break;
    if (GetTickCount64() > deadline) {
      timed_out = true;
      TerminateProcess(pi.hProcess, 1);
      WaitForSingleObject(pi.hProcess, 1000);
      break;
    }
  }
  // Whatever is left in the pipe.
  for (;;) {
    DWORD available = 0;
    if (!PeekNamedPipe(read_end, nullptr, 0, nullptr, &available, nullptr) ||
        available == 0) {
      break;
    }
    char buffer[1024];
    DWORD got = 0;
    if (!ReadFile(read_end, buffer, sizeof(buffer), &got, nullptr) || got == 0) break;
    output.append(buffer, got);
  }

  DWORD code = 0;
  GetExitCodeProcess(pi.hProcess, &code);
  CloseHandle(pi.hProcess);
  CloseHandle(pi.hThread);
  CloseHandle(read_end);

  if (timed_out || code != 0) return false;  // the program does not answer
  const std::string line = first_line(output);
  if (line.empty()) return false;
  child.version = line;
  return true;
}

// Ask the program for its version - but only when the answer can have
// changed: the file on disk is newer than the one the question was asked
// about.
void refresh_version(Child& child) {
  const std::string watched = child.watch_file.empty() ? child.command : child.watch_file;
  const ULONGLONG mtime = file_modified_time(watched);
  if (child.version_asked && mtime == child.version_image_time) return;
  child.version_asked = true;
  child.version_image_time = mtime;
  child.version.clear();
  if (ask_version(child)) {
    logf("[%s] version: %s", child.id.c_str(), child.version.c_str());
  } else {
    logf("[%s] no version: the program does not answer --version", child.id.c_str());
  }
}

bool start_child(Child& child) {
  if (child.running) return true;
  if (!file_exists(child.command)) {
    child.note = "找不到程序";
    logf("[%s] not found: %s", child.id.c_str(), child.command.c_str());
    return false;
  }
  if (child.port > 0 && port_in_use(child.port)) {
    child.note = "端口已被占用";
    logf("[%s] port %d is already in use - not starting", child.id.c_str(), child.port);
    return false;
  }

  std::string command_line = quote_argument(child.command);
  for (const auto& a : child.args) command_line += " " + quote_argument(a);

  std::wstring wcmd = widen(command_line);
  std::wstring wcwd = widen(child.cwd);
  std::wstring env = build_environment(child.env);

  STARTUPINFOW si;
  std::memset(&si, 0, sizeof(si));
  si.cb = sizeof(si);
  si.dwFlags = STARTF_USESHOWWINDOW;
  si.wShowWindow = SW_HIDE;
  PROCESS_INFORMATION pi;
  std::memset(&pi, 0, sizeof(pi));

  const DWORD flags = CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT;
  const BOOL ok = CreateProcessW(nullptr, &wcmd[0], nullptr, nullptr, FALSE, flags,
                                 env.empty() ? nullptr : &env[0], wcwd.empty() ? nullptr : wcwd.c_str(),
                                 &si, &pi);
  if (!ok) {
    char message[256];
    DWORD code = GetLastError();
    snprintf(message, sizeof(message), "CreateProcess failed (%lu)", code);
    child.note = message;
    logf("[%s] %s", child.id.c_str(), message);
    return false;
  }
  child.process = pi.hProcess;
  child.pid = pi.dwProcessId;
  child.running = true;
  child.exit_code = 0;
  child.note.clear();
  child.image_time = file_modified_time(child.watch_file.empty() ? child.command : child.watch_file);
  CloseHandle(pi.hThread);
  if (g_job && !AssignProcessToJobObject(g_job, pi.hProcess)) {
    logf("[%s] could not be put into the job object (%lu) - it will not die with appstub",
         child.id.c_str(), GetLastError());
  }
  logf("[%s] started (pid %lu): %s", child.id.c_str(), child.pid, command_line.c_str());
  // The program on disk may be a new build: ask it for its version again.
  refresh_version(child);
  return true;
}

void stop_child(Child& child) {
  if (!child.process) {
    child.running = false;
    return;
  }
  logf("[%s] stopping (pid %lu)", child.id.c_str(), child.pid);
  TerminateProcess(child.process, 0);
  WaitForSingleObject(child.process, 3000);
  CloseHandle(child.process);
  child.process = nullptr;
  child.pid = 0;
  child.running = false;
  child.restart_pending = false;
  child.note = "已停止";
}

std::string status_text(const Child& child) {
  if (child.running) {
    char buf[64];
    snprintf(buf, sizeof(buf), "运行中 (pid %lu)", child.pid);
    return buf;
  }
  if (child.restart_pending) return "等待重启";
  if (!child.note.empty()) return child.note;
  return "已停止";
}

// --- the tray icon ---------------------------------------------------------

// IDI_APPLICATION is a narrow MAKEINTRESOURCE; the wide API wants the wide
// one.
const wchar_t* const kDefaultIcon = MAKEINTRESOURCEW(32512);

HICON fallback_icon() { return LoadIconW(nullptr, kDefaultIcon); }

// A small filled disc, drawn at runtime: a stock icon says nothing about this
// program and an .ico file would have to be shipped next to the executable.
// Where the tray icon comes from. "resource" is the normal case; the
// other two exist to tell them apart when the notification area misbehaves.
enum class IconSource { Resource, Drawn, System };
IconSource g_icon_source = IconSource::Resource;

// The icon comes from the resource (resources\appstub.ico). The drawn
// fallback below is only for the case that the resource is missing.
HICON make_tray_icon() {
  if (g_icon_source == IconSource::System) {
    logf("using the system icon");
    return fallback_icon();
  }
  if (g_icon_source == IconSource::Resource) {
    HICON from_resource = LoadIconW(g_instance, MAKEINTRESOURCEW(1));
    if (from_resource) {
      logf("using the icon resource");
      return from_resource;
    }
    logf("the icon resource is missing - drawing one instead");
  }

  const int w = 32;
  const int h = 32;
  BITMAPINFO bi;
  std::memset(&bi, 0, sizeof(bi));
  bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
  bi.bmiHeader.biWidth = w;
  bi.bmiHeader.biHeight = -h;  // negative = top-down, so row 0 is the top
  bi.bmiHeader.biPlanes = 1;
  bi.bmiHeader.biBitCount = 32;
  bi.bmiHeader.biCompression = BI_RGB;

  HDC screen = GetDC(nullptr);
  void* bits = nullptr;
  HBITMAP colour = CreateDIBSection(screen, &bi, DIB_RGB_COLORS, &bits, nullptr, 0);
  ReleaseDC(nullptr, screen);
  if (!colour || !bits) return fallback_icon();

  // The colour bitmap: a green disc on a transparent background.
  unsigned char* px = static_cast<unsigned char*>(bits);
  const unsigned char r = 0x2d, g = 0x8f, b = 0x5f;
  std::vector<bool> inside_mask(static_cast<size_t>(w) * h, false);
  for (int y = 0; y < h; ++y) {
    for (int x = 0; x < w; ++x) {
      const double dx = x - 15.5;
      const double dy = y - 15.5;
      const bool inside = (dx * dx + dy * dy) <= 13.5 * 13.5;
      inside_mask[static_cast<size_t>(y) * w + x] = inside;
      unsigned char* p = px + (static_cast<size_t>(y) * w + x) * 4;
      p[0] = b;
      p[1] = g;
      p[2] = r;
      p[3] = inside ? 255 : 0;
    }
  }

  // The AND mask says which pixels are *masked out* (the background shows
  // through). GDI ignores the alpha channel of a 32 bit icon and uses this
  // mask, so it has to be right: 1 outside the disc, 0 inside. Rows of a 1 bit
  // bitmap are padded to a 4 byte boundary - and the bits have to be written
  // out, not left uninitialised (an uninitialised mask makes the icon vanish
  // as soon as the shell draws it again, e.g. when the mouse hovers over it).
  const size_t stride = ((static_cast<size_t>(w) + 31) / 32) * 4;
  std::vector<unsigned char> mask_bits(stride * h, 0);
  for (int y = 0; y < h; ++y) {
    for (int x = 0; x < w; ++x) {
      if (inside_mask[static_cast<size_t>(y) * w + x]) continue;
      mask_bits[static_cast<size_t>(y) * stride + (x / 8)] |= (0x80 >> (x % 8));
    }
  }
  HBITMAP mask = CreateBitmap(w, h, 1, 1, mask_bits.data());
  if (!mask) {
    DeleteObject(colour);
    return fallback_icon();
  }

  ICONINFO ii;
  std::memset(&ii, 0, sizeof(ii));
  ii.fIcon = TRUE;
  ii.hbmColor = colour;
  ii.hbmMask = mask;
  HICON icon = CreateIconIndirect(&ii);
  if (!icon) {
    DeleteObject(colour);
    DeleteObject(mask);
    return fallback_icon();
  }
  // The icon keeps *pointing at* the two bitmaps - CreateIconIndirect does
  // not copy them. They have to live as long as the icon does.
  g_icon_colour = colour;
  g_icon_mask = mask;
  return icon;
}

// Write the icon to a BMP file, so that its pixels can be looked at (the
// notification area is the one place where a broken icon shows up as
// "the icon vanishes", and eyes are better than guesswork).
void dump_icon(const std::string& path) {
  const int w = 32;
  const int h = 32;
  HICON icon = make_tray_icon();
  if (!icon) return;

  BITMAPINFO bi;
  std::memset(&bi, 0, sizeof(bi));
  bi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
  bi.bmiHeader.biWidth = w;
  bi.bmiHeader.biHeight = -h;
  bi.bmiHeader.biPlanes = 1;
  bi.bmiHeader.biBitCount = 32;
  bi.bmiHeader.biCompression = BI_RGB;

  HDC screen = GetDC(nullptr);
  void* bits = nullptr;
  HBITMAP bmp = CreateDIBSection(screen, &bi, DIB_RGB_COLORS, &bits, nullptr, 0);
  HDC dc = CreateCompatibleDC(screen);
  HGDIOBJ old = SelectObject(dc, bmp);
  DrawIconEx(dc, 0, 0, icon, w, h, 0, nullptr, DI_NORMAL);
  SelectObject(dc, old);
  DeleteDC(dc);
  ReleaseDC(nullptr, screen);

  // A BMP file: 14 byte file header, the info header, then the pixels
  // (bottom-up, BGRA).
  FILE* out = nullptr;
  if (fopen_s(&out, path.c_str(), "wb") == 0 && out) {
    const int stride = w * 4;
    const int image_size = stride * h;
    unsigned char file_header[14] = {0};
    file_header[0] = 'B';
    file_header[1] = 'M';
    const unsigned int pixel_offset = 14 + 40;
    const unsigned int file_size = pixel_offset + image_size;
    file_header[2] = static_cast<unsigned char>(file_size & 0xff);
    file_header[3] = static_cast<unsigned char>((file_size >> 8) & 0xff);
    file_header[4] = static_cast<unsigned char>((pixel_offset >> 0) & 0xff);
    file_header[5] = static_cast<unsigned char>((pixel_offset >> 8) & 0xff);
    file_header[6] = static_cast<unsigned char>((pixel_offset >> 16) & 0xff);
    file_header[7] = static_cast<unsigned char>((pixel_offset >> 24) & 0xff);
    fwrite(file_header, 1, sizeof(file_header), out);

    BITMAPINFOHEADER header;
    std::memset(&header, 0, sizeof(header));
    header.biSize = sizeof(BITMAPINFOHEADER);
    header.biWidth = w;
    header.biHeight = h;  // positive = bottom-up, as BMP wants
    header.biPlanes = 1;
    header.biBitCount = 32;
    header.biCompression = BI_RGB;
    header.biSizeImage = image_size;
    fwrite(&header, 1, sizeof(header), out);

    unsigned char* px = static_cast<unsigned char*>(bits);
    for (int y = h - 1; y >= 0; --y) {  // bottom-up
      fwrite(px + static_cast<size_t>(y) * stride, 1, stride, out);
    }
    fclose(out);
  }
  DeleteObject(bmp);
}

void tray_add() {
  g_tray = NOTIFYICONDATAW();
  g_tray.cbSize = sizeof(g_tray);
  g_tray.hWnd = g_window;
  g_tray.uID = 1;
  g_tray.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP;
  g_tray.uCallbackMessage = WM_APP + 1;
  g_tray.hIcon = g_icon ? g_icon : fallback_icon();
  wcscpy_s(g_tray.szTip, 64, L"appstub");
  if (!Shell_NotifyIconW(NIM_ADD, &g_tray)) {
    logf("the tray icon could not be added (error %lu)", GetLastError());
  } else {
    logf("the tray icon was added (window %p, icon %p, source %s)", (void*)g_window,
         (void*)g_tray.hIcon,
         g_icon_source == IconSource::System ? "system"
                                            : (g_icon_source == IconSource::Drawn ? "drawn" : "resource"));
  }
  // Version 4 of the notification-area protocol: the shell then delivers the
  // mouse events in the documented way (the event in the low word of lParam).
  NOTIFYICONDATAW version = g_tray;
  version.uVersion = NOTIFYICON_VERSION_4;
  version.uFlags = 0;
  if (!Shell_NotifyIconW(NIM_SETVERSION, &version)) {
    logf("the notification-area protocol version could not be set (error %lu)", GetLastError());
  }
}

/// Put the tray icon back when the shell has lost it.
///
/// The shell drops notification icons without telling anybody: Explorer
/// restarts, the notification area is reset, the icon is dragged away. The
/// app keeps running, but the icon is gone - and nobody notices until
/// somebody goes looking for it. Explorer's `TaskbarCreated` broadcast is
/// the usual way to hear about it, but it does not always arrive.
///
/// So the icon is checked every now and then. NIM_MODIFY on an icon that is
/// not there fails - that is the cheapest way to ask the shell.
void tray_keepalive() {
  static ULONGLONG next_check = 0;
  const ULONGLONG now = GetTickCount64();
  if (now < next_check) return;
  next_check = now + 30000ULL;   // every half minute
  if (!g_window) return;
  NOTIFYICONDATAW probe = g_tray;
  probe.uFlags = NIF_TIP;
  if (Shell_NotifyIconW(NIM_MODIFY, &probe)) return;   // it is still there
  logf("the tray icon was gone - putting it back");
  tray_add();
}

void tray_balloon(const std::string& text) {
  NOTIFYICONDATAW data = g_tray;
  data.uFlags = NIF_INFO;
  data.dwInfoFlags = NIIF_INFO;
  wcsncpy_s(data.szInfoTitle, 64, L"appstub", _TRUNCATE);
  wcsncpy_s(data.szInfo, 256, widen(text).c_str(), _TRUNCATE);
  Shell_NotifyIconW(NIM_MODIFY, &data);
}

// --- the menu --------------------------------------------------------------

void show_menu() {
  HMENU menu = CreatePopupMenu();
  if (!menu) return;

  // The command ids: 1000 is the first toggle, 2000 the first "open", 9000
  // quits. The mapping is rebuilt while the menu is built.
  std::vector<int> toggle_ids;
  std::vector<int> open_ids;

  int id = 1000;
  for (size_t i = 0; i < g_children.size(); ++i) {
    const Child& c = g_children[i];
    const std::string status = status_text(c);
    std::string label = c.name + "  —  " + status;
    // The version the program reports about itself, when it reports one.
    if (!c.version.empty()) label += "  ·  " + c.version;
    AppendMenuW(menu, MF_STRING | MF_GRAYED, id++, widen(label).c_str());
    toggle_ids.push_back(id);
    AppendMenuW(menu, MF_STRING, id++, widen(c.running ? ("停止 " + c.name) : ("启动 " + c.name)).c_str());
    if (!c.url.empty()) {
      open_ids.push_back(id);
      AppendMenuW(menu, MF_STRING, id++, widen("打开 " + c.name + " 的网页").c_str());
    }
    AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
  }
  AppendMenuW(menu, MF_STRING, 9000, L"退出 appstub");

  POINT pt;
  GetCursorPos(&pt);
  SetForegroundWindow(g_window);
  const UINT cmd = TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0,
                                  g_window, nullptr);
  PostMessageW(g_window, WM_NULL, 0, 0);
  DestroyMenu(menu);

  if (cmd == 9000) {
    DestroyWindow(g_window);
    return;
  }
  for (size_t i = 0; i < toggle_ids.size(); ++i) {
    if (cmd == static_cast<UINT>(toggle_ids[i])) {
      Child& c = g_children[i];
      if (c.running) {
        stop_child(c);
      } else {
        c.wanted = true;
        if (!start_child(c)) c.note = c.note.empty() ? "启动失败" : c.note;
      }
      return;
    }
  }
  for (size_t i = 0; i < open_ids.size(); ++i) {
    if (cmd == static_cast<UINT>(open_ids[i])) {
      const std::string url = g_children[i].url;
      ShellExecuteW(nullptr, L"open", widen(url).c_str(), nullptr, nullptr, SW_SHOWNORMAL);
      return;
    }
  }
}

// --- configuration ---------------------------------------------------------

bool load_config(const std::string& path, std::string* error) {
  std::ifstream in(path.c_str(), std::ios::binary);
  if (!in) {
    *error = "cannot read " + path;
    return false;
  }
  json doc;
  try {
    in >> doc;
  } catch (const std::exception& e) {
    *error = std::string("invalid JSON: ") + e.what();
    return false;
  }

  std::string log_dir = doc.value("log_dir", "%LOCALAPPDATA%\\appstub");
  log_dir = expand_env(log_dir);
  if (!dir_exists(log_dir)) CreateDirectoryW(widen(log_dir).c_str(), nullptr);
  g_log_file = join_path(log_dir, "appstub.log");

  if (!doc.contains("apps") || !doc["apps"].is_array()) {
    *error = "the config has no \"apps\" array";
    return false;
  }

  for (const json& item : doc["apps"]) {
    Child child;
    child.id = item.value("id", std::string());
    child.name = item.value("name", child.id);
    child.command = expand_env(item.value("command", std::string()));
    if (child.command.empty()) {
      *error = "an app without a \"command\"";
      return false;
    }
    if (child.id.empty()) child.id = child.name;
    if (child.id.empty()) child.id = child.command;
    if (item.contains("args") && item["args"].is_array()) {
      for (const json& a : item["args"]) child.args.push_back(expand_env(a.get<std::string>()));
    }
    child.cwd = expand_env(item.value("cwd", std::string()));
    child.url = expand_env(item.value("url", std::string()));
    child.port = item.value("port", 0);
    child.autostart = item.value("autostart", false);
    child.restart_delay_ms = item.value("restart_delay_ms", 3000);
    child.watch = item.value("watch", true);
    child.watch_file = expand_env(item.value("watch_file", std::string()));
    // How to ask the program for its version. The default is "--version";
    // an empty list switches the question off.
    child.version_args.push_back("--version");
    if (item.contains("version_args") && item["version_args"].is_array()) {
      child.version_args.clear();
      for (const json& a : item["version_args"]) {
        child.version_args.push_back(expand_env(a.get<std::string>()));
      }
    }
    const std::string restart = item.value("restart", std::string("on-failure"));
    child.restart_on_failure = (restart != "never");
    if (item.contains("env") && item["env"].is_object()) {
      for (auto it = item["env"].begin(); it != item["env"].end(); ++it) {
        child.env.emplace_back(it.key(), expand_env(it.value().get<std::string>()));
      }
    }
    g_children.push_back(child);
  }
  return true;
}

// --- the message loop ------------------------------------------------------

void tick() {
  const ULONGLONG now = GetTickCount64();
  const ULONGLONG wall = now_file_time();
  tray_keepalive();
  for (Child& child : g_children) {
    // The program on disk is newer than the one we started: it was rebuilt.
    // Restart it, so the new code is what runs from now on.
    if (child.watch && child.running && child.process) {
      const std::string watched = child.watch_file.empty() ? child.command : child.watch_file;
      const ULONGLONG mtime = file_modified_time(watched);
      // Two seconds of settle time: a build writes the new file in one go,
      // but a copy in progress should not be picked up half-written.
      if (mtime > child.image_time && mtime > 0 && (wall - mtime) > 20000000ULL) {
        logf("[%s] the program file is newer than the running instance - restarting it",
             child.id.c_str());
        stop_child(child);
        child.wanted = true;
        start_child(child);
        continue;
      }
    }
    if (child.process && WaitForSingleObject(child.process, 0) == WAIT_OBJECT_0) {
      DWORD code = 0;
      GetExitCodeProcess(child.process, &code);
      CloseHandle(child.process);
      child.process = nullptr;
      child.running = false;
      child.exit_code = code;
      child.pid = 0;
      logf("[%s] exited with code %lu", child.id.c_str(), code);
      if (child.wanted && child.restart_on_failure && code != 0) {
        child.restart_pending = true;
        child.restart_at = now + static_cast<ULONGLONG>(child.restart_delay_ms);
        child.note = "崩溃，等待重启";
        tray_balloon(child.name + " 退出了（代码 " + std::to_string(code) + "），正在重启");
      } else {
        child.note = code == 0 ? "已停止" : ("已退出 (代码 " + std::to_string(code) + ")");
      }
    }
    if (child.restart_pending && now >= child.restart_at) {
      child.restart_pending = false;
      child.wanted = true;
      start_child(child);
    }
  }
}

LRESULT CALLBACK window_proc(HWND hwnd, UINT msg, WPARAM wparam, LPARAM lparam) {
  switch (msg) {
    case WM_CREATE:
      // The tray icon is *not* added here: while WM_CREATE runs,
      // CreateWindowExW has not returned yet, so the window handle is not
      // known - and an icon registered with a NULL owner window is not the
      // shell's business (it shows up, then vanishes). The icon is added
      // after the window exists.
      SetTimer(hwnd, 1, 1000, nullptr);
      for (Child& child : g_children) {
        if (child.autostart) {
          child.wanted = true;
          start_child(child);
        }
      }
      return 0;

    case WM_TIMER:
      tick();
      return 0;

    case WM_APP + 1: {
      // The tray icon: left click opens the menu, double click opens the first
      // app's page.
      const UINT event = LOWORD(lparam);
      if (event == WM_LBUTTONUP || event == WM_RBUTTONUP || event == WM_CONTEXTMENU) {
        show_menu();
      } else if (event == WM_LBUTTONDBLCLK) {
        for (const Child& child : g_children) {
          if (!child.url.empty()) {
            ShellExecuteW(nullptr, L"open", widen(child.url).c_str(), nullptr, nullptr, SW_SHOWNORMAL);
            break;
          }
        }
      }
      return 0;
    }

    case WM_QUERYENDSESSION:
    case WM_ENDSESSION:
      for (Child& child : g_children) stop_child(child);
      return 0;

    case WM_DESTROY:
      for (Child& child : g_children) stop_child(child);
      Shell_NotifyIconW(NIM_DELETE, &g_tray);
      PostQuitMessage(0);
      return 0;

    default:
      return DefWindowProcW(hwnd, msg, wparam, lparam);
  }
}

void print_usage() {
  OutputDebugStringA("appstub [--config <file>] [--no-autostart] [--version]\n");
}

// A GUI program has no console of its own. Where the text goes depends on
// how it was started:
//   * a console (started from cmd): the text goes there, as Unicode;
//   * a redirected stdout (a script): the text goes into the pipe, as UTF-8;
//   * neither (started from the desktop): a message box is the only way to
//     show it.
void print_to_operator(const std::string& utf8_text) {
  const HANDLE out = GetStdHandle(STD_OUTPUT_HANDLE);
  if (out && out != INVALID_HANDLE_VALUE) {
    DWORD mode = 0;
    if (GetConsoleMode(out, &mode)) {
      // A console: WriteConsoleW takes Unicode, so no code page games.
      const std::wstring wide = utf8_to_wide(utf8_text);
      DWORD written = 0;
      if (WriteConsoleW(out, wide.c_str(), static_cast<DWORD>(wide.size()), &written, nullptr)) {
        return;
      }
    } else {
      // A pipe or a file: the bytes go out as they are.
      std::fwrite(utf8_text.data(), 1, utf8_text.size(), stdout);
      std::fflush(stdout);
      return;
    }
  }
  // No stdout at all: borrow the console of whoever started us.
  if (AttachConsole(ATTACH_PARENT_PROCESS)) {
    FILE* console = nullptr;
    if (freopen_s(&console, "CONOUT$", "w", stdout) == 0 && console) {
      std::fputs(utf8_text.c_str(), stdout);
      std::fflush(stdout);
      return;
    }
  }
  MessageBoxW(nullptr, widen(utf8_text).c_str(), L"appstub --version", MB_OK | MB_ICONINFORMATION);
}

// "appstub --version": what this program is, and what the programs it hosts
// say about themselves.
void print_versions() {
  std::string text = std::string("appstub ") + kAppstubVersion + "\n";
  for (Child& child : g_children) {
    refresh_version(child);
    text += child.id + "  " + child.name + "  " +
            (child.version.empty() ? std::string("(no version)") : child.version) + "\n";
  }
  print_to_operator(text);
}

}  // namespace

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, LPWSTR, int) {
  g_instance = instance;
  WSADATA wsa;
  WSAStartup(MAKEWORD(2, 2), &wsa);

  std::string config_path;
  bool no_autostart = false;
  bool want_version = false;
  for (int i = 1; i < __argc; ++i) {
    const std::string arg = narrow(__wargv[i]);
    if (arg == "--icon" && i + 1 < __argc) {
      const std::string mode = narrow(__wargv[++i]);
      if (mode == "system") g_icon_source = IconSource::System;
      else if (mode == "drawn") g_icon_source = IconSource::Drawn;
      else g_icon_source = IconSource::Resource;
    } else if (arg == "--dump-icon" && i + 1 < __argc) {
      dump_icon(narrow(__wargv[++i]));
      WSACleanup();
      return 0;
    }
    if ((arg == "--config" || arg == "-c") && i + 1 < __argc) {
      config_path = narrow(__wargv[++i]);
    } else if (arg == "--no-autostart") {
      no_autostart = true;
    } else if (arg == "--version" || arg == "-V") {
      want_version = true;
    } else if (arg == "--help" || arg == "-h") {
      print_usage();
      return 0;
    }
  }
  if (config_path.empty()) {
    const std::string local = join_path(env_or_empty("LOCALAPPDATA"), "appstub");
    const std::string beside = join_path(exe_dir(), "config.json");
    if (file_exists(beside)) {
      config_path = beside;
    } else {
      config_path = join_path(local, "config.json");
    }
  }

  std::string error;
  if (!load_config(config_path, &error)) {
    MessageBoxW(nullptr, widen("appstub: " + error).c_str(), L"appstub", MB_OK | MB_ICONERROR);
    WSACleanup();
    return 1;
  }
  if (want_version) {
    print_versions();
    WSACleanup();
    return 0;
  }
  if (no_autostart) {
    for (Child& child : g_children) child.autostart = false;
  }

  logf("appstub started, config %s, %d app(s)", config_path.c_str(), static_cast<int>(g_children.size()));

  // One instance: a second one would fight over the ports.
  HANDLE mutex = CreateMutexW(nullptr, TRUE, L"Local\\appstub-single-instance");
  if (GetLastError() == ERROR_ALREADY_EXISTS) {
    logf("another instance is already running - exiting");
    if (mutex) CloseHandle(mutex);
    WSACleanup();
    return 0;
  }

  // What do the hosted programs say about themselves? The tray menu shows
  // it, and it is asked before the icon appears: the answer is what the
  // menu is about.
  for (Child& child : g_children) refresh_version(child);

  g_icon = make_tray_icon();
  g_taskbar_created = RegisterWindowMessageW(L"TaskbarCreated");

  // The job that holds the children.
  g_job = CreateJobObjectW(nullptr, nullptr);
  if (g_job) {
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits;
    std::memset(&limits, 0, sizeof(limits));
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!SetInformationJobObject(g_job, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) {
      logf("the job object could not be configured (%lu)", GetLastError());
    }
  }

  WNDCLASSEXW wc;
  std::memset(&wc, 0, sizeof(wc));
  wc.cbSize = sizeof(wc);
  wc.lpfnWndProc = window_proc;
  wc.hInstance = instance;
  wc.lpszClassName = L"appstub_window";
  // The class icon and the window icon matter: the notification area draws
  // the icon it is given, but the hover fly-out asks the *window* for its
  // icon (WM_GETICON). Without one, the fly-out has nothing to draw - and the
  // icon looks like it vanished.
  wc.hIcon = g_icon;
  wc.hIconSm = g_icon;
  RegisterClassExW(&wc);

  // A hidden top-level window (not a message-only one): it never appears in
  // the task bar, but it is a normal window, which is what the shell expects
  // under a notification-area icon.
  g_window = CreateWindowExW(0, L"appstub_window", L"appstub", WS_OVERLAPPED, 0, 0, 0, 0, nullptr,
                              nullptr, instance, nullptr);
  if (!g_window) {
    MessageBoxW(nullptr, L"appstub: cannot create the message window", L"appstub",
                MB_OK | MB_ICONERROR);
    WSACleanup();
    return 1;
  }
  ShowWindow(g_window, SW_HIDE);
  SendMessageW(g_window, WM_SETICON, ICON_BIG, reinterpret_cast<LPARAM>(g_icon));
  SendMessageW(g_window, WM_SETICON, ICON_SMALL, reinterpret_cast<LPARAM>(g_icon));

  // Now that the window exists, the tray icon can be registered against it.
  tray_add();

  MSG msg;
  while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
    if (msg.message == static_cast<UINT>(g_taskbar_created)) {
      // Explorer restarted: the icon has to be added again.
      tray_add();
      continue;
    }
    TranslateMessage(&msg);
    DispatchMessageW(&msg);
  }

  if (g_icon) DestroyIcon(g_icon);
  if (g_icon_colour) DeleteObject(g_icon_colour);
  if (g_icon_mask) DeleteObject(g_icon_mask);
  if (g_job) CloseHandle(g_job);  // the children go with it
  if (mutex) {
    ReleaseMutex(mutex);
    CloseHandle(mutex);
  }
  WSACleanup();
  logf("appstub stopped");
  return 0;
}
