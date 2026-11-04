## 一、结论先说

1. **那份报告的方向是对的，但它的假设是「给一个不存在的第三方编排者用」。**
   现在策略层就在这个仓里：`sched/task_server.py`（8283），同机、同人、同仓、能直接读源码。
   报告里 12 个 P0/P1 缺口，真正卡住这个组合的只有 5 个；反过来，**报告没提的两条才是头号风险**：
   机制层没有 per-turn 预算的**硬上限**（`src/agent.cpp:1308` 的循环没有上界），
   以及**任务与轨迹串不成一条链**（轨迹里没有 `task_id`，队列也没记 `trajectory_id`）。

2. **两层其实已经天然成型了，只是没写下来。**
   a2 = 通道（回合、工具、文件、沙箱、轨迹）；a2_task = 队列（什么时候干、干哪件、干到什么程度、
   要不要问人、怎么通知人）。`docs/tasks.md` 开篇那句「agent 是通道，队列在 Python 侧」就是
   这个分层的口语版。**要补的不是「机制层要变成产品」，而是把两层的契约钉死。**

3. **要补的是四条契约，不是十二个接口**（第三节）：
   **句柄**（提交完能撒手）、**预算**（策略层定、机制层强制）、**策略**（策略层定规矩、机制层执行）、
   **事件**（机制层出、策略层订阅）。这四条立住了，报告里剩下的 P1/P2 大多变成"以后再说"。

4. **一个必须先拍板的前提**：a2_task 是**同机同用户的 Python 进程**——它能 `os.system`、
   能直接读写工作区文件、能绕过 a2 的任何围栏。所以 a2 的"围栏"对策略层**没有强制力**，
   除非把策略层关进容器或换成另一个用户。
   **先决定 a2 是「沙箱」还是「记账 + 审计的执行器」**，否则第三节 M3 会做错方向，
   也做不出真实的安全承诺（详见 3.3）。

5. **路线（一句话）**：机制层先给「回合句柄 + 请求级预算」（让策略层敢撒手），
   策略层改成「提交 + 订阅」（不再一条 SSE 撑到底），然后才是审批闸门与幂等。

---

## 二、报告核对：哪些准、哪些要改口径

| 报告条目 | 0.18.5 源码 | 结论 |
| --- | --- | --- |
| 工具面 D：外部只能调 2 个 web 工具 | `src/server.cpp:1529` 只放行 `toolkit_search/fetch`；MCP 另有一条 `/api/mcp/servers/{name}/call`（`:2772`） | **准，但漏算**：外部可调的是 2 个 web 工具 + 全部 MCP 工具 |
| 缺「控制权中间那档」 | `/api/chat` **已经**支持每轮的 `tools` 白名单（`server.cpp:4381`）、`allow_paths`（`:4408`）、`system_prompt`、`task_id/task_context`、旋钮覆盖 | **要改口径**：不是"没有中间档"，是**中间档只做了一半**——策略层能给一轮限定工具与目录，但**不能自己调工具**（脚本驱动 loop 那档） |
| 没有请求级预算 | `src/agent.cpp:1308` `for (int iteration = 0; ; ++iteration)` **无上界**；无墙钟、无工具调用数、无 token 上限 | **比报告说的更严重**。且 `agent.cpp:2089` 那句「迭代上限」的注释是**误导**——真正的自动停止是「同样的输出连着 3 次」（`kMaxIdenticalOutputs`，`:1266/:1912`） |
| 没有审批闸门 | 本仓确实没有；`tea_a2_agent.md:305` 说审批在外层 Python 框架里 | **准**。但 a2_task 已有**软审批**：`toolkit_task needs_confirmation`（模型自己在 loop 里停下来问人）——那是"模型自觉"，不是"机制强制" |
| 没有异步轮次句柄 | `done` 事件带 `trajectory_id`，`GET /api/turns/{id}/trajectory` 可查（`server.cpp:3908`） | **要改口径**：**审计有句柄，控制没有**——查状态 / 取消 / 预算 / 进度都还缺 |
| 会话分页参数被静默忽略 | `server.cpp:3465` 那个 handler 根本不读这些参数（不是读了不用，是没读） | **准**，且违反本仓自己的规矩（旋钮那节：「写错了会报错，不会被悄悄忽略」） |
| 并发语义两层不一致 | 会话层有 `409 turn_running` / `interrupt` / 排队只采纳一条；文件层无 ETag、无租约 | **准**，且对 a2_task 更要命：队列的「一个工作区同时一个任务」是**队列层的君子协定**，拦不住网页端或第二个队列实例同时改同一个工作区 |
| `MAX_SUBSCRIBERS=8` 是硬天花板 | `server.cpp:148` 是**每回合的跟随者**上限；并发回合上限是 `MAX_ACTIVE_TURNS=24`（`:155`） | **口径要改**：真正卡住 a2_task 的是 **24**（每个任务占一条 SSE 连接 + 一个线程，`task_server.py:1722`），而 8 只是"一个回合能被几个客户端同时看" |
| 无 `/api/whoami`、无 `/api/audit`、无 `/api/metrics`、无 `/api/capabilities` | 路由表里确实没有（`src/server.cpp` 全量路由已核） | **准，但在本组合里优先级低**：编排者是自己人、单机、有源码 |
| 无 `X-Request-Id`、错误无目录 | `src/server.cpp` 里搜不到任何 `Request-Id` | **准** |
| 幂等缺失 | a2 侧确实没有；**但 a2_task 侧有**（`task_server.py:2623/2883` 认 `Idempotency-Key`） | **要改口径**：缺的是**回合/工具调用**这一层的幂等，不是任务这一层 |

