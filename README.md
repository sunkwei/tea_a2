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
# 准备配置（也可以跳过这一步，见下面的「首次运行」）
mkdir -p ~/.tea_agent
cp provider.example.yaml ~/.tea_agent/provider.yaml
$EDITOR ~/.tea_agent/provider.yaml          # 填 api_url / api_key / model

# 启动（默认 127.0.0.1:8282），把当前目录注册为工作区
build/bin/tea_agent --workspace . --open
```

**首次运行**：如果 `$HOME/.tea_agent/provider.yaml` 不存在，服务不会退出，而是以「配置模式」
启动——浏览器打开页面后会被一个不可跳过的对话框挡住，点击（或拖入）`provider.yaml`、
也可以直接粘贴内容，点「上传并应用」即可。配置会先校验再落盘（校验失败不会覆盖已有文件），
成功后页面自动刷新，状态栏从「未配置」变成「已连接」，可以直接开始对话。
命令行会打印 `starting in setup mode` 和提示地址，方便无 GUI 环境照常启动后再上传。

注意只有**文件不存在**才进入配置模式；文件存在但 YAML 语法错误仍然会直接报错退出——
那种情况下静默启动只会把问题藏起来。

浏览器打开 <http://127.0.0.1:8282> 就是内置 UI：对话、文件浏览编辑、记忆、日志、设置。
左侧栏右上角的按钮（或 `Ctrl/Cmd+B`）可以把它收起，把横向空间让给正文；收起后顶栏左侧
出现同一个图标，点一下即可展开，状态记在浏览器里，下次打开保持原样。

浏览器标签页的标题是「`{当前工作区} · TEA AGENT`」，没有工作区时退回 `TEA AGENT`——同时开着
几个页面时，靠标签页就能分清哪个是哪个项目。
对话页的输入框下方常驻**一行**指标：耗时、轮次、步数、速度（t/s）、前缀缓存命中率、
`max_context_tokens` 用量（后两者带进度条，超 70% / 90% 变黄变红）以及本轮输出与首字延迟。
这一行永远是单行：`flex-wrap:nowrap` 保证不会折成两行（折行会让输入区高度跳动），
窗口变窄时按「轮次/步数 → 缓存 → 耗时」的顺序收起，最宽的一项最多出省略号，完整内容
仍在 `title` 里。耗时在流式期间按本地时钟跳动，结束后取 `done` 的 `elapsed_ms`；
轮次是该会话已发送的用户消息数，步数是本轮 `tool_start` 次数。

**切换/新增工作区**（每个工作区就是一个项目目录，各自独立的会话、记忆、文件树和
`.tea_agent_run/`）：点左上角的**工作区名称**（形如 `tea_a2 ›`）打开下拉，列出所有已注册
工作区，最后一项是「＋ 添加工作区…」。点它会**直接弹出系统的目录选择对话框**，选中后立刻
注册并切换过去。

为什么是服务端弹框：浏览器给不出绝对路径（`showDirectoryPicker()` 返回的是句柄，
`<input webkitdirectory>` 只有相对名），所以对话框由运行 tea_agent 的那台机器弹出
（Linux 用 kdialog/zenity，macOS 用 `osascript`，Windows 用 PowerShell 的
FolderBrowserDialog）。**注意**：如果你从另一台电脑打开这个 UI，对话框会出现在服务器上；
那种情况下请用手动输入。

服务端没有可用对话框时（无桌面、没装 zenity/kdialog），UI 自动退回手动输入表单，并且会
问一次「目录不存在，是否创建」——确认后 `mkdir -p` 再注册。`TEA_AGENT_DIR_PICKER` 可以把
选择器换成任意命令（stdout 即所选路径），用于无桌面环境或 SSH 下的自建选择器。
命令行侧对应 `--workspace <path>`（可重复）和 `--workspace-root <dir>`（注册其下所有一级子目录）。

侧边栏的**会话索引**按更新时间倒序列出当前工作区的会话（标题、相对时间、消息条数），
点击即切换，当前会话高亮，行内可**改名**、**删除**，右上角 ＋ 新建；一轮对话结束后自动刷新。
助手输出里的 `![alt](路径)` 会渲染成图片（alt 作为说明文字），工作区相对路径经
`/api/workspaces/{id}/raw` 取原始字节；围栏代码块、表格、引用、有序/无序列表、标题、
行内代码、粗斜体与链接都会渲染，链接一律新标签页打开。文件视图里选中图片直接显示，
选中 `.md` 可在「预览 / 编辑」之间切换。

**粘贴图片**：在输入框里直接 Ctrl+V 粘贴截图（或把图片拖进输入框），缩略图会出现在
输入框上方，点 × 可移除，一条消息最多 4 张、单张 8 MB。发送时图片随消息提交，服务端按
**内容**而不是文件名嗅探真实类型（PNG / JPEG / GIF / WebP），落到工作区的
`.tea_agent_run/artifacts/`，会话里只记路径不存字节，因此会话文件依旧很小、可直接阅读。
历史消息里的图片由 `/api/workspaces/{id}/raw` 回放。

图片只有在模型声明 `supports_vision: true` 时才会发送：否则 `/api/chat` 直接返回
`400 vision_unsupported` 并说明原因，而不是让 provider 报一个难懂的错，或者更糟——静默
忽略图片让用户以为模型看过了。视觉输入在上下文预算里按每张 1024 token 估算（真实用量以
provider 回报的 `usage` 为准）。

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
| `GET` | `/api/workspaces/{id}/raw?path=` | 读文件原始字节（图片 / PDF 等，16 MB 上限） |
| `GET` / `POST` | `/api/workspaces/{id}/sessions` | 会话列表 / 新建 |
| `GET` | `/api/sessions/{sid}` | 取完整会话（含 reasoning、工具调用与结果） |
| `PATCH` / `DELETE` | `/api/sessions/{sid}` | 重命名（`{"title":"…"}`）/ 删除会话 |
| `GET` | `/api/sessions/{sid}/trajectory` | 每轮轨迹摘要，最新在前；`?full=1` 带步骤与文件变更，`?format=jsonl` 原始 JSONL |
| `GET` | `/api/turns/{turn_id}/trajectory` | 单轮完整轨迹（`turn_id` 即 `done` 事件的 `trajectory_id`） |
| `GET` / `POST` | `/api/workspaces/{id}/memory` | 工作区长期记忆 |
| `GET` | `/api/workspaces/{id}/logs?tail=300` | 运行日志尾部 |
| `GET` | `/api/tools` | 工具定义与 JSON Schema |
| **`POST`** | **`/api/chat`** | **SSE 流式对话（主入口）** |
| `POST` | `/api/chat/stop` | 中断当前轮次 |
| `POST` | `/api/chat/queue` | 轮次进行中把消息插队（下一轮采纳） |
| `POST` | `/api/chat/queue/cancel` | 取消一条（带 `id`）或全部排队消息 |
| `GET` | `/api/chat/queue?session_id=` | 查看当前排队消息 |

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
| `tool_args_delta` | 工具调用参数的流式增量（只用于进度/速度统计，不当正文渲染） |
| `tool_call` | 模型请求的工具（`id` / `name` / `arguments`） |
| `tool_start` | 即将执行 |
| `tool_result` | `ok` / `elapsed_ms` / `output` / `truncated` |
| `iteration_end` | 单轮结束，带 `usage` 与 `metrics` |
| `queue_adopted` | 采纳了一条排队消息（`id` / `content` / `pending`） |
| `queue_unconsumed` | 轮次结束时仍未采纳的排队消息（`items[]`），客户端应放回输入框 |
| `done` | 全部结束，带整轮的 `usage` / `metrics` / `content` / `trajectory_id` |
| `stopped` | 被 `/api/chat/stop` 中断 |
| `error` | `message` / `detail` |
| `ping` | 空闲心跳（15s），用于判断链路存活 |

上层 agent 只需要：发一条 `POST /api/chat`，读 SSE，看 `tool_result` 有没成功，`done` 结束。
需要打断就调 `/api/chat/stop`——它会在下一个模型/工具边界停住，已产生的会话照样落盘。

### 插队：轮次进行中追加消息

一轮可能跑很久（长工具、多轮迭代、思考模型）。这段时间里再发一条 `POST /api/chat` 会把
当前轮次顶掉，所以 `tea_a2` 提供了「排队」而不是「抢占」：消息进入**当前轮次自己的**队列，
agent loop 在下一轮迭代开始前采纳最早的一条，把它作为普通 user 消息追加进会话，然后带着
新的上下文继续请求模型。一轮只采纳一条，连发几条不会挤进同一个请求。

```bash
# 轮次进行中（session_id 来自 start 事件）
curl -s -X POST http://device:8282/api/chat/queue \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s_xxx","message":"顺便把日志级别改成 debug"}'
# {"ok":true,"queued":{"id":"q_...","content":"..."},"pending":1}

