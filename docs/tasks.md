# 任务队列（Python 侧）

> 这一页就是队列自己的文档：`GET /api/doc`（网页）、`?format=md`（Markdown）、
> `?format=json`（版本 + 全文 + 渲染好的页面）——**服务的就是这个文件**，没有第二份拷贝。
> 队列还把 `docs/` 整个目录服务出来（`GET /api/doc/tea_a2_manual.md` 这样），页面里的
> 相对链接会指向它，点得通。

> 这一页讲**队列的策略**。要直接敲命令，看
> [使用手册](tea_a2_manual.md)。

## 任务队列：agent 是通道，队列在 Python 侧

不是所有事都要马上做。「排队」这件事本身也可以排：把活儿写进队列，等**供应商还有额度**
的时候再让 agent 去做。

分工是这样的：

```
tea_agent（C++，基础能力）              task server（Python，可选伴生件）
  /api/chat (async + 句柄) ←── 调用 ──  任务队列：排队 / 执行 / 状态 / 结果
  /api/turns/{id}/events ←── 订阅 ──    额度闸门：查 minimax 还有多少
  /api/providers/{n}/usage ←── 调用 ──  记录存放在它自己这里
  /api/workspaces     ←── 调用 ──      事件流：task_created / task_done / …
  /api/turns/{id}/stop ←── 调用 ──
```

**队列怎么跑一轮**（0.19.0 起）：先把这一轮**交出去**（`POST /api/chat {"async":true}`，
拿到 `turn_id`），再**订**它的帧（`GET /api/turns/{id}/events?since=`）。连接断了不要紧：
问一句 `GET /api/turns/{id}` 就知道这一轮怎么样了，再从最后一个 `seq` 接回去。
**队列重启了也不要紧**：任务记录里有 `turn_id`，新起的队列会**接回**那一轮
（见下面「服务重启时」）。

**额度就是这一轮的预算**（0.20.0 起）：额度以前只是「开工前看一眼」（剩得少于
`--min-remaining` 就不开工）。现在队列多了两个参数：

```bash
--quota-tokens 1000000   # 100% 的额度值多少 token（0 = 不换算）
--quota-share 0.5        # 一个任务最多能用剩下的多少（默认一半）
```

配上之后，剩下的额度会换算成**这一轮能花多少**（`max_total_tokens`）随任务下发：额度还剩
40%、一个任务最多用一半 → 这一轮最多 200,000 token。**请求只能收紧**，机器的天花板仍然
说了算。不配 `--quota-tokens` 就还是老样子（额度只当闸门）——百分比本身换不成 token，
「100% 值多少」得操作者自己说。

**命令也有围栏了**（0.20.0 起）：文件工具一直有围栏，命令工具没有——一轮被限制了目录，
照样能跑一条碰别处的命令。任务的 `policy` 现在可以带命令围栏：

```json
{"policy": {"tools": ["toolkit_exec"], "exec": {"allow": ["npm *"], "deny": ["rm -rf *"]}}}
```

`deny` 先看（命中的命令**根本不跑**），`allow` 非空时清单外都不跑，模式是前缀
（`"npm *"` 匹配 `npm install`，不匹配 `sudo npm install`）。不给 `policy.exec` 就什么都不围。

**事件都到一条总线上**（0.20.0 起）：机制层的事件也上队列自己的 `/api/events`
（`turn_done` / `turn_error` / `turn_stopped` / `budget_exhausted` / `tool_error`，带
`turn_id` 和任务记录）。于是看门狗、router、网页**只订一处**，而且顺序是定的：
**先**是 agent 的视角，**后**是队列的判决。

**版本段**（0.21.0 起）：队列的接口现在有两套写法——需求书里的 `/v1/agent-tasks...`
和一直以来的 `/api/tasks...`。**加的是版本段，不是搬家**：旧的照样能用，客户端、队列
网页、看门狗都改成了新路径。

