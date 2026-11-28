# 消息平台（HTTP）—— 设计文档 v1.0

> **状态：设计 v1.0（待实现）。** 输入是 [需求说明书 v1.0](message-platform.md) 的 §12 交接单
> （Q1~Q9 全部已定）。本文回答 §12.2 的九个问题，并给出「改哪些文件 / 接口怎么变 /
> 旧行为怎么兼容 / 怎么回滚 / AC 落在哪」。
>
> **本文不改需求**：与需求不一致的地方只有一处（§3.1），单独列出来等你点头。

---

## 1. 设计假设与边界

### 1.1 假设

| 编号 | 假设 | 如果不成立会怎样 |
|---|---|---|
| A1 | 队列（8283）与 agent（8383）在本机；平台只听 `127.0.0.1` | 多机需要 TLS + 认证升级（Q8 已定不做） |
| A2 | 本机是 Windows（开发机），部署机是 Linux | 两边都要跑（NFR-02），差异只在「怎么起进程」 |
| A3 | 事件量是「人看」的量级（几十~几千条/天），不是遥测 | 窗口 1000 条 / 60 分钟够用；再大要改背压策略 |
| A4 | 队列的全局事件流保持现状（无 `since`、内存广播） | 桥接的缺口提示会显得多余（不成立时反而更简单） |
| A5 | 没有旧客户端要兼容（平台是新东西） | —— |

### 1.2 这次不做什么（设计层面显式列出）

- **不做 TLS / 多机 / 每台一把 token**（Q8 已定）；
- **不做落盘**：窗口、retain、幂等键表都在内存里，重启即清（Q6 已定）；
- **不改 C++**、不改 MQTT broker 的 topic 白名单（MQTT 路线以后再说）；
- **不让平台做业务**：不判读、不派任务、不存任务状态；
- **不做 GUI**（观察 = 订阅端 + 流水 + 一条命令）；
- **不做「至少一次投递」的承诺**：窗口内能给、给不了就明确说给不了（gap），不假装可靠；
- **不新增 topic**：只用需求 §2.5 登记过的那几个；
- **不改造队列的 SSE**（只读它）；
- **不做权限细分**：本机一份 token，能发就能订，反之亦然。

---

## 2. 总体架构

```
        ┌──────────────┐   SSE(现状,无since)   ┌──────────────────┐
        │ 队列 8283     │ ────────────────────► │ bus_pump.py      │
        │ /api/events   │                       │ 队列事件 → 平台    │
        └──────┬───────┘                       └────────┬─────────┘
               │ SSE(现状)                              │ POST /api/publish
               ▼                                        ▼
        ┌──────────────┐  speech                ┌──────────────────────┐
        │ router.py     │ ─────────────────────► │ bus_server.py 8299   │
        │ 线索 + 说话    │  heartbeat             │ topics + 窗口 + retain│
        └──────────────┘                        │ /health /api/publish │
                                                │ /api/subscribe (SSE) │
        ┌──────────────┐  alert + heartbeat     └───────┬──────────────┘
        │ watchdog.py   │ ──────────────────────────────┤
        │ 巡检 + 告警    │ ◄──── subscribe heartbeat ────┘
        └──────────────┘
```

| 进程 | 文件 | 输入 | 输出 | 挂了会怎样 |
|---|---|---|---|---|
| 平台 | `sched/bus_server.py` | 发布请求 / 订阅请求 | 订阅者收到消息 | 订阅方重连、发布方记日志；**告警仍有文件出口** |
| 桥接 | `sched/bus_pump.py` | 队列 SSE | `a2/queue/*` | 队列照常；恢复后发一条 `gap`（见 §8） |
| Router | `sched/router.py` | 队列 SSE（不变） | `a2/router/speech` + `a2/heartbeat/router` | 用户听不见；watchdog 报 `router_missing` |
| Pump | `sched/bus_pump.py` | 队列 SSE（不变） | `a2/queue/*` + `a2/heartbeat/pump` | 外面的人看不见活儿；watchdog 报 `pump_missing` |
| Watchdog | `sched/watchdog.py` | 队列/agent（不变）+ `a2/heartbeat/#` | `a2/watchdog/alert` + 审计 + 队列事件流 | 没人盯；**Q5「谁看 watchdog」仍未定**，本文只提供心跳（§10.4） |

**启动顺序**：平台 → 桥接 / router / watchdog（后三者各自重连，顺序不是硬依赖）。

---

