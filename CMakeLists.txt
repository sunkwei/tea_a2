cmake_minimum_required(VERSION 3.20)

# ---------------------------------------------------------------------------
# Version: one source of truth
# ---------------------------------------------------------------------------
# `src/version.cpp` is what the binary actually reports (--version, the
# User-Agent header, /api/health), so this file parses it instead of carrying
# a second copy. The git commit is build metadata and is generated into the
# build directory (see below), so a build never modifies a tracked file.
# It used to say 0.1.0 while the binary said 0.4.1, which made the configure-time
# line `tea_agent 0.1.0: compiler=...` a lie and left the bump to be done twice
# (and forgotten once). Splitting on the first two dots keeps the build working
# if the string ever gains a pre-release suffix (0.8.2-rc1 -> version 0.8.2).
#
# The string lives in a .cpp and not in a header on purpose: it is used in
# exactly one place, and a constant in version.h would be pulled into the six
# translation units that include it - bumping the version would rebuild all
# six instead of one.
set(TEA_VERSION_SOURCE ${CMAKE_CURRENT_SOURCE_DIR}/src/version.cpp)
if(NOT EXISTS ${TEA_VERSION_SOURCE})
  message(FATAL_ERROR "missing ${TEA_VERSION_SOURCE}; it defines the version string")
endif()
file(READ ${TEA_VERSION_SOURCE} _tea_version_source)
# Take the whole quoted literal first (it may end in -rc1), then its leading
# major.minor.patch: project(VERSION) only accepts digits and dots.
string(REGEX MATCH "kVersionString[ \t]*=[ \t]*\"([^\"]*)\""
       _tea_version_literal "${_tea_version_source}")
set(_tea_version_raw "${CMAKE_MATCH_1}")
string(REGEX MATCH "^[0-9]+\\.[0-9]+\\.[0-9]+" _tea_version_numeric "${_tea_version_raw}")
set(TEA_AGENT_VERSION "${_tea_version_numeric}")
if(NOT TEA_AGENT_VERSION MATCHES "^[0-9]+\\.[0-9]+\\.[0-9]+$")
  message(FATAL_ERROR
    "cannot read the version out of ${TEA_VERSION_SOURCE}; expected\n"
    "  const char* const kVersionString = \"0.13.2\"")
endif()
# The version is read here, at configure time: changing it has to re-run the
# configure step, so make the file a configure dependency.
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS ${TEA_VERSION_SOURCE})
project(tea_agent VERSION ${TEA_AGENT_VERSION} LANGUAGES C CXX)

# ---------------------------------------------------------------------------
# Toolchain sanity: pointing CMAKE_CXX_COMPILER at a mingw triplet is not a
# cross build, and a "-win32" thread model has no std::mutex before GCC 13
# ---------------------------------------------------------------------------
# Both mistakes produce the same confusing symptom - a C++ error deep inside a
# perfectly correct header ("'mutex' in namespace 'std' does not name a type" in
# src/config.h, which *does* #include <mutex>) - so they are diagnosed here,
# where the fix is a single command line.
if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
  execute_process(COMMAND ${CMAKE_CXX_COMPILER} -dumpmachine
                  OUTPUT_VARIABLE _tea_cxx_target
                  OUTPUT_STRIP_TRAILING_WHITESPACE
                  ERROR_QUIET)
  # `AND NOT WIN32`: on Windows the compiler targeting mingw *is* the native
  # build (scripts\build-mingw.bat / `cmake --preset mingw`), so only a
  # non-Windows host pointing at a mingw triplet without a toolchain file is the
  # mistake this guard is about.
  if(_tea_cxx_target MATCHES "mingw" AND NOT CMAKE_CROSSCOMPILING AND NOT WIN32)
    message(FATAL_ERROR
      "the C++ compiler (${CMAKE_CXX_COMPILER}) targets ${_tea_cxx_target}, but this "
      "build directory is configured for the host (CMAKE_SYSTEM_NAME=${CMAKE_SYSTEM_NAME}).\n"
      "Swapping the compiler alone does not make CMake cross-compile: WIN32 stays false, "
      "the build-time tools stay target binaries, and the result is a host build with a "
      "foreign compiler. Configure a separate directory with the toolchain file instead:\n"
      "  cmake --preset win64-mingw-cross        # -> build-win64\n"
      "  cmake --build --preset win64-mingw-cross")
  endif()

  if(_tea_cxx_target MATCHES "mingw")
    # MinGW-w64 is shipped in two flavours. With -win32 threading, libstdc++
    # leaves bits/std_mutex.h (and <thread>) empty until GCC 13, so std::mutex,
    # std::thread, std::condition_variable are simply undeclared - the headers
    # include fine and the error shows up at the first use.
    execute_process(COMMAND ${CMAKE_CXX_COMPILER} -v
                    OUTPUT_QUIET ERROR_VARIABLE _tea_cxx_v)
    if(_tea_cxx_v MATCHES "Thread model: win32")
      execute_process(COMMAND ${CMAKE_CXX_COMPILER} -dumpversion
                      OUTPUT_VARIABLE _tea_gcc_version
                      OUTPUT_STRIP_TRAILING_WHITESPACE)
      if(_tea_gcc_version VERSION_LESS 13)
        message(FATAL_ERROR
          "${CMAKE_CXX_COMPILER} is GCC ${_tea_gcc_version} with the \"-win32\" thread model. "
          "That libstdc++ has no std::mutex/std::thread/std::condition_variable (GCC 13 added "
          "them for win32 threads), so any C++ program with threads fails to compile with "
          "\"'mutex' in namespace 'std' does not name a type\" - including this one, which is "
          "multi-threaded end to end.\n"
          "Use the POSIX-thread variant instead:\n"
          "  apt install mingw-w64\n"
          "  cmake --preset win64-mingw-cross        # picks x86_64-w64-mingw32-g++-posix\n"
          "or point CMAKE_CXX_COMPILER/CMAKE_C_COMPILER at x86_64-w64-mingw32-{gcc,g++}-posix.")
      endif()
    endif()
  endif()
