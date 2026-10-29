# 源码结构与第三方依赖

## 源码结构

```
src/
  main.cpp                  CLI、信号、启动横幅
  config.{h,cpp}            provider.yaml / default / headers 解析
  message.{h,cpp}           消息模型（wire 格式 + 存储格式）
  llm.{h,cpp}               OpenAI 兼容客户端：SSE、tool_calls 聚合、重试
  usage.{h,cpp}             订阅额度查询：provider 专用端点 + 解析器（minimax_token_plan）
  model_discovery.{h,cpp}   查 provider 有哪些模型（/v1/models）+ 默认配置推断
  provider_yaml.{h,cpp}     原地改写 provider.yaml 里某个 provider 的 models: 块
  agent.{h,cpp}             多轮工具调用主循环、预算与滚动摘要、system prompt
  prompt_queue.h            轮次进行中的排队消息（线程安全 FIFO，每轮采纳一条）
  tools.{h,cpp}             工具注册表与内置工具（exec/file/memory/agent_doc/load_image/todo/task/
                            search/fetch/fleet/peer——peer 是级联：问另一台 tea_a2 自己的 agent）
  mcp.{h,cpp}               MCP 客户端：起一个 MCP 服务器进程、stdio 上的 JSON-RPC
                            （initialize / tools/list / tools/call）、把它的工具并进工具表
  command.{h,cpp}           命令执行（Job Object / 进程组、超时、编码归一）
  workspace.{h,cpp}         多工作区、.tea_agent_run/、会话/记忆/产出文件、索引；
                            工作区搜索（文件名 + 内容，完全字符串匹配，见 [api.md](api.md#工作区搜索get-ap-workspacesidsearch)）；
                            并发写用进程级锁 + 原子替换（见 [并发](workspace.md#并发同一个工作区可以同时跑好几轮)）
  html_export.{h,cpp}       会话 → 自包含 HTML（内联 CSS/图片、可导入的 JSON 区块）
  server.{h,cpp}            HTTP 路由、SSE 泵、内置资源；/api/peer/* 是机器对机器的
                            令牌版镜像（级联的对端通道）
  shell.{h,cpp}             网页终端的会话/缓冲/能力探测（不经过模型）
  shell_posix.cpp            Linux/macOS：pty（posix_openpt + fork + exec）
  shell_win32.cpp            Windows：ConPTY（CreatePseudoConsole）+ PowerShell
  http.{h,cpp}              传输分派（socket+TLS / WinHTTP，自动回退）
  http_socket.cpp           HTTP/1.1 over TCP（+ 可选 TLS）
  http_winhttp.cpp          Windows WinHTTP 后端
  tls.{h,cpp}               mbedTLS 客户端封装
  net_compat.h              SIGPIPE / send 标志的平台差异
  platform_fs.cpp           路径与文件 I/O（Win32 宽字符 / POSIX，全 UTF-8）+ write_file_atomic
  env.{h,cpp}               设备环境探测（OS/架构/时区/CPU/内存/磁盘/shell）
  output_filter.{h,cpp}     工具输出按内容属性削减（build/test/通用）
  util.{h,cpp} log.{h,cpp}  字符串、时间、编码、日志
  version.{h,cpp}           版本号（唯一手改的真相）；commit 在二进制旁边的
                            tea_agent.buildinfo 里，程序启动时读它
web/                        index.html / tokens.css / app.css / app.js（构建期嵌入）
provider.example.yaml        provider.yaml 的模板（模型提供方、search/asr 段）
tea_agent.example.yaml         tea_agent.yaml 的模板（设置：输出上限、超时、压缩、peers…）
mcp.yaml.example            mcp.yaml 的模板（MCP 服务器：程序、参数、环境变量）
docs/                       文档：构建、部署、接口、工具、配置、队列、源码、测试
tools/                      embed_assets.cpp、embed_ca_bundle.cpp、host_embed/（宿主工具子项目）、mock_*.mjs、
                            merge_work_area.py（把一份 .tea_agent_run 合并进另一份）
tests/mock_mcp_server.py    一个假的 MCP 服务器（stdio 上的 JSON-RPC），用来测 MCP 客户端
tests/e2e.ps1              端到端测试（157 项）；tests/sse_probe.mjs 严格流客户端
scripts/                    build.sh / build.bat / build-mingw.bat / vcenv.bat /
                            install-autostart.ps1（+ .bat）= Windows 登录自启动
sched/task_server.py        任务服务（Python，可选）：任务队列 + 额度闸门 + 事件流
sched/fleet.py               机群引擎（Python，可选）：设备台账 + 并发采集 + 批量作业
                              （灰度/分批/失败阈值/回滚）+ 审计日志 + 每日快照与报表
sched/fleet_server.py        机群管控服务（Python，可选）：把上面的引擎变成 HTTP API，
                              供服务器级 a2（toolkit_fleet）、机器人和定时任务调用
sched/tea_api.py            HTTP 客户端封装（纯标准库）：Tea 说 agent，Queue 说任务队列
sched/wechat_bot.py          微信那一端：iLink 协议（扫码登录、长轮询、CDN 媒体）
sched/telegram_bot.py        Telegram 那一端：Bot API（长轮询、getFile/sendDocument、chat 白名单）
CMakePresets.json           msvc / mingw / posix / arm64-linux / win64-mingw-cross 预设
cmake/toolchains/           aarch64-linux-gnu.cmake / mingw-w64-x86_64.cmake（交叉编译入口）
cmake/version_git_hash.cmake 生成 tea_agent.buildinfo（写在二进制旁边，不动源码树）
cmake/free-running-exe.cmake   运行中也能链接：先把旧 exe 改名让位
3rd/                        第三方依赖
```

