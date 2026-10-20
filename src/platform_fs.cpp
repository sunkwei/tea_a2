// platform_fs.cpp -- path handling and file I/O built directly on the platform
// APIs. Every std::string here is UTF-8, including paths.
//
// Why not std::filesystem:
//   * its narrow<->wide conversions on Windows use the ANSI code page, so
//     non-ASCII paths break (MinGW throws "Illegal byte sequence");
//   * GCC 6.5 only has std::experimental::filesystem, and linking it needs
//     extra work on every platform.
// The surface area we actually need is small enough to own.

#include "util.h"

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstring>

#ifdef _WIN32
#include <winsock2.h>
#include <windows.h>
#else
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#endif

namespace tea {
namespace {

#ifdef _WIN32
const char kSep = '\\';
#else
const char kSep = '/';
#endif

bool is_sep(char c) { return c == '/' || c == '\\'; }

bool char_equal(char a, char b) {
#ifdef _WIN32
  return to_lower(std::string(1, a)) == to_lower(std::string(1, b));
#else
  return a == b;
#endif
}

/// Collapse "." and ".." segments without touching the filesystem.
std::string lexical_normal(const std::string& in) {
  if (in.empty()) return in;
  std::string root;
  size_t i = 0;

#ifdef _WIN32
  const bool unc = in.size() >= 2 && is_sep(in[0]) && is_sep(in[1]);
  if (unc) {
    root = "\\\\";
    i = 2;
  } else if (in.size() >= 2 && in[1] == ':') {
    root = in.substr(0, 2);
    i = 2;
  }
  if (unc) {
    // keep the server and share as opaque segments; the loop below handles them
  }
#endif

  bool absolute = false;
  if (i < in.size() && is_sep(in[i])) {
    absolute = true;
    ++i;
  }

  std::vector<std::string> parts;
  std::string cur;
  for (; i <= in.size(); ++i) {
    if (i == in.size() || is_sep(in[i])) {
      if (cur.empty() || cur == ".") {
        cur.clear();
        continue;
      }
      if (cur == "..") {
        if (!parts.empty() && parts.back() != "..") {
          parts.pop_back();
        } else if (!absolute) {
          parts.push_back("..");
        }
      } else {
        parts.push_back(cur);
      }
      cur.clear();
    } else {
      cur.push_back(in[i]);
    }
  }

  std::string out = root;
  if (absolute) out.push_back(kSep);
  for (size_t k = 0; k < parts.size(); ++k) {
    if (k) out.push_back(kSep);
    out += parts[k];
  }
  if (out.empty()) out = ".";
  return out;
}

#ifndef _WIN32
std::string current_dir() {
  char buffer[PATH_MAX];
  if (::getcwd(buffer, sizeof(buffer))) return std::string(buffer);
  return std::string(".");
}
#endif

#ifdef _WIN32
/// UTF-8 -> UTF-16 for API calls, adding the long-path prefix when needed.
std::wstring win_path(const std::string& utf8) {
  std::wstring w = utf8_to_wide(utf8);
  if (w.size() < 248) return w;
  if (w.size() >= 2 && w[1] == L':') return L"\\\\?\\" + w;
  if (w.size() >= 2 && w[0] == L'\\' && w[1] == L'\\') return L"\\\\?\\UNC\\" + w.substr(2);
  return w;
}

std::string win_error_text(DWORD code) {
  char message[256];
  message[0] = '\0';
  const DWORD n = FormatMessageA(
      FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS, nullptr, code,
      MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT), message, sizeof(message) - 1, nullptr);
  std::string detail;
  if (n > 0) {
    detail = trim(std::string(message, n));
  }
  char buffer[512];
  if (detail.empty()) {
    std::snprintf(buffer, sizeof(buffer), "Windows error %lu", static_cast<unsigned long>(code));
  } else {
    std::snprintf(buffer, sizeof(buffer), "Windows error %lu: %s",
                  static_cast<unsigned long>(code), detail.c_str());
  }
  return buffer;
}

std::string filetime_to_iso(const FILETIME& ft) {
  ULARGE_INTEGER li;
  li.LowPart = ft.dwLowDateTime;
  li.HighPart = ft.dwHighDateTime;
  const uint64_t kUnixEpochInFiletime = 116444736000000000ull;
  if (li.QuadPart < kUnixEpochInFiletime) return std::string();
  const uint64_t unix_100ns = li.QuadPart - kUnixEpochInFiletime;
  return iso8601_from_unix(static_cast<int64_t>(unix_100ns / 10000000ull));
}

bool win_read_into(HANDLE h, char* dest, size_t want, size_t* got) {
  *got = 0;
  while (*got < want) {
    DWORD chunk = 0;
    const DWORD request = static_cast<DWORD>(std::min<size_t>(want - *got, 1u << 20));
    if (!ReadFile(h, dest + *got, request, &chunk, nullptr)) return false;
    if (chunk == 0) break;
    *got += chunk;
  }
  return true;
}
#endif

}  // namespace

