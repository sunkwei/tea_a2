// version.h -- single place for the version string.
#pragma once

#define TEA_AGENT_NAME "tea_agent"

namespace tea {
    const char *version_string();
    const char *version_git_hash();
} // namespace tea
