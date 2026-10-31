# 工作区与运行时存储

## 工作区与运行时存储

每个工作区在自己目录下有一个 `.tea_agent_run/`，agent 的全部状态都在里面：

```
<workspace>/.tea_agent_run/
├── meta.json                 # 工作区元信息
├── logs/agent.log            # 该工作区日志
├── sessions/
│   ├── index.json            # 会话索引（标题/时间/条数）
│   └── <session_id>.json     # 完整会话（reasoning、工具调用与结果；分叉会话带 forked_from/fork_turn）
├── memory/memory.json        # 长期记忆（结构化条目）
├── agent.md                  # 本工作区的经验（注入 prompt，见下）
├── system_prompt.md          # 工作区级 system prompt 覆盖（空 = 用默认，见下）
├── scripts/                  # 可复用脚本，跨会话保留
├── merges.jsonl              # 合并记录（tools/merge_work_area.py 每次合并留一行）
├── trajectories/<sid>.jsonl  # 每轮轨迹，一轮一行（见 [轨迹](api.md#轨迹每一轮实际做了什么的审计记录)）
├── tmp/                      # toolkit_exec 的临时脚本
└── artifacts/                # 预留产物目录
```

### 并发：同一个工作区可以同时跑好几轮

每个进行中的轮次都有自己的 worker 线程，`turns` 表按 session id 索引，所以在同一台设备上
**不同会话可以真正并行**（实测三个会话同时对话，事件确实交错）。但上面这些状态文件几乎都是
「读文件 → 改一处 → 整份写回」，两个轮次同时写就会互相覆盖。修法是两条：

**1. 一个进程级的锁，护住所有 read-modify-write**

```cpp
// src/workspace.cpp
std::recursive_mutex& state_mutex();      // 进程级；recursive 是因为入口互相调用
using StateLock = std::lock_guard<std::recursive_mutex>;
```

`memory.json`、`sessions/index.json`、`settings.yaml`、`agent.md`、会话文件、workspaces.json
的写入点全部进这把锁。为什么是一把全局锁而不是每个 `Workspace` 一把：`Workspace` 对象很便宜、
每个请求都会新建，而且两个对象完全可能指向同一个目录——per-object 锁等于没锁。临界区只碰几 KB
JSON，相对一次模型请求可以忽略。

**2. 写入走「临时文件 + rename」**

```cpp
bool write_file_atomic(const std::string& path, const std::string& data, std::string* err);
// 写 <path>.tmp-<pid>-<n>，然后 rename 覆盖 <path>
```

`write_file` 原来是 `CREATE_ALWAYS`（先截断再写），并发读者可能读到半个 JSON，进程在中间
崩掉会整份丢失。rename 是原子的：读者要么看到旧的完整文件，要么看到新的完整文件。临时文件放在
目标旁边（rename 只在同一文件系统内原子，`%TEMP%` 经常是另一个盘）。

会话轨迹（`trajectories/*.jsonl`）另有一套：它是追加写的，为省 IO 会**原地刷新最后一行**，
所以读函数也拿同一把文件锁，避免读到写了一半的行。

**代价**：同一工作区的两轮在「落盘」这一刻会互相等几微秒，其余时间完全并行。

**3. 传输层也要跟得上：SSE 占用的是 HTTP 线程**

`cpp-httplib` 给每个请求分配一个线程，而一条 SSE 会一直占用它直到这一轮结束。线程池原来是写死的
32 个，于是"同时跟 32 个窗口"就等于"服务器不接受任何新请求"（实测 `/api/health` 超时）。
现在池子是 `ThreadPool(16, TEA_AGENT_HTTP_THREADS_MAX=128, 1024)`：基础 16 个常驻，按需增加，
空闲 3 秒回收；单个轮次的跟随者上限 8，同时在跑的轮次上限 24。写超时也从 10 分钟收到 2 分钟，
一条死连接不能扣着一个线程那么久。

**回归测试**：`python3 tests/smoke_concurrency.py`（13 项）。它同时发 24 个 memory 写入、
16 个会话创建，中间还挂着一轮正在跑的对话，最后核对「每个 200 都在盘上」「index.json 与
会话文件一一对应」「没有残留 `.tmp-*`」。用修复前的二进制跑会挂 4 项——24 个写入只剩 2 条。

