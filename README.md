# tea_agent (C++)

一个用 C++ 从零实现的最小可用 agent：兼容 OpenAI Chat Completions 接口、自带 HTTP 服务与
Web UI、支持多轮工具调用、多工作区，以及每个工作区独立的运行缓冲/日志/记忆目录。

配置沿用现有 Python 版 tea_agent 的 `$HOME/.tea_agent/provider.yaml`，可以直接复用。

---

## 1. 特性

| 能力 | 说明 |
| --- | --- |
| LLM 接入 | 任意 OpenAI 兼容端点（DeepSeek / OpenCode / Qwen / 本地 vLLM、Ollama…），流式 SSE 解析，支持 `reasoning_content` 与 tool calling |
| 工具调用 | 多轮循环：模型 → 工具 → 模型 …，直到不再请求工具或达到 `max_iterations` |
| 内置工具 | `toolkit_exec`（执行系统命令）、`toolkit_file`（读/写/追加/列目录/建目录/删除/移动/查看属性）、`toolkit_memory`（工作区长期记忆） |
| 多工作区 | 每个工作区在自身目录下创建 `.tea_agent_run/` 作为缓冲/日志/会话/记忆存储；`$HOME/.tea_agent/workspaces.json` 保存全部工作区索引 |
| HTTP 服务 | 启动参数 `--host` / `--port`（默认 `127.0.0.1:8282`），提供 REST API 与 SSE 流式对话接口 |
| Web UI | 风格参考 dsh（同一套深色设计令牌），内置到可执行文件中，无外部 CDN 依赖 |
| 传输层 | Windows 默认走 WinHTTP（系统证书库）；内置 mbedTLS 作为跨平台 HTTPS 后端；两者可互相回退 |
| 构建 | CMake；所有第三方依赖都在 `3rd/`；代码兼容 GCC 6.5（不使用 C++17 专属语言特性，也不使用 `std::filesystem`） |

---

## 2. 快速开始

### 2.1 准备配置

```bash
mkdir -p ~/.tea_agent
cp provider.example.yaml ~/.tea_agent/provider.yaml
# 编辑 provider.yaml，填入 api_key
```

### 2.2 构建

**Windows + MinGW-w64（推荐）**

```bat
:: 首次：把 MinGW 解压到 .toolchain\mingw64（见 tools\fetch.mjs 用法）
scripts\build-mingw.bat
```

`scripts\build-mingw.bat` 会依次在 `%MINGW_HOME%`、`.toolchain\mingw64`、`PATH` 中查找 `g++`。

**Windows + MSVC**

```bat
scripts\build.bat
```

脚本自己通过 `scripts\vcenv.bat` 装配 MSVC 环境（不依赖 `vcvars64.bat`，后者在缺少可选组件
的 VS 安装上会失败）。默认使用 `NMake Makefiles`，因为部分 Ninja 发行版
（`ninja 1.13.x jobserver-pipe`）在受限环境中会死锁；需要 Ninja 时用
`scripts\build.bat out ninja`。

**Linux / macOS / 其它 GCC**

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"
```

产物在 `build/bin/tea_agent(.exe)`。

**验证 GCC 6.5 兼容性**：`-DTEA_AGENT_CXX_STANDARD=14` 会用 C++14 语言模式编译全部代码
（GCC 6.5 上 CMake 会把 17 映射为 `-std=c++1z`，但代码本身不依赖 C++17 语言特性）。

### 2.3 运行

```bash
# 把当前目录注册为工作区并启动，默认 http://127.0.0.1:8282
build/bin/tea_agent --workspace . --open

# 也可以批量注册某个目录下的所有子目录
build/bin/tea_agent --workspace-root D:\projects --port 9000
```

启动后会打印工作区索引、当前 provider/model、可用工具，浏览器打开 Web UI 即可对话。

### 2.4 命令一览

```
--host <addr>              监听地址（默认 127.0.0.1）
--port <n>                 监听端口（默认 8282）
--web-dir <path>           从磁盘加载 UI（便于改前端），否则用内置资源
--open                     启动后用默认浏览器打开