## 3. 与需求的对照

### 3.1 唯一一处解释性差异（请你点头）

**AC-08.1 写的是「router 订阅平台；每收到一个事件 → 发布 `a2/router/speech`」。**

设计上 **router 的输入仍然只订阅队列**，平台只是它的**输出**。理由：

- Q2 = 并存：平台上的 `a2/queue/*` 本来就是队列事件的副本；router 若同时读队列与平台，
  每件事会说两遍（同一事件有两个来源）；
- 队列事件流包含平台**不转发**的类型（`task_cancelled`、`task_waiting_confirmation`、
  `trigger_decided`、`queue_paused`…），router 现在会对它们说话；只读平台会丢这些话术；
- 保持「一个声音、一个输入源」比「两路输入去重」简单得多。

**验收等价性**：AC-08.1 要验的是「每个事件都有一句话上平台」，这一条照旧成立
（输入来自队列，输出到平台）。如果坚持字面意义上的「订阅平台」，就要在 router 里加一套
按事件指纹去重的逻辑——那是多出来的复杂度，我建议不加。

### 3.2 其它与需求的对应

| 需求 | 设计里落在哪 |
|---|---|
| FR-01~FR-06（平台本体） | §5~§9（`bus_server.py`） |
| FR-07（桥接） | §8（`bus_pump.py`） |
| FR-08（router） | §9.1 |
| FR-09（watchdog） | §9.2 |
| FR-10（演练） | §12（`tests/smoke_bus_e2e.py`） |
| FR-11（回退） | §13 |
| FR-12（传播中心接口） | §5.4（发布/订阅语义即接口；传播中心按 `since` 续订） |

---

## 4. 数据模型

### 4.1 消息

```json
{
  "id": 42,
  "topic": "a2/queue/task_done",
  "at": "2026-11-27T09:00:00.123Z",
  "key": "queue:t_abc:task_done:2026-11-27T09:00:00Z",
  "payload": { "...": "任意 JSON 对象" }
}
```

| 字段 | 说明 |
|---|---|
| `id` | 本进程内单调递增整数（从 1 开始）。**重启后重新从 1 开始**——配合「窗口不落盘」，语义是「这台平台这次运行的编号」 |
| `topic` | 见 §4.2 |
| `at` | UTC ISO8601，毫秒 |
| `key` | 可选的幂等键；同一个 key 只投递一次（§7） |
| `payload` | JSON **对象**（不是裸字符串），≤ 64 KB（序列化后） |

### 4.2 topic 与匹配

> 名字上的一个坑：`tests/smoke_router.py` 里早有一个 `speech_in_the_bus()`，那里的 "bus"
> 指的是**队列的事件流**。本文的「平台」是新的那个东西；`bus_*` 只是文件名/进程名（Q9 已定）。
> 文档里一律叫「平台」，别把两者混成一个。

固定名单（与需求 §2.5 一致，新增必须改文档）：

| topic | 谁发 | payload 要点 |
|---|---|---|
| `a2/queue/task` | pump | 队列 `task_created` 事件（含 `task.id/title/workspace_id/kind`） |
| `a2/queue/task_done` | pump | 队列 `task_done`（含 `task.id`、结果摘要） |
| `a2/queue/task_failed` | pump | 队列 `task_failed`（含 `task.id`、`error`） |
| `a2/router/speech` | router | `{line, speech_of, task_id?, workspace_id?, at}` |
| `a2/watchdog/alert` | watchdog | `{check, target, reason, level, at, task_id?}` |
| `a2/heartbeat/router` | router | `{at, pid, version, uptime_s, queue_url, bus_url}`，retain=true |
| `a2/heartbeat/watchdog` | watchdog | 同上 |
| `a2/heartbeat/bus` | bus_server | `{at, pid, version, uptime_s, subscribers, window:{oldest,newest}}`，retain=true |
| `a2/heartbeat/pump` | bus_pump | `{at, pid, version, uptime_s, queue_connected, last_event_at, published, skipped, failed, gaps}`，retain=true |
| `a2/heartbeat/channel` | channel（两个 bot） | `{at, pid, version, channel, workspace_id}`，retain=true |
| `a2/heartbeat/channel` | channel（每个 bot） | `{at, pid, version, channel, workspace_id}`，retain=true |

**命名规则**（发布时校验，坏的一律 400，不静默）：

```
topic   = level ("/" level)*              level = [a-z0-9][a-z0-9_.-]*
filter  = level+ ("/" level+)*            level+ = level | "+"
          最后一层可以是 "#"（只能出现在最后）
```

