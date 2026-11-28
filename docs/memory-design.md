# tea_a2 记忆分层设计（四层模型）v0.1

状态：**P0、P0b、P0c、P1、P2 都已落地**（2026-11-28）。剩下的只有 P3（可选）。
参照 Anthropic《The components of agent memory》(2026-11) 的分层，借鉴 dsh-world 的实现经验
（MIT 许可，只借鉴设计，不引入它的代码——它是 DSH 的 Cordis 插件，与 tea_a2 的 agent loop 不通用）。

本文只写**四层模型**。已有的结论层（`memory.json` 条目、v1.2 权重基线）保持不变，见
`docs/workspace.md`「记忆的权重：会衰减，也会被『加强』」。

---

## 1. 为什么要分层

现在的长期记忆只有一层：**结论层**。它记的是「事实与偏好」（"这个项目用 yarn，不用 npm"），条目化、
可加权、可衰减、自动注入 prompt。这套设计本身没问题，保留。

三个短板：

1. **记的是结论，丢的是过程。** 条目是单行 bullet，没有上下文脉络。「那天我们为什么这么定」「中间试过
   哪几条路」没有落脚点。
2. **不按对象组织。** 所有条目平铺在一份 JSON 里，不按人/事/系统分页、不互链。多跳回忆
   （"约小林去哪儿"要串起小林 → 唱片店 → 面馆）无解。
3. **检索分裂。** `toolkit_history_search`（会话视角）、`toolkit_workspace_search`（文件视角）、
   `toolkit_memory`（结论）三个入口，语义不同、参数不同，无法一次查完；也分不清「没找到」和
   「来源读不到」。

dsh-world 的实测（它自己写在 NOTES.md 里）：多跳回忆题，**没有「认识页」时答对率 0.47~0.70，有「认识页」
时 1.00**；按页名直开（`wiki_open`）让工具调用从平均 3.4 次降到 2.3 次、花费约 -20%。单件事的回忆，
日记本来就够用——所以**日记不是重点，主题页才是**。

## 2. 四层模型

分法对齐 Anthropic 那篇的分层，但每层用 tea_a2 自己的存储和工具实现。

| 层 | 内容 | 存储 | 写入者 | 常驻注入 | 检索 |
| --- | --- | --- | --- | --- | --- |
| L1 原文层 | 对话原文与工具轨迹 | `.tea_agent_run/sessions/*.json`、`.tea_agent_run/trajectories/*.jsonl` | 系统自动 | 会话内天然在上下文里；跨会话按需检索 | `memory_find(kinds=[conversation])` |
| L2 日记层 | 每天一页的工作日记 | `.tea_agent_run/journal/YYYY-MM-DD.md` | agent | 否（目录里只给日期指针） | `memory_find(kinds=[journal])` |
| L3 主题层 | 对某个对象/系统的当前理解，一主题一页 | `.tea_agent_run/knowledge/<slug>.md` | agent（人也能在 UI 改） | 否（正文按需读） | `memory_find(kinds=[knowledge])`、`knowledge_open(title)` |
| L4 目录层 | 主题页目录 + 程序算出的索引 | `.tea_agent_run/knowledge/index.md` | agent 写描述，程序算关系 | 是，每会话一次 | `memory_find(kinds=[knowledge])` 也能命中 |

四层之外，**结论层**（v1.2 的 `memory.json`）继续存在：它是四层里**值得常驻**的那部分结论的投影。
关系是单向的：结论层不反向写回四层。

### 2.1 L1 原文层

- **存哪**：会话文件 `sessions/<sid>.json`（`messages[]`，含 role / content / created_at / tool_calls）
  和轨迹 `trajectories/<sid>.jsonl`。两者已经在写了，本次不改存储。
- **谁写**：系统，每轮自动落盘。人不能手改（改了会被下一轮覆盖）。
- **格式**：JSON（会话）/ JSONL（轨迹），**不新建数据库**。理由同 dsh-world：量不大，
  「查的时候现读」比维护索引省事，也省掉升级建库。
- **注入**：会话内，历史消息本来就在上下文里；跨会话**不常驻**（P2 再考虑「近期块」）。
- **检索**：扫会话文件，按字面匹配。

### 2.2 L2 日记层

- **存哪**：`.tea_agent_run/journal/YYYY-MM-DD.md`（一层，不进子目录）。
- **谁写**：agent。写日记的门槛：**发生了值得记的过程**，不是值得记的结论
  （结论写 `memory.json`）。格式自由，程序不解释内容。
- **什么时候写**：会话结束前、或当天第一次干活时，由模型自己决定；也可以用 `sched/` 的任务队列
  做 nightly 触发（P2）。
- **注入**：不注入正文。目录块里给最近 3~7 天的日期指针。
- **检索**：字面扫描，和 L1/L3 一次查完。