--config <path>            provider 配置（默认 $HOME/.tea_agent/provider.yaml）
--tea-home <path>          agent 主目录（默认 $HOME/.tea_agent）
--provider <name>          指定 provider
--model <name>             指定模型
--http-engine <name>       auto | socket | winhttp
--ca-bundle <path>         PEM 根证书（默认：二进制旁的 ca-bundle/cacert.pem）
--insecure                 跳过 TLS 证书校验（仅调试）

--workspace <path>         注册工作区，可重复
--workspace-root <dir>     注册 <dir> 下所有一级子目录
--allow-outside-workspace  允许文件工具访问工作区之外的路径

--log-level <level>        trace|debug|info|warn|error|off（默认 info）
--log-file <path>          全局日志（默认 $HOME/.tea_agent/logs/tea_agent.log）
--no-console               不向控制台回显日志
--help / --version
```

环境变量：`TEA_AGENT_HOME`（覆盖 agent 主目录）、`HTTP_PROXY` / `HTTPS_PROXY`（WinHTTP 代理）。

---

## 3. 目录与存储约定

### 3.1 agent 主目录 `$HOME/.tea_agent/`

```
provider.yaml           # provider / 模型 / api_key（复用 Python 版格式）
workspaces.json         # ★ 工作区索引：id、名称、路径、创建/最近使用时间、会话数
tea_agent.yaml          # 可选：C++ 版专属 settings 覆盖（见 4.2）
logs/tea_agent.log      # 全局日志（>8MB 自动轮转，保留 .1）
```

`workspaces.json` 示例：

```json
{
  "version": 1,
  "updated_at": "2026-02-01T08:00:00Z",
  "workspaces": [
    { "id": "ws_a0e251fb8a3f", "name": "tea_a2",
      "path": "C:\\Users\\me\\work\\tea_a2",
      "run_dir": "C:\\Users\\me\\work\\tea_a2\\.tea_agent_run",
      "created_at": "…", "last_used_at": "…", "exists": true, "sessions": 3 }
  ]
}
```

启动后 `GET /api/workspaces` 会返回全部索引信息，Web UI 左侧栏即由它渲染。

**与 Python 版共存**：本程序复用 `$HOME/.tea_agent/` 时只**新增**两个条目，不改动任何既有文件：

| 新增 | 用途 |
| --- | --- |
| `workspaces.json` | 工作区索引 |
| `logs/tea_agent.log` | 全局日志（独立子目录，不与根目录的 `tea_agent.log` 冲突） |

`provider.yaml`、`custom_providers.yaml`、各类 `.db` 等一律只读，可以放心和 Python 版共用同一个配置目录。
若索引写不进去（目录只读等），会打印 `workspaces: cannot update the index: … (Windows error N: …)` 并继续运行。

### 3.2 工作区内的 `.tea_agent_run/`

```
.tea_agent_run/
├── meta.json                 # 工作区元信息
├── logs/agent.log            # 该工作区的运行日志（同时写入全局日志）
├── sessions/
│   ├── index.json            # 会话列表（标题/时间/消息数）
│   └── <session_id>.json     # 完整会话记录（含 reasoning、工具调用与结果）
├── memory/memory.json        # 长期记忆条目
├── tmp/                      # toolkit_exec 生成的临时脚本
└── artifacts/                # 预留：产物输出目录
```

> 建议把 `.tea_agent_run/` 加入被管理项目的 `.gitignore`（本仓库已忽略）。

---

## 4. 配置

### 4.1 `provider.yaml`

与 Python 版一致，关键字段：

```yaml
providers:
  <名称>:
    api_url: https://api.deepseek.com      # 无路径 → 追加 /v1/chat/completions
                                           # 以 /v1 结尾 → 追加 /chat/completions
    api_key: sk-xxx                        # 也可只写 api_keys 列表
    api_keys: [sk-xxx, sk-yyy]
    default_model: deepseek-chat
    models:
      deepseek-chat:
        max_context_tokens: 128000         # 用于历史裁剪
        max_output_tokens: 8192
        supports_tools: true
        supports_reasoning: false
        reasoning_effort: auto
        temperature: 0.7                   # 可选
        top_p: 0.9                         # 可选
roles:
  main: { provider: DeepSeek, model: deepseek-chat }
