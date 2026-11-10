# 隧道（侧载）：让外网访问内网里的 tea_a2

> 「客户设备都在内网，我们运维现在只能远程桌面（ToDesk/RustDesk）进去，再点内网的 tea_a2。
> 能不能搞一个单二进制，走 WebRTC 数据通道、STUN 打洞，代理 a2 的 HTTP 传输，
> 这样外网就能直接访问内网的 a2 了？」
>
> **2026-11-09 现场实测（zonekey，192.168.12.12，36.112.70.164）：**
> 客户侧网络出口是**家用路由器**；UDP 出站正常（STUN 问得通）；
> 但 NAT 是**对称的**（同一个 IP 换个端口，映射端口就变），
> **教科书式 STUN 打洞实测失败**（客户端发 23 个打洞包，0 回包；对端收到 0 包）。
>
> → **隧道的可靠主路径是「中继（TURN）」，直连降级为「能直连就直连」的优化。**
> 详见 §四。

**结论：可行，而且 P2P 直连是主路径。** 按「**a2 不依赖隧道**」这条原则，
隧道是一个**侧载（sidecar）**，不是 a2 的一个模块。

本页是可行性结论 + 设计草案 + 已做的实测。实测部分全部在本机跑过，可以复现
（见 [tools/tunnel_probe/](../tools/tunnel_probe/)）。

---

## 一句话

```
内网 a2  ──(本地 HTTP)──▶ 内网侧载 ⇄══════════════⇄ 外网侧载 ──▶ 运维同事的 a2 / 浏览器
                              │   WebRTC 数据通道（打洞后端到端直连，DTLS 加密）
                              │   断了才走 公网 broker 中继
                              └── broker 只做信令 + STUN（几百字节，看不到内容）
```

**a2 自己一行代码都不用改**：内网那台只被本地代理访问，外网那台只多一条 `peers:` 配置。

---

## 一、第一原则：a2 不依赖隧道

这是设计约束，不是偏好。它带来四个好处：

| 好处 | 说明 |
| --- | --- |
| a2 的依赖不变 | 不用为了隧道往 a2 里塞 libdatachannel/usrsctp，**a2 的二进制体积和依赖都不动** |
| 版本冲突消失 | 见 §六：libdatachannel 要 mbedtls ≥ 3，而 a2 自带的是 2.28.9——侧载带自己的一份，两边井水不犯河水 |
| 隧道挂了 a2 照常 | 侧载是独立进程，崩了、重启了、内网没公网了，a2 都还是原来那个 a2 |
| 客户可以不要它 | 内网设备上只放 a2；要不要外网访问，是运维侧的决定，不是设备出厂的决定 |

具体到两端，**a2 侧零改动**：

- **内网那台**：侧载连 `http://127.0.0.1:8383`（a2 自己的端口），
  a2 甚至不需要 `--api-token`，除非要用 `/api/peer/*` 镜像。
- **外网那台（运维同事的 a2）**：`settings.peers:` 多一条，指向侧载在本机开的代理口：

  ```yaml
  settings:
    peers:
      customer-plant-a:
        url: http://127.0.0.1:9931        # 侧载在本机开的本地代理
        token: <内网 a2 的 --api-token>     # 可选：走 /api/peer/* 镜像
        description: 客户工厂那台内网设备
  ```

  `toolkit_peer` 的代码一个字都不用动——它本来就只会说 HTTP。

---

## 二、三个角色

| 角色 | 跑在哪 | 是什么 | 要不要公网 IP |
| --- | --- | --- | --- |
| **侧载 · 内网端**（`a2_tunnel --agent`） | 客户内网设备上，a2 旁边 | 主动外连；把内网 a2 的 HTTP 塞进隧道 | 不需要 |
| **broker**（`a2_broker`） | 运维方或客户的公网机器（**VPS**） | **STUN 服务器 + 信令服务器 + TURN 中继**（一个进程） | 需要 |

**broker 的端口怎么摆**：

| 端口 | 协议 | 谁在用 | 说明 |
| --- | --- | --- | --- |
| **3478** | UDP | broker 自己 | STUN + TURN 中继。**必须是标准口**——ICE 就是靠它发现中继 |
| **8382** | TCP | nginx stream | **信令的公网口（TLS）** |
| 3479 | TCP | 仅本机 | broker 的明文信令口，**只绑 127.0.0.1**，外面碰不到 |

信令里带着房间令牌，所以公网那一口**必须是 TLS**：nginx 的 stream 模块终结 TLS，
后面转给 broker 的本机明文口（模板：`scripts/nginx-stream-tunnel.conf`）。

