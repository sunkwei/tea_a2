# a2_ext_py：a2 的扩展层（机制层可以长出新的能力）

三层，各管各的：

| 层 | 是什么 | 在哪 |
| --- | --- | --- |
| `a2` | **内核**：执行、沙箱、轨迹、预算。它自己就能用，谁也不依赖 | C++，`src/` |
| `a2_task` | **策略层**：什么时候干、干哪件、拆成几步、要不要问人 | `sched/task_server.py` |
| `a2_ext_py` | **扩展层**：Python 侧的客户端 + 扩展的宿主 | `sched/a2_ext_py/` |

这一层有两件事：

1. **客户端只有一份。** a2 的接口以前有三份拷贝（`tea_api.Tea`、`channel.Agent`、
   `task_server.Agent`），接口一变要改三处——两套语义必然漂移（截断规则、沙箱、
   轨迹记录全对不上）。现在只有 `Client` 一份（`sched/a2_ext_py/client.py`），
   队列客户端是 `Queue`（`sched/a2_ext_py/queue.py`）。
2. **机制层能长出新能力，而机制层是 C++。** 加一个工具本来要改代码、三平台交叉编译、
   发版。这一层就是那条缝：**模块用 Python 写，加载之后变成 a2 的工具**，
   **a2 那边一行 C++ 都不用改**。

## 怎么用

### 一、写一个模块

模块是**一个文件**，住在**你自己的目录**里（默认 `~/.tea_agent/ext/`，可用
`--modules` 或环境变量 `TEA_EXT_DIR` 改）：

```python
# ~/.tea_agent/ext/toolkit_hello.py
def toolkit_hello(who="world"):
    """Say hello."""
    return "hello, " + who


def meta_toolkit_hello():
    return {"name": "toolkit_hello",
            "description": "Say hello to somebody.",
            "inputSchema": {"type": "object",
                            "properties": {"who": {"type": "string"}},
                            "required": ["who"]}}
```

两条约定，没有别的：

* 文件名是工具名（`toolkit_<名字>.py`）；
* 文件里有两个东西：函数 `toolkit_<名字>()`，和它的说明 `meta_toolkit_<名字>()`。

**用户模块不在发版包里**（发版会覆盖它），也**不在工作区里**（模型的
`toolkit_file` 够不着它——工作区的围栏到工作区为止）。

### 二、接上 a2

这一层本身就是一个 **MCP 服务器**（stdio），a2 会把它当普通 MCP 服务器启动：

```yaml
# ~/.tea_agent/mcp.yaml
servers:
  a2ext:
    command: "python3"
    args: ["/path/to/tea_a2/sched/a2_ext_py/toolkit/mcp_server.py",
           "--modules", "/home/you/.tea_agent/ext",
           "--agent-url", "http://127.0.0.1:8282"]
```

然后 `POST /api/mcp/refresh`（网页上的「刷新」也是它），新工具就进模型的手里了。

模型看到的是 `mcp__a2ext__toolkit_hello`——MCP 的公开名字带服务器前缀。

### 三、模型自己写工具（自举）

服务器给模型四个元工具：

| 工具 | 做什么 |
| --- | --- |
| `toolkit_save` | 存一个工具（名字、说明、代码） |
| `toolkit_reload` | 重新加载所有模块 |
| `toolkit_list_versions` | 看一个工具有哪些旧版本 |
| `toolkit_rollback` | 回滚到某个旧版本 |

模型写一个工具、reload、**同一轮里就能用它**——因为 a2 在每一轮迭代之前会问一遍
MCP 服务器「你现在能做什么」（`McpRegistry::refresh_tools()`，见 `src/mcp.cpp`），
服务器不需要重启。

保存之前会检查（`sched/a2_ext_py/toolkit/store.py`）：

* 名字必须以 `toolkit_` 开头，是个正经的标识符；
* 说明得是说明（名字要和工具名一致，schema 得是 schema）；
* 代码**能解析**（`ast.parse`）——语法错在这一步就报出来，不是等到下次加载；
* 代码里得真有那个函数；
* 缺的第三方依赖会报出来（**不会偷偷装**）。

每一次保存都留档：旧版本写成 `<名字>.v<旧版本>.bak.py`，`toolkit_rollback`
能退回去；每一次保存都写一行审计（`~/.tea_agent/ext-audit.jsonl`，含代码本身）。

