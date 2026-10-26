# tea_a2 开发笔记

> 这份文件在**仓库里**，因为这个项目会在不同的机器上被开发：一个坑踩过一次，就该留在这里，
> 别让下一个人（或者下一个会话）再踩一遍。
>
> 它只记**跟这个项目有关**的事：命令、平台的差异、这套代码里的陷阱、做事的约定。
> 一次做完一件有坑的事，就在这里加一段。
>
> 目录：
> [先看这十条](#先看这十条) ·
> [构建](#构建) ·
> [测试](#测试) ·
> [代码里的坑](#这套代码里的坑) ·
> [快捷通道](#首次运行快捷通道只填一个-api-key) ·
> [额度查询](#额度查询provider-级) ·
> [平台差异](#平台差异) ·
> [约定](#约定) ·
> [进行中](#进行中红着的测试)

---

## 先看这十条

1. **改完 `web/` 必须重建二进制**。Web UI 在构建期被嵌进二进制，e2e 会逐字节比对内嵌资源与
   `web/` 里的文件——只改文件不重建，那 3 项会挂，而且挂得莫名其妙。
2. **换行符不统一**。仓库里存的是 LF（`.gitattributes` 的 `* text=auto`），Windows 上签出是
   CRLF；但历史上有些文件是 LF（`src/env.h`、`src/env.cpp`、`src/server.h`）。**动手改之前先数一遍**，
   用 LF 片段去匹配 CRLF 文件会 0 命中。
3. **版本号只有一处真相**：`src/version.cpp` 里的 `kVersionString`（CMake 用正则从那里解析）。
   改版本 = 改它 + `docs/api.md`、`docs/build.md`、`docs/deploy.md`、`docs/source.md`、
   `docs/tea_a2_manual.md` 这 5 个文件里引用版本的地方。commit hash 不在源码里也不在二进制里，
   它是构建期产物 `tea_agent.buildinfo`（放在二进制旁边）。
4. **一个端口一个实例**。Windows 上 httplib 设了 `SO_REUSEADDR`，第二个实例会**静默**绑到同一个
   端口，流量被两个实例分走——浏览器可能连到错的那个，症状是「写不进某个工作区」。
5. **受限机器上 TEMP/TMP 要指到仓库里**（`set TMP=%CD%\.tmp`）。g++、python 的 tempfile、
   node 的 mkdtemp 都会因为写不了系统 `%TEMP%` 而失败（EPERM / "Permission denied"）。
6. **Windows 上 `python3` 是 Microsoft Store 的占位符**，`python3 -c` 会失败——用 `python`。
7. **别用 `git add -A`**。工作区里可能有**别的会话**暂存的东西；提交前先看 `git status` 的第一列，
   自己只 `git add` 明确的文件。
8. **运行中的 exe 覆盖不了**（Windows）。`cmake/free-running-exe.cmake` 会在链接前把旧 exe 改名
   让位、链接后删掉，所以「程序开着也能构建」。
9. **长构建要前台跑**。`start /b` / `start /min` 起的后台进程会随着命令结束被杀；用
   `cmd /c scripts\build.bat > log 2>&1` 前台跑，再单独去看 log。
10. **浏览器测试需要 `CHROME_BIN`**。没装 Chrome 时 Edge 也是 Chromium：
    `set CHROME_BIN=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`；
    同时 `TMP/TEMP` 仍要指到仓库内。

---

## 构建

| 平台 | 命令 | 备注 |
| --- | --- | --- |
| Windows / MinGW | `scripts\build-mingw.bat` | 脚本自己设 PATH 与 TEMP，并显式 configure |
| Windows / MSVC | `scripts\build.bat` | NMake（单线程，慢）；`vcenv.bat` 设 `VSLANG=1033` 让 cl.exe 用英文报 include |
| Linux / macOS | `cmake --preset posix && cmake --build build` | |
| 交叉（aarch64） | `cmake --preset arm64-linux` | 需要 aarch64-linux-gnu 工具链 |

- **不要**在没把 MinGW 工具链加进 PATH 时直接对 `build-mingw` 跑 `cmake --build`：改版本会触发
  重新配置，这时它找不到 `mingw32-make`，会把缓存里的 `CMAKE_MAKE_PROGRAM` 写成空，之后整个目录
  全量重编。用 `scripts\build-mingw.bat`。
- 改版本号只会重编 `src/version.cpp` + 链接（版本串在 .cpp 里，不在头文件里）。
- 编译中断留下的损坏 `.obj` 会报 **LNK1136 无效或损坏的 object 文件**：删掉对应的
  `build-msvc\CMakeFiles\tea_agent.dir\src\*.obj` 重编即可。
- 同一 commit 两次构建得到逐字节相同的二进制（MinGW `-Wl,--no-insert-timestamp` /
  MSVC `/Brepro`）。

---

## 测试

```bash
pwsh -File tests/e2e.ps1 -Exe build-mingw/bin/tea_agent.exe   # 端到端（HTTP/SSE/工具/资产）
bash scripts/test-all.sh            # 全部 python + 浏览器测试
bash scripts/test-all.sh smoke_todo # 只跑名字里含 smoke_todo 的
pwsh -File scripts/test-all.ps1     # Windows 上同一个
python tests/smoke_shell.py         # 单个冒烟测试
node tests/ui_shell.mjs             # 单个浏览器测试（需 CHROME_BIN）
```

- **已知的常失败**：`tests/e2e.ps1` 里有 5 项任务队列检查（`POST /api/tasks creates a task` 等）
  在本机恒失败——那是 e2e 的期望跟当前 `sched/task_server.py` 的返回结构对不上，与本仓库无关，
  别去追。
- 浏览器测试要 `TMP/TEMP` 指向仓库内，否则 node 的 `mkdtemp` 会 EPERM。
- 预置会话的 UI 测试必须同时写 `sessions/index.json`，否则新建/分叉第一个会话时索引重建会把
  预置会话从侧栏弄丢。

---

## 首次运行：快捷通道（只填一个 API key）

`POST /api/config/quickstart`（界面上是「设置 → 配置 → 快捷开始」）只要一个 API key，服务端就生成
一份**只含该 provider** 的 `provider.yaml` 并落盘。生成函数在 `src/server.cpp`：
`deepseek_config_yaml()` / `minimax_config_yaml()` / `mimo_config_yaml()`；落盘走
`apply_config_yaml()`——先写临时文件、用一个 scratch 配置校验，通过才换名，POSIX 上权限 `0600`
（文件里是 key）。key 不出现在任何响应里。

- **默认模型 = 第一个 provider 的第一个模型**（顺序即开关），`default_model:` 只是「provider 没有
  `models:` 列表」时的兜底。所以生成的文件里**不写** `default_model:`，也不写旧的 `roles:` 块
  （C++ 版根本不读 `roles:`，只在启动时打一条告警）。
- **生成的配置不查额度**（`supports_usage` 关着）——理由和那几个键的规则见下一节。
- **小米 MiMo 有两个网关，key 决定走哪个**：`sk-…` 是按量付费 `https://api.xiaomimimo.com/v1`，
  `tp-…` / `ttp-…` 是 Token Plan `https://token-plan-cn.xiaomimimo.com/v1`。两者**不能混用**
  （用错就是 401），所以快捷通道按 key 的形状自己选；显式传 `api_url` 仍然优先。默认模型
  `mimo-v2.6-flash`（1M 上下文 / 128K 输出），另附 `mimo-v2.6-pro`。
- **MiMo 的 v2.6 模型默认开启思考**，小米要求思考模式下把上一轮的 `reasoning_content` 原样带回去，
  所以生成的 yaml 里写 `reasoning_content: keep`。`auto` 其实也是带（只有 deepseek.com 例外），
  写出来是为了不依赖「哪个端点要」的猜测。
- **实测（2026-10-26，真实 Token Plan key）**：网关接受本程序发的 `max_tokens`（小米文档只写
  `max_completion_tokens`，但 OpenAI 兼容的旧名字它也收，所以 `src/llm.cpp` 不用改）；
  `reasoning_content` 带与不带都跑得通——小米文档说的 400 是指「历史里有的 assistant 消息带、
  有的不带」这种不一致，而本程序按 provider 统一策略，不会混。小米文档的机器可读版：
  <https://mimo.mi.com/llms.txt>（每页正文在 `/static/docs/....md`）。
- 测试：`tests/smoke_config_quickstart.py`、`tests/ui_setup_quickstart.mjs`。

---

## 额度查询（provider 级）

「订阅还剩多少」没有标准接口，每家的形状都不一样，所以按 provider **声明**而不是探测——探错了
就是把 key 发到一个不该去的地方。完整的键表在 `docs/config.md`，这里记代码里的规则：

- **`supports_usage: true`** = 这个 provider 有账号级的额度接口。**默认关**（关着就完全不查）。
- **写了 `usage_type:` 或 `usage_url:` 会自动把额度查询打开**（`src/config.cpp`），并告警一句
  「你写了 usage_type 却没写 supports_usage，已替你打开」。所以**生成**的 provider.yaml 里这两行
  只能待在**注释**里；`usage_console_url` 不发请求，可以照写。**上传**的 provider.yaml 不受这条
  限制——那是操作者写的，按原样生效。
- **`usage_auth`**：`bearer`（默认）/ `api-key` / `none`——key 怎么送出去（`api-key: <key>` 是
  MiMo Token Plan 的写法，`none` 是端点靠会话 cookie）。
- **`usage_type: json_fields` + `usage_fields`**：通用形状，点的路径 → 数字
  （`data.remainingPercent`）。另一个取值是 `minimax_token_plan`。
- **UI 三态**：有额度接口 → 顶栏「额度 NN%」；只有 `usage_console_url` → 「额度 ↗」（点开控制台，
  **不发请求**）；都没有 → 不显示。
- **与 `stream_include_usage` 是两回事**：那个是「在对话响应里附带 token 用量」，这个是「另外的
  账号接口，问还剩多少」。两个名字挨得近，所以代码里分开写了。
- **批量部署时别默认开着**：`supports_usage: true` 意味着**每台设备**都问一次，一个账号上成千
  上万个请求，这种模式最容易把账号弄进风控。快捷通道生成的配置一律关着。

---

## 这套代码里的坑

### 二进制读：别用 `read_file()` 读图片

`read_file()` 会把结尾那个「像半个 UTF-8 字符」的字节削掉（对文本是对的，JSON 层不接受非法
UTF-8）。但 JPEG 结尾是 `FF D9`，而 `0xD9` 正是 2 字节序列的前导符 → **副本少一个字节 → 坏图 →
模型回 `invalid params, 400` → 坏图留在会话历史里 → 之后每个请求都同样 400，整个会话锁死**。
图片路径一律用 `read_file_binary()`（不修剪）。

### 上下文超窗（长回合被 400 打死）

`HTTP 400 invalid params, context window exceeds limit` 有三条根因：

1. 压缩只在**回合开始**做一次，长回合（几十次工具调用）里没人检查压力；
2. 摘要请求只给 `max_tokens=800`，思考模型先把预算花在 `reasoning_content` 上、正文为空 →
   `compact_history` 判定失败 → 历史一条没折 → 原始 400 直接把回合打死（日志证据：
   `compaction: summarisation failed (200)`）；
3. 本地 token 估算太乐观（ASCII 4 字符/token、reasoning 打五折）。

**2026-10-26 已修**（`src/agent.cpp`）：迭代循环内加压力检查、思考模型摘要预算 4096 且
「只回 reasoning」时用 4 倍预算重试、估算改 3 字符/token + reasoning 全额 + 硬上限留 10% 余量、
折叠边界对齐到 user 消息、摘要彻底失败时退回丢弃最旧消息。钉住它的测试是
`tests/smoke_compaction_overflow.py`（10/10）。

**踩过的坑**：折叠边界的对齐写错过一格——`messages[keep_from - 1]` 是 user 消息时，代码会把
**那条 user 消息折进摘要**、把没有问题的回答留在窗口里，模型看到的是一个它没问过的回合。
对齐要看 `messages[keep_from]` 本身是不是 user 消息。

### 计划（toolkit_todo）是**这一轮**的脚手架

会话结束了，界面上还挂着一份没做完的 todo 列表——看着像「还有人没干完的活」，其实没人���做它。
所以计划在**每一轮结束时就被关掉**（做完没做完都关）：`session.todos` 清空，没做完的步骤
记进 `session.todo_carry`（会话文件里的一个数组），下一轮一开始把它们接回成这一轮的计划，
跨重启也还在（`fold` 回来时 prompt 里会写「these steps were left open when the previous
turn ended」）。

坑与取舍：

- **`save_todos` 与回合的 `save_session` 抢同一个列表**：回合手里的是自己的副本，别人（浏览器
  点一下、脚本写文件）在这期间改了列表。所以回合结束时算 carry 用的是**磁盘上最新的那份**
  （`Workspace::load_todos`），不是内存里的。
- **`PUT /api/sessions/:id/todos` 在两轮之间改的是 carry**：计划关着的时候列表是空的，改一条
  leftover 就是把计划接回来（`todos` 重新变成活的，carry 清空）。
- 导出的 HTML 与导入走的是「活着的计划，否则用 carry」，所以导出的快照里进度不会丢。

### 并发写状态

`workspace.cpp` 用进程级 `state_mutex()`（recursive，故意 leak）串起所有状态写入，写文件一律
`write_file_atomic`（同目录的 tmp + rename；rename 只在同盘才是原子的，所以 tmp 必须放在目标旁边）。
轨迹 jsonl 是原地刷最后一行，所以 `load_trajectories` 也要拿 `trajectory_file_mutex`。

### Windows 的 ConPTY（网页终端）

- `PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE` 的值是 **0x20016**（= 22 | INPUT），不是 0x40016；
  用错 `UpdateProcThreadAttribute` 直接失败。
- **必须显式把 `hStdInput/hStdOutput/hStdError` 设成 NULL 并加 `STARTF_USESTDHANDLES`**，否则子进程
  会继承服务端自己的控制台句柄，PowerShell 会把提示符打到服务端控制台上、永远不看 pty
  （`cmd.exe` 却正常，很容易误判成「ConPTY 坏了」）。
- `WindowsApps` 里的 `pwsh.exe` 是**应用执行别名（重解析点）**，它启动的打包应用看不到 ConPTY；
  选默认 shell 时要用 `GetFileAttributesA` 排除 `FILE_ATTRIBUTE_REPARSE_POINT`。
- 子进程退出后 conhost 仍握着输出管道，`PeekNamedPipe` 不会失败——读循环里要额外
  `WaitForSingleObject(process, 0)` 才知道 shell 已经没了。

### CMake 的字符串

`set(a "x" "y")` 会变成 `;` 分隔的**列表**，多行内容要用 `string(CONCAT ...)`。

### 审批闸门不在本仓库

用户看到的「执行」确认框来自**外层 Python 框架**（`tea_agent/tool_approval.py`），本仓库（C++ 这一份）
没有任何审批代码。白名单放在**进程启动目录**的 `.tea_agent_run/approval_allow.json`
（不是工作区！），环境变量 `TEA_APPROVAL_TOKEN` 最稳（会被子进程继承）。

---

## 平台差异

- **Windows**：路径是宽字符（`utf8_to_wide`），没有 fork（用 `_spawn`），终端是 ConPTY；
  符号链接需要 `CreateSymbolicLinkW(..., ALLOW_UNPRIVILEGED_CREATE)`，普通 token 里
  `New-Item -ItemType SymbolicLink` 不传那个标志就一定要管理员。
- **POSIX**：终端是 pty（`posix_openpt` + `fork` + `exec`）；SIGPIPE 的处理在 `net_compat.h`。
- **跨机器开发**：POSIX 那一半在 Windows 上编译不了时，可以用
  `scripts\posix-syntax-check.bat src\shell_posix.cpp`——它用 `scripts/posix_stub/` 的桩头做
  **-fsyntax-only** 解析，只能抓拼写/参数错，**不能代替真机跑**。

---

## 约定

- **一次一件事**：一个 commit 一件事；`chore: bump to X` 单独一个 commit；发版前核对文档里写的
  测试项数。
- **文档跟着代码走**：新增/改动接口 → `docs/api.md`；新增测试 → `docs/testing.md`；
  新增/改名源文件 → `docs/source.md`；改界面 → `docs/using.md`。
- **中文注释解释「为什么」，不是「是什么」**。
- **改文件的换行符按原样**（新文件用 LF，仓库里存 LF）。
- **提交前看 `git status` 的第一列**：工作区里可能有别的会话的改动，别一把 `git add -A`。
- 精确替换大文件不要手改：本地工作区里有 `.tea_agent_run/scripts/agent_edit.py`（按片段精确替换，
  每处必须命中一次）、`patch-block.py`（整体替换一个 JS 函数）、`replace-block.py`（替换两个标记行
  之间的块）。片段文件用 LF 写，脚本会自己保持目标的 CRLF/LF。

---

## 网页搜索与抓取（provider 级能力）

DeepSeek 的 `POST /v1/tools/{search,fetch}` 不是 OpenAI 标准协议，探测不了，所以**按 provider
声明**（`supports_search` / `supports_fetch`），默认全关——每次成功调用都按次计费
（各 5 元 / 1000 次）。

### 独立的 search provider（0.12.2 起）

`provider.yaml` 顶层多了一个 `search:` 段，指定**哪个 provider 提供 web 工具**：

```yaml
search:
  provider: DeepSeek      # providers: 里的一个名字
  always: false           # true = 所有 web 调用都走它（默认是「回退」）
```

为什么要有它：对话用哪个 provider 和搜索用哪个 provider **通常不是同一个账号**——本地模型
没有联网能力，而搜索要花钱。以前只能把能力挂在对话用的 provider 上，等于把两件事绑死。

- 解析与生效：`AppConfig::resolve_web_tool()`（config.cpp）——**本轮 provider 自己能搜就先用它**
  （便宜、同一套凭据），不能搜才走 `search:` 段；`always: true` 反过来。
- 工具表：`ToolRegistry::openai_tools()` 多收一个 `AppConfig*`，两个 web 工具在
  「本轮 provider 能」**或**「search provider 能（有 key）」时才出现。
- 调用：`tool_search` / `tool_fetch` 走解析出来的那个 provider——URL、key、端点路径全跟着换，
  工具结果 meta 里记 `provider`。
- 段写错（provider 不存在 / 没有 api_key）→ 启动时 WARN + 横幅 `Search : X (unusable: …)`，
  `/api/providers` 的 `search_provider.error` 里也有；web 工具保持关闭。

### 坑

- `search:` 段是**全局**的（一个文件一份），所以「不支持搜索的 provider 不给工具」这种断言
  只能在**单独的 agent 实例**里测——`tests/smoke_web_tools.py` 为此起了第二个 agent。
- 测「请求到底是谁应的」时，mock 要能分辨来源：两个 mock 端口，records 里记 port、
  chat 记录里塞 `_auth`/`_port`。

---

## 机群（多级协同）

学校场景：一台服务器 + 40 台设备，服务器和设备上都跑 `a2.service`。

### 三条下发的路子（别混用）

| 路子 | 通道 | 模型在哪 | 用在哪 |
| --- | --- | --- | --- |
| 确定性 | 服务器 → 设备 `POST /api/exec` | 不在 | 采集、升级、重启 |
| 机群编排 | 服务器 a2 → `toolkit_fleet` → 机群服务 → 设备 | 在服务器 | 「把 3 号机房升一下」 |
| 认知 | 机群服务 → 设备 `POST /api/chat` | **在设备** | 「这台机器不亮了，查查」 |

日常运维**不进模型**：40 台设备一次采集就是 40 次模型调用，慢、贵、还不确定。

### 设备侧新增

- `GET /api/status` —— 结构化快照（版本、运行时长、系统、CPU、内存、磁盘、工作区数）。
- `POST /api/exec` —— 跑一条命令，**不经过模型**，复用 `command.cpp` 的 `run_command`。
- `--api-token` / `TEA_AGENT_API_TOKEN` —— 机群通道的令牌。**没配令牌时 `/api/exec`
  根本不存在（404）**，默认安装不多出任何新的攻击面；配了就要带
  `Authorization: Bearer <token>`（或 `X-Api-Token`），比对是常数时间的。
  `/api/status` 是只读的，没令牌时也能读。

### 服务器侧新增（`sched/`，纯标准库）

- `sched/fleet.py` —— 机群引擎：台账、并发采集、批量作业（灰度/分批/失败阈值/回滚/校验）、
  审计日志、每日快照与日报。
- `sched/fleet_server.py` —— 把引擎变成 HTTP API，供服务器级 a2（`toolkit_fleet`）、
  机器人和定时任务调用。

### 坑（写的时候踩到的）

- **argparse 的 `dest` 会被子命令的同名选项覆盖**：`add_subparsers(dest="command")` 遇上
  `exec --command` 时，`args.command` 变成了选项的值（"echo"），程序以为没给子命令，
  只会打印帮助。子命令名要另起一个名字（`dest="subcommand"`）。
- **两个令牌是两回事**：「谁能调用机群服务」和「机群服务拿什么去访问设备」是**两个**
  秘密。合成一个的时候，机群服务会拿着自己的令牌去访问设备，设备全部 401，作业全挂。
  现在是 `--token` / `--device-token` 两个参数、两个文件。
- **模型接口默认是流式的**：mock provider 返回普通 JSON 的话，`/v1/chat/completions`
  的回答会被当成空流，工具调用根本不会发生。mock 要吐 `data: {...}` 的 SSE 块。
- **JSON schema 里的引号**：工具的 description 里写 `{"room": "A"}` 会让整个 schema
  解析失败，而且是在 ToolRegistry 构造函数里炸——agent 启动即死。描述里别写裸引号。

### 约定

- 批量作业**先金丝雀后全量**：金丝雀失败就别动其余设备。
- 升级**必须带回滚命令**；没有回滚的升级等于没有回滚。
- 采集**一台设备不回答只是一行错误**，不能拖垮其他 39 台（并发 + 逐台兜住异常）。
- 审计日志**只追加**，一行一个 JSON 对象。

---

## 进行中：红着的测试

下面三个测试是红的：前两个是**上一轮留下的规格**（测试在仓库里，但**对应的实现不在树里**，
那次会话的代码没落地），第三个是**测试自己的顺序问题**。跑它们会红——这是**已知状态**，不是新回归：

| 测试 | 现在 | 它钉住的东西（还没实现） |
| --- | --- | --- |
| `tests/smoke_port_guard.py` | 4/7 | 启动前独占端口（`SO_EXCLUSIVEADDRUSE`），被占就拒绝启动并指名占用者；`<tea_home>/tea_agent.port` 写 pid + 启动时间，退出时删掉 |
| `tests/smoke_workspace_writable.py` | 3/6 | 工作区列表要报 `writable`；写不进去的工作区是只读，并给出原因 |
| `tests/smoke_shell.py` | 28/38 | **实现是在的**（`/api/shell/session/{id}/upload` 与 `…/file` 都有），但测试先把会话 `exit` 掉了，再拿同一个 session id 去做上传/下载 → 全是 404。文件那几项要另开一个会话 |

要动手时先跑一遍它们：**前两个变绿，就是那件事做完了**；第三个要改的是测试本身。
