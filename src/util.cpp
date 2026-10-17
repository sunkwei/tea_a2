#include "util.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <iomanip>
#include <random>
#include <sstream>

#ifdef _WIN32
#include <windows.h>
#else
#include <sys/types.h>
#include <unistd.h>
#endif

namespace tea {
namespace {

std::tm gmtime_safe(std::time_t t) {
  std::tm tmv{};
#ifdef _WIN32
  gmtime_s(&tmv, &t);
#else
  gmtime_r(&t, &tmv);
#endif
  return tmv;
}

std::string format_tm(const std::tm& tmv, bool with_ms, int ms) {
  char buf[64];
  std::snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02d", tmv.tm_year + 1900,
                tmv.tm_mon + 1, tmv.tm_mday, tmv.tm_hour, tmv.tm_min, tmv.tm_sec);
  std::string s(buf);
  if (with_ms) {
    char msbuf[8];
    std::snprintf(msbuf, sizeof(msbuf), ".%03d", ms);
    s += msbuf;
  }
  s += "Z";
  return s;
}

std::mt19937_64& rng() {
  static thread_local std::mt19937_64 engine([] {
    std::random_device rd;
    std::seed_seq seq{rd(), rd(), rd(), rd(),
                      static_cast<unsigned>(
                          std::chrono::steady_clock::now().time_since_epoch().count())};
    return std::mt19937_64(seq);
  }());
  return engine;
}

const char* kHex = "0123456789abcdef";

}  // namespace

// ---------------------------------------------------------------------------
// time
// ---------------------------------------------------------------------------
int64_t now_ms() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

std::string now_iso8601() {
  const std::time_t t = std::chrono::system_clock::to_time_t(std::chrono::system_clock::now());
  return format_tm(gmtime_safe(t), false, 0);
}

std::string now_iso8601_ms() {
  const auto now = std::chrono::system_clock::now();
  const std::time_t t = std::chrono::system_clock::to_time_t(now);
  const int ms = static_cast<int>(
      std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()).count() % 1000);
  return format_tm(gmtime_safe(t), true, ms);
}

std::string iso8601_from_unix(int64_t unix_seconds) {
  return format_tm(gmtime_safe(static_cast<std::time_t>(unix_seconds)), false, 0);
}

std::string format_duration_ms(int64_t ms) {
  if (ms < 1000) return std::to_string(ms) + "ms";
  if (ms < 60000) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%.1fs", static_cast<double>(ms) / 1000.0);
    return buf;
  }
  const int64_t total_s = ms / 1000;
  const int64_t m = total_s / 60;
  const int64_t s = total_s % 60;
  char buf[48];
  std::snprintf(buf, sizeof(buf), "%lldm%02llds", static_cast<long long>(m),
                static_cast<long long>(s));
  return buf;
}

// ---------------------------------------------------------------------------
// strings
// ---------------------------------------------------------------------------
std::string to_lower(std::string s) {
  for (char& c : s) {
    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
  }
  return s;
}

std::string trim(const std::string& s) {
  size_t b = 0, e = s.size();
  auto is_ws = [](unsigned char c) { return c == ' ' || c == '\t' || c == '\r' || c == '\n'; };
  while (b < e && is_ws(static_cast<unsigned char>(s[b]))) ++b;
  while (e > b && is_ws(static_cast<unsigned char>(s[e - 1]))) --e;
  return s.substr(b, e - b);
}

bool starts_with(const std::string& s, const std::string& prefix) {
  return s.size() >= prefix.size() && s.compare(0, prefix.size(), prefix) == 0;
}

bool ends_with(const std::string& s, const std::string& suffix) {
  return s.size() >= suffix.size() &&
         s.compare(s.size() - suffix.size(), suffix.size(), suffix) == 0;
}

bool contains(const std::string& s, const std::string& needle) {
  return s.find(needle) != std::string::npos;
}

std::string replace_all(std::string s, const std::string& from, const std::string& to) {
  if (from.empty()) return s;
  size_t pos = 0;
  while ((pos = s.find(from, pos)) != std::string::npos) {
    s.replace(pos, from.size(), to);
    pos += to.size();
  }
  return s;
}

std::vector<std::string> split(const std::string& s, char delim) {
  std::vector<std::string> out;
  std::string cur;
  for (char c : s) {
    if (c == delim) {
      out.push_back(cur);
      cur.clear();
    } else {
      cur.push_back(c);
    }
  }
  out.push_back(cur);
  return out;
}