# 采纳前可以取消：带 id 取消一条，不带 id 取消全部
curl -s -X POST http://device:8282/api/chat/queue/cancel \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s_xxx","id":"q_..."}'
```

SSE 上的对应事件：

| `type` | 含义 |
| --- | --- |
| `queue_adopted` | 已采纳：`id` / `content` / `iteration` / `pending`。该消息已落盘为 user 消息 |
| `queue_unconsumed` | 轮次在采纳前结束（停止 / 报错 / 迭代上限），`items[]` 原样退回，客户端应放回输入框 |

几条刻意的约束：

- **队列随轮次存活**。轮次结束队列即销毁，不会留到下一次、无关的对话里突然发出去。
- **模型已经给完最终答案时也会采纳**：loop 发现队列非空就继续迭代，而不是把消息留成下一轮
  HTTP 请求；只有停止 / 报错 / 迭代上限才会退回。
- **只收文本**。图片必须在请求时解码、校验并落到工作区，`/api/chat` 已经为「开始一轮」做了
  这些；`/api/chat/queue` 带 `images` 会明确返回 `400 images_not_queued`，而不是静默丢掉。
- 没有进行中的轮次时返回 `409 no_active_turn`：这时直接 `POST /api/chat` 就行。

内置 Web UI 把这条路径做成了输入框行为：生成中按 **Enter** 进入排队（输入框上方显示排队条，
每条都能点 × 取消），`Shift+Enter` 仍是换行，右侧按钮仍是「停止」。轮次结束时仍未采纳的
消息会自动放回输入框，不会丢字。

### `metrics`：指标栏的数据来源

`iteration_end` 与 `done` 都带一个 `metrics` 块，内置 UI 用它渲染输入框下方的
「输出速度 / 缓存命中率 / 上下文用量」：

```json
{
  "max_context_tokens": 128000,  // 模型窗口；模型没配 max_context_tokens 时为 0
  "context_tokens": 8642,        // 本轮单次请求最大的 prompt tokens（不是各轮累加）
  "output_tokens": 512,          // 本轮生成的 tokens（多轮工具调用累加）
  "ttft_ms": 640,                // 首字延迟
  "generation_ms": 2140,         // 生成窗口（首字→最后一个增量），t/s 的分母
  "elapsed_ms": 3120,            // 整轮耗时，含工具执行
  "estimated": false             // true：provider 没回 usage，这些数字是本地估算
}
```

输出速度 = `output_tokens / generation_ms`。用生成窗口而不是整轮耗时，是因为后者包含
网络排队与工具执行时间，会把 t/s 压得没有意义；窗口短于 250ms（单块响应、非流式）时
退回整轮耗时。多轮工具调用时两边都是**各轮之和**，所以 `done` 里的 t/s 是「纯生成时间」
的吞吐，而 `elapsed_ms` 会大得多（工具执行不在里面）。

UI 在生成过程中显示的是 `≈` 前缀的**实时估算**：把本轮收到的 `content_delta` /
`reasoning_delta` / `tool_args_delta` 按字符折算成 token，除以「本轮首字→现在」的窗口
（窗口在每次 `iteration_end` 重置，因此工具执行时间同样不计入）。服务端数字到达后立刻
换成真实值——两边的口径一致，实时值不会长时间偏离最终值。

provider 只在 `stream_options.include_usage` 打开时才回传 usage。没回传时后端用同一个
token 估算器补上 `context_tokens` / `output_tokens` 并置 `estimated: true`，UI 用「≈」
标出来；缓存命中率无法估算，于是显示 `—` 而不是编一个数字。每轮日志里也有一行：

```
turn finished ok=true iterations=4 elapsed_ms=3120 output_tokens=512 ttft_ms=640 gen_ms=2140 context=128/128000
```

### 轨迹：每一轮「实际做了什么」的审计记录

会话记录（`/api/sessions/{sid}`）回答「说了什么」，轨迹回答「做了什么」：每一次模型
迭代、每一次工具调用的原始参数与真实输出、上下文压缩、以及工具**自己回报**的文件改动。
被上层 agent 远程操控时，这是唯一不用反推聊天记录的证据链。

- 位置：`<workspace>/.tea_agent_run/trajectories/<session_id>.jsonl`，**一轮一行**。
- 一行 = 一个 turn 对象：`turn_id` / `session_id` / `workspace_id` / `provider` / `model` /
  `started_at` / `finished_at` / `ok` / `iterations` / `usage{prompt,completion,total,cached}`
  / `steps[]` / `files_changed[]`。
- 步骤 `kind`：
  - `model`：`reasoning_chars` / `content_chars` / 请求的 `tool_calls` 名字 /
    `tokens{prompt,completion,cached}` / `ttft_ms`（provider 没回 usage 时标 `usage_estimated`）；
  - `tool`：`name` / `arguments`（模型发出的原始 JSON 字符串）/ `ok` / `exit_code` /
    `truncated` / `output_filter` + `dropped_lines` / `output`（按上限截断并留标记）；
  - `compaction`：`covered_messages` / `summary_bytes`。
- `files_changed` 只写工具真实回报的结果：`toolkit_file` 的 write/append/delete/move 带
  `path`（写入还带 `bytes` 与 `size`），`toolkit_exec` 只带 `command`——不猜路径。
- 增量落盘：turn 一开始就写头行，之后每一步**原地刷新同一行**（同偏移重写 + 截断），
  因此进程被 kill 或断电时文件里仍是最后一份完整快照。读入时按 `turn_id` 取最后一条，
  尾部被写坏的半行会被跳过；`?full=1` 里未完成的轮次带 `"partial": true`。
- 文件里不含任何 api_key，API 响应也不含。

```bash
# SSE 的 done 事件直接给出 id，不需要轮询
curl -N -X POST http://device:8282/api/chat -d '{"workspace_id":"ws_x","message":"..."}'
# data: {"type":"done",...,"trajectory_id":"t_1a2b3c4d5e6f"}

