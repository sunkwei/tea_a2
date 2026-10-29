# tea_a2 开发笔记

> 这份文件在**仓库里**，因为这个项目会在不同的机器上被开发：一个坑踩过一次，就该留在这里，
> 别让下一个人（或者下一个会话）再踩一遍。
>
> 它只记**跟这个项目有关**的事：命令、平台的差异、这套代码里的陷阱、做事的约定。
> 一次做完一件有坑的事，就在这里加一段。
>
> 目录：
> [先看这十一条](#先看这十一条) ·
> [构建](#构建) ·
> [测试](#测试) ·
> [代码里的坑](#这套代码里的坑) ·
> [快捷通道](#首次运行快捷通道只填一个-api-key) ·
> [额度查询](#额度查询provider-级) ·
> [语音输入](#语音输入sensesvoicesmall) ·
> [MCP](#mcpmodel-context-protocolagent-当客户端) ·
> [平台差异](#平台差异) ·
> [约定](#约定) ·
> [进行中](#进行中红着的测试)

---

## 先看这十一条

1. **改完 `web/` 必须重建二进制**。Web UI 在构建期被嵌进二进制，e2e 会逐字节比对内嵌资源与
   `web/` 里的文件——只改文件不重建，那 3 项会挂，而且挂得莫名其妙。
2. **换行符不统一**。仓库里存的是 LF（`.gitattributes` 的 `* text=auto`），Windows 上签出是
   CRLF；但历史上有些文件是 LF（`src/env.h`、`src/env.cpp`、`src/server.h`）。**动手改之前先数一遍**，
   用 LF 片段去匹配 CRLF 文件会 0 命中。
3. **版本号只有一处真相**：`src/version.cpp` 里的 `kVersionString`（CMake 用正则从那里解析）。
   改版本 = 改它 + `docs/api.md`、`docs/build.md`、`docs/deploy.md`、`docs/source.md`、
   `docs/tea_a2_manual.md`、`docs/mcp.md` 这 6 个文件里引用版本的地方（CMakeLists.txt 错误
   提示里的字面量也要一起改）。commit hash 不在源码里也不在二进制里，
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
11. **接了 MCP 之后**：工具表是**每轮现算**的（`openai_tools()` 会去连服务器），所以启动
    横幅、任何不打算用工具的地方都要 `include_mcp=false`。细节见下面的「MCP」一节。

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

### 服务端繁忙（HTTP 529）：等，不是失败

MiniMax 整点高峰会直接拒绝请求：`HTTP 529: 当前为整点高峰时段，服务器短暂繁忙，通常 1-5
分钟内恢复`，body 里是 `{"type":"error","error":{"type":"overloaded_error",…,"http_code":"529"}}`。
同一个请求过一两分钟照样成功——**所以这一轮不该失败，该等**。

原来的重试只有 400ms / 800ms（`llm.cpp` 里的 `400 * (attempt + 1)`），对 529 毫无意义：
一分钟后才是同一堵墙。2026-10-27 改成两套预算：

- **hiccup**（连接断了、502、429）：照旧几百毫秒，重试 `max_retries` 次（默认 2）；
  provider 给了 `Retry-After` 就听它的。
- **overloaded**（529，或错误里带 `overloaded_error` / 「繁忙」/「高峰」）：按分钟退避
  （默认 60s → 120s → 240s，上限 5 分钟/次、10 分钟/次请求），重试 `overload_retries` 次
  （默认 3）。等待可被「停止」打断（每 50ms 查一次 cancel 标志）。

坑与取舍：

- **「繁忙」要认字，不能只看状态码**：有的网关 HTTP 200 + 流里一个 error 事件
  （`http_code` 是字符串 `"529"`）——状态码在错误对象里，`attempt_once` 得把它取出来，
  否则这次失败看起来像普通的 200，直接被当成致命错误。
- **已经吐过内容的请求不重试**：否则 provider 的回答会被贴两遍。
- **等待必须有上限**：`overload_max_total_ms`（默认 10 分钟）兜底，不然 provider 一直忙
  下去就会变成一场没有尽头的等待。
- 等待期间要**告诉用户在等什么**：`agent.cpp` 把 `RetryNotice` 变成一个 `retry` SSE 事件，
  界面显示带倒计时的提示（`web/app.js` 的 `retryNoticeText`）——否则几分钟的空白看起来
  就是卡死了。

设置项：`overload_retries` / `overload_backoff_ms` / `overload_max_backoff_ms` /
`overload_max_total_ms`（`settings:` 里，可按工作区覆盖）。钉住它的测试：
`tests/smoke_overload_retry.py`（14/14）、`tests/ui_retry_notice.mjs`。

### 中断：尽量结束，结束之后还要能继续

用户点「中断」时想要的是「尽快停下来」，但**停下之后不能把会话弄坏**。2026-10-27 之前
这两件事都没做到：

1. `toolkit_exec` 跑的命令**根本停不掉**——`run_command` 只看超时，不看取消标志。
   于是一条 `sleep 60` 让这一轮多活 60 秒，`{"interrupt":true}` 15 秒内等不到就返回
   `409 turn_still_running`。
2. 更要命的是**历史会坏**：工具循环里 `if (out.cancelled) break;` 只给**第一个**没跑的
   调用补了结果，同一批剩下的调用就悬着。悬着的调用 = 下一次请求 provider 直接拒
   （MiniMax：`invalid params, 400 (2013)`，「工具调用没有对应的结果」）。
   `message.cpp` 的 `repair_tool_calls()` 会在**发出去之前**把悬着的调用删掉，所以请求
   不会报错——但会话记录里那一段是残的，模型也不知道自己问过什么。

现在的做法：

- `CommandOptions::cancel`（`std::atomic<bool>*`）：`run_command` 的轮询循环（~30ms 一次）
  看到标志就杀**整个进程组**（SIGTERM → 300ms → SIGKILL），返回 `cancelled`、退出码 130。
  Windows 那边是 `TerminateJobObject`（job object 本来就是为超时准备的）。
- 工具循环**不再提前 break**：被取消的调用照常得到结果（内容是「cancelled by user…」），
  同一批剩下的调用也各自得到一份「没有执行」的结果——这样每个调用都有答案。
- 收尾还有一道保险：`close_dangling_tool_calls()` 扫一遍会话，把**任何**没有答案的调用
  补上（万一还有别的路径留下悬空调用）。补的时候写明「被中断，没有执行」。
- 被中断的工具结果会写清楚「命令被中断，可能只跑了一部分，它写过的东西还在」——
  这不是失败，是停在了半路，下一轮不能当成做完了。
- 界面上：工具行显示「已中断」（方块图标，不是红叉），轨迹徽标也是「已中断」；
  补齐了调用时多一条 `interrupted` 事件。

坑：

- **杀进程组，不是杀进程**：`command.cpp` 的子进程 `setsid()` 自己是组长，所以
  `kill(-pid, …)` 能连它 spawn 的孙子一起收掉；只杀 pid 会留下孤儿在后台跑。
- **别在中断后重发请求**：`repair_tool_calls()` 会静默删掉悬空的调用，看起来「没事」，
  但那是在**掩盖**问题。真正的修复是把结果补回去。
- 测试：`tests/smoke_interrupt.py`（13/13）、`tests/ui_interrupt.mjs`。

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

## 语音输入（SenseVoiceSmall）

界面上的麦克风按钮：录音 → 转写 → **文字填进输入框**（不自动发，转写会错）。

### 分工

| 谁 | 干什么 |
| --- | --- |
| 浏览器（`web/app.js`） | 录音、解码成 PCM、重采样到 16 kHz 单声道、编码成 WAV、POST |
| agent（`src/asr.cpp`） | 把 WAV 转成 `multipart/form-data`，转发给转写服务，取回 `text` |
| 转写服务（`sched/asr_server.py`） | 真的跑模型（sherpa-onnx + SenseVoiceSmall int8 ONNX） |

**为什么模型不在 C++ 里面**：sherpa-onnx 要拖进 onnxruntime（十几 MB），还要拖
tokens 和 240 MB 的模型文件——这个项目的规矩是「一个文件丢上设备就能跑」。所以模型
跑在**同机的一个小服务**里（`sched/` 里的 Python 伴生件，和任务队列一个位置），
agent 只当通道。

### 协议

`asr:` 段（provider.yaml 顶层，仿 `search:` 段）说服务在哪：

```yaml
asr:
  url: http://127.0.0.1:8284
  model: sense-voice
  api_key: sk-...        # 可选
  timeout_ms: 30000
```

接口用 OpenAI 的转写那套：`POST <url>/v1/audio/transcriptions`，multipart，
答 `{"text": "..."}`。所以**任何** OpenAI 兼容的服务都能换上去（云上的也行），
本地那个只是默认选项。

agent 自己的两个接口：`GET /api/asr/status`（配了没）和 `POST /api/asr/transcribe`
（请求体是原始 WAV 字节）。

### 整段，不是流式（做「伪实时」之前先读这段）

现在这套是**整段识别**：浏览器把整段 WAV 一次性 POST，服务端一次
`accept_waveform` + 一次 `decode_stream`，返回一个 `{"text": ...}`。

**SenseVoice 在 sherpa-onnx 里没有流式形态**：`OnlineRecognizer` 的 factory 只有
`from_nemo_ctc / from_paraformer / from_t_one_ctc / from_transducer / from_wenet_ctc /
from_zipformer2_ctc`——**没有 `from_sense_voice`**。所以用 sherpa 跑 SenseVoice
**只有整段这一种可能**，「边说边出字」得靠别的办法。

延迟不是问题（int8 实测，比实时快 **30 倍**：10 秒音频 → 0.325 秒）：

```
 1 s -> 0.046 s (21.5x)   5 s -> 0.168 s (29.7x)   10 s -> 0.325 s (30.8x)
```

整段的等待 = 说话时间 + 0.1~0.5 秒。

**要做「伪实时」的话**（分块累加重解码：每 1 秒重发已录的音频，服务端对累计音频
重新整段识别，返回最新文本）：

- 模型代价很小：15 次增量重解码，模型总耗时 3.9 秒（比实时便宜 10 倍以上），CPU 吃得下。
- **协议要变**：`POST /v1/audio/transcriptions` 现在是一次性契约，伪实时需要会话式接口
  （带 session id 的 partial 端点，或 chunked multipart）。
- 副作用：中间结果是「未完句」的猜测，文字会跳变（`系.` → `开饭时间。` →
  `开饭时间早上9点。` → …）。做草稿输入其实整段更合适（一次拿到正确完整的句子）。

交互现在也是**点一下开始/再点一下结束**（toggle），不是按住-松开；按住式要改
`pointerdown`/`pointerup` 代替 `click`。

### 坑

- **浏览器只在安全上下文里给麦克风**（https 或 localhost）。`http://192.168.x.x` 上
  `navigator.mediaDevices` 是 undefined，按钮因此不出现——**这是浏览器的规矩**，
  不是代码的 bug。测试里用 `http://127.0.0.1` 绕开。
- **MediaRecorder 给的是 webm/opus**，模型要 PCM。所以浏览器里要 `decodeAudioData`
  解回采样点、重采样到 16 kHz、再自己拼 44 字节 WAV 头。**服务端不做这个转换**
  （C++ 里塞一个音频库不值得）。
- **浏览器测试里的假麦克风**：容器里没有声卡，`getUserMedia` 会失败。用 Chrome 的
  `--use-fake-device-for-media-stream`（+ `--use-fake-ui-for-media-stream` 自动放行
  权限），它给一段合成的音调，**是真的音频**，`decodeAudioData` 能解。
  标志加在 `tests/chrome_pool.mjs` 和 `scripts/test-all.sh` 里。
  踩过的坑：自己用 `AudioContext` + 振荡器造流，**AudioContext 是挂起的**（没有
  用户手势），录出来只有 110 字节的头，解码直接 `EncodingError`——别自己造流。
- **侧车服务只听 127.0.0.1**：录音不该在没有认证的情况下暴露给局域网。
- **`sched/` 里其他文件都是纯标准库**，`asr_server.py` 是唯一的例外（它要
  `pip install sherpa-onnx` 才能真的跑推理）——因为它做的是真的神经网络推理。
- 模型从 ModelScope 拉（`fengge2024/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`），
  HuggingFace 那份是备选。int8 模型 239 MB，tokens.txt 316 KB。

---

## MCP（Model Context Protocol）：agent 当客户端

`provider.yaml` 顶层 `mcp:` 段里配一组**独立的程序**，agent 把它们启动起来、用 stdio 上的
JSON-RPC 跟它们说话，它们提供的工具就并进模型的工具表（`mcp__<服务器>__<工具>`）。

### 配置在**自己的文件**里

`mcp.yaml`，和 provider.yaml 同目录（`--config` 指到哪，它就在哪旁边）。
provider.yaml 装的是 API key，`mcp.yaml` 装的是「给模型装新手」的程序——两回事，
也不该互相牵扯。provider.yaml 里如果还留着旧的 `mcp:` 段，**不再被读**，只留一条警告
（`AppConfig::reload_mcp()`）。

### 分工

| 谁 | 干什么 |
| --- | --- |
| `src/config.cpp` | 读 `mcp.yaml`（`servers:` 可以是映射也可以是列表）；`AppConfig::reload_mcp()` |
| `src/server.cpp` | `POST /api/mcp/refresh` 会**先重新读一遍文件**再重连（改完文件点一下刷新就行）；文件坏了 → 400，正在跑的服务器不受影响 |
| `src/mcp.cpp` | 起进程、握手（initialize → notifications/initialized → tools/list）、调工具（tools/call） |
| `src/tools.cpp` | 把 MCP 工具并进 `openai_tools()`，`mcp__` 开头的调用分发给 MCP 注册表 |
| `src/server.cpp` | `/api/mcp/servers`、`/api/mcp/refresh`、`/api/mcp/servers/{n}/call` |

### 协议（stdio）

每条消息**一行 JSON**（MCP 的 stdio 传输不是 LSP 那种 `Content-Length:` 帧——但客户端
顺手也认那种帧）。握手固定四步：`initialize` → `notifications/initialized` → `tools/list`
→ `tools/call`。协议版本默认要 `2024-11-05`（.NET 的 ModelContextProtocol 0.3.0-preview.3
认这个）；服务器嫌版本老会报错，客户端会自动换 `2025-06-18` / `2025-03-26` 再试。

`tools/call` 的回答里 `content` 是内容块：`text` / `image`（base64）/ `resource`。
**图片会存进工作区并附在对话里**（跟 `toolkit_load_image` 一样），所以模型得声明
`supports_vision: true` 才看得到。

### 坑（写的时候踩到的）

- **服务器是按需启动的，但工具表是每轮现算的**：`openai_tools()` 会去连接服务器，
  所以**启动横幅不能带 MCP 工具**（否则一个连不上的服务器会把启动拖慢 20 秒）。
  `openai_tools(..., include_mcp=false)` 就是给横幅用的。
- **连不上的服务器一分钟内不重试**（`retry_after_ms`）：不然每一轮都要等它握手超时。
- **服务器发来的请求（sampling / roots）要回「方法未实现」**：不回的话服务器会一直等
  一个不会来的答案（.NET SDK 会卡住）。
- **读管道和关管道有顺序**：先杀进程（管道到头 → 读线程退出），**再 join 读线程**，
  **最后**关句柄。反过来做就是在另一个线程阻塞的句柄上关句柄（句柄值会被复用）。
- **函数名有 64 字符上限**（OpenAI 兼容）：`mcp__<服务器>__<工具>` 超了就截断加哈希。
- **配置文件坏了不能拖垮正在跑的东西**：`reload_mcp()` 解析失败时**保留旧配置**并返回
  错误，`/api/mcp/refresh` 因此返回 400 而不是把服务器全停了。
- **Windows 上 `python3` 是 Microsoft Store 的占位符**（见上面第 6 条）——测试脚本里用
  `sys.executable` 才靠谱。
- **`CreateProcessW` 传了环境块就必须带 `CREATE_UNICODE_ENVIRONMENT`**：环境块是按 UTF-16
  拼的，不带这个 flag 时 Windows 当成 ANSI 读，spawn 直接失败，报
  「Windows error 87: 参数错误」（error 87 = ERROR_INVALID_PARAMETER）。
- **YAML 里的 Windows 路径要用单引号**：`"C:\Tools\x.exe"`（双引号 + 双反斜杠）或者
  `'C:\Tools\x.exe'`（单引号更省心）。写成 `"C:\Tools"` 里的单反斜杠会被 YAML 当成转义
  序列（`\T` 是合法转义，`\m` 不是）→ 整个配置解析失败。

### 联调（Windows 上的 uiautomation-mcp）

`uiautomation-mcp` 是 .NET 写的 Windows UI Automation 服务器，只能在 Windows 上跑。
本机（wsl2）验不了它，所以仓库里给了两个 PowerShell 脚本：

- `scripts\mcp_probe.ps1`——**不经过 agent** 直接跟服务器说话（判断「服务器本身有没有问题」）
- `scripts\mcp-check.ps1`——查正在跑的 agent 的 MCP 状态、调一个工具

另外注意：**UI Automation 需要交互式桌面会话**（不能把 agent 跑成 session 0 的服务）。

### 测试

`tests/mock_mcp_server.py` 是一个假的 MCP 服务器（echo / add / picture 三个工具，
还能 `--crash` / `--garbage` 模拟坏掉的服务器）。`tests/smoke_mcp.py` 用它钉住整条链，
`tests/ui_mcp.mjs` 钉住设置页上的那张卡。

### 怎么在 wsl2 里验 Windows 的代码路径

WSL2 能直接跑 Windows 的 exe（互操作），所以**不用切到 Windows 就能验 Windows 那条路**：

```bash
# Windows 侧的程序和文件放在 /mnt/c 下
cd /mnt/c/temp/teatest
cp build-win64/bin/tea_agent.exe .
./tea_agent.exe --config 'C:\temp\teatest\provider.yaml' --port 8391 --workspace 'C:\temp\teatest\ws'
# 进程 cwd 是 UNC 路径 → Windows 程序看不到相对路径，参数一律写 Windows 绝对路径
```

- WSL → Windows 的 HTTP：Windows 侧监听 127.0.0.1，WSL 里 `curl 127.0.0.1:端口` 就能通。
- Windows → WSL 的 HTTP：WSL 里的服务要 `--host 0.0.0.0`（默认只听 127.0.0.1，Windows
  那边连不上），地址用 `hostname -I` 里的地址。
- **后台起的东西会随 shell 一起死**：要用 `setsid nohup ... &`；不然「curl 连不上」
  其实是对面进程已经没了。
- Windows 上没有 Python/Node 时，mock 服务器可以写成 **PowerShell**（`[Console]::In.ReadLine()`
  读行、`[Console]::Out.WriteLine()` 写行，记得 `[Console]::OutputEncoding = UTF8`）。

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

## 级联（0.16.0）：一台 tea_a2 控制另一台

**不用新协议。** HTTP/SSE 本来就是总线（`POST /api/chat` = 一台 agent 的完整入口），
缺的是「对端」这一层：名字、地址、令牌、工具、轨迹。

### 三样东西

| 东西 | 在哪 | 干什么 |
| --- | --- | --- |
| `peers:` 段 | settings（`provider.yaml` / `tea_agent.yaml` / 工作区 `settings.yaml`） | 对端的 name/url/token/workspace/description |
| `toolkit_peer` | `src/tools.cpp` | `list/health/status/chat/exec/tools/workspaces/stop` |
| `/api/peer/*` | `src/server.cpp`（pre-routing handler） | `/api/<路由>` 的**令牌版镜像**（白名单七条） |

### 为什么是镜像（而不是新路由）

对端已经有一整套路由了；再造一套只会漂。镜像 = 同一份实现、另一把锁：
`/api/peer/chat` → 改写 `req.path` 成 `/api/chat` → 走原来那个 handler。
**没配 `--api-token` 时 `/api/peer/*` 根本不存在（404）**，默认安装不多出攻击面。

### 坑（写的时候踩到的）

- **镜像路径别拼错**：`"/api/peer" + "/api/chat"` = `/api/peer/api/chat`（白名单不认 →
  404 → 工具退回普通路由 → 「级联没走令牌通道」但**看起来一切正常**）。用
  `peer_channel_path()`：剥掉 `/api` 前缀再拼 `/api/peer/`。
- **SSE 解析器每次尝试前必须 reset**。上一次尝试（404、被拒）留下的半行会粘到下一帧
  前面，那一帧的整行就不以 `data:` 开头 → **被静默丢掉**。症状：答案少了一帧、
  工具结果里没有 session id，而日志里那帧明明到过。
- **cpp-httplib 的 pre-routing handler 拿到的是 `const Request&`**，要改 `req.path` 得
  `const_cast`（那个对象其实不是 const：`Server::routing(Request&, …)` 传的是引用）。
- **写 mock 时对端和控制端不能共用类级状态**。共用 `seen` 计数 → 对端以为自己在第二轮
  → 直接答话不调工具 → 看起来「对端没有自己的脑子」，其实是你 mock 写错了。
- **令牌只从配置取**。模型给的是**名字**（或一个地址）；地址和某个已配置对端相同时才借用
  那一台的令牌。模型自己编的地址拿不到令牌——这是有意的。

### 约定

- **确定性的事用 `exec`，别用 `chat`**：`chat` 会烧对端的模型，`exec` 不会。
- **结果里必须带对端的轨迹**（它调了什么工具、哪一步失败）+ 对端的 session id。
  没有这些，上层模型只能相信对端的转述。
- **旧版本对端要能连**：对端没有镜像时 404 → 退回普通路由（工具里自动做，不要让用户配）。

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

---

## 坑：a2_task.service 偶尔 100% CPU（2026-10-28）

用户报「偶尔几次 `a2_task.service` 占满一个核」。查下来**不是死循环，是三处 O(N²)**：
队列的存储层是「一个任务一个文件」，几乎每个查询都是**全目录扫描**
（`os.listdir` + 逐个 `json.load`），而调用方把扫描放进了循环里。

### 三处（都在 `sched/task_server.py`）

1. **`Engine.round()`** —— 对**每个**排队任务问一次「这个项目忙不忙」
   （`running_count()` → `store.list(status=RUNNING)` → 全扫描）。
   「项目忙」的分支 `continue` **不增加 `started`**，所以 `--max-per-round` 的 break
   永远轮不到 → 队列里每个任务都扫一遍。O(Q×N)。
   实测：N=500 排队任务时一轮 **2.2 秒 > `--interval` 2 秒** → 调度器从此
   **永不停歇**（一轮跑完睡 2 秒，队列越长越追不上）→ 100% CPU 且自我加剧。
2. **`App.decorate()` → `queue_position()`** —— 每个排队任务再问一次「我排第几」
   （又一次全扫描）→ `GET /api/tasks` 是 O(Q×N)。实测 N=1000 时 **21.3 秒**；
   而 `sched/task_ui.html` 有 `setInterval(refresh, 5000)` → **队列页面开着就每 5 秒烧一次**。
3. **`Store.record_event()`** —— 每写一条事件，把该任务的**整份事件日志**读回来算
   `event_id` → O(M²)。实测单个任务 3000 条事件 = 5.2 秒，10000 条 = 59 秒。
   触发条件：agent 在任务里频繁 `toolkit_task` 汇报进度（每次汇报 publish 一次）。

### 帮凶

- **`Store.lock` 是全局 RLock，而 `list()` / `record_event()` 持锁做全部磁盘 IO**
  → 一次 21 秒的扫描把所有 HTTP 请求（健康检查、事件流、提交任务）全堵住。
- **日志风暴**：round 里「task %s waits: its project is busy」每个排队任务每 2 秒一行
  → 实测 20 秒 2893 行。
- **`Agent.chat()` 用 `timeout=None`** → SSE 流可以永久挂住，那个 running 任务就永远
  占着项目槽位 → 正好喂给 1。
- **SSE `_events` 的 `state["alive"]` 永远是 True**（没人把它置 False）→ 连接只靠
  写失败才退出；客户端半开时线程一直留着，每 0.3 秒醒一次。

### 改法（O(N²) → O(N)）

| 地方 | 改法 |
| --- | --- |
| `Store.list()` | 扫描**放到锁外**（写是原子的：mkstemp + `os.replace`，读者不会看到半条记录） |
| `Store.record_event()` | `event_id` **在内存里数**；进程第一次遇到某任务时**只读文件尾**（`_last_event_id()`） |
| `Engine.round()` | 一轮里 running **只算一次**（快照），启动的任务加进快照；「项目忙」改成**计数汇总**一行 |
| `App.decorate()` | `queue_position()` 接受一个 `order`（`App.queue_order()` 一次算好）；`GET /api/tasks` 整批复用 |
| `Agent.chat()` | 加 `--chat-timeout`（默认 3600 秒），超时算任务失败 |
| SSE `_events` | 加 `--sse-idle-timeout`（默认 3600 秒）+ `select()` peek 探测客户端断开 |

### 效果（同一个复现脚本）

| | 改前 | 改后 |
| --- | --- | --- |
| 调度器 CPU（600 排队 + 1 running，20 秒） | 66% | **1%** |
| 调度器 CPU（1000 排队） | 90% | **2%** |
| `GET /api/tasks`（1000 任务） | 21.3 秒 | **0.7 秒** |
| 一轮 round（800 任务） | 5.7 秒 | **0.02 秒** |
| 3000 条事件 | 5.6 秒 | **0.03 秒** |

### 教训

- **「一个文件一条记录」的存储层，配上「全目录扫描」的查询方式 = 队列一长就平方级。**
  凡是「对每个元素问一次集合」的地方，问之前先问一句：**这个答案能不能只算一次？**
- 写回归测试要**能证明它抓得住回归**：把旧版文件 `git show HEAD:sched/task_server.py`
  放到临时目录跑同一个测试，三项都 FAIL（5.66s / 6.75s / 5.61s）才算数。
  探针：`.tea_agent_run/scripts/task_cpu_probe.py`、`task_round_probe.py`、
  `task_events_probe.py`、`task_cpu_repro.py`（端到端复现，配 `mock_agent.py`）。
- 顺手改掉的 bug：`--interval` 的 help 写「default: 60」实际是 2；
  `Store.list()` 里 `name.endswith(".events.jsonl")` 是死条件（`.jsonl` 不以 `.json` 结尾）。
- 回归测试：`tests/smoke_task_queue_perf.py`（测工作量，不测形状：慢盘就是慢盘，
  但平方级在哪种盘上都是平方级）。

---

## 坑：知乎发文章（专栏）的接口（2026-10-28）

`sched/zhihu.py post article` 原来是 `raise SystemExit("发文章暂未实现")`。这次把它做出来了，
顺便加了 `post llm`（读最近看过的 → 让 LLM 分析 → 写一篇新的）。接口全是抓包确认的。

### 浏览历史没了

知乎的「浏览历史」**整条路都断了**（2026-10 实测）：

| 试过的 | 结果 |
| --- | --- |
| `GET /api/v4/history`、`/api/v4/history/list`、`/api/v4/browsing_histories`、`/api/v4/users/{t}/histories`、`/api/v4/me/histories` | 全 404 |
| `GET /api/v4/activities`、`/api/v4/users/{t}/activities` | 404 |
| `GET /api/v4/creators/publish/column`、`/api/v4/columns/{t}/articles` | 404 |
| 网页 `https://www.zhihu.com/history` | **404 页面**（浏览器里也是「你似乎来到了没有知识存在的荒原」） |
| `GET /api/v3/moments/{t}/activities` | 200，但那是**自己的动态**，不是读过什么 |

所以「最近读过的」只能从**收藏夹**（按收藏时间）和**本地语料**（`fetch` 抓过的）凑。

### 发文章的流程（抓包）

```
1) POST https://zhuanlan.zhihu.com/api/articles/drafts
   {"title":..., "content": <html>, "delta_time":0, "can_reward":false, "table_of_contents":false}
   → 响应里有 id（草稿 id）
2) POST https://www.zhihu.com/api/v4/content/publish
   {"action": "article", "data": {...}}
```

`data` 的字段（和**发回答**不一样，别照抄回答那份）：

| 字段 | 值 |
| --- | --- |
| `draft` | `{"disabled":1, "id":"<草稿 id>", "isPublished":false}` |
| `hybrid` | `{"html": <正文 html>, "textLength": <纯文本长度>}` ← **正文在这里**（文章没有 `content` 字段） |
| `title` | `{"title": "<标题>"}` |
| `extra_info.pc_business_params` | 一个 **JSON 字符串**：commentPermission/disclaimer_type/disclaimer_status/table_of_contents_enabled/**content**/**title**/commercial_report_info/commercial_zhitask_bind_info/canReward |
| `commentsPermission` | `{"comment_permission": "anyone"}` ← **文章是 anyone，回答是 all** |
| 其余 | `creationStatement`、`contentsTables`、`commercialReportInfo`、`appreciate`、`hybridInfo`（空对象）、`publish.traceId` |

- **`action` 是 `"article"`**（不是 bundle 里看到的 `"publish"`/`"update"`——那三个是 API 客户端的
  内部动作名，落到请求里的是 type）。
- `textLength` 是**纯文本**长度（去掉 HTML 标签），不是 HTML 长度。
- 正文 HTML 是 `<p>段落</p>` 连起来的片段（网页端编辑器存的就是这个）。

### zhuanlan 域不需要签名，www 域要

| 接口 | 域 | 签名 |
| --- | --- | --- |
| `POST /api/articles/drafts` | zhuanlan | **不要**（`zsAutoSignature:false`） |
| `GET /api/articles/my_drafts?limit=10&offset=0` | **zhuanlan** | **不要** |
| `DELETE /api/articles/{id}/draft` | zhuanlan | **不要** |
| `GET /api/v4/articles/my_drafts` | www | 要 x-zse-96 → **403** |
| `GET /api/v4/articles/my_drafts/count` | www | 不要（`{"count":3}`） |
| `POST /api/v4/content/publish` | www | 不要（cookie 就够） |

**坑**：草稿列表在 **zhuanlan** 域（`zhuanlan.zhihu.com/api/articles/my_drafts`），
在 www 域是 403。我先试 www 域绕了半天才发现。列表**不要**带 `include=$.data[*].schedule`
（带了反而 403）。

### 坑

- **时间戳两种格式**：知乎的 `created` 有时是 epoch（`1790003655`），有时是 ISO
  （`2026-09-21T23:14:15+08:00`）。`int()` 一下就把**整个来源块**炸掉了（异常在循环里，
  一条坏时间戳 = 后面全丢）。zhihu.py 里的 `to_epoch()` 两种都认（自己解析 ISO，别用
  `datetime.fromisoformat`——这仓库还支持 Python 3.6）。
- **本地语料别过 `entry_from_generic()`**：那函数认的是原始对象（description/excerpt/content），
  语料条目正文在 `text` 字段里 → 整条被丢掉。语料已经是条目了，直接用。
- **`llm_chat` 别把 `reasoning_content` 当答案**：deepseek-flash 这种模型会先思考，
  token 花完了 `content` 就是空的，老代码 `content or reasoning_content` 于是把**思考过程**
  当成回复返回（症状：模型回复是「The user wants a new Chinese hard sci-fi story...」）。
  改法：`content` 空就当空回复（报 finish_reason 和 token 用量），重试。
  另外**思考很吃 token**：创作正文要 `max_tokens=8000` 才够。
- **给模型的材料要每篇一份预算**，不是总量封顶：总量封顶的话前面两篇长文就把额度用完，
  后面八篇模型根本没见过——那还叫什么「分析最近读过的十篇」。
- **测试的 `check()` 别忘了记失败**：`if not ok: failures.append(name)` 漏了的话，
  测试会永远打印「22/22 checks passed」——我第一版就这样，藏了 2 个真失败。

### 工具

- `sched/zhihu.py post llm [--count N] [--provider] [--model] [--out] [--yes]`
  （默认只打印文稿，`--yes` 才投递；文稿写到 `~/.tea_agent/zhihu/draft.md`）
- 探针 `.tea_agent_run/scripts/zhihu_publish_capture.mjs`（**拦截并 abort** 发布请求，
  拿到 payload 但不真发）、`zhihu_drafts.py`（列出/清理草稿箱）

### 这条链路的验证到哪一步了

| 环节 | 状态 |
| --- | --- |
| `recent_reading`（收藏夹 + 本地语料） | 实跑过（真知乎账号） |
| LLM 分析 + 创作 | 实跑过（deepseek-flash，出了 2476 字的《授缰人》） |
| `POST zhuanlan.zhihu.com/api/articles/drafts` | **实跑过**（纯 requests，200 + 拿到草稿 id；试完立刻删了） |
| `POST www.zhihu.com/api/v4/content/publish` | payload 逐字段核对过（浏览器里拦截 abort 抓的），**但没有真发过** |

最后一步没有真发，是因为它会往用户的知乎账号里公开发一篇文章——那是人的决定，不是我的。

### 另一个坑：打包后 release/ 里多出来的散文件

0.16.0 打包时，`release/` 里除了 zip 还多出 `docs/`、`tasks/` 和三个二进制
（暂存目录内容的副本，时间戳和 cp 那一刻一样）。`git status` 显示成 `?? release/`，
很容易被 `git add release/` 一起提交进去。**打���完要检查 `release/` 里只剩那个 zip**。

原因没查清（每个命令的路径都是对着暂存目录写的），但结论是可靠的：
`ls -la release/` 之后应该是「一个 zip」的状态。

---

## 坑：任务做完时发到聊天端的消息不是完整答案（2026-10-29）

用户报「测试发现实际收到的内容相比完整的最终回复，差了很多」。查下来不是模型的问题，
是通知那一行取错了字段。

### 根因

`sched/channel.py` 的 `notify_event()`：

```python
text = "做完了 %s\n%s" % (task_id, (task.get("note") or (task.get("result") or "")[:300]))
```

- **`note` 排在前面**：那是模型汇报进度的**一句话**（`toolkit_task` 的 note 参数）。
- **模型是被明确要求汇报进度的**：`src/agent.cpp` 的 `build_system_prompt()` 里，
  `task_context` 非空就加一段「This turn works on a QUEUED TASK: … Report on the task
  with toolkit_task (op=update): progress=0-100 and a one-line note of what you just did」。
  → 所以排队任务的记录里**几乎总有 note** → 通知发出去的就是那句「我正在做 X」。
- 没有 note 时才用 `result`，而且**只有前 300 字**。

用真实队列里的 8 个已完成任务量了一下：修复前发出 57～209 字（全是 note），
完整答案是 233～1894 字。

### 修法

- `notify_event()`：**只发最后的完整答案**（`result`），没有答案才退回那句 note
  （不再把 note 当脚注附在后面——用户要的是「仅最后的完整消息」）。
- `task_server.py` 那层**不用改**：`result` 存的就是 agent 的 `out.content`
  （`agent.cpp` 的 `if (!chat.content.empty()) out.content = chat.content;` —— 最后一个
  有内容的轮次 = 最后的完整消息），多轮任务（confirm 后继续）也是后一轮覆盖前一轮，
  正好是「最后的」。实测记录里的 result 是 1600/1894 字这种完整答案 ✓。
- **长答案分段发**（不截断）：`chunks()` 从 `telegram_bot.py` 挪到 `channel.py` 共用；
  Telegram 每段 3800 字（原来就有），**微信原来是一条整发** → 现在也按 1800 字分段，
  每段带同一个 `context_token`。

### 教训

- 「模型汇报进度」和「模型给出答案」是两个字段，通知要的是后者。**别让进度占住答案的位置。**
- 平台限制要分段解决，**不要用「截前 N 字」糊弄**——那是静默丢答案。
- 测试要钉住「完整答案发出去」这件事本身（`tests/smoke_wechat_bot.py` /
  `tests/smoke_telegram_bot.py` 各加了四条：完整答案、note 不顶替答案、长答案分段、
  每个字都到）。

## 与其他应用对接：watcher（2026-10-29）

tea_a2 是被动的，缺的从来不是「再加一个内置功能」，而是**触发**和**出口**。三个方向
（它调别人 = MCP、别人调它 = HTTP/级联、别人那边发生事 = watcher）在
`docs/integrations.md` 里；落地的是第三个：`sched/mail_watch.py`（Gmail/任意 IMAP）。

### 形状

`fetch_new()` 拿新东西 + 书签 → `judge()` 一次 `/api/llm/chat` 出结构化 JSON →
`apply_decision()` 动邮箱 → digest 留痕 → **只在 `needs_me` 时** `notify()` →
`follow_up` 非空就排一个队给 tea_a2。

### 几个刻意的取舍

- **不发信**。最后一步是往草稿箱 `APPEND`（带 `In-Reply-To`/`References`，所以是同一线程的回复），
  发不发由人按。自动发信不是待办功能，是边界。
- **判不准就打扰**：模型不给 JSON / 空回复 / agent 连不上 → `needs_me=True, action=keep`，
  留在收件箱、通知人。任何「猜一个 archive」的兜底都是静默丢信。
- **Gmail 标签用 `X-GM-LABELS`**：归档就是 `-X-GM-LABELS (\Inbox)`（不删、不 COPY）；
  没有这个扩展的服务器退回「建文件夹 + COPY + \Deleted」。`archive()` 里两条路。
- **UIDVALIDITY 变了一切作废**（Gmail 会重置）；`n:*` 搜索在 n 超过最大 uid 时会返回最后一封，
  所以 `uid > last` 要自己再筛一遍。这两条不写就会重复处理老信。
- **`imaplib` 的日期不吃 locale**：`strftime("%d-%b-%Y")` 在 zh_CN 下给「26-10月-2026」，
  服务器直接 NO。要自己拼英文月份（`imap_date()`）。
- **dry-run 不推进书签**（否则真跑时那几封会被当成处理过）；**每次动作前都记账**，
  通知失败也能从 digest 里查。
- **单实例锁**（pid 文件，进程死了会被接管），否则两个 watcher 会把同一封信处理两遍。
- 判读借 agent 的 `/api/llm/chat`，不自己读 provider.yaml —— 换模型只改一个键，
  思考型 provider 的空正文也有 agent 那边兜着。

### 测试怎么钉（`tests/smoke_mail_watch.py`，95 条）

- **假 IMAP 服务器是真的协议对话**：CAPABILITY / LOGIN / SELECT（UIDVALIDITY）/
  UID SEARCH / UID FETCH（literal，imap 的 `_command` 在 `self.literal` 上拼 `{n}`）/
  UID STORE（`X-GM-LABELS` 加与减）/ APPEND（`+ Ready for literal data` 之后才发正文）。
  这样「标签贴没贴上、信移没移出收件箱、草稿是不是回复」是按服务器记录验的。
- 假 agent / 假队列 / 假 Telegram / 假 webhook 一个本地 `ThreadingHTTPServer` 全包
  （路径分流 `bot*`、`/hook`、`/api/tasks`、`/api/llm/chat`）。
- 陷阱：`BaseHTTPRequestHandler` 忘了继承 → `MockWorld() takes no arguments`；
  Telegram 的域要可配（`telegram_api`），否则测试会去打真的 api.telegram.org（这台机器无网时
  每封等 20 秒超时）。

### 还缺的（排在 docs/integrations.md 文末）

队列没有「只通知不干活」的记录类型（watcher 只好自己发通知，网页队列里看不到）；没有
`watch.py` 基类（第二个源落地时再抽）；agent 侧没有 `callback_url`；Gmail 只有应用专用密码
（Workspace 关了这条通道就得写 XOAUTH2）；回信之后没有标签闭环。