### agent.md：工作区的「经验」，而不只是「事实」

`memory.json` 记的是**事实与偏好**（「这个项目用 yarn，不用 npm」）；`agent.md` 记的是
**怎么在这个工作区干活**，由 agent 自己维护：

- 踩过的坑：`- 2026-… cargo test 需要先导出 TEA_ARCH，否则链接失败`
- 可复用的命令/脚本：临时写的一次性验证脚本放进 `.tea_agent_run/scripts/`，把路径记在这里
- 项目约定：生成代码的位置、测试怎么跑、哪些目录别碰

它和 `instructions.md` 的区别是**谁写**：instructions 是人写给 agent 的硬规则，进程生命周期内
只读一次（保证 prompt 前缀稳定）；agent.md 是 agent 写给下一个会话的，每轮读取，所以同一轮里
`toolkit_agent_doc op=append` 之后，后续那次模型请求就已经能看到新条目。

默认模板给出四个小节（Overview / Commands & scripts worth reusing / Pitfalls / Conventions）。
两条硬约束：

- **有上限**：文件 32 KB，注入 prompt 的那部分再截到 6 KB。超过上限时 `op=append` 直接报错并
  要求用 `op=replace` 合并/删除过期条目，绝不静默把 prompt 撑大。
- **可人工维护**：内置 UI 的「记忆」页上方就是它的编辑框（带未保存提示与还原），
  `GET/PUT /api/workspaces/{id}/agent-doc` 也能直接读写；文件本身就是纯 Markdown，
  用编辑器改也行。

### system prompt：工作区可编辑的人设

`<workspace>/.tea_agent_run/system_prompt.md` 是该工作区的 system prompt 覆盖，和
`instructions.md` 分开：instructions 是**追加**的硬规则，这个文件则**整体替换**默认人设
（「You are tea_agent…」那一段）。环境探测、工作区指令、agent.md、记忆等小节仍然自动追加在
后面，所以它只决定「你是谁、按什么风格工作」。

优先级从具体到通用：

1. `<workspace>/.tea_agent_run/system_prompt.md`（每次请求重新读取）；
2. 该工作区 `settings.yaml` 的 `system_prompt`（打开工作区时加载）；
3. 机器级默认（`tea_agent.yaml` 的 `system_prompt`，否则内置规则）。

内置 UI 的「设置 → 系统提示词」提供编辑框、还原、恢复默认和「预览」（完整拼好的提示词），
`GET/PUT /api/workspaces/{id}/system-prompt` 也能读写。因为第 1 项每轮重新读取，**保存后
下一次模型请求就生效**，不用重启，也不用改会话。文件上限 64 KB，超限直接 400/报错，不会
悄悄撑大每一轮请求。

编辑框里显示的一直是**当前真正生效的人设**：有工作区覆盖就是覆盖内容，没有就是默认提示词
（不是空的输入框）。接口里对应的字段是 `current_prompt` 与 `source`
（`workspace` / `settings.yaml` / `builtin`），`content` 仍然是磁盘上那份覆盖（空 = 没有）。
因此：

- 打开面板就能看到模型此刻拿到的人设；改一个字保存即成为本工作区覆盖；
- 「恢复默认」把编辑框换回默认文本，保存后服务端发现内容与默认一致，就删除覆盖
  （`system_prompt.md` 变成空文件）——以后机器级默认改了，这里还会跟着变；
- 保存空白（或与默认完全相同的文本）同样等于「没有覆盖」，不会把今天的默认值钉死在
  工作区里。

### 只读的工作区：路径在，写不进去

工作区**能读**不等于**能用**：会话、日志、记忆都在 `<工作区>/.tea_agent_run/` 里。进程
被沙箱框住（只允许写某一个目录）、目录在只读挂载上、或者 ACL 里有拒绝项时，路径在、
内容读得到，但会话写不进去。

这种工作区在列表里会标出来（下拉里那一行写「只读」，悬停能看到原因）：

