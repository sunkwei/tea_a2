# MCP：把别的程序接进来当工具

MCP（Model Context Protocol）让**别的程序**变成 agent 的工具。`tea_a2` 是 MCP 的**客户端**：
它在**和 provider.yaml 同目录**的 `mcp.yaml` 里读到一组服务器，**按需把它们启动起来**，
用 stdio 上的 JSON-RPC 跟它们说话，把它们提供的工具并进模型看到的工具表。

```
mcp.yaml (servers:)          ← 和 provider.yaml 同目录
        │
        ▼
   tea_a2 ──启动──▶ uiautomation-mcp（独立的程序）
        │  ◀─JSON-RPC over stdio── initialize / tools/list / tools/call
        ▼
   模型看到的工具：mcp__uiautomation__SearchElements、mcp__uiautomation__TakeScreenshot …
```

只实现了 **stdio** 这一种传输：这也是真正有用的那些服务器用的方式（不需要端口、不需要
令牌、不需要网络）。HTTP / SSE 传输没有做。

## 配置

配置在**和 provider.yaml 同目录**的 `mcp.yaml` 里（不是 provider.yaml——那边装的是 API key，
这边装的是「给模型装新手」的程序）：

```yaml
# $HOME/.tea_agent/mcp.yaml
servers:
  uiautomation:
    command: uiautomation-mcp
    args: []
```

仓库里有一份带注释的模板：`mcp.yaml.example`。

| 字段 | 意思 | 默认 |
| --- | --- | --- |
| `command:` | 要跑的程序（不带斜杠时按 PATH 找） | 必填 |
| `args:` | 参数（列表，或者一个按空格切的字符串） | 空 |
| `env:` | 给子进程加的环境变量 | 空 |
| `cwd:` | 子进程的起始目录 | agent 自己的目录 |
| `enabled:` | `false` 保留这条但不启动 | `true` |
| `timeout_ms:` | 一次工具调用的上限 | 60000 |
| `startup_timeout_ms:` | 握手（启动 + initialize）的上限 | 20000 |
| `protocol_version:` | 要的 MCP 协议版本 | 2024-11-05 |

工具的公开名字是 `mcp__<服务器>__<工具>`。函数名有 64 个字符的上限，所以名字太长时会被
截断并加一段哈希（仍然唯一）。

## 和 uiautomation-mcp 一起用（Windows）