### 2.3 L3 主题层

- **存哪**：`.tea_agent_run/knowledge/<slug>.md`，一层。`slug` 用 ASCII 小写短横线
  （中文主题的 slug 由模型自己起，例如 `mqtt-broker`）；**正文第一行 `# 标题` 是中文标题**，
  目录和 `knowledge_open` 都按这个标题认人。
- **谁写**：agent。它只在**对某个对象有了新的、不只是一次的理解**时才写一页
  （"这个 broker 的 zone 是设备端声明的，不在 broker 端集中管理"是一页；"端口是 8383"是结论，不是页）。
  人也可以在 UI 里手写/改。
- **互链**：正文里 `[[标题]]` 指向另一页。**链接关系由程序每次现算**，不存。
- **注入**：不注入正文，只注入目录（每页一行"这页讲什么"）。
- **检索**：`memory_find(kinds=[knowledge])` 按字面搜正文；`knowledge_open(title)` 按标题/文件名直开。

### 2.4 L4 目录层

- **存哪**：`.tea_agent_run/knowledge/index.md`（agent 自己写的目录）+ 程序算出的索引
  （页标题、mtime、字数、链接关系、指向但还没写的页）。
- **注入**：**每会话一次**——会话开始、压缩之后、或闲置超过 N 小时（默认 24h）再开口时重读；
  中途改了目录要等下次刷新。放在 prompt **尾部**（见 §4）。
- **预算**：目录块默认 ≤ 40 行（`settings.yaml` 的 `memory_catalog_lines` 可改，0 = 不注入）；
  排序是**与本轮话题的相关度优先**（同一套 2-gram 相关度），其次最近改的；
  超预算时**写明"还有 N 页没列"**——只砍行数而不说砍了谁，模型就不知道自己还缺什么。
- **人也能改**：主题页在网页的「记忆」页里能读、能改、能删（`GET/PUT/DELETE /api/workspaces/{id}/knowledge/{slug}`）。agent 用文件工具写同一批文件。

### 2.5 结论层（v1.2，不动）

- **存哪**：`.tea_agent_run/memory/memory.json`。
- **注入**：手动 ≤20 + 强 ≤24 + 中 ≤14 + 弱抽 2 = **≤60 条**，顺序固定（手动→强→中→弱）。
- **与四层的关系**：写哪一层的判断标准——
  - 「以后每次都要用」→ 结论层（notes）
  - 「对某个对象的理解、过程、为什么」→ 主题层 / 日记层
  - 「原话、原始输出、命令结果」→ 原文层

## 3. 统一检索接口

四层用**一个**检索入口查完，参照 dsh-world 的 `memory_find` / `memory_read` 语义：

### `toolkit_memory_find`

参数：

| 参数 | 含义 |
| --- | --- |
| `query` | 字面文字，**区分大小写**，不是搜索语法；留空则按时间翻页 |
| `kinds` | `conversation` / `journal` / `knowledge`，默认全部 |
| `from` / `to` | 带时区的 ISO 时间，左闭右开 |
| `sourceId` | 限定来源（会话 id / 日记文件名 / 主题页 slug） |
| `order` | `newest` / `oldest` |
| `limit` | 默认 8，最多 100 |
| `cursor` | 翻页用；**绑定查询条件**（换了查询就作废，返回 `cursorExpired`）。位置记的是命中本身（时间 + ref），不是序号——列表在读的时候还会长（新的消息比旧的都新），记序号会指错 |

返回：`items[]`（每条 `ref` / `kind` / `time` / `title?` / `who?` / `text`（摘录 ≤200 字））、
`nextCursor`、`coverage`（有来源读不到时出现：`state: partial` + `failures`）、`note`（零命中时的提示：
字面没找到 ≠ 没发生过，换个说法或按时间翻）。

### `toolkit_memory_read`

参数：`ref`（程序生成，不能手写）、`offset`（字符，默认 0）、`limit`（默认 2000，最多 16000）。
返回**原文**（不是摘要），带 `nextOffset` / `totalChars`。会话被修订后旧 `ref` 报 `sourceChanged`。

### `toolkit_knowledge_open`

参数：`title`（页名，`小林` / `[[小林]]` / `小林.md` 都行）、`offset`、`limit`。
返回正文 + `related`（`links` / `linkedFrom` / `missing` / `staleRefs`）。

### ref 格式

```
conv:<session_id>#m<message_index>      会话里的一条消息
conv-summary:<session_id>               被压缩掉的那段摘要
journal:<YYYY-MM-DD.md>#c<char_offset>  日记里的一个位置
knowledge:<slug>#c<char_offset>         主题页里的一个位置
```

`ref` 只证明找得到出处，不证明出处支持结论——工具说明里要写这句话。

### 与现有工具的关系

