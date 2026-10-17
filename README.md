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

嵌入式设备上通常跑不了大模型，但「在设备上干活」需要的是本地能力：执行命令、读写文件、
记住上下文。`tea_a2` 就是这部分能力的最小实现：

```
┌──────────────────────┐        HTTP / SSE (JSON)         ┌───────────────────────────┐
│  Remote brain        │  ───────────────────────────▶   │  Embedded device          │
│  (LLM agent /        │                                 │  ┌─────────────────────┐  │
│   orchestrator /     │   POST /api/chat                │  │ tea_a2 (single exe) │  │
│   云端 workflow)     │   ◀─── content_delta / tools ──  │  │  · agent loop       │  │
└──────────────────────┘                                 │  │  · tools            │  │
                                                         │  │  · workspaces       │  │
                                                         │  │  · built-in Web UI  │  │
                                                         │  └─────────┬───────────┘  │
                                                         │            │              │
                                                         │     本地文件 / 本地命令     │
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

### 跑起来

```bash
# 准备配置
mkdir -p ~/.tea_agent
cp provider.example.yaml ~/.tea_agent/provider.yaml
$EDITOR ~/.tea_agent/provider.yaml          # 填 api_url / api_key / model

# 启动（默认 127.0.0.1:8282），把当前目录注册为工作区
build/bin/tea_agent --workspace . --open
```

浏览器打开 <http://127.0.0.1:8282> 就是内置 UI：对话、文件浏览编辑、记忆、日志、设置。

```bash
# 常用参数
--host 0.0.0.0        # 监听所有网卡（暴露到网络前务必读「安全须知」）
--port 8282           # 端口
--workspace <path>    # 注册工作区，可重复
--workspace-root <d>  # 注册 <d> 下所有一级子目录
--provider/--model    # 覆盖配置里的 roles.main
--log-level debug     # trace|debug|info|warn|error|off
--help                # 全部参数
```

---

## 给上层 Agent 用的接口

所有响应是 JSON，错误统一为 `{"error":{"code":"…","message":"…"}}`。核心就两类：同步查询 + 一条 SSE 流。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/health` | 探活、版本、当前 provider/model、传输引擎 |
| `GET` | `/api/workspaces` | 工作区索引（`index_path` + `workspaces[]`） |
| `POST` | `/api/workspaces` | 注册工作区 `{"path":"…","name":"…"}` |
| `GET` | `/api/workspaces/{id}/tree?path=` | 列目录 |
| `GET` / `PUT` | `/api/workspaces/{id}/file` | 读 / 写文件 |
| `GET` / `POST` | `/api/workspaces/{id}/sessions` | 会话列表 / 新建 |
| `GET` | `/api/sessions/{sid}` | 取完整会话（含 reasoning、工具调用与结果） |
| `GET` / `POST` | `/api/workspaces/{id}/memory` | 工作区长期记忆 |
| `GET` | `/api/workspaces/{id}/logs?tail=300` | 运行日志尾部 |
| `GET` | `/api/tools` | 工具定义与 JSON Schema |
| **`POST`** | **`/api/chat`** | **SSE 流式对话（主入口）** |
| `POST` | `/api/chat/stop` | 中断当前轮次 |

### 一次完整的「远程操控」长这样

```bash
# 1) 找到工作区
curl -s http://device:8282/api/workspaces | jq -r '.workspaces[0].id'

# 2) 下发任务，边跑边收
curl -N -X POST http://device:8282/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"workspace_id":"ws_xxx","message":"检查 /etc/tea 下的配置文件，把端口改成 9000 并重启服务"}'
```

SSE 每行 `data: {json}`，按 `type` 分派：

| `type` | 含义 |
| --- | --- |
| `start` | 本轮开始（session / provider / model） |
| `reasoning_delta` | 思考流 |
| `content_delta` | 正文增量 |
| `tool_call` | 模型请求的工具（`id` / `name` / `arguments`） |
| `tool_start` | 即将执行 |
| `tool_result` | `ok` / `elapsed_ms` / `output` / `truncated` |
| `iteration_end` | 单轮结束，带 `usage` |
| `done` | 全部结束 |
| `stopped` | 被 `/api/chat/stop` 中断 |
| `error` | `message` / `detail` |
| `ping` | 空闲心跳（15s），用于判断链路存活 |

上层 agent 只需要：发一条 `POST /api/chat`，读 SSE，看 `tool_result` 有没成功，`done` 结束。
需要打断就调 `/api/chat/stop`——它会在下一个模型/工具边界停住，已产生的会话照样落盘。

---

## 内置工具