- 最长 200 字符、最多 8 层；
- 大写、空格、以 `/` 开头、`//`、`a2/**`、`a2/#/x`、`+x` 一律拒；
- 匹配规则与 `src/mqtt_codec.h` 的 `topic_matches` **同语义**（`+` 一层、`#` 其余），
  这样以后换 MQTT 不用改订阅端的写法。

### 4.3 窗口 / retain / 幂等键（都不落盘）

| 东西 | 容量 | 淘汰 | 重启后 |
|---|---|---|---|
| 消息窗口 | 1000 条 **且** 60 分钟（先到者为准） | 旧的先走 | **清空** |
| retain | 每个 topic 一条 | 被新的一条覆盖 | **清空** |
| 幂等键表 | 2000 个 key | 与它指向的消息一起走；满了按最早丢 | **清空** |

> 三个数字都在 `<tea home>/bus.json` 里可改。`/health` 必须报 `window.oldest/newest`
> 与 `persisted: false`——**别让人以为能补**（AC-04.3）。

---

## 5. HTTP 接口（`bus_server.py`）

| 方法 | 路径 | 用途 | 鉴权 |
|---|---|---|---|
| `POST` | `/api/publish` | 发布一条 | 要 token |
| `GET` | `/api/subscribe?filter=…&since=…` | 订阅（SSE） | 要 token |
| `GET` | `/health` | 只读状态 | **免** |
| `GET` | `/` | 一行说明 + 版本 | 免 |

### 5.1 发布

```http
POST /api/publish
Authorization: Bearer <token>
Content-Type: application/json

{"topic":"a2/test/hello","payload":{"a":1},"key":"k1","retain":false}
```

| 结果 | 何时 |
|---|---|
| `200 {"id":42,"topic":…,"at":…}` | 新消息 |
| `200 {"id":7,"topic":…,"duplicate":true}` | 同一个 `key` 已经发过：**不重复投递**，返回第一次的 id |
| `400 {"code":"bad_topic"}` / `{"code":"bad_payload"}` | topic 不合法 / payload 不是对象 |
| `401 {"code":"unauthorized"}` | 无 token 或错 |
| `413 {"code":"payload_too_large"}` | 序列化后 > 64 KB（回复里带上限与实测大小） |

### 5.2 订阅（SSE）

```http
GET /api/subscribe?filter=a2/%23&since=42
Authorization: Bearer <token>
Accept: text/event-stream
```

**`since` 的三种写法**（写进文档与错误消息，免得人猜）：

| 传法 | 含义 |
|---|---|
| 不传 | **只看从现在起**的新消息（watchdog/channel 用） |
| `since=0` | 把**窗口里现有的**全给我（有人刚连上想看最近） |
| `since=N`（N≥1） | 要 `id > N` 的；如果 N 落在窗口之前 → 先发一条 `gap` |

帧格式（带 `id:`，SSE 客户端可以自己记最后一条）：

```
id: 43
event: message
data: {"id":43,"topic":"a2/router/speech","at":"…","key":"…","payload":{…}}

event: gap
data: {"reason":"window","requested":1,"oldest":51}
```

- **`gap` 帧不带 `id:`**：它不是一条消息，客户端不能把它当成「最后收到的 id」；
- `reason`：`window`（要的 id 已滑出窗口）/ `no_history`（平台重启过，什么都没有）；
- 保活：每 15 秒一行 `: keepalive`（**不是** data 帧，客户端解析器不用处理）；
- 服务端空闲超时 3600 秒（沿用队列的 `--sse-idle-timeout` 语义），到点关流，客户端重连。

### 5.3 健康

```json
{
  "ok": true, "version": "1.0.0",
  "uptime_s": 3600,
  "persisted": false,
  "window": {"capacity": 1000, "minutes": 60, "messages": 37, "oldest": 5, "newest": 41},
  "retained": ["a2/heartbeat/router", "a2/heartbeat/bus"],
  "last_message_at": "2026-11-27T09:00:00.123Z",
  "subscribers": [
    {"id": "s_1", "filter": "a2/#", "since": 3, "connected_at": "…",
     "last_sent_at": "…", "queued": 0, "dropped": 0}
  ]
}
```

- `/health` 不含任何 payload，免鉴权；
- `dropped > 0` 是「这个订阅者落下了」的唯一证据（AC-03.4）。

