# sched/ — tea_a2 的 Python 侧

tea_a2 本体是一个 C++ 单二进制（`src/`，Web UI 构建期嵌进去）。`sched/` 是**跑在它旁边的
Python 进程**，专门装那些「不该塞进二进制」的东西：任务队列、聊天前端、机群运维、语音转写，
外加两个自用的知识提炼工具。

两边的关系是**单向的**：Python 侧通过 HTTP 调 agent（`http://127.0.0.1:8282`），
agent 完全不知道 Python 侧的存在。所以：

- agent 单独就能跑，一个 Python 进程都没有也不影响任何功能；
- 所有策略（什么时候开工、做完通知谁）都写在 Python 里，不用改 C++；
- 设备上通常没有 pip，所以**随包发布的那些只用标准库**（`tests/smoke_py36.py` 盯着 Python 3.6 兼容）。

> 这一页是这一目录的**索引和说明书**。讲架构和策略的深度文档在仓库的 `docs/`
> （`docs/tasks.md` 队列、`docs/fleet.md` 机群、`docs/api.md` 接口、`docs/tea_a2_manual.md` 命令清单）。

## 谁该看哪一章

| 脚本 | 是什么 | 什么时候需要它 | 章节 |
|---|---|---|---|
| `task_server.py` | 任务队列（HTTP 服务，8283） | 想让活儿排队、等有额度时再做 | [任务队列](#任务队列) |
| `task_order.py` | 任务单文本格式的解析 | 想用文件提交任务 | [任务队列](#任务队列) |
| `task_ui.html` | 队列的网页界面 | 想在浏览器里看队列 | [任务队列](#任务队列) |
| `tea_api.py` | 调 agent / 队列的客户端库 | 想写自己的脚本 | [客户端库](#客户端库tea_apy) |
| `channel.py` | 聊天前端的共用半边 | 想加第三种聊天前端 | [聊天前端](#聊天前端) |
| `wechat_bot.py` | 微信端（iLink 协议，扫码登录） | 想用微信排任务 | [聊天前端](#聊天前端) |
| `telegram_bot.py` | Telegram 端（Bot API） | 想用 Telegram 排任务 | [聊天前端](#聊天前端) |
| `fleet.py` | 机群引擎（命令行） | 一台服务器管一批设备 | [机群](#机群) |
| `fleet_server.py` | 机群控制面（HTTP，8290） | 想让 agent/机器人驱动机群 | [机群](#机群) |
| `asr_server.py` | 语音转写侧车（OpenAI 兼容，8284） | 想用麦克风说话 | [语音转写](#语音转写) |
| `mail_watch.py` | 邮件看门狗：收信 → 判读 → 归档/起草 → 只在该我参与时通知 | 想让邮箱自己整理、只在要紧时打扰你 | [邮件看门狗](#邮件看门狗watcher) |
| `pan_sync.py` | 百度网盘同步配置：多台机器共用一份 provider.yaml（加密 + 冲突不覆盖） | 换机器不用重抄配置 | [配置同步](#配置同步百度网盘) |
| `pan_publish.py` | 把发布产物传到百度网盘（4MB 分片 + 回读校验）—— 包不再提交到 git | 发版 | [配置同步](#配置同步百度网盘) |
| `bili.py` | B 站观看历史 → 摘要 → 周报（自用） | 想看「我最近在看什么」 | [个人工具](#个人工具) |
| `zhihu.py` | 知乎抓取/阅读/发回答（自用） | 想抓知乎内容或自动答题 | [个人工具](#个人工具) |
| `browser_login.py` + `login_state.mjs` | 两个工具共用的登录入口 | 登录态过期了 | [个人工具](#个人工具) |

## 两种角色

**随包发布**（发版时复制进 release 的 `tasks/`，给终端用户）：
`task_server.py`、`task_order.py`、`task-order.template`、`task_ui.html`、`tea_api.py`、`wechat_bot.py`。

**开发者/自用**（不随包发布，但都在这个仓库里）：
`fleet.py`、`fleet_server.py`、`channel.py`、`telegram_bot.py`、`asr_server.py`、
`mail_watch.py`、`mail-watch.template`、`pan_sync.py`、`pan_publish.py`、`pan-sync.template`、
`bili.py`、`zhihu.py`、`browser_login.py`、`login_state.mjs`。

---

## 任务队列

**agent 是通道，队列在 Python 侧。** 不是所有事都要马上做：「排队」这件事本身也可以排 ——
把活儿写进队列，等供应商还有额度时再让 agent 去做。

```bash
python3 sched/task_server.py                      # 起队列（默认 127.0.0.1:8283）
python3 sched/task_server.py --submit "把 README 的构建例子跑一遍"
```

- 队列只听 HTTP：`/api/tasks`（列表/提交）、`/api/tasks/{id}`（状态）、`/api/events`（事件流）、
  `/api/audit`、`/api/health`、`/api/task-order-template`；另外把 agent 的几个路由
  （`/api/chat`、`/api/chat/stop`、`/api/providers/{n}/usage`、`/api/workspaces`、`/api/projects`）代理过来。
- **额度闸门**：开工前先查 agent 的 `GET /api/providers/{n}/usage`，
  余额不足就等到下一个窗口（`--min-remaining`、`--quota-interval`）。
- **并发**：`--max-concurrent`（0 = 不限）；**僵尸任务**：`--stale-after` 超时自动回收。
- 策略全在 Python：`--provider`（默认 `minimax`）、`--window window|weekly`、`--workspace`。
- 任务单是**纯文本**（`key: value` + 缩进续行 + 列表），**不是 yaml**（标准库没 yaml 解析器，
  而队列要跑在装不了东西的设备上）。模板见 `task-order.template`；写错 key 会带行号报错，
  不会悄悄丢掉一个设置。
- 网页界面：`sched/task_ui.html`（由队列服务托管）。

深层说明：`docs/tasks.md`。

## 客户端库（tea_api.py）

写脚本时用的一层薄封装，两个类对应两个服务：

```python
from tea_api import Tea, Queue

tea = Tea("http://127.0.0.1:8282")     # agent：模型调用、provider、额度、工作区
quota = tea.usage("minimax")           # 还剩多少
q = Queue("http://127.0.0.1:8283")     # 队列：任务记录、事件
```

`Tea` 对应 C++ 的 `/api/chat`（SSE）、`/api/providers/{n}/usage`、`/api/workspaces`；
`Queue` 对应队列的 `/api/tasks`。注意**任务路由已经搬到 Python 侧**了 —— 想操作任务请用 `Queue`，
不是 `Tea`。

## 聊天前端

`channel.py` 是**所有聊天前端都要做的同一半**（同一套命令、同一个固定工作区、同一个收件箱、
同一套通知）；传输层是子类，只实现 `send_text` / `send_file`。

给微信发一句话 = 排一个任务；`/help` 有全部命令。

```bash
python3 sched/wechat_bot.py --login     # 扫码登录（一次就行）
python3 sched/wechat_bot.py             # 跑起来
python3 sched/telegram_bot.py --token 123456:ABC --print-chat-id
python3 sched/telegram_bot.py --token 123456:ABC --allow-chat 12345
```

- **微信**走 iLink 协议（base `https://ilinkai.weixin.qq.com`）。每条回复必须带 `context_token`
  （errcode -14 = 会话过期，要重扫）；媒体走 iLink 自己的加解密（AES-128-ECB，key = md5(token)）。
- **Telegram** 走 Bot API，token 从 `TEA_TELEGRAM_TOKEN` 或 `--token` 来。
- 两边都有白名单：`--allow-path`（允许 agent 碰的路径）、`--allow-chat`（Telegram 的 chat id）、
  `--allow-any-chat`（别用）。
- **任务做完时发的是「最后的完整答案」**（任务记录里的 `result`，也就是模型那一轮的
  最终回复），不是进度汇报的话。模型在排队任务里是被要求汇报进度的（那条汇报写在
  记录的 `note` 里），早先通知发的是 `note`、答案只给前 300 字——现在只发完整答案，
  没有答案才退回那句进度备注。
- **长答案分段发**（Telegram 每段 3800 字、微信 1800 字），不截断：完整答案要完整到。
- 状态目录：`~/.tea_agent/wechat`、`~/.tea_agent/telegram`；各自的工作区在
  `~/.tea_agent/*-workspace`。

## 机群

一台学校服务器、40 台设备，每台跑一个 a2。`fleet.py` 是**确定性的那半**（走设备的
`/api/exec`，不经过模型：40 台重启服务 = 40 个 HTTP 请求，不烧 token）；`fleet_server.py`
是它的 HTTP 控制面。

```bash
python3 sched/fleet.py devices                         # 台账
python3 sched/fleet.py collect                         # 问每台设备「你怎么样」
python3 sched/fleet.py report                          # 今天的汇总
python3 sched/fleet.py exec --command "systemctl restart nginx" --group lab
python3 sched/fleet.py upgrade --package nginx --group lab
python3 sched/fleet.py ask "磁盘还剩多少"               # 让每台设备自己的 agent 回答
python3 sched/fleet_server.py                          # 控制面（默认 127.0.0.1:8290）
python3 sched/fleet_server.py --collect-once           # 只跑一次采集
```

- 台账在 `~/.tea_agent/fleet/devices.json`（`TEA_FLEET_HOME` 可改），**只有这一份**。
- 两个不同的密钥：调用方要带 `token`（`~/.tea_agent/fleet/token`），服务给设备看的是
  `device-token`（`device-token` 文件）。
- 选设备的办法：`--group` / `--device` / `--label key=value`，都能重复。
- 批量要稳：`--verify`（改完必须成功的命令）、`--rollback`（怎么回滚）、`--batch-size`（一批几台）。
- 三种驱动方式，模型 involvement 递增：本模块（确定性）→ fleet server（HTTP）→
  设备自己的 agent（`fleet.py ask`）。

深层说明：`docs/fleet.md`。

## 语音转写

`asr_server.py` 是个侧车：跑 SenseVoiceSmall（int8 ONNX，~240MB），
对外是 OpenAI 兼容的 `POST /v1/audio/transcriptions`。**模型不进 C++ 二进制** ——
onnxruntime 会把「单文件 3.8MB」这个卖点破掉。

```bash
python3 sched/asr_server.py --download        # 一次性：拉模型到 ~/.tea_agent/asr
python3 sched/asr_server.py                   # 起服务（127.0.0.1:8284）
```

- agent 侧在 `provider.yaml` 顶层配 `asr:` 段（url/token/模型），agent 只负责转发录音；
  分工和机群一样：重的东西放在重的东西该在的地方。
- **只有「整段识别」一种形态**：sherpa-onnx 的 `OnlineRecognizer` 没有 `from_sense_voice`，
  所以模型级流式不存在（协议还是一次性的，想流式得分块累加重解码）。
- 实测 int8 比实时快 30 倍（10s 音频 → 0.325s），等待时间约等于说话时间。

## 邮件看门狗（watcher）

`mail_watch.py` 是**第三个方向**的对接：不是 agent 去调别人、也不是别人来调它，而是
**别人那边发生了事**（新邮件）自己走进来。它守着收件箱，一封信一次 `/api/llm/chat`：

```bash
python3 sched/mail_watch.py init              # 写配置模板到 ~/.tea_agent/mail/config
python3 sched/mail_watch.py check             # 配置 / IMAP / agent 是否都通
python3 sched/mail_watch.py peek --limit 3    # 只看判读，不动邮箱（调提示词用）
python3 sched/mail_watch.py run --once --dry-run
python3 sched/mail_watch.py run               # 常驻（默认 5 分钟一轮）
python3 sched/mail_watch.py status            # 处理到哪儿了
```

- **不需要我参与的**：贴 `tea/done` 并移出收件箱（Gmail 是去掉 `\Inbox`，不删除），我这边一声不响；
- **需要我参与的**：贴 `tea/needs-me`、留在收件箱、草稿写进 Gmail 草稿箱（**不发**），
  才发一条 Telegram / webhook；
- **要实际干活的**（判定里有 `follow_up`）：再排一个任务给队列，agent 做完通知我。

判不准（模型不回 JSON）一律当成「需要我」：多打扰一次比漏一封信便宜。每一次动作都记在
`~/.tea_agent/mail/digest/YYYY-MM-DD.md`。配置、Gmail 应用专用密码、systemd 单元、
以及**怎么把同一套循环套到日历/RSS/Slack/工单**上，见
[与其他应用对接](../docs/integrations.md)；测试是 `tests/smoke_mail_watch.py`。

## 配置同步（百度网盘）

`pan_sync.py` 让**多台机器共用一份配置**：provider.yaml、tea_agent.yaml、mcp.yaml
这些「跟谁说话、怎么干活」的文件，打包、加密，传到百度网盘的一个固定目录；另一台机器
再拉下来。**同步的是配置，不是历史、也不是清单** —— 会话记录、记忆、日志一律不同步；
`workspaces.json`（工作区清单，里面是本机绝对路径）也不同步。

```bash
python3 sched/pan_sync.py auth                # 授权一次（给个网址 + 用户码）
python3 sched/pan_sync.py check               # 配置 / 令牌 / 网盘通不通
python3 sched/pan_sync.py status              # 本地和远端各是什么，有没有冲突
python3 sched/pan_sync.py push                # 把本地配置推上去
python3 sched/pan_sync.py pull                # 把远端配置拉下来
python3 sched/pan_sync.py sync                # 先拉后推（日常用这个）
python3 sched/pan_sync.py watch --interval 300
```

- **推上去之前先加密**：provider.yaml 里有 API key。口令用 PBKDF2 派生密钥，整包加密 +
  HMAC 校验（encrypt-then-MAC）；没口令不推。
- **两边都改了就停手**：远端比上次同步新、本地也改了 —— 谁覆盖谁都是猜，脚本不动任何
  一边，把两边说清楚，让人来定（`--force` 才覆盖）。同步工具最忌讳默默覆盖。
- **写之前先备份**：旧版本存进 `~/.tea_agent/pan-sync/backup/<时间戳>/`。
- **删除不传播**：本地删了文件，远端的包里就没有它了；但拉取不会删掉别的机器上那份。
- **只同步「配置」，不碰「清单」**：`workspaces.json` 是每台机器自己的（里面是本机的绝对
  路径和会话计数），**不在默认名单里**；拉取时也只写名单里的文件，远端包里名单外的会被跳过。

配置在 `~/.tea_agent/pan-sync/config`（模板 `sched/pan-sync.template`），令牌在
`token.json`（0600），状态在 `state.json`。细节（百度网盘接口、加密做法、配额、坑）
见 [百度网盘同步配置](../docs/pan-sync.md)；测试是 `tests/smoke_pan_sync.py`。

**发布产物也走这里**（2026-10-30 起包不再提交到 git）：

```bash
python3 sched/pan_publish.py release/tea_a2-0.17.5.zip     # → /tea_a2/releases/0.17.5/
```

按 4MB 一片上传（分片数 = ceil(大小/4MB)，最后一片是余数，小文件走单片），传完**回读校验**
sha256，远端留一份 `release-manifest.json`。`--keep 5` 让网盘上只留最新的 5 个发布包
（按版本号排），`--list` 看现在有哪些版本。发版流程见
[构建与跨平台编译](../docs/build.md#发版包传到百度网盘不进-git)。

## 个人工具

两个自用工具，同一套结构：纯 HTTP + cookie 抓内容 → 送 LLM 出结构化摘要 → markdown digest。
产物在 `~/.tea_agent/bili/`、`~/.tea_agent/zhihu/`；LLM 配置共用 `~/.tea_agent/provider.yaml`
（第一个 provider 就是默认）。

```bash
python3 sched/bili.py  --login                 # 登录（B 站）
python3 sched/bili.py  fetch --pages 2 --limit 20
python3 sched/bili.py  summarize && python3 sched/bili.py digest --overview

python3 sched/zhihu.py --login                 # 登录（知乎）
python3 sched/zhihu.py whoami | hot | search "大模型" | invited | collections | moments
python3 sched/zhihu.py fetch --collection 737068447
python3 sched/zhihu.py summarize && python3 sched/zhihu.py digest --overview
python3 sched/zhihu.py post answer --file body.md --question <id> --yes   # 真发回答
python3 sched/zhihu.py post llm --count 10                              # 读最近看过的 10 篇 → 让 LLM 写一篇新的
python3 sched/zhihu.py rm --answer <id>                                 # 删掉
```

### post llm：让模型写一篇新的

知乎**没有浏览历史接口了**（`/api/v4/history` 404，网页 `/history` 也是 404），
所以「最近读过的」从**收藏夹**（按收藏时间）和**本地语料**（`fetch` 抓过的）两处凑，
去重后按时间倒序取前 N 篇。

```bash
python3 sched/zhihu.py post llm                    # 读 10 篇 → 分析 → 写一篇 → 只显示
python3 sched/zhihu.py post llm --count 6 --out /tmp/a.md
python3 sched/zhihu.py post llm --yes              # 真投递到知乎专栏
```

- 默认**只打印**文稿（`--yes` 才投递）；文稿也写到 `~/.tea_agent/zhihu/draft.md`。
- 模型被问两次：一次**分析**这些文章（母题/角度/可用素材），一次**创作**（标题 + 正文）。
- 正文是 markdown 纯文本（空行分段），转成知乎编辑器要的 HTML 后发出去。
- 投递的流程是抓包确认的（`POST zhuanlan.zhihu.com/api/articles/drafts` 建草稿 →
  `POST www.zhihu.com/api/v4/content/publish`，`action=article`，正文在 `data.hybrid.html`）。
- 风格是**中文硬科幻的通用手法**（宏大尺度 + 危机 + 硬核细节 + 冷峻抒情），
  prompt 里不冒充任何具体作家的身份。

- **登录**：`--login` 开一个**有头浏览器**（playwright，`headless=false`）停在登录页，
  你登录完**把窗口关掉**即可，storageState 会被存到工具读的那个文件
  （知乎 `z_c0` / B 站 `SESSDATA` 是成功判据）。没有图形界面时自动退回无头 + 截二维码
  （`--headless` 可强制）。共用实现：`sched/login_state.mjs`（浏览器）+ `sched/browser_login.py`（编排）。
- **B 站的坑**：字幕会「串台」（`x/player/v2` 有时返回别的视频的轨），必须用权威轨道列表校验，
  宁可没字幕也不要错的。
- **知乎的坑**：上了 x-zse-96 签名，问题详情和问题页 403；发回答要走
  `read_history → draft → content/publish` 三步（老的 `/answers` 接口已废）。
- 知乎工具还会**自动关注**刚回答的问题、并在调「等你来答」接口时把邀请标为已读。

---

## 共享约定

### 端口

| 端口 | 谁 | 环境变量 |
|---|---|---|
| 8282 | tea_a2 agent（默认） | `--port` |
| 8283 | 任务队列 | `TEA_TASK_PORT` |
| 8284 | 语音转写侧车 | `TEA_ASR_PORT` |
| 8290 | 机群控制面 | `TEA_FLEET_PORT` |

> systemd 单元里 agent 用的是 **8383**（`scripts/a2.service`），队列/机器人用 `--agent` 指过去。

### 状态目录

都在 `~/.tea_agent/` 下：`bili/`、`zhihu/`（工具数据）、`mail/`（邮件看门狗：配置+书签+digest）、
`asr/`（模型）、`fleet/`（台账+密钥）、
`wechat/`、`telegram/`（各端状态）、`*-workspace/`（各端的固定工作区）、`provider.yaml`（LLM 配置）。

### 环境变量

agent / 队列：`TEA_AGENT_URL`、`TEA_AGENT_WORKSPACE`、`TEA_TASK_URL`、`TEA_TASK_PORT`、
`TEA_TASK_PROVIDER`、`TEA_TASK_WORKSPACE`、`TEA_TASK_WORKSPACE_NAME`、`TEA_TASK_DATA`；
机群：`TEA_FLEET_HOME`、`TEA_FLEET_HOST`、`TEA_FLEET_PORT`、`TEA_FLEET_DEVICE_PORT`、
`TEA_FLEET_TIMEOUT`、`TEA_FLEET_CONCURRENCY`、`TEA_FLEET_TOKEN`；
语音：`TEA_ASR_HOST`、`TEA_ASR_PORT`、`TEA_ASR_MODEL_DIR`；
聊天端：`TEA_TELEGRAM_TOKEN`、`TEA_TELEGRAM_API`、`TEA_WECHAT_CDN`；
个人工具：`BILI_STATE`/`BILI_HOME`、`ZHIHU_STATE`/`ZHIHU_HOME`；
邮件看门狗：`TEA_MAIL_HOME`（状态目录）、`TEA_MAIL_CONFIG`（配置文件）。

### 几条硬规矩

- **只用标准库**（随包发布的部分）：设备上装不了 pip。Python 3.6 也要能跑
  （`http.server.ThreadingHTTPServer`、`subprocess.run(capture_output=…)` 这些 3.7+ 的东西不能用）。
- **不碰 agent 的内部**：Python 侧只通过 HTTP API 说话；C++ 侧不需要知道队列/机群/语音的存在。
- **密钥不写进代码**：provider.yaml 里的 key、放行白名单（`--allow-chat`）、机群的两个 token
  都从环境变量或独立文件读。
- **日志**：写 `~/.tea_agent/logs/`，systemd 单元已配好 `ExecStartPre` 建目录。

### systemd 单元

`scripts/` 下现成的有：`a2.service`、`a2_task.service`、`a2_wechat.service`、
`a2_telegram.service`、`a2_fleet.service`、`a2_fleet_collect.service` + `.timer`（每天采集）。
邮件看门狗也能这么起，单元片段见 `docs/integrations.md`。
安装见 `scripts/install-units.sh`。

### 打包

发版时把这些文件复制进 release 的 `tasks/`：`task_server.py`、`task_order.py`、
`task-order.template`、`task_ui.html`、`tea_api.py`、`wechat_bot.py`（详见 `tea_a2_agent.md` 的发版流程）。
