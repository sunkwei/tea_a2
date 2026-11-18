# modules/：现成的扩展模块

这里放**可以直接用**的扩展模块（不是「例子」——例子在 `../examples/`，那三个是桌面
三件套，GUI 测试要用）。

| 文件 | 工具 | 干什么 |
| --- | --- | --- |
| `toolkit_plan.py` | `toolkit_plan` | **任务编排**：把一个大任务拆成并行子任务（只读的并行，要改东西的独占），跑完由模型汇总。见 [multi_agent.md](../../../docs/multi_agent.md) |

## 装法

```bash
cp sched/a2_ext_py/modules/toolkit_plan.py ~/.tea_agent/ext/
```

```yaml
# ~/.tea_agent/mcp.yaml
servers:
  a2ext:
    command: "python3"
    args: ["/path/to/tea_a2/sched/a2_ext_py/toolkit/mcp_server.py",
           "--modules", "/home/you/.tea_agent/ext",
           "--agent-url", "http://127.0.0.1:8282"]
    env:
      TEA_AGENT_URL: "http://127.0.0.1:8282"   # 模块要自己找 agent
      TEA_HOME: "/home/you/.tea_agent"          # 计划落在 <TEA_HOME>/plans
```

模块靠 `sched/planning.py`（规划内核：模型分解 + 模型汇总 + 校验）。它从 MCP 服务器
自己的路径推出源码目录；也可以用 `TEA_A2_SCHED` 显式指过去。

只用标准库，不用 pip 装东西。