### 5.4 传播中心怎么用（FR-12）

- 取：`GET /api/subscribe?filter=a2/queue/%23&since=<上次最后一条>`，把 `id` 存进自己的游标；
- 发：`POST /api/publish`，`key` 用自己的去重键（就是传播中心的幂等键）；
- 平台不判读、不派任务——传播中心把它们放在自己那一层。

---

## 6. 进程、端口与单实例

| 项 | 决定 | 理由 |
|---|---|---|
| 端口 | 默认 **8299**（`--port`，`TEA_BUS_PORT` 可覆盖） | 8383/8283/8284/8290 已被占用 |
| 监听 | 默认 `127.0.0.1`；`--host 0.0.0.0` 时启动日志**必须警告**（AC-05.3） | 这条线不该随手越 |
| 单实例 | `<data-dir>/bus.pid`（pid + 启动时间）+ **端口独占** | 静默抢端口是这个仓库踩过的坑 |
| 端口独占 | `allow_reuse_address = False`，Windows 上再加 `SO_EXCLUSIVEADDRUSE` | 光关 SO_REUSEADDR 在 Windows 上不够 |
| 被占时 | 拒绝启动，读 `bus.pid` 说出「谁占着（pid/启动时间）」，说不出来就说「端口被别的程序占着」 | 不静默、不猜 |
| 优雅退出 | 关监听 → 给订阅者发一条 `event: closed` → 关连接 → 删 pid 文件 | 客户端能分辨「平台关了」与「网络断了」 |
| 心跳 | 每 10 秒写 `<data-dir>/bus.heartbeat`（pid + at），并每 30 秒发一次 `a2/heartbeat/bus`（retain） | 文件是「平台死了」的证据；retain 给新订阅者立刻看到状态 |

> 平台**不写业务状态**：除了 token / pid / 心跳 / 日志，它不落任何东西。

---

## 7. 幂等

- `key` 是调用方给的「这件事的唯一名字」；同一个 key 第二次发布 → 返回第一次的
  `id` + `duplicate: true`，**不再投递**；
- key 表在内存里，随窗口淘汰（消息被淘汰 → 它的 key 一起淘汰）；
- key 表上限 2000，满了丢最早的（并记一条日志「幂等键表已满」）；
- **重启后 key 表清空** → 同一个 key 会被当成新消息。这不是 bug，是 Q6（不落盘）的
  直接后果，写进文档与 `/health`（`persisted:false`）；
- 各生产者的 key 约定（让重复发布天然幂等）：

| 生产者 | key |
|---|---|
| pump | `queue:<task_id>:<event type>:<at>` |
| router | `speech:<sha1(line + at + event type + task id)>` |
| watchdog | `alert:<check>:<target>:<cooldown bucket>`（bucket = `at` 的整 10 分钟） |
| channel | `hb:<channel>:<pid>:<at>`（心跳不去重，本来每次都不同） |

---

## 8. 桥接（`bus_pump.py`）—— 设计里最硬的一处

**问题**：队列的全局事件流**没有 `since`、断线即丢**。所以「恢复后从哪续」没有现成答案。

### 8.1 决定：实时续 + 缺口记在桥接自己身上

| 情况 | 行为 |
|---|---|
| 刚启动 | 从**现在**开始（不补历史，也补不了） |
| 队列断开 / 队列重启 | 重连（退避 2s→10s）；重连上之后**从当下继续** |
| 断线期间有事件 | **补不了**——这是队列事件流（无 `since`）的性质，不是桥接的 bug |

缺口的**记录方式**（不塞进 topic 流）：

- 固定 topic 名单里**没有** `a2/queue/gap`，Q4 规定新增 topic 必须先登记。这一版**不新增**，
  所以**平台上收不到缺口通知**——这是本决定的已知代价，列在这里等确认（要收到，就得登记
  一个 `a2/queue/gap`，一句话的事，但要改需求 §2.5 的名单）；
- 缺口记在桥接自己身上：`<data-dir>/pump-state.json`

  ```json
  {"last_event_at": "...", "last_event_hash": "...",
   "gaps": [{"from_at": "...", "to_at": "...", "reason": "queue_down"}]}
  ```

  `gaps` 只留最近 20 条；`bus_pump.py --status` 打印它们，每轮巡检日志也留一行
  「桥接缺口：从 X 到 Y」——**人能看到，别假装没丢**。

### 8.2 映射（v1 只转发三种）

