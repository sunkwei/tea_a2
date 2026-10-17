#include "log.h"

#include <cstdio>
#include <ctime>

#include "util.h"

#ifdef _WIN32
#include <windows.h>
#endif

namespace tea {
namespace {

const uint64_t kRotateBytes = 8ull * 1024 * 1024;

std::string timestamp_for_log() {
  const std::time_t t = std::time(nullptr);
  std::tm tmv{};
#ifdef _WIN32
  localtime_s(&tmv, &t);
#else
  localtime_r(&t, &tmv);
#endif
  char buf[32];
  std::snprintf(buf, sizeof(buf), "%04d-%02d-%02d %02d:%02d:%02d", tmv.tm_year + 1900,
                tmv.tm_mon + 1, tmv.tm_mday, tmv.tm_hour, tmv.tm_min, tmv.tm_sec);
  return buf;
}

}  // namespace

const char* log_level_name(LogLevel level) {
  switch (level) {
    case LogLevel::Trace: return "TRACE";
    case LogLevel::Debug: return "DEBUG";
    case LogLevel::Info:  return "INFO";
    case LogLevel::Warn:  return "WARN";
    case LogLevel::Error: return "ERROR";
    case LogLevel::Off:   return "OFF";
  }
  return "?";
}

LogLevel log_level_from_string(const std::string& s) {
  const std::string v = to_lower(trim(s));
  if (v == "trace") return LogLevel::Trace;
  if (v == "debug") return LogLevel::Debug;
  if (v == "info") return LogLevel::Info;
  if (v == "warn" || v == "warning") return LogLevel::Warn;
  if (v == "error") return LogLevel::Error;
  if (v == "off" || v == "none") return LogLevel::Off;
  return LogLevel::Info;
}

// ---------------------------------------------------------------------------
// console output
// ---------------------------------------------------------------------------
void console_write(const std::string& utf8_text) {
#ifdef _WIN32
  const HANDLE h = GetStdHandle(STD_OUTPUT_HANDLE);
  DWORD mode = 0;
  if (h && h != INVALID_HANDLE_VALUE && GetConsoleMode(h, &mode)) {
    const std::wstring w = utf8_to_wide(utf8_text);
    size_t offset = 0;
    while (offset < w.size()) {
      DWORD written = 0;
      const DWORD chunk =
          static_cast<DWORD>((w.size() - offset) > 8192 ? 8192 : (w.size() - offset));
      if (!WriteConsoleW(h, w.data() + offset, chunk, &written, nullptr)) break;
      if (written == 0) break;
      offset += written;
    }
    return;
  }
#endif
  std::fwrite(utf8_text.data(), 1, utf8_text.size(), stdout);
  std::fflush(stdout);
}

void console_write_line(const std::string& utf8_text) { console_write(utf8_text + "\n"); }

// ---------------------------------------------------------------------------
// Logger
// ---------------------------------------------------------------------------
Logger& Logger::instance() {
  static Logger logger;
  return logger;
}

void Logger::rotate_if_needed(AppendFile& file) {
  if (!file.is_open()) return;
  if (file.size() < kRotateBytes) return;
  const std::string path = file.path();
  file.close();
  const std::string backup = path + ".1";
  std::remove(backup.c_str());
  std::rename(path.c_str(), backup.c_str());
  file.open(path, false);
}

void Logger::configure(const std::string& file_path, LogLevel level, bool console) {
  std::lock_guard<std::mutex> lock(mu_);
  level_ = level;
  console_ = console;
  if (!file_path.empty() && file_path != global_path_) {
    global_path_ = file_path;
    global_file_.open(file_path, false);
  }
}

void Logger::set_level(LogLevel level) {
  std::lock_guard<std::mutex> lock(mu_);
  level_ = level;
}

LogLevel Logger::level() const {
  std::lock_guard<std::mutex> lock(mu_);
  return level_;
}

std::string Logger::log_file() const {
  std::lock_guard<std::mutex> lock(mu_);
  return global_path_;
}

void Logger::add_sink(const std::string& tag, const std::string& file_path) {
  if (file_path.empty()) return;
  std::lock_guard<std::mutex> lock(mu_);
  for (size_t i = 0; i < sinks_.size(); ++i) {
    if (sinks_[i].tag != tag) continue;
    if (sinks_[i].file.is_open() && sinks_[i].file.path() == file_path) return;
    sinks_.erase(sinks_.begin() + static_cast<std::ptrdiff_t>(i));
    break;
  }
  Sink sink;
  sink.tag = tag;
  sink.file.open(file_path, false);
  sinks_.push_back(std::move(sink));
}

void Logger::remove_sink(const std::string& tag) {
  std::lock_guard<std::mutex> lock(mu_);
  for (size_t i = 0; i < sinks_.size(); ++i) {
    if (sinks_[i].tag == tag) {
      sinks_.erase(sinks_.begin() + static_cast<std::ptrdiff_t>(i));
      return;
    }
  }
}

void Logger::write(LogLevel level, const std::string& tag, const std::string& message) {
  if (static_cast<int>(level) < static_cast<int>(level_)) return;

  std::string line = timestamp_for_log();
  line += " [";
  line += log_level_name(level);
  line += "] ";
  if (!tag.empty()) {
    line += "[";
    line += tag;
    line += "] ";
  }
  line += message;
  if (line.empty() || line.back() != '\n') line += '\n';

  std::lock_guard<std::mutex> lock(mu_);
  if (global_file_.is_open()) {
    rotate_if_needed(global_file_);
    global_file_.write(line);
  }
  for (size_t i = 0; i < sinks_.size(); ++i) {
    if (!sinks_[i].file.is_open()) continue;
    rotate_if_needed(sinks_[i].file);
    sinks_[i].file.write(line);
  }
  if (console_) console_write(sanitize_for_terminal(line));
}

}  // namespace tea
