"""a2 的 Python 扩展层。

三层，各管各的：

| 层 | 是什么 | 在哪 |
| --- | --- | --- |
| `a2` | **内核**：执行、沙箱、轨迹、预算。它自己就能用，谁也不依赖 | C++，`src/` |
| `a2_task` | **策略层**：什么时候干、干哪件、拆成几步、要不要问人 | `sched/task_server.py` |
| `a2_ext_py` | **这一层**：Python 侧的客户端 + 扩展的宿主 | `sched/a2_ext_py/` |

为什么要有这一层，两条理由：

1. **客户端只有一份。** a2 的接口以前有三份拷贝（`tea_api.Tea`、
   `channel.Agent`、`task_server.Agent`），接口一变要改三处——两套语义
   必然漂移（截断规则、沙箱、轨迹记录全对不上）。现在只有 `Client` 一份。
2. **机制层要能长出新能力，而机制层是 C++。** 加一个工具要改代码、三平台
   交叉编译、发版——「机制层只执行不生长」。这一层就是那条缝：模块用
   Python 写，加载之后变成 a2 的工具（走 MCP，a2 那边一行 C++ 都不用改）。

**模块不住在这一层里**：用户自己写的模块放 `~/.tea_agent/ext/`（可用
`--ext-dir` 改），发版升级不会覆盖它们；这一层只放框架（客户端、加载器、
MCP 服务端）。
"""

from .errors import ApiError
from .client import Client, DEFAULT_BASE, DEFAULT_AGENT
from .queue import Queue, DEFAULT_QUEUE_BASE

__all__ = [
    "ApiError",
    "Client",
    "Queue",
    "DEFAULT_BASE",
    "DEFAULT_AGENT",
    "DEFAULT_QUEUE_BASE",
]
