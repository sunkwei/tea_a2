# a2_tunnel 配置手册

> 侧载（sidecar）是蹲在 a2 旁边的一个小进程，给那台 a2 开一条通向外网的隧道。
> 它**不是** a2 的一部分 —— a2 停了、侧载没起，a2 照常工作。
>
> 这一页给运维/实施照着做：装、起、两种角色分别怎么用、参数、常见问题。

---

## 一、它是什么

一个二进制文件。**一个文件就是全部**，没有依赖。

它有两个角色：

| 角色 | 装在哪 | 干什么 |
| --- | --- | --- |
| `--role agent` | **客户那台设备上**（a2 旁边） | 等着外面的人来连 |
| `--role client` | **运维/外面那台机器上** | 连进去，本地多一个端口 |

两边通过 **broker**（公网上的那台，见《a2_broker 配置手册》）会合。

---

## 二、装

把对应平台的二进制放到机器上：

```
linux_x86_64/a2_tunnel     linux_aarch64/a2_tunnel     win_x86_64/a2_tunnel.exe
```

```bash
sudo install -m 755 a2_tunnel /usr/local/bin/a2_tunnel
```

设备上（a2 旁边）：

```bash
a2_tunnel --role agent
```

外面那台机器：

```bash
a2_tunnel --role client --session <码>
```

---

## 三、agent 侧（设备上）

```bash
a2_tunnel --role agent
```

起来之后它会打印：

```
sidecar: ready. Waiting in room "s43910a0a" for the other side.
  broker        : 103.232.92.54:8382
  room token    : 43910a0ab1600dc462b072114fe90132
  target        : 127.0.0.1:8383
  give this to whoever connects from the outside:
      a2_tunnel --role client --session 43910a0ab1600dc462b072114fe90132
```

**把那行 `a2_tunnel --role client --session ...` 发给外面那个人**，他直接粘上跑就行。

> ⚠️ **这串 session 码就是房间的钥匙**：谁拿着它谁能进。
> **别发到群里、别贴到工单上。**
> 侧载**每次启动都会换一个新码**（旧码会被 broker 拒绝）。

它默认连本机的 a2（`127.0.0.1:8383`）。a2 在别的端口就写上：

```bash
a2_tunnel --role agent --target 127.0.0.1:8282
```

### 等多久

默认等 **5 分钟**，没人连就自己退出（省得在设备上留个空进程）。
想一直等着：

```bash
a2_tunnel --role agent --wait-timeout 0
```

---

## 四、client 侧（外面那台机器）

```bash
a2_tunnel --role client --session <码>
```

起来之后：

```
sidecar: listening on 127.0.0.1:9931
sidecar: the data channel is open (client)
```

**本机多了一个 9931 端口**，它后面就是设备上那台 a2。

用浏览器打开 **http://127.0.0.1:9931/** 就是那台设备上 a2 的界面。

用命令行问它：

```bash
curl -s http://127.0.0.1:9931/api/health | head -c 200
```

> 隧道**刚建好但还没通**的时候，这个端口会**直接拒绝连接**（不是干等着），
> 免得调用方等到自己的超时。

### 想让别的机器也能用这个端口

```bash
a2_tunnel --role client --session <码> --host 0.0.0.0 --port 9931
```

同网段任何机器都能打开 `http://<这台机器>:9931/`。

> ⚠️ **开 0.0.0.0 前想清楚**：代理转过去的是那台设备的**完整 API**，
> 而它的对话接口（`/api/chat`）本来就不需要令牌 → **能连到这个端口的人就能驱动那台的
> agent**（agent 有跑命令的工具）。只在可信网段这么做。

---

## 五、broker 在哪儿

默认是公网上那台 broker（`103.232.92.54:8382`）。

**地址变了不用改命令** —— 改一个文件就行，两边都改：

```
~/.tea_agent/tunnel/broker.txt
```

```
address=新地址:8382
pin=3f9a1c...
```

`pin` 是 broker 的证书指纹（broker 启动时打印）。
**有 pin 就用它来确认对端**：证书对不上就断，比「证书里的名字对得上」更靠得住，
因为 broker 换地址之后名字还是那个名字，但证书是同一张。

也可以命令行覆盖：

```bash
a2_tunnel --role client --session <码> --broker 新地址:8382 --tls-pin 3f9a1c...
```

