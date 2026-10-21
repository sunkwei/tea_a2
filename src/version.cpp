#include "version.h"

// version.cpp -- single place for the version string.
// cmake sets GIT_HASH to the current commit hash, or "unknown" if the source is not in a git repo.
#define GIT_HASH "1093f3d95381-dirty"
#define TEA_AGENT_VERSION_STRING "0.8.2"

namespace tea {
    const char *version_string() {
        return TEA_AGENT_VERSION_STRING;
    }

    const char *version_git_hash() {
        return GIT_HASH;
    }
} // namespace tea