curl -s http://device:8282/api/sessions/s_9f8e7d/trajectory            # 摘要，最新在前
curl -s 'http://device:8282/api/sessions/s_9f8e7d/trajectory?full=1'   # 带 steps / files_changed
curl -s http://device:8282/api/turns/t_1a2b3c4d5e6f/trajectory         # 单轮完整轨迹
curl -s 'http://device:8282/api/sessions/s_9f8e7d/trajectory?format=jsonl'  # 原始 JSONL
```

默认视图（摘要）：

```json
[{"turn_id":"t_1a2b3c4d5e6f","started_at":"2026-01-01T00:00:00.000Z","elapsed_ms":4213,
  "ok":true,"iterations":4,"steps":7,
  "usage":{"prompt_tokens":1843,"completion_tokens":512,"total_tokens":2355,"cached_tokens":1024},
  "files_changed":1,"partial":false}]
```

内置 UI 在每条助手回复上方显示一行折叠的「轨迹」（例如 `4 步 · 1.2 s · 284 tok`），
展开后是步骤时间线（模型 / 工具 / 压缩）、工具参数与实际输出、以及本轮改动过的文件与
字节数。

留存与上限，按工作区覆盖（`<workspace>/.tea_agent_run/settings.yaml`）：

```yaml
settings:
  enable_trajectory: true           # false => 完全不写轨迹
  trajectory_step_max_bytes: 8192   # 单条工具输出保留多少字节（超出留截断标记）
  trajectory_max_turns: 200         # 每个会话文件最多留几轮
  trajectory_max_bytes: 8388608     # 每个会话文件最多多少字节（默认 8 MB）