endif()

# ---------------------------------------------------------------------------
# MSVC: the /showIncludes prefix has to be ASCII, or nothing filters it
# ---------------------------------------------------------------------------
# cl.exe reports included files with a localized prefix (on a Chinese VS the
# English "Note: including file:" becomes a Chinese phrase). CMake passes that
# prefix to
#     cmake -E cmake_cl_compile_depends --filter-prefix=<prefix>
# inside the generated make rules; the command line does not survive the console
# code page, so nothing matches and **every included-file line is echoed into the
# build log** - hundreds of lines per translation unit - while the .obj.d files
# stay empty (editing a header then silently fails to trigger a rebuild).
#
# scripts\vcenv.bat (used by scripts\build.bat) sets VSLANG=1033, which makes
# cl.exe speak English and turns the prefix into the ASCII
# "Note: including file: ". This block keeps that working:
#   * a build directory configured while the compiler was still localized has the
#     old prefix cached in CMakeFiles/<ver>/CMakeCXXCompiler.cmake - override it,
#     so an existing directory is repaired instead of needing to be wiped;
#   * without VSLANG, say what to do rather than leaving hundreds of lines of
#     noise to puzzle over.
# It has to run here, right after project(): subdirectories added later (the
# vendored yaml-cpp) inherit the variable at the point they are added.
if(MSVC AND CMAKE_CXX_CL_SHOWINCLUDES_PREFIX MATCHES "[^ -~]")
  if("$ENV{VSLANG}" STREQUAL "1033")
    # The cached prefix was detected while cl.exe still spoke the local language.
    # cl.exe will print English now (VSLANG=1033), so make the generated filter
    # agree with it. Doing it here repairs an existing build directory instead of
    # forcing a from-scratch reconfigure.
    set(CMAKE_CXX_CL_SHOWINCLUDES_PREFIX "Note: including file: ")
    set(CMAKE_C_CL_SHOWINCLUDES_PREFIX "Note: including file: ")
    set(CMAKE_CL_SHOWINCLUDES_PREFIX "Note: including file: ")
    message(STATUS
      "tea_agent: this build directory cached a localized /showIncludes prefix; using the "
      "English one now (VSLANG=1033 is set).")
  else()
    message(STATUS
      "tea_agent: this Visual Studio prints /showIncludes in a localized form "
      "(\"${CMAKE_CXX_CL_SHOWINCLUDES_PREFIX}\"). CMake cannot filter those lines out of the "
      "build log, so it will contain one line per included header. VSLANG=1033 must be set "
      "both when configuring AND when building (cl.exe reads it, not CMake): use "
      "scripts\\build.bat or `cmake --preset msvc` / `cmake --build --preset msvc`.")
  endif()
endif()

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
# The code is written so that it compiles in C++14 mode as well (GCC 6.5 is the
# reference "old" toolchain). Configure with -DTEA_AGENT_CXX_STANDARD=14 to
# verify that claim.
set(TEA_AGENT_CXX_STANDARD "17" CACHE STRING "C++ standard to build with (14 or 17)")
set_property(CACHE TEA_AGENT_CXX_STANDARD PROPERTY STRINGS 14 17)
option(TEA_AGENT_USE_MBEDTLS "Use the bundled mbedTLS for https:// on non-Windows" ON)

