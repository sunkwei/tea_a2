# 多 agent 并发：一个大任务拆成并行子任务

> 「这个项目要同时查三件事」——一个工作区里的大任务，由**模型**分解成若干子任务，
> 只读的那几个**真并行**，跑完由**模型**汇总成一份交付物。

- 形态：**a2 层 + MCP 扩展**，**不走 a2_task**
- 改动面：一处工具层 C++（只读工具加正则）+ 两个新文件（规划内核 + 扩展模块）
  + 客户端三个小方法；a2 内核（agent loop / 回合句柄 / 插话 / 隧道）**一行不动**
- 回归：`tests/smoke_plan_orchestrator.py`（37 项）、`tests/smoke_plan_parallel.py`（19 项）、
  `tests/smoke_planning_rules.py`（28 项，纯函数）、`tests/smoke_workspace_search.py`（41 项）、
  `tests/ui_parallel_turns.mjs`（12 项）

---

## 一、这个功能长什么样

```
用户（聊天里）：「把这事拆开并行做」
   │
   ├─ 模型调用 mcp__a2ext__toolkit_plan(op="start", goal=…)
   │     ① 分解：POST /api/llm/chat → 模型出计划（每个子任务标 mode: ro/rw）
   │     ② 校验：mode 枚举 / 写路径不重叠 / 依赖无环
   │     ③ 起 N 个 ro 子任务：POST /api/chat {async:true, role:"readonly", limits}
   │     ④ **立即返回** 计划号 + 计划摘要（秒级，不等结果）
   │
   ├─ 后台：子任务并发跑 → 收结果 → 模型汇总（POST /api/llm/chat）→ 落盘
   │
   └─ 模型调用 mcp__a2ext__toolkit_plan(op="status", plan_id=…)
         → 拿到汇总（带来源标注）→ **模型自己讲给用户**
```

**两段式是硬要求**：MCP 工具调用**无法被 a2 取消**（`src/mcp.cpp:854` 只有超时，
没有 `notifications/cancelled`）。若把编排做成一个长阻塞调用，用户 stop 那一轮或
工具超时，都会留下一批**没人管、还在烧 token 的孤儿轮次**。

---

## 二、文件都在哪

| 文件 | 是什么 |
| --- | --- |
| `sched/planning.py` | **规划内核**：`make_plan()`（模型分解 + 校验）、`aggregate()`（模型汇总）、计划的落盘与读取、只读角色的 YAML |
| `sched/a2_ext_py/modules/toolkit_plan.py` | **扩展模块**（复制到 `~/.tea_agent/ext/`）：两段式工具、闸门、调度器、回收器 |
| `sched/a2_ext_py/client.py` | 客户端多了三个方法：`turn_trajectory()`、`roles()`、`put_role()`；`submit()`/`chat()` 多了 `role` 参数 |
| `src/workspace.{h,cpp}`、`src/tools.cpp`、`src/server.cpp` | 只读工具的**正则搜索**（`regex=1` / `regex=true`） |

---

## 三、装上它

```yaml
# ~/.tea_agent/mcp.yaml（每台要跑编排的机器）
servers:
  a2ext:
    command: "python3"
    args: ["/path/to/tea_a2/sched/a2_ext_py/toolkit/mcp_server.py",
           "--modules", "/home/you/.tea_agent/ext",
           "--agent-url", "http://127.0.0.1:8282"]
    env:
      TEA_AGENT_URL: "http://127.0.0.1:8282"   # 模块要自己找 agent
      TEA_HOME: "/home/you/.tea_agent"          # 计划落在 <TEA_HOME>/plans
    timeout_ms: 120000
```

```bash
cp sched/a2_ext_py/modules/toolkit_plan.py ~/.tea_agent/ext/
# 建议加 --no-save（或 TEA_EXT_SAVE=0）：模型不能自己写工具
# 生效：POST /api/mcp/refresh
```