| 队列事件 `type` | 平台 topic |
|---|---|
| `task_created` | `a2/queue/task` |
| `task_done` | `a2/queue/task_done` |
| `task_failed` | `a2/queue/task_failed` |
| 其它（`task_cancelled` / `task_waiting_confirmation` / `trigger_decided` / `queue_paused` …） | **不转发**：router 仍从队列读到它们并说话（Q2 = 并存）。要转发哪一种，先登记 topic，再加一行映射 |

### 8.3 行为

- 订阅 `GET {queue}/api/events`（与 router 同一套读法，SSE），逐帧转成发布请求；
- 每条发布的 key 见 §7；平台不可达 → 记日志、把这一条**丢掉**（不排队，队列是事实源，
  平台只是分发；排队会制造「恢复后一次喷出一堆旧消息」）；
- 队列不可达 → 重连；平台不可达 → 下一帧再试；
- `--once`：只跑一遍（给测试用）。

---

## 9. 接入方改动

### 9.1 Router（`sched/router.py`）

| 改动 | 内容 |
|---|---|
| 参数 | `--bus URL`（默认空 = 关）、`--bus-token`（默认读 `<tea home>/bus/token`）、`--bus-heartbeat 30`、`--bus-timeout 5` |
| 输出 | `say()` 里在写完 `speech.jsonl` 之后，把同一句话发到 `a2/router/speech`（key 见 §7）；**先写盘再发网**——平台挂了话术也不丢 |
| 心跳 | 后台线程每 `--bus-heartbeat` 秒发 `a2/heartbeat/router`（retain） |
| 降级 | 发布失败只记日志（同一错误最多每 60 秒一行）；不重试、不排队（下一句会带上新状态）；**不订阅平台**（§3.1） |
| 回退 | `--bus` 不给 → 行为与今天**逐字一致**（AC-08.4/11.1） |
| 新增 | `--version` 与 `/health` 无关（router 没有 HTTP 服务）；版本号在心跳里带 |

### 9.2 Watchdog（`sched/watchdog.py`）

| 改动 | 内容 |
|---|---|
| 参数 | `--bus URL`、`--bus-token`、`--bus-heartbeat 30`、`--expect-heartbeat TOPIC`（可重复；默认 `a2/heartbeat/router`）、`--heartbeat-timeout 180`、`--alert-cooldown 600` |
| 新检查 | `router_missing` / `channel_missing` / `bus_missing` / `watchdog_missing`？——**watchdog 不检查自己**（Q5 未定，见 §10.4） |
| 输入 | 后台线程订阅 `a2/heartbeat/#`（`since=0` 拿 retain 的现状），刷新「最后一次心跳」表 |
| 输出 | `escalate()` 在写完审计 + 队列事件流之后，再发 `a2/watchdog/alert`（三处一致，AC-09.2） |
| 冷却 | 同 `check + target` 在 `--alert-cooldown` 内只发一次；冷却表写进 `state.json`（重启后仍生效） |
| 平台挂了 | 仍巡检队列/agent（AC-09.4）：告警写审计 + 队列事件流；日志一行「平台不可达，告警只到文件/队列」 |
| 心跳 | 每 `--bus-heartbeat`（沿用 router 的默认 30）发 `a2/heartbeat/watchdog`（retain） |

### 9.3 Channel（`sched/channel.py` + 两个 bot）

| 改动 | 内容 |
|---|---|
| 参数 | 两个 bot 各加 `--bus`、`--bus-token`、`--bus-heartbeat 60` |
| 心跳 | `TaskChannel.watch_tasks()` 启动时（只一次）起一个守护线程发 `a2/heartbeat/channel`；payload 带 `channel`（wechat/telegram）与 `workspace_id` |
| 不变量 | 不配置 `--bus` → 一行代码都不走（行为不变） |

> 为什么 channel 也要心跳：没有它，watchdog 的 `channel_missing` 永远没法区分
> 「渠道本来就没配」和「渠道死了」。默认不检查 channel（`--expect-heartbeat` 里没有它），
> 要用再配上——这样不会一装好就报警。

---

## 10. 配置、状态与心跳

### 10.1 配置

`<tea home>/bus.json`（不存在就用默认值；**代码里没有硬编码地址**，AC-01.4）：

```json
{
  "host": "127.0.0.1",
  "port": 8299,
  "window_messages": 1000,
  "window_minutes": 60,
  "max_payload_bytes": 65536,
  "subscriber_buffer": 1000,
  "sse_keepalive_s": 15,
  "sse_idle_timeout_s": 3600
}
```

