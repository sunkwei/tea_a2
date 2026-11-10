# 运维手册：a2 + 隧道侧载

读完这一篇，你应该能：

- 在一台设备上把 a2 跑起来，并用隧道从外网连进去；
- 隧道断了知道怎么查；
- 知道哪些东西不能外传。

> 术语：**a2**（`tea_agent`）= 那台机器上的 llm agent，支持 **web终端** ， 默认使用 http://localhost:8282 访问；**侧载**（`a2_tunnel`）=
> 蹲在 a2 旁边的一个小进程，负责把外网的连接转到 a2 上。
> 侧载**不是** a2 的一部分——a2 停了、侧载没起，a2 照常工作。


---

## 一、最短路径（三步）

### 1. 设备上：a2 跑起来，旁边起一个侧载

```sh
# a2 本体（普通 a2，不需要令牌）
a2 --port 8383 &

# 侧载（就这一行，别名和令牌它自己会生成）
exec a2_tunnel --role agent
```

侧载启动后会打印一段东西，**里面有给外面那位用的命令**：

```
sidecar: ready. Waiting in room "s43910a0a" for the other side.
  broker        : 103.232.92.54:8382
  room token    : 43910a0ab1600dc462b072114fe90132
  target        : 127.0.0.1:8383
  give this to whoever connects from the outside:
      a2_tunnel --role client --session 43910a0ab1600dc462b072114fe90132
  giving up in 300 seconds if nobody connects.
```

> **这串 session 码就是房间的钥匙**：谁拿着它谁能进。**别发到群里、别贴到工单上。**
> 侧载**每次启动都会换一个新码**（旧码会被 broker 拒绝）。

### 2. 把上面那条命令转给外面那位

用你们平时用的渠道（电话、微信、邮件）。**只有这一条命令要传**，
房间名和令牌都藏在 session 码里，外面那位不用记任何东西。

### 3. 外面那位：一条命令，本地多一个端口

```sh
a2_tunnel --role client --session 43910a0ab1600dc462b072114fe90132
```

```
sidecar: listening on 127.0.0.1:9931
sidecar: the data channel is open (client)
sidecar:   path: local 103.232.92.54:114786 (relayed) <-> remote 36.112.70.164:111182 (prflx) - through the broker
```

看到 `the data channel is open` 就是通了。本机多了一个 **9931** 端口，
它后面就是设备上那台 a2，此时可以使用浏览器打开 http://localhost:9931 即可访问内网中的 a2

---

## 二、日常操作

### 隧道通没通

```sh
curl -s http://127.0.0.1:9931/api/health | head -c 200
```

通了会返回那台 a2 的健康信息（里面有它的版本、provider、运行时长）。

### 用浏览器开那台设备的 Web UI

浏览器打开 **http://127.0.0.1:9931/** —— 就是设备上那台 a2 的界面，
人在外面，操作的是里面那台机器。

### 用命令行问那台设备的 agent

先拿到工作区 id（`workspace_id` 是必填的，缺了会明确告诉你）：

```sh
WS=$(curl -s http://127.0.0.1:9931/api/workspaces | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
```

然后问（**返回的是 SSE 流**，所以要 `-N` 不缓冲）：

```sh
curl -sN -X POST http://127.0.0.1:9931/api/chat \
  -H 'Content-Type: application/json' \
  -d "{\"workspace_id\":\"$WS\",\"message\":\"看看 /root 下有什么\"}"
```

不想看流就异步提交，回头再取结果：

```sh
curl -s -X POST http://127.0.0.1:9931/api/chat \
  -H 'Content-Type: application/json' \
  -d "{\"workspace_id\":\"$WS\",\"message\":\"...\",\"async\":true}"
# → {"ok":true,"turn_id":"t_…","session_id":"s_…","events_url":"/api/turns/t_…/events"}
```

### 多台机器的浏览器共用一个隧道

client 侧的本地代理**本来就是多路复用的**（一个隧道端口可以同时接很多连接 ✔）。
想让**别的机器**的浏览器也用上，就让代理监听在所有网卡上：

```sh
a2_tunnel --role client --session <码> --host 0.0.0.0 --port 9931
```

然后同一网段里任何机器都能打开 `http://<这台机器>:9931/` → 那台被访问的设备。

> ⚠️ **开 0.0.0.0 前想清楚**：代理转过去的是那台设备的**完整 API**，
> 而它的对话接口（`/api/chat`）本来就不需要令牌 → **能连到这个端口的人就能驱动那台的
> agent**（agent 有跑命令的工具）。只在可信网段这么做；要给单个机器用就保持默认的
> 127.0.0.1（或者用 SSH 端口转发）。

### agent 侧什么时候会退出（重要）

侧载是**按需启动**的，不是常驻服务：