> **端口号不是防线，TLS 才是。** 把 3479 换成 8382 确实避免了「和 STUN/TURN 的标准口
> 挨太近、扫描器容易认出」这种噪音（3479 本来就是 TURN-over-TCP 的惯例口），
> 但真正的防线是**令牌**：没有令牌，join 请求会被直接掐掉。
> 而原方案真正的毛病是**信令走明文 TCP → 令牌在网上裸奔**——那个才是要修的。
| **侧载 · 外网端**（`a2_tunnel --client`） | 运维同事的机器上 | 从隧道里取 HTTP，在本机开一个 `127.0.0.1:9931` 代理 | 不需要 |

**broker 现在很轻**：P2P 打通时它只转发几百字节的 SDP/ICE 候选，
**流量不经过它、内容它也看不到**（数据通道自己是 DTLS 加密的）。
打洞失败时它才顺手当中继——而按实测，**中继是常态路径**。

> **broker 要准备什么（VPS 的规格、端口、验收）**：见 [tunnel_broker.md](tunnel_broker.md)。
> 一句话：**1 核 1 GB 就够**，要开 **443/TCP（信令）、3478/UDP（STUN）、
> 443/UDP（STUN 备用）、5349/TCP（TURN over TLS，UDP 全封时的救命通道）、
> 49160-49260/UDP（TURN 中继端口池）**；香港/新加坡的轻量 VPS 最省事（免 ICP 备案）。

> **STUN 服务器也放在 broker 上，别用公共的。**
> 公共 STUN（stun.l.google.com 之类）在企业网里经常被屏蔽、限速甚至劫持，
> 而且客户网络能不能出网到公共 STUN 是另一回事。broker 顺带做 STUN，
> 内网侧载只要能 UDP 到 broker 就行——这正是打洞必须的第一步。
>
> **好消息：broker 不用额外装 coturn。** libjuice 自带 server 模式
> （`juice_server_create`），同一个库既能当 **STUN 服务器**，
> 给凭据就能开 **TURN 中继**（`max_allocations` / `relay_port_range` 都现成）。
> 所以 broker 是**一个进程、三种角色**：STUN 服务器 + 信令服务器 +（可选）TURN 中继。
> 建议：信令走 **TCP 443**（一定通），STUN 同时监听 **UDP 3478 和 UDP 443**
> （有些网络只放行 443）。

### 三种使用形态

1. **运维同事的 a2 驱动内网 a2**（最省事）：
   外网侧载开 `127.0.0.1:9931`，运维 a2 的 `peers:` 指过去 → `toolkit_peer` 照常问它。
   这也正是 [peer.md](peer.md) 那套级联：**级联早就设计好了，只是缺一条能穿过 NAT 的腿。**

2. **运维同事的浏览器直接开内网 a2 的 Web UI**：
   外网侧载除了代理 `127.0.0.1:9931`，也可以只监听本机、由运维同事用 SSH 端口转发看一眼；
   或者由 broker 按设备开一个带令牌的公网 HTTPS 入口（这时流量走中继）。

3. **两边都是浏览器/别的工具**：broker 暴露 `https://<device>.<broker>/`，令牌进 URL 或 header。

---

## 三、主路径与兜底路径（实测后调整过）

| | 路径 | 什么时候 | 代价 |
| --- | --- | --- | --- |
| **主路径（可靠）** | 内网侧载 → broker → 外网侧载（**TURN 中继**） | **实测证明打不通**，所以这是常态路径 | 多一跳；broker 转发的是 **DTLS 密文**，看不到内容 |
| **优化** | 内网侧载 ⇄ 外网侧载（ICE 直连） | 网络允许打洞时（部分企业网、部分家用宽带） | 延迟最低；流量完全不过第三方 |

**两个模式的切换不用我们手写**——ICE 的候选类型里本来就有 relay(TURN)：
侧载 ICE 配置 = `stun:broker` + `turn:broker(带凭据)`，
ICE 自己挑（直连候选能用就用直连，否则用中继）。

实现上，两种模式用**同一套隧道协议**：数据通道里跑的就是 HTTP。
外层换的是「字节从哪来」，里面那一层完全一样。

**而且「回退」不是我们手写的 if/else——ICE 自己会做。**
ICE 的候选类型里本来就有 relay（TURN）候选：只要给侧载配一个 TURN 服务器，
ICE 打洞失败时会自己选中继候选。也就是：

> 侧载的 ICE 配置 = `stun:broker` + `turn:broker(带凭据)`，
> **P2P 优先、TURN 兜底，切换由 ICE 完成**，我们要做的只是「broker 开 TURN」。

这比原计划（自己写中继模式 + 自己写切换）少一大块代码，也少一类 bug。

**回退必须是自动的、对用户无感的**：侧载先试打洞，超时（比如 5 秒）没成就切中继，
并在日志里写一行「P2P 没打通，走中继」。运维同事不该为此手工做任何事。

---

## 四、客户侧的网络画像：家用路由器出口

客户那边的设备挂在**一台家用路由器**后面，这是好消息也是要注意的地方。

### 三个条件，实测只满足了一个