settings:
  max_iterations: 50
  max_tool_output: 131072
  keep_turns: 10
  enable_thinking: false
  command_timeout_ms: 180000
  stream: true
```

Provider / 模型解析顺序：`--provider`/`--model` → `roles.main` → 第一个 provider 的
`default_model`。API key 只会在日志里以掩码形式出现，接口从不下发。

**自定义请求头**：部分网关需要额外的路由头。在 provider 下加 `headers:`，值里的
`${session_id}` 会被替换成当前会话 id：

```yaml
providers:
  opencode:
    api_url: https://opencode.ai/zen/go/v1
    headers:
      x-opencode-session: '${session_id}'
```

`opencode.ai` 端点若未显式配置 `x-opencode-session`，程序会自动补一个（用会话 id），
因此**不必修改 Python 版已有的 `provider.yaml`** 就能直接使用。

### 4.2 `tea_agent.yaml`（可选，C++ 版专属）

不污染 `provider.yaml`，用 `settings:` 覆盖：

```yaml
settings:
  allow_outside_workspace: false
  http_connect_timeout_ms: 30000
  http_read_timeout_ms: 600000
  stream_include_usage: false     # 需要 usage 的端点才打开
  shell: cmd                      # cmd | powershell | pwsh | bash | sh
  system_prompt: |                # 覆盖默认 system prompt
    You are ...
```

---

## 5. 工具

模型看到的是标准 OpenAI function-calling schema（`GET /api/tools` 可查看）。

### `toolkit_exec`

| 参数 | 说明 |
| --- | --- |
| `command`（必需） | 命令行 |
| `cwd` | 工作目录，相对路径基于工作区根目录 |
| `timeout_ms` | 超时后杀掉整个进程树（默认 180000） |
| `shell` | `cmd` / `powershell` / `pwsh` / `bash` / `sh` |

实现要点：命令写入临时脚本文件再执行，绕开 cmd/PowerShell 的引号地狱；Windows 下用
Job Object 保证超时能杀掉子进程；控制台代码页切到 UTF-8，输出统一转成 UTF-8 再回填模型。

### `toolkit_file`

`op` ∈ `read` `write` `append` `list` `mkdir` `delete` `move` `stat`，配合
`path` / `content` / `dest` / `recursive` / `max_bytes`。

相对路径基于工作区根目录；默认禁止越出工作区（`--allow-outside-workspace` 可放开）。读取按
`max_tool_output` 截断并标注。

### `toolkit_memory`

`op` ∈ `add` `list` `delete`，读写 `.tea_agent_run/memory/memory.json`。记忆内容会自动注入
system prompt，使偏好和项目事实跨会话保留。

---

## 6. HTTP API

所有响应为 JSON；错误统一为 `{"error":{"code":"…","message":"…"}}`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` `/app.js` `/app.css` `/tokens.css` | Web UI（内置或 `--web-dir`） |
| GET | `/api/health` | 版本、运行时长、当前 provider/model、传输引擎 |
| GET | `/api/providers` | provider / 模型列表（不含 api_key）、配置路径 |
| GET | `/api/tools` | 工具定义与 JSON Schema |
| GET | `/api/workspaces` | 工作区索引（`index_path` + `workspaces[]`） |
| POST | `/api/workspaces` | 注册：`{"path":"…","name":"…"}` |
| DELETE | `/api/workspaces/{id}` | 仅从索引移除 |
| GET | `/api/workspaces/{id}/tree?path=` | 目录列表 |
| GET | `/api/workspaces/{id}/file?path=` | 读文件（2MB 截断） |
| PUT | `/api/workspaces/{id}/file` | 写文件：`{"path":"…","content":"…"}` |
| GET/POST | `/api/workspaces/{id}/sessions` | 会话列表 / 新建 |
| GET/DELETE | `/api/sessions/{sid}` | 会话详情（含全部消息）/ 删除 |
| GET/POST | `/api/workspaces/{id}/memory` | 记忆列表 / 新增 |
| DELETE | `/api/workspaces/{id}/memory/{nid}` | 删除记忆 |
| GET | `/api/workspaces/{id}/logs?tail=300` | 工作区日志尾部 |
| POST | `/api/chat` | **SSE** 流式对话（见下） |
| POST | `/api/chat/stop` | `{"session_id":"…"}` 中断当前轮次 |

