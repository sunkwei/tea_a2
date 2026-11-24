# MQTT 控制通道：在内网设备上召唤一条隧道

> 给运维看的一页纸。设备在内网、没有人守着，也能被召唤出一条到你这边的隧道。
> 设计定稿见 `docs/tunnel.md` 与需求说明 v1.3；实现是 `src/mqtt_*.{h,cpp}`。

---

## 一、它是什么

```
 运维                                    客户内网设备
 │  publish mt_control/<注册名>/cmd         │  a2 ──TLS(8382)──► a2_broker（--mqtt）
 ├──────────────────────────► a2_broker ◄─────────┤   注册(retained) + 订阅自己的 cmd
 │  subscribe mt_control/<注册名>/resp              │  start → 拉起 a2_tunnel --role agent
 │  人抄：a2_tunnel --role client --session <码>     │  抓码 → publish resp
 └──── TLS(8382) ──► 同一个 a2_broker（TURN）────────┘
```

- **一个端口两协议**：8382 上 MQTT CONNECT（首字节 `0x10`）与信令（行分隔 JSON，首字节 `{`）共存；
  TLS 由 broker 自己终结，两者共用同一张证书。老侧载不需要升级。
- **只做三件事**：`tunnel.start` / `tunnel.stop` / `status`。其它动作一律
  `rejected: unsupported_action`——这条通道永远不是远程命令执行。
- **不常开**：没有任何隧道时，设备上只有一个 MQTT 长连接；隧道只在被召唤时存在。

---

## 二、设备侧（a2）

```bash
a2 --mqtt 103.232.92.54:8382 \
   --mqtt_user plant-a-01 --mqtt_pass '<secret>' \
   --mqtt_name plant-a-01 \
   --mqtt_oper_name zonekey \
   --tunnel-broker 103.232.92.54:8382
```

| 参数 | 说明 |
|---|---|
| `--mqtt HOST:PORT` | 只接受 TLS。**证书不校验**（broker 自签），口令才是门 |
| `--mqtt_user` / `--mqtt_pass` | broker 上 `mqtt_accounts` 里的账号；口令会出现在 `ps` 与 unit 文件里（已知限制，部署侧收紧权限） |
| `--mqtt_name` | 只能是 `A-Z a-z 0-9 . _ -`；注册名 = `<name>@<本机网卡 IPv4>-<网卡 MAC>` |
| `--tunnel-broker` | 隧道 broker；**默认跟随 `--mqtt`**（默认部署里 a2_broker 一个进程既是 MQTT 又是隧道中枢、同一个端口）。MQTT 用客户的 EMQX 时才需要单独给 |
| `--mqtt_oper_name` | 哪个 operator 账号能管这台设备（默认 `zonekey`）。broker 只认**它自己库里存在的 operator 账号**，没有这个名字就自动落回 `zonekey` |

### 注册名里的 MAC

`<名字>@<IPv4>-<MAC>` 里的 IPv4 **只是标签**：两个客户的内网都可能有一台 `192.168.1.5`，
两台都叫 `a2` 的话，注册名会相同 —— 一台会被另一台踢掉，命令也会发错人。
MAC（默认路由网卡，12 位小写十六进制）才是唯一的那个。

部署时先把账号建对，再起服务：

```bash
a2 --mqtt 103.232.92.54:8382 --mqtt_user plant-a-01 --mqtt_pass x \
   --mqtt_name plant-a-01 --print-registration
# → plant-a-01@192.168.12.12-3c7c3f0a1b2c
```

把这个字符串填进 broker 的 `mqtt_accounts.device_name`，然后用同样的四个参数启动。

**谁能管这台设备**：设备用 `--mqtt_oper_name`（默认 `zonekey`）报一个 operator 账号的名字。
broker 在自己的 `mqtt_accounts` 里查这个名字：**必须是 role=operator 且 enabled 的账号**，
查不到就自动落回 `zonekey`。只有这个名字的 operator 账号能 `list` / `wake` / `stop` /
`status` 这台设备 —— 别的 operator 看不见它，也命令不了它。

> 换网卡/换机器 = 换注册名 = 要重建账号；单纯换 IP 不再影响注册名（MAC 不变）。

### 心跳：1~5 分钟随机

设备侧的 MQTT 客户端**每隔一段随机时间发一个 PINGREQ**：间隔在 **1~5 分钟之间随机抽**，
每发一次就**重新抽**（所以设备之间会越走越散，不会齐步走）。

为什么不是固定值：同一个 broker 后面可能挂着几百台设备，它们都是同一时刻上线、
同一时刻重连的 —— 固定心跳会让 broker 每分钟挨一次「整点暴击」。
随机化把这一百次 ping 摊开到五分钟里。

两个配套的数字（改的时候要一起看）：