模型看到的是标准 OpenAI function-calling schema（`GET /api/tools` 可查）。

| 工具 | 能力 |
| --- | --- |
| `toolkit_exec` | 执行系统命令。Windows 写成临时 `.cmd`/`.ps1` 再执行（绕开引号问题），超时用 Job Object 杀整个进程树；POSIX 用 `setsid` + 进程组 `kill`。输出统一转 UTF-8 回填模型 |
| `toolkit_file` | `read` `write` `append` `list` `mkdir` `delete` `move` `stat` |
| `toolkit_memory` | 工作区长期记忆的增删查，自动注入 system prompt，跨会话保留偏好与项目事实 |

相对路径基于工作区根目录；默认**禁止越出工作区**（`--allow-outside-workspace` 可放开），
判定前会解析符号链接，所以工作区里的软链无法把工具带出去。

---

## 多轮工具调用与上下文

- 单条用户消息内循环「模型 → 工具 → 模型」，直到不再请求工具或达到 `max_iterations`。
- 每轮都按 `max_context_tokens` 的 55% 预算从最新消息向前回溯，并对齐到 user 轮次边界，
  避免从半个工具调用中间截断。
- 工具参数 JSON 解析失败时，把解析错误作为工具结果回灌给模型（而不是静默当成 `{}`），
  模型有机会自我修正。

---

## 工作区与运行时存储

每个工作区在自己目录下有一个 `.tea_agent_run/`，agent 的全部状态都在里面：

```
<workspace>/.tea_agent_run/
├── meta.json                 # 工作区元信息
├── logs/agent.log            # 该工作区日志
├── sessions/
│   ├── index.json            # 会话索引（标题/时间/条数）
│   └── <session_id>.json     # 完整会话（reasoning、工具调用与结果）
├── memory/memory.json        # 长期记忆
├── tmp/                      # toolkit_exec 的临时脚本
└── artifacts/                # 预留产物目录
```

`$HOME/.tea_agent/`（可用 `--tea-home` 覆盖）只放三类东西，**不改动任何既有文件**，
因此可以和别的 agent 实现共用同一个配置目录：

| 文件 | 用途 |
| --- | --- |
| `provider.yaml` | provider / 模型 / api_key（只读） |
| `workspaces.json` | 工作区索引：id、名称、路径、创建/最近使用时间、会话数 |
| `logs/tea_agent.log` | 全局日志（>8MB 自动轮转） |
| `tea_agent.yaml` | 可选，C++ 版专属 settings 覆盖 |

---

## 配置

`provider.yaml` 就是 OpenAI 兼容端点清单，`roles.main` 决定默认用哪个：

```yaml
providers:
  DeepSeek:
    api_url: https://api.deepseek.com      # 无路径 → 追加 /v1/chat/completions
                                           # 以 /v1 结尾 → 追加 /chat/completions
    api_key: sk-xxx
    default_model: deepseek-chat
    headers:                               # 可选：网关自定义头
      x-opencode-session: '${session_id}'  # ${session_id} 会替换为当前会话 id
    reasoning_content: auto                # keep | strip | auto（默认 auto，见下）
    models:
      deepseek-chat:
        max_context_tokens: 128000
        max_output_tokens: 8192
        supports_tools: true
        supports_reasoning: false

roles:
  main: { provider: DeepSeek, model: deepseek-chat }

settings:
  max_iterations: 50           # 单条消息最多几轮工具调用
  max_tool_output: 131072      # 回填给模型的工具输出上限（字节）
  command_timeout_ms: 180000   # toolkit_exec 默认超时
  keep_turns: 10
```

解析顺序：`--provider`/`--model` → `roles.main` → 第一个 provider 的 `default_model`。
api_key 只在日志里以掩码出现，HTTP 接口从不下发。

### 回传 `reasoning_content`（多轮工具调用的坑）

第二轮请求要不要把上一轮 assistant 的 `reasoning_content` 原样带回，各家要求正好相反：

- **thinking 模式网关必须带回**（opencode.ai 等），否则第二次请求直接 400：
  *The `reasoning_content` in the thinking mode must be passed back to the API.*
- **DeepSeek 只把它当响应字段**——[官方 Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion)
  里 `reasoning_content` 出现在 assistant 响应中，请求体没有这个字段，所以默认不回传。

`auto`（默认）按端点判断：`*.deepseek.com` 不回传，其余回传。猜错也不会卡住——收到 400 且错误里
提到 `reasoning_content` 时，程序会翻转策略、记住结论（进程内）并重试同一次请求，同时打一条
`provider disagreed about reasoning_content; retrying with echo=...`。要固定行为就显式写：