### 报告漏掉的两条（对本组合更要紧）

- **L1 任务→轨迹断链**：`src/trajectory.cpp:230-232` 记了 `turn_id/session_id/workspace_id`，**没有 `task_id`**；
  而 `task_server.py` 的 `_consume()` 在 `done` 帧里**只取 `content`，把 `trajectory_id`、`usage`、`metrics` 全丢了**。
  结果：队列知道 `session_id`，但拿不到"这一轮是哪一轮"；看门狗只能去抓
  `/api/sessions/{id}/trajectory?full=1` 的最近两轮凑合（`sched/watchdog.py:315`），
  同一个会话跑过两轮就可能对错对象。
- **L2 围栏漏了命令工具**：`allow_paths` 只管文件工具（`toolkit_file` / `toolkit_load_image`），
  **`toolkit_exec` 完全不受限**（`docs/tasks.md` 里写明了）。所以队列现在给任务开的
  「额外目录」授权，对一个能执行任意命令的模型**形同虚设**。

---

## 三、两层的职责：今天是什么样子，目标是什么

### 3.1 现状

| 关注点 | 今天谁做 | 在哪 |
| --- | --- | --- |
| 回合生命周期、事件流、seq 回放、断线接回 | 机制层 | `src/server.cpp` `/api/chat`、`/api/chat/attach` |
| 工具执行、沙箱、输出截断、轨迹落盘 | 机制层 | `src/tools.cpp`、`src/trajectory.cpp` |
| 文件原语、provider 配置、模型与旋钮 | 机制层 | `/api/workspaces/{id}/file`、`/api/config`、`/api/llm/chat` |
| 什么时候开工（额度闸门） | 策略层 | `task_server.py:1285 round()` |
| 干哪一件、什么顺序、什么依赖 | 策略层 | `task_server.py` + `sched/plans.py` + `sched/triggers.py` |
| 拆成几步、最后汇总 | 策略层 | `sched/planner.py`（一次模型调用出人可改的计划） |
| 要不要问人、问谁、怎么续 | 策略层 | `toolkit_task needs_confirmation` + `confirm_task` |
| 谁跟人说话、跨会话线索 | 策略层 | `sched/router.py` |
| 盯着卡住的任务 | 策略层 | `sched/watchdog.py` |

**分工其实已经是对的**：机制层不知道队列的存在，策略层也不自己写文件。
两边唯一真正的耦合点是 `POST /api/chat` 的一条 SSE 流。

### 3.2 一条原则：策略层「定」，机制层「执行」

凡是要 **不可绕过** 的东西，必须在机制层执行一次：