需求里的路径都在：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/agent-tasks` | 提交 |
| GET | `/v1/agent-tasks` | 列表 |
| GET | `/v1/agent-tasks/{id}` | 单个任务 |
| GET | `/v1/agent-tasks/{id}/events` | 事件日志（`since_event_id` 增量） |
| GET | `/v1/agent-tasks/{id}/result` | **结果摘要** + 生成物清单（每个生成物有自己的 id） |
| GET | `/v1/agent-tasks/{id}/artifacts/{aid}` | 按 id 下载一个生成物（钥匙管着） |
| GET | `/v1/agent-tasks/{id}/export` | 导出（同步） |
| POST | `/v1/agent-tasks/{id}/export` | **起一个导出任务**（202 + `export_id`） |
| GET | `/v1/exports/{id}` | 导出任务的进度 |
| GET | `/v1/exports/{id}/download` | 导出任务的字节（带有效期） |
| POST | `/v1/agent-tasks/{id}/interactions` | **回答**那个问题（要带 `interaction_id`） |
| POST | `/v1/agent-tasks/{id}/cancel` \| `/retry` \| `/start` | 取消 / 重试 / 启动 |
| GET | `/v1/queues/{id}/status` | 这个队列的状态（暂停、计数） |
| GET | `/v1/projects` | 项目（钥匙） |

**检查点与接着做**（0.21.0 起）：任务记录里存着**检查点**（这一轮的句柄、会话、轨迹）。
队列重启后：轮次还在跑 → **接回**它；轮次没了 → **默认判失败**并写明原因。想**接着做**
（同一会话里的新的一轮）得点头：`--resume-on-restart`。

为什么默认不接着做：一轮被切断的活儿**可能已经做了一半**（写了一半文件、改了别的东西），
没人叫你别重跑，那就别重跑。想要这个行为的人自己开。

**问题有自己的编号与期限**（0.21.0 起）：任务等人回答时，问题的 `interaction_id` 与
`expires_at` 记在任务的 `interaction` 里；回答要带上那个 id（对不上 → 409）；
`--interaction-timeout`（秒）到了没人回答 → 任务判失败，写明「没人回答」。

**谁在问，是谁的活儿**（0.21.0 起）：领钥匙时可以写租户与身份
（`POST /v1/projects {"workspace_id":"ws_x","tenant_id":"acme","identity":"张三"}`），
任务记录里写着租户与是谁提交的；`X-Tenant-Id` 与钥匙的租户对不上 → **403**。

**限流与保留**（0.21.0 起）：`--rate-limit-per-minute N`（按钥匙，超了 429 +
`Retry-After`）、`--retention-days N`（旧记录自动清掉）、`--export-ttl-hours N`
（导出的包留多久）、`--cleanup-interval`（多久清一次）。

**提交用幂等键**（0.19.3 起）：队列提交那一轮时带 `Idempotency-Key: task:<任务号>:<第几轮>`
——所以**提交本身超时了可以重发**：agent 那边记住的是同一轮，拿到的是同一个 `turn_id`，
活儿不会被做两遍。任务号后面的「第几轮」是**这一轮**的序号（不是重试次数）：一个任务等
人回答之后继续做，那是一轮**新的**活儿，键也就不同。

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

**服务重启时**：上一次正在跑的任务**不会被判死**——0.19.0 起，队列把每一轮交给 agent
时就拿到了**句柄**（`turn_id`，写在任务记录里），agent 那边那一轮**还在跑**。新起的队列
看到这条记录，会去问 agent「那一轮怎么样了」：

- 还在跑 → **接回**它（订它的帧，等它跑完，答案照样落在记录上）；
- 已经跑完 → 直接把 agent 那边记着的结论（答案、用量、结束原因）写回任务；
- agent 也不认得了（agent 自己重启过）→ 这时候才标成 failed，记录里写明原因，重新排一次。

没有句柄的老记录（0.19.0 之前排的）照旧判失败。
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
| `POST` | `/api/tasks` | 排队：`{workspace_id?, prompt, title?, provider?, model?, model_params?, allow_paths?, meta?}` |
| `GET` | `/api/tasks/{id}?workspace_id=` | 单个任务：状态、进度、中间状态、结果（生成物带下载链接） |
| `GET` | `/api/tasks/{id}/download?path=&workspace_id=` | 下载这个任务产出的一个文件（队列自己伺服，agent 在别的机器上也行） |
| `GET` | `/api/workspaces` | 任务可以放在哪些工作区里（agent 的 + 队列自己的）——网页的下拉框就是它画的 |
| `GET` | `/api/providers` | agent 认识的 provider 和它们的模型——网页的下拉框也是它画的 |
| `DELETE` | `/api/tasks/{id}?workspace_id=` | 删掉一个任务（正在跑的先停掉） |
| `POST` | `/api/tasks/{id}/start?workspace_id=` | 现在就做这件事（同样过额度闸门） |
| `POST` | `/api/tasks/{id}/cancel?workspace_id=` | 停掉正在跑的那个（`POST /api/chat/stop`） |
| `POST` | `/api/tasks/{id}/confirm?workspace_id=` | 回答一个「等你确认」的任务：`{message}`，答案会成为同一会话的下一轮 |
| `GET` | `/api/events` | 生命周期事件流（SSE）：`task_created` / `task_updated` / `task_done` / `task_failed` / `task_waiting_confirmation` / `task_cancelled`，以及 `trigger_received` / `trigger_decided`（触发器入口）、`watchdog_alert`（看门狗）、`speech`（router 的一句话）、`queue_paused` / `queue_resumed`、`thread_summarized` |

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

还可以带：

| 字段 | 是什么 |
| --- | --- |
| `workspace` | 工作区的**路径**（也可以写 id）——人写路径，队列换成 agent 认识的那个 id |
| `not_before`（或 `start_at`） | 什么时候开始做：`"+5"`、`"23:30"`、`"+30m"`、`"+2d"`、`"2026-10-24T09:00:00Z"`，或者**本机时间** `"2026-10-24T09:30"`（网页的日期框给的就是这个） |
| `model_params` | 这一轮用哪些模型参数：`{"temperature": 0.3, "max_tokens": 8000, "reasoning_effort": "off"}` |

**模型参数**是 agent 自己 `/api/chat` 也认的那几个旋钮（`temperature`、`top_p`、`top_k`、
`frequency_penalty`、`presence_penalty`、`seed`、`max_tokens`、`reasoning_effort`）。
它们只对**这一轮**生效（下一个任务还是模型自己的默认值）；写错名字或者超范围会被
当场挡下（400，并说清是哪个旋钮），不会等到开工才发现。也可以直接写在请求的顶层
（`{"prompt":"…","temperature":0.3}`），和 agent 的接口一个写法。

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

`install-units.sh` 把 `a2` / `a2_task` / `a2_wechat` / `a2_telegram` 四个单元装到
`~/.config/systemd/user/`，
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

### Telegram 那一端：`sched/telegram_bot.py`

Telegram 也能当队列的前端：给 bot 发一句话就是排一个任务，做完 Telegram 里会收到结果。
命令与微信那一端**一模一样**——两边共用 `sched/channel.py`，只有协议不同。

```bash
# 1) 看看 token 对不对（BotFather 给的）
python3 sched/telegram_bot.py --token 123456:ABC --check

