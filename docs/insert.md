# 两段式插话：什么时候让新消息生效

> 「这一轮还在跑，我又说了一句 —— 它该什么时候听？」

这一页是**设计**：2026-11-13 定稿（§9 是拍板结果），**当天已实现**（§12 记了落地时与
设计的出入）。它回答三件事：语义是什么、数据怎么走、哪些地方要改。

---

## 一、今天是什么行为

一轮 agent 会跑很久（长工具、多轮迭代、思考模型）。这段时间里输入框还能用，
按 Enter 不会开第二轮，而是：

```
POST /api/chat/queue  ──▶  ChatTurn::inbox（挂在**这一轮**身上）
                             │
                             ▼  agent loop 每次迭代开头取走最早的一条
                        作为一条普通 user 消息追加进会话
```

也就是 `src/prompt_queue.h` 里写的那句：「The agent loop adopts exactly one queued prompt per
iteration」。今天只有**一种**时机：**下一轮迭代**。

问题有两个：

1. **它会插到正在干活的这一轮中间**。用户想说的可能是「先别动那个文件」「换个方向」，
   而模型是在旧指令下决定的下一个工具调用 —— 插话晚了半拍。
2. **它随轮次生死**。轮次结束（停止 / 报错 / 迭代上限）时队列里的东西被退回给调用方
   （`queue_unconsumed`），谁也没接着跑它们。

用户要的是**两段式**：

| | 时机 | 表现 |
| --- | --- | --- |
| **默认** | **等上一轮会话完成后才生效** | 这一轮照常跑完；新消息成为**下一轮**的第一条 user 消息 |
| **可要求** | **立即插入** | 插到正在跑的这一轮里，**下一轮工具调用之前**生效 |

---

## 二、语义（先把话说死）

### 2.1 「等这轮结束」（默认，`insert=after`）

```
用户提交 ──▶ 进入会话的插话队列（不进入正在跑的那一轮）
                     │
   这一轮跑完 ───────┤  交接：队列里最早的一条 → 开**新一轮**
                     ▼
              新一轮：那条消息就是它的 user 消息
```

- 这一轮**完全不受影响**：它的工具、它的答案、它的 stop_reason 都不变。
- 交接发生在轮次**自己结束**之后（正常答完 / 到预算 / 报错）。**用户主动叫停不算**
  —— 叫停 = 这一轮连同它排队的都停，消息退回输入框（见 §6）。
- 队列里有多条时，**一条一条来**：第一条开新一轮，那一轮结束时再开下一轮。

### 2.2 「立即插入」（`insert=now`）

```
用户提交 ──▶ 进入会话的插话队列（标着「立即」）
                     │
   agent loop 下一轮迭代开头 ──▶ 取走最早的一条「立即」消息
                     │            作为 user 消息追加进会话
                     ▼
              下一次模型请求就带着它 ──▶ 模型据此决定下一轮工具调用
```

生效点的精确说法：

> **「立即插入」保证的是：这条消息在下一次模型请求之前进入上下文，
> 因此模型决定的下一轮工具调用已经知道它。**

它**不**做的事（要写进文档，别让人误会）：

- **不打断正在执行的工具**。一个跑了 40 秒的 `toolkit_exec` 会跑完。
  插话在它之后生效。
- **不撤销模型已经决定的工具调用**。同一轮里模型一次要了 3 个工具，用户在第 1 个
  执行时插话，第 2、3 个照跑（它们是旧指令下决定的）。要「连本轮剩下的都别跑」
  是另一种语义，见 §9 的待确认项。
- **第一轮例外**：轮次刚开始时排的「立即」消息，也要等到**第二次**模型请求才生效
  （今天的循环就是从第二次迭代开始采纳的）。第一轮的工具调用是模型在消息到达之前
  就决定好的 —— 这不是插话迟到，是它还没到。

### 2.3 两种时机什么时候用

| 用户想说的话 | 用哪种 |
| --- | --- |
| 「顺便把日志级别改成 debug」「等这轮完了接着看第二件事」 | 默认（等这轮结束） |
| 「先别动那个文件」「停，改用另一个方案」「刚才那条命令是错的」 | 立即插入 |

---

## 三、数据模型

### 3.1 插话队列（会话级，不是轮次级）

今天的队列挂在 `ChatTurn` 上（`src/prompt_queue.h`），**随轮次生死**。新设计里它挂在
**会话**上，因为「等这轮结束」的消息必须活过这一轮。

