# VPS（103.232.92.54）上在跑的单元

这四份是**从那台机器上原样同步回来的**（`~/.config/systemd/user/`），
2026-11-18。**以后部署这台机器就照这份来**，别再从 `scripts/` 里推。

`scripts/*.service` 是通用模板（路径指向仓库检出 `%h/work/git/tea_a2`，
给开发机和随手装的环境用）；这里是一台**按发布包布局**装的生产机器，
两者有实质差别，所以分开放：

| | `scripts/`（通用模板） | 这里（VPS 实况） |
| --- | --- | --- |
| 代码在哪 | 仓库检出 `%h/work/git/tea_a2` | 发布包解开的 `%h/a2`（`bin/` + `sched/` + `scripts/`） |
| 二进制 | `%h/bin/a2` | `%h/bin/a2` → 符号链接到 `%h/a2/bin/a2`（换二进制不用改单元） |
| agent 的端口 | 8383 | 8383（**只听 127.0.0.1**） |
| 队列的端口 | 8283 | 8283（只听 127.0.0.1） |
| telegram 的 chat id | 不写（模板里讲怎么加） | 写死了一个（这台机器的） |
| LANG | zh_CN.UTF-8 | **C.UTF-8**（这台机器没装 zh_CN.UTF-8，用 C.UTF-8 才不刷 "cannot change locale"） |
| broker 的 Documentation | 仓库里的手册 | GitHub 上的手册（这台机器没有仓库检出） |

## 这台机器上现在有什么、没有什么

```
127.0.0.1:8383   a2          （Web UI，只听本机）
127.0.0.1:8283   a2_task     （任务队列）
127.0.0.1:8391   wechat 配对页（用的时候才起）
0.0.0.0:8382     a2_broker  （隧道信令，自带 TLS）
udp/3478         a2_broker  （STUN + TURN）
udp/49160-50175  a2_broker  （中继端口池，1016 个）
```

**nginx 已禁用**（`systemctl disable nginx`）：对外不再有「TLS + basic auth 的
8383」那一层。agent 只听 127.0.0.1，进这台机器靠**隧道**或 SSH 转发；
公网的 8382 是 **broker 的信令口**（自带 TLS），不是 agent 的网页。

## 怎么访问这台机器上的 a2 / a2_task

**nginx 已经禁用，agent 和队列都只听 127.0.0.1** → **从外面一律走 SSH 隧道**
（`-L` 本地转发），没有第二条路：

```bash
# agent 的网页 UI（VPS 的 8383 → 本机 19383）
ssh -N -L 127.0.0.1:19383:127.0.0.1:8383 vps
#   然后浏览器开 http://127.0.0.1:19383/

# 任务队列（VPS 的 8283 → 本机 18283）
ssh -N -L 127.0.0.1:18283:127.0.0.1:8283 vps
#   然后浏览器开 http://127.0.0.1:18283/
```

开发机上已经有一个现成的单元做这件事（不用自己敲 ssh）：

```bash
systemctl --user status vps-a2-forward.service     # 转发 127.0.0.1:19384 → VPS 的 8383
```

⚠️ 端口别搞混：VPS 那边 **agent=8383、队列=8283**（broker 的信令是 8382，
那是给隧道侧载用的，跟网页 UI 没关系）。

## 装/改

```bash
cp deploy/vps/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart a2 a2_task a2_broker
```

## 几个不能忘的细节

- **`~/.tea_agent/peer-token.env` 必须存在**（0600）：agent 的 peer 令牌从那儿读，
  不写在命令行里（`ps` 看得见）。单元里是 `EnvironmentFile=`（不是 `-` 开头），
  文件不在单元就起不来 —— 那是故意的：没有令牌的 agent 不该悄悄跑起来。
- **停机要等正在跑的对话做完**：`TimeoutStopSec=660`（agent 自己会等 600 秒，
  见 `TEA_AGENT_DRAIN_SECONDS`）。systemd 默认只等 90 秒就 SIGKILL —— 那会把
  对话打断。想立刻停：`systemctl --user kill a2`。
- **日志不在 journald 里**：这台机器的 `/etc/systemd/journald.conf` 是
  `Storage=none`，`journalctl --user` 读不到，所以单元直接 append 到文件：
  `~/.tea_agent/logs/{a2.console,a2_task,a2_telegram,a2_broker}.log`。
- **broker 的单元必须带 LimitNOFILE / TasksMax**：512 条信令连接 + 512 个中继
  socket 会超过 systemd 的默认上限（fd 1024 / 线程 1008）。
- **broker 换二进制前先看云安全组**：端口池放到 49160-50175 就要放行这段 UDP；
  放行之前就换 = 中继静默不通（Allocate 成功、数据出不去，最难查的故障形态）。
  内核参数见 `docs/tunnel_broker_manual.md`（`/etc/sysctl.d/99-a2-broker.conf`，
  **不是** `~/.config/sysctl.d` —— `sysctl --system` 不读那儿）。