> ⚠️ **broker 的证书是自签的**（它自己造的，没有 CA 签）。
> 所以**证书库里找不到它**——`--ca-bundle` 对它没用。
> **要认它，只有指纹一条路**：写 `broker.txt` 的 `pin=`，或者命令行 `--tls-pin`。
> 忘了写就会看到：
>
> ```
> sidecar: the TLS handshake failed: X509 - Certificate verification failed, ...
>   the broker's certificate is self-signed, so a CA bundle cannot check it.
>   the fingerprint can: put it in <home>/.tea_agent/tunnel/broker.txt as
>       pin=<fingerprint>
> ```
>
> **Windows 上的路径**是 `%USERPROFILE%\.tea_agent\tunnel\broker.txt`
> （比如 `C:\Users\<用户名>\.tea_agent\tunnel\broker.txt`）——
> 文件夹还没有就自己建一个。

---

## 六、参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--role agent\|client` | client | 自己是哪一边 |
| `--session <码>` | 无 | 一次隧道的码（房间名和令牌都从它推出来） |
| `--room <名>` | 无 | 房间名（一般不用管，用 --session 就够） |
| `--token <密>` | 无 | 房间令牌（同上） |
| `--broker <host:port>` | 见配置文件 | broker 的地址 |
| `--listen <host:port>` | 127.0.0.1:9931 | client 侧本地代理听在哪 |
| `--host <addr>` | 无 | 同上，分开写（跟 a2 的 --host 一个意思） |
| `--port <n>` | 9931 | 同上，分开写 |
| `--target <host:port>` | 127.0.0.1:8383 | agent 侧要连的 a2 |
| `--stun <host:port>` | 跟 broker 走 | STUN 服务器 |
| `--turn <host:port>` | 跟 broker 走 | TURN 服务器 |
| `--turn-user <名>` | 内置 | TURN 账号 |
| `--turn-pass <密>` | 内置 | TURN 口令 |
| `--turn-db <file>` | a2_broker.db（当前目录） | 账号簿（跟 broker 同一个 sqlite 文件） |
| `--ice-relay-only` | 关 | 只走中继（测 TURN 账号时用） |
| `--tls` / `--no-tls` | 开 | 信令要不要 TLS |
| `--ca-bundle <pem>` | 无 | 用这个证书文件验证 broker |
| `--tls-pin <sha256>` | 无 | 用指纹验证 broker（**推荐**） |
| `--wait-timeout <秒>` | 300 | agent 侧等多久没人连就退出（0 = 一直等） |
| `--verbose` | 关 | 打印 ICE 的详细日志 |

---

## 七、TURN 账号从哪来

侧载用哪套 TURN 账号，**按这个顺序**：

1. 命令行给了 `--turn-user` / `--turn-pass` → **用它**；
2. 否则，账号簿（`--turn-db`，默认 `a2_broker.db`，当前目录）里有账号 →
   **用第一个**，并且会打印一行告诉你用的是哪个；
3. 都没有 → 用**内置**的那套。

账号簿是 broker 的那个 sqlite 文件（见《a2_broker 配置手册》）——
`a2_broker.db`，里面有一张表 `turn_accounts(user, pass)`，
一行一个账号，**明文存**，方便手改。

broker 那边改完账号簿，**把同一个文件拷到这台机器上**（或者放在同一个目录里），
侧载就会用里面的账号：

```bash
# 侧载跟 broker 用同一个账号簿
a2_tunnel --role client --session <码> --turn-db /etc/a2/a2_broker.db
```

日志里会有这样一行（说明用的是哪个账号）：

```
sidecar: using the TURN account "alice" from /etc/a2/a2_broker.db
```

---

## 八、怎么确认通了

**客户端那边**：

```
sidecar: the data channel is open (client)
sidecar:   path: local ... - direct (no relay)      ← 打洞成功了
sidecar:   path: local ... - through the broker      ← 走中继（正常，主路径）
```

两种都算通。

**真的通了**再问一下：

```bash
curl -s http://127.0.0.1:9931/api/health
```

返回那台 a2 的健康信息就对了。

---

## 九、常见问题

| 现象 | 大概率原因 | 怎么办 |
| --- | --- | --- |
| `broker says: bad token` | 码是旧的 | 让设备侧**重新起**侧载，拿新的码 |
| `not the one that was pinned` | broker 换了证书 | 把 broker 打印的新指纹配给客户端 |
| `X509 - Certificate verification failed` | 没配指纹，证书库又不认自签的证书 | 写 `broker.txt` 的 `pin=<指纹>`，或命令行 `--tls-pin` |
| `cannot reach the broker` | 地址错 / broker 没起 | 查 `broker.txt` 里的 address |
| 隧道通了但网页打不开 | `--target` 指错了 | 查 agent 侧的 `--target` |
| 端口起来了但连不上 | 隧道还没建好 | 等 `the data channel is open` |
| 设备上侧载自己退出了 | 5 分钟没人连 | 正常；要一直等着加 `--wait-timeout 0` |
| 中间断了 | broker 重启 / 网络抖 | **两边都会自己重连**，不用管 |
