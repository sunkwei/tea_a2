# Host-side helper project.
#
# tools/embed_assets.cpp turns web/ into a C++ translation unit, and
# tools/embed_ca_bundle.cpp turns 3rd/ca-bundle/cacert.pem into one. Both run at
# build time on the *build host*, so when the agent itself is cross-compiled for
# an embedded target this project is configured and built with the host
# toolchain (see TEA_AGENT_USE_HOST_ASSET_TOOL in the top-level CMakeLists.txt).
# Building either of them with the target toolchain would produce a binary the
# host cannot execute.
cmake_minimum_required(VERSION 3.20)
project(tea_agent_host_tools LANGUAGES CXX)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(MSVC)
  add_compile_options(/utf-8 /MP /W3)
else()
  add_compile_options(-O2)
endif()

foreach(_tool embed_assets embed_ca_bundle)
  add_executable(tea_${_tool} ${CMAKE_CURRENT_SOURCE_DIR}/../${_tool}.cpp)
  set_target_properties(tea_${_tool} PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
endforeach()