```bash
curl -s http://127.0.0.1:8282/api/workspaces | jq '.workspaces[] | {name, writable, write_error}'
# {"name":"proj","writable":true}
# {"name":"locked","writable":false,"write_error":"cannot create /srv/locked/.tea_agent_run: Windows error 5: 拒绝访问。"}
```

`writable` 是**真的写了一次**才知道的（在 `.tea_agent_run` 里建目录、试写一个临时文件），
不是猜的；`write_error` 带着操作系统自己的话。

添加工作区时如果写不进去，这条登记**照样加上**（路径本身是对的），但回答里带 `warning`，
把两件「以后才会出事」的事当场说清楚：

- `the workspace cannot be written to: …`：工作区登记成功，但里面的会话都会失败；
- `the workspace index could not be saved (…)`：索引 `workspaces.json` 没写成功，
  **这次登记重启后就没了**——换个能写的目录、或者给进程写权限，再重新添加一次。

回归：`python tests/smoke_workspace_writable.py`（6 项）、
`python tests/smoke_workspace_add_warning.py`（16 项）。

### tea home 写不进去：启动时就说

家目录是用户自己的，进程理应能往里写。但**沙箱**（或者低完整性级别的进程）会拦下来：
ACL 看着是「完全控制」，Windows 的强制性完整性控制却不许低完整性进程写高完整性对象，
报错是 `Windows error 5: 拒绝访问`。这种情况以前只有日志里一句 WARN，界面上完全看不出来。

现在启动时就会检查一次，并在横幅和控制台里直说：

```
warning: the tea home is not writable: C:\Users\Hetin\.tea_agent
         (cannot open for writing: ... (Windows error 5: 拒绝访问。))
         the workspace index and the global log cannot be written;
         point --tea-home at a writable directory, or start the agent
         with normal integrity (a low-integrity process may not write there).
```

- 横幅里多一行 `Tea home    : …   NOT WRITABLE: <原因>`；
- `GET /api/health` 带 `tea_home_writable` 与 `tea_home_error`，网页顶部挂一条横幅；
- **两条出路**：把 `--tea-home` 指到一个写得进去的目录（agent 的状态可以放在工作区里，
  例如 `<工作区>/.tea_agent_run/home`），或者用正常完整性级别启动这个进程。

### 写不进去的项目，照样能当工作区用

项目目录写不进去（沙箱、只读挂载、ACL 拒绝项）时，**运行数据不必跟着项目走**：
会话、日志、记忆、`agent.md` 会搬到 tea home 里的 `<tea_home>/workspaces/<工作区 id>/`，
这个工作区照常能开对话、能搜索、能记笔记。列表里那一行的悬停提示会写明运行数据在哪儿，
`GET /api/workspaces` 的 `run_dir` 字段也指向它（索引里记着，重启后不变）。

搬过去的是**新数据**：项目里原来那份 `.tea_agent_run` 留在原地，不会被自动搬走。

项目文件**本身**的写入仍然由操作系统说话——app 改不了那个（把标签降下来或者用正常
完整性级别运行进程才行）。

### 合并两个工作区：把一份 `.tea_agent_run` 并进另一份

工作区搬家（换机器、换盘），或者两台机器各干了一半想把历史收到一处时，**不能直接拷目录**：
目标里已经有自己的会话、自己的记忆，直接拷会把更新的那份覆盖掉。

`tools/merge_work_area.py` 做的是「合并」：

```bash
# 先看看会做什么（不碰任何文件）
python tools/merge_work_area.py /old/project/.tea_agent_run ./.tea_agent_run --dry-run

# 真合并
python tools/merge_work_area.py /old/project/.tea_agent_run ./.tea_agent_run
```

两个参数都是 `.tea_agent_run` 目录；给工作区根目录也行，脚本会自己找。合并的内容：