| 时刻 | 行为 |
| --- | --- |
| 启动 | 开始等对端，最多等 `--wait-timeout`（**默认 300 秒 = 5 分钟**） |
| 5 分钟内没有 client 连进来 | **自己退出**（退出码 0，不是崩溃） |
| client 连上 | 开始服务，倒计时停掉 |
| client 中断（掉线/被杀） | **从断开那一刻重新计时**，5 分钟后退出 |
| 5 分钟内 client 又连回来 | **继续服务**（倒计时重置），可以反复这样 |

所以运维的流程是：**需要时通知现场起侧载 → 拿到那条命令 → 连上去 → 用完就断**。
现场那台不用常驻一个进程。

（隧道中途断掉——比如 broker 重启了——两侧会**自动重新协商**，
不用手工重启侧载；日志里会出现 `the data channel closed` 然后 `offering again`。）

### 另一种用法：从微信叫醒它（人在家里）

设备上跑着微信 bot 的话，**不用 ssh**，直接给 bot 发一句 `/tunnel`：

```text
> /tunnel
隧道已启动。家里那台机器上跑这条：

    a2_tunnel --role client --session 488c9f7af34d9d2f40c92721e4b52536

连上以后本机多一个端口 9931，浏览器打开 http://127.0.0.1:9931/ 就是这台设备的 a2。
```

机器人 1 秒多就把码回给你（这条是**死命令**，不走模型 → 可靠、也不烧模型调用）。

然后在**家里那台机器**上跑那条命令，本机就有了一个通向设备的端口：

```sh
a2_tunnel --role client --session 488c9f7af34d9d2f40c92721e4b52536
```

- 侧载 5 分钟没人连就自己退出 → 拿到码要尽快连。
- 想收紧谁能开：`~/.tea_agent/tunnel_allow.txt` 里写允许的 user_id（一行一个）。
- 命令也可以在设备上直接跑：`python3 sched/tunnel_cmd.py start` / `stop`。

---

## 三、参数速查

### a2_tunnel（侧载）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--role agent\|client` | 必填 | agent = 蹲在 a2 旁边；client = 在外网那头 |
| `--session <码>` | agent 自动生成 | **两边之间唯一要传的东西**（房间名+令牌都由它派生） |
| `--port <n>` | 9931 | 本地监听端口（client 侧的「隧道入口」） |
| `--listen <host:port>` | 127.0.0.1:9931 | 完整写法；`--host` / `--port` 分别压过它 |
| `--host <addr>` | 127.0.0.1 | 代理监听在哪个地址上（和 a2 本体的 `--host` 一个写法） |
| `--target <host:port>` | 127.0.0.1:8383 | agent 侧：隧道另一头是哪个 a2 |
| `--broker <host:port>` | 103.232.92.54:8382 | broker（信令 + 中继） |
| `--stun` / `--turn <host:port>` | 103.232.92.54:3478 | 打洞和中继服务器 |
| `--room` / `--token` | — | 手工指定房间/令牌（一般不用，用 `--session`） |
| `--wait-timeout <秒>` | 300 | agent 侧等对端的时限，到点自己退出（0=永远等） |
| `--tls` / `--no-tls` | 开 | broker 公网口是 TLS；内网 broker 才用 `--no-tls` |
| `--ca-bundle <pem>` | ~/.tea_agent/ca-bundle/cacert.pem | 找不到会**大声警告**（证书不校验） |

### a2（只列常用的）

| 参数 | 说明 |
| --- | --- |
| `--port <n>` | 监听端口（默认 8282；部署里一般用 8383） |
| `--host <addr>` | 监听地址（默认 127.0.0.1，**别改成 0.0.0.0**，除非你清楚后果） |
| `--api-token <secret>` | 打开「fleet 通道」(`/api/exec`、`/api/peer/*`) 并要求令牌 |
| `--config <path>` | provider 配置（默认 `~/.tea_agent/provider.yaml`） |
| `--workspace <path>` | 注册工作区（可重复） |
| `--log-level <level>` | 日志级别（trace/debug/info/warn/error/off） |
| `--version` / `--selftest` | 版本 / 自检 |

---

## 四、端口与网络

```
外网那台运维机                       公网 broker (VPS)              设备所在网络
┌──────────────────┐                ┌────────────────────┐        ┌──────────────────┐
│ a2               │  8382/TLS ───▶│ nginx(stream,TLS)  │        │ a2  127.0.0.1:8383│
│   ↳ 侧载 client  │◀─ 3478/UDP ──▶│ broker: STUN+TURN   │◀──────▶│  ↳ 侧载 agent    │
│   ↳ 127.0.0.1:9931│  （中继）      │  127.0.0.1:3479 信令│        │                  │
└──────────────────┘                └────────────────────┘        └──────────────────┘
```

