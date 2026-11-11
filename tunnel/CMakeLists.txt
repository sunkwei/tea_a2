# The tunnel binaries: a2_broker (the public rendezvous) and a2_tunnel (the
# sidecar that sits next to an a2).
#
# This is a **separate CMake project** on purpose:
#
#   libdatachannel needs mbedTLS 3.x, while the agent itself uses the
#   vendored mbedTLS 2.28 LTS -- and both define targets called "mbedtls",
#   "mbedx509" and "mbedcrypto". In one CMake project the second
#   add_subdirectory() would fail with "add_library cannot create target
#   mbedtls because another target with the same name already exists".
#   The two TLS stacks end up in different executables anyway, so keeping the
#   projects apart costs nothing and leaves the agent's build untouched.
#
# It is built by the top-level CMakeLists (like tools/host_embed), so
#   cmake --build build
# still produces build/bin/a2_broker and build/bin/a2_tunnel, with the
# **target** toolchain (the Windows cross build works the same way).
#
cmake_minimum_required(VERSION 3.20)
project(tea_tunnel LANGUAGES C CXX)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

set(TEA_ROOT ${CMAKE_CURRENT_SOURCE_DIR}/..)
set(TEA_3RD ${TEA_ROOT}/3rd)

option(TEA_TUNNEL_STATIC_RUNTIME "Link the C++ runtime statically" ON)
option(TEA_TUNNEL_STATIC_LIBC "Link libc statically (a self-contained file)" ON)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED ON)

add_compile_options(-O2)

find_package(Threads REQUIRED)

# --- mbedTLS (the same tree the agent uses) ---------------------------------
# The agent and the tunnel both need mbedTLS 3.x now, and they live in two
# separate CMake projects (one project cannot hold two trees that define the
# same targets), so both point at 3rd/mbedtls.
set(ENABLE_PROGRAMS OFF CACHE BOOL "" FORCE)
set(ENABLE_TESTING OFF CACHE BOOL "" FORCE)
set(USE_SHARED_MBEDTLS_LIBRARY OFF CACHE BOOL "" FORCE)
set(USE_STATIC_MBEDTLS_LIBRARY ON CACHE BOOL "" FORCE)
set(MBEDTLS_FATAL_WARNINGS OFF CACHE BOOL "" FORCE)
set(LINK_WITH_PTHREAD ON CACHE BOOL "" FORCE)
add_subdirectory(${TEA_3RD}/mbedtls ${CMAKE_BINARY_DIR}/mbedtls EXCLUDE_FROM_ALL)

# libdatachannel does `if(NOT TARGET MbedTLS::MbedTLS) find_package(MbedTLS 3)`,
# so providing the target is enough - no patch to the vendored tree and no
# installed copy of mbedTLS anywhere.
if(NOT TARGET MbedTLS::MbedTLS)
  add_library(MbedTLS::MbedTLS INTERFACE IMPORTED)
  target_link_libraries(MbedTLS::MbedTLS INTERFACE mbedtls mbedx509 mbedcrypto)
endif()

# --- libdatachannel (the WebRTC data channel) --------------------------------
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(USE_MBEDTLS ON CACHE BOOL "" FORCE)
set(USE_NETTLE OFF CACHE BOOL "" FORCE)
set(NO_MEDIA ON CACHE BOOL "" FORCE)
set(NO_WEBSOCKET ON CACHE BOOL "" FORCE)
set(NO_EXAMPLES ON CACHE BOOL "" FORCE)
set(NO_TESTS ON CACHE BOOL "" FORCE)
set(WARNINGS_AS_ERRORS OFF CACHE BOOL "" FORCE)
add_subdirectory(${TEA_3RD}/libdatachannel ${CMAKE_BINARY_DIR}/libdatachannel EXCLUDE_FROM_ALL)

# --- SQLite (the TURN account book) ------------------------------------------
# The official amalgamation: one .c, one .h. Built as a static library so the
# broker stays a single file that needs nothing installed on the machine.
#
# The switches: no extension loading (an attack surface we do not need), no
# deprecated API, no double-quoted strings as identifiers, and thread safe
# (the broker's signalling and its TURN server are separate threads).
add_library(tea_sqlite3 STATIC ${TEA_3RD}/sqlite3/sqlite3.c)
target_include_directories(tea_sqlite3 PUBLIC ${TEA_3RD}/sqlite3)
# The account book only needs "open a file, create a table, read a table",
# so everything else is left out: the tunnel binaries go onto devices with
# little free space and every kilobyte counts.
target_compile_definitions(tea_sqlite3 PRIVATE
  SQLITE_OMIT_LOAD_EXTENSION=1
  SQLITE_OMIT_DEPRECATED=1
  SQLITE_DQS=0
  SQLITE_THREADSAFE=1
  SQLITE_DEFAULT_MEMSTATUS=0
  SQLITE_OMIT_SHARED_CACHE=1
  SQLITE_OMIT_TRACE=1
  SQLITE_OMIT_PROGRESS_CALLBACK=1
  SQLITE_OMIT_TCL_VARIABLE=1
  SQLITE_OMIT_INCRBLOB=1)
