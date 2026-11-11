# a2_broker 配置手册

> broker 是隧道的中枢：**STUN + 信令 + TURN 中继**，一个进程。
> 它是整个方案里**唯一必须站在公网上的东西**。
>
> 这一页给运维/实施照着做：怎么装、怎么起、要开哪些端口、怎么确认起来了。

---

## 一、它是什么

一个二进制文件。没有别的依赖：不需要 Python、不需要 Docker、不需要数据库、
**也不需要 nginx**（以前要 nginx 终结 TLS，现在 broker 自己来）。

一个进程干三件事：

| 干什么 | 端口 | 说明 |
| --- | --- | --- |
| STUN | UDP 3478 | 问 ICE「你看到我是谁」，拿到 srflx 候选 |
| TURN 中继 | UDP 3478 + 49160-49260 | 打洞不通时中继（**转发的是 DTLS 密文**） |
| 信令 | TCP 8382（TLS） | 两边交换 SDP/ICE 候选、校验房间令牌 |

> **中继是主路径**：客户那条网络（家用宽带 + 家用路由器）实测是对称 NAT，
> 打洞不通。所以 TURN 的端口一定要开。

---

## 二、装

把对应平台的二进制放到机器上：

```
linux_x86_64/a2_broker     linux_aarch64/a2_broker     win_x86_64/a2_broker.exe
```

```bash
sudo install -m 755 a2_broker /usr/local/bin/a2_broker
```

**一个文件就是全部**。没有配置文件要写。

---

## 三、起

```bash
a2_broker
```

起来之后它会打印这几行：

```
broker: STUN+TURN on udp/3478 (realm a2, relay 49160-49260)
broker: signalling on tcp/8382 (TLS, bound to 0.0.0.0)
broker: certificate fingerprint (sha256): 3f9a1c...
broker: the client can pin it with --tls-pin 3f9a1c...
broker: the certificate itself is in /root/.tea_agent/tunnel/broker.crt
```

**把那串指纹（fingerprint）抄下来** —— 想看着它的人可以把它写进客户端的
`broker.txt`（`pin=`）。**它不是门槛**：指纹对不上，客户端会说一声但照常连
（真正把门守住的是**房间令牌**）。所以**客户端什么都不配也能用**。

### 第一次启动会自己造一张证书

broker 第一次启动时自己生成一张证书，存在：

```
~/.tea_agent/tunnel/broker.crt     证书（可以给别人装）
~/.tea_agent/tunnel/broker.key     私钥（只有 broker 自己看）
```

**重启不会换证书**，所以指纹一直不变。

### 想用自己买的证书

```bash
a2_broker --cert-name broker.example.com
```

这只改证书里的名字。指纹还是会打印出来。

---

## 四、要开哪些端口

| 端口 | 协议 | 干什么 | 必须？ |
| --- | --- | --- | --- |
| **8382** | TCP | 信令（TLS） | ✅ 必须 |
| **3478** | UDP | STUN + TURN | ✅ 必须 |
| 49160-49260 | UDP | TURN 中继的端口池 | ✅ 必须 |

```bash
# 云厂商的安全组 + 机器上的防火墙，两层都要开
sudo ufw allow 8382/tcp
sudo ufw allow 3478/udp
sudo ufw allow 49160:49260/udp
```

> ⚠️ **安全组和机器内 firewall 是两层**，只开一层不通。

---

## 五、systemd 单元

```ini
# ~/.config/systemd/user/a2_broker.service
[Unit]
Description=a2 tunnel broker (STUN + signalling + TURN)
After=network.target

[Service]
ExecStart=%h/bin/a2_broker
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now a2_broker
systemctl --user status a2_broker
```

日志在 journald：`journalctl --user -u a2_broker -f`。

---

## 六、参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--stun-port <n>` | 3478 | STUN/TURN 的 UDP 端口 |
| `--signal-port <n>` | 8382 | 信令的 TCP 端口 |
| `--signal-bind <addr>` | 0.0.0.0 | 信令绑在哪个地址 |
| `--relay-begin <n>` | 49160 | TURN 中继端口池的起点 |
| `--relay-end <n>` | 49260 | TURN 中继端口池的终点 |
| `--realm <name>` | a2 | TURN 的 realm |
| `--token <secret>` | 无 | 给所有房间定一个令牌（不设就每房间自己定） |
| `--turn-user <name>` | 内置 | TURN 账号 |
| `--turn-pass <secret>` | 内置 | TURN 口令 |
| `--turn-db <file>` | a2_broker.db（当前目录） | 账号簿（sqlite 文件，用量也记在这里） |
| `--usage` | 关 | 打印中继的用量报表然后退出 |
| `--usage-days <n>` | 15 | 明细保留几天（0 = 一直留） |
| `--cert-name <name>` | a2-broker | 自签证书里的名字（可给多个） |
| `--no-tls` | 关 | 信令走明文（只在内网用） |

