# a2_broker 容量与能力上限 —— 技术方案

> 对应需求：《a2_broker 需要增加默认端口池，默认支持 N 对 peers》（需求说明 v3，已定稿）。
> 本文是**实现方案**，不是需求复述；需求里的编号（R1-R11 / AC1-AC14 / NFR）在本文中直接引用。
>
> **语言约定**：本文件是中文文档；**程序运行期的所有输出用英文**
> （用户 2026-11-09 的明确要求：中文只留在文档、注释和对话里）。

---

## 0. 设计前提与假设

| # | 前提 | 来源 / 依据 |
| --- | --- | --- |
| A1 | 一个侧载 = 一个 ICE agent = **一个 TURN 分配 + 一个中继端口 + 一条信令连接** | `agent.c:357-450,972-975`（Allocate 在启动即发）；`server.c:933-945`（每分配一个 UDP socket + 一个池端口）；`tunnel_broker.cpp:793`（每连接一线程） |
| A2 | 一对 peers = 2 个侧载 → **2 个分配** | 房间模型：1 个 agent 侧载 + 1 个 client 侧载 |
| A3 | 默认 **256 对 = 512 分配 = 512 信令连接**，端口池 **49160-50175（1016 个）** | 需求 v3 §0 决策表 |
| A4 | 端口池容量必须 **≥ 2N**（N>0） | 一个分配占一个端口 |
| A5 | **所有 TURN 账号共用一个全局上限** `max_allocations = 2N`；每账号配额同为 2N | 需求 v3 §0 Q3 |
| A6 | 主流量是**压缩文字**，H264 desktop 极少用 → socket buffer 调到 64 KB 可接受 | 用户 2026-11-17 确认 |
| A7 | VPS 上 `LimitNOFILE=1024`、`TasksMax=1008`、`rmem/wmem_default=212992` 是**当前实况**，必须由单元与 sysctl 修正 | 线上实测（103.232.92.54） |
| A8 | **线协议不变**（STUN/TURN + 信令行协议） → 新 broker 对旧侧载完全兼容 | 本次不动协议 |

**边界**：本方案只改 broker 与其 vendored 依赖；**侧载（a2_tunnel）零改动**（唯一影响是 `juice.h` 头文件新增类型 → 需同版本重编，见 §7）。

---

## 1. 范围

### 做

1. 新增 `--max-peer-pair`（别名 `--max_peer_pair`）与"能力上限"模型（2N 派生）；
2. 默认端口池 49160-50175，默认分配上限/账号配额 512；
3. 命令行解析归一化（`--flag value` 与 `--flag=value` 等价）+ 启动期校验（fail fast）；
4. 三类耗尽的**可区分报错**（账号配额 / 全局上限 / 端口池无可用端口）；
5. 启动摘要 + 运行期容量快照（每 60 s）+ `--usage` 增列；
6. `scripts/a2_broker.service` 单元模板（`LimitNOFILE` / `TasksMax`）；
7. 文档同步（手册 / 准备清单 / ops 手册）。

### 不做（明确排除，避免过度设计）

| 不做 | 理由 |
| --- | --- |
| 新增进程、新增端口（如 HTTP 状态口） | 容量状态写进已有的 `a2_broker.db`，不引入新的攻击面与运维面 |
| 信令线程池化 | 512 线程在 2 GB 机器上可接受（RSS ~16 MB）；改造面大、风险高，另开 backlog |
| 每账号独立配额的 CLI | 需求 Q3 定的是"所有账号共用一个上限" |
| TURN over TLS（5349） | 与本次无关 |
| 池满时踢掉最老会话 | 需求 R5 定的是"拒绝新分配" |
| 端口占用自检（R11） | 需求里是 P2 可选；端口池留了 504 个余量，单点撞车不再破承诺 → 留作后续 |

---

## 2. 总体架构

**架构不变**：仍是**一个进程三件事**（STUN + TURN + 信令），不引入新组件、不改协议。