```cpp
// src/insert_queue.h（替掉 src/prompt_queue.h）
enum class InsertMode { kAfter = 0, kNow = 1 };   // 等这轮结束 / 立即插入

struct QueuedPrompt {
  std::string id;            // q_xxx
  std::string text;
  std::string created_at;
  InsertMode mode = InsertMode::kAfter;
};

class InsertQueue {          // 线程安全的 FIFO（今天的 PromptInbox 是同一套东西）
 public:
  void push(const QueuedPrompt&);
  /// 取走最早的一条「立即插入」—— agent loop 用它。
  bool pop_now(QueuedPrompt* out);
  bool has_now() const;
  /// 取走最早的一条（不管模式）—— 开新一轮时用。
  bool pop_next(QueuedPrompt* out);
  bool remove(const std::string& id);            // 取消一条
  std::vector<QueuedPrompt> snapshot() const;    // GET /api/chat/queue
  std::vector<QueuedPrompt> drain();             // 全部退回
  std::size_t size() const;
  bool empty() const;
};
```

服务端持有：`std::map<std::string, std::shared_ptr<InsertQueue>> session_queues;`（跟 `mu` 一把锁）。
每个正在跑的轮次持有一份 `shared_ptr`（指向它自己会话的那个队列），这样：

- 轮次结束时队列**还在**（交接要用）；
- 队列空了且没有轮次在跑 → 删掉这条记录（不漏内存）；
- 同一个会话的两个窗口看到的是**同一个**队列（今天的队列也是这个效果）。

### 3.2 一轮要跑的东西（`TurnSpec`）

交接要开新一轮，新一轮得知道「上一轮是怎么配的」。所以把一轮的参数打包：

```cpp
struct TurnSpec {
  std::string workspace_id, session_id, message, request_id;
  std::vector<ImageAttachment> images;
  std::string provider, model, system_prompt;
  std::vector<std::string> tools;  bool tools_given = false;
  std::shared_ptr<const Role> role;
  std::string task_id, task_context;
  std::vector<std::string> allow_paths;
  ExecPolicy exec_policy;
  nlohmann::json knob_overrides;
  TurnBudget budget;
  std::string queue_id;      // 这一轮是从哪条排队消息起来的（空 = 用户直接发的）
};
```

`ChatTurn` 存一份 `std::shared_ptr<const TurnSpec>`。交接 = 复制一份，把 `message` 换成
排队消息、`queue_id` 换成那条消息的 id。

**角色例外**：交接的那一轮会**重新解析角色**（`role_for_turn`）。角色是「下一轮生效」的语义，
而交接出来的正是下一轮 —— 用户在上一轮跑的时候切了角色，理应作用在这一轮。

---

## 四、模块与改动

| 文件 | 改什么 |
| --- | --- |
| `src/insert_queue.h`（新，替 `src/prompt_queue.h`） | 上面的 `InsertMode` / `QueuedPrompt` / `InsertQueue` |
| `src/agent.h` | `TurnOptions::inbox` → `TurnOptions::queue`（`InsertQueue*`） |
| `src/agent.cpp` | ① 采纳只取「立即」条目（`pop_now`）；② 「模型答完了但队列里还有立即消息 → 续上这一轮」的判断改用 `has_now()`；③ 轮次结尾**不再** drain 整个队列（那是服务端的事） |
| `src/server.cpp` | ① 会话级队列 + `TurnSpec` + `launch_turn()`；② `/api/chat` 与 `/api/chat/queue` 接受 `insert`；③ worker 尾巴做**交接**（开新一轮）或**退回**（用户叫停）；④ `queue_adopted` / `queue_queued` / `queue_unconsumed` / `turn_handover` 事件 |
| `web/index.html` | 输入框旁的**插话方式**指示器（§7） |
| `web/app.js` | 指示器状态、Alt+Enter、排队条上的时机标签、交接事件 → 自动接上新一轮 |
| `web/app.css` | 指示器与标签的样式（深色模式用 `:root` 变量） |
| `CMakeLists.txt` | 头文件改名 |
| `docs/api.md` / `docs/tea_a2_manual.md` | 排队那节改写（§8 的接口表） |
| `tea_a2_agent.md` | 开发笔记：为什么这么设计、踩过的坑 |

**服务端要做的一件大手术**：把 `/api/chat` 里「开一轮」的那段（建 `ChatTurn`、装 session、
登记 `turns`、起 worker 线程）抽成 `launch_turn(const TurnSpec&, std::string* err)`。
HTTP 处理器和交接路径都走它。**参数解析**（provider/model/tools/limits/policy…）留在处理器里，
不动 —— 那是调用方的事。

