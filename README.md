<div align="center">

# tea_a2

**A tiny C++ agent runtime for embedded devices — one self-contained binary that exposes a local
HTTP/SSE API, so a larger agent can drive real work on the device.**

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
| **主控/被控** | 设备上跑 `tea_a2`，上层 agent (也可以是级联的 tea_a2) 通过 `/api/chat` 下发任务，用 SSE 收结果和工具执行过程 |
| **独立运行** | 设备或开发机上直接打开内置 Web UI，人机对话 |
| **级联** | 一台 `tea_a2` 直接控制另一台（`toolkit_peer`）：问它自己的 agent、让它跑命令、看它的状态。见 [级联](docs/peer.md) |
| **看门狗** | 别人那边发生的事自己走进来（邮件/日历/告警）：`sched/mail_watch.py` 判读、归档、起草，**只在需要你参与时通知你**。见 [与其他应用对接](docs/integrations.md) |

设计上的取舍都是为「一个文件丢上设备就能跑」：

- **单进制**：Web UI 在构建期被嵌入二进制，没有静态资源目录、没有前端构建链、没有 CDN。
- **零外部库**：nlohmann/json、cpp-httplib、yaml-cpp、mbedTLS 全部 vendored 在 `3rd/`，不依赖 vcpkg / conan / 系统开发包。
- **跨平台同一套代码**：路径、文件 I/O、进程、SIGPIPE 都按平台封装，没有 `std::filesystem`。
- **内置网页终端**：这台机器上的**真实 shell**（Linux 上是 bash、Windows 上是 PowerShell），跑在
  pty / ConPTY 上；浏览器里直接敲命令，**不经过模型**，也不经过 agent 的工具边界。