// ---------------------------------------------------------------------------
// diagnostics
// ---------------------------------------------------------------------------
std::string last_os_error() {
#ifdef _WIN32
  return win_error_text(GetLastError());
#else
  return std::string(std::strerror(errno));
#endif
}

// ---------------------------------------------------------------------------
// path manipulation
// ---------------------------------------------------------------------------
std::string expand_user(const std::string& p) {
  if (p == "~") return home_dir();
  if (starts_with(p, "~/") || starts_with(p, "~\\")) return path_join(home_dir(), p.substr(2));
  return p;
}

std::string path_join(const std::string& a, const std::string& b) {
  if (a.empty()) return b;
  if (b.empty()) return a;
  std::string out = a;
  while (out.size() > 1 && is_sep(out.back())) out.pop_back();
  size_t start = 0;
  while (start < b.size() && is_sep(b[start])) ++start;
  if (out.empty()) return b.substr(start);
  if (start >= b.size()) return out;
  // "C:" + "x" must become "C:\x"
  if (out.size() == 2 && out[1] == ':') out.push_back(kSep);
  else out.push_back(kSep);
  out += b.substr(start);
  return out;
}

std::string path_filename(const std::string& p) {
  if (p.empty()) return p;
  size_t end = p.size();
  while (end > 1 && is_sep(p[end - 1])) --end;
  const size_t pos = p.find_last_of("/\\");
  if (pos == std::string::npos || pos >= end) return p.substr(0, end);
  return p.substr(pos + 1, end - pos - 1);
}

std::string path_parent(const std::string& p) {
  if (p.empty()) return p;
  size_t end = p.size();
  while (end > 1 && is_sep(p[end - 1])) --end;
  const size_t pos = p.find_last_of("/\\", end - 1);
  if (pos == std::string::npos) return std::string();
#ifdef _WIN32
  if (pos == 2 && p.size() >= 3 && p[1] == ':') return p.substr(0, 3);
#endif
  if (pos == 0) return p.substr(0, 1);
  return p.substr(0, pos);
}

std::string path_extension(const std::string& p) {
  const std::string name = path_filename(p);
  const size_t dot = name.find_last_of('.');
  if (dot == std::string::npos || dot == 0) return std::string();
  return to_lower(name.substr(dot));
}

std::string path_stem(const std::string& p) {
  const std::string name = path_filename(p);
  const size_t dot = name.find_last_of('.');
  if (dot == std::string::npos || dot == 0) return name;
  return name.substr(0, dot);
}

bool path_is_absolute(const std::string& p) {
  if (p.empty()) return false;
#ifdef _WIN32
  if (is_sep(p[0])) return true;
  return p.size() >= 3 && p[1] == ':' && is_sep(p[2]);
#else
  return p[0] == '/';
#endif
}