```

超出上限时**从最旧的轮次删起**，并在工作区日志里写清删了什么：

```
trajectory: pruned 1 turn(s) from s_9f8e7d.jsonl (kept 1 turn(s), 629 bytes; limits 1 turns / 8388608 bytes)
```


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

单条用户消息内循环「模型 → 工具 → 模型」，直到不再请求工具或达到 `max_iterations`。
工具参数 JSON 解析失败时，把解析错误作为工具结果回灌给模型（而不是静默当成 `{}`），
模型有机会自我修正。

这一块的四个机制值得单独说，因为它们直接决定长会话能不能用：

### 1. 工具输出按内容属性削减，而不是按字节硬切

对 4 万行的构建日志，按字节截断留下来的是进度噪声、丢掉的往往是错误行。`toolkit_exec`
会先给命令分类（build / test / diff / 通用）再削减：

- **build / test**：保留每一条诊断行（`error`、`failed`、`undefined reference`、`错误`、`失败`
  …）及其后 2 行上下文，丢掉进度噪声（`[ 42%] Building ...`、cmake 的 `-- ...`、
  `Note: including file`），并始终保留头部与尾部；
- **其它**：保留头部 + 尾部；
- 每一段被省略的内容都替换成显式标记：
  `... [tea_agent omitted 321 lines of output: progress noise; diagnostics kept] ...`，
  模型因此知道自己看到的是删减版，不会基于残缺日志瞎猜。

过滤是确定性的（同样的输入必然产出同样的输出），因为工具结果会落盘并在后续轮次重放。
可以随时自测：`tea_agent --selftest`。

### 2. 预算按实际额度算，而不是拍一个百分比

```
budget = 上下文窗口 − 输出预留 − (system prompt + 工具 schema + 框架开销)
```

输出预留取 `max_tokens` 或模型的 `max_output_tokens`，并限制在窗口的 1/3 以内（避免饿死历史）；
预算有 `窗口 / 5` 的下限。`keep_turns` 是「至少保留最近 N 轮」的**硬下限**，单条消息本身超预算时
按头尾裁剪而不是直接溢出。日志里每次都能看到实际数字：

```
context: max=1000000 reserve_output=333333 fixed=847 budget=665820 kept=4/4 used=960 summarised=23
```

### 3. 超预算时做滚动摘要，而不是静默丢弃

历史超过预算时，把最旧的一段交给模型压缩成摘要，写入会话的 `summary`，并记录
`summary_upto`（已被摘要覆盖的消息条数）。**原始消息不会删除**——UI 和审计仍能看到全文，
只是不再发给模型。摘要会写进 system prompt 的「Compact history」小节，并在 SSE 里发一个
`compaction` 事件。摘要失败（网络/模型报错）时自动退回丢弃最旧消息，不会卡死会话。

### 4. 前缀缓存命中率是**可测量**的

DeepSeek / opencode 网关都按「最长公共前缀」缓存提示词。系统提示词里任何每轮都变的内容
（比如时间戳）都会让缓存整体失效，所以：

- Environment 块全部来自**启动时探测一次**的真实设备信息（见下），不随时间变化；
- 墙钟被移出提示词，改为提示模型需要时自己执行 `date` / `echo %DATE% %TIME%`；
- 每次补全都记录命中情况：`cache=1280/1444 (88%)`。

实测同一个真实端点连续两轮：**88% 和 87%** 的前缀命中。

### 环境探测（system prompt 的 Environment 块）

不是写死的 `Windows`/`Linux` 字符串，而是启动时真实探测设备（`src/env.cpp`）：

```text
Environment (probed from this device at startup)
- OS: Windows 10.0.19045 (x86_64)      # RtlGetVersion 拿真实 build，不用会撒谎的 GetVersionEx
- Host: SUNKW-X1
- Time zone: UTC+08:00
- CPUs: 8, memory: 15.9 GiB
- Free space on the workspace volume: 329.5 GiB
- toolkit_exec shell: cmd (also installed: powershell, pwsh)
```

Linux 下走 `/etc/os-release`（嵌入式发行版能读成 `Buildroot` 之类）+ `uname` + `statvfs`，
macOS 走 `uname`。宿主名、时区、CPU 数、物理内存、可用 shell（真的在 PATH 里能找）都是实测值。
`GET /api/workspaces/:id/prompt` 可以随时看到当前 workspace 实际拼出的完整 system prompt。

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
├── trajectories/<sid>.jsonl  # 每轮轨迹，一轮一行（见上文「轨迹」）
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
    supports_search: true                  # 网页搜索工具 toolkit_search（见下「网页搜索与抓取」）
    supports_fetch: true                   # 网页抓取工具 toolkit_fetch
    # search_path: /v1/tools/search        # 可选：端点覆盖（相对 api_url 的路径或完整 URL）
    # fetch_path: /v1/tools/fetch
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

### 网页搜索与抓取（provider 级可选能力）

DeepSeek 对所有 API 用户开放了两个原生端点（`POST /v1/tools/search`、`POST /v1/tools/fetch`，
与 chat 相同的 Bearer 鉴权）。这不是 OpenAI 标准协议，探测不了，所以按 provider 声明：

```yaml
providers:
  DeepSeek:
    supports_search: true    # 向模型暴露 toolkit_search（结构化搜索结果）
    supports_fetch: true     # 向模型暴露 toolkit_fetch（网页 → Markdown 正文）
