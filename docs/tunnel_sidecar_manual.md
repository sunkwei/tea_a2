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

### 要一个终端（不是 a2 的网页）

设备上**没有 a2 也行**：侧载自己能开一个真的 shell（Linux 上是 pty，
Windows 上是 ConPTY），隧道把它带到外面。

```bash
a2_tunnel --role agent --target terminal
```

外面那个人照旧跑 client 侧，然后**浏览器打开 http://127.0.0.1:9931/** ——
打开的就是设备上那个终端（不是 a2 的界面）。

client 侧会自己说一声：

```
sidecar: the other end is a terminal
  The far end is a terminal: open http://127.0.0.1:9931/ in the browser.
```

终端里能干什么：敲命令、往回翻、鼠标选字复制、上传下载文件。

> ⚠️ **这个 shell 是那台机器上侧载的权限**（设备上通常是 root）。
> **拿到 session 码的人，就能在那台机器上敲命令。**
> 房间令牌是唯一的门，所以 session 码还是别乱发。

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
（对面跑的是 `--target terminal` 的话，打开的就是**那个终端**。）

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

## 五、远程桌面：看对面的屏幕，动对面的鼠标

侧载还能当**远程桌面**用：设备那一边抓自己的屏幕，外面那一边看画面、动鼠标。

| 角色 | 装在哪 | 干什么 |
| --- | --- | --- |
| `--role desktop` | **客户那台设备上** | 抓自己的屏幕，编码，给外面的人看；接收鼠标和键盘 |
| `--role control` | **运维/外面那台机器上** | 把画面解码出来给浏览器；把鼠标键盘送回去 |

设备上（跟 `--role agent` 一样，它就是 agent 那边多了一台屏幕）：

```bash
a2_tunnel --role desktop
```

外面那台机器：

```bash
a2_tunnel --role control --session <码>
```

然后浏览器打开 **http://127.0.0.1:9932/**（端口可以用 `--viewer-port` 改）。

画面：每秒 2 张（`--fps`，可以是小数，比如 `2.0` / `2.5`），**H.264**
（`--bitrate`，默认 **100k** ✓），尺寸默认是**主显示器**的真实像素
（`--resize 1280x720` 把它拉成正好那么大 —— 比例不一样也拉，**会变形** ✓；
`--width 1280` 只限宽度，高度跟着走 ✓）。
鼠标和键盘原路回去：鼠标是**对面主显示器上的坐标**，键盘是按键和字符。

**要注意的**：

- 这一套目前是 **Windows 的**（抓屏用 GDI，键鼠用 SendInput）。别的平台上这一对角色
  会说「没有桌面」。
- 外面那台的 **9931 还是隧道本身**（a2 的 `peers:` 用的就是它）；**9932 才是给人看
  画面的**。
- 画面走的就是隧道（数据通道，还是字节透明的），所以它占的是隧道那条河的带宽。
  默认压得很低：**100 kbps** ✓ —— 一张 2560x1440 的画面 6 KB 出头 ✓，一秒两张
  12 KB ✓（同一张屏幕编成 JPEG 要 200 KB 出头 ✓）。代价是画面**软**：文字看不清
  ✓。**鼠标和键盘走另一条通道 ✓，不受影响 ✓**。
- 想清楚一点：`--resize 1280x720`（像素少四倍，同样的码率下清楚得多 ✓，整个桌面
  照样看得全 ✓），或者 `--bitrate 2000`。
- **只抓主显示器**（`SM_CXSCREEN` ✓）：别的显示器不管 ✓，鼠标的坐标也是相对主显示器
  的 ✓（`SendInput` 不带 `VIRTUALDESK` ✓）。主显示器的左上角永远是 (0,0) ✓。
- **只走 libx264**（x264 ✓）：设备那边只编 H.264，不编 JPEG ✓。浏览器用
  **WebCodecs** 解 ✓（Chrome / Edge / Firefox 都行 ✓）；浏览器不会解的话它会直说 ✓。
- `--keyint <秒>`：关键帧的间隔（默认 10 秒 ✓）。中间的是 P 帧 ✓，小得多 ✓
  （安静的屏幕一张只有几 KB ✓），但它们**要靠前面的帧** ✓ —— 所以画面是**成流**
  的 ✓：一张都不能少 ✓，少一张解码器就得等下一个关键帧 ✓。

