# Cross toolchain file: Linux / aarch64, built from an x86_64 host.
#
#   cmake -S . -B build-arm64 \
#     -DCMAKE_TOOLCHAIN_FILE=cmake/toolchains/aarch64-linux-gnu.cmake
#   cmake --build build-arm64 -j"$(nproc)"
#
# What makes this a *cross* build is CMAKE_SYSTEM_NAME below: without it
# CMAKE_CROSSCOMPILING stays FALSE, the host-tool path (tools/host_embed) never
# engages and the build dies trying to run an embed tool built for the target
# ("aarch64-binfmt-P: Could not open '/lib/ld-linux-aarch64.so.1'").
#
# Override the toolchain location with  export TEA_AARCH64_PREFIX=/path/to/prefix
# (the directory that contains bin/aarch64-linux-gnu-gcc).
#
# Candidate order matters. The Linaro 6.5 toolchain is kept LAST because its
# static libc (glibc 2.23, 2018) is broken on the 4.14-kernel aarch64 devices
# this repo targets: pthread_cond_wait() returns an error immediately, so every
# std::condition_variable::wait() in the process throws inside a noexcept
# function and the agent aborts with "terminate called without an active
# exception" a second after startup (cpp-httplib's thread pool is the first
# victim). Verified on device with a 20-line mutex/condvar repro: Linaro static
# aborts, Linaro dynamic is fine, Arm GNU 11.3 (glibc 2.35) static is fine.

set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)

# Candidate prefixes, first hit wins: an explicit override, then the toolchains
# this repo is known to build with, then whatever is on PATH.
set(_tea_prefixes "")
if(DEFINED ENV{TEA_AARCH64_PREFIX})
  list(APPEND _tea_prefixes "$ENV{TEA_AARCH64_PREFIX}")
endif()
list(APPEND _tea_prefixes
     "/opt/arm-gnu-toolchain-11.3.rel1-x86_64-aarch64-none-linux-gnu"
     "/opt/gcc-linaro-6.5.0-2018.12-x86_64_aarch64-linux-gnu")

foreach(_tea_triplet aarch64-none-linux-gnu aarch64-linux-gnu)
  foreach(_tea_prefix ${_tea_prefixes})
    if(NOT CMAKE_C_COMPILER AND EXISTS "${_tea_prefix}/bin/${_tea_triplet}-gcc")
      set(CMAKE_C_COMPILER "${_tea_prefix}/bin/${_tea_triplet}-gcc")
      set(CMAKE_CXX_COMPILER "${_tea_prefix}/bin/${_tea_triplet}-g++")
    endif()
  endforeach()
endforeach()

if(NOT CMAKE_C_COMPILER)
  find_program(TEA_AARCH64_GCC NAMES aarch64-none-linux-gnu-gcc aarch64-linux-gnu-gcc)
  find_program(TEA_AARCH64_GXX NAMES aarch64-none-linux-gnu-g++ aarch64-linux-gnu-g++)
  if(TEA_AARCH64_GCC AND TEA_AARCH64_GXX)
    set(CMAKE_C_COMPILER ${TEA_AARCH64_GCC})
    set(CMAKE_CXX_COMPILER ${TEA_AARCH64_GXX})
  endif()
endif()

if(NOT CMAKE_C_COMPILER OR NOT CMAKE_CXX_COMPILER)
  message(FATAL_ERROR
    "no aarch64 cross compiler found; install one or set "
    "TEA_AARCH64_PREFIX=<dir containing bin/aarch64-linux-gnu-gcc>")
endif()

if(CMAKE_C_COMPILER MATCHES "linaro" AND TEA_AGENT_STATIC_LIBC)
  message(WARNING
    "tea_agent: the Linaro 6.5 toolchain was selected AND TEA_AGENT_STATIC_LIBC is "
    "ON. Its static glibc (2.23) aborts in pthread_cond_wait() on the target "
    "(\"terminate called without an active exception\" right after startup). Use "
    "the Arm GNU Toolchain 11.3 instead, or configure with "
    "-DTEA_AGENT_STATIC_LIBC=OFF (static C++ runtime + target glibc).")
endif()

# The target is a device we do not control: default to a binary that does not
# care about the glibc/libstdc++ versions installed there (both toolchains above
# ship a static libc). Turn off with -DTEA_AGENT_STATIC_LIBC=OFF when the target
# matches the build host and you want dynamic linking / NSS-based name lookup.
set(TEA_AGENT_STATIC_RUNTIME ON CACHE BOOL
    "Link the C++ runtime statically so the binary is self-contained")
set(TEA_AGENT_STATIC_LIBC ON CACHE BOOL
    "Link libc statically so the binary runs on older systems")