| 数字 | 值 | 谁用 | 说明 |
| --- | --- | --- | --- |
| CONNECT 里的 keepalive | **300 秒** | broker | broker 按 **keepalive × 1.5 = 450 秒** 判超时（`src/mqtt_server.cpp`），所以最长的 5 分钟心跳也永远踩不到线 |
| 实际心跳间隔 | **60~300 秒随机** | a2 | 每抽一次都用它 |
| 注册重发 | 60 秒 | a2 | 跟心跳无关：它刷新的是 retained 的在线状态（`mt_register/<注册名>`） |

> broker 侧的离线判定也看这个 keepalive：设备突然断电时，broker 最多 450 秒后
> 才把 `mqtt_devices.online` 置 0（设备有 will 的话会立刻收到 offline 的 retained 注册）。

调试时用 `--log-level debug` 能看到抽到的值：

```
[DEBUG] mqtt: the heartbeat is a random 60..300 seconds
[DEBUG] mqtt: ping sent, the next one in 173 seconds
```

启动后 `/api/status` 里有：

```json
"mqtt": {"enabled": true, "connected": true, "name": "plant-a-01@192.168.12.12",
         "tunnel": "stopped", "last_error": ""}
```

---

## 三、broker 侧（a2_broker）

```bash
a2_broker --mqtt --cert-name plant-a-01.example.com
```

公网机器上的布局约定：**三个可执行文件都放 `$HOME/bin/`** —— `tea_a2`（a2 本体）、
`a2_tunnel`、`a2_broker`，`tea_agent.buildinfo` 也放同一个目录（a2 从二进制旁边读它）。
a2 找侧载的规则是「自己所在目录 + `a2_tunnel`」，所以这两者必须同目录：

```text
~/bin/tea_a2       a2 本体（systemd: ExecStart=%h/bin/tea_a2）
~/bin/a2_tunnel    sidecar（被 a2 拉起）
~/bin/a2_broker    MQTT + 隧道中枢（systemd: ExecStart=%h/bin/a2_broker）
~/bin/tea_agent.buildinfo
```

- MQTT 与信令**共用 `--signal-port`（默认 8382）**，不新增监听端口。
- 账号在 `a2_broker.db` 的 `mqtt_accounts` 表（每次 CONNECT 查库，**改库即生效，不用重启 broker**）。
- `--mqtt` 与 `--no-tls` 互斥：不提供明文 MQTT。

建账号（两种角色）。**账号只回答「谁能登录」，不绑定某一台设备**：

```bash
sqlite3 ~/.tea_agent/tunnel/a2_broker.db <<'SQL'
-- 设备账号：可以被多台设备共用，每台用自己的注册名
INSERT INTO mqtt_accounts(user, pass, role, enabled)
VALUES ('va', '<secret>', 'device', 1);

-- 运维账号：能订阅目录、能给任何设备下命令
INSERT INTO mqtt_accounts(user, pass, role, enabled)
VALUES ('zonekey', '<secret>', 'operator', 1);
SQL
```

> `mqtt_accounts.device_name` 列还在（旧库兼容），但**已经不用了**：设备能碰哪些
> topic，由它 CONNECT 时声明的 client_id（注册名）决定 —— 见下面的实例表。

**设备实例表 `mqtt_devices`**（broker 自己维护，不用手写）：

| 列 | 含义 |
|---|---|
| `client_id` | 注册名 `<name>@<ipv4>-<mac12>`，主键 |
| `account` | 哪个 `mqtt_user` 在用它（多台设备可以共用一个账号） |
| `ip` / `mac` / `version` / `port` | 从注册消息里取 |
| `oper_name` | 这台设备归哪个 operator 账号管（broker 解析后的结果，不是设备自报的那个） |
| `online` | 当前是否连着（断开时置 0） |
| `first_seen` / `last_seen` | broker 时钟，比设备自报的 `ts` 可靠 |

所以：**认证 = user + pass；权限 = 自己声明的注册名 + 设备报的 operator 账号（zone）；
实例 = 表里的一行。**
设备掉线、broker 重启都不会丢这条记录（retained 消息不落盘，这张表落盘）。

> `turn_accounts`（TURN 中继账号）与 `mqtt_accounts` 是**两张表**：能蹭中继的账号
> **不能**连 MQTT。文档里公开的那对内置账号 `1hblsqt./2ghlmcl,` 只保护带宽，不是钥匙。

---

## 四、运维怎么用

用 `mosquitto_pub` / `mosquitto_sub`（`--insecure` 是因为测试环境常是自签证书；
正式环境把 broker 的 CA 放进 `--cafile`）：