```
                 ┌──────────────── a2_broker (单进程) ─────────────────┐
                 │                                                      │
  侧载 A ──UDP──▶│ juice server (STUN+TURN)  ── 分配/中继 socket         │
                 │      ▲ cb_turn_usage  (已存在)                       │
                 │      ▲ cb_turn_error  (本次新增，错误分类)             │
                 │                                                      │
  侧载 A/B ─TCP─▶│ 信令线程(每连接一个) ── 房间/令牌/转发              │
                 │                                                      │
                 │  容量快照线程(每 60s) ──▶ stdout                    │
                 │  TurnUsageRecorder ──▶ a2_broker.db                 │
                 │      └ broker_meta 表 (本次新增：容量参数)           │
                 └──────────────────────────────────────────────────────┘
```

新增的只有三块：**解析与校验**（§3.1）、**错误分类回调**（§3.3）、**容量可观测**（§3.4/§3.5）。

---

## 3. 模块划分与设计

### 3.1 命令行解析与启动期校验（`src/tunnel_broker.cpp`）

现状：`main()` 里一个长 `if/else` 链（`tunnel_broker.cpp:585-616`），三个已知缺陷：
`--flag=value` 不认、非法数值 `std::stoi` 抛未捕获异常 → abort → systemd 崩溃循环、
没有任何"配置自洽性"校验。

**设计**：把参数解析从 `main()` 拆成三个可单测的纯函数 + 两个数据结构。

```cpp
// 参数（原始值）
struct BrokerOptions {
  uint16_t stun_port = 3478;
  uint16_t signal_port = 8382;
  std::string signal_bind = "0.0.0.0";
  uint16_t relay_begin = 49160;      // 需求 v3 §0
  uint16_t relay_end = 50175;        // 需求 v3 §0
  int max_peer_pair = 256;           // 需求 v3 §0；0 = 不限
  std::string realm = "a2";
  std::string token;
  std::string turn_user, turn_pass, turn_db;
  bool turn_user_given = false, turn_pass_given = false;
  int usage_days = 15;
  bool usage_only = false;
  bool use_tls = true;
  std::vector<std::string> cert_names;
};

// 能力（派生值，代码里只有这一处算）
struct Capacity {
  int peer_pairs = 0;        // 0 = 不限
  int allocations = 0;       // -> juice max_allocations（全局，跨账号共享）
  int quota_per_account = 0; // -> 每账号 allocations_quota
  uint16_t pool_begin = 0;
  uint16_t pool_end = 0;
  int pool_size = 0;         // end - begin + 1
};
```

**解析规则**：

| 规则 | 说明 |
| --- | --- |
| `--flag value` 与 `--flag=value` 等价 | 对所有取值型参数统一；实现上先按 `=` 切分再取下一个 argv |
| 别名 | `--max_peer_pair` ≡ `--max-peer-pair` |
| 布尔参数 | 只有 `--flag` 形式，不接受 `--flag=true` |
| 未知参数 | `broker: unknown option '--foo'` + 打印 usage + **退出 2** |
| 缺值 | `broker: --max-peer-pair needs a value` + **退出 2** |
| 非法数值 | **不抛异常**：`broker: --max-peer-pair needs a number, got 'abc'` + **退出 2**（消除 abort/崩溃循环） |

**数值安全**：所有数值参数改用 `strtol` + 显式区间检查，**不使用 `std::stoi`**（它会抛 `std::invalid_argument`，`main()` 无 try/catch → `std::terminate`）。这是 R4 的核心。

**校验规则**（`validate_options`，任一不满足 → 打印原因 → 退出 2，**不做静默降级**）：

| # | 规则 | 英文报错（运行期输出用英文） |
| --- | --- | --- |
| V1 | `relay_begin <= relay_end` | `the relay port pool is empty: --relay-begin 49672 is above --relay-end 49160` |
| V2 | 端口范围落在 49152-65535 | `the relay port pool must be inside 49152-65535` |
| V3 | 池与 `stun_port` / `signal_port` 重叠 → **只警告，不拒绝启动** | `the STUN port 49985 is inside the relay port pool 49160-50175 (that port cannot be used for a relay)` |
| V4 | `max_peer_pair >= 0` | `--max-peer-pair must not be negative` |
| V5 | N>0 时 `pool_size >= 2N` | `the relay port pool is too small: 513 ports for 512 allocations (--max-peer-pair 256 needs 1024); widen --relay-begin/--relay-end` |
| V6 | `--turn-user` 与 `--turn-pass` 要么都给要么都不给 | 现状已有，保留 |

