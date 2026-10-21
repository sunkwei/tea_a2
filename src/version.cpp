#include "version.h"

#include "util.h"

namespace tea {
namespace {

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
  return TEA_AGENT_VERSION_STRING;
}

std::string version_git_hash() {
  // Read once: the file cannot change under a running process in any way that
  // matters, and the banner asks for it on every start.
  static const std::string hash = read_buildinfo_commit();
  return hash;
}

}  // namespace tea
