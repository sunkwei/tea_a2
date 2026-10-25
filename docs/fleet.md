# 多级协同：学校机群的运维管理

> 一个学校一台服务器，40 台设备，服务器和设备上都跑 `a2.service`。
> 每天怎么收集设备的运行状态？软件怎么批量升级？出了问题谁来判断？
>
> 这一页讲**架构**和**怎么用**；接口的字段在 [api.md](api.md)，命令清单在
> [使用手册](tea_a2_manual.md)。

---

## 一句话的架构

```
       人（老师 / 运维）              微信 / Telegram 机器人
              │                              │
              └──────────────┬───────────────┘
                             ▼
        ┌────────────────────────────────────────────┐
        │ 学校服务器                                    │
        │  ┌──────────────────────────────────────┐  │
        │  │ a2.service        ← 大脑：决定「做什么」 │  │
        │  │   toolkit_fleet ──┐                    │  │
        │  └───────────────────┼────────────────────┘  │
        │  ┌───────────────────┼────────────────────┐  │
        │  │ a2_fleet.service  ← 手：台账 / 批量 / 审计 │  │
        │  │   (sched/fleet_server.py)               │  │
        │  └───────────────────┼────────────────────┘  │
        └──────────────────────┼─────────────────────┘
                               │  HTTP + 令牌
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
        ┌──────────┐     ┌──────────┐     ┌──────────┐
        │ 设备 a2  │     │ 设备 a2  │ ... │ 设备 a2  │   40 台
        │ a2.service│    │ a2.service│    │ a2.service│
        └──────────┘     └──────────┘     └──────────┘
              │                │                │
              └──── 本地文件 / 本地命令 ──────────┘
```

**两级 a2，三种下发的路子**，各有各的用处：

| 路子 | 走哪条通道 | 模型在不在场 | 什么时候用 |
| --- | --- | --- | --- |
| **确定性** | 服务器 → 设备 `POST /api/exec` | 不在 | 采集状态、批量升级、重启服务、拉日志 |
| **机群编排** | 服务器 a2 → `toolkit_fleet` → 机群服务 → 设备 | 在**服务器**这一侧 | 「把 3 号机房升级一下」——服务器决定做什么 |
| **认知** | 机群服务 → 设备 `POST /api/chat` | 在**设备**这一侧 | 「这台机器投影仪不亮了，帮我查查」——设备自己查 |

关键的一点：**日常运维不经过模型**。40 台设备每天采集一次状态、每周升级一次软件，
如果每次都让大模型决定，代价高、慢、还不确定。真正需要判断的时候才用模型，
而且**让设备自己的 a2 判断**——它有那台机器的现场上下文。

---

## 一、为什么要有 `/api/exec`

设备上的 a2 原本只有两条路能对设备做事：

- `POST /api/chat` → 走一遍 agent loop → 模型决定调用 `toolkit_exec`；
- 网页终端 `/api/shell` → 人在浏览器里敲。

对机群管理，这两条都不合适：

- **`/api/chat` 太贵也太慢**。40 台设备 × 每天一次采集 = 40 次模型调用，
  而且每次的结果都不一样（今天它给你 `df -h`，明天给你 `df -h --output`）。
- **`/api/shell` 没法脚本化**。它是一个交互式终端，输出要靠游标读，
  退出码要靠提示符猜。

所以加了一条最朴素的通道：

```bash
curl -X POST http://device:8282/api/exec \
     -H "Authorization: Bearer $TOKEN" \
     -d '{"command": "apt-get -y upgrade", "timeout_ms": 600000}'
```

```json
{"ok": true, "exit_code": 0, "output": "...", "elapsed_ms": 4210, "timed_out": false}
```

它复用 `toolkit_exec` 背后的同一套 `run_command`（超时、输出截断、退出码、
Windows/POSIX 的差异都处理好了），只是**不经过模型**。

### 安全：默认根本没有这条通道

`tea_agent` 一直以来的规矩是**没有任何认证机制**，所以它默认只绑 `127.0.0.1`。
机群要跨机器访问它，就必须先解决这件事。这里的做法是：

- **`--api-token <secret>`**（或环境变量 `TEA_AGENT_API_TOKEN`）打开机群通道；
- **没配令牌，`/api/exec` 根本不存在**（返回 404，不是 403）——默认安装**不会**
  多出任何新的攻击面；