---

## 五、三个流程

### 5.1 默认：等这轮结束

```
浏览器                     服务端                        agent loop
  │ POST /api/chat {insert:"after"}                          │
  ├──────────────────────▶ 会话队列.push(mode=after)          │
  │◀── 200 {queued:{id,insert:"after"}, pending:1} ──────────┤
  │                                                             │
  │                                                          这一轮继续跑
  │                                                          （不看这条消息）
  │                                                             │
  │                                        轮次结束（done）    │
  │                                        worker 尾巴：       │
  │                                          queue.pop_next()  │
  │                                          launch_turn(新消息)│
  │◀── SSE: turn_handover {next_turn_id, queue_id} ───────────┤
  │                                                          新一轮开始
  │ POST /api/attach {session_id}                             │
  ├──────────────────────▶ 跟新一轮 ──────────────────────────▶
```

### 5.2 立即插入

```
浏览器                     服务端                        agent loop
  │ POST /api/chat {insert:"now"}                             │
  ├──────────────────────▶ 会话队列.push(mode=now)             │
  │◀── 200 {queued:{id,insert:"now"}, pending:1} ─────────────┤
  │                                                             │
  │                                        下一轮迭代开头：     │
  │                                          queue.pop_now()   │
  │                                          追加成 user 消息   │
  │◀── SSE: queue_adopted {id, iteration} ────────────────────┤
  │                                        下一次模型请求带着它 │
  │                                        模型据此决定下一轮工具│
```

### 5.3 用户叫停

```
浏览器                     服务端
  │ POST /api/chat/stop {session_id}                         │
  ├──────────────────────▶ cancel=true → 这一轮 stopped       │
  │                          worker 尾巴：                     │
  │                            队列里的条目全部退回             │
  │◀── SSE: queue_unconsumed {items:[…]} ────────────────────┤
  │ 放回输入框（今天就是这么做的，行为不变）                    │
```

---

## 六、边界与失败场景

| 场景 | 行为 | 理由 |
| --- | --- | --- |
| 插话时正在跑一个长工具 | 不打断那个工具；「立即插入」在它结束后生效 | 打断工具 = 半截副作用（写了一半的文件、跑了一半的部署） |
| 模型已经给出最终答案 | 「立即插入」把这一轮**续上**（今天就有这个行为）；「等这轮结束」开新一轮 | 已经答完了还要插话，说明用户有新的要求 |
| 用户按停止 / `interrupt` | 排队的消息**退回**（`queue_unconsumed`），不自动开新一轮 | 叫停 = 「什么都别自动开始」；字还给用户，不丢 |
| 这一轮报错 / 到预算 | 轮次算结束 → 排队的照常开新一轮 | 「上一轮结束了」就是结束了（已拍板） |
| 交接时活跃轮次达上限（`TEA_AGENT_MAX_ACTIVE_TURNS`） | 交接失败 → 那条消息**放回队列**，记日志 + `queue_dropped` 事件 | 不能因为开不了新一轮就把用户的字吞了 |
| 会话被删了 | 队列里的消息作废（记日志） | 没有会话就没有下一轮 |
| 服务重启 | 队列在内存里 → **丢** | 已知限制（见 §9.4 的持久化选项） |
| 排队消息带图片 | 仍然拒绝（`images_not_queued`） | 图片要落盘、校验、编码，排队路径没有这套 |
| 同一个会话两个窗口同时排队 | 按到达顺序 FIFO | 队列本来就是 FIFO |
| 排队消息多于 10 条 | 不限；UI 全列（超过一屏就滚动） | 不人为设限 |
| `insert` 字段写错（不是 after/now） | `400 bad_request` | 拼错的参数不能当默认值（项目的老规矩） |
| 队列里没有轮次在跑时调用 `/api/chat/queue` | **直接开一轮**（今天返回 `409 no_active_turn`） | 消除竞态：轮次恰好在这一刻结束，消息不能丢 |

---

## 七、前端：那个「指示」

用户要的是「**增加一个指示**，用户可以要求立即插入」。所以输入框旁边要有一个**看得见、
点得动**的东西：

```
┌──────────────────────────────────────────────┬──────────┐
│ 排队条：  ⏳ 等这轮结束：把日志级别改成 debug  │          │
│           ⚡ 立即插入：先别动那个文件      ×   │          │
├──────────────────────────────────────────────┴──────────┤
│ 输入…                                                  │  ⏎  │
└──────────────────────────────────────────────────────────┘
   [ 插话：等这轮结束 ▾ ]                        [ 发送 ]
```

