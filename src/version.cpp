#include "version.h"

#include "util.h"

namespace tea {
namespace {

/// The version this binary reports - the only hand-edited version truth in
/// the tree. CMake parses it from here (CMakeLists.txt) to fill in
/// project(VERSION) and the buildinfo file, so the binary, the build log and
/// the buildinfo always say the same thing.
///
/// It lives in this .cpp rather than in version.h on purpose: version.h is
/// included by six translation units, and a constant in a header is pulled
/// into every one of them - a version bump would rebuild all six
/// (server.cpp among them) instead of this single file.
const char* const kVersionString = "0.17.6";

/// The commit recorded in tea_agent.buildinfo next to the executable.
///
/// The build writes that file (cmake/version_git_hash.cmake), so the binary
/// itself carries no build-specific data: the same source built twice produces
/// the same bytes, and a single-file copy of the binary simply has no
/// provenance to show. An empty result means "there is no buildinfo file",
/// which is not the same as "built from an unknown commit" - the caller
/// decides what to print.
std::string read_buildinfo_commit() {
  const std::string dir = executable_dir();
  if (dir.empty()) return std::string();
  std::string text;
  if (!read_file(path_join(dir, "tea_agent.buildinfo"), &text, 4096)) return std::string();
  const std::string marker = "commit=";
  const std::size_t pos = text.find(marker);
  if (pos == std::string::npos) return std::string();
  const std::size_t start = pos + marker.size();
  const std::size_t end = text.find_first_of("\r\n", start);
  const std::string value =
      text.substr(start, end == std::string::npos ? std::string::npos : end - start);
  return trim(value);
}

}  // namespace

const char* version_string() {
  return kVersionString;
}

std::string version_git_hash() {
  // Read once: the file cannot change under a running process in any way that
  // matters, and the banner asks for it on every start.
  static const std::string hash = read_buildinfo_commit();
  return hash;
}

}  // namespace tea