> V1 是**必须新增**的：现状 `--relay-begin 49672 --relay-end 49160` 会让 `get_next_port_in_range`（`udp.c:27-43`）恒返回 begin → 所有分配抢同一个端口 → 第二个分配就 500，**静默且极难排查**。
>
> **V3 为什么只是警告**（实现时改的）：池子绑端口时本来就会跳过绑不上的端口
> （`udp.c:117-135`），所以"STUN 端口落在池子里"只会少一个可用端口，**不是致命冲突**。
> 早先把它做成启动错误，结果 `tests/smoke_tunnel_desktop.py` 随机选到的 STUN 端口
> 正好落在 49160-50175 里 → broker 拒绝启动 → 测试挂。降为警告后测试照过。

**派生（`derive_capacity`）**：

```
allocations       = (N > 0) ? 2*N : kUnlimitedAllocations   // kUnlimitedAllocations = 4096
quota_per_account = (N > 0) ? 2*N : 0
pool_size         = relay_end - relay_begin + 1
```

**为什么 N=0 时 `allocations` 不能填 0**：libjuice 把 `max_allocations == 0` 解释为"用默认 1000"
（`server.c:163-164`），那就不是"不限"而是"悄悄封顶 1000"。填 4096 才是真上限，
而实际约束落在端口池（1016 个）。`quota_per_account = 0` 在 libjuice 里表示"取 max_allocations"
（`server.c:214-215,326-327`），语义正好。

**账号共享语义（A5）如何落地**：libjuice 的全局 cap = `max(max_allocations, 各账号 quota)`；
因为我们让 `quota == max_allocations == 2N`，全局就是 2N，**跨账号共享**（需求 Q3）。
账号簿里新增多少账号都不会抬高全局上限。

### 3.2 启动流程（改后）

```
parse_options() ──失败→ stderr + 退出 2
   ↓
validate_options() ──失败→ stderr + 退出 2
   ↓
derive_capacity()
   ↓
令牌 → 账号簿 → TurnUsageRecorder::start()（开 DB + 写线程）
   ↓
写 broker_meta（容量参数，§3.5）
   ↓
start_stun_turn(stun_port, realm, accounts, pool_begin, pool_end, capacity, recorder)
   ↓
TLS 材料 → 信令监听
   ↓
打印启动摘要（R6）
   ↓
快照线程（每 60s） + accept 循环（forever）
```

**启动摘要**（英文，进 broker 日志）：

```
broker: capacity: 256 peer pairs (512 allocations max, shared by all TURN accounts)
broker: STUN+TURN on udp/3478 (realm a2, relay 49160-50175, 1016 ports)
broker: signalling on tcp/8382 (TLS, bound to 0.0.0.0)
```

现有那行 `broker: STUN+TURN on udp/3478 (realm a2, relay 49160-49260)` 改为带端口数。

### 3.3 错误分类（R5）—— 落点在 vendored libjuice

**问题**：三类耗尽都在 libjuice 的 server 线程里发生，broker 现在**一个都看不到**
（broker 只注册了 `cb_turn_usage`；libjuice 的默认日志是 WARN 级、输出到 stdout，
文案是 libjuice 自己的措辞，且无结构、无节流）。

**三个失败点**（`3rd/libdatachannel/deps/libjuice/src/server.c`）：

| 类 | 触发点 | STUN 应答 | 现状 |
| --- | --- | --- | --- |
| `SERVER_QUOTA` | `find_allocation()` 返回 NULL（`server.c:892-897`） | 486 | 静默 |
| `ACCOUNT_QUOTA` | `credentials->allocations_quota <= 0`（`server.c:922-926`） | 486 | 静默 |
| `RELAY_PORT` | `udp_create_socket()` 失败（`server.c:933-941`） | 500 | 只有 libjuice 的 WARN |