- 环境变量：`TEA_BUS_URL`（客户端用）、`TEA_BUS_TOKEN`、`TEA_BUS_PORT`；
- 命令行优先于文件，文件优先于默认值。

### 10.2 token

- `<data-dir>/token`，**0600**；首次启动自动生成（32 字节 hex），日志里只打印前 8 位；
- 所有客户端（pump/router/watchdog/channel/传播中心）从文件读，不写进命令行（不进程表泄漏）；
- `--token` 只给测试用，文档里标注「测试用」。

### 10.3 状态目录

```
~/.tea_agent/bus/
  token                 0600
  bus.pid               pid + 启动时间（退出删）
  bus.heartbeat         pid + at（每 10 秒）
  pump-state.json       桥接的游标与缺口（只这份落盘，且只记缺口）
  logs → ~/.tea_agent/logs/a2_bus.log / a2_bus_pump.log
```

### 10.4 心跳与「谁看 watchdog」（Q5 仍未定）

- 心跳 topic 已经有了（router/watchdog/bus/channel）；
- **谁看 watchdog**：本文不决定（观察者架构 Q5 还没拍板）。可选方案都留着：
  a) 队列巡检 `audit.jsonl` 的最后写入时间；b) 一个极简的外部进程；c) 系统级监控。
  设计只保证一件事：**watchdog 的心跳是真的**，将来谁看都行。

---

## 11. 部署

| 平台 | 怎么起 | 文件 |
|---|---|---|
| Linux | 两个新单元：`a2_bus.service`（`bus_server.py`）、`a2_bus_pump.service`（`bus_pump.py`）；`scripts/install-units.sh` 的列表加两项 | `scripts/a2_bus.service`、`scripts/a2_bus_pump.service` |
| Windows | 计划任务（登录触发、`wscript` 隐藏窗口、失败重启、忽略第二实例），一个脚本管四个：`-Name bus|bus_pump|router|watchdog` 或 `-All` | `scripts/install-bus-autostart.ps1` |

- Windows 脚本复用 `install-autostart.ps1` 的做法（生成 `start-hidden.vbs` + 任务 XML），
  但**不改那个脚本**（它管 agent，别把职责混在一起）；
- 单元与任务都**不互相拉起**：谁先起都行，各自重连（§2）。

---

## 12. 测试与演练（FR-10）

| 文件 | 覆盖 | 关键手法 |
|---|---|---|
| `tests/smoke_bus.py` | AC-01~AC-06 | 进程内起平台（线程），用 `urllib` 当客户端；SSE 断言按帧解析；窗口/幂等/retain/401/413/坏 topic 逐条 |
| `tests/smoke_bus_pump.py` | FR-07 | 假队列（本地 `ThreadingHTTPServer` 吐 SSE），真平台；断言三种映射、key 形状、队列断开→重连→缺口记录 |
| `tests/smoke_bus_e2e.py` | FR-10、AC-09.1/09.2 | 起真 `bus_server` + 假队列 + 假 agent + 真 `router.py --bus` + 真 `watchdog.py --bus`；发一条队列事件 → 断言平台上有 `speech`；**杀掉 router → 断言 `a2/watchdog/alert`（`router_missing`）+ 审计行**；退出后端口释放、临时目录可清 |
| 既有测试 | AC-08.4/11.1 | 平台关掉时跑 `tests/smoke_router.py`（其中 `speech_in_the_bus` 一段用的就是队列事件流）、`tests/smoke_watchdog.py`，必须全绿；两者已经有 MockAgent + 假队列的骨架，e2e 复用同一套 |

- 所有测试用**临时 tea home**（`TEA_AGENT_HOME` 指到 temp 目录）+ 随机端口（起真进程时）；
- Windows 上 `TEMP/TMP` 指到仓库内（仓库的老坑）；
- e2e 必须能在同一台机器上跑第二遍（AC-10.3）。

---

## 13. 兼容、回退与回滚

| 项 | 做法 |
|---|---|
| 旧行为 | 所有新参数默认关；不传 `--bus` 时 router/watchdog/channel 的代码路径与今天一致 |
| 旧数据 | 无（平台没有历史数据；队列/agent 的数据一个字节不动） |
| 旧客户端 | 无 |
| 回退（运行中） | 停四个新进程；router/watchdog/channel 去掉 `--bus`；不需要改数据 |
| 回滚（发版后） | 删两个单元 + 一个 ps1 + 两个脚本；`sched/` 里三个文件的新参数不用删（默认关） |
| 证明「与今天一致」 | 平台全关，跑一遍现有回归（`tests/smoke_router*.py`、`smoke_watchdog*.py`、`tests/e2e.ps1`），与本轮改动前的基线对比 |