```

开关是 provider 级而非模型级——端点挂在账号上，与具体模型无关。未声明的 provider 完全看不到
这两个工具；即使模型硬调也会得到能力错误，不会发请求。默认全关：DeepSeek 按请求次数计费
（各 5 元 / 1000 次成功请求，账号级各 5 QPS），开了才会计费。搜索支持 `site:`、`lang:`、
`after:`/`before:` 等语法，工具描述里会教给模型。

超时与计费预算单独配置（`settings:` 里，也可按工作区覆盖）：

```yaml
settings:
  web_tool_connect_timeout_ms: 10000   # 连接超时
  web_tool_timeout_ms: 60000           # 单次 search/fetch 的读超时
  web_tool_max_calls_per_turn: 10      # 每轮对话最多几次计费调用
```

这三项是必须的，不能让 web 工具沿用 LLM 的超时：`http_read_timeout_ms` 默认 600 秒（流式
响应要用），一次卡住的搜索会让整轮对话停十分钟，而且中途无法取消。现在 web 工具用独立的
有界超时，用户点停止会立即中断传输（返回 `cancelled by user`）。预算用尽后这两个工具会从
工具表里消失，模型不会继续空转重试——否则它会把 `max_iterations` 全部耗在重试上，既慢又费钱。

### 按工作区覆盖设置

同一份二进制部署到不同设备/项目时，shell、超时、上下文策略往往不同，所以设置是分层的
（后者覆盖前者）：

```
内置默认 → $TEA_AGENT_HOME/tea_agent.yaml → <workspace>/.tea_agent_run/settings.yaml → CLI
```

```yaml
# <workspace>/.tea_agent_run/settings.yaml
settings:
  shell: bash                  # 这台设备上该用哪个 shell
  command_timeout_ms: 600000
  keep_turns: 6
  enable_compaction: true
  compaction_keep_recent: 4
  tool_output_max_lines: 400
  system_prompt: |             # 也可以整体替换默认规则
    ...
