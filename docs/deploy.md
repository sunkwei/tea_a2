# 部署

## 部署到嵌入式设备

### 交叉编译

通用的交叉编译方法（编译矩阵、toolchain 文件、宿主工具、产物校验、错误对照）在
[跨平台编译](#跨平台编译) 一节。嵌入式这边只多两件事：

```bash
cmake --preset arm64-linux          # 工具链位置用 TEA_AARCH64_PREFIX 覆盖
cmake --build --preset arm64-linux -j"$(nproc)"
```

- **默认全静态**：aarch64 的 toolchain 文件打开 `TEA_AGENT_STATIC_RUNTIME` 与
  `TEA_AGENT_STATIC_LIBC`，目标机的 glibc 版本无关紧要；构建机与目标机环境一致时用
  `-DTEA_AGENT_STATIC_LIBC=OFF` 改回动态（静态 glibc 无法 `dlopen` NSS 模块，域名解析行为
  会变，详见下文「单文件部署」）。
- **选对工具链**：默认用 Arm GNU Toolchain 11.3（glibc 2.35）。Linaro 6.5 会被选中时
  toolchain 文件会给 WARNING——它的静态 glibc 2.23 在 4.14 内核设备上一进
  `std::condition_variable::wait()` 就抛错，进程启动约 1 秒即
  `terminate called without an active exception`（真机结论：Linaro 静态 ❌、Linaro 动态 ✅、
  11.3 静态 ✅）。

### 体积

| 构建 | 大小 | 运行库依赖 |
| --- | --- | --- |
| Windows x64 / MinGW-w64（0.10.2 实测，`win64-mingw-cross`） | 6.8 MB（`strip` 后 4.3 MB） | 仅 Windows 10+ 自带的 UCRT 与系统 DLL，可单文件分发 |
| Windows x64 / MSVC（0.8.1 实测，未在 0.10.2 重测） | 2.6 MB | 需要目标机安装 VC++ 运行库（`MSVCP140.dll` / `VCRUNTIME140*.dll`） |
| Linux x64（0.10.2 实测，静态构建） | 6.2 MB（`strip` 后 5.2 MB） | **完全静态**：不依赖目标机的 glibc 版本 |
| Linux aarch64（0.10.2 实测，静态构建） | 17.8 MB，`strip` 后 4.6 MB | 默认完全静态（Arm GNU 11.3 + 静态 glibc）；该工具链的 libstdc++ 自带调试信息，部署前记得 `strip` |

想进一步压：`-DCMAKE_BUILD_TYPE=MinSizeRel` + `strip`；不需要 HTTPS 可
`-DTEA_AGENT_USE_MBEDTLS=OFF`（体积显著下降，但 Linux 上就没有 `https://` 了）；
还可以用自定义 `MBEDTLS_CONFIG_FILE` 裁掉 mbedTLS 里用不到的算法。

### 单文件部署（完全静态）

一个 ELF 拷到目标机就能跑：**不依赖目标机的 glibc 版本**，也**不需要旁边的 PEM 文件**。
这正是「部署到比构建机更老的系统」所需要的——动态链接的二进制在那种机器上会先报
`version 'GLIBC_2.xx' not found` 再退出，连一句有用的错误都来不及打印。

| 平台 | 链接方式 | 目标机需要什么 |
| --- | --- | --- |
| Windows / MinGW-w64 | `-static-libgcc -static-libstdc++ -static`（自动） | 只有 Windows 自带的 `KERNEL32/ADVAPI32/SHELL32/WINHTTP/WS2_32` + UCRT |
| Linux / 其它 ELF（GCC/Clang） | `-static -no-pie`（**需显式打开**，aarch64 toolchain 已默认打开） | **什么都不需要**，连 glibc 都不用 |
| macOS | 不适用 | 系统不提供静态 `libc++`，自动跳过 |

本机构建默认是**动态**的（能跑、编译快）；要做单文件部署就显式打开两个开关：

```bash
cmake -S . -B build-static -DCMAKE_BUILD_TYPE=Release \
  -DTEA_AGENT_STATIC_RUNTIME=ON -DTEA_AGENT_STATIC_LIBC=ON
cmake --build build-static -j"$(nproc)"
file    build-static/bin/tea_agent     # → statically linked
readelf -d build-static/bin/tea_agent  # → There is no dynamic section in this file.
```

三个相关开关：

| 选项 | 默认 | 作用 |
| --- | --- | --- |
| `TEA_AGENT_STATIC_RUNTIME` | `OFF`（aarch64 toolchain: `ON`） | 静态链接 `libstdc++`/`libgcc` |
| `TEA_AGENT_STATIC_LIBC` | `OFF`（aarch64 toolchain: `ON`） | 再静态链接 libc（`-static -no-pie`） |
| `TEA_AGENT_USE_MBEDTLS` | `ON` | 关掉可显著减小体积，但 Linux 上就没有 `https://` 了 |

#### glibc 的 NSS 陷阱（已处理）

静态链接的 glibc **不能**安全地 `dlopen` NSS 模块：`getaddrinfo` 会按
`/etc/nsswitch.conf` 加载 `libnss_resolve.so.2`（systemd-resolved）或
`libnss_mdns*.so.2`，把共享模块混进静态二进制里会 **SIGSEGV**。这在 `resolve` 出现在
nsswitch 里的现代发行版上是必现的（本项目实测崩溃过）。

所以 `TEA_STATIC_LIBC` 打开时，`http_socket.cpp` 会把 hosts 数据库钉到 glibc 内建的两种实现：

```c
__nss_configure_lookup("hosts", "files dns");   // 仅静态构建
```

代价（只影响静态构建）：绕过 systemd-resolved 的 NSS 前端与 mDNS。普通 DNS 仍然工作——
这类机器的 `/etc/resolv.conf` 指向 `127.0.0.53` 这个 stub，域名照样由 systemd-resolved 解析，
只是不经过它的 NSS 模块。musl 工具链没有 NSS 模块，无需这层处理。

`tests/smoke_single_file.py` 把这条钉住了：它确认证书嵌在 ELF 里、`ldd` 无共享库依赖、
把二进制单独拷到一个空目录也能启动并使用内嵌根证书，最后**用真实域名走一遍解析**——
回归时的表现是进程被 SIGSEGV 杀掉，而不是一个难查的线上问题。

### TLS 与证书

- Windows 默认走 WinHTTP，使用**系统证书库**，无需额外文件；拿不到状态行时自动回退内置 mbedTLS。
  **内嵌的 `cacert.pem` 与 WinHTTP 无关**：它只被 mbedTLS 后端读取（见下面的覆盖顺序），
  WinHTTP/schannel 永远用 Windows 的证书库。所以「Windows 上编译时要不要内嵌 CA」不影响
  WinHTTP 的行为——两者是两套独立的信任链。

  少数 Windows 环境下 WinHTTP 会连不上（常见于 `WinHttpSendRequest failed (WinHTTP error
  12029)`：schannel 被策略禁用、系统代理只配在 Internet 选项里、或者域名只解析到 IPv6 而
  WinHTTP 不会 Happy Eyeballs），这时会自动回退到内置 mbedTLS，用户无感。现在的行为：

  - 每个主机**只探测一次**：回退成功后记住该主机，后续请求直接走 mbedTLS，不再每次先失败
    一次（以前每轮模型调用都会多一次失败的连接尝试和一行 WARN）；
  - 首次回退记一行 WARN 并提示可以用 `--http-engine socket`（或配置里写
    `settings: { http_engine: socket }`，可持久化、也可按工作区覆盖）彻底跳过探测，
    之后降为 DEBUG；
  - 两个后端都失败时，错误信息同时带上两边的原因，而不是只留 WinHTTP 那句；
  - `GET /api/health` 的 `http_socket_fallback` 会列出已经改用内置 TLS 的主机（没有则为空）。
- 其它平台走内置 mbedTLS，根证书**编译进二进制**（`3rd/ca-bundle/cacert.pem` 由
  `tools/embed_ca_bundle.cpp` 生成 `ca_bundle.cpp`），所以单文件部署不需要随附 PEM。
  启动日志会写 `tls: CA bundle embedded in the binary (N bytes)`。
- 覆盖顺序：`--ca-bundle <pem>` 优先（便于不重新编译就更新根证书）→ 内嵌证书 → 磁盘查找
  （同级 `ca-bundle/cacert.pem`、同级 `cacert.pem`、`../3rd/ca-bundle/cacert.pem`、
  工作目录 `3rd/ca-bundle/cacert.pem`、`$TEA_AGENT_HOME/ca-bundle/cacert.pem`）。
  最后这层只对「构建时没有 vendored 证书」的构建有意义。

---

## Windows 登录自启动（相当于 `systemctl --user enable`）

Windows 没有用户级 systemd。最接近的三种做法，差别在于「跟不跟人走」：

| 方案 | 相当于 | 是否推荐 |
| --- | --- | --- |
| **任务计划程序 + 登录触发**（`InteractiveToken`） | `systemctl --user enable --now` + `Restart=` | ✅ 本仓库自带脚本 |
| `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` / 启动文件夹 | 只有 `enable`：没有重启、没有任务管理 | 临时用 |
| Windows 服务（`sc create`，session 0） | 机器级 `systemctl enable`，**与用户登录无关** | 需要开机即起、多用户共享时 |

Windows 服务不适合这个程序：它跑在 session 0，看不到用户桌面会话，`%USERPROFILE%`、映射的
网络驱动器、`TEA_AGENT_HOME` 都可能不是你以为的那一个，而且注册要管理员权限。所以这里用
任务计划程序——在你自己的会话里跑，不弹 UAC，也不用存密码。

```bat
:: 注册：登录后 5 秒启动（自动找 build-mingw → build-gcc → build-msvc）
scripts\install-autostart.bat

:: 注册并立刻启动一次；后面的参数原样透传给 tea_agent
scripts\install-autostart.bat -Start --port 8282 --workspace C:\work\proj

:: 查看 / 停止 / 卸载
scripts\install-autostart.bat -Status
schtasks /End /TN tea_agent
scripts\install-autostart.bat -Uninstall

:: 只看会注册什么（生成 launcher 与任务 XML，不写进计划程序）
scripts\install-autostart.bat -DryRun
```

`pwsh -File scripts/install-autostart.ps1 ...` 与 `.bat` 等价。

**它做了什么**

- **隐藏窗口**：任务计划程序直接跑控制台程序会留一个黑窗口。脚本生成
  `%TEA_AGENT_HOME%\autostart\start-hidden.vbs`，由 `wscript.exe` 用 `SW_HIDE` 起进程——
  这正是「登录后悄悄跑起来的后台服务」应有的样子。`--no-console` 也会自动补上，
  日志照旧写 `~/.tea_agent/logs/tea_agent.log`。
- **崩溃重启**：VBS 在退出码非 0 时 3 秒后重拉；任务本身还配了 `<RestartOnFailure>`
  （1 分钟一次，最多 999 次），两者叠加接近 `Restart=always`。
- **单实例**：`<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>`，拨弄两下
  也不会起两个抢同一个端口的进程。
- **不超时**：`<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>`，服务不该被计划程序杀掉。
- **只用当前用户身份**：`<LogonType>InteractiveToken</LogonType>` + 正常权限，
  不需要管理员，也不保存密码。

**注意**

- 端口默认仍是 `127.0.0.1:8282`。要在局域网访问就加 `--host 0.0.0.0`，并先读「安全须知」。
  启动信息会把该网卡的活跃地址（如 `http://192.168.1.38:8282`）直接打印出来。
- 自启动用的参数固定在任务里。改参数就重新跑一次脚本（`/F` 会覆盖同名任务）。
- `-NoHide` 可让任务直接执行 exe（调试时想看到控制台就用它）。
- 注销/重启时进程随会话结束；下次登录由触发器重新拉起。
- Linux 上对应的做法是 `~/.config/systemd/user/tea_agent.service` +
  `systemctl --user enable --now tea_agent`，条件不合适（没有 systemd）时用 `@reboot` 的
  crontab 也能达到同样效果。

---


---

← 回到 [README](../README.md)
