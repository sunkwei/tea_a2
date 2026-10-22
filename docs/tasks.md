# 任务队列（Python 侧）

## 任务队列：agent 是通道，队列在 Python 侧

不是所有事都要马上做。「排队」这件事本身也可以排：把活儿写进队列，等**供应商还有额度**
的时候再让 agent 去做。

分工是这样的：

```
tea_agent（C++，基础能力）              task server（Python，可选伴生件）
  /api/chat (SSE)      ←── 调用 ──      任务队列：排队 / 执行 / 状态 / 结果
  /api/providers/{n}/usage ←── 调用 ──  额度闸门：查 minimax 还有多少
  /api/workspaces     ←── 调用 ──      记录存放在它自己这里
  /api/chat/stop      ←── 调用 ──      事件流：task_created / task_done / …
```

**agent 侧完全不知道队列的存在**：它只有「这一轮在做某个任务」的提示，以及一个
「报进度」的工具。队列的一切（记录、状态机、什么时候开工）都在 Python 侧。
所以：

- agent 单独就能跑，一个 Python 进程都没有也不影响任何功能；
- 队列侧的一切策略（什么时候开工、做什么、做完通知谁）都写在 Python 里，不用改 C++。

### 任务服务：`sched/task_server.py`

> **Python 3.6+**：设备上的 Python 通常很老，队列与微信那一端因此只用标准库、并且不碰
> 3.7 之后才有的东西（`http.server.ThreadingHTTPServer` 在 3.6 上是
> `socketserver.ThreadingMixIn` + `HTTPServer`，`subprocess.run(capture_output=…)` 在 3.6 上是
> `stdout=PIPE` + `universal_newlines=True`）。`tests/smoke_py36.py` 盯着这件事，
> `scripts/py36_compat.py` 负责改写。

一个只用标准库的 HTTP 服务（设备上没有 pip 也能跑），默认听 `127.0.0.1:8283`：

```bash
# 起服务（agent 默认在 8282）
python3 sched/task_server.py --agent http://127.0.0.1:8282 --port 8283

**开机自启（systemd user unit）**：`scripts/a2_task.service` 是一份现成的模板，装到
`~/.config/systemd/user/a2_task.service` 之后：

```bash
cp scripts/a2_task.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now a2_task
systemctl --user status a2_task
tail -f ~/.tea_agent/logs/a2_task.log     # 这台机器 journald 是 Storage=none，user 单元的输出只能看文件
```

agent 还没起来时它不会退出，只是每轮重试（服务化以后「先起谁」不重要了）。
服务重启时，**上一次正在跑的任务会被标成 failed**（线程跟着进程死了，留在 running
就是一条僵尸记录，调度器再也不会碰它）——记录里写明了原因，重新排一次就行。
浏览器里把「设置 → 任务服务（可选）」填成 `http://127.0.0.1:8283`，后台任务跑完时标签页就会闪。

# 排一个任务（不用起服务也能排：直接写进记录）
# 它要先问 agent「有哪些工作区」，所以 agent 不在默认的 8282 上时要指路：
python3 sched/task_server.py --agent http://127.0.0.1:8383 --submit "把 README 里的例子都跑一遍"
#   （等价写法：TEA_AGENT_URL=http://127.0.0.1:8383 python3 sched/task_server.py --submit "…"）

# 看看队列里有什么
curl http://127.0.0.1:8283/api/tasks
```

它提供的**任务管理接口**：

| 方法 | 路径 | 干什么 |
| --- | --- | --- |
| `GET` | `/api/health` | 服务自己、以及它连着的 agent 的状态、队列计数 |
| `GET` | `/api/tasks?workspace_id=&status=` | 任务列表 + 每种状态的计数 |
| `POST` | `/api/tasks` | 排队：`{workspace_id?, prompt, title?, provider?, model?, allow_paths?, meta?}` |
| `GET` | `/api/tasks/{id}?workspace_id=` | 单个任务：状态、进度、中间状态、结果 |
| `DELETE` | `/api/tasks/{id}?workspace_id=` | 删掉一个任务（正在跑的先停掉） |
| `POST` | `/api/tasks/{id}/start?workspace_id=` | 现在就做这件事（同样过额度闸门） |
| `POST` | `/api/tasks/{id}/cancel?workspace_id=` | 停掉正在跑的那个（`POST /api/chat/stop`） |
| `POST` | `/api/tasks/{id}/confirm?workspace_id=` | 回答一个「等你确认」的任务：`{message}`，答案会成为同一会话的下一轮 |
| `GET` | `/api/events` | 生命周期事件流（SSE）：`task_created` / `task_updated` / `task_done` / `task_failed` / `task_waiting_confirmation` / `task_cancelled` |

### 使用手册：排队与撤销

**排一条新任务**（三种办法，任选）：

```bash
# 1) 网页：打开 http://127.0.0.1:8283/ ，顶部写一句要做的事，点「排队」
#    （Ctrl/Cmd+Enter 也可以）