- `toolkit_history_search` **保留**（会话视角："我之前在哪个会话里干过这个"），内部复用同一套扫描器。
- `toolkit_workspace_search` **保留**（工作区文件，与记忆无关）。
- `toolkit_memory` **保留**（结论层的增删查）。

## 4. 注入预算与缓存纪律

### 4.1 现状的问题

- 记忆块在**系统提示词头部**，且按**当轮用户提问**做相关性筛选（中档 `memory_relevance`）→
  换个话题块就变 → 系统提示词变 → **整个会话的前缀缓存失效**，整段历史重新计费。
- `agent.md` 每轮重读，也在头部。agent 自己 `op=append` 之后立刻生效是有价值的，但代价同样在头部。

tea_a2 自己在 `build_system_prompt` 里写过规矩：「头部放稳定内容，变化的块放尾部」——todo 已经放在尾部了，
记忆块和 agent.md 还没跟上。

### 4.2 三条纪律

1. **头部只放稳定内容**：persona / role / 环境 / `instructions.md`。
2. **易变内容一律放消息尾部**（追加在最后一条消息之后，用一条尾部消息承载）：
   结论层记忆块、目录块、todo、agent.md 摘要。**改动只影响尾部，前缀缓存保持命中。**
3. **人设类改动不中途生效**：`system_prompt.md`（工作区人设）只在会话开始或压缩后重读，
   中途改要等下次压缩才生效——改了就得让整段上下文重新计费，不值。
   `agent.md` 例外（它是 agent 自己写的经验，立刻生效有价值），但同样挪到尾部。

### 4.3 尾块是怎么发的

系统提示词只留稳定的头部（人设、角色、环境、instructions、压缩摘要）；**每轮会变的那一块**
（工作区经验 agent.md、结论层记忆、主题页目录、任务列表、日记的写法）合成**一条尾消息**，
放在上下文的最末尾。好处：改一次记忆条目只废掉缓存的尾巴，不废掉整段对话。

尾消息用 `role=system` 发。万一某个网关不接受历史之后的 system 消息（会回 400），
服务端会自动把它折回系统提示词再试一次——回合不丢，只是那一次缓存不省。

### 4.4 各块位置与预算

| 块 | 位置 | 预算 | 刷新时机 |
| --- | --- | --- | --- |
| 结论层（notes） | 尾部 | ≤60 条（v1.2 不变） | 每步（内容变了就变） |
| 目录层（L4） | 尾部，紧挨 notes | ≤40 行 / 4000 字符 | 会话开始 / 压缩后 / 闲置 >24h |
| L1/L2/L3 正文 | 不常驻 | — | 按需 `memory_find` / `memory_read` |
| agent.md | 尾部 | 文件 ≤32 KB，注入 ≤6 KB | 每步 |

## 5. 分期与验收

### P0：地基（不改现有行为）—— **已落地**

1. 四层的存储骨架：`journal/`、`knowledge/` 目录 + `Workspace` 侧的扫描/读写接口
   （`src/workspace.cpp`、`src/workspace.h`）。
2. 三个新工具：`toolkit_memory_find` / `toolkit_memory_read` / `toolkit_knowledge_open`
   （`src/tools.cpp`）。**不改**现有三个工具的语义。
3. `toolkit_history_search` 内部改用同一套扫描器。

验收：`python tests/smoke_memory_layers.py`（23 项）
——写入四层 → 一次 find 查到四类 → read 读回原文（不切坏 UTF-8）→ 来源读不到时出现 `coverage`
→ 零命中给 `note` 而不是空数组 → `knowledge_open` 按标题打开并返回 `related`
→ 游标翻页落在不同的命中上 → `order=oldest` 时最旧的在前。

已实现的落点：`src/workspace.h`（四层的结构体与接口）、`src/workspace.cpp`
（扫描与检索）、`src/tools.cpp`（`toolkit_memory_find` / `toolkit_memory_read` /
`toolkit_knowledge_open`）。`toolkit_history_search` 的语义没变。

### P0b：缓存纪律（改 prompt 形态）—— **已落地**

4. 结论层记忆块 + 目录块从系统提示词头部挪到消息尾部；`system_prompt.md` 改成会话开始/压缩后重读。

验收：e2e 回归；用 provider 的 usage 字段验证——改了 notes 之后 `cache_read` 不掉到 0
（若 provider 不返回该字段，则在测试里比对请求体前缀哈希）。

### P0c：压缩改成「只留对话」—— **已落地**

5. `compact_history`（`src/agent.cpp`）的 segment 里剔掉工具结果正文，只留工具名清单；
   摘要后附一行「【这段用过的工具】a, b, c」。

依据：dsh-world 实测把每次调用写得更详细并没有让模型答得更准或少查几次（改过的文件压缩后总会重看），
但把工具输出喂进压缩请求是实打实的 token 和噪声。