**版本号与 commit hash**：`src/version.cpp` 是唯一手改的版本真相（`kVersionString`），
CMake 从它解析 `project(VERSION)`，所以「二进制报什么版本」和「构建日志写什么版本」不会各说各话。

版本串故意放在 `.cpp` 里而不是头文件：它只被 `version_string()` 用到，而 `version.h` 有六个
翻译单元包含——放在头文件里，改一次版本要重编六个文件（`server.cpp` 也在内），放在 `.cpp`
里只重编一个。CMake 那边把它登记成 configure 依赖，所以改版本会触发重新配置。

commit 则是**构建期元数据**，它既不在源码里、也不在二进制里：构建把它写进二进制旁边的
`tea_agent.buildinfo`（`cmake/version_git_hash.cmake`），程序启动时读这个文件并在横幅和
`/api/health` 里报出来。

```
# tea_agent.buildinfo（与二进制放在一起）
version=0.16.1
commit=678cd2c1f014        # 工作区有未提交改动时是 <hash>-dirty
```

好处有三层：

- **构建不再改源码树**。以前是把 hash 写回 `src/version.cpp`，代价是每次构建都把工作区弄脏，
  构建完立刻 `git status` 什么也看不出来；而且源码树永远处于「-dirty」（连它自己那次改动也算脏），
  旧脚本只好特判自己写的那个文件；native 与交叉两个构建目录还会抢同一个文件。
- **二进制可复现**。既然编译进去的构建数据已经没有了，再让链接器别把构建时间戳进 PE 头，
  同一个 commit 两次构建就得到**逐字节相同**的二进制（MinGW 用
  `-Wl,--no-insert-timestamp`，MSVC 用 `/Brepro`）。要核对发布件是不是某个 commit 构建的，
  比对 `tea_agent.buildinfo` 即可。
- **单文件部署不撒谎**。只把 `tea_agent.exe` 拷到设备上的话，旁边没有 buildinfo，于是没有
  provenance 可显示——横幅就只印版本号，而不是假装自己来自某个 commit。

**为什么不用 `std::filesystem`**：Windows 上它的窄字符转换走 ANSI 代码页，非 ASCII 路径会抛
`Illegal byte sequence`（MinGW 下尤其明显）；同时 GCC 6.5 只有 `std::experimental::filesystem`。
所以路径与文件 I/O 直接基于 Win32 宽字符 API / POSIX，全流程 UTF-8。

**为什么支持 GCC 6.5**：代码刻意不使用 C++17 专属语言特性，可用
`-DTEA_AGENT_CXX_STANDARD=14` 复核（这条命令会以 C++14 模式编译全部代码）。

---

## 第三方依赖（全部位于 `3rd/`）

| 目录 | 版本 | 用途 | 许可证 |
| --- | --- | --- | --- |
| `3rd/nlohmann_json/` | 3.11.3 | JSON | MIT |
| `3rd/cpp-httplib/` | 0.61.0 | HTTP 服务端 | MIT |
| `3rd/yaml-cpp/` | 0.8.0 | YAML 解析 | MIT |
| `3rd/mbedtls/` | 2.28.9 LTS | TLS 客户端（跨平台） | Apache-2.0 / GPL-2.0（见 `3rd/mbedtls/LICENSE`） |
| `3rd/miniz/` | 3.1.2 | deflate + ZIP 打包（工作区归档下载） | MIT |
| `3rd/ca-bundle/` | curl CA | 根证书 | MPL-2.0 |

各目录下的 `UPSTREAM_VERSION.txt` 记录上游版本（不叫 `VERSION`：Windows 文件名不区分大小写，
会遮蔽标准头 `<version>`），并记下上游 URL 与 sha256。yaml-cpp 0.8.0 依赖 `<string>` 间接引入
`<cstdint>`，用 `-include cstdint` 解决，不修改上游源码。

miniz 只取上游 release 里的**合并版两个文件**（`miniz.c` + `miniz.h`，逐字节未改），作为独立的
静态库目标 `tea_miniz` 编译：它是 C，所以顶层 `project()` 声明了 `LANGUAGES C CXX`；它的告警
不是我们的，所以只对这个目标关掉（MSVC `/W0`，其它 `-w`）。

---


---

← 回到 [README](../README.md) · 命令清单见 [使用手册](manual.md)
