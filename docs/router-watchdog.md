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

| 阶段 | 是什么 | 状态 |
| --- | --- | --- |
| M0 | 这份文档 | ✅ |
| M1 | Watchdog v1（独立进程，默认只告警） | ✅ 已实现（`sched/watchdog.py`） |
| M2 | Trigger 统一入口（`POST /api/triggers`） | ✅ 已实现（`sched/triggers.py`） |
| M3 | Router（跨会话的上下文 + 对外说话） | ✅ v1/v2 已实现（`sched/router.py`；v2：router 说话、渠道 `--speech` 递话） |
| M4 | 模型版 Watchdog（判断跑偏了没有） | ✅ 已实现（`--drift`，只产建议） |
| M5 | Router 接到机群（可选） | ⏳ 未开始 |

### M0：这份文档（已完成）

产出：本文。冻结对象名、接口草案、分阶段顺序。
验收：文档进 `README.md` 的文档表，`tea_a2_agent.md` 里留一条指路。

### M1：Watchdog v1（规则版，独立进程）—— ✅ 已实现

**为什么先做它**：它是 Super-agent 里最值钱、也最容易在我们这儿落地的一块——「Worker 卡住了
没人知道」是现在最疼的问题，而且不需要动 agent 内核、不需要改渠道行为。

**落点**：新文件 `sched/watchdog.py`（纯标准库，跟 `fleet.py` 一个风格：没有 pip 依赖，设备上
也能跑）。

#### 怎么用

```bash
# 只说不动手（默认）：盯住队列、agent 和额度
python3 sched/watchdog.py --tasks http://127.0.0.1:8283 --agent http://127.0.0.1:8282

# 盯紧一点：3 分钟没动静就算卡住；只跑一遍就退出（给 cron 用）
python3 sched/watchdog.py --stall-after 3 --once

# 允许它动手：把卡死的任务判死（只建议在盯着的时候用）
python3 sched/watchdog.py --auto --action fail

# 连带看机群设备
python3 sched/watchdog.py --fleet http://127.0.0.1:8284
```

审计与状态默认写在 `~/.tea_agent/watchdog/`（`audit.jsonl` 只追加，`state.json` 记上次看到什么）。

#### 巡检什么

| 检查 | 判据 | 动作 |
| --- | --- | --- |
| `stalled_task` | 队列里 `running` 的任务：`updated_at` 超过 `--stall-after` 分钟**且**两次巡检之间 `progress` 没动 | `escalate`（默认）/ `fail` / `retry`（`--auto` 才准） |
| `agent_down` | `GET /api/health` 不通或超时 | `escalate`（整台机器的事，不带任务） |
| `quota_low` | 队列的 `/api/health` 里的 `quota_percent` 低于 `--min-quota` | `escalate` |
| `device_offline` | 机群设备连不上（配了 `--fleet` 才查） | 只记审计（日报里本来就有） |

**「卡住」要两个条件**：记录安静够久（`--stall-after`），**而且**两次巡检之间进度没动。
只看时间会误杀正在慢慢干活的活儿；只看一次也不算数——一次慢工具调用不是卡死。

**动作的默认是保守的**：`escalate`（把情况告诉人），**不自动 fail、不自动 delete**。
`--auto` 才允许 `fail` / `retry`，而且 `fail` 必须带原因写进审计。理由：watchdog 误杀一个正在
跑的长任务，代价比晚一点发现大得多。

#### 队列侧新增的接口（`sched/task_server.py`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/tasks/{id}/fail` | 把 running 的任务标成 failed（**必须带 `reason`**）。watchdog 专用 |
| `POST` | `/api/queue/pause` | 暂停调度（`{reason}`）；`Engine.round()` 开头检查，暂停时不取任务 |
| `POST` | `/api/queue/resume` | 恢复调度 |
| `GET` | `/api/queue/state` | `{paused, reason, paused_at, counts}` |
| `POST` | `/api/events` | 往事件流里塞一条事件（`{"type": "watchdog_alert", …}`）。**只有本机操作员能塞**——项目的 key 不许伪造事件 |

暂停写在数据目录的 `queue_state.json` 里，**服务重启也还是停着的**：一个「先别干活」的
决定不该因为进程重启就自己失效。

#### 告警怎么到人手里

看门狗把告警塞进队列的事件流（`watchdog_alert`），渠道那条 `watch_tasks` 轮询链路认这个
事件类型，把告警推给对应的人——**不新增通知通道**：

