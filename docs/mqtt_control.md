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
   --tunnel-broker 103.232.92.54:8382 \
   --ca-bundle ~/.tea_agent/ca-bundle/cacert.pem
```

| 参数 | 说明 |
|---|---|
| `--mqtt HOST:PORT` | 只接受 TLS；`HOST` 必须是**证书里有名字**的那个（域名，或证书带 IP SAN） |
| `--mqtt_user` / `--mqtt_pass` | broker 上 `mqtt_accounts` 里的账号；口令会出现在 `ps` 与 unit 文件里（已知限制，部署侧收紧权限） |
| `--mqtt_name` | 只能是 `A-Z a-z 0-9 . _ -`；注册名 = `<name>@<本机网卡 IPv4>-<网卡 MAC>` |
| `--tunnel-broker` | 隧道 broker（默认 `103.232.92.54:8382`）；MQTT broker 与它可以是同一台，也可以是别的（例如客户的 EMQX） |
| `--ca-bundle` | 信任的根证书。broker 自签时指向它的 `broker.crt` |

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

> 换网卡/换机器 = 换注册名 = 要重建账号；单纯换 IP 不再影响注册名（MAC 不变）。

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

- MQTT 与信令**共用 `--signal-port`（默认 8382）**，不新增监听端口。
- 账号在 `a2_broker.db` 的 `mqtt_accounts` 表（每次 CONNECT 查库，**改库即生效，不用重启 broker**）。
- `--mqtt` 与 `--no-tls` 互斥：不提供明文 MQTT。

建账号（两种角色）：

```bash
sqlite3 ~/.tea_agent/tunnel/a2_broker.db <<'SQL'
-- 设备：device_name 必须等于它注册时用的名字
INSERT INTO mqtt_accounts(user, pass, role, device_name, enabled)
VALUES ('plant-a-01', '<secret>', 'device', 'plant-a-01@192.168.12.12', 1);

-- 运维：能订阅目录、能给任何设备下命令
INSERT INTO mqtt_accounts(user, pass, role, device_name, enabled)
VALUES ('zonekey', '<secret>', 'operator', NULL, 1);
SQL
```

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
export TEA_MQTT_CA=~/broker.crt       # broker 的自签证书

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

> 证书说明：broker 的证书是自签的，地址写在 CN 里。Python 不认「CN 当 IP」，
> 所以 CLI 只验证书链（`CERT_REQUIRED`）不验名字（`check_hostname=False`）；
> `--insecure` 连链也不验。CA 用 broker 的 `broker.crt`。

## 五、安全边界（读一遍再上线）

| 项 | 说明 |
|---|---|
| 谁能下命令 | 只有 `role='operator'` 的账号，且只能发到 `mt_control/<某个名字>/cmd` |
| 设备能干什么 | 只能订阅**自己**的命令、发布**自己**的回执与注册；订阅别人的 topic 会被拒 |
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
