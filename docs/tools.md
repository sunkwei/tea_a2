# 内置工具与上下文管理

## 内置工具

模型看到的是标准 OpenAI function-calling schema（`GET /api/tools` 可查）。

| 工具 | 能力 |
| --- | --- |
| `toolkit_exec` | 执行系统命令。Windows 写成临时 `.cmd`/`.ps1` 再执行（绕开引号问题），超时用 Job Object 杀整个进程树；POSIX 用 `setsid` + 进程组 `kill`。输出统一转 UTF-8 回填模型 |
| `toolkit_file` | `read` `write` `append` `list` `mkdir` `delete` `move` `stat` |
| `toolkit_memory` | 工作区长期记忆的增删查，自动注入 system prompt，跨会话保留偏好与项目事实 |
| `toolkit_agent_doc` | `read` `append` `replace` `scripts`：维护 `.tea_agent_run/agent.md`（本工作区的经验：坑、可复用命令与脚本） |
| `toolkit_load_image` | 从磁盘加载图片（PNG / JPEG / GIF / WebP）并让模型真正看见它。仅在模型声明 `supports_vision: true` 时出现在工具表里 |
| `toolkit_todo` | 把复杂任务分解成阶段列表并维护进度：`list` `set`/`replace` `add` `update` `clear`。列表存在会话里、每轮注入 system prompt、每次变更通过 SSE 的 `todo` 事件推给 UI |
| `toolkit_task` | 汇报**排队任务**的进度：`op=update progress=0-100 note=…`，需要人确认时 `needs_confirmation=true question=…`。任务队列在 Python 侧（`sched/task_server.py`），agent 这边只报数：任务服务从这一轮的事件流里读走这些参数 |
| `toolkit_search` | 网页搜索：结构化结果（标题 / URL / 日期 / 摘要）。**provider 级能力**——对话用的 provider 声明 `supports_search: true` 才出现在工具表里；另外配了独立搜索 provider（`provider.yaml` 顶层 `search:` 段）时也会出现，请求发给那个 provider（用它的 key）。见 [config.md](config.md) |
| `toolkit_fetch` | 抓取单个网页 → Markdown 正文。同样是 provider 级能力（`supports_fetch`），也走独立搜索 provider |
| `toolkit_fleet` | 管理一队设备（学校机群）：`status` `devices` `report` `collect` `exec` `upgrade` `ask`。**只在配了 `fleet_url` 时才出现在工具表里**——和 provider 不支持就不给 `toolkit_search` 一个规矩：不让模型去试一个它用不了的工具。详见 [fleet.md](fleet.md) |
| `mcp__<服务器>__<工具>` | **MCP 服务器提供的工具**：和 provider.yaml 同目录的 `mcp.yaml` 里配的独立程序，agent 启动它们并用 stdio 上的 JSON-RPC 跟它们说话，它们的工具就进入模型的工具表。服务器连不上时它的工具不会出现（原因在设置页和 `/api/mcp/servers` 里）。详见 [config.md](config.md#mcp-服务器mcp-yaml) |

相对路径基于工作区根目录；默认**禁止越出工作区**，判定前会解析符号链接，所以工作区里的软链
也无法把工具带出去。这个边界可以在网页上开关——见下面的「文件访问范围」。

### 图片：`toolkit_load_image`

`toolkit_file` 只能把字节当文本返回，视觉模型读不了；在此之前，图片唯一的入口是人手动粘贴。
`toolkit_load_image` 补上另一半：

```jsonc
// 模型调用
{"path": "build/screenshot.png", "note": "看看这个弹窗为什么错位"}
```

它按**内容**（magic bytes，而不是扩展名）判定是不是 PNG/JPEG/GIF/WebP，把文件**复制**到
`.tea_agent_run/artifacts/images/`（源文件之后被改、被删都不影响已存下的会话，`/raw` 也能把它
送回界面），然后返回一段文本结果 —— 图片本身不能挂在工具结果上：OpenAI 的 wire 协议只允许
**user** 消息带图片。所以 agent loop 在这一次迭代的所有工具结果之后追加一条 follow-up user
消息（内容形如 `Images loaded by toolkit_load_image (attached so you can see them): - …`），
下一轮请求就从这里读到图片。UI 通过 SSE 的 `tool_images` 事件立刻把这条消息和图片显示出来，
刷新后从会话里恢复，位置不变。

几条边界：

- **只有视觉模型才看得到**：`supports_vision: false` 的模型根本不会被告知这个工具（免得它白调），
  真被调到时也会明确说明「当前模型不支持视觉」，而不是静默丢掉图片；
- **每轮最多 6 张**（`kMaxToolImagesPerTurn`）：每张图之后每次请求都要重新上传，占上下文也占带宽，
  超出部分在工具结果里说明「未附加」，模型可以下一轮再看；
- **单张 8 MB**：超了就让模型先缩放（工具结果里会说清楚）；
- **历史图片会继续发送**：会话里存的是路径，每轮把上下文里所有图片重新编码成 data URL
  （压缩掉的部分跳过），所以「上一轮看过的那张图还能不能接着问」的答案是能。为免无限增长，
  一次请求的图片总量封顶 24 MB，超出时最旧的几张降级为纯文本并记一条 WARN。

### 文件访问范围：受限工作区 / 全局

`--allow-outside-workspace` 原本只能写在命令行上：想改主意就得重启进程，而 agent 跑在设备上
时这往往意味着再找一次 shell。现在它同时也是设置页上的一个开关，写进工作区自己的
`.tea_agent_run/settings.yaml`，所以重启后依然生效，读项目的人也能看见这个项目被放宽了。

```
设置 → 文件访问范围
  [ 受限工作区 ]  工具只能在工作区根目录内读写，越界路径直接报错
  [ 全局 ]        取消边界：工具与文件页可访问整台机器上的任意路径
```

- **默认是「受限工作区」**，与命令行默认值一致；
- 切到「全局」会先弹一次确认框——这是一次权限放宽，不该由误点完成。切回「受限」不需要确认；
- 切换**立即生效**：新请求按新设置走；已经开始的那一轮仍按它开始时的设置执行（工具在调用
  时才解析路径，但一轮之内的行为保持一致）；
- 系统提示词里的那一行会跟着变：受限时是「File tools are restricted to the workspace root」，
  放宽后明确告诉模型边界已解除、但别去动工作区外面的东西。少了这句话，模型会以为它仍然被
  关着，把本来能做的事说成做不到。

**命令行是下限，不是默认值。** 用 `--allow-outside-workspace` 启动时，工作区的
`settings.yaml` 和网页开关都**不能**再把边界收回来放开的这一步取消——此时两个按钮都是禁用的，
卡片上写明「命令行固定」，`PUT` 直接回 `409 fixed_by_cli`。这是有意的：操作员在命令行上做的
安全决定，不该被一个之后的网页请求悄悄推翻。真想恢复限制，就去掉参数重启。

**接口**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/workspaces/{id}/settings` | `allow_outside_workspace` / `fixed_by_cli` / `settings_file` |
| `PUT` | `/api/workspaces/{id}/settings` | `{"allow_outside_workspace": true\|false}`；`409 fixed_by_cli` 表示命令行锁死，`400` 表示值不是布尔 |

写入是**按行修改**，不是把整份 YAML 读进来再序列化回去：注释、键顺序、引号风格和文件里其他
设置都保持原样，只改那一行（没有这一行就插进 `settings:` 块里）。文件解析不了时直接报错而
不是重写——把一个手写坏的 settings.yaml 悄悄换成程序生成的版本，会把「为什么这台设备的配置
没生效」这个线索一起删掉。

浏览器端的回归测试是 `node tests/ui_workspace_access.mjs`（18 项，需要 Chrome）：它真的点按钮、
真的取消一次确认框、真的去读一个工作区外的文件，并覆盖命令行锁死那种状态。

### 任务列表：`toolkit_todo`

一个「实现 X」的请求往往会跑几十步，只说「我正在做」没有任何信息量。`toolkit_todo` 让模型把
工作写下来，而这份列表同时是**给人看的进度**：

```jsonc
// 开工时写计划（也可以直接带初始状态）
{"op":"set","items":["读取现有实现","实现 todo 工具","在界面上呈现","补充测试"]}

// 开工前把要做的这一条置为 active，做完立刻 done
{"op":"update","index":2,"status":"active"}
{"op":"update","index":2,"status":"done"}
```

几条刻意的约束：

- **同时只有一条 `active`**：把另一条置为 `active` 时，原来那条自动退回 `pending`。这是让列表
  能回答「现在到哪儿了」的前提——两条同时「进行中」等于没有信息。
- **状态只有三档**：`pending` / `active` / `done`。想表达「被跳过」就删掉或用 `set` 重写。
- **每轮都注入 system prompt**（末尾一节 `Task list (3/7 done)`）：压缩过历史、或者进程重启
  之后，模型仍然知道自己走到哪一步。放在最后是因为它是唯一会在一轮里变化的块，这样更新列表
  只影响提示词尾部，不破坏前缀缓存。
- **计划是这一轮的脚手架，这一轮结束就关掉**（做完没做完都关）：会话结束了，界面上还挂着一份
  没做完的列表，看着像「还有人没干完的活」——其实没有人在做它了。做完的计划留着只是死重
  （每轮都注入提示词，还占着界面上一块面板），没做完的留着则更误导人。
  所以这一轮结束时：列表清空，**没做完的步骤不会丢**——它们记在会话的 `todo_carry` 里，
  下一轮一开始就把它们接回成这一轮的计划（「这些步骤是上一轮留下的」），跨重启也还在。
  想直接丢掉：界面上那一行有「丢掉」，或者模型在结束前 `op=clear`。
- **改一次就落盘并推送一次**：`session.json` 里存 `todos`，索引里存 `todo_total` / `todo_done`，
  SSE 发 `todo` 事件（`items` + `total` + `done`），所以 UI 在**生成过程中**就能看到进度条前进。
- **上限 50 条、单条 300 字节**，某个模型把自己当项目管理软件时不会把提示词撑爆。

Web UI 里它在输入框上方：

- 进度条 + `3/7`，全部完成时数字变绿；
- 每条前面是 `✓ / ● / ○`，进行中那条加粗、已完成的划掉并压暗；
- **点一下即可勾选完成**（`PUT /api/sessions/{id}/todos`），不需要等模型自己更新；
- 点标题行可以折叠，省出屏幕空间；
- 侧栏的会话行也带一个「3/7」小徽章，不用点进去就能看出哪个会话还没做完；
- **全部做完后面板自动收起**：一整列删除线钉在输入框上方既占地方又没有下一步可看，做完就
  让位；下一轮不再显示（侧栏的「7/7」和会话里的记录都还在，想翻随时能翻）。
- **这一轮结束时**：面板收起来（计划已经关掉），如果还有步骤没做完，输入框上方留一行
  「上一轮结束时还有 N 个步骤没做完（下一轮会接着做）」——那几步可以直接点掉（这一步会把计划
  接回来，你接管了它），也可以一键「丢掉」。

导出的 HTML 同样带这块进度（见 [导出会话](workspace.md#导出会话一个能拷走的-html-文件)），导入回来连进度一起恢复。

### 本轮产出：把做好的文件交出去

agent 干活的结果经常是**文件**而不是文字：一份报告、一张趋势图、一段渲染好的视频。这些
不该只留在工作区里等人自己去翻。会话因此维护一份 `artifacts` 列表：

- **谁写进去的**：`toolkit_file` 的 `write` / `append` 直接报路径；`toolkit_exec` 无法知道命令
  写了什么，所以命令结束后扫描一次工作时间窗内**新建或修改**的文件；
- **列的是哪些文件**：除构建噪音外**全部**列出——`.o`/`.obj`/`.a`/`.so`/`.d`/`.pyc`/`.class`/
  `.cmake`/`.moc`/`.log`/`.tmp`/`.bak` 这类编译与缓存产物不算，源码（`.c`/`.cpp`/`.py`/`.ts`…）、
  文档、图片、视频、数据文件都算。跳过的目录：`.git`、`.tea_agent_run`、`node_modules`、
  `build*`、`target`、`dist`、`3rd`、`__pycache__`、`.venv` 等；
- **不是「只列新增」**：同一轮里被**修改**的文件也会出现（改了 README 就会列出 README），
  因为对使用者来说「这轮动过什么」比「这轮新建了什么」更有用；
- **上限**：一次命令最多带回 200 个文件；真的被截断时，工具结果里会写明
  「more files were produced than the list can show」，界面也会提示「文件太多，只列出了部分」，
  不会拿一份残缺的清单冒充完整结果；
- **不收录什么**：`.tea_agent_run/` 下的内部状态（会话、轨迹、日志、`toolkit_load_image` 的
  私有副本）与空文件——把它们列出来只会淹没真正有用的那几行；
- **去重**：同一个路径改写多次只出现一次，大小取最新；
- **落在哪里**：`<session>.json` 的 `artifacts[]`（`path` / `bytes` / `kind` / `tool` /
  `created_at` / `turn`），每发现新文件就发一个 SSE `artifacts` 事件。

Web UI 只在**这一轮的最终回答下面**列出它们（这是「final msg 给出下载链接」的落点）：

```text
本轮产出  3 个文件
▤ report.md   文档 · 22 B                     下载
▣ chart.png   图片 · 68 B              下载   查看
▶ demo.mp4    视频 · 2.0 KB            下载   查看
```

- 「下载」走 `GET /api/workspaces/{id}/download?path=…`：`Content-Disposition: attachment`，
  从磁盘流式发送，所以一段 200 MB 的视频不会被 `/raw` 的 16 MB 上限挡掉（`/raw` 仍然是
  「在页面里显示」用的，两者分工明确）；
- 「查看」只对图片 / 视频 / 音频出现：图片进原图查看器，视频 / 音频用 `&inline=1` 在新标签页打开；
- 会话滚动到旧轮次时，旧的产出仍挂在**那一轮**的回答下面——不是把全会话的文件堆在一起；
- **文件多的时候自己折叠**：超过 5 个只显示前 5 个，标题行右侧出现「展开全部 N 个」，点开变
  「收起」。一轮产出 40 张截图也不该把对话挤出屏幕；
- 导出的 HTML 里有一节静态清单（文件名、类型、大小），并说明它是快照、不含文件本体。

---

## 多轮工具调用与上下文

单条用户消息内循环「模型 → 工具 → 模型」，直到模型不再请求工具（这就是一轮的
「最终回答」）。默认**没有轮数上限**（`max_iterations: 0` = 不限）：一轮要跑多久
由事情本身决定。

只有一种情况会提前停：模型**连续三次给出相同的结果**（同一个工具、同样的参数，
或者同一段内容）——它在原地打转，再跑下去只是烧钱。这时这一轮会停下，并在对话里
留一句说明（模型也会看到，下一轮知道发生了什么）。想要一个硬上限时，把
`max_iterations` 设成一个正数（到顶同样会停下并留一句说明）。

工具参数 JSON 解析失败时，把解析错误作为工具结果回灌给模型（而不是静默当成 `{}`），
模型有机会自我修正。

这一块的四个机制值得单独说，因为它们直接决定长会话能不能用：

### 1. 工具输出按内容属性削减，而不是按字节硬切

对 4 万行的构建日志，按字节截断留下来的是进度噪声、丢掉的往往是错误行。`toolkit_exec`
会先给命令分类（build / test / diff / 通用）再削减：

- **build / test**：保留每一条诊断行（`error`、`failed`、`undefined reference`、`错误`、`失败`
  …）及其后 2 行上下文，丢掉进度噪声（`[ 42%] Building ...`、cmake 的 `-- ...`、
  `Note: including file`），并始终保留头部与尾部；
- **其它**：保留头部 + 尾部；
- 每一段被省略的内容都替换成显式标记：
  `... [tea_agent omitted 321 lines of output: progress noise; diagnostics kept] ...`，
  模型因此知道自己看到的是删减版，不会基于残缺日志瞎猜。

过滤是确定性的（同样的输入必然产出同样的输出），因为工具结果会落盘并在后续轮次重放。
可以随时自测：`tea_agent --selftest`。

### 2. 预算按实际额度算，而不是拍一个百分比

```
budget = 上下文窗口 − 输出预留 − (system prompt + 工具 schema + 框架开销)
```

输出预留取 `max_tokens` 或模型的 `max_output_tokens`，并限制在窗口的 1/3 以内（避免饿死历史）；
预算有 `窗口 / 5` 的下限。**硬上限再留 10% 的窗口余量**：我们算 token 是估的，估偏了也不会
把请求顶到 provider 的墙上。`keep_turns` 是「至少保留最近 N 轮」的**硬下限**，单条消息本身超预算时
按头尾裁剪而不是直接溢出。日志里每次都能看到实际数字：

```
context: max=1000000 reserve_output=333333 fixed=847 budget=665820 kept=4/4 used=960 summarised=23
```

### 3. 超预算时做滚动摘要，而不是静默丢弃

历史超过预算时，把最旧的一段交给模型压缩成摘要，写入会话的 `summary`，并记录
`summary_upto`（已被摘要覆盖的消息条数）。**原始消息不会删除**——UI 和审计仍能看到全文，
只是不再发给模型。摘要会写进 system prompt 的「Compact history」小节，并在 SSE 里发一个
`compaction` 事件。摘要失败（网络/模型报错）时自动退回丢弃最旧消息，不会卡死会话。

**什么时候开始压**：不是压到窗口边界，而是到**预算的 65%**（`compaction_trigger_ratio`，
默认 0.65）。压一次之后，逐字保留的原文只剩预算的 16%（`compaction_retain_ratio`），
摘要本身最多 4096 token（`compaction_summary_max_tokens`）。留这么宽的余量有两个原因：
一是压到「刚好够」的话下一轮立刻又超，二是我们算 token 是**估**的
（ASCII 3 字符/token、CJK 1 字符/token、`reasoning_content` 按全额算，工具 schema 按固定值估），
估算偏乐观时真实请求会比预算大——提前压就是给这种偏差留的余量。

**压力检查在回合内部也做**：一个长回合（几十次工具调用）每一步都在长上下文，只在回合开始时
检查一次的话，等 provider 拒绝请求时这一回合已经死了。所以每次迭代发请求之前都查一次压力，
超了就先压再发。

**思考模型的摘要预算**：MiniMax-M3、DeepSeek reasoner 这类模型先把预算花在 `reasoning_content`
上，预算太小就只回思考、正文为空——压缩看起来「失败」了，历史一条没折，原始的 400 直接把回合
打死。所以摘要请求给 4096 token（够思考 + 写摘要），**只回 reasoning 时用 4 倍预算再问一次**。


`keep_turns`（默认 10）是「最近 N 轮逐字保留」的**硬下限**，即使超预算也照发；
`compaction_keep_recent`（默认 4）同理，压完至少留下这么多条消息。

### 4. 前缀缓存命中率是**可测量**的

DeepSeek / opencode 网关都按「最长公共前缀」缓存提示词。系统提示词里任何每轮都变的内容
（比如时间戳）都会让缓存整体失效，所以：

- Environment 块全部来自**启动时探测一次**的真实设备信息（见下），不随时间变化；
- 墙钟被移出提示词，改为提示模型需要时自己执行 `date` / `echo %DATE% %TIME%`；
- 每次补全都记录命中情况：`cache=1280/1444 (88%)`。

实测同一个真实端点连续两轮：**88% 和 87%** 的前缀命中。

### 环境探测（system prompt 的 Environment 块）

不是写死的 `Windows`/`Linux` 字符串，而是启动时真实探测设备（`src/env.cpp`）：

```text
Environment (probed from this device at startup)
- OS: Windows 10.0.19045 (x86_64)      # RtlGetVersion 拿真实 build，不用会撒谎的 GetVersionEx
- Host: SUNKW-X1
- Time zone: UTC+08:00
- CPUs: 8, memory: 15.9 GiB
- Free space on the workspace volume: 329.5 GiB
- toolkit_exec shell: cmd (also installed: powershell, pwsh)
```

Linux 下走 `/etc/os-release`（嵌入式发行版能读成 `Buildroot` 之类）+ `uname` + `statvfs`，
macOS 走 `uname`。宿主名、时区、CPU 数、物理内存、可用 shell（真的在 PATH 里能找）都是实测值。
`GET /api/workspaces/:id/prompt` 可以随时看到当前 workspace 实际拼出的完整 system prompt。

---


---

← 回到 [README](../README.md) · 命令清单见 [使用手册](manual.md)
