#include "version.h"

namespace tea {

const char* version_string() {
  return TEA_AGENT_VERSION_STRING;
}

// version_git_hash() is NOT here: it is generated into the build directory
// (generated/version_git.cpp) because the commit is build metadata, not source.

}  // namespace tea
