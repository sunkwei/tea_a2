# Sync the GIT_HASH in src/version.cpp with the current git HEAD.
#
# Called at build time (see the add_custom_command in CMakeLists.txt):
#   cmake -DVERSION_SOURCE=<path to version.cpp> -DREPO_DIR=<repo root> \
#         -P cmake/sync_git_hash.cmake
#
# Why: the binary prints its commit hash (startup banner, --version). A hash
# that was typed by hand goes stale the moment the next commit lands, and a
# stale hash is worse than none - it looks like provenance but is a lie.
#
# The file is rewritten only when the hash actually differs, so an unchanged
# commit does not touch the timestamp and does not trigger a recompile of
# version.cpp.
#
# Not a git repository (release tarball) => leave the file alone.

if(NOT DEFINED VERSION_SOURCE OR NOT DEFINED REPO_DIR)
  message(FATAL_ERROR "sync_git_hash.cmake needs -DVERSION_SOURCE=<file> -DREPO_DIR=<dir>")
endif()

execute_process(
  COMMAND git rev-parse --short=12 HEAD
  WORKING_DIRECTORY ${REPO_DIR}
  OUTPUT_VARIABLE _hash
  ERROR_QUIET
  RESULT_VARIABLE _rc
  OUTPUT_STRIP_TRAILING_WHITESPACE)

if(NOT _rc EQUAL 0 OR _hash STREQUAL "")
  message(STATUS "git hash: unknown (not a git repository - ${VERSION_SOURCE} left alone)")
  return()
endif()

# A dirty tree is not that commit. Mark it, or the binary claims a clean state
# it does not have. The marker is stable while the tree stays dirty, so this
# does not cause a rewrite on every build.
#
# The one file this script edits itself (version.cpp) does not count: otherwise
# the first build would dirty the tree and every later build would keep saying
# "-dirty" even after the operator committed everything.
execute_process(
  COMMAND git status --porcelain
  WORKING_DIRECTORY ${REPO_DIR}
  OUTPUT_VARIABLE _dirty
  ERROR_QUIET
  RESULT_VARIABLE _dirty_rc
  OUTPUT_STRIP_TRAILING_WHITESPACE)
if(_dirty_rc EQUAL 0 AND NOT _dirty STREQUAL "")
  string(REPLACE "\n" ";" _dirty_lines "${_dirty}")
  set(_user_changes "")
  foreach(_line IN LISTS _dirty_lines)
    if(_line MATCHES "version\\.(cpp|h)$")
      continue()
    endif()
    set(_user_changes "${_user_changes}1")
  endforeach()
  if(NOT _user_changes STREQUAL "")
    set(_hash "${_hash}-dirty")
  endif()
endif()

file(READ ${VERSION_SOURCE} _content)
string(REGEX MATCH "#define[ \t]+GIT_HASH[ \t]+\"([^\"]*)\"" _match "${_content}")
if(NOT _match)
  message(FATAL_ERROR "no `#define GIT_HASH \"...\"` line in ${VERSION_SOURCE}")
endif()
set(_current "${CMAKE_MATCH_1}")

if(_current STREQUAL _hash)
  message(STATUS "git hash: ${_hash} (up to date)")
  return()
endif()

string(REGEX REPLACE "(#define[ \t]+GIT_HASH[ \t]+\")[^\"]*(\")" "\\1${_hash}\\2" _new "${_content}")
if(_new STREQUAL _content)
  message(STATUS "git hash: ${_hash} (already in ${VERSION_SOURCE})")
  return()
endif()

# Write through a temporary file and rename: two build trees (e.g. the native
# one and a cross build) compiling at the same time must never see a
# half-written version.cpp.
set(_tmp "${VERSION_SOURCE}.tmp")
file(WRITE ${_tmp} "${_new}")
file(RENAME ${_tmp} ${VERSION_SOURCE})
message(STATUS "git hash: ${_current} -> ${_hash} (${VERSION_SOURCE} updated)")