**设计**：与已有的 `cb_turn_usage` 同构，新增一个错误回调（本仓已在同一个文件里打过
tea_a2 补丁，有先例，改动可控）：

```c
// juice.h（新增，紧挨 cb_turn_usage 那一段）
typedef enum juice_turn_error_reason {
    JUICE_TURN_ERROR_SERVER_QUOTA = 0,  // 486: the server has no allocation left
    JUICE_TURN_ERROR_ACCOUNT_QUOTA,      // 486: the account has no allocation left
    JUICE_TURN_ERROR_RELAY_PORT          // 500: no free port in the relay range
} juice_turn_error_reason_t;

typedef struct juice_turn_error {
    juice_turn_error_reason_t reason;
    const char *username;   // may be NULL
    const char *client;     // "ip:port"
} juice_turn_error_t;

typedef void (*juice_cb_turn_error_t)(const juice_turn_error_t *error, void *user_ptr);
// juice_server_config_t 新增：cb_turn_error / turn_error_user_ptr
```

**约束**：回调在 server 线程里调用 → **不得阻塞**（与 `cb_turn_usage` 同一条纪律）；
broker 侧只做"计数 + 限流 + 打一行日志"，不碰 IO。

**broker 侧文案与限流**：

| 类 | 文案 | 限流 |
| --- | --- | --- |
| SERVER_QUOTA | `broker: the relay is full: 512 allocations are in use (the limit is 512)` | 同类 5 分钟一条 + 计数 |
| ACCOUNT_QUOTA | `broker: the relay is full for the account '1hblsqt.': its quota of 512 is used up` | 同上 |
| RELAY_PORT | `broker: no free relay port in 49160-50175 (the pool is exhausted)` | 同上 |

**没有兜底**（实现时删掉了）：broker 和 libjuice 是同一个仓、一起编出来的，
回调**永远**是接上的；再加一套"按计数器猜"的逻辑只会是没人走的死代码。
要防的是"回调没接上"这种情况，而它不可能发生。

**取舍说明**：为什么不选"只在 broker 侧推断"？——推断只能知道"满了"，分不清是账号配额
还是全局上限、也不知道是哪个客户端；而且 pool 耗尽（RELAY_PORT）与配额满的处置不同
（前者要查防火墙/端口冲突，后者要扩容）。

### 3.4 运行期容量快照（R7）

**新线程**，每 60 s 打一行（英文）：

```
broker: capacity: allocations 118/512 (23%), relay ports 118/1016, signalling 59, fds 191, rss 24 MB
```

| 指标 | 来源 | 备注 |
| --- | --- | --- |
| 在用分配 | `TurnUsageRecorder::open_sessions()`（已存在，`tunnel_usage.h:138-141`） | 写入线程异步 → 有毫秒级滞后，可接受 |
| 端口池占用 | 同上（1 分配 = 1 端口） | |
| 信令连接数 | `g_rooms` 里 Peer 总数（加 `g_rooms_mu`） | 需新增一个 3 行的计数 helper |
| fd 数 | Linux：`/proc/self/fd` 计数；Windows：`GetProcessHandleCount` | 新 helper，头文件 `src/tunnel_proc_stat.h`（header-only，与 `tunnel_usage.h` / `tunnel_turn_db.h` 同风格） |
| RSS | Linux：`/proc/self/statm`；Windows：打印 `n/a` | Windows 上 broker 只是开发/测试用，不必接 psapi |

**取舍**：不引入"每 N 秒写 DB"（DB 是给 `--usage` 用的，实时性由日志承担）；
日志一行一行看即可，也能在告警系统里直接 grep。

### 3.5 容量元数据落库 + `--usage` 增列（R9）

**问题**：`--usage` 是**独立进程**（`--usage` 时只读 DB、不启动任何服务，
`tunnel_broker.cpp:620-658`），它拿不到运行中 broker 的内存 → "当前分配数 / 端口池上限"
在 `--usage` 里无从获得。

**设计（不加端口）**：broker 启动时把容量参数写进同一个 `a2_broker.db` 的新表：

```sql
CREATE TABLE IF NOT EXISTS broker_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
```