```yaml
providers:
  opencode:
    reasoning_content: keep    # keep：总是回传；strip：从不回传
```

### 自定义请求头

`headers:` 里的值支持 `${session_id}` 占位符，按会话替换。`opencode.ai` 端点若没配
`x-opencode-session`，程序会自动补一个——[其文档](https://opencode.ai/docs/go/)要求客户端
「为每个会话发送稳定的 session id」。同一份文档还要求客户端用自己的 UA 标识，所以两个传输后端
（socket+TLS 与 WinHTTP）都会带 `User-Agent: tea_agent/<version>`，不会伪装成通用 HTTP 库。

因此**不必修改 Python 版已有的 `provider.yaml`** 就能直接接入。

`tea_agent.yaml`（可选）用来放 C++ 版专属设置，避免污染 `provider.yaml`：

```yaml
settings:
  allow_outside_workspace: false
  http_read_timeout_ms: 600000
  shell: sh                # cmd | powershell | pwsh | bash | sh
  system_prompt: |         # 覆盖默认 system prompt
    You are ...
```

---

## 部署到嵌入式设备

### 交叉编译

构建期辅助工具（`tools/embed_assets.cpp`，负责把 Web UI 生成 C++ 源）必须**在构建机上运行**，
所以交叉编译时它会用宿主编译器单独编译，不会出现「用 ARM 工具链编出一个宿主跑不起来的程序」。
该行为由 `TEA_AGENT_USE_HOST_ASSET_TOOL` 控制，交叉编译时自动为 `ON`。

```bash
# 以 aarch64 为例
cmake -S . -B build-arm64 -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_SYSTEM_NAME=Linux \
  -DCMAKE_SYSTEM_PROCESSOR=aarch64 \
  -DCMAKE_C_COMPILER=aarch64-linux-gnu-gcc \
  -DCMAKE_CXX_COMPILER=aarch64-linux-gnu-g++
cmake --build build-arm64 -j"$(nproc)"
```

### 体积

| 构建 | 大小 | 运行库依赖 |
| --- | --- | --- |
| Windows x64 / MinGW-w64（已实测） | 6.1 MB | 仅 Windows 10+ 自带的 UCRT 与系统 DLL，可单文件分发 |
| Windows x64 / MSVC（已实测） | 1.7 MB | 需要目标机安装 VC++ 运行库（`MSVCP140.dll` / `VCRUNTIME140*.dll`） |
| Linux / ARM（未实测） | 取决于是否静态链接 | 默认动态链接 libstdc++ |

想进一步压：`-DCMAKE_BUILD_TYPE=MinSizeRel` + `strip`；不需要 HTTPS 可
`-DTEA_AGENT_USE_MBEDTLS=OFF`（体积显著下降，但 Linux 上就没有 `https://` 了）；
还可以用自定义 `MBEDTLS_CONFIG_FILE` 裁掉 mbedTLS 里用不到的算法。

### TLS 与证书

- Windows 默认走 WinHTTP，使用**系统证书库**，无需额外文件；拿不到状态行时自动回退内置 mbedTLS。
- 其它平台走内置 mbedTLS，需要一份 CA 证书。构建时会把 `3rd/ca-bundle/cacert.pem` 复制到
  `bin/ca-bundle/`，查找顺序：`--ca-bundle` → 可执行文件同级的 `ca-bundle/cacert.pem` →
  同级 `cacert.pem` → 同级 `../3rd/ca-bundle/cacert.pem` → 工作目录 `3rd/ca-bundle/cacert.pem`
  → `$TEA_AGENT_HOME/ca-bundle/cacert.pem`。

---

## 源码结构

```
src/
  main.cpp                  CLI、信号、启动横幅
  config.{h,cpp}            provider.yaml / roles / headers 解析
  message.{h,cpp}           消息模型（wire 格式 + 存储格式）
  llm.{h,cpp}               OpenAI 兼容客户端：SSE、tool_calls 聚合、重试
  agent.{h,cpp}             多轮工具调用主循环、上下文裁剪、system prompt
  tools.{h,cpp}             工具注册表与三个内置工具
  command.{h,cpp}           命令执行（Job Object / 进程组、超时、编码归一）
  workspace.{h,cpp}         多工作区、.tea_agent_run/、会话/记忆、索引
  server.{h,cpp}            HTTP 路由、SSE 泵、内置资源
  http.{h,cpp}              传输分派（socket+TLS / WinHTTP，自动回退）
  http_socket.cpp           HTTP/1.1 over TCP（+ 可选 TLS）
  http_winhttp.cpp          Windows WinHTTP 后端
  tls.{h,cpp}               mbedTLS 客户端封装
  net_compat.h              SIGPIPE / send 标志的平台差异
  platform_fs.cpp           路径与文件 I/O（Win32 宽字符 / POSIX，全 UTF-8）
  util.{h,cpp} log.{h,cpp}  字符串、时间、编码、日志
web/                        index.html / tokens.css / app.css / app.js（构建期嵌入）
tools/                      embed_assets.cpp、host_embed/（宿主工具子项目）、mock_*.mjs
tests/e2e.ps1               端到端测试
scripts/                    build.sh / build.bat / build-mingw.bat / vcenv.bat
3rd/                        第三方依赖
```

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
| `3rd/ca-bundle/` | curl CA | 根证书 | MPL-2.0 |

各目录下的 `UPSTREAM_VERSION.txt` 记录上游版本（不叫 `VERSION`：Windows 文件名不区分大小写，
会遮蔽标准头 `<version>`）。yaml-cpp 0.8.0 依赖 `<string>` 间接引入 `<cstdint>`，用
`-include cstdint` 解决，不修改上游源码。

---

## 测试

```bash
pwsh -File tests/e2e.ps1                                  # 默认构建产物
pwsh -File tests/e2e.ps1 -Exe build-msvc/bin/tea_agent.exe
```

`tests/e2e.ps1` 起一个假 OpenAI 服务（`tools/mock_llm_server.mjs`，按 4 步脚本驱动工具调用），
再以独立 `--tea-home` 启动 agent，覆盖 **43 项断言**：健康检查、key 脱敏、工具注册、工作区索引、
SSE 事件序列、两个工具的落盘副作用、`.tea_agent_run/` 全部子目录、会话持久化、目录树与文件读写、
记忆与日志接口、内置 UI 交付且无 CDN 依赖。全程本地 `http://`，不需要 api_key。

其中一条是**思考回传的回归测试**：mock 会像真实 thinking 网关那样，一旦发现被重放的 assistant
消息丢了 `reasoning_content` 就回 400。用修复前的二进制跑这套测试会挂 7 项，可以据此确认测试
确实覆盖了这个 bug。

脚本开跑前会检查端口占用，发现残留监听就直接报错退出——否则会静默连到上一次遗留的实例上，
表现为「卡住」而不是失败。

---

## ⚠️ 安全须知

**这个程序没有任何认证机制。** `toolkit_exec` 可以执行任意系统命令，所以：

- 默认绑定 `127.0.0.1`，只允许本机访问——请保持这个默认值。
- 用 `--host 0.0.0.0` 或放到公网之前，必须自己在前面加一层认证（反向代理 + mTLS / token 校验 /
  VPN），否则等同于把设备的 shell 暴露出去。
- 只应部署在受信任的网段，或通过隧道访问。
- 文件工具默认限制在工作区内；`--allow-outside-workspace` 会取消这个限制，慎用。

---

## 当前的验证状态

诚实标注，避免误导：

| 项 | 状态 |
| --- | --- |
| Windows x64 + MinGW-w64 GCC 16.2 | ✅ 构建 0 warning，E2E 42/42 |
| Windows x64 + MSVC 19.51 | ✅ 构建 0 error，E2E 42/42 |
| C++14 语言模式（GCC 6.5 兼容性） | ✅ 全量编译通过 |
| 真实 `api.deepseek.com` / `opencode.ai` | ✅ 含多轮工具调用 |
| 交叉编译宿主工具机制（`TEA_AGENT_USE_HOST_ASSET_TOOL=ON`） | ✅ 在本机以原生方式验证该路径 |
| **Linux 构建与运行** | ⚠️ **未实测**（代码已按平台分支处理） |
| **macOS 构建与运行** | ⚠️ **未实测**（`MSG_NOSIGNAL` / `/proc/self/exe` 已按平台处理） |
| **真实 ARM 交叉编译产物** | ⚠️ **未实测** |

Linux / macOS 首次构建若报错，最可能集中在 `src/command.cpp`（`fork`/`setsid`/进程组）与
`src/platform_fs.cpp`（POSIX `stat`/`dirent`），欢迎提 issue 附报错。

---

## License

本项目**尚未添加许可证文件**。在补上之前，默认保留所有权利（all rights reserved）。
如果打算开源，建议补一个 `LICENSE`（MIT / Apache-2.0 都适合，注意 Apache-2.0 与 mbedTLS 的
双许可组合更省事）。

`3rd/` 下各依赖遵循其自身许可证，详见上表与各目录内的 `LICENSE` 文件。