# 2) HTTP API：workspace_id 可以不写（默认第一个工作区）
curl -X POST http://127.0.0.1:8283/api/tasks \
     -H 'Content-Type: application/json' \
     -d '{"prompt":"把 README 的例子跑一遍","title":"例子"}'
#    → {"id":"t_xxxxxxxx","status":"queued","workspace_id":"ws_...","created_at":"..."}

# 3) 命令行：不用服务在跑，直接写进队列
python3 sched/task_server.py --submit "把 README 的例子跑一遍" --title "例子"
```

可以带的字段：`prompt`（必填）、`title`、`workspace_id`（默认第一个）、`provider`（默认
`minimax`，即用谁的额度当闸门）、`model`、`meta`（自己用的自由字段）。写错 `workspace_id`
会被当场挡下（400，并列出 agent 真正认识的工作区），不会排到一半才失败。

排完它就**等着**：调度器每轮看一眼队列，minimax 额度够（默认至少剩 5%）就开工，不够就一直等。
想立刻做：`POST /api/tasks/{id}/start`（网页上就是卡片里的「开始」按钮）。

**删掉一条还没执行的任务**：

```bash
# 命令行
curl -X DELETE http://127.0.0.1:8283/api/tasks/t_xxxxxxxx
# → {"ok":true,"id":"t_xxxxxxxx"}

# 网页：卡片右下角的「删除」（会先确认）
```

说明：

- `workspace_id` 同样可以不给 —— 任务 id 是唯一的，接口自己在所有工作区里找。
- 删的是**记录文件**（`~/.tea_agent/tasks/<workspace_id>/<id>.json`），删完再查是 404。
- 正在跑的任务也删得掉：先 `POST /api/chat/stop` 把那一轮停掉，再删记录（网页上的「删除」、
  命令行上的 `DELETE` 都是这个行为）。
- 「等你确认」的任务删掉之前记得先回答或者取消它——不过删掉会连带把等待丢掉，所以想留着就
  用 `POST /api/tasks/{id}/cancel`。

**改一条任务**（改不了 prompt，只能改状态）：`PATCH` 那条路给的是 agent 侧的任务记录，
队列这边没有改 prompt 的接口——要改就删了重排。

### 微信那一端：`sched/wechat_bot.py`

微信也能当队列的前端：发一句话就是排一个任务，做完微信里会收到结果。

```bash
# 1) 扫码登录（把打印出来的链接在手机上用微信扫）
python3 sched/wechat_bot.py --login

