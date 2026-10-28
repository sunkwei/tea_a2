# 级联：让一台 tea_a2 控制另一台

> 「你能不能去控制另一台设备上的 tea_a2？比如 `http://172.16.1.31:8383`。」
>
> 这一页回答这个问题，并给出做法。

---

## 一句话的答案

**不用再设计一套通讯方式。** HTTP + SSE 就是总线，tea_a2 本来就把它端出来了：
`POST /api/chat` 是一台 agent 的完整入口（跑它自己的 agent loop，用它自己的 provider、
它自己的工具、它自己的上下文），SSE 把过程和结果流回来。

真正缺的不是协议，是**「对端」这一层**：

| 缺什么 | 现在补上的是 |
| --- | --- |
| 一个「另一台机器」的地址和名字 | 配置里的 `peers:` 段（地址 + 令牌 + 说明） |
| 一个能对端说话的**工具** | `toolkit_peer`（问它 / 让它跑命令 / 看它的状态 / 停它） |
| 一条**机器对机器**的通道 | 被控端的 `/api/peer/*`（`--api-token` 打开，令牌保护） |
| 看得见的「它到底干了什么」 | 工具结果里带对端的工具调用轨迹和对端的 session id |

所以级联长这样：

```
   人 ──▶ tea_a2 (A)  ──toolkit_peer──▶  tea_a2 (B)  ──▶  B 的工具 / B 的机器
             │                              │
        A 自己的脑子                  B 自己的脑子
      （决定问什么）              （决定怎么做、用什么上下文）
```

**上一层决定「做什么」，下一层决定「怎么做」。** 这和机群的多级是同一个道理，
只是规模不同：机群是「一台服务器管 40 台设备」（有台账、有灰度、有审计），
级联是「A 直接找 B」（一条命令、一次问答）。

---

## 一、两种「多级」，别搞混

|  | **级联（本文）** | **机群** |
| --- | --- | --- |
| 场景 | A 想让 B 干一件事 | 一台服务器管 40 台设备 |
| 配置 | `peers:` 段，写一两台 | 台账 `devices.json` + 机群服务 |
| 中间件 | **没有**：A 直接说 HTTP | `sched/fleet_server.py`（台账 / 批量 / 审计） |
| 工具 | `toolkit_peer` | `toolkit_fleet` |
| 适合 | 两台机器、临时搭一条链路 | 批量运维、灰度升级、每日巡检 |

机群那一套见 [fleet.md](fleet.md)。**要管很多台**，用机群；
**只是想让另一台机器上的 agent 干活**，用级联——它不需要装任何 Python 侧的东西。

---

## 二、配置：对端是谁

`peers:` 放在 **settings 段**里——也就是 `<tea_home>/tea_agent.yaml`（机器级）或工作区的
`.tea_agent_run/settings.yaml`（按工作区），规则和其它设置一样：后者盖前者。
（`provider.yaml` 的 `settings:` 段不再被读，所以别把它写在那里。）

```yaml
# ~/.tea_agent/tea_agent.yaml
settings:
  peers:
    study-pc:                              # 名字：模型用它来指这台机器
      url: http://172.16.1.31:8383         # 对端 a2 的地址
      token: <对端的 --api-token>          # 机器对机器通道的令牌
      workspace: ws_9f2c                   # 对端上用哪个工作区（可省）
      description: 书房的电脑，能看家里的摄像头 # 模型会看到这句
```

也可以写成列表：

```yaml
settings:
  peers:
    - name: study-pc
      url: http://172.16.1.31:8383
      token: <对端的 --api-token>
```

规矩：

- `url` 必须是 `http://` 或 `https://`；**不合法的那条会被丢掉，并在启动日志里
  留下一条警告**（一个模型永远够不着的对端，比没有对端更糟）。
- `name` 不能空；重名的后者被丢掉。
- `token` 是**对端**的令牌（对端启动时 `--api-token` 给的那个）。
  令牌写在配置里，**永远不进提示词**——模型只拿到名字。
- `workspace` 可以省：省了就问对端「你有哪个工作区」，用第一个。

---

## 三、被控端：把机器对机器的通道打开

对端照常跑，只是多一个令牌：

```bash
tea_agent --host 0.0.0.0 --port 8383 --api-token "$(cat /etc/tea/peer-token)"
```

于是对端多出一组路由：**`/api/peer/<路由>` 是 `/api/<路由>` 的令牌版镜像**。

