# 构建与跨平台编译

> 这一页讲**怎么把二进制造出来**。要直接敲命令，看
> [使用手册](manual.md)。

| 角色 | 要求 |
| --- | --- |
| 构建系统 | CMake ≥ 3.20（3.x / 4.x 均可） |
| C++ 编译器 | 支持 C++17（已验证 GCC 6.5 / MinGW GCC 16.2 / MSVC 19.51） |
| C 编译器 | C99，仅用于编译 mbedTLS |
| 生成器 | Ninja / Unix Makefiles / NMake / Xcode |
| 链接库 | 系统自带：Windows `winhttp` `ws2_32` `advapi32`；POSIX `pthread` |

不需要 OpenSSL、zlib、vcpkg、conan、Python、Perl。


# 构建与跨平台编译

### 程序正在运行时也能直接构建（Windows）

Windows 不允许链接器写一个正在运行的 `.exe`：

```
LINK : fatal error LNK1104: 无法打开文件"bin\tea_agent.exe"      (MSVC)
cannot open output file bin\tea_agent.exe: Permission denied     (MinGW)
```

以前只能「先停掉 agent → 构建 → 再启动」，而在开发 UI 时这个循环要走上百遍。其实这个锁没
那么严（实测 Windows 10 19045）：

| 操作 | 运行中的 `.exe` |
| --- | --- |
| `CreateFile(WRITE)`（链接器做的事） | ❌ 失败——这就是 LNK1104 的来源 |
| `DeleteFile`（删除） | ❌ `ERROR_ACCESS_DENIED` |
| `MoveFile`（改名） | ✅ **可以**，名字可以拿走 |

所以 `cmake/free-running-exe.cmake` 用上了第三条：链接前把旧二进制改名为
`tea_agent.exe.prev-<时间戳>`，让链接器在原名上写新文件；链接成功后再删掉那份旧拷贝。

```
Freeing tea_agent.exe for relinking
-- tea_agent: tea_agent.exe was moved to tea_agent.exe.prev-20261019-185712
          (it was still running, so the new binary takes the old name; restart the
           instance to pick up the new code)
```

要点：

- **旧进程不受影响**，继续用内存里那份代码服务请求，直到你重启它；
- 旧拷贝在进程退出前删不掉（被映射着），所以它会留到**下一次构建**的 POST_BUILD 才清理；
- 链接失败也不会丢东西：旧二进制以 `.prev-*` 留在原目录，可以直接改名回去；
- 只在**真的要链接**时才动作（`PRE_LINK`），空构建不会产生任何改名；
- POSIX 上整个步骤直接 return（Linux/macOS 允许覆盖运行中的镜像）；
- 三个产物都覆盖到了：`tea_agent`、`tea_embed_assets`、`tea_embed_ca_bundle`。

回归测试：`pwsh -File tests/relink-while-running.ps1 -Exe build-gcc\bin\tea_agent.exe`
（会真的起一个隐藏实例、构建一次、验证新二进制就位且旧实例仍在服务，再验证 leftovers 清理；
MinGW 与 MSVC 各 12 项）。它先断言「运行中的镜像确实打不开写权限」这个前提，前提不成立就说明
这套机制已经没必要了，而不是静悄悄地通过。

## 构建

```bash
# Linux / macOS
./scripts/build.sh

# Linux / macOS（不用脚本，等价）
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"
```

```bat
:: Windows + MinGW-w64
scripts\build-mingw.bat

:: Windows + MSVC（Ninja 在部分环境会卡死，默认走 NMake）
scripts\build.bat
```