| 条件 | 实测结果 |
| --- | --- |
| ① 内网侧 NAT 是 cone 类 | ❌ **实测是对称的**（同一个 IP 换个端口，映射端口就变：27889 → 46693） |
| ② 外网侧能被 ICE 发现 | ✅ 外网那台是 cone（所有 STUN 看到同一映射端口） |
| ③ 两边能 UDP 到 broker | ✅ 双方 UDP 出站都通 |

**打洞实测：失败（三种组合，全灭）。**

| 组合 | 结果 |
| --- | --- |
| 内网当 agent、外网当 client，单向打 | 23 个包，**0 回包**；对端收到 0 个 |
| 外网当 agent、内网当 client，单向打 | 25 个包，**0 回包**；对端收到 0 个 |
| **两边同时主动打**（真实 ICE 的做法，双方都知道对方地址） | 各 100 个包，**0 回包** |

> **第一轮测试本身有缺陷，已修正**：那时内网侧是「被动等回包」，
> 它不知道外网在哪，所以没有主动打——而真实的 ICE 是**双方都主动打**。
> 修正后（两边交换 token、各自同时主动打）**结果不变**。
>
> 另外，「内网侧收到 0 个包」这件事本身也说明：光靠 NAT 类型不够，
> **运营商 CGNAT 很可能直接丢弃外来的 UDP**（或者过滤规则严格到端口级）。
> 这一层我们控制不了——只能靠中继绕开。

> **所以「客户支持 NAT 打洞」这句话要拆开看**：UDP 出站是通的（不然 STUN 都问不到），
> 但**「NAT 类型」不是 cone，而且入站 UDP 可能被运营商直接丢弃**。
> 家用宽带 + 家用路由器 → 运营商侧很可能还有一层 CGNAT（100.64.x.x）→ 对外表现就是对称 NAT。
> **这是国内家庭宽带的常态，不是配置问题。**
>
> 这不是坏消息——**它只是把架构的主次换了**：中继必须第一天就有（见 §九），
> 直连变成锦上添花。隧道该解决的问题（外网访问内网 a2）一点没受影响。

### 但「家用路由器」不等于「没有运营商 NAT」

这是最常见的翻车点：**家用路由器 + 运营商 CGNAT（100.64.x.x）= 对称 NAT**，
从设备上看是「家用路由器」，从公网看是对称的。家用宽带里移动/部分地区的 CGNAT 比例不低。

**唯一的判据是实测**（见 [tunnel_ops.md](tunnel_ops.md)）。

> **判据本身有个坑，已经踩过并修正。**
> 2026-11-09 在 36.112.70.164 那台实测了两次，两次都是：
>
> ```
> 映射端口 stun.l.google.com:19302:41904 / 59083,  stun1.l.google.com:19302:41904 / 59083,
>           stun.cloudflare.com:3478:44839 / 10532
> ```
>
> **第一版工具报「symmetric」，这个结论下早了**：`stun.l.google.com` 和
> `stun1.l.google.com` 是**同一个 IP**（74.125.250.129，Google 的 anycast），
> 它们端口一样是必然的，什么也证明不了。真正有信息的只有「换 IP 就换端口」这一条
> → 正确分类是 **address-dependent（映射依赖目标地址）**，
> 不是 symmetric（连端口都依赖目标）。
>
> （两次跑映射端口不同是正常的：每次本地源端口不同。
> 两次「Google 一个端口、Cloudflare 另一个端口」的**形态**一致，说明结论可复现。）
>
> **测 symmetric 的唯一有效对照是「同一个 IP 的不同端口」。**
> `nat_probe.py` 现在默认带上了 `stun.nextcloud.com:3478` 和 `stun.nextcloud.com:443`
> （同一个 IP 46.225.95.169 的两个端口），拿这条对照就能给出准话。
>
> 结论分级（工具会直接打印「含义」）：
>
> | 判定 | 含义 | 该怎么办 |
> | --- | --- | --- |
> | `cone` | 映射端口与目标无关 | 打洞基本必成 |
> | `address-dep` | 换目标 IP 就换端口 | 打洞**不一定**成（看过滤规则），**必须实测**；不通就走 TURN |
> | `symmetric` | 同一个 IP 换端口也换映射 | 打洞大概率失败 → **隧道必须能走 TURN** |

### 家用路由的两个具体影响

| 影响 | 怎么处理 |
| --- | --- |
| **NAT 映射被回收**（家用路由的 UDP 空闲超时常常只有 30~120 秒） | **ICE 自己管**：libjuice 内置 STUN keepalive + consent freshness（源码 `agent.c` 的 `STUN_KEEPALIVE_PERIOD`），映射会被保活。**但侧载别在「没请求」时把连接主动关掉**，否则保活也白搭 |
| **内网上行小** | a2 的响应、文件、截图都要从客户上行出去。远程桌面（ToDesk/RustDesk）也是同样的账，**不是隧道独有**；但要提前跟客户说清楚「内网→外网」是窄的 |