验收：压缩产物的 summary 不含工具输出正文、含工具名行；e2e 相应断言要改。

### P1：L3 主题层 + L4 目录层（真收益在这）—— **已落地**

6. `knowledge/<slug>.md` 的写入约定 + `[[链接]]` 现算 + `index.md` 目录块 + UI 页
   （`web/app.js`、UI API）。

验收：`node tests/ui_memory_layers.mjs`；主题页写入 → 目录块列出 → `knowledge_open` 打开 →
多跳回忆题（串三个对象）能答对。

### P2：L2 日记层—— **已落地**

7. `journal/YYYY-MM-DD.md` + 触发机制（模型自发 或 `sched/` 任务队列 nightly）。

验收：跨天会话能在目录块里看到昨天的日记指针；find 能搜到日记正文。

### P3（可选，不排期）

8. 跨会话「近期原文块」（dsh-world 的 200 轮）；记忆里的图片占位符 + 取回。

## 6. 明确不做

- **不做向量检索 / embedding**：dsh-world 自己反对（*Don't Do RAG*、*Is Grep All You Need?*），
  tea_a2 同。
- **不自动从对话里抽主题页**：模型自己写，程序不代写（和 dsh-world 同一条规矩：
  「日记和『我』由她自己动笔，插件从不替她写」）。
- **不把四层的内容倒进结论层**：结论层只放"以后每次都要用"的短结论，注入预算不因四层而变大。
- **不跨工作区共享**：除了已有的「合并工作区」导入机制。
- **不动 v1.2 的权重、衰减、合并、淘汰审计**。

## 7. 已拍板（2026-11-28）

| # | 问题 | 结论 |
| --- | --- | --- |
| Q1 | 主题页谁写？ | **agent 写 + 人工可改**（网页的「记忆」页里能读/改/删） |
| Q2 | 目录块 40 行够用么？ | 够作**默认**（可配 `memory_catalog_lines`）；关键不是行数而是**排序**（相关度优先）和**说清楚砍了谁**（「还有 N 页没列」） |
| Q3 | 接受记忆块不在系统提示词头部？ | 接受（尾块方案，见 §4.2） |
| Q4 | 日记要不要？ | 要（每天一页，agent 自己写；规则写在尾块里） |
| Q5 | 主题页 slug 用 ASCII、标题放正文首行？ | 认 |

## 8. 还没做的（2026-11-28 定：**过一段时间再做**）

剩下的功能推迟。下面每条都写了「下次从哪儿接着做」，免得下次重新摸一遍。

| # | 待办 | 下次怎么做 | 落点 |
| --- | --- | --- | --- |
| R1 | **日记的 nightly 触发器** | 任务队列的 schedule：每天 23:00 给这个工作区发一轮「把今天的过程写进 journal/YYYY-MM-DD.md」。schedule 走队列的 `POST /api/schedules`（`cron` + `prompt` + `workspace_id`），到点就把那条 prompt 当一轮对话发出去。agent 侧不用改——写法已经在尾块里了 | sched/ 侧加一条默认 schedule；每台机器每个工作区一条 |
| R2 | **跨会话「近期原文块」** | dsh-world 的做法：最近 N 轮原文放在眼前。tea_a2 的会话本来就能恢复，收益一般；要做就放进尾块，按 `from`/`to` 取最近 N 轮 | `src/workspace.cpp` 取法 + 尾块 |
| R3 | **记忆里的图片占位符 + 取回** | 图片在记忆里存成一行占位符（`[图片 sha256:… image/jpeg 宽×高 多少字节]`），新工具 `toolkit_memory_image` 凭那一行取回 | `src/workspace.cpp` + 一个新工具 |
| R4 | **目录块里报告断链** | 链接关系已经现算（`knowledge_open` 的 `missing`），但目录块还没说「哪一页指向了还没写的页」 | `src/workspace.cpp::knowledge_catalog` |

**建议的顺序**：R1（一次模型调用，最小）→ R3（要动图片存储）→ R2（改尾块，改动面最大）。
R4 随时可加。

**别做的**（§6 的规矩仍然成立）：向量检索、程序代写主题页、把四层倒进结论层、跨工作区共享。

## 9. 回归

| 测试 | 覆盖 |
| --- | --- |
| `tests/smoke_memory_layers.py`（30 项） | 四层一次查完、ref 读原文、coverage、零命中提示、游标翻页与失效、目录块的排序与省略提示 |
| `tests/ui_memory_layers.mjs`（10 项） | 网页里列出/打开/保存/新建/删除主题页 |
| `tests/smoke_memory_weights.py`（45 项） | 结论层的权重基线（尾块之后仍成立） |
| `tests/e2e.ps1`（158 项） | 整体回归 |