### 6.1 `POST /api/chat` 事件流

请求：`{"workspace_id":"…","session_id":"…","message":"…","provider":"…","model":"…"}`

响应 `Content-Type: text/event-stream`，每行 `data: {json}`，按 `type` 分派：

```
start            本轮开始（session/provider/model）
reasoning_delta  思考流（reasoning_content）
content_delta    正文增量
tool_call        模型请求的工具（id/name/arguments）
tool_start       即将执行
tool_result      ok / elapsed_ms / output / truncated
iteration_end    单轮结束（含 usage）
done             全部结束（iterations/elapsed_ms/usage）
stopped          被 /api/chat/stop 中断
error            message / detail
ping             空闲心跳（15s）
```

```bash
curl -N -X POST -H 'Content-Type: application/json' \
  -d '{"workspace_id":"ws_xxx","message":"列出当前目录"}' \
  http://127.0.0.1:8282/api/chat
```

---

## 7. Web UI

- 左侧栏：工作区切换、会话列表、`对话 / 文件 / 记忆 / 日志 / 设置` 五个视图。
- 对话：用户气泡右对齐；助手 Markdown 渲染（自研渲染器，全部做 HTML 转义）；工具调用折叠成
  一行（图标 + 名称 + 摘要 + 耗时），展开显示输入/输出；思考过程单独折叠。
- 文件：目录浏览 + 文本编辑保存（走 `GET/PUT .../file`）。
- 记忆 / 日志 / 设置：分别对应记忆、日志、provider+工具只读展示。
- 顶部：模型选择器、连接状态；底部：自动增高的输入框，Enter 发送、Shift+Enter 换行，
  运行中变红色停止按钮。

前端资源在构建期由 `tools/embed_assets.cpp` 生成 `web_assets.cpp` 嵌入二进制；改 UI 时可用
`--web-dir web` 直接读磁盘，免重新编译。

---

## 8. 目录结构

```
CMakeLists.txt              构建入口（3rd/ 依赖在此接入）
provider.example.yaml       配置示例
scripts/
  build-mingw.bat           MinGW/GCC 构建
  build.bat                 MSVC（NMake，可用 ninja）
  vcenv.bat                 不依赖 VsDevCmd 的 MSVC 环境装配
src/
  main.cpp                  CLI、信号、启动横幅
  config.{h,cpp}            provider.yaml 解析与 roles 解析
  message.{h,cpp}           会话消息模型（wire 格式 / 存储格式）
  llm.{h,cpp}               OpenAI 兼容客户端：SSE、tool_calls 聚合、重试
  agent.{h,cpp}             多轮工具调用主循环、上下文裁剪、system prompt
  tools.{h,cpp}             工具注册表与三个内置工具
  command.{h,cpp}           命令执行（Job Object / 进程组、超时、编码归一）
  workspace.{h,cpp}         多工作区、`.tea_agent_run/`、会话与记忆存储、索引
  server.{h,cpp}            HTTP 路由、SSE 泵、静态资源
  http.{h,cpp}              传输层分派（socket / winhttp + 回退）
  http_socket.cpp           HTTP/1.1 over TCP，可选 TLS
  http_winhttp.cpp          Windows WinHTTP 后端
  tls.{h,cpp}               mbedTLS 客户端封装
  platform_fs.cpp           路径与文件 I/O（Win32 宽字符 / POSIX）
  util.{h,cpp} log.{h,cpp}  字符串、时间、编码、日志
web/                        index.html / tokens.css / app.css / app.js
tools/
  embed_assets.cpp          构建期把 web/ 嵌入二进制
  fetch.mjs                 下载依赖（走 Node TLS）
  mock_llm_server.mjs       假 OpenAI 服务，驱动 4 步工具调用
  mock_ui_server.mjs        前端开发用假后端
tests/e2e.ps1               端到端测试（37 项断言）
3rd/                        第三方依赖（见下）
```

---

## 9. 第三方依赖（全部位于 `3rd/`）