# 2) 先给 bot 发一句话，再问自己的 chat id
python3 sched/telegram_bot.py --token 123456:ABC --print-chat-id

# 3) 常驻（只回答名单上的人）
python3 sched/telegram_bot.py --token 123456:ABC --allow-chat 12345 \
        --agent http://localhost:8383 --tasks http://127.0.0.1:8283
```

**Telegram bot 是公开的**：谁找到它都能发消息，所以默认**谁都不理**——不给 `--allow-chat`
的话它不回答任何人（日志里写着该怎么办）。名单上的人（`--allow-chat`，可重复）之外的消息
一律忽略；确实想谁来都能用，加 `--allow-any-chat`。

协议是 **Telegram Bot API**（<https://core.telegram.org/bots/api>）：HTTPS + 长轮询，
所以只用标准库就够（设备上没有 pip 也能跑）。token 从 `--token`、环境变量
`TEA_TELEGRAM_TOKEN` 或 `~/.tea_agent/telegram/token` 读；update 的 offset 记在
`~/.tea_agent/telegram/state.json`（重启不会把同一条消息再答一遍）。

**开机自启（systemd user unit）**：`scripts/a2_telegram.service` 是一份跟机器无关的模板：

```bash
scripts/install-units.sh [仓库路径]
systemctl --user enable --now a2_telegram
systemctl --user status a2_telegram
tail -f ~/.tea_agent/logs/a2_telegram.log
```

单元里要先放好 token（`~/.tea_agent/telegram/token`），并在 `ExecStart` 后面加上
`--allow-chat <你的 chat id>`。

**文件也能两个方向走**：Telegram 发来的文件落到工作区的 `inbox/`
（`~/.tea_agent/telegram-workspace/inbox/`），随任务交给 agent；任务做完，产出的文件
作为**文件消息**发回 Telegram（`/files <id>` 可以再要一次）。走 Bot API 的 `getFile` /
`sendDocument`：不用加密，也不用 openssl。

### 队列自己的 API

`sched/task_server.py` 既是队列也是它的 API。完整的接口表和**需求逐条对照**
（`docs/pending_task.txt` 是需求书，`docs/pending_task.md` 是我们的回答）在
[需求对照表](pending_task.md)。这里只说与日常使用有关的几条：

| 做的事 | 怎么用 |
| --- | --- |
| 排队 | `POST /api/tasks {"prompt": …, "workspace_id": …, "priority": 0}`；同一个 `Idempotency-Key` 重复提交只排一次 |
| 定时做 | 排队时带 `not_before`：`"+5"`（五小时后）、`"23:30"`（今天 23:30；已经过了就尽快做）、`"+30m"`、`"+2d"`、或绝对的 `"2026-10-24T09:00:00Z"`。到点之前这个任务不会被启动，**但它不挡着后面的任务** |
| 看队列 | `GET /api/tasks?status=queued&page=1&page_size=20`（还能按 `created_from/to`、`tag`、`business_id` 过滤） |
| 看一个任务 | `GET /api/tasks/{id}`（`include=full` 带上事件与流水） |
| 看发生了什么 | `GET /api/tasks/{id}/events?since_event_id=N`（事件落盘，重启后还在） |
| 回答「问你呢」 | `POST /api/tasks/{id}/confirm {"message": "…"}`——**任务回到队列队尾**，不打断正在跑的活儿，答案随任务带回去 |
| 重做一个失败的 | `POST /api/tasks/{id}/retry`（同一个任务，`attempt + 1`，历史留着） |
| 导出结果 | `GET /api/tasks/{id}/export?format=zip`（`manifest.json` / `final_answer.md` / `conversation.jsonl` / `events.jsonl` / `artifacts/`） |
| 看指标 | `GET /api/health` 的 `metrics`（队列长度、平均排队时长、失败率……） |
| 看审计 | `GET /api/audit?task_id=…` |
| 拿任务单模板 | `GET /api/task-order-template`（网页上就是那个「下载任务单模板」） |
| 让活儿跑在设备上 | 排队时带 `target: "fleet"`（和 `fleet: {names|groups|labels, timeout_s}`）——设备自己的 a2 回答，队列把设备的回答收成结果。队列要知道机群：`--fleet-url` / `--fleet-token` |
| 让队列停下 / 继续 | `POST /api/queue/pause {"reason": "…"}` / `POST /api/queue/resume`；`GET /api/queue/state` 看现在是不是停着。**暂停写在文件里**，服务重启也还是停着的——队列不该自己又开始干活 |
| 把一个卡死的任务判死 | `POST /api/tasks/{id}/fail {"reason": "…"}`（看门狗用；必须带原因） |
| 往事件流里塞一条事件 | `POST /api/events {"type": "watchdog_alert", "task_id": "…", …}`（只有本机操作员能塞；看门狗的告警就走这条路） |
| 投一条外部事件 | `POST /api/triggers {"source": "webhook", "text": "…", "dedup_key": "…"}`——**触发器入口**：同一个 key 是同一件事（不重复排），规则先判（`~/.tea_agent/triggers.conf`），规则没定音才问模型一次；答 `accept` 才变成任务。`GET /api/triggers` 看都来了什么，`GET /api/threads` 看线索 |
| 让 router 替队列说话 | 队列事件流里的 `speech` 事件（router 写的，一句话一个事件）：聊天前端加 `--speech` 就只递 router 的话，不再自己翻译原始事件 |
| 看一条线索知道什么 | `GET /api/threads/{id}/context`——窗口（最近 8 步）、掉出窗口的旧进展、以及下一个同线索任务会拿到的 prompt。`POST /api/threads` 手工开一条线索 |
| 写回线索的摘要 | `POST /api/threads/{id}/spill {"summary": "…", "count": N}`（router 用；只有本机操作员能写——摘要会进 prompt） |
| 交一个**计划**（复杂活儿拆成几步） | `POST /api/plans {"title": "…", "goal": "…", "steps": [{"key": "s1", "title": "…", "prompt": "…", "depends_on": []}]}`——计划变成一个父任务（装最终结果）+ 每步一个任务；`depends_on` 里的步骤没做完，这一步就不启动。`GET /api/plans` / `GET /api/plans/{id}` 看计划，`POST /api/plans/{id}/cancel` 取消 |
| 交一份任务单 | `python3 sched/task_server.py --submit-order <文件>`，或把 `*.order` 文件发给微信 bot |
| 排队任务用哪个 **provider+模型** | provider.yaml 顶层一行 `task_model: <provider>+<模型名>`（例：`task_model: minimax+MiniMax-M3.1-Flash`）。队列问 agent 要这个值，改完一分钟内自己看到，不用重启。额度闸门查的就是这个 provider 的额度 |

### 计划：一个复杂活儿拆成几步

`POST /api/plans` 交的不是一件事，是**一件事的拆法**。它变成：

- 一个**父任务**（`kind: "plan"`）：不干活，只装最终结果。进度是「几步完成了」；
- 每一步一个**子任务**：真的干活，可以有依赖（`depends_on` 写的是同计划里别的步骤的 `key`）。

队列只启动「等够了」的步骤：依赖的步骤没做完，这一步就在队列里等着（不占位置、不挡别人）。
哪一步失败了，依赖它的步骤会被停掉（不会永远等）；全做完了，队列用**一次模型调用**把各步的
结果合成一份最终交付物，写进父任务的 `result`。

几步是**同一条线索**：router 记得住谁做了什么，下一步的 prompt 里带着上一步的结果。

```bash
# 交一个计划
curl -X POST http://127.0.0.1:8283/api/plans -H 'Content-Type: application/json' -d '{
  "title": "写一份上手指南",
  "goal": "让新用户 5 分钟会用 tea_a2",
  "steps": [
    {"key": "read",   "title": "读 README", "prompt": "读 README.md，列出关键事实"},
    {"key": "write",  "title": "写指南",   "prompt": "写成一份上手指南", "depends_on": ["read"]}
  ]}'