### 还有个更隐蔽的坑：入站过滤（这才是打不通的常见原因）

NAT 有两件事：**映射**（出站用哪个端口）和**过滤**（哪些来源的入站包放行）。
上面测的是映射。过滤是另一回事，家用路由典型的过滤规则是
**「只放行我主动联系过的地址」**（address-dependent filtering）。

于是会出现一个死锁：

```
客户端 ──(打洞包)──▶ 36.112.70.164:59083  ✗ 被内网 NAT 丢了
        （内网 NAT 只放行 STUN 服务器的地址，客户端不在名单里）
内网侧压根收不到包 → 不知道客户端在哪 → 不会回包 → 死锁
```

**解法：让 broker 当中介主动推一次地址（"打洞引导器"，几十行）。**

- broker 自己是 STUN 服务器，**天然知道双方的 srflx 地址**；
- broker 给内网侧发一个包（带「对端在 X:Y」），这个包的来源是 broker 自己——
  而内网侧**刚向 broker 问过 STUN**，所以 broker 的地址在放行名单里 → **收得到**；
- 内网侧拿到对端地址后主动打过去 → 它的 NAT 现在放行对端地址 → 对端打回来的包就进得来了。

这不是 TURN（不转发任何业务流量），只是**握手阶段的引导**，几十行代码，
但它是「address-dependent 过滤」环境下打洞能不能成的关键。
**P1.5 把它和 TURN 开关一起做掉**（一个进程，两个开关）。

### 顺带：客户网络常常有 IPv6

家用宽带大多双栈。如果设备和运维那台都有公网 IPv6，ICE 会直接拿到 IPv6 的
host candidate，**连打洞都不用**（延迟还更低）。不用专门做，ICE 自己会选——
但要知道：**IPv6 直连时，NAT 那一层的问题直接消失**。

## 五、实测记录（2026-11-09，本机）

### 5.1 WebRTC 数据通道能进单二进制

用 `libdatachannel` 0.22（C++，自带 ICE：libjuice + STUN/TURN；SCTP：usrsctp）
静态链接，**不开媒体、不开 WebSocket**（只留数据通道）：

| 平台 | 静态构建 | 链接成可执行文件 | 备注 |
| --- | --- | --- | --- |
| Linux x86_64 | ✅ | ✅ 2.38 MB（strip 后） | 原型见下 |
| Windows x64（mingw 交叉） | ✅ | ✅ 3.91 MB | 消费者要 `-DRTC_STATIC`；usrsctp 要 `-liphlpapi` |
| Linux aarch64（Arm GNU 11.3 交叉） | ✅ | ✅ 2.80 MB | 就是嵌入式设备那条线 |

原型（两个 PeerConnection 在一个进程里，一条数据通道，**HTTP 请求骑在通道上跑通**）：

```
[client] local: offer
[server] local: answer
[server] data channel accepted: a2
[client] data channel open
[server] got 44 bytes: GET /api/health HTTP/1.1
[client] response:
HTTP/1.1 200 OK
Content-Type: application/json
Content-Length: 27

{"version":"1.2.1","ok":true}
```

也就是说：**「HTTP over WebRTC 数据通道」这条路是通的**，不是纸上谈兵。

### 5.2 STUN 打洞的原料是有的

手写了一个 30 行的 STUN binding 请求（RFC 5389）打公共 STUN 服务器：

```
stun.l.google.com   local=0.0.0.0:0  reflexive=('39.65.223.83', 60567)  rtt=187ms
```

本机 UDP 出站正常、能拿到 reflexive 地址，而且是 **cone** 类
（三个 STUN 服务器看到的映射端口一致）→ 本机这套网络**能被打洞**。

### 5.3 体积

| 组件 | object code 体积 |
| --- | --- |
| libdatachannel | 1250 KB |
| libusrsctp | 392 KB |
| libjuice | 123 KB |
| mbedtls 3.5（tls+x509+crypto） | 521 KB |

**侧载**（含 DTLS 加密 + ICE）strip 后约 **2.4 MB**；
**a2 本体不变**（现在 4.89 MB strip 后），因为隧道不进 a2。

---

## 六、依赖与「单二进制」

侧载是独立二进制，所以它可以自带一套完整的第三方依赖（照 `3rd/` 的老规矩 vendoring）：

| 依赖 | 干什么 | 许可证 | 备注 |
| --- | --- | --- | --- |
| libdatachannel 0.22 | WebRTC 数据通道 | MPL-2.0 | 只开数据通道，NO_MEDIA + NO_WEBSOCKET |
| libjuice | ICE：STUN + TURN 客户端 | MPL-2.0 | libdatachannel 自带；**broker 侧用它的 server 模式做 STUN** |
| usrsctp | SCTP（数据通道的传输层） | BSD-3 | libdatachannel 自带 |
| mbedtls **3.5/3.6** | DTLS 加密 | Apache-2.0 | **和 a2 自带的 2.28.9 不是一份** |