- 配了令牌，`/api/exec` 和 `/api/status` 都要带 `Authorization: Bearer <token>`
  （或者 `X-Api-Token`），比对是**常数时间**的，不泄露前缀；
- `/api/status` 是只读的，没有令牌时也能读（和 `/api/health` 一个级别）。

```bash
# 设备上
a2 --host 0.0.0.0 --port 8282 --api-token "$(cat /etc/tea/fleet-token)"
```

> **注意**：`--host 0.0.0.0` 之后，除了机群通道，设备上的其它 API（`/api/chat`
> 之类）也一起暴露在局域网里了——`toolkit_exec` 能执行任意命令。
> 要么把设备放在受控的 VLAN 里，要么在前面加一层反向代理（nginx + token /
> mTLS）。这一点和 [README](../README.md#️-安全须知) 里说的是同一件事。

---

## 二、设备台账

机群服务只认一份台账：`~/.tea_agent/fleet/devices.json`。

```json
{
  "defaults": { "port": 8282 },
  "devices": [
    {
      "name": "class-a-01",
      "host": "192.168.10.31",
      "port": 8282,
      "group": "classroom-a",
      "labels": { "room": "3-201", "model": "pi4" },
      "enabled": true,
      "probe": "df -h /data | tail -1; /opt/app/version.sh"
    },
    { "name": "class-a-02", "host": "192.168.10.32", "group": "classroom-a" },
    { "name": "office-pc", "host": "192.168.10.41", "group": "office", "enabled": false }
  ]
}
```

| 字段 | 干什么 |
| --- | --- |
| `name` | 设备的唯一名字。日志、报表、任务里都用它 |
| `host` / `port` | 设备上 a2 的地址 |
| `group` | 分组（机房、教室、用途）。批量作业按它选设备。**不写就是默认组 `default`**——所以一份只有 IP 列表的台账也照样有分组，只是只有一个 |
| `labels` | 更细的标签（房间、型号）。`--label room=3-201` 这样选 |
| `enabled` | `false` 就是暂时不管它（放暑假、坏在备件间） |
| `probe` | 一条额外的命令，采集时顺便跑。它的输出原样进日报 |
| `token` | 这台设备自己的令牌（不给就用 `defaults.token` 或统一的那个） |

台账是**服务器上唯一的一份**。设备自己不知道有谁在管它们——这对设备是个好事：
设备坏了、被重装了，服务器改一行台账就行。

### 没有分组的时候

台账里不写 `group`，设备就属于**默认组 `default`**。这不是「一个叫空名字的组」——那个组
谁也选不中。规则很简单：

- 不写 `group` → 默认组 `default`；
- `--group default` 选的就是它们；
- `--group ""` 等同于 `--group default`；
- `--group lab` 只选 `lab` 里的设备，没写 group 的不在其中。

所以将来如果只给一份 IP 列表（部署环境不支持 mDNS，也就没有自动发现），台账就是：

```json
{ "devices": [ {"name": "10.0.0.31"}, {"name": "10.0.0.32"} ] }
```

分组还是能用，只不过只有一个组；灰度升级、分批、失败阈值那套策略一个字都不用改。

---

## 三、每天收集状态

```bash
python3 sched/fleet.py collect          # 问每台设备「你怎么样」
python3 sched/fleet.py report            # 今天的日报
```

采集是**并发**的（默认 8 台一批），一台设备不回答只是一行错误，不会拖垮其他 39 台。
结果落在：

```
~/.tea_agent/fleet/
  devices.json          台账
  history/2026-10-25.jsonl   每台设备每次采集一行
  reports/2026-10-25.md      给人看的日报
  audit.log             谁在什么时候对哪台设备做了什么
  token                 机群服务自己的令牌（600）
  device-token          访问设备用的令牌（600）
```

日报长这样：

```markdown
# 机群日报 2026-10-25

- 设备总数: 40
- 在线: 39    离线: 1
- 版本分布: 0.12.1 x38, 0.12.0 x1

| 设备 | 地址 | 状态 | 版本 | 运行时间 | 系统 | 磁盘可用 |
| --- | --- | --- | --- | --- | --- | --- |
| class-a-01 | 192.168.10.31 | 在线 | 0.12.1 | 72 小时 | Debian GNU/Linux | 7.6 GiB |
| class-a-07 | 192.168.10.37 | **离线** | - | - | - | HTTP 0: cannot reach ... |
```

### 每天自动跑

```bash
systemctl --user enable --now a2_fleet_collect.timer
```

每天 07:10 采集一次，报告写到 `~/.tea_agent/fleet/reports/`。错过了一次（服务器
那时关机）开机后会补跑一次。

---

## 四、批量升级

**别一次全推。** 一次坏掉的包会把 40 台设备一起弄坏。所以顺序是：

1. **金丝雀**（canary）：先在 1 台设备上试。**它失败了，剩下的设备一台都不碰**；
2. **分批**：剩下的按批推进（默认 5 台一批），批与批之间可以停一会儿；
3. **失败阈值**：失败率超过 `--max-failure-rate` 就停；
4. **回滚**：失败的设备跑 `--rollback` 指定的命令，不让它停在半升级状态；
5. **校验**：每台设备升完跑 `--verify`，它不过，这台就算失败。

```bash
# 先看一遍要动哪些设备（不碰任何设备）
python3 sched/fleet.py --group classroom-a upgrade --package nginx --dry-run

# 真的做：1 台金丝雀 → 每批 5 台 → 批间等 30 秒
python3 sched/fleet.py --group classroom-a upgrade \
    --package nginx --canary 1 --batch-size 5 --pause 30 \
    --verify "systemctl is-active nginx" \
    --rollback "apt-get install -y nginx=1.20.1-1"
```

包管理器是**在设备上探测**的（apt / dnf / yum / apk），所以一个机群里混着
Debian 板子和 Fedora 机器也没问题：

```bash
if command -v apt-get >/dev/null 2>&1; then apt-get install -y nginx; ...
```

跑完的每一步都在 `audit.log` 里，一行一个 JSON 对象：

```json
{"action": "device_done", "at": "2026-10-25T01:43:37Z", "device": "class-a-01",
 "ok": true, "exit_code": 0, "job": "job_20261025_014337_32sj2", "phase": "canary"}
```

---

## 五、让服务器上的 a2 来管

服务器上跑着 `a2.service`，它就是这 40 台设备的大脑。给它配一个机群服务地址，
它就多了一个 `toolkit_fleet` 工具：

```yaml
# ~/.tea_agent/tea_agent.yaml
settings:
  fleet_url: http://127.0.0.1:8290
  fleet_token: <机群服务的令牌>
```

（也可以用环境变量 `TEA_AGENT_FLEET_URL` / `TEA_AGENT_FLEET_TOKEN`。）

**没有配 `fleet_url`，这个工具根本不会出现在给模型的工具列表里**——和
`toolkit_search` 那种「provider 不支持就不给」的规矩一样：不让模型去试一个
它用不了的工具。

于是可以直接对服务器上的 a2 说：

> 「看看机群现在怎么样」
> 「把 3 号机房的 nginx 升到最新」
> 「class-a-07 掉线了，帮我看看怎么回事」

对应的是 `toolkit_fleet` 的几个 action：

| action | 干什么 |
| --- | --- |
| `status` | 每台设备现在怎么样 |
| `devices` | 台账 |
| `report` | 今天的日报 |
| `collect` | 现在就采集一次 |
| `exec` | 在选中的设备上跑命令 |
| `upgrade` | 升级一个包（带灰度策略） |
| `ask` | **让设备自己的 a2 回答**（见下） |

`exec` / `upgrade` 可以带灰度策略：`canary`、`batch_size`、`pause_s`、
`max_failure_rate`、`verify`、`rollback`、`dry_run`。

---

## 六、让设备自己判断（真正的多级）

有些问题脚本答不了：「这台机器的投影仪不亮了，帮我查查」。这时候
**不要**让服务器上的大模型去猜——服务器看不到那台机器的现场。让**设备自己的
a2** 去查：它有那台机器的日志、配置、工作区，它自己的 provider 和上下文。

```bash
python3 sched/fleet.py --group classroom-a ask \
    --prompt "检查投影仪服务是否在跑；如果不在，看日志找出原因并修复"
```

或者让服务器上的 a2 转达（`toolkit_fleet` 的 `ask` action）：

> 「让 3 号机房所有设备自己检查一下，投影仪有问题就修好」

机群服务只是**转达**：它把 prompt 发给设备的 `/api/chat`，收 SSE 流，
把每台设备自己的回答收上来。**设备怎么修，是设备自己决定的**——服务器不需要
知道那台机器的软件是怎么装的。

这就是「多级」的意思：**每一级都有自己的一颗脑子和自己的上下文**，
上一级做决策，下一级做执行，出了问题下一级自己处理。

---

## 七、三个入口

同一套能力，三个入口：

```bash
# 1. 直接跑（cron / systemd timer / 手工）
python3 sched/fleet.py --group lab exec --command "systemctl restart nginx"

# 2. 机群服务（服务器上的 a2、机器人都走这条）
curl -X POST http://127.0.0.1:8290/api/fleet/exec \
     -H "Authorization: Bearer $FLEET_TOKEN" -H "Content-Type: application/json" \
     -d '{"command": "systemctl restart nginx", "groups": ["lab"], "canary": 1}'

# 3. 机器人和人：走机群服务的 HTTP API
```

机群服务的接口（都带令牌）：

| 方法 | 路径 | 干什么 |
| --- | --- | --- |
| `GET` | `/api/fleet/health` | 服务自己还在吗 |
| `GET` | `/api/fleet/devices` | 台账 |
| `GET` | `/api/fleet/status` | 最近一次采集的结果 |
| `GET` | `/api/fleet/report` | 今天的日报（Markdown） |
| `GET` | `/api/fleet/history` | 某天的原始记录 |
| `GET` | `/api/fleet/audit` | 审计日志 |
| `GET` | `/api/fleet/jobs` | 最近的任务 |
| `GET` | `/api/fleet/job/{id}` | 一个任务的进度 |
| `POST` | `/api/fleet/collect` | 现在采集一次 |
| `POST` | `/api/fleet/exec` | 批量执行命令（返回 job id，异步） |
| `POST` | `/api/fleet/upgrade` | 批量升级（返回 job id，异步） |
| `POST` | `/api/fleet/ask` | 让设备自己的 a2 回答 |

批量作业是**异步**的：40 台设备要跑几分钟，HTTP 连接不该挂那么久。
`POST` 立刻返回一个 job id，用 `GET /api/fleet/job/{id}` 轮询。

---

## 八、装起来

### 服务器上

```bash
scripts/install-units.sh                 # 装单元
systemctl --user enable --now a2 a2_fleet
systemctl --user enable --now a2_fleet_collect.timer
```

```bash
mkdir -p ~/.tea_agent/fleet
cat > ~/.tea_agent/fleet/devices.json <<'EOF'
{ "devices": [ { "name": "class-a-01", "host": "192.168.10.31", "group": "classroom-a" } ] }
EOF
# 两个令牌，都是 600
openssl rand -hex 24 > ~/.tea_agent/fleet/token
openssl rand -hex 24 > ~/.tea_agent/fleet/device-token
chmod 600 ~/.tea_agent/fleet/token ~/.tea_agent/fleet/device-token
```

### 设备上

```bash
# 每台设备：开一个只属于机群的 a2 实例，绑到局域网，带令牌
a2 --host 0.0.0.0 --port 8282 --api-token "$(cat /etc/tea/fleet-token)"
```

token 要**一样**：机群服务访问设备时带的那个（`device-token`），
和设备自己 `--api-token` 收的那个，是同一个。

### 服务器上的 a2 接管群

```yaml
# ~/.tea_agent/tea_agent.yaml
settings:
  fleet_url: http://127.0.0.1:8290
  fleet_token: <~/.tea_agent/fleet/token 里的那个>
```

---

## 九、几条规矩

1. **先金丝雀，后全量。** 一次坏掉的包比一次慢的作业贵得多。
2. **升级要有回滚。** 没有回滚命令的升级，等于没有回滚。
3. **阈值要有。** `--max-failure-rate` 是最后一道闸，别关掉它。
4. **日常操作不进模型。** 采集、升级、重启，走 `/api/exec`。
   模型留给真正需要判断的事。
5. **设备不暴露在公网。** `--host 0.0.0.0` 只在受控的内网用。
6. **令牌分开。** 「谁能调用机群服务」和「机群服务拿什么去访问设备」是两个秘密。
7. **审计别删。** `audit.log` 是出事之后唯一能查的东西。

---

← 回到 [README](../README.md) · 接口字段见 [api.md](api.md) · 源文件见 [source.md](source.md)