```

另外有一个工作区指令文件 `<workspace>/.tea_agent_run/instructions.md`，内容会原样拼进
system prompt 的「Workspace instructions」小节——用来放「这个项目怎么构建、怎么测、有哪些约定」，
与机器级配置彻底分开。两者都在 workspace 打开时读取一次，保证提示词前缀稳定。

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
| Linux x64（已实测） | 5.7 MB | **完全静态**：不依赖目标机的 glibc 版本 |
| Linux / ARM（未实测） | 取决于工具链 | 默认完全静态，需要目标架构的静态 libc |

想进一步压：`-DCMAKE_BUILD_TYPE=MinSizeRel` + `strip`；不需要 HTTPS 可
`-DTEA_AGENT_USE_MBEDTLS=OFF`（体积显著下降，但 Linux 上就没有 `https://` 了）；
还可以用自定义 `MBEDTLS_CONFIG_FILE` 裁掉 mbedTLS 里用不到的算法。

### 单文件部署（完全静态）

一个 ELF 拷到目标机就能跑：**不依赖目标机的 glibc 版本**，也**不需要旁边的 PEM 文件**。
这正是「部署到比构建机更老的系统」所需要的——动态链接的二进制在那种机器上会先报
`version 'GLIBC_2.xx' not found` 再退出，连一句有用的错误都来不及打印。

| 平台 | 链接方式 | 目标机需要什么 |
| --- | --- | --- |
| Windows / MinGW-w64 | `-static-libgcc -static-libstdc++ -static` | 只有 Windows 自带的 `KERNEL32/ADVAPI32/SHELL32/WINHTTP/WS2_32` + UCRT |
| Linux / 其它 ELF（GCC/Clang） | `-static -no-pie`（默认开启） | **什么都不需要**，连 glibc 都不用 |
| macOS | 不适用 | 系统不提供静态 `libc++`，自动跳过 |

```bash
file build/bin/tea_agent     # → statically linked
ldd  build/bin/tea_agent     # → not a dynamic executable
```

三个相关开关：

| 选项 | 默认 | 作用 |
| --- | --- | --- |
| `TEA_AGENT_STATIC_RUNTIME` | `ON` | 静态链接 `libstdc++`/`libgcc` |
| `TEA_AGENT_STATIC_LIBC` | `ON` | 再静态链接 libc（`-static -no-pie`） |
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
- 其它平台走内置 mbedTLS，根证书**编译进二进制**（`3rd/ca-bundle/cacert.pem` 由
  `tools/embed_ca_bundle.cpp` 生成 `ca_bundle.cpp`），所以单文件部署不需要随附 PEM。
  启动日志会写 `tls: CA bundle embedded in the binary (N bytes)`。
- 覆盖顺序：`--ca-bundle <pem>` 优先（便于不重新编译就更新根证书）→ 内嵌证书 → 磁盘查找
  （同级 `ca-bundle/cacert.pem`、同级 `cacert.pem`、`../3rd/ca-bundle/cacert.pem`、
  工作目录 `3rd/ca-bundle/cacert.pem`、`$TEA_AGENT_HOME/ca-bundle/cacert.pem`）。
  最后这层只对「构建时没有 vendored 证书」的构建有意义。

---

## 源码结构