`TEA_A2_SCHED` 可以显式指向源码里的 `sched/` 目录——模块靠它找到规划内核
（否则它从 MCP 服务器自己的路径推）。

> **模块与内核要配套**：`toolkit_plan.py` 用规划内核里的函数
> （`role_is_read_only()` / `result_text()` / `aggregate_prompt()`），两边要同一个版本。
> 升级时别只拷模块：`TEA_A2_SCHED` 指着一棵旧源码树，工具会在加载时直接报
> `planning has no attribute …`。

> **设备上没有 Python**（既定：设备上只放一个 a2 二进制）→ 设备上的 a2 **不能自带
> 编排**。要用编排，从 PC / 手机上的 a2 经隧道驱动它。

---

## 四、模型怎么用它

```jsonc
// 开始
{"op": "start", "goal": "查清楚这个项目里的鉴权是怎么做的"}
// 看进展
{"op": "status", "plan_id": "plan_1a2b3c4d5e6f"}
// 只看一个子任务
{"op": "status", "plan_id": "plan_1a2b3c4d5e6f", "subtask": "s1"}
```

`op="start"` 的回答长这样：

```
编排已经开始了（计划 plan_1a2b3c4d5e6f，工作区：tea_a2 (/home/you/tea_a2)）：
  - s1    [ro] 查一下鉴权中间件
  - s2    [ro] 找找配置项

子任务在后台跑。用 toolkit_plan(op="status", plan_id="plan_1a2b3c4d5e6f") 看进展；
都跑完之后那里会有汇总。
```

**回答里写明在哪个工作区**（名字 + 路径）。不给 `workspace_id` 时用的是第一个工作区——
而一台机器上往往不止一个，静悄悄跑到别的项目里去改文件是最贵的那种错。机器上有多个工作区时，
回答里还会提一句「默认用第一个，要换就传 `workspace_id`」；传了一个不存在的工作区 → 直接报错，
不会悄悄换成第一个。`op="status"` 也会把工作区打出来。

---

## 五、六条规则

### R1 只读的执行

ro 子任务 = 角色 `readonly`（`sched/planning.py` 里的 `READ_ONLY_ROLE_YAML`，
第一次用时装进工作区的 `roles/`）：

- `read_only: true` → 文件工具的写/建/删/移**被机制层拒绝**
  （`src/tools.cpp:424`；`src/agent.cpp:1823` 把角色的 `read_only` 传进工具上下文）
- 工具白名单：`toolkit_file`（读/列）、`toolkit_workspace_search`（搜源码）、
  `toolkit_history_search`、`toolkit_search` / `toolkit_fetch`（搜网络）、
  `toolkit_load_image`、`toolkit_todo`、`toolkit_task`
- **不含** `toolkit_exec`（会改东西）、`toolkit_memory` / `toolkit_agent_doc`
  （写的是**共享**状态，多个子任务并发会互相覆盖）、`mcp__a2ext__toolkit_plan`
  （只读不该有「发起并发」的权力）

最后一条**不用额外代码**：角色的 `tools` 是白名单交集（`src/server.cpp:2906-2918`），
轮次没给 `tools` 时角色的列表直接成为白名单；MCP 工具也在工具表里 → 交集生效。

**角色要按内容校验，不是按名字**（`planning.role_is_read_only()`）：工作区里已经有一个
叫 `readonly` 的角色时，先看它是不是真的只读——`read_only` 为真、**有**工具清单
（a2 里空清单 = 全部工具）、且清单里没有 `toolkit_exec` / `toolkit_memory` /
`toolkit_agent_doc` / 编排工具本身。任何一条不满足就**拒绝使用，也不覆盖它**（操作者可能
是特意那么写的）。装不上或不合规时只读任务**不跑**：队列直接判失败
（`stop_reason: read_only_role_missing`），编排器连计划都不起——不会退化成「没有角色、
但记录里还写着 ro」。

