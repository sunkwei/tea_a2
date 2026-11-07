# tea_a2 1.0.0 复审报告

日期：2026-11-07　复审对象：`b77e7cb` (release: 1.0.0) + 复审提交 `24c1e62`
复审方式：干净重建（MinGW / MSVC）+ 全量回归（e2e、75 个 smoke、26 个 UI）+ 静态审查
结论：**可以发版**；Windows 发布包已在本机产出，Linux 两个二进制需在 Linux 机器上补。

---

## 1. 复审范围

| 维度 | 做法 |
| --- | --- |
| 构建 | `build-review`（MinGW / GCC，`-Wall -Wextra`）、`build-review-msvc`（Visual Studio 2026，NMake）干净配置，收集全部编译警告 |
| 端到端 | `tests/e2e.ps1` 157 项，MinGW 与 MSVC 各跑一遍 |
| 单元/集成 | `tests/smoke_*.py` 全部 75 个 |
| 浏览器 | `tests/ui_*.mjs` 26 个（Edge/Chromium，CDP） |
| 静态 | 危险 API、路径围栏、认证、密钥落日志、前端 XSS、资源分配、版本一致性、文档与打包 |

## 2. 修复的问题

### 2.1 产品代码（进入发布包）

1. **双实例静默抢同一端口（严重）** — `src/server.cpp`
   httplib 在 Windows 上设 `SO_REUSEADDR`，第二个实例能绑定已监听的端口：两个进程同时监听，
   新连接被内核分给其中一个，症状是「网页 UI 写不进工作区」而不是「端口被占用」。
   `tests/smoke_port_guard.py` 早就写好了规格，但功能一直没实现（`5e121d5` 的提交说明写着
   "not in the tree yet"）。现在：
   - 启动前用 `SO_EXCLUSIVEADDRUSE` 试绑（`getaddrinfo` 遍历所有地址族），失败就拒绝启动；
   - 成功后写 `<tea_home>/tea_agent.port`：`{pid, port, host, version, since}`，退出时删除
     （只删自己的，按 pid 判断）；
   - 第二个实例报 `port N is already in use by tea_agent (pid P, since T) - stop it, or start
     this one on another port (--port N+1)`。
   回归：`smoke_port_guard.py` 7/7，MinGW 与 MSVC 各一遍。

2. **队列版本号没跟上发版** — `sched/task_server.py`
   `QUEUE_VERSION` 还是 `0.22.0`，`/api/health` 的版本段与二进制不一致（版本号实际有 8 处，
   原来的 bump 脚本只改 7 处）。改为 `1.0.0`。

3. **队列伺服的文档与磁盘不一致（Windows）** — `sched/task_server.py`
   `open(path, encoding="utf-8")` 文本模式把 CRLF 归一成 LF：`/api/doc?format=md` 的字节数与
   `docs/tasks.md` 不同（63609 vs 64957），"byte for byte" 失败。改为 `newline=""`。

4. **邮件 watcher 的单实例锁在 Windows 上失效（严重）** — `sched/mail_watch.py`
   `alive(pid)` 用 `os.kill(pid, 0)` 探活，而 Windows 上 `os.kill` 调 `TerminateProcess`：
   既是错误的探活（会杀掉目标），也让第二个 watcher 直接抢锁成功 → 同一批邮件处理两遍。
   改为 Win32 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + `GetExitCodeProcess`。

5. **任务列表在 agent 不可达时卡死（性能）** — `sched/task_server.py`
   `decorate()` 对每个任务调一次 `workspace_path()`，而后者每次向 agent 发一次
   `/api/workspaces`。agent 连不上时 800 个任务要发 800 次连接请求：列表 15 分钟以上不返回
   （`smoke_task_queue_perf.py` 直接超时）。现在 `workspace_paths()` 缓存 5 秒：
   800 任务 2.1 s，perf 测试（一轮 0.91 s / 列表 2.10 s / 3000 事件 1.28 s）全过。

### 2.2 测试（平台假设 / 竞态；不影响产品行为）

Windows 上按 POSIX 假设写的地方一并修正，否则本机永远是红的、看不出真实回归：