**例子**：

```bash
a2_tunnel --role desktop --bitrate 100k --fps 2.0 --resize 1280x720
```

这样下来 1280x720 的画面一张 2~3 KB ✓，一秒两张 5 KB 出头 ✓（40 kbps ✓）。
- 外面那台的机器上，浏览器和看图页面都在本机（127.0.0.1），画面**只有解码出来之后**
  才交给浏览器 —— 隧道上跑的是压缩过的图。

## 六、broker 在哪儿

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

> ⚠️ **指纹只是「看着」，不是「拦着」。**
> broker 的证书是**自签的**（它自己造的，没有 CA 签），所以：
>
> - **证书不验**（没有链可走，证书库里也永远找不到它）→ **`--ca-bundle` 对 broker 没用**
>   （这个参数留着只是为了老的命令行不报错）；
> - 配了指纹，broker 递上别的证书时，**侧载会说一声，但继续用**：
>
>   ```
>   sidecar: the broker's certificate is not the one that was pinned (3f9a1c...)
>   sidecar:   continuing anyway - the room token is what gates the room
>   ```
>
>   **不断隧道**：指纹这东西，离「过期」只有一次编辑的距离（broker 重装、
>   证书换了），断了隧道比没验证书更糟。
>
> **真正把门守住的是房间令牌**：令牌不对，broker 直接不理 join。

也可以命令行覆盖：

```bash
a2_tunnel --role client --session <码> --broker 新地址:8382 --tls-pin 3f9a1c...
```

**Windows 上的路径**是 `%USERPROFILE%\.tea_agent\tunnel\broker.txt`
（比如 `C:\Users\<用户名>\.tea_agent\tunnel\broker.txt`）——
文件夹还没有就自己建一个。

---

## 七、参数

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
| `--target terminal` | — | agent 侧改成开一个真的 shell（pty / ConPTY），隧道把它带出去 |
| `--stun <host:port>` | 跟 broker 走 | STUN 服务器 |
| `--turn <host:port>` | 跟 broker 走 | TURN 服务器 |
| `--turn-user <名>` | 内置 | TURN 账号 |
| `--turn-pass <密>` | 内置 | TURN 口令 |
| `--turn-db <file>` | a2_broker.db（当前目录） | 账号簿（跟 broker 同一个 sqlite 文件） |
| `--ice-relay-only` | 关 | 只走中继（测 TURN 账号时用） |
| `--tls` / `--no-tls` | 开 | 信令要不要 TLS |
| `--ca-bundle <pem>` | 无 | **对 broker 没用**（它的证书是自签的）——留着只是为了老的命令行不报错 |
| `--tls-pin <sha256>` | 无 | broker 证书的指纹，**只看不拦**：对不上会说一声，但不断隧道 |
| `--wait-timeout <秒>` | 300 | agent 侧等多久没人连就退出（0 = 一直等） |
| `--verbose` | 关 | 打印 ICE 的详细日志 |

---

## 八、TURN 账号从哪来

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

## 九、怎么确认通了

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

## 十、常见问题

| 现象 | 大概率原因 | 怎么办 |
| --- | --- | --- |
| `broker says: bad token` | 码是旧的 | 让设备侧**重新起**侧载，拿新的码 |
| `not the one that was pinned` | broker 换了证书 | **只是提醒**，隧道照用；想安静就把 `broker.txt` 里的 `pin=` 删掉 |
| `X509 - Certificate verification failed` | 老版本的侧载（1.2.9 及以前） | 升级到 1.2.10 以后：证书不再验，这种错不会再有 |
| `cannot reach the broker` | 地址错 / broker 没起 | 查 `broker.txt` 里的 address |
| 隧道通了但网页打不开 | `--target` 指错了 | 查 agent 侧的 `--target` |
| 打开 9931 是终端不是 a2 | agent 侧用了 `--target terminal` | 想看 a2 就把它改回 `--target 127.0.0.1:8383` |
| 端口起来了但连不上 | 隧道还没建好 | 等 `the data channel is open` |
| 设备上侧载自己退出了 | 5 分钟没人连 | 正常；要一直等着加 `--wait-timeout 0` |
| 中间断了 | broker 重启 / 网络抖 | **两边都会自己重连**，不用管 |