### R2 并发规则

**ro↔ro 并行；rw 独占**（rw 与任何子任务不同时）。**依赖失败不会传染成等待**：
一个子任务等的那一步失败了（或者被停掉、到预算），它立刻被标成 `skipped`
（`它等的那一步没有完成`）并进汇总，而不是留在 `pending` 让调度器一直等它——
那会让循环空转到 deadline（默认预算是 300 秒 × 步数），白烧一个核，汇总里还看不出发生了什么。

依据：业务文件是**非原子写**（`src/platform_fs.cpp:773` 的 `write_file` 直接写目标
文件）→ rw 改到一半时，ro 可能读到**半截**；即使读到完整版也是「改之前的版本」
→ 结论建立在不一致的输入上。运行期状态不受影响（原子写 + 进程级锁
`src/state_lock.h:30`）。

调度器就是这么写的（`toolkit_plan.py` 的 `_run_plan`）：一轮里先起所有「等齐了」的
ro；rw 只有在**没有任何子任务在跑**时才起，且一次只起一个。

### R3 两段式

`toolkit_plan(op="start")` **秒回**（只起任务，不等结果）；`op="status"` 取结果。

### R4 汇总

一次模型调用。输入 = 目标 + 各子任务结果（按「【子任务名】」分段）；输出必须
**标注结论来自哪个子任务**，并写明**失败 / 未启动**的子任务。

### R5 闸门（扩展跑在 a2 的围栏之外 → 模型能自己制造并发与花钱）

1. **防抖**：同一工作区 60 秒内只允许起 1 个计划（失败的那次也算——它已经花掉了一次
   模型调用；拒绝的消息里会说还剩几秒）
2. **子任务数 ≤ 4**（工具默认上限 `max_subtasks`，内核上限是 6：**模型被要求直接按这个数
   拆**，`planning.make_plan(max_steps=…)`，不会先拆 6 个再被拒）、**并发 ≤ 4**
3. 每个子任务**强制 `limits`**，`max_wall_ms` **必填**（→ 孤儿自己会死）
4. 只读角色不得调用本工具
5. rw 子任务必须独占（R2）

### R6 回收

计划与子任务 `turn_id` 落盘（`<TEA_HOME>/plans/<plan_id>.json`，原子写）；扩展进程内的
回收线程每 60 秒扫一次 → 超时计划的**孤儿子轮次**用 `POST /api/turns/{id}/stop` 停掉。
回收线程**在模块加载时**就起（不是第一次 `op="start"` 才起）：否则扩展进程重启（a2 重启、
网页里刷新 MCP）之后，老计划既没有调度器也没有回收器，会永远停在「running」。

回收器写盘之前**重新读一遍计划**（同一把锁），只改它要改的那一格：它手上那份是几秒前的
快照，整份写回去会把调度器刚写好的结果盖掉。收尾也归它管：过了「自己的 deadline + 5 分钟」
还挂着 `running`、且没有子任务在跑的计划，标成 `interrupted` 并写明原因。

附带事实：**本地 a2 进程退出 → 本地子轮次一起死**（回合是进程内 worker 线程，
`src/server.cpp:3041`）；跨机编排时由对端负责。

---

## 六、怎么看见子 agent 的过程

每个子任务就是一个回合 → `turn_id` 就是它的句柄，三层现成可查（**零改动**）：

| 想看什么 | 接口 | 拿到什么 |
| --- | --- | --- |
| 状态与开销 | `GET /api/turns/{id}` | `state` / `iterations` / `usage` / `elapsed_ms` / `files_changed` / `stop_reason` |
| **过程直播** | `GET /api/turns/{id}/events?since=N` | **SSE：先回放再直播**，每帧带 `seq`；帧类型 `start` / `iteration_end` / `tool_call` / `tool_start` / `tool_result` / `content_delta` / `reasoning_delta` / `todo` / `budget` / `stopped` / `done` / `error` |
| 结构化审计 | `GET /api/turns/{id}/trajectory` | 完整轨迹（思考、工具调用、文件变更）；也落盘 `trajectories/<sid>.jsonl` |