std::vector<std::string> split_lines(const std::string& s) {
  std::vector<std::string> out;
  std::string cur;
  for (char c : s) {
    if (c == '\n') {
      if (!cur.empty() && cur.back() == '\r') cur.pop_back();
      out.push_back(cur);
      cur.clear();
    } else {
      cur.push_back(c);
    }
  }
  if (!cur.empty()) out.push_back(cur);
  return out;
}

std::string join(const std::vector<std::string>& parts, const std::string& sep) {
  std::string out;
  for (size_t i = 0; i < parts.size(); ++i) {
    if (i) out += sep;
    out += parts[i];
  }
  return out;
}

std::string truncate_utf8(const std::string& s, size_t max_bytes, bool* truncated) {
  if (s.size() <= max_bytes) {
    if (truncated) *truncated = false;
    return s;
  }
  size_t cut = max_bytes;
  while (cut > 0 && (static_cast<unsigned char>(s[cut]) & 0xC0) == 0x80) --cut;
  if (truncated) *truncated = true;
  return s.substr(0, cut);
}

size_t utf8_length(const std::string& s) {
  size_t n = 0;
  for (unsigned char c : s) {
    if ((c & 0xC0) != 0x80) ++n;
  }
  return n;
}

bool is_valid_utf8(const std::string& s) {
  size_t i = 0;
  const size_t n = s.size();
  while (i < n) {
    const unsigned char c = static_cast<unsigned char>(s[i]);
    size_t extra = 0;
    unsigned int cp = 0;
    if (c < 0x80) {
      ++i;
      continue;
    } else if ((c & 0xE0) == 0xC0) {
      extra = 1;
      cp = c & 0x1F;
    } else if ((c & 0xF0) == 0xE0) {
      extra = 2;
      cp = c & 0x0F;
    } else if ((c & 0xF8) == 0xF0) {
      extra = 3;
      cp = c & 0x07;
    } else {
      return false;
    }
    if (i + extra >= n) return false;
    for (size_t k = 1; k <= extra; ++k) {
      const unsigned char cc = static_cast<unsigned char>(s[i + k]);
      if ((cc & 0xC0) != 0x80) return false;
      cp = (cp << 6) | (cc & 0x3F);
    }
    if ((extra == 1 && cp < 0x80) || (extra == 2 && cp < 0x800) || (extra == 3 && cp < 0x10000) ||
        cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) {
      return false;
    }
    i += extra + 1;
  }
  return true;
}

std::string human_size(uint64_t bytes) {
  static const char* units[] = {"B", "KB", "MB", "GB", "TB"};
  double v = static_cast<double>(bytes);
  int u = 0;
  while (v >= 1024.0 && u < 4) {
    v /= 1024.0;
    ++u;
  }
  char buf[48];
  if (u == 0) {
    std::snprintf(buf, sizeof(buf), "%llu B", static_cast<unsigned long long>(bytes));
  } else {
    std::snprintf(buf, sizeof(buf), "%.1f %s", v, units[u]);
  }
  return buf;
}

std::string mask_secret(const std::string& secret) {
  if (secret.empty()) return "";
  if (secret.size() <= 8) return std::string(secret.size(), '*');
  return secret.substr(0, 4) + "..." + secret.substr(secret.size() - 4);
}

// ---------------------------------------------------------------------------
// ids
// ---------------------------------------------------------------------------
std::string random_hex(size_t nbytes) {
  std::uniform_int_distribution<int> dist(0, 15);
  std::string out;
  out.reserve(nbytes * 2);
  for (size_t i = 0; i < nbytes * 2; ++i) out.push_back(kHex[dist(rng())]);
  return out;
}

std::string uuid4() {
  std::uniform_int_distribution<int> dist(0, 15);
  std::uniform_int_distribution<int> dist2(8, 11);
  std::string s;
  s.reserve(36);
  for (int i = 0; i < 36; ++i) {
    if (i == 8 || i == 13 || i == 18 || i == 23) {
      s.push_back('-');
    } else if (i == 14) {
      s.push_back('4');
    } else if (i == 19) {
      s.push_back(kHex[dist2(rng())]);
    } else {
      s.push_back(kHex[dist(rng())]);
    }
  }
  return s;
}

