# 配置

> 这一页讲**配置文件的字段**。要直接敲命令，看
> [使用手册](manual.md)。

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

search:                        # 可选：独立的搜索 provider（见下「独立的 search provider」）
  provider: DeepSeek           # providers: 里的一个名字：web 工具发给它、用它的 key
  # always: true               # 强制所有 web 调用都走它（默认是「回退」）

settings:
  max_iterations: 0            # 单条消息最多几轮工具调用；0 = 不限（跑到模型自己收尾）
  max_tool_output: 131072      # 回填给模型的工具输出上限（字节）
  command_timeout_ms: 180000   # toolkit_exec 默认超时
  keep_turns: 10
  http_engine: auto            # auto | socket | winhttp（也可用 --http-engine 覆盖）
  peers:                       # 可选：别的 tea_a2（级联），见 [peer.md](peer.md)
    study-pc:
      url: http://172.16.1.31:8383
      token: <对端的 --api-token>   # 令牌只存在这里，不进提示词
      # workspace: ws_9f2c          # 可选：对端上用哪个工作区
      # description: 书房的电脑       # 可选：模型会看到这句
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

### 查模型并回填（设置页的「更新模型」）

`models:` 块是手写的：provider 上了新模型、模型改了上下文窗口，都得人去改文件。设置页每个
provider 卡片上的「**更新模型**」按钮做这件事——服务端拿 provider 自己的 key 去问
`GET {api_url}/v1/models`（跟聊天接口同一个地址推导规则），把每个模型的默认配置列出来，
确认后写回这个 provider 的 `models:` 块。

- **数字从哪来**：上游说的（`context_length` / `max_completion_tokens` /
  `supported_parameters` / `modalities` …）→ provider.yaml 里本来就有的 → 内置表
  （`deepseek-chat`、`gpt-4o` 这类常见模型）→ 按名字猜（`-reasoner` 会思考、`-vl` 能看图）。
  界面上每个模型都标了来源。
- **只改一个块**：别的 provider、注释、`settings:` 块原样保留——provider.yaml 是给人看的
  文件，不能因为点了一次按钮就重排它。写完先校验再替换，配置随即热加载生效。
- **不越权**：采样参数（`temperature` / `top_p`）和 `reasoning_effort` 是用户自己调过的，
  一次列表更新不会抹掉它们。
- **顺序是开关**：`models:` 里的第一个模型就是该 provider 的默认模型，所以新模型排在后面。

