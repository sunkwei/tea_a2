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
  compaction_trigger_ratio: 0.65   # 历史到预算的 65% 就开始做摘要
  compaction_keep_recent: 4
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
