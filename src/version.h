// version.h -- the version string, and the accessors the binary reports.
//
// This header is the ONLY hand-edited source of truth for the version. The git
// hash is deliberately not here: a commit is build metadata, not source, so the
// build writes it into the build directory (cmake/version_git_hash.cmake) and
// compiles that in. A build that rewrote a tracked source file would leave the
// working tree dirty - "git status" would be useless right after a build, and
// two build trees (native and cross) would fight over one file.
#pragma once

#define TEA_AGENT_NAME "tea_agent"
#define TEA_AGENT_VERSION_STRING "0.9.1"

namespace tea {

/// The version this binary was built from, e.g. "0.9.1".
const char* version_string();

/// The commit this binary was built from, e.g. "b0575d01c748", with "-dirty"
/// appended when the working tree had uncommitted changes - the binary must not
/// claim a clean state it does not have. "unknown" outside a git checkout (a
/// release tarball). Defined in the generated file in the build directory.
const char* version_git_hash();

}  // namespace tea
