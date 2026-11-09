#!/usr/bin/env bash
# build_probe.sh -- build the "HTTP over a WebRTC data channel" prototype.
#
# It downloads the three dependencies, builds them statically, links the
# prototype and runs it. No system packages, no root, nothing installed:
# everything lands in $WORKDIR (default /tmp/a2-tunnel-probe).
#
#   bash tools/tunnel_probe/build_probe.sh              # linux x86_64
#   bash tools/tunnel_probe/build_probe.sh --cross win  # windows x64 (mingw)
#   bash tools/tunnel_probe/build_probe.sh --cross arm  # linux aarch64
#
# See docs/tunnel.md for what this proves and what it does not.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WORKDIR="${WORKDIR:-/tmp/a2-tunnel-probe}"
CROSS="${CROSS:-none}"
JOBS="$(nproc 2>/dev/null || echo 4)"

DC_VERSION="0.22.0"
MBEDTLS_VERSION="3.5.2"

usage() { echo "usage: $0 [--cross win|arm]"; exit 1; }
if [ "${1:-}" = "--cross" ]; then CROSS="${2:-}"; shift 2; fi
[ "${1:-}" = "--help" ] && usage

CXXFLAGS_EXTRA=""
LINK_EXTRA=""
SUFFIX=""
case "$CROSS" in
  none)
    CXX="${CXX:-g++}"
    LINK_EXTRA="-lpthread -ldl"
    ;;
  win)
    CXX="${MINGW_CXX:-x86_64-w64-mingw32-g++-posix}"
    CXXFLAGS_EXTRA="-DRTC_STATIC"
    # usrsctp needs GetAdaptersAddresses (iphlpapi); winsock and the rest are
    # what libdatachannel and usrsctp expect on Windows.
    LINK_EXTRA="-lws2_32 -lwinmm -lbcrypt -luser32 -liphlpapi"
    SUFFIX=".exe"
    ;;
  arm)
    CXX="${ARM_CXX:-/opt/arm-gnu-toolchain-11.3.rel1-x86_64-aarch64-none-linux-gnu/bin/aarch64-none-linux-gnu-g++}"
    CXXFLAGS_EXTRA="-DRTC_STATIC"
    LINK_EXTRA="-lpthread"
    ;;
  *) usage ;;
esac

mkdir -p "$WORKDIR"
cd "$WORKDIR"

fetch() {  # fetch <url> <file>
  if [ ! -s "$2" ]; then
    echo "==> downloading $2"
    curl -fsSL -o "$2" "$1"
  fi
}

echo "==> fetching dependencies into $WORKDIR"
fetch "https://codeload.github.com/Mbed-TLS/mbedtls/tar.gz/refs/tags/mbedtls-$MBEDTLS_VERSION" mb.tgz
fetch "https://codeload.github.com/paullouisageneau/libdatachannel/tar.gz/refs/tags/v$DC_VERSION" dc.tgz
fetch "https://codeload.github.com/SergiusTheBest/plog/tar.gz/refs/heads/master" plog.tgz
fetch "https://codeload.github.com/sctplab/usrsctp/tar.gz/refs/heads/master" usrsctp.tgz
fetch "https://codeload.github.com/paullouisageneau/libjuice/tar.gz/refs/heads/master" libjuice.tgz

[ -d mbedtls-mbedtls-$MBEDTLS_VERSION ] || tar xzf mb.tgz
[ -d libdatachannel-$DC_VERSION ] || tar xzf dc.tgz

# libdatachannel's submodules are empty in the tarball; drop them in by hand.
DC_DIR="libdatachannel-$DC_VERSION"
for dep in plog usrsctp libjuice; do
  if [ ! -f "$DC_DIR/deps/$dep/CMakeLists.txt" ]; then
    tar xzf "$dep.tgz"
    rm -rf "$DC_DIR/deps/$dep"
    mv "$(ls -d $dep-*/ | head -1)" "$DC_DIR/deps/$dep"
  fi
done

# libdatachannel's DTLS code calls mbedtls_ssl_conf_dtls_srtp_protection_profiles(),
# which only exists when MBEDTLS_SSL_DTLS_SRTP is on. It is off by default.
MB_DIR="mbedtls-mbedtls-$MBEDTLS_VERSION"
grep -q '^#define MBEDTLS_SSL_DTLS_SRTP' "$MB_DIR/include/mbedtls/mbedtls_config.h" || \
  sed -i 's|^//#define MBEDTLS_SSL_DTLS_SRTP$|#define MBEDTLS_SSL_DTLS_SRTP|' \
    "$MB_DIR/include/mbedtls/mbedtls_config.h"

