#!/usr/bin/env sh
# ---------------------------------------------------------------------------
# tea_agent build helper (Linux / macOS / other POSIX)
#
#   scripts/build.sh              -> Release build into ./build
#   scripts/build.sh build-debug  -> build into ./build-debug
#
# Only CMake and a C/C++ toolchain are required; every third-party library is
# vendored under 3rd/.
# ---------------------------------------------------------------------------
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "${script_dir}/.." && pwd)

build_dir="${root}/build"
if [ "$#" -ge 1 ]; then
  build_dir="${root}/$1"
fi

if command -v nproc >/dev/null 2>&1; then
  jobs=$(nproc)
elif command -v sysctl >/dev/null 2>&1; then
  jobs=$(sysctl -n hw.ncpu)
else
  jobs=4
fi

echo "[build] root      : ${root}"
echo "[build] build dir : ${build_dir}"
echo "[build] jobs      : ${jobs}"

cmake -S "${root}" -B "${build_dir}" -DCMAKE_BUILD_TYPE=Release
cmake --build "${build_dir}" --parallel "${jobs}"

echo
echo "[build] ok -> ${build_dir}/bin/tea_agent"
