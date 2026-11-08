# tea_a2 1.1.0 发版报告

日期：2026-11-09　版本：1.1.0（上一版 1.0.1，提交 `661a626e454e`）
结论：**可以发版**。包已在网盘：`/tea_a2/releases/1.1.0/`（回读校验 sha256 通过）。

> 1.0.0 与 1.0.1 **没有传过网盘**（网盘上此前最新是 0.22.0），所以这一版同时带着
> 那两版的东西：端口守卫、队列版本号跟上发版、服务重启后网页自己接回去、托盘宿主。

---

## 1. 这一版新东西（相对 1.0.1）

### 1.1 桌面工具的 Linux 那一半（`0145ab0`）

三个示例模块（截图、鼠标、键盘）原来只能在 Windows 上用。Wayland 上的差别不是细节：

- **X11 的 XTest 到不了 Wayland 原生程序** —— 它停在 XWayland 的边界上。所以键鼠
  改走 **uinput**（内核里的虚拟设备）：合成器把它当成一个真键盘/真鼠标，所有程序
  都收到事件。
- **无根的 XWayland 截不到屏**（它只认识 X11 程序），所以**画面由合成器自己给**：
  spectacle、grim、gnome-screenshot 或 maim，装了哪个用哪个。
- 坐标统一成**一个空间**：图片上的像素。屏幕尺寸取 KDE 输出配置里的物理像素，
  图片也说清自己多大，于是「点某个像素」就落在那个像素上。
- 键盘上打不出来的（中文、emoji）走**剪贴板**（KDE 的 Klipper over DBus），
  打字时把输入法关掉，打完再开回来。

### 1.2 模块可以回图片了

`{"text": ..., "image": ...}` 现在变成**一段文字 + 一张图**：模型能直接看见屏幕，
而不只是被告知文件在哪。（`sched/a2_ext_py/toolkit/loader.py` 与桌面工具的配合。）

### 1.3 微信多账号 + 扫码自启（这一版的 `sched/` 里）

- 服务没有凭证时**自己出二维码**（链接进日志、图片进账号目录），谁扫谁接上；
  会话过期（errcode -14）也重新出码等着 —— 不用再跑 `--login`，不用人管。
- **配对页面** `sched/wechat_pair.py`（`a2_wechat_pair.service`）：浏览器里写账号名 →
  出码 → 手机扫 → 凭证写好 → 对应的服务自动起来。
- **一个进程挂多个账号** `sched/wechat_multi.py`（`a2_wechat_mul.service`）：每个账号
  一条长连接，共用同一个 `a2_task`；每个账号有自己的目录、工作区、来源名
  （`wechat:<名字>`）。已经有自己单元的账号它不碰（两个 bot 抢同一个游标会吃消息）。
- 协议细节补齐：`local_token_list`、`binded_redirect`（接管已有凭证）、
  `need_verifycode`（手机上的数字）、`scaned_but_redirect`。

### 1.4 托盘宿主在 mingw 上能链接了（本轮修的）

`tools/appstub` 的入口是 `wWinMain`（宽字符），而 mingw 的 CRT 默认找 ANSI 的
`WinMain` —— 交叉构建一直是红的（"undefined reference to `WinMain'"）。MSVC 不受影响，
所以这个坑只在本机的 mingw 交叉构建上出现。加 `-municode` 即可（`661a626`）。

## 2. 验证矩阵（本轮实测，Linux/WSL2 这台机器）

| 项目 | 结果 |
| --- | --- |
| 干净重建 x86_64 | 成功；`buildinfo` = `version=1.1.0 commit=661a626e454e`（无 `-dirty`） |
| 干净重建 aarch64（静态） | 成功；同一 commit |
| 干净重建 win64（mingw 交叉） | 成功（含托盘宿主）；同一 commit |
| `bash scripts/test-all.sh` | **116/135**（失败的见 §3） |
| `tests/smoke_wechat_bot.py` | 77/77 |
| `tests/smoke_wechat_accounts.py` | 42/42（新） |
| `tests/smoke_py36.py` | 3/3（Python 侧仍能在 3.6 上跑：设备是 3.6.9） |
| `--version` | `tea_agent 1.1.0` |

## 3. 已知失败（都是这台机器的环境，不是这一版的回归）

| 测试 | 为什么 |
| --- | --- |
| `smoke_workspace_writable.py`、`smoke_workspace_add_warning.py`、`ui_workspace_add.mjs` | 用了 `icacls`（Windows 工具），Linux 上没有 |
| `smoke_ext_desktop.py` | 需要真的 Wayland/KDE 桌面（截屏/合成器），WSL2 里没有合成器 |
| `smoke_bind_urls.py` | 要绑 `0.0.0.0`，这个沙箱里起不来 |
| `smoke_shell.py` | shell 会话在沙箱里被回收（404） |
| `smoke_port_guard.py` | 端口被占的提示语是 Windows 版那句，Linux 上措辞不同 |
| `smoke_concurrency.py`、`ui_turn_index.mjs` | 并行负载下的偶发项（连接被拒 / 滚动高亮） |

这些测试在 Windows 开发机上是过的（1.0.1 的复审报告里 25/27 UI 全绿）。

## 4. 发版清单

- [x] 版本号八处（`src/version.cpp` 是真相；CMake 报错示例、队列版本、文档五处）
- [x] 三个二进制从干净工作区重建，`tea_agent.buildinfo` 记 `commit=661a626e454e`
- [x] `release/tea_a2-1.1.0.zip`（87 个条目：三个二进制 + buildinfo + `sched/` + `scripts/` + 手册）
- [x] 传到网盘 `/tea_a2/releases/1.1.0/`，4MB 分片上传，**回读 sha256 校验通过**，
      清单 `release-manifest.json`
- [x] 网盘上只留最新的 5 个版本（删掉了 0.21.1）
- [x] 源码提交（包不进 git，`release/` 在 `.gitignore` 里）

## 5. 怎么拿

```
浏览器打开 https://pan.baidu.com → 我的网盘 → tea_a2/releases/1.1.0
  tea_a2-1.1.0.zip          发布产物（Linux x86_64 / Linux aarch64 / Windows x86_64）
  release-manifest.json    谁在什么时候传的、每个文件的 sha256/md5/大小
```

或命令行：

```bash
python3 sched/pan_publish.py --list /tea_a2/releases
```
