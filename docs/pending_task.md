# 需求对照表：`docs/pending_task.txt` → 现在有什么

需求书是 `docs/pending_task.txt`（v0.1 草案）。这一页是**逐条的回答**：哪一条已经做了、
哪一条还差着、差在哪里、为什么。文末是补齐的顺序。

一句话概括差距：需求要的是**多租户、可审计、可导出、可恢复**的服务级接口；现在的是
**单机、纯文本任务、串行**的轻量队列。差距不在接口数量，在架构（鉴权/租户、幂等、
事件持久化、优先级、检查点、导出）。

## 现在的接口面

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/tasks` | 提交（`workspace_id` `prompt` `title` `provider` `model` `allow_paths` `meta` `priority`，`Idempotency-Key` 头） |
| GET | `/api/tasks` | 列表（过滤 `status` `workspace_id` `created_from` `created_to` `tag` `business_id`；分页 `page` `page_size`） |
| GET | `/api/tasks/{id}` | 单个任务（`include=full` 带上事件与流水） |
| GET | `/api/tasks/{id}/events` | 任务的事件日志（`since_event_id` 增量） |
| GET | `/api/tasks/{id}/export` | 导出（`format=json|zip`，`include_artifacts`） |
| POST | `/api/tasks/{id}/start` \| `cancel` \| `confirm` \| `retry` | 启动 / 取消 / 回答交互 / 重试 |
| DELETE | `/api/tasks/{id}` | 删除记录 |
| GET | `/api/events` | 全局 SSE（实时） |
| GET | `/api/audit` | 审计日志（`task_id` `limit`） |
| GET | `/api/health` | agent 连通性 + 额度 + 各状态计数 + 指标 |
| GET | `/` | 队列网页 |

状态：`queued` / `running` / `waiting_confirmation` / `done` / `failed` / `cancelled`。

## 6.1 任务提交

| 需求 | 状态 | 说明 |
| --- | --- | --- |
| FR-01 提交接口（agent 标识、输入、上下文、元数据、优先级、回调地址） | ⚠️ | 有 `workspace_id`（≈agent 标识）、`prompt`（≈input）、`title`、`provider`、`model`、`allow_paths`、`meta`、`priority`。**缺** `context`（独立字段）与 `callback_url`（webhook） |
| FR-02 返回 task_id / QUEUED / **队列位置** / 创建时间 | ✅ | `queue_position` 有了（按优先级→入队时间排队） |
| FR-03 Idempotency-Key | ✅ | 头或 body 字段都行；同一个键返回**同一个任务**，不重复入队 |
| FR-04 参数校验 | ⚠️ | 空 prompt / 未知 workspace → 400；**没有超长输入的上限** |
| FR-05 标签与业务元数据 | ✅ | 放在 `meta` 里（`meta.tags` / `meta.business_id`），可按它们过滤 |

## 6.2 队列与串行调度

| 需求 | 状态 | 说明 |
| --- | --- | --- |
| FR-06 从 QUEUED 里选任务置 RUNNING | ✅ | `Engine.round()` |
| FR-07 同一时刻最多一个 RUNNING | ✅ | **粒度按 Agent 队列 = 按项目（工作区）**：同一个项目一个时刻只跑一个任务，**不同项目可以并行**。以前是「每轮最多起 1 个」，上一轮没跑完下一轮照起（实测同时跑 2 个）；现在按**记录**数占位，没跑完的任务占着位置。`--max-concurrent` 可以再给一个全局上限（默认 0 = 只按项目算） |
| FR-08 FIFO，可选优先级 | ✅ | 高优先级先，同优先级按入队时间 |
| FR-09 WAITING_INPUT 不占执行槽 | ✅ | 挂起的任务那一轮已经结束，调度器接着选下一个 |
| FR-10 交互回复后重新入队、排队尾、**不抢占** | ✅ | **修好了**：以前 `confirm` 直接把任务置 running 并起线程（会抢占）。现在回答让任务回到 `queued`，答案随任务带着（`pending_message`），轮到它时作为那一轮的第一句话 |
| FR-11 返回队列位置与前方任务数 | ✅ | `queue_position`（1 = 下一个） |
| FR-12 调度延迟通常 ≤ 1s | ⚠️ | `--interval` 默认从 60s 改成 **2s**；额度接口**只在有排队任务时**才查（空闲时不打扰 provider），所以轮询变快不会变成骚扰 |

## 6.3 全状态持久化

| 需求 | 状态 | 说明 |
| --- | --- | --- |
| FR-13 任务元数据 | ⚠️ | 有 id/workspace/provider/model/status/prompt/meta/priority/attempt/时间戳；**没有** `tenant_id`、`queue_id`（`agent_id` ≈ workspace，就是队列的粒度） |
| FR-14 消息历史、工具调用、交互记录 | ⚠️ | 队列记的是 `result`（最终回答）、`steps`（进度流水）、`question`/`pending_message`（交互）、`events`。**消息历史和工具调用在 agent 侧**（session 与 trajectory），队列不复制一份 |
| FR-15 检查点 + 重启后恢复 | ❌ | **没有检查点**。重启时 running 的任务被**明确标记为 failed**（`recover_stale_tasks`），符合「或按配置置 FAILED」那一半，「从检查点恢复」没有 |
| FR-16 生成物存储 + 元数据 | ⚠️ | 记录里有 `artifacts`（path/bytes/kind/tool）；文件本体在 agent 的工作区，下载走 agent 的 `/download` |
| FR-17 摘要默认 / `include=full` 全量 | ✅ | `include=full` 带上事件与流水；更大的东西（对话、文件）走导出接口拿链接 |
| FR-18 状态与事件一致 | ✅ | 事件现在**落盘**（每个任务一个 `events.jsonl`），状态是原子写的文件 |

## 6.4 交互挂起与恢复

| 需求 | 状态 | 说明 |
| --- | --- | --- |
| FR-19 交互请求 + WAITING_INPUT | ✅ | `needs_confirmation` + `question`，状态 `waiting_confirmation` |
| FR-20 返回待交互内容、交互 ID、超时 | ⚠️ | 有内容；**没有 interaction_id、没有超时时间** |
| FR-21 回复接口 | ✅ | `POST /api/tasks/{id}/confirm {"message": …}` |
| FR-22 回复幂等 | ✅ | 再答一次不会重复入队（答案已经在任务里，直接返回同一个任务） |
| FR-23 回复后进入 QUEUED | ✅ | 见 FR-10 |
| FR-24 交互超时策略 | ❌ | 还没有 |

## 6.5 状态查询与历史

| 需求 | 状态 | 说明 |
| --- | --- | --- |
| FR-25 状态详情 | ✅ | 状态、进度、当前步骤（`current_step`）、队列位置、待交互信息、生成物摘要、错误摘要、时间戳 |
| FR-26 历史列表 + 过滤 | ✅ | `status` `workspace_id` `created_from` `created_to` `tag` `business_id` |
| FR-27 分页 | ✅ | `page` + `page_size`，返回 `total` |
| FR-28 任务事件 + `since_event_id` | ✅ | `GET /api/tasks/{id}/events?since_event_id=N`；事件**落盘**，重启后还在 |
| FR-29 终态仍可查 | ✅ | 记录一直在（除非 DELETE） |
| FR-30 `queue_position` 只对 QUEUED 有效 | ✅ | 不是排队中的任务返回 0 |

## 6.6 结果导出

| 需求 | 状态 | 说明 |
| --- | --- | --- |
| FR-31 `format=json` / `format=zip` | ✅ | `GET /api/tasks/{id}/export` |
| FR-32 导出内容 | ✅ | 基本信息、最终状态、最终回答、生成物清单、交互记录（事件流）、错误信息 |
| FR-33 ZIP 目录 | ✅ | `manifest.json`、`final_answer.md`、`conversation.jsonl`、`events.jsonl`、`error.json`、`artifacts/` |
| FR-34 生成物单独下载（鉴权 + 有效期） | ⚠️ | 导出包里带文件；**单独下载**走 agent 的 `/download`（无鉴权、无有效期） |
| FR-35 大结果异步导出 | ❌ | 同步 |
| FR-36 未完成任务可导出当前快照 | ✅ | 导出现在这一刻的状态（`manifest.status` 如实写着） |

## 6.7 任务控制

| 需求 | 状态 | 说明 |
| --- | --- | --- |
| FR-37 取消 QUEUED / WAITING | ✅ | `POST .../cancel` |
| FR-38 取消 RUNNING（协作式停止） | ✅ | 停掉那一轮的 session，事件回来后置 cancelled |
| FR-39 失败任务重试（新 attempt，保留历史） | ✅ | `POST .../retry`：`attempt + 1`，事件与流水留着 |
| FR-40 终态默认不可修改 | ✅ | 终态不能取消（409）、不能重试（409） |

## 6.8 事件、审计与可观测

| 需求 | 状态 | 说明 |
| --- | --- | --- |
| FR-41 状态变更写入事件、不可改 | ✅ | 每个任务一个 `events.jsonl`，追加写；实时广播之外**有历史** |
| FR-42 审计日志 | ✅ | `audit.log`：提交、回答、取消、重试、导出都记；`GET /api/audit` 可查 |
| FR-43 指标 | ✅ | `/api/health` 的 `metrics`：队列长度、运行数、等待交互数、平均排队时长、平均任务时长、失败率 |
| FR-44 request_id / trace_id | ❌ | 还没有 |

## 第 8 章接口规范

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 路径 `/v1/agent-tasks` | ❌ | 现在是 `/api/tasks`（中间没有版本段） |
| 统一响应信封 `{code, message, request_id, data}` | ❌ | 现在直接返回对象，错误是 `{"error": {...}}` |
| 鉴权 `Authorization: Bearer` | ⚠️ | **权限按项目**：每个项目（工作区）一把钥匙，`Authorization: Bearer <key>`（或 `X-Project-Key`）带着它就**只能**碰这个项目的任务、事件、导出与排队；别人的 → 403。**不带钥匙的调用者是本机操作者**（网页就是它），因为服务只监听 127.0.0.1 |
| 租户（`X-Tenant-Id`） | ⚠️ | 没有租户概念，**项目**就是这里的隔离单位（按需求第 13 章的答复：权限按项目） |
| HTTPS | ❌ | 本机 HTTP |
| 幂等键 | ✅ | 提交与回答 |
| ISO 8601 UTC | ✅ | |

## 第 9 章数据模型

`agent_task` ⚠️（字段见 FR-13）｜`task_event` ✅（落盘）｜`task_message` ❌（在 agent 侧）｜
`task_interaction` ⚠️（有内容，缺 ID/超时/状态）｜`task_checkpoint` ❌｜
`task_artifact` ⚠️（有元数据，缺独立 ID/校验和/有效期）｜`export_job` ❌（同步导出）｜`audit_log` ✅

## 第 10 章非功能

可靠性 ⚠️（状态不丢；运行中的任务重启后标记失败，不恢复）｜幂等 ✅（提交、回答）｜
并发安全 ✅（记录写入有锁，队列串行）｜性能 ✅（提交毫秒级，调度 ≤2s）｜
安全 ❌（无鉴权/无租户/无脱敏，记录里有 prompt 全文）｜可观测 ✅（指标 + 事件 + 审计）｜
审计 ✅｜保留策略 ❌（记录不会自动清理）｜限流 ❌

## 已经定了的两条（需求第 13 章的待确认问题）

- **串行粒度 = Agent 队列 = 项目（工作区）**：同一个项目一个时刻一个任务，不同项目并行。
  队列里的「Agent」在这里就是工作区——任务在哪个工作区做，就归哪个项目的队列管。
- **权限 = 按项目**：每个项目一把钥匙（`Authorization: Bearer <key>`），带着它就只看得见、
  动得了这个项目的东西；别人的任务 → 403。钥匙由本机操作者用
  `POST /api/projects {"workspace_id": …}` 领取；**带钥匙的人不能再领别人的钥匙**。
  微信 bot 用的是它自己工作区的那把钥匙，所以它只在自己的项目里活动。

## 还没做的（按建议顺序）

1. **`/v1/agent-tasks` 路由 + 统一响应信封**（FR 8.1/8.2）——接口层的改造，机械但不小，
   要改所有调用方（含微信 bot 和网页）
2. **检查点与重启恢复**（FR-15）——需要 agent 侧能保存/恢复一轮的状态
3. **异步导出与生成物的鉴权下载**（FR-34/35）——现在导出是同步的，文件在导出包里
4. **交互超时与 interaction_id**（FR-20/24）
5. **request_id / trace_id、限流、保留策略**（FR-44、10）
6. **密钥与租户的进一步隔离**（FR 8.1/10）——现在是「项目 = 钥匙」，没有真正的租户；
   操作者身份（谁在用）也还没有

剩下的这些里，只有第 1 条会动到所有调用方，其余都是加接口。