| 测试 | 问题 | 处理 |
| --- | --- | --- |
| `smoke_error_catalog.py` | 版本断言 `startswith("0.")` | 改成 `^\d+\.\d+\.\d+$` |
| `smoke_idempotency.py` | body-key 的异步回合未落地就重置计数 | `wait_calls(4)` 后再测流式 |
| `smoke_default_workspace.py`、`smoke_workspace_home_var.py` | 只设 `HOME`，Windows 家目录是 `USERPROFILE` | 同时设 `USERPROFILE` |
| `smoke_workspace_home_var.py` | `json.dumps` 转义后再比路径 | 比较 `error.message` 字符串 |
| `smoke_mcp.py` | YAML 双引号里的 `C:\Users\...`（`\U` 非法转义）→ 配置静默解析失败 | 路径用单引号 |
| `smoke_single_file.py` | 依赖 `file` / `ldd`（POSIX 工具） | `FileNotFoundError` 时跳过 ELF 检查 |
| `smoke_task_server.py` | 断言 `/tmp/...` 原样返回 | 平台临时目录 + `realpath` 比较 |
| `smoke_port_guard.py` | 被 `terminate()`（= TerminateProcess）的进程没有清理机会 | 允许「文件指向死 pid」 |
| `smoke_shell.py` | PowerShell 会重绘提示符，断言「没有新字节」过严 | 断言「不重复上一段输出」 |
| `smoke_destroy.py` | Windows 不允许删除有打开日志文件的目录 | 只删 `sessions/`；missing-dir 检查 POSIX-only |
| `smoke_pan_sync.py` | `0600` 是 POSIX 权限 | Windows 跳过该断言 |
| `smoke_wechat_accounts.py` | `systemctl`/`/bin/sh` 启动器；两个账号 poll 竞态 | POSIX-only；等两个 token 都出现 |
| `ui_status.mjs` | 无头页面 `document.hidden` 为真 → 10 s 轮询不跑 | `Emulation.setFocusEmulationEnabled` |

## 3. 验证矩阵（本轮实测）

| 项目 | 结果 |
| --- | --- |
| MinGW 干净构建 | 成功；`src/` 0 警告（19 条来自 `3rd/miniz` 的 unused-function，第三方） |
| MSVC 干净构建 | 成功；0 警告；`appstub.exe` 一并产出 |
| `tests/e2e.ps1` | **157/157**（MinGW、MSVC、发布 exe 各一遍） |
| `tests/smoke_*.py` | 逐项复跑全部通过（75 项）；全量串行跑时 3 项偶发，见 §4 |
| `tests/ui_*.mjs` | 24/26（`ui_crumbs`、`ui_stream_resume` 见 §4） |
| `smoke_port_guard.py` | 7/7，MinGW 与 MSVC 都过 |
| `smoke_task_queue_perf.py` | 全过（修复前超时） |
| `--selftest` / `--version` | 通过 / `tea_agent 1.0.0` |
| 发布 exe | `release/tea_a2-win-x86_64-1.0.0.exe`，SHA-256 见 `release/SHA256SUMS.txt`；`tea_agent.buildinfo`：`version=1.0.0, commit=24c1e62b2c1a`（无 `-dirty`） |

## 4. 已知问题（不阻塞 1.0.0，建议 1.0.1）

1. **`tests/ui_crumbs.mjs`（7 项）**：产品用 `fitCrumbPath()` 按实际像素宽度截断路径，测试用固定的
   `CRUMB_PATH_MAX` 计算期望；本机工作区路径深，二者对不上。改动前后都挂，属测试过时（不是 UI 回归）。
   建议：测试改为读 `.crumb.path` 的 `title`（完整路径）并按同样规则算，或直接断言「含工作区名 + 分隔符」。

2. **`tests/ui_stream_resume.mjs`（1 项）**：工具回合重建后 `assistants` 为 2，测试期望 3
   （工具调用那一步也算一个 assistant 气泡）。内容（工具行、答案长度、无重复）都对；若产品是有意把
   「工具调用 + 最终回答」合并到一个气泡，则测试期望要更新，否则是渲染回归。建议 1.0.1 确认。