---

## 七、TURN 账号

账号簿（`a2_broker.db`）**就是** broker 收的账号清单：
第一次启动时它被建好、里面写着默认的那一套；之后改这个文件、重启，就换账号了。

### 内置的那一套（账号簿的初始内容）

账号口令**内置在二进制里**，两边（broker 和侧载）用的是同一套，
所以**不用配置、不用拷文件**：

```
用户  1hblsqt.
口令  2ghlmcl,
```

这套凭据不是机密：它只决定「谁能用这台机器的中继带宽」，
而中继里跑的是 DTLS 密文，broker 看不到会话内容。

要换一套就命令行覆盖（**broker 和所有侧载必须一起换**）：

```bash
a2_broker --turn-user myuser --turn-pass mypass
```

### 账号簿（sqlite 文件，人手改）

账号放在一个 sqlite 文件里：**加一行、改一列、删一行**，不用重编任何东西。

文件叫 **`a2_broker.db`，就在 broker 启动的那个目录里**。
**第一次启动时 broker 会自己建好它，并把默认账号写进去**：

```
broker: the TURN account book is a2_broker.db (1 account, created now)
broker:   the default account is in it; change it with:
           sqlite3 a2_broker.db "UPDATE turn_accounts SET user='name', pass='secret';"
broker: TURN accounts accepted: 1
```

里面有一张表：

```sql
CREATE TABLE turn_accounts (
  user TEXT NOT NULL,
  pass TEXT NOT NULL
);
```

**一行就是一个账号**（明文存，就是为了让改动一眼看懂）：

```sql
INSERT INTO turn_accounts (user, pass) VALUES ('alice', 's3cret');
```

怎么改？用**任何** sqlite 工具都行（broker 就在旁边，路径是相对的）：

```bash
# 看现在有哪些
sqlite3 a2_broker.db "SELECT user, pass FROM turn_accounts;"

# 加一个
sqlite3 a2_broker.db \
  "INSERT INTO turn_accounts (user, pass) VALUES ('bob', 'hunter2');"

# 改一个
sqlite3 a2_broker.db \
  "UPDATE turn_accounts SET pass = 'newpass' WHERE user = 'bob';"

# 删一个
sqlite3 a2_broker.db \
  "DELETE FROM turn_accounts WHERE user = 'bob';"
```

**改完要重启 broker**（它启动时读一次）。

账号簿在别处也行（`--turn-db <file>`）：

```bash
a2_broker --turn-db /etc/a2/turn.db
```

### 想临时换一套

`--turn-user` / `--turn-pass` **这一轮**直接用它们，**账号簿就不看了**：

```bash
a2_broker --turn-user bob --turn-pass hunter2
```

启动时会说清楚用的是哪一套：

```
broker: the TURN accounts come from the command line (1)
```

> 账号簿里的账号**明文存**在这个文件里，所以文件权限是 0600（只有本人能读）。
> 侧载也可以读同一个文件（`--turn-db`），这样它挑的账号一定是 broker 收的。

---

## 八、TURN 中继的用量记账

谁用了这台机器的中继、用了多少，都记在**同一个 `a2_broker.db`** 里。

### 两张表

| 表 | 是什么 | 留多久 |
| --- | --- | --- |
| `turn_usage` | **明细**：一次中继会话一行（谁、什么时候、多久、多少字节） | **默认 15 天**（`--usage-days` 可改），过了就自动删 |
| `turn_usage_daily` | **汇总**：按天按账号（会话数、总时长、总字节） | 一直留着（一天一个账号一行，很小） |

明细表的每一行：