# → {"plan": {…}, "parent": {父任务}, "steps": [{key, title, task}, …]}
```

规划本身是**独立的一步**（`sched/planner.py`）：一次模型调用出计划，写成 JSON 文件，
人可以改过再交：

```bash
python3 sched/planner.py --goal "把 30 个文件分类并写一份报告" --out /tmp/plan.json
$EDITOR /tmp/plan.json          # 改一改（步骤、依赖、每步的 prompt）
python3 sched/planner.py --submit /tmp/plan.json
```

**计划是建议，不是命令**：模型拆的步不一定对，所以中间有人——改完再交。

### 任务单：一个文件交一份活儿

不想敲 curl，就填一份**任务单**（文本文件，模板在 `sched/task-order.template`，队列的网页上
也有「下载任务单模板」）。它带的是**一个任务能有的全部设置**：

```
title: 把 README 的构建例子跑一遍
prompt: |
  把 tea_a2 的 README 里的构建例子真的跑一遍，
  遇到问题就记下来，最后给我一份清单。
workspace: /home/sunkw/work/git/tea_a2
allow_paths:
  - /tmp
provider: minimax
model: MiniMax-M3.1-Flash
model_params:
  temperature: 0.3
  max_tokens: 8000
priority: 0
not_before: +5
tags:
  - 文档