> **这里本来有个坑，现在因为「侧载」没了。**
> libdatachannel 0.19+ 要求 **mbedtls ≥ 3**（`find_package(MbedTLS 3 REQUIRED)`），
> 而 a2 自带的是 **mbedtls 2.28.9 LTS**。两个版本**不能链进同一个二进制**（符号同名、结构体不同）。
> 如果当初把隧道塞进 a2，就得先把 a2 的 mbedtls 升到 3.6 LTS——
> 好消息是实测 `src/tls.cpp` 和 `src/http.cpp` 用 mbedtls 3.5 的头文件**原样编译通过**
> （它们只用到 25 个标准 API），但那是另一件事，不该混在隧道里做。
> 侧载自己带一份 3.x，这事就没了。

其它要 vendoring 的注意点：

- mbedtls 3.6 LTS 的源码树拆成了 `framework/` 子模块，tarball 里是空的——要么把子模块也 vendor 进来，要么用 3.5.x 整树。
- mbedtls 3.x 的 DTLS-SRTP 选项（`MBEDTLS_SSL_DTLS_SRTP`）默认是关的，libdatachannel 的 DTLS 代码要它——**开这个选项**。
- Windows 静态链接时消费者要 `-DRTC_STATIC`，否则一堆 `__imp_` 未定义；usrsctp 还要 `-liphlpapi`。

---

## 七、安全边界

| 面 | 结论 |
| --- | --- |
| 传输加密 | P2P 走 WebRTC 的 DTLS（强制）→ **broker 看不到内容**；中继兜底时建议两侧各自再包一层 TLS 到 broker |
| 鉴权 | 复用 a2 已有的 `--api-token`（`/api/peer/*` 镜像）+ broker 层的设备令牌（每个设备一个，可单独吊销） |
| **内网 a2 的 Web UI 本身没有鉴权** | ⚠️ 隧道一旦暴露 `/`，谁拿到 broker 地址谁就能用内网 a2 的界面。**broker 的令牌就是唯一的门**，必须做对：强随机、逐设备、可吊销、走 HTTPS |
| broker 的可见性 | **走 TURN 中继时 broker 仍然看不到内容**：WebRTC 的 DTLS 是在**两个端点之间**终结的，TURN 只转发 DTLS 密文（broker 看得到包的大小和节奏，看不到 token/prompt/文件）。⚠️ 但如果改成**自己写 HTTP 式中继**，broker 就是 HTTP 转发方 → **看得到明文**——这是选 TURN 而不是自研中继的一个隐私理由 |
| 越权 | 侧载只转发它被配置转发的那一个地址；不要给它「任意目标」的能力 |
| 审计 | 隧道建立/断开/每次请求都要落日志；**「这次走的是 P2P 还是中继」要记**，否则出问题排查没线索 |
| 撤销 | 设备令牌吊销 = 该设备立刻失联；a2 本体不受影响 |

一句话：**隧道的信任边界在 broker 和令牌上，不在 a2 上。** a2 侧不用为隧道改任何东西。

---

## 八、要不要自己造？

既然客户确认支持打洞，自研 P2P 的价值就更高了（省掉第三方中继、令牌和审计都在我们手里）。
但现成方案仍然是**退路**，不是没意义：

| 方案 | 打洞 | 单二进制 | 我们的收益 | 代价 |
| --- | --- | --- | --- | --- |
| **frp**（`xtcp` 访客模式） | ✅ 自带 STUN 打洞 + 中继回退 | ✅ frpc/frps | 自研打洞出问题时的退路；也用来对照效果 | 客户内网多一个第三方二进制；没有 a2 级的令牌/审计 |
| **rathole / chisel** | ❌ 纯中继 | ✅ | 简单 | 没有 P2P |
| **cloudflared** | ❌ 纯中继 | ✅ | 浏览器直连 Web UI 很顺 | 依赖 Cloudflare；数据过第三方 |
| **tailscale** | ✅（WireGuard 打洞 + DERP） | ✅ 客户端 | 组网能力强 | 要协调服务器；是 VPN 不是 HTTP 代理 |
| **自己做** | ✅ | ✅ | 令牌、审计、级联、UI 全在我们手里；broker 只做信令 | 要自己维护 broker 和 NAT 兼容性 |

---

## 九、分阶段计划（已按「客户支持打洞」调整）