浏览器直接打开 events URL 就是一块仪表盘（`curl -N` 同理）。子任务的句柄在
`<TEA_HOME>/plans/<plan_id>.json` 里（`subtasks.<key>.turn_id`），`op="status"`
也会把句柄和这个 URL 一起报出来。

**注意**：网页 UI 的轨迹是**按会话**取的（`web/app.js:803-826`），而子任务各自独立
会话 → 父会话的轨迹里**看不到**它们的过程。右下角的热区见 §十一（「并行中的轮次」）。

---

## 七、只读工具的正则搜索（顺带做的）

`toolkit_workspace_search` 与 `GET /api/workspaces/{id}/search` 现在收一个
`regex` 开关：

```jsonc
{ "query": "TODO|FIXME", "regex": true, "scope": "content" }
```

- **默认关闭**：不传 `regex` 时行为与从前**一字不差**（逐字节比较）
- 开了以后：查询串是 **ECMAScript 正则**，只匹配**文件内容**（路径不参与匹配）
- `exact=true` + `regex`：整行要匹配这个正则（而不是「行里含有」）
- 编译不过 → 明确报错（`invalid regular expression: …`），**不静默当成字面量**
- 有上限：正则式 ≤ 256 字符、单次搜索最多看 20 万行（`std::regex` 没有超时，
  病态表达式会把 agent 卡住）。⚠️ 这两条挡不住**单行很长**：上限是按**行数**算的，
  一行 100 KB 也只是「一行」。所以给准 `path=`、别写会回溯的式子（`(a+)+b`）——
  这是这个开关唯一的残余风险

---

## 八、需求对照表

| 编号 | 需求 | 状态 |
| --- | --- | --- |
| FR-01 | 工作区 = 项目；子任务各自独立会话 | ✅ 已有 |
| FR-02 | **模型**一次调用出结构化计划 | ✅ `planning.make_plan()` |
| FR-03 | 模型标注 `mode`（ro/rw，缺省 rw）+ `write_paths` | ✅ `planning.normalize_steps()` |
| FR-04 | 校验：mode 枚举 / ro 禁带 write_paths / rw 路径在工作区内且互不重叠 / 依赖无环 | ✅ 同上（`/etc/passwd`、`a/../../etc`、`C:/…` 一律拒，`..` 按段判） |
| FR-05 | **ro 的写/建/删/移被机制层拒绝** | ✅ 角色 `read_only`（`src/tools.cpp:424`） |
| FR-06 | ro 硬只读工具集（含排除 MCP 编排工具） | ✅ `READ_ONLY_ROLE_YAML` + 白名单交集 |
| FR-07 | 只读工具的**正则搜索** | ✅ `regex=1` |
| FR-08 | **ro↔ro 并行、rw 独占** | ✅ `_run_plan()` |
| FR-09 | 并发上限 4；父级 token 闸门 0（不限） | ✅ `planning.MAX_SUBTASKS`（子任务**数**的默认上限 4 由工具把住；内核 `MAX_STEPS=6`，模型被直接要求按传进来的数拆） |
| FR-10 | 每子任务 `limits`（`max_wall_ms` 必填） | ✅ `DEFAULT_LIMITS` |
| FR-11 | **两段式**：`op="start"` 秒回；`op="status"` 取结果 | ✅ |
| FR-12 | 后台汇总（模型）+ 结果回对话 | ✅ `planning.aggregate()` |
| FR-13 | 汇总**溯源**（结论标注来源子任务）+ 写明失败/未启动 | ✅ `AGGREGATE_SYSTEM` + `planning.aggregate_prompt()`（没跑的子任务写「这个子任务没有留下结果」，单步结果超 8000 字截断） |
| FR-14 | 计划与子任务句柄**落盘** | ✅ `<TEA_HOME>/plans/` |
| FR-15 | **回收器**收走孤儿子轮次 | ✅ `_reaper_loop()`：**模块加载即起**；写盘前重读计划；超时 + 重启遗留的计划标 `interrupted` |
| FR-16 | **闸门五条**（见 R5） | ✅ |
| FR-17 | 幂等：同一目标重复编排不重复起 | ⚠️ 计划号 + `Idempotency-Key`（每个子任务一把） |
| FR-18 | 部署可选 `--no-save`（模型不能自己写工具） | ✅ 扩展层已有 |
| FR-19 | **不改** a2 内核 | ✅ |
| FR-20 | 队列版：一个计划 → 父任务 + 每步一个子任务；`mode` 进队列、只读步骤可并行 | ✅ §十 |
| FR-21 | 每个子任务的 `turn_id` 落进计划文件 | ✅ |
| FR-22 | `op="status"` 支持**按子任务**返回过程摘要 | ✅ |
| FR-23 | 网页「子任务卡片」（按 `turn_id` 订阅 events） | ✅ §十一（前端，后端零改动） |
| FR-24 | 只读角色必须**真**只读（`read_only` + 非空工具清单、不含 exec/内存/文档/编排工具）；装不上或不合规**不跑** | ✅ `planning.role_is_read_only()`（扩展层与队列共用） |
| FR-25 | 多渠道（TG / 微信 / 账本 / 审计）都有编排入口 | ⏳ 没做：现在只有 `POST /api/plans` 与 `planner.py` |