business_id: DOC-2026-1023
meta:
  owner: sunwei
```

格式很简单（**不是 YAML**：标准库没有 yaml 解析器，而队列跑在没有 pip 的设备上）：

- 一行一个「键: 值」，`#` 是注释，空行忽略；
- 键下面的缩进行是它的续行（`prompt` 的正文就写在这里）；
- 列表可以缩进多行写（`- 每一项`），也可以用逗号分隔；
- 值后面可以用 ` # 注释` 注释掉；
- **不认识的键会报错**（带着行号）——任务单里的笔误不能悄悄丢掉一个设置。

怎么交：

```bash
python3 sched/task_server.py --submit-order 我的任务单.txt     # 命令行
```

`workspace:` 里写的工作区必须是**这个 agent** 认识的（路径或 id 都行）：队列默认问 8282 上的
agent，这台机器上还有别的 agent（比如 8383 上那个）时加 `--agent http://127.0.0.1:8383`。
对不上就会直说「agent（http://…）里没有这个工作区」并列出它认识的那些，不会把任务排进错的地方。

或者**把文件发给微信 bot**（文件名以 `.order` 结尾，或名字里有「任务单」）：bot 读它、排队，
任务单有问题就把错处念给你听。

队列的记录里会留下 `not_before`（绝对时间）、`not_before_text`（你写的那句）、`starts_at_local`
（界面显示用）。

