// log.h -- tiny thread-safe logger with a global sink plus tagged extra sinks
// (used to keep a per-workspace log alongside the global one).
#pragma once

#include <mutex>
#include <sstream>
#include <string>
#include <vector>

#include "util.h"

namespace tea {

enum class LogLevel : int { Trace = 0, Debug = 1, Info = 2, Warn = 3, Error = 4, Off = 5 };

const char* log_level_name(LogLevel level);
LogLevel log_level_from_string(const std::string& s);

class Logger {
 public:
  static Logger& instance();

  void configure(const std::string& file_path, LogLevel level, bool console);
  void set_level(LogLevel level);
  LogLevel level() const;

  /// Register/replace a named extra log file. Safe to call repeatedly.
  void add_sink(const std::string& tag, const std::string& file_path);
  void remove_sink(const std::string& tag);

  void write(LogLevel level, const std::string& tag, const std::string& message);

  std::string log_file() const;

 private:
  Logger() = default;

  struct Sink {
    std::string tag;
    AppendFile file;
  };

  static void rotate_if_needed(AppendFile& file);

  mutable std::mutex mu_;
  std::string global_path_;
  AppendFile global_file_;
  LogLevel level_ = LogLevel::Info;
  bool console_ = true;
  std::vector<Sink> sinks_;
};

/// Console/UTF-8 aware writer used by the logger and by --help output.
void console_write(const std::string& utf8_text);
void console_write_line(const std::string& utf8_text);

template <typename... Args>
std::string log_fmt(Args&&... args) {
  std::ostringstream os;
  (void)std::initializer_list<int>{(os << args, 0)...};
  return os.str();
}

}  // namespace tea

#define TEA_LOG(level, ...) \
  ::tea::Logger::instance().write((level), std::string(), ::tea::log_fmt(__VA_ARGS__))
#define TEA_LOG_TAG(level, tag, ...) \
  ::tea::Logger::instance().write((level), (tag), ::tea::log_fmt(__VA_ARGS__))

#define LOG_TRACE(...) TEA_LOG(::tea::LogLevel::Trace, __VA_ARGS__)
#define LOG_DEBUG(...) TEA_LOG(::tea::LogLevel::Debug, __VA_ARGS__)
#define LOG_INFO(...) TEA_LOG(::tea::LogLevel::Info, __VA_ARGS__)
#define LOG_WARN(...) TEA_LOG(::tea::LogLevel::Warn, __VA_ARGS__)
#define LOG_ERROR(...) TEA_LOG(::tea::LogLevel::Error, __VA_ARGS__)