---

## 九、风险

| 风险 | 缓解 |
| --- | --- |
| **MCP 调用不可取消** → 孤儿轮次烧钱 | 两段式（R3）+ `limits.max_wall_ms` 必填（R5-3）+ 回收器（R6） |
| 模型滥用编排（多花钱） | 闸门五条（R5）+ 建议 `--no-save` |
| 扩展在围栏之外（能直接写文件） | 模块目录不放工作区（`mcp_server.py` 自带检查告警）+ 只用 `Client` 调 a2 接口 |
| `std::regex` 回溯 / 慢 | 式长 ≤256 + 行数上限 + 提示缩小 `path`；**单行很长时上限不管用**（见 §七） |
| 只读角色被改成能写（`readonly` 只是个名字） | 用之前校验 `read_only` + 工具清单（`planning.role_is_read_only`）；不合规不跑 |
| 一个子任务失败把整份计划卡到 deadline | 依赖失败 → 后继立刻标 `skipped`；没有能跑的也没有在跑的 → 收尾（`_run_plan`） |
| 模型把该 rw 的标成 ro（或反过来） | 缺省 rw（保守）；ro 带 write_paths 报错 |
| 无队列的账本 / 通知 / 恢复 | P0 自写最小版（落盘 + 回收器 + 结果回写）；P1 队列版补齐 |

---

## 十、队列版（P1，也做了）

同一个内核，队列那一侧也用上了：

| 落在哪 | 是什么 |
| --- | --- |
| `sched/planning.py` | **两边共用**：出计划、校验（mode / write_paths / 依赖无环）、汇总、只读角色的 YAML |
| `sched/task_server.py` | 任务的记录带 `mode`；只读任务跑在只读角色下（队列把角色装进工作区，再随轮次发出去）；调度器认得「两个只读可以并行、一个会写的独占」 |
| `sched/plans.py` | 计划的步骤带 `mode` / `write_paths`（规则来自共用内核） |
| `sched/planner.py` | 出计划走共用内核 → 计划自带读写类型（人在 `--out` 的 JSON 里也看得见） |
| `sched/a2_ext_py/queue.py` | 多了 `create_plan()`：把一个计划交给队列 |