---

## 14. AC → 模块 / 文件

| 需求 | 落点 |
|---|---|
| AC-01.1~01.4 | `sched/bus_server.py`：`main()`/`--help`、`/health`、`bind_and_guard()`（端口独占 + pid）、`load_config()` |
| AC-02.1~02.4 | `bus_server.py`：`Hub.publish()`、`validate_topic()`、`Idempotency`、`Retained` |
| AC-03.1~03.5 | `bus_server.py`：`Hub.subscribe()`、`Subscriber`（deque + dropped）、`match_filter()`、SSE 写出循环（`select`/写失败摘除） |
| AC-04.1~04.3 | `bus_server.py`：`Window`（条数 + 分钟双淘汰）、`/health.window`、`gap` 帧 |
| AC-05.1~05.3 | `bus_server.py`：`require_token()`、`--host` 警告 |
| AC-06.1~06.2 | `bus_server.py`：心跳文件 + `a2/heartbeat/bus`；客户端重连日志（pump/router/watchdog） |
| AC-07.1~07.3 | `sched/bus_pump.py`：`follow()`、`publish_event()`、`PumpState`（缺口） |
| AC-08.1~08.4 | `sched/router.py`：`--bus*` 参数、`say()` 里的发布、`heartbeat_loop()` |
| AC-09.1~09.4 | `sched/watchdog.py`：`check_heartbeats()`、`escalate()` 的第三处、`AlertCooldown`；`sched/channel.py` + 两个 bot 的 `--bus` 与心跳线程 |
| AC-10.1~10.3 | `tests/smoke_bus_e2e.py` |
| AC-11.1~11.2 | 参数默认关（三个文件）+ §13 的回归清单 |
| AC-12.1~12.3 | §5.4（接口语义）；传播中心实现时直接照用 |

---

## 15. 实现顺序（每步都能自测）

| 步 | 做什么 | 做完怎么验 |
|---|---|---|
| 1 | `bus_server.py` 骨架 + `/health` + 端口独占 + pid + token | `tests/smoke_bus.py` 的 AC-01/05/06 段 |
| 2 | 发布 / 窗口 / retain / 幂等 | 同文件 AC-02/04 段 |
| 3 | 订阅 SSE + `since` + gap + 背压 | 同文件 AC-03 段 |
| 4 | `bus_pump.py` + 映射 + 缺口记录（+ 2026-11-28 的 `a2/heartbeat/pump`） | `tests/smoke_bus_pump.py` |
| 5 | `router.py` 的 `--bus`（输出 + 心跳 + 降级） | `tests/smoke_bus_e2e.py` 的 speech 段；平台关掉跑旧回归 |
| 6 | `watchdog.py` 的心跳检查 + 告警三处 + 冷却 | e2e 的 `router_missing` 段 |
| 7 | `channel.py` + 两个 bot 的 `--bus` 心跳 | e2e 里用假 bot 发心跳 → `channel_missing` 段 |
| 8 | 单元 / Windows 脚本 / 文档（`sched/README.md`、`docs/router-watchdog.md`、`docs/testing.md`） | 手动起停一遍；`pwsh -File scripts/install-bus-autostart.ps1 -All -DryRun` |

---

## 16. 风险与备选方案

| 风险 | 影响 | 对策 |
|---|---|---|
| 平台成为单点 | 订阅方全瞎、发布方只记日志 | 发布方先写盘（router 的 `speech.jsonl`、watchdog 的 audit）再发网；watchdog 看 `bus_missing`；回退开关 |
| 订阅者慢把内存撑大 | 平台 OOM | 每订阅者 deque 上限 + `dropped` 计数 + 写失败摘除（AC-03.4） |
| 桥接补不齐 | 人以为看到了全部 | `pump-state.json` 的缺口 + `--status` + 日志（§8.1），**不假装可靠** |
| 重启后 id 从 1 开始，客户端 `since` 错乱 | 订阅方以为「什么都收到了」 | `since` 落在窗口外 → 先发 `gap`（`reason=no_history`）；文档写死 |
| Windows 上长连接/退出不干净 | 端口占着、任务起不来 | 退出时关监听 → 发 `closed` → 删 pid；e2e 跑两遍验证 |
| token 泄漏 | 本机任意进程能发 | 0600 + 不进命令行 + 日志只打前 8 位 |
| 心跳误报（时钟/暂停） | 半夜报警 | 超时默认 180s（6× 间隔）；冷却 600s；心跳 payload 带 `pid`，换进程就算新心跳 |

