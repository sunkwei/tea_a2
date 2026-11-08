<div align="center">

# tea_a2

**一个自包含的 C++ agent 运行时——一个二进制，带本地 HTTP/SSE 接口，让上层的 agent 能在设备上真的干活。**

把大模型 agent 的「手和脚」放到设备上;

<br/>

<a href="https://github.com/sunkwei/tea_a2"><img alt="repo" src="https://img.shields.io/badge/repo-sunkwei%2Ftea__a2-181717.svg"></a>
<img alt="c++" src="https://img.shields.io/badge/C%2B%2B-17-00599C.svg">
<img alt="build" src="https://img.shields.io/badge/build-CMake-064F8C.svg">
<img alt="deps" src="https://img.shields.io/badge/dependencies-vendored%20in%203rd%2F-2ea44f.svg">
<img alt="verified" src="https://img.shields.io/badge/verified-Windows%20x64%20%C2%B7%20GCC%20%2B%20MSVC-2ea44f.svg">
<img alt="runs" src="https://img.shields.io/badge/targets-Windows%20%C2%B7%20Linux%20%C2%B7%20macOS-6e7681.svg">

</div>

---

## 它是什么

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

| 模式 | 说明 |
| --- | --- |
| **主控/被控** | 设备上跑 `tea_a2`，上层 agent（也可以是级联的 tea_a2）通过 `/api/chat` 下发任务，用 SSE 收结果和工具执行过程 |
| **独立运行** | 设备或开发机上直接打开内置 Web UI，人机对话 |

## 三分钟跑起来

```bash
# 依赖：CMake ≥ 3.20 + 支持 C++17 的编译器（别的都不要）
cmake -B build -S . -DCMAKE_BUILD_TYPE=Release
cmake --build build -j8

# 准备配置（也可以跳过：第一次启动会进「配置模式」，在网页里填 API key）
mkdir -p ~/.tea_agent
cp provider.example.yaml ~/.tea_agent/provider.yaml
$EDITOR ~/.tea_agent/provider.yaml          # 填 api_url / api_key / model

# 启动（默认 127.0.0.1:8282），把当前目录注册为工作区
build/bin/tea_agent --workspace . --open
```

浏览器打开 <http://127.0.0.1:8282> 就是内置 UI：对话、文件浏览编辑、记忆、日志、设置、终端。

第一次跑（配置模式、快捷开始、各家 provider 的 key 怎么填、常用参数、curl 配方）见
**[使用手册](docs/tea_a2_manual.md)**；构建细节（交叉编译、Windows、常见错误）见
[构建与跨平台编译](docs/build.md)。

## ⚠️ 安全须知

**这个程序没有内置的认证**，而 `toolkit_exec` 能执行任意系统命令。所以：

- 默认绑定 `127.0.0.1`，只允许本机访问——**请保持这个默认值**；
- 用 `--host 0.0.0.0` 或放到公网之前，前面必须自己加一层认证（反向代理 + mTLS / token 校验 /
  VPN），否则等同于把设备的 shell 暴露出去；
- 带令牌的通道（`--api-token` 打开的 `/api/exec`、给「另一台 tea_a2」用的 `/api/peer/*`）
  **默认不存在**（不配 `--api-token` 就是 404）；
- 文件工具默认限制在工作区内；`--allow-outside-workspace`（或设置页的「全局」）会取消这个
  限制，慎用；
- 网页里的「终端」是这台机器的真实 shell，**以运行程序的那个用户的身份执行**——不需要时用
  `TEA_AGENT_SHELL=off` 关掉。

细节见 [部署](docs/deploy.md)、[级联](docs/peer.md)、[机群运维](docs/fleet.md)。

## 设计取舍

都是为「一个文件丢上设备就能跑」：

- **单二进制**：Web UI 在构建期被嵌入二进制，没有静态资源目录、没有前端构建链、没有 CDN。
- **零外部库**：nlohmann/json、cpp-httplib、yaml-cpp、mbedTLS 全部 vendored 在 `3rd/`，不依赖
  vcpkg / conan / 系统开发包。
- **跨平台同一套代码**：路径、文件 I/O、进程、SIGPIPE 都按平台封装，没有 `std::filesystem`。
- **内置网页终端**：这台机器上的**真实 shell**（Linux 上是 bash、Windows 上是 PowerShell），跑在
  pty / ConPTY 上；浏览器里直接敲命令，**不经过模型**，也不经过 agent 的工具边界。