| key | 值 | 写入时机 |
| --- | --- | --- |
| `peer_pairs` | `256` | 启动 |
| `max_allocations` | `512` | 启动 |
| `quota_per_account` | `512` | 启动 |
| `relay_begin` / `relay_end` | `49160` / `50175` | 启动 |
| `pool_size` | `1016` | 启动 |
| `broker_version` | `a2_broker 1.x.y` | 启动 |
| `started_at` | unix 秒 | 启动 |

**写入方式**：复用 `TurnUsageRecorder` 的事件队列（新增 `meta` 事件类型），
由**已有的 writer 线程**落库 → 不新增 DB 连接、不新增锁、不新增线程。

**`--usage` 输出新增段**（在现有明细表之后）：

```
capacity: 256 peer pairs (512 allocations max, shared by all accounts)
          relay pool 49160-50175 (1016 ports)
now:      118 allocations in use (23% of 512), 118 of 1016 relay ports in use
```

- "在用分配数"从 `turn_usage` 里数 `ended_at IS NULL` 的行（**现成逻辑**，`tunnel_usage.h:212-219`）；
- 老 DB 没有 `broker_meta` → 打印
  `capacity: unknown (this book was written by an older broker)`，**不报错**（`read_report`
  已容忍"no such table"，`tunnel_usage.h:182-186`）。

**兼容性**：新表是**追加**的 → 旧二进制读同一个 DB 完全不受影响（回滚安全，见 §8）。

### 3.6 systemd 单元模板（R10）

新增 `scripts/a2_broker.service`（仓里此前只有文档片段，没有模板文件）：

```ini
[Unit]
Description=tea_a2 tunnel broker (STUN + TURN + signalling)
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
ExecStart=%h/.tea_agent/tunnel/a2_broker --stun-port 3478 --signal-port 8382 --signal-bind 0.0.0.0 --realm a2 --max-peer-pair 256 --relay-begin 49160 --relay-end 50175
WorkingDirectory=%h/.tea_agent/tunnel
Environment=HOME=%h
Restart=always
RestartSec=3s
LimitNOFILE=2048
TasksMax=2048
StandardOutput=append:%h/.tea_agent/logs/a2_broker.log
StandardError=append:%h/.tea_agent/logs/a2_broker.log

[Install]
WantedBy=default.target
```

- `LimitNOFILE=2048`：512 信令 + 512 中继 + 若干（实测需 ~1030，超 1024）；
- `TasksMax=2048`：512 信令线程 + broker 自身线程（systemd 默认 1008 会撞顶）。

### 3.7 内核参数（不属于代码，落在文档与运维手册）

```ini
# ~/.config/sysctl.d/99-a2-broker.conf
net.core.rmem_default = 65536
net.core.wmem_default = 65536
```

影响：1024 个 socket 的内核内存从 ~425 MB 降到 ~128 MB（A6 已确认可接受）。
**注意这是主机级设置**，同机 a2/a2_task 带宽很小，可忽略。

---

## 4. 接口定义

### 4.1 CLI

```
a2_broker [--version] [--help]
           [--stun-port N] [--signal-port N] [--signal-bind ADDR]
           [--realm S] [--relay-begin N] [--relay-end N]
           [--max-peer-pair N | --max_peer_pair N]
           [--token S] [--turn-user U] [--turn-pass P] [--turn-db FILE]
           [--usage] [--usage-days N] [--cert-name NAME]... [--no-tls|--tls]
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--relay-begin` | 49160 | 中继端口池起点 |
| `--relay-end` | 50175 | 中继端口池终点（池容量 1016） |
| `--max-peer-pair` / `--max_peer_pair` | 256 | 能力上限（N 对）；0 = 不限（端口池实际上限） |

**退出码**：`0` 正常；`1` 运行时失败（端口占用、证书不可用、DB 打不开）；`2` 参数/配置错误。

### 4.2 日志与错误文案（全部英文）