# ---------------------------------------------------------------------------
# Global settings
# ---------------------------------------------------------------------------
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# C++17 is requested, but the code deliberately avoids C++17-only *language* and
# library features so that GCC 6.5 still works. Notably it does not use
# std::filesystem at all (see src/platform_fs.cpp for why).
set(CMAKE_CXX_STANDARD ${TEA_AGENT_CXX_STANDARD})
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

if(MSVC)
  add_compile_options(/utf-8 /MP /W3 /bigobj /wd4996)
  add_compile_definitions(_CRT_SECURE_NO_WARNINGS NOMINMAX WIN32_LEAN_AND_MEAN)
else()
  add_compile_options(-Wall -Wextra -Wno-unused-parameter)
endif()

if(WIN32)
  # MinGW defaults to a much older _WIN32_WINNT than MSVC, which makes
  # cpp-httplib refuse to build and hides CreateFile2(). Target Windows 10.
  add_compile_definitions(_WIN32_WINNT=0x0A00 WINVER=0x0A00 NTDDI_VERSION=0x0A000000)
endif()

# ---------------------------------------------------------------------------
# 3rd party dependencies -- everything lives under 3rd/
# ---------------------------------------------------------------------------
set(TEA_3RD ${CMAKE_CURRENT_SOURCE_DIR}/3rd)

# nlohmann/json + cpp-httplib are header only.
add_library(tea_3rd_headers INTERFACE)
target_include_directories(tea_3rd_headers INTERFACE
  ${TEA_3RD}/nlohmann_json
  ${TEA_3RD}/cpp-httplib
)
target_compile_definitions(tea_3rd_headers INTERFACE
  CPPHTTPLIB_THREAD_POOL_COUNT=32
  CPPHTTPLIB_NO_EXCEPTIONS=0
)

# miniz: deflate + the ZIP writer behind GET /api/workspaces/{id}/archive.
# Vendored as the upstream amalgamation (miniz.c + miniz.h, unmodified) and built
# as its own static library: it is C, it is not ours, and its warnings are not
# interesting, so they are turned off for this target only.
add_library(tea_miniz STATIC ${TEA_3RD}/miniz/miniz.c)
target_include_directories(tea_miniz PUBLIC ${TEA_3RD}/miniz)
set_target_properties(tea_miniz PROPERTIES POSITION_INDEPENDENT_CODE ON)
if(MSVC)
  target_compile_options(tea_miniz PRIVATE /W0)
else()
  target_compile_options(tea_miniz PRIVATE -w)
endif()

# yaml-cpp is a real library; build it from source inside 3rd/.
set(YAML_CPP_BUILD_TESTS   OFF CACHE BOOL "" FORCE)
set(YAML_CPP_BUILD_TOOLS   OFF CACHE BOOL "" FORCE)
set(YAML_CPP_BUILD_CONTRIB OFF CACHE BOOL "" FORCE)
set(YAML_CPP_INSTALL       OFF CACHE BOOL "" FORCE)
set(YAML_CPP_FORMAT_SOURCE OFF CACHE BOOL "" FORCE)
# yaml-cpp still declares `cmake_minimum_required(VERSION 3.4)`, which CMake 4.x
# rejects. This escape hatch keeps it working on both CMake 3 and CMake 4.
set(CMAKE_POLICY_VERSION_MINIMUM 3.5)
set(_tea_saved_cxx_standard ${CMAKE_CXX_STANDARD})
set(_tea_saved_cxx_required ${CMAKE_CXX_STANDARD_REQUIRED})
add_subdirectory(${TEA_3RD}/yaml-cpp ${CMAKE_BINARY_DIR}/yaml-cpp EXCLUDE_FROM_ALL)
set(CMAKE_CXX_STANDARD ${_tea_saved_cxx_standard})
set(CMAKE_CXX_STANDARD_REQUIRED ${_tea_saved_cxx_required})

# yaml-cpp 0.8.0 predates the libstdc++ transitive-include cleanup of GCC 13 and
# expects <cstdint> to be pulled in by <string>. Force the include instead of
# patching the vendored sources, so 3rd/ stays byte-identical to upstream.
if(TARGET yaml-cpp)
  if(MSVC)
    target_compile_options(yaml-cpp PRIVATE /FIcstdint)
  else()
    target_compile_options(yaml-cpp PRIVATE -include cstdint)
  endif()