| 谁定 | 谁执行 | 例子 |
| --- | --- | --- |
| 策略层定预算 | 机制层强制 | 「这轮最多 5 分钟 / 40 次工具 / 20 万 token」 |
| 策略层定策略 | 机制层强制 | 「这单活儿只准读 /data/raw，删东西要人批」 |
| 策略层定顺序与依赖 | 策略层 | plans 的依赖图、优先级、额度窗口 |
| 策略层定话术与通知 | 策略层 | router 的 `speech`、微信/telegram 回话 |

反过来：**机制层不该长出编排能力**。报告第 8 节的三条"不建议加"我完全同意，
再加一条：**机制层不要替策略层做"要不要重试""要不要升级到更贵的模型"的判断**——
那是策略；机制层只需要诚实地报「我被什么打断了、用了多少」。

### 3.3 必须先决定的前提：a2 是「沙箱」还是「记账器」

a2_task 与 a2 **同机同用户**。这意味着：

- a2_task 可以 `os.system("rm -rf ...")`，可以 `open()` 工作区里任何文件；
- 所以 a2 的工具围栏、审批闸门，**对策略层自身没有强制力**；
- 报告 P0-4 说的「敢不敢把 agent 交给一个不完全可信的调度方」，在本组合里
  **换一个说法**：敢不敢让**模型**在策略层授权的范围内活动。

于是有两条路，必须选一条：

- **路线甲（推荐，成本低）：a2 = 记账 + 审计的执行器。**
  承认策略层是可信的；a2 的围栏与审批是用来约束**模型**的（防止模型跑飞、
  防止它删掉不该删的东西），价值在"可审计 + 可停 + 可限额"，不在"隔离"。
  → M3 的实现成本可控（复用已有的 `tools` 白名单 + `allow_paths`），预期收益是
  **可观测 + 可中止 + 有上限**。
- **路线乙：a2 = 真正的沙箱。**
  那策略层就不能是同机同用户的 Python 进程：要么 a2_task 跑在容器里、
  要么工作区归 a2 独占（策略层只能通过 HTTP 读写）、要么策略层用另一个 unix 用户。
  → 这会牵动**整个 `sched/` 的文件访问方式**（队列现在直接写
  `~/.tea_agent/task-workspace/tasks/*.json`、直接读工作区文件做摘要），
  是一次架构改造，不是加接口。

**先定路线甲还是乙，再决定 M3 做多少。** 本文后面的清单默认按**甲**写（工作量可控、
收益立刻可见）；如果选乙，M3 要按"机制层自己管沙箱"重写，队列侧的读写全部要改走 API。

---

## 〇、2026-11-04 已经做掉的部分

这份评估之后，三件事先落地了（见 [tea_a2_agent.md](../tea_a2_agent.md) 的开发笔记）：

| 项 | 状态 |
| --- | --- |
| **M2 请求级预算** | ✅ 做了：机器默认 **300 轮**（`settings` 里可改），`POST /api/chat` 收 `limits`（请求只能**收紧**），到点按**正常结束**收尾（`stop_reason: "budget"` + `budget_used`），墙钟对长工具也生效（`ToolContext::deadline_ms`） |
| **M6 任务与轨迹串链** | ✅ 做了：轨迹记 `task_id` / `stop_reason` / `budget`；队列记 `trajectory_id` / `usage` / `stop_reason`，看门狗不用再猜「最近两轮」 |
| **S2/S7 预算与权限的下发** | ✅ 做了：任务带 `limits`（预算）与 `policy`（可用工具），队列照 agent 的 `limits` 填默认值；**工具白名单从「不给模型看」改成「真的拒绝」**——模型点名要一个没给它的工具，它不执行 |
| M1 回合句柄 + 异步提交 | ⏳ 还没做（队列现在仍是一条 SSE 撑到底） |
| M3 策略执行点（审批闸门 / exec 围栏） | ⏳ 还没做（`allow_paths` 仍只管文件工具，`toolkit_exec` 不受限） |
| M4 幂等键 / M5 能力清单 / M7 请求 id | ⏳ 还没做 |

---

## 四、机制层（a2）要加的

> 优先级：**必做** = 不做则策略层无法安全地撒手；**该做** = 不做则长期别扭；**可选** = 有空再说。

### M1 回合句柄 + 异步提交 【必做，收益最大】

**问题**：a2_task 现在**一个任务占一条 SSE 连接 + 一个线程**（`task_server.py:1722` 起线程 →
`Agent.chat()` 撑着 SSE 到结束）。后果：