**加载失败的模块要报出来，不是悄悄丢掉**——「我配了，怎么没反应」是最坏的答案。

## 边界（说清楚）

- **扩展代码不在 a2 的进程里**（它是 a2 起的子进程）：一个写坏的模块带倒的是扩展服务器，
  不是 agent；而且它在 a2 的围栏之外，**别在模块里乱写文件**（用 `Client` 去调 a2 的
  文件接口，围栏才管得住）。
- **模型能写自己的工具，等于模型能写以后要执行的代码。这个风险是被接受的**
  （2026-11-04 决定：都是成年人了，为自己的决定负责）。所以**不**做「要人批」的
  闸门（机制层的 M3 因此只剩「策略层下发的权限」那部分）。
  留下的是四道便宜的防线：

  | 防线 | 在哪 |
  | --- | --- |
  | 模块目录**不在任何工作区里**（模型的 `toolkit_file` 够不着；启动时会检查并警告） | `mcp_server.py` 的 `check_module_dir()` |
  | 保存前检查（名字、schema、`ast.parse`、函数在不在、缺依赖报出来） | `store.py` 的 `save_tool()` |
  | 每次保存有审计（**含代码本身**） | `~/.tea_agent/ext-audit.jsonl` |
  | **能一键关掉模型自存** | `--no-save` / `TEA_EXT_SAVE=0` |

  第四道是应急用的：`--no-save` 起来之后模型就存不了工具（人自己放进模块目录的
  模块照旧能用）。出事的时候不用改代码、不用发版。
- **工具在 a2 的 loop 里执行**，所以这一轮的预算、轨迹、截断、停止都是 a2 的，
  没有例外。

## 什么时候该合并回 a2（C++）

留在 Python 层的判据很简单——**需要下面这些的，就该进 C++**：

* 需要 a2 的内部状态（会话、工作区、记忆）；
* 需要原子写、工作区围栏这类**只有内核能给的保证**；
* 需要进程内的性能。

其余的留在这一层：一个新能力从「写一个 .py」到「能用」是**一分钟**，进 C++ 是
「改代码 + 三平台交叉编译 + 发版」。**这一层越厚，a2 越不动**——所以合并的判据要
定期看一眼，别让 Python 层长成第二个内核。

## 客户端怎么用

```python
from a2_ext_py import Client, Queue

client = Client("http://127.0.0.1:8282")
quota = client.usage("minimax")        # provider 的额度
limits = client.limits()               # 这一轮的预算（机制层的上限）
workspaces = client.workspaces()

queue = Queue("http://127.0.0.1:8283")
task = queue.create_task(ws, "do the thing")
for event in queue.events():
    if event["type"] == "task_done":
        notify(event["task"])
```

旧的 import 名还认（`from tea_api import Tea, Queue`），但那是过时的写法。

## 现成的例子

`sched/a2_ext_py/examples/` 里有两个可以直接用的模块（Windows 上的 GUI 测试常用）：

| 文件 | 工具 | 干什么 |
| --- | --- | --- |
| `toolkit_screenshot.py` | `toolkit_screenshot` | 抓屏存成 PNG（工作区里），配合 `toolkit_load_image` 给模型看 |
| `toolkit_mouse.py` | `toolkit_mouse` | 鼠标：移动/单击/双击/右键/拖拽/滚轮 |

只用标准库（Win32 走 ctypes，PNG 容器自己写），不用 pip 装东西。装法见那个目录里的
README。

两个坑：

- **模块可以自己 import**：加载器用同一个字典当 globals 和 locals，模块级的
  `import os` 在函数和类体里都看得见（早先用两个字典，模块级 import 落在 locals，
  函数和类体都看不见——「模块可以自己 import 需要的东西」其实是坏的）。
- **`mcp.yaml` 里的 Windows 路径要用单引号**：YAML 的双引号标量会处理反斜杠转义，
  `command: "C:\Users\..."` 里的 `\U` 不是合法转义 → yaml-cpp 拒绝整个文件 → MCP
  配置被**静默丢掉**（表现是「文件不存在、服务器一个都没起来」，日志里一行都没有）。

---

← 回到 [README](../README.md) · MCP 见 [MCP](mcp.md) · 队列见 [任务队列](tasks.md) ·
机制层与策略层的评估见 [机制层与策略层](mechanism-strategy.md)