**每个 JSON 回答都是同一个形状**：`{code, message, request_id, data}`。成功时 `code=0`、
`message="ok"`，正文在 `data` 里；失败时 `code` 是需求附录里的编号（40001 参数、40301 越权、
40401 不存在、40901 状态冲突……），细节在 `data.error`。`request_id` 也放在 `X-Request-Id` 头里——
报障时把这个号说出来就能在日志里找到那一次请求。

**定时（not before）**：一个任务可以带一个「最早什么时候开始」的时间。`+5` 是从现在起五小时后，
`23:30` 是今天晚上十点半——**如果这个钟点已经过了，就当成「尽快做」**（不是明天）。到点之前它躺在队列里，
但**不挡着**后面的任务（那不是「排在第几位」，是「晚点做」）。队列的记录里有 `not_before`（绝对时间）、
`not_before_text`（用户写的那句话）和 `starts_at_local`（本机时间，给界面显示）。

**队列按项目串行**：**同一个工作区（项目）**一个时刻只跑一个任务，**不同项目可以并行**。
这条以前只是「每轮最多起一个」，上一轮没跑完下一轮照起——现在按**记录**数，跑着的任务占着位置。
`--max-concurrent` 可以再给一个全局上限（默认 0 = 只按项目算）。

**权限按项目**：每个项目一把钥匙，`Authorization: Bearer <key>`（或 `X-Project-Key`）带着它就
**只能**碰这个项目的任务、事件、导出和排队，别人的 → 403。钥匙由本机操作者用
`POST /api/projects {"workspace_id": …}` 领；**带钥匙的人领不到别人的钥匙**。
事件流和审计日志也按这个规矩：`GET /api/events`（SSE）带钥匙的连接只收到**本项目**的事件帧
（帧里带着任务内容，所以别人的帧就是别人的秘密）；`GET /api/audit` 同理，带钥匙的调用者
只读得到本项目的审计行。**不带钥匙的调用者是本机操作者**，看得到全部——队列网页就是它。
微信 bot 用的是它自己工作区的那把钥匙，所以它只在自己的项目里活动。
不带钥匙的调用者是本机操作者（队列网页就是它）——服务只监听 127.0.0.1。

