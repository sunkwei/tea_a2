# HTTP 接口

> 这一页讲**每个接口的字段与语义**。要直接敲命令，看
> [使用手册](manual.md)。

## 给上层 Agent 用的接口

所有响应是 JSON，错误统一为 `{"error":{"code":"…","message":"…"}}`。核心就两类：同步查询 + 一条 SSE 流。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/health` | 探活、版本（`0.17.2`）、当前 provider/model、传输引擎、`tea_home`、`config_loaded` |
| `GET` | `/api/status` | **机群通道**：这台设备的结构化快照（名称、版本、运行时长、系统、CPU、内存、磁盘、工作区数、进行中的轮次）。只读；没配 `--api-token` 时开放，配了就要带令牌 |
| `POST` | `/api/exec` | **机群通道**：在这台设备上跑一条命令，**不经过模型**，返回退出码和输出。**没配 `--api-token` 时这个路由不存在（404）**；配了就要带 `Authorization: Bearer <token>`（或 `X-Api-Token`） |
| `GET` | `/api/peer/health` | **对端通道**：`/api/health` 的令牌版镜像（见下「机器对机器的通道」） |
| `GET` | `/api/peer/status` | **对端通道**：`/api/status` 的令牌版镜像 |
| `POST` | `/api/peer/chat` | **对端通道**：`/api/chat` 的令牌版镜像（级联真正用的那条，SSE） |
| `POST` | `/api/peer/chat/stop` | **对端通道**：停掉对端正在跑的一轮 |
| `POST` | `/api/peer/exec` | **对端通道**：`/api/exec` 的令牌版镜像 |
| `GET` | `/api/peer/tools` | **对端通道**：对端有哪些工具（能力探测） |
| `GET` | `/api/peer/workspaces` | **对端通道**：对端的工作区 |
| `GET` | `/api/workspaces` | 工作区索引（`index_path` + `workspaces[]`） |
| `POST` | `/api/workspaces` | 注册工作区 `{"path":"…","name":"…"}` |
| `GET` | `/api/workspaces/{id}/tree?path=` | 列目录 |
| `GET` | `/api/workspaces/{id}/search?q=` | **在工作区里搜东西**：文件名 + 文件内容。查询串按原样逐字节比较（**不做正则/模糊匹配**）。`scope=name\|content\|both`（默认 both）、`exact=1`（完全匹配：路径整串相等 / 整行相等）、`case=1`（区分大小写）、`path=`（只搜这个子目录）、`limit=`（最多几条，默认 100） |
| `GET` / `PUT` | `/api/workspaces/{id}/file` | 读 / 写文件 |
| `GET` | `/api/workspaces/{id}/raw?path=` | 读文件原始字节（图片 / PDF 等，**16 MB 上限**，用于在页面里显示） |
| `GET` | `/api/workspaces/{id}/download?path=` | 以附件形式下载文件（`Content-Disposition: attachment`），**从磁盘流式发送、无 16 MB 限制**；`&inline=1` 改为在浏览器里直接打开（视频 / PDF） |
| `GET` | `/api/workspaces/{id}/sessions/search?q=` | **搜历史对话**：会话标题 + 说过的内容 + 轨迹里的工具输出。查询串按原样逐字节比较。`scope=all\|title\|messages\|trajectory`（默认 all）、`exact=1`（完全匹配）、`case=1`、`limit=` |
| `GET` / `POST` | `/api/workspaces/{id}/sessions` | 会话列表 / 新建 |
| `GET` | `/api/sessions/{sid}` | 取完整会话（含 reasoning、工具调用与结果）；另外带 `summary_upto` / `summary_bytes`——前面有多少条消息已经折成摘要 |
| `GET` / `PUT` | `/api/sessions/{sid}/todos` | 任务列表：GET 随 `/api/sessions/{sid}` 一起返回；PUT 用 `{"items":[…]}` 整体替换，或 `{"index":n,"status":"…"}` 改一条（`index` **从 1 起算**）。计划在轮次结束时关掉，两轮之间 `todos` 是空的、步骤在 `todo_carry` 里；这时 PUT 改的是 `todo_carry`（改完它就变回活着的计划） |
| `PATCH` / `DELETE` | `/api/sessions/{sid}` | 重命名（`{"title":"…"}`）/ 删除会话 |
| `POST` | `/api/sessions/{sid}/fork` | 从某一轮分叉出新会话：`{"message_index":N}` 是父会话里某条**用户消息**的下标，复制到该轮结束（含回答与工具结果）；省略或负数 = 复制整个会话。返回 `session`（带 `forked_from` / `fork_turn`）与复制后的 `messages`；父会话不变 |
| `GET` | `/api/sessions/{sid}/export` | 导出成单个自包含 HTML（CSS/图片内联，离线可读，末尾带可导入的会话 JSON） |
| `POST` | `/api/workspaces/{id}/sessions/import` | 导入导出的 `.html`（原样或 `{"html":"…"}`）或会话 JSON，新建一个会话；重复导入不会覆盖 |
| `GET` | `/api/sessions/{sid}/trajectory` | 每轮轨迹摘要，最新在前；`?full=1` 带步骤与文件变更，`?format=jsonl` 原始 JSONL |
| `GET` | `/api/turns/{turn_id}/trajectory` | 单轮完整轨迹（`turn_id` 即 `done` 事件的 `trajectory_id`） |
| `GET` / `POST` | `/api/workspaces/{id}/memory` | 工作区长期记忆 |
| `POST` | `/api/llm/chat` | **一次模型调用**（无 agent loop、不执行工具）；见下面的「原语层」 |
| `POST` | `/api/chat` | agent loop 本身；带 `task_id` + `task_context` 时，这一轮就是「在做某个排队任务」，模型会用 `toolkit_task` 汇报进度 |
| `GET` | `/api/workspaces/{id}/logs?tail=300` | 运行日志尾部 |
| `POST` | `/api/config` | 上传完整 `provider.yaml`（原始 YAML 或 `{"yaml":"…"}`）；先校验再替换，校验失败不会覆盖现有文件。设置页的「重置 provider」走的就是它 |
| `POST` | `/api/config/reload` | **重新读一遍磁盘上的** `provider.yaml` 并热加载（文件是手写或从别处拷过来的时候用；光刷新网页没用，配置在服务端的内存里）。先校验再替换：文件不存在 → 404 `config_missing`，文件坏了 → 400 `invalid_config`（**当前配置不动**）；有轮次在跑时 409 |
| `POST` | `/api/config/quickstart` | 首次运行快捷通道：`{"provider":"deepseek\|minimax\|mimo","api_key":"sk-…"}` → 生成并应用只含该 provider 的配置（DeepSeek 默认模型 `deepseek-flash`，MiniMax 默认模型 `MiniMax-M3.1-Flash`，小米 MiMo 默认模型 `mimo-v2.6-flash`）；key 不会出现在任何响应里。**生成的配置不查额度**（`supports_usage` 关着）：批量部署时每台设备都去问额度会把账号弄进风控；上传的 `provider.yaml` 不受此限。MiMo 的网关由 key 形状决定：`sk-…` → `api.xiaomimimo.com`，`tp-…`/`ttp-…` → `token-plan-cn.xiaomimimo.com` |
| `GET` / `PUT` | `/api/workspaces/{id}/system-prompt` | 读 / 写工作区 system prompt：`current_prompt` 是真正生效的人设，`source` 是 `workspace`/`settings.yaml`/`builtin`，`content` 是磁盘上的覆盖（空 = 无覆盖） |
| `GET` | `/api/destroy` | 预览「销毁」会删除的路径（tea home + 每个工作区的 `.tea_agent_run`），不动任何文件 |
| `POST` | `/api/destroy` | **不可逆**：删除 tea home 与所有已索引工作区的 `.tea_agent_run`；必须带 `{"confirm":"destroy"}`，否则 400；有轮次在跑时 409 |
| `GET` | `/api/workspaces/{id}/archive?path=` | 把目录打成 zip 下载（默认 `.tea_agent_run`）；`X-Archive-Entries` / `X-Archive-Skipped` / `X-Archive-Uncompressed` 报打包结果 |
| `GET` | `/api/tools` | 工具定义与 JSON Schema（**含 MCP 服务器提供的工具**，名字是 `mcp__<服务器>__<工具>`） |
| `GET` | `/api/mcp/servers` | 每个 MCP 服务器的状态与它提供的工具：`{configured, enabled, path, file_exists, servers:[{name, command, args, enabled, connected, protocol_version, server_name, tool_count, tools:[…], error}]}`。`path` 是配置文件（和 provider.yaml 同目录的 `mcp.yaml`）；文件不在时 `file_exists:false` |
| `POST` | `/api/mcp/refresh` | **重新读一遍 `mcp.yaml`** 并把所有 MCP 服务器停掉再启动一遍（改完文件后用），返回同上的状态。文件坏了 → 400 `invalid_mcp_config`（正在跑的服务器不受影响） |
| `POST` | `/api/mcp/servers/{name}/call` | 不用模型，直接调一个 MCP 工具：`{"tool":"mcp__<服务器>__<工具>","arguments":{…}}` → `{ok, tool, is_error, text, images, result}`。工具不存在或服务器连不上 → 502 `mcp_call_failed` |
| `GET` | `/api/providers` | provider 清单与能力标记（`supports_search` / `supports_fetch` / `supports_usage`），**不含 key**；另有 `search_provider` 对象：独立搜索 provider（`provider.yaml` 顶层 `search:` 段）的名字、能力与错误信息 |
| `GET` | `/api/providers/{name}/usage` | 查该 provider 的订阅额度（provider 声明了 `supports_usage` 才有）；用 provider 自己的 key 去查，key 不下发。上游失败返回 502 与原因 |
| `GET` | `/api/providers/{name}/models` | 问这个 provider 现在提供哪些模型：服务端用 provider 自己的 key 去问 `GET {api_url}/v1/models`（key 不下发）。返回每个模型的默认配置、来源、新增了哪些、哪些已经不再提供。上游失败返回 502 与原因 |
| `POST` | `/api/providers/{name}/models` | 把确认后的模型列表写进 `provider.yaml`：`{"models":[{"id":…,"max_context_tokens":…,"max_output_tokens":…,"supports_tools":…,"supports_reasoning":…,"supports_vision":…}]}`。**只改这个 provider 的 `models:` 块**（别的 provider、注释、settings 块原样保留），先校验再替换 |
| **`POST`** | **`/api/chat`** | **SSE 流式对话（主入口）**。该会话已有轮次在跑时**默认排队**：返回 `200` + `{"queued":{…},"turn_running":true}`（JSON，不是 SSE），消息在该轮的下一次迭代被采纳；要替换正在跑的那一轮，带 `{"interrupt": true}` |
| `POST` | `/api/chat/stop` | 中断当前轮次（对所有人）。**必须带 `{"session_id":"…"}`**，否则 `cancelled` 一直是 false |
| `POST` | `/api/chat/queue` | 轮次进行中把消息插队（下一轮采纳） |
| `POST` | `/api/chat/queue/cancel` | 取消一条（带 `id`）或全部排队消息 |
| `GET` | `/api/chat/queue?session_id=` | 查看当前排队消息 |
| `GET` | `/api/chat/turn?session_id=` | 这个会话是否还有轮次在跑（`running` / `last_seq` / `oldest_seq` / `dropped` / `subscribers`） |
| `GET` | `/api/chat/turns` | 所有还在跑的轮次（含 `workspace_id` 与 `subscribers`），最近开始的在前 |
| `POST` | `/api/chat/attach` | 接回正在跑的轮次：回放 `since` 之后的事件，然后继续直播。跟随者超过上限时 `429 too_many_subscribers` |

## 机器对机器的通道（级联：一台 a2 控制另一台）

`/api/peer/<路由>` 是 `/api/<路由>` 的**令牌版镜像**：`/api/peer/chat` → `/api/chat`，
`/api/peer/exec` → `/api/exec`，以此类推。镜像只有七条（`chat`、`chat/stop`、`exec`、
`status`、`health`、`tools`、`workspaces`），其它前缀下的路径一律 404。

规矩：

- **没配 `--api-token`，`/api/peer/*` 根本不存在**（404，不是 403）；
- 令牌不对是 **401**（`Authorization: Bearer <token>` 或 `X-Api-Token`），常数时间比对；
- 镜像**不改变**普通路由的行为：`/api/chat` 还是原来那个样子（网页端要用它）。

它存在的理由是给「机器对机器」一个自己的身份：另一台 tea_a2 用它来驱动这一台，
这一台的日志里就能分出「这是另一台 agent 来的」和「这是网页里那个人来的」。
用法见 [peer.md](peer.md)。

```bash
# 问对端自己的 agent（SSE）
curl -N -X POST http://peer:8383/api/peer/chat \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"workspace_id":"ws_9f2c","message":"这台机器现在有什么在跑？"}'
```

## 机群通道（服务器管 40 台设备）

`--api-token <secret>`（或环境变量 `TEA_AGENT_API_TOKEN`）打开的通道，供学校服务器
统一管理设备。**没配令牌时 `/api/exec` 根本不存在**（404），默认安装不多出任何
新的攻击面。详见 [fleet.md](fleet.md)。

### 设备侧

```bash
curl -X POST http://device:8282/api/exec \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"command": "apt-get -y upgrade", "timeout_ms": 600000, "cwd": ""}'
```

```json
{"ok": true, "exit_code": 0, "output": "...", "elapsed_ms": 4210,
 "timed_out": false, "truncated": false, "shell": "sh"}
```

`command` 必填；`timeout_ms` 默认 60000、上限 600000；`cwd` 相对路径按 `tea_home`
解析，绝对路径照用。输出上限是 `max_tool_output`。

### 机群服务侧（服务器上，`sched/fleet_server.py`，默认 127.0.0.1:8290）

所有路由都要带 `Authorization: Bearer <token>`（或 `X-Api-Token`）。响应用统一信封
`{code, message, request_id, data}`。

| 方法 | 路径 | 干什么 |
| --- | --- | --- |
| `GET` | `/api/fleet/health` | 服务还在吗 |
| `GET` | `/api/fleet/devices` | 台账（可带 `groups` / `devices` / `labels` 过滤） |
| `GET` | `/api/fleet/status` | 最近一次采集的结果 |
| `GET` | `/api/fleet/report` | 今天的日报（`data.markdown`） |
| `GET` | `/api/fleet/history` | 某天的原始记录（`?day=`） |
| `GET` | `/api/fleet/audit` | 审计日志（`?limit=`） |
| `GET` | `/api/fleet/jobs` | 最近的任务 |
| `GET` | `/api/fleet/job/{id}` | 一个任务的进度 |
| `POST` | `/api/fleet/collect` | 现在采集一次 |
| `POST` | `/api/fleet/exec` | 批量执行命令，**返回 job id，异步跑** |
| `POST` | `/api/fleet/upgrade` | 批量升级，同上 |
| `POST` | `/api/fleet/ask` | 让**设备自己的 a2** 回答一个 prompt |

`exec` / `upgrade` 的请求体：

```jsonc
{
  "command": "systemctl restart nginx",   // exec 用；upgrade 用 "package"
  "groups": ["classroom-a"],              // 可选：选哪些设备
  "devices": ["class-a-01"],              // 可选
  "labels": {"room": "3-201"},            // 可选
  "canary": 1,                            // 先试几台；它们失败就别动其余的
  "batch_size": 5,                        // 一批几台
  "pause_s": 30,                          // 批与批之间停几秒
  "max_failure_rate": 0.2,                // 失败率超过这个就停
  "verify": "systemctl is-active nginx",  // 升完要过的检查
  "rollback": "apt-get install -y nginx=1.20.1-1",  // 失败时的回滚
  "dry_run": false
}
```

---

### 历史搜索（`GET /api/workspaces/{id}/sessions/search`）

搜**这个工作区里发生过什么**：会话标题、说过的话、想过的、调用过的工具，以及**轨迹**
（命令的输出在那里）。

```bash
curl -s "http://127.0.0.1:8282/api/workspaces/$WS/sessions/search?q=水獭" | jq
```

```json
{
  "query": "水獭",
  "scope": "all",
  "exact": false,
  "case_sensitive": false,
  "count": 2,
  "sessions_scanned": 17,
  "truncated": false,
  "sessions_skipped": false,
  "results": [
    {
      "session_id": "s_9f2c1a",
      "title": "关于水獭的讨论",
      "created_at": "2026-10-29T07:00:00Z",
      "updated_at": "2026-10-29T07:10:00Z",
      "message_count": 12,
      "hits": 3,
      "title_hit": true,
      "snippets": [
        { "kind": "title", "text": "关于水獭的讨论" },
        { "kind": "message", "role": "user", "index": 4, "text": "…水獭到底吃什么？…" },
        { "kind": "trajectory", "tool": "toolkit_exec", "text": "…水獭吃鱼…" }
      ]
    }
  ]
}
```

| 参数 | 取值 | 意思 |
| --- | --- | --- |
| `q` | 必填 | 查询串。**按原样逐字节比较**（同文件搜索：不是正则、不做模糊匹配） |
| `scope` | `all`（默认）/ `title` / `messages` / `trajectory` | 搜标题、搜对话、搜轨迹，还是都搜 |
| `exact` | `1` | **完全匹配**：标题整串相等，或某一整行相等 |
| `case` | `1` | 区分大小写（默认不区分，只折 ASCII 大写） |
| `limit` | 数字 | 最多返回几个会话（默认 50，上限 200） |

`snippets[].kind` 说明命中来自哪里：`title`（标题）、`message`（对话里的一条消息）、
`summary`（被压缩成摘要的那段）、`tool_call`（模型发起的工具调用）、`trajectory`
（轨迹里的工具输出）。`message`/`tool_call` 带 `index`（消息在会话里的下标），
网页据此跳到那一条。

**有上限**：最多读 200 个会话、单会话超过 4 MB 只读标题、每个会话最多 5 条片段；
撞到上限时 `truncated` / `sessions_skipped` 为 true——**结果不完整时要说出来**。

网页的入口在**侧栏「会话」上面那个搜索框**（回车搜索，Esc 返回）；agent 那边是
`toolkit_history_search` 工具。

---

### 工作区搜索（`GET /api/workspaces/{id}/search`）

在工作区里搜文件：**文件名**和**文件内容**一起搜。

```bash
curl -s "http://127.0.0.1:8282/api/workspaces/$WS/search?q=toolkit_workspace_search" | jq
```

```json
{
  "query": "toolkit_workspace_search",
  "scope": "both",
  "exact": false,
  "case_sensitive": false,
  "path": "",
  "root": "/home/me/project",
  "count": 2,
  "files_scanned": 318,
  "truncated": false,
  "files_skipped": false,
  "results": [
    {
      "path": "src/tools.cpp",
      "name": "tools.cpp",
      "type": "file",
      "size": 91233,
      "mtime": "2026-10-29T07:16:02Z",
      "hits": 3,
      "name_hit": false,
      "lines": [417, 2330, 2848],
      "samples": ["ToolResult tool_workspace_search(...)", "..."],
      "preview": "ToolResult tool_workspace_search(...)"
    }
  ]
}
```

**匹配规则**（就是「完全字符串匹配」，没有别的花样）：

| 参数 | 取值 | 意思 |
| --- | --- | --- |
| `q` | 必填 | 查询串。**按原样逐字节比较**：不是正则、不是通配符、不是模糊匹配。查 `.*` 就是找字面量 `.*` |
| `scope` | `both`（默认）/ `name` / `content` | 搜文件名、搜文件内容，还是两个都搜 |
| `exact` | `1` | **完全匹配**：路径（或者文件名）必须整串相等；内容则是整行相等。不带这个参数时是「包含」 |
| `case` | `1` | 区分大小写（默认不区分，只折 ASCII 大写） |
| `path` | 子目录 | 只在这个子目录里搜（工作区相对路径） |
| `limit` | 数字 | 最多返回几条（默认 100，上限 500） |

几个要说清楚的边界：

- **只搜工作区里面**：路径不会跑出工作区根目录；`.tea_agent_run`、`node_modules`、`.git` 这些目录**不参与**搜索。
- **有上限**：最多访问 10000 个文件、目录深度 12、单次最多读 32 MB 文本、单文件超过 2 MB 只搜文件名不搜内容、二进制文件（按扩展名或内容里有 NUL 字节）不搜内容。撞到上限时 `truncated` / `files_skipped` 为 true——**结果不完整时要说出来，不能装作这就是全部**。
- `hits` 是内容命中的处数，`lines` 是命中的行号（1 起），`samples` / `preview` 是命中的那几行原文。

网页的「文件」页左上角就是它（`文件` → 搜索框 → 回车）；agent 那边是 `toolkit_workspace_search` 工具。

---

## 终端（这台机器上的 shell）

这组接口**不经过模型**：浏览器直接驱动这台机器上的一个 shell（Linux/macOS 上是
bash / `$SHELL`，Windows 上是 PowerShell），它跑在一个真正的终端上（pty / ConPTY），
所以行编辑、历史、颜色、Ctrl+C 都和命令行里一样。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/api/shell` | 这台机器能不能给终端：`available` / `platform` / `backend`（`pty` / `conpty`）/ `shell` / `reason`（不能的时候为什么） |
| `GET` | `/api/shell/sessions` | 当前所有会话 |
| `POST` | `/api/shell/session` | 开一个会话：`{"workspace_id":"…","cols":100,"rows":30}`；工作区给了就以它为当前目录。返回 `id` |
| `GET` | `/api/shell/session/{id}` | 会话状态：shell、当前目录、行列、pid、是否还在跑、退出码 |
| `POST` | `/api/shell/session/{id}/input` | 把用户敲的东西送进去：`{"data":"…"}`（普通文本，控制字符用 JSON 转义）或 `{"data_b64":"…"}`（原始字节） |
| `GET` | `/api/shell/session/{id}/output?cursor=&wait=` | 读输出：`data_b64` 是从 `cursor` 之后的字节，`cursor` 是新的游标；`wait=20000` 表示没有输出就等到超时（长轮询，空闲时零流量）；`ended` / `exit_code` 说明 shell 有没有自己退出；`resync` 表示缓冲区丢过字节，客户端要重画 |
| `POST` | `/api/shell/session/{id}/resize` | 改终端大小：`{"cols":120,"rows":40}` |
| `DELETE` | `/api/shell/session/{id}` | 结束这个会话（shell 会被关掉） |
| `POST` | `/api/shell/session/{id}/upload?name=&dir=` | **上传**：请求体就是文件本身（原始字节，不 base64）。落在会话的目录（shell 启动时所在的工作区，没工作区就是服务端的当前目录），`dir` 指定别的目录（绝对路径，或相对会话目录）。文件名只取最后一段，`../..` 走不出去。上限 32 MB |
| `GET` | `/api/shell/session/{id}/file?path=` | **下载**：把文件作为附件发回去（流式，大的文件也不占内存）。相对路径按会话的目录算，绝对路径直接用。shell 能看整个机器，下载也一样 |

环境变量 `TEA_AGENT_SHELL=off` 可以把整个功能关掉（`GET /api/shell` 会说为什么）；
`TEA_AGENT_SHELL_BIN` 指定用哪个程序当 shell（默认 Windows 上是 `powershell.exe`，
有 PowerShell 7 时优先用它；POSIX 上是 `$SHELL`）。

**权限**：shell 以**运行 tea_agent 的那个用户**的身份执行——它是运维用的入口，权限边界就是
那个用户的权限，接口本身不做鉴权。访问控制靠部署那一侧（默认绑 `127.0.0.1`，或者自己
在前面加一层认证）。上传与下载同理：能进这个会话的人，就能把文件放上去、拿下来。

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
| `compaction` | 上下文被压缩：`covered_messages`（折掉的条数）/ `summary_bytes` / `live_tokens` / `trigger_tokens`，`dropped: true` 表示摘要没生成成、那一段旧消息被直接丢弃。回合内部也会发（长回合每轮都查压力） |
| `queue_adopted` | 采纳了一条排队消息（`id` / `content` / `pending`） |
| `queue_unconsumed` | 轮次结束时仍未采纳的排队消息（`items[]`），客户端应放回输入框 |
| `done` | 全部结束，带整轮的 `usage` / `metrics` / `content` / `trajectory_id` |
| `stopped` | 被 `/api/chat/stop` 中断 |
| `error` | `message` / `detail`，外加 `request`（被拒请求的规模：`messages` / `tool_calls` / `bytes`，修过结构时还有 `repaired`） |
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
| `todo` | 任务列表变了：`items[]`（`{id,text,status}`）、`total`、`done`。UI 用它实时更新进度条，不需要等轮次结束。轮次结束、计划被关掉时 `items` 为空，`carried[]` 是这一轮没做完的步骤（下一轮会接回来）；下一轮接回计划时 `resumed: true` |
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
  - `compaction`：`covered_messages` / `summary_bytes`（`dropped: true` 表示摘要没生成成，
    那一段旧消息被直接丢弃了）。
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

### 查模型：问 provider 有哪些模型，回填 provider.yaml

`provider.yaml` 里的 `models:` 块是手写的：provider 上了新模型、模型改了上下文窗口，
都得人去改文件。设置页每个 provider 上的「**更新模型**」按钮做这件事：

1. `GET /api/providers/{name}/models` —— 服务端拿 provider 自己的 key 去问
   `GET {api_url}/v1/models`（跟聊天接口同一个地址推导规则：`…/v1` → `…/v1/models`，
   裸域名 → `…/v1/models`）。回答的形状不统一，tea_a2 都读：

   | 上游的回答 | 怎么读 |
   | --- | --- |
   | `{"data":[{"id":…}]}`（OpenAI 系） | `id` 就是模型名 |
   | `{"models":[{"name":…}]}`（Ollama） | `name` 就是模型名 |
   | `{"model-name":{…}}`（一张裸表） | 键就是模型名 |
   | `{"data":["a","b"]}` | 字符串就是模型名 |

   模型名之外的数字，各家放在不同的地方，tea_a2 挨个找：`context_length` /
   `max_context_tokens` / `context_window` / `max_input_tokens` / `max_model_len`
   （上下文），`max_completion_tokens` / `max_output_tokens`（输出上限），
   `supported_parameters` / `capabilities` / `modalities`（工具、思考、视觉）。
   找不到就看内置表（`deepseek-chat`、`gpt-4o` 这类常见模型），再不行就按名字猜
   （`-reasoner` 会思考，`-vl` 能看图），最后留空由人来填。

   回答长这样：

   ```json
   {"ok":true,"provider":"DeepSeek","url":"https://api.deepseek.com/v1/models",
    "models":[{"id":"deepseek-chat","max_context_tokens":128000,"max_output_tokens":8192,
               "supports_tools":true,"supports_reasoning":false,"supports_vision":false,
               "note":"","source":"provider","in_config":true}],
    "current":[{"id":"deepseek-chat","max_context_tokens":128000,"…":"…"}],
    "added":["deepseek-v3.2"],"removed":["deepseek-coder"],
    "warnings":["provider.yaml lists models the provider does not offer: deepseek-coder"]}
   ```

   `source` 说明这个模型的数字是**从哪来的**：`provider`（上游说的）、`config`
   （provider.yaml 里本来就有的）、`builtin`（内置表）、`guess`（按名字猜的）、
   `default`（都不知道）。`added` 是新出现的模型，`removed` 是文件里有、但上游已经
   不提供的模型。

2. 用户在对话框里改完，确认之后 `POST /api/providers/{name}/models`，body 是
   `{"models":[…]}`——**要写进去的最终列表**（顺序 = 顺序，第一个就是默认模型）。

   写入只动这个 provider 的 `models:` 块：别的 provider 和注释原样保留
   （provider.yaml 是给人看的文件，不能因为点了一次按钮就重排它）。
   写完先校验（拿临时文件试加载），通过才替换并热加载配置；轮次正在跑时 409。

   请求里没带的字段沿用文件里原来的值：采样参数（`temperature` / `top_p`）和
   `reasoning_effort` 是用户自己调过的，不该被一次列表更新抹掉。

> 提供方没有这个接口时（返回 404、不是 JSON、或者空列表），`GET` 会得到 502 和
> 一句人话原因，界面照实显示——不会静默地把列表清空。

---

### 改模型配置：PUT /api/providers/{name}/models

`GET /api/providers` 里每个 provider 都带一张 `model_info` 表：模型名 → 这个模型的
完整配置。设置页的「**模型配置**」对话框就是它渲染出来的：

```json
{"providers":[{"name":"deepseek","models":["deepseek-chat","deepseek-reasoner"],
  "model_info":{
    "deepseek-chat":{"max_context_tokens":128000,"max_output_tokens":8192,
      "supports_tools":true,"supports_reasoning":false,"supports_vision":false,
      "reasoning_effort":"auto","has_temperature":false,"temperature":0.7,
      "has_top_p":false,"top_p":0.9,"has_top_k":false,"top_k":0,
      "has_frequency_penalty":false,"frequency_penalty":0,
      "has_presence_penalty":false,"presence_penalty":0,
      "has_seed":false,"seed":0,"note":""}}}]}
```

`has_*` 说的是「这个值到底设了没有」——采样参数**没设**和**设成 0**是两回事：
没设的参数不会进到请求里（网关不认识的字段会回 400），设成 0 会。

写回去用 `PUT /api/providers/{name}/models`，body 是 `{"models":[…]}`。和上面那个
`POST` 不同，这个是**就地改**，不是换掉整个列表：

| 请求里的写法 | 意思 |
| --- | --- |
| `{"id":"deepseek-chat"}` | 只改这一个模型；请求里没带的字段保持文件里的原值 |
| `{"id":"x","temperature":null}` | 清掉这个值（那一行从 yaml 里消失） |
| `{"id":"x","remove":true}` | 把这个模型从 provider 里去掉 |
| `{"id":"x","default":true}` | 把它挪到第一位——第一个模型就是这个 provider 的默认模型 |
| 请求里没提到的模型 | 原样留着，位置也不变 |

能改的字段：`max_context_tokens`、`max_output_tokens`、`reasoning_effort`、
`temperature`、`top_p`、`top_k`、`frequency_penalty`、`presence_penalty`、`seed`、
`supports_tools`、`supports_reasoning`、`supports_vision`、`note`。

```bash
curl -s -X PUT http://127.0.0.1:8282/api/providers/deepseek/models \
     -H 'Content-Type: application/json' \
     -d '{"models":[{"id":"deepseek-reasoner","max_context_tokens":131072,
                      "max_output_tokens":65536,"reasoning_effort":"high",
                      "supports_reasoning":true,"temperature":0.3,"top_k":40,
                      "default":true}]}'
```

写完同样只动这个 provider 的 `models:` 块，先校验再替换，然后热加载。
值不合法（`temperature: 5`、`top_p: 3`、`top_k: -1`、重复的模型名、把模型全删光）
会得到 400，**文件一个字都不会动**。

> 思考强度只有在这个模型自己声明了 `supports_reasoning: true`、而且强度不是
> `auto` 的时候，才会真的进到请求里（`reasoning_effort` 字段）。一个不会思考的
> 模型不该被要求思考，而一个不认识的字段会让网关回 400。

---

### MCP

**和 provider.yaml 同目录**的 `mcp.yaml` 里配的 MCP 服务器（Model Context Protocol）是
独立的程序：agent 启动它们，用 stdio 上的 JSON-RPC 跟它们说话，它们提供的工具变成模型
可以调用的工具（见 docs/config.md 的「MCP 服务器」一节）。这三个接口可以在没有模型的
情况下检查它们：

| 接口 | 干什么 |
| --- | --- |
| `GET /api/mcp/servers` | 每个服务器的状态（连上了没有、协议版本、它自称是谁）、它提供的工具、连不上时的原因 |
| `POST /api/mcp/refresh` | 重新连接所有服务器 |
| `POST /api/mcp/servers/{name}/call` | 直接调一个工具 |

```bash
curl -s http://127.0.0.1:8282/api/mcp/servers
curl -s -X POST http://127.0.0.1:8282/api/mcp/refresh
curl -s -X POST http://127.0.0.1:8282/api/mcp/servers/uiautomation/call \
     -H 'Content-Type: application/json' \
     -d '{"tool":"mcp__uiautomation__SearchElements","arguments":{"name":"OK"}}'
```

### 语音输入

Web UI 的麦克风按钮把录好的音频交给服务端转写，转写服务在 provider.yaml 的 `asr:` 段里
配置（见 docs/config.md）。服务端只做转发，所以这两个接口也可以直接用：

| 接口 | 干什么 |
| --- | --- |
| `GET /api/asr/status` | 转写服务配了没有：`{configured, url, model, error}`。没配时 `error` 说明原因。 |
| `POST /api/asr/transcribe` | 请求体是**原始 WAV 字节**（`Content-Type: audio/wav`），返回 `{ok, text, elapsed_ms}`。 |

```bash
# 转写一个 wav 文件
curl --data-binary @hello.wav -H 'Content-Type: audio/wav' \
     http://127.0.0.1:8282/api/asr/transcribe
# {"ok":true,"text":"开饭时间早上9点至下午5点。","elapsed_ms":181}
```

- 音频必须是 WAV（RIFF/WAVE）。浏览器录完会在页面里解码、重采样成 16 kHz 单声道
  再发过来——SenseVoice 正好要这个格式，服务端不用做转换。
- 没配 `asr:` 段 → 400 `asr_not_configured`；转写服务挂了或拒绝 → 502 `asr_failed`，
  里面是服务自己的原话（比如「model not loaded」）。
- 不是 WAV 的请求体在**转发之前**就被拒了（400 `bad_request`），不会白跑一趟服务。

### 原语层：给脚本用的 API

产品是「聊天」和「任务队列」；**原语**是「一次模型调用」「一条记录」「一个事件」。
策略——什么时候开工、做什么、什么时候停、通知谁——应该写在脚本里，而不是长在服务端里。

| 接口 | 干什么 |
| --- | --- |
| `POST /api/llm/chat` | **一次模型调用**：没有 agent loop，不执行任何工具。`{prompt\|messages, system, provider, model, tools, temperature, max_tokens, stream}`。不流式时返回 `{content, reasoning, tool_calls[], usage, elapsed_ms}`；流式时是 SSE（`content_delta` / `reasoning_delta`，最后一条 `done` 带全部结果）。`tools` 原样透传——协议归调用方，服务端只负责把请求送出去。 |
| `POST /api/chat` | agent loop 本身也是原语：多带 `system_prompt`（换掉人设，设备事实/工作区指令/agent.md 仍在）、`tools`（只给模型这几个工具，给 `[]` 就是纯聊天）、`task_id` + `task_context`（这一轮在做某个排队任务，模型会据此汇报进度）。 |
| `GET /api/providers/{name}/usage` | 这个 provider 的额度（5 小时窗口 + 每周窗口）；provider 没声明额度端点时是 400。 |

`sched/tea_api.py` 是这两层接口的客户端（纯标准库，设备上跑得起来）：`Tea` 说 agent 的
原语层，`Queue` 说任务队列（任务相关的接口住在 Python 侧的队列上，不在 agent 上）：

```python
from tea_api import Tea, Queue

tea = Tea("http://127.0.0.1:8282")
quota = tea.usage("minimax")              # 额度

queue = Queue("http://127.0.0.1:8283")
task = queue.create_task("do the thing")
queue.start_task(task["id"])
for event in queue.events():              # 订阅，而不是轮询
    if event["type"] == "task_done":
        notify(event["task"])             # 通知怎么发，是你的事
```

`Queue` 会把队列的信封（`{code, message, request_id, data}`）拆开：`data` 里的东西直接
给你，`code` 非零就抛 `ApiError`。带一把项目钥匙就只看得见那个项目。

### 项目钥匙：门不是万能钥匙

队列给每个项目发一把钥匙（`POST /api/projects {"workspace_id": "ws_…"}`，本机操作者才能发，
幂等：同一个项目永远是同一把）。带上钥匙（`Authorization: Bearer <key>`）之后：

| 看到的东西 | 带钥匙的人看到什么 |
| --- | --- |
| `GET /api/tasks` | 只有自己项目的任务（`counts` 也只数自己项目的） |
| `GET /api/tasks/{id}`、`/events`、`/export` | 自己项目的随便看；别人的 → `403` |
| `GET /api/audit` | 只有自己项目的行（`submit` 那行里带着提示词） |
| `GET /api/projects` | 只有自己那一条（名册不是公共的） |
| `GET /api/events`（SSE） | 只有自己项目的事件 |

**不带钥匙 = 本机操作者**，什么都看得到（队列就在本机，操作者是主人）。

**带了钥匙就必须是真的钥匙**：不属于任何项目的钥匙 → `401`。
以前写错一个字符会静默变成「本机操作者」，也就是一把万能钥匙——那不是安全，是没报错。

一把钥匙只能开自己项目：带钥匙的人不能再领别的项目的钥匙（`403`）。

> 注意：项目钥匙守的是**队列**这一层，它背后的 agent 接口（`/api/workspaces`、`/api/chat`）
> 还没有钥匙的概念——要真正做到「项目之间互相看不见」，agent 那一层也得按项目分。

---


---

← 回到 [README](../README.md)

---

← 回到 [README](../README.md) · 命令清单见 [使用手册](manual.md)