上游没有这个接口时（404、不是 JSON、空列表），界面照实说查询失败——不会静默地把列表清空。
接口细节见 [HTTP 接口](api.md#查模型问-provider-有哪些模型回填-provideryaml)。

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

### 独立的 search provider（`search:` 段）

上面那种写法把「对话用哪个 provider」和「搜索用哪个 provider」绑死了。可这两件事通常
**不是同一个账号**：对话可能跑在本地模型上（根本没有联网能力），而搜索是要花钱的
（DeepSeek 按成功请求计费）。`search:` 段就是专门拆开这两件事的：

```yaml
providers:
  local:                       # 对话跑这里，没有任何 web 能力
    api_url: http://127.0.0.1:11434/v1
    api_key: not-needed
    default_model: qwen2.5-coder:7b
    models:
      qwen2.5-coder:7b:
        supports_tools: true
  DeepSeek:                    # 搜索跑这里
    api_url: https://api.deepseek.com
    api_key: sk-...
    supports_search: true
    supports_fetch: true
    models:
      deepseek-chat:
        supports_tools: true

search:
  provider: DeepSeek           # ← providers: 里的一个名字
```

配好之后，用 `local` 对话时模型照样能看到 `toolkit_search` / `toolkit_fetch`；请求发给
`DeepSeek` 的 `POST /v1/tools/search`，用的是 **DeepSeek 的 key**（不是 local 的）。

规则：

- **默认是「回退」**：这一轮自己的 provider 能搜就用它（便宜、同一套凭据），不能搜才走
  `search:` 段里那个。写 `always: true` 就反过来——**所有** web 调用都走 search provider，
  即使对话用的 provider 自己能搜（一个账号、一份账单）。
- `supports_search` / `supports_fetch` 默认继承被指向的那个 provider；在 `search:` 段里写
  可以覆盖。
- `search_path` / `fetch_path` 覆盖端点（相对 `api_url` 的路径，或完整 URL）。
- 指向一个不存在的 provider、或那个 provider 没有 `api_key`，启动时会有 WARN，
  `GET /api/providers` 的 `search_provider.error` 里也能看到；web 工具保持关闭。
- 也可以写成 `search_provider:`（同一个东西的另一种拼法）。

`search:` 段是 provider.yaml 的**顶层**段（和 `providers:`、`settings:` 平级），
不是某个 provider 块里的字段。

### 语音输入（`asr:` 段）

在 Web UI 的输入框旁边有一个麦克风按钮：点一下开始录音，再点一下结束，转写出来的文字
**填进输入框**（不自动发送——转写会出错，人看过再发）。

它需要一个**转写服务**。服务在哪由 `asr:` 段说：

```yaml
asr:
  url: http://127.0.0.1:8284        # ← 转写服务的地址
  model: sense-voice               # ← 模型名（本地服务会忽略它）
  api_key: sk-...                  # ← 可选：要 key 的网关才需要
  timeout_ms: 30000                # ← 可选，默认 30 秒
```

`asr:` 段是 provider.yaml 的**顶层**段（和 `providers:`、`search:`、`settings:` 平级）。
没配这一段，界面上就**没有**麦克风按钮——给一个点了没反应的死按钮更糟。

服务端做的事很少：把浏览器录好的音频（16 kHz 单声道 WAV）转成
`multipart/form-data`，POST 到 `<url>/v1/audio/transcriptions`，把返回的 `text` 字段
交回去。所以转写服务只要说 OpenAI 的转写接口那套话就行。

**本地跑 SenseVoiceSmall**（仓库里带了）：

```bash
pip install sherpa-onnx                      # 唯一需要的依赖
python3 sched/asr_server.py --download        # 拉模型（约 240 MB）到 ~/.tea_agent/asr
python3 sched/asr_server.py                   # 监听 127.0.0.1:8284
```

然后 `asr: { url: http://127.0.0.1:8284 }`。这个服务只监听回环地址：麦克风的录音
不该在没有认证的情况下暴露给局域网。

**用云上的转写服务**：任何 OpenAI 兼容 `/v1/audio/transcriptions` 的服务都行，把
`url` 指过去、填上 `api_key` 即可。

规则：

- `url` 写**基础地址**就行，路径按 `/v1/audio/transcriptions` 补；已经写全了就不动。
- `url` 必须是 http(s)，别的协议会在启动时 WARN，配置视作没配。
- `api_key` 走 `Authorization: Bearer`；本地服务不需要，就别写。
- 浏览器只在**安全上下文**（https 或 localhost）里给麦克风权限。用
  `http://192.168.x.x:8282` 访问设备时，浏览器不给麦克风，界面因此也不显示按钮——
  这是浏览器的规矩，不是这个程序的选择。

### MCP 服务器（`mcp.yaml`）

**MCP 服务器**（Model Context Protocol）是**独立的程序**：agent 把它们启动起来，用 stdio
上的 JSON-RPC 跟它们说话，它们提供的工具就变成模型可以调用的工具。

配置**不在 provider.yaml 里**，而在**它同目录下的 `mcp.yaml`**：

```yaml
# $HOME/.tea_agent/mcp.yaml
servers:
  uiautomation:
    command: uiautomation-mcp
    args: []
```

为什么单独一个文件：provider.yaml 装的是模型提供方和 API key，`mcp.yaml` 装的是「给模型
装新手」的程序——两回事，节奏也不同（换服务器不用碰 key）。`mcp.yaml` 的路径跟着
provider.yaml 走：`--config` 指到哪，`mcp.yaml` 就在哪旁边。

（provider.yaml 里如果还留着旧的 `mcp:` 段，它**不再被读**，启动时会警告一句，告诉你
搬到 `mcp.yaml` 去。）

工具的公开名字是 `mcp__<服务器>__<工具>`（例如 `mcp__uiautomation__SearchElements`），
这样两个服务器里同名的工具不会打架。

`servers:` 既可以写成**映射**（键是服务器的名字），也可以写成**列表**（每项自己带
`name:`）：

```yaml
servers:
  - name: uiautomation
      command: 'C:\Tools\uiautomation-mcp\uiautomation-mcp.exe'
      timeout_ms: 60000
```

每个服务器可以写的字段：

| 字段 | 意思 | 默认 |
| --- | --- | --- |
| `command:` | 要跑的程序（不带斜杠时按 PATH 找） | 必填 |
| `args:` | 参数（列表，或者一个按空格切的字符串） | 空 |
| `env:` | 给子进程加的环境变量（映射） | 空 |
| `cwd:` | 子进程的起始目录 | agent 自己的目录 |
| `enabled:` | `false` 保留这条但不启动 | `true` |
| `timeout_ms:` | 一次工具调用的上限 | 60000 |
| `startup_timeout_ms:` | 握手（启动 + initialize）的上限 | 20000 |
| `protocol_version:` | 要的 MCP 协议版本 | 2024-11-05 |

**和 uiautomation-mcp 一起用**（Windows 上的 UI Automation 服务器，能找控件、点按钮、
往输入框打字、截屏）：

```powershell
winget install Locomorange.UIAutomationMCP     # 或者从 release 下载
```

```yaml
# mcp.yaml
servers:
  uiautomation:
    command: 'C:\Tools\uiautomation-mcp\uiautomation-mcp.exe'
    timeout_ms: 60000          # 截屏这种慢操作要给够时间
```

行为上的几条规则：

- 服务器**按需启动**：第一次用到它的工具时才拉起来。启动横幅不等待它。
- **改完 `mcp.yaml` 点「刷新」就行**：那个按钮会重新读一遍文件再重连（文件坏了会被
  拒绝，正在跑的服务器不受影响）。
- 连不上的服务器在「设置 → MCP 服务器」和日志里写明原因（命令找不到、握手超时、
  回答不是 JSON……），**别的服务器照常工作**，这一轮的工具清单里只是少了它的工具。
- 一个连不上的服务器在一分钟内不会被反复重试（不然每一轮都要等它）。
- 服务器返回的图片会存进工作区并附在对话里（跟 `toolkit_load_image` 一样），所以
  **模型要支持看图**才看得到。
- 服务器发给客户端的请求（比如 sampling、roots）一律回「方法未实现」——不回的话
  服务器会一直等一个不会来的答案。

自己试一个工具（不用模型）：

```bash
curl -X POST localhost:8282/api/mcp/servers/uiautomation/call \
  -H 'Content-Type: application/json' \
  -d '{"tool":"mcp__uiautomation__SearchElements","arguments":{"name":"OK"}}'
```

Windows 上还有两个现成的脚本：`scripts\mcp-check.ps1`（查正在跑的 agent 的 MCP 状态、
调一个工具）和 `scripts\mcp_probe.ps1`（**不经过 agent** 直接跟服务器说话，用来判断
「服务器本身有没有问题」）。完整的联调步骤见 [mcp.md](mcp.md)。

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

**批量部署时别默认开着**。`supports_usage: true` 意味着**每台设备**都会去问一次「订阅还剩多少」：
一个账号上成千上万个请求，这种模式最容易把账号弄进风控。所以「只填一个 API key」的快捷通道
（`POST /api/config/quickstart`）生成的配置里它是**关着的**（文件里注释着怎么打开），
只有控制台链接（`usage_console_url`，不发请求）。自己上传的 `provider.yaml` 按原样生效——
那份是你写的。

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
    # v2.6 模型默认开启思考：小米的文档要求思考模式下把上一轮的
    # reasoning_content 原样带回去（程序本来就带，写出来是为了不依赖端点猜测）。
    reasoning_content: keep
    models:
      mimo-v2.6-flash:            # 1M 上下文 / 128K 输出
        max_context_tokens: 1000000
        max_output_tokens: 128000
        supports_tools: true
        supports_reasoning: true
        supports_vision: true
      mimo-v2.6-pro:
        max_context_tokens: 1000000
        max_output_tokens: 128000
        supports_tools: true
        supports_reasoning: true
        supports_vision: true
```

**MiMo 有两个网关，key 决定走哪个**（官方文档：<https://mimo.mi.com/docs>）：

| key 形状 | 是什么 | `api_url` | 控制台 |
| --- | --- | --- | --- |
| `sk-…` | 按量付费 | `https://api.xiaomimimo.com/v1` | <https://platform.xiaomimimo.com/#/console/usage> |
| `tp-…` / `ttp-…` | Token Plan 订阅（个人 / 团队） | `https://token-plan-cn.xiaomimimo.com/v1` | <https://platform.xiaomimimo.com/#/console/plan-manage> |

两者不能混用：Token Plan 的 key 打到按量付费的网关上（或反过来）就是 401。
「只填一个 API key」的快捷通道（`POST /api/config/quickstart`）**按 key 的形状自己选网关**，
默认模型 `mimo-v2.6-flash`；也可以自己传 `api_url` 覆盖（新加坡站是
`token-plan-sgp.xiaomimimo.com`，欧洲站是 `token-plan-ams.xiaomimimo.com`）。

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

### 服务端繁忙时的自动重试（HTTP 529）

有的 provider 会在整点高峰直接拒绝请求，MiniMax 就是这样：

```
HTTP 529: 当前为整点高峰时段，服务器短暂繁忙，通常 1-5 分钟内恢复。请稍后重试 (2064)
{"type":"error","error":{"type":"overloaded_error","message":"…","http_code":"529"}}
```

这不是「请求写错了」——同一个请求过一两分钟照样成功。所以程序**不等这一轮失败，而是等它**：
认出「服务端繁忙」（HTTP 529，或者错误里带 `overloaded_error` / 「繁忙」/「高峰」），
按**分钟**计的退避重发（默认 1 分钟、2 分钟、4 分钟，最多 3 次）。等待期间界面会显示
「服务端繁忙（HTTP 529），稍后自动重试（第 1/3 次，还剩 57 秒）」，点「停止」随时能中断。

两个预算是分开算的：网络抖动（连接断了、502）照旧是几百毫秒级的重试；只有「服务端繁忙」
才等几分钟——provider 自己说了要 1-5 分钟，400 毫秒后重发只会再撞一次同一堵墙。

```yaml
settings:
  overload_retries: 3              # 「服务端繁忙」最多重试几次（0 = 不等，直接失败）
  overload_backoff_ms: 60000       # 第一次等多久（毫秒），之后每次翻倍
  overload_max_backoff_ms: 300000  # 单次等待的上限
  overload_max_total_ms: 600000    # 一次请求所有等待的总上限
```

默认值最多等 7 分钟（60s + 120s + 240s）。想更保守就把 `overload_backoff_ms` 调大，
想彻底关掉就设 `overload_retries: 0`。

两个容易漏的细节：

- **有的网关把失败写在流里**：HTTP 200 + 一个 `{"type":"error","error":{…,"http_code":"529"}}`
  事件。程序会从事件里把状态码取出来，一样按「繁忙」处理（不然它看起来像一次普通的 200）。
- **`Retry-After` 会被尊重**：provider 说「90 秒后再来」就等 90 秒，而不是自己拍脑袋。
  它只会让等待**更长**，不会更短——provider 说「1-5 分钟恢复」，一个 5 秒的
  `Retry-After` 不该把等待缩短成 5 秒。

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
  compaction_trigger_ratio: 0.65   # 历史到预算的 65% 就开始做摘要
  compaction_keep_recent: 4
  compaction_summary_max_tokens: 4096   # 摘要请求的预算：思考模型先花 reasoning 的钱
  tool_output_max_lines: 400
  system_prompt: |             # 也可以整体替换默认规则
    ...
```

另外有一个工作区指令文件 `<workspace>/.tea_agent_run/instructions.md`，内容会原样拼进
system prompt 的「Workspace instructions」小节——用来放「这个项目怎么构建、怎么测、有哪些约定」，
与机器级配置彻底分开。它和 `settings.yaml` 一样在 workspace 打开时读取一次，保证提示词前缀稳定；
需要「改完下一轮就生效」的 system prompt 覆盖请放到 `system_prompt.md`（见 [工作区与运行时存储](workspace.md#工作区与运行时存储)），它每轮重新读取。

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


---

← 回到 [README](../README.md) · 命令清单见 [使用手册](manual.md)
