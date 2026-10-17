cmake_minimum_required(VERSION 3.20)
project(tea_agent VERSION 0.1.0 LANGUAGES CXX)

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
target_link_libraries(tea_3rd INTERFACE tea_3rd_headers yaml-cpp::yaml-cpp)
target_compile_definitions(tea_3rd INTERFACE YAML_CPP_STATIC_DEFINE)
if(TEA_HAVE_MBEDTLS)
  target_link_libraries(tea_3rd INTERFACE mbedtls mbedx509 mbedcrypto)
  target_compile_definitions(tea_3rd INTERFACE TEA_HAVE_MBEDTLS=1)
endif()

# ---------------------------------------------------------------------------
# Web asset embedder: turns web/* into a C++ translation unit so that the
# produced binary is fully self-contained.
# ---------------------------------------------------------------------------
# The asset embedder is a *build-time* helper: it must run on the build host,
# not on the target. When cross-compiling (ARM embedded boards and the like),
# building it with the target toolchain would produce a binary the host cannot
# execute, so it is built as a nested project with the host toolchain instead.
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
  message(STATUS "tea_agent: building the asset embedder with the host toolchain")
  set(TEA_HOST_TOOLS_DIR ${CMAKE_BINARY_DIR}/host-tools)
  set(TEA_EMBED_EXE
      ${TEA_HOST_TOOLS_DIR}/bin/tea_embed_assets${CMAKE_EXECUTABLE_SUFFIX})
  add_custom_command(
    OUTPUT ${TEA_EMBED_EXE}
    COMMAND ${CMAKE_COMMAND}
            -S ${CMAKE_CURRENT_SOURCE_DIR}/tools/host_embed
            -B ${TEA_HOST_TOOLS_DIR}
            -DCMAKE_BUILD_TYPE=Release
    COMMAND ${CMAKE_COMMAND} --build ${TEA_HOST_TOOLS_DIR} --config Release
    COMMENT "Building host asset embedder"
    VERBATIM
  )
  add_custom_target(tea_embed_assets_host DEPENDS ${TEA_EMBED_EXE})
  set(TEA_EMBED_DEP tea_embed_assets_host)
else()
  add_executable(tea_embed_assets tools/embed_assets.cpp)
  set_target_properties(tea_embed_assets PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
  set(TEA_EMBED_EXE $<TARGET_FILE:tea_embed_assets>)
  set(TEA_EMBED_DEP tea_embed_assets)
endif()

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
set(TEA_SOURCES
  src/main.cpp
  src/util.cpp
  src/platform_fs.cpp
  src/env.cpp
  src/output_filter.cpp
  src/log.cpp
  src/config.cpp
  src/message.cpp
  src/http.cpp
  src/http_socket.cpp
  src/tls.cpp
  src/command.cpp
  src/tools.cpp
  src/workspace.cpp
  src/llm.cpp
  src/trajectory.cpp
  src/agent.cpp
  src/server.cpp
  ${TEA_WEB_ASSETS_CPP}
)

if(WIN32)
  list(APPEND TEA_SOURCES src/http_winhttp.cpp)
endif()

# Localized Visual Studio emits a non-ASCII `/showIncludes` prefix (for a
# Chinese VS: "注意: 包含文件:"). CMake stores that prefix in the generated
# makefile, where it does not survive the encoding, so the filter matches
# nothing, the .obj.d files stay empty and editing a header silently does not
# trigger a rebuild. Declare the dependency explicitly instead: with ~10 TUs it
# costs a full recompile of our sources only when a header actually changes.
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
target_include_directories(tea_agent PRIVATE ${TEA_GEN_DIR})
target_link_libraries(tea_agent PRIVATE tea_3rd)

if(WIN32)
  # winhttp: https via schannel; ws2_32: sockets; advapi32: mbedTLS entropy
  target_link_libraries(tea_agent PRIVATE winhttp ws2_32 advapi32)
endif()

if(MINGW)
  # Produce a self-contained .exe: without this the binary needs
  # libstdc++-6.dll / libgcc_s_seh-1.dll / libwinpthread-1.dll on PATH.
  target_link_options(tea_agent PRIVATE -static-libgcc -static-libstdc++ -static)
endif()

find_package(Threads REQUIRED)
target_link_libraries(tea_agent PRIVATE Threads::Threads)

# Put the binary in a predictable place and copy the web sources next to it so
# that --web-dir can be used for live UI editing.
set_target_properties(tea_agent PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin
)
add_custom_command(TARGET tea_agent POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E copy_directory ${TEA_WEB_DIR} ${CMAKE_BINARY_DIR}/bin/web
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

message(STATUS "tea_agent ${PROJECT_VERSION}: compiler=${CMAKE_CXX_COMPILER_ID} std=${CMAKE_CXX_STANDARD}")