- **指示器**：一个 chip 按钮，两态：`插话：等这轮结束`（默认）/ `插话：立即插入`。
  点一下切换，状态记在 `localStorage`（每个浏览器各记各的）。
  空闲时（没有轮次在跑）它变灰 —— 这时候没有「插话」可言。
- **快捷键**：`Alt+Enter` = **立即插入**（不管 chip 现在是什么）；`Enter` = 按 chip 的
  当前时机；`Shift+Enter` = 换行（不变）。
- **排队条**：每条消息带一个时机标签 —— ⏳「等这轮结束」/ ⚡「立即插入」。
  标签告诉用户「这条什么时候会生效」，这正是「指示」的意义。
- **交接**：`turn_handover` 事件到达 → toast「上一轮结束，排队消息开始新一轮」→
  自动 `POST /api/chat/attach` 跟到新一轮（`maybeAttach(sid)` 已经有这个能力）。
- 空闲时的输入框提示语不变；生成中改成：
  「会话进行中：Enter 排队（这轮结束后开始），Alt+Enter 立即插入」。

---

## 八、接口

### 8.1 HTTP

**`POST /api/chat`** —— 新增可选字段 `insert`：

```jsonc
// 等这轮结束（默认，不写 insert 也是这个）
{"workspace_id":"ws_x","session_id":"s_x","message":"顺便把日志级别改成 debug"}

// 立即插入
{"workspace_id":"ws_x","session_id":"s_x","message":"先别动那个文件","insert":"now"}
```

- `insert`：`"after"`（默认）| `"now"`。别的值 → `400 bad_request`。
- 只有**这个会话正在跑一轮**时它才有意义。没有轮次在跑 → 照旧开一轮，`insert` 被忽略。
- 排队时的回答（形状和今天一样，多一个 `insert`）：

```json
{"ok":true,
 "queued":{"id":"q_x","content":"…","created_at":"…","insert":"after"},
 "pending":1,
 "session_id":"s_x","workspace_id":"ws_x",
 "turn_running":true,"turn_id":"t_x","state":"running",
 "status_url":"/api/turns/t_x","events_url":"/api/turns/t_x/events"}
```

**`POST /api/chat/queue`** —— 同样接受 `insert`。

- 今天：没有轮次在跑 → `409 no_active_turn`。
- 新：**直接开一轮**（消掉「轮次恰好在这一刻结束、消息被吞」的竞态，已拍板）：

```json
{"ok":true,
 "queued":{"id":"q_x","content":"…","created_at":"…","insert":"after"},
 "pending":0,
 "session_id":"s_x","workspace_id":"ws_x",
 "turn_running":true,"turn_id":"t_new","state":"running",
 "status_url":"/api/turns/t_new","events_url":"/api/turns/t_new/events"}
```

  这一轮是**从这条排队消息起来的**（`start.queue_id = "q_x"`），客户端可以据此把
  排队条上的那一条划掉，再 `POST /api/chat/attach` 跟上去看结果。
- `session_id` 指向的会话不存在 → `404 not_found`（这个接口是**按会话**排队的，
  会话都没有就没有下一轮；这种情况请直接 `POST /api/chat`）。
- 排队消息仍然是**只收文本**（带图片 → `400 images_not_queued`）。

**`GET /api/chat/queue?session_id=`** —— 列出会话队列里的条目，每条带 `insert`：

```json
{"ok":true,"pending":2,
 "items":[{"id":"q_x","content":"…","created_at":"…","insert":"after"},
          {"id":"q_y","content":"…","created_at":"…","insert":"now"}]}
```

**`POST /api/chat/queue/cancel`** —— 语义不变（带 `id` 取消一条，不带取消全部），
但取消的是**会话队列**里的条目（包括还没生效的「等这轮结束」那些）。

### 8.2 SSE 事件

| `type` | 什么时候 | 字段 |
| --- | --- | --- |
| `queue_queued` | 有新条目进了队列（本轮正在跑时广播给所有跟随者） | `id` `content` `insert` `pending` |
| `queue_adopted` | 「立即插入」被正在跑的这一轮采纳 | `id` `content` `iteration` `pending` `insert:"now"` |
| `queue_unconsumed` | 用户叫停，条目被退回 | `items[]`（`id` `content` `created_at` `insert`） |
| `queue_dropped` | 交接开不了新一轮（活跃轮次到顶），条目放回队列 | `id` `reason` |
| `turn_handover` | 这一轮结束，队列里的下一条开新一轮 | `next_turn_id` `queue_id` `remaining` `session_id` |
| `start` | 新的一轮开始 | 多一个 `queue_id`（这一轮是从哪条排队消息起来的；直接发的是空） |