**调度很快，但额度不打扰人**：`--interval` 默认 2 秒（以前 60 秒），而 provider 的额度接口
**只在有排队任务时**才查——空闲时一次都不调。

**聊天端的工作区是固定的**（微信默认 `~/.tea_agent/wechat-workspace`，Telegram 默认
`~/.tea_agent/telegram-workspace`，`--workspace-path` 可改）：聊天里排的任务都在这个目录里做，
不会去动别的项目。任务做完了，bot 通过任务服务的事件流知道，然后把结果发回聊天里。

### 网页：<http://127.0.0.1:8283/>

任务服务自带一个页面（`sched/task_ui.html`，一个静态文件，纯标准库伺服），上面一张
**排任务的表单**，下面三段：

**表单**能一次把一个任务能有的设置都写清楚：

| 格子 | 是什么 |
| --- | --- |
| 主题 | 一句话说清要做什么（`title`） |
| 详细内容 | 真正要做的事（`prompt`） |
| 工作区 | 下拉里是 agent 认识的工作区（`GET /api/workspaces`），也可以直接写路径 |
| 什么时候开始 | 尽快 / 30 分钟后 / 2 小时后 / 明天 09:00 / 自己挑一个日期时间（`not_before`） |
| provider | 下拉里是 agent 认识的 provider（`GET /api/providers`） |
| 模型 | 跟着 provider 变的模型下拉（留空 = 那个 provider 的默认模型） |
| 模型参数 | 展开才看见：`temperature`、`top_p`、`max_tokens`、`思考强度`、`seed`（留空 = 什么都不额外发） |
| 额外可访问的目录 | 这个任务额外能碰的目录（逗号分隔） |

填完点「排队」，下面三段：

- **排队中**：什么时候排的、任务内容、provider、模型、模型参数、什么时候开始
  （`⏰`）、可以「开始」或「删除」；
- **进行中**：进度条与百分比、agent 报上来的最新中间状态（展开看历史）、可以「取消」；
  「等你确认」的任务在这里，答一句就继续；
- **已完成**：**最终回答**、**生成物**（agent 产出的文件，点开直接下载——链接是队列自己的
  `/api/tasks/{id}/download`，agent 在别的机器上也点得开）、中间状态、
  **任务目录**（记录文件在哪、工作区在哪）、「下载全部结果（zip）」和「在 agent 里打开这个会话」的链接。

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
| `limits` | **这一轮的预算**（`max_iterations` / `max_wall_ms` / `max_tool_calls` / `max_total_tokens`）。没写就用机器的（`GET /api/health` 的 `limits`） |
| `policy` | **这一轮的权限**：`tools` 是这次允许用的工具清单（空 = 全部） |
| `stop_reason` | 这一轮为什么停：`""`（模型自己收尾）/ `budget`（预算到点）/ `cancelled` / `error` |
| `budget_used` | 预算用了多少、撞到的是哪一条（`limit`） |
| `usage` | 这一轮的 token 用量（provider 自己报的） |
| `trajectory_id` | 这一轮在 agent 那边的轨迹 id（`GET /api/turns/<id>/trajectory`） |
| `meta` | 给脚本自己用的自由字段 |

### 预算与权限：这一轮能花多少、能用什么

agent 是**机制层**：它决定一轮最多能花多少，并且**强制**执行；队列是**策略层**：
它决定每个任务分到多少、能用哪些工具，然后把这两样交给 agent。