- 带任务的告警 → 推给排这个任务的人（任务 meta 里的 `user_id`）；
- 不带任务的告警（agent 挂了、额度见底）→ 推给所有跟 bot 说过话的人（整台机器的事）。

事件的过滤规则也改了：**不带任务的事件 everybody 都收**（它不含任何项目的内容），带任务的
事件仍然只到它自己的项目——项目的 key 不许看见别的项目在干什么。

#### 「先判后写」

watchdog 把一个任务判死时，**先写结论再停那一轮**：worker 那一轮的最后一句「done」晚到时，
不能把结论改回去。队列里这条规矩叫「先到先算」（`Engine._finalize`）。

#### 与计划的出入

- **配置走命令行，不走 YAML**：这台机器上没人装 pyyaml（`fleet.py` 也是自己解析的）。
  `--stall-after 0` 不看卡住的任务，`--min-quota 0` 不看额度，不给 `--fleet` 不看设备。
- **多了一个接口 `POST /api/events`**：计划里只写了「新增 `watchdog_alert` 事件」，但看门狗是
  **外面**的进程，它得有个门能把告警塞进事件流。
- **`retry` 的实现是「先判死再重排」**：队列的 `retry` 只接受已经结束的任务，所以「重来一次」
  = fail + retry（attempt + 1）。

#### 验收：`tests/smoke_watchdog.py`

- 造一个 running 且很久没动静的任务 → 看门狗判定 stalled → 默认动作是 escalate
  （发事件、写审计），**任务还在跑**；
- 带 `--auto --action fail` 时才判死，且审计里有原因；
- `POST /api/queue/pause` 之后调度器不再起新任务（**一个请求都不发给 agent**），`resume` 之后恢复；
- agent 不通时产生 `agent_down` 告警；
- 告警进了任务的事件日志，渠道那一半（`sched/channel.py`）会把它推给对应的人；
- 看门狗自己挂了不影响队列（它是独立进程）。

### M2：Trigger 统一入口 —— ✅ 已实现

**为什么**：现在每个渠道各写一套投递逻辑，「这个事件要不要做」的判断散在渠道里
（`mail_watch.py` 用 `/api/llm/chat` 判读邮件就是个孤立的例子）。统一之后，
「规则 + 模型判断」只写一次。

**落点**：新文件 `sched/triggers.py`（规则、触发器记录、线索记录、决策流程），
队列新增接口（`sched/task_server.py`），聊天前端多一个开关
（`sched/channel.py` + 两个 bot 的 `--triggers`）。

#### 怎么用

```bash
# 一个外部系统（CI、GitHub、自己的脚本）投一条事件
curl -X POST http://127.0.0.1:8283/api/triggers \
  -H 'Content-Type: application/json' \
  -d '{"source": "webhook", "dedup_key": "ci:build:1234",
       "text": "构建失败了：单元测试报错",
       "workspace_id": "ws_xxx"}'
# → {"duplicate": false, "trigger": {"decision": "accept", "task_id": "t_…", …}}

# 看今天都来了什么、怎么判的
curl http://127.0.0.1:8283/api/triggers
curl http://127.0.0.1:8283/api/threads          # 线索（同一段对话）
```

规则写在 `~/.tea_agent/triggers.conf`（模板：`sched/triggers.template`）：

```ini
[webhook]
require = 部署
ignore  = 广告

[mail]
require = /CI (failed|error)/i
ignore  = 订阅, newsletter
```

#### 流程（顺序是有讲究的）

1. **去重**：`dedup_key` 见过 → 直接返回原来那条（幂等，不重复建任务）。没带 key 时，
   key = 来源 + 文本的哈希（同一个来源的同一段话算同一件事；要排两次就自己带不同的 key）。
2. **规则**（先于模型，省钱）：命中 `ignore` → 不管；命中 `require` → 受理。
   **`ignore` 优先于 `require`**：「这条不是活儿」是明确的决定，而漏掉一条真活儿的
   代价比多问一句大。
3. **模型**（规则没定音时）：借 agent 的 `POST /api/llm/chat` 问一句「这件事要不要做」，
   答 `accept` / `ignore` + 理由。**模型没答上来时按「受理」处理**——宁可多排一个能取消的
   任务，也不让一条真活儿悄悄消失。