**被否掉的备选**：

| 方案 | 为什么不用 |
|---|---|
| 直接用队列的 `/api/events` 当平台 | 没有 `since`、监听队列无上限（慢订阅者把队列内存撑大）、没有 token、发布只有本机操作员 |
| 现在就用 MQTT | 要改 broker 的 topic 白名单（C++）+ TLS 证书；Q1 已定 HTTP |
| 把总线做进 router | watchdog 必须能独立于 router 存在；单点更糟 |
| 文件队列（一个目录当 topic） | 没有扇出语义、延迟高、清理麻烦 |
| 平台落盘（SQLite） | Q6 已定不落盘；先证明「内存 + 缺口说明」够用 |

---

## 17. 留给下一阶段的三件事

1. **观察者架构 Q5（谁看 watchdog）** 还没拍板——本文只把心跳准备好；
2. **传播中心 Q2~Q12** 还开着——平台是它的传输层，但它的业务规则不在本文范围；
3. **MQTT 换传输层**：接口语义已按 MQTT 同形设计（§4.2/§5.2），换的时候只需改
   `bus_server` 的传输实现与 broker 的 topic 白名单。

---

## 18. 实现记录（v1.0 之后补记）

八步按 §15 的顺序落地，一步一个提交：

| 步 | 提交 | 自测 |
|---|---|---|
| 1~3 | `03ed6a2` `sched/bus_server.py` | `tests/smoke_bus.py`（3 遍） |
| 4 | `630bc10` `sched/bus_pump.py` | `tests/smoke_bus_pump.py`（2 遍） |
| 5 | `332b952` router 的 `--bus` | `tests/smoke_bus_router.py`；`tests/smoke_router.py` 旧回归 |
| 6 | `73cd1a3` watchdog 的 `--bus` | `tests/smoke_bus_watchdog.py`（2 遍）；`tests/smoke_watchdog.py` 旧回归 |
| 7 | `dbd4a08` channel 心跳 | `tests/smoke_channel_heartbeat.py`；`tests/smoke_telegram_bot.py` 65/65 |
| 8 | `231b981` e2e + 单元 + Windows 脚本 | `tests/smoke_bus_e2e.py`（2 遍）；`-All -DryRun` |

实现时定下、本文原先没写死的细节：

1. **回填与实时分开**（`Subscriber.put_backlog()`）：`since` 的回填只受窗口约束，不经过实时队列的 `deque(maxlen=N)`。否则一个要补 1000 条的订阅者会被先判成「太慢」——连那条 `gap` 帧都发不出去。
2. **`since` 大于最新 id**（例如平台重启后客户端还带着旧的大数字）：发一条 `gap` 帧，`reason: no_history`，然后只给新消息。**不能**当成「从 1 开始重放」——那会让客户端以为今天发生的事从没发生过。
3. **退出时发 `closed` 帧**，再关监听、删 pid：客户端立刻重连，而不是等 keepalive 超时。
4. **桥接的心跳**（原先的缺口，2026-11-28 补上）：Q4 的固定名单原先只有 `{router,watchdog,bus,channel}`，于是桥接死了没人报。现在 `docs/message-platform.md` §2.5 登记了 `a2/heartbeat/pump`，`bus_pump.py` 每 `--bus-heartbeat`（默认 30）发一条 retain 心跳（带 `queue_connected`、计数、缺口数），watchdog 映射成 `pump_missing`。**默认仍只期待 router**：没跑桥接的机器不该因为「没有心跳」被报警，要看桥接就加 `--expect-heartbeat a2/heartbeat/pump`（Windows 安装脚本已经加上了）。
5. **router 不订阅平台**（§3.1 的 Q2 解释）：平台是它的输出；输入仍是队列。
6. **看门狗的冷却表落在 `state.json`**：重启不会把十分钟内说过的话再说一遍；审计里被压掉的告警不写条目，日志里每分钟一行「still going on」。
7. **告警三处独立投递**：队列投不进去也照写审计（改之前是「队列失败就 return」，于是队列一挂，审计里连一条记录都没有）。