# 2) 常驻
python3 sched/wechat_bot.py --agent http://localhost:8383 --tasks http://127.0.0.1:8283
```

**开机自启（systemd user unit）**：`scripts/a2_wechat.service` 是一份**跟机器无关的模板**
（路径写的是 `%h`，即运行单元的用户的主目录；仓库默认在 `~/work/git/tea_a2`）。装：

```bash
scripts/install-units.sh [仓库路径]       # 不给参数就是本仓库；给了就装那个路径
systemctl --user enable --now a2_wechat
systemctl --user status a2_wechat
tail -f ~/.tea_agent/logs/a2_wechat.log     # 这台机器 journald 是 Storage=none
```

`install-units.sh` 把 `a2` / `a2_task` / `a2_wechat` 三个单元装到 `~/.config/systemd/user/`，
把模板里的仓库路径换成本机的，然后 `daemon-reload`。**目标机器的目录不一样不用手改**：
给对路径就行；实在要手改，模板里要改的只有 `ExecStart` / `WorkingDirectory` /
`StandardOutput` 三处的仓库路径。

服务会**等着凭证出现**：先 `systemctl --user start a2_wechat`，什么时候扫完码，它什么时候接上
（凭证写在 `~/.tea_agent/wechat/credentials.json`，服务每 5 秒看一眼）。

协议是**微信 iLink Bot API**（<https://www.wechatbot.dev/zh/protocol>）：HTTP + 长轮询，
所以只用标准库就够（设备上没有 pip 也能跑）。凭证和每个用户的 `context_token` 存在
`~/.tea_agent/wechat/credentials.json`（context_token 是回复的路由凭据，重启不能丢）。

微信里的命令：

| 说的话 | 做的事 |
| --- | --- |
| 任何一句话 | 排一个任务（额度有余量就自动开工），回你任务 id |
| `/tasks` | 任务列表（排队 / 进行中 / 已完成） |
| `/status <id>` | 一个任务的状态、进度、最新中间状态 |
| `/result <id>` | 最终回答 + 产出文件 |
| `/start <id>` / `/cancel <id>` / `/del <id>` | 立刻做 / 停掉 / 删掉 |
| `/answer <id> <内容>` | 回答一个「等你确认」的任务 |
| `/files <id>` | 把这个任务的产出文件再发一遍 |
| `/quota` | minimax 额度还剩多少 |
| `/help` | 命令表 |

**文件能两个方向走。** 微信发的文件会落到工作区的 `inbox/` 里（`~/.tea_agent/wechat-workspace/inbox/`），
随任务交给 agent；任务做完，产出的文件会作为**文件消息**发回微信（`/files <id>` 可以再要一次）。

文件走的是 iLink 的媒体那一段：媒体放在微信 CDN（`novac2c.cdn.weixin.qq.com/c2c`）上，
**AES-128-ECB + PKCS7 加密**（密钥 16 字节随机生成）。加解密用 `openssl` 命令行
（`openssl enc -aes-128-ecb -K <hex> -nosalt`）——设备上没有 pip，标准库也没有 AES，
但 openssl 每台 Linux 都有。没有 openssl 时文本通道照常工作，只是文件发不了。

协议里密钥有三种写法（32 位十六进制 / base64(16 字节) / base64(十六进制串)），三种都能解。

**微信这一端的工作区是固定的**（默认 `~/.tea_agent/wechat-workspace`，`--workspace-path`
可改）：微信排的任务都在这个目录里做，不会去动别的项目。任务做完了，bot 通过任务服务的事件
流知道，然后把结果发回微信。

### 网页：<http://127.0.0.1:8283/>

任务服务自带一个页面（`sched/task_ui.html`，一个静态文件，纯标准库伺服），分三段：

- **排队中**：什么时候排的、任务内容、provider、可以「开始」或「删除」；
- **进行中**：进度条与百分比、agent 报上来的最新中间状态（展开看历史）、可以「取消」；
  「等你确认」的任务在这里，答一句就继续；
- **已完成**：**最终回答**、**生成物**（agent 产出的文件，点开直接下载）、中间状态、
  **任务目录**（记录文件在哪、工作区在哪）和「在 agent 里打开这个会话」的链接。

页面靠 `/api/events`（SSE）实时刷新，5 秒轮询一次兜底；顶栏还显示 agent 的连通状态和
**minimax 额度还剩多少**。

### 队列自己的工作区

后台任务不该往别人的代码仓库里写东西。所以队列有**自己独占的一个工作区**：

```
~/.tea_agent/task-workspace/        ← 默认（--workspace-path 可改）
├── tasks/                         ← 队列的账本：一个任务一个文件
├── .tea_agent_run/                ← agent 在这个工作区里的运行时目录
└── …                              ← 任务产出的文件
```

服务启动时把它建好，并在 agent 的索引里注册它（所以它也会出现在 agent 的工作区列表里，
可以在 agent 的 UI 里打开看任务产出了什么）。**没有指定工作区的任务都在这里做**；要派到别处，
排队时带上 `workspace_id` 即可。

**队列自己的数据也都放在这个目录里**：`tasks/<id>.json` 是任务记录（一个任务一个文件，扁平
放置 —— 任务 id 唯一，任务在哪个工作区做是记录里的一个字段，不是目录）。于是「关于这个队列的
东西」全在一个目录下：账本、产物、agent 的运行时目录。

### 任务能碰哪些目录

agent 的文件工具默认被关在**工作区**里（`toolkit_file` 读工作区外的路径会得到
`path escapes the workspace`）。后台任务常常要碰别处的目录，所以每条任务可以**自己声明**
能额外访问哪些目录：

```bash
# 排队时带上
curl -X POST http://127.0.0.1:8283/api/tasks -H 'Content-Type: application/json' \
     -d '{"prompt":"整理 /data/raw 的日志","allow_paths":["/data/raw"]}'