| 场景 | 文案 |
| --- | --- |
| 启动摘要 | `broker: capacity: 256 peer pairs (512 allocations max, shared by all TURN accounts)` |
| 启动 STUN/TURN | `broker: STUN+TURN on udp/3478 (realm a2, relay 49160-50175, 1016 ports)` |
| 快照 | `broker: capacity: allocations 118/512 (23%), relay ports 118/1016, signalling 59, fds 191, rss 24 MB` |
| 全局满 | `broker: the relay is full: 512 allocations are in use (the limit is 512)` |
| 账号满 | `broker: the relay is full for the account '1hblsqt.': its quota of 512 is used up` |
| 端口池空 | `broker: no free relay port in 49160-50175 (the pool is exhausted)` |
| 校验失败 | `broker: the relay port pool is too small: 513 ports for 512 allocations ...` |

### 4.3 数据模型

| 结构 | 位置 | 说明 |
| --- | --- | --- |
| `BrokerOptions` | `src/tunnel_broker.cpp` | 原始参数 |
| `Capacity` | `src/tunnel_broker.cpp` | 派生能力（2N 派生的唯一落点） |
| `broker_meta` 表 | `a2_broker.db` | 容量参数（新增表，追加式） |
| `UsageEvent` | `src/tunnel_usage.h` | 扩展一个 `meta` 事件类型（key/value） |
| `juice_turn_error_t` | `3rd/.../juice/juice.h` | 错误分类回调载荷 |

---

## 5. 关键流程

### 5.1 分配成功

侧载 ICE → Allocate → libjuice：查分配 → 查配额 → `udp_create_socket()`（占一个池端口）→
`cb_turn_usage(ALLOCATED)` → 记 DB → 应答 101。**与今天一致，无行为变化。**

### 5.2 容量耗尽（第 513 个 Allocate）

libjuice → `find_allocation()` 返回 NULL → 486 + `cb_turn_error(SERVER_QUOTA)` →
broker 打一行（限流）→ 侧载 ICE 拿不到 relay 候选 → 若直连也不通 → **隧道起不来**，
侧载日志出现 `TURN allocation failed`。**broker 侧无副作用，已有 512 个会话不受影响。**

### 5.3 端口池真的没有可用端口

`udp_create_socket()` 在池内轮转找空闲端口（`udp.c:117-135`），单点被占会自动跳过；
**只有池里 1016 个端口全被占**才会失败 → 500 + `cb_turn_error(RELAY_PORT)`。
现实触发条件：宿主机有服务占了这个范围（部署前 `ss -lun` 检查，NFR-5）。

### 5.4 迁移（线上）

```
1) 云安全组 + ufw 放行 49160:50175/udp      ← 先开端口
2) sysctl 写 99-a2-broker.conf 并生效
3) 换新二进制 + 新单元（LimitNOFILE/TasksMax）→ systemctl --user restart a2_broker
4) 看启动摘要与第一行快照，确认 capacity 数字正确
5) 灌真实流量
```

> 顺序不能反：先换二进制后开端口 → Allocate 仍会成功（端口在本机 bind 成功），
> **中继静默不通**（Allocate 成功、数据不通），是最难排查的故障形态。

---

## 6. 失败场景与降级

| 场景 | 表现 | 设计上的处置 |
| --- | --- | --- |
| 防火墙没开新端口 | Allocate 成功、隧道无数据 | 启动摘要打印端口范围 + 文档写明顺序；`--usage` 的 bytes 长期为 0 可佐证 |
| 容量满 | 新 Allocate 486，隧道起不来 | 分类报错 + 限流；broker 存活，已有会话不受影响 |
| 端口池被宿主机占 | 少量分配 500 | `RELAY_PORT` 分类 + 部署前 `ss -lun`（NFR-5） |
| DB 写失败 | 记账丢失 | **不影响转发**（recorder 的 writer 线程失败只打日志，现状如此，保持） |
| sysctl 没调 | 内存 ~460 MB，1 GB 机器 OOM → broker 被杀 → 侧载重连风暴 | 单元模板 + 文档；快照里的 rss 字段可提前发现 |
| broker 被杀 | 侧载信令断 | 侧载现有逻辑自动重连并重新协商（`tunnel_sidecar.cpp:1564-1584`），**无需改动** |
| 参数写错 | 旧行为：abort + 崩溃循环 | 新行为：退出 2 + 明确原因（R4） |
| 端口范围写反 | 旧行为：静默抢同一端口，第二个分配 500 | 新行为：启动即拒绝（V1） |