target_link_libraries(tea_sqlite3 PUBLIC Threads::Threads)
if(NOT WIN32 AND NOT APPLE AND CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
  # The C file is compiled by the C compiler, so the static-libc flags have to
  # be repeated here (the loop at the bottom only sets them on the binaries).
  target_compile_options(tea_sqlite3 PRIVATE -fno-pie)
endif()

# --- the two binaries --------------------------------------------------------
add_executable(a2_broker ${TEA_ROOT}/src/tunnel_broker.cpp)
if(WIN32)
  # MinGW defaults to a much older _WIN32_WINNT than MSVC, which makes
  # cpp-httplib refuse to build and hides CreateFile2(). Target Windows 10.
  # The agent's own build says the same in its top-level CMakeLists - the
  # tunnel is a separate project, so it has to say it again.
  #
  # Only on the two binaries: the vendored C libraries (usrsctp, libjuice,
  # sqlite3, mbedTLS) are written for the Windows headers as they are, and
  # NOMINMAX would take the min/max macros away from usrsctp.
  target_compile_definitions(a2_broker PRIVATE
    _WIN32_WINNT=0x0A00 WINVER=0x0A00 NTDDI_VERSION=0x0A000000
    WIN32_LEAN_AND_MEAN NOMINMAX)
endif()
target_include_directories(a2_broker PRIVATE ${TEA_3RD}/nlohmann_json ${TEA_3RD}/libdatachannel/deps/libjuice/include)
target_link_libraries(a2_broker PRIVATE juice Threads::Threads)
# libjuice's STUN/TURN server needs a crypto backend for the message
# integrity: mbedTLS is already in the tree, so use it instead of OpenSSL
# (which would be a new system dependency on Windows).
target_link_libraries(a2_broker PRIVATE mbedtls mbedx509 mbedcrypto)
# The TURN accounts can be kept in a sqlite file that a person edits by hand.
target_link_libraries(a2_broker PRIVATE tea_sqlite3)

# --- the terminal the sidecar can serve -------------------------------------
# The sidecar can hand out a real shell on the machine it runs on (see
# src/tunnel_term.cpp): the browser on the other side of the tunnel talks to
# it through the very same tunnel, so no new transport is needed.
#
# That needs three things the sidecar did not have yet:
#   * cpp-httplib - the HTTP server in front of the shell;
#   * the shell code itself (src/shell*.cpp) and the small helpers it needs
#     (src/util.cpp, src/platform_fs.cpp, src/log.cpp) - the same code the
#     agent uses, so there is only one shell implementation in the project;
#   * the terminal page, compiled into the binary (there is no file to read
#     on a device): TEA_TUNNEL_ASSETS_DIR points at the generated table.
set(TEA_TUNNEL_ASSETS_DIR "" CACHE PATH "The directory with the generated web assets")

add_executable(a2_tunnel
  ${TEA_ROOT}/src/tunnel_sidecar.cpp
  ${TEA_ROOT}/src/tunnel_term.cpp
  ${TEA_ROOT}/src/shell.cpp
  ${TEA_ROOT}/src/util.cpp
  ${TEA_ROOT}/src/platform_fs.cpp
  ${TEA_ROOT}/src/log.cpp)
if(WIN32)
  # The shell runs on a ConPTY (a pseudo console) here ...
  target_sources(a2_tunnel PRIVATE ${TEA_ROOT}/src/shell_win32.cpp)
else()
  # ... and on a pty everywhere else.
  target_sources(a2_tunnel PRIVATE ${TEA_ROOT}/src/shell_posix.cpp)
endif()
if(TEA_TUNNEL_ASSETS_DIR)
  target_sources(a2_tunnel PRIVATE ${TEA_TUNNEL_ASSETS_DIR}/web_assets.cpp)
  target_include_directories(a2_tunnel PRIVATE ${TEA_TUNNEL_ASSETS_DIR})
endif()
if(WIN32)
  # Windows 10, like the broker above and like the agent's own build.
  target_compile_definitions(a2_tunnel PRIVATE
    _WIN32_WINNT=0x0A00 WINVER=0x0A00 NTDDI_VERSION=0x0A000000
    WIN32_LEAN_AND_MEAN NOMINMAX)
endif()
target_include_directories(a2_tunnel PRIVATE
  ${TEA_3RD}/nlohmann_json
  ${TEA_3RD}/cpp-httplib)
# The HTTP server runs its own threads; a small pool is enough (one browser
# at a time is the normal case).
target_compile_definitions(a2_tunnel PRIVATE CPPHTTPLIB_THREAD_POOL_COUNT=8)
target_link_libraries(a2_tunnel PRIVATE datachannel-static MbedTLS::MbedTLS Threads::Threads)
# The sidecar reads the same account book: it has to use one of the accounts
# the broker accepts.
target_link_libraries(a2_tunnel PRIVATE tea_sqlite3)

foreach(_bin a2_broker a2_tunnel)
  set_target_properties(${_bin} PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
  if(MINGW)
    # A self-contained .exe: without this the binary needs
    # libstdc++-6.dll / libgcc_s_seh-1.dll / libwinpthread-1.dll on PATH.
    target_link_options(${_bin} PRIVATE -static-libgcc -static-libstdc++ -static)
    target_link_libraries(${_bin} PRIVATE ws2_32 advapi32 bcrypt)
  endif()
  if(NOT WIN32 AND NOT APPLE AND CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    if(TEA_TUNNEL_STATIC_RUNTIME)
      target_link_options(${_bin} PRIVATE -static-libstdc++ -static-libgcc)
    endif()
    if(TEA_TUNNEL_STATIC_LIBC)
      # A fully static executable cannot be a PIE on glibc; -static alone then
      # fails to link.
      target_link_options(${_bin} PRIVATE -static -no-pie)
      target_compile_options(${_bin} PRIVATE -fno-pie)
    endif()
  endif()
endforeach()