```bash
# 1. 看谁在线（retained 注册消息）
mosquitto_sub -h 103.232.92.54 -p 8382 --cafile ca.pem \
  -u zonekey -P '<secret>' -t 'mt_register/#' -v

# 2. 召唤一条隧道
mosquitto_pub -h 103.232.92.54 -p 8382 --cafile ca.pem \
  -u zonekey -P '<secret>' -q 1 \
  -t 'mt_control/plant-a-01@192.168.12.12-3c7c3f0a1b2c/cmd' \
  -m '{"id":"c-0001","action":"tunnel.start","args":{"wait_timeout":1800},"ttl":120}'

# 3. 读回执（session 码在里面）
mosquitto_sub -h 103.232.92.54 -p 8382 --cafile ca.pem \
  -u zonekey -P '<secret>' -t 'mt_control/plant-a-01@192.168.12.12-3c7c3f0a1b2c/resp' -C 1

# 4. 拿回执里的 session 码，在本机接管
a2_tunnel --role client --session <码>
#    本机多一个 127.0.0.1:9931，浏览器打开就是那台内网 a2

# 5. 用完关掉
mosquitto_pub ... -t 'mt_control/plant-a-01@192.168.12.12-3c7c3f0a1b2c/cmd' \
  -m '{"id":"c-0002","action":"tunnel.stop"}'
```

回执长这样：

```json
{"id":"c-0001","state":"ready","session":"494e62055f89b321447b49e251598af2",
 "broker":"103.232.92.54:8382","listen":"127.0.0.1:9931","expires_in":1800}
```

`state` 的取值：`starting | ready | stopping | stopped | failed | rejected | status`。
`rejected` 的原因：`unsupported_action | expired | not_allowed`。

---

## 五、运维 CLI：`sched/mqtt_cli.py`

单文件、纯标准库（Python 3.6+），拷到任意机器就能用。

```bash
export TEA_MQTT_USER=zonekey          # 账号不内置，必须显式给
export TEA_MQTT_PASS='<secret>'       # 放环境变量，不进 ps 历史

python3 sched/mqtt_cli.py list                  # 在线设备（完整注册名）
python3 sched/mqtt_cli.py list --all --json     # 含离线 + 机器可读
python3 sched/mqtt_cli.py wake <device_name>    # 召唤隧道，等 session
python3 sched/mqtt_cli.py status <device_name>
python3 sched/mqtt_cli.py stop <device_name>
```

`wake` 的输出：

```
device  : qdbot-31@172.16.1.31-00044bdf176f (1.8.0, 2026-11-23 09:16:05)
state   : starting
state   : ready
session : 41b457649298c7ef027e6f6aa4567034
broker  : 103.232.92.54:8382
listen  : 127.0.0.1:9931
expires : 120s
next    : a2_tunnel --role client --session 41b457649298c7ef027e6f6aa4567034
```

失败时退出码 1，并说清是哪一类：

| 情况 | 输出 |
|---|---|
| 设备不在线 | `error: device "x" is not online (no retained registration)` |
| 口令错 | `error: the broker refused the credentials (wrong user or password)` |
| 没给账号 | `error: --user and --pass are required (this tool has no built-in account)` |
| 设备起了 sidecar 但失败 | 回执里的 `state=failed` + `error`（例如 `listen_port_in_use`） |
| 设备不回答 | `error: no answer from x within 60s`（`--timeout` 可调） |

重复 `wake` 不会起第二个 sidecar：设备会返回**现有的** session。

> 证书：**不校验**。TLS 只负责加密（broker 的证书是自签的，地址还写在 CN 里），
> 口令才是门。所以这个工具适合在可信网络里用；公网上有中间人冒充 broker 的风险。
> 要验证书就用 `a2_tunnel --tls-pin` 那一套，或者自己加。

## 五、安全边界（读一遍再上线）

| 项 | 说明 |
|---|---|
| 谁能下命令 | 只有 `role='operator'` 的账号，且只能发到 `mt_control/<某个名字>/cmd`；**而且那个账号的名字必须等于设备的 zone**（设备用 `--mqtt_oper_name` 报的，默认 `zonekey`）——别的 operator 看得见列表之外什么也做不了 |
| 设备能干什么 | 只能订阅**自己注册名**的命令、发布**自己**的回执与注册；订阅别人的 topic 会被拒 |
| 一个账号多台设备 | 可以。账号只决定「能不能登录」，注册名决定「能碰哪些 topic」，zone 决定「哪个 operator 能管它」 |
| 目标端口 | `tunnel.start` 的 `args.target` 默认被忽略（锁定 a2 自己的端口）；想放开需要改代码里的白名单 |
| 终端 | 不能通过这条通道开 `--target terminal`（那是远端 shell） |
| 日志 | broker 只记 topic 与结果，**不记 payload**（session 码就是房间钥匙） |
| 隧道常开？ | 不。`wait_timeout` 到期 sidecar 自己退；`tunnel.stop` 立刻退 |

---

## 六、自测

```bash
# 端到端：起真 broker + 真 a2，用纯 Python 的 MQTT 客户端当运维
python3 .tea_agent_run/scripts/mqtt_e2e.py

# 既有隧道回归（MQTT 关掉时行为必须一模一样）
python3 tests/smoke_tunnel.py "$PWD/build"
```