| 路由 | 干什么 |
| --- | --- |
| `POST /api/peer/chat` | 跑对端自己的 agent loop（SSE 流回来）——**级联真正用的那条** |
| `POST /api/peer/chat/stop` | 停掉对端正在跑的一轮 |
| `POST /api/peer/exec` | 在对端跑一条命令（不经过模型） |
| `GET /api/peer/status` | 对端的快照（版本、运行时长、磁盘、正在跑几轮……） |
| `GET /api/peer/health` | 对端在不在、什么版本、用的哪个 provider |
| `GET /api/peer/tools` | 对端有哪些工具（能力探测：它有没有机群工具？有没有 MCP？） |
| `GET /api/peer/workspaces` | 对端的工作区 |

三条规矩：

1. **没配 `--api-token`，`/api/peer/*` 根本不存在**（404，不是 403）——
   默认安装不会长出新的攻击面。
2. **令牌不对就是 401**（常数时间比对，不泄露前缀）。
3. **它是白名单**：镜像只有上面那几条；`/api/peer/destroy` 这种不存在的东西是 404，
   不会因为「有前缀」就变成真的路由。

> 为什么要有这个镜像，而不是直接用 `/api/chat`？因为它给「机器对机器」这件事一个
> **自己的身份**：对端可以只把这一组路由当成机器接口看待，审计日志里也能一眼分出
> 「这是另一台 agent 来的」和「这是网页里那个人来的」。
> 旧的 tea_a2（没有这个镜像）对端，工具会自动退回普通路由——级联不挑版本。

---

## 四、控制端：一个工具，七个动作

`toolkit_peer` 在工具表里（不需要额外开关；配了对端就用得上）。

| action | 干什么 |
| --- | --- |
| `list` | 列出配置里的对端（名字、地址、有没有令牌、工作区、说明） |
| `health` | 对端在不在、什么版本、用的哪个 provider / 模型 |
| `status` | 对端的快照 |
| `chat` | **问对端自己的 agent**——它用它自己的 provider、工具和上下文回答 |
| `exec` | 在对端跑一条命令（不经过模型；退出码 + 输出） |
| `tools` | 对端有哪些工具（能力探测） |
| `workspaces` | 对端的工作区 |
| `stop` | 停掉对端正在跑的一轮（用 session id） |

参数：

| 参数 | 说明 |
| --- | --- |
| `peer` | 配置里的名字（`peer="study-pc"`） |
| `url` | 没配置过的地址（`url="http://172.16.1.31:8383"`）。**令牌仍然只从配置里取**：地址和某个已配置对端一样时，用那一台的令牌和说明 |
| `prompt` | `chat` 的问题 |
| `command` / `cwd` | `exec` 的命令和工作目录 |
| `workspace_id` | 对端上用哪个工作区（默认：配置里写的，否则对端的第一个） |
| `session_id` | `chat` 续用某个会话 / `stop` 停某轮 |
| `timeout_s` | 等对端多久（默认 900 秒） |

于是对 A 说：

> 「让书房的电脑看看投影仪是不是开着，它有投影仪的控制权」

A 会：

1. `toolkit_peer chat peer=study-pc prompt="检查投影仪服务是否在跑，不在就看日志找原因"`；
2. B 用**它自己的** provider 和工具去查，**用 B 自己的话**回答；
3. A 拿到答案，外加一份 **B 干了什么**的轨迹：

```
[chat study-pc @ http://172.16.1.31:8383] (18422ms, 2 tool call(s) on the peer)
投影仪服务是 DOWN，日志里说 /opt/app/displayd.conf 里的 device 字段空了。

--- what the peer did ---
- called toolkit_exec({"command":"systemctl status displayd"})
- toolkit_exec: FAILED (12ms) - [shell: sh] [exit code: 3] inactive
- called toolkit_file({"path":"/opt/app/displayd.conf"})
- toolkit_file: ok (2ms) - device=

(the peer's session is s_5b1c9a44d2e0 - use it to stop the turn or to continue the conversation)
```

轨迹很重要：**A 的模型看到的不只是 B 的一句话，还有 B 试过什么、哪一步失败了**。
没有它，A 只能相信 B 的转述。

---

## 五、级联的形状

对端自己也可以有对端，于是可以串起来：

```
A ──toolkit_peer──▶ B ──toolkit_peer──▶ C
```

每台机器只认识它配置里的对端，**没有全局的拓扑**——这也意味着没有全局的故障点：
C 挂了，B 照常，A 只是拿到一句「B 也没能问到 C」。