| 阶段 | 做什么 | 验收标准 | 粗略工作量 |
| --- | --- | --- | --- |
| **P0 现场验证** | 按 [tunnel_ops.md](tunnel_ops.md) 在**真客户环境**跑一次 `nat_probe.py` | 两边都打印「P2P 打洞成立」；记下 NAT 判定和 RTT | 0.5 天 |
| **P1 侧载 + 中继主路径** | `a2_tunnel`（两端）+ `a2_broker`（**STUN + 信令 + TURN 中继**，libjuice server 模式一个进程搞定）；libdatachannel + ICE（候选里同时给 stun 和 turn）；帧化 HTTP；令牌；日志 | 外网 a2 用 `peers:` 指向 `127.0.0.1:9931` 就能 `toolkit_peer` 到内网 a2；**在实测失败的那条网络上也能通** | 5–7 天 |
| **P2 直连优化** | broker 兼**打洞引导器**（把对端 srflx 主动推给对方，绕过入站过滤）；ICE 里同时留 stun 候选 | 在**允许打洞**的网络上（部分企业网、部分家用宽带）走直连；日志能看到「直连/中继」 | 1–2 天 |
| **P3 硬化** | SSE 不缓冲、多设备台账、令牌吊销、掉线重连 | 长时间稳定、中继带宽够用 | 3–5 天 |

> **为什么中继是主路径**：实测打不通（对称 NAT + 入站过滤）。
> 既然 ICE 本身就支持 relay 候选，把 TURN 放进第一版只多花半天，
> 却把「打不通」这个风险从「事故」降级成「多一跳」。
> 直连（P2）依然值得做——在允许打洞的网络上它更省带宽、更私密。

**P1 的关键设计**（决定了 P2 不会推翻 P1）：

- **隧道里跑的是原始 TCP 字节**（不是重新实现的 HTTP）：侧载在本机开一个代理口
  （`127.0.0.1:9931`），外面进来的 TCP 连接被原样搬进隧道，内网侧再连到 a2 的 8383。
  于是 **a2 那边只当它是个普通 HTTP 服务器**，`peers:` 机制零改动。
  （数据通道内部是 `[u8 type][u32 len][payload]` 的分帧，用来区分
  OPEN/DATA/CLOSE 和多条并发连接。）
- **保活不能省**：ICE 的 keepalive 负责 NAT 映射，但**应用层的长空闲连接**（比如一次跑很久的 SSE）
  要自己决定超时策略，别默认 60 秒就断。

## 十、还没验证的（别当成已知）

- **真客户环境的一次实跑**：条件①②③口头上都满足了，但没在他们那台机器上量过。
  → P0 的 `nat_probe.py` 就是干这个的，5 分钟。
- **SSE 长连接穿过隧道**：a2 的对话是 SSE（chunked）。隧道必须**按字节流转发、不要缓冲**——
  数据通道天然是消息边界，仍然按 HTTP 的分块语义处理。
- **macOS**：没交叉编译（本机没有 macOS 工具链）。libdatachannel 支持 macOS，但 a2 的 macOS 构建线本来也不是重点。
- **Windows 侧载的真机实跑**：Windows 版**已经能编出来**（`a2_tunnel.exe`，自包含、只依赖系统
  DLL），但还没在 Windows 机器上真的连过一次隧道——这是下一步。
- **同一台 a2 同时被多个侧载连**：连接复用、并发、限流，都还没设计。

---

## 十一、已经跑通的（2026-11-09 实测）

三个角色都写出来了，并在这三台真机上跑通：

| 组件 | 源码 | 部署 |
| --- | --- | --- |
| **broker**（STUN + 信令 + TURN，一个进程） | `src/tunnel_broker.cpp` | VPS：`~/.tea_agent/tunnel/a2_broker`（用户级 systemd 单元 `a2_broker.service`） |
| **侧载**（agent / client 两种角色） | `src/tunnel_sidecar.cpp` | 内网侧（zonekey）与外网侧各一份 |

**实测路径**（客户侧是家用宽带出口 + 对称 NAT）：

```
外网侧载  39.65.223.83:127185  <->  VPS 103.232.92.54:114765   (RELAYED by the broker)
内网侧载  36.112.70.164:80207  <->  VPS 103.232.92.54:114794   (RELAYED by the broker)
```

- 本机 `curl http://127.0.0.1:9931/api/health` → **拿到内网那台 a2 的健康**（经 VPS 中继）；
- 2.7 MB 文件穿隧道传输正常（速度受**内网侧上行**限制，家用宽带约 38 KB/s）；
- `peers:` 里加一条 `url: http://127.0.0.1:9931`，`toolkit_peer` 就能驱动内网那台 a2。

**信令上有三个坑（都踩过）**：

1. **offer 发出时对端还没进房** → broker 要么缓存，要么让新加入的一方主动
   「要一个 offer」（`need-offer`），并且**每次对端到访只发一次 offer**（发两次，
   对端的 answer 就对不上我们手里的描述了）。
2. **answer 侧也会自动协商**：收到数据通道后客户端自己发 offer → 两边无限循环。
   → answer 侧必须 `disableAutoNegotiation`。
3. **offer/answer 的角色不能反**：a2 侧永远发 offer，客户端永远答；反了会报
   `Incompatible roles with remote description`。