endif()

# mbedTLS provides the https:// backend for platforms without WinHTTP.
# It is optional so that a Windows-only build never needs it.
set(TEA_HAVE_MBEDTLS OFF)
if(TEA_AGENT_USE_MBEDTLS AND EXISTS ${TEA_3RD}/mbedtls/CMakeLists.txt)
  set(ENABLE_TESTING              OFF CACHE BOOL "" FORCE)
  set(ENABLE_PROGRAMS             OFF CACHE BOOL "" FORCE)
  set(USE_SHARED_MBEDTLS_LIBRARY  OFF CACHE BOOL "" FORCE)
  set(USE_STATIC_MBEDTLS_LIBRARY  ON  CACHE BOOL "" FORCE)
  set(MBEDTLS_FATAL_WARNINGS      OFF CACHE BOOL "" FORCE)
  # mbedTLS links pthread on POSIX; on Windows it uses the native threading API.
  # Force the flag in both directions so a stale cache cannot leak through.
  if(WIN32)
    set(LINK_WITH_PTHREAD OFF CACHE BOOL "" FORCE)
  else()
    set(LINK_WITH_PTHREAD ON CACHE BOOL "" FORCE)
  endif()
  # mbedTLS 2.28 is C99; recent GCC defaults to C23, which is stricter about
  # implicit declarations and K&R style definitions.
  set(CMAKE_C_STANDARD 99)
  set(CMAKE_C_EXTENSIONS ON)
  set(_tea_saved_cxx_standard ${CMAKE_CXX_STANDARD})
  set(_tea_saved_cxx_required ${CMAKE_CXX_STANDARD_REQUIRED})
  add_subdirectory(${TEA_3RD}/mbedtls ${CMAKE_BINARY_DIR}/mbedtls EXCLUDE_FROM_ALL)
  set(CMAKE_CXX_STANDARD ${_tea_saved_cxx_standard})
  set(CMAKE_CXX_STANDARD_REQUIRED ${_tea_saved_cxx_required})
  set(TEA_HAVE_MBEDTLS ON)
  message(STATUS "tea_agent: mbedTLS found in 3rd/ -> https:// support on all platforms")
else()
  message(STATUS "tea_agent: mbedTLS not used (https:// handled by WinHTTP on Windows)")
endif()

add_library(tea_3rd INTERFACE)
target_link_libraries(tea_3rd INTERFACE tea_3rd_headers yaml-cpp::yaml-cpp tea_miniz)
target_compile_definitions(tea_3rd INTERFACE YAML_CPP_STATIC_DEFINE)
if(TEA_HAVE_MBEDTLS)
  target_link_libraries(tea_3rd INTERFACE mbedtls mbedx509 mbedcrypto)
  target_compile_definitions(tea_3rd INTERFACE TEA_HAVE_MBEDTLS=1)
endif()

# ---------------------------------------------------------------------------
# Build-time embedders: turn web/* and 3rd/ca-bundle/cacert.pem into C++
# translation units so that the produced binary is fully self-contained.
# ---------------------------------------------------------------------------
# Both embedders are *build-time* helpers: they must run on the build host, not
# on the target. When cross-compiling (ARM embedded boards and the like),
# building them with the target toolchain would produce binaries the host
# cannot execute, so they are built as a nested project with the host toolchain
# instead (tools/host_embed builds BOTH tea_embed_assets and tea_embed_ca_bundle).
#
# TEA_AGENT_USE_HOST_ASSET_TOOL defaults to CMAKE_CROSSCOMPILING; set it to ON
# for a native build as well if you want to exercise that path.
if(NOT DEFINED TEA_AGENT_USE_HOST_ASSET_TOOL)
  set(TEA_AGENT_USE_HOST_ASSET_TOOL ${CMAKE_CROSSCOMPILING})
endif()

set(TEA_WEB_DIR ${CMAKE_CURRENT_SOURCE_DIR}/web)
set(TEA_GEN_DIR ${CMAKE_BINARY_DIR}/generated)
file(MAKE_DIRECTORY ${TEA_GEN_DIR})

set(TEA_WEB_RELPATHS index.html tokens.css app.css app.js)
set(TEA_WEB_FILES "")
foreach(_rel ${TEA_WEB_RELPATHS})
  list(APPEND TEA_WEB_FILES ${TEA_WEB_DIR}/${_rel})
endforeach()

