<div align="center">

# tea_a2

**A tiny C++ agent runtime for embedded devices — one self-contained binary that exposes a local
HTTP/SSE API, so a larger agent can drive real work on the device.**

把大模型 agent 的「手和脚」放到设备上：设备只跑 `tea_a2`，推理与决策留在远端。

<br/>

<a href="https://github.com/sunkwei/tea_a2"><img alt="repo" src="https://img.shields.io/badge/repo-sunkwei%2Ftea__a2-181717.svg"></a>
<img alt="c++" src="https://img.shields.io/badge/C%2B%2B-17-00599C.svg">
<img alt="build" src="https://img.shields.io/badge/build-CMake-064F8C.svg">
<img alt="deps" src="https://img.shields.io/badge/dependencies-vendored%20in%203rd%2F-2ea44f.svg">
<img alt="verified" src="https://img.shields.io/badge/verified-Windows%20x64%20%C2%B7%20GCC%20%2B%20MSVC-2ea44f.svg">
<img alt="runs" src="https://img.shields.io/badge/targets-Windows%20%C2%B7%20Linux%20%C2%B7%20macOS-6e7681.svg">

</div>

---
## 这个项目解决什么问题

嵌入式设备上通常跑不了通用 agent，「在设备上干活」需要的是本地能力：执行命令、读写文件、
记住上下文。`tea_a2` 就是这部分能力的最小实现：

```
┌──────────────────────┐        HTTP / SSE (JSON)        ┌───────────────────────────┐
│  Remote brain        │  ───────────────────────────▶  │  Embedded device          │
│  (LLM agent /        │                                 │  ┌─────────────────────┐  │
│   orchestrator /     │   POST /api/chat                │  │ tea_a2 (single exe) │  │
│   云端 workflow)     │   ◀─── content_delta / tools ── │  │  · agent loop       │  │
└──────────────────────┘                                 │  │  · tools            │  │
                                                         │  │  · workspaces       │  │
                                                         │  │  · built-in Web UI  │  │
                                                         │  └─────────┬───────────┘  │
                                                         │            │              │
                                                         │     本地文件 / 本地命令    │
                                                         └───────────────────────────┘
```

两种用法：

| 模式 | 说明 |
| --- | --- |
| **被控端（主要用途）** | 设备上只跑 `tea_a2`，上层 agent 通过 `/api/chat` 下发任务，用 SSE 收结果和工具执行过程 |
| **独立运行** | 设备或开发机上直接打开内置 Web UI，人机对话；配置里直接填 LLM 的 `api_url` / `api_key` |

设计上的取舍都是为「一个文件丢上设备就能跑」：

- **单进制**：Web UI 在构建期被嵌入二进制，没有静态资源目录、没有前端构建链、没有 CDN。
- **零外部库**：nlohmann/json、cpp-httplib、yaml-cpp、mbedTLS 全部 vendored 在 `3rd/`，不依赖 vcpkg / conan / 系统开发包。
- **不依赖 OpenSSL / zlib / Python / Perl**：TLS 由 mbedTLS 承担；构建期没有任何脚本语言参与。
- **跨平台同一套代码**：路径、文件 I/O、进程、SIGPIPE 都按平台封装，没有 `std::filesystem`。
- **内置网页终端**：这台机器上的**真实 shell**（Linux 上是 bash、Windows 上是 PowerShell），跑在
  pty / ConPTY 上；浏览器里直接敲命令，**不经过模型**，也不经过 agent 的工具边界。
## ⚠️ 安全须知

**这个程序由 tea_agent 项目创建 0.1 版本；之后版本由 a2 自己进化完成，模型使用 deepseek-flash **

**这个程序没有任何认证机制。** `toolkit_exec` 可以执行任意系统命令，所以：

- 默认绑定 `127.0.0.1`，只允许本机访问——请保持这个默认值。
- 用 `--host 0.0.0.0` 或放到公网之前，必须自己在前面加一层认证（反向代理 + mTLS / token 校验 /
  VPN），否则等同于把设备的 shell 暴露出去。
- 只应部署在受信任的网段，或通过隧道访问。
- 文件工具默认限制在工作区内；`--allow-outside-workspace`（或设置页的「全局」）会取消这个限制，
  慎用——放宽后任何能访问这个端口的人都能读写整台机器上的文件。
- 网页里的「终端」是这台机器的真实 shell，等同于把 `toolkit_exec` 交给每一个能访问这个端口的人。
  不需要时用 `TEA_AGENT_SHELL=off` 把它关掉（页面上的入口会一起消失）。

---

---

## 快速开始

### 依赖：只有编译器和 CMake

| 角色 | 要求 |
| --- | --- |
| 构建系统 | CMake ≥ 3.20（3.x / 4.x 均可） |
| C++ 编译器 | 支持 C++17（已验证 GCC 6.5 / MinGW GCC 16.2 / MSVC 19.51） |
| C 编译器 | C99，仅用于编译 mbedTLS |
| 生成器 | Ninja / Unix Makefiles / NMake / Xcode |
| 链接库 | 系统自带：Windows `winhttp` `ws2_32` `advapi32`；POSIX `pthread` |