4. **受理的触发器变成任务**：带上 `trigger_id` 和 `thread_id`。
5. **线索（thread）**：同一个来源 + 同一个会话（`thread_key`，没带就从 `dedup_key` 的前缀推）
   → 同一条线索，线索里有它的任务和触发器。Router（M3）的上下文就长在这里。

#### 与计划的出入

- **规则文件不是 yaml**：`~/.tea_agent/triggers.conf`，一段一个来源，`key = 值`。
  这台机器上没人装 pyyaml（队列和机群都是自己解析的）。文件改了自动生效，不用重启。
- **多了一个 `--triggers` 开关**：聊天前端默认**不走**触发器入口。白名单里的人跟 bot
  说话通常就是有活儿干，问一遍模型既花钱又可能判错。要走入口就加 `--triggers`。
- **邮件看门狗没有改**：它有自己的一套判读（更专业：分广告/账单/私人，还要起草回复），
  再套一层通用判断就是花两次钱、可能得出两个结论。等它自己想走入口时再改。
- **决策的词是 `accept` / `ignore`**（模型的原话），不是 `accepted` / `ignored`——
  模型的答案可以原样用。

#### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/triggers` | 投一条事件；答 `{"duplicate": bool, "trigger": {…}}` |
| `GET` | `/api/triggers` | 触发器列表（`source` / `decision` / `workspace_id` / `limit`） |
| `GET` | `/api/triggers/{id}` | 一条触发器 |
| `GET` | `/api/threads` | 线索列表 |
| `GET` | `/api/threads/{id}` | 一条线索（带它的任务） |

**事件**：`trigger_received` / `trigger_decided`（带 `decision`）。

**保留退路**：`POST /api/tasks` 继续能用（脚本、cron、老代码都在用）。触发器入口是**新增的
入口**，不是替换。

#### 验收：`tests/smoke_triggers.py`

- 命中 `require` → 建任务，**模型一次都没被问**（用 mock 计数）；
- 命中 `ignore` → 不建任务，同样不问模型；
- 规则没命中 → 问模型，模型说 accept 才建任务，说 ignore 就不建；
- 同一个 `dedup_key` 投两次 → 第二条是 duplicate，**没有**排第二个任务；
- 同一段对话的两个触发器 → 同一条线索，线索里有这两个任务；
- 老路 `POST /api/tasks` 仍然工作。

### M3：Router（跨会话的上下文 + 对外说话）—— ✅ 已实现（v1）

**为什么**：Super-agent 的核心不是「多开几个会话」，而是**上下文同步从人手上移到系统层面**。
我们现在每个 session 是孤立的：微信里聊过的事，队列里的另一个任务不知道。

**落点**：新文件 `sched/router.py`（常驻进程，订阅队列的 `/api/events`），
线索的记录在 `sched/triggers.py`（M2 留下的），队列负责把进展记进去。

#### v1 做了什么（两件事）

1. **线索记得住**：任务每报一次进度、给出结果、提一个问题，都记进它所属的**线索**。
   下一个同线索的任务，prompt 里就带着「这条线索之前的事」——**这就是 M3 的核心**。
   - 窗口（`context`）留最近 8 条，直接进 prompt；
   - 掉出窗口的进 `spill`（还在，只是outside了 prompt）；
   - `spill` 攒够了由 **router 用模型收拢成一句摘要**（router 唯一的一次模型调用），
     摘要顶掉那些原文——prompt 还是那么大，但更早的事也还在。
2. **对外说话（影子模式）**：router 把每个事件说成一句话，写进
   `~/.tea_agent/router/speech.jsonl`。**聊天前端暂时还自己说话**——先把话术记下来，
   对比一段时间，确认说得更好再切（切的那一刻才动渠道的行为，风险最大的一步留到最后）。

