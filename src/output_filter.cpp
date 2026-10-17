#include "output_filter.h"

#include <algorithm>
#include <sstream>
#include <vector>

#include "util.h"

namespace tea {
namespace {

enum class Kind { Generic, Build, Test, Diff };

bool has_any(const std::string& haystack, const char* const* needles, std::size_t count) {
  for (std::size_t i = 0; i < count; ++i) {
    if (haystack.find(needles[i]) != std::string::npos) return true;
  }
  return false;
}

Kind classify_command(const std::string& command) {
  const std::string c = to_lower(command);
  static const char* const kBuild[] = {
      "cmake", "make", "ninja", "msbuild", "gcc", "g++", "clang", "ld ",
      "cargo build", "cargo check", "go build", "gradle", "mvn ", "dotnet build",
      "npm run build", "tsc", "rustc", "cc1"};
  static const char* const kTest[] = {
      "ctest", "pytest", "go test", "cargo test", "npm test", "npm run test",
      "jest", "mocha", "dotnet test", "python -m unittest", "phpunit", "make test",
      "make check", "vitest"};
  static const char* const kDiff[] = {"git diff", "git log", "git show", "git status"};
  if (has_any(c, kBuild, sizeof(kBuild) / sizeof(kBuild[0]))) return Kind::Build;
  if (has_any(c, kTest, sizeof(kTest) / sizeof(kTest[0]))) return Kind::Test;
  if (has_any(c, kDiff, sizeof(kDiff) / sizeof(kDiff[0]))) return Kind::Diff;
  return Kind::Generic;
}

/// Progress chatter that carries no information once the build finished.
bool is_noise(const std::string& line) {
  const std::string t = trim(line);
  if (t.empty()) return false;
  // "[ 42%] Building CXX object ..." / "[  7%] Built target"
  if (t[0] == '[') {
    std::size_t i = 1;
    while (i < t.size() && (t[i] == ' ' || (t[i] >= '0' && t[i] <= '9') || t[i] == '%')) ++i;
    if (i > 1 && i < t.size() && t[i] == ']') return true;
  }
  if (starts_with(t, "-- ")) return true;  // cmake configure progress
  if (starts_with(t, "Note: including file")) return true;
  if (starts_with(t, "注意: 包含文件")) return true;
  return false;
}

/// Lines that carry the reason a build/test did or did not succeed.
bool is_diagnostic(const std::string& line) {
  const std::string t = to_lower(line);
  if (t.empty()) return false;
  static const char* const kMarkers[] = {
      "error", "fatal", "failed", "failure", "undefined reference", "cannot find",
      "not found", "no such file", "assert", "traceback", "panic:", "segmentation",
      "warning:", "deprecated", "unresolved", "multiple definition", "exception",
      "错误", "失败", "无法", "异常", "未定义"};
  return has_any(t, kMarkers, sizeof(kMarkers) / sizeof(kMarkers[0]));
}

std::vector<std::string> to_lines(const std::string& text) {
  std::vector<std::string> lines;
  std::string current;
  for (std::size_t i = 0; i < text.size(); ++i) {
    const char c = text[i];
    if (c == '\n') {
      if (!current.empty() && current.back() == '\r') current.pop_back();
      lines.push_back(current);
      current.clear();
    } else {
      current.push_back(c);
    }
  }
  if (!current.empty() || !text.empty()) lines.push_back(current);
  return lines;
}

std::string omitted_marker(std::size_t lines, const char* why) {
  std::ostringstream out;
  out << "... [tea_agent omitted " << lines << " line";
  if (lines != 1) out << "s";
  out << " of output: " << why << "] ...";
  return out.str();
}

/// Keeps `keep[i]` ranges, replacing gaps with a single marker each.
std::string merge_ranges(const std::vector<std::string>& lines, const std::vector<bool>& keep,
                         const char* why, std::size_t* dropped_out) {
  std::ostringstream out;
  std::size_t i = 0;
  std::size_t dropped = 0;
  bool first = true;
  while (i < lines.size()) {
    if (keep[i]) {
      if (!first) out << "\n";
      out << lines[i];
      first = false;
      ++i;
      continue;
    }
    std::size_t gap_start = i;
    while (i < lines.size() && !keep[i]) ++i;
    const std::size_t gap = i - gap_start;
    if (gap == 0) continue;
    dropped += gap;
    if (!first) out << "\n";
    out << omitted_marker(gap, why);
    first = false;
  }
  if (dropped_out) *dropped_out = dropped;
  return out.str();
}

std::string head_tail(const std::vector<std::string>& lines, std::size_t head, std::size_t tail,
                      std::size_t* dropped_out) {
  std::vector<bool> keep(lines.size(), false);
  for (std::size_t i = 0; i < lines.size() && i < head; ++i) keep[i] = true;
  for (std::size_t i = lines.size(); i > 0 && lines.size() - i < tail; --i) keep[i - 1] = true;
  return merge_ranges(lines, keep, "middle of a long output with nothing worth keeping", dropped_out);
}

std::string hard_byte_cap(const std::string& text, std::size_t max_bytes, bool* changed) {
  if (text.size() <= max_bytes) return text;
  *changed = true;
  const std::size_t head = max_bytes / 3;
  const std::size_t tail = max_bytes - head;
  std::ostringstream out;
  out << text.substr(0, head) << "\n... [tea_agent omitted " << (text.size() - head - tail)
      << " bytes of output] ...\n"
      << text.substr(text.size() - tail);
  return out.str();
}

}  // namespace

FilteredOutput filter_tool_output(const std::string& command, const std::string& output,
                                  const OutputFilterOptions& options) {
  FilteredOutput result;
  result.text = output;
  result.strategy = "none";

  const std::vector<std::string> lines = to_lines(output);
  const bool over_bytes = output.size() > options.max_bytes;
  const bool over_lines = lines.size() > options.max_lines;
  if (!over_bytes && !over_lines) return result;

  const Kind kind = classify_command(command);
  const std::size_t head = options.head_lines;
  const std::size_t tail = options.tail_lines;
  const std::size_t context = options.context_lines;

  std::string reduced;
  std::size_t dropped = 0;

  if (kind == Kind::Build || kind == Kind::Test) {
    std::vector<bool> keep(lines.size(), false);
    for (std::size_t i = 0; i < lines.size() && i < head; ++i) keep[i] = true;
    for (std::size_t i = lines.size(); i > 0 && lines.size() - i < tail; --i) keep[i - 1] = true;
    // Diagnostics, plus the few lines that usually explain them.
    for (std::size_t i = 0; i < lines.size(); ++i) {
      if (!is_diagnostic(lines[i])) continue;
      const std::size_t end = std::min(lines.size(), i + context + 1);
      for (std::size_t j = i; j < end; ++j) keep[j] = true;
    }
    // Silence the progress chatter inside the retained head/tail as well, unless
    // it is a diagnostic itself.
    for (std::size_t i = 0; i < lines.size(); ++i) {
      if (keep[i] && is_noise(lines[i]) && !is_diagnostic(lines[i])) keep[i] = false;
    }
    reduced = merge_ranges(
        lines, keep, kind == Kind::Build ? "progress noise; diagnostics kept" : "test noise; failures kept",
        &dropped);
    result.strategy = kind == Kind::Build ? "build" : "test";
  } else {
    reduced = head_tail(lines, head, tail, &dropped);
    result.strategy = "generic";
  }

  // Still too long (a single giant diagnostic block, for example): fall back to
  // a plain head+tail so the byte cap can be respected.
  if (reduced.size() > options.max_bytes) {
    reduced = head_tail(to_lines(reduced), head, tail, &dropped);
    result.strategy = "generic";
  }

  bool capped = false;
  reduced = hard_byte_cap(reduced, options.max_bytes, &capped);
  if (capped) result.strategy = "bytes";

  result.text = reduced;
  result.changed = (reduced != output);
  result.dropped_lines = dropped;
  return result;
}

bool output_filter_selftest(std::string* report) {
  std::ostringstream log;
  std::vector<std::string> failures;

  log << "-- The CXX compiler identification is GNU 16.2.0\n";
  for (int i = 0; i < 200; ++i) {
    log << "[ " << (i % 100) << "%] Building CXX object src/file" << i << ".cc.o\n";
  }
  log << "src/main.cc:12:5: error: undefined reference to 'foo'\n";
  log << "  note: this line explains the error above\n";
  for (int i = 0; i < 120; ++i) {
    log << "[ " << (i % 100) << "%] Linking CXX executable app\n";
  }
  log << "gmake: *** [Makefile:9: all] Error 1\n";

  OutputFilterOptions o;
  o.max_bytes = 4096;
  o.max_lines = 60;
  o.head_lines = 5;
  o.tail_lines = 8;
  o.context_lines = 2;

  const FilteredOutput build = filter_tool_output("cmake --build build", log.str(), o);
  if (!build.changed) failures.push_back("build: output was not reduced");
  if (build.strategy != "build") failures.push_back("build: strategy not detected");
  if (build.text.find("undefined reference") == std::string::npos) {
    failures.push_back("build: diagnostic line was dropped");
  }
  if (build.text.find("explains the error above") == std::string::npos) {
    failures.push_back("build: context after the diagnostic was dropped");
  }
  if (build.text.find("Error 1") == std::string::npos) {
    failures.push_back("build: summary line was dropped");
  }
  if (build.text.find("tea_agent omitted") == std::string::npos) {
    failures.push_back("build: no omission marker");
  }
  if (build.text.size() > o.max_bytes) failures.push_back("build: byte cap exceeded");

  std::size_t progress = 0;
  for (std::size_t at = build.text.find("Building CXX object"); at != std::string::npos;
       at = build.text.find("Building CXX object", at + 1)) {
    ++progress;
  }
  if (progress > o.head_lines + o.tail_lines + o.context_lines + 2) {
    failures.push_back("build: progress noise survived");
  }

  const FilteredOutput again = filter_tool_output("cmake --build build", log.str(), o);
  if (again.text != build.text) failures.push_back("build: filter is not deterministic");

  const FilteredOutput generic = filter_tool_output("cat build.log", log.str(), o);
  if (generic.strategy != "generic") failures.push_back("generic: strategy mismatch");
  if (generic.text.find("tea_agent omitted") == std::string::npos) {
    failures.push_back("generic: no omission marker");
  }
  if (generic.text.size() > o.max_bytes) failures.push_back("generic: byte cap exceeded");

  const FilteredOutput tiny = filter_tool_output("cmake --build build", "ok\n", o);
  if (tiny.changed) failures.push_back("small: short output must pass through untouched");

  if (report) {
    std::ostringstream out;
    out << "output filter self test\n";
    out << "  build:   " << build.text.size() << " bytes, strategy=" << build.strategy
        << ", dropped=" << build.dropped_lines << " lines, progress=" << progress << "\n";
    out << "  generic: " << generic.text.size() << " bytes, strategy=" << generic.strategy << "\n";
    if (failures.empty()) {
      out << "  PASS\n";
    } else {
      for (std::size_t i = 0; i < failures.size(); ++i) out << "  FAIL " << failures[i] << "\n";
    }
    *report = out.str();
  }
  return failures.empty();
}

}  // namespace tea
