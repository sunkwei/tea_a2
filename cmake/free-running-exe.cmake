# cmake/free-running-exe.cmake -- let a rebuild replace an .exe that is running.
#
#   cmake -DEXE=<path> [-DMODE=prepare|clean] -P cmake/free-running-exe.cmake
#
# Windows refuses to open a running executable for writing, so link.exe fails with
# LNK1104 and ld with "cannot open output file ... Permission denied" - the
# classic "stop the app before you rebuild" annoyance. The image is protected in
# a narrower way than it looks (measured on Windows 10 19045):
#
#   MoveFile(running .exe)          -> succeeds: the *name* can be taken away
#   DeleteFile(running .exe)        -> ERROR_ACCESS_DENIED: this is the real lock
#   CreateFile(running .exe, WRITE) -> fails, which is why the linker gives up
#
# So: rename the old binary aside, let the linker write a fresh file under the
# original name, and delete the old copy once the link has succeeded. Nothing has
# to be stopped, and a failed link cannot lose the previous binary either - the
# renamed copy stays on disk until the next successful POST_BUILD.
#
# MODE=prepare  (PRE_LINK)   drop stale copies, then move <exe> to <exe>.prev-<stamp>
# MODE=clean    (POST_BUILD) delete whatever <exe>.prev* is no longer in use
#
# prepare runs in a *child* cmake process: file(RENAME) aborts the script when it
# fails, and a build must never die because of this convenience step - the child's
# non-zero exit only turns into a warning here.

if(NOT WIN32)
  return()
endif()
if(NOT DEFINED EXE)
  message(FATAL_ERROR "free-running-exe.cmake: -DEXE=<path> is required")
endif()
get_filename_component(_exe "${EXE}" ABSOLUTE)
if(NOT DEFINED MODE)
  set(MODE "prepare")
endif()

get_filename_component(_dir "${_exe}" DIRECTORY)
get_filename_component(_name "${_exe}" NAME)

# Copies of previous binaries. Deletable ones (their process is gone) go away;
# locked ones survive and are retried by the next build.
function(tea_clean_prev_copies)
  file(GLOB _copies "${_dir}/${_name}.prev*")
  foreach(_f IN LISTS _copies)
    file(REMOVE "${_f}")
  endforeach()
endfunction()

if(MODE STREQUAL "clean")
  # POST_BUILD: the link succeeded, so the previous binary is no longer needed.
  tea_clean_prev_copies()
  return()
endif()

# --- MODE=prepare, child part ----------------------------------------------
if(DEFINED TEA_FREE_EXE_CHILD)
  tea_clean_prev_copies()
  if(NOT EXISTS "${_exe}")
    message(STATUS "${_name} is not built yet")
    return()
  endif()

  # A locked leftover may have survived the cleanup above, so pick a name that is
  # guaranteed to be free instead of relying on a fixed one.
  string(TIMESTAMP _stamp "%Y%m%d-%H%M%S")
  set(_bak "${_exe}.prev-${_stamp}")
  if(EXISTS "${_bak}")
    message(STATUS "${_name} was already preserved as ${_name}.prev-${_stamp}")
    return()
  endif()

  file(RENAME "${_exe}" "${_bak}")
  if(EXISTS "${_exe}")
    message(WARNING "could not move ${_name} aside")
    return()
  endif()
  message(STATUS "${_name} was moved to ${_name}.prev-${_stamp}")
  return()
endif()

# --- MODE=prepare, parent part ---------------------------------------------
execute_process(
  COMMAND ${CMAKE_COMMAND} -DTEA_FREE_EXE_CHILD=ON -DEXE=${_exe} -DMODE=prepare
          -P ${CMAKE_CURRENT_LIST_FILE}
  RESULT_VARIABLE _rc
  OUTPUT_VARIABLE _out
  ERROR_VARIABLE _err)
string(STRIP "${_out}${_err}" _text)
# The child's message(STATUS ...) output carries cmake's "-- " prefix.
string(REGEX REPLACE "^-+[ \t]*" "" _text "${_text}")
string(REGEX REPLACE "\r?\n" " " _text "${_text}")

if(NOT _rc EQUAL 0)
  message(WARNING
    "tea_agent: could not free ${_exe} for relinking (${_text}). The link will "
    "fail while the old instance keeps running; stop it and build again.")
elseif(_text MATCHES "was moved to")
  message(STATUS
    "tea_agent: ${_text} (it was still running, so the new binary takes the old "
    "name; restart the instance to pick up the new code)")
else()
  message(STATUS "tea_agent: ${_text}")
endif()