`done` / `stopped` / `error` 的形状不变。

---

## 九、已经拍板的事（2026-11-13）

| # | 问题 | 决定 | 后果 |
| --- | --- | --- | --- |
| 1 | 这一轮报错 / 到预算时，排队的还要不要自动开新一轮？ | **要** | 「轮次自己结束」一律交接。只有**用户叫停**才退回 |
| 2 | 「立即插入」要不要连本轮剩下的工具调用一起跳过？ | **不跳** | 已决定的调用照跑；不引入「补假结果」那套机制，噪音小 |
| 3 | 默认时机要不要全局可配？ | **不配** | 默认就是「等这轮结束」；要立即就点一下 chip。没有 `settings.insert_default` |
| 4 | 插话队列要不要落盘？ | **不落盘** | 队列在内存里，**服务重启即丢**（已知限制，§6 那行写着） |
| 5 | `/api/chat/queue` 没有轮次时改成「直接开一轮」？ | **可以** | 接口语义变更：不再返回 `409 no_active_turn`（§8.1 写了新形状） |

由此定下的**总规则**（实现时照这条走）：

> **轮次自己结束（答完 / 到预算 / 报错）→ 交接：排队的消息开新一轮。
> 用户主动叫停（`/api/chat/stop`、`interrupt`）→ 退回：消息还回输入框，什么都不自动开始。**

## 十、影响范围

### 10.1 行为变更（对调用方可见）

| 变更 | 谁会受影响 |
| --- | --- |
| 同会话的默认排队：从「下一轮迭代采纳」变成「这一轮结束后开新一轮」 | 任何用 `session_id` 复用会话调 `/api/chat` 的客户端 |
| `/api/chat/queue` 没有轮次时不再返回 409，而是开一轮 | 同上 |
| 队列从轮次级变成会话级（能活过一轮） | 依赖「队列随轮次销毁」的调用方 |

**逐个确认过的调用方**：

- 内置 Web UI —— 主要目标，要改。
- `sched/task_server.py` —— 每个任务一个会话，不会撞上；**不受影响**。
- `sched/wechat_bot.py` / `telegram_bot.py` —— 走触发器 → 任务队列 → 每条消息一个会话；**不受影响**。
- `sched/fleet.py` —— 不带 `session_id`；**不受影响**。
- `sched/a2_ext_py/client.py` —— `chat()` 只读 SSE；服务端回 JSON（排队）时它读不到事件。
  这是**今天就有的**毛病，但新的默认让排队更容易发生 → 建议顺手修（收到 JSON 就当成「已排队」返回）。

### 10.2 测试

| 测试 | 改什么 |
| --- | --- |
| `tests/smoke_prompt_queue.py` | 大改：现在的断言是「下一轮迭代采纳」→ 拆成两组：`insert:"now"` 仍然下一轮采纳；`insert:"after"` 这一轮结束后开新一轮 |
| `tests/smoke_multi_session.py` | 「同会话默认排队」那组：断言从「running turn adopts」改成「新一轮接手」 |
| 新增 `tests/ui_insert_mode.mjs` | 浏览器：指示器两态、Alt+Enter、排队条的时机标签、交接后自动跟到新一轮 |
| `tests/smoke_multi_session.py` 的 409 那条 | `/api/chat/queue` 无轮次时的行为变了 |

**回归基线**：本机（WSL2）有 8 个测试本来就是红的（环境问题，不是回归）——
见 `agent.md` 的「本机测试基线的红」。改完要对着基线逐项比。

### 10.3 文档

- `docs/api.md`：排队那节整节改写（「插队」→「插话：两种时机」），加 `insert` 字段、
  新事件、新错误码。
- `docs/tea_a2_manual.md`：curl 示例加 `insert`。
- `tea_a2_agent.md`：开发笔记（为什么默认是「等这轮结束」、为什么交接用新轮次而不是同一轮续跑、
  踩过的坑）。
- `README.md`：功能表里加一行（可选）。

---

## 十一、实施顺序

