# Host-side helper project.
#
# tools/embed_assets.cpp turns web/ into a C++ translation unit. It runs at
# build time on the *build host*, so when the agent itself is cross-compiled for
# an embedded target this project is configured and built with the host
# toolchain (see TEA_AGENT_USE_HOST_ASSET_TOOL in the top-level CMakeLists.txt).
cmake_minimum_required(VERSION 3.20)
project(tea_agent_host_tools LANGUAGES CXX)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(MSVC)
  add_compile_options(/utf-8 /MP /W3)
else()
  add_compile_options(-O2)
endif()

add_executable(tea_embed_assets ${CMAKE_CURRENT_SOURCE_DIR}/../embed_assets.cpp)
set_target_properties(tea_embed_assets PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