**侧载必须静态链接**：zonekey 是 Ubuntu 20.04（glibc 2.31），动态链接的二进制根本起不来
（`GLIBC_2.34 not found`）——和 a2 本体一样，**静态链接是硬要求**。

**完整闭环（真机）**：外网的 a2 通过隧道驱动内网的 a2——

```
[chat zonekey @ http://127.0.0.1:9931] (3290ms, 0 tool call(s) on the peer)
我运行在这台名为 zonekey 的 Ubuntu 20.04 机器上（主机名 zonekey，工作区根目录 /root）。
```

即：外网那台 a2 的 `peers:` 里写 `url: http://127.0.0.1:9931`，
`toolkit_peer` 就把问题送进了内网那台 a2，它用**自己的**模型、**自己的**上下文回答。

**信令走 TLS 之后还要注意**：OpenSSL 校验证书里的 **IP 地址**要用
`X509_VERIFY_PARAM_set1_ip_asc()`，`set1_host()` 是给 DNS 名的——
用错了会一直报 `certificate verify failed`。

### 凭据：哪些是内置的，哪些要传

| 东西 | 是什么 | 怎么来的 |
| --- | --- | --- |
| **TURN 账号口令** | 谁能用 broker 的中继带宽 | **内置在源码里**（`1hblsqt.` / `2ghlmcl,`）。它不是秘密：泄露了只是别人能用你的中继，不至于打开内网那台机器 |
| **房间令牌** | 谁能进这个房间 | **agent 侧每次启动随机生成** |

所以**三台机器都不用配凭据文件**，而且**现场人员和运维之间只需要传一个字符串**：

```
# 现场（内网那台）：a2_tunnel --role agent
#   → 打印：a2_tunnel --role client --session 43910a0ab1600dc462b072114fe90132
# 运维（外网那台）：a2_tunnel --role client --session 43910a0ab1600dc462b072114fe90132
```

那个 **session 码**里含着房间名和令牌：房间 = `s` + 码的前 8 位，令牌 = 整串。
broker 自己学每个房间的令牌（第一个带 `register` 的 join 说了算），
agent 侧每次启动换一个码，client 侧照抄即可。

> 令牌不对时 broker 直接拒绝，client 侧**立刻退出**并提示「用对端印出来的那条命令」。

### 默认值：几乎不用写参数

| 参数 | 默认 |
| --- | --- |
| `--broker` | `103.232.92.54:8382`（公网 broker，走 TLS） |
| `--stun` / `--turn` | `103.232.92.54:3478` |
| `--target` | `127.0.0.1:8383`（内网那台的 a2） |
| `--listen` | `127.0.0.1:9931`（外网侧的本地代理）；`--port <n>` 直接改端口（和 a2 本体一个写法） |
| `--tls` | **开**（8382 就是 TLS 口）；`--no-tls` 给内网 broker 用 |
| `--ca-bundle` | `~/.tea_agent/ca-bundle/cacert.pem`（找不到就**大声警告**并跳过校验） |
| `--room` / `--token` | agent 侧自动生成；client 侧用 `--session` |
| `--wait-timeout` | 300 秒 |

> 令牌**按房间**而不是全局：broker 的 `--token` 不给时，每个房间的令牌由加入方注册；
> 给了 `--token` 则是全局钉死一个。令牌不对时 broker 直接拒绝，client 侧**立刻退出**
> 并提示「用对端印出来的那条命令」（agent 侧每次启动都会换一个令牌）。

### 按需启动：现场人员配合的流程

内网设备上**不需要常驻**一个侧载。默认 `--wait-timeout 300`（**5 分钟**）：
侧载起来后如果 5 分钟内没有对端连进来，就自己干净退出（退出码 0，
所以 `Restart=on-failure` 的单元不会把它当成崩溃而反复重启）。

于是流程是：

1. 运维那边需要访问内网设备 → **通过别的渠道（电话/微信）通知现场人员**；
2. 现场人员启动侧载 → 它打印「怎么连」的**完整命令**，转给运维；
3. 运维那边启动 client 侧（或者把它贴进自己 a2 的 `settings.peers`）→ 隧道建立；
4. 用完各自断开即可。

agent 侧启动后打印的东西（可以直接转给运维）：

```
sidecar: ready. Waiting in room "plant-a" for the other side.
  broker        : 103.232.92.54:8382
  room token    : 7f3d64098d9f0c6dcebd7b86410e55a3
  target        : 127.0.0.1:8383
  give this to whoever connects from the outside:
      a2_tunnel --role client --broker 103.232.92.54:8382 --tls --ca-bundle <the broker's ca.pem> --room plant-a --token 7f3d64098d9f0c6dcebd7b86410e55a3
  (it will listen locally and be reachable through the tunnel)
  giving up in 300 seconds if nobody connects.
```

那串 session 码**每次启动都不一样**——所以现场人员每次都要把新的那条命令发过去，
运维那边也要用新的码重启 client 侧（旧码会被 broker 拒绝）。