| 类别 | 合并方式 |
| --- | --- |
| `sessions/` | 会话文件拷过去，`index.json` 用两边的会话重建；**目标里已有的会话 id 会改名**（`--conflict skip` / `overwrite` 可换策略），分叉会话的 `forked_from` 跟着改成新 id，它的轨迹文件也一起改名 |
| `memory/memory.json` | 记忆条目追加；**同 id 或同文本的条目不重复** |
| `trajectories/<sid>.jsonl` | 轨迹按轮合并（同一个 `turn_id` 取新的一条），记录里的 `session_id` / `workspace_id` 改成目标工作区的 |
| `agent.md` | 按小节合并，**已有的行不重复**；超过 32 KB 上限时保持目标原样并报错 |
| `artifacts/` | 把合并进来的对话引用的图片拷过去（`--copy-all-artifacts` 拷整个目录） |
| `audit/` | 哈希链文件**换个名字整份拷过去**——每份文件是一条独立的链，拼链会断 |
| `logs/agent.log` | 追加到目标日志后面（带一行标记，重复跑不会追加两次） |
| `scripts/` | 目标没有的脚本才拷，不覆盖 |

几条保证：

- **只读源目录**：源 `.tea_agent_run` 一个字节都不动；
- **可重复执行**：第二遍是空操作（已存在的会话、记忆、行、图片都跳过），中断了直接重跑就行；
- **不覆盖目标已有内容**：`index.json`、`memory.json` 走「读-改-写」，目标自己的条目都留着；
- **坏掉的 `index.json` 不会让目标自己的会话消失**：索引读不出来时按会话文件重建；
- **有备份**：被替换的文件先复制到 `<to>/.merge-backups/<时间戳>/`（`--no-backup` 关闭）。

每次合并在 `<to>/merges.jsonl` 留一行（时间、来源、目标、合了几个会话）。
`--only sessions,memory` / `--skip audit,logs` 可以只合一部分，`--json` 打印机器可读的报告。

回归：`python tests/smoke_merge_work_area.py`（51 项）。

### 删除工作区：只是把它从列表里摘掉

左上角工作区下拉里，每一行右边有一个「✕」。点它会先弹一个确认框（写明是哪个路径、
**文件不会被删**），确认后：

- 该工作区从索引（tea home 里的 `workspaces.json`）里删掉，下拉列表和侧栏同步刷新；
- 删的是当前工作区时，页面自动落到剩下的第一个工作区；一个都不剩就回到「还没有工作区」
  的空态；
- **项目目录和其中的 `.tea_agent_run` 一个字节都不动**：会话、记忆、`agent.md` 都还在，
  以后重新添加同一个路径就能接着用。

真要把文件也删干净，用设置页的「危险操作 → 销毁…」（见下）。

```bash
# 同样的事，命令行也行
curl -X DELETE http://127.0.0.1:8282/api/workspaces/<id>
```

### 销毁：把这个 agent 留下的痕迹全部删掉

设置页最下方有「危险操作 → 销毁…」。确认框里会先列出**这台机器上具体要删哪些路径**，再问
一句「是否真的彻底删除痕迹？」，确认后删除：

- tea home（`$HOME/.tea_agent` 或 `--tea-home`）：`provider.yaml`、`tea_agent.yaml`、
  `workspaces.json`、全局日志；
- 索引里每个工作区的 `.tea_agent_run`：会话、轨迹、记忆、`agent.md`、`system_prompt.md`、
  `scripts/`、该工作区日志。

**项目目录本身不会被删**，只删其中的 `.tea_agent_run`。删除后进程回到「没有 provider.yaml」
的首次运行状态：`/api/health` 的 `config_loaded=false`，页面刷新后弹出上传配置的对话框，
内存里的工作区索引一并清空（不会因为一次 `last_used_at` 更新又把旧条目写回去）。

```bash
# 先看会删什么（不动文件）
curl -s http://127.0.0.1:8282/api/destroy | jq
# 再确认执行；不带 confirm 一律 400，有轮次在跑则 409
curl -s -X POST http://127.0.0.1:8282/api/destroy   -H 'Content-Type: application/json' -d '{"confirm":"destroy"}'
# {"ok":true,"tea_home":"/home/me/.tea_agent","removed":[…],"skipped":[…],"failed":[]}
```

两个实现细节值得一提：

- **先关日志句柄再删目录**。全局日志就在 tea home 里，Windows 不允许删除仍被打开的文件
  （`ERROR_ACCESS_DENIED`），POSIX 上删掉也只是「已 unlink」，空间要到进程退出才回来。所以
  `Logger::close_files()` 先关掉全局与所有工作区 sink；之后日志只走终端。
