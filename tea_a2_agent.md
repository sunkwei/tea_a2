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
[模型配置页](#模型配置页设置页的模型配置) ·
[文件页的预览](#文件页的预览右边那块) ·
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

### 配好 provider 之后：没有工作区就把 `$HOME` 顶上

快捷通道和上传（`POST /api/config/quickstart` / `POST /api/config`）成功后，如果这台机器
**一个工作区都没有**，服务端就把 **`$HOME` 注册成一个工作区**（`ensure_default_workspace()`，
`src/server.cpp`）：对话、文件、记忆都要有个落脚的地方，默认就用家目录，省得第一次用还要
先去建一个目录。

- **只在索引为空时做**：已经注册过的工作区一个都不动，也不会在旁边再加一个家目录。
  （「重置 provider」走的就是同一个上传路由，所以重置后行为一样：有就不动，没有就补一个。）
- 落进 `workspaces.json`，所以重启后还在；`.tea_agent_run/` 当场建出来（跟 CLI 一样），
  第一次对话就有地方写。
- 响应里多一个 `workspace` 字段（新工作区的 JSON）；没建就不带这个字段——前端不用猜。
- 家目录用不了（不是目录、建 `.tea_agent_run` 失败）**不算失败**：配置照样生效，
  前端照旧让人自己加工作区。
- `$HOME` 是 `home_dir()` 读出来的（POSIX 读 `HOME`，Windows 读 `USERPROFILE`）。
- 测试：`tests/smoke_default_workspace.py`（上传、快捷通道、重启后还在、已有工作区不动）。

### 路径可以写成 `$HOME`

注册工作区（`POST /api/workspaces`、`--workspace`）时，路径里的 `~` / `$HOME` / `${HOME}`
（Windows 上还有 `%USERPROFILE%`）会展开成**服务端那台机器**的用户目录。

- 展开在 `expand_user()`（`src/platform_fs.cpp`）里做，`expand_home_vars()` 是新加的那一段：
  手工扫一遍字符串，`${HOME}` 优先，然后 `$HOME`——**后面必须不是变量名的一部分**
  （`$HOMEDIR/x` 是另一个变量，相对路径，仍然拒绝）。
- 值一律取 `home_dir()`：POSIX 上它就是 `$HOME`；Windows 上取 `USERPROFILE`
  （Git-Bash 的 `HOME=/c/Users/me` 不是 agent 能打开的路径）。
- 因为 `expand_user()` 本来就被 `WorkspaceRegistry::add()` 调用，所以**命令行、HTTP 接口、
  界面表单是同一套规则**，索引里存的也是展开后的绝对路径。
- 界面那一侧（`web/app.js` 的 `looksAbsolutePath()`）本来只认盘符/UNC/`/` 开头，
  `$HOME/...` 会被当相对路径挡下来——所以客户端也得改，两边要一起。
- 测试：`tests/smoke_workspace_home_var.py`(20) + `tests/ui_workspace_home_var.mjs`(7)。

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

**2026-10-30 已修**（`src/util.h` / `src/platform_fs.cpp`）：`read_file_binary()` 这次真的存在了，
图片这条路上全换过去——`toolkit_load_image` 存副本、agent 把图编成 data: URL、导出 HTML 内联
图片、`/raw` 给浏览器送字节、归档打包，统统按字节读，一个字节都不削。

那次的现场：微信工作区里「搜索 DL20 推理卡的相关介绍」那个会话，加载了一张
`analysis_terminal_01.jpg`（51150 字节，结尾 `FF D9`）。副本存成了 51149 字节——少了最后一个
字节。MiniMax 收到这张坏图，整个请求被拒：`invalid params, 400 (2013)`。

**而且它的拒绝是时灵时不灵的**：2026-10-30 在 api.minimax.cn 实测，同一张坏图发 10 次，5 次
被拒、5 次通过；完好的图发 10 次全过。所以「这个会话续不上」不是每轮都死，而是**看运气**——
坏图留在会话历史里，每一轮都要重发它，那一轮就有大约一半的机会被拒。截图里那句
`request_id` 就是这么来的。

### provider 拒了带图的请求：丢图再试，别锁死会话

坏图已经躺在会话历史里的时候，光修代码救不回那个会话——历史里那张图还在，每一轮都会被拒。
所以请求被拒时（HTTP 400）如果带着图，就把图从请求里摘掉再试一次：这一轮继续，只是模型这一
次看不到图（`src/agent.cpp` 的 `strip_image_parts`；日志里一行 `the picture(s) are dropped and
the request is retried without them`）。会话因此还能接着用——这比「每个请求都 400」强得多。

注意：**摘图只是止血**，模型这一轮真的看不见那张图。坏图要另外修（把完好的源文件重新拷进
`.tea_agent_run/artifacts/images/`），下一轮模型才又能看见。

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

## 模型配置页（设置页的「模型配置」）

参考 deepseek harness 的模型配置界面做的：设置页每个 provider 卡片上多了一个
**「模型配置」**按钮，打开一个对话框，一个模型一行，点开就能改。

改的东西：上下文长度、输出长度、思考强度（off/minimal/low/medium/high/xhigh/max）、
温度、top_p、top_k、频率与存在惩罚、随机种子，外加「能不能用工具 / 思考 / 看图」和备注。
还能加模型、删模型、把某个模型设成默认模型（= `models:` 里的第一个）。

**几个设计决定，别改回去：**

- **「没设」和「设成 0」是两回事。** 每个采样参数都有一个 `has_*` 标志：没设的
  参数**不进请求**——网关不认识的字段会回 400。设成 0 会进请求。
  `ModelInfo` 里的 `has_temperature` / `has_top_p` / `has_top_k` / `has_seed` 就是干这个的。
- **`reasoning_effort` 以前是解析了但从不发出去的**（`agent.cpp` 里
  `request.send_reasoning_effort = false` 写死了）。现在发了，但有三重闸：
  值不是 `auto`、模型自己声明了 `supports_reasoning: true`、全局设置没有更优先的值。
  一个不会思考的模型不该被要求思考。
- **`auto` 的思考强度不发表。** 全局 `settings.reasoning_effort` 是**兜底**：模型自己的
  值是 `auto` 或空的时候才用它。
- **写文件有两个接口，语义不同**：
  `POST /api/providers/{name}/models` = **换掉整个列表**（「更新模型」：问 provider 有哪些模型，
  确认后整份替换）；`PUT /api/providers/{name}/models` = **就地改**（「模型配置」：没提到的
  模型留着、没提到的字段保持原值、`null` 清掉、`remove` 删掉、`default` 挪到第一位）。
  两个都只改这个 provider 的 `models:` 块，别的一个字节都不动。
- **容量能写 `128K` / `1M`**，文件里存的是数字。界面上那两个框是 `type=text` 不是
  `type=number`——数字框写不进 `K` / `M`，浏览器会把值清空。

**踩过的坑：**

- `input.list = 'x'` 在现代浏览器里是**只读 getter**，会抛
  `TypeError: Cannot set property list`。用 `setAttribute('list', …)`。
- 滚动容器（`max-height` + `overflow:auto`）里的 flex 子项会被**压扁**
  （默认 `flex-shrink:1`），展开的行只剩一个标签。写 `flex:none`。
- 行对象（entry）和 DOM 节点是两回事：按钮的回调里改的是 entry 上的字段
  （`entry.removed` / `entry.isDefault`），不是 DOM 节点上的属性。写错了保存时
  一点反应都没有。

钉住它的测试：`tests/smoke_model_config.py`（55 条，含「这些参数真的进了请求」）和
`tests/ui_model_config.mjs`（25 条，浏览器里点一遍）。

---

## 文件页的预览（右边那块）

在左边的目录里点一个文件，右边**立刻**显示它：文本上色、图片直接看、markdown 渲染、
二进制文件说「预览不了」并给一个下载按钮。右上角的「编辑 / 预览」在文本文件上一直可用。

**踩过的坑（这个 bug 藏了很久）：**

`web/index.html` 里 `<div class="files-grid">` 写了两遍。HTML 解析器不报错，它把多出来的
那个 `</div>` 悄悄补到别处，于是**外层 grid 只有一个子元素** → 它落进第一列（280px）→
内层 grid 只剩 0px → 右边那块 `.file-pane` 实际宽度是 **2px**（只剩两条边框）。
表现是：文件能打开、状态栏写着「未修改」、但右边**什么都没有**。

- **教训：改了 HTML 结构，数一遍 div 的开合**（`grep -c '<div'` vs `'</div>'`，或者按缩进
  打印深度）。浏览器的容错机制会把不平衡的标签藏起来，症状出现在几层之外。
- 症状和原因隔得很远的时候，先量一下尺寸：`getBoundingClientRect().width` 直接告诉你
  哪块塌了（这里 764 vs 2）。

**几个决定：**

- **默认是预览，不是编辑。** 点一下文件是「看」，改东西要点「编辑」——文件浏览器的老规矩。
- **没设的语言不瞎猜**：扩展名对不上就纯文本。高亮错了比不高亮难看。
- **大文件不高亮**（> 200 KB）：一屏几十万个 span 会把浏览器拖垮。
- **二进制由服务端说**（`/file` 的 `binary` 字段：头 8 KB 里有 NUL 就算），前端不自己猜。

钉住它的测试：`tests/ui_file_preview.mjs`（19 条，浏览器里点一遍）。

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

---

## 工作区搜索（0.17.0）：完全字符串匹配

在**工作区里**搜文件名和文件内容。三处共用同一套核心（`Workspace::search()`，
`src/workspace.cpp`）：

| 位置 | 入口 |
| --- | --- |
| 网页「文件」页的搜索框 | `GET /api/workspaces/{id}/search`（`src/server.cpp`） |
| agent 的工具 | `toolkit_workspace_search`（`src/tools.cpp`） |
| 脚本 | 同一个 HTTP 接口（`docs/api.md` 有字段表） |

### 语义：**完全字符串匹配**，没有别的花样

查询串**按原样逐字节比较**——不是正则、不是通配符、不是模糊匹配。查 `.*` 找的是字面量
`.*`。`exact=1` 是「完全匹配」：路径（或文件名）必须整串相等，内容则是整行相等。
默认大小写不敏感，**只折 ASCII 大写**（折非 ASCII 需要 locale，折错比不折更糟）。

这是刻意的取舍：搜索是给人用的，猜错意图比找不到更让人火大；正则和模糊以后想加再加，
现在先把「完全匹配」这一档做扎实。

### 坑（改这个功能前先看）

- **走查必须有上限，而且上限要说出来**：最多访问 10000 个文件、深度 12、单次读 32 MB 文本、
  单文件 > 2 MB 只搜文件名、二进制文件不搜内容。撞到上限时 `truncated` / `files_skipped`
  为 true，界面和工具输出都要写明「还有更多」——**结果不完整时不能装作这就是全部**。
- **不能走出工作区**：`path=` 参数要过 `resolve_path()` + `path_is_within(root_)`，
  越界直接不搜（不是报错，是空结果）。
- **`.tea_agent_run` / `node_modules` / `.git` 不参与搜索**：里面全是会话、日志和依赖，
  搜它们只会把真结果淹掉。agent 自己的 `agent.md` 用 `toolkit_agent_doc` 读。
- **二进制判定**：先看扩展名（省得把 40 MB 视频读进内存），再看内容前 8 KB 里有没有 NUL。
- **改完 `web/` 要重建二进制**，Web UI 是构建期嵌进去的（见本文件开头第 1 条）。
- 测试：`tests/smoke_workspace_search.py`（28 项，含工具那一半）、
  `tests/ui_workspace_search.mjs`（浏览器那一半：输入框、结果、点开文件、完全匹配、返回目录）。

---

---

## 历史搜索（0.17.0，对话 + 轨迹）：`toolkit_history_search`

搜「这个工作区里发生过什么」：会话标题、说过的话、想过的、模型调用过的工具，以及
**轨迹**（命令的输出在那里）。跟「搜文件」是两回事，两套核心、各自一个接口：

| 搜什么 | 核心 | 接口 | 界面入口 |
| --- | --- | --- | --- |
| 文件名 + 文件内容 | `Workspace::search()` | `GET /api/workspaces/{id}/search` | 「文件」页的搜索框 |
| 历史对话 + 轨迹 | `Workspace::search_history()` | `GET /api/workspaces/{id}/sessions/search` | 侧栏「会话」下面的搜索框 |

### 坑

- **会话的内容在会话文件里，不在 index.json 里**。`list_sessions()` 走 index.json 时只有
  标题和时间；搜内容必须**逐个读 `<run_dir>/sessions/<id>.json`**（`load_session()`）。
  所以这个搜索是 O(会话数 × 会话大小)，必须卡上限：最多 200 个会话、单会话 > 4 MB
  只读标题、每会话最多 5 条片段。撞到上限时 `truncated` / `sessions_skipped` 为 true，
  界面和工具输出都要说「还有更多」——**结果不完整时不能装作这就是全部**。
- **轨迹是命令输出的所在地**：会话文件里有 tool 消息（工具的输出），轨迹里另外记了
  **工具名 + 原始参数 + 输出**（每轮一行 jsonl，`<run_dir>/trajectories/<id>.jsonl`）。
  只在命令输出里出现过的词，靠搜轨迹才找得到。
- **`num()` 只认 number**（`web/app.js`）：`dataset.mid` 是字符串，`num()` 会把它变成 0。
  从搜索结果跳到某条消息时要用 `parseInt` 自己解析，否则永远落在最后一条用户消息上。
- **助手消息没有 id**：只有用户消息有 `data-mid`。所以「跳到命中的那一条」落在**它之前
  最近的一条用户消息**上（那正是这一轮的开头），再给它加一个 `.hit` 类闪一下。
- 改完 `web/` 要重建二进制（Web UI 是构建期嵌进去的）。
- 测试：`tests/smoke_history_search.py`（31 项，含工具那一半）、
  `tests/ui_history_search.mjs`（浏览器那一半：结果、标粗、跳到落点、Esc 返回）。

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

## Router 与 Watchdog（2026-11-01 落地，M0~M5 全做完）

DeepSeek Harness 团队 2026-10-31 发了《Super-agent 设计探索》，里面有两个角色我们原来
**完全没有**：把人和任务连起来的 **Router**（全局上下文 + 分派 + 对外说话），和在外面盯着
Worker 的 **Watchdog**（外部控制回路）。现在都落地了：
**触发器入口**（`sched/triggers.py`）、**Router**（`sched/router.py`）、**看门狗**
（`sched/watchdog.py`），使用文档在 [docs/router-watchdog.md](docs/router-watchdog.md)。

改这块时记住这几件事：

1. **Watchdog 默认不动手**。默认动作只有 escalate（告警），`--auto` 才允许 fail / retry，
   而且每次动作都写审计。误杀一个正在跑的长任务，代价比晚发现大得多。
   判「卡住」要**双阈值**：`updated_at` 过了 **且** `progress` 没变——只看时间会误杀正在
   慢慢干活的任务。
2. **别动 agent 内核**。Worker 还是现在这个 agent、session 还是隔离的；Router 和 Watchdog
   都在**外面**。Watchdog 必须是**独立进程**：它要是跑在 agent 里，一个卡死的 Worker 就能
   把它一起关掉。
3. **线索的记录由队列写，router 只做「要判断或要花钱」的事**（收拢摘要、说话）。
   这样 router 挂了线索照样记——只是没人收拢、没人说话。
4. **模型只产建议，不动手**：看门狗问模型「跑偏了没有」，模型说跑偏也只是**说一声**，
   动作仍然由规则决定。

可以复用的零件（别重写）：`sched/channel.py` 的 `Queue`（队列 API 客户端）、
agent 的 `POST /api/llm/chat`（一次模型调用，判读用）、agent 的
`GET /api/sessions/{id}/trajectory`（轨迹，判「跑偏了没有」用）、队列记录里已有的
`updated_at` / `progress` / `note`、机群 `run_job()` 的金丝雀+验证+回滚。

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

## 机制层与策略层：a2 与 a2_task 的分工（2026-11-04 评估）

一份对 8282 上 a2 0.18.5 的接口评估（`~/Downloads/tea_a2-8282-mechanism-gaps.md`）问的是
「a2 能不能当机制层」。我们的答案是：**能，而且两层早就天然成型了**——
a2 = 通道（回合、工具、文件、轨迹），a2_task = 队列（什么时候干、干哪件、要不要问人）。
完整评估与扩展清单在 [docs/mechanism-strategy.md](docs/mechanism-strategy.md)。

改这两块之前先记住四件事：

1. **先定前提**：a2_task 与 a2 **同机同用户**——它能 `os.system`、能直接写工作区文件，
   所以 a2 的围栏**对策略层没有强制力**。要么承认 a2 是「记账 + 审计的执行器」
   （围栏约束的是**模型**，推荐），要么把策略层关进容器/换用户（那是一次架构改造，不是加接口）。
2. **a2 的回合循环没有上界**：`src/agent.cpp:1308` 是 `for (int iteration = 0; ; ++iteration)`。
   `agent.cpp:2089` 那句「迭代上限」的注释是**误导**——真正的自动停止是「同样的输出连着 3 次」。
   任何"跑一轮最多花多少"的需求，现在都得由机制层加硬上限（`limits`），别靠客户端超时。
3. **任务与轨迹现在是断的**：`src/trajectory.cpp` 不记 `task_id`；队列的 `_consume()` 在
   `done` 帧里只取 `content`，把 `trajectory_id` / `usage` / `metrics` 全丢了。
   所以看门狗只能去猜"最近两轮"（`sched/watchdog.py:315`）。
   **补 `task_id` 进轨迹 + 队列记 `turn_id`/`usage` 是最便宜的两处改动。**
4. **围栏漏了命令工具**：`allow_paths` 只管文件工具，`toolkit_exec` 完全不受限
   （`docs/tasks.md` 里写明了）。给任务开的「额外目录」授权，对能执行任意命令的模型形同虚设。

下一步的三步（句柄 → 幂等/能力 → 审批/围栏）与验收标准都在那份文档里。

### 2026-11-04：回合句柄 + 异步提交（M1，0.19.0）

机制层现在有**句柄**了：调用方可以把一轮交出去，然后走开。

- `POST /api/chat {"async":true}` → **202** + `{"turn_id":"t_…","session_id":"s_…",
  "state":"running","status_url":…,"events_url":…}`。**不传 `async` 就还是老样子**
  （一条 SSE 流，行为一个字没变）——这条是硬要求，别为了统一把老客户端改了。
- `GET /api/turns/{id}` → 状态：`running` / `done` / `failed` / `stopped` / **`budget`**
  （撞到预算**不是失败**），外加 `result` / `usage` / `metrics` / `iterations` /
  `stop_reason` / `files_changed` / `elapsed_ms`。
- `GET /api/turns/{id}/events?since=` → 回放 + 直播；**轮次跑完了也能读**（缓冲区还在）。
- `POST /api/turns/{id}/stop` → 精确停这一轮（不用 session_id）。
- `GET /api/turns` → 最近的句柄（最新的在前）。

**句柄就是审计 id**：`turn_id` 与 `done` 事件里的 `trajectory_id`、轨迹文件里的 `turn_id`
是**同一个字符串**（`TrajectoryRecorder::begin()` 多收一个 `turn_id`，agent 从
`TurnOptions::turn_id` 传进去）。所以「这一轮怎么样了」与「这一轮干了什么」用同一个 id 查，
调用方不用记两套 id。

几个当时踩到的点：

- **句柄要在轮次开始前就有**，所以不能等轨迹生成——`ChatTurn` 自己先生成 id，
  再通过 `TurnOptions::turn_id` 交给 recorder。两边共用一个 id 的代价是：
  `turn_id` 必须是**能当文件名用**的（`trajectory_id_is_safe()`）：它是 URL 的一段，
  也是轨迹文件里的一行。
- **跑完的轮次也得能读**：`add_subscriber()` 原来一看到 `closed` 就拒绝（那是给
  「接回正在跑的轮次」用的）。事件流要读**已完成**轮次的回放缓冲区，所以加了
  `replay_only` 参数：只放开「已完成」这一种情况，人数上限照旧。
- **别在锁里读文件**：`handle_json()` 里的 `files_changed` 是从轨迹文件读的，
  读文件放在 `mu` 外面——worker 每发一帧都要拿这把锁，卡在磁盘上会把 agent 拖住。
- 状态是从**事件**里推出来的（`observe_event()` 看 `done` / `error` / `stopped`），
  不是从 `cancel` 标志推的：`stopped` 是「被人停了」，`failed` 是「出错」，
  `budget` 是「撞到预算」，三件事在轨迹里也是分开记的。
- 客户端（`sched/a2_ext_py/client.py`）多了 `submit()` / `turn_status()` /
  `turn_events()` / `stop_turn()`；`chat()` 与 `submit()` 共用 `_chat_body()`，
  免得两条路慢慢长歪。
- 测试：`tests/smoke_turn_handle.py`（36 项）。

### 2026-11-04：队列改「提交 + 订阅」（S1，0.19.0）

队列（`sched/task_server.py`）不再「一个任务占一条 SSE 撑到底」。现在是一轮**交出去**
（`POST /api/chat {"async":true}` → 拿 `turn_id`），再**订**它的帧
（`GET /api/turns/{id}/events?since=`）。三个好处：

1. **断线不再等于失败**：订的过程断了，先问 `GET /api/turns/{id}`，再从最后一个 `seq`
   接回去；真的结束了（done/failed/stopped/budget）就从状态里把结论取回来写进记录。
2. **队列重启能认领**：任务记录里存着 `turn_id`，新起的队列看到「running 的旧记录」
   会去问 agent 那一轮怎么样——还在跑就**接回**它，已经跑完就把结论写回记录，
   agent 也不认得了才判失败。老记录（没有 `turn_id`）照旧判失败。
3. **超时停的是那一轮**：到点先 `POST /api/turns/{id}/stop` 再判失败——不留一个没人看的
   轮次继续烧 token（这正是 M1 要解决的问题）。看门狗判死任务时也一样（`fail_task()`）。

几个当时踩到的点：

- **`log()` 只收一个参数**（`sched/task_server.py:182`）！我按别处的习惯写了
  `log("...", a, b)` → `TypeError`，任务直接判失败。这个文件里**本来就有两处**这样写的
  （`start_task()` 里两条只在冷路径上走的），顺手改了。**改这个文件时注意**。
- **重启恢复不能靠「记录旧不旧」**：原来 `recover_stale_tasks()` 用 `stale_after`（默认
  300 秒）判断「没人管了」，结果重启后 5 分钟内根本不碰那条记录——正好把「认领」这件事
  废掉了。改成**启动时 `min_age=0`**：这个进程刚起来，不可能是它自己在做的那件事。
  （另一个实例正在做的任务被接走也不要紧：两边都订同一轮，**先到的结论算数**
  —— `_finalize()` 本来就有这个保护。）
- **别在锁里读文件**、**读状态要在锁外**这类老规矩继续适用。
- **老 agent 要能配**：队列可能指着没升级的设备（0.18.x）。按 `GET /api/health` 的
  `version` 判断（≥ 0.19.0 才走新路），否则照旧走「一条 SSE 撑到底」的老路——
  两套路径共用同一个事件处理函数（`_on_event()`），别写成两份。
- 测试：`tests/smoke_task_turn_handle.py`（24 项：句柄在记录上、重启被接回、
  轮次已完时从状态取结论、超时停轮次不留孤儿、老 agent 照旧能用）。

### 2026-11-06：请求 id、错误目录、能力清单（M5 + M7，0.19.2）

三件让**程序**（而不是人）用得上接口的事：

1. **`X-Request-Id`**：每个回答都带。调用方自己带的**回显**（可以自己编 trace id），
   没带的生成一个（`req_` 开头）。同一个 id 出现在**错误体**和**每一帧 SSE** 里——
   所以「客户端说它卡住了」可以用一个字符串在日志里查到底。
2. **错误目录**（`GET /api/errors`）：每个错误带 `{code, http, retryable, retry_after,
   meaning, hint}`。`retryable` 是给程序的（「能不能再来一次」），429 一律带
   `Retry-After`。
3. **能力清单**（`GET /api/capabilities`）：`features` 里的每个键都是实打实的开关。
   队列**读它降级**（`features.async_turns` 为 false 就走老路），读不到就用版本号兜底。

几个当时踩到的点：

- **cpp-httplib 只有一个 `pre_routing` 钩子，后注册的会顶掉先注册的**！我把
  「给每个请求发 id」注册成 pre-routing，结果**完全不生效**——因为对端通道
  （`/api/peer/*` 的令牌门）早就注册了一个，在它后面注册就把我的顶掉了。
  现在两件事合成一个函数 `pre_route()`（id + 令牌门），只注册一次。
  **症状是「代码明明写了却不生效」，日志里一行都没有**——查这个要记得先怀疑钩子。
- **`Request::headers` 是 const 的**：`req.headers.insert(...)` 编不过。请求 id 因此
  放在**响应**上（`res.set_header`），处理函数再从 `res` 读回来（`response_request_id()`）。
- **错误码目录要按 (状态, 码) 配对，不能只按码**：`config_missing` 既是 404（根本没有
  provider.yaml）又是 409（没东西可替换），`session_error` / `write_failed` /
  `no_active_turn` / `images_not_queued` 同理。按码建 map 会**静默丢掉**其中一条
  （map 的重复键保留第一个）——所以目录是一个 **vector**，每条自带状态。
- 目录对着源码核：`tests/smoke_error_catalog.py` 读 `src/server.cpp` 里的
  `send_error(res, 状态, "码")`，两边集合必须**完全一样**（少一条 = 目录有洞，
  多一条 = 目录里有个发不出去的条目）。
- 测试：`tests/smoke_error_catalog.py`（30 项）。

### 2026-11-06：幂等键——同一个请求问两次，就是一个请求（M4，0.19.3）

会重试的调用方（队列、看门狗、信号不好的手机）最怕**重试把活儿做两遍**：多烧一次钱，
多跑一遍命令。所以写请求可以带 **`Idempotency-Key`**（头里或 body 的 `idempotency_key`
字段里都行）。

- **同键同请求** → **同一个答案**（同一个 `turn_id`），活儿只做一次。流式那条路也一样：
  重试**接回**已经在跑的那一轮，不是再开一轮。
- **同键不同请求** → `409 idempotency_conflict`（拿错了钥匙：照着回答会给调用方别人的答案）。
- **不带键** → 一切照旧。
- 答案记在 **tea home**（`<tea_home>/idempotency/`，与 `workspaces.json` 同级），**重启不丢**。
- **只记 2xx**：失败的那次必须能重试——重试得还能再来一次。

实现上的几处：

- **答案要在发出去之前就记住**（`send_json` 里顺手记，不是在路由末尾），否则「刚发出去
  就重试」的那一瞬间会做两遍。`IdempotencyContext` 握着 store 的锁，从「查」到
  「记住」是同一把锁——两个同键请求不能都开工。
- **键不能当路径用**：文件名是键的 **sha256**（`src/sha256.cpp`，自己写的，因为
  mbedtls 不是每个平台都有，而指纹必须**跨平台一致**：一个机器写的文件另一个机器要读）。
- **只记 2xx**：失败的那次不记，不然重试会被钉死在第一次的失败上。
- **死掉的轮次要重做**：agent 重启后，记住的那一轮已经没了 → 重试**再做一遍**（活儿本来
  就没了，重做是对的），而不是把一个死掉的 `turn_id` 交回去。
- **队列那一侧的键是 `task:<任务号>:<第几轮>`**：第几轮是**这一轮**的序号（记录里的
  `turn_seq`），**不是重试次数**——我一开始用了 attempt，结果「等人回答后继续做」的
  那一轮拿到了同一个键，agent 把上一轮的答案重放了（任务卡住不往下走）。测试抓到的。
- 测试：`tests/smoke_idempotency.py`（21 项）。

### 2026-11-06：exec 围栏 + 额度当预算 + 事件桥接（M3 / S5 / S6，0.20.0）

剩下的三件，一起做掉：

**M3 exec 围栏**（命令工具终于有围栏了）。文件工具一直有（工作区 + `allow_paths`），
命令工具没有——一轮被限制了目录，照样能跑一条碰别处的命令。现在
`POST /api/chat` 收 `policy:{"exec":{"allow":[…],"deny":[…]}}`：
`deny` **先**看（命中的命令**根本不跑**，工具回一句「策略不许」），`allow` 非空时清单外
都不跑，模式是**前缀**（`"npm *"` 匹配 `npm install`，**不**匹配 `sudo npm install`）。
**不给 policy 就不围**——开放语义不变，老用法什么都不受影响。

**S5 额度当预算**。额度以前只是「开工前看一眼」。队列多了 `--quota-tokens`（100% 的额度
值多少 token）与 `--quota-share`（一个任务最多用剩下的多少），剩下的额度就换算成**这一轮
能花多少**（`max_total_tokens`）随任务下发。**百分比本身换不成 token**——「100% 值多少」
得操作者自己说，所以这个换算是**显式**的，不配就还是老样子（只当闸门）。

**S6 事件桥接**。机制层的事件也上队列自己的 `/api/events`：`turn_done` /
`turn_error` / `turn_stopped` / `budget_exhausted` / `tool_error`（带 `turn_id` 和任务
记录）。看门狗、router、网页**只订一处**，而且顺序是定的：**先** agent 的视角，
**后**队列的判决（以前两套事件各说各的，谁先谁后没人知道）。

几个坑：

- **`tool_exec` 在匿名 namespace 里**（`src/tools.cpp` 开头就 `namespace {`）：我把它旁边
  新加的 `exec_command_allowed()` 也写在了匿名 namespace 里 → 头文件里那个声明和它
  **同名同签名** → 调用时 "ambiguous"。公共的函数要放在 `namespace tea` 里（匿名 namespace
  里的工具函数只能内部用）。
- **围栏要在跑命令之前判**，不是在跑完看结果——被拒的命令**根本没跑**（测试用一个
  「删掉某个文件」的命令验证文件还在）。
- 队列的幂等键用「第几轮」（`turn_seq`）而不是 attempt 仍然是关键：**换了请求就是换了
  活儿**，同键不同请求会被机制层 409 掉。

测试：`tests/smoke_exec_fence.py`（10 项）、`tests/smoke_queue_policy.py`（9 项）。

### 2026-11-06：需求书剩下的六条（0.21.0）

`docs/pending_task.md`（那份「LLM Agent 任务排队接口需求说明书」的对照表）里最后六条都做完了。

**版本段**（第 1 条）：`/v1/agent-tasks...` 都有了，**旧路径 `/api/tasks...` 照样能用**
——加的是版本段，不是搬家。实现上只有一层：`_versioned(path)` 把新路径**翻译**成旧路径，
后面还是同一套 handler。队列客户端、队列网页、看门狗都改成了新路径。

**检查点与接着做**（第 2 条）：任务记录里存着**检查点**（这一轮的句柄、会话、轨迹）。
agent **不能**把跑到一半的循环存起来——但**会话在**：轮次没了而会话还在时，任务**可以**
回到队列里接着做（同一会话里的新的一轮）。

**但这件事默认不做**（0.21.1 起）：要 `--resume-on-restart` 才接着做，默认是**判失败**并
写明原因。理由：一轮被切断的活儿**可能已经做了一半**（写了一半文件、改了别的东西），
没人叫你别重跑，那就别重跑——自动重跑比停下来更危险。

**异步导出 + 生成物按 id 下载**（第 3 条）：`POST .../export` 起一个导出任务（202 +
`export_id`），`GET /v1/exports/{id}` 看进度，做完了下载（带有效期）。生成物每个有**自己
的 id**（路径的哈希——路径改名，id 不变，下载链接就一直有效）。

**交互的编号与期限**（第 4 条）：问题有 `interaction_id` 与 `expires_at`；回答要带上那个
id（对不上 → 409）；`--interaction-timeout` 到了没人回答 → 判失败。

**request_id / trace_id、限流、保留**（第 5 条）：调用方自己的 `X-Request-Id` **回显**
（信封与头里都是）；每个任务有 `trace_id`；`--rate-limit-per-minute`（按钥匙）→ 429 +
`Retry-After`；`--retention-days` → 旧记录自动清掉。

**租户与身份**（第 6 条）：钥匙带租户与身份（`POST /v1/projects` 领钥匙时写），
任务记录里写着；`X-Tenant-Id` 与钥匙的租户对不上 → 403。

几个坑：

- **`Engine` 里没有 `self.app`**（只有 `self.args`）：我在 Engine 的方法里写
  `self.app.args` → AttributeError → 那个方法里的更新**没写进记录**，而异常被
  「订阅断了就重订」的逻辑吃掉，任务表面上还跑完了——**测试才发现**（记录里的
  `interaction` 是空的）。**Engine 与 Handler 的字段不一样**。
- **钥匙的格式变了**：一个项目现在可以有多把钥匙（每个租户/身份一把），所以
  `projects.json` 里从 `{"key": ...}` 变成 `{"keys": [{...}]}`。**读的时候要认老格式**
  （老钥匙不能因为升级而作废）。
- **导出拆成了「造字节」与「送出去」两半**（`_export_bytes`），同步的那条路与异步的
  导出任务才能共用同一份实现——不然两份代码很快会长歪。

测试：`tests/smoke_queue_spec.py`（42 项）。

### 2026-11-06：队列也有 /api/doc（跟 agent 同一个思路）

agent 早就有 `GET /api/doc`（服务 docs/api.md，构建期嵌进二进制）。队列现在也有：

- `GET /api/doc` → 网页；`?format=md` → Markdown；`?format=json` → 版本 + 全文 + 渲染好的页面；
- **服务的就是 `docs/tasks.md`**（队列自己的文档）——不是第二份拷贝，开发者写的那份就是读者看的那份；
- 队列把 **docs/ 整个目录**都服务出来（`/api/doc/tea_a2_manual.md`），页面里的相对链接
  会重写成 `/api/doc/<名字>`，**点得通**；
- 队列是 Python 脚本，本来就住在仓库里，所以**不用嵌进二进制**——要就现读。

**坑**：

- **别依赖 pip 装的 `markdown` 包**：发版包里没有它，用户那边会 ImportError（本机装了 3.4.1，
  但发版清单里没有）。所以自己写了个小渲染器（标题/代码块/表格/列表/引用/行内标记）。
- **`_doc()` 的参数**：路由按路径给名字（`/api/doc/xxx.md`），一开始只在查询参数里读，
  结果 handler 抛 TypeError → **连接被直接掐掉**（客户端看到的是「远端关闭连接」，不是 500）。
- 顺手修了**文档里的坏链接**：`manual.md` → `tea_a2_manual.md`（文件改名时链接没跟上，
  10 个文件都指着它），以及 `sched/README.md` 里跨目录的 `fleet.md`。

**队列的版本号该报自己的**：先改成「跟着 agent 走」（同包发布），但那是**问错了问题**——
客户端问队列「你认不认识 /v1/agent-tasks」，答案得是**队列自己的**版本，不是它哥们的。
（agent 与队列同包发布，但 agent 可能还没重启 → 报出来的是旧版本。）于是：队列报自己的
`QUEUE_VERSION`，agent 的版本另开一个字段 `agent_version`；并且用**测试**把两处版本号钉住
（队列报的 == `src/version.cpp` 里的），免得发版时改了一个忘了另一个。

测试：tests/smoke_queue_spec.py 多了 7 项（59 项全过）。

### 2026-11-04：扩展层 a2_ext_py（机制层能长出新能力）

**名字**：`a2_ext_py`（不是 `a2_py`——它不是「a2 的封装」，是「a2 的扩展层」）。
位置 `sched/a2_ext_py/`，三层于是说清楚了：`a2`（C++ 内核）/ `a2_task`（策略）/
`a2_ext_py`（扩展）。

两件事：

1. **客户端只有一份**。以前有三份拷贝：`tea_api.Tea`、`channel.Agent`、
   `task_server.Agent`（连 `_sse` 都抄了两遍），而且 `channel.Queue.project_key`
   是容错的（吞掉 404 返回空）、`tea_api.Queue.project_key` 是抛的——合并时**这个差异
   差点被抹掉**，微信/telegram 两个 bot 直接挂了（`ensure_workspace` 那里）。
   **教训**：合并两份抄本时，先把它们的**行为差异**列出来，别只合并方法。
2. **机制层能长出新能力**。`sched/a2_ext_py/toolkit/` 是一个 **MCP 服务器**
   （stdio，a2 按 `mcp.yaml` 启动它）：模块 = 一个 `.py` 文件（`toolkit_<名字>.py`
   + `meta_toolkit_<名字>()`），加载后就是 a2 的工具。**a2 一行 C++ 都不用改**。
   模型自己也能写（`toolkit_save` → `toolkit_reload`），保存前有检查（名字前缀、
   schema、`ast.parse`、函数在不在、缺依赖报出来但不偷偷装），每次保存留旧版本
   （`toolkit_rollback` 能退）+ 审计（`~/.tea_agent/ext-audit.jsonl`）。

C++ 那侧只动了两处（`src/mcp.cpp`、`src/agent.cpp`）：

* `McpClient::refresh_tools()` / `McpRegistry::refresh_tools()`：**不重启服务器**，
  只重问一遍 `tools/list`；
* agent 的工具清单**每轮迭代重建一次**——所以模型存了工具、reload 一下，
  **同一轮里就能用**（服务器不用重启，状态也不丢）。
  代价：每个有 MCP 的回合每轮多一次 `tools/list` 往返。

**坑**：用户模块住在 `~/.tea_agent/ext/`，**不在发版包里**（发版会覆盖它），
**也不在工作区里**（模型的 `toolkit_file` 够不着——围栏到工作区为止）。

**自举的闸门不做了**（用户 2026-11-04 决定：「这个风险是值得的，都是成年人了，
要为自己的决定负责」）。模型能写以后要执行的代码——跟 DSH 的创造模式是同一个风险，
这里接受它。留下四道便宜的防线：模块目录不在任何工作区里（启动时检查并警告）、
保存前检查、每次保存有审计（含代码）、**能一键关掉模型自存**
（`--no-save` / `TEA_EXT_SAVE=0`，出事时不用改代码）。
机制层的 M3 因此只剩「策略层下发的权限」那部分，审批闸门不再做。

### 2026-11-04：预算回来了（这次是有上界的）

**改主意了**：2026-10-28 那次提交（`c582996`）把「一轮最多几轮」这个旋钮**删掉**了，
理由是「配置不该替 agent 做决定」。现在反过来了——一轮必须有上界，否则策略层
（任务队列）不敢把活儿撒手出去。用户拍板：**默认 300 轮**。

现在的规矩：

- **机器的上限是天花板**：设置里的 `max_iterations`（默认 300）/ `max_wall_ms` /
  `max_tool_calls` / `max_total_tokens`。`GET /api/health` 的 `limits` 报当前生效的那套。
- **请求只能收紧，不能放宽**：`TurnBudget::tightened_by()`（src/config.cpp）取两者的
  **较小值**。所以「这台机器一轮最多花多少」是机器说了算，队列只能在范围里再切一刀。
- **到点是正常结束**：`stop_reason: "budget"` + `budget_used` + 一条 `budget` 事件，
  **不是 error**。做过的部分算数，对话里留一句「预算用完了」。别把它做成失败——
  调用方要能分清「活干完了」和「没地方了」。
- **工具调用撞到上限时那一次不执行**，但仍然给它一个回答（provider 不接受没有结果的
  工具调用，MiniMax 会 400）。

坑：

1. **provider.yaml 的 `settings:` 块有一个例外**：那四个预算键**也**在那里读
   （用户点名要的），其余设置仍然不读。启动日志里那句话改了——`smoke_port_guard.py`
   之类的测试会 grep 它。
2. **工具白名单原来只是「不给模型看」**：`options.tools` 只过滤了发给模型的 schema，
   模型点名要一个没给它的工具，**它照样执行**。现在改成了**真的拒绝**
   （src/agent.cpp：不在白名单里的工具得到一句「这一轮不准用这个工具」）。
   队列的 `policy.tools` 靠这个才是真的权限。
3. **`trajectory.cpp` 现在记 `task_id`**：任务 → 回合 → 轨迹终于是一条链。
   队列的 `_consume()` 也记 `trajectory_id` / `usage` / `stop_reason` 了
   （以前只取 `content`，用量和轨迹 id 全丢了，看门狗只能去猜「最近两轮」）。
4. **墙钟对长工具也生效**：`ToolContext::deadline_ms`（绝对时间），`toolkit_exec`
   把它算进自己的 `timeout_ms`——不然一条长命令能把超了预算的这一轮拖下去。

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

### 现状：Gmail 这条路先搁着（2026-10-29，用户判断）

从没对着真账号跑过。用户看过 Google 现在的风控（应用专用密码也不再是「有密码就能连」：
账号、设备、IP、登录历史一起参与判断，新机器第一次连就可能被拦，拦了还要人工解），
判断不值得为它继续投入 —— **不要再花时间试 Gmail 应用专用密码**。代码是通用 IMAP，
换一家只是 `host`/`port`/`password` 三个值；要真接 Gmail 只剩 OAuth2（XOAUTH2 +
refresh token + Google Cloud 的 OAuth 客户端），入口在 `ImapSource.connect()` 的 `login()`。

### 还缺的（排在 docs/integrations.md 文末）

队列没有「只通知不干活」的记录类型（watcher 只好自己发通知，网页队列里看不到）；没有
`watch.py` 基类（第二个源落地时再抽）；agent 侧没有 `callback_url`；Gmail 只有应用专用密码
（Workspace 关了这条通道就得写 XOAUTH2）；回信之后没有标签闭环。

## 百度网盘同步配置（2026-10-30）

用户的需求：**多台机器共用一份 tea_a2 配置**。配置住在机器本地（`<tea_home>/provider.yaml`
等），换机器就要重抄一遍，而 provider.yaml 里有 API key、模型顺序、peers 令牌。

### 放在 Python 侧（`sched/pan_sync.py`）

和任务队列、watcher 同一个理由：同步哪些文件、远端放哪、冲突了怎么办，都是**策略**，
改一行配置就该生效，不该重新编译；而且同步挂了不影响本体。它不用 C++ 的 HTTP 栈，
只用标准库（urllib / hashlib / hmac / zlib），所以能在没 pip 的机器上跑。

### 百度网盘开放平台的要点（查文档 + 官方 MCP Server 的源码得来的）

- 三种授权：**授权码 / 简化 / 设备码**。这里用**设备码**（`/oauth/2.0/device/code`）：
  终端里不用粘 code，给个网址 + 用户码就行；`scope=basic,netdisk` 必填。
- **access_token 30 天过期**；**refresh_token 只能用一次**，刷新响应里给的是**新的**
  refresh_token —— 所以刷新后必须马上落盘（旧的那个已经废了）。
- 上传是三步：`precreate`（拿 uploadid，rtype=3=覆盖）→ `superfile2` 传分片（4MB 一片，
  multipart，字段名 `file`）→ `create`（isdir=0，合并）。少一步文件就是坏的。
  **两个真机才发现的坑**（2026-10-30 联调）：①**上传域名是动态的** —— 官方文档明说「必须通过
  获取上传域名接口获取，不要在生产代码中固定使用示例域名」；把 `superfile2` 打在
  `pan.baidu.com` 上是 `HTTP 403 页面不存在`（网页前端的 404 页），要打在
  `d.pcs.baidu.com`，更好是先问 `GET d.pcs.baidu.com/rest/2.0/pcs/file?method=locateupload
  &appid=250528&access_token=…&path=…&uploadid=…&upload_version=2.0`（返回 host/servers）。
  ②**分片参数的位置**：`path`/`uploadid`/`partseq`/`type` 都是**查询参数**，multipart 体里只有
  `file`（官方 SDK 的 `location_map` 也是这么写的）。照文档写，别照别人的 SDK 抄。
- 建目录也是 `create`（isdir=1），**目录已存在返回 errno -8**，当成功处理。
- 下载：先 `filemetas`（`fsids` 是 JSON 数组，URL 编码，`dlink=1`）拿 dlink，再顺着 dlink
  下载（dlink 里没有 access_token 要自己补）。
- 错误码：鉴权类的 20016（过期）/ 20017（无效，要重新授权）/ -6（token 无效或 scope 不对）；
  31034 = 风控要验证。errno != 0 一律当失败，不重试到死。
- 官方还发了一个 MCP Server（`github.com/baidu-netdisk/mcp`，SSE 在
  `https://mcp-pan.baidu.com/sse`）—— 要更细的网盘能力（分享、转存、搜索）接它更划算。
- **文档说 precreate「仅支持访问 /apps/{appname} 目录下的文件」**，但官方 MCP Server 传的是
  普通目录（`/来自：mcp_server`）—— 所以普通目录大概率能用。真账号上如果不行，把
  `remote_dir` 改成 `/apps/<应用名>/tea_a2`。

### 三个刻意的取舍

- **只同步配置，不同步历史、也不同步清单**。provider.yaml / tea_agent.yaml / mcp.yaml 同步；
  会话记录、记忆、日志不同步（每台机器自己的历史）；**`workspaces.json` 也不同步** ——
  它是**工作区清单**，里面是本机的绝对路径（`/home/sunkw/...`）和会话计数，搬到另一台上
  （Windows 那台的路径是 `C:\...`）会把它的清单覆盖成一堆不存在的路径。
  **拉取时只写 `files:` 白名单里的文件**：远端包里名单外的（比如上一版留下的
  workspaces.json）跳过并报出来，不写进 tea home；范围改了（少一个文件）会重推一份，
  远端包里不留旧文件。
- **推之前先加密**。provider.yaml 里有 API key，明文躺在网盘里等于把钥匙挂在门上。
  口令 → PBKDF2-HMAC-SHA256（200000 轮 + 16 字节随机盐）→ 64 字节密钥 →
  HMAC-SHA256 当伪随机流做计数器模式加密 → encrypt-then-MAC（标签覆盖 magic+盐+随机数+密文）。
  没口令不推（`allow_plain: yes` 才明文）。
- **两边都改了就不动手**。远端比上次同步新、本地也改了 —— 谁覆盖谁都是猜。判据是三样：
  本地每个文件的 sha256、远端清单（谁在什么时候推的）、本地 state.json（上次两边什么样）。
  冲突时两边都不动，把话说清楚，`--force` 才覆盖。**默默覆盖是同步工具最糟的失败方式。**
  另外：拉之前先备份到 `~/.tea_agent/pan-sync/backup/<时间戳>/`；删除不传播（拉取不会删
  别的机器上的文件）；顺序永远先拉后推。

### 测试怎么钉（`tests/smoke_pan_sync.py`，97 条）

- **假开放平台是协议级的**：设备码、换令牌、刷新、用户信息、列目录、建目录（已存在返 -8）、
  预上传、分片上传（multipart 要自己解析）、合并、filemetas 拿 dlink、下载，一个不落地实现。
  协议写错（比如把 `isdir` 当 1/0 传反）在测试里就会露。
- 钉住的是：配置格式（写错键带行号）、加密（密文里没有明文、口令错解不开、篡改一个字节
  验不过）、打包（名字不许带路径、校验对不上要报错）、授权（令牌 0600、过期会刷新）、
  推拉、**冲突不动手**、`--force` 才覆盖、备份、删除不传播、两台机器拉到同一份、
  **新机器第一次拉**（不该被「冲突」挡住，但本地那份要先备份）、**本地有远端没有的文件**
  （下一次 push 要把它带上去 —— 少了这条它永远传不上去）、**同步范围**：默认名单里没有
  workspaces.json、拉取只写白名单里的文件（名单外的跳过并报出来）、范围改小要重推一份。
- 陷阱：`Syncer` 的状态文件是**每台机器一份**（`home` 参数），不是每个网盘账号一份 ——
  一开始写成全局的，测试里两台机器共用一个 state，冲突永远测不出来。

### 发版：包传到百度网盘，不进 git（2026-10-30 起）

**发布产物（zip）不再提交到 git。** 仓库只留源码和文档；每一版的包传到网盘的
`/tea_a2/releases/<版本>/`，远端留一份 `release-manifest.json`。理由很直白：每版一个
6~40MB 的二进制塞在版本库里，clone 变慢、`git log` 变成噪声，而源码和产物混在一起，
回头想「这版到底发了什么」都不好翻。

```bash
.tea_agent_run/scripts/release.sh 0.18.5                      # 打包 + 上传 + 清理旧版本，一步到位
git add -A && git commit -m "release: 0.18.5"                 # **不**提交 release/*.zip
```

`release.sh` 是这台机器上的流程（`.tea_agent_run/scripts/`，不进 git）：打包 → 传到
`/tea_a2/releases/<版本>/` → **网盘上只留最新的 5 个发布包**（按版本号排，`0.17.10` 比
`0.17.3` 新；像版本号的目录才算发布目录，手工建的目录不碰）。

- `.gitignore` 加了 `release/*.zip` 和 `release/tmp/`；旧的那一版 zip 已经
  `git rm --cached`（文件还在本地，也还在网盘上）。
- **4MB 分片**：百度网盘的上传接口按 4MB 一片传（`partseq` 是片号，`block_list` 给每片
  md5）。分片数 = `ceil(大小 / 4MB)`，整 4MB 一片、最后一片是余数；**小于 4MB 的文件走
  单片**（官方文档那条「首片不得小于 4MB」说的是「非最后一片」；官方 MCP Server 的
  小文件分支也是单片）。实测：6.6MB = 2 片、12MB = 3 片，回读校验都对。
- **回读校验**：传完把网盘上的文件下下来比对 sha256 —— 发版的东西不能只看「上传成功」。
- 清理旧版本：`PanClient.delete_path()`（`opera=delete` 在**查询串**里，`filelist` 是
  路径的 JSON 数组在表单里；照表单发会 errno=2）。删目录要连里面的东西一起删（先删目录，
  再删残留，再删目录）。
- 看网盘上有哪些版本：`python3 sched/pan_publish.py --list`。

### 现状：对着真账号跑通了（2026-10-30）

授权（设备码，扫码）→ 推送 → 远端目录 `/tea_a2/default` 里出现 `config.tea`（加密，2146 字节，
明文里搜不到 API key）+ `manifest.json`（明文清单）→ 另一台机器（空 tea home）拉下来，
两边的 provider.yaml sha256 完全一样 → 冲突检测也验过（两边都改 → 停手，两边都不动）。

**普通目录能用**：文档说 precreate「仅支持访问 /apps/{appname} 目录下的文件」，实测
`/tea_a2/default` 下建目录、上传、下载都正常，不用绕到 `/apps/`。

还没验的：令牌 30 天过期后的自动刷新（真账号上要等 30 天）、配额上限、多账号多组、
大文件（>4MB 的分片）——配置包才 2KB，真实场景里 provider.yaml 也只有几 KB。

### 还缺的

没有「远端更新了就通知我」——现在只有 `watch` 的日志；要通知就把它挂到 watcher 那套出口上。
没有多账号（一个网盘账号只能同步一个 profile，要多组就多账号或者改远端目录）。
没有同步「工作区的 `.tea_agent_run/settings.yaml`」（工作区路径各机器不同，没做映射）。

## 计划：复杂活儿拆成几步，做完汇总（M6，2026-11-02）

Super-agent 的原型里，Worker 各干各的；我们这边多一层：**步骤之间可以互相等**，而且做完要有
一个人把它们**合成**一份交付物。问的是「能不能自主规划、分解、汇总」——答案是能，但**差三样
东西**，这次把它们补上了。

### 原来差什么（2026-11-02 之前的代码事实）

- 队列里**没有父子、没有依赖、没有汇总**：`sched/task_server.py` 里 grep `parent_id` /
  `depends_on` / `aggregate` 全是空的；任务记录只有 `trigger_id` / `thread_id` 两个关系字段。
- **只有一个 workspace 时任务是串行的**：`round()` 里「一个工作区同时一个任务」
  （`One task at a time per project`）。所以「并行子任务」不是加个字段的事——子任务得分到
  不同工作区（机群设备各有各的），或者给计划单开一条并发口子。这次没做，记在这里。
- **只有触发器创建的任务才带 `thread_id`**（`make_task_from_trigger` 才设，`new_task()` 恒为空）。
  直接 `POST /api/tasks` 排的任务不在任何线索里，router 记不住它们。这次计划里的步骤是队列自己
  建的，所以显式挂了 `thread_id`。
- agent 内核的 `toolkit_todo` 是**会话内**的计划：同一个 session 里自己列步骤自己做。它不是
  「一个计划拆成 N 个队列任务」。

### 这次的取舍

| 决定 | 为什么 |
| --- | --- |
| 汇总放在**队列**里，不放 router | router 有「线索」容器和唯一那次模型调用，看起来该归它；但**队列知道最后一步什么时候做完的**，router 只是隔 30 秒看一眼。汇总放队列，计划才不会因为 router 恰好不在就悬着。router 照旧负责**说话**（`task_done` → `speech`） |
| 父任务状态用 `queued` + `kind: "plan"` | 父任务是容器，不干活。**不能用 `running`**：`running_tasks()` 按工作区数运行中的任务，父任务占着槽位的话，同工作区的步骤就永远排不上 |
| 依赖失败 → 依赖它的步骤被**取消** | 等一个永远等不到的东西，就是永远排队。停掉（`cancelled` + 原因）比挂着强 |
| 规划是**独立一步**（`sched/planner.py`），计划写成 JSON 给人改 | 模型拆的步不一定对，而且它不知道这台机器今天能做什么。**计划是建议，不是命令**——中间留人 |
| 步骤数上限 32（`plans.MAX_STEPS`），规划器默认 6 | 「自主」最容易变成「拆出 40 步烧钱」 |
| 汇总只用 `content`，**永远不把 `reasoning` 当答案** | 踩过的坑：deepseek-flash 先思考（reasoning 2000+ 字符），`max_tokens=1200` 被思考吃光 → `content` 空 → 当时回退到 reasoning，**把思考过程写进了最终交付物**。现在：只用 content；空了就重试；`finish_reason == "length"` 就把预算加倍（4000 → 8000 → 16000）再试 |
| 汇总失败重试 3 次，然后计划判失败 | 一个永远汇总不出来的计划不该永远挂着；3 次之后父任务 `failed` 并写明原因 |
| 看门狗加 `plan_stuck` | 步骤都做完了、父任务却一直没写结果——这种「工作做完了但没人交差」的状态，规则化的「卡住」检查看不见（它只看 `running` 的任务）。看门狗是从外面看的那双眼睛 |

### 新的接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/plans` | 交一个计划：`{title, goal, steps:[{key,title,prompt,depends_on[]}], workspace_id?, thread_key?, aggregate_prompt?}`。变成一个父任务（`kind: "plan"`）+ 每步一个子任务（带 `parent_id` / `plan_id` / `depends_on`）。同一个 `Idempotency-Key` 只排一次 |
| `GET` | `/api/plans` | 计划列表 |
| `GET` | `/api/plans/{id}` | 计划 + 父任务 + 每步的任务 |
| `POST` | `/api/plans/{id}/cancel` | 取消计划：停掉还在跑的步骤和父任务 |

计划的记录在 `<数据目录>/plans/<id>.json`（**不放根目录**：队列的任务列表会读数据目录下每一个
`*.json`，计划不是任务）。校验在 `sched/plans.py`（不联网，能单测）：步骤要有内容、依赖要存在、
不许互相对着等成环。

### 发版清单的坑（顺手修的）

`release/tea_a2-0.17.4.zip` 里的 `tasks/` 有 `task_server.py`，**却没有它 `import` 的
`triggers.py`** —— 终端用户拿到包，`python3 tasks/task_server.py` 直接 ImportError。
`pack_release.sh` 现在把 `task_server.py` import 的模块（`triggers.py`、`plans.py`）一起打进去，
外加 `planner.py`（用户用得上的规划器）。**以后加一个 `import` 就要看一眼发版清单。**

### 还没做的

- **网页上看不到计划**：队列的页面（`sched/task_ui.html`）只画普通任务卡，父任务现在显示成
  「排队中」的一个任务。计划要能点开看几步、谁在等谁，UI 得改。
- **计划不能中途改**：模型拆错了只能取消重来，没有「replan」（把没开始的步骤换掉）。
- **步骤之间不能传数据**：只能靠同一条线索的上下文（prompt 里带上前一步的结果）。要显式传
  产物（比如第 1 步写了个文件，第 2 步要知道路径），得有个「步骤的产出」字段。
- **并行**：同一工作区的步骤还是串行（见上）。要并行得让步骤分到不同工作区。

## `task_model:`：排队任务用哪个 provider+模型（0.18.1）

provider.yaml 顶层加一行，指定**所有排队任务**用哪个 provider 和模型：

```yaml
task_model: minimax+MiniMax-M3.1-Flash
```

### 为什么是「一个 provider+模型」而不是「每个任务各自」

排队任务和聊天页不是一回事：聊天页用**这个文件的默认**（第一个 provider 的第一个模型），
队列是后台干活、花你托付给它的那张卡。以前队列只有一个硬编码的 `--provider`（minimax），
改它要改 systemd 单元；现在改一行配置就行。

但队列**一次只跑一个 provider**：额度闸门是「一个 provider 的额度」，跑别的 provider 就是
假账（查的是 A 的额度、跑的是 B）。所以这是一条「队列级别的决定」，不是每个任务的决定；
单个任务仍然可以写自己的 `model`（那个优先）。

### 为什么 agent 解析、队列问 agent

provider.yaml 是 **agent 的**文件（它热加载、设置页能改）。如果队列自己再解析一遍 yaml，
同一个文件就有两个主人、两份解析代码——迟早对不上。所以队列**问 agent**
（`GET /api/health` 的 `task_model` 字段），不自己装 yaml 解析器（设备上没有 pyyaml）。

例外：agent 挂了、或者还是不认识这个 key 的老版本时，队列自己**扫那一行**——顶层一个标量，
`split(":")` 就够，不用 yaml。这条兼底让新配置在 agent 没重启时也照样生效。

### 别的取舍

- **优先级**：命令行 `--provider` > `task_model:` > 内置默认（minimax）。命令行是起服务的人
  明确说的，配置文件是机器上的默认。
- **额度闸门跟着走**：闸门查的就是这个 provider 的额度。**provider 没有额度端点 → 队列不开工**
  （除非 `--allow-without-quota`）——这条对「task_model 指到一个不报额度的 provider」同样成立。
- **改完不用重启**：队列 60 秒缓存一次这个值（一分钟之内自己看到）。
- **写错不致命**：少了 `+`、`providers:` 里没有这个 provider → agent 警告一句，这一行当没写。

### 顺手修的：任务的 `model` 字段以前是死的

API 和任务单都收 `model:`，也存进任务记录，但队列的 `Agent.chat()` **只传 provider**，
从不传 model → 模型永远是那个 provider 的首选模型。现在真传了（agent 的 `/api/chat` 本来就
支持 `model`；不认识的模型名按 agent 的老规矩回退到首选）。

## 工具的时间：什么时候开始、用了多久、上限多少（0.18.2）

一个跑三分钟的构建，在界面上和一个卡死的 agent 一模一样：只有一行转圈。用户没法判断
「还在干活」还是「已经死了」，也没法判断「再等一分钟值不值」。**跑得久的工具必须报时间。**

### 三个数

- **`started_at`**（`tool_start` + `tool_result` 都带）：这次调用什么时候开始，ISO8601 UTC
  （毫秒精度，`now_iso8601_ms()`）。
- **`elapsed_ms`**（`tool_result`，早就有）：服务端量的耗时。
- **`timeout_ms`**（`tool_start` + `tool_result` 都带）：这一次的**上限**。
  **0 = 这个工具没有自己的上限**（读文件、记笔记这类毫秒级工具），不是「没有上限」。

`Message`（role == "tool"）也存这两个，所以**刷新页面后数字还在**（`to_store()` /
`from_store()` 各加一行）。

### 上限是真值，不是猜的

`ToolRegistry::timeout_ms(name, args, ctx)` 算这个数，各工具再把**自己正在用的那个**
报上来（`result.meta["timeout_ms"]`）——两边指向同一个值，所以界面上写的和工具真的执行的
不会打架：

| 工具 | 上限从哪来 |
| --- | --- |
| `toolkit_exec` | 命令自己拿到的 `timeout_ms`（没写就用 `command_timeout_ms`，下限 1 s） |
| `toolkit_search` / `toolkit_fetch` | 读超时（`web_tool_timeout_ms`，默认 60 s） |
| `toolkit_fleet` | 15 分钟（一次机群作业不是一次网页请求） |
| `toolkit_peer` | 模型给的 `timeout_s`（默认 900 s） |
| `mcp__<server>__<tool>` | 那个 MCP 服务器在 `mcp.yaml` 里的 `timeout_ms` |
| 文件 / 记忆 / 待办 | 没有上限 → 0 |

**MCP 的坑**：发布出来的工具名是 `mcp__<净化后的服务器名>__<工具>`，服务器名太长会被
截断加哈希——所以「从名字反查服务器」不能靠字符串匹配。`mcp_server_prefix()` 是唯一知道
这件事的地方（`mcp_tool_full_name()` 和 `McpRegistry::timeout_ms_for()` 都用它）。

### 秒针在浏览器这边走

服务端只在**开始和结束**时报两次，中间那几十秒是空的——工具没返回，它就一直在等。
所以界面上的「已用 Xs」是**浏览器自己按 `started_at` 走的秒针**
（`web/app.js` 的 `tickToolElapsed()`，每 500 ms 刷一次带 `data-started-at` 的元素）。
这样工具还在跑，用户就知道它还活着；结束的那一刻换回服务端量的 `elapsed_ms`。

### 坑

- **时间只挂在 `tool_start` 上**：模型可以一次请求好几个工具，`tool_call` 是**预告**
  （全都发出来），`tool_start` 才是**真开始**——预告的那一刻工具还没跑，谈「用了多久」没有意义。
- **live 结构签名要带上 `started_at`**：界面按结构签名决定要不要重画，签名不变就不重画——
  工具开始的那一帧不重画，秒针就没有落脚点。
- **`tool_call` 的预告行也会显示「运行中」**：预告 ≠ 真的开始。老样子，没改。

## 一次调用的旋钮：/api/llm/chat 也能带「别思考、便宜、稳一点」（0.18.3）

`/api/llm/chat` 原来只接了 `temperature` / `top_p` / `max_tokens`，**`reasoning_effort`
被静默忽略**——传了跟没传一样。一个机制接口给不出策略组合，就只能当「问一句话」用，
不能当「按我的策略调一次模型」用（比如一个 Firefox 翻译插件：便宜、不要思考、翻译要稳）。

### 现在能带的旋钮

`reasoning_effort`（思考强度）、`temperature`、`top_p`、`top_k`、
`frequency_penalty`、`presence_penalty`、`seed`、`max_tokens`。
`/api/chat` 的**这一轮**也吃同一套（`TurnOptions::knob_overrides`）。

### 三条规则

1. **优先级：请求体 > 机器设置 > 模型自己的配置。** 没人设的旋钮**不会出现在请求里**
   ——网关不认识某个字段会回 400。没设 ≠ 设成 0。
2. **`null` 是「清掉」**：`"temperature": null` 让这个字段离开请求，哪怕模型配置里设了它。
3. **写错了会 400，不会被悄悄忽略**。这是这次改动里最要紧的一条：原来传
   `"temperature": "很冷"` 不报错也不生效，调用方只能靠猜。

### 落点：`llm.cpp` 里的两个函数

```cpp
apply_default_knobs(&request, st, model_info);   // 机器设置 → 模型配置
apply_knob_overrides(&request, body, &knob_err); // 请求体（赢）
knobs_to_json(request);                          // 响应里报「真正发出去的是什么」
```

**为什么放在 llm.cpp 而不是 agent.cpp**：以前这段逻辑写在 `Agent::run` 里，
`/api/llm/chat` 各写一份 —— 两份必然漂移。现在两条路共用一个函数，
`agent.cpp` 里那段内联代码删掉换成 `apply_default_knobs()`，行为一模一样。

### `reasoning_effort` 的两个特例

- **显式值照发**：`supports_reasoning` 只挡着**默认**那一条路。调用方显式写了
  `reasoning_effort`，就发 —— 调用方知道自己的网关。「禁用思考」正是这么用的
  （模型没声明 `supports_reasoning` 也照样发 `off`）。
- **`auto` = 不发这个字段**（不是「自动强度」）。网关不认这个字段时会回 400。
  值原样上线，所以 `none` 这种它自己的写法也照发；不在已知列表里的值会**在日志里
  提一句**（拼错了不会立刻看出来，但至少留了痕迹）。

### 响应带 `knobs`

调用方能确认「我说的禁思考，真的禁了」：
`knobs.reasoning_effort` 是实际值，`knobs.reasoning_effort_sent` 说明字段**有没有上线**。

**但它证明不了网关照做了** —— 网关不认 `off` 就会**忽略**这个字段，模型照样思考。
要绝对不思考，只能选一个本来就不思考的模型。这句话写进了 api.md，不能只写在代码里。

### 顺带修的

- `/api/llm/chat` 以前**不回退**到模型配置里的采样参数（你传就用你的，不传就发 provider
  的默认）。现在跟 agent loop 一样回退，规则统一了。

## 接口文档进二进制：GET /api/doc（0.18.4）

设备上可能没有网、没有仓库、连 `docs/` 目录都没有——但**要查接口的人手上只有这个设备**。
所以 `docs/api.md` 在**编译期就进了二进制**（跟网页资源一起），`GET /api/doc` 从二进制里读出来。

### 三个格式

| 请求 | 回答 |
| --- | --- |
| `GET /api/doc` | 渲染好的 HTML（自带样式，深浅色跟系统走） |
| `GET /api/doc?format=md` | Markdown 原文（`Accept: text/markdown` 也一样） |
| `GET /api/doc?format=json` | `{ok, version, title, markdown, html}`——脚本不用挑格式 |

不认识的 `format` 是 400，不静默当成 html。

### 嵌入机制改了：`tools/embed_assets.cpp`

以前资源只有一个根目录（`web/`），命令行是 `<out> <web-dir> <relpath>...`，
**相对路径既是源文件也是虚拟名**。现在资源不止住一个地方（网页在 `web/`，
文档在 `docs/`），所以改成显式的一一对应：

```
tea_embed_assets <out-dir> <root-dir> <name>=<relpath> ...
```

`name` 是运行时要的虚拟名，`relpath` 是源文件（相对 root-dir）。CMake 里：

```cmake
set(TEA_ASSET_SPECS index.html=web/index.html ... api.md=docs/api.md)
```

**两份资源一份机制**——以后再要往二进制里塞东西（一个图标、一份 schema），
加一行就行，不用改工具。

### 顺带修的 markdown 渲染 bug

`**粗体 `code` 粗体**` 渲染出来是 `**粗体 `code` 粗体**`（星号没变成粗体）。
原因：`inline_md()` 原来按 `<code>` 把文本切成段，**每段单独跑一遍加粗**——
跨着代码块的加粗，第二个 `**` 落在另一段里，永远配不上对。

改成：**先把代码块提出来**（内容存进数组，原地留一个占位符），整段文本跑完加粗/斜体/
删除线**之后**再把代码块放回去。代码块里的 `*` 仍然是字面量（它不在文本里），
而包着代码块的标记现在跨得过去了。

**这个 bug 不只影响文档页**：会话导出（`/api/sessions/{id}/export`）用的是同一个渲染器，
模型回答里一句「**重点 `code` 重点**」以前也是坏的。

### 文档不能落后于代码

`tests/smoke_api_docs.py` 做两件事：

1. **比对字节**：`/api/doc?format=md` 的内容必须跟仓库里的 `docs/api.md` 一模一样——
   改了文档没重新编译，这里会失败（错误信息会说「重新编译」）。
2. **路由覆盖**：`src/server.cpp` 里注册的每一条路由都必须在 `docs/api.md` 里出现过——
   **代码里有、文档里没写的路由**算失败。

第 2 条的坑：两边拼路由的方式不一样（代码 `([^/]+)`、文档 `{sid}`；文档还常带
查询串），所以要先把两边归一成同一个形状再比。归一化写错过一次：
`\[\^/\]+` 里的 `+` 在字符类里是「一个或多个 `]`」，不是「`]` 后面跟个 `+`」——
所以那个替换**根本没生效**，测试却「通过」了（它只是把更多路由算成没写）。
现在的写法是 `\(\[\^/\]\+\)`。

## 原语层也能联网搜索：/api/tools/call 与 web_tools（0.18.4）

DeepSeek 的搜索和抓取是 **provider 上的两个端点**（`/v1/tools/search`、
`/v1/tools/fetch`），不是模型自己执行的工具——tea_a2 去调它们，把结果当工具结果喂回模型。
这在 `/api/chat`（agent loop）里一直能用，但 **`/api/llm/chat` 是原语层，它不执行任何
工具**：想在那条路上搜索的脚本，今天只能自己拼 DeepSeek 的端点。

现在两条路都有了：

- **`POST /api/tools/call`** —— 调一个网页工具，不要模型。
  `{"tool":"toolkit_search","arguments":{…}}` → `{ok, tool, provider, output, elapsed_ms}`。
  只有 `toolkit_search` / `toolkit_fetch` 两个：别的工具要工作区、要会话，属于一轮对话。
- **`/api/llm/chat` 带 `web_tools: true`** —— 一次调用就完事。服务端把 provider 能提供的
  网页工具告诉模型；模型要搜时服务端替它调那个端点，把结果喂回去，模型接着回答（最多 4 轮）。
  回答里带 `web_tool_calls`（服务端替模型做了什么）。

**只有网页工具会被执行**：模型要是调了别的工具，那个调用照旧回到调用方手里——这条路由
答应的是「一次模型调用」，不是「一个 agent」。没开 `web_tools` 时，服务端一个工具都不执行。

### 坑：流式分支里 provider 会被反复调用（真 bug，已修）

`/api/llm/chat` 的流式分支用 `set_chunked_content_provider`，**但没有调 `sink.done()`**。
httplib 的 chunked provider 是个循环：

```cpp
while (data_available && !is_shutting_down()) {
  ... else if (!content_provider(offset, 0, data_sink)) return false;
}
```

`data_available` 只有 `sink.done()` 才会变成 false。所以 httplib 会**一遍又一遍地调用这个
provider**——而 provider 里是「调一次模型」：**同一个问题被问了一遍又一遍，每一遍都计费**，
直到客户端断开。

`/api/chat` 那条 SSE 路由没这个问题：它的 provider 是 `turn->pump()`，pump 结束时
调了 `sink.done()`（那里的注释已经写清楚了为什么：返回 false 会让传输被中断，浏览器报
"Error in input stream"）。

修法就一行：`sink.write(frame...)` 之后 `sink.done();`。

**为什么这么久没人发现**：这条路由的流式分支很少有人用；用的人多半是 `curl` 一次，
看到答案就断了——循环在断开的瞬间停掉，模型也被多问了几次（钱照扣）。

### `resolve_reasoning_echo` 搬到了 agent.h

`/api/llm/chat` 的 web_tools 分支要把助手的 `reasoning_content` 一起发回去（thinking 模式的
网关要求这样，DeepSeek 拒绝这样）。这个判断以前是 `agent.cpp` 里的一个匿名函数，
现在两条路都要用 → 挪到 `agent.h`（实现还在 agent.cpp，因为它要用 `parse_url`，
而 `ParsedUrl` 在 `http.h` 里，config.cpp 不该拖上整个 HTTP 客户端）。

## 发版包带上完整的 sched/ 与 scripts/（0.18.5）

包里的 `tasks/` 一直是**一份手写清单**（`task_server.py`、`triggers.py`、`plans.py`……），
Python 侧每加一个 `import`，打包脚本就得跟着改一次——忘了改，终端用户拿到包就是一次
ImportError。`0.17.4` 就这么翻过一次车（包里少了 `triggers.py`）。

现在**没有清单了**：

```
docs/tea_a2_manual.md
sched/          整个目录
scripts/        整个目录
tea_a2-linux-x86_64-<版本> / -aarch64- / tea_a2-win-x86_64-<版本>.exe
tea_agent.buildinfo
```

**目录名跟仓库里一样**，这一点比「文件全了」还重要：

- systemd 单元（`scripts/a2_task.service`）里写的是 `<路径>/sched/task_server.py`。
  以前包里只有 `tasks/`，单元指向的 `sched/` 根本不存在——**包其实是不自洽的**。
- `scripts/install-units.sh` 用 `$(dirname "$0")/..` 当仓库根，解包后跑它，
  单元里的路径就被换成解包目录 → `<解包目录>/sched/task_server.py` ✓ 现在真的存在。
  所以**解包出来的包现在能自己装服务**（`systemctl --user enable --now a2_task`）。

**打包脚本自己会再查一遍**：打完包后把仓库里 `sched/`、`scripts/` 的文件列表跟包里的
条目比，少一个就中止发版。清单能出错，目录不能。

`__pycache__` 与 `*.pyc` 仍然不进包（脚本打完会清）。
包大了约 300 KB（压缩后）——sched/ 去掉 pycache 是 788 KB，scripts/ 是 172 KB。

---

## 历史留存：GC + 冷数据 gzip（2026-11-06）

**问题**：轨迹（`trajectories/<sid>.jsonl`）本来就有上限（`trajectory_max_turns` /
`trajectory_max_bytes`，按**单个文件**算），但整个工作区的历史只增不减。实测本机：

| 目录 | 大小 |
|---|---|
| `sessions/`（会话记录） | 164 MB |
| `trajectories/`（轨迹） | 96 MB |
| `logs/` | 37 MB |
| `artifacts/` | 36 MB |

**会话记录比轨迹还大，而且一个上限都没有**（一场长对话 1800 条消息 = 4.4 MB）。
另外删会话时轨迹文件**留在盘上**（孤儿）。

**做法**（`src/history_gc.h`、`src/workspace.cpp` 的 `Workspace::gc_history`）：

1. **太旧的删**：`history_max_age_days`（按文件 mtime，会话在用就会刷新 mtime）；
2. **冷数据压**：`history_compress_after_days` → `foo.json` 变成 `foo.json.gz`
   （**真的 gzip**，`gunzip` 能读；miniz 出 deflate，自己包 RFC1952 头尾 + crc32）；
3. **超预算删最旧**：`history_max_bytes`（run_dir 总量，含日志/产物/索引——不是历史的部分
   删不动，所以超了会在 `notes` 里说清楚）；
4. `history_keep_recent`（默认 5）保护最新 N 个会话——**只对预算删生效**，年龄删不保
   （年龄是显式指令；在用的会话 mtime 是新的，删不到）。

**读路径透明解压**：`read_file_maybe_gzip()`（`src/gzip.cpp`）——路径不存在时自动找
`path + ".gz"`。所以被压过的会话照旧能打开、能搜（`load_session` /
`read_json_file_maybe_gzip` / `load_trajectories` / `find_session_workspace` 都走这条）。
会话一旦变热（`save_session` / `save_todos`）就写回 plain 并删掉 `.gz`。

**入口**：

- `POST /api/workspaces/{id}/gc`（body 可覆盖上限，`{"dry_run":true}` 只报告）；
- 每轮跑完自动清一次（同一工作区 10 分钟一次；没设任何上限就**不扫盘**）；
- `tea_agent --gc [--gc-dry-run] [--max-age-days N] [--max-bytes N]
  [--compress-after-days N] [--keep-recent N]`——**服务没开也能清**（设备上「磁盘满了」
  不是启动服务的时刻）。

**坑**：

- `read_file_maybe_gzip(path, out, err, max_bytes)` 的第 3 个参数是 **err** 不是上限，
  传错会编译不过（编译器会拦住，但别浪费一轮编译）。
- `Workspace::open()` 会把 `<run_dir>/settings.yaml` 叠在机器设置上，所以**每个工作区
  可以有自己的一套留存上限**（`history_*` 四个键）。
- `delete_session()` 现在连带删 `trajectories/<sid>.jsonl`（和 `.gz`）——不然删了会话，
  搜索还能搜到它的轨迹。
- `find_session_workspace()` 之前只看 `e.path + "/.tea_agent_run"`（忽略了 `run_dir` 兜底
  路径）和 plain 文件；两处都修了。
- 测试：`tests/smoke_gc.py`（29 项）。