- 连接断了（代理超时、网络抖、客户端 3600s 超时）→ 队列把任务判 FAILED
  （`task_server.py:1873`「the turn ended without an answer」），**但 a2 那边这一轮还在跑、还在烧 token**；
- 队列进程重启 → `recover_stale_tasks()` 把任务判死，**a2 那边那一轮还在跑**；
- 并发上限被"连接数"绑死，而不是被"回合数"绑死；
- 策略层想"提交完就走、回头再来取结果"，现在没有这个姿势。

**形状**：

```jsonc
POST /api/chat {"workspace_id":"ws_x","message":"…","async":true}
  → 202 {"ok":true,"turn_id":"t_…","session_id":"s_…","events_url":"/api/turns/t_…/events"}

GET  /api/turns/{turn_id}          → {state:"running|done|failed|stopped|budget",
                                     session_id, started_at, elapsed_ms, last_seq,
                                     iterations, usage, metrics, stop_reason,
                                     result, files_changed, trajectory_id}
POST /api/turns/{turn_id}/stop     → {ok:true}          # 精确停这一轮
GET  /api/turns/{turn_id}/events?since=<seq>   # 事件流（可断点续传）
```

**要点**：

- 复用**现有的 `ChatTurn` 生命周期**（`src/server.cpp` 里已经有的 `turns` map、`seq`、`attach` 回放），
  只是把「HTTP 连接」换成「回合 id」——工作量比看上去小；
- 默认仍然是 SSE（不传 `async` 行为不变），**不破坏现有客户端**；
- `turn_id` 就是 `done` 帧里的 `trajectory_id` 那一族 id（现在叫 `t_1a2b3c…`），可以直接统一。

**验收**：提交一个 10 分钟的回合 → 关掉客户端 → 30 秒后 `GET /api/turns/{id}` 看到 `running` +
已用 token；`POST /api/turns/{id}/stop` 能停掉它。

### M2 请求级预算 【必做】

**问题**：`src/agent.cpp:1308` 的循环**没有上界**。现在唯一的刹车是策略层的
「客户端 3600 秒超时 + `/api/chat/stop`」，而 `stop` 只在**下一个模型/工具边界**生效——
正好卡在最贵的长工具里。一个跑飞的任务可以烧掉整个订阅额度，
而策略层**事前不知道**、**事后查不到**（`_consume()` 把 `usage` 丢了，见 L1）。

**形状**（与报告一致，我补上「到点 ≠ 失败」的语义）：

```jsonc
POST /api/chat {…, "limits":{"max_iterations":8,"max_wall_ms":300000,
                              "max_tool_calls":40,"max_total_tokens":200000}}
→ done: {"stop_reason":"budget",
         "budget_used":{"iterations":8,"wall_ms":300012,"tool_calls":8,"total_tokens":199430}}
```

- **超预算是正常结束**，不是 error：队列据此把任务记成 `done_budget`（或 `failed` 但带
  `stop_reason:"budget"`），不要跟"模型报错"混在一起；
- `max_wall_ms` 要能在**长工具中间**生效：工具执行时定期看预算（`ToolContext` 里已经有
  `cancel` 指针，`agent.cpp:1700` 那段已经在用），加一个 `budget` 指针即可；
- `max_total_tokens` 用 `done` 帧里已有的 `usage` 累计。

**验收**：`max_tool_calls:1` 的回合，模型要调第二次工具时**不再执行**，直接以
`stop_reason:"budget"` 收尾。

### M3 策略执行点（工具围栏 + 审批闸门 + exec 围栏）【该做，路线乙下是必做】

**问题**：现在只有"全权"和"没有"两档；`toolkit_task needs_confirmation` 是**模型自觉**——
模型可以不问，工具已经跑完了才发 `tool_start`，抢在前面停是时序运气（报告 P0-4 的判断成立）。

**形状**（三层，从轻到重）：