不需要 OpenSSL、zlib、vcpkg、conan、Python、Perl。

### 构建

```bash
# Linux / macOS
cmake -B build -S . -DCMAKE_BUILD_TYPE=Release
cmake --build build -j8

# Windows（MSVC）
cmake -B build-msvc -S . -A x64
cmake --build build-msvc --config Release
```

更多构建方式（不装 Python 的等价写法、Windows 运行中构建、交叉编译、产物校验、常见错误
对照表）见 [构建与跨平台编译](docs/build.md)。

### 跑起来

```bash
# 准备配置（也可以跳过这一步，见下面的「首次运行」）
mkdir -p ~/.tea_agent
cp provider.example.yaml ~/.tea_agent/provider.yaml
$EDITOR ~/.tea_agent/provider.yaml          # 填 api_url / api_key / model

# 启动（默认 127.0.0.1:8282），把当前目录注册为工作区
build/bin/tea_agent --workspace . --open
```

**首次运行**：如果 `$HOME/.tea_agent/provider.yaml` 不存在，服务不会退出，而是以「配置模式」
启动——浏览器打开页面后会被一个不可跳过的对话框挡住。对话框里有两条路：

1. **快捷开始：只填 API key**（最省事，光标已经在这个输入框里）。下拉里选 provider、
   粘贴 key、回车即可：服务端据此生成一份只含该 provider 的 `provider.yaml`，按与上传
   完全相同的流程校验、落盘（POSIX 上权限 `0600`，因为它含密钥）、热加载。

   | provider | 粘贴的 key | 默认模型 | 生成的配置里还有什么 |
   | --- | --- | --- | --- |
   | **DeepSeek** | `sk-…` | `deepseek-flash` | 另附 `deepseek-chat` / `deepseek-reasoner` 备用 |
   | **MiniMax** | M Plan 的 `sk-cp-…` | `MiniMax-M3.1-Flash`（1M 上下文） | 额度徽章（`supports_usage` + `minimax_token_plan`），顶栏直接显示还剩多少 |

   MiniMax 走 `https://api.minimax.cn/v1`（全球站 `https://api.minimax.io/v1` 同样可用，
   改 `api_url` 一行即可）；key 形状和说明会随下拉一起变，不用猜。

2. **上传 / 粘贴完整配置**：点击（或拖入）`provider.yaml`，或直接把内容粘到文本框里，点
   「上传并应用」。

已经配好之后想换一整套 provider，「设置」页的「模型提供方」一节里有 **重置 provider**：
它打开的就是上面这个对话框（快捷 key 通道 + 上传 + 粘贴，一个都不少），但会先说明风险——
现有的 `provider.yaml` 会被覆盖，**旧的 provider、模型和 API key 找不回来**——而且这个对话框
**可以取消**（首次运行那个是不可关闭的：那时没有配置，不配就没法用）。新配置仍然先校验再落盘，
不合法就拒绝写入，不会留下半个文件；有轮次正在跑时也会被拒绝（409）。

两条路都是**先校验再落盘**（校验失败不会覆盖已有文件），成功后页面自动刷新，状态栏从
「未配置」变成「已连接」，可以直接开始对话。快捷通道生成的配置就是普通的 `provider.yaml`，
之后随时可以在「文件」页或编辑器里改（加 provider、换默认模型、打开 DeepSeek 的网页工具等）。
命令行会打印 `starting in setup mode` 和提示地址，方便无 GUI 环境照常启动后再上传。

注意只有**文件不存在**才进入配置模式；文件存在但 YAML 语法错误仍然会直接报错退出——
那种情况下静默启动只会把问题藏起来。

浏览器打开 <http://127.0.0.1:8282> 就是内置 UI：对话、文件浏览编辑、记忆、日志、设置。

（界面的导览在 [用起来](docs/using.md)；要直接敲命令，看
[使用手册](docs/manual.md)。）

```bash
# 常用参数
--host 0.0.0.0        # 监听所有网卡（暴露到网络前务必读「安全须知」）
--port 8282           # 端口
--workspace <path>    # 注册工作区，可重复
--workspace-root <d>  # 注册 <d> 下所有一级子目录
--provider/--model    # 临时换用别的 provider / 模型（默认是第一个）
--log-level debug     # 写进日志文件的级别：trace|debug|info|warn|error|off
--console-level warn  # 回声到控制台的级别（默认 warn，见下）
--no-console          # 控制台不回声任何日志（--quiet 同义，且连启动横幅也省掉）
--help                # 全部参数
```

**控制台默认只打印 WARNING / ERROR。** 服务是长期挂在终端里的，每次请求的 `INFO`
（`chat:`、`server:`、`toolkit_*` …）都往终端刷会把真正要紧的告警顶出屏幕；而排查问题时
又确实需要这些细节。所以两个阈值是分开的：