### 空闲的连接不会被自己掐掉

「浏览器开着、连接建好了、很久没有数据」——这种连接**必须一直活着**，
只有两侧有一方明确断开才算结束。隧道这一侧的三重保活：

| 层 | 谁在管 | 多久一次 |
| --- | --- | --- |
| ICE（consent freshness） | libjuice | 几秒一次探测，路径不通才断 |
| TURN 租期与权限 | libjuice 自动续 | 租期 10 分钟、权限 5 分钟，都按一半时间提前续 |
| SCTP 关联心跳 | usrsctp | 双方互相应答，关联不会自己老死 |

所以**空闲不会导致断开**；断开只来自：对方进程退出、隧道两端有一方主动关、
或者链路真的断了（ICE 探测不到对端——那是真断了，该断）。

> ⚠️ 反过来要注意的是**a2 自己那侧的超时**：`toolkit_peer` 的连接超时是 10 秒、
  读超时 60 秒（聊天按工具的超时算）。隧道再稳，a2 也不会拿着一条连接永远不放——
> 那是 a2 的预算，不是隧道断链。

---

## 十二、怎么编

隧道那两个二进制用**自己的一套 CMake 工程**（`tunnel/`）编，原因是 libdatachannel
要 mbedTLS 3.x，而 a2 本体用的是 vendored 的 2.28 LTS——两份 mbedtls 的 target
同名（`mbedtls` / `mbedx509` / `mbedcrypto`），放进一个工程里 CMake 会直接报错。
细节见 [`3rd/README.md`](../3rd/README.md)。

主工程会顺带把它编出来（用的是**目标**工具链，交叉编译同理）：

```bash
cmake --preset posix && cmake --build build -j8
#   → build/bin/a2_broker  build/bin/a2_tunnel

cmake --preset win64-mingw-cross && cmake --build build-win64 -j8
#   → build-win64/bin/a2_broker.exe  build-win64/bin/a2_tunnel.exe
```

| 二进制 | 作用 | 链接 |
| --- | --- | --- |
| `a2_broker` | 公网 broker（STUN + TURN 中继 + 信令） | 静态（Linux/macOS） |
| `a2_tunnel` | 侧载（agent / client 两种角色） | 静态（Linux/macOS） |

- **Windows 的 .exe 是自包含的**：只依赖系统 DLL（KERNEL32 / WS2_32 / BCRYPT /
  IPHLPAPI + UCRT），和 a2 本体一个待遇——不需要在目标机器上装
  libstdc++-6.dll 那一套。
- **信令的 TLS 客户端用的是 mbedTLS**（和仓库其它地方同一套栈），所以侧载
  **不需要 OpenSSL**，整个二进制只有 libc + pthread。
- ⚠️ **Linux 的静态版不做 DNS 解析**：glibc 的 NSS 在动态库里，`-static` 的
  程序调不了 `getaddrinfo`。侧载只用 IP，所以不影响；但静态版也会丢掉
  **host 候选**（srflx 和中继候选还在）。需要域名或 host 候选就
  `-DTEA_TUNNEL_STATIC_LIBC=OFF`。

---

## 十三、相关文档

| 文档 | 内容 |
| --- | --- |
| [tunnel_ops.md](tunnel_ops.md) | **现场操作指引**：两台机器各跑两条命令，验证打洞（给客户/运维照着做） |
| [tunnel_broker.md](tunnel_broker.md) | **broker 机器（VPS）准备清单**：规格、端口、验收、带宽成本 |
| [peer.md](peer.md) | 级联：`peers:` 段 + `toolkit_peer`（隧道接的就是它） |

---

## 十四、复现这次实测

```bash
# 下载依赖 → 静态构建 → 链接原型（不需要 root，不装任何东西）
bash tools/tunnel_probe/build_probe.sh

# 跑起来：应该看到一个 200 OK 从数据通道里回来
/tmp/a2-tunnel-probe/dc_probe

# 交叉编译（同一台 Linux 机器实测过）
bash tools/tunnel_probe/build_probe.sh --cross win   # Windows x64
bash tools/tunnel_probe/build_probe.sh --cross arm   # Linux aarch64

# 现场验证打洞（两台机器各跑一条，纯 Python）
python3 tools/tunnel_probe/nat_probe.py agent
python3 tools/tunnel_probe/nat_probe.py client --peer <token>
```

原型源码：[tools/tunnel_probe/dc_probe.cpp](../tools/tunnel_probe/dc_probe.cpp)
（两个 PeerConnection + 一条数据通道 + 一个 HTTP 请求/响应）；
坑清单在 [tools/tunnel_probe/README.md](../tools/tunnel_probe/README.md)。

**现场怎么跑**（给客户的运维同事）：[tunnel_ops.md](tunnel_ops.md)——一页纸，
两台机器各一条命令 + 验收标准 + 打不通怎么办。