```jsonc
// 1) 已有能力的正规化：白名单 + 路径（/api/chat 已经有，这里只是给它一个"策略"的样子）
POST /api/chat {…, "policy":{"tools":{"toolkit_exec":"ask","toolkit_file":"allow"},
                              "paths":{"read":["/srv/proj"],"write":["/srv/proj/.tmp"]}}}

// 2) 审批闸门：命中 ask 时，**执行前**暂停，发事件
{"type":"approval_required","approval_id":"ap_…","tool":"toolkit_exec",
 "arguments":{"command":"rm -rf build"},"expires_at":"…"}
POST /api/turns/{turn_id}/approvals/ap_… {"decision":"allow_once|allow_always|deny"}

// 3) exec 围栏（补 L2 的漏）：命令白名单 / 危险模式拒绝
{"policy":{"exec":{"allow":["npm *","git *"],"deny":["rm -rf *","curl *"]}}}
```

- 默认**保持现在的开放语义**（没有 `policy` 就不收口），不打断现有用法；
- **审批闸门必须配 M1**：它需要一个能被策略层订阅、能被回答的回合句柄；
- `always`（永久允许）要落盘到 `~/.tea_agent/`，否则重启后策略就没了。

**策略层（a2_task）侧要配套**：队列把"命中 ask"翻译成任务记录里的
`needs_confirmation + question`，人在网页/微信/telegram 上答，队列调
`POST /api/turns/{id}/approvals/{aid}`。

### M4 幂等键 【该做，改动最小】

**问题**：a2 侧所有写操作都不认 `Idempotency-Key`。策略层是**重试机器**
（队列轮询、看门狗 `retry`、计划重跑、网络超时后重发）。一次超时重试 = 跑两遍命令 + 扣两次钱。

**形状**：统一 `Idempotency-Key: <uuid>`，24h 内同键返回**同一响应体**（同一个 `turn_id`）；
键相同但 body 不同 → `409 idempotency_conflict`。缓存放 tea home（与 `workspaces.json` 同级），重启不丢。

**注意**：a2_task **已经有**任务级幂等（`task_server.py:2623`），所以这一条是**补齐回合/工具这一层**，
两边风格一致（都用 `Idempotency-Key` 头 + body 里的同名字段）。

### M5 能力清单 【该做，少写胶水】

**问题**：策略层现在**硬编码**在 0.18.5 的接口上（`Agent.primary_model()` 打 `/api/providers/{n}/models`、
看门狗打 `/api/sessions/{id}/trajectory`）。a2 升级、队列没升级时，只有"404 了才知道"。

**形状**：

```jsonc
GET /api/capabilities
{"version":"0.18.5","git_hash":"…",
 "features":{"async_turns":true,"limits":true,"approvals":false,
             "policy":false,"idempotency":false,"exec":false,"mcp":true},
 "limits":{"max_active_turns":24,"max_subscribers":8,"max_tool_output":131072},
 "routes":[{"method":"POST","path":"/api/chat","since":"0.18.0"}]}
```

**用法**：a2_task 启动时读一次，按 `features` 降级（没有 `async_turns` 就退回今天的 SSE 模式）。

### M6 回合摘要 + 任务与轨迹串链 【该做，很便宜】

**问题**（L1）：轨迹里没有 `task_id`；队列不记 `trajectory_id`；`usage` 被丢。
结果：**「这个任务花了多少钱、改了哪些文件、对应哪一轮轨迹」三个问题都答不上**。

**形状**：

- 机制层：`task_id` 进轨迹记录（`src/trajectory.cpp` 的 turn 头里加一个字段，值来自
  `agent.h:39` 已经有的 `options.task_id`）——**几乎零成本**；
- 机制层：`GET /api/turns/{id}`（M1）里带 `result` / `files_changed` / `usage` / `trajectory_id`；
- 策略层：任务记录里存 `turn_id` + `trajectory_id` + `usage`（`new_task()` 加三个字段），
  于是「任务 → 回合 → 轨迹」是一条可点的链，看门狗也不用再猜"最近两轮"。

### M7 请求 id 与错误目录 【可选，但很便宜】

- `X-Request-Id` 从 HTTP 贯到每个 SSE 帧（现在 `src/server.cpp` 里一个都没有）；
- `GET /api/errors` → `{code, http, retryable, meaning, hint}`；429 一律带 `Retry-After`。
  报告里说得对：现在策略层只能写 `if (message.includes('409'))` 这种代码。

---

## 五、策略层（a2_task）要加的