# ---- toolchains ------------------------------------------------------------
# The repo already ships toolchain files that are known to work here; use them
# when they are there, and fall back to a local one otherwise.
TOOLCHAIN_ARGS=()
case "$CROSS" in
  win)
    TC="$REPO/cmake/toolchains/mingw-w64-x86_64.cmake"
    [ -f "$TC" ] || TC="$WORKDIR/toolchain-mingw.cmake"
    if [ "$TC" = "$WORKDIR/toolchain-mingw.cmake" ]; then
      cat > "$TC" <<'EOF'
set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR x86_64)
find_program(TC_GCC NAMES x86_64-w64-mingw32-gcc-posix x86_64-w64-mingw32-gcc)
find_program(TC_GXX NAMES x86_64-w64-mingw32-g++-posix x86_64-w64-mingw32-g++)
if(NOT TC_GCC OR NOT TC_GXX)
  message(FATAL_ERROR "no x86_64-w64-mingw32 compiler found")
endif()
set(CMAKE_C_COMPILER ${TC_GCC})
set(CMAKE_CXX_COMPILER ${TC_GXX})
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
EOF
    fi
    TOOLCHAIN_ARGS=(-DCMAKE_TOOLCHAIN_FILE="$TC")
    ;;
  arm)
    TC="$REPO/cmake/toolchains/aarch64-linux-gnu.cmake"
    [ -f "$TC" ] || TC="$WORKDIR/toolchain-aarch64.cmake"
    if [ "$TC" = "$WORKDIR/toolchain-aarch64.cmake" ]; then
      cat > "$TC" <<'EOF'
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)
set(CMAKE_C_COMPILER   aarch64-linux-gnu-gcc)
set(CMAKE_CXX_COMPILER aarch64-linux-gnu-g++)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
EOF
    fi
    TOOLCHAIN_ARGS=(-DCMAKE_TOOLCHAIN_FILE="$TC")
    ;;
esac

# ---- mbedtls ---------------------------------------------------------------
echo "==> building mbedtls $MBEDTLS_VERSION (static)"
cmake -S "$MB_DIR" -B mb-build "${TOOLCHAIN_ARGS[@]}" \
  -DCMAKE_INSTALL_PREFIX="$WORKDIR/prefix" -DCMAKE_BUILD_TYPE=Release \
  -DENABLE_PROGRAMS=OFF -DENABLE_TESTING=OFF \
  -DUSE_SHARED_MBEDTLS_LIBRARY=OFF -DUSE_STATIC_MBEDTLS_LIBRARY=ON > /dev/null
cmake --build mb-build -j"$JOBS" > /dev/null
cmake --install mb-build > /dev/null

# ---- libdatachannel --------------------------------------------------------
echo "==> building libdatachannel $DC_VERSION (static, data channels only)"
cmake -S "$DC_DIR" -B dc-build "${TOOLCHAIN_ARGS[@]}" \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
  -DUSE_MBEDTLS=ON -DUSE_NETTLE=OFF \
  -DNO_MEDIA=ON -DNO_WEBSOCKET=ON -DNO_EXAMPLES=ON -DNO_TESTS=ON \
  -DCMAKE_PREFIX_PATH="$WORKDIR/prefix" > /dev/null
cmake --build dc-build -j"$JOBS" > /dev/null

# ---- the prototype ---------------------------------------------------------
echo "==> linking the prototype"
"$CXX" -O2 -std=c++17 $CXXFLAGS_EXTRA "$HERE/dc_probe.cpp" \
  -I"$DC_DIR/include" \
  "$WORKDIR/dc-build/libdatachannel.a" \
  "$WORKDIR/dc-build/deps/libjuice/libjuice.a" \
  "$WORKDIR/dc-build/deps/usrsctp/usrsctplib/libusrsctp.a" \
  "$WORKDIR/prefix/lib/libmbedtls.a" \
  "$WORKDIR/prefix/lib/libmbedx509.a" \
  "$WORKDIR/prefix/lib/libmbedcrypto.a" \
  $LINK_EXTRA -o "$WORKDIR/dc_probe$SUFFIX"

ls -la "$WORKDIR/dc_probe$SUFFIX"
echo "==> done. Run it:"
echo "    $WORKDIR/dc_probe$SUFFIX"