**队列的开关**：`--parallel-ro-steps`（**默认关**）。关着时队列一个字没变（一个项目一个
时刻一个任务）；开了以后，**只读**任务（`mode: "ro"`）可以同时跑，**会写的**（`mode: "rw"`）
仍然独占。普通任务也能标：`POST /api/tasks {"mode": "ro"}`。

**只读任务必须跑在只读角色下**：队列第一次给某个工作区装 `readonly`，已经有的先校验
（`read_only` + 工具清单；空清单在 a2 里等于全部工具，也算不合规）。**不合规不跑**：
任务直接判失败（`stop_reason: read_only_role_missing`），而不是在没有角色的情况下照跑——
那样它仍然按「只读」和别的只读任务并行，却握着全套工具。

回归：`tests/smoke_plan_parallel.py`（19 项：开关关=不并行、开关开=只读并行、只读独占、
只读角色装好并随轮次发出、矛盾的计划被拒、**工作区里的 `readonly` 不合规时任务不跑**）。

## 十一、网页上的「并行中的轮次」（1.7.0 改成右下角的热区）

子 agent 各自是一个会话，所以它们的轨迹**不在**父会话的轨迹里。网页上唯一能看到它们
在做什么的地方，是对话页**右下角（输入框上方）的热区**：**一个进行中的轮次一个热区**
（最多四个，多出来的合并成「+N」）。

- **热区**：一行高（30px），`⟳` + 标题（只露 5 个字符，更长的横向滚动）。右缘对齐
  发送按钮。它是 `position:fixed` 的浮块，**不占对话区的布局**——1.6.0/1.6.1 那条横幅
  把当前会话压掉了约 150px，这一版把它整个摘掉。
- **悬停 180ms** 弹出面板：会话标题 + 状态 + 已跑时长 + 「关闭」，卡片里是这一轮
  **正在做什么**（工具名 + 参数、每个工具成没成、已经说出口的字；事件流直播），
  日志区 7 行（112px），每张卡片还能「展开」到 320px。
- **鼠标移开不收起**：要能读那 7 行、要能点「展开」翻日志。收起只有三条路——
  「关闭」按钮、点面板外、悬停到另一个热区（切换；同时只开一个浮层）。
- **只列进行中的轮次**：跑完 / 失败 / 到预算 / 被中断的**立刻消失**，没有「完成」态，
  也不再留一分钟。代价要说清楚：子 agent 最后说了什么，只有它还在跑的时候打开面板
  才看得到；要复盘请去它自己的会话。页面打开之前就结束的轮次更不会出现。
- 只在**对话页**显示；切到文件/终端/记忆/日志/设置时藏起来（数据留着，回来立刻就有）。
- 一个轮次都没有时也**不停轮询**（6 秒一次；有东西时 2 秒），不然「又有一轮跑起来了」
  永远听不见。
- 事件流（SSE）收起时**仍挂着**，展开即可用，不必等回放。

**只有鼠标事件**：不做键盘、不做触屏，所以手机上的 a2 APK（同一套 Web UI）看不到这个面板。

面板只读服务端**已有**的东西：`GET /api/turns`（最近的轮次句柄，最新在前）、
`GET /api/turns/{id}/events`（事件流）。**后端一个字都没改**——纯前端。

浏览器回归：`tests/ui_parallel_turns.mjs`（27 项：热区出现、一行高、对齐发送按钮、不挡布局、
悬停弹出、移开不收起、「关闭」与点外收起、切换、`+N` 合并、结束即消失、只在对话页出现；
悬停用 CDP 的 `Input.dispatchMouseEvent` 打**真鼠标事件**——180ms 定时器 + 真
`mouseenter` 是 `Runtime.evaluate` 的合成事件测不到的）。

> 网页上的轨迹仍然是**按会话**的（`web/app.js` 的轨迹面板）：子 agent 的过程在这个面板里看，
> 不是在它的轨迹里。