| 步 | 做什么 | 验收 |
| --- | --- | --- |
| 1 | `src/insert_queue.h`：模式字段 + `pop_now` / `has_now` / `pop_next`；`agent.cpp` 只采纳「立即」 | 现有 smoke_prompt_queue 加 `insert:"now"` 后仍然全绿 |
| 2 | 服务端：会话级队列 + `insert` 参数 + `/api/chat/queue` 的新行为 | 新 smoke：`after` 不进这一轮；`now` 进这一轮 |
| 3 | `TurnSpec` + `launch_turn()` 抽取 + worker 尾巴的**交接** | 新 smoke：这一轮结束后新一轮自动开，消息在里面 |
| 4 | 事件：`queue_queued` / `turn_handover` / `queue_dropped` / `start.queue_id` | 新 smoke：事件顺序对 |
| 5 | 叫停时的退回（`queue_unconsumed` 带模式） | 停止后输入框里能拿回那些字 |
| 6 | 前端：指示器 + Alt+Enter + 排队条标签 + 交接自动接上 | `tests/ui_insert_mode.mjs` |
| 7 | 文档 + `smoke_multi_session.py` / `smoke_prompt_queue.py` 收尾 | `bash scripts/test-all.sh` 与基线一致 |

**回滚**：第 1–5 步都在服务端，Web UI 不改也能用（老的 UI 发的消息没有 `insert` 字段 =
默认「等这轮结束」= 新行为）。真要回滚，`git revert` 即可 —— 没有数据格式要迁移
（队列在内存里）。

**交接的顺序（别搞反，race 都在这儿）**：

1. `run_turn` 返回（`done` 已发出，会话**已落盘**）；
2. `begin_shutdown()`；把没被采纳的「立即」条目**降级**成「等这轮结束」
   （不是退回 —— 除非是用户叫停，见 §5.3）；
3. 从会话队列 `pop_next()` 取出下一条；
4. **把旧轮次从 `turns[session_id]` 里摘掉**（它的流还开着，但已经不占位子了）——
   不摘的话 `launch_turn()` 会以为「有人正在跑，要顶掉它」，发出那条警告并取消它；
5. `launch_turn()` 开新一轮（它从**磁盘**上读会话：旧 worker 手里那份是它自己的拷贝，
   这时磁盘上的才是事实）；
6. 在**旧**轮次的流上发 `turn_handover`（带上新轮次的 id）；
7. `close()` 旧轮次 → 旧流结束。

**只有用户叫停才退回**：其它结束方式（答完 / 到预算 / 报错）一律走交接。
「立即」条目在轮次结束时没被采纳 → 降级成「等这轮结束」，跟着下一轮跑掉。

---

## 十二、落地（2026-11-13）

实现完了，与设计相比有这些出入：

| 地方 | 设计 | 落地 |
| --- | --- | --- |
| `Alt+Enter` | 按 chip 的模式 | **强制「立即插入」**（chip 是默认值，Alt+Enter 是「话急」的快捷键——两个键做同一件事没意义） |
| chip 的文案 | `插话：等这轮结束` / `插话：立即插入` | `⏳ 等这轮结束` / `⚡ 立即插入`（窄屏放得下） |
| 队列的存放 | `session_queues[session_id]` | 一样；另外多了一个 `session_specs[session_id]`（这个会话**上一轮**的参数）——没有轮次在跑时，`/api/chat/queue` 要自己开一轮，就得知道用哪个工作区、哪个 provider |
| 收尾的兜底 | — | 多了一步 `start_queued_turn_if_idle()`：轮次收尾那一瞬间挤进来的消息没人跑（队列在内存里，没人跑 = 字丢了） |
| 错误目录 | — | 顺手补了 `already_exists` / `shutting_down` 两条（`smoke_error_catalog.py` 是扫源码的，新加的错误码不登记就会红） |

实现时踩到的两个坑（写在这里，免得下一个人再踩）：

- **队列对象不能在还有轮次在跑的时候从 `session_queues` 里删掉**：轮次从它那里采纳消息，
  换成另一个对象，那条「立即」消息就没人看得见了。
- **交接的两把锁有顺序**：`handover_mu` 在 `mu` 之外拿（反过来死锁）。两个交接同时来会
  开两轮跑同一个会话。

怎么验：

```bash
python3 tests/smoke_insert_queue.py    # 服务端：两种时机、取消、叫停退回、无轮次时自己开一轮
node tests/ui_insert_mode.mjs           # 浏览器：指示器、Alt+Enter、排队条的时机标签、交接后自动跟上
```