A 想知道 C 的状态，让 B 去问：

> 「让书房的电脑去问一下客厅那台还能用吗」

B 的模型自己决定：它可能用 `toolkit_peer health`，也可能直接 `chat` 问 C。

---

## 六、手工调一条（不经过模型）

工具是给模型用的；调试时直接 curl 也行：

```bash
# 1) 对端在不在
curl -s -H "Authorization: Bearer $PEER_TOKEN" \
     http://172.16.1.31:8383/api/peer/health

# 2) 问对端自己的 agent（SSE，边跑边收）
curl -N -X POST http://172.16.1.31:8383/api/peer/chat \
     -H "Authorization: Bearer $PEER_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"workspace_id":"ws_9f2c","message":"这台机器现在有什么在跑？"}'

# 3) 让对端跑一条命令（不经过模型）
curl -s -X POST http://172.16.1.31:8383/api/peer/exec \
     -H "Authorization: Bearer $PEER_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"command":"uptime","timeout_ms":5000}'
```

---

## 七、安全：现在是什么，不是什么

**是什么**

- 对端**默认只绑 127.0.0.1**。要跨机器访问，才需要 `--host 0.0.0.0`。
- 机器对机器的通道（`/api/peer/*`）**必须带令牌**，没配令牌的设备根本没有这条通道。
- 令牌在配置里，**不进提示词、不进会话、不进日志**（日志里只有 URL）。
- 工具的每个动作都记在轨迹里：A 问了谁、问了什么、用了多久、对方干了什么。

**不是什么**

- **令牌不是网络隔离的替代品。** 开了 `--host 0.0.0.0` 之后，对端的网页端、
  `/api/chat` 这些路由**仍然是开的**（网页端要能用，就不能一刀切）。
  也就是说：**能连到这台机器的人，就能驱动它的 agent**（agent 有 `toolkit_exec`）。
  真正的边界是网络：受控的 VLAN，或者前面加一层反向代理（nginx + token / mTLS）。
  和 [README](../README.md#️-安全须知) 里说的是同一件事。

- **令牌不会保护对端的 `/api/chat`。** 它保护的是 `/api/peer/*` 这组镜像路由
  （以及本来就受保护的 `/api/exec`）。要让整台设备都要求令牌，是另一件事
  （网页端也要跟着改），还没做。

几条规矩，和机群那边一样：

1. **别把设备直接放到公网。**
2. **令牌分开**：谁能调用 A，和 A 拿什么去访问 B，是两个秘密。
3. **级联出去的权限，就是对端那台机器的权限。** 让 A 能 `exec` B，就等于让 A
   能在 B 上跑任意命令——`toolkit_exec` 能做的事，`toolkit_peer exec` 也能做。

---

## 八、故障排查

| 现象 | 多半是 |
| --- | --- |
| `this device has no peer channel`（404） | 对端没带 `--api-token` 启动 |
| `the peer channel needs the API token`（401） | 令牌不对，或者控制端没配 `peers[].token` |
| `the peer channel has no route 'xxx'`（404） | 对端是旧版本（没有镜像）——工具会自动退回普通路由 |
| `cannot reach the peer at ...` | 地址不通、端口不对、对端只绑了 127.0.0.1 |
| `the peer has no workspace to work in` | 对端一个工作区都没注册（`POST /api/workspaces`） |
| 对端答了，但内容像是上一轮的 | 对端那个会话还在跑；用 `session_id` 续，或者 `stop` 掉 |
| `the peer's agent failed` | 对端自己的模型/provider 出错——看对端的日志 |

排查的入口：

```bash
# 控制端：这次到底往哪发的
grep "toolkit_peer" ~/.tea_agent/logs/tea_agent.log
# 对端：它自己那边发生了什么
tail -f ~/.tea_agent/logs/tea_agent.log
```

---

## 九、和机群的关系

- 机群管**很多台**：有台账、有每日采集、有灰度升级、有审计日志。见 [fleet.md](fleet.md)。
- 级联管**一台**：一条命令、一次问答，不需要装 Python 侧的任何东西。
- 两者可以叠起来：机群服务自己也可以用级联去问某一台设备自己的 agent
  （`fleet.py ask` 走的就是设备的 `/api/chat`）。

一句话：**要「管」，用机群；要「问」，用级联。**

---

← 回到 [README](../README.md) · 接口字段见 [api.md](api.md) · 工具见 [tools.md](tools.md) · 机群见 [fleet.md](fleet.md)