- **能接别的程序（MCP）**：`mcp.yaml` 里列出的 MCP 服务器会被启动起来，它们的工具直接进模型的
  工具表——比如 Windows 上的
  [uiautomation-mcp](https://github.com/locomorange/uiautomation-mcp)，让模型真的去操作这台
  机器上的窗口。

## 还能做什么

| | |
| --- | --- |
| **任务队列**（`sched/task_server.py`） | 排队、调度、交互挂起、导出、事件流；微信 / Telegram 前端。见 [任务队列](docs/tasks.md) |
| **级联**（`toolkit_peer`） | 一台 tea_a2 直接控制另一台。见 [级联](docs/peer.md) |
| **机群**（`sched/fleet_server.py`） | 一台服务器管 40 台设备：台账、每日采集、灰度升级、审计。见 [机群运维](docs/fleet.md) |
| **看门狗 / Router**（`sched/watchdog.py`、`sched/router.py`） | 在外面盯着卡住的任务与跑偏的 worker；把分开的工作区会话串成一条线索。见 [Router 与 Watchdog](docs/router-watchdog.md) |
| **扩展层**（`sched/a2_ext_py/`） | 一个 .py 文件就是一件新工具，不用改 C++、不用发版，模型自己也能写。现成三件套：**截图 / 鼠标 / 键盘**（Windows 与 Linux 都能用）。见 [扩展层](docs/a2_ext_py.md) |
| **与其他应用对接**（`sched/mail_watch.py` 等） | 邮件/日历/告警自己走进来，**只在需要你参与时通知你**。见 [对接](docs/integrations.md) |
| **配置同步**（`sched/pan_sync.py`） | 多台机器共用一份**加密**的配置。见 [网盘同步配置](docs/pan-sync.md) |
| **语音输入**（`sched/asr_server.py`） | 本地 SenseVoiceSmall 转写（也可换成任何 OpenAI 兼容的转写服务）。见 [配置](docs/config.md#语音输入asr-段) |

## 文档

| | |
| --- | --- |
| **开始用** | |
| [使用手册](docs/tea_a2_manual.md) | **命令清单**：命令行参数、curl 配方、队列命令、微信命令、脚本、环境变量 |
| [用起来](docs/using.md) | 界面导览：配置对话框、工作区、主题、分叉、图片、搜索 |
| [配置](docs/config.md) | `provider.yaml`、额度与搜索抓取、语音输入、MCP、按工作区覆盖 |
| [工作区与运行时存储](docs/workspace.md) | `.tea_agent_run/` 里有什么、并发、agent.md、销毁、导出 |
| **跑起来 / 运维** | |
| [构建与跨平台编译](docs/build.md) | 依赖、本机构建、交叉编译（aarch64 / Windows）、产物校验 |
| [部署](docs/deploy.md) | 嵌入式设备、静态单文件、TLS、systemd 单元、Windows 自启 |
| [设备运维手册](docs/device_ops_manual.md) | 给运维/售后的图文手册：设备上的 a2（配 key → 建工作区 → 排错表） |
| [多级协同与机群运维](docs/fleet.md) | 一台服务器管 40 台设备：台账、灰度升级、审计、让设备自己判断 |
| [级联](docs/peer.md) | `peers:` 配置、`toolkit_peer`、`/api/peer/*`、安全边界 |
| **接口与内部** | |
| [HTTP 接口](docs/api.md) | 全部接口、SSE 事件、原语层（给脚本用） |
| [任务队列](docs/tasks.md) | 排队/调度/交互/导出、微信前端、需求逐条对照（队列自己也在 `GET /api/doc` 上服务这一页） |
| [内置工具与上下文](docs/tools.md) | 每个工具、文件访问范围、上下文预算、轨迹 |
| [MCP](docs/mcp.md) | 把别的程序接进来当工具：配置、接口、与 uiautomation-mcp 联调 |
| [与其他应用对接](docs/integrations.md) | 它调别人（MCP）/ 别人调它（HTTP、级联）/ 别人那边发生事（watcher） |
| [Router 与 Watchdog](docs/router-watchdog.md) | 触发器入口、跨会话的线索上下文、在外面盯着卡住的任务 |
| [扩展层 a2_ext_py](docs/a2_ext_py.md) | 模块（一个 .py 文件）加载后就是 a2 的工具 |
| [机制层与策略层](docs/mechanism-strategy.md) | a2 当机制层、a2_task 当策略层：职责边界与验收标准 |
| **改这个仓库** | |
| [源码结构](docs/source.md) | 每个文件干什么、第三方依赖 |
| [测试与验证状态](docs/testing.md) | 怎么跑、每个测试钉住了什么、哪些平台实测过 |
| [开发笔记](tea_a2_agent.md) | **给改这个仓库的人**：踩过的坑、命令、约定 |
| [Python 侧脚本](sched/README.md) | `sched/` 目录说明：队列、聊天前端、机群、语音转写、邮件看门狗、网盘同步 |
| [百度网盘同步配置](docs/pan-sync.md) | 加密后传到网盘、冲突不覆盖、删除不传播、备份 |

## License

本项目代码采用 **MIT** 许可证，见 [`LICENSE`](LICENSE)；`3rd/` 下的依赖各按其原许可
（细节见 [docs/license.md](docs/license.md)）。

Copyright (c) 2026 sunkwei