| 目录 | 版本 | 用途 | 形态 |
| --- | --- | --- | --- |
| `3rd/nlohmann_json/` | v3.11.3 | JSON | 单头文件 |
| `3rd/cpp-httplib/` | v0.61.0 | HTTP 服务端 | 单头文件 |
| `3rd/yaml-cpp/` | 0.8.0 | YAML 解析 | 源码 + CMake 子项目 |
| `3rd/mbedtls/` | 2.28.9 LTS | HTTPS 客户端 TLS | 源码 + CMake 子项目 |
| `3rd/ca-bundle/` | curl CA | mbedTLS 根证书 | PEM |

说明：

- cpp-httplib 仅在定义 `CPPHTTPLIB_OPENSSL_SUPPORT` 时才需要 OpenSSL，本项目不定义它——
  TLS 由 WinHTTP（Windows）或 mbedTLS（跨平台）承担，因此**不需要 OpenSSL**。
- 每个依赖目录下的 `UPSTREAM_VERSION.txt` 记录上游版本（不叫 `VERSION`：在 Windows 上
  大小写不敏感，会遮蔽标准头 `<version>`）。
- yaml-cpp 0.8.0 早于 GCC 13 的头文件清理，依赖 `<string>` 间接引入 `<cstdint>`；
  `CMakeLists.txt` 用 `-include cstdint` 解决，不修改上游源码。

---

## 10. 测试

```bash
pwsh -File tests/e2e.ps1                       # 用默认构建产物
pwsh -File tests/e2e.ps1 -Exe build-cxx14/bin/tea_agent.exe
```

脚本会启动 `tools/mock_llm_server.mjs`（一个按 4 步脚本驱动工具调用的假 OpenAI 服务），
再以独立 `--tea-home` 启动 agent，覆盖 37 项断言：健康检查、provider 脱敏、工具注册、工作区
索引、SSE 事件序列（start / reasoning / content / tool_call / tool_result / iteration_end /
done）、两个工具的副作用、`.tea_agent_run/` 全部子目录、会话落盘、目录树与文件读写接口、记忆
与日志接口。全部本地 `http://`，无需 api_key。

前端可单独调试：

```bash
node tools/mock_ui_server.mjs      # http://127.0.0.1:8791
```

---

## 11. 实现注意事项 / 已知限制

1. **TLS 后端选择**：`--http-engine auto`（默认）在 Windows 上先用 WinHTTP（系统证书库，零
   配置），若完全拿不到状态行（例如 schannel 被策略禁用，返回 WinHTTP 12185）则自动改用内置
   mbedTLS 重试；非 Windows 直接用 mbedTLS。可用 `--http-engine socket|winhttp` 强制。
2. **不使用 `std::filesystem`**：Windows 上它的窄字符转换走 ANSI 代码页，非 ASCII 路径会抛
   “Illegal byte sequence”（MinGW 下尤其明显）；同时 GCC 6.5 只有
   `std::experimental::filesystem`。因此路径与文件 I/O 直接基于 Win32 宽字符 API / POSIX
   实现（`src/platform_fs.cpp`），全流程 UTF-8。
3. **工作区边界**：`path_canonical` 会解析符号链接/联接点后再做前缀比较，因此工作区内的链接
   无法把文件工具带出去；确需越界时用 `--allow-outside-workspace`。
4. **历史裁剪**：按 `max_context_tokens` 的 55% 预算从最新消息向前回溯，并对齐到 user 轮次
   边界，避免从半个工具调用中间截断。
5. **代理**：WinHTTP 后端读取 `HTTP_PROXY` / `HTTPS_PROXY`；socket/mbedTLS 后端暂不支持代理。
6. **文件读取上限**：`GET .../file` 为 2MB；工具读取受 `max_tool_output` 限制。
7. **CMake 4.x**：yaml-cpp/mbedTLS 声明了 `cmake_minimum_required(VERSION 3.4/3.5.1)`，
   顶层通过 `CMAKE_POLICY_VERSION_MINIMUM` 兼容，CMake 3.x 与 4.x 均可配置。
8. **前端无 CDN**：所有 CSS/JS 内置，Markdown 渲染器自研并对模型输出做 HTML 转义。