| 开关 | 作用 | 默认 |
| --- | --- | --- |
| `--log-level` | 写进 `logs/tea_agent.log` 与工作区 `logs/agent.log` 的级别 | `info` |
| `--console-level` | 回声到终端的级别 | `warn` |
| `--no-console` / `--quiet` | 终端彻底不回声（`--quiet` 连横幅也不打印） | 关 |

完整日志始终在文件里，`--console-level debug` 可以在终端跟着看一次复现过程。写成不认识
的级别名会打印一行 `warning: … is not a level; using warn` 并按 `warn` 处理，而不是
静默退回 `info` 把终端重新刷满。启动横幅、`error:` 这类面向人的提示不受级别影响。

---

## 这台机器上跑着的三个服务

| 单元 | 是什么 | 端口 |
| --- | --- | --- |
| `a2.service` | agent —— 工作区通道（内置网页 UI） | 8383 |
| `a2_task.service` | 任务队列（Python）：排队 / 执行 / 状态 / 结果 | 8283 |
| `a2_wechat.service` | 队列的微信前端 | — |
| `a2_telegram.service` | 队列的 Telegram 前端 | — |

单元文件在 `scripts/` 里，是**跟机器无关的模板**（路径用 `%h`，也就是运行单元的用户的主目录）。
装的时候用 `scripts/install-units.sh [仓库路径]`：它把单元装到 `~/.config/systemd/user/` 并把仓库
路径换成本机的。详见 [部署](docs/deploy.md)。

## 文档

| | |
| --- | --- |
| [使用手册](docs/manual.md) | **命令清单**：命令行参数、curl 配方、队列命令、微信命令、脚本、环境变量 |
| [用起来](docs/using.md) | 界面导览：配置对话框、工作区、主题地址、分叉、图片 |
| [任务队列](docs/tasks.md) | 排队/调度/交互/导出、微信前端、需求逐条对照 |
| [构建与跨平台编译](docs/build.md) | 依赖、本机构建、交叉编译（aarch64 / Windows）、产物校验 |
| [部署](docs/deploy.md) | 嵌入式设备、静态单文件、TLS 证书、Windows 自启、systemd 单元 |
| [HTTP 接口](docs/api.md) | 全部接口、SSE 事件、原语层（给脚本用） |
| [内置工具与上下文](docs/tools.md) | 每个工具、文件访问范围、上下文预算、轨迹 |
| [工作区与运行时存储](docs/workspace.md) | `.tea_agent_run/` 里有什么、并发、agent.md、system prompt、销毁、导出 |
| [配置](docs/config.md) | `provider.yaml`、额度与搜索抓取、按工作区覆盖、请求头 |
| [任务队列](docs/tasks.md) | 队列服务、微信 / Telegram 那一端、网页、使用手册、逐任务授权目录 |
| [任务队列](docs/tasks.md) | 队列服务、微信那一端、网页、使用手册、逐任务授权目录 |
| [源码结构](docs/source.md) | 每个文件干什么、第三方依赖 |
| [测试与验证状态](docs/testing.md) | 怎么跑、每个测试钉住了什么、哪些平台实测过 |

## License

本项目代码采用 **MIT** 许可证，见根目录 [`LICENSE`](LICENSE)。

Copyright (c) 2026 sunkwei

`3rd/` 下的依赖各自遵循其原有许可证，**不被 MIT 覆盖**，详见各目录内的 `LICENSE` 文件：

| 依赖 | 许可证 |
| --- | --- |
| nlohmann/json | MIT |
| cpp-httplib | MIT |
| yaml-cpp | MIT |
| mbedTLS | Apache-2.0（双许可，另有 GPL-2.0） |
| miniz | MIT |

分发二进制时注意：mbedTLS 的 Apache-2.0 条款要求保留其许可证与声明，`3rd/mbedtls/LICENSE`
需随发行物一起带上。

**许可证兼容性**（新增依赖时先看这一段）：本项目是 MIT，`3rd/` 里 nlohmann/json、cpp-httplib、
yaml-cpp、miniz 也都是 MIT，互相兼容、可直接静态链接进同一个二进制，只需保留各自的版权声明。
两处需要留意，但都**不构成冲突**：

- **mbedTLS** 是 Apache-2.0 / GPL-2.0 双许可。这里按 **Apache-2.0** 使用：Apache-2.0 与 MIT
  单向兼容（MIT 代码可以并入 Apache-2.0 分发），只要保留 `3rd/mbedtls/LICENSE` 与声明即可；
  **不要**选 GPL-2.0 那一支——GPL-2.0 与 MIT 组合会把整个发行物变成 GPL，那才是真冲突。
- **CA 证书包**（`3rd/ca-bundle/cacert.pem`）是 MPL-2.0。它是**数据**而不是链接进程序的代码，
  随发行物附带并保留出处即可，不影响二进制的 MIT 许可。

也就是说：新增依赖时优先选 MIT / BSD / zlib / Apache-2.0；**GPL / AGPL 一律不要**（会传染整个
二进制），LGPL 需要能保证重新链接（静态链接下要提供目标文件），也不建议。