std::string path_absolute(const std::string& p) {
  if (p.empty()) return p;
#ifdef _WIN32
  const std::wstring w = utf8_to_wide(p);
  DWORD need = GetFullPathNameW(w.c_str(), 0, nullptr, nullptr);
  if (need == 0) return lexical_normal(p);
  std::wstring buffer(need, L'\0');
  const DWORD got = GetFullPathNameW(w.c_str(), need, &buffer[0], nullptr);
  if (got == 0 || got >= need) return lexical_normal(p);
  buffer.resize(got);
  return wide_to_utf8(buffer);
#else
  if (p[0] == '/') return lexical_normal(p);
  return lexical_normal(path_join(current_dir(), p));
#endif
}

std::string path_canonical(const std::string& p) {
  const std::string abs = path_absolute(p);
#ifdef _WIN32
  HANDLE h = CreateFileW(utf8_to_wide(abs).c_str(), 0,
                         FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
                         OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, nullptr);
  if (h == INVALID_HANDLE_VALUE) return abs;
  std::vector<wchar_t> buffer(1024);
  DWORD n = 0;
  for (;;) {
    n = GetFinalPathNameByHandleW(h, &buffer[0], static_cast<DWORD>(buffer.size()),
                                  FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
    if (n == 0) break;
    if (n < buffer.size()) break;
    buffer.resize(buffer.size() * 2);
    if (buffer.size() > (1u << 20)) {
      n = 0;
      break;
    }
  }
  CloseHandle(h);
  if (n == 0) return abs;
  std::string out = wide_to_utf8(std::wstring(&buffer[0], n));
  if (starts_with(out, "\\\\?\\UNC\\")) {
    out = "\\\\" + out.substr(8);
  } else if (starts_with(out, "\\\\?\\")) {
    out = out.substr(4);
  }
  return out.empty() ? abs : out;
#else
  char resolved[PATH_MAX];
  if (::realpath(abs.c_str(), resolved)) return lexical_normal(std::string(resolved));
  return abs;
#endif
}

std::string path_generic(const std::string& p) {
  std::string out = p;
  for (size_t i = 0; i < out.size(); ++i) {
    if (out[i] == '\\') out[i] = '/';
  }
  return out;
}

// ---------------------------------------------------------------------------
// queries
// ---------------------------------------------------------------------------
bool path_exists(const std::string& p) {
  if (p.empty()) return false;
#ifdef _WIN32
  return GetFileAttributesW(win_path(p).c_str()) != INVALID_FILE_ATTRIBUTES;
#else
  struct stat st;
  return ::stat(p.c_str(), &st) == 0;
#endif
}

bool path_is_dir(const std::string& p) {
  if (p.empty()) return false;
#ifdef _WIN32
  const DWORD attr = GetFileAttributesW(win_path(p).c_str());
  return attr != INVALID_FILE_ATTRIBUTES && (attr & FILE_ATTRIBUTE_DIRECTORY) != 0;
#else
  struct stat st;
  return ::stat(p.c_str(), &st) == 0 && S_ISDIR(st.st_mode);
#endif
}

bool path_is_file(const std::string& p) {
  if (p.empty()) return false;
#ifdef _WIN32
  const DWORD attr = GetFileAttributesW(win_path(p).c_str());
  if (attr == INVALID_FILE_ATTRIBUTES) return false;
  if (attr & FILE_ATTRIBUTE_DIRECTORY) return false;
  return true;
#else
  struct stat st;
  return ::stat(p.c_str(), &st) == 0 && S_ISREG(st.st_mode);
#endif
}

bool make_dirs(const std::string& p) {
  if (p.empty()) return false;
  if (path_is_dir(p)) return true;
#ifdef _WIN32
  if (CreateDirectoryW(win_path(p).c_str(), nullptr)) return true;
  const DWORD err = GetLastError();
  if (err == ERROR_ALREADY_EXISTS) return true;
  const std::string parent = path_parent(p);
  if (parent.empty() || parent == p) return false;
  if (!path_is_dir(parent) && !make_dirs(parent)) return false;
  if (CreateDirectoryW(win_path(p).c_str(), nullptr)) return true;
  return GetLastError() == ERROR_ALREADY_EXISTS;
#else
  if (::mkdir(p.c_str(), 0777) == 0) return true;
  if (errno == EEXIST) return path_is_dir(p);
  const std::string parent = path_parent(p);
  if (parent.empty() || parent == p) return false;
  if (!path_is_dir(parent) && !make_dirs(parent)) return false;
  return ::mkdir(p.c_str(), 0777) == 0;
#endif
}

bool path_is_within(const std::string& child, const std::string& parent) {
  std::string c = path_canonical(child);
  std::string p = path_canonical(parent);
  if (c.empty() || p.empty()) return false;
#ifdef _WIN32
  c = to_lower(c);
  p = to_lower(p);
#endif
  if (c == p) return true;
  if (!p.empty() && !is_sep(p.back())) p.push_back(kSep);
  if (c.size() < p.size()) return false;
  for (size_t i = 0; i < p.size(); ++i) {
    if (!char_equal(c[i], p[i])) return false;
  }
  return true;
}

uint64_t file_size(const std::string& p) {
#ifdef _WIN32
  WIN32_FILE_ATTRIBUTE_DATA data;
  if (!GetFileAttributesExW(win_path(p).c_str(), GetFileExInfoStandard, &data)) return 0;
  return (static_cast<uint64_t>(data.nFileSizeHigh) << 32) | data.nFileSizeLow;
#else
  struct stat st;
  if (::stat(p.c_str(), &st) != 0) return 0;
  return static_cast<uint64_t>(st.st_size);
#endif
}

std::string file_mtime_iso(const std::string& p) {
#ifdef _WIN32
  WIN32_FILE_ATTRIBUTE_DATA data;
  if (!GetFileAttributesExW(win_path(p).c_str(), GetFileExInfoStandard, &data)) return std::string();
  return filetime_to_iso(data.ftLastWriteTime);
#else
  struct stat st;
  if (::stat(p.c_str(), &st) != 0) return std::string();
  return iso8601_from_unix(static_cast<int64_t>(st.st_mtime));
#endif
}

bool list_directory(const std::string& dir, std::vector<DirEntryInfo>* out, std::string* err) {
  out->clear();
  if (dir.empty()) {
    if (err) *err = "directory path is empty";
    return false;
  }
#ifdef _WIN32
  std::wstring pattern = win_path(dir);
  if (!pattern.empty() && !is_sep(static_cast<char>(pattern.back() & 0x7f))) {
    pattern.push_back(L'\\');
  }
  pattern.push_back(L'*');

  WIN32_FIND_DATAW data;
  HANDLE h = FindFirstFileW(pattern.c_str(), &data);
  if (h == INVALID_HANDLE_VALUE) {
    const DWORD code = GetLastError();
    if (code == ERROR_FILE_NOT_FOUND) return true;  // empty directory
    if (err) *err = "cannot list '" + dir + "': " + win_error_text(code);
    return false;
  }
  do {
    const std::wstring name(data.cFileName);
    if (name == L"." || name == L"..") continue;
    DirEntryInfo info;
    info.name = wide_to_utf8(name);
    info.path = path_join(dir, info.name);
    info.is_dir = (data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
    if (!info.is_dir) {
      info.size = (static_cast<uint64_t>(data.nFileSizeHigh) << 32) | data.nFileSizeLow;
    }
    info.mtime = filetime_to_iso(data.ftLastWriteTime);
    out->push_back(info);
  } while (FindNextFileW(h, &data));
  FindClose(h);
#else
  DIR* d = ::opendir(dir.c_str());
  if (!d) {
    if (err) *err = "cannot list '" + dir + "': " + std::strerror(errno);
    return false;
  }
  struct dirent* entry = nullptr;
  while ((entry = ::readdir(d)) != nullptr) {
    const std::string name = entry->d_name;
    if (name == "." || name == "..") continue;
    DirEntryInfo info;
    info.name = name;
    info.path = path_join(dir, name);
    struct stat st;
    if (::stat(info.path.c_str(), &st) == 0) {
      info.is_dir = S_ISDIR(st.st_mode);
      if (!info.is_dir) info.size = static_cast<uint64_t>(st.st_size);
      info.mtime = iso8601_from_unix(static_cast<int64_t>(st.st_mtime));
    }
    out->push_back(info);
  }
  ::closedir(d);
#endif

  std::sort(out->begin(), out->end(), [](const DirEntryInfo& a, const DirEntryInfo& b) {
    if (a.is_dir != b.is_dir) return a.is_dir;
    const std::string la = to_lower(a.name);
    const std::string lb = to_lower(b.name);
    if (la != lb) return la < lb;
    return a.name < b.name;
  });
  return true;
}

// ---------------------------------------------------------------------------
// file I/O
// ---------------------------------------------------------------------------
namespace {
constexpr size_t kUnboundedReadCap = 256u * 1024u * 1024u;
}

bool read_file(const std::string& path, std::string* out, size_t max_bytes, bool* truncated) {
  if (truncated) *truncated = false;
  out->clear();
  if (path.empty() || !path_is_file(path)) return false;
  const size_t limit = (max_bytes == 0) ? kUnboundedReadCap : max_bytes;

#ifdef _WIN32
  HANDLE h = CreateFileW(win_path(path).c_str(), GENERIC_READ,
                         FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
                         OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
  if (h == INVALID_HANDLE_VALUE) return false;
  LARGE_INTEGER size;
  size.QuadPart = 0;
  if (!GetFileSizeEx(h, &size)) {
    CloseHandle(h);
    return false;
  }
  const uint64_t total = static_cast<uint64_t>(size.QuadPart);
  const size_t want = static_cast<size_t>(total < limit ? total : limit);
  out->resize(want);
  size_t got = 0;
  const bool read_ok = want == 0 ? true : win_read_into(h, &(*out)[0], want, &got);
  if (want == 0) {
    char probe = 0;
    DWORD probe_got = 0;
    if (ReadFile(h, &probe, 1, &probe_got, nullptr) && probe_got > 0 && truncated) *truncated = true;
  } else if (total > want && truncated) {
    *truncated = true;
  }
  CloseHandle(h);
  if (!read_ok) return false;
  out->resize(got);
  return true;
#else
  const int fd = ::open(path.c_str(), O_RDONLY);
  if (fd < 0) return false;
  struct stat st;
  if (::fstat(fd, &st) != 0) {
    ::close(fd);
    return false;
  }
  const uint64_t total = static_cast<uint64_t>(st.st_size);
  const size_t want = static_cast<size_t>(total < limit ? total : limit);
  out->resize(want);
  size_t got = 0;
  while (got < want) {
    const ssize_t n = ::read(fd, &(*out)[0] + got, want - got);
    if (n > 0) {
      got += static_cast<size_t>(n);
      continue;
    }
    if (n < 0 && errno == EINTR) continue;
    break;
  }
  ::close(fd);
  if (total > want && truncated) *truncated = true;
  out->resize(got);
  return true;
#endif
}

bool read_file_tail(const std::string& path, size_t max_bytes, std::string* out) {
  out->clear();
  if (path.empty() || !path_is_file(path)) return false;
  const uint64_t total = file_size(path);
  const uint64_t skip = total > max_bytes ? total - max_bytes : 0;

#ifdef _WIN32
  HANDLE h = CreateFileW(win_path(path).c_str(), GENERIC_READ,
                         FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
                         OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
  if (h == INVALID_HANDLE_VALUE) return false;
  LARGE_INTEGER pos;
  pos.QuadPart = static_cast<LONGLONG>(skip);
  if (!SetFilePointerEx(h, pos, nullptr, FILE_BEGIN)) {
    CloseHandle(h);
    return false;
  }
  const size_t want = static_cast<size_t>(total - skip);
  out->resize(want);
  size_t got = 0;
  const bool read_ok = want == 0 ? true : win_read_into(h, &(*out)[0], want, &got);
  CloseHandle(h);
  if (!read_ok) return false;
  out->resize(got);
  return true;
#else
  const int fd = ::open(path.c_str(), O_RDONLY);
  if (fd < 0) return false;
  if (::lseek(fd, static_cast<off_t>(skip), SEEK_SET) < 0) {
    ::close(fd);
    return false;
  }
  const size_t want = static_cast<size_t>(total - skip);
  out->resize(want);
  size_t got = 0;
  while (got < want) {
    const ssize_t n = ::read(fd, &(*out)[0] + got, want - got);
    if (n > 0) {
      got += static_cast<size_t>(n);
      continue;
    }
    if (n < 0 && errno == EINTR) continue;
    break;
  }
  ::close(fd);
  out->resize(got);
  return true;
#endif
}

// Publisher id used to make temporary file names unique per process: two
// instances sharing one tea home must not pick the same `<file>.tmp-...`.
unsigned long atomic_write_pid() {
#ifdef _WIN32
  return static_cast<unsigned long>(GetCurrentProcessId());
#else
  return static_cast<unsigned long>(::getpid());
#endif
}

bool write_file_atomic(const std::string& path, const std::string& data, std::string* err) {
  if (path.empty()) {
    if (err) *err = "empty path";
    return false;
  }
  static std::atomic<unsigned> counter{0};
  const unsigned long pid = atomic_write_pid();
  const std::string tmp =
      path + ".tmp-" + std::to_string(pid) + "-" + std::to_string(counter.fetch_add(1));

  // The temporary file lives next to the target on purpose: rename is only
  // atomic within one filesystem, and %TEMP% frequently is a different one.
  if (!write_file(tmp, data, false, nullptr)) {
    if (err) *err = "cannot write " + tmp;
    return false;
  }
  // Replace-on-rename: a reader sees either the previous complete file or the new
  // complete one, never a truncated half. That is what makes a concurrent reader
  // (another turn, the HTTP layer, a human with an editor) safe without a lock.
  if (!move_path(tmp, path, err)) {
    remove_path(tmp, false, nullptr);
    return false;
  }
  return true;
}

bool write_file(const std::string& path, const std::string& data, bool append, std::string* err) {
  if (path.empty()) {
    if (err) *err = "empty path";
    return false;
  }
  const std::string parent = path_parent(path);
  if (!parent.empty() && !make_dirs(parent)) {
    if (err) *err = "cannot create directory: " + parent;
    return false;
  }

#ifdef _WIN32
  HANDLE h = CreateFileW(win_path(path).c_str(),
                         append ? FILE_APPEND_DATA : GENERIC_WRITE,
                         FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                         append ? OPEN_ALWAYS : CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
  if (h == INVALID_HANDLE_VALUE) {
    if (err) *err = "cannot open for writing: " + path + " (" + last_os_error() + ")";
    return false;
  }
  size_t written = 0;
  bool ok = true;
  while (written < data.size()) {
    DWORD chunk = 0;
    const DWORD request = static_cast<DWORD>(std::min<size_t>(data.size() - written, 1u << 20));
    if (!WriteFile(h, data.data() + written, request, &chunk, nullptr) || chunk == 0) {
      ok = false;
      break;
    }
    written += chunk;
  }
  CloseHandle(h);
  if (!ok && err) *err = "write failed: " + path;
  return ok;
#else
  int flags = O_WRONLY | O_CREAT | (append ? O_APPEND : O_TRUNC);
  const int fd = ::open(path.c_str(), flags, 0666);
  if (fd < 0) {
    if (err) *err = "cannot open for writing: " + path;
    return false;
  }
  size_t written = 0;
  bool ok = true;
  while (written < data.size()) {
    const ssize_t n = ::write(fd, data.data() + written, data.size() - written);
    if (n > 0) {
      written += static_cast<size_t>(n);
      continue;
    }
    if (n < 0 && errno == EINTR) continue;
    ok = false;
    break;
  }
  ::close(fd);
  if (!ok && err) *err = "write failed: " + path;
  return ok;
#endif
}

bool write_file_at(const std::string& path, uint64_t offset, const std::string& data) {
  if (path.empty()) return false;
  const std::string parent = path_parent(path);
  if (!parent.empty() && !make_dirs(parent)) return false;

#ifdef _WIN32
  HANDLE h = CreateFileW(win_path(path).c_str(), GENERIC_WRITE,
                         FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
                         OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
  if (h == INVALID_HANDLE_VALUE) return false;
  LARGE_INTEGER pos;
  pos.QuadPart = static_cast<LONGLONG>(offset);
  if (!SetFilePointerEx(h, pos, nullptr, FILE_BEGIN)) {
    CloseHandle(h);
    return false;
  }
  size_t written = 0;
  bool ok = true;
  while (written < data.size()) {
    DWORD chunk = 0;
    const DWORD request = static_cast<DWORD>(std::min<size_t>(data.size() - written, 1u << 20));
    if (!WriteFile(h, data.data() + written, request, &chunk, nullptr) || chunk == 0) {
      ok = false;
      break;
    }
    written += chunk;
  }
  if (ok) {
    LARGE_INTEGER end;
    end.QuadPart = static_cast<LONGLONG>(offset + written);
    if (!SetFilePointerEx(h, end, nullptr, FILE_BEGIN) || !SetEndOfFile(h)) ok = false;
  }
  CloseHandle(h);
  return ok;
#else
  const int fd = ::open(path.c_str(), O_WRONLY | O_CREAT, 0666);
  if (fd < 0) return false;
  if (::lseek(fd, static_cast<off_t>(offset), SEEK_SET) < 0) {
    ::close(fd);
    return false;
  }
  size_t written = 0;
  bool ok = true;
  while (written < data.size()) {
    const ssize_t n = ::write(fd, data.data() + written, data.size() - written);
    if (n > 0) {
      written += static_cast<size_t>(n);
      continue;
    }
    if (n < 0 && errno == EINTR) continue;
    ok = false;
    break;
  }
  if (ok && ::ftruncate(fd, static_cast<off_t>(offset + written)) != 0) ok = false;
  ::close(fd);
  return ok;
#endif
}

bool remove_path(const std::string& path, bool recursive, std::string* err) {
  if (!path_exists(path)) {
    if (err) *err = "path does not exist: " + path;
    return false;
  }
  if (path_is_dir(path)) {
    if (recursive) {
      std::vector<DirEntryInfo> entries;
      std::string list_err;
      if (list_directory(path, &entries, &list_err)) {
        for (size_t i = 0; i < entries.size(); ++i) {
          if (!remove_path(entries[i].path, true, err)) return false;
        }
      }
    }
#ifdef _WIN32
    if (!RemoveDirectoryW(win_path(path).c_str())) {
      if (err) *err = "cannot remove directory '" + path + "': " + win_error_text(GetLastError());
      return false;
    }
    return true;
#else
    if (::rmdir(path.c_str()) != 0) {
      if (err) *err = "cannot remove directory '" + path + "': " + std::strerror(errno);
      return false;
    }
    return true;
#endif
  }

#ifdef _WIN32
  const std::wstring w = win_path(path);
  if (!DeleteFileW(w.c_str())) {
    const DWORD code = GetLastError();
    if (code == ERROR_ACCESS_DENIED) {
      SetFileAttributesW(w.c_str(), FILE_ATTRIBUTE_NORMAL);
      if (DeleteFileW(w.c_str())) return true;
    }
    if (err) *err = "cannot delete '" + path + "': " + win_error_text(GetLastError());
    return false;
  }
  return true;
#else
  if (::unlink(path.c_str()) != 0) {
    if (err) *err = "cannot delete '" + path + "': " + std::strerror(errno);
    return false;
  }
  return true;
#endif
}

bool move_path(const std::string& from, const std::string& to, std::string* err) {
  const std::string parent = path_parent(to);
  if (!parent.empty() && !make_dirs(parent)) {
    if (err) *err = "cannot create directory: " + parent;
    return false;
  }
#ifdef _WIN32
  if (MoveFileExW(win_path(from).c_str(), win_path(to).c_str(),
                  MOVEFILE_REPLACE_EXISTING | MOVEFILE_COPY_ALLOWED)) {
    return true;
  }
  if (err) *err = "cannot move '" + from + "' to '" + to + "': " + win_error_text(GetLastError());
  return false;
#else
  if (::rename(from.c_str(), to.c_str()) == 0) return true;
  if (err) *err = "cannot move '" + from + "' to '" + to + "': " + std::strerror(errno);
  return false;
#endif
}

// ---------------------------------------------------------------------------
// AppendFile
// ---------------------------------------------------------------------------
AppendFile::~AppendFile() { close(); }

AppendFile::AppendFile(AppendFile&& other) noexcept
    : handle_(other.handle_),
      open_(other.open_),
      path_(std::move(other.path_)),
      base_size_(other.base_size_),
      written_(other.written_) {
  other.handle_ = -1;
  other.open_ = false;
  other.path_.clear();
  other.base_size_ = 0;
  other.written_ = 0;
}

AppendFile& AppendFile::operator=(AppendFile&& other) noexcept {
  if (this == &other) return *this;
  close();
  handle_ = other.handle_;
  open_ = other.open_;
  path_ = std::move(other.path_);
  base_size_ = other.base_size_;
  written_ = other.written_;
  other.handle_ = -1;
  other.open_ = false;
  other.path_.clear();
  other.base_size_ = 0;
  other.written_ = 0;
  return *this;
}

bool AppendFile::open(const std::string& path, bool truncate) {
  close();
  if (path.empty()) return false;
  const std::string parent = path_parent(path);
  if (!parent.empty()) make_dirs(parent);
  path_ = path;

#ifdef _WIN32
  HANDLE h = CreateFileW(win_path(path).c_str(), FILE_APPEND_DATA,
                         FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                         truncate ? CREATE_ALWAYS : OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
  if (h == INVALID_HANDLE_VALUE) return false;
  handle_ = reinterpret_cast<std::intptr_t>(h);
#else
  const int flags = O_WRONLY | O_CREAT | (truncate ? O_TRUNC : O_APPEND);
  const int fd = ::open(path.c_str(), flags, 0666);
  if (fd < 0) return false;
  handle_ = static_cast<std::intptr_t>(fd);
#endif
  open_ = true;
  base_size_ = truncate ? 0 : file_size(path);
  written_ = 0;
  return true;
}

bool AppendFile::write(const std::string& data) {
  if (!open_) return false;
  if (data.empty()) return true;
#ifdef _WIN32
  HANDLE h = reinterpret_cast<HANDLE>(handle_);
  size_t written = 0;
  while (written < data.size()) {
    DWORD chunk = 0;
    const DWORD request = static_cast<DWORD>(std::min<size_t>(data.size() - written, 1u << 20));
    if (!WriteFile(h, data.data() + written, request, &chunk, nullptr) || chunk == 0) return false;
    written += chunk;
  }
  written_ += written;
  return true;
#else
  const int fd = static_cast<int>(handle_);
  size_t written = 0;
  while (written < data.size()) {
    const ssize_t n = ::write(fd, data.data() + written, data.size() - written);
    if (n > 0) {
      written += static_cast<size_t>(n);
      continue;
    }
    if (n < 0 && errno == EINTR) continue;
    return false;
  }
  written_ += written;
  return true;
#endif
}

void AppendFile::close() {
  if (!open_) return;
#ifdef _WIN32
  CloseHandle(reinterpret_cast<HANDLE>(handle_));
#else
  ::close(static_cast<int>(handle_));
#endif
  handle_ = -1;
  open_ = false;
}

uint64_t AppendFile::size() const { return base_size_ + written_; }

}  // namespace tea