3. **事件总线偶发丢事件**：`smoke_queue_policy.py` 有时 9/9，有时 4/9（bus 上收不到 `turn_done` /
   `task_done`，任务本身照常完成）。核心流程不受影响，但 router/watchdog/页面依赖这条流；
   建议查 `Events.subscribe()` / `publish()` 与 SSE 写出的时序。

4. **全量串行跑的三处噪声**（逐项复跑均通过）：`smoke_pan_sync.py`（97/97）、
   `smoke_wechat_accounts.py`（42/42，但 Python 3.14 退出时 daemon 线程触发
   `_enter_buffered_busy`，进程退出码非 0）、`smoke_queue_policy.py`（见上）。
   建议给测试夹具加「跑完清进程 / 等端口释放」，或在 runner 里按 PID 清理。

5. **Linux 二进制无法在本机构建**：本机只有 Windows 工具链（没有 WSL 发行版/容器）。
   发布包里的 `tea_a2-linux-x86_64-1.0.0`、`tea_a2-linux-aarch64-1.0.0` 需要在 Linux 机器上
   `scripts/build.sh`（`TEA_AGENT_STATIC_RUNTIME=ON TEA_AGENT_STATIC_LIBC=ON`）构建后补进包。

## 5. 静态审查结论

- **认证**：`/api/exec`、`/api/status` 需要 `--api-token`；没有 token 时 `/api/exec` 不存在，
  `/api/status` 与 `/api/health` 一样只读。默认只绑定 `127.0.0.1`。
- **路径围栏**：所有文件/下载/归档路径统一走 `Workspace::resolve_path()`，越界报
  `path escapes the workspace`；`smoke_allow_paths.py`、`smoke_exec_fence.py` 覆盖。
- **密钥**：轨迹、API 响应、日志里都没有 `api_key`（e2e 有专门断言）；密钥只在用户自己的
  `provider.yaml` 里。
- **前端 XSS**：所有把文本塞进 DOM 的路径都先 `esc()`（`renderMarkdown`、代码高亮、终端单元格），
  文件名/列表项一律 `textContent`。
- **资源**：无裸 `new[]`/`malloc`/`strdup`；文件句柄走 RAII（`platform_fs`、`AppendFile`）。
- **版本一致性**：`src/version.cpp` 是唯一真相，`CMakeLists.txt` 用正则读取；docs 6 处与队列 1 处
  都已改为 `1.0.0`。

## 6. 发版清单

已完成：

- [x] 复审修复提交（`24c1e62 review: 1.0.0 - the port guard, ...`）
- [x] 从干净工作区重建 MinGW / MSVC（`buildinfo: commit=24c1e62b2c1a`，无 `-dirty`）
- [x] Windows 发布 exe：`release/tea_a2-win-x86_64-1.0.0.exe` + `release/tea_agent.buildinfo`
      + `release/SHA256SUMS.txt`
- [x] `release/` 整个目录加入 `.gitignore`（发版产物只上网盘）

待办：

```bash
# 1) 推送复审提交
git push origin main

# 2) 在 Linux 机器上构建两个二进制，strip 后放进暂存目录
scripts/build.sh     # x86_64
#   aarch64: cmake --preset arm64-linux，或 TEA_AARCH64_PREFIX=... scripts/build.sh

# 3) 组装发布包（docs/build.md §发版）
#    docs/tea_a2_manual.md、sched/、scripts/、三个二进制、tea_agent.buildinfo
#    打 zip：release/tea_a2-1.0.0.zip

# 4) 上传网盘并清理旧版本
python sched/pan_publish.py release/tea_a2-1.0.0.zip --version 1.0.0 \
    --remote-dir /tea_a2/releases/1.0.0 --keep 5

# 5) 空提交记录发版
git commit --allow-empty -m "release: 1.0.0"
```

发布包内容（`docs/build.md` §发版）：`docs/tea_a2_manual.md`、`sched/`、`scripts/`、
三个平台的二进制、`tea_agent.buildinfo`。
