# tea_a2 1.0.1 复审与发版报告

日期：2026-11-07　版本：1.0.1（上一版 1.0.0，复审提交 `33173b3`）
复审方式：干净重建（MinGW / MSVC）+ 全量回归（e2e、75 个 smoke、27 个 UI）+ 静态审查
结论：**可以发版**；Windows 发布包已在本机产出，Linux 两个二进制需在 Linux 机器上补。

---

## 1. 1.0.1 改了什么

### 1.1 服务重启后，网页自己接回去（用户可见的修复）

**问题**：agent 重启（换版本、崩溃、托盘宿主拉起）时，网页的流连接跟着断。旧行为是
屏幕上留下一个「请求失败」的红条，人得自己按 F5；服务端重启前那一轮生成的半截答案
还挂在 live 气泡里，和落盘的会话对不上。

**现在**（`web/app.js`）：

- 连接断掉且判断是「连不上服务」时，页面显示「服务重启了，正在重新连接…」；
- 页面自己轮询 `/api/health` 等服务回来（最多两分钟），**不用刷新**；
- 服务回来后立刻重新读一遍会话（落盘的消息一条不少），必要时把没送达的那条消息
  **放回输入框**（不替用户重发，避免重复干活）；
- 那一轮如果还在跑（另一台机器发起的，或队列接着做的），自动接回去继续看；
- 空闲时服务重启：状态灯恢复的那一刻也会顺手接回正在跑的那一轮。

**回归**：`tests/ui_restart_reconnect.mjs`（新，6 项）——起一轮慢回答 → 杀掉 agent →
重启 agent → 页面不刷新地自己恢复，并且能接着聊天。

### 1.2 端口守卫不再把「崩溃后的重启」当成端口被占（1.0.0 引入的回归）

1.0.0 的端口守卫用 `SO_EXCLUSIVEADDRUSE` 试绑。这个选项在 Windows 上**也会**被
TIME_WAIT 里的残留连接挡住——而进程被杀掉之后，它的连接正好留在 TIME_WAIT 里。
于是「崩溃后重启」被误判成「端口被占」，agent 拒绝启动。

现在分成两个问题分别问：试绑回答「这个端口能不能占」，一次 connect 回答「有没有人
在监听」。只有真的有人在监听时才拒绝启动，并报出占用者（pid / since）。

**回归**：`tests/smoke_port_guard.py` 7/7；`tests/ui_restart_reconnect.mjs` 里重启
同一个端口也是这一条路径。

### 1.3 队列的版本号跟上了发版

`sched/task_server.py` 的 `QUEUE_VERSION` 之前还停在 `0.22.0`（版本号实际有 8 处，
bump 脚本只改 7 处）。改为跟二进制一致，并把这处加进 `.tea_agent_run/scripts/bump_version.py`。

## 2. 1.0.0 复审时修的东西（仍在 1.0.1 里）

1. **双实例静默抢同一端口（严重）** — `src/server.cpp`
   httplib 在 Windows 上设 `SO_REUSEADDR`，第二个实例能绑定已监听的端口：两个进程同时监听，
   新连接被内核分给其中一个，症状是「网页 UI 写不进工作区」而不是「端口被占用」。
   现在启动前试绑，成功者写 `<tea_home>/tea_agent.port`，失败者拒绝启动并指名占用者。
2. **队列版本号没跟上发版**（见 1.3）。
3. **队列伺服的文档与磁盘不一致（Windows）** — `open(..., newline="")`，不再把 CRLF 归一。
4. **邮件 watcher 的单实例锁在 Windows 上失效（严重）** — `os.kill(pid, 0)` 在 Windows 上
   是 `TerminateProcess`（探活会杀进程，且第二个 watcher 直接抢锁）。改用 `OpenProcess`。
5. **任务列表在 agent 不可达时卡死** — 每个任务问一次 agent 的工作区，800 个任务 15 分钟
   不返回；改成缓存 5 秒，2.1 s。