std::string make_id(const std::string& prefix) { return prefix + random_hex(6); }

// ---------------------------------------------------------------------------
// home / executable location
// ---------------------------------------------------------------------------
std::string home_dir() {
#ifdef _WIN32
  const char* up = std::getenv("USERPROFILE");
  if (up && *up) return up;
  const char* hd = std::getenv("HOMEDRIVE");
  const char* hp = std::getenv("HOMEPATH");
  if (hd && hp) return std::string(hd) + hp;
#else
  const char* h = std::getenv("HOME");
  if (h && *h) return h;
#endif
  const char* cwd = std::getenv("CD");
  if (cwd && *cwd) return cwd;
  return std::string(".");
}

std::string tea_home_dir() {
  const char* env = std::getenv("TEA_AGENT_HOME");
  if (env && *env) return path_absolute(env);
  return path_join(home_dir(), ".tea_agent");
}

std::string executable_path() {
#ifdef _WIN32
  std::vector<wchar_t> buffer(1024);
  for (;;) {
    const DWORD n = GetModuleFileNameW(nullptr, &buffer[0], static_cast<DWORD>(buffer.size()));
    if (n == 0) return std::string();
    if (n < buffer.size() - 1) return wide_to_utf8(std::wstring(&buffer[0], n));
    buffer.resize(buffer.size() * 2);
    if (buffer.size() > (1u << 20)) return std::string();
  }
#else
  std::vector<char> buffer(1024);
  for (;;) {
    const ssize_t n = ::readlink("/proc/self/exe", &buffer[0], buffer.size() - 1);
    if (n <= 0) return std::string();
    if (static_cast<size_t>(n) < buffer.size() - 1) return std::string(&buffer[0], n);
    buffer.resize(buffer.size() * 2);
    if (buffer.size() > (1u << 20)) return std::string();
  }
#endif
}

std::string executable_dir() {
  const std::string path = executable_path();
  if (path.empty()) return std::string();
  return path_parent(path);
}

// ---------------------------------------------------------------------------
// platform / encoding
// ---------------------------------------------------------------------------
#ifdef _WIN32
std::wstring utf8_to_wide(const std::string& s) {
  if (s.empty()) return std::wstring();
  const int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
  if (n <= 0) return std::wstring();
  std::wstring w(static_cast<size_t>(n), L'\0');
  MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), &w[0], n);
  return w;
}

std::string wide_to_utf8(const std::wstring& w) {
  if (w.empty()) return std::string();
  const int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), nullptr, 0,
                                    nullptr, nullptr);
  if (n <= 0) return std::string();
  std::string s(static_cast<size_t>(n), '\0');
  WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), &s[0], n, nullptr, nullptr);
  return s;
}

std::string acp_to_utf8(const std::string& s) {
  if (s.empty()) return std::string();
  UINT use = GetOEMCP();
  int n = MultiByteToWideChar(use, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
  if (n <= 0) {
    use = CP_ACP;
    n = MultiByteToWideChar(use, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
    if (n <= 0) return s;
  }
  std::wstring w(static_cast<size_t>(n), L'\0');
  MultiByteToWideChar(use, 0, s.data(), static_cast<int>(s.size()), &w[0], n);
  return wide_to_utf8(w);
}
#else
std::wstring utf8_to_wide(const std::string& s) { return std::wstring(s.begin(), s.end()); }
std::string wide_to_utf8(const std::wstring& w) { return std::string(w.begin(), w.end()); }
std::string acp_to_utf8(const std::string& s) { return s; }
#endif

std::string native_to_utf8(const std::string& s) {
  if (s.empty()) return s;
  if (is_valid_utf8(s)) return s;
  return acp_to_utf8(s);
}

std::string json_escape(const std::string& s) {
  std::string out;
  out.reserve(s.size() + 16);
  for (unsigned char c : s) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
      default:
        if (c < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          out += buf;
        } else {
          out += static_cast<char>(c);
        }
    }
  }
  return out;
}

std::string sanitize_for_terminal(const std::string& s) {
  std::string out;
  out.reserve(s.size());
  for (unsigned char c : s) {
    if (c == 0x1b) {
      out += "\\e";
    } else if (c < 0x20 && c != '\n' && c != '\t' && c != '\r') {
      out += '?';
    } else {
      out += static_cast<char>(c);
    }
  }
  return out;
}

}  // namespace tea