```
src/
  main.cpp                  CLI、信号、启动横幅
  config.{h,cpp}            provider.yaml / roles / headers 解析
  message.{h,cpp}           消息模型（wire 格式 + 存储格式）
  llm.{h,cpp}               OpenAI 兼容客户端：SSE、tool_calls 聚合、重试
  agent.{h,cpp}             多轮工具调用主循环、预算与滚动摘要、system prompt
  prompt_queue.h            轮次进行中的排队消息（线程安全 FIFO，每轮采纳一条）
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
  env.{h,cpp}               设备环境探测（OS/架构/时区/CPU/内存/磁盘/shell）
  output_filter.{h,cpp}     工具输出按内容属性削减（build/test/通用）
  util.{h,cpp} log.{h,cpp}  字符串、时间、编码、日志
web/                        index.html / tokens.css / app.css / app.js（构建期嵌入）
tools/                      embed_assets.cpp、host_embed/（宿主工具子项目）、mock_*.mjs
tests/e2e.ps1              端到端测试（69 项）；tests/sse_probe.mjs 严格流客户端
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

python3 tests/smoke_web_tools.py                          # 网页工具（本地 mock，不产生费用）
python3 tests/smoke_vision.py                             # 粘贴图片：校验/落盘/多模态请求体
node tests/ui_setup_flow.mjs                              # 首次运行上传配置（需 google-chrome）
node tests/ui_paste_image.mjs                             # 粘贴图片的浏览器端行为（需 google-chrome）
node tests/ui_metrics_layout.mjs                          # 指标栏始终单行且不截断（需 google-chrome）
python3 tests/smoke_single_file.py                        # 内嵌证书 + 静态链接 + 静态下的 DNS
python3 tests/smoke_dir_dialog.py                         # 原生目录选择器（后端选择/取消/不可用）
python3 tests/smoke_prompt_queue.py                       # 消息插队：采纳 / 取消 / 退回
node tests/ui_workspace_add.mjs                           # 新增工作区（原生对话框 + 手动回退）
node tests/ui_stream_guard.mjs                            # 生成期间切换/新建被明确拒绝
node tests/ui_queue.mjs                                   # 生成中 Enter 排队、取消、采纳（需 google-chrome）
node tests/ui_sidebar.mjs                                 # 侧栏隐藏/显示与正文宽度自适应（需 google-chrome）
```

`tests/smoke_web_tools.py` 用一个假的 provider 同时模拟 chat 与 `/v1/tools/{search,fetch}`，
覆盖：工具按 provider 声明才出现在工具表、未声明时强制调用被拒且不外呼、端点卡住时按
`web_tool_timeout_ms` 超时而不是拖住整轮、`/api/chat/stop` 能中断传输中的请求。

`tests/smoke_vision.py` 覆盖图片通路：没有 `supports_vision` 的模型被拒且**不会**请求 provider、
坏 base64 / 非图片字节 / 超过 4 张 / 空载荷分别报对应错误码、合法图片落盘且字节一致、
请求体是 `[{type:text},{type:image_url}]` 且 base64 往返无损、会话只存路径、`/raw` 能取回原图、
纯图片消息与纯文本消息都仍然可用。

`tests/ui_paste_image.mjs` 用真实粘贴事件（`ClipboardEvent` + `DataTransfer`）验证缩略图出现、
× 能移除、发送后缩略图条清空、用户气泡经 `/raw` 渲染且图片真的加载成功（`naturalWidth > 0`）。

`tests/ui_metrics_layout.mjs` 在 9 个窗口宽度（1440→600）下把每个指标填成最宽的真实数值，
断言指标栏**只有一行**、且最长的那项没有被截断。这类「不许换行」的布局约束加一个指标或
改一个标签就会破，所以单独钉住。

`tests/smoke_dir_dialog.py` 覆盖目录选择器的后端：`TEA_AGENT_DIR_PICKER` 覆盖、带空格的路径
原样返回、KDE 走 kdialog / GNOME 走 zenity（用假的可执行文件记录 argv，不弹窗）、取消返回
200+cancelled、失败返回 500、完全没有选择器时返回 501 且 `/api/health` 的 `dir_picker` 为
false。选出的路径能直接喂给 `/api/workspaces`。

`tests/smoke_prompt_queue.py` 用三个假 provider 覆盖插队语义：工具执行中入队后，agent 在
下一轮迭代采纳它并出现在后续请求里、落盘为 user 消息；模型已经流式输出完最终答案时入队，
本轮会继续一次迭代而不是结束；按 id 取消 / 全部取消；停止轮次后未采纳的消息通过
`queue_unconsumed` 原样退回且不进入会话；没有进行中的轮次返回 409；带图片 / 空消息返回 400。