- **能接别的程序（MCP）**：`mcp.yaml`（和 provider.yaml 同目录）里列出的 MCP 服务器会被启动
  起来，它们的工具直接进模型的工具表——比如 Windows 上的
  [uiautomation-mcp](https://github.com/locomorange/uiautomation-mcp)，让模型真的去操作这台
  机器上的窗口。
## ⚠️ 安全须知

**这个程序由 tea_agent 项目创建 0.1 版本；之后版本由 tea_a2 自己进化完成，模型使用 deepseek-flash/minimax-m3.1-flash**

**这个程序没有任何认证机制。** `toolkit_exec` 可以执行任意系统命令，所以：

- 带令牌的通道（`--api-token` 打开的 `/api/exec`、以及给「另一台 tea_a2」用的
  `/api/peer/*`）是**仅有的**带令牌的部分，而且**默认关闭**：不配 `--api-token` 时
  这些路由根本不存在（404）。但只要用了 `--host 0.0.0.0`，设备上的其它 API
  （`/api/chat` 之类）也一起暴露了——`toolkit_exec` 能执行任意命令。
  要么把设备放在受控的 VLAN 里，要么在前面加一层反向代理（nginx + token / mTLS）。
  见 [多级协同与机群运维](docs/fleet.md) 和 [级联](docs/peer.md)。

- 默认绑定 `127.0.0.1`，只允许本机访问——请保持这个默认值。
- 用 `--host 0.0.0.0` 或放到公网之前，必须自己在前面加一层认证（反向代理 + mTLS / token 校验 /
  VPN），否则等同于把设备的 shell 暴露出去。
- 只应部署在受信任的网段，或通过隧道访问。
- 文件工具默认限制在工作区内；`--allow-outside-workspace`（或设置页的「全局」）会取消这个限制，
  慎用——放宽后任何能访问这个端口的人都能读写整台机器上的文件。
- 网页里的「终端」是这台机器的真实 shell，**以运行 tea_agent 的那个用户的身份执行**——它是
  运维用的入口，权限边界就是这个用户的权限，接口本身不再做鉴权，所以能访问这个端口的人
  就能用它（和 `toolkit_exec` 一样）。不需要时用 `TEA_AGENT_SHELL=off` 把它关掉（页面上的
  入口会一起消失）。

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
启动。对话、文件、记忆、日志这几项**要经过模型**，所以暂时置灰；**终端**和**设置**照常
可用（终端就是这台机器的 shell，不经过模型）——设备不是死的。到「设置」页里配置好
provider.yaml，这几项就都使能了。配置对话框里有两条路：

1. **快捷开始：只填 API key**（最省事，光标已经在这个输入框里）。下拉里选 provider、
   粘贴 key、回车即可：服务端据此生成一份只含该 provider 的 `provider.yaml`，按与上传
   完全相同的流程校验、落盘（POSIX 上权限 `0600`，因为它含密钥）、热加载。

   | provider | 粘贴的 key | 默认模型 | 生成的配置里还有什么 |
   | --- | --- | --- | --- |
   | **DeepSeek** | `sk-…` | `deepseek-flash` | 另附 `deepseek-chat` / `deepseek-reasoner` 备用 |
   | **MiniMax** | M Plan 的 `sk-cp-…` | `MiniMax-M3.1-Flash`（1M 上下文） | 控制台链接（`usage_console_url`，点开看额度，**不发请求**） |
   | **小米 MiMo** | 按量付费 `sk-…`，或 Token Plan `tp-…` / `ttp-…` | `mimo-v2.6-flash`（1M 上下文） | 另附 `mimo-v2.6-pro`；控制台链接（MiMo 没有能用 key 查的额度接口）；`reasoning_content: keep` |

   **生成的配置不查额度**（`supports_usage` 是关的）。这份文件通常是一份配置发给一大片设备：
   每台设备都去问一次「订阅还剩多少」，就是一个账号上成千上万个请求——这种模式最容易把账号
   弄进风控。单台机器上要这个徽章，把生成的 yaml 里那两行注释放开即可（key 得是 M Plan 订阅 key）。
   自己**上传**的 `provider.yaml` 不受这条限制：那份是你写的，按原样生效。

   MiniMax 走 `https://api.minimax.cn/v1`（全球站 `https://api.minimax.io/v1` 同样可用，
   改 `api_url` 一行即可）；key 形状和说明会随下拉一起变，不用猜。

   小米 MiMo 有**两个网关，key 决定走哪个**：`sk-…` 是按量付费（`https://api.xiaomimimo.com/v1`），
   `tp-…` / `ttp-…` 是 Token Plan 订阅（`https://token-plan-cn.xiaomimimo.com/v1`）——两者不能混用，
   用错了就是 401。生成的配置里还带 `reasoning_content: keep`：v2.6 模型默认开启思考，
   小米的文档要求思考模式下把上一轮的 `reasoning_content` 原样带回去，程序本来就带，
   写进文件是为了不依赖「哪个端点要」的猜测。

2. **上传 / 粘贴完整配置**：点击（或拖入）`provider.yaml`，或直接把内容粘到文本框里，点
   「上传并应用」。

对话框里还有一个按钮：

- **「重新检测」**：让服务端**重新读一遍磁盘上的** `provider.yaml`。设备上常常是先把文件
  写好（或从别处 scp 过来）再让它生效——光刷新网页没用，配置在服务端的内存里。

已经配好之后想换一整套 provider，「设置」页的「模型提供方」一节里有 **重置 provider**：
它打开的就是上面这个对话框（快捷 key 通道 + 上传 + 粘贴，一个都不少），但会先说明风险——
现有的 `provider.yaml` 会被覆盖，**旧的 provider、模型和 API key 找不回来**——而且这个对话框
**可以取消**。新配置仍然先校验再落盘，
不合法就拒绝写入，不会留下半个文件；有轮次正在跑时也会被拒绝（409）。

同一节里，每个 provider 还有个 **更新模型** 按钮：问这个 provider 现在提供哪些模型
（服务端用 provider 自己的 key 去问 `{api_url}/v1/models`），把每个模型的上下文窗口、输出
上限和「能不能用工具 / 思考 / 看图」列出来，标上每个数字是从哪来的（上游说的、本机配置里
的、内置表里的、还是按名字猜的）。改完确认，写回这个 provider 的 `models:` 块——别的
provider 和注释原样保留，配置随即热加载生效，不用重启。

两条路都是**先校验再落盘**（校验失败不会覆盖已有文件），成功后页面自动刷新，状态栏从
「未配置」变成「已连接」，可以直接开始对话。快捷通道生成的配置就是普通的 `provider.yaml`，
之后随时可以在「文件」页或编辑器里改（加 provider、换默认模型、打开 DeepSeek 的网页工具等）。
命令行会打印 `starting in setup mode` 和提示地址，方便无 GUI 环境照常启动后再上传。

注意只有**文件不存在**才进入配置模式；文件存在但 YAML 语法错误仍然会直接报错退出——
那种情况下静默启动只会把问题藏起来。

浏览器打开 <http://127.0.0.1:8282> 就是内置 UI：对话、文件浏览编辑、记忆、日志、设置。

（界面的导览在 [用起来](docs/using.md)；要直接敲命令，看
[使用手册](docs/tea_a2_manual.md)。）

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

### 对话和搜索可以分家（`search:` 段）

网页搜索/抓取工具（`toolkit_search` / `toolkit_fetch`）默认跟着「这一轮用的 provider」走：
provider 声明了 `supports_search` 才看得见。这对大多数场景够了，但**对话用的 provider 和
搜索用的 provider 往往不是同一个账号**——本地模型没有联网能力。在 `provider.yaml` 顶层加一个 `search:` 段就能拆开：

```yaml
search:
  provider: DeepSeek      # providers: 里的一个名字：搜索请求发给它、用它的 key
```

配好之后，用一个没有任何 web 能力的 provider（比如本机 vLLM）对话，模型照样能搜网页。
默认是**回退**（这一轮的 provider 自己能搜就还是用它）；写 `always: true` 就所有 web 调用
都走 search provider。细节见 [配置](docs/config.md)。

### 语音输入（SenseVoiceSmall）

输入框旁边有个麦克风按钮：点一下开始录音，再点一下结束，转写出来的文字**填进输入框**
（不自动发送——转写会出错，人看过再发）。

转写服务在 provider.yaml 顶层的一个 `asr:` 段里配。仓库自带一个本地跑
**SenseVoiceSmall** 的服务（多语言，中文效果好，CPU 就行，约 240 MB）：

```bash
pip install sherpa-onnx                      # 唯一的额外依赖
python3 sched/asr_server.py --download        # 拉模型到 ~/.tea_agent/asr
python3 sched/asr_server.py                   # 起服务（127.0.0.1:8284）
```

```yaml
asr:
  url: http://127.0.0.1:8284
  model: sense-voice
```

任何 OpenAI 兼容 `/v1/audio/transcriptions` 的服务都能换上去（云上的也行，加
`api_key:`）。没配 `asr:` 段，界面上就没有麦克风按钮。

> 浏览器只在**安全上下文**（https 或 localhost）里给麦克风权限。用
> `http://192.168.x.x:8282` 访问设备时浏览器不给权限，界面因此也不显示按钮——这是
> 浏览器的规矩。细节见 [配置](docs/config.md#语音输入asr-段)。

### MCP：把别的程序接进来当工具

**MCP 服务器**（Model Context Protocol）是**独立的程序**：agent 把它们启动起来，用 stdio 上的
JSON-RPC 跟它们说话，它们提供的工具就变成模型可以调用的工具（名字是
`mcp__<服务器>__<工具>`）。

配置**不在** `provider.yaml` 里，而是它同目录下的一个 **`mcp.yaml`**——provider.yaml 装的是
API key，这个文件装的是「给模型装新手」的程序，两回事：

```yaml
# $HOME/.tea_agent/mcp.yaml
servers:
  uiautomation:
    command: uiautomation-mcp
    args: []
```

（带注释的模板在仓库里：`mcp.yaml.example`。）

最典型的例子是 [uiautomation-mcp](https://github.com/locomorange/uiautomation-mcp)：
Windows 上的 UI Automation 服务器，能找按钮、点按钮、往输入框里打字、截屏——
也就是**让模型真的去操作这台机器上的窗口**。装好之后：

```powershell
winget install Locomorange.UIAutomationMCP     # 或者直接下 release
```

```yaml
# mcp.yaml
servers:
  uiautomation:
    command: 'C:\Tools\uiautomation-mcp\uiautomation-mcp.exe'
    timeout_ms: 60000          # 截屏这种慢操作要给够时间
```

细节：

- 服务器是**按需启动**的：第一次用到它的工具时才拉起来，连不上就在设置页和日志里
  写明原因，不影响别的工具。
- 「设置 → MCP 服务器」能看到每个服务器的状态和它提供的工具，**「刷新」会重新读一遍
  `mcp.yaml` 再重连**——改完文件点一下就行。
- 也可以不用模型，直接调一个 MCP 工具试试：
  `curl -X POST localhost:8282/api/mcp/servers/uiautomation/call -d '{"tool":"mcp__uiautomation__SearchElements","arguments":{"name":"OK"}}'`
- 服务器返回的图片会像 `toolkit_load_image` 那样存进工作区并附在对话里（模型能看图才行）。

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

## 服务
好吧，可以使用 user systemd service。

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
| [使用手册](docs/tea_a2_manual.md) | **命令清单**：命令行参数、curl 配方、队列命令、微信命令、脚本、环境变量 |
| [Python 侧脚本](sched/README.md) | **sched/ 目录说明**：任务队列、聊天前端（微信/Telegram）、机群、语音转写、邮件看门狗、自用工具（B 站 / 知乎） |
| [用起来](docs/using.md) | 界面导览：配置对话框、工作区、主题地址、分叉、图片、**工作区搜索**、**历史搜索** |
| [任务队列](docs/tasks.md) | 排队/调度/交互/导出、微信前端、需求逐条对照 |
| [构建与跨平台编译](docs/build.md) | 依赖、本机构建、交叉编译（aarch64 / Windows）、产物校验 |
| [部署](docs/deploy.md) | 嵌入式设备、静态单文件、TLS 证书、Windows 自启、systemd 单元 |
| [HTTP 接口](docs/api.md) | 全部接口、SSE 事件、原语层（给脚本用） |
| [多级协同与机群运维](docs/fleet.md) | **一台服务器管 40 台设备**：台账、每日采集、灰度升级、审计、让设备自己的 a2 判断 |
| [级联：一台 a2 控制另一台](docs/peer.md) | **让一台 tea_a2 控制另一台**：`peers:` 配置、`toolkit_peer`、机器对机器的通道 `/api/peer/*`、安全边界 |
| [内置工具与上下文](docs/tools.md) | 每个工具、文件访问范围、上下文预算、轨迹 |
| [MCP](docs/mcp.md) | **把别的程序接进来当工具**：配置、接口、和 Windows 上的 uiautomation-mcp 联调 |
| [与其他应用对接](docs/integrations.md) | **三个方向**：它调别人（MCP）/ 别人调它（HTTP、级联）/ 别人那边发生事（watcher）。Gmail 看门狗、通知策略、换一个应用的配方 |
| [工作区与运行时存储](docs/workspace.md) | `.tea_agent_run/` 里有什么、并发、agent.md、system prompt、销毁、导出、合并两个工作区 |
| [配置](docs/config.md) | `provider.yaml`、额度与搜索抓取、按工作区覆盖、请求头 |
| [源码结构](docs/source.md) | 每个文件干什么、第三方依赖 |
| [测试与验证状态](docs/testing.md) | 怎么跑、每个测试钉住了什么、哪些平台实测过 |
| [开发笔记](tea_a2_agent.md) | **给改这个仓库的人**：踩过的坑、命令、约定（换一台机器开发先看这个） |

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