---

## 7. 影响范围

| 文件 | 改动 | 风险 |
| --- | --- | --- |
| `src/tunnel_broker.cpp` | 参数解析重构 + 新参数 + 校验 + 容量派生 + 启动摘要 + 快照线程 + 错误回调接线 | 中（main() 结构调整） |
| `src/tunnel_usage.h` | `meta` 事件 + `broker_meta` 表 + `--usage` 增列 | 低（追加式） |
| `src/tunnel_proc_stat.h`（新建） | fd 数 / RSS 的跨平台取值 | 低 |
| `3rd/libdatachannel/deps/libjuice/include/juice/juice.h` | 新增错误回调类型与配置字段 | 低（仅 broker 使用该结构） |
| `3rd/libdatachannel/deps/libjuice/src/server.c` | 三个失败点各加一次回调 | 低（与已有 tea_a2 补丁同风格） |
| `scripts/a2_broker.service`（新建） | 单元模板 | 低 |
| `docs/tunnel_broker_manual.md` / `docs/tunnel_broker.md` / `docs/ops_manual.md` | 端口表、参数表、容量口径、sysctl、迁移顺序、排错表 | 低 |
| 线上 VPS | 防火墙、sysctl、单元、二进制 | **中**（顺序敏感，见 §5.4） |

**对侧载（a2_tunnel）的影响**：**零代码改动**。`juice.h` 新增的是**新类型**（不动已有结构体布局，
`juice_server_config_t` 只有 broker 用），协议不变 → **旧侧载与新 broker 互相兼容**。
唯一影响：仓里 libjuice 变了 → 侧载需要随版本一起重编（发版流程本来就是这样）。

---

## 8. 回滚

| 步骤 | 回滚动作 |
| --- | --- |
| 1 | **先改单元**：去掉 `--max-peer-pair` / `--relay-begin` / `--relay-end`（旧二进制不认这些参数，会 `unknown option` 退出 2 → 起不来） |
| 2 | 换回旧二进制 → `systemctl --user restart a2_broker` |
| 3 | 防火墙**不用收**（旧 broker 只用 49160-49260，多开的端口无害） |
| 4 | sysctl**不用回**（只影响 socket buffer，不影响正确性） |
| 5 | `a2_broker.db` 里的 `broker_meta` 表**不用删**（旧二进制根本不看它） |

**回滚代价**：一次二进制替换 + 一次 restart；**无数据迁移、无协议影响**。
**回滚时唯一要记住的**：先改单元再换二进制（顺序反了会起不来）。

---

## 9. 验证方法

### 9.1 新增自动化测试（建议 `tests/smoke_broker_capacity.py`）

风格对齐 `tests/smoke_mail_watch.py`（协议级、纯标准库、不碰真网络）：

| # | 用例 | 断言 | 实现结果 |
| --- | --- | --- | --- |
| T1 | `--help` | 列出新默认值（256 / 49160 / 50175） | ✅ |
| T2 | `--max-peer-pair 2 --relay-begin 49160 --relay-end 49180` | 启动成功；摘要含 `4 allocations max` 与 `21 ports` | ✅ |
| T3 | `--max-peer-pair 512`（默认池 1016 < 1024） | **退出 2**，stderr 含 `too small` | ✅ |
| T4 | `--relay-begin 49672 --relay-end 49160` | **退出 2**，stderr 含 `empty` | ✅ |
| T5 | `--max-peer-pair abc` | **退出 2**，**不 abort** | ✅ |
| T6 | `--max-peer-pair=512` vs `--max-peer-pair 512` | 行为一致 | ✅ |
| T7 | 裸 STUN Allocate × 5 | 前 4 个成功（101），第 5 个得 **486** | ✅ |
| T8 | broker 日志 | 含 `the relay is full` 与 `4 allocations are in use` | ✅ |
| T9 | `--usage` | 含 `capacity: 2 peer pairs` 与 `now:` 行 | ✅ |

一共 **13 条断言**（`tests/smoke_broker_capacity.py`，协议级、纯标准库）。

