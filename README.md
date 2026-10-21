<div align="center">

# tea_a2

**A tiny C++ agent runtime for embedded devices — one self-contained binary that exposes a local
HTTP/SSE API, so a larger agent can drive real work on the device.**

把大模型 agent 的「手和脚」放到设备上：设备只跑 `tea_a2`，推理与决策留在远端。

<br/>

<a href="https://github.com/sunkwei/tea_a2"><img alt="repo" src="https://img.shields.io/badge/repo-sunkwei%2Ftea__a2-181717.svg"></a>
<img alt="c++" src="https://img.shields.io/badge/C%2B%2B-17-00599C.svg">
<img alt="build" src="https://img.shields.io/badge/build-CMake-064F8C.svg">
<img alt="deps" src="https://img.shields.io/badge/dependencies-vendored%20in%203rd%2F-2ea44f.svg">
<img alt="verified" src="https://img.shields.io/badge/verified-Windows%20x64%20%C2%B7%20GCC%20%2B%20MSVC-2ea44f.svg">
<img alt="runs" src="https://img.shields.io/badge/targets-Windows%20%C2%B7%20Linux%20%C2%B7%20macOS-6e7681.svg">

</div>

---

## 这个项目解决什么问题

嵌入式设备上通常跑不了通用 agent，「在设备上干活」需要的是本地能力：执行命令、读写文件、
记住上下文。`tea_a2` 就是这部分能力的最小实现：

```
┌──────────────────────┐        HTTP / SSE (JSON)        ┌───────────────────────────┐
│  Remote brain        │  ───────────────────────────▶  │  Embedded device          │
│  (LLM agent /        │                                 │  ┌─────────────────────┐  │
│   orchestrator /     │   POST /api/chat                │  │ tea_a2 (single exe) │  │
│   云端 workflow)     │   ◀─── content_delta / tools ── │  │  · agent loop       │  │
└──────────────────────┘                                 │  │  · tools            │  │
                                                         │  │  · workspaces       │  │
                                                         │  │  · built-in Web UI  │  │
                                                         │  └─────────┬───────────┘  │
                                                         │            │              │
                                                         │     本地文件 / 本地命令    │
                                                         └───────────────────────────┘
```

两种用法：

| 模式 | 说明 |
| --- | --- |
| **被控端（主要用途）** | 设备上只跑 `tea_a2`，上层 agent 通过 `/api/chat` 下发任务，用 SSE 收结果和工具执行过程 |
| **独立运行** | 设备或开发机上直接打开内置 Web UI，人机对话；配置里直接填 LLM 的 `api_url` / `api_key` |

设计上的取舍都是为「一个文件丢上设备就能跑」：

- **单进制**：Web UI 在构建期被嵌入二进制，没有静态资源目录、没有前端构建链、没有 CDN。
- **零外部库**：nlohmann/json、cpp-httplib、yaml-cpp、mbedTLS 全部 vendored 在 `3rd/`，不依赖 vcpkg / conan / 系统开发包。
- **不依赖 OpenSSL / zlib / Python / Perl**：TLS 由 mbedTLS 承担；构建期没有任何脚本语言参与。
- **跨平台同一套代码**：路径、文件 I/O、进程、SIGPIPE 都按平台封装，没有 `std::filesystem`。

## ⚠️ 安全须知

**这个程序由 tea_agent 项目创建 0.1 版本；之后版本由 a2 自己进化完成，模型使用 deepseek-flash **

**这个程序没有任何认证机制。** `toolkit_exec` 可以执行任意系统命令，所以：

- 默认绑定 `127.0.0.1`，只允许本机访问——请保持这个默认值。
- 用 `--host 0.0.0.0` 或放到公网之前，必须自己在前面加一层认证（反向代理 + mTLS / token 校验 /
  VPN），否则等同于把设备的 shell 暴露出去。
- 只应部署在受信任的网段，或通过隧道访问。
- 文件工具默认限制在工作区内；`--allow-outside-workspace`（或设置页的「全局」）会取消这个限制，
  慎用——放宽后任何能访问这个端口的人都能读写整台机器上的文件。

---

---

## 快速开始

### 依赖：只有编译器和 CMake

| 角色 | 要求 |
| --- | --- |
| 构建系统 | CMake ≥ 3.20（3.x / 4.x 均可） |
| C++ 编译器 | 支持 C++17（已验证 GCC 6.5 / MinGW GCC 16.2 / MSVC 19.51） |
| C 编译器 | C99，仅用于编译 mbedTLS |
| 生成器 | Ninja / Unix Makefiles / NMake / Xcode |
| 链接库 | 系统自带：Windows `winhttp` `ws2_32` `advapi32`；POSIX `pthread` |

不需要 OpenSSL、zlib、vcpkg、conan、Python、Perl。

### 构建

```bash
# Linux / macOS
./scripts/build.sh

# Linux / macOS（不用脚本，等价）
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"
```

```bat
:: Windows + MinGW-w64
scripts\build-mingw.bat

:: Windows + MSVC（Ninja 在部分环境会卡死，默认走 NMake）
scripts\build.bat
```

