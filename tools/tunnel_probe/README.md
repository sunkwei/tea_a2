# tunnel_probe — 「HTTP 跑在 WebRTC 数据通道上」+「现场打洞验证」

这两个工具是 [docs/tunnel.md](../../docs/tunnel.md) 那页可行性结论的**证据**，不是产品代码。

| 工具 | 干什么 | 依赖 |
| --- | --- | --- |
| `dc_probe.cpp` | 证明 **HTTP 请求/响应可以骑在 WebRTC 数据通道上** | libdatachannel（要编译） |
| `nat_probe.py` | 证明 **这两台机器之间能不能真的打洞** | 纯标准库（不用编译） |

---

## 一、nat_probe.py：现场验证打洞（先跑这个）

客户口头说「我们这支持打洞」不算数——要在这两台真实机器之间跑一次，看 UDP 是不是
真能双向通。这个脚本就是那一次验证，**两台机器各跑两条，纯 Python 3.6+，
不用编译、不用 root、不装任何东西**。

**协议和真实 ICE 一样：两边都知道对方的地址，然后同时主动打洞。**

```bash
# 第 1 步：两边各跑一次，拿自己的 token
python3 tools/tunnel_probe/nat_probe.py probe
#   -> NAT 判定 + 一行 token：39.x.x.x:port:rand     ← 抄给对方

# 第 2 步：把两边的 token 互换

# 第 3 步：两边同时跑
python3 tools/tunnel_probe/nat_probe.py punch --mine <自己的token> --peer <对方的token>
```

两边最后都打印结论：

```
[punch] 发了 100 个包，收到 100 个回包，延迟 23 ms
[punch] 结论：P2P 打洞成立（双向都通）。
```

⚠️ **两边必须用同一个 STUN 服务器**（脚本会打印「用的 STUN 服务器」）——
对称 NAT 的映射端口随目标变，只有「双方经同一个服务器看到的地址」才可比。
不一致就两边都加 `--stun <host:port>` 指定同一个。

> **为什么不是「一方打、另一方等」**：真实 ICE 里双方都知道对方地址、
> **都主动打**——NAT 的入站放行规则要靠「双方都发过包」才打开。
> 早期版本是一方被动等，结果连「本机收不到任何包」都分不清是 NAT 过滤还是运营商丢包。

```
[agent] NAT 判定：cone（映射端口 stun.l.google.com:19302:65331, ...）
```

**那两行输出就是验收依据**：

- `NAT 判定：cone` → 打洞基本必成；`address-dep` → 换目标 IP 就换映射端口，
  **不一定**打不通，看过滤规则，必须实测；`symmetric` → 打洞大概率失败，得靠 TURN。
- `P2P 打洞成立` → srflx ↔ srflx 直连可用，隧道主路径成立。

### 判定这里有个坑（第一版工具踩过）

第一版把「不同 STUN 服务器的映射端口不一样」直接判成 symmetric，**下结论早了**：

```
映射端口 stun.l.google.com:19302:41904, stun1.l.google.com:19302:41904, stun.cloudflare.com:3478:44839
```

`stun.l.google.com` 和 `stun1.l.google.com` **是同一个 IP**（74.125.250.129，Google anycast），
它们端口一样是必然的，什么也证明不了。真正有信息的只有「换 IP 就换端口」这一条
→ 正确分类是 **address-dependent**，不是 symmetric。

**测 symmetric 的唯一有效对照是「同一个 IP 的不同端口」。**
所以默认服务器里带了 `stun.nextcloud.com:3478` 和 `stun.nextcloud.com:443`
（同一个 IP 46.225.95.169 的两个端口）：

```
[agent] NAT 判定：cone
[agent]   依据：同一个 IP(46.225.95.169) 换个端口，映射端口不变（55807）
[agent]   stun.l.google.com:19302      ip=74.125.250.129   端口 19302 -> 映射端口 55807
[agent]   stun.cloudflare.com:3478     ip=162.159.207.0    端口 3478  -> 映射端口 55807
[agent]   stun.nextcloud.com:3478      ip=46.225.95.169    端口 3478  -> 映射端口 55807
[agent]   stun.nextcloud.com:443       ip=46.225.95.169    端口 443   -> 映射端口 55807
[agent]   stun.miwifi.com:3478         ip=111.206.174.2    端口 3478  -> 映射端口 55807
[agent]   含义：打洞基本必成。
```

自检（同一台机器上跑两阶段，走回环）：

```bash
T=$(python3 tools/tunnel_probe/nat_probe.py probe | grep -oE '[0-9.]+:[0-9]+:[0-9a-f]+' | head -1)
python3 tools/tunnel_probe/nat_probe.py punch --mine "$T" --peer "$T" --wait 5
```

