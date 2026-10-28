// version.h -- the accessors the binary reports its version with.
//
// The version STRING is not here: it lives in version.cpp, next to the
// function that returns it. version.h is included by six translation units,
// and a constant in a header is pulled into every one of them - so bumping
// the version would rebuild six files (server.cpp among them) instead of the
// one that actually holds the string. CMake reads the literal from
// version.cpp (see CMakeLists.txt), so there is still exactly one copy of it
// in the tree.
//
// The git commit is not here either: it is build metadata, so the build
// writes it into tea_agent.buildinfo *next to the binary*
// (cmake/version_git_hash.cmake) and the binary reads that file at startup.
// A build therefore never modifies a tracked file, and the binary itself
// carries no build-specific data - two builds of the same commit produce
// the same bytes.
#pragma once

#include <string>

#define TEA_AGENT_NAME "tea_agent"

namespace tea {

/// The version this binary was built from, e.g. "0.15.0" (see version.cpp).
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