> 这里只列**因为"a2 当机制层"才需要**的改动；队列已有的能力（额度闸门、计划、依赖、
> 触发器、router、看门狗、微信/telegram 前端、导出）不在此列。

### S1 改成「提交 + 订阅」 【跟着 M1】

- `Agent.chat()` 换成：`POST /api/chat {"async":true}` → 拿 `turn_id` →
  订阅 `GET /api/turns/{id}/events?since=`（或轮询 `/api/turns/{id}`）；
- 好处一：连接数与任务数解耦，**并发不再被 24 顶住**（连接只是订阅）；
- 好处二：队列进程重启后能**认领**在跑的回合（`turn_id` 存在任务记录里），
  而不是现在的 `recover_stale_tasks()` 一律判死；
- 好处三：断线不再等于失败。

### S2 预算与策略的下发 【跟着 M2/M3】

- 任务记录加 `limits`（`max_wall_ms` / `max_iterations` / `max_tool_calls` / `max_total_tokens`）
  与 `policy`（工具白名单 / 路径 / 是否要人批）；
- 排队时就能填（`task_order.template`、`sched/task_ui.html` 的表单），
  默认值由 `--task-limits` 一类的参数给；
- **区分"到点"与"失败"**：任务状态加 `done_budget`（或 `failed` + `stop_reason:"budget"`），
  看门狗不要把它当"跑飞了"去 retry。

### S3 审批闭环 【跟着 M3】

- 机制层发 `approval_required` → 队列转成 `waiting_confirmation`（**复用现有字段**，
  `new_task()` 里已有 `needs_confirmation` / `question`）→
  人在网页/微信/telegram 上答 → 队列调 `POST /api/turns/{id}/approvals/{aid}` → 机制层继续；
- 注意与现有的 `confirm_task` 区分：那个是**模型问**（答案作为下一轮消息进去），
  这个是**机制拦**（回合原地停住）。两者的记录字段可以共用，但事件类型要分开。

### S4 能力协商与降级 【跟着 M5】

启动时读 `/api/capabilities`，按 `features` 决定走哪条路（async / 审批 / 幂等），
并在 `/api/health` 里报「当前工作模式」。

### S5 额度 → 预算 【策略层自己的事】

现在的闸门只有一条：provider 剩余百分比 ≥ `--min-remaining` 才开工。
策略层应该把额度换算成**这一轮能花多少**（`max_total_tokens` / `max_wall_ms`）随任务下发，
否则额度是"开工前看一眼"，而不是"这一轮的预算"。

### S6 事件桥接 【策略层自己的事】

队列已有自己的 `/api/events`（`task_server.py:3040`）。应该把**机制层的事件**
（`turn_done` / `approval_required` / `budget_exhausted` / `tool_error`）也汇进这一条总线，
让 router、看门狗、前端**只订阅一处**。否则现在的事件有两套：队列的 `task_done`
和机制的 `done`，两者对不齐（谁先谁后、丢事件怎么办）。

### S7 成本记账 【跟着 M6】

`_consume()` 现在只取 `content`，把 `usage` / `metrics` 丢了。任务记录里存下来，
才能回答「这个月每个 provider 花在哪些任务上」——这正是策略层存在的意义。

### S8 工作区租约 【机制层出、策略层用】

队列的「一个工作区同时一个任务」只在自己进程内成立。机制层提供
`POST /api/workspaces/{id}/lease {ttl_s}`（advisory），
队列在开始任务前取一个、结束时放掉；别的入口（网页直接 `/api/chat`）也能看见"这个工作区有人"。

---

## 六、不要做的

报告第 8 节的三条我同意（不做通用 DAG/工作流引擎、不把 UI 状态塞进接口、不默认强制审批），
再补三条本组合特有的：

- **不要把队列的记录搬进 C++**。任务的真相在 `sched/task_server.py` 的台账里，
  机制层只需要认 `task_id`（用来把轨迹串起来）。
- **不要让 a2 直接调度任务**。`POST /api/schedules`（cron）这种属于策略层，
  放 `sched/` 里更合适（已有 systemd timer 的用法）。
- **不要在 a2 里做多租户**。项目钥匙已经在队列层（`task_server.py:871 project_key()`），
  机制层只需要 `--api-token` 这一把粗锁就够了。

---

## 七、落地顺序与验收