| 列 | 是什么 |
| --- | --- |
| `account` | 用的哪套 TURN 凭据（就是 `turn_accounts` 里的 `user`） |
| `client` | 客户端地址（`ip:端口`） |
| `relayed` | 中继地址（`ip:端口`，在 broker 这台机器上） |
| `started_at` / `ended_at` | 开始和结束（unix 秒）。**还在跑的行 `ended_at` 是空的** |
| `seconds` | 用了多久 |
| `bytes_in` / `bytes_out` | 客户端收到 / 发出去了多少字节 |
| `packets_in` / `packets_out` | 收发的包数 |

### 怎么看

**用 broker 自己打印**（不用记 sqlite 的语法）：

```bash
a2_broker --usage --turn-db a2_broker.db
```

```
TURN usage in a2_broker.db (the last 15 days)

day         account         sessions   duration      bytes in   bytes out
2026-11-11  1hblsqt.               3     12m30s       12.3 MB       4.5 MB
2026-11-10  1hblsqt.               1      3m01s        1.2 MB       0.4 MB

15 days in total: 4 sessions, 15m31s, 13.5 MB in, 4.9 MB out
1 session running right now
```

看多少天：`--usage-days 30`。

**或者直接问 sqlite**：

```bash
# 明细
sqlite3 a2_broker.db "SELECT account, client, seconds, bytes_in, bytes_out FROM turn_usage ORDER BY started_at DESC LIMIT 10;"

# 某个账号一共用了多少
sqlite3 a2_broker.db "SELECT account, COUNT(*), SUM(bytes_in + bytes_out) FROM turn_usage GROUP BY account;"

# 按天的汇总
sqlite3 a2_broker.db "SELECT * FROM turn_usage_daily ORDER BY day DESC;"
```

### 保留多久

- **明细留 15 天**（`--usage-days 15`，改成 0 就是一直留）；
  超过期限的行在 broker 启动时和**每小时**清一次。
- **汇总一直留着**（`turn_usage_daily`），所以「这个月用了多少」永远查得到。

### 有几件事要说清楚

- **字节是 broker 数的**：只有走过中继的流量才算（直连的不算，
  因为直连根本不经过 broker）。
- **一次会话 = 一次中继分配**（TURN 的 allocation）。
  **一条隧道不一定只有一次会话**：ICE 反复协商的时候会多分配几次，
  多出来的那几次**马上就被还掉了**（不到 1 秒），所以它们的字节数是 0。
- **会话结束的时刻**：客户端把侧载关掉的时候，中继立刻把端口放出来；
  客户端**被 SIGTERM 杀掉**的话（析构函数不会跑），
  要等到**租期到期**（最多 10 分钟）才放。
- **明细的字节数是最后写进去的**：broker 中途挂掉的那一次会话，
  字节数会停在最后一次写下的地方（不会丢账号和次数）。
  broker 自己重启时，也会把还开着的会话结掉。

---

## 九、broker 的地址变了怎么办

（IP 换了、机器换了、域名换了。）

**broker 侧不用动** —— 证书和指纹都还在原处。

**客户端侧改一个地方**：`~/.tea_agent/tunnel/broker.txt`

```
address=新地址:8382
pin=3f9a1c...
```

改完这个文件，侧载就知道去哪儿连了 —— STUN 和 TURN 的地址会跟着 broker 走。

也可以直接命令行覆盖：

```bash
a2_tunnel --role client --session <码> --broker 新地址:8382 --tls-pin 3f9a1c...
```

---

## 十、怎么确认起来了

```bash
# 1) 端口通不通
nc -z -w2 <地址> 8382

# 2) STUN 有没有应答（用我们自己的探针）
python3 tools/tunnel_probe/nat_probe.py probe --stun <地址>:3478

# 3) 指纹对不对
a2_broker --help
# 指纹在启动日志里，也存在 broker.crt 里
```

**真的通了**要看侧载的日志：客户端那边出现
`the data channel is open`，就说明整条链路是通的。

---

## 十一、常见问题

| 现象 | 大概率原因 | 怎么办 |
| --- | --- | --- |
| 客户端连不上 8382 | 端口没开 / 云安全组没开 | 8382/TCP 放开 |
| 中继不工作 | 3478/UDP 或端口池没开 | 三个端口都放开 |
| `bad token` | 客户端的码是旧的 | 让设备侧重新起侧载，拿新的码 |
| 客户端说 `not the one that was pinned` | broker 换了证书 | **只是提醒**，隧道照用（1.2.10 起不再拦） |
| 打洞一直失败 | 客户那边是对称 NAT | **正常**，走中继（这是主路径） |