排队时就能写：

```bash
curl -X POST http://127.0.0.1:8283/api/tasks -H 'Content-Type: application/json' \
     -d '{"prompt":"整理 /data/raw 的日志",
          "allow_paths":["/data/raw"],
          "limits":{"max_iterations":8,"max_wall_ms":300000},
          "policy":{"tools":["toolkit_exec","toolkit_file"]}}'
```

- **预算**：没写就用机器的（agent 的 `GET /api/health` → `limits`，`max_iterations`
  默认 300）。请求**只能收紧，不能放宽**机器的上限——这是机制层的规矩，队列也改不动。
- **权限**：`policy.tools` 是这次允许用的工具清单。清单外的工具**模型点名也不执行**
  （工具会得到一句「这一轮不准用这个工具」的回答）。`allow_paths` 仍是额外能碰的目录。
- **不认识的键当场报错**（400），不会等排队排到一半才发现。
- **到点不是失败**：预算用完时这一轮**正常结束**（`stop_reason: "budget"`），
  任务记成 `done`，`note` 里写清楚撞到的是哪一条。做过的部分算数。

网页上排队时，「预算与权限」那一栏就是这两样（留空 = 用机器的预算和全部工具）。

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

安排执行任务时，任务服务先问 agent：「**排队任务用的那个** provider 还剩多少额度？」
（`GET /api/providers/<provider>/usage`，即「5 小时窗口 + 每周窗口」）。那个 provider 是
provider.yaml 的 `task_model:` 说的（没写就是 minimax），也是**真正跑活儿的那个**。
**只有余量够的时候才开工**：

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
  「查的是 A 的额度、跑的却是 B」就成了假账。那个 provider 由 `task_model:` 决定；
- **provider 没有额度端点就不开工**（除非 `--allow-without-quota`）：所以 `task_model:`
  指到一个不报额度的 provider 时，得配上这个开关。
- **别的 provider 的任务不碰**：任务自己指定了 provider 就由它自己排队。
- **一次做一件**（`--max-per-round`）：额度是窗口制的，一口气全做完没有意义。
- **在等人确认时停下**：那一轮跑完就停，记录里写清楚要人回答什么；回答之后在**同一个会话**
  里接着做。

---

## 往后：Router 与 Watchdog

这个队列现在只做一件事：**把排进来的活儿按规矩做完**。它没有「谁跟人说话」这一层（Router），
也没有「谁盯着别卡住」这一层（Watchdog）——DeepSeek Harness 团队的 Super-agent 架构把这两层
拆了出来，我们打算照着补上：

- **M1 Watchdog（先做）**：独立进程巡检 running 的任务、agent 的健康、额度和设备；
  默认只告警不动手，`--auto` 才允许 fail/retry。
- **M2 Trigger 入口**（已做）：`POST /api/triggers` 统一收事件，去重 → 规则 → 模型判断
  要不要做；`POST /api/tasks` 继续留着当退路。
- **M5 机群**（已做 v1）：任务带 `target: fleet` 就跑在设备自己的 a2 上，设备的回答收成
  任务的结果（没答上来的设备也写进结果）。
- **M3 Router**（已做 v1/v2）：跨 session 的共享上下文（thread）——下一个同线索的任务会被
  告知「之前发生了什么」；掉出窗口的进展由 router 收拢成摘要。**v2：router 说话**
  （话术进队列事件流的 `speech` 事件），聊天前端加 `--speech` 就只递 router 的话。
- **M6 计划**（已做）：`POST /api/plans` 把一个复杂活儿拆成几步（父任务 + 子任务 + 依赖），
  队列只启动「等够了」的步，全做完了合成最终结果；`sched/planner.py` 负责出计划（一次模型
  调用，写成 JSON 给人改过再交）。

完整的对象模型、接口和验收标准见 [Router 与 Watchdog](router-watchdog.md)。

---

← 回到 [README](../README.md) · 命令清单见 [使用手册](tea_a2_manual.md)