### Step 1（机制层 M1 + M2，策略层 S1 + S2）——让策略层敢撒手

- 机制层：回合句柄 + `limits` + `stop_reason:"budget"`；
- 策略层：`async` 模式 + 任务记录带 `limits` + 「到点」与「失败」分开记；
- **验收**：
  1. 提交一个 30 秒的活儿，**立刻**关掉客户端 → 任务照样跑完、结果照样回到队列；
  2. 队列进程重启 → 在跑的回合被**认领**而不是被判死；
  3. `max_tool_calls:1` 的任务，模型第二次调工具时**没执行**，任务记成"到点"；
  4. 并发 30 个任务（`MAX_ACTIVE_TURNS=24` 之外的部分排队）不再因为"连接不够"而失败。

### Step 2（机制层 M4 + M5 + M6，策略层 S4 + S7）——敢重试、敢升级、算得清

- 幂等键、能力清单、`task_id` 进轨迹、`usage` 进任务记录；
- **验收**：
  1. 同一个 `Idempotency-Key` 发两次 → 同一个 `turn_id`、只跑一遍；
  2. 老版本 a2 + 新队列 → 队列降级到 SSE 模式并在 health 里说明；
  3. 从任务记录能点到那一轮的轨迹；能查出这个任务用了多少 token。

### Step 3（机制层 M3，策略层 S3 + S8）——可托管

- 审批闸门 + 围栏（含 exec）+ 工作区租约；
- **验收**：
  1. 命中 `ask` 的工具**没有执行**，回合停在审批态，队列任务变成 `waiting_confirmation`；
  2. 答"允许一次"→ 工具执行；答"拒绝"→ 工具没跑、任务按拒绝收尾；
  3. `toolkit_exec` 的越权命令被围栏挡住（这是现在最大的漏）。

---

## 八、报告条目在「a2 + a2_task」这个组合下的降级 / 升级

| 报告条目 | 在本组合下 | 理由 |
| --- | --- | --- |
| P0-1 工具通用调用 | **降级为 P2** | 策略层不需要自己调工具：模型在 loop 里调，策略层管的是"准不准调"（→ M3），不是"替模型调" |
| P0-2 工具调用句柄 | **并入 M1** | 回合句柄 + 事件流已经覆盖"看得见进度"；单独的工具句柄只有策略层自己驱动 loop 时才需要 |
| P0-3 请求级预算 | **升为 P0（头号）** | 报告说"必须有"，源码证明是**真的没有上界**，且策略层事后查不到用量 |
| P0-4 策略与审批 | **保留 P0（路线乙下）/ P1（路线甲）** | 取决于 3.3 的前提选择；a2_task 自己已经有软审批 |
| P0-5 异步句柄 | **升为 P0（头号）** | 策略层就是批量作业的调度方，"提交完就走"是它的日常 |
| P0-6 幂等 | **保留 P0，但范围变小** | 任务级幂等已有，只补回合/工具层 |
| P1-1 主体身份 | **降级 P2** | 编排者是自己人；项目钥匙在队列层已有 |
| P1-2 能力清单 | **保留 P1** | 升级 a2 与升级队列不同步时需要它 |
| P1-3 审计与指标 | **拆分**：请求 id / 事件总线保留，Prometheus 降级 | 机制层已有轨迹（审计够用）；缺的是"跨两层的关联 id" |
| P1-4 文件接口前置条件 | **降级 P2** | 策略层目前不走文件 API 改文件；等 M3 的围栏要覆盖写路径时再补 |
| P1-5 工作区并发租约 | **保留 P1** | 队列的串行是单进程内的君子协定（见 S8） |
| P1-6 全局事件总线 | **升为 P1（在策略层做）** | 队列已有 `/api/events`，把机制层事件汇进去即可（S6） |
| P1-7 会话分页被静默忽略 | **保留 P1（顺手修）** | 违反本仓自己的规矩；router 的 thread 窗口迟早要读大会话 |
| P2 全部 | **P2** | 录放、调度、模型路由、MCP/A2A 都不影响这个组合 |

---

← 回到 [README](../README.md) · 接口清单见 [HTTP 接口](api.md) · 队列见 [任务队列](tasks.md) ·
开发笔记见 [tea_a2_agent.md](../tea_a2_agent.md)
