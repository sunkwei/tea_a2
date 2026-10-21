// version.h -- the version string, and the accessors the binary reports.
//
// This header is the ONLY hand-edited source of truth for the version. The git
// commit is not here: it is build metadata, so the build writes it into
// tea_agent.buildinfo *next to the binary* (cmake/version_git_hash.cmake) and
// the binary reads that file at startup. A build therefore never modifies a
// tracked file, and the binary itself carries no build-specific data - two
// builds of the same commit produce the same bytes.
#pragma once

#include <string>

#define TEA_AGENT_NAME "tea_agent"
#define TEA_AGENT_VERSION_STRING "0.9.1"

namespace tea {

/// The version this binary was built from, e.g. "0.9.1".
const char* version_string();

/// The commit this binary was built from, e.g. "b0575d01c748", with "-dirty"
/// appended when the working tree had uncommitted changes - a build must not
/// claim a clean state it does not have.
///
/// Read from tea_agent.buildinfo next to the executable, so it is empty when
/// that file is not there: a single-file copy of the binary carries no
/// provenance, and says so instead of claiming one.
std::string version_git_hash();

}  // namespace tea