产物：`build/bin/tea_agent`（Windows 为 `build-msvc\bin\tea_agent.exe` 等）。
本机编译只有这几条命令；**交叉编译（Linux → Windows / Linux aarch64）见下面的
[跨平台编译](#跨平台编译) 一节**，那里有编译矩阵、toolchain 文件、产物校验与错误对照。

**MSVC 的 `/showIncludes` 刷屏**：本地化的 Visual Studio 会把「包含文件」用本地语言输出，
CMake 拿到这个前缀后要传给 `cmake -E cmake_cl_compile_depends --filter-prefix=…`，而命令行在
控制台代码页里存不住非 ASCII 字节，于是过滤一条也匹配不上——日志里每个编译单元都会多出
上百行 `注意: 包含文件: …`（副作用是 `.obj.d` 为空，改头文件不会触发重编）。修法只有一个：
让 cl.exe 说英文。`scripts\build.bat` 通过 `scripts\vcenv.bat` 设置 `VSLANG=1033`，前缀变成
ASCII 的 `Note: including file: `，问题消失，顺带让诊断信息可搜索。

**关键点：`VSLANG` 必须同时存在于「构建」进程的环境里**，不只是配置时——`cl.exe` 自己读它，
CMake 不读。只在配置时设过、后来在别的窗口里跑 `cmake --build`，日志照样是本地化的。

三种方式任选（都不需要改 IDE 设置）：

```bat
:: 1) 仓库自带脚本（vcenv.bat 里设 VSLANG，构建进程继承）
scripts\build.bat

:: 2) CMake presets（configure/build 两个 preset 都带 VSLANG）
cmake --preset msvc
cmake --build --preset msvc

:: 3) 手敲：同一个窗口里设好再 configure/build
set VSLANG=1033
cmake -S . -B build-msvc -G "NMake Makefiles" -DCMAKE_BUILD_TYPE=Release
cmake --build build-msvc
```

已经配置过的构建目录会把旧前缀缓存在 `CMakeFiles\<ver>\CMakeCXXCompiler.cmake` 里；
CMakeLists 在检测到「本地化前缀 + `VSLANG=1033`」时会自动换成英文前缀并打印一行说明，所以
多数情况下重跑即可，实在顽固就 `cmake --fresh`（或删掉 `CMakeCache.txt`）重配一次。

`scripts\build.bat` 在编译前还会真的跑一次 `cl /showIncludes` 探测：如果这台机器上的 `cl.exe`
无视 `VSLANG`（极少数本地化安装会这样），它会直接打印一条明确的 WARNING，而不是让你对着
几百行 `包含文件` 猜。

### 程序正在运行时也能直接构建（Windows）

Windows 不允许链接器写一个正在运行的 `.exe`：

```
LINK : fatal error LNK1104: 无法打开文件"bin\tea_agent.exe"      (MSVC)
cannot open output file bin\tea_agent.exe: Permission denied     (MinGW)
```

以前只能「先停掉 agent → 构建 → 再启动」，而在开发 UI 时这个循环要走上百遍。其实这个锁没
那么严（实测 Windows 10 19045）：

| 操作 | 运行中的 `.exe` |
| --- | --- |
| `CreateFile(WRITE)`（链接器做的事） | ❌ 失败——这就是 LNK1104 的来源 |
| `DeleteFile`（删除） | ❌ `ERROR_ACCESS_DENIED` |
| `MoveFile`（改名） | ✅ **可以**，名字可以拿走 |

所以 `cmake/free-running-exe.cmake` 用上了第三条：链接前把旧二进制改名为
`tea_agent.exe.prev-<时间戳>`，让链接器在原名上写新文件；链接成功后再删掉那份旧拷贝。

```
Freeing tea_agent.exe for relinking
-- tea_agent: tea_agent.exe was moved to tea_agent.exe.prev-20261019-185712
          (it was still running, so the new binary takes the old name; restart the
           instance to pick up the new code)
```

要点：

- **旧进程不受影响**，继续用内存里那份代码服务请求，直到你重启它；
- 旧拷贝在进程退出前删不掉（被映射着），所以它会留到**下一次构建**的 POST_BUILD 才清理；
- 链接失败也不会丢东西：旧二进制以 `.prev-*` 留在原目录，可以直接改名回去；
- 只在**真的要链接**时才动作（`PRE_LINK`），空构建不会产生任何改名；
- POSIX 上整个步骤直接 return（Linux/macOS 允许覆盖运行中的镜像）；
- 三个产物都覆盖到了：`tea_agent`、`tea_embed_assets`、`tea_embed_ca_bundle`。

回归测试：`pwsh -File tests/relink-while-running.ps1 -Exe build-gcc\bin\tea_agent.exe`
（会真的起一个隐藏实例、构建一次、验证新二进制就位且旧实例仍在服务，再验证 leftovers 清理；
MinGW 与 MSVC 各 12 项）。它先断言「运行中的镜像确实打不开写权限」这个前提，前提不成立就说明
这套机制已经没必要了，而不是静悄悄地通过。

### 跑起来

```bash
# 准备配置（也可以跳过这一步，见下面的「首次运行」）
mkdir -p ~/.tea_agent
cp provider.example.yaml ~/.tea_agent/provider.yaml
$EDITOR ~/.tea_agent/provider.yaml          # 填 api_url / api_key / model

# 启动（默认 127.0.0.1:8282），把当前目录注册为工作区
build/bin/tea_agent --workspace . --open
```

**首次运行**：如果 `$HOME/.tea_agent/provider.yaml` 不存在，服务不会退出，而是以「配置模式」
启动——浏览器打开页面后会被一个不可跳过的对话框挡住。对话框里有两条路：

1. **快捷开始：只填 API key**（最省事，光标已经在这个输入框里）。下拉里选 provider、
   粘贴 key、回车即可：服务端据此生成一份只含该 provider 的 `provider.yaml`，按与上传
   完全相同的流程校验、落盘（POSIX 上权限 `0600`，因为它含密钥）、热加载。

   | provider | 粘贴的 key | 默认模型 | 生成的配置里还有什么 |
   | --- | --- | --- | --- |
   | **DeepSeek** | `sk-…` | `deepseek-flash` | 另附 `deepseek-chat` / `deepseek-reasoner` 备用 |
   | **MiniMax** | M Plan 的 `sk-cp-…` | `MiniMax-M3.1-Flash`（1M 上下文） | 额度徽章（`supports_usage` + `minimax_token_plan`），顶栏直接显示还剩多少 |

   MiniMax 走 `https://api.minimax.cn/v1`（全球站 `https://api.minimax.io/v1` 同样可用，
   改 `api_url` 一行即可）；key 形状和说明会随下拉一起变，不用猜。

2. **上传 / 粘贴完整配置**：点击（或拖入）`provider.yaml`，或直接把内容粘到文本框里，点
   「上传并应用」。

已经配好之后想换一整套 provider，「设置」页的「模型提供方」一节里有 **重置 provider**：
它打开的就是上面这个对话框（快捷 key 通道 + 上传 + 粘贴，一个都不少），但会先说明风险——
现有的 `provider.yaml` 会被覆盖，**旧的 provider、模型和 API key 找不回来**——而且这个对话框
**可以取消**（首次运行那个是不可关闭的：那时没有配置，不配就没法用）。新配置仍然先校验再落盘，
不合法就拒绝写入，不会留下半个文件；有轮次正在跑时也会被拒绝（409）。

两条路都是**先校验再落盘**（校验失败不会覆盖已有文件），成功后页面自动刷新，状态栏从
「未配置」变成「已连接」，可以直接开始对话。快捷通道生成的配置就是普通的 `provider.yaml`，
之后随时可以在「文件」页或编辑器里改（加 provider、换默认模型、打开 DeepSeek 的网页工具等）。
命令行会打印 `starting in setup mode` 和提示地址，方便无 GUI 环境照常启动后再上传。

注意只有**文件不存在**才进入配置模式；文件存在但 YAML 语法错误仍然会直接报错退出——
那种情况下静默启动只会把问题藏起来。

浏览器打开 <http://127.0.0.1:8282> 就是内置 UI：对话、文件浏览编辑、记忆、日志、设置。
「设置」页分成三块：工作区级 **system prompt**（编辑框里就是当前生效的人设，保存后下一轮生效）、
运行环境与模型提供方 / 工具清单，以及最下方的 **危险操作 → 销毁**（删除 tea home 与所有工作区的
`.tea_agent_run`，见「工作区与运行时存储」）。

用 `--host 0.0.0.0` 启动时，启动信息里的 `Web UI` 不再是 `127.0.0.1`，而是**这台设备当前的
活跃地址**（走默认路由的那张网卡，UDP `connect()` 只选源地址、不发包），所以可以直接把它
念给同一局域网的手机/另一台电脑；其余网卡地址排在其后，`127.0.0.1` 放最后并标注
`(this machine only)`。绑定了具体地址时只打印那一个。没有任何可用地址（离线的板子）时回落到
`127.0.0.1`。`--open` 打开的永远是本机回环地址——它跑在服务端这台机器上，不依赖网络状态。
左侧栏右上角的按钮（或 `Ctrl/Cmd+B`）可以把它收起，把横向空间让给正文；收起后顶栏左侧
出现同一个图标，点一下即可展开，状态记在浏览器里，下次打开保持原样。

浏览器标签页的标题是「`{当前工作区} · TEA AGENT`」，没有工作区时退回 `TEA AGENT`——同时开着
几个页面时，靠标签页就能分清哪个是哪个项目。
对话页的输入框下方常驻**一行**指标：耗时、轮次、步数、速度（t/s）、前缀缓存命中率、
`max_context_tokens` 用量（后两者带进度条，超 70% / 90% 变黄变红）以及本轮输出与首字延迟。
这一行永远是单行：`flex-wrap:nowrap` 保证不会折成两行（折行会让输入区高度跳动），
窗口变窄时按「轮次/步数 → 缓存 → 耗时」的顺序收起，最宽的一项最多出省略号，完整内容
仍在 `title` 里。耗时在流式期间按本地时钟跳动，结束后取 `done` 的 `elapsed_ms`；
轮次是该会话已发送的用户消息数，步数是本轮 `tool_start` 次数。

**切换/新增工作区**（每个工作区就是一个项目目录，各自独立的会话、记忆、文件树和
`.tea_agent_run/`）：点左上角的**工作区名称**（形如 `tea_a2 ›`）打开下拉，列出所有已注册
工作区，最后一项是「＋ 添加工作区…」。点它会弹出一个表单，要求输入项目目录的**完整路径**
（绝对路径），注册后立刻切换过去。

这里没有「浏览…」按钮：浏览器给不出绝对路径（`showDirectoryPicker()` 返回的是句柄，
`<input webkitdirectory>` 只有相对名），只能由服务端弹系统对话框——那意味着每次点击多一个
进程、多一份临时脚本，而且 UI 从另一台机器打开时对话框出现在服务器上，反而更迷惑。所以就是
手输一个完整路径：`C:\Users\me\project`、`\\server\share\project` 或 `/home/me/project`。

路径不对时会**在表单里就地提示**，对话框不关，改一个字符就能重试：
- 空着或者写成相对路径（如 `project`）→ 直接拒绝，不发请求（服务端 `/api/workspaces` 也会以
  `path must be a full path` 拒绝：相对路径会被解析成服务端自己的 cwd，用户看不到那个目录）；
- 目录不存在 → 问一次「目录不存在，是否创建」，确认后 `mkdir -p` 再注册；
- 路径是个文件、没有权限之类 → 显示服务端返回的原因。

命令行侧对应 `--workspace <path>`（可重复）和 `--workspace-root <dir>`（注册其下所有一级子目录）。

侧边栏的**会话索引**按更新时间倒序列出当前工作区的会话（标题、相对时间、消息条数），
点击即切换，当前会话高亮，行内可**改名**、**删除**，右上角 ＋ 新建；一轮对话结束后自动刷新。
服务端正在跑的会话（不管是哪个浏览器发起的）会标出「· 进行中（你已离开）」，点一下接上直播；
进行中的会话不能改名/删除（worker 会把改动写回去）。

**主题有地址**：地址栏跟着当前会话走（`/?s=<session_id>`），顶栏右侧的「复制链接」按钮把它
放进剪贴板。贴到另一个浏览器/另一台机器打开就是同一个会话，正在跑的那一轮会直接接上；刷新
也停在这个主题上。两个窗口各看各的主题时互不干扰（详见「多主题并行」一节）。
会话很长时（比如已经聊了 30 轮）用右侧的**跳转索引**快速定位：每条用户消息对应一个编号，
悬停（或键盘聚焦）在索引条左侧展开一张提示卡片，内容是那条输入的前 **64 个字符**（超出加
省略号，换行折成空格，最多 4 行），点击直接滚到那一条；滚动时当前视口所在的轮次会高亮。
索引完全由会话消息推导，不额外请求服务端；不足 2 轮时隐藏，窗口窄于 900px 时也让位给正文。

**分叉**：在跳转索引里点一个编号，除了滚过去，还会把它选成**分叉点**（编号出现一圈描边，
输入框上方出现「分叉点」提示条，占位符改成「分叉模式」）。这时直接输入并发送，不会往当前
会话追加：服务端先以当前会话为父本，把截止到这一轮（**含**这一轮的回答与工具结果）的对话
复制成一个新会话，再把这句话作为新会话的下一轮，原会话一个字节都不动。再点一次同一个编号、
点提示条上的 ×、按 Esc，或切换会话/工作区，都会撤销分叉点；分叉成功后它自动消失，所以后续
消息留在新分支里。侧边栏会多出一行（标题形如 `分叉 3 · 原标题`），会话文件和 `index.json` 里
也记下 `forked_from` / `fork_turn`，重启后仍看得出它从哪一轮分出来。生成回答期间点编号只跳转
不选中——排队消息不携带分叉，免得让人以为它会进新会话。
助手输出里的 `![alt](路径)` 会渲染成图片（alt 作为说明文字），工作区相对路径经
`/api/workspaces/{id}/raw` 取原始字节；围栏代码块、表格、引用、有序/无序列表、标题、
行内代码、粗斜体与链接都会渲染，链接一律新标签页打开。文件视图里选中图片直接显示，
选中 `.md` 可在「预览 / 编辑」之间切换。

**下载运行数据**：文件页右上角的「下载 .tea_agent_run」把**这台设备上、这个工作区**的
`.tea_agent_run/` 整个打成一个 zip 传回来（会话、轨迹、日志、记忆、`agent.md`、`scripts/`），
不用登到机器上去 `tar`。几个刻意的取舍：

- 归档在内存里组装（`src/archive.cpp` + `3rd/miniz`），**不落临时文件**；单文件 32 MB、
  合计 256 MB、20000 个条目的上限之外，超限的文件是**跳过并在响应头里报数**
  （`X-Archive-Skipped`），而不是让整个下载失败；
- 条目名保留 `.tea_agent_run/` 这个前缀（含那个点），解压出来就是原样的目录；空目录也会
  存成条目，不会丢；
- 走 `fetch` + blob 而不是直接跳转，所以服务端报错时 UI 能说清原因，而不是把一个 JSON
  错误体当文件存下来；成功后 toast 里给出文件名、大小和条目数；
- `?path=` 可以换成工作区里的任何目录（默认就是 `.tea_agent_run`），越界路径由
  `resolve_path` 挡在打包之前（`403`）。

**粘贴图片**：在输入框里直接 Ctrl+V 粘贴截图（或把图片拖进输入框），缩略图会出现在
输入框上方，点 × 可移除，一条消息最多 4 张、单张 8 MB。发送时图片随消息提交，服务端按
**内容**而不是文件名嗅探真实类型（PNG / JPEG / GIF / WebP），落到工作区的
`.tea_agent_run/artifacts/`，会话里只记路径不存字节，因此会话文件依旧很小、可直接阅读。
历史消息里的图片由 `/api/workspaces/{id}/raw` 回放。

图片只有在模型声明 `supports_vision: true` 时才会发送：否则 `/api/chat` 直接返回
`400 vision_unsupported` 并说明原因，而不是让 provider 报一个难懂的错，或者更糟——静默
忽略图片让用户以为模型看过了。视觉输入在上下文预算里按每张 1024 token 估算（真实用量以
provider 回报的 `usage` 为准）。

```bash
# 常用参数
--host 0.0.0.0        # 监听所有网卡（暴露到网络前务必读「安全须知」）
--port 8282           # 端口
--workspace <path>    # 注册工作区，可重复
--workspace-root <d>  # 注册 <d> 下所有一级子目录
--provider/--model    # 临时换用别的 provider / 模型（默认是第一个）
--log-level debug     # 写进日志文件的级别：trace|debug|info|warn|error|off
--console-level warn  # 回声到控制台的级别（默认 warn，见下）
--no-console          # 控制台不回声任何日志（--quiet 同义，且连启动横幅也省掉）
--help                # 全部参数
```

**控制台默认只打印 WARNING / ERROR。** 服务是长期挂在终端里的，每次请求的 `INFO`
（`chat:`、`server:`、`toolkit_*` …）都往终端刷会把真正要紧的告警顶出屏幕；而排查问题时
又确实需要这些细节。所以两个阈值是分开的：

| 开关 | 作用 | 默认 |
| --- | --- | --- |
| `--log-level` | 写进 `logs/tea_agent.log` 与工作区 `logs/agent.log` 的级别 | `info` |
| `--console-level` | 回声到终端的级别 | `warn` |
| `--no-console` / `--quiet` | 终端彻底不回声（`--quiet` 连横幅也不打印） | 关 |

完整日志始终在文件里，`--console-level debug` 可以在终端跟着看一次复现过程。写成不认识
的级别名会打印一行 `warning: … is not a level; using warn` 并按 `warn` 处理，而不是
静默退回 `info` 把终端重新刷满。启动横幅、`error:` 这类面向人的提示不受级别影响。

---

## 跨平台编译

同一份源码，构建命令只取决于「宿主 → 目标」这一组组合。全部分支都收在
`CMakePresets.json` 与 `cmake/toolchains/` 里，下面这张表就是全部入口。

### 编译矩阵

| 宿主 → 目标 | 命令 | 产物 | 状态 |
| --- | --- | --- | --- |
| Linux x86_64 → 本机 | `./scripts/build.sh` 或 `cmake --preset posix && cmake --build --preset posix -j"$(nproc)"` | `build/bin/tea_agent` | ✅ 实测 |
| macOS → 本机 | 同上（`Xcode`/`Unix Makefiles`） | `build/bin/tea_agent` | ✅ 实测（10.6+） |
| Windows → 本机（MinGW-w64） | `scripts\build-mingw.bat` 或 `cmake --preset mingw` | `build-mingw\bin\tea_agent.exe` | ✅ 实测 |
| Windows → 本机（MSVC） | `scripts\build.bat` 或 `cmake --preset msvc` | `build-msvc\bin\tea_agent.exe` | ✅ 实测 |
| Linux / macOS → **Windows x64** | `cmake --preset win64-mingw-cross && cmake --build --preset win64-mingw-cross -j"$(nproc)"` | `build-win64/bin/tea_agent.exe`（PE32+，CA 已内嵌） | ✅ 产出；运行需 Windows |
| Linux x86_64 → **Linux aarch64** | `cmake --preset arm64-linux && cmake --build --preset arm64-linux -j"$(nproc)"` | `build-arm64/bin/tea_agent`（静态） | ✅ 真机运行 |

两类「交叉」用的都是 toolchain 文件（`cmake/toolchains/*.cmake`），它们的区别只是
`CMAKE_SYSTEM_NAME` 与编译器前缀，机制相同。

### 先装好编译器

```bash
# Debian / Ubuntu：本机
sudo apt install build-essential cmake
# Debian / Ubuntu：交叉到 Windows（会同时装上 -posix 线程模型的 g++，必需，见下文）
sudo apt install mingw-w64
```

```bash
# Linux x86_64 → aarch64：推荐 Arm GNU Toolchain 11.3，解压到 /opt 即可被自动发现
#   /opt/arm-gnu-toolchain-11.3.rel1-x86_64-aarch64-none-linux-gnu
# 或显式指定前缀目录（该目录下有 bin/aarch64-linux-gnu-gcc 或 aarch64-none-linux-gnu-gcc）
export TEA_AARCH64_PREFIX=/path/to/aarch64-toolchain
```

工具链查找顺序：`TEA_AARCH64_PREFIX` → `/opt/arm-gnu-toolchain-11.3…` →
`/opt/gcc-linaro-6.5.0-2018.12…` → `PATH` 上的 `aarch64-*-gcc`。Linaro 6.5 排在最后是
故意的（原因见下面的坑）。

```bash
# macOS
xcode-select --install
brew install cmake
brew install mingw-w64      # 只有要交叉出 Windows 版时才需要
```

Windows 本机：装 Visual Studio Build Tools（MSVC，配 `scripts\build.bat`）或 winlibs
MinGW-w64（`scripts\build-mingw.bat` 会依次找 `%MINGW_HOME%`、`.toolchain\mingw64`、`PATH`）。
两条路径都会设置 `VSLANG=1033`（MSVC）或 `TEMP`（MinGW），细节见「快速开始」。

### 本机编译

```bash
# Linux / macOS，手敲（等价于 scripts/build.sh）
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"
./build/bin/tea_agent --version

# 或者用 preset（configure 与 build 各一个）
cmake --preset posix
cmake --build --preset posix -j"$(nproc)"
```

```bat
:: Windows
scripts\build-mingw.bat            :: MinGW-w64
scripts\build.bat                  :: MSVC（NMake，带 VSLANG=1033）
```

### 交叉编译：必须走 toolchain 文件

**只把 `CMAKE_C(XX)_COMPILER` 换成目标编译器不算交叉编译**：没设 `CMAKE_SYSTEM_NAME` 时
`CMAKE_CROSSCOMPILING` 仍是假，`WIN32` 也不会打开，构建期工具路径不启用，最终报出的错误
和目标平台毫无关系（两个真实症状见下面的对照表）。所以要么用 preset，要么显式传
`-DCMAKE_TOOLCHAIN_FILE=...`：

```bash
# Linux x86_64 → Windows x64
cmake -S . -B build-win64 -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_TOOLCHAIN_FILE=cmake/toolchains/mingw-w64-x86_64.cmake
cmake --build build-win64 -j"$(nproc)"
```

```bash
# Linux x86_64 → Linux aarch64
cmake -S . -B build-arm64 -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_TOOLCHAIN_FILE=cmake/toolchains/aarch64-linux-gnu.cmake
cmake --build build-arm64 -j"$(nproc)"
```

CMakeLists.txt 在 configure 阶段就会拦下两种「看起来像交叉、其实不是」的配置，并直接给出
修法，而不是留到编译到一半才炸：

- 编译器目标是 mingw，但 `CMAKE_CROSSCOMPILING` 为假 → 让你改用 toolchain 文件 / preset；
- 编译器是 MinGW 的 win32 线程模型且 GCC < 13 → 告诉你这份 libstdc++ 根本没有 `std::mutex`。

#### 构建期工具必须用宿主编译器

`tools/embed_assets.cpp`（`web/` → C++ 源）与 `tools/embed_ca_bundle.cpp`
（`cacert.pem` → C++ 源）是在**构建机**上运行的：用目标编译器编出来的程序，宿主机根本跑不了。
交叉编译时它们由 `tools/host_embed/` 子项目用宿主编译器单独构建，
`TEA_AGENT_USE_HOST_ASSET_TOOL` 默认为 `CMAKE_CROSSCOMPILING`（本机构建也可以
`-DTEA_AGENT_USE_HOST_ASSET_TOOL=ON` 来验证这条路径）。相关的两个细节：

- 宿主工具的**可执行后缀**按宿主算（`TEA_HOST_EXE_SUFFIX`）：从 Linux 交叉到 Windows 时，
  宿主工具叫 `tea_embed_assets`，没有 `.exe`——用目标的 `CMAKE_EXECUTABLE_SUFFIX` 会找不到；
- 生成规则按**路径**而不是 target 引用它们（`COMMAND ${TEA_CA_BUNDLE_EXE}`），否则
  `COMMAND <target>` 会解析成目标架构的二进制。

### 校验产物

```bash
file build-win64/bin/tea_agent.exe   # PE32+ executable (console) x86-64, for MS Windows, 19 sections
file build-arm64/bin/tea_agent       # ELF 64-bit LSB executable, ARM aarch64, version 1 (GNU/Linux),
                                     #   statically linked, for GNU/Linux 3.7.0
readelf -d build-arm64/bin/tea_agent | head -1
                                     # There is no dynamic section in this file.   ← 默认全静态
grep -c "BEGIN CERTIFICATE" build-arm64/generated/ca_bundle.cpp
                                     # 121  ← CA 真的内嵌进构建产物了
```

`file` 说明架构对了，`readelf -d` 说明不依赖目标机的 glibc（用 `-DTEA_AGENT_STATIC_LIBC=OFF`
构建时会反过来出现 `Dynamic section` 与 `NEEDED … libc.so.6`），CA 计数说明证书进二进制了。
上面几行是本机实测输出。**别用 `ldd` 检查跨架构的产物**：宿主上的 `ldd` 跑不了 ARM 二进制，
它会一律回答 `not a dynamic executable`，看起来像通过、其实什么都没验证。

aarch64 产物在宿主上跑不了，装了 qemu-user 可以先冒烟一次，再拿到真机上验证：

```bash
qemu-aarch64-static build-arm64/bin/tea_agent --version     # tea_agent 0.9.1
```

### 常见错误的对照表

| 症状 | 原因 | 修法 |
| --- | --- | --- |
| 运行 embed 工具时报 `Could not open '/lib/ld-linux-aarch64.so.1'`（或 `cannot execute binary file`） | 没设 `CMAKE_SYSTEM_NAME`，宿主工具用目标编译器编了 | 用 toolchain 文件 / preset 重配 |
| `src/config.h:178: 'mutex' in namespace 'std' does not name a type`（明明 `#include <mutex>` 了） | ① 同上，配置其实不是交叉构建；② 选了 MinGW 的 **win32** 线程模型，GCC < 13 的 libstdc++ 在该模型下不提供 `std::mutex`/`std::thread` | 用 toolchain 文件 / preset（它们优先选 `-posix`）；手敲时用 `x86_64-w64-mingw32-g++-posix` |
| 找不到 `.../tea_embed_assets.exe` | 用目标侧后缀找宿主工具 | 已由 `TEA_HOST_EXE_SUFFIX` 处理，别自己改回 `CMAKE_EXECUTABLE_SUFFIX` |
| aarch64 启动约 1 秒后 `terminate called without an active exception` | Linaro 6.5 的静态 glibc 2.23 在 4.14 内核上 `pthread_cond_wait()` 立即失败 | 换成 Arm GNU Toolchain 11.3（默认），或 `-DTEA_AGENT_STATIC_LIBC=OFF` |
| 启动即 abort，日志里 `Invalid range in bracket expression` | GCC ≤ 6 的 `std::regex` 不接受方括号里转义的 `\-` | 已修（`-` 放最后、不转义）：`[A-Za-z0-9_.-]` |
| Windows 重新链接时 `LNK1104` / `Permission denied` | 旧的 `.exe` 还在运行 | 见上文「程序正在运行时也能直接构建」 |

### 可调开关

| 选项 | 默认 | 作用 |
| --- | --- | --- |
| `TEA_AGENT_CXX_STANDARD` | `17` | 设成 `14` 验证「C++14 也能编」（GCC 6.5 是参照的老工具链） |
| `TEA_AGENT_USE_MBEDTLS` | `ON` | 关掉可显著减小体积，但 Linux 上就没有 `https://` |
| `TEA_AGENT_STATIC_RUNTIME` | `OFF`（aarch64 toolchain 置 `ON`） | 静态链接 `libstdc++`/`libgcc` |
| `TEA_AGENT_STATIC_LIBC` | `OFF`（aarch64 toolchain 置 `ON`） | 再静态链接 libc（`-static -no-pie`） |
| `TEA_AGENT_USE_HOST_ASSET_TOOL` | `= CMAKE_CROSSCOMPILING` | 强制用宿主编译器构建 embed 工具 |
| `CMAKE_BUILD_TYPE` | `Release` | 想更小用 `MinSizeRel`，再 `strip` |

---

## 给上层 Agent 用的接口

所有响应是 JSON，错误统一为 `{"error":{"code":"…","message":"…"}}`。核心就两类：同步查询 + 一条 SSE 流。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/health` | 探活、版本（`0.9.1`）、当前 provider/model、传输引擎、`tea_home`、`config_loaded` |
| `GET` | `/api/workspaces` | 工作区索引（`index_path` + `workspaces[]`） |
| `POST` | `/api/workspaces` | 注册工作区 `{"path":"…","name":"…"}` |
| `GET` | `/api/workspaces/{id}/tree?path=` | 列目录 |
| `GET` / `PUT` | `/api/workspaces/{id}/file` | 读 / 写文件 |
| `GET` | `/api/workspaces/{id}/raw?path=` | 读文件原始字节（图片 / PDF 等，**16 MB 上限**，用于在页面里显示） |
| `GET` | `/api/workspaces/{id}/download?path=` | 以附件形式下载文件（`Content-Disposition: attachment`），**从磁盘流式发送、无 16 MB 限制**；`&inline=1` 改为在浏览器里直接打开（视频 / PDF） |
| `GET` / `POST` | `/api/workspaces/{id}/sessions` | 会话列表 / 新建 |
| `GET` | `/api/sessions/{sid}` | 取完整会话（含 reasoning、工具调用与结果） |
| `GET` / `PUT` | `/api/sessions/{sid}/todos` | 任务列表：GET 随 `/api/sessions/{sid}` 一起返回；PUT 用 `{"items":[…]}` 整体替换，或 `{"index":n,"status":"…"}` 改一条 |
| `PATCH` / `DELETE` | `/api/sessions/{sid}` | 重命名（`{"title":"…"}`）/ 删除会话 |
| `POST` | `/api/sessions/{sid}/fork` | 从某一轮分叉出新会话：`{"message_index":N}` 是父会话里某条**用户消息**的下标，复制到该轮结束（含回答与工具结果）；省略或负数 = 复制整个会话。返回 `session`（带 `forked_from` / `fork_turn`）与复制后的 `messages`；父会话不变 |
| `GET` | `/api/sessions/{sid}/export` | 导出成单个自包含 HTML（CSS/图片内联，离线可读，末尾带可导入的会话 JSON） |
| `POST` | `/api/workspaces/{id}/sessions/import` | 导入导出的 `.html`（原样或 `{"html":"…"}`）或会话 JSON，新建一个会话；重复导入不会覆盖 |
| `GET` | `/api/sessions/{sid}/trajectory` | 每轮轨迹摘要，最新在前；`?full=1` 带步骤与文件变更，`?format=jsonl` 原始 JSONL |
| `GET` | `/api/turns/{turn_id}/trajectory` | 单轮完整轨迹（`turn_id` 即 `done` 事件的 `trajectory_id`） |
| `GET` / `POST` | `/api/workspaces/{id}/memory` | 工作区长期记忆 |
| `POST` | `/api/tasks` | 排一个任务：`{workspace_id, prompt, title?, session_id?, provider?}` → 任务记录（status=queued） |
| `GET` | `/api/tasks?workspace_id=&status=` | 任务列表：排队中 / 进行中 / 等待确认 / 已完成，含进度、中间状态与 `counts` 汇总 |
| `GET` | `/api/tasks/{id}?workspace_id=` | 单个任务 |
| `POST` | `/api/tasks/{id}/start?workspace_id=` | 现在就做这件事（body 可带 `provider`/`model` 覆盖） |
| `POST` | `/api/tasks/{id}/cancel?workspace_id=` | 停掉正在跑的那个 |
| `POST` | `/api/tasks/{id}/confirm?workspace_id=` | 回答一个「等你确认」的任务：`{message}`，答案会成为同一会话的下一轮 |
| `GET` | `/api/workspaces/{id}/logs?tail=300` | 运行日志尾部 |
| `POST` | `/api/config` | 上传完整 `provider.yaml`（原始 YAML 或 `{"yaml":"…"}`）；先校验再替换，校验失败不会覆盖现有文件。设置页的「重置 provider」走的就是它 |
| `POST` | `/api/config/quickstart` | 首次运行快捷通道：`{"provider":"deepseek\|minimax","api_key":"sk-…"}` → 生成并应用只含该 provider 的配置（DeepSeek 默认模型 `deepseek-flash`，MiniMax 默认模型 `MiniMax-M3.1-Flash`）；key 不会出现在任何响应里 |
| `GET` / `PUT` | `/api/workspaces/{id}/system-prompt` | 读 / 写工作区 system prompt：`current_prompt` 是真正生效的人设，`source` 是 `workspace`/`settings.yaml`/`builtin`，`content` 是磁盘上的覆盖（空 = 无覆盖） |
| `GET` | `/api/destroy` | 预览「销毁」会删除的路径（tea home + 每个工作区的 `.tea_agent_run`），不动任何文件 |
| `POST` | `/api/destroy` | **不可逆**：删除 tea home 与所有已索引工作区的 `.tea_agent_run`；必须带 `{"confirm":"destroy"}`，否则 400；有轮次在跑时 409 |
| `GET` | `/api/workspaces/{id}/archive?path=` | 把目录打成 zip 下载（默认 `.tea_agent_run`）；`X-Archive-Entries` / `X-Archive-Skipped` / `X-Archive-Uncompressed` 报打包结果 |
| `GET` | `/api/tools` | 工具定义与 JSON Schema |
| `GET` | `/api/providers` | provider 清单与能力标记（`supports_search` / `supports_fetch` / `supports_usage`），**不含 key** |
| `GET` | `/api/providers/{name}/usage` | 查该 provider 的订阅额度（provider 声明了 `supports_usage` 才有）；用 provider 自己的 key 去查，key 不下发。上游失败返回 502 与原因 |
| **`POST`** | **`/api/chat`** | **SSE 流式对话（主入口）**。该会话已有轮次在跑时**默认排队**：返回 `200` + `{"queued":{…},"turn_running":true}`（JSON，不是 SSE），消息在该轮的下一次迭代被采纳；要替换正在跑的那一轮，带 `{"interrupt": true}` |
| `POST` | `/api/chat/stop` | 中断当前轮次（对所有人） |
| `POST` | `/api/chat/queue` | 轮次进行中把消息插队（下一轮采纳） |
| `POST` | `/api/chat/queue/cancel` | 取消一条（带 `id`）或全部排队消息 |
| `GET` | `/api/chat/queue?session_id=` | 查看当前排队消息 |
| `GET` | `/api/chat/turn?session_id=` | 这个会话是否还有轮次在跑（`running` / `last_seq` / `oldest_seq` / `dropped` / `subscribers`） |
| `GET` | `/api/chat/turns` | 所有还在跑的轮次（含 `workspace_id` 与 `subscribers`），最近开始的在前 |
| `POST` | `/api/chat/attach` | 接回正在跑的轮次：回放 `since` 之后的事件，然后继续直播。跟随者超过上限时 `429 too_many_subscribers` |

#### token 用量与「缓存命中率」：为什么有的提供方一开始拿不到

指标栏的「缓存命中率」来自 provider 自己报的数字，不是本地估算。但**只有流式响应里带了
`usage` 的请求才有这些数字**，而 OpenAI 兼容网关大多要你显式开口：

```jsonc
// 请求体里多这一段
"stream_options": { "include_usage": true }
```

MiniMax 的文档写得很直白（`platform.minimax.cn/docs/api-reference/text-chat-openai`）：
`stream_options.include_usage` 默认 **false**——「是否在流式响应中包含 token 用量」。不传这一句，
流里的 `usage` 就是 `null`，于是 tea_a2 只能本地估算，指标栏显示「provider 未返回缓存字段」。
传了之后，用量长这样（同一份文档的响应示例）：

```json
"usage": {
  "total_tokens": 473,
  "total_characters": 0,
  "prompt_tokens": 170,
  "completion_tokens": 303,
  "prompt_tokens_details": { "cached_tokens": 157 }   // ← 前缀缓存命中
}
```

`prompt_tokens_details.cached_tokens` 正是 OpenAI 的写法，tea_a2 认；DeepSeek 那种
`prompt_cache_hit_tokens` 也认（见 `src/llm.cpp` 的 `parse_usage_into`）。

**现在的行为**：

- 默认**会**带 `stream_options.include_usage`（`settings.stream_include_usage`，默认 `true`）——
  否则大多数网关的用量与缓存命中率都是拿不到的；
- 某个网关不认这个字段、会回 400 时，tea_a2 记下“这个提供方不支持”，去掉字段重试一次，
  这一轮照常完成，之后也不再发它（只有这一次多一个来回）；
- 想按 provider 单独控制，在 provider 块里写 `stream_include_usage: true|false`（不写=跟随全局）；
- 不用流式（`settings.stream: false`）时不会有这个字段，用量在响应体里，MiniMax 也会给。

### 一次完整的「远程操控」长这样

```bash
# 1) 找到工作区
curl -s http://device:8282/api/workspaces | jq -r '.workspaces[0].id'

# 2) 下发任务，边跑边收
curl -N -X POST http://device:8282/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"workspace_id":"ws_xxx","message":"检查 /etc/tea 下的配置文件，把端口改成 9000 并重启服务"}'
```

SSE 每行 `data: {json}`，按 `type` 分派：

| `type` | 含义 |
| --- | --- |
| `start` | 本轮开始（session / provider / model / `messages_before`） |
| `reasoning_delta` | 思考流 |
| `content_delta` | 正文增量 |
| `tool_args_delta` | 工具调用参数的流式增量（只用于进度/速度统计，不当正文渲染） |
| `tool_call` | 模型请求的工具（`id` / `name` / `arguments`） |
| `tool_start` | 即将执行 |
| `tool_result` | `ok` / `elapsed_ms` / `output` / `truncated` |
| `iteration_end` | 单轮结束，带 `usage` 与 `metrics` |
| `queue_adopted` | 采纳了一条排队消息（`id` / `content` / `pending`） |
| `queue_unconsumed` | 轮次结束时仍未采纳的排队消息（`items[]`），客户端应放回输入框 |
| `done` | 全部结束，带整轮的 `usage` / `metrics` / `content` / `trajectory_id` |
| `stopped` | 被 `/api/chat/stop` 中断 |
| `error` | `message` / `detail` |
| `ping` | 空闲心跳（15s），用于判断链路存活 |
| `resync` | 回放缓冲溢出：重新拉一次 `/api/sessions/{sid}`，再从 `oldest_seq` 接着收 |

每帧都带一个从 1 递增的 `seq`（`ping` 除外），断线重连靠它对齐。

上层 agent 只需要：发一条 `POST /api/chat`，读 SSE，看 `tool_result` 有没成功，`done` 结束。
需要打断就调 `/api/chat/stop`——它会在下一个模型/工具边界停住，已产生的会话照样落盘。

### 断线重连：关掉页面不会取消这一轮

轮次的生命周期和那条 HTTP 连接是**分开**的。浏览器关掉、笔记本合盖、换台机器打开 UI，服务端
那一轮照跑到底——断开只是少了一个订阅者。取消只有三条途径：`POST /api/chat/stop`、
`POST /api/chat {"interrupt": true}`（显式替换，且会等旧 worker 收尾）、服务端进程退出。

为此每帧都带一个递增的 `seq`，服务端把这一轮发出的帧原样留在一个有上限的回放缓冲里（8 MB，
超了丢最旧的，并让客户端 `resync`）。重新打开页面时 UI 做三件事：

1. `GET /api/chat/turn?session_id=…` 问一句「这个会话还有轮次在跑吗」；
2. 有就 `POST /api/chat/attach` 接回去：先回放这一轮从 `seq 1` 开始的全部事件，再继续直播，
   直到 `done` / `stopped`；
3. `start` 里的 `messages_before` 说明落盘历史到哪儿为止——回放会把这一轮已经存盘的前几次
   迭代重新画进 live 气泡，所以客户端先把超出这个下标的消息截掉，屏幕上不会出现两份。

```bash
# 还在跑吗？跑到第几帧了？
curl -s 'http://device:8282/api/chat/turn?session_id=s_xxx'
# {"ok":true,"running":true,"session_id":"s_xxx","last_seq":137,"oldest_seq":1,
#  "buffered":137,"dropped":0,"pending":0,"started_at":"...","workspace_id":"ws_xxx"}

# 接回去：从 seq 42 之后继续（0 或省略 = 从头回放）
curl -N -X POST http://device:8282/api/chat/attach \
  -H 'Content-Type: application/json' -d '{"session_id":"s_xxx","since":42}'
```

轮次刚结束、还没被回收时 attach 依然可用（把尾巴放完就 `done`）；已经回收则返回
`404 no_active_turn`，这时 `/api/sessions/{sid}` 就是最终状态。排队消息也一起回来：
`GET /api/chat/queue?session_id=` 列出的条目仍然挂在服务端那一轮的队列里，等它采纳。

**换一台机器接着看**也是同一条路：轮次属于服务端、按 session 归属，跟发起它的那个浏览器没有
关系。把主题链接（`/?s=<session_id>`，见下一节）贴到另一台机器上打开即可；不带链接的冷启动
页面**不会**被"谁在跑就跳到谁"拽走，而是在侧栏把正在跑的会话标成「· 进行中」，点一下才接上。

```bash
curl -s http://device:8282/api/chat/turns
# {"ok":true,"turns":[{"running":true,"session_id":"s_xxx","workspace_id":"ws_xxx",
#   "started_at":"...","last_seq":137,"oldest_seq":1,"buffered":137,"dropped":0,
#   "pending":0,"subscribers":2}]}
```

`subscribers` 是此刻有多少个浏览器正跟着这一轮（含发起它的那个）。它同时是一个上限：
每个跟随者都占着一个 HTTP 线程，所以超过 `TEA_AGENT_MAX_SUBSCRIBERS`（默认 8）的 attach
会被 `429 too_many_subscribers` 拒绝，而不是把整个服务拖死。

**多个客户端同时看一轮**是允许的、而且是鼓励的用法：每个连接有自己的回放游标，
所以在台式机上发起一个长任务、拿着笔记本走过去接着看，两边看到的是同一条流；任意一边都能
插队（`queue_adopted` 会同时出现在两边）。

跟着看和打断它是两件事，所以输入框右侧有两个按钮：

| 按钮 | 作用 |
| --- | --- |
| **离开** | 只断开自己这条流：服务端照跑，别人也不受影响。侧栏里那个会话标出「· 进行中」，点一下重新接上 |
| **停止** | 取消这一轮（对所有人）：在下一个模型/工具边界停住，已产生的内容照样落盘 |

这个页面从不被一轮"锁住"：跟随中切工作区、开新会话、点别的会话都会自动「离开」（服务端照跑），
不需要先按停止。

注意：默认只监听 `127.0.0.1`，另一台机器要连得上，得用 `--host 0.0.0.0`（或反向代理）启动。
`--host 0.0.0.0` 意味着同一网段的任何人都能发消息、改配置、甚至销毁状态，所以只在内网使用，
或者挡一层带鉴权的反向代理。

### 多主题并行：一个 server，几个浏览器

一台设备、一个 `tea_a2`、两三个浏览器，各自看自己的主题——这是预期用法，不是"多个客户端抢一个
会话"。**主题就是一个会话**；同时看一个主题的多个窗口则是同一轮次的多个订阅者，看到的内容完全
一样。

**主题有地址**：`/?s=<session_id>`。地址栏跟着当前会话走（打开 / 新建 / 分叉都会更新），顶栏
右侧的「复制链接」把这条地址放进剪贴板，贴到另一个浏览器或另一台机器上打开就是同一个会话，
带着正在跑的那一轮直接接上。刷新页面也仍然停在这个主题上，不会跳到"最近更新的那个会话"。

启动时的落点顺序（`web/app.js`）：

1. 地址栏的 `?s=`（最明确，跨机器/贴链接都靠它）；
2. 本标签页上次看的会话（`sessionStorage`，两个窗口各记各的）；
3. 上次的工作区（`localStorage`），再不行就是索引里的第一个工作区。

**刻意不再做的事**：以前冷启动会问 `GET /api/chat/turns`，谁在跑就跳到谁的工作区和会话。多主题
并行时这条规则很烦人——另开一个窗口想看点别的，一打开就被拽到别人正在跑的任务上。现在正在跑
的会话只在侧栏标「· 进行中（你已离开）」，点一下才接上；工作区、会话、URL 都不会被它改掉。

**跟随中也能走去别处**：切工作区、开新会话、点别的会话都会自动「离开」（断开本页那条 SSE，
服务端照跑），不需要先按停止，也不会丢掉别人的任务。回到那个会话（点它的行，或用它的
`?s=` 链接）就重新接上，回放从 `seq 1` 开始，屏幕上的内容和一直开着那边一样。

**上限与保护**（都是环境变量，默认值适合两三台机器）：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `TEA_AGENT_HTTP_THREADS_MAX` | 128 | HTTP 线程池上限（基础 16 个，按需增长、空闲回收）。每个跟随中的 SSE 都占一个线程，所以这个值同时决定了"同时能跟多少个窗口" |
| `TEA_AGENT_MAX_SUBSCRIBERS` | 8 | 单个轮次的跟随者上限，超出 `429 too_many_subscribers` |
| `TEA_AGENT_MAX_ACTIVE_TURNS` | 24 | 同时在跑的轮次上限，超出 `429 too_many_turns` |

改造前线程池固定 32 个、且不回收：实测打开第 32 条 SSE 之后 `/api/health` 直接超时，整个页面
（包括"停止"按钮）都失灵。现在池子上限可调，跟随者也有上限，被拒绝的只是一个多余的标签页。

**两条写路径的规矩**：

- 同一个会话：新消息默认排队（见下一节），要顶掉正在跑的那一轮必须显式 `interrupt`，而且
  服务端会等旧 worker 收尾落盘后才开新一轮——否则旧的那份 `Session` 会把新消息覆盖掉。
- 任务列表、改名、删除会话：轮次进行中一律 `409 turn_running`，因为 worker 会在下一次迭代把
  改动写回去。等它结束再来。

回归测试：`python3 tests/smoke_multi_session.py`（23 项：三个会话并行、晚到跟随者回放一致、
默认排队与 `interrupt`、写操作 409、跟随者上限、三个主题各带一个跟随者）、
`node tests/ui_two_clients.mjs`、`node tests/ui_second_client.mjs`（冷启动不被拽走）、
`node tests/ui_topic_url.mjs`（同一浏览器的两个标签页各看各的主题，刷新仍在原主题）。

### 插队：轮次进行中追加消息（同会话默认排队）

一轮可能跑很久（长工具、多轮迭代、思考模型）。这段时间里**再发一条 `POST /api/chat` 不会
顶掉它**：消息进入**当前轮次自己的**队列，agent loop 在下一轮迭代开始前采纳最早的一条，
把它作为普通 user 消息追加进会话，然后带着新的上下文继续请求模型。一轮只采纳一条，连发几条
不会挤进同一个请求。

这是为"一个主题、多个窗口"服务的默认行为——另一个浏览器不能因为手快就把别人正在看的任务
扔掉。响应因此有两种形状：

```bash
# 轮次进行中：200 + JSON，消息排队，没有新开轮次
curl -s -X POST http://device:8282/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"workspace_id":"ws_xxx","session_id":"s_xxx","message":"顺便把日志级别改成 debug"}'
# {"ok":true,"session_id":"s_xxx","workspace_id":"ws_xxx","turn_running":true,
#  "queued":{"id":"q_...","content":"..."},"pending":1}

# 明确要求顶掉正在跑的那一轮（对所有人）：服务端先取消、等旧 worker 收尾并落盘，
# 再重新读取会话、开始新一轮。15 秒内停不下来（比如卡在长工具里）返回 409 turn_still_running。
curl -s -N -X POST http://device:8282/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"workspace_id":"ws_xxx","session_id":"s_xxx","message":"换个方向","interrupt":true}'

# 只想排队，不需要 SSE 的客户端可以直接用这个接口
curl -s -X POST http://device:8282/api/chat/queue \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s_xxx","message":"顺便把日志级别改成 debug"}'
# {"ok":true,"queued":{"id":"q_...","content":"..."},"pending":1}

# 采纳前可以取消：带 id 取消一条，不带 id 取消全部
curl -s -X POST http://device:8282/api/chat/queue/cancel \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s_xxx","id":"q_..."}'
```

为什么 `interrupt` 要等旧 worker：那个 worker 手里攥着自己那份 `Session`，结束时会整份写回。
在它停之前启动新一轮，新一轮写下的问题与回答会被旧的那份覆盖掉（修复前可稳定复现：新一轮
的 SSE 明明 `done` 了，磁盘上却没有它的消息）。等待的代价只有一次轮次的收尾时间。

SSE 上的对应事件：

| `type` | 含义 |
| --- | --- |
| `queue_adopted` | 已采纳：`id` / `content` / `iteration` / `pending`。该消息已落盘为 user 消息 |
| `queue_unconsumed` | 轮次在采纳前结束（停止 / 报错 / 迭代上限），`items[]` 原样退回，客户端应放回输入框 |
| `artifacts` | 本轮产出了新文件：`items[]`（`path` / `bytes` / `kind` / `tool` / `turn`）。UI 用它把下载链接补到回答下面 |
| `todo` | 任务列表变了：`items[]`（`{id,text,status}`）、`total`、`done`。UI 用它实时更新进度条，不需要等轮次结束 |
| `tool_images` | `toolkit_load_image` 把图片附加进了对话：`content` 是那条 user 消息的文本，`images[]` 是 `{path, mime}`。客户端应把它插进当前对话（图片走 `/raw`），与本轮结束时回读会话的结果一致 |

几条刻意的约束：

- **队列随轮次存活**。轮次结束队列即销毁，不会留到下一次、无关的对话里突然发出去。
- **模型已经给完最终答案时也会采纳**：loop 发现队列非空就继续迭代，而不是把消息留成下一轮
  HTTP 请求；只有停止 / 报错 / 迭代上限才会退回。
- **只收文本**。图片必须在请求时解码、校验并落到工作区，`/api/chat` 已经为「开始一轮」做了
  这些；排队路径（包括 `/api/chat` 的默认排队）带 `images` 会明确返回 `400/409
  images_not_queued`，而不是静默丢掉。
- 没有进行中的轮次时 `/api/chat/queue` 返回 `409 no_active_turn`：这时直接 `POST /api/chat` 就行。
- **任务列表 / 改名 / 删除在轮次进行中会被拒绝（`409 turn_running`）**：这些写操作也是
  "读文件 → 改一处 → 整份写回"，正在跑的 worker 会在下一次迭代把改动覆盖回去。等它结束再来。

内置 Web UI 把这条路径做成了输入框行为：生成中按 **Enter** 进入排队（输入框上方显示排队条，
每条都能点 × 取消），`Shift+Enter` 仍是换行，右侧是「停止」，旁边还有一个「离开」（只停止
跟随、不中断轮次）。轮次结束时仍未采纳的消息会自动放回输入框，不会丢字。

### `metrics`：指标栏的数据来源

`iteration_end` 与 `done` 都带一个 `metrics` 块，内置 UI 用它渲染输入框下方的
「输出速度 / 缓存命中率 / 上下文用量」：

```json
{
  "max_context_tokens": 128000,  // 模型窗口；模型没配 max_context_tokens 时为 0
  "context_tokens": 8642,        // 本轮单次请求最大的 prompt tokens（不是各轮累加）
  "output_tokens": 512,          // 本轮生成的 tokens（多轮工具调用累加）
  "ttft_ms": 640,                // 首字延迟
  "generation_ms": 2140,         // 生成窗口（首字→最后一个增量），t/s 的分母
  "elapsed_ms": 3120,            // 整轮耗时，含工具执行
  "estimated": false             // true：provider 没回 usage，这些数字是本地估算
}
```

输出速度 = `output_tokens / generation_ms`。用生成窗口而不是整轮耗时，是因为后者包含
网络排队与工具执行时间，会把 t/s 压得没有意义；窗口短于 250ms（单块响应、非流式）时
退回整轮耗时。多轮工具调用时两边都是**各轮之和**，所以 `done` 里的 t/s 是「纯生成时间」
的吞吐，而 `elapsed_ms` 会大得多（工具执行不在里面）。

UI 在生成过程中显示的是 `≈` 前缀的**实时估算**：把本轮收到的 `content_delta` /
`reasoning_delta` / `tool_args_delta` 按字符折算成 token，除以「本轮首字→现在」的窗口
（窗口在每次 `iteration_end` 重置，因此工具执行时间同样不计入）。服务端数字到达后立刻
换成真实值——两边的口径一致，实时值不会长时间偏离最终值。

provider 只在 `stream_options.include_usage` 打开时才回传 usage。没回传时后端用同一个
token 估算器补上 `context_tokens` / `output_tokens` 并置 `estimated: true`，UI 用「≈」
标出来；缓存命中率无法估算，于是显示 `—` 而不是编一个数字。每轮日志里也有一行：

```
turn finished ok=true iterations=4 elapsed_ms=3120 output_tokens=512 ttft_ms=640 gen_ms=2140 context=128/128000
```

### 流式渲染的预算：别让 UI 成为瓶颈

SSE 的 delta 比屏幕刷新还密（一个 30 KB 的回答能有几百条）。如果每条 delta 都重建气泡，
工作量就是「段落数 × delta 数」，主线程被占满，Firefox 会直接弹出「此网页拖慢了您的
firefox，可停止此页面」。内置 UI 现在按三条规则做：

1. **一帧一次**：delta 只置脏，真正的渲染在 `requestAnimationFrame` 里做一次，连续 10 条
   delta 合并成一次 DOM 操作。rAF 会被浏览器挂起（后台/被遮挡的标签页，headless 下导航回来
   也可能不再产生帧），所以旁边挂一个 200ms 看门狗：帧来了走帧、看门狗自动取消，帧不来也
   保证每 200ms 至少画一次——事件在收、`state.live` 在长、画面却一动不动是最坏的组合。
2. **只改变化的那部分**：气泡的「结构」（段落数、工具行状态、思考行有无、是否已结束）
   变了才整块重建；纯文字增长只重画最后一个段落 + 思考面板，之前的段落和工具行保持
   原来的 DOM 节点，不会被重新解析、重新创建。
3. **指标栏只写变了的项**：每 500ms 的指标刷新逐项比较，`textContent`/`style.width` 没变
   就不写；贴底滚动也只在真的偏移时才写 `scrollTop`。

同一段 320 条 delta 的流（`tests/ui_render_perf.mjs`）：整块重建从 **324 次降到 4 次**，
样式与布局时间从 ~420ms 降到 ~170ms。

### 轨迹：每一轮「实际做了什么」的审计记录

会话记录（`/api/sessions/{sid}`）回答「说了什么」，轨迹回答「做了什么」：每一次模型
迭代、每一次工具调用的原始参数与真实输出、上下文压缩、以及工具**自己回报**的文件改动。
被上层 agent 远程操控时，这是唯一不用反推聊天记录的证据链。

- 位置：`<workspace>/.tea_agent_run/trajectories/<session_id>.jsonl`，**一轮一行**。
- 一行 = 一个 turn 对象：`turn_id` / `session_id` / `workspace_id` / `provider` / `model` /
  `started_at` / `finished_at` / `ok` / `iterations` / `usage{prompt,completion,total,cached}`
  / `steps[]` / `files_changed[]`。
- 步骤 `kind`：
  - `model`：`reasoning_chars` / `content_chars` / 请求的 `tool_calls` 名字 /
    `tokens{prompt,completion,cached}` / `ttft_ms`（provider 没回 usage 时标 `usage_estimated`）；
  - `tool`：`name` / `arguments`（模型发出的原始 JSON 字符串）/ `ok` / `exit_code` /
    `truncated` / `output_filter` + `dropped_lines` / `output`（按上限截断并留标记）；
  - `compaction`：`covered_messages` / `summary_bytes`。
- `files_changed` 只写工具真实回报的结果：`toolkit_file` 的 write/append/delete/move 带
  `path`（写入还带 `bytes` 与 `size`），`toolkit_exec` 只带 `command`——不猜路径。
- 增量落盘：turn 一开始就写头行，之后每一步**原地刷新同一行**（同偏移重写 + 截断），
  因此进程被 kill 或断电时文件里仍是最后一份完整快照。读入时按 `turn_id` 取最后一条，
  尾部被写坏的半行会被跳过；`?full=1` 里未完成的轮次带 `"partial": true`。
- 文件里不含任何 api_key，API 响应也不含。

```bash
# SSE 的 done 事件直接给出 id，不需要轮询
curl -N -X POST http://device:8282/api/chat -d '{"workspace_id":"ws_x","message":"..."}'
# data: {"type":"done",...,"trajectory_id":"t_1a2b3c4d5e6f"}

curl -s http://device:8282/api/sessions/s_9f8e7d/trajectory            # 摘要，最新在前
curl -s 'http://device:8282/api/sessions/s_9f8e7d/trajectory?full=1'   # 带 steps / files_changed
curl -s http://device:8282/api/turns/t_1a2b3c4d5e6f/trajectory         # 单轮完整轨迹
curl -s 'http://device:8282/api/sessions/s_9f8e7d/trajectory?format=jsonl'  # 原始 JSONL
```

默认视图（摘要）：

```json
[{"turn_id":"t_1a2b3c4d5e6f","started_at":"2026-01-01T00:00:00.000Z","elapsed_ms":4213,
  "ok":true,"iterations":4,"steps":7,
  "usage":{"prompt_tokens":1843,"completion_tokens":512,"total_tokens":2355,"cached_tokens":1024},
  "files_changed":1,"partial":false}]
```

内置 UI 在每条助手回复上方显示一行折叠的「轨迹」（例如 `4 步 · 1.2 s · 284 tok`），
展开后是步骤时间线（模型 / 工具 / 压缩）、工具参数与实际输出、以及本轮改动过的文件与
字节数。

留存与上限，按工作区覆盖（`<workspace>/.tea_agent_run/settings.yaml`）：

```yaml
settings:
  enable_trajectory: true           # false => 完全不写轨迹
  trajectory_step_max_bytes: 8192   # 单条工具输出保留多少字节（超出留截断标记）
  trajectory_max_turns: 200         # 每个会话文件最多留几轮
  trajectory_max_bytes: 8388608     # 每个会话文件最多多少字节（默认 8 MB）
```

超出上限时**从最旧的轮次删起**，并在工作区日志里写清删了什么：

```
trajectory: pruned 1 turn(s) from s_9f8e7d.jsonl (kept 1 turn(s), 629 bytes; limits 1 turns / 8388608 bytes)
```


---

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

导出的 HTML 同样带这块进度（见下文「导出会话」），导入回来连进度一起恢复。

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

单条用户消息内循环「模型 → 工具 → 模型」，直到不再请求工具或达到 `max_iterations`。
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
预算有 `窗口 / 5` 的下限。`keep_turns` 是「至少保留最近 N 轮」的**硬下限**，单条消息本身超预算时
按头尾裁剪而不是直接溢出。日志里每次都能看到实际数字：

```
context: max=1000000 reserve_output=333333 fixed=847 budget=665820 kept=4/4 used=960 summarised=23
```

### 3. 超预算时做滚动摘要，而不是静默丢弃

历史超过预算时，把最旧的一段交给模型压缩成摘要，写入会话的 `summary`，并记录
`summary_upto`（已被摘要覆盖的消息条数）。**原始消息不会删除**——UI 和审计仍能看到全文，
只是不再发给模型。摘要会写进 system prompt 的「Compact history」小节，并在 SSE 里发一个
`compaction` 事件。摘要失败（网络/模型报错）时自动退回丢弃最旧消息，不会卡死会话。

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
├── trajectories/<sid>.jsonl  # 每轮轨迹，一轮一行（见上文「轨迹」）
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
| `workspaces.json` | 工作区索引：id、名称、路径、创建/最近使用时间、会话数 |
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

## 配置

`provider.yaml` 就是 OpenAI 兼容端点清单（第一个 provider 的第一个模型是默认，见下）：

```yaml
providers:
  DeepSeek:
    api_url: https://api.deepseek.com      # 无路径 → 追加 /v1/chat/completions
                                           # 以 /v1 结尾 → 追加 /chat/completions
    api_key: sk-xxx
    headers:                               # 可选：网关自定义头
      x-opencode-session: '${session_id}'  # ${session_id} 会替换为当前会话 id
    reasoning_content: auto                # keep | strip | auto（默认 auto，见下）
    supports_search: true                  # 网页搜索工具 toolkit_search（见下「网页搜索与抓取」）
    supports_fetch: true                   # 网页抓取工具 toolkit_fetch
    # search_path: /v1/tools/search        # 可选：端点覆盖（相对 api_url 的路径或完整 URL）
    # fetch_path: /v1/tools/fetch
    # stream_include_usage: true           # 可选：是否在流式响应里索取 token 用量
                                           # （不写=跟随全局 settings.stream_include_usage）
    # supports_usage: true                 # 可选：provider 有「额度查询」接口（见下）
    # usage_type: minimax_token_plan       # 解析器名（目前内置这一个）
    # usage_url: https://www.minimax.cn/v1/token_plan/remains   # 可选端点覆盖
    models:
      deepseek-chat:
        max_context_tokens: 128000
        max_output_tokens: 8192
        supports_tools: true
        supports_reasoning: false

settings:
  max_iterations: 50           # 单条消息最多几轮工具调用
  max_tool_output: 131072      # 回填给模型的工具输出上限（字节）
  command_timeout_ms: 180000   # toolkit_exec 默认超时
  keep_turns: 10
  http_engine: auto            # auto | socket | winhttp（也可用 --http-engine 覆盖）
```

默认值只有一条规则：**文件里第一个 provider 的 `models:` 里的第一个模型**。

```yaml
providers:
  DeepSeek:                 # ← 第一个 provider
    api_url: https://api.deepseek.com
    api_key: sk-...
    models:
      deepseek-flash: {}    # ← 第一个模型，默认就用它
      deepseek-chat: {}
      deepseek-reasoner: {}
  local:                    # 后面的 provider 不会自动成为默认
    api_url: http://127.0.0.1:11434/v1
    ...
```

想换默认：把某个 provider 挪到最前面，或把某个模型挪到它 `models:` 的最前面；临时切换用
`--provider` / `--model`（UI 顶部的模型菜单也是同一回事）。

`default_model:` 仍会被解析，但**只在某个 provider 完全没有 `models:` 列表时**才当模型名用——
它是给「只有一个端点、连模型清单都懒得写」的配置留的兜底，而不是第二个「默认」旋钮。

> **`roles:` 已不再读取。** 它来自 Python 版实现，那里的「role」意味着按角色路由到不同模型，
> 本实现从来没有这个概念（`cheap`、`interruption` 之类的角色名会让人以为有行为，其实没有）。
> 配置文件里若还留着 `roles:`，启动时会打一条 WARN 列出被忽略的角色名，但**不会**再影响选择
> ——升级后默认模型可能因此变化（变成第一个 provider 的第一个模型），请检查一次
> `/api/providers` 的 `active`。
api_key 只在日志里以掩码出现，HTTP 接口从不下发。

### 网页搜索与抓取（provider 级可选能力）

DeepSeek 对所有 API 用户开放了两个原生端点（`POST /v1/tools/search`、`POST /v1/tools/fetch`，
与 chat 相同的 Bearer 鉴权）。这不是 OpenAI 标准协议，探测不了，所以按 provider 声明
（**写在 provider 块上**；误写在某个 model 下也认，但会在日志里提醒搬上去）：

```yaml
providers:
  deepseek:
    api_url: https://api.deepseek.com
    api_key: sk-...
    supports_search: true      # 放开 toolkit_search
    supports_fetch: true       # 放开 toolkit_fetch
    # supports_featch: true    # ← 拼错不会报错，但会有一条 WARN 告诉你
```

两个易踩的坑（都是实测踩到的）：

- `supports_search` / `supports_fetch` 是 **provider 级**能力，不是模型级。写在 `models:` 下面
  过去会被静默忽略——结果是模型永远拿不到这两个工具，日志里也看不出原因。
- 搜索参数 `localization` 是**地理定位用的 IP 地址**（如 `203.0.113.5`），不是语言码。传
  `"en"` 会被服务端 400；现在工具在发请求之前就拒绝并提示「语言过滤请用 `lang:en`」，
  不会浪费一次计费调用。


```yaml
providers:
  DeepSeek:
    supports_search: true    # 向模型暴露 toolkit_search（结构化搜索结果）
    supports_fetch: true     # 向模型暴露 toolkit_fetch（网页 → Markdown 正文）
```

开关是 provider 级而非模型级——端点挂在账号上，与具体模型无关。未声明的 provider 完全看不到
这两个工具；即使模型硬调也会得到能力错误，不会发请求。默认全关：DeepSeek 按请求次数计费
（各 5 元 / 1000 次成功请求，账号级各 5 QPS），开了才会计费。搜索支持 `site:`、`lang:`、
`after:`/`before:` 等语法，工具描述里会教给模型。

超时与计费预算单独配置（`settings:` 里，也可按工作区覆盖）：

```yaml
settings:
  web_tool_connect_timeout_ms: 10000   # 连接超时
  web_tool_timeout_ms: 60000           # 单次 search/fetch 的读超时
  web_tool_max_calls_per_turn: 10      # 每轮对话最多几次计费调用
```

这三项是必须的，不能让 web 工具沿用 LLM 的超时：`http_read_timeout_ms` 默认 600 秒（流式
响应要用），一次卡住的搜索会让整轮对话停十分钟，而且中途无法取消。现在 web 工具用独立的
有界超时，用户点停止会立即中断传输（返回 `cancelled by user`）。预算用尽后这两个工具会从
工具表里消失，模型不会继续空转重试——否则它会把 `max_iterations` 全部耗在重试上，既慢又费钱。

### 订阅额度（provider 级可选能力）

有些 provider 除了对话接口，还有一个**账号级**接口能回答「我的订阅还剩多少」。目前内置的
解析器是 MiniMax M Plan（5 小时窗口 + 周窗口）：

```bash
curl --location 'https://www.minimax.cn/v1/token_plan/remains' \
  --header 'Authorization: Bearer <M Plan 订阅 Key>'
```

```json
{"model_remains":[{"model_name":"general",
  "start_time":1792561091281,"end_time":1792579091281,"remains_time":9681772,
  "current_interval_remaining_percent":69,"current_interval_status":1,
  "weekly_start_time":1792561091281,"weekly_end_time":1793165891281,
  "weekly_remains_time":596129838,"current_weekly_remaining_percent":96}],
 "base_resp":{"status_code":0,"status_msg":"success"}}
```

官方文档在 [M Plan 常见问题](https://platform.minimax.cn/docs/m-plan/faq)（「如何查看 M Plan 用量」
方式三）与 [用量说明](https://platform.minimax.cn/docs/m-plan/usage-rules)——注意它**不在**
`api-reference/api-overview` 里，那一页只是多模态能力概览。

这不是 OpenAI 标准协议，探测不了，所以按 provider 声明：

```yaml
providers:
  minimax:
    api_url: https://api.minimax.cn/v1
    api_key: sk-cp-…            # M Plan 订阅 Key（按量计费的普通 Key 没有套餐额度）
    supports_usage: true        # 这个 provider 有额度查询接口
    usage_type: minimax_token_plan   # 用哪个解析器读答案
    # usage_url: …              # 可选：端点覆盖（默认按 usage_type 取）
    models: {MiniMax-M3.1-Flash: {…}}
```

### 额度查询的通用配置

MiniMax 之外还有别的形状，所以这几个键是通用的（`usage_type` 决定用哪个解析器）：

| 键 | 取值 | 作用 |
| --- | --- | --- |
| `supports_usage` | `true` / `false` | 这个 provider 有没有额度接口。默认关（关着就完全不查） |
| `usage_type` | `minimax_token_plan` / `json_fields` | 用哪个解析器读答案。`json_fields` 是通用取值 |
| `usage_url` | 完整 URL | 端点。留空则按 `usage_type` 取默认值（`json_fields` 必须写） |
| `usage_auth` | `bearer`（默认）/ `api-key` / `none` | key 怎么送出去：`Authorization: Bearer <key>`、`api-key: <key>`（MiMo Token Plan 的写法）、或者不带（端点靠会话 cookie） |
| `usage_headers` | 键值对 | 额度请求**额外**的头，例如 `Cookie: …`。provider 的 `headers:` 只用于对话，不会带到这里 |
| `usage_fields` | 键值对 | 只有 `usage_type: json_fields` 时用：每个键对应答案 JSON 里的一条路径 |
| `usage_console_url` | 完整 URL | **人**去看额度的页面（控制台）。与 `supports_usage` 相互独立：查不了的 provider 至少能给出这个入口，UI 的徽标会变成「额度 ↗」，点开就是控制台（新标签页），需要自己登录 |

`json_fields` 的路径是点号路径，数字段是数组下标（`data.quota.remaining`、`list.0.used`）。可用的键：

- 窗口：`remaining_percent`（0–100）、`used` + `total`（不给百分比时用这两个算）、`reset_at_ms`（重置时刻，epoch 毫秒）、`reset_in_ms`（还剩多少毫秒）、`label`（窗口名字）
- 周窗口：`weekly_remaining_percent`、`weekly_used` + `weekly_total`、`weekly_reset_at_ms`、`weekly_reset_in_ms`

```yaml
providers:
  someconsole:
    api_url: https://example.com/v1
    api_key: sk-…
    supports_usage: true
    usage_type: json_fields
    usage_url: https://example.com/api/v1/quota
    usage_auth: none                 # 端点靠会话，不带 key
    usage_headers:
      Cookie: 'session=…'            # 会话 cookie（会过期，得自己换）
    usage_fields:
      remaining_percent: data.quota.remainingPercent
      used: data.quota.used
      total: data.quota.total
      reset_at_ms: data.quota.resetAt
```

**查不到就给链接**：只写 `usage_console_url`（不写 `supports_usage`）也行——UI 顶栏会出现一个
「额度 ↗」徽标，点开就是该提供方的控制台（新标签页），登录后自己看。设置页里同一个 provider
也会多一个「打开控制台」按钮。这样「额度只存在于网页控制台」的 provider 也能在顶栏有个入口，
而不是默默消失。


### 各家现状（实测）

| Provider | 有额度接口吗 | 怎么查 |
| --- | --- | --- |
| **MiniMax M Plan** | ✅ | `GET https://www.minimax.cn/v1/token_plan/remains`，`Authorization: Bearer <订阅 Key>`；5 小时窗口 + 周窗口 |
| **小米 MiMo Token Plan** | ❌（**没有能用 API Key 查的接口**） | 额度（Credits）只在网页控制台看：<https://platform.xiaomimimo.com/#/console/plan-manage>。实测：Token Plan 网关 `token-plan-cn.xiaomimimo.com` 只有 OpenAI 兼容接口（`/v1/models`、`/v1/chat/completions`），20 多种猜测的额度路径全是 404；控制台自己的数据接口 `GET /api/v1/tokenPlan/usage` 要**小米账号网页登录态**（`Authorization: Bearer tp-…`、`X-Api-Key`、`api-key`、query 参数一律 401 + `loginUrl` 指向 account.xiaomi.com）；响应头里也没有额度信息。官方文档的 API Reference 一节也没有这个接口 |

所以 provider.yaml 里 **MiMo 不要写 `supports_usage`**——写了只会每次 401，UI 变成「额度 ?」。
它该写的是控制台链接：

```yaml
providers:
  mimo:
    api_url: https://token-plan-cn.xiaomimimo.com/v1
    api_key: tp-…
    usage_console_url: https://platform.xiaomimimo.com/#/console/plan-manage
```

顶栏徽标会变成「额度 ↗」，点开就是控制台（登录后自己看额度）。
真要把它**接进程序里**，只能用 `usage_auth: none` + `usage_headers: {Cookie: …}` 走控制台那条路，
代价是 cookie 会过期、字段名要自己对着控制台的返回填。

要点：

- **和 `stream_include_usage` 完全是两回事**。后者是「在对话响应里附带 token 用量」，
  前者是「另外的账号接口，问还剩多少额度」。两个名字挨得近，所以这里特意分开写。
- 额度查询用 **provider 自己的 key**，由服务端去查；key 不会出现在任何 HTTP 响应里
  （`/api/providers` 只报 `supports_usage` / `usage_type` 这个能力标记）。
- 声明写在 model 下面也会被提升到 provider（并给一条 WARN）——额度是账号级的，不是模型级的。
- 写了 `usage_type:` / `usage_url:` 却忘了 `supports_usage: true` 时会自动打开并提示；
  `usage_type` 写错（不认识）则保持关闭并给一条 WARN，不会拿 key 去乱试。
- 未声明的 provider 完全没有这个接口：UI 不显示徽标，`GET /api/providers/{name}/usage`
  返回 400。

查询接口与 UI：

```bash
curl -s http://127.0.0.1:8282/api/providers/minimax/usage
```

```json
{"ok":true,"provider":"minimax","type":"minimax_token_plan","model_name":"general",
 "window":{"present":true,"name":"5h","remaining_percent":69,
           "start_ms":1792561091281,"end_ms":1792579091281,"remains_ms":9329838,"status":1},
 "weekly":{"present":true,"name":"weekly","remaining_percent":96,
           "start_ms":1792561091281,"end_ms":1793165891281,"remains_ms":596129838,"status":1}}
```

Web UI 顶栏会出现一个「额度 NN%」徽标：鼠标悬停看窗口还剩多久、什么时候重置、周窗口剩多少；
点一下立刻重查；每分钟自动刷新一次。设置页里每个 provider 都能单独点「查询」看额度（不只
当前 provider）。上游报错（500、key 没有订阅等）只会显示成一行提示，不会拖垮服务。

### 按工作区覆盖设置

同一份二进制部署到不同设备/项目时，shell、超时、上下文策略往往不同，所以设置是分层的
（后者覆盖前者）：

```
内置默认 → $TEA_AGENT_HOME/tea_agent.yaml → <workspace>/.tea_agent_run/settings.yaml → CLI
```

```yaml
# <workspace>/.tea_agent_run/settings.yaml
settings:
  shell: bash                  # 这台设备上该用哪个 shell
  command_timeout_ms: 600000
  keep_turns: 6
  enable_compaction: true
  compaction_keep_recent: 4
  tool_output_max_lines: 400
  system_prompt: |             # 也可以整体替换默认规则
    ...
```

另外有一个工作区指令文件 `<workspace>/.tea_agent_run/instructions.md`，内容会原样拼进
system prompt 的「Workspace instructions」小节——用来放「这个项目怎么构建、怎么测、有哪些约定」，
与机器级配置彻底分开。它和 `settings.yaml` 一样在 workspace 打开时读取一次，保证提示词前缀稳定；
需要「改完下一轮就生效」的 system prompt 覆盖请放到 `system_prompt.md`（见上文「工作区与运行时
存储」），它每轮重新读取。

### 回传 `reasoning_content`（多轮工具调用的坑）

第二轮请求要不要把上一轮 assistant 的 `reasoning_content` 原样带回，各家要求正好相反：

- **thinking 模式网关必须带回**（opencode.ai 等），否则第二次请求直接 400：
  *The `reasoning_content` in the thinking mode must be passed back to the API.*
- **DeepSeek 只把它当响应字段**——[官方 Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion)
  里 `reasoning_content` 出现在 assistant 响应中，请求体没有这个字段，所以默认不回传。

`auto`（默认）按端点判断：`*.deepseek.com` 不回传，其余回传。猜错也不会卡住——收到 400 且错误里
提到 `reasoning_content` 时，程序会翻转策略、记住结论（进程内）并重试同一次请求，同时打一条
`provider disagreed about reasoning_content; retrying with echo=...`。要固定行为就显式写：

```yaml
providers:
  opencode:
    reasoning_content: keep    # keep：总是回传；strip：从不回传
```

### 自定义请求头

`headers:` 里的值支持 `${session_id}` 占位符，按会话替换。`opencode.ai` 端点若没配
`x-opencode-session`，程序会自动补一个——[其文档](https://opencode.ai/docs/go/)要求客户端
「为每个会话发送稳定的 session id」。同一份文档还要求客户端用自己的 UA 标识，所以两个传输后端
（socket+TLS 与 WinHTTP）都会带 `User-Agent: tea_agent/<version>`，不会伪装成通用 HTTP 库。

因此**不必修改 Python 版已有的 `provider.yaml`** 就能直接接入。

`tea_agent.yaml`（可选）用来放 C++ 版专属设置，避免污染 `provider.yaml`：

```yaml
settings:
  allow_outside_workspace: false
  http_read_timeout_ms: 600000
  shell: sh                # cmd | powershell | pwsh | bash | sh
  system_prompt: |         # 覆盖默认 system prompt
    You are ...
```

---

## 任务队列与配额调度（`sched/`）

不是所有事都要马上做。「排队」这件事本身也可以排：把活儿写进任务队列，等**供应商还有额度**
的时候再让 agent 去做。

### 任务是什么

一个任务 = 一句要做的事 + 它的状态。状态文件是**一个任务一个文件**
（`<workspace>/.tea_agent_run/tasks/<id>.json`），所以两个写入者不会撞在同一个文件上，
重启也不会丢。

| 字段 | 含义 |
| --- | --- |
| `status` | `queued` → `running` → `done` / `failed` / `cancelled`，或 `waiting_confirmation`（在等用户） |
| `progress` | 0..100，由 agent 自己报 |
| `note` | 最近一条中间状态（一行） |
| `steps[]` | 中间状态的历史：什么时候、做了什么、进度多少 |
| `needs_confirmation` + `question` | **需要用户确认**：agent 停下来等一个答案，任务不会假装做完 |
| `session_id` | 跑这个任务的会话（确认之后接着在同一个会话里继续） |

任务的生命周期由服务端管：开始 → 跑 → 结束；agent 只负责报进度和中间状态。

### agent 怎么报进度：`toolkit_task`

```
toolkit_task op=update progress=50 note="已经把构建产物删了" 
toolkit_task op=update needs_confirmation=true question="可以删掉旧构建产物吗？"
toolkit_task op=get / op=list
```

不带 `task_id` 时指的是**当前这一轮所属的任务**。`needs_confirmation=true` 会让任务停在
`waiting_confirmation`——这是「任务需要用户确认」在状态里的样子。

### 调度器：`sched/task_runner.py`

它做三件事：查额度 → 挑排队中的任务 → 看着它做完。

```bash
# 放进队列
python3 sched/task_runner.py --submit "把 README 里的例子都跑一遍"

# 跑一轮就退出（看效果用这个）
python3 sched/task_runner.py --once

# 常驻：每 5 分钟看一次额度，有余量就做一件
python3 sched/task_runner.py --workspace ws_xxx --interval 300
```

额度取自 provider 自己的额度端点（MiniMax M Plan 走 `GET /api/providers/minimax/usage`，
即「5 小时窗口 + 每周窗口」）。**只有余量够的时候才开工**：

```
2026-10-21 20:11:32  scheduler up: http://127.0.0.1:8282, workspace ws_b5e0…, provider minimax
2026-10-21 20:11:32  quota of 'minimax': 0.0% left (need 5%) - not starting anything, the window resets in 42 min
2026-10-21 20:12:32  quota of 'minimax': 60.0% left - going to work
2026-10-21 20:12:32  started task t_4b2f… (a task)
2026-10-21 20:12:35  task t_4b2f…: done 100%
```

几个刻意的选择：

- **额度读不到就不开工**（provider 没声明额度端点、或者查询失败）。宁可不做，也不要在
  额度已经用光时继续烧；确实要无条件做，加 `--allow-without-quota`。
- **工作一定跑在查额度的那个 provider 上**：启动任务时会把 provider 带过去，否则
  「查的是 A 的额度、跑的却是 B」就成了假账。
- **别的 provider 的任务不碰**：任务自己指定了 provider 就由它自己排队。
- **一次做一件**（`--max-per-round`）：额度是窗口制的，一口气全做完没有意义。
- **在等人确认时停下**：那一轮跑完就停，日志里会写清楚要人回答什么。

只依赖标准库：这个调度器跑在设备上、和 agent 在一起，那里没有 pip。

回归测试：`python3 tests/smoke_task_runner.py`（28 项）——用一个假 provider 同时提供
「聊天」和「额度」两个端点，验证余量为 0 时任务保持排队、**一次模型请求都没发**；余量
够时任务被启动并跑完；需要确认的任务停在 `waiting_confirmation`，回答之后继续做完。

---
## 部署到嵌入式设备

### 交叉编译

通用的交叉编译方法（编译矩阵、toolchain 文件、宿主工具、产物校验、错误对照）在
[跨平台编译](#跨平台编译) 一节。嵌入式这边只多两件事：

```bash
cmake --preset arm64-linux          # 工具链位置用 TEA_AARCH64_PREFIX 覆盖
cmake --build --preset arm64-linux -j"$(nproc)"
```

- **默认全静态**：aarch64 的 toolchain 文件打开 `TEA_AGENT_STATIC_RUNTIME` 与
  `TEA_AGENT_STATIC_LIBC`，目标机的 glibc 版本无关紧要；构建机与目标机环境一致时用
  `-DTEA_AGENT_STATIC_LIBC=OFF` 改回动态（静态 glibc 无法 `dlopen` NSS 模块，域名解析行为
  会变，详见下文「单文件部署」）。
- **选对工具链**：默认用 Arm GNU Toolchain 11.3（glibc 2.35）。Linaro 6.5 会被选中时
  toolchain 文件会给 WARNING——它的静态 glibc 2.23 在 4.14 内核设备上一进
  `std::condition_variable::wait()` 就抛错，进程启动约 1 秒即
  `terminate called without an active exception`（真机结论：Linaro 静态 ❌、Linaro 动态 ✅、
  11.3 静态 ✅）。

### 体积

| 构建 | 大小 | 运行库依赖 |
| --- | --- | --- |
| Windows x64 / MinGW-w64（0.9.1 实测，`win64-mingw-cross`） | 6.7 MB（`strip` 后 4.3 MB） | 仅 Windows 10+ 自带的 UCRT 与系统 DLL，可单文件分发 |
| Windows x64 / MSVC（0.8.1 实测，未在 0.9.1 重测） | 2.6 MB | 需要目标机安装 VC++ 运行库（`MSVCP140.dll` / `VCRUNTIME140*.dll`） |
| Linux x64（0.9.1 实测，静态构建） | 6.1 MB（`strip` 后 5.2 MB） | **完全静态**：不依赖目标机的 glibc 版本 |
| Linux aarch64（0.9.1 实测，静态构建） | 17.8 MB，`strip` 后 4.5 MB | 默认完全静态（Arm GNU 11.3 + 静态 glibc）；该工具链的 libstdc++ 自带调试信息，部署前记得 `strip` |

想进一步压：`-DCMAKE_BUILD_TYPE=MinSizeRel` + `strip`；不需要 HTTPS 可
`-DTEA_AGENT_USE_MBEDTLS=OFF`（体积显著下降，但 Linux 上就没有 `https://` 了）；
还可以用自定义 `MBEDTLS_CONFIG_FILE` 裁掉 mbedTLS 里用不到的算法。

### 单文件部署（完全静态）

一个 ELF 拷到目标机就能跑：**不依赖目标机的 glibc 版本**，也**不需要旁边的 PEM 文件**。
这正是「部署到比构建机更老的系统」所需要的——动态链接的二进制在那种机器上会先报
`version 'GLIBC_2.xx' not found` 再退出，连一句有用的错误都来不及打印。

| 平台 | 链接方式 | 目标机需要什么 |
| --- | --- | --- |
| Windows / MinGW-w64 | `-static-libgcc -static-libstdc++ -static`（自动） | 只有 Windows 自带的 `KERNEL32/ADVAPI32/SHELL32/WINHTTP/WS2_32` + UCRT |
| Linux / 其它 ELF（GCC/Clang） | `-static -no-pie`（**需显式打开**，aarch64 toolchain 已默认打开） | **什么都不需要**，连 glibc 都不用 |
| macOS | 不适用 | 系统不提供静态 `libc++`，自动跳过 |

本机构建默认是**动态**的（能跑、编译快）；要做单文件部署就显式打开两个开关：

```bash
cmake -S . -B build-static -DCMAKE_BUILD_TYPE=Release \
  -DTEA_AGENT_STATIC_RUNTIME=ON -DTEA_AGENT_STATIC_LIBC=ON
cmake --build build-static -j"$(nproc)"
file    build-static/bin/tea_agent     # → statically linked
readelf -d build-static/bin/tea_agent  # → There is no dynamic section in this file.
```

三个相关开关：

| 选项 | 默认 | 作用 |
| --- | --- | --- |
| `TEA_AGENT_STATIC_RUNTIME` | `OFF`（aarch64 toolchain: `ON`） | 静态链接 `libstdc++`/`libgcc` |
| `TEA_AGENT_STATIC_LIBC` | `OFF`（aarch64 toolchain: `ON`） | 再静态链接 libc（`-static -no-pie`） |
| `TEA_AGENT_USE_MBEDTLS` | `ON` | 关掉可显著减小体积，但 Linux 上就没有 `https://` 了 |

#### glibc 的 NSS 陷阱（已处理）

静态链接的 glibc **不能**安全地 `dlopen` NSS 模块：`getaddrinfo` 会按
`/etc/nsswitch.conf` 加载 `libnss_resolve.so.2`（systemd-resolved）或
`libnss_mdns*.so.2`，把共享模块混进静态二进制里会 **SIGSEGV**。这在 `resolve` 出现在
nsswitch 里的现代发行版上是必现的（本项目实测崩溃过）。

所以 `TEA_STATIC_LIBC` 打开时，`http_socket.cpp` 会把 hosts 数据库钉到 glibc 内建的两种实现：

```c
__nss_configure_lookup("hosts", "files dns");   // 仅静态构建
```

代价（只影响静态构建）：绕过 systemd-resolved 的 NSS 前端与 mDNS。普通 DNS 仍然工作——
这类机器的 `/etc/resolv.conf` 指向 `127.0.0.53` 这个 stub，域名照样由 systemd-resolved 解析，
只是不经过它的 NSS 模块。musl 工具链没有 NSS 模块，无需这层处理。

`tests/smoke_single_file.py` 把这条钉住了：它确认证书嵌在 ELF 里、`ldd` 无共享库依赖、
把二进制单独拷到一个空目录也能启动并使用内嵌根证书，最后**用真实域名走一遍解析**——
回归时的表现是进程被 SIGSEGV 杀掉，而不是一个难查的线上问题。

### TLS 与证书

- Windows 默认走 WinHTTP，使用**系统证书库**，无需额外文件；拿不到状态行时自动回退内置 mbedTLS。
  **内嵌的 `cacert.pem` 与 WinHTTP 无关**：它只被 mbedTLS 后端读取（见下面的覆盖顺序），
  WinHTTP/schannel 永远用 Windows 的证书库。所以「Windows 上编译时要不要内嵌 CA」不影响
  WinHTTP 的行为——两者是两套独立的信任链。

  少数 Windows 环境下 WinHTTP 会连不上（常见于 `WinHttpSendRequest failed (WinHTTP error
  12029)`：schannel 被策略禁用、系统代理只配在 Internet 选项里、或者域名只解析到 IPv6 而
  WinHTTP 不会 Happy Eyeballs），这时会自动回退到内置 mbedTLS，用户无感。现在的行为：

  - 每个主机**只探测一次**：回退成功后记住该主机，后续请求直接走 mbedTLS，不再每次先失败
    一次（以前每轮模型调用都会多一次失败的连接尝试和一行 WARN）；
  - 首次回退记一行 WARN 并提示可以用 `--http-engine socket`（或配置里写
    `settings: { http_engine: socket }`，可持久化、也可按工作区覆盖）彻底跳过探测，
    之后降为 DEBUG；
  - 两个后端都失败时，错误信息同时带上两边的原因，而不是只留 WinHTTP 那句；
  - `GET /api/health` 的 `http_socket_fallback` 会列出已经改用内置 TLS 的主机（没有则为空）。
- 其它平台走内置 mbedTLS，根证书**编译进二进制**（`3rd/ca-bundle/cacert.pem` 由
  `tools/embed_ca_bundle.cpp` 生成 `ca_bundle.cpp`），所以单文件部署不需要随附 PEM。
  启动日志会写 `tls: CA bundle embedded in the binary (N bytes)`。
- 覆盖顺序：`--ca-bundle <pem>` 优先（便于不重新编译就更新根证书）→ 内嵌证书 → 磁盘查找
  （同级 `ca-bundle/cacert.pem`、同级 `cacert.pem`、`../3rd/ca-bundle/cacert.pem`、
  工作目录 `3rd/ca-bundle/cacert.pem`、`$TEA_AGENT_HOME/ca-bundle/cacert.pem`）。
  最后这层只对「构建时没有 vendored 证书」的构建有意义。

---

## Windows 登录自启动（相当于 `systemctl --user enable`）

Windows 没有用户级 systemd。最接近的三种做法，差别在于「跟不跟人走」：

| 方案 | 相当于 | 是否推荐 |
| --- | --- | --- |
| **任务计划程序 + 登录触发**（`InteractiveToken`） | `systemctl --user enable --now` + `Restart=` | ✅ 本仓库自带脚本 |
| `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` / 启动文件夹 | 只有 `enable`：没有重启、没有任务管理 | 临时用 |
| Windows 服务（`sc create`，session 0） | 机器级 `systemctl enable`，**与用户登录无关** | 需要开机即起、多用户共享时 |

Windows 服务不适合这个程序：它跑在 session 0，看不到用户桌面会话，`%USERPROFILE%`、映射的
网络驱动器、`TEA_AGENT_HOME` 都可能不是你以为的那一个，而且注册要管理员权限。所以这里用
任务计划程序——在你自己的会话里跑，不弹 UAC，也不用存密码。

```bat
:: 注册：登录后 5 秒启动（自动找 build-mingw → build-gcc → build-msvc）
scripts\install-autostart.bat

:: 注册并立刻启动一次；后面的参数原样透传给 tea_agent
scripts\install-autostart.bat -Start --port 8282 --workspace C:\work\proj

:: 查看 / 停止 / 卸载
scripts\install-autostart.bat -Status
schtasks /End /TN tea_agent
scripts\install-autostart.bat -Uninstall

:: 只看会注册什么（生成 launcher 与任务 XML，不写进计划程序）
scripts\install-autostart.bat -DryRun
```

`pwsh -File scripts/install-autostart.ps1 ...` 与 `.bat` 等价。

**它做了什么**

- **隐藏窗口**：任务计划程序直接跑控制台程序会留一个黑窗口。脚本生成
  `%TEA_AGENT_HOME%\autostart\start-hidden.vbs`，由 `wscript.exe` 用 `SW_HIDE` 起进程——
  这正是「登录后悄悄跑起来的后台服务」应有的样子。`--no-console` 也会自动补上，
  日志照旧写 `~/.tea_agent/logs/tea_agent.log`。
- **崩溃重启**：VBS 在退出码非 0 时 3 秒后重拉；任务本身还配了 `<RestartOnFailure>`
  （1 分钟一次，最多 999 次），两者叠加接近 `Restart=always`。
- **单实例**：`<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>`，拨弄两下
  也不会起两个抢同一个端口的进程。
- **不超时**：`<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>`，服务不该被计划程序杀掉。
- **只用当前用户身份**：`<LogonType>InteractiveToken</LogonType>` + 正常权限，
  不需要管理员，也不保存密码。

**注意**

- 端口默认仍是 `127.0.0.1:8282`。要在局域网访问就加 `--host 0.0.0.0`，并先读「安全须知」。
  启动信息会把该网卡的活跃地址（如 `http://192.168.1.38:8282`）直接打印出来。
- 自启动用的参数固定在任务里。改参数就重新跑一次脚本（`/F` 会覆盖同名任务）。
- `-NoHide` 可让任务直接执行 exe（调试时想看到控制台就用它）。
- 注销/重启时进程随会话结束；下次登录由触发器重新拉起。
- Linux 上对应的做法是 `~/.config/systemd/user/tea_agent.service` +
  `systemctl --user enable --now tea_agent`，条件不合适（没有 systemd）时用 `@reboot` 的
  crontab 也能达到同样效果。

---

## 源码结构

```
src/
  main.cpp                  CLI、信号、启动横幅
  config.{h,cpp}            provider.yaml / default / headers 解析
  message.{h,cpp}           消息模型（wire 格式 + 存储格式）
  llm.{h,cpp}               OpenAI 兼容客户端：SSE、tool_calls 聚合、重试
  usage.{h,cpp}             订阅额度查询：provider 专用端点 + 解析器（minimax_token_plan）
  agent.{h,cpp}             多轮工具调用主循环、预算与滚动摘要、system prompt
  prompt_queue.h            轮次进行中的排队消息（线程安全 FIFO，每轮采纳一条）
  tools.{h,cpp}             工具注册表与内置工具（exec/file/memory/agent_doc/load_image/todo/task）
  command.{h,cpp}           命令执行（Job Object / 进程组、超时、编码归一）
  workspace.{h,cpp}         多工作区、.tea_agent_run/、会话/记忆/产出文件、索引；
                            并发写用进程级锁 + 原子替换（见「并发」一节）
  task.{h,cpp}               任务队列：一个任务一个文件（.tea_agent_run/tasks/），
                            状态/进度/中间状态/等待用户确认
  state_lock.h              进程级锁：所有「读-改-写」的状态文件共用一把
  html_export.{h,cpp}       会话 → 自包含 HTML（内联 CSS/图片、可导入的 JSON 区块）
  server.{h,cpp}            HTTP 路由、SSE 泵、内置资源
  http.{h,cpp}              传输分派（socket+TLS / WinHTTP，自动回退）
  http_socket.cpp           HTTP/1.1 over TCP（+ 可选 TLS）
  http_winhttp.cpp          Windows WinHTTP 后端
  tls.{h,cpp}               mbedTLS 客户端封装
  net_compat.h              SIGPIPE / send 标志的平台差异
  platform_fs.cpp           路径与文件 I/O（Win32 宽字符 / POSIX，全 UTF-8）+ write_file_atomic
  env.{h,cpp}               设备环境探测（OS/架构/时区/CPU/内存/磁盘/shell）
  output_filter.{h,cpp}     工具输出按内容属性削减（build/test/通用）
  util.{h,cpp} log.{h,cpp}  字符串、时间、编码、日志
  version.{h,cpp}           版本号（唯一手改的真相）；commit 在二进制旁边的
                            tea_agent.buildinfo 里，程序启动时读它
web/                        index.html / tokens.css / app.css / app.js（构建期嵌入）
tools/                      embed_assets.cpp、embed_ca_bundle.cpp、host_embed/（宿主工具子项目）、mock_*.mjs
tests/e2e.ps1              端到端测试（155 项）；tests/sse_probe.mjs 严格流客户端
scripts/                    build.sh / build.bat / build-mingw.bat / vcenv.bat /
                            install-autostart.ps1（+ .bat）= Windows 登录自启动
sched/task_runner.py        调度器：查 provider 额度，有余量就做排队中的任务
CMakePresets.json           msvc / mingw / posix / arm64-linux / win64-mingw-cross 预设
cmake/toolchains/           aarch64-linux-gnu.cmake / mingw-w64-x86_64.cmake（交叉编译入口）
cmake/version_git_hash.cmake 生成 tea_agent.buildinfo（写在二进制旁边，不动源码树）
cmake/free-running-exe.cmake   运行中也能链接：先把旧 exe 改名让位
3rd/                        第三方依赖
```

**版本号与 commit hash**：`src/version.h` 是唯一手改的版本真相（`TEA_AGENT_VERSION_STRING`），
CMake 从它解析 `project(VERSION)`，所以「二进制报什么版本」和「构建日志写什么版本」不会各说各话。

commit 则是**构建期元数据**，它既不在源码里、也不在二进制里：构建把它写进二进制旁边的
`tea_agent.buildinfo`（`cmake/version_git_hash.cmake`），程序启动时读这个文件并在横幅和
`/api/health` 里报出来。

```
# tea_agent.buildinfo（与二进制放在一起）
version=0.9.1
commit=678cd2c1f014        # 工作区有未提交改动时是 <hash>-dirty
```

好处有三层：

- **构建不再改源码树**。以前是把 hash 写回 `src/version.cpp`，代价是每次构建都把工作区弄脏，
  构建完立刻 `git status` 什么也看不出来；而且源码树永远处于「-dirty」（连它自己那次改动也算脏），
  旧脚本只好特判自己写的那个文件；native 与交叉两个构建目录还会抢同一个文件。
- **二进制可复现**。既然编译进去的构建数据已经没有了，再让链接器别把构建时间戳进 PE 头，
  同一个 commit 两次构建就得到**逐字节相同**的二进制（MinGW 用
  `-Wl,--no-insert-timestamp`，MSVC 用 `/Brepro`）。要核对发布件是不是某个 commit 构建的，
  比对 `tea_agent.buildinfo` 即可。
- **单文件部署不撒谎**。只把 `tea_agent.exe` 拷到设备上的话，旁边没有 buildinfo，于是没有
  provenance 可显示——横幅就只印版本号，而不是假装自己来自某个 commit。

**为什么不用 `std::filesystem`**：Windows 上它的窄字符转换走 ANSI 代码页，非 ASCII 路径会抛
`Illegal byte sequence`（MinGW 下尤其明显）；同时 GCC 6.5 只有 `std::experimental::filesystem`。
所以路径与文件 I/O 直接基于 Win32 宽字符 API / POSIX，全流程 UTF-8。

**为什么支持 GCC 6.5**：代码刻意不使用 C++17 专属语言特性，可用
`-DTEA_AGENT_CXX_STANDARD=14` 复核（这条命令会以 C++14 模式编译全部代码）。

---

## 第三方依赖（全部位于 `3rd/`）

| 目录 | 版本 | 用途 | 许可证 |
| --- | --- | --- | --- |
| `3rd/nlohmann_json/` | 3.11.3 | JSON | MIT |
| `3rd/cpp-httplib/` | 0.61.0 | HTTP 服务端 | MIT |
| `3rd/yaml-cpp/` | 0.8.0 | YAML 解析 | MIT |
| `3rd/mbedtls/` | 2.28.9 LTS | TLS 客户端（跨平台） | Apache-2.0 / GPL-2.0（见 `3rd/mbedtls/LICENSE`） |
| `3rd/miniz/` | 3.1.2 | deflate + ZIP 打包（工作区归档下载） | MIT |
| `3rd/ca-bundle/` | curl CA | 根证书 | MPL-2.0 |

各目录下的 `UPSTREAM_VERSION.txt` 记录上游版本（不叫 `VERSION`：Windows 文件名不区分大小写，
会遮蔽标准头 `<version>`），并记下上游 URL 与 sha256。yaml-cpp 0.8.0 依赖 `<string>` 间接引入
`<cstdint>`，用 `-include cstdint` 解决，不修改上游源码。

miniz 只取上游 release 里的**合并版两个文件**（`miniz.c` + `miniz.h`，逐字节未改），作为独立的
静态库目标 `tea_miniz` 编译：它是 C，所以顶层 `project()` 声明了 `LANGUAGES C CXX`；它的告警
不是我们的，所以只对这个目标关掉（MSVC `/W0`，其它 `-w`）。

---

## 测试

```bash
pwsh -File tests/e2e.ps1                                  # 默认构建产物
pwsh -File tests/e2e.ps1 -Exe build-msvc/bin/tea_agent.exe
pwsh -File tests/autostart.ps1                            # 登录自启动：launcher/任务 XML + 隐藏启动实测
pwsh -File tests/relink-while-running.ps1 -Exe build-gcc/bin/tea_agent.exe  # 运行中也能构建（改名让位）

python3 tests/smoke_web_tools.py                          # 网页工具（本地 mock，不产生费用）
python3 tests/smoke_provider_usage.py                      # 订阅额度查询：声明/归一化/错误处理
node tests/ui_usage.mjs                                   # 顶栏额度徽标 + 设置页按 provider 查询（需 google-chrome）
python3 tests/smoke_vision.py                             # 粘贴图片：校验/落盘/多模态请求体
python3 tests/smoke_tool_images.py                        # toolkit_load_image：磁盘图片进对话/上限/拒绝
node tests/ui_tool_images.mjs                             # 加载的图片在对话里可见（含刷新后，需 google-chrome）
python3 tests/smoke_console_level.py                      # 控制台只回声 WARN/ERROR、日志文件仍是 INFO
python3 tests/smoke_bind_urls.py                          # --host 0.0.0.0 打印活跃地址，且该地址真能打开 UI
node tests/ui_boot_probe.mjs                              # 不启浏览器也能跑：app.js 启动路径不抛异常
python3 tests/smoke_config_quickstart.py                  # 首次运行只填 key 生成 provider.yaml
node tests/ui_setup_flow.mjs                              # 首次运行上传配置（需 google-chrome）
node tests/ui_setup_quickstart.mjs                        # 首次运行快捷通道：粘贴 key 回车即开始（需 google-chrome）
node tests/ui_paste_image.mjs                             # 粘贴图片的浏览器端行为（需 google-chrome）
node tests/ui_metrics_layout.mjs                          # 指标栏始终单行且不截断（需 google-chrome）
python3 tests/smoke_single_file.py                        # 内嵌证书 + 静态链接 + 静态下的 DNS
python3 tests/smoke_prompt_queue.py                       # 消息插队：采纳 / 取消 / 退回
python3 tests/smoke_concurrency.py                        # 并发写：锁 + 原子替换，一个事件都不能丢
python3 tests/smoke_task_runner.py                        # 任务队列 + 配额调度器（余量为 0 就不开工）
python3 tests/smoke_agent_doc.py                          # 工作区经验 agent.md：注入/写入/上限
python3 tests/smoke_system_prompt.py                      # 工作区 system prompt：读写/下一轮生效/上限
node tests/ui_workspace_add.mjs                           # 新增工作区：完整路径 + 就地提示 + 创建
node tests/ui_workspace_access.mjs                        # 文件访问范围开关：受限 / 全局 / 命令行锁死（需 google-chrome）
node tests/smoke_turn_resume.mjs                          # 断线不取消轮次：回放 / 续播 / stop 仍然有效
node tests/ui_stream_resume.mjs                           # 关掉页面再打开，接回正在跑的那一轮（需 google-chrome）
node tests/ui_second_client.mjs                           # 冷启动不被其它工作区的轮次拽走，点开才接上（需 google-chrome）
node tests/ui_two_clients.mjs                             # 两个浏览器一个主题：?s= 接上 / 排队 / 离开 / 停止（需 google-chrome）
node tests/ui_topic_url.mjs                               # 同浏览器两个标签页各看各的主题，刷新仍在原主题（需 google-chrome）
python3 tests/smoke_multi_session.py                      # 多会话并发 + 默认排队 + interrupt + 跟随者上限
python3 tests/smoke_todo.py                               # 任务列表：分解/进度/唯一的 active/落盘/导入
python3 tests/smoke_artifacts.py                          # 产出文件与 /download：收录/去重/流式/超限
node tests/ui_artifacts.mjs                               # 最终回答下方的下载链接（需 google-chrome）
node tests/ui_todo.mjs                                    # 输入框上方的进度面板与侧栏徽章（需 google-chrome）
python3 tests/smoke_export_html.py                        # 导出会话为单文件 HTML：内联/转义/往返导入
node tests/ui_session_export.mjs                          # 导出按钮 → 下载 → 离线打开 → 导入（需 google-chrome）
python3 tests/smoke_run_archive.py                        # 工作区归档下载：真 zip / 沙箱 / 超限跳过
node tests/ui_files_download.mjs                          # 文件页「下载」按钮真的产出 zip（需 google-chrome）
node tests/ui_stream_guard.mjs                            # 生成期间切换/新建被明确拒绝
node tests/ui_queue.mjs                                   # 生成中 Enter 排队、取消、采纳（需 google-chrome）
node tests/ui_sidebar.mjs                                 # 侧栏隐藏/显示与正文宽度自适应（需 google-chrome）
node tests/ui_agent_doc.mjs                               # 记忆页的 agent.md 编辑框（需 google-chrome）
node tests/ui_system_prompt.mjs                           # 设置页的系统提示词编辑/预览/恢复默认（需 google-chrome）
python3 tests/smoke_destroy.py                            # 销毁：预览/确认/删除 tea home 与各 .tea_agent_run
node tests/ui_destroy.mjs                                 # 设置页「销毁」确认框与删除后回到首次运行（需 google-chrome）
node tests/ui_render_perf.mjs                             # 流式渲染不重建气泡（需 google-chrome）
node tests/ui_crumbs.mjs                                  # 顶栏显示工作区与会话标题（需 google-chrome）
node tests/ui_turn_index.mjs                              # 对话跳转索引：64 字 tip / 点击定位（需 google-chrome）
node tests/ui_fork.mjs                                    # 分叉：选中编号→输入即开新会话，再点/Esc 取消（需 google-chrome）
node tests/ui_status.mjs                                  # 连接状态灯：抖动容忍 / 自愈 / 可点（需 google-chrome）
```

`tests/smoke_web_tools.py` 用一个假的 provider 同时模拟 chat 与 `/v1/tools/{search,fetch}`，
覆盖：工具按 provider 声明才出现在工具表、未声明时强制调用被拒且不外呼、端点卡住时按
`web_tool_timeout_ms` 超时而不是拖住整轮、`/api/chat/stop` 能中断传输中的请求。

`tests/smoke_tool_images.py` 覆盖 `toolkit_load_image`：视觉模型的工具表里有它、纯文本模型没有；
工具结果本身是文本（不是几百 KB 的 base64），图片作为 follow-up user 消息进入**下一次请求**，
data URL 解码后与磁盘文件逐字节相同；副本落在 `artifacts/images/`，会话里存的是工作区相对路径、
`/raw` 能取回，源文件删掉之后下一轮照样发送；不存在的文件、非图片、越出工作区分别给出可操作的
报错且不附加图片；一轮最多附加 6 张，其余在工具结果里说明。

`tests/ui_tool_images.mjs` 在浏览器里看效果：一轮中 `tool_images` 事件让那条消息连同图片出现，
`<img>` 真的解码成功（`naturalWidth` 正确）、`src` 走 `/raw`；工具行展开后写着副本路径与 MIME；
刷新后图片仍在原位且没有重复的气泡。

`tests/smoke_vision.py` 覆盖图片通路：没有 `supports_vision` 的模型被拒且**不会**请求 provider、
坏 base64 / 非图片字节 / 超过 4 张 / 空载荷分别报对应错误码、合法图片落盘且字节一致、
请求体是 `[{type:text},{type:image_url}]` 且 base64 往返无损、会话只存路径、`/raw` 能取回原图、
纯图片消息与纯文本消息都仍然可用。

`tests/ui_paste_image.mjs` 用真实粘贴事件（`ClipboardEvent` + `DataTransfer`）验证缩略图出现、
× 能移除、发送后缩略图条清空、用户气泡经 `/raw` 渲染且图片真的加载成功（`naturalWidth > 0`）。

`tests/ui_metrics_layout.mjs` 在 9 个窗口宽度（1440→600）下把每个指标填成最宽的真实数值，
断言指标栏**只有一行**、且最长的那项没有被截断。这类「不许换行」的布局约束加一个指标或
改一个标签就会破，所以单独钉住。

`tests/smoke_prompt_queue.py` 用三个假 provider 覆盖插队语义：工具执行中入队后，agent 在
下一轮迭代采纳它并出现在后续请求里、落盘为 user 消息；模型已经流式输出完最终答案时入队，
本轮会继续一次迭代而不是结束；按 id 取消 / 全部取消；停止轮次后未采纳的消息通过
`queue_unconsumed` 原样退回且不进入会话；没有进行中的轮次返回 409；带图片 / 空消息返回 400。

`tests/smoke_agent_doc.py` 覆盖工作区经验文件：打开工作区即生成带模板的 `agent.md` 与
`scripts/`；每次模型请求的 system prompt 都带着它；`op=append` 落盘带日期并立刻出现在同一轮的
下一次请求里；`op=read/replace/scripts` 各自生效；超过 32 KB 的 append 报错并提示压缩且不
改动文件；`GET/PUT /api/workspaces/{id}/agent-doc` 能读、能改、超限报 400。

`tests/smoke_system_prompt.py` 覆盖工作区级 system prompt：默认回落到内置规则（或
`settings.yaml` 的 `system_prompt`）；`GET/PUT /api/workspaces/{id}/system-prompt` 能读能写，
`current_prompt`/`source` 反映真正生效的人设；保存后**下一次模型请求**就以新内容开头，且替换
而非追加内置人设，环境块与 agent.md 仍在；把默认文本原样保存回去不会产生覆盖；
空白内容恢复默认；超过 64 KB 报 400 且不改文件。

`tests/smoke_destroy.py` 覆盖「销毁」：`GET /api/destroy` 列出的正是 tea home 与各工作区的
`.tea_agent_run`（目录已不存在的也照实标 `exists:false`）；不带 `confirm` 的 POST 返回 400 且
一个文件都不动；有轮次在跑时返回 409；确认后 tea home（含 provider.yaml 与**正被打开的**全局
日志）和各 `.tea_agent_run` 都消失，而项目目录与其中的文件还在；随后 `/api/health` 报
`config_loaded=false`、`/api/config/status` 要求重新配置、`/api/workspaces` 为空；再删一次是
幂等的；删除后重新添加同一工作区也正常。

`tests/ui_destroy.mjs` 在真实浏览器里点一遍：设置页有红色「销毁…」按钮，确认框写着
「是否真的彻底删除痕迹？」并逐条列出 tea home 与两个工作区的 `.tea_agent_run`；取消后什么都没
删；确认后目录消失、页面自动刷新并回到上传 provider.yaml 的首次运行界面，localStorage 里
指向已删工作区/会话的 id 也被清掉。

`tests/ui_system_prompt.mjs` 在真实浏览器里钉住「设置 → 系统提示词」：打开面板编辑框里就是
当前生效的人设（没有覆盖时是默认提示词，不是空框）且不算「未保存」、编辑后出现「未保存」、
还原回到当前人设、保存落盘且 `/prompt` 反映新内容、预览弹窗展示完整提示词、切换工作区各自
显示自己的提示词、恢复默认后保存会删掉覆盖。

`tests/ui_status.mjs` 覆盖右上角状态灯：用 CDP 只屏蔽 `/api/health`（其它接口照常工作，正是
报告里的情形）。断言单次失败**不会**翻成「未连接」（一次抖动不是掉线）、连续两次才翻且横幅
说明原因、恢复后**不用刷新**自动变回「已连接」、一次普通请求成功即可提前自愈（不等 10s 轮询）、
状态灯可点击并立即重查。旧实现在这套断言下会一直停在同一个状态（它只在启动时查一次）。

`tests/ui_crumbs.mjs` 钉住顶栏面包屑：启动后必须显示「工作区 / 会话标题」而不是占位符
「无工作区 / 新会话」（旧实现只在切换视图时刷新，启动时工作区还没加载完就再也不会更新）；
首轮回答后标题换成服务端从第一条消息生成的标题、刷新后保持、切换工作区、新建会话、
切回原工作区、重命名，六个时机都要跟着变。

`tests/smoke_console_level.py` 钉住两个阈值的分工：默认启动 + 一次 `/api/providers` 请求后，
终端上的日志行**没有一条** `INFO`（只剩 WARN/ERROR），而日志文件里照样有 `[INFO]`，横幅仍然
只打印一次；`--console-level info` 让终端重新出现 `INFO`；`--no-console` 与 `--quiet` 都不回声
日志（`--quiet` 还省掉横幅），但文件照写；`--console-level verbose` 报错并按 `warn` 处理；
`--log-level error` 只收窄文件、不影响控制台；`--help` 同时列出两个开关。

`tests/smoke_bind_urls.py` 钉住启动信息里的 `Web UI` 行：`--host 0.0.0.0` 时第一条必须是
本机**走默认路由的活跃地址**（与测试自己用 UDP `connect()` 选出的源地址一致），而不是
`127.0.0.1`；该地址在服务运行期间真的能取到 `/` 且返回内置 UI；回环地址仍然列出并标注
`(this machine only)`；setup 模式的 `hint: open …` 用同一个地址；`--host 127.0.0.1` 只打印一条
且没有多余标注；绑定某个具体地址时原样打印。

`tests/ui_turn_index.mjs` 预置一个 30 轮的会话再打开页面：右侧索引必须有 30 个编号；第 10 条
用户输入故意超过 64 个字符，断言 `title` 与悬停 tip 都只保留前 64 个字符加省略号，且 tip
位于编号左侧、不遮住编号；提示卡片宽度必须是 260–320px、预览最多折 4 行——用 `left` 定位 +
`max-width` 时浏览器拿「left 到视口右缘」当可用宽度，索引条贴右就会把提示压成一列单字，
所以 950px 窄窗下再验一次；点第 10 个编号后对应的用户气泡滚到视口顶部且高亮第 10 项；滚到
顶部/底部分别高亮第 1/30 项；新建会话时索引隐藏，重新打开会话后恢复。

`tests/ui_fork.mjs` 预置一个 4 轮会话并开一个只会回「分支回答」的假 provider：点第 3 个编号
后编号必须有分叉描边、`aria-pressed=true`、输入框上方出现「第 3 条」提示条且占位符变成分叉
模式；再点同一个编号、点提示条的 ×、按 Esc 都能撤销（提示条隐藏、占位符复原）；重新选第 2 条并发送一句话
后，必须**多出一个会话**（`forked_from` / `fork_turn=2`，标题以 `分叉 2 · ` 开头），它的用户
消息恰好是第 1、2 轮加上这句新输入，父会话仍是原来的 4 轮且不含新输入；页面本身也切到分支
（3 个用户气泡、顶栏跟着换标题）。分叉结束后再发一句普通消息，仍然留在分支里，不会冒出第三个
会话。

`tests/ui_render_perf.mjs` 起一轮 320 条 delta、中间夹一次工具调用的回答，用
`MutationObserver` 数 live 气泡被替换的次数：**每来一条 delta 就重建整块**是这次的回归点
（旧实现 320 条 delta → 324 次重建），现在必须是个位数。同时给已完成的段落和工具行打标记，
断言它们在后续流式过程中没有被重建；结束后的最终渲染仍然完整（正文、工具行、光标消失）。

`tests/ui_agent_doc.mjs` 在浏览器里操作「记忆」页的 agent.md 卡片：内容来自服务端、路径显示为
`.tea_agent_run/agent.md`、脚本列在下面；改动后提示「未保存」、还原按钮恢复服务端副本、保存后
磁盘与 API 都拿到新内容；切到另一个工作区时加载的是它自己的那份（不串内容、不串脚本）。

`tests/ui_workspace_add.mjs` 覆盖「添加工作区」这唯一一条入口：`/api/health` 不再有
`dir_picker`、`/api/dialog/directory` 返回 404、点菜单直接出表单且**没有「浏览…」按钮**；空路径和
相对路径在表单里就地报错（且不产生目录、不发请求）；完整路径能添加并切换（标签页标题跟着变）；
目录不存在时给出创建提示并在确认后真的建目录；路径是个文件时报服务端原因且对话框不关；服务端
自己也拒绝相对路径（`path must be a full path`）。

`tests/smoke_turn_resume.mjs` 在 HTTP 层钉住「轮次比连接活得久」：客户端读到第 8 帧就断开，
服务端那一轮和它对 provider 的请求都继续（事件序号还在涨）；`GET /api/chat/turn` 报
`running:true` 与完整回放窗口；`POST /api/chat/attach` 从 `seq 1` 连续回放到 `done`，最终答案
一字不少地落盘；`since=` 只回放尾巴；轮次结束后 attach 返回 404；`/api/chat/stop` 对没人看的
轮次照样生效（并真的掐断上游请求）。

`tests/ui_stream_resume.mjs` 在真浏览器里走同一条路：发消息 → 导航到 about:blank（等价于关掉
标签页）→ 确认服务端还在跑 → 导航回来，断言页面重新进入生成态、live 气泡追上了期间生成的内容、
**没有重复渲染**，然后跟着直播到结束。

`tests/ui_second_client.mjs` 换一台「机器」：服务端注册两个工作区，一个纯 HTTP 客户端在
**第二个**工作区里发起轮次、读到第 9 帧就断开，然后用一个全新的浏览器 profile（没有
localStorage）打开页面——它必须落在 `proj_b`（而不是索引里的第一个 `proj_a`）、直接进入生成态、
回放已经生成的内容、继续直播到结束，标签页标题也跟着工作区走。

`tests/smoke_todo.py` 覆盖任务列表：`set` 写入整份计划、`list`/`add`/`update`/`clear` 各自生效；
列表出现在**下一轮**的 system prompt 里（压缩或重启后仍然在）；每次变更都发一个 `todo` 事件并
带 `items`/`total`/`done`；状态落盘在 `session.json`，索引里的 `todo_total`/`todo_done` 不用打开
会话就能读；把另一条置为 `active` 会把原来那条降回 `pending`（永远只有一条进行中）；全部完成时
工具结果明说「All steps are done」；两条 active / 越界下标 / 未知 op 都被拒绝且不改动现有列表；
`PUT /api/sessions/{id}/todos` 能整体替换也能勾选一条，非法状态 400、未知下标 404；进程重启后
列表还在，并且仍然注入提示词；导出的 HTML 与其中的 JSON 都带列表，导入后进度一并恢复。

`tests/smoke_artifacts.py` 覆盖产出文件：写的文档、命令生成的 SVG 都进 `artifacts`（带种类、
大小、turn、产出工具），`.o` 之类的构建噪声与 `.tea_agent_run/` 下的内部副本都不收录；
同一路径改写两次只出现一次；进程重启后仍在；`/download` 返回 `attachment` 与逐字节相同的内容，
`&inline=1` 变成 `inline`，17 MB 的文件 `/raw` 拒绝（413）而 `/download` 正常发完；越过工作区
的路径被拒、缺文件 404、缺参数 400；导出的 HTML 里有静态清单且不含死链接。

`tests/ui_artifacts.mjs` 在真浏览器里看「本轮产出」：它贴在**最后一条助手消息**下面、只列
本轮的若干个文件（上一轮的 `old.csv` 不出现）、每行有种类与大小、下载链接指向 `/download` 并
带 `download` 属性、文档没有「查看」而图片/视频有；点链接真的拿到文件（状态 200、
`Content-Disposition: attachment`、内容与磁盘一致）；一轮运行中产出的图片在结束后自动出现且
可下载；没有产出的那一轮不显示这块。

`tests/ui_todo.mjs` 在真浏览器里看这块面板：有任务列表的会话显示面板、`1/4`、进度条 25%、三态
标记与 `aria-checked`；侧栏会话行同步显示「1/4」；点第三条即勾选完成且**写进 session.json**，
刷新后仍是 `2/4` 且样式为已完成；点标题行折叠/展开；一轮运行中 agent 改列表时面板**实时**更新
（`todo` 事件）；没有列表的会话整个面板隐藏；新建草稿也会清掉。

`tests/smoke_export_html.py` 把导出当成「要离开这台机器」的文件来验：HTML 完整、CSS 内联、
没有任何 `<link>` 或外部 `src`；Markdown 各元素都渲染出来；模型回答里的 `<script>` 被转义、
`javascript:` 链接不会变成活链接；图片内联后的字节与磁盘逐字节相同；工具输出按 8 KB 截断
并注明；末尾的 JSON 能解析且条数一致；带着 `</script>` 的消息不会截断那个区块；把这份 HTML
POST 到**另一个工作区**能新建会话（文本一致、图片写进新工作区并可按 `/raw` 取回），再导一次
是第二个会话而不是覆盖，纯 JSON 也能导，乱发的 HTML / 空 payload 返回 400 且不产生会话。

`tests/ui_session_export.mjs` 在真实浏览器里走完整链路：顶栏导出按钮在打开会话时出现（草稿时
隐藏）、点击后拿到 `text/html` blob 且文件名是 `.html`、内容里有会话文本/内联样式/`data:` 图片、
不含本机地址；随后把文件写到磁盘并用 **`file://`** 打开——这正是「拷到别的机器上双击」的场景，
断言样式生效、消息气泡与图片都在（`naturalWidth > 0`）、没有任何网络请求；最后验证导入按钮
弹出 `.html` 文件选择器，导入后侧栏多出一个会话。

`tests/smoke_run_archive.py` 把归档当**真 zip** 验：`zipfile.testzip()` 通过、条目名全部以
`.tea_agent_run/` 开头（那个点必须保住）、空目录也在、`note.txt` 解出来与磁盘逐字节相同且确实被
压缩过、`X-Archive-Entries` 与包内条目数一致；`?path=` 指子目录只打那一棵；`../..` 返回 403、
不存在返回 404、指到文件返回 400 `not_a_directory`、未知工作区 404；放一个 33 MB 的文件进去，
它被跳过（`X-Archive-Skipped: 1`）而其余内容照常打包。

`tests/ui_files_download.mjs` 在真浏览器里点那个按钮：按钮存在、文案与 tooltip 说清下载什么、
点击后立刻进入「打包中…」并禁用；用 `URL.createObjectURL` 的替身接住 blob，断言它是
`application/zip`、magic 是 `PK\x03\x04`、大小合理；结束后按钮恢复、toast 报出文件名与条目数；
最后再直接打一次接口，确认包里真的有这个工作区的标记文件。

`tests/ui_two_clients.mjs` 开两个独立 profile 的浏览器看同一个主题：A 发起一轮后拿到 `?s=` 链接，
B **用这条链接**打开并接上（内容长度始终接近，B 的地址栏停在同一个主题）；B 插队一条消息，
**两边**都看到它被采纳成第二个问题；B 点「离开」后自己不再跟随、侧栏标出「· 进行中」、
可以自由开新会话，而服务端和 A 都不受影响；B 点会话又能接回同一个 `?s=`；最后 A 点「停止」，
两边同时结束。

`tests/ui_second_client.mjs` 反过来钉"不该发生的事"：proj_b 里有一轮在跑，另一台机器的全新
profile 打开服务器地址时**仍然落在 proj_a**（不再被"谁在跑就跳谁"拽走），标题也是 proj_a；
切到 proj_b 后侧栏把那个会话标成「· 进行中」，点它才 attach 并回放；重新打开 `?s=` 直接回到
这个主题；清空 localStorage + sessionStorage 再来一次，仍然落在 proj_a。

`tests/ui_topic_url.mjs` 用同一个浏览器 profile 开两个标签页（`?s=A` / `?s=B`）：两边同时流式、
各自只看到自己的问题与回答，两个会话文件互不串味；刷新 A 仍回到 A 并保有完整对话；「复制链接」
复制的是 `…?s=A`；「新会话」只清掉本标签页的 `?s=`，另一个标签页不受影响；再开第三个标签页
也不会动前两个。

`tests/ui_stream_guard.mjs` 起一轮慢速流式回答，在生成过程中点侧边栏：工作区按钮仍可点开菜单，
**切换工作区是允许的**——这一页自动「离开」那一轮（服务端照跑），「添加工作区」正常弹对话框，
「新建会话」只清掉本页的主题（草稿），点回那个会话又能接上；最后「停止」把它结束。

`tests/ui_queue.mjs` 在流式回答期间用真实的 Enter 按键入队：排队条按顺序出现、输入框清空、
没有产生第二个会话；点 × 取消第一条；随后被采纳的一条从排队条消失并变成对话里的 user 气泡，
本轮在同一 SSE 内继续（恰好两次模型请求），结束后排队条隐藏、输入框恢复「发送」。

`tests/ui_sidebar.mjs` 覆盖侧栏收起：点隐藏按钮后侧栏宽度归零、顶栏出现展开按钮、正文栏
变宽（780 → 1040）；刷新后仍是收起状态；再点展开（或 `Ctrl+B`）后侧栏与列宽复原，选择被
写进 localStorage。同一个脚本还断言标签页标题带上了当前工作区。

`tests/ui_boot_probe.mjs` 不需要浏览器：它把真实的 `web/index.html` 解析成一个最小 DOM，然后
在 Node 里执行真实的 `web/app.js`、按浏览器的方式触发 `DOMContentLoaded`，喂给它与服务端同形的
JSON。任何同步异常或未处理的 Promise rejection 都算失败，状态灯没走到「已连接」也算失败——
`e2e.ps1` 只检查 HTML/JS 里**有没有**某些字符串，`init()` 里抛错时页面会一直停在
`index.html` 里写死的「连接中…」，那种静默故障只有真执行一遍才能发现。它支持
`--base <url>`（跑服务端实际发出来的页面）、`--state noworkspace|setup|…`、`--all` 等变体。

`tests/smoke_config_quickstart.py` 覆盖首次运行的快捷通道：没有配置时 `/api/health` 报
`config_loaded=false`；只传 `api_key` 就写出 `provider.yaml`（DeepSeek 单个 provider，
`models:` 里 `deepseek-flash` 排第一，于是它就是默认 —— 文件里不写 `default_model:` 也不用
`roles:`），进程无需重启即可用；key 不出现在
任何响应里；POSIX 下文件权限是 `0600`；带引号/反斜杠的 key 以单引号标量落盘且仍能解析；缺 key、
空白 key、控制字符、未知 provider 一律 400 且不写文件；再调一次是替换而不是一次性。
`provider: minimax` 走同一条路：写出 minimax 单 provider 的配置，默认模型 `MiniMax-M3.1-Flash`、
1M 上下文，并打开额度徽章（`supports_usage` + `minimax_token_plan`）；显式传的 `api_url`
仍然优先于默认端点。

`tests/ui_setup_quickstart.mjs` 在浏览器里走这条快捷通道：首次运行对话框里有 provider 下拉
（DeepSeek / MiniMax）、`password` 类型的 key 输入框和「用 key 创建配置」按钮，光标已在输入框里，
完整上传路径也还在；切到 MiniMax 时提示文字和 key 占位符一起变（`sk-cp-…`）；
空 key 就地报错且不写文件；粘贴 key 按回车后 `provider.yaml` 出现、页面自动刷新、配置对话框
消失、状态栏变「已连接」、模型按钮显示 `deepseek-flash`。

`tests/ui_setup_flow.mjs` 起一个**没有** `provider.yaml` 的实例，再用 headless Chrome 通过
CDP 操作真实页面：断言弹出配置对话框、Esc/点遮罩关不掉、坏 YAML 报错且不落盘、文件选择器
能填入内容、上传成功后页面自动重载且状态栏变回「已连接」。

`tests/e2e.ps1` 起一个假 OpenAI 服务（`tools/mock_llm_server.mjs`，按 4 步脚本驱动工具调用），
再以独立 `--tea-home` 启动 agent，覆盖 **155 项断言**：健康检查、key 脱敏、工具注册、工作区索引、
SSE 事件序列与 `metrics` 指标块、两个工具的落盘副作用、`.tea_agent_run/` 全部子目录、会话持久化、
会话分叉（复制到选中轮次、血缘落盘、父会话不变、非法下标/未知会话报错）、目录树与文件读写、
记忆与日志接口、内置 UI 交付（含指标栏）且无 CDN 依赖。全程本地 `http://`，不需要 api_key。

其中几条是**回归测试**：

1. **思考回传**：mock 像真实 thinking 网关那样，一旦发现被重放的 assistant 消息丢了
   `reasoning_content` 就回 400。用 0.1.0 的二进制跑会挂 7 项。
2. **上下文压缩**：fixture 里有一个 `max_context_tokens: 1500` 的 `mock_small` provider，
   `e2e.ps1` 用它 + 复用同一会话 + 工作区级 `settings.yaml` 强制触发压缩，断言 SSE 出现
   `compaction` 事件、会话落盘了 `summary` / `summary_upto`、且原始消息没有被删除
   （压缩是「不再发送」而不是「删除」）。
3. **流正确收尾**：`tests/sse_probe.mjs` 用 Node 的 fetch 当严格客户端读 SSE。curl 容忍
   未终止的 chunked 响应，浏览器/Node 不容忍（`error: Error in input stream` /
   `TypeError: terminated`），所以必须显式断言。用 0.1.1 的二进制跑，44 项里恰好挂这 1 项。
4. **指标块**：mock 只在 `stream_options.include_usage` 打开时才回传 usage（真实网关就是这个
   行为）。默认那一轮因此必须回落成本地估算并置 `estimated: true`；打开 include_usage 后
   `context_tokens` 必须等于 provider 报的 prompt tokens；再换用忽略该选项的 `mock_nousage`
   provider，必须又回到估算，而不是 0 或空。

脚本开跑前会检查端口占用，发现残留监听就直接报错退出——否则会静默连到上一次遗留的实例上，
表现为「卡住」而不是失败。

---


## 当前的验证状态

诚实标注，避免误导：

| 项 | 状态 |
| --- | --- |
| Windows x64 + MinGW-w64 GCC 16.2 | ✅ 构建 0 warning，E2E 155/155 |
| Windows x64 + MSVC | ✅ 构建 0 error，E2E 155/155 |
| C++14 语言模式（GCC 6.5 兼容性） | ✅ 全量编译通过 |
| 真实 `api.deepseek.com` / `opencode.ai` | ✅ 含多轮工具调用 |
| 真实 `www.minimax.cn` 额度接口 | ✅ M Plan 订阅 Key 实查：5 小时窗口与周窗口的剩余百分比、重置时间都对得上 |
| 交叉编译宿主工具机制（`TEA_AGENT_USE_HOST_ASSET_TOOL=ON`） | ✅ 在本机以原生方式验证该路径 |
| **Linux 构建与运行** | ✅ 在 wsl2, ubuntu20.04 等环境测试 |
| **macOS 构建与运行** | ✅ 在 Mac OS X 10.6 Snow Leopard 测试通过 |
| **真实 ARM 交叉编译产物** | ✅ aarch64-linux-gnu-gcc (Linaro GCC 6.5-2018.12) 6.5.0 编译，到 horizon x3 执行正常 GLIBC 2.28 |
| Linux→Windows MinGW-w64 交叉构建（`win64-mingw-cross` preset） | ✅ 产出 PE32+ `tea_agent.exe` 且内嵌 CA；运行需在 Windows 上验证 |
| **ARM64 设备实跑**（x3sdbx3, kernel 4.14.74, glibc 2.28） | ✅ 全静态 11.3 产物：启动不崩，`/`、`/app.css`、`/app.js`、`/api/health`、`/api/workspaces` 全 200，`/favicon.ico` 204，驻留 15s+ |

Linux / macOS 首次构建若报错，最可能集中在 `src/command.cpp`（`fork`/`setsid`/进程组）与
`src/platform_fs.cpp`（POSIX `stat`/`dirent`），欢迎提 issue 附报错。

---

## License

本项目代码采用 **MIT** 许可证，见根目录 [`LICENSE`](LICENSE)。

Copyright (c) 2026 sunkwei

`3rd/` 下的依赖各自遵循其原有许可证，**不被 MIT 覆盖**，详见各目录内的 `LICENSE` 文件：

| 依赖 | 许可证 |
| --- | --- |
| nlohmann/json | MIT |
| cpp-httplib | MIT |
| yaml-cpp | MIT |
| mbedTLS | Apache-2.0（双许可，另有 GPL-2.0） |
| miniz | MIT |

分发二进制时注意：mbedTLS 的 Apache-2.0 条款要求保留其许可证与声明，`3rd/mbedtls/LICENSE`
需随发行物一起带上。

**许可证兼容性**（新增依赖时先看这一段）：本项目是 MIT，`3rd/` 里 nlohmann/json、cpp-httplib、
yaml-cpp、miniz 也都是 MIT，互相兼容、可直接静态链接进同一个二进制，只需保留各自的版权声明。
两处需要留意，但都**不构成冲突**：

- **mbedTLS** 是 Apache-2.0 / GPL-2.0 双许可。这里按 **Apache-2.0** 使用：Apache-2.0 与 MIT
  单向兼容（MIT 代码可以并入 Apache-2.0 分发），只要保留 `3rd/mbedtls/LICENSE` 与声明即可；
  **不要**选 GPL-2.0 那一支——GPL-2.0 与 MIT 组合会把整个发行物变成 GPL，那才是真冲突。
- **CA 证书包**（`3rd/ca-bundle/cacert.pem`）是 MPL-2.0。它是**数据**而不是链接进程序的代码，
  随发行物附带并保留出处即可，不影响二进制的 MIT 许可。

也就是说：新增依赖时优先选 MIT / BSD / zlib / Apache-2.0；**GPL / AGPL 一律不要**（会传染整个
二进制），LGPL 需要能保证重新链接（静态链接下要提供目标文件），也不建议。