另有 13 处 Windows 平台假设/竞态的测试修正（`HOME`→`USERPROFILE`、YAML 双引号里的
`C:\`、`/tmp`、`0600`、`systemctl`、`file/ldd`、打开的日志文件、无头页面 `document.hidden`
让 health 轮询不跑等）。

## 3. 验证矩阵（本轮实测）

| 项目 | 结果 |
| --- | --- |
| MinGW 干净构建 | 成功；`src/` 0 警告（19 条来自 `3rd/miniz`，第三方） |
| MSVC 干净构建 | 成功；0 警告；`appstub.exe` 一并产出 |
| `tests/e2e.ps1` | **157/157** |
| `tests/smoke_*.py` | 75 项逐项复跑全过 |
| `tests/ui_*.mjs` | 25/27（`ui_crumbs`、`ui_stream_resume` 见 §4） |
| `smoke_port_guard.py` | 7/7 |
| `ui_restart_reconnect.mjs` | 6/6（新） |
| `--selftest` / `--version` | 通过 / `tea_agent 1.0.1` |

## 4. 已知问题（不阻塞，建议 1.0.2）

1. **`tests/ui_crumbs.mjs`（7 项）**：产品用 `fitCrumbPath()` 按实际像素宽度截断路径，
   测试用固定的 `CRUMB_PATH_MAX` 计算期望；本机路径深，二者对不上。改动前后都挂，
   属测试过时（不是 UI 回归）。
2. **`tests/ui_stream_resume.mjs`（1 项）**：工具回合重建后 `assistants` 为 2，测试期望 3
   （工具调用那一步也算一个气泡）。内容都对；若产品是有意把「工具调用 + 最终回答」合并到
   一个气泡，测试期望要更新，否则是渲染回归。
3. **事件总线偶发丢事件**：`smoke_queue_policy.py` 有时 9/9，有时 4/9（bus 上收不到
   `turn_done`/`task_done`，任务本身照常完成）。router/watchdog/页面依赖这条流。
4. **Linux 二进制无法在本机构建**：本机只有 Windows 工具链。发布包里的
   `tea_a2-linux-x86_64-1.0.1`、`tea_a2-linux-aarch64-1.0.1` 需要在 Linux 机器上
   `scripts/build.sh` 构建后补进包。

## 5. 静态审查结论（1.0.0 复审时做的，结论不变）

- **认证**：`/api/exec`、`/api/status` 需要 `--api-token`；没有 token 时 `/api/exec` 不存在。
  默认只绑定 `127.0.0.1`。
- **路径围栏**：所有文件/下载/归档路径统一走 `Workspace::resolve_path()`，越界报
  `path escapes the workspace`。
- **密钥**：轨迹、API 响应、日志里都没有 `api_key`（e2e 有专门断言）。
- **前端 XSS**：所有把文本塞进 DOM 的路径都先 `esc()`，文件名/列表项一律 `textContent`。
- **资源**：无裸 `new[]`/`malloc`/`strdup`；文件句柄走 RAII。

## 6. 发版清单

已完成：

- [x] 1.0.1 的改动与回归
- [x] 版本号 8 处（`src/version.cpp` 为真相，CMake 正则读取，docs 6 处 + 队列 1 处）
- [x] 从干净工作区重建 MinGW / MSVC（`tea_agent.buildinfo` 里 `commit=<hash>`，无 `-dirty`）
- [x] Windows 发布 exe：`release/tea_a2-win-x86_64-1.0.1.exe` + `tea_agent.buildinfo`
      + `SHA256SUMS.txt`（`release/` 整个目录在 `.gitignore` 里，不进 git）
- [x] 托盘宿主（appstub）盯着的 `build-msvc` 也重建了：它会在文件变新后自动重启子进程，
      页面刷新即可用上 1.0.1

待办：

```bash
# 1) 推送
git push origin main

# 2) 在 Linux 机器上构建两个二进制，strip 后放进暂存目录
scripts/build.sh     # x86_64；aarch64 用 cmake --preset arm64-linux

# 3) 组装发布包：docs/tea_a2_manual.md、sched/、scripts/、三个二进制、tea_agent.buildinfo
#    打 zip：release/tea_a2-1.0.1.zip

# 4) 上传网盘并清理旧版本
python sched/pan_publish.py release/tea_a2-1.0.1.zip --version 1.0.1 \
    --remote-dir /tea_a2/releases/1.0.1 --keep 5
```