#### 队列侧新增

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/threads/{id}/context` | 线索的窗口、spill 和给下一个任务的 prompt |
| `POST` | `/api/threads` | 手工开一条线索（`{"key": "…"}`） |
| `POST` | `/api/threads/{id}/spill` | router 写回摘要（**只有本机操作员能写**：摘要会进 prompt） |

`GET /api/threads`、`GET /api/threads/{id}` 是 M2 已经有的。

#### 与计划的出入

- **线索的记录由队列写，不由 router 写**：队列本来就看得见每个任务的每一步（它自己发的
  事件），线索本来就属于任务记录。router 只做两件**花钱或需要判断**的事：收拢摘要、说话。
  这样 router 挂了，线索照样记，只是没人收拢、没人说话。
- **spill 里放的是原文，不是摘要**：收拢是 router 的活，队列不替它做主。摘要写回时，
  原文被一条摘要条目顶掉（`kind: summary`）。
- **摘要没有单独的模型调用预算**：router 每 `--interval` 秒扫一遍线索，攒够
  `--spill-after` 条才收拢一次。

#### 怎么用

```bash
python3 sched/router.py                      # 跟着队列，攒上下文、收拢摘要、说话
python3 sched/router.py --spill-after 2      # 两条就收拢（默认 4）
cat ~/.tea_agent/router/speech.jsonl         # router 都说了什么
curl http://127.0.0.1:8283/api/threads/<id>/context   # 一条线索现在知道什么
```

常驻用 systemd：`scripts/a2_router.service`（`scripts/install-units.sh` 会一起装）。

#### 验收：`tests/smoke_router.py`

- 任务的进度进了线索（`GET /api/threads/{id}/context` 看得见）；
- **同线索的第二个任务被告知了之前的事**（mock agent 收到的 `task_context` 里有
  「这条线索之前的事」和前一个任务的进展）；
- 掉出窗口的进展被收拢成了一句摘要，摘要进了下一个任务的 prompt；
- router 每个事件说一句话（`speech.jsonl` 里有「做完了 …」）。

#### M3 v2：router 说话，渠道只递话 —— ✅ 已实现

**v1 里 router 的话只是记下来**（`speech.jsonl`），聊天前端还在自己按事件翻译。现在
router 把每句话**交到队列的事件流**里（`speech` 事件），聊天前端加了 `--speech` 开关：
开了之后它**只递 router 的话**，不再自己翻译队列的原始事件。

- **一个声音**：话术只写一遍（router 里），微信和 Telegram 说同样的话。
- **谁该听**：带任务的话 → 排这个任务的人；不带任务的话（agent 挂了、队列停了）→ 所有
  跟 bot 说过话的人。做完了的那句话后面照旧跟着这个任务做出的文件。
- **router 挂了用户就听不见**：这是这个模式的代价，所以前端会**提醒**
  （`the router has been quiet for N minutes`）。前端不能替 router 说话——那正是这个
  模式的意义。
- **默认还是老路**：不加 `--speech` 就是今天的行为（前端自己翻译）。切之前先用
  `speech.jsonl` 里的记录跟用户实际收到的话比一比。

#### 还没做的（M3 v3）

- **模型读的「更早的事」**：摘要进了 prompt，但模型还不能**按需**去读某条线索的全部原文
  （那需要一个工具，或者把线索写进工作区让它用 `toolkit_file` 读）。
- **跨线索的长期记忆**：一个线索的上下文不会流到另一个线索去——这是有意的（串味比
  忘事更糟）。
- **router 说话时的「线索感」**：现在一句话里只有任务号；等 M3 v3 有了「按需读线索」，
  话术里可以说「接着上次的字体那件事」。

### M4：模型版 Watchdog（判断「跑偏了没有」）—— ✅ 已实现

规则版只能发现「没动静」，发现不了「在忙但方向错了」。这一步用 agent 的
`POST /api/llm/chat` 读任务的轨迹（agent 已经在写了，接口是
`GET /api/sessions/{id}/trajectory`），问两个问题：**这个 Worker 还在推进吗？方向跑偏了吗？**

**产出的只是建议**：模型的话写进审计，跑偏了就说一声（`watchdog_alert`，check=`drift`），
**动作仍然由规则决定**——模型说「跑偏了」也不能结束谁的工作。

**什么时候问**：
- 规则判定「卡住」的任务 → 问一次（**动手之前**问：判完就没得问了）；
- `--drift-after N` 的话，跑超过 N 分钟的任务也会被问（这就是「在忙但方向错了」那类）；
- 同一个进度值只问一次（任务不动就不会每轮烧一次模型调用）。

**怎么用**：

```bash
python3 sched/watchdog.py --drift                 # 只问「卡住」的那些
python3 sched/watchdog.py --drift --drift-after 30 # 跑超过半小时的也问
```

**验收**：`tests/smoke_watchdog.py`
- 模型被问了，判读结果写进了审计（模型说跑偏 → 有一条 drift 的告警）；
- 模型说「正常推进」→ **没有** drift 告警（不打扰人）。

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