- **失败不中断，逐条报告**。某个目录删不掉（权限、占用）时其余目录照删，`failed[]` 里带上
  路径与原因，UI 会把它们列出来，而不是笼统说一句「删除失败」。

`$HOME/.tea_agent/`（可用 `--tea-home` 覆盖）只放三类东西，**不改动任何既有文件**，
因此可以和别的 agent 实现共用同一个配置目录：

| 文件 | 用途 |
| --- | --- |
| `provider.yaml` | provider / 模型 / api_key（只读） |
| `workspaces.json` | 工作区索引：id、名称、路径、创建/最近使用时间、会话数（也记着上次的可写标记，启动时会重新探测） |
| `logs/tea_agent.log` | 全局日志（>8MB 自动轮转） |
| `tea_agent.yaml` | 可选，C++ 版专属 settings 覆盖 |

### 导出会话：一个能拷走的 HTML 文件

对话页右上角的**导出**按钮（`GET /api/sessions/{sid}/export`）把当前会话写成一个
**自包含的 HTML 文件**：CSS 内联、图片内联成 `data:` URL、没有任何外部引用。把它拷到
U 盘或另一台电脑，双击即可在浏览器里离线阅读——那台机器上没装 tea_agent、也没有网络，
照样能看。

文件里有什么：

- 每条消息（用户 / 助手 / 工具）；Markdown 渲染成标题、列表、代码块、表格、引用；
- 模型的 `reasoning`、每次工具调用的参数、以及**所有工具输出**都折叠在 `<details>` 里，
  默认全部收起——导出的是一份可读的记录，不是把每个 `ls` 的结果铺满一屏；
- 收起时摘要仍说明「是什么」：工具输出那行带状态、字节数和首行预览（`输出 · 79 B ·
  [ 88%] Linking…`），调用那行带参数预览；要看全文点开即可；
- 工具输出单条超过 8 KB 会截断，并在原处注明原始字节数；
- 会话里所有图片：内联成 `data:` URL（单张上限 8 MB、总计 40 MB；超出的显示成说明文字，
  而不是把文件撑到几个 GB）；
- 页眉：工作区名与路径、会话 id、创建/更新时间、导出时的 tea_agent 版本；
- 会话的**任务列表**（如果有）：同样的进度条与三态标记，导出的记录因此能一眼看出做到哪一步；
- **本轮产出的文件清单**（文件名 / 类型 / 大小）。这是静态快照，不含文件本体，也不会写出
  指向本机的死链接——需要文件请从 Web UI 的「下载」取。

配色跟随系统的深浅色（`prefers-color-scheme`），两边都保证正文字号与对比度：所有颜色走
CSS 变量，深色只在 `:root` 上覆盖——早期版本把深色规则写在浅色规则之前，同优先级下后写的
浅色声明获胜，于是出现「深色页面 + 白底卡片 + 灰字几乎看不见」。`tests/ui_session_export.mjs`
现在会真的在两种配色下读取计算样式与对比度，把这条钉住。

两个安全细节：模型回答里的 HTML 一律转义（`<script>` 只显示成文字），链接只保留
`http(s)` / `mailto` / 锚点，`javascript:` 之类的退化成纯文本。

文件末尾还有一个 `<script type="application/json" id="tea-session-data">` 区块，装着完整
会话（含图片数据），所以同一个文件**还能导入回来**：会话列表旁的 **⤒** 按钮，或者

```bash
# 把导出的 .html 原样 POST 回去，恢复成一个新会话
curl -s -X POST http://127.0.0.1:8282/api/workspaces/<ws_id>/sessions/import \
  -H 'Content-Type: text/html' --data-binary @tea_agent-xxx.html
# {"ok":true,"session":{"id":"s_...","title":"..."},"messages":7}
```

导入总是**新建**会话（重复导入不会覆盖原会话）；图片会写进目标工作区的
`artifacts/images/` 并重写路径，所以在另一台机器上恢复出来的对话照样看得见图。也可以
只发会话 JSON（`{"messages":[...]}`）。

---


---

← 回到 [README](../README.md) · 命令清单见 [使用手册](manual.md)
