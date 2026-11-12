# cmake/android-ndk.cmake -- NDK toolchain for the Android build of liba2.so.
#
# The phone build is not a second implementation of the agent: it is the same
# C++ core, compiled for bionic and loaded by the APK through JNI.
#
#   cmake -S . -B build-android \
#     -DCMAKE_TOOLCHAIN_FILE=cmake/android-ndk.cmake \
#     -DTEA_ANDROID_NDK=$HOME/work/android-sdk/ndk/27.3.13750724 \
#     -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-24 \
#     -DTEA_AGENT_ANDROID_LIB=ON -DTEA_AGENT_BUILD_TUNNEL=OFF
#
# The APK ships the library in jniLibs/<abi>/liba2.so; there is no separate
# executable and no Termux: Android 10+ refuses to exec files out of an app's
# own data directory, so the agent has to live inside the process.

# The NDK root. The environment comes first: the toolchain file is included
# again inside every try_compile, and only the environment survives that
# trip. The command line (-DTEA_ANDROID_NDK=...) is the fallback.
if(NOT "$ENV{TEA_ANDROID_NDK}" STREQUAL "")
  set(TEA_ANDROID_NDK "$ENV{TEA_ANDROID_NDK}")
elseif(NOT TEA_ANDROID_NDK)
  if(DEFINED ENV{ANDROID_NDK_HOME})
    set(TEA_ANDROID_NDK "$ENV{ANDROID_NDK_HOME}")
  elseif(DEFINED ENV{ANDROID_NDK_ROOT})
    set(TEA_ANDROID_NDK "$ENV{ANDROID_NDK_ROOT}")
  endif()
endif()
if(NOT TEA_ANDROID_NDK)
  message(FATAL_ERROR
    "Set TEA_ANDROID_NDK (or ANDROID_NDK_HOME) to the NDK root, e.g. "
    "$HOME/work/android-sdk/ndk/27.3.13750724")
endif()
if(NOT EXISTS "${TEA_ANDROID_NDK}/build/cmake/android.toolchain.cmake")
  message(FATAL_ERROR "No android.toolchain.cmake under ${TEA_ANDROID_NDK} - wrong NDK root?")
endif()

include("${TEA_ANDROID_NDK}/build/cmake/android.toolchain.cmake")

# One .so per ABI; everything in it is position independent.
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# The agent's own third-party libraries (yaml-cpp, mbedtls, miniz) are built
# as static archives and linked into the shared library, so the APK only has
# to ship liba2.so plus the C++ runtime the NDK provides.
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
