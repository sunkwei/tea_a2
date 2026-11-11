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

**把那串指纹（fingerprint）抄下来** —— 客户端要用它来确认自己连的是这个 broker。

### 第一次启动会自己造一张证书

broker 第一次启动时自己生成一张证书，存在：

```
~/.tea_agent/tunnel/broker.crt     证书（可以给别人装）
~/.tea_agent/tunnel/broker.key     私钥（只有 broker 自己看）
```

**重启不会换证书**，所以指纹一直不变，客户端配一次就行。

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
| `--cert-name <name>` | a2-broker | 自签证书里的名字（可给多个） |
| `--no-tls` | 关 | 信令走明文（只在内网用） |

---

## 七、TURN 凭据

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

> 📌 **将来**会支持用 sqlite3 存多套手动配置的账号（一张表，一行一个账号）。
> 现在还没做，先用内置的这套或者命令行覆盖。

---

## 八、broker 的地址变了怎么办

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

## 九、怎么确认起来了

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

## 十、常见问题

| 现象 | 大概率原因 | 怎么办 |
| --- | --- | --- |
| 客户端连不上 8382 | 端口没开 / 云安全组没开 | 8382/TCP 放开 |
| 中继不工作 | 3478/UDP 或端口池没开 | 三个端口都放开 |
| `bad token` | 客户端的码是旧的 | 让设备侧重新起侧载，拿新的码 |
| 客户端说 `not the one that was pinned` | broker 换了证书 | 把 broker 打印的新指纹给客户端 |
| 打洞一直失败 | 客户那边是对称 NAT | **正常**，走中继（这是主路径） |