（Allocate 的最小实现：STUN Binding Request，`REQUESTED-TRANSPORT: UDP`，
`USERNAME`/`REALM`/`NONCE`/`MESSAGE-INTEGRITY` 按 RFC 5766 走 —— 长term 凭据的
密钥是 `MD5(user:realm:pass)`，HMAC-SHA1 覆盖到 MESSAGE-INTEGRITY 之前的字节。）

> **测试里的一处环境适配**：broker 的 STUN 端口是 IPv6 socket（libjuice 优先 IPv6），
> 而 **WSL2 不会把 IPv4 回环包交给它**（普通 Linux 会）。所以测试先探一下
> `127.0.0.1` 通不通，不通就用 `::1`。线上 VPS（普通 Linux）不受影响。

### 9.2 线上验证（AC12）

在 VPS 上：起 broker → 用压测脚本并发 512 个 Allocate → 记录 RSS / fd / CPU
→ 与需求 NFR-6 的预期值（~170 MB / ~1030 fd）比对 → 写进 `docs/tunnel_broker_manual.md`。

### 9.3 端到端回归

复用 `.tea_agent_run/scripts/tunnel_e2e.sh`（外网 ⇄ broker ⇄ 内网），
确认改造后隧道仍能建、且侧载日志的 `path: ...` 行符合预期（直连 / through the broker）。

---

## 10. 关键取舍

| 取舍 | 选择 | 放弃的方案与原因 |
| --- | --- | --- |
| 容量状态怎么暴露 | 写进已有的 `a2_broker.db`（新表）+ 日志快照 | **新增 HTTP 状态端口**：多一个端口、多一个攻击面、多一份运维文档；且 `--usage` 本来就是离线读 DB 的形态 |
| 错误分类怎么拿到 | 给 vendored libjuice 加一个回调（本仓已有补丁先例） | **只在 broker 侧推断**：分不清账号配额/全局上限/端口池三类，也拿不到是哪个客户端；**依赖 libjuice 默认日志**：文案不可控、无法限流 |
| `main()` 要不要拆 | 拆成 parse / validate / derive 三个纯函数 | **继续堆 if/else**：新参数只会更多，校验无处安放；**拆成独立文件**：这个体量不值得多一个编译单元 |
| 信令线程 | 维持"一连接一线程" | **线程池**：512 线程 RSS 仅 ~16 MB，改造面大；留 backlog |
| 端口池默认值 | 1016（2 倍余量） | **513（零余量）**：宿主机撞一个端口就破"256 对"的承诺 |
| N=0 的语义 | `allocations=4096` 显式上限 + 端口池为实际上限 | **填 0**：libjuice 会当成"默认 1000"，变成悄悄封顶 |

---

## 11. 实现阶段的开放项（不阻塞）

| # | 项 | 说明 |
| --- | --- | --- |
| 1 | 云安全组单规则能否开 1016 个 UDP 端口 | 需求 v3 §10 事项 A；不行则拆两条规则（49160-49672 / 49673-50175） |
| 2 | 512 分配在位时的实测 RSS/fd/CPU | 需求 v3 §10 事项 B；验收 AC12 |
| 3 | 端口占用自检（R11） | P2 可选；实现成本低（扫一遍 `/proc/net/udp`），可在第二期做 |
| 4 | broker 优雅退出 | 现状是 SIGTERM 直接死（accept 循环阻塞）；与本次无关，另议 |

---

## 12. 实现与本方案的偏差（实现时记的）

| # | 偏差 | 原因 |
| --- | --- | --- |
| 1 | **V3 从"报错"改成"警告"** | 池子本来就会跳过绑不上的端口，重叠不是致命冲突；早先做成错误会让随机选端口的测试起不来（见 §3.1 的说明） |
| 2 | **删掉"计数器兜底"** | broker 和 libjuice 一起编，回调永远接得上，兜底是死代码 |
| 3 | **参数表多了一行 `--turn-pass` 单独给** | 保留旧行为（用户仍是内置账号），没有新增限制 |
| 4 | **测试从 7 条变成 13 条断言** | 拆分更细：端口池大小、错误分类、记账各自分开验 |
