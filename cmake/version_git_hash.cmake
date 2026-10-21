# Write the build information next to the binary (tea_agent.buildinfo).
#
#   cmake -DREPO_DIR=<repo> -DOUTPUT=<file> -P cmake/version_git_hash.cmake
#
# The commit a binary was built from is build metadata, not source. It used to
# be written into src/version.cpp, which meant that every build left the working
# tree dirty ("git status" was useless right after a build), the tree was then
# permanently "-dirty" - the file's own modification counted, so the old script
# had to special-case the file it wrote - and two build trees (native and
# cross) wrote to the same file.
#
# Now the binary carries no build-specific data at all: the same source built
# twice produces the same bytes, and the provenance lives in a small text file
# next to the executable. Copying the binary alone to a device means there is
# no provenance to show - which the binary then says honestly, instead of
# embedding a hash that would make it unreproducible.
#
# The file is written only when its content changes, so an unchanged commit
# does not touch its timestamp.
#
# Not a git repository (a release tarball) => "unknown".

if(NOT DEFINED REPO_DIR OR NOT DEFINED OUTPUT)
  message(FATAL_ERROR
    "version_git_hash.cmake needs -DREPO_DIR=<dir> and -DOUTPUT=<file>")
endif()

execute_process(
  COMMAND git rev-parse --short=12 HEAD
  WORKING_DIRECTORY ${REPO_DIR}
  OUTPUT_VARIABLE _hash
  ERROR_QUIET
  RESULT_VARIABLE _rc
  OUTPUT_STRIP_TRAILING_WHITESPACE)

if(NOT _rc EQUAL 0 OR _hash STREQUAL "")
  set(_hash "unknown")
  set(_note "not a git repository")
else()
  # A dirty tree is not that commit. Mark it, or the build claims a clean state
  # it does not have. The build directory is normally ignored by git; if it is
  # not, the tree really is dirty and saying so is correct.
  execute_process(
    COMMAND git status --porcelain
    WORKING_DIRECTORY ${REPO_DIR}
    OUTPUT_VARIABLE _dirty
    ERROR_QUIET
    RESULT_VARIABLE _dirty_rc
    OUTPUT_STRIP_TRAILING_WHITESPACE)
  if(_dirty_rc EQUAL 0 AND NOT _dirty STREQUAL "")
    set(_hash "${_hash}-dirty")
  endif()
  set(_note "")
endif()

# string(CONCAT) and not set(): set() with several arguments builds a
# semicolon-separated LIST, which would turn every newline into a ";".
string(CONCAT _content
  "# tea_agent build information\n"
  "#\n"
  "# Written by cmake/version_git_hash.cmake next to the binary. The binary reads\n"
  "# it at startup to report which commit it came from; it carries no build\n"
  "# specific data itself, so it stays byte-for-byte reproducible.\n"
  "version=${TEA_AGENT_VERSION}\n"
  "commit=${_hash}\n")

set(_existing "")
if(EXISTS ${OUTPUT})
  file(READ ${OUTPUT} _existing)
endif()
if(_existing STREQUAL _content)
  message(STATUS "build info: ${_hash} (up to date)")
  return()
endif()

# Write through a temporary file and rename: two build trees writing at the
# same time must never see a half-written file.
set(_tmp "${OUTPUT}.tmp")
file(WRITE ${_tmp} "${_content}")
file(RENAME ${_tmp} ${OUTPUT})
if(_note STREQUAL "")
  message(STATUS "build info: ${_hash}")
else()
  message(STATUS "build info: ${_hash} (${_note})")
endif()
