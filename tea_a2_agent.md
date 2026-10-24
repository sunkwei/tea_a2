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

修法：迭代循环内加压力检查、思考模型摘要预算 4096 且「只回 reasoning」时用 4 倍预算重试、
估算改 3 字符/token + reasoning 全额 + 窗口预算留 10% 余量、折叠边界对齐到 user 消息。
（**这套修复目前不在树里**，见[进行中](#进行中红着的测试)。）

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

## 进行中：红着的测试

下面三个测试是**上一轮留下的规格**：测试在仓库里，但**对应的实现不在树里**（那次会话的代码没
落地）。跑它们会红——这是**已知状态**，不是新回归：

| 测试 | 现在 | 它钉住的东西（还没实现） |
| --- | --- | --- |
| `tests/smoke_port_guard.py` | 4/7 | 启动前独占端口（`SO_EXCLUSIVEADDRUSE`），被占就拒绝启动并指名占用者；`<tea_home>/tea_agent.port` 写 pid + 启动时间，退出时删掉 |
| `tests/smoke_workspace_writable.py` | 3/6 | 工作区列表要报 `writable`；写不进去的工作区是只读，并给出原因 |
| `tests/smoke_compaction_overflow.py` | 3/10 | 上下文超窗的那套修复（迭代内压力检查、摘要 4096 预算、只回 reasoning 时重试、token 估算） |

要动手时先跑一遍它们：**它们变绿，就是那件事做完了**。
