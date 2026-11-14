# appstub —— Windows 上的托盘宿主

> **只在 Windows 上用。** Linux 上同样的活儿由 **systemd --user 单元**管
> （本仓库的 `scripts/a2.service`、`scripts/a2_task.service`）。

一个托盘图标，把几个程序看着：启动、盯着（挂了拉起来、构建了重启）、右键菜单。

典型用法：把 tea_a2 的 agent（`tea_agent.exe`）和任务队列（`task_server.py`）交给它。

## 为什么不是 Windows 服务

Windows 服务活在 **session 0**，而且默认以 **LocalSystem** 身份运行：

| | 服务 | 托盘程序 |
| --- | --- | --- |
| `%USERPROFILE%` | `C:\Windows\System32\config\systemprofile` | 你自己的 |
| 新建文件的属主 | `SYSTEM` / `Administrators` | 你自己 |

agent 这类工具要读写你自己的仓库、用你自己的 provider 密钥；服务那套 session 0 +
LocalSystem 会把这些全搞坏（`git` 还会因为 "dubious ownership" 拒绝操作你的仓库）。
托盘程序跑在**你自己的登录会话**里 —— 它在 Windows 上对应的就是 `systemctl --user enable`
挂的那个登录触发任务，只不过多了个图标可点。

## 用法

```
appstub.exe [--config <文件>] [--no-autostart] [--version]
```

* `--config`：配置文件。默认找 `<exe 同目录>\config.json`，没有就找
  `%LOCALAPPDATA%\appstub\config.json`。
* `--no-autostart`：起来了，但先把配置里 `autostart` 的程序都不启动。
* `--version`：打印自己的版本和它托管的各个程序的版本，然后退出（不启动
  托盘图标）。

## 配置

一份 JSON（`config/appstub.json` 是本机用的那份）：

```json
{
  "log_dir": "%LOCALAPPDATA%\\appstub",
  "apps": [
    {
      "id": "agent",
      "name": "tea_a2 agent",
      "command": "C:\\...\\tea_agent.exe",
      "args": ["--port", "8383"],
      "cwd": "C:\\...\\tea_a2",
      "env": { "TEA_AGENT_HOME": "%USERPROFILE%\\.tea_agent" },
      "port": 8383,
      "url": "http://127.0.0.1:8383/",
      "autostart": true,
      "restart": "on-failure",
      "restart_delay_ms": 3000
    }
  ]
}
```

| 字段 | 干什么 |
| --- | --- |
| `id` | 标识（写进日志）；不写就用 `name` |
| `name` | 菜单里显示的名字 |
| `command` | 要跑的 exe（必填） |
| `args` | 参数数组 |
| `cwd` | 工作目录 |
| `autostart` | 启动时是否自动拉起 |
| `restart` | `on-failure`（退出码非 0 就重启，默认）或 `never` |
| `restart_delay_ms` | 重启前等多久（默认 3000） |
| `watch_file` | 盯哪个文件（默认就是 `command`）。脚本型程序盯脚本：`"watch_file": "…\\task_server.py"` |
| `version_args` | 用什么参数问程序要版本号（默认 `["--version"]`）。空的数组 = 不问 |

## 版本

托盘菜单里，每个程序后面都写着它自己报的版本号：

```
tea_a2 agent  —  运行中 (pid 19664)  ·  tea_agent 1.3.0
tea_a2 任务队列  —  运行中 (pid 17644)  ·  tea-task-server 1.3.0
```

问法是把它平常的参数再加上 `--version` 跑一遍，输出的第一行就是版本号
（`tea_agent 1.3.0`）。**程序不认识这个参数，就什么都不显示** —— 瞎编一个
版本号比没有版本号更糟。

* 启动时问一次；程序文件被重新编译过（就是被盯着改的那个文件）就再问一次。
* 问的动作最多等 5 秒：万一有程序不理这个参数、自己跑起来了，就把它停掉。
* `version_args` 可以换问法（`["-V"]` 之类）；写成 `[]` 就不问了。
* 命令行上：`appstub --version` 打印同样的内容。

## 构建

**只支持 Windows。** 需要 CMake ≥ 3.16 和一个 C++17 编译器（**MinGW-w64**）。

跟着仓库一起编（Windows 上默认就会编它）：

```bat
scripts\build-mingw.bat
:: 输出：build-mingw\bin\appstub.exe
```

只编它一个：

```bat
cmake --build build-mingw --target appstub
```

不要它：`-DTEA_BUILD_APPSTUB=OFF`。

## 部署与开机自启

```bat
:: 复制 exe + 配置到 %LOCALAPPDATA%\appstub（这是真正被运行的那份）
tools\appstub\scripts\deploy.ps1

tools\appstub\scripts\install-autostart.ps1
tools\appstub\scripts\install-autostart.ps1 -Status
tools\appstub\scripts\install-autostart.ps1 -Uninstall
```

`deploy.ps1` 是**优雅停止**旧实例的（`taskkill` 不带 `/F`，即发 WM_CLOSE）：程序在
窗口销毁时会删掉自己的托盘图标。直接杀进程的话，通知栏里会留下一个**幽灵图标**——
shell 要等鼠标滑上去才把它清掉。

## 盯着构建产物

配置里可以给每个程序一个 `watch_file`（默认就是它自己的 exe）。文件比正在跑的那份
新，就重启它：

```
[a2-agent] the program file is newer than the running instance - restarting it
[a2-agent] stopping (pid 25104)
[a2-agent] started (pid 39404): …\tea_agent.exe --port 8383 …
```

这也是为什么链接器不能覆盖正在运行的 exe：仓库里的
`cmake/free-running-exe.cmake` 会在链接前把旧的那份改名、链接后删掉。

## 日志

`%LOCALAPPDATA%\appstub\appstub.log`：启动、退出、崩溃、重启都记在这里。

## 已知的事

* **别和手动实例同时跑**：托盘程序拉起来的 agent 和你手敲的 agent 用同一个 tea home
  （`%USERPROFILE%\.tea_agent`），两个进程会抢 `workspaces.json`。
* 托盘图标必须注册在一个**真实存在的窗口**上：注册在空窗口（`NULL`）上的图标
  shell 留不住，悬停重绘时就没了。