if(TEA_AGENT_USE_HOST_ASSET_TOOL)
  message(STATUS "tea_agent: building the embedders with the host toolchain")
  set(TEA_HOST_TOOLS_DIR ${CMAKE_BINARY_DIR}/host-tools)
  # Suffix of an executable on the *host*, not on the target: the nested project
  # is compiled with the host toolchain, so cross-compiling to Windows from
  # Linux must expect no ".exe" (CMAKE_EXECUTABLE_SUFFIX is the target's).
  if(CMAKE_HOST_WIN32)
    set(TEA_HOST_EXE_SUFFIX .exe)
  else()
    set(TEA_HOST_EXE_SUFFIX "")
  endif()
  set(TEA_EMBED_EXE
      ${TEA_HOST_TOOLS_DIR}/bin/tea_embed_assets${TEA_HOST_EXE_SUFFIX})
  set(TEA_CA_BUNDLE_EXE
      ${TEA_HOST_TOOLS_DIR}/bin/tea_embed_ca_bundle${TEA_HOST_EXE_SUFFIX})
  # One nested configure+build produces both helpers; listing both as outputs
  # keeps the dependency on the generated ca_bundle.cpp honest.
  add_custom_command(
    OUTPUT ${TEA_EMBED_EXE} ${TEA_CA_BUNDLE_EXE}
    COMMAND ${CMAKE_COMMAND}
            -S ${CMAKE_CURRENT_SOURCE_DIR}/tools/host_embed
            -B ${TEA_HOST_TOOLS_DIR}
            -DCMAKE_BUILD_TYPE=Release
    COMMAND ${CMAKE_COMMAND} --build ${TEA_HOST_TOOLS_DIR} --config Release
    DEPENDS tools/host_embed/CMakeLists.txt
            tools/embed_assets.cpp tools/embed_ca_bundle.cpp
    COMMENT "Building host embed tools"
    VERBATIM
  )
  add_custom_target(tea_embed_assets_host
                    DEPENDS ${TEA_EMBED_EXE} ${TEA_CA_BUNDLE_EXE})
  set(TEA_EMBED_DEP tea_embed_assets_host)
  set(TEA_CA_BUNDLE_DEP tea_embed_assets_host)