`tests/ui_workspace_add.mjs` 覆盖两条入口：有选择器时「添加工作区」**不出现表单**、直接采用
对话框结果并切换；没有选择器时退回手动表单、隐藏「浏览…」按钮、目录不存在时给出创建提示
并在确认后真的建目录、取消不产生副作用（并验证服务端对「路径是个文件」仍然拒绝，即使带了
`create: true`）。

`tests/ui_stream_guard.mjs` 起一轮慢速流式回答，在生成过程中点侧边栏：工作区按钮仍可点开菜单
（不能变成死按钮），切换工作区 / 添加工作区 / 新建会话都被拒绝并给出原因，会话行显示为禁用；
一轮结束后切换恢复正常。

`tests/ui_queue.mjs` 在流式回答期间用真实的 Enter 按键入队：排队条按顺序出现、输入框清空、
没有产生第二个会话；点 × 取消第一条；随后被采纳的一条从排队条消失并变成对话里的 user 气泡，
本轮在同一 SSE 内继续（恰好两次模型请求），结束后排队条隐藏、输入框恢复「发送」。

`tests/ui_sidebar.mjs` 覆盖侧栏收起：点隐藏按钮后侧栏宽度归零、顶栏出现展开按钮、正文栏
变宽（780 → 1040）；刷新后仍是收起状态；再点展开（或 `Ctrl+B`）后侧栏与列宽复原，选择被
写进 localStorage。同一个脚本还断言标签页标题带上了当前工作区。

`tests/ui_setup_flow.mjs` 起一个**没有** `provider.yaml` 的实例，再用 headless Chrome 通过
CDP 操作真实页面：断言弹出配置对话框、Esc/点遮罩关不掉、坏 YAML 报错且不落盘、文件选择器
能填入内容、上传成功后页面自动重载且状态栏变回「已连接」。

`tests/e2e.ps1` 起一个假 OpenAI 服务（`tools/mock_llm_server.mjs`，按 4 步脚本驱动工具调用），
再以独立 `--tea-home` 启动 agent，覆盖 **69 项断言**：健康检查、key 脱敏、工具注册、工作区索引、
SSE 事件序列与 `metrics` 指标块、两个工具的落盘副作用、`.tea_agent_run/` 全部子目录、会话持久化、
目录树与文件读写、记忆与日志接口、内置 UI 交付（含指标栏）且无 CDN 依赖。全程本地 `http://`，
不需要 api_key。

其中几条是**回归测试**：

1. **思考回传**：mock 像真实 thinking 网关那样，一旦发现被重放的 assistant 消息丢了
   `reasoning_content` 就回 400。用 0.1.0 的二进制跑会挂 7 项。
2. **上下文压缩**：fixture 里有一个 `max_context_tokens: 1500` 的 `mock_small` provider，
   `e2e.ps1` 用它 + 复用同一会话 + 工作区级 `settings.yaml` 强制触发压缩，断言 SSE 出现
   `compaction` 事件、会话落盘了 `summary` / `summary_upto`、且原始消息没有被删除
   （压缩是「不再发送」而不是「删除」）。
3. **流正确收尾**：`tests/sse_probe.mjs` 用 Node 的 fetch 当严格客户端读 SSE。curl 容忍
   未终止的 chunked 响应，浏览器/Node 不容忍（`error: Error in input stream` /
   `TypeError: terminated`），所以必须显式断言。用 0.1.1 的二进制跑，44 项里恰好挂这 1 项。
4. **指标块**：mock 只在 `stream_options.include_usage` 打开时才回传 usage（真实网关就是这个
   行为）。默认那一轮因此必须回落成本地估算并置 `estimated: true`；打开 include_usage 后
   `context_tokens` 必须等于 provider 报的 prompt tokens；再换用忽略该选项的 `mock_nousage`
   provider，必须又回到估算，而不是 0 或空。

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

本项目代码采用 **MIT** 许可证，见根目录 [`LICENSE`](LICENSE)。

Copyright (c) 2026 sunkwei

`3rd/` 下的依赖各自遵循其原有许可证，**不被 MIT 覆盖**，详见各目录内的 `LICENSE` 文件：

| 依赖 | 许可证 |
| --- | --- |
| nlohmann/json | MIT |
| cpp-httplib | MIT |
| yaml-cpp | MIT |
| mbedTLS | Apache-2.0（双许可，另有 GPL-2.0） |

分发二进制时注意：mbedTLS 的 Apache-2.0 条款要求保留其许可证与声明，`3rd/mbedtls/LICENSE`
需随发行物一起带上。