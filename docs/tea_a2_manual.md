# 使用手册

**这一页是「命令清单」**：tea_a2 里你真正会敲的东西都在这里——命令行参数、curl 配方、
队列命令、微信命令、脚本、环境变量。每一节都能直接复制粘贴。

README 讲的是「这是什么、为什么这么设计」；[HTTP 接口](api.md) 讲的是每个接口的字段与
语义；[任务队列](tasks.md) 讲队列的策略。这一页讲**怎么敲**。

## 目录

| 想做的事 | 去哪一节 |
| --- | --- |
| 起服务、改端口、换模型、看日志 | [1. 起服务](#1-起服务) |
| 用 curl 让 agent 干活 | [2. agent 的 HTTP 命令](#2-agent-的-http-命令) |
| 排队、查进度、取消、重做 | [3. 任务队列的命令](#3-任务队列的命令) |
| 用麦克风说话（语音输入） | [2.12](#212-语音输入) |
| 让这台机器去控制另一台 tea_a2（级联） | [2.13](#213-级联控制另一台-tea_a2) |
| 定时做一件活儿（`not_before`、微信 `/at`） | [3.2.1](#321-定时做一件活儿) · [4](#4-聊天端的命令) |
| 在微信 / Telegram 里排队 | [4. 聊天端的命令](#4-聊天端的命令) |
| 写脚本调 agent | [5. 给脚本用的客户端](#5-给脚本用的客户端) |
| 构建、测试、装成服务 | [6. 脚本](#6-脚本) |
| 改配置、换 provider | [7. 配置的命令](#7-配置的命令) |
| 出问题了 | [8. 排错](#8-排错) |

---

## 1. 起服务

### 1.1 最常见的三条

```bash
# 起服务，把当前目录注册成工作区，打开浏览器
build/bin/tea_agent --workspace . --open

# 换个端口（本机默认 8282）
build/bin/tea_agent --port 9000

# 换 provider / 模型（临时，不改配置）
build/bin/tea_agent --provider minimax --model MiniMax-M3.1-Flash
```

启动后终端会打印一段横幅：版本、git hash、**Web UI 的地址**、当前 provider/model、
HTTP 引擎、可用工具、工作区列表。地址那行就是要打开的。

### 1.2 命令行参数全表

`tea_agent --help` 是权威版本，这里按用途分组抄一份：

**服务**

| 参数 | 作用 | 默认 |
| --- | --- | --- |
| `--host <addr>` | 监听地址。`0.0.0.0` = 所有网卡（**见下面的安全须知**） | `127.0.0.1` |
| `--port <n>` | 端口。必须是 1–65535 的数字，写错直接报错退出 | `8282` |
| `--web-dir <path>` | 从磁盘目录伺服界面，而不是用嵌在二进制里的那份（改前端时用） | 用内置的 |
| `--open` | 启动后用默认浏览器打开界面 | 不打开 |

**模型**

| 参数 | 作用 | 默认 |
| --- | --- | --- |
| `--config <path>` | provider 配置文件 | `$HOME/.tea_agent/provider.yaml` |
| `--tea-home <path>` | agent 主目录（配置、日志、工作区索引都在这） | `$HOME/.tea_agent` |
| `--provider <name>` | 这一轮用哪个 provider | 配置文件里的第一个 |
| `--model <name>` | 用哪个模型 | 该 provider 的第一个模型 |
| `--http-engine <name>` | `auto` / `socket` / `winhttp` | `auto` |
| `--ca-bundle <path>` | 信任的根证书 PEM | 二进制里嵌的那份 |
| `--insecure` | 不校验 TLS 证书（**只用于测试**） | 校验 |

**工作区**

| 参数 | 作用 |
| --- | --- |
| `--workspace <path>` | 注册一个工作区，可重复给多次 |
| `--workspace-root <dir>` | 把 `<dir>` 下所有一级子目录都注册成工作区 |
| `--allow-outside-workspace` | 取消文件工具的目录围栏（**慎用**，见安全须知） |

**诊断**

| 参数 | 作用 | 默认 |
| --- | --- | --- |
| `--log-level <level>` | 写进日志文件的级别：`trace`/`debug`/`info`/`warn`/`error`/`off` | `info` |
| `--console-level <level>` | 回声到终端的级别（同一组取值） | `warn` |
| `--log-file <path>` | 全局日志文件 | `$HOME/.tea_agent/logs/tea_agent.log` |
| `--no-console` / `--quiet` | 终端什么都不回声（`--quiet` 连启动横幅也不打） | 关 |
| `--help` / `-h` | 打印参数表 | |
| `--version` / `-V` | 打印版本 | |
| `--selftest` | 跑内置自检（输出过滤器）然后退出 | |

**两个级别是分开的**，这是刻意的：服务长期挂在终端里，每次请求的 INFO 会把真正要紧的
WARNING 顶出屏幕。排查时 `--console-level debug` 就能在终端跟着看一次完整复现。

级别名写错不会静默吞掉：终端会打印一行
`warning: --console-level 'bogus' is not a level; using warn`，然后按 `warn` 处理。

### 1.3 环境变量

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `TEA_AGENT_HOME` | 覆盖 agent 主目录（等于 `--tea-home`） | `$HOME/.tea_agent` |
| `TEA_AGENT_HTTP_THREADS_MAX` | HTTP 线程池上限（每条 SSE 占一个线程） | `128`（下限 16） |
| `TEA_AGENT_MAX_SUBSCRIBERS` | 一轮能被几个浏览器同时跟着看 | `8` |
| `TEA_AGENT_MAX_ACTIVE_TURNS` | 同时能跑几轮 | `24` |
| `HTTP_PROXY` / `HTTPS_PROXY` | 代理（WinHTTP 后端会看） | |

`TEA_AGENT_HOME` 与 `--tea-home` 同时给，以**命令行**为准。

队列和聊天端（微信 / Telegram）另有一组自己的环境变量，见
[3.8](#38-队列的命令行小抄) 与 [4](#4-聊天端的命令)。

### 1.4 ⚠️ 安全须知

**这个程序没有任何认证机制。** `toolkit_exec` 能执行任意系统命令，所以：

- 默认绑 `127.0.0.1`，只允许本机访问——**请保持这个默认值**；
- 用 `--host 0.0.0.0` 或放到公网之前，必须自己在前面挡一层认证（反向代理 + token /
  mTLS / VPN），否则等同于把设备的 shell 暴露出去；
- `--allow-outside-workspace` 会取消文件工具的目录围栏：放宽后任何能访问这个端口的人
  都能读写整台机器上的文件。

### 1.5 第一次跑：配置模式

`$HOME/.tea_agent/provider.yaml` **不存在**时，服务不会退出，而是进入「配置模式」。
界面**不会**被一个对话框挡住：对话、文件、记忆、日志这几项要经过模型，暂时置灰；
**终端**和**设置**照常可用（终端不经过模型）。到「设置」页里配置好 provider.yaml，
那几项就都使能了。对话框里有两条路：

1. **快捷开始**：下拉选 provider，粘贴 API key，回车。服务端据此生成一份只含该 provider
   的 `provider.yaml`，按与上传完全相同的流程校验、落盘（POSIX 上权限 `0600`）、热加载。
   key 不会出现在任何响应里。

   | provider | 粘贴的 key | 默认模型 | 生成的配置里还有什么 |
   | --- | --- | --- | --- |
   | **DeepSeek** | `sk-…` | `deepseek-flash` | 另附 `deepseek-chat` / `deepseek-reasoner` |
   | **MiniMax** | M Plan 的 `sk-cp-…` | `MiniMax-M3.1-Flash` | 控制台链接（`usage_console_url`，**不发请求**） |
   | **小米 MiMo** | `sk-…`（按量付费）/ `tp-…`、`ttp-…`（Token Plan） | `mimo-v2.6-flash` | 另附 `mimo-v2.6-pro`；控制台链接；`reasoning_content: keep` |

   MiMo 的网关由 **key 的形状**决定：`sk-…` → `https://api.xiaomimimo.com/v1`，
   `tp-…` / `ttp-…` → `https://token-plan-cn.xiaomimimo.com/v1`，两者不能混用（用错是 401）。
   生成的配置还带 `reasoning_content: keep`：v2.6 模型默认思考，小米的文档要求思考模式下
   把上一轮的 `reasoning_content` 原样带回去，程序本来就带，写进文件是为了不依赖端点猜测。

   生成的配置**不查额度**（`supports_usage` 关着）：这份文件一般是一份配置发给一大片设备，
   每台设备都去问一次订阅还剩多少，就是一个账号上成千上万个请求，最容易把账号弄进风控。
   要徽标就把生成的 yaml 里那两行注释放开（key 得是 M Plan 订阅 key `sk-cp-`）。
   自己上传的 `provider.yaml` 不受这条限制。

2. **上传 / 粘贴完整配置**：把 `provider.yaml` 拖进对话框，或把内容粘进文本框，点
   「上传并应用」。

对话框里还有一个按钮：

- **「重新检测」**：让服务端**重新读一遍磁盘上的** `provider.yaml`。设备上常常是先把
  文件写好（或从别处 scp 过来）再让它生效——光刷新网页没用：配置在服务端的内存里。

命令行等价物（不用浏览器）：

```bash
# 快捷开始：只填 key
curl -X POST http://127.0.0.1:8282/api/config/quickstart \
     -H 'Content-Type: application/json' \
     -d '{"provider":"deepseek","api_key":"sk-..."}'
# → {"ok":true,"provider":"DeepSeek","model":"deepseek-flash",
#    "models":["deepseek-flash","deepseek-chat","deepseek-reasoner"],"path":"..."}

# 小米 MiMo：key 的形状决定网关（sk-… 按量付费，tp-…/ttp-… Token Plan）
curl -X POST http://127.0.0.1:8282/api/config/quickstart \
     -H 'Content-Type: application/json' \
     -d '{"provider":"mimo","api_key":"tp-..."}'
# → {"ok":true,"provider":"mimo","model":"mimo-v2.6-flash",
#    "models":["mimo-v2.6-flash","mimo-v2.6-pro"],"path":"..."}

# 上传完整配置
curl -X POST http://127.0.0.1:8282/api/config \
     -H 'Content-Type: application/json' \
     -d '{"yaml":"version: 1\nproviders:\n  ..."}'

# 重新读一遍磁盘上的 provider.yaml（文件是手写/拷过来的时候用）
curl -X POST http://127.0.0.1:8282/api/config/reload
# → {"ok":true,"path":"...","providers":["DeepSeek"],"active":{...}}
#    文件不存在 → 404 config_missing；文件坏了 → 400 invalid_config（当前配置不动）
```

两条路都是**先校验再落盘**：校验失败不会覆盖已有文件。有轮次正在跑时也会被拒绝（409）。

> 只有**文件不存在**才进配置模式。文件存在但 YAML 语法错误会**直接报错退出**——那种情况
> 静默启动只会把问题藏起来。

---

## 2. agent 的 HTTP 命令

接口在 `127.0.0.1:8282`（或你给的 `--port`）。下面的 `$B` 就是它：

```bash
B=http://127.0.0.1:8282
```

### 2.1 探活与自检

```bash
curl -s $B/api/health
# {"ok":true,"version":"0.16.1","git_hash":"2e9185f1d5c4","provider":"deepseek",
#  "model":"deepseek-flash","http_engine":"auto","tea_home":"/home/me/.tea_agent",
#  "config_loaded":true,"uptime_s":42.1}

curl -s $B/api/config/status     # 配置在哪儿、加载了没、有哪些 provider
curl -s $B/api/providers          # provider 清单与能力标记（不含 key）
curl -s $B/api/tools              # 工具定义与 JSON Schema

# 这个 provider 的额度还剩多少（key 只在服务端用，客户端看不到）
curl -s $B/api/providers/minimax/usage
# {"ok":true,"provider":"minimax","percent":75.0,...}
```

`config_loaded:false` 表示还没配置好——这时候 `POST /api/chat` 会返回
`409 config_missing`，而不是闷头跑。

额度那一行：provider 没声明 `supports_usage` 时是 `400 usage_not_supported`，
上游查不到是 `502 usage_failed`，provider 名写错是 `404`。

### 2.2 下发一轮对话（主入口）

```bash
curl -N -X POST $B/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"workspace_id":"ws_xxx","message":"检查 /etc/tea 下的配置文件，把端口改成 9000"}'
```

`-N` 是关键：不加它 curl 会把 SSE 缓冲住，你看不到流式输出。

响应是 SSE，每行 `data: {json}`，按 `type` 分派：

| `type` | 含义 |
| --- | --- |
| `start` | 本轮开始（session / provider / model / `messages_before`） |
| `reasoning_delta` | 思考流 |
| `content_delta` | 正文增量 |
| `tool_call` / `tool_start` | 模型请求的工具 / 即将执行 |
| `tool_result` | `ok` / `elapsed_ms` / `output` / `truncated` |
| `iteration_end` | 单轮结束，带 `usage` 与 `metrics` |
| `queue_adopted` | 采纳了一条排队消息 |
| `queue_unconsumed` | 轮次结束时仍未采纳的排队消息（客户端应放回输入框） |
| `todo` | 任务列表变了（`items[]` / `total` / `done`） |
| `artifacts` | 本轮产出了新文件（`items[]`） |
| `tool_images` | 有图片被附加进对话 |
| `retry` | 服务端繁忙，等一会儿自动重试（`kind` / `http_status` / `delay_ms` / `attempt` / `max_attempts`） |
| `interrupted` | 中断时补齐了 N 个没有执行的工具调用（`filled_tool_calls`） |
| `done` | 全部结束，带整轮的 `usage` / `metrics` / `content` / `trajectory_id` |
| `stopped` | 被 `/api/chat/stop` 中断 |
| `error` | `message` / `detail` |
| `ping` | 空闲心跳（15 s），用来判断链路还活着 |
| `resync` | 回放缓冲溢出：重新拉一次 `/api/sessions/{sid}` 再接着收 |

每帧带一个从 1 递增的 `seq`（`ping` 除外），**断线重连靠它对齐**。

发图片（需要模型声明 `supports_vision: true`，否则返回 `400 vision_unsupported`）：

```bash
curl -N -X POST $B/api/chat -H 'Content-Type: application/json' \
  -d '{"workspace_id":"ws_xxx","message":"看看这个图",
       "images":[{"filename":"a.png","data":"<base64>"}]}'
```

一次最多 4 张、单张 8 MB。服务端按**内容**嗅探真实类型，落到工作区的
`.tea_agent_run/artifacts/`，会话里只记路径。

### 2.3 同一会话正在跑时：排队 / 顶掉

**默认是排队，不是顶掉**——另一个窗口正在看这个会话时，你手快不该把人家的活儿扔掉。

```bash
# 该会话已有轮次在跑：200 + JSON，消息排队，没有新开轮次
curl -s -X POST $B/api/chat -H 'Content-Type: application/json' \
  -d '{"workspace_id":"ws_xxx","session_id":"s_xxx","message":"顺便把日志级别改成 debug"}'
# {"ok":true,"queued":{"id":"q_...","content":"..."},"pending":1,"turn_running":true}

# 明确要顶掉正在跑的那一轮（对所有人）
curl -sN -X POST $B/api/chat -H 'Content-Type: application/json' \
  -d '{"workspace_id":"ws_xxx","session_id":"s_xxx","message":"换个方向","interrupt":true}'
```

`interrupt` 会**等旧 worker 收尾落盘**（最多 15 秒）才开新一轮——旧 worker 手里攥着
自己那份会话，提前重启会把新消息覆盖掉。15 秒内停不下来返回 `409 turn_still_running`。

只想排队、不要 SSE 的客户端用这两个接口：

```bash
# 插队
curl -s -X POST $B/api/chat/queue -H 'Content-Type: application/json' \
  -d '{"session_id":"s_xxx","message":"顺便把日志级别改成 debug"}'

# 取消一条（带 id）或全部（不带 id）
curl -s -X POST $B/api/chat/queue/cancel -H 'Content-Type: application/json' \
  -d '{"session_id":"s_xxx","id":"q_..."}'

# 看排队
curl -s "$B/api/chat/queue?session_id=s_xxx"
```

### 2.4 中断、查询、接回去

中断是「**尽量**结束」：

- **能安全停掉的工具直接停掉**。`toolkit_exec` 跑的命令会被整个进程组杀掉
  （先 SIGTERM，再 SIGKILL），所以一条跑一分钟的命令不会把这一轮拖成一分钟；
  web 工具（search / fetch）的 HTTP 请求当场断开。
- **停在一半的工具会留下痕迹，而且痕迹会被记下来**。命令被杀之后，它已经写下或
  改过的东西**还在**——这没法回滚，所以工具结果里会写明「命令被中断，可能只跑了一
  部分」，下一轮看得到，不会把没做完的活当成做完了。
- **会话不会因此坏掉**。模型发起的每个工具调用都必须有对应的结果，否则下一次请求
  会被 provider 拒掉（MiniMax：`invalid params, 400 (2013)`）。中断时没来得及执行的
  调用会被补上一个「没有执行」的结果（界面上是「已中断」），会话因此还能继续用。
- 被中断的这一轮在轨迹里标成「已中断」，不是「失败」。


```bash
# 中断当前轮次（**必须带 session_id**，否则 cancelled 一直是 false）
curl -s -X POST $B/api/chat/stop -H 'Content-Type: application/json' \
  -d '{"session_id":"s_xxx"}'
# → {"ok":true,"cancelled":true}

# 这个会话还有轮次在跑吗
curl -s "$B/api/chat/turn?session_id=s_xxx"
# {"ok":true,"running":true,"session_id":"s_xxx","last_seq":137,"oldest_seq":1,
#  "buffered":137,"dropped":0,"pending":0,"subscribers":2,"workspace_id":"ws_xxx"}

# 所有还在跑的轮次
curl -s $B/api/chat/turns
```

关掉页面**不会**取消这一轮：轮次的生命周期与那条 HTTP 连接是分开的。要接回去：

```bash
curl -N -X POST $B/api/chat/attach -H 'Content-Type: application/json' \
  -d '{"session_id":"s_xxx","since":42}'
# since 省略或 0 = 从头回放
```

跟随者超过 `TEA_AGENT_MAX_SUBSCRIBERS`（默认 8）会被 `429 too_many_subscribers` 拒绝。

### 2.5 工作区

```bash
# 列工作区
curl -s $B/api/workspaces
# {"index_path":"...","workspaces":[{"id":"ws_...","name":"tea_a2","path":"/home/me/work",
#                                   "run_dir":"...","sessions":3,"exists":true}]}

# 注册一个（path 必须是绝对路径）
curl -s -X POST $B/api/workspaces -H 'Content-Type: application/json' \
  -d '{"path":"/home/me/work/proj","name":"proj"}'

# 注销一个工作区（**只从索引里去掉，文件一个都不删**）
curl -s -X DELETE $B/api/workspaces/ws_xxx

# 列目录
curl -s "$B/api/workspaces/ws_xxx/tree?path=src"

# 在工作区里搜（文件名 + 内容；查询串按原样逐字节比较，不做正则/模糊匹配）
curl -s "$B/api/workspaces/ws_xxx/search?q=toolkit_workspace_search"
# 只搜文件名 / 只搜内容 / 完全匹配（整串相等）/ 区分大小写 / 只搜某个子目录
curl -s "$B/api/workspaces/ws_xxx/search?q=tools.cpp&scope=name&exact=1"
curl -s "$B/api/workspaces/ws_xxx/search?q=TODO&scope=content&path=src"

# 搜历史对话（标题 + 说过的内容 + 轨迹里的工具输出）
curl -s --get "$B/api/workspaces/ws_xxx/sessions/search" --data-urlencode "q=水獭"
# 只看轨迹 / 只看标题 / 完全匹配
curl -s "$B/api/workspaces/ws_xxx/sessions/search?q=otter&scope=trajectory"
curl -s "$B/api/workspaces/ws_xxx/sessions/search?q=%E6%B0%B4%E7%8D%AD&scope=title&exact=1"

# 读 / 写文件
curl -s "$B/api/workspaces/ws_xxx/file?path=README.md"
curl -s -X PUT "$B/api/workspaces/ws_xxx/file" -H 'Content-Type: application/json' \
  -d '{"path":"notes.txt","content":"你好\n"}'

# 原始字节（图片 / PDF，16 MB 上限）
curl -s "$B/api/workspaces/ws_xxx/raw?path=build/logo.png" -o logo.png

# 附件下载（流式，无 16 MB 限制；&inline=1 改成浏览器里直接打开）
curl -OJ "$B/api/workspaces/ws_xxx/download?path=build/app.bin"
```

### 2.6 会话

```bash
# 列表 / 新建
curl -s "$B/api/workspaces/ws_xxx/sessions"
curl -s -X POST "$B/api/workspaces/ws_xxx/sessions" -H 'Content-Type: application/json' \
  -d '{"title":"我的会话"}'

# 取完整会话（含 reasoning、工具调用与结果）
curl -s $B/api/sessions/s_xxx

# 改名 / 删除
curl -s -X PATCH $B/api/sessions/s_xxx -H 'Content-Type: application/json' \
  -d '{"title":"新标题"}'
curl -s -X DELETE $B/api/sessions/s_xxx

# 从某一轮分叉（message_index 是父会话里某条**用户消息**的下标，0 起）
curl -s -X POST $B/api/sessions/s_xxx/fork -H 'Content-Type: application/json' \
  -d '{"message_index":2}'
```

分叉是**复制**：父会话一个字节都不动，新会话带 `forked_from` / `fork_turn`。

> 轮次进行中时，改名 / 删除 / 改任务列表一律返回 `409 turn_running`——worker 会在下一次
> 迭代把改动写回去。等它结束再来。

### 2.7 任务列表（todo）

列表随 `GET /api/sessions/{sid}` 一起返回；**修改**走 `PUT`：

```bash
# 整体替换
curl -s -X PUT $B/api/sessions/s_xxx/todos -H 'Content-Type: application/json' \
  -d '{"items":[{"text":"第一步","status":"pending"},{"text":"第二步","status":"pending"}]}'
# {"ok":true,"items":[...],"total":2,"done":0}

# 改一条：按 id，或者按 **1 起算** 的序号
curl -s -X PUT $B/api/sessions/s_xxx/todos -H 'Content-Type: application/json' \
  -d '{"id":"t1","status":"done"}'
curl -s -X PUT $B/api/sessions/s_xxx/todos -H 'Content-Type: application/json' \
  -d '{"index":1,"status":"active"}'
```

`status` 只能是 `pending` / `active` / `done`，写别的返回
`400 status must be pending, active or done`。序号是 **1 起算**（`index:0` 找不到条目）。

**全部做完的列表会被清掉**（系统行为，不用手动 `clear`）。

### 2.8 记忆、人设、经验、日志

```bash
# 工作区长期记忆（跨会话保留）
curl -s "$B/api/workspaces/ws_xxx/memory"
curl -s -X POST "$B/api/workspaces/ws_xxx/memory" -H 'Content-Type: application/json' \
  -d '{"text":"这个项目用 pnpm，不要用 npm"}'

# 删掉一条记忆（id 形如 n_xxx，从 GET /memory 的返回里拿）
curl -s -X DELETE "$B/api/workspaces/ws_xxx/memory/n_xxx"

# system prompt（current_prompt 是当前真正生效的人设，content 是磁盘上的覆盖）
curl -s "$B/api/workspaces/ws_xxx/system-prompt"
curl -s -X PUT "$B/api/workspaces/ws_xxx/system-prompt" -H 'Content-Type: application/json' \
  -d '{"content":"你是一个只说中文的助手"}'

# 工作区经验（.tea_agent_run/agent.md）
curl -s "$B/api/workspaces/ws_xxx/agent-doc"
curl -s -X PUT "$B/api/workspaces/ws_xxx/agent-doc" -H 'Content-Type: application/json' \
  -d '{"content":"这个仓库用 pnpm，不要用 npm"}'

# 运行日志
curl -s "$B/api/workspaces/ws_xxx/logs?tail=300"

# 工作区设置（文件访问范围等）
curl -s "$B/api/workspaces/ws_xxx/settings"
curl -s -X PUT "$B/api/workspaces/ws_xxx/settings" -H 'Content-Type: application/json' \
  -d '{"allow_outside_workspace":true}'
```

保存 system prompt 后**下一轮**生效（每轮重新读盘）。

### 2.9 轨迹与导出

```bash
# 每轮轨迹摘要，最新在前
curl -s "$B/api/sessions/s_xxx/trajectory"
curl -s "$B/api/sessions/s_xxx/trajectory?full=1"      # 带步骤与文件变更
curl -s "$B/api/sessions/s_xxx/trajectory?format=jsonl" # 原始 JSONL

# 单轮完整轨迹（turn_id 就是 done 事件里的 trajectory_id）
curl -s "$B/api/turns/t_xxx/trajectory"

# 导出一个自包含的 HTML（离线可读）
curl -sJ "$B/api/sessions/s_xxx/export" -o session.html

# 把导出的 HTML 导回去（新建一个会话，不覆盖）
curl -s -X POST "$B/api/workspaces/ws_xxx/sessions/import" \
     -H 'Content-Type: text/html' --data-binary @session.html
# 也接受 {"html":"..."} 或会话 JSON
```

### 2.10 打包下载与销毁

```bash
# 把 .tea_agent_run 打成 zip（也可 ?path= 换成工作区里的别的目录）
curl -sJ "$B/api/workspaces/ws_xxx/archive?path=.tea_agent_run" -o run.zip
# 响应头：X-Archive-Entries / X-Archive-Skipped / X-Archive-Uncompressed

# 销毁：先预览（不动文件）
curl -s $B/api/destroy

# 真删（不可逆，必须带 confirm；有轮次在跑时 409）
curl -s -X POST $B/api/destroy -H 'Content-Type: application/json' \
  -d '{"confirm":"destroy"}'
```

### 2.11 绕过 agent，直接问模型

`/api/chat` 跑的是完整的一圈（模型 → 工具 → 模型 …）。只是要一次模型回答，用
`/api/llm/chat`——**不执行任何工具**，答案直接回来：

```bash
curl -s -X POST $B/api/llm/chat -H 'Content-Type: application/json' \
  -d '{"prompt":"用一句话说：什么是 SSE","max_tokens":64}'
# {"ok":true,"provider":"deepseek","model":"deepseek-flash","content":"...",
#  "finish_reason":"stop","elapsed_ms":812,"usage":{...}}
```

带 `messages` 就是自己拼历史；带 `tools` 就是把工具定义交给模型——**它只会返回
工具调用，不会替你执行**，要执行得你自己来（那正是 `/api/chat` 做的事）。

### 2.12 语音输入

界面上的麦克风按钮（输入框旁边）把说的话变成文字**填进输入框**——不自动发送，
转写有错还能改。

它要一个转写服务，在 provider.yaml 的 `asr:` 段里配：

```yaml
asr:
  url: http://127.0.0.1:8284
  model: sense-voice
```

**本地跑 SenseVoiceSmall**（仓库自带，模型 ~240 MB，只在第一次需要下载）：

```bash
pip install sherpa-onnx                        # 唯一的额外依赖
python3 sched/asr_server.py --download          # 拉模型到 ~/.tea_agent/asr
python3 sched/asr_server.py                     # 起服务（127.0.0.1:8284）
```

服务只听回环地址：录音不该在没有认证的情况下暴露给局域网。转写服务换成任何
OpenAI 兼容 `/v1/audio/transcriptions` 的服务都行（云上的也行），把 `url` 指过去、
需要 key 就加 `api_key:`。

命令行也能转写：

```bash
# 看服务配了没有
curl -s $B/api/asr/status
# {"configured":true,"url":"http://127.0.0.1:8284/v1/audio/transcriptions",...}

# 转写一个 wav（请求体是原始 WAV 字节）
curl -s -X POST $B/api/asr/transcribe -H 'Content-Type: audio/wav' \
     --data-binary @hello.wav
# {"ok":true,"text":"开饭时间早上9点至下午5点。","elapsed_ms":181}
```

**看不到麦克风按钮？** 三种可能：

1. provider.yaml 里没有 `asr:` 段（或者 url 不是 http(s)）——查 `GET /api/asr/status`。
2. 浏览器只在**安全上下文**里给麦克风权限：https 或 localhost。用
   `http://192.168.x.x:8282` 访问设备时浏览器不给权限，所以按钮不出现。
3. 浏览器不支持 `MediaRecorder`（很老的浏览器）。

### 2.6 MCP 服务器

**和 provider.yaml 同目录**的 `mcp.yaml` 里配的 MCP 服务器（Model Context Protocol）是
**独立的程序**：agent 启动它们，用 stdio 上的 JSON-RPC 跟它们说话，它们提供的工具变成模型
可以调用的工具（名字是 `mcp__<服务器>__<工具>`）。

```yaml
# mcp.yaml
servers:
  uiautomation:
    command: 'C:\Tools\uiautomation-mcp\uiautomation-mcp.exe'
    timeout_ms: 60000
```

```bash
# 每个服务器的状态和它提供的工具
curl -s $B/api/mcp/servers
# {"configured":true,"enabled":true,"servers":[{"name":"uiautomation","connected":true,
#   "protocol_version":"2024-11-05","server_name":"UIAutomationMCP","tool_count":31,
#   "tools":[{"name":"SearchElements","full_name":"mcp__uiautomation__SearchElements"}]}]}

# 重新读一遍 mcp.yaml 并重新连接所有服务器
curl -s -X POST $B/api/mcp/refresh

# 不用模型，直接调一个工具试试
curl -s -X POST $B/api/mcp/servers/uiautomation/call \
  -H 'Content-Type: application/json' \
  -d '{"tool":"mcp__uiautomation__SearchElements","arguments":{"name":"OK"}}'
# {"ok":true,"tool":"mcp__uiautomation__SearchElements","is_error":false,"text":"..."}
```

服务器是**按需启动**的：第一次用到它的工具时才拉起来。连不上的服务器在
`/api/mcp/servers` 里写着原因（`error`），别的服务器照常工作。细节见
[配置](config.md#mcp-服务器mcp-yaml)。

### 2.13 级联：控制另一台 tea_a2

被控端加一个令牌（`--api-token`）就多出一组**机器对机器**的路由：
`/api/peer/<路由>` 是 `/api/<路由>` 的令牌版镜像。没配令牌时这些路由**不存在**（404）。

```bash
# 被控端
tea_agent --host 0.0.0.0 --port 8383 --api-token "$(cat /etc/tea/peer-token)"

# 控制端：问对端自己的 agent（SSE，边跑边收）
curl -N -X POST http://peer:8383/api/peer/chat \
     -H "Authorization: Bearer $PEER_TOKEN" -H "Content-Type: application/json" \
     -d '{"workspace_id":"ws_9f2c","message":"这台机器现在有什么在跑？"}'

# 让对端跑一条命令（不经过模型）
curl -s -X POST http://peer:8383/api/peer/exec \
     -H "Authorization: Bearer $PEER_TOKEN" -H "Content-Type: application/json" \
     -d '{"command":"uptime","timeout_ms":5000}'

# 对端在不在 / 什么状态 / 有哪些工具 / 哪些工作区
curl -s -H "Authorization: Bearer $PEER_TOKEN" http://peer:8383/api/peer/health
curl -s -H "Authorization: Bearer $PEER_TOKEN" http://peer:8383/api/peer/status
curl -s -H "Authorization: Bearer $PEER_TOKEN" http://peer:8383/api/peer/tools
curl -s -H "Authorization: Bearer $PEER_TOKEN" http://peer:8383/api/peer/workspaces

# 停掉对端正在跑的一轮
curl -s -X POST http://peer:8383/api/peer/chat/stop \
     -H "Authorization: Bearer $PEER_TOKEN" -H "Content-Type: application/json" \
     -d '{"session_id":"s_xxx"}'
```

想让**模型**去控制对端（而不是脚本），在 settings 里配 `peers:` 段（名字 + 地址 + 令牌），
模型就能用 `toolkit_peer` 这个工具（`chat` / `exec` / `status` / …）指名道姓地问对端；
不配也能用 `url=` 临时指一台地址，但那样没有令牌。细节见 [级联](peer.md)。

---

---

## 3. 任务队列的命令

队列是**可选**的伴生件（Python，标准库，设备上没 pip 也能跑）。agent 单独就能工作。

### 3.1 起队列服务

```bash
python3 sched/task_server.py --agent http://127.0.0.1:8282 --port 8283
```

参数全表（`python3 sched/task_server.py --help` 是权威版本）：

| 参数 | 作用 | 默认 |
| --- | --- | --- |
| `--agent <url>` | agent 的 HTTP API 地址 | `http://127.0.0.1:8282` |
| `--host <addr>` | 队列自己监听哪 | `127.0.0.1` |
| `--port <n>` | 队列自己监听哪个端口 | `8283` |
| `--data-dir <path>` | 任务记录放在哪 | `<workspace-path>/tasks` |
| `--workspace <id>` | 这一份队列服务做哪个工作区的活儿 | 队列自己的 |
| `--workspace-path <path>` | 队列独占的工作区目录 | `~/.tea_agent/task-workspace` |
| `--workspace-name <name>` | 它在 agent 里叫什么名字 | `任务` |
| `--provider <name>` | 用谁的额度当闸门 | `minimax` |
| `--window <w>` | 看哪个额度窗口：`window` / `weekly` | `window` |
| `--min-remaining <pct>` | 余额低于这个百分比就不开工 | `5.0` |
| `--allow-without-quota` | provider 不报额度时也照做 | 关 |
| `--max-concurrent <n>` | 全局同时跑几个（0 = 只按项目算） | `0` |
| `--quota-interval <s>` | 额度答案复用多久 | `60` |
| `--stale-after <s>` | 多久没动就不占位置了 | `300` |
| `--interval <s>` | 调度器几秒看一轮队列 | `2` |
| `--max-per-round <n>` | 一轮最多起几个任务 | `4` |
| `--chat-timeout <s>` | 一轮对话最多等多久（0 = 不限） | `3600` |
| `--sse-idle-timeout <s>` | 事件流多久没人说话就关掉 | `3600` |
| `--submit <PROMPT>` | 排一个任务然后退出（**不需要服务在跑**） | |
| `--title <t>` | `--submit` 的标题 | |
| `--allow-path <dir>` | 这个任务能额外碰哪个目录（可重复） | |
| `--no-serve` | 不开 HTTP 服务，只跑调度器（cron 式用法） | 关 |

### 3.2 排一个任务

三种办法，任选：

```bash
# 1) 命令行：不用服务在跑，直接写进队列
python3 sched/task_server.py --agent http://127.0.0.1:8383 \
        --submit "把 README 里的例子都跑一遍" --title "例子"

# 2) HTTP API
curl -X POST http://127.0.0.1:8283/api/tasks \
     -H 'Content-Type: application/json' \
     -d '{"prompt":"把 README 里的例子都跑一遍","title":"例子"}'
# → {"code":0,"message":"ok","request_id":"req_...","data":{"id":"t_...","status":"queued",...}}

# 3) 网页：打开 http://127.0.0.1:8283/ ，写一句要做的事，点「排队」
```

能带的字段：`prompt`（必填）、`title`、`workspace_id`（默认第一个工作区）、
`provider`、`model`、`allow_paths`、`meta`、`priority`、`idempotency_key`、
`not_before`（**定时**）。

### 3.2.1 定时做一件活儿

给任务一个「最早什么时候开始」的时间：

```bash
# 五小时后
curl -X POST http://127.0.0.1:8283/api/tasks -H 'Content-Type: application/json' \
     -d '{"prompt":"把 README 的例子跑一遍","not_before":"+5"}'

# 今天 23:30（已经过了就尽快做，**不是明天**）
curl -X POST http://127.0.0.1:8283/api/tasks -H 'Content-Type: application/json' \
     -d '{"prompt":"把 README 的例子跑一遍","not_before":"23:30"}'

# 绝对时间
curl -X POST http://127.0.0.1:8283/api/tasks -H 'Content-Type: application/json' \
     -d '{"prompt":"把 README 的例子跑一遍","not_before":"2026-10-24T09:00:00Z"}'
```

写法一共这几种：`+5`（五小时后）、`+30m`（半小时后）、`+2d`（两天后）、
`23:30`（今天这个钟点，过了就尽快做）、`2026-10-24T09:00:00Z`（绝对时间）。
看不懂就返回 `400 bad_request`，`data.error` 里说明它没听懂。

**到点之前这个任务不会被启动，但它也不挡着后面的任务**——「晚点做」不是
「排在第几位」。

同一个 `Idempotency-Key` 重复提交**只排一次**（客户端重试、机器人重复发消息都靠它）：

```bash
curl -X POST http://127.0.0.1:8283/api/tasks \
     -H 'Content-Type: application/json' -H 'Idempotency-Key: run-2026-10-23-001' \
     -d '{"prompt":"..."}'
```

### 3.3 看队列

```bash
# 全部
curl -s http://127.0.0.1:8283/api/tasks

# 过滤 + 分页
curl -s "http://127.0.0.1:8283/api/tasks?status=queued&page=1&page_size=20"
curl -s "http://127.0.0.1:8283/api/tasks?created_from=2026-10-23T00:00:00Z"
curl -s "http://127.0.0.1:8283/api/tasks?tag=nightly&business_id=order-42"

# 一个任务（include=full 带上事件与流水）
curl -s http://127.0.0.1:8283/api/tasks/t_xxx
curl -s "http://127.0.0.1:8283/api/tasks/t_xxx?include=full"

# 这个任务发生过什么（事件落盘，重启后还在）
curl -s "http://127.0.0.1:8283/api/tasks/t_xxx/events?since_event_id=0"

# 指标 / 审计
curl -s http://127.0.0.1:8283/api/health      # 队列计数、平均排队时长、失败率……
curl -s "http://127.0.0.1:8283/api/audit?task_id=t_xxx"
```

一条任务记录里除了状态和结果，还有：`not_before`（绝对时间）、`not_before_text`
（你写的那句话）、`starts_at_local`（本机时间，给界面显示）、`queue_position`
（前面还有几个）、`attempt`（第几次做）、`pending_message`（等确认时你给的答案）。

### 3.4 动一个任务

```bash
# 立刻做（仍然过额度闸门）
curl -s -X POST http://127.0.0.1:8283/api/tasks/t_xxx/start

# 停掉正在跑的
curl -s -X POST http://127.0.0.1:8283/api/tasks/t_xxx/cancel

# 回答一个「等你确认」的任务
curl -s -X POST http://127.0.0.1:8283/api/tasks/t_xxx/confirm \
     -H 'Content-Type: application/json' -d '{"message":"可以删"}'

# 重做一个失败的（同一个任务，attempt + 1，历史留着）
curl -s -X POST http://127.0.0.1:8283/api/tasks/t_xxx/retry

# 删掉
curl -s -X DELETE http://127.0.0.1:8283/api/tasks/t_xxx
```

> **回答不会抢占**：确认的回答让任务回到**队尾**，答案随任务带回去，不打断正在跑的活儿。
> 已经完成的任务不能 cancel（`409 already_finished`），只有 failed / cancelled 能 retry。

### 3.5 导出

```bash
curl -s "http://127.0.0.1:8283/api/tasks/t_xxx/export?format=json"
curl -sJ "http://127.0.0.1:8283/api/tasks/t_xxx/export?format=zip" -o task.zip
```

zip 里有 `manifest.json` / `final_answer.md` / `conversation.jsonl` / `events.jsonl` /
`artifacts/`。

### 3.6 按项目授权

**每个项目一把钥匙**。带钥匙就只看得到这个项目的任务、事件、导出和排队，别人的 → 403。

```bash
# 领一把钥匙（本机操作者）
curl -s -X POST http://127.0.0.1:8283/api/projects \
     -H 'Content-Type: application/json' -d '{"workspace_id":"ws_xxx"}'
# → {"code":0,"data":{"workspace_id":"ws_xxx","key":"XXXX..."}}

# 带着它
curl -s http://127.0.0.1:8283/api/tasks -H 'Authorization: Bearer XXXX...'
curl -s http://127.0.0.1:8283/api/tasks -H 'X-Project-Key: XXXX...'
```

**带钥匙的人领不到别人的钥匙**。不带钥匙的调用者是本机操作者（队列网页就是它），
所以队列服务只监听 `127.0.0.1`。

事件流也按钥匙过滤：带钥匙的订阅者只收到**自己项目**的事件，看不到别的项目在做什么。

### 3.7 回答长什么样

队列的每个 JSON 回答都是同一个信封：

```jsonc
{"code": 0, "message": "ok", "request_id": "req_xxxx", "data": { ... }}
```

成功 `code=0`、正文在 `data`；失败时 `code` 是编号（`40001` 参数、`40301` 越权、
`40401` 不存在、`40901` 状态冲突……），细节在 `data.error`。`request_id` 也在
`X-Request-Id` 头里——报障时把这个号说出来就能在日志里找到那一次请求。

**客户端要解包**：队列网页、微信 bot、测试里的 mock 都得先把 `data` 取出来。

### 3.8 队列的命令行小抄

```bash
# 排一条（不用服务在跑）
python3 sched/task_server.py --submit "做这件事" --title "标题"

# 让这个任务能碰工作区以外的目录
python3 sched/task_server.py --submit "整理 /data/raw 的日志" --allow-path /data/raw

# agent 不在默认端口时要指路
python3 sched/task_server.py --agent http://127.0.0.1:8383 --submit "..."

# 等价写法（环境变量）
TEA_AGENT_URL=http://127.0.0.1:8383 python3 sched/task_server.py --submit "..."
```

队列这一层有自己的环境变量：

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `TEA_AGENT_URL` | agent 的地址 | `http://127.0.0.1:8282` |
| `TEA_TASK_PORT` | 队列自己监听哪个端口 | `8283` |
| `TEA_TASK_WORKSPACE` | 队列独占的工作区目录 | `~/.tea_agent/task-workspace` |
| `TEA_TASK_DATA` | 任务记录放在哪 | `<工作区>/tasks` |
| `TEA_TASK_PROVIDER` | 用谁的额度当闸门 | `minimax` |
| `TEA_AGENT_WORKSPACE` | 这一份队列做哪个工作区的活儿 | 队列自己的 |
| `TEA_TASK_WORKSPACE_NAME` | 它在 agent 里叫什么名字 | `任务` |

---

## 4. 聊天端的命令

队列有两个聊天前端：**微信**（[4.1](#41-微信端的命令)）和 **Telegram**
（[4.2](#42-telegram-端的命令)）。发一句话就是排一个任务，做完聊天里会收到结果。
两边的命令一模一样（它们共用 `sched/channel.py`）。

### 4.1 微信端的命令

微信是队列的另一个前端：发一句话就是排一个任务，做完微信里会收到结果。

```bash
# 扫码登录（把打印出来的链接在手机上用微信扫）
python3 sched/wechat_bot.py --login

# 常驻
python3 sched/wechat_bot.py --agent http://localhost:8383 --tasks http://127.0.0.1:8283
```

| 参数 | 作用 | 默认 |
| --- | --- | --- |
| `--agent <url>` | agent 的 HTTP API | `http://127.0.0.1:8282` |
| `--tasks <url>` | 队列的 HTTP API | `http://127.0.0.1:8283` |
| `--base-url <url>` | iLink 服务器 | `https://ilinkai.weixin.qq.com` |
| `--state-dir <path>` | 凭证与 context_token 放哪 | `~/.tea_agent/wechat` |
| `--workspace-path <path>` | 微信任务在哪个工作区做 | `~/.tea_agent/wechat-workspace` |
| `--workspace-name <name>` | 它在 agent 里叫什么 | `微信` |
| `--provider <name>` | 用谁的额度 | 队列的 |
| `--allow-path <dir>` | 微信任务能额外碰哪个目录（可重复） | |
| `--cdn-url <url>` | 微信 CDN（文件走它） | 官方地址 |
| `--login` | 扫码登录然后退出 | |
| `--login-timeout <s>` | 等扫码多久 | `300` |

微信端的环境变量：`TEA_AGENT_URL`（agent 的地址）、`TEA_TASK_URL`（队列的地址，
默认 `http://127.0.0.1:8283`）、`TEA_TASK_PROVIDER`（用谁的额度）、
`TEA_WECHAT_CDN`（微信 CDN 的地址）。

#### 微信里的命令

| 说的话 | 做的事 |
| --- | --- |
| 任何一句话 | 排一个任务（额度有余量就自动开工），回你任务 id |
| `/tasks` | 任务列表（排队 / 进行中 / 已完成） |
| `/status <id>` | 一个任务的状态、进度、最新中间状态 |
| `/result <id>` | 最终回答 + 产出文件 |
| `/files <id>` | 把这个任务的产出文件再发一遍 |
| `/start <id>` | 立刻做 |
| `/cancel <id>` | 停掉 |
| `/del <id>` | 删掉 |
| `/answer <id> <内容>` | 回答一个「等你确认」的任务 |
| `/retry <id>` | 重做一个失败的任务 |
| `/quota` | 额度还剩多少 |
| `/at <时间> <活儿>` | 定时做：`+5` 五小时后、`23:30` 今天晚上十点半、`+30m` 半小时后 |
| `/help` | 命令表 |

中文别名也认：`/任务`、`/状态`、`/结果`、`/文件`、`/开始`、`/取消`、`/删除`、`/回答`、
`/重试`、`/额度`、`/定时`、`/晚点`（`/help` 的中文别名是 `/帮助`）。

**定时**有两条路说，效果一样：

```
/at 23:30 把 README 跑一遍        # 明确一点
把 README 跑一遍 @23:30          # 消息结尾带一个钟点
把 README 跑一遍 @+5             # 或者加一个相对时间
```

到点之前这个活儿不会被启动，但它**不挡着**别的活儿。

**文件能两个方向走**：微信发的文件落到工作区的 `inbox/`，随任务交给 agent；任务做完，
产出的文件作为**文件消息**发回微信。文件走微信 CDN，**AES-128-ECB + PKCS7 加密**，
加解密用 `openssl` 命令行（`openssl enc -aes-128-ecb -K <hex> -nosalt`）。没有 openssl 时
文本通道照常工作，只是文件发不了。

### 4.2 Telegram 端的命令

Telegram 也是队列的前端：给 bot 发一句话就是排一个任务，做完 Telegram 里会收到结果。

```bash
# 1) 看看 token 对不对（BotFather 给的）
python3 sched/telegram_bot.py --token 123456:ABC --check

# 2) 先在 Telegram 里给 bot 发一句话，再问自己的 chat id
python3 sched/telegram_bot.py --token 123456:ABC --print-chat-id

# 3) 常驻（只回答名单上的人）
python3 sched/telegram_bot.py --token 123456:ABC --allow-chat 12345 \
        --agent http://localhost:8383 --tasks http://127.0.0.1:8283
```

| 参数 | 作用 | 默认 |
| --- | --- | --- |
| `--token <t>` | bot token（也可以用 `TEA_TELEGRAM_TOKEN` 或 `~/.tea_agent/telegram/token`） | |
| `--token-file <path>` | token 放哪个文件 | `<state-dir>/token` |
| `--allow-chat <id>` | 哪个 chat 可以用这个 bot（可重复） | 没有人 |
| `--allow-any-chat` | 谁都能用（**bot 是公开的，想清楚再开**） | 关 |
| `--print-chat-id` | 打印等着 bot 的消息来自哪些 chat，然后退出 | |
| `--check` | 检查 token（我是谁），然后退出 | |
| `--api-url <url>` | Bot API 的地址 | `https://api.telegram.org` |
| `--agent <url>` | agent 的 HTTP API | `http://127.0.0.1:8282` |
| `--tasks <url>` | 队列的 HTTP API | `http://127.0.0.1:8283` |
| `--state-dir <path>` | token 与 update offset 放哪 | `~/.tea_agent/telegram` |
| `--workspace-path <path>` | Telegram 任务在哪个工作区做 | `~/.tea_agent/telegram-workspace` |
| `--workspace-name <name>` | 它在 agent 里叫什么 | `Telegram` |
| `--provider <name>` | 用谁的额度 | 队列的 |
| `--allow-path <dir>` | Telegram 任务能额外碰哪个目录（可重复） | |
| `--poll-timeout <s>` | 一次长轮询等多久 | `30` |

Telegram 里的命令与微信里的**完全一样**（见上面的表）。在 Telegram 客户端里敲 `/` 会看到
命令菜单（bot 启动时自己注册）；在群里用 `/tasks@你的bot` 也认。

**默认谁都不能用**：Telegram bot 是公开的——谁找到它都能发消息。所以没有 `--allow-chat`
时它**谁都不理**（日志里写着该怎么办），名单外的人发来的消息直接忽略。确实想谁来都能用，
加 `--allow-any-chat`。

**文件也能两个方向走**：Telegram 发来的文件落到工作区的 `inbox/`
（`~/.tea_agent/telegram-workspace/inbox/`），随任务交给 agent；任务做完，产出的文件作为
**文件消息**发回 Telegram（`/files <id>` 可以再要一次）。走的是 Bot API 的 `getFile` /
`sendDocument`：不用加密，也不用 openssl。

**开机自启**：`scripts/a2_telegram.service` 是跟机器无关的模板，装法与微信那一端一样：

```bash
scripts/install-units.sh [仓库路径]
systemctl --user enable --now a2_telegram
tail -f ~/.tea_agent/logs/a2_telegram.log
```

单元要先放好 token（`~/.tea_agent/telegram/token`），并在 `ExecStart` 后面加上
`--allow-chat <你的 chat id>`。

---

## 5. 给脚本用的客户端

`sched/tea_api.py` 是这两层接口的客户端（纯标准库，设备上跑得起来）。**两个类**：
`Tea` 说 agent 的原语层，`Queue` 说任务队列。

```python
import sys
sys.path.insert(0, "sched")
from tea_api import Tea, Queue

tea = Tea("http://127.0.0.1:8282")       # agent：模型、provider、额度、工作区
queue = Queue("http://127.0.0.1:8283")   # 队列：任务记录、事件

# ---- agent 这一层 ----
tea.health()                       # 探活
tea.providers()                    # provider 清单
tea.usage("minimax")               # 额度（provider 没声明额度端点时抛 400）
tea.workspaces()                   # 工作区列表
tea.llm_chat(prompt="说：ok", max_tokens=10)   # 一次模型调用（不跑 agent loop）

# ---- 队列这一层 ----
task = queue.create_task("把 README 的例子跑一遍", title="例子")
later = queue.create_task("明早再说", not_before="+5")     # 定时做
queue.start_task(task["id"])
queue.task(task["id"])             # 一个任务
queue.tasks(status="queued")       # 列表
queue.cancel_task(task["id"])
queue.confirm_task(task["id"], "可以删")
queue.retry_task(task["id"])
queue.delete_task(task["id"])
queue.task_events(task["id"])      # 这个任务发生过什么
queue.audit()                      # 谁做了什么

# 订阅事件，而不是轮询
for event in queue.events():
    if event["type"] == "task_done":
        notify(event["task"])
```

`Queue` 会**把队列的信封拆开**：`data` 里的东西直接给你，`code` 非零就抛
`ApiError`。带一把项目钥匙就只看得见那个项目：

```python
queue = Queue("http://127.0.0.1:8283")
queue.project_key("ws_xxx")        # 领一把钥匙，之后这个 client 就带着它
```

失败时抛 `ApiError(status, message)`；`status == 0` 表示压根没连上。

> 任务相关的接口住在**队列**（Python 侧，8283），不在 agent 上——所以要用 `Queue`
> 而不是 `Tea`。`Tea` 只有模型、provider、额度、工作区那几样。

---

## 6. 脚本

### 6.1 构建

```bash
# Linux / macOS（脚本）
scripts/build.sh                    # → ./build
scripts/build.sh build-debug        # → ./build-debug

# 等价的手敲
cmake -B build -S . -DCMAKE_BUILD_TYPE=Release
cmake --build build -j8

# Windows (MSVC)
scripts\build.bat                   # → .\build-msvc (NMake)
scripts\build.bat out ninja         # → .\out (Ninja)

# Windows (MinGW)
scripts\build-mingw.bat
```

交叉编译、产物校验、常见错误对照表见 [构建与跨平台编译](build.md)。

### 6.2 跑测试

```bash
scripts/test-all.sh                 # 全套（并行，共用一个浏览器）
scripts/test-all.sh ui              # 只跑浏览器测试
scripts/test-all.sh smoke_todo      # 只跑名字里含 smoke_todo 的
JOBS=4 scripts/test-all.sh          # 少几个并行任务

# 单个测试直接跑
python3 tests/smoke_multi_session.py
node tests/ui_two_clients.mjs
```

### 6.3 装成服务

```bash
# systemd（Linux）：把单元装到 ~/.config/systemd/user/ 并换成这台机器的仓库路径
scripts/install-units.sh                 # 就用本仓库
scripts/install-units.sh /opt/tea_a2     # 仓库在别处

systemctl --user enable --now a2 a2_task a2_wechat
systemctl --user status a2
tail -f ~/.tea_agent/logs/a2_task.log    # 这台机器 journald 是 Storage=none，输出只能看文件
```

三个单元：`a2`（agent，网页 UI）、`a2_task`（任务队列）、`a2_wechat`（队列的微信端）。
单元文件在 `scripts/` 里，是**跟机器无关的模板**（路径用 `%h`）。

```bash
# Windows：注册「登录时启动」（Task Scheduler，InteractiveToken，不需要管理员）
scripts\install-autostart.bat -Start
scripts\install-autostart.bat -Status
scripts\install-autostart.bat -Uninstall
# 多余的参数原样传给 tea_agent
scripts\install-autostart.bat -Start --port 9000 --workspace C:\work\proj
```

---

## 7. 配置的命令

配置文件是 `provider.yaml`（默认 `$HOME/.tea_agent/provider.yaml`），格式与字段见
[配置](config.md)。这里只放**怎么改**的命令。

```bash
# 看现在用的是哪个文件、加载了没
curl -s $B/api/config/status

# 上传一份新的（先校验再落盘；校验失败不覆盖）
curl -s -X POST $B/api/config -H 'Content-Type: application/json' \
  -d '{"yaml":"version: 1\nproviders:\n  deepseek:\n    api_url: https://api.deepseek.com\n    api_key: sk-...\n    models:\n      deepseek-flash:\n        supports_tools: true\n"}'

# 快捷开始（只填 key）
curl -s -X POST $B/api/config/quickstart -H 'Content-Type: application/json' \
  -d '{"provider":"minimax","api_key":"sk-cp-..."}'

# 小米 MiMo（sk-… 按量付费 / tp-… Token Plan）
curl -s -X POST $B/api/config/quickstart -H 'Content-Type: application/json' \
  -d '{"provider":"mimo","api_key":"tp-..."}'

# 手工编辑
$EDITOR ~/.tea_agent/provider.yaml
```

**默认 provider = 配置文件里第一个 provider 的第一个模型**（顺序即切换顺序）。
`default_model:` 只是「没有 models 列表的 provider」的兜底。

**工作区级覆盖**：`<workspace>/.tea_agent_run/settings.yaml`，改文件访问范围、
HTTP 引擎、轨迹开关等。

---

## 8. 排错

| 症状 | 原因与处理 |
| --- | --- |
| 启动就退出，提示 `configuration error` | `provider.yaml` 存在但 YAML 语法错。**故意的**：配置模式只处理「文件不存在」，坏文件要当场报错，别让它藏起来 |
| `409 config_missing` | 还没配置。先 `POST /api/config/quickstart` 或上传配置 |
| `400 vision_unsupported` | 模型没声明 `supports_vision: true`，图片发不过去。换模型，或改成文字描述 |
| `409 turn_running` | 轮次正在跑，改名 / 删除 / 改任务列表会被拒。等它结束 |
| `HTTP 529: 当前为整点高峰时段，服务器短暂繁忙` | provider 整点高峰拒绝了请求。**程序会自己等**：认出「繁忙」后按分钟退避重发（默认 1/2/4 分钟，最多 3 次），界面上显示「服务端繁忙（HTTP 529），稍后自动重试（第 1/3 次，还剩 N 秒）」。想自己控制就调 `settings.overload_retries` / `overload_backoff_ms`（`0` = 不等） |
| `HTTP 400: invalid params, 400 (2013)`（MiniMax） | provider 对不上历史里的工具调用：同一个调用被答复了两次、结果没有对应的调用、调用没有答复。**发出去之前会自动修好**（日志里有一行 `repaired the tool-call structure`）；还是被拒就看日志里 `the request was rejected - N messages, ...` 那行——请求有多大、provider 原话是什么都在里面 |
| `409 turn_still_running` | 顶掉旧轮次时它 15 秒内没停下来（通常卡在一个停不掉的工具里）。先 `POST /api/chat/stop`，等它停了再顶 |
| `409 no_active_turn` | 这个会话没有在跑的轮次。直接 `POST /api/chat` 就行，不用 `/api/chat/queue` |
| `400 images_not_queued` | 排队路径不收图片（图片必须在请求时解码落盘）。等这一轮结束，或用 `interrupt` 顶掉 |
| `429 too_many_subscribers` | 跟着看的浏览器超过 `TEA_AGENT_MAX_SUBSCRIBERS`（默认 8） |
| `429 too_many_active_turns` | 同时在跑的轮次超过 `TEA_AGENT_MAX_ACTIVE_TURNS`（默认 24） |
| `path escapes the workspace` | 文件工具的围栏挡住了这个路径。要么用工作区里的路径，要么给这个任务加 `allow_paths` |
| `/api/chat/stop` 返回 `cancelled:false` | **没带 `session_id`**。带上再试 |
| `409 no_quota` | 队列的额度闸门挡住了。换个 provider，或 `--min-remaining 0` / `--allow-without-quota` |
| `40301` / `forbidden` | 带了别的项目的钥匙。钥匙只管自己那个项目 |
| 任务一直 `queued` 不动 | **同一个项目一次只跑一个任务**（这是设计）。日志里写的是 `its project is busy` |
| 队列的接口返回 `{code, message, request_id, data}` | 队列的统一信封。**客户端要取 `data`** |
| 终端刷屏 | 那是 INFO 回声。`--console-level warn`（默认）或 `--no-console`；要看细节用 `--console-level debug` |
| 看日志 | `~/.tea_agent/logs/tea_agent.log`（全局）、`<workspace>/.tea_agent_run/logs/agent.log`（工作区）。`--log-level debug` 更全 |
| `400 bad_request`（排任务时） | `not_before` 的时间没看懂。写法只有 `+5` / `+30m` / `+2d` / `23:30` / 绝对 ISO 时间 |
| `409 fixed_by_cli`（改工作区设置时） | 这个进程是用 `--allow-outside-workspace` 起的，界面里**收不回来**。去掉那个参数重启 |

---

← 回到 [README](../README.md) · 下一站：[HTTP 接口](api.md) · [任务队列](tasks.md)
