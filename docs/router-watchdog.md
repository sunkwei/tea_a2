# Router 与 Watchdog：把「一个人盯着一堆会话」变成系统能力

> 这一页是**规划**，不是已实现的功能。写它是因为 DeepSeek Harness 团队 2026-10-31 发了
> 《Super-agent 设计探索：迈向更自主的 Agent 系统》（[报道](https://m.ithome.com/html/1020615.htm)、
> [原型仓库](https://github.com/deepseek-ai/dsh-superagent-prototype)），那套架构里有两个角色
> 我们现在**完全没有**：把人和任务连起来的 **Router**，和在外面盯着 Worker 的 **Watchdog**。
>
> 本文只写**我们要做什么、落在哪个文件、怎么验收**。每一步都能独立上线，不做完也不影响
> 现有功能。

## 一句话的架构

```
人 ──► 渠道（微信 / Telegram / 邮件 / Webhook / 定时）
          │
          ▼
      Trigger 入口 ──规则过滤──► 模型判断 ──► 队列里的 work item（现在的 task）
          │                                          │
          │                                          ▼
          │                                   Worker（agent 的一个隔离 session）
          │                                          │
          └──────────── Router（全局上下文、对外说话）◄─┤ 进展 / 结果
                                                     │
      Watchdog（独立进程，外部回路）：盯着 Worker、agent、额度、设备 ──► 动作 + 通知
```

关键的两条边界，和 Super-agent 一样：

- **Worker 不直接跟人说话**。Worker 只管干活，进展和结果回到 Router，由 Router 统一对外。
- **Watchdog 在 Agent 外面**。它不能被 Agent 自己关掉——否则一个卡死的 Worker 也能把看门狗
  关了。

## 现状盘点：哪些已经有了

Super-agent 的五个部件，我们已经有了三个半：

| Super-agent 的部件 | 我们现在对应什么 | 差在哪 |
| --- | --- | --- |
| **Worker**（隔离会话里并发干活） | agent 的 session/turn（`src/agent.cpp`），队列按工作区串行、跨工作区并行（`sched/task_server.py` 的 `Engine.round()`） | 隔离和并发都有了。缺「Worker 只回报给 Router」这条纪律 |
| **Trigger**（外部事件） | 微信/Telegram 机器人往队列投任务、邮件 IMAP 触发（`sched/mail_watch.py`） | 每个渠道各写一套投递逻辑；没有统一的 trigger 记录、去重和「要不要做」的判断 |
| **Router**（接人 + 全局上下文 + 分派 + 对外说话） | **没有**。渠道直接投队列、agent 直接回渠道；没有跨 session 的上下文 | 整层缺 |
| **Watchdog**（外部控制回路） | **没有**。只有三处局部兜底：agent 单轮的 `max_iterations`、队列重启时把 running 标 failed（`recover_stale_tasks`）、机群升级的金丝雀/回滚 | 都是「事后补救」，没有「运行中巡检 + 介入」 |
| 事件驱动 | 队列的 SSE 事件流（`/api/events`）、审计（`/api/audit`）、任务事件落盘（`events.jsonl`） | 事件有了，但没有「事件 = Trigger」的统一入口 |

已经有的零件可以直接复用，不用重写：

- `sched/channel.py` 的 `Queue`（队列 API 客户端）与 `TaskChannel`（聊天前端的共同那一半）：
  Router 和 Watchdog 都用它说话。
- agent 的 `POST /api/llm/chat`：**一次模型调用，不跑 agent 循环**。`mail_watch.py` 已经用它做
  邮件判读——Router 的「这个事件要不要动手」和 Watchdog 的「这个 Worker 还在推进吗」都用它。
- 队列的任务记录里已经有 `updated_at` / `progress` / `note` / `attempt` / `session_id`：
  判断「卡住」要用的字段全都在。
- 机群的 `sched/fleet.py`：`run_job()` 已经是**带金丝雀、批量、验证、回滚**的作业执行器——
  我们在设备升级上已经有「外部控制回路」的雏形，缺的只是把它推广到 agent 任务上。

## 目标对象模型

名字先冻结，避免和现有的 task / session 打架：

| 对象 | 是什么 | 落在哪 |
| --- | --- | --- |
| **Trigger** | 一次外部事件（「微信里有人说了句话」「有一封新邮件」）。**去重靠 `dedup_key`** | 队列里的新表，和 task 并列 |
| **Thread** | 一组相关 work item 的线索，也是**跨 session 的共享上下文**单元 | 队列里的新表 |
| **Work item** | **就是现在的 task**，不改名 | `sched/task_server.py` 现有记录，加两个字段 |
| **Watchdog check** | 一次巡检的判定（看到了什么、做了什么） | 只追加的审计文件 |

### Trigger 记录

```json
{
  "id": "tr_1a2b3c",
  "source": "wechat",
  "dedup_key": "wechat:u42:1730000000",
  "text": "把字体设置拆成三行",
  "workspace_id": "ws_xxx",
  "decision": "accepted",
  "reason": "命中规则 require:字体",
  "task_id": "t_9f8e7d",
  "thread_id": "th_2c1b0a",
  "created_at": "2026-11-01T09:00:00+08:00"
}
```

`decision` 有三态：`accepted`（建了任务）、`ignored`（规则或模型判定不用做）、`pending`
（还在等模型判断）。

### Work item（task）加的字段

| 字段 | 干什么 |
| --- | --- |
| `trigger_id` | 这个任务是从哪条 trigger 来的（可空——直接 POST /api/tasks 的老路没有） |
| `thread_id` | 属于哪条线索（可空；为空则不进任何 thread 的上下文） |

### Thread 记录

```json
{
  "id": "th_2c1b0a",
  "title": "字体设置",
  "workspace_id": "ws_xxx",
  "context": [{"at": "...", "text": "..."}],   // Inline：最近几条进展，直接进 prompt
  "spill":   [{"at": "...", "text": "..."}],   // Spill：溢出的摘要，按需再读
  "task_ids": ["t_9f8e7d", "t_2a11bc"]
}
```

Inline & Spill 的意思是：**近期的进展直接放进上下文窗口，超出预算或很久以前的存到窗口外面，
需要时再读回来**。我们不需要一开始就上向量检索——先做「最近 N 条 + 摘要落盘」。

## 分阶段路线

每一阶段都能单独上线，都有自己的测试文件。**M1 先做**：Watchdog 不改任何现有行为，纯增量，
而且立刻解决真问题（任务卡住没人管）。

### M0：这份文档（已完成）

产出：本文。冻结对象名、接口草案、分阶段顺序。
验收：文档进 `README.md` 的文档表，`tea_a2_agent.md` 里留一条指路。

### M1：Watchdog v1（规则版，独立进程）

**为什么先做它**：它是 Super-agent 里最值钱、也最容易在我们这儿落地的一块——「Worker 卡住了
没人知道」是现在最疼的问题，而且不需要动 agent 内核、不需要改渠道行为。

**落点**：新文件 `sched/watchdog.py`（纯标准库，跟 `fleet.py` 一个风格：没有 pip 依赖，设备上
也能跑）。

**巡检什么**（配置在 YAML 里，默认全关，逐条开）：

| 检查 | 判据 | 动作 |
| --- | --- | --- |
| `stalled_task` | 队列里 `running` 的任务：`updated_at` 超过 `--stall-after` 分钟**且** `progress` 没变 | `escalate`（通知人）/ `retry`（换一个 provider 重排）/ `fail` |
| `agent_down` | `GET /api/health` 不通或超时 | `escalate` |
| `quota_low` | 队列的额度接口返回的剩余低于阈值 | `escalate` + 建议暂停队列 |
| `device_offline` | 机群设备连不上（有 fleet_url 才查） | 记审计（日报里已有） |

**动作的默认是保守的**：`escalate`（把情况告诉人），**不自动 fail、不自动 delete**。
`--auto` 才允许 `fail` / `retry`，而且 `fail` 必须带原因写进审计。理由：watchdog 误杀一个正在
跑的长任务，代价比晚一点发现大得多。

**队列侧要新增的接口**（`sched/task_server.py`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/tasks/{id}/fail` | 把 running 的任务标成 failed（带 reason）。watchdog 专用 |
| `POST` | `/api/queue/pause` | 暂停调度（`{reason}`）；`Engine.round()` 开头检查，暂停时不取任务 |
| `POST` | `/api/queue/resume` | 恢复调度 |
| `GET` | `/api/queue/state` | `{paused, reason, paused_at}` |

**事件**：新增 `watchdog_alert`（`{task_id, check, level, reason, action}`）。
渠道那条 `watch_tasks` 轮询链路认这个事件类型，把告警推给对应的人——**不新增通知通道**。

**验收**：`tests/smoke_watchdog.py`
- 造一个 running 且 `updated_at` 很旧的任务 → watchdog 判定 stalled → 默认动作是 escalate
  （发事件、写审计），**没有**把任务标 failed；
- 带 `--auto` 时才 fail，且审计里有原因；
- `POST /api/queue/pause` 之后调度器不再起新任务，`resume` 之后恢复；
- agent 不通时产生 `agent_down` 告警；
- watchdog 自己挂了不影响队列（它是独立进程）。

### M2：Trigger 统一入口

**为什么**：现在每个渠道各写一套投递逻辑，「这个事件要不要做」的判断散在渠道里
（`mail_watch.py` 用 `/api/llm/chat` 判读邮件就是个孤立的例子）。统一之后，
「规则 + 模型判断」只写一次。

**落点**：队列新增 `POST /api/triggers`（`sched/task_server.py`），渠道改造
（`sched/wechat_bot.py`、`sched/telegram_bot.py`、`sched/mail_watch.py`）。

**流程**：

1. 渠道投一条 trigger：`{source, dedup_key, text, workspace_id}`。
2. **去重**：`dedup_key` 见过 → 直接返回原来那条（幂等，不重复建任务）。
3. **规则过滤**（先于模型，省钱）：`triggers:` 配置里按 source 写 `ignore` / `require` 关键词
   或正则。命中 ignore → `ignored`；命中 require → 直接 `accepted`。
4. **模型判断**（规则没定音时）：借 agent 的 `POST /api/llm/chat` 问一句「这件事要不要做」，
   返回 `accept` / `ignore` + 理由。只有 accept 才建 task。
5. 建 task 时带上 `trigger_id` 和 `thread_id`（同一 dedup 前缀或同一渠道会话 → 同一 thread）。

**保留退路**：`POST /api/tasks` 继续能用（脚本、cron、老代码都在用）。Trigger 入口是**新增的
入口**，不是替换。

**事件**：`trigger_received` / `trigger_decided`（accepted|ignored）。

**验收**：`tests/smoke_triggers.py`
- 同一个 `dedup_key` 投两次 → 只有一条 trigger、一个任务；
- 命中 `require` 规则 → 建任务，**没有**调用模型（用 mock 计数）；
- 规则没命中 → 调模型判读，accept 才建任务，ignore 不建；
- 老路 `POST /api/tasks` 仍然工作。

### M3：Router 进程（全局上下文 + 对外说话）

**为什么**：Super-agent 的核心不是「多开几个会话」，而是**上下文同步从人手上移到系统层面**。
我们现在每个 session 是孤立的：微信里聊过的事，队列里的另一个任务不知道。

**落点**：新文件 `sched/router.py`（常驻进程，订阅队列的 `/api/events` SSE）。

**做什么**：

1. **维护 thread 上下文**：一个 thread 的进展写进 `context`（Inline，最近 N 条），
   超出预算或太旧的摘要进 `spill`（Spill）。下一个属于这个 thread 的任务，创建时把这份
   上下文作为 `task_context` 的一部分交给 agent（队列的 `context_for()` 已经是干这个的位置）。
2. **统一对外说话**（先影子运行）：Router 订阅事件，把「任务完成 / 需要确认 / 卡住 / 告警」
   翻译成一句人话，**记**下来但**不接管**渠道推送。
3. 渠道改造（第二阶段再切）：`TaskChannel` 不再直接轮询任务状态，改成收 Router 的回话。

**队列侧新增**：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/threads` | 线索列表 |
| `GET` | `/api/threads/{id}` | 一条线索 + 它的任务 |
| `GET` | `/api/threads/{id}/context` | Inline + Spill（给 prompt 用） |
| `POST` | `/api/threads` | 手工开一条线索 |

**验收**：`tests/smoke_router.py`
- 同一 thread 的第二个任务能拿到第一个任务的进展摘要；
- 不同 thread 的上下文**不串**（串味是最容易犯的错，必须有测试钉住）；
- 溢出的摘要落盘、可读回；
- 影子模式下渠道推送行为**没有变化**。

### M4：模型版 Watchdog（判断「跑偏了没有」）

规则版只能发现「没动静」，发现不了「在忙但方向错了」。这一步用 agent 的
`POST /api/llm/chat` 读任务的轨迹（`<run_dir>/trajectories/<session_id>.jsonl`，agent 已经在写了），
问两个问题：**这个 Worker 还在推进吗？方向跑偏了吗？**

- 产出的**只是建议**，动作仍由规则决定（除非显式 `--auto-drift`）。
- 建议写进审计，人可以在 UI/渠道里看到「watchdog 认为这个任务跑偏了」。

**验收**：`tests/smoke_watchdog.py` 加一组用例（mock `/api/llm/chat` 的回答）。

### M5（可选）：把 Router 接到机群

Router 不只往本机 agent 派活，也能往**设备上的 a2** 派（`sched/fleet.py` 的 `ask_devices()`
已经是雏形：让设备自己的 agent 回答）。这一步等 M1~M3 稳了再说。

## 明确不做的事

- **不做云端 BYOM**（计算在云端、执行环境在本地）。我们是单机/局域网部署，BYOM 是给别人
  的方案，不是我们的问题。
- **不做 A2A 通信**（Agent 实例之间互相对话）。机群的 `fleet_server.py` 已经是「一台服务器
  管 N 台设备」的形态，那才是我们的 A2A。
- **不做跨月级长程记忆**。thread 级（一条线索内）就够；跨线索的长期记忆是另一个课题。
- **不改 agent 内核**。Worker 还是现在这个 agent，session 还是隔离的；Router 只在**外面**。
- **不把 task 改名成 work item**。队列里、接口里、文档里都还叫 task；文档里说「work item」
  只是为了和 Super-agent 的说法对齐。

## 风险与取舍

| 风险 | 对策 |
| --- | --- |
| watchdog 误杀正在跑的长任务 | 双阈值（`updated_at` **且** `progress` 都没变）；默认动作是 escalate，`--auto` 才动手；每次动作写审计 |
| Trigger 入口成了单点 | 队列已有幂等键和审计；`POST /api/tasks` 老路一直留着 |
| 全局上下文串味（不同项目/线索混在一起） | thread 按 workspace 隔离；thread_id 为空的任务不进任何上下文；必须有测试钉住 |
| Router 接管回话后渠道行为变了 | 分两步：先影子运行（只记录），对比一段时间再切 |
| 队列被 watchdog 的 fail 打断 | fail 只改记录不动正在跑的线程；要真正停掉走 `POST /api/tasks/{id}/cancel`（走 agent 的 stop） |

## 文档地图

- 队列本身：[tasks.md](tasks.md)
- 机群（另一条“控制平面”）：[fleet.md](fleet.md)
- HTTP 接口全表：[api.md](api.md)
- 怎么跑测试：[testing.md](testing.md)