[uiautomation-mcp](https://github.com/locomorange/uiautomation-mcp) 是一个 Windows 上的
UI Automation 服务器：它能找控件、点按钮、往输入框里打字、截屏、列窗口——也就是**让模型
真的去操作这台机器上的窗口**。它用 .NET 9 写的，只在 Windows 上跑。

### 1. 装服务器

```powershell
winget install Locomorange.UIAutomationMCP      # 或者从 release 下 zip
```

装完一般在 `C:\Program Files\...` 或者 `%LOCALAPPDATA%\Microsoft\WinGet\Packages\...`，
先确认它在哪、能不能直接跑：

```powershell
& "C:\Tools\uiautomation-mcp\uiautomation-mcp.exe"    # 它在等 stdio 上的 JSON-RPC，
                                                       # 直接跑会「没反应」——这是正常的
```

### 2. 写配置

```yaml
servers:
  uiautomation:
    command: 'C:\\Tools\\uiautomation-mcp\\uiautomation-mcp.exe'
    timeout_ms: 60000        # 截屏、启动应用这类操作要给够时间
```

（YAML 里的 Windows 路径**用单引号**：双引号里的 `\\` 是 YAML 的转义，写成单引号最省心。）

### 3. 起 agent，看状态

```powershell
tea_agent.exe --config $env:USERPROFILE\.tea_agent\provider.yaml --port 8282
```

另开一个窗口：

```powershell
pwsh -File scripts\mcp-check.ps1
```

输出应该长这样：

```
[OK  ] uiautomation  (31 tool(s))
       command : C:\Tools\uiautomation-mcp\uiautomation-mcp.exe
       protocol: 2024-11-05   server: UIAutomationMCP 0.1.0
       tools  : SearchElements, GetElementTree, TakeScreenshot, LaunchApplication, InvokeElement …
```

### 4. 不带模型，先调一个工具

```powershell
pwsh -File scripts\mcp-check.ps1 -Tool mcp__uiautomation__SearchElements `
      -Arguments '{"name":"OK"}'
```

这一步很重要：它把「服务器本身能不能用」和「模型会不会用」分开。

### 5. 让模型去用

在 Web UI 里问它：

> 列出当前所有窗口的标题

或者：

> 在记事本里输入 "hello"，然后保存

模型会看到 `mcp__uiautomation__SearchElements` 这些工具并自己调用。

## 几个要注意的地方

- **UI Automation 需要一个交互式的桌面会话**。把 agent 当成 Windows 服务（session 0）跑，
  它看不到用户桌面上的窗口；用同一个用户、在同一个会话里启动它才行。
- **服务器是按需启动的**：第一次用到它的工具时才拉起来。启动横幅不等待它，
  所以一个连不上的服务器不会拖慢启动。
- **改完 `mcp.yaml` 点设置页的「刷新」**：那个按钮会重新读一遍文件再重连。
  文件写坏了会被拒绝（400），正在跑的服务器不受影响。
- **连不上的服务器会被写明原因**（命令找不到、握手超时、回答不是 JSON……），
  在设置页和 `GET /api/mcp/servers` 里都能看到；别的服务器照常工作。
- **一个连不上的服务器一分钟内不会被反复重试**，不然每一轮都要等它。
- **服务器返回的图片**会存进工作区并附在对话里（跟 `toolkit_load_image` 一样），
  所以模型得声明 `supports_vision: true` 才看得到。
- **服务器发给客户端的请求**（sampling、roots）一律回「方法未实现」——不回的话服务器会
  一直等一个不会来的答案。

## 接口

| 接口 | 干什么 |
| --- | --- |
| `GET /api/mcp/servers` | 每个服务器的状态（连上了没有、协议版本、它自称是谁）、它提供的工具、连不上时的原因 |
| `POST /api/mcp/refresh` | 把所有服务器停掉再重新启动一遍 |
| `POST /api/mcp/servers/{name}/call` | 不用模型，直接调一个工具 |

```bash
curl -s http://127.0.0.1:8282/api/mcp/servers
curl -s -X POST http://127.0.0.1:8282/api/mcp/refresh
curl -s -X POST http://127.0.0.1:8282/api/mcp/servers/uiautomation/call \
     -H 'Content-Type: application/json' \
     -d '{"tool":"mcp__uiautomation__SearchElements","arguments":{"name":"OK"}}'
```

## 自己写一个 MCP 服务器

协议就是 JSON-RPC 2.0 over stdio，四步：

```
-> {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"tea_agent","version":"0.16.0"}}}
<- {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"my-server","version":"1.0"}}}
-> {"jsonrpc":"2.0","method":"notifications/initialized"}
-> {"jsonrpc":"2.0","id":2,"method":"tools/list"}
<- {"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"echo","description":"…","inputSchema":{"type":"object","properties":{"text":{"type":"string"}}}}]}}
-> {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"echo","arguments":{"text":"hi"}}}
<- {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"hi"}]}}
```

每条消息一行 JSON。仓库里有一个现成的例子：`tests/mock_mcp_server.py`（几十行，
假的三件套工具：echo / add / picture）。

`tools/call` 的回答里 `content` 是一组内容块：`{"type":"text","text":"…"}`、
`{"type":"image","data":"<base64>","mimeType":"image/png"}`、
`{"type":"resource","resource":{"text":"…"}}`。`isError: true` 表示工具自己报错了
（内容照样回给模型）。