# 命令行
python3 sched/task_server.py --submit "整理 /data/raw 的日志" --allow-path /data/raw
```

网页上排队时，第三个输入框就是它（逗号分隔）。

这个授权是**逐轮**的：agent 收到 `allow_paths` 后，只在这一轮里把这些目录算作工作区内，
下一轮就恢复围栏；system prompt 里也会写明这一轮能碰哪些目录（模型不知道就会乱试或者
干脆拒绝做）。

范围仍然是收紧的：授权 `/data/raw` 不等于授权 `/data`（越出授权目录照样被挡）。

> 命令工具（`toolkit_exec`）本来就不受这个围栏限制：它在进程当前目录里跑，shell 里
> `cd`/`cat` 随便走。围栏管的是**文件工具**（`toolkit_file`、`toolkit_load_image`）和
> `toolkit_exec` 显式传的 `cwd`。

### 任务是什么

一个任务 = 一句要做的事 + 它的状态。记录是**一个任务一个文件**
（`<data-dir>/<workspace_id>/<id>.json`，默认 `~/.tea_agent/tasks/`），所以两个写入者
不会撞在同一个文件上，重启也不会丢。

| 字段 | 含义 |
| --- | --- |
| `status` | `queued` → `running` → `done` / `failed` / `cancelled`，或 `waiting_confirmation`（在等用户） |
| `progress` | 0..100，由 agent 自己报 |
| `note` | 最近一条中间状态（一行） |
| `steps[]` | 中间状态的历史：什么时候、做了什么、进度多少 |
| `needs_confirmation` + `question` | **需要用户确认**：agent 停下来等一个答案，任务不会假装做完 |
| `session_id` | 跑这个任务的会话（确认之后接着在同一个会话里继续） |
| `result` | 最后一轮的回答 |
| `meta` | 给脚本自己用的自由字段 |

### agent 怎么报进度：`toolkit_task`

```
toolkit_task op=update progress=50 note="已经把构建产物删了"
toolkit_task op=update needs_confirmation=true question="可以删掉旧构建产物吗？"
```

模型怎么知道这一轮属于某个任务？**system prompt 里会写清楚**（任务 id、标题、要做的事），
并告诉它用 `toolkit_task` 汇报进度与中间状态、遇到需要人确认的就停下来问。没有这一段，
模型只会把任务当成普通提问：进度永远是 0，`needs_confirmation` 也永远用不上。

**agent 这一侧不存任务记录**：`toolkit_task` 只是「报数」——把 progress/note/question
说出来。任务服务在读这一轮的事件流（`tool_call` 里带着这些参数），据此更新自己的记录。
所以两边各有各的职责，也就没有两份真相。

### 额度闸门

安排执行任务时，任务服务先问 agent：「minimax 这个 provider 还剩多少额度？」
（`GET /api/providers/minimax/usage`，即「5 小时窗口 + 每周窗口」）。**只有余量够的时候
才开工**：

```
2026-10-22 07:38:47  agent up: http://127.0.0.1:8282 (provider minimax, model MiniMax-M3.1-Flash)
2026-10-22 07:38:47  quota of 'minimax': 95.0% left - going to work
2026-10-22 07:38:47    started task t_66fc879b3089 (调度器冒烟)
2026-10-22 07:38:53    task t_66fc879b3089: running 100% - 已在工作区根目录创建 notes.txt…
2026-10-22 07:38:56    task t_66fc879b3089: done 100% - 已在工作区根目录创建 notes.txt…
```

几个刻意的选择：

- **额度读不到就不开工**（provider 没声明额度端点、或者查询失败）。宁可不做，也不要在
  额度已经用光时继续烧；确实要无条件做，加 `--allow-without-quota`。
- **工作一定跑在查额度的那个 provider 上**：启动任务时会把 provider 带过去，否则
  「查的是 A 的额度、跑的却是 B」就成了假账。
- **别的 provider 的任务不碰**：任务自己指定了 provider 就由它自己排队。
- **一次做一件**（`--max-per-round`）：额度是窗口制的，一口气全做完没有意义。
- **在等人确认时停下**：那一轮跑完就停，记录里写清楚要人回答什么；回答之后在**同一个会话**
  里接着做。

---

← 回到 [README](../README.md)