产物：`build/bin/tea_agent`（Windows 为 `build-msvc\bin\tea_agent.exe` 等）。
本机编译只有这几条命令；**交叉编译（Linux → Windows / Linux aarch64）见下面的
[跨平台编译](#跨平台编译) 一节**，那里有编译矩阵、toolchain 文件、产物校验与错误对照。

**MSVC 的 `/showIncludes` 刷屏**：本地化的 Visual Studio 会把「包含文件」用本地语言输出，
CMake 拿到这个前缀后要传给 `cmake -E cmake_cl_compile_depends --filter-prefix=…`，而命令行在
控制台代码页里存不住非 ASCII 字节，于是过滤一条也匹配不上——日志里每个编译单元都会多出
上百行 `注意: 包含文件: …`（副作用是 `.obj.d` 为空，改头文件不会触发重编）。修法只有一个：
让 cl.exe 说英文。`scripts\build.bat` 通过 `scripts\vcenv.bat` 设置 `VSLANG=1033`，前缀变成
ASCII 的 `Note: including file: `，问题消失，顺带让诊断信息可搜索。

**关键点：`VSLANG` 必须同时存在于「构建」进程的环境里**，不只是配置时——`cl.exe` 自己读它，
CMake 不读。只在配置时设过、后来在别的窗口里跑 `cmake --build`，日志照样是本地化的。

三种方式任选（都不需要改 IDE 设置）：

```bat
:: 1) 仓库自带脚本（vcenv.bat 里设 VSLANG，构建进程继承）
scripts\build.bat

:: 2) CMake presets（configure/build 两个 preset 都带 VSLANG）
cmake --preset msvc
cmake --build --preset msvc

:: 3) 手敲：同一个窗口里设好再 configure/build
set VSLANG=1033
cmake -S . -B build-msvc -G "NMake Makefiles" -DCMAKE_BUILD_TYPE=Release
cmake --build build-msvc
```

已经配置过的构建目录会把旧前缀缓存在 `CMakeFiles\<ver>\CMakeCXXCompiler.cmake` 里；
CMakeLists 在检测到「本地化前缀 + `VSLANG=1033`」时会自动换成英文前缀并打印一行说明，所以
多数情况下重跑即可，实在顽固就 `cmake --fresh`（或删掉 `CMakeCache.txt`）重配一次。

`scripts\build.bat` 在编译前还会真的跑一次 `cl /showIncludes` 探测：如果这台机器上的 `cl.exe`
无视 `VSLANG`（极少数本地化安装会这样），它会直接打印一条明确的 WARNING，而不是让你对着
几百行 `包含文件` 猜。


## 跨平台编译

同一份源码，构建命令只取决于「宿主 → 目标」这一组组合。全部分支都收在
`CMakePresets.json` 与 `cmake/toolchains/` 里，下面这张表就是全部入口。

### 编译矩阵

| 宿主 → 目标 | 命令 | 产物 | 状态 |
| --- | --- | --- | --- |
| Linux x86_64 → 本机 | `./scripts/build.sh` 或 `cmake --preset posix && cmake --build --preset posix -j"$(nproc)"` | `build/bin/tea_agent` | ✅ 实测 |
| macOS → 本机 | 同上（`Xcode`/`Unix Makefiles`） | `build/bin/tea_agent` | ✅ 实测（10.6+） |
| Windows → 本机（MinGW-w64） | `scripts\build-mingw.bat` 或 `cmake --preset mingw` | `build-mingw\bin\tea_agent.exe` | ✅ 实测 |
| Windows → 本机（MSVC） | `scripts\build.bat` 或 `cmake --preset msvc` | `build-msvc\bin\tea_agent.exe` | ✅ 实测 |
| Linux / macOS → **Windows x64** | `cmake --preset win64-mingw-cross && cmake --build --preset win64-mingw-cross -j"$(nproc)"` | `build-win64/bin/tea_agent.exe`（PE32+，CA 已内嵌） | ✅ 产出；运行需 Windows |
| Linux x86_64 → **Linux aarch64** | `cmake --preset arm64-linux && cmake --build --preset arm64-linux -j"$(nproc)"` | `build-arm64/bin/tea_agent`（静态） | ✅ 真机运行 |

两类「交叉」用的都是 toolchain 文件（`cmake/toolchains/*.cmake`），它们的区别只是
`CMAKE_SYSTEM_NAME` 与编译器前缀，机制相同。

### 先装好编译器

```bash
# Debian / Ubuntu：本机
sudo apt install build-essential cmake
# Debian / Ubuntu：交叉到 Windows（会同时装上 -posix 线程模型的 g++，必需，见下文）
sudo apt install mingw-w64
```

```bash
# Linux x86_64 → aarch64：推荐 Arm GNU Toolchain 11.3，解压到 /opt 即可被自动发现
#   /opt/arm-gnu-toolchain-11.3.rel1-x86_64-aarch64-none-linux-gnu
# 或显式指定前缀目录（该目录下有 bin/aarch64-linux-gnu-gcc 或 aarch64-none-linux-gnu-gcc）
export TEA_AARCH64_PREFIX=/path/to/aarch64-toolchain
```

工具链查找顺序：`TEA_AARCH64_PREFIX` → `/opt/arm-gnu-toolchain-11.3…` →
`/opt/gcc-linaro-6.5.0-2018.12…` → `PATH` 上的 `aarch64-*-gcc`。Linaro 6.5 排在最后是
故意的（原因见下面的坑）。

```bash
# macOS
xcode-select --install
brew install cmake
brew install mingw-w64      # 只有要交叉出 Windows 版时才需要
```

Windows 本机：装 Visual Studio Build Tools（MSVC，配 `scripts\build.bat`）或 winlibs
MinGW-w64（`scripts\build-mingw.bat` 会依次找 `%MINGW_HOME%`、`.toolchain\mingw64`、`PATH`）。
两条路径都会设置 `VSLANG=1033`（MSVC）或 `TEMP`（MinGW），细节见 [快速开始](../README.md#快速开始)。

### 本机编译

```bash
# Linux / macOS，手敲（等价于 scripts/build.sh）
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"
./build/bin/tea_agent --version

# 或者用 preset（configure 与 build 各一个）
cmake --preset posix
cmake --build --preset posix -j"$(nproc)"
```

```bat
:: Windows
scripts\build-mingw.bat            :: MinGW-w64
scripts\build.bat                  :: MSVC（NMake，带 VSLANG=1033）
```

### 交叉编译：必须走 toolchain 文件

**只把 `CMAKE_C(XX)_COMPILER` 换成目标编译器不算交叉编译**：没设 `CMAKE_SYSTEM_NAME` 时
`CMAKE_CROSSCOMPILING` 仍是假，`WIN32` 也不会打开，构建期工具路径不启用，最终报出的错误
和目标平台毫无关系（两个真实症状见下面的对照表）。所以要么用 preset，要么显式传
`-DCMAKE_TOOLCHAIN_FILE=...`：

```bash
# Linux x86_64 → Windows x64
cmake -S . -B build-win64 -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_TOOLCHAIN_FILE=cmake/toolchains/mingw-w64-x86_64.cmake
cmake --build build-win64 -j"$(nproc)"
```

```bash
# Linux x86_64 → Linux aarch64
cmake -S . -B build-arm64 -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_TOOLCHAIN_FILE=cmake/toolchains/aarch64-linux-gnu.cmake
cmake --build build-arm64 -j"$(nproc)"
```

CMakeLists.txt 在 configure 阶段就会拦下两种「看起来像交叉、其实不是」的配置，并直接给出
修法，而不是留到编译到一半才炸：

- 编译器目标是 mingw，但 `CMAKE_CROSSCOMPILING` 为假 → 让你改用 toolchain 文件 / preset；
- 编译器是 MinGW 的 win32 线程模型且 GCC < 13 → 告诉你这份 libstdc++ 根本没有 `std::mutex`。

#### 构建期工具必须用宿主编译器

`tools/embed_assets.cpp`（`web/` → C++ 源）与 `tools/embed_ca_bundle.cpp`
（`cacert.pem` → C++ 源）是在**构建机**上运行的：用目标编译器编出来的程序，宿主机根本跑不了。
交叉编译时它们由 `tools/host_embed/` 子项目用宿主编译器单独构建，
`TEA_AGENT_USE_HOST_ASSET_TOOL` 默认为 `CMAKE_CROSSCOMPILING`（本机构建也可以
`-DTEA_AGENT_USE_HOST_ASSET_TOOL=ON` 来验证这条路径）。相关的两个细节：

- 宿主工具的**可执行后缀**按宿主算（`TEA_HOST_EXE_SUFFIX`）：从 Linux 交叉到 Windows 时，
  宿主工具叫 `tea_embed_assets`，没有 `.exe`——用目标的 `CMAKE_EXECUTABLE_SUFFIX` 会找不到；
- 生成规则按**路径**而不是 target 引用它们（`COMMAND ${TEA_CA_BUNDLE_EXE}`），否则
  `COMMAND <target>` 会解析成目标架构的二进制。

### 校验产物

```bash
file build-win64/bin/tea_agent.exe   # PE32+ executable (console) x86-64, for MS Windows, 19 sections
file build-arm64/bin/tea_agent       # ELF 64-bit LSB executable, ARM aarch64, version 1 (GNU/Linux),
                                     #   statically linked, for GNU/Linux 3.7.0
readelf -d build-arm64/bin/tea_agent | head -1
                                     # There is no dynamic section in this file.   ← 默认全静态
grep -c "BEGIN CERTIFICATE" build-arm64/generated/ca_bundle.cpp
                                     # 121  ← CA 真的内嵌进构建产物了
```

`file` 说明架构对了，`readelf -d` 说明不依赖目标机的 glibc（用 `-DTEA_AGENT_STATIC_LIBC=OFF`
构建时会反过来出现 `Dynamic section` 与 `NEEDED … libc.so.6`），CA 计数说明证书进二进制了。
上面几行是本机实测输出。**别用 `ldd` 检查跨架构的产物**：宿主上的 `ldd` 跑不了 ARM 二进制，
它会一律回答 `not a dynamic executable`，看起来像通过、其实什么都没验证。

aarch64 产物在宿主上跑不了，装了 qemu-user 可以先冒烟一次，再拿到真机上验证：

```bash
qemu-aarch64-static build-arm64/bin/tea_agent --version     # tea_agent 0.17.7
```

### 常见错误的对照表

| 症状 | 原因 | 修法 |
| --- | --- | --- |
| 运行 embed 工具时报 `Could not open '/lib/ld-linux-aarch64.so.1'`（或 `cannot execute binary file`） | 没设 `CMAKE_SYSTEM_NAME`，宿主工具用目标编译器编了 | 用 toolchain 文件 / preset 重配 |
| `src/config.h:178: 'mutex' in namespace 'std' does not name a type`（明明 `#include <mutex>` 了） | ① 同上，配置其实不是交叉构建；② 选了 MinGW 的 **win32** 线程模型，GCC < 13 的 libstdc++ 在该模型下不提供 `std::mutex`/`std::thread` | 用 toolchain 文件 / preset（它们优先选 `-posix`）；手敲时用 `x86_64-w64-mingw32-g++-posix` |
| 找不到 `.../tea_embed_assets.exe` | 用目标侧后缀找宿主工具 | 已由 `TEA_HOST_EXE_SUFFIX` 处理，别自己改回 `CMAKE_EXECUTABLE_SUFFIX` |
| aarch64 启动约 1 秒后 `terminate called without an active exception` | Linaro 6.5 的静态 glibc 2.23 在 4.14 内核上 `pthread_cond_wait()` 立即失败 | 换成 Arm GNU Toolchain 11.3（默认），或 `-DTEA_AGENT_STATIC_LIBC=OFF` |
| 启动即 abort，日志里 `Invalid range in bracket expression` | GCC ≤ 6 的 `std::regex` 不接受方括号里转义的 `\-` | 已修（`-` 放最后、不转义）：`[A-Za-z0-9_.-]` |
| Windows 重新链接时 `LNK1104` / `Permission denied` | 旧的 `.exe` 还在运行 | 见上文「程序正在运行时也能直接构建」 |

### 可调开关

| 选项 | 默认 | 作用 |
| --- | --- | --- |
| `TEA_AGENT_CXX_STANDARD` | `17` | 设成 `14` 验证「C++14 也能编」（GCC 6.5 是参照的老工具链） |
| `TEA_AGENT_USE_MBEDTLS` | `ON` | 关掉可显著减小体积，但 Linux 上就没有 `https://` |
| `TEA_AGENT_STATIC_RUNTIME` | `OFF`（aarch64 toolchain 置 `ON`） | 静态链接 `libstdc++`/`libgcc` |
| `TEA_AGENT_STATIC_LIBC` | `OFF`（aarch64 toolchain 置 `ON`） | 再静态链接 libc（`-static -no-pie`） |
| `TEA_AGENT_USE_HOST_ASSET_TOOL` | `= CMAKE_CROSSCOMPILING` | 强制用宿主编译器构建 embed 工具 |
| `CMAKE_BUILD_TYPE` | `Release` | 想更小用 `MinSizeRel`，再 `strip` |

---

## 发版：包传到百度网盘，不进 git

**发布产物（zip）不再提交到 git**（2026-10-30 起）。仓库里只留源码和文档；每一版的包
传到**百度网盘**，远端路径写进发版记录。

```bash
# 打包 + 上传 + 清理旧版本，一步到位（这台机器上的流程）
.tea_agent_run/scripts/release.sh 0.17.7

# 等价于这三步：
# 1. 打包（暂存目录里打，只 strip 暂存副本，release/ 里最后只剩那个 zip）
.tea_agent_run/scripts/pack_release.sh 0.17.7
# 2. 传到网盘 /tea_a2/releases/0.17.7/，并把网盘上旧版本清到只剩 5 个
python3 sched/pan_publish.py release/tea_a2-0.17.7.zip --version 0.17.7 \
    --remote-dir /tea_a2/releases/0.17.7 --keep 5
# 3. 提交源码（**不**提交 release/ 里的 zip —— .gitignore 已经挡了）
git add -A && git commit -m "release: 0.17.7"
```

**网盘上最多留 5 个发布包**：传完按版本号（`0.17.10 > 0.17.3`，按数字比不是按字符串）排，
最新的 5 个留下，其余的目录连包带清单删掉。想看现在有哪些：

```bash
python3 sched/pan_publish.py --list            # /tea_a2/releases 下有哪些版本、多大、什么时候传的
```

`pan_publish.py` 会：

- 按 **4MB 一片**上传（百度网盘的上传接口就是 4MB 分片，最后一片是余数；
  小于 4MB 的文件走单片），分片数 = `ceil(大小 / 4MB)`；
- 传完**回读校验**（把网盘上的文件下下来比对 sha256）—— 发版的东西不能只看
  「上传成功」；
- 在远端写一份 `release-manifest.json`（版本号、时间、机器、每个文件的
  sha256 / md5 / 大小 / 分片数）。

配置和令牌跟[配置同步](pan-sync.md)共用一套（`~/.tea_agent/pan-sync/config`）。

清理网盘上的旧版本（`opera=delete` 在查询串里，`filelist` 是路径的 JSON 数组）：

```python
import os, sys; sys.path.insert(0, "sched")
import pan_sync
cfg = pan_sync.load_config()
c = pan_sync.PanClient(cfg.text("app_key"), cfg.text("app_secret"),
                       token_path=os.path.expanduser("~/.tea_agent/pan-sync/token.json"))
c.delete_path("/tea_a2/releases/0.17.3")
```

---

---

← 回到 [README](../README.md) · 命令清单见 [使用手册](manual.md)