else()
  add_executable(tea_embed_assets tools/embed_assets.cpp)
  set_target_properties(tea_embed_assets PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
  set(TEA_EMBED_EXE $<TARGET_FILE:tea_embed_assets>)
  set(TEA_EMBED_DEP tea_embed_assets)
  set(TEA_CA_BUNDLE_EXE $<TARGET_FILE:tea_embed_ca_bundle>)
  set(TEA_CA_BUNDLE_DEP tea_embed_ca_bundle)
endif()

# ---------------------------------------------------------------------------
# CA bundle embedder: compiles 3rd/ca-bundle/cacert.pem into the binary so a
# single-file deployment has working https:// without a sidecar PEM.
# ---------------------------------------------------------------------------
# In the host-toolchain case (cross-compiling) tea_embed_ca_bundle already
# exists as a host executable built above; only the native case defines it here.
if(NOT TEA_AGENT_USE_HOST_ASSET_TOOL)
  add_executable(tea_embed_ca_bundle tools/embed_ca_bundle.cpp)
  set_target_properties(tea_embed_ca_bundle PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
endif()

set(TEA_CA_BUNDLE_PEM ${TEA_3RD}/ca-bundle/cacert.pem)
set(TEA_CA_BUNDLE_CPP ${TEA_GEN_DIR}/ca_bundle.cpp)
# A missing PEM is not fatal: the generator emits an empty bundle and the
# runtime falls back to a file. Keep the dependency list honest either way.
if(EXISTS ${TEA_CA_BUNDLE_PEM})
  set(TEA_CA_BUNDLE_DEPS ${TEA_CA_BUNDLE_PEM})
else()
  set(TEA_CA_BUNDLE_DEPS "")
  message(STATUS "tea_agent: no ${TEA_CA_BUNDLE_PEM}; the binary will look for a PEM at runtime")
endif()
# The generator must be named by path, not by target: in the cross-compile case
# it is a host executable that only exists as a file, and `COMMAND <target>`
# would resolve to the (unrunnable) target-architecture binary.
add_custom_command(
  OUTPUT ${TEA_CA_BUNDLE_CPP}
  COMMAND ${TEA_CA_BUNDLE_EXE} ${TEA_GEN_DIR} ${TEA_CA_BUNDLE_PEM}
  DEPENDS ${TEA_CA_BUNDLE_DEP} ${TEA_CA_BUNDLE_DEPS}
  COMMENT "Embedding the CA bundle into ca_bundle.cpp"
  VERBATIM
)

set(TEA_WEB_ASSETS_CPP ${TEA_GEN_DIR}/web_assets.cpp)
add_custom_command(
  OUTPUT ${TEA_WEB_ASSETS_CPP} ${TEA_GEN_DIR}/web_assets.h
  COMMAND ${TEA_EMBED_EXE} ${TEA_GEN_DIR} ${TEA_WEB_DIR} ${TEA_WEB_RELPATHS}
  DEPENDS ${TEA_EMBED_DEP} ${TEA_WEB_FILES}
  COMMENT "Embedding web assets into web_assets.cpp"
  VERBATIM
)

# ---------------------------------------------------------------------------
# tea_agent
# ---------------------------------------------------------------------------
# The build information (which commit this came from): build metadata, written
# next to the binary as tea_agent.buildinfo. It used to be written back into
# src/version.cpp, which left the working tree dirty after every build and made
# two build trees fight over one file. Now the binary itself carries no
# build-specific data at all, so it stays byte-for-byte reproducible.
set(TEA_BUILD_INFO ${TEA_GEN_DIR}/tea_agent.buildinfo)
file(MAKE_DIRECTORY ${TEA_GEN_DIR})
find_package(Git QUIET)
if(GIT_FOUND)
  execute_process(
    COMMAND ${CMAKE_COMMAND}
            -DREPO_DIR=${CMAKE_CURRENT_SOURCE_DIR}
            -DOUTPUT=${TEA_BUILD_INFO}
            -DTEA_AGENT_VERSION=${TEA_AGENT_VERSION}
            -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/version_git_hash.cmake
    RESULT_VARIABLE _tea_git_sync_rc
    OUTPUT_VARIABLE _tea_git_sync_out
    ERROR_VARIABLE _tea_git_sync_err)
  if(NOT _tea_git_sync_out STREQUAL "")
    string(STRIP "${_tea_git_sync_out}" _tea_git_sync_out)
    # The script prints with message(STATUS ...), which already prefixes "-- ".
    string(REGEX REPLACE "^--[ 	]+" "" _tea_git_sync_out "${_tea_git_sync_out}")
    message(STATUS "${_tea_git_sync_out}")
  endif()
  if(NOT _tea_git_sync_rc EQUAL 0)
    message(WARNING "could not generate the git hash: ${_tea_git_sync_err}")
  endif()

  # Watch the refs: HEAD plus the branch it points at (or packed-refs, when the
  # branch is packed). A commit touches one of them, which re-runs CMake - and
  # therefore regenerates the hash - before the next build compiles anything.
  execute_process(
    COMMAND ${GIT_EXECUTABLE} rev-parse --absolute-git-dir
    WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
    OUTPUT_VARIABLE _tea_git_dir
    ERROR_QUIET
    OUTPUT_STRIP_TRAILING_WHITESPACE)
  if(_tea_git_dir)
    set(_tea_git_watch "${_tea_git_dir}/HEAD")
    execute_process(
      COMMAND ${GIT_EXECUTABLE} symbolic-ref HEAD
      WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
      OUTPUT_VARIABLE _tea_git_ref
      ERROR_QUIET
      OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(_tea_git_ref AND EXISTS "${_tea_git_dir}/${_tea_git_ref}")
      list(APPEND _tea_git_watch "${_tea_git_dir}/${_tea_git_ref}")
    elseif(EXISTS "${_tea_git_dir}/packed-refs")
      list(APPEND _tea_git_watch "${_tea_git_dir}/packed-refs")
    endif()
    foreach(_f IN LISTS _tea_git_watch)
      if(EXISTS "${_f}")
        set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${_f}")
      endif()
    endforeach()
  endif()
else()
  # No git (a release tarball): still produce the file, with "unknown".
  execute_process(
    COMMAND ${CMAKE_COMMAND}
            -DREPO_DIR=${CMAKE_CURRENT_SOURCE_DIR}
            -DOUTPUT=${TEA_BUILD_INFO}
            -DTEA_AGENT_VERSION=${TEA_AGENT_VERSION}
            -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/version_git_hash.cmake
    RESULT_VARIABLE _tea_git_sync_rc
    OUTPUT_VARIABLE _tea_git_sync_out
    ERROR_VARIABLE _tea_git_sync_err)
endif()

set(TEA_SOURCES
  src/main.cpp
  src/util.cpp
  src/platform_fs.cpp
  src/env.cpp
  src/output_filter.cpp
  src/log.cpp
  src/config.cpp
  src/usage.cpp
  src/message.cpp
  src/http.cpp
  src/http_socket.cpp
  src/tls.cpp
  src/command.cpp
  src/archive.cpp
  src/tools.cpp
  src/workspace.cpp
  src/llm.cpp
  src/trajectory.cpp
  src/agent.cpp
  src/html_export.cpp
  src/model_discovery.cpp
  src/provider_yaml.cpp
  src/server.cpp
  src/shell.cpp
  src/version.cpp
  ${TEA_WEB_ASSETS_CPP}
  ${TEA_CA_BUNDLE_CPP}
)

if(WIN32)
  list(APPEND TEA_SOURCES src/http_winhttp.cpp)
  # The interactive shell runs on a ConPTY (a pseudo console) here.
  list(APPEND TEA_SOURCES src/shell_win32.cpp)
else()
  # ... and on a pty everywhere else.
  list(APPEND TEA_SOURCES src/shell_posix.cpp)
endif()

# Header dependencies are declared explicitly instead of relying on the
# compiler-generated ones (see the /showIncludes note at the top of this file).
# With ~10 TUs it only costs a full recompile of our sources when a header
# actually changes, and it keeps incremental builds correct even on a machine
# whose localized /showIncludes output CMake cannot parse.
if(MSVC)
  file(GLOB_RECURSE TEA_PROJECT_HEADERS CONFIGURE_DEPENDS
       ${CMAKE_CURRENT_SOURCE_DIR}/src/*.h)
  set_property(SOURCE ${TEA_SOURCES} APPEND PROPERTY OBJECT_DEPENDS ${TEA_PROJECT_HEADERS})
endif()

add_executable(tea_agent ${TEA_SOURCES})

# NOTE: src/ is deliberately NOT on the include path. It would make the
# compiler resolve system headers such as <process.h> (pulled in by <thread>)
# to our own src/process.h. All intra-project includes are quoted and resolve
# relative to the including file, which is exactly what we want.
# src/ is needed by the generated translation units (ca_bundle.cpp includes
# ca_bundle.h); the hand-written sources find their headers next to themselves.
target_include_directories(tea_agent PRIVATE ${TEA_GEN_DIR} ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(tea_agent PRIVATE tea_3rd)

if(WIN32)
  # winhttp: https via schannel; ws2_32: sockets; advapi32: mbedTLS entropy
  target_link_libraries(tea_agent PRIVATE winhttp ws2_32 advapi32)
endif()

if(MINGW)
  # Produce a self-contained .exe: without this the binary needs
  # libstdc++-6.dll / libgcc_s_seh-1.dll / libwinpthread-1.dll on PATH.
  target_link_options(tea_agent PRIVATE -static-libgcc -static-libstdc++ -static)
  # Reproducible builds: by default the linker stamps the build time into the PE
  # header, so two builds of the same commit differ. Nothing build-specific is
  # compiled in any more (the commit lives in tea_agent.buildinfo next to the
  # binary), so with this flag two builds produce the very same bytes.
  target_link_options(tea_agent PRIVATE -Wl,--no-insert-timestamp)
elseif(MSVC)
  # The same for MSVC: /BREPRO makes the PE timestamp and the recorded PDB path
  # deterministic, so the binary of a given commit is byte-for-byte stable.
  target_link_options(tea_agent PRIVATE /Brepro)
endif()

find_package(Threads REQUIRED)
target_link_libraries(tea_agent PRIVATE Threads::Threads)

# --- self-contained runtime -------------------------------------------------
# Single-file deployment: the C++ runtime is linked statically so the binary does
# not need libstdc++.so.6 / libgcc_s.so.1 on the target device. MinGW already
# gets -static-libgcc -static-libstdc++ -static above (a MinGW build then needs
# only Windows' own DLLs: KERNEL32/ADVAPI32/SHELL32/WINHTTP/WS2_32 plus the UCRT).
#
# On macOS a static libc++ does not exist, so the option only applies to ELF
# targets built with GCC or Clang.
option(TEA_AGENT_STATIC_RUNTIME
       "Link the C++ runtime statically so the binary is self-contained" OFF)
# Fully static (also libc). On by default: the binary is meant to be copied to
# devices that may run an older glibc than the build host, and a dynamically
# linked binary then dies with "version `GLIBC_2.xx' not found" before it can
# print anything useful.
#
# The cost is glibc's NSS: a static binary cannot dlopen libnss_*.so modules,
# and on hosts whose /etc/nsswitch.conf lists systemd-resolved (`resolve`) or
# mDNS, getaddrinfo segfaults. http_socket.cpp therefore pins the hosts
# database to "files dns" (the two implementations compiled into libc) when
# TEA_STATIC_LIBC is defined, so DNS keeps working and mDNS/systemd-resolved's
# NSS front end is bypassed. musl has no NSS modules at all and needs none of
# this. Set to OFF for a dynamic binary when the target matches the build host.
option(TEA_AGENT_STATIC_LIBC "Link libc statically so the binary runs on older systems" OFF)

if(TEA_AGENT_STATIC_RUNTIME AND NOT WIN32 AND NOT APPLE AND
   CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  target_link_options(tea_agent PRIVATE -static-libstdc++ -static-libgcc)
  if(TEA_AGENT_STATIC_LIBC)
    # -no-pie: a fully static executable cannot be a PIE on glibc; the default
    # toolchain here produces PIE, and `-static` alone then fails to link.
    target_link_options(tea_agent PRIVATE -static -no-pie)
    target_compile_options(tea_agent PRIVATE -fno-pie)
    # Tells http_socket.cpp to keep NSS to the built-in "files dns" lookups;
    # without it a static binary crashes inside libnss_resolve.so on
    # systemd-resolved hosts.
    target_compile_definitions(tea_agent PRIVATE TEA_STATIC_LIBC=1)
    message(STATUS "tea_agent: fully static binary (-static -no-pie)")
  endif()
endif()

# Put the binary in a predictable place and copy the web sources next to it so
# that --web-dir can be used for live UI editing.
set_target_properties(tea_agent PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin
)

# --- rebuild while the previous build is still running -----------------------
# Windows will not let the linker overwrite a running .exe (LNK1104 /
# "Permission denied"), which would turn every "edit, build, look at the UI"
# cycle into "stop the agent, build, start the agent". The image only protects its
# *contents*, not its name, so PRE_LINK moves the old binary aside and POST_BUILD
# deletes the copy once the link has succeeded; see cmake/free-running-exe.cmake.
#
# PRE_LINK only runs when the target really links, so a no-op build does not
# rename anything. On POSIX the whole step is a no-op: a running image can be
# overwritten there.
if(WIN32)
  foreach(_tea_exe_target tea_agent tea_embed_assets tea_embed_ca_bundle)
    if(TARGET ${_tea_exe_target})
      add_custom_command(TARGET ${_tea_exe_target} PRE_LINK
        COMMAND ${CMAKE_COMMAND}
                -DEXE=$<TARGET_FILE:${_tea_exe_target}>
                -DMODE=prepare
                -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/free-running-exe.cmake
        COMMENT "Freeing ${_tea_exe_target}.exe for relinking"
        VERBATIM)
      add_custom_command(TARGET ${_tea_exe_target} POST_BUILD
        COMMAND ${CMAKE_COMMAND}
                -DEXE=$<TARGET_FILE:${_tea_exe_target}>
                -DMODE=clean
                -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/free-running-exe.cmake
        VERBATIM)
    endif()
  endforeach()
endif()

add_custom_command(TARGET tea_agent POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E copy_directory ${TEA_WEB_DIR} ${CMAKE_BINARY_DIR}/bin/web
)

# The build information travels with the binary: the file is written at
# configure time (a new commit re-runs CMake) and copied next to the executable
# on every build, so it is never stale. The binary reads it at startup.
#
# The copy is its own always-run target, not a POST_BUILD step of the link:
# a commit that touches no source leaves the binary untouched (no relink, so
# no POST_BUILD), and the file next to the executable would then still name the
# previous commit - /api/health would report a commit this binary is not from.
add_custom_target(tea_build_info ALL
  COMMAND ${CMAKE_COMMAND} -E make_directory $<TARGET_FILE_DIR:tea_agent>
  COMMAND ${CMAKE_COMMAND} -E copy_if_different
          ${TEA_BUILD_INFO} $<TARGET_FILE_DIR:tea_agent>/tea_agent.buildinfo
  COMMENT "build info next to the binary"
  VERBATIM
)

# Ship the CA bundle next to the binary so the mbedTLS backend works without
# any further configuration.
if(EXISTS ${TEA_3RD}/ca-bundle/cacert.pem)
  add_custom_command(TARGET tea_agent POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_BINARY_DIR}/bin/ca-bundle
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
            ${TEA_3RD}/ca-bundle/cacert.pem ${CMAKE_BINARY_DIR}/bin/ca-bundle/cacert.pem
  )
endif()

message(STATUS "tea_agent ${PROJECT_VERSION} (from src/version.cpp): compiler=${CMAKE_CXX_COMPILER_ID} std=${CMAKE_CXX_STANDARD}")
