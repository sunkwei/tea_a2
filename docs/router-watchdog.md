# Router 与 Watchdog：触发器入口、线索与看门狗

> 这一页是**使用文档**：三个组件（触发器入口、Router、看门狗）已经落地，并且在这台机器上
> 常驻（`a2_router.service` / `a2_watchdog.service`）。写法与取舍记在文末的
> [设计与取舍](#设计与取舍)。
>
> 缘起：DeepSeek Harness 团队 2026-10-31 发了《Super-agent 设计探索：迈向更自主的
> Agent 系统》（[报道](https://m.ithome.com/html/1020615.htm)、
> [原型仓库](https://github.com/deepseek-ai/dsh-superagent-prototype)），那套架构里有两个
> 角色我们原来**完全没有**：把人和任务连起来的 **Router**，和在外面盯着 Worker 的
> **Watchdog**。

## 现在有什么

| 组件 | 是什么 | 代码 | 常驻？ |
| --- | --- | --- | --- |
| **触发器入口** | 外部事件（webhook / 邮件 / 聊天）统一进门：去重 → 规则 → 模型判断 → **受理才变任务** | `sched/triggers.py` + 队列 | 队列的一部分 |
| **Router** | 把分开的工作区会话串成一条**线索**：记住每一步、收拢旧进展、统一对外说话 | `sched/router.py` | `a2_router.service` |
| **看门狗** | 在 agent 和队列**外面**盯着：卡住的任务、agent 健康、额度、机群设备；还能问模型「跑偏了没有」 | `sched/watchdog.py` | `a2_watchdog.service` |
| **计划** | 一个复杂活儿拆成几步：步骤之间可以互相等，做完合成一份交付物 | `sched/plans.py` + `sched/planner.py` | 队列的一部分 |

```
人 ──► 渠道（微信 / Telegram / 邮件 / Webhook / 定时）
          │
          ▼
      触发器入口 ──规则过滤──► 模型判断 ──► 队列里的任务（work item）
          │                                          │
          │                                          ▼
          │                                   Worker（agent 的一个隔离 session）
          │                                          │
          └──────────── Router（线索、上下文、说话）◄─┤ 进展 / 结果
                                                     │
      看门狗（独立进程，外部回路）：盯着 Worker、agent、额度、设备 ──► 动作 + 通知
```

两条边界，和 Super-agent 一样：

- **Worker 不直接跟人说话**：进展和结果回到 Router，由 Router 统一对外（渠道要加
  `--speech` 才切过来）。
- **看门狗在 Agent 外面**：它不能被 Agent 自己关掉——否则一个卡死的 Worker 也能把看门狗
  关了。

## 上手

### 装 & 起

```bash
scripts/install-units.sh                             # 装 a2_router / a2_watchdog 两个单元
systemctl --user enable --now a2_router a2_watchdog
systemctl --user status a2_router a2_watchdog --no-pager
```

两个单元的默认参数（单元文件在 `scripts/`）：

| 单元 | 跑什么 | 默认参数 |
| --- | --- | --- |
| `a2_router.service` | `sched/router.py` | `--tasks http://localhost:8283 --agent http://localhost:8383 --spill-after 4` |
| `a2_watchdog.service` | `sched/watchdog.py` | `--tasks http://localhost:8283 --agent http://localhost:8383 --stall-after 15` |

### 每天看的几个地方

| 看什么 | 在哪 |
| --- | --- |
| 队列里都有什么 | `GET http://127.0.0.1:8283/api/tasks` |
| 线索知道什么 | `GET http://127.0.0.1:8283/api/threads`、`.../threads/{id}/context` |
| 触发器都判了什么 | `GET http://127.0.0.1:8283/api/triggers` |
| router 都说了什么 | `~/.tea_agent/router/speech.jsonl` |
| 看门狗看到什么 | `~/.tea_agent/watchdog/audit.jsonl`（只追加） |
| 队列暂停了吗 | `GET http://127.0.0.1:8283/api/queue/state` |

两个日志文件**在第一次有内容时才生成**（队列一直空着的时候，它们还不存在——不是坏了）。

### 第一次用：先写一份规则

规则文件默认还没有（`~/.tea_agent/triggers.conf`）。没有规则的后果是：**每一条触发器都会
问模型一次**（花钱、也可能判错）。先从模板抄一份，把「明显不是活儿」的挡在模型之前：

```bash
cp sched/triggers.template ~/.tea_agent/triggers.conf
$EDITOR ~/.tea_agent/triggers.conf
```

抄完**不用重启**——队列每次判读前都会看一眼文件有没有变。

### 排错

| 症状 | 多半是 |
| --- | --- |
| 用户没收到「做完了」 | 渠道单元**没加 `--speech`**（router 的话没人递）；或者 router 挂了 → `systemctl --user status a2_router` |
| 任务卡住没人管 | 看门狗默认**只告警**：告警在 `audit.jsonl` 和队列事件流里。要它动手得加 `--auto --action fail` |
| router 沉默很久 | 前端会在日志里说「router 沉默 N 分钟」；router 挂了用户就听不见——这是那个模式的代价 |
| 队列自己又开始干活 | 有人调过 `POST /api/queue/resume`；暂停状态在 `<队列数据目录>/queue_state.json` |
| 事件投进来没变成任务 | `GET /api/triggers` 看 `decision` 和 `reason`：规则命中了 ignore，或者模型判了 ignore |
| 模型判读没生效 | 规则文件写错（`~/.tea_agent/triggers.conf`）：每段一个来源，`ignore` 优先于 `require` |

---

## 触发器入口

**所有外部事件走同一个门。** 一个 webhook、一封邮件、一条聊天消息，本质上是同一件事：
「外面来了个东西，要不要让 agent 去办」。以前每个渠道自己判断一遍，现在只写一次。

```bash
# 外部系统（CI、GitHub、自己的脚本）投一条事件
curl -X POST http://127.0.0.1:8283/api/triggers \
  -H 'Content-Type: application/json' \
  -d '{"source": "webhook", "dedup_key": "ci:build:1234",
       "text": "构建失败了：单元测试报错",
       "workspace_id": "ws_xxx"}'
# → {"duplicate": false, "trigger": {"decision": "accept", "task_id": "t_…", …}}

# 今天都来了什么、怎么判的
curl http://127.0.0.1:8283/api/triggers
curl http://127.0.0.1:8283/api/triggers?source=webhook&decision=ignored
```

### 决策的顺序（讲究在顺序上）

1. **去重**：`dedup_key` 见过就是同一件事 → 直接返回原来那条，**不重复排活儿**。
   没带 key 时 key = 来源 + 文本的哈希；想排两次就自己带不同的 key。
2. **规则**（先于模型，省钱也稳）：`~/.tea_agent/triggers.conf`，每段一个来源。
   命中 `ignore` → 不管；命中 `require` → 受理。**`ignore` 优先于 `require`**：
   「这条不是活儿」是明确的决定，而漏掉一条真活儿的代价比多问一句大。
3. **模型**（规则没定音才问）：借 agent 的 `POST /api/llm/chat` 答 `accept` / `ignore` + 理由。
   **模型没答上来时按「受理」处理**——宁可多排一个能取消的任务，也不让一条真活儿悄悄消失。

受理的触发器变成任务（带 `trigger_id` 和 `thread_id`）。同一段对话的触发器落进同一条**线索**。

### 规则文件

`~/.tea_agent/triggers.conf`（模板：`sched/triggers.template`）：

```ini
[webhook]
require = 部署
ignore  = 广告

[mail]
require = /CI (failed|error)/i
ignore  = 订阅, newsletter
```

- 每个来源一段（`[wechat]`、`[mail]`、`[webhook]`……），关键词用逗号分开（中文逗号也行）；
- 写成 `/.../` 就是正则，后面可以加 `i`（不区分大小写）；
- **文件改了自动生效**，不用重启队列；
- 规则文件不是 yaml（这台机器上没人装 pyyaml）。

### 队列的参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--trigger-config` | `~/.tea_agent/triggers.conf` | 规则文件 |
| `--trigger-timeout` | 60 | 判读那次模型调用的超时（秒） |

### 渠道要不要走这个门

聊天前端**代码层面默认不走**（白名单里的人跟 bot 说话通常就是有活儿干，问一遍模型既花钱又可能
判错）。要走就加 `--triggers`：

```bash
python3 sched/wechat_bot.py --triggers      # 消息先过触发器入口
```

**装出来的两个单元已经带了 `--triggers`**（`scripts/a2_telegram.service`、
`scripts/a2_wechat.service`）——为的是线索：受理的触发器带一条「线索」（thread），
下一个任务就知道之前发生过什么（prompt 里多一段「这条线索之前的事」）。
代价是每条消息都要过一遍判断，所以**规则要先写好**：

```ini
# ~/.tea_agent/triggers.conf
[telegram]
require = /.*/      # 这个来源的每一条都受理，不问模型
[wechat]
require = /.*/
```

`/.*/` 是正则（匹配一切）；写成 `.*` 是找字面量「.*」，几乎永远不命中——那种情况下
每条消息都得问模型一次。`scripts/install-units.sh` 会在没有规则文件时把
`sched/triggers.template` 抄到 `~/.tea_agent/triggers.conf`（**已有的不覆盖**）。

邮件看门狗（`sched/mail_watch.py`）**没有改**：它有自己的一套判读（更专业：分广告/账单/私人，
还要起草回复），再套一层通用判断就是花两次钱、可能得出两个结论。

---

## Router

**把分开的工作区会话串成一条线索。** 每个任务都是自己的 session，谁也不知道谁——
「接着刚才那件事继续」对模型来说就是从零开始。Router 记住每一步，下一个同线索的任务
就被告知之前发生了什么。

```bash
python3 sched/router.py                  # 跟着队列，攒上下文、收拢摘要、说话
python3 sched/router.py --spill-after 2  # 两条就收拢（默认 4）
cat ~/.tea_agent/router/speech.jsonl     # router 都说了什么
```

### 两件事

1. **线索记得住**：
   - 任务每报一次进度、给出结果、提一个问题，都记进它所属的线索；
   - **窗口**（最近 8 步）直接进下一个同线索任务的 prompt（`task_context` 里多一段
     「这条线索之前的事」）；
   - 掉出窗口的进 `spill`（还在，只是不在 prompt 里）；
   - `spill` 攒够了由 router 用模型**收拢成一句摘要**顶掉那些原文（router 唯一的一次
     模型调用）——prompt 还是那么大，更早的事也还在；
   - **跨线索故意不流通**：串味比忘事更糟。
2. **对外说话**：每个事件说一句话，写进 `~/.tea_agent/router/speech.jsonl`。
   router 还会把这句话**递到队列的事件流**里（`speech` 事件）——聊天前端加 `--speech`
   就只递 router 的话，不再自己翻译原始事件：

   ```bash
   python3 sched/wechat_bot.py --speech     # 前端只递 router 的话
   ```

   切之前建议先比一比：`speech.jsonl` 里的 router 话术 vs 用户实际收到的话。

### 模型怎么按需读更早的事

线索的完整记录（摘要 + 窗口 + 更早的原文）写在**工作区**里：

```
<工作区>/.tea_agent_run/threads/<线索 id>.md
```

下一个同线索任务的 prompt 里有一句「这条线索的完整记录在 …，要用 `toolkit_file` 读它」——
模型用**它本来就有的**文件工具就能读更早的事，不用加新工具。工作区不在（或者 agent 挂了）
就写不了，那种情况下 prompt 里只是少这一句，窗口里的上下文照样有。

### 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--spill-after` | 4 | 攒够几条才收拢成摘要 |
| `--interval` | 30 | 多久扫一遍线索（秒） |
| `--publish` / `--no-publish` | 开 | 递进队列事件流，还是只记在 `speech.jsonl` |
| `--data-dir` | `~/.tea_agent/router` | 话术日志的目录 |
| `--model-timeout` | 120 | 收拢摘要那次模型调用的超时（秒） |

### 队列侧的新接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/threads` | 线索列表 |
| `GET` | `/api/threads/{id}` | 一条线索 + 它的任务 |
| `GET` | `/api/threads/{id}/context` | 窗口、spill、给下一个任务的 prompt |
| `POST` | `/api/threads` | 手工开一条线索（`{"key": "…"}`） |
| `POST` | `/api/threads/{id}/spill` | router 写回摘要（**只有本机操作员能写**：摘要会进 prompt） |

---

## 计划：复杂活儿拆成几步

Super-agent 的原型里，复杂活儿是**拆开**的：Worker 各干各的，谁也不用等谁；我们这边多一层
——步骤之间可以**互相等**，而且做完要有一个人把它们**合成**一份交付物。

这层落在队列里（不是 agent 里）：队列本来就管「什么时候做什么」，它知道每一步的状态。

```bash
# 交一个计划：几步、每步等谁
curl -X POST http://127.0.0.1:8283/api/plans -H 'Content-Type: application/json' -d '{
  "title": "写一份上手指南",
  "goal": "让新用户 5 分钟会用 tea_a2",
  "steps": [
    {"key": "read",  "title": "读 README", "prompt": "读 README.md，列出关键事实"},
    {"key": "write", "title": "写指南",   "prompt": "写成一份上手指南", "depends_on": ["read"]}
  ]}'
```

交完就有了：一个**父任务**（`kind: "plan"`，装最终结果）+ 每一步一个子任务。

| 谁 | 做什么 |
| --- | --- |
| 队列 | 只启动「依赖都做完了」的步骤；哪一步失败，依赖它的步骤被停掉（不等了） |
| 队列 | 全做完了 → **一次模型调用**把各步结果合成一份，写进父任务的 `result` |
| 队列 | 几步是**同一条线索**：router 记得住谁做了什么，下一步的 prompt 里带着上一步的结果 |
| router | 像平常一样说话：「排了一个计划（4 步）：…」「做完了 t_x：…」（`speech` 事件） |
| 看门狗 | 步骤都做完了、父任务却一直没写结果 → `plan_stuck` 告警（`audit.jsonl` + 事件流） |

**规划是单独的一步**（`sched/planner.py`）：一次模型调用把活儿拆成计划，写成 JSON 文件，
**人可以改过再交**——模型拆的步不一定对。

```bash
python3 sched/planner.py --goal "把 30 个文件分类并写一份报告" --out /tmp/plan.json
$EDITOR /tmp/plan.json
python3 sched/planner.py --submit /tmp/plan.json
```

### 为什么汇总在队列里，不在 router 里

router 已经有「线索」这个容器和唯一那次模型调用，看起来汇总该归它。但**队列知道最后一步是
什么时候做完的**，router 只是隔 30 秒来看一眼。汇总放在队列里，计划才不会因为 router 恰好
不在就悬着。router 照旧负责**说话**：最终结果是一个 `task_done` 事件，router 把它变成一句话。

## 看门狗

**在 agent 和队列外面盯着它们。** agent 很会干活，但它不会注意到自己停了：一次卡住的工具
调用、一个跟队列一起死掉的 worker、一个额度用光还在烧钱的 provider——这些都不会自己说话。
得有个外面的人看，而且不能是被看的那一个。

```bash
python3 sched/watchdog.py                       # 只说不动手（默认）：盯住队列、agent 和额度
python3 sched/watchdog.py --stall-after 3       # 3 分钟没动静就算卡住
python3 sched/watchdog.py --once                # 只跑一遍就退出（给 cron 用）
python3 sched/watchdog.py --auto --action fail  # 允许它把卡死的任务判死（只建议在盯着的时候用）
python3 sched/watchdog.py --drift               # 连「在忙但跑偏了」也问模型
python3 sched/watchdog.py --fleet http://127.0.0.1:8290   # 连带看机群设备
```

### 巡检什么

| 检查 | 判据 | 动作 |
| --- | --- | --- |
| `stalled_task` | 队列里 `running` 的任务：`updated_at` 超过 `--stall-after` 分钟**而且**两次巡检之间 `progress` 没动 | `escalate`（默认）/ `fail` / `retry`（`--auto` 才准） |
| `agent_down` | `GET /api/health` 不通或超时 | `escalate` |
| `quota_low` | 队列 `/api/health` 里的 `quota_percent` 低于 `--min-quota` | `escalate` |
| `device_offline` | 机群设备连不上（配了 `--fleet` 才查） | 只记审计（日报里本来就有） |
| `drift` | 模型看轨迹后认为「没在推进」或「跑偏了」（`--drift` 才开） | `escalate`（**只是建议**） |

**「卡住」要两个条件**：记录安静够久（`--stall-after`）**而且**两次巡检之间进度没动。
只看时间会误杀正在慢慢干活的活儿；只看一次也不算数——一次慢工具调用不是卡死。

**动作的默认是保守的**：`escalate`（把情况告诉人），**不自动 fail、不自动 delete**。
`--auto` 才允许 `fail` / `retry`，而且 `fail` 必须带原因写进审计。理由：看门狗误杀一个正在
跑的长任务，代价比晚一点发现大得多。

**模型版判读（`--drift`）**：读 agent 的轨迹（`GET /api/sessions/{id}/trajectory`，agent 早
就在写了）加任务自己报的进展，问「还在推进吗、方向跑偏了吗」。**产出的只是建议**：写进审计、
跑偏了说一声，**动作仍然由规则决定**——模型说「跑偏了」也不能结束谁的工作。
`--drift-after N` 让跑超过 N 分钟的任务也被问（这就是「在忙但方向错了」那类）。

### 告警怎么到人手里

看门狗把告警塞进队列的事件流（`watchdog_alert`），渠道那条轮询链路认这个事件类型，把告警
推给对应的人——**不新增通知通道**：

- 带任务的告警 → 推给排这个任务的人（任务 meta 里的 `user_id`）；
- 不带任务的告警（agent 挂了、额度见底）→ 推给所有跟 bot 说过话的人（整台机器的事）。

事件的过滤规则：**不带任务的事件 everybody 都收**（它不含任何项目的内容），带任务的仍然只到
它自己的项目——项目的 key 不许看见别的项目在干什么。

### 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--tasks` / `--agent` | `127.0.0.1:8283` / `127.0.0.1:8282` | 队列和 agent 的地址 |
| `--fleet` | 空 | 机群服务地址（空 = 不看设备） |
| `--interval` | 30 | 两趟之间隔多久（秒） |
| `--stall-after` | 15 | 多久没动静算卡住（分钟，0 = 不看） |
| `--min-quota` | 0 | 额度低于多少要提醒（0 = 不看） |
| `--drift` / `--drift-after` | 关 / 0 | 要不要问模型；跑超过多少分钟也问 |
| `--action` | `escalate` | 对卡住的任务做什么：`escalate` / `fail` / `retry` |
| `--auto` | 关 | 允许它动手（否则只说） |
| `--once` | 关 | 只跑一遍 |
| `--data-dir` | `~/.tea_agent/watchdog` | 审计与状态 |
| `--audit` / `--state` | 在 `--data-dir` 下 | 审计日志（只追加）/ 状态文件 |

### 队列侧的新接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/tasks/{id}/fail` | 把 running 的任务标成 failed（**必须带 `reason`**）。看门狗专用 |
| `POST` | `/api/queue/pause` | 暂停调度（`{reason}`） |
| `POST` | `/api/queue/resume` | 恢复调度 |
| `GET` | `/api/queue/state` | `{paused, reason, paused_at, counts}` |
| `POST` | `/api/events` | 往事件流里塞一条事件（**只有本机操作员能塞**——项目的 key 不许伪造事件） |

暂停写在数据目录的 `queue_state.json` 里，**服务重启也还是停着的**：一个「先别干活」的
决定不该因为进程重启就自己失效。

---

## 设计与取舍

以下是**为什么这么写**，不是怎么用。

### 三个部件的分工

Super-agent 的五个部件，我们已经有了五个（Trigger / Worker / Router / Watchdog / 事件驱动）。
关键在**谁做什么**：

| 部件 | 谁写 | 为什么 |
| --- | --- | --- |
| 线索的记录 | **队列** | 队列本来就看得见每个任务的每一步（事件是它自己发的），线索本来就属于任务记录。router 挂了，线索照样记，只是没人收拢、没人说话 |
| 收拢摘要 | **router** | 摘要要花一次模型调用，队列不该替它做主 |
| 对外说话 | **router** | 「一个声音」：话术只写一遍，微信和 Telegram 说同样的话 |
| 巡检与介入 | **看门狗**（独立进程） | 它要是跑在 agent 里，一个卡死的 worker 就能把它一起关掉 |

### 对象模型

| 对象 | 是什么 | 落在哪 |
| --- | --- | --- |
| **Trigger** | 一次外部事件。**去重靠 `dedup_key`** | `<数据目录>/triggers/<id>.json` |
| **Thread** | 一组相关任务的线索，也是**跨 session 的共享上下文**单元 | `<数据目录>/threads/<id>.json` |
| **Work item** | **就是现在的 task**，不改名 | 任务记录里加 `trigger_id` / `thread_id` |
| **Watchdog check** | 一次巡检的判定 | `~/.tea_agent/watchdog/audit.jsonl`（只追加） |

触发器与线索的记录放在**子目录**里（`triggers/`、`threads/`），不放根目录：队列的任务列表会
读数据目录下每一个 `*.json`，触发器不是任务。

### 明确不做的事

- **不做云端 BYOM**（计算在云端、执行环境在本地）：我们是单机/局域网部署。
- **不做 A2A 通信**：机群的 `fleet_server.py` 已经是「一台服务器管 N 台设备」的形态。
- **不做跨月级长程记忆**：thread 级（一条线索内）就够。
- **不改 agent 内核**：Worker 还是现在这个 agent、session 还是隔离的；Router 和看门狗都在
  **外面**。
- **不把 task 改名成 work item**：文档里说「work item」只是为了和 Super-agent 的说法对齐。

### 风险与取舍

| 风险 | 对策 |
| --- | --- |
| 看门狗误杀正在跑的长任务 | 双阈值；默认动作是 escalate，`--auto` 才动手；每次动作写审计 |
| 触发器入口成了单点 | 队列已有幂等键和审计；`POST /api/tasks` 老路一直留着 |
| 全局上下文串味 | 线索按工作区隔离；`thread_id` 为空的任务不进任何上下文 |
| Router 接管回话后渠道行为变了 | 分两步：先影子运行（`--no-publish`），对比一段时间再切 |
| 队列被看门狗的 fail 打断 | fail 只改记录不动正在跑的线程；要真正停掉走 `POST /api/tasks/{id}/cancel` |

### 路线（M0~M6 全部完成）

| 阶段 | 是什么 | 状态 |
| --- | --- | --- |
| M0 | 这份文档 | ✅ |
| M1 | 看门狗 v1（独立进程，默认只告警） | ✅ `sched/watchdog.py` |
| M2 | 触发器统一入口（`POST /api/triggers`） | ✅ `sched/triggers.py` |
| M3 | Router（跨会话的上下文 + 对外说话） | ✅ v1/v2/v3 `sched/router.py` |
| M4 | 模型版看门狗（判断跑偏了没有） | ✅ `--drift` |
| M5 | 把活儿派给设备上的 a2 | ✅ `target: fleet` |
| M6 | 复杂活儿自主拆成几步，做完汇总 | ✅ `POST /api/plans` + `sched/planner.py` |

## 文档地图

- 队列本身：[tasks.md](tasks.md)
- 机群（另一条控制平面）：[fleet.md](fleet.md)
- HTTP 接口全表：[api.md](api.md)
- 怎么跑测试：[testing.md](testing.md)
- 开发笔记（为什么这么写）：[tea_a2_manual.md](../tea_a2_agent.md)
