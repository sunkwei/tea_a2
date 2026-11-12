#!/usr/bin/env bash
#
# build_android.sh -- the agent as an APK.
#
# Two steps:
#   1. the C++ core is compiled for Android (bionic) into liba2.so, once per
#      ABI: the phone (arm64-v8a) and the emulator (x86_64);
#   2. the Gradle project in android/ packages those libraries into the app.
#
# The app has no executable: Android 10+ refuses to run files out of an app's
# own data directory, so the agent lives inside the app process and is called
# through JNI.
#
#   scripts/build_android.sh              # arm64-v8a + x86_64, debug APK
#   scripts/build_android.sh --release    # a release APK (signed with the debug key)
#   scripts/build_android.sh --arm64      # only the phone's ABI (faster)
#
# Needs: an Android SDK with the NDK, and a JDK 17. Both come from
# docs/android.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SDK="${ANDROID_HOME:-$HOME/work/android-sdk}"
NDK="${TEA_ANDROID_NDK:-$SDK/ndk/27.3.13750724}"
PLATFORM="${ANDROID_PLATFORM:-android-24}"
MIN_SDK=26
TARGET_SDK=34
ABIS=("arm64-v8a" "x86_64")
GRADLE_USER_HOME_DIR="${TEA_GRADLE_HOME:-$HOME/.gradle-a2}"
BUILD_TYPE="debug"
TASK="assembleDebug"

for arg in "$@"; do
  case "$arg" in
    --release) BUILD_TYPE="release"; TASK="assembleRelease" ;;
    --arm64) ABIS=("arm64-v8a") ;;
    --x86_64) ABIS=("x86_64") ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [ ! -d "$NDK" ]; then
  echo "error: no NDK at $NDK" >&2
  echo "  set TEA_ANDROID_NDK, or install it: see docs/android.md" >&2
  exit 1
fi
if [ ! -d "$SDK/platforms" ]; then
  echo "error: no Android SDK at $SDK" >&2
  exit 1
fi

STRIP="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-strip"

# The toolchain file is read again inside every try_compile; only the
# environment survives that trip.
export TEA_ANDROID_NDK="$NDK"

echo "== the agent as a shared library =="
for abi in "${ABIS[@]}"; do
  build_dir="$ROOT/build-android-$abi"
  cmake -S "$ROOT" -B "$build_dir" -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$ROOT/cmake/android-ndk.cmake" \
    -DTEA_ANDROID_NDK="$NDK" \
    -DANDROID_ABI="$abi" \
    -DANDROID_PLATFORM="$PLATFORM" \
    -DTEA_AGENT_ANDROID_LIB=ON \
    -DTEA_AGENT_BUILD_TUNNEL=OFF \
    -DCMAKE_BUILD_TYPE=Release > "$build_dir.config.log" 2>&1 || {
      cat "$build_dir.config.log"; exit 1; }
  cmake --build "$build_dir" -j"$(nproc)" > "$build_dir.build.log" 2>&1 || {
      tail -40 "$build_dir.build.log"; exit 1; }
  mkdir -p "$ROOT/android/app/src/main/jniLibs/$abi"
  cp "$build_dir/liba2.so" "/tmp/liba2-$abi.so"
  "$STRIP" --strip-unneeded "/tmp/liba2-$abi.so"
  cp "/tmp/liba2-$abi.so" "$ROOT/android/app/src/main/jniLibs/$abi/liba2.so"
  echo "   $abi: $(du -h "$ROOT/android/app/src/main/jniLibs/$abi/liba2.so" | cut -f1)"
done

echo "== the app =="
echo "sdk.dir=$SDK" > "$ROOT/android/local.properties"
cd "$ROOT/android"
GRADLE_USER_HOME="$GRADLE_USER_HOME_DIR" ./gradlew "$TASK" --console=plain
APK="$ROOT/android/app/build/outputs/apk/$BUILD_TYPE/app-$BUILD_TYPE.apk"
echo
echo "APK: $APK ($(du -h "$APK" | cut -f1))"
echo "install: adb install -r \"$APK\""