- **8382/TCP**：信令，走 TLS。**令牌走这条路，所以必须是 TLS**（nginx 终结）。
- **3478/UDP**：STUN + TURN 中继。客户侧是对称 NAT，**中继是常态路径**，直连只是优化。
- **9931**：外网那台机器上的本地端口（隧道入口）。**只绑 127.0.0.1**。

---

## 五、故障排查（按症状查）

| 症状 | 原因 | 怎么办 |
| --- | --- | --- |
| `sidecar: cannot listen on 127.0.0.1:9931` | 端口被占 | `ss -tlnp \| grep 9931` 找到那个进程杀掉；或用 `--port 9494` 换端口 |
| `sidecar: the broker rejected the room token` | session 码过期（设备侧载重启过了） | 找现场重新起侧载，把新的那条命令给你 |
| `sidecar: broker says: bad token` | 同上 | 同上（这个会立刻退出，不会一直刷屏） |
| `juice: TURN authentication failed` | 两边用的 TURN 凭据不是一套 | 两边都换成同一个版本（凭据是内置在二进制里的） |
| `a2: /lib/x86_64-linux-gnu/libc.so.6: version 'GLIBC_2.34' not found` | 二进制是动态链接的，而系统太老 | 换**静态链接**的 a2（见下节） |
| `sidecar: the data channel is open` 但请求很慢 | 走的是中继路径 | 正常（客户侧上行慢）；能直连就会快很多，ICE 自己会选 |
| 侧载起来 5 分钟就自己退了 | `--wait-timeout` 到点（默认 300 秒） | 没人在 5 分钟内连进来就会退。**这是设计如此**：需要时再起 |
| 隧道时通时不通 | 信令断了 | 侧载会自动重连（日志里 `the signalling link is down, reconnecting...`）；一直不行就重启侧载 |
| `unknown option '--xxx'` | 参数拼错了 | `--help` 看一遍。**现在参数拼错会直接报错，不会被悄悄忽略** |

---

## 六、安全须知

1. **session 码 = 房间钥匙**。拿到它的人能通过你的隧道连到设备上那台 a2，
   并**驱动它的 agent**（agent 有 shell 工具）。别外传、别进工单。
2. **侧载的日志里含令牌**（它自己会提醒：`the lines above contain the token - do not share this log.`）。
   要贴日志给别人之前先抹掉。
3. **设备上的 a2 建议不设 `TEA_AGENT_API_TOKEN`**：
   - 不设 → `/api/exec`（直接跑命令）和 `/api/peer/*` 不开（404），其余接口和 Web UI 照常；
   - 设了 → 这两个口子才开，而且要带令牌。
   不设并不影响通过隧道使用那台 a2（`/api/chat` 本来就不受令牌管）。
4. **别把 a2 绑到 0.0.0.0**。默认 127.0.0.1 就好——外面进来的路是隧道，不是网络。
5. broker 的公网口走 TLS + 房间令牌两道门；**令牌和 TURN 凭据是两回事**，
   TURN 凭据是内置的（泄露只是别人能用你的中继带宽）。

---

## 七、附录


### broker

目前 broker 是我部署的一个 VPS 主机，IP 为 103.232.92.54，如果有自己的公网服务器，可以在自己的服务器上部署 a2_broker。
broker 目前支持 STUN/TURN，使用 ICE 协商，以实现 a2_tunnel 功能，如果支持 NAT 穿透，会优先使用 UDP 打洞，否则会作为终极代理；


| 路径 | 是什么 |
| --- | --- |
| `~/.tea_agent/tunnel/a2_broker` | broker 二进制 |
| `~/.config/systemd/user/a2_broker.service` | systemd 单元（`systemctl --user restart a2_broker`） |
| `~/.tea_agent/logs/a2_broker.log` | 日志 |
| `/etc/nginx/stream-a2.conf` | nginx 的 TLS 终结配置（公网 8382 → 本机 3479） |

### 版本对照

| 版本 | 有什么 |
| --- | --- |
| 1.1.0 | 隧道第一次跑通 |
| 1.1.1 | 信令改走 TLS；公网口 3479 → 8382 |
| 1.1.2 | 侧载默认值全指向公网 broker；`--session` 一个码搞定；加 `--port`；参数拼错会报错 |
| 1.1.3 | README 加隧道索引 |
| **1.2.0** | **mbedTLS 统一到 3.5.2**（2.28 的支持期到 2026-11）；隧道侧载自动重协商；**微信里发 `/tunnel` 就开隧道并回传 session 码** |
| **1.2.1** | `--target` 支持域名；目标连不上就立刻断开；client 侧加 `--host`（多台机器的浏览器可以共用一个隧道） |

更多设计细节见 [`tunnel.md`](tunnel.md)；a2 本身的命令清单见 [`tea_a2_manual.md`](tea_a2_manual.md)。
