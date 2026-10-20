# Cross toolchain file: Windows x64 / MinGW-w64, built from any host with the
# x86_64-w64-mingw32 triplet on PATH (Debian/Ubuntu: apt install mingw-w64).
#
#   cmake -S . -B build-win64 \
#     -DCMAKE_TOOLCHAIN_FILE=cmake/toolchains/mingw-w64-x86_64.cmake
#   cmake --build build-win64 -j"$(nproc)"
#
# On Windows itself use the native preset instead: `cmake --preset mingw`.

set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR x86_64)

# Debian ships both thread models. Prefer -posix: libstdc++ built with the
# -win32 model has no usable std::thread before GCC 13, and this program is
# multi-threaded end to end (cpp-httplib's thread pool, the agent loop, the
# web server).
find_program(TOOLCHAIN_MINGW_GCC NAMES x86_64-w64-mingw32-gcc-posix x86_64-w64-mingw32-gcc)
find_program(TOOLCHAIN_MINGW_GXX NAMES x86_64-w64-mingw32-g++-posix x86_64-w64-mingw32-g++)
find_program(TOOLCHAIN_MINGW_WINDRES NAMES x86_64-w64-mingw32-windres)

if(NOT TOOLCHAIN_MINGW_GCC OR NOT TOOLCHAIN_MINGW_GXX)
  message(FATAL_ERROR
    "no x86_64-w64-mingw32 compiler found; install mingw-w64 or put the "
    "triplet's bin directory on PATH")
endif()

set(CMAKE_C_COMPILER   ${TOOLCHAIN_MINGW_GCC})
set(CMAKE_CXX_COMPILER ${TOOLCHAIN_MINGW_GXX})
if(TOOLCHAIN_MINGW_WINDRES)
  set(CMAKE_RC_COMPILER ${TOOLCHAIN_MINGW_WINDRES})
endif()

# Only search the target for libraries/headers; never pick up host ones.
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