**注意**：默认用的公共 STUN（stun.l.google.com 等）在企业网里经常被屏蔽或劫持。
现场跑不通时，先用 broker 自建的 STUN 再测一次：

```bash
python3 tools/tunnel_probe/nat_probe.py agent --stun <broker-ip>:3478
```

---

## 二、dc_probe.cpp：证明 HTTP 跑在数据通道上

```bash
bash tools/tunnel_probe/build_probe.sh
/tmp/a2-tunnel-probe/dc_probe
```

脚本把依赖下到 `/tmp/a2-tunnel-probe` 静态编译，第一次跑要几分钟（编 mbedtls + libdatachannel）。

交叉编译（这台 Linux 机器上实测过）：

```bash
bash tools/tunnel_probe/build_probe.sh --cross win   # Windows x64 (mingw)
bash tools/tunnel_probe/build_probe.sh --cross arm   # Linux aarch64
```

### 期望输出

```
[client] local: offer
[server] local: answer
[server] data channel accepted: a2
[server] channel open
[client] data channel open
[server] got 44 bytes: GET /api/health HTTP/1.1
[client] response:
HTTP/1.1 200 OK
Content-Type: application/json
Content-Length: 27

{"version":"1.1.0","ok":true}
```

### 它用了什么

| 依赖 | 版本 | 干什么 |
| --- | --- | --- |
| libdatachannel | 0.22.0 | WebRTC 数据通道（`NO_MEDIA` + `NO_WEBSOCKET`，只留数据通道） |
| libjuice | master | ICE：STUN/TURN 客户端（libdatachannel 自带） |
| usrsctp | master | SCTP：数据通道的传输层（libdatachannel 自带） |
| mbedtls | 3.5.2 | DTLS 加密（**和 a2 自带的 2.28.9 是两份**，侧载模式才允许） |

---

## 踩过的坑（照抄就行）

- **建数据通道时会自动产生 offer**：`onLocalDescription` 必须在 `createDataChannel`
  **之前**注册，否则 offer 丢了，后面所有等待都白等。
  之后再手动 `setLocalDescription(Offer)` 会被拒：
  `Unexpected local description in signaling state have-local-offer`；
  服务端的 answer 也会自动产生，状态已经是 stable 时再手动 answer 会被拒：
  `Unexpected local desciption type answer in signaling state stable`
  （`desciption` 是 libdatachannel 自己的拼写错误）。
- **`MBEDTLS_SSL_DTLS_SRTP` 默认是关的**，而 libdatachannel 的 DTLS 代码要
  `mbedtls_ssl_conf_dtls_srtp_protection_profiles()` → 编译报
  `'srtpSupportedProtectionProfiles' was not declared`。脚本里把
  `include/mbedtls/mbedtls_config.h` 的 `//#define MBEDTLS_SSL_DTLS_SRTP` 打开。
- **Windows 静态链接**：消费者要 `-DRTC_STATIC`（否则一堆 `__imp_` 未定义符号），
  usrsctp 还要 `-liphlpapi`（`GetAdaptersAddresses`）；**没有 `-ldl`**（那是 Linux 的）。
- **候选要等远端描述**：`addRemoteCandidate` 早于 `setRemoteDescription` 会被丢掉
  （日志：`Got a remote candidate without remote description`）——先缓存，等描述到了再补。
- **libdatachannel 0.19+ 要 mbedtls ≥ 3**（`find_package(MbedTLS 3 REQUIRED)`）。
  a2 自带的是 2.28.9，两份不能链进同一个二进制——这正是「隧道做侧载」的原因。
- **mbedtls 3.6 LTS 的源码树拆成了 `framework/` 子模块**，tarball 里是空的（3.5.x 没有这个问题）。
- **libdatachannel 的 tarball 里子模块是空的**（plog / usrsctp / libjuice），脚本手动塞进去。
- GitHub 的 `git clone` 在这台机器上会被 reset 掉，**用 codeload 的 tar.gz**。
- **nat_probe 的 NAT 判定**：要问**几个不同的 STUN 服务器**才准——只问一个的话，
  端口一样不能说明是 cone（映射可能只是碰巧没变）。所以问完第一个要等后面几个，
  别急着把 token 印出来（脚本已经改成：第一个有回应就先印 token，再继续问）。

---

## 这两个工具**没有**证明什么

- `dc_probe` 没证明能穿过真实的企业 NAT（它只在回环上跑）；那要靠 `nat_probe` 在真环境跑。
- `nat_probe` 只测 UDP 能不能双向通，**没测 TLS/DTLS 握手、没测大数据、没测长时间稳定性**。
- 两个都不是产品代码：没有鉴权、没有重连、没有多设备、没有日志。
