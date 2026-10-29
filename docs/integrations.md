# 与其他应用对接

tea_a2 是个**被动**的东西：有人跟它说话，它才动。要让它在「别的地方发生了事」的时候自己
动起来 —— 新邮件、日历提醒、CI 挂了、工单来了 —— 需要的不是再给它加一个内置功能，而是
把**触发**和**出口**这两头接上。这一页讲对接的三个方向、为什么第三种放在 Python 侧，
以及一个已经落地的例子：收件箱看门狗（`sched/mail_watch.py`）。

---

## 三个方向

| 方向 | 场景 | 用什么 | 现成的东西 |
| --- | --- | --- | --- |
| **它调别人** | agent 干活时需要某个应用的能力（发消息、查库、点界面） | MCP server | `mcp.yaml`，见 [MCP](mcp.md) |
| **别人调它** | 脚本 / 服务 / 另一台机器要驱动它 | `POST /api/chat`（SSE）、原语 `/api/llm/chat`、队列 API、`/api/peer/*`、机群 | [HTTP 接口](api.md)、[级联](peer.md)、[机群](fleet.md) |
| **别人那边发生事** | 新邮件、订阅更新、提醒、告警 | **watcher**：`sched/` 里一个常驻脚本 | 这一页 + `sched/mail_watch.py` |

前两个方向都齐了，第三个一直是空的 —— 而「定期从 Gmail 查邮件、自动处理、只在该我参与时
通知我」正好落在第三个方向：**事件在别人那边发生**，agent 不在场。

### 为什么 watcher 写在 Python 侧

和任务队列、微信/Telegram 前端同一个理由（见 [sched/README](../sched/README.md)）：

- **策略不是功能**。「什么样的邮件算需要我」「打不打扰我」「要不要交给 agent 去办」都是
  口味问题，改一行配置就该生效，不该重新编译；
- **坏了不影响本体**：watcher 挂了，agent 照常能聊；
- **只用标准库**：`imaplib` / `email` / `urllib` 就够，设备上没有 pip 也能跑；
- **判读借 agent**：一次 `POST /api/llm/chat`，provider.yaml 里的 key、模型、超时都不用再抄一遍，
  换模型就换配置。

---

## watcher 的形状

```
  源（IMAP / HTTP / 文件 / 别的应用）
    │  fetch_new()      新东西 + 「处理到哪儿了」的书签
    ▼
  判读  judge()         一次 /api/llm/chat → 结构化 JSON：要不要我、多急、做什么
    │
    ├──► 动作 apply()      这个应用的真实动作（Gmail：贴标签 / 移出收件箱 / 写草稿）
    ├──► 留痕 digest()     ~/.tea_agent/<watcher>/digest/YYYY-MM-DD.md，按天记
    ├──► 出口 notify()     telegram / webhook / file —— **只在 needs_me 时**
    └──► 交办 queue()      follow_up 非空 → 排进任务队列，agent 真去做，做完通知我
```

三条硬规矩，`mail_watch.py` 和以后的 watcher 都该守：

1. **发送类的动作留给人**。邮件这一头最后一步是「把草稿放进草稿箱」，不是「发出去」。
   自动回信不是「以后再做的功能」，是一条不该越过的线。
2. **判不准就不动它**。模型答非所问、超时、返回的不是 JSON —— 一律按「需要我」处理：
   留在收件箱、通知人。宁可多一次打扰，不可悄悄归档一封信。
3. **每次都留痕**。就算通知没发出去，digest 里也有「什么时候、哪封信、判成什么、动了什么」。

---

## 邮件：`sched/mail_watch.py`

一个文件，标准库，1300 行左右（注释比代码多）。Gmail 走应用专用密码，别的 IMAP 服务器改 `host`/`port` 就行。

### 1. 拿应用专用密码（Gmail）

账号 → 安全 → 两步验证 → [应用专用密码](https://myaccount.google.com/apppasswords)，
生成 16 位一串（`abcd efgh ijkl mnop`）。**不是账号密码**；Workplace 管理员关了应用专用密码
的话，这条路走不通，得换成 OAuth2（见文末「还缺什么」）。

### 2. 写配置

```bash
python3 sched/mail_watch.py init        # 把模板写到 ~/.tea_agent/mail/config（600）
$EDITOR ~/.tea_agent/mail/config
```

模板在 `sched/mail-watch.template`，全部键都有注释。最小的配置长这样：

```text
address: me@gmail.com
password: abcd efgh ijkl mnop

agent: http://127.0.0.1:8383      # 判读用哪个 agent
provider: deepseek                # 留空 = agent 的默认 provider
model: deepseek-flash

notify: telegram                  # 先跑几天可以用 file（只写 digest，不打扰）
telegram_token: 123456:AA...
telegram_chat: 6460753829
```

判得准不准，八成取决于 `rules:` 那一段。它是**原样塞进提示词**的人话：

```text
rules: |
  · 老板（boss@example.com）和家人的信一律算需要我，不看内容。
  · 开发票、续费、到期提醒：需要我，动作 label。
  · 各种周报、CI 通知、GitHub/知乎/B 站通知：不需要我，归档。
  · 带「面试」「报价」「合同」字样的，紧急。
```

### 3. 先看，再信

```bash
python3 sched/mail_watch.py check             # 配置 / IMAP 登录 / agent 在不在
python3 sched/mail_watch.py peek --limit 3    # 只判读，不动邮箱，不记账
python3 sched/mail_watch.py run --once --dry-run   # 整条流水线，但一封都不动
python3 sched/mail_watch.py run --once        # 真跑一轮
```

`peek` 会打出每封信的判定、摘要、草稿 —— 提示词不好就改 `rules:`，不用碰代码。

### 4. 常驻

```bash
setsid nohup python3 sched/mail_watch.py run >> ~/.tea_agent/logs/mail_watch.log 2>&1 &
python3 sched/mail_watch.py status            # 处理到哪儿了、今天处理了什么
python3 sched/mail_watch.py notify-test       # 通知通不通
```

systemd user unit（和 `scripts/a2_task.service` 一个套路）：

```ini
[Unit]
Description=tea_a2 邮件看门狗
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 %h/work/git/tea_a2/sched/mail_watch.py run
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
```

同一时刻只允许一个 watcher（`~/.tea_agent/mail/watcher.lock`，pid 死了会被接管）。

### 5. 它到底动了什么

| 判定 | 邮箱里发生什么 | 我这边 |
| --- | --- | --- |
| 不需要我参与 | 贴 `tea/done`，**移出收件箱**（Gmail 里是去掉 `\Inbox`，不删） | 安静，只进 digest |
| 需要我参与 | 贴 `tea/needs-me`，留在收件箱；有 `draft_reply` 就把草稿写进 Gmail 草稿箱 | Telegram/webhook 一条消息 |
| 要实际做一件事 | 同上 | 另外排一个任务给 tea_a2，做完它再通知我 |

草稿带 `In-Reply-To` / `References`，所以在 Gmail 里是**这封信的回复**，不是一封孤零零的新草稿。
发不发由人按。

运行时文件：

```
~/.tea_agent/mail/config                 配置（600）
~/.tea_agent/mail/state.json             uidvalidity / 书签 / 处理过的信的指纹
~/.tea_agent/mail/digest/2026-10-29.md   按天的流水账
~/.tea_agent/mail/watcher.lock           单实例锁
```

### 6. 成本

一封信一次 `llm_chat`：正文截到 4000 字、引用砍掉，deepseek-flash 上大约 1~3k token。
一天 50 封是「几分钱」的量级。`max_per_round` 是流控（一轮最多判几封，新信优先），
不是省钱开关 —— 省钱的开关是**别让广告进到判读**（在 Gmail 里先分好类，或者把 watcher
指到别的 mailbox）。

---

## 「只在该我参与时通知我」是怎么做到的

这是这个 watcher 的唯一目的，所以拆开说：

1. **判定标准写在提示词里**，不是黑盒：问问题、等我决定、有时限、真人写来的 → 需要我；
   群发、通知机器人、订阅摘要 → 不需要我。`rules:` 覆盖在上面。
2. **fail-safe 偏向打扰**：判读失败 = 需要我。模型的不确定性不该变成「悄悄归档」。
3. **三档出口**：静默（只进 digest）/ 打扰（telegram、webhook）/ 交办（队列任务）。
   `notify: file` 就是「只看不说」，适合先跑三天校准提示词。
4. **每轮最多打扰 5 次**（`MAX_NOTIFY_PER_ROUND`）：一次几百封信的时候不会炸出一屏通知，
   多出来的只进 digest，下一轮还有机会。
5. **去重**：message-id 指纹 + uid 书签 + UIDVALIDITY 变化时重置（Gmail 会重置一次就全废）。
6. **通知里带够判断的信息**：主题、发件人、分类、为什么找你、摘要、草稿预览、Gmail 链接。

---

## 接到别的应用（配方）

同一个形状，换个源：

| 来源 | 取什么 | 判读（问模型什么） | 动作 / 出口 |
| --- | --- | --- | --- |
| 日历 | 未来 24h 的事件（CalDAV） | 哪个要我准备、要带东西、要提前出发 | 提前 N 分钟提醒；要准备的排成任务 |
| RSS / 博客 | 新条目（HTTP + 指纹） | 跟我的方向有关吗，值不值得读 | 一天一条摘要；重要的单独通知 |
| Slack / 微信群 | 我被 @ 的消息 | 是不是真的要回，回什么 | 草稿回复（贴回聊天）；要办事的排任务 |
| GitHub / Gitee | 我的 issue、review 请求 | 谁在等我、卡了多久 | 排任务（改代码/回复），或每天一次汇总 |
| CI / 部署 | 失败构建 | 是不是我的改动、要不要立刻处理 | 失败通知；要我修的排任务 |
| 工单 / 客服 | 新工单 | 分类、紧急度、能不能自动答 | 起草回复，人按发送 |
| 语音留言 / ASR | 转写文本 | 是不是找我、要不要回电话 | 通知 |

写法就三个方法。把 `mail_watch.run_round()` 拿出来用，循环、留痕、去重、通知都是现成的：

```python
class MySource(object):
    def fetch_new(self, state, limit=10):
        """新的东西 + 书签；返回 dict 列表，每个至少要有 id / title / text"""

    def add_labels(self, item, labels):  ...   # 动作，按这个应用的方式
    def archive(self, item): ...               # 没有「归档」这个概念就 pass

def judge(item):                               # 也可以用 mail_watch.Judge 换个提示词
    answer = Tea("http://127.0.0.1:8383").llm_chat(
        prompt=my_prompt(item), system=MY_SYSTEM, temperature=0.1)
    return mail_watch.normalize_decision(mail_watch.extract_json(answer["content"]))
```

判读的输出协议是固定的（`category` / `needs_me` / `urgency` / `why` / `summary` / `action` /
`draft_reply` / `follow_up`），换源不用换协议 —— 这样 `run_round`、digest、通知、队列交办
全都能复用。

### 什么时候**不要**写 watcher

- 只是想让 agent **会用**某个应用 → 写 MCP server（`docs/mcp.md`），别写 watcher；
- 只是想让**别的程序**驱动 tea_a2 → 直接调 HTTP 接口（`docs/api.md`），或者用级联
  （`docs/peer.md`）让另一台机器上的 a2 去干；
- 那件事**本来就该在对话里做** → 别自动化。watcher 只处理「人不在场时发生的事」。

---

## 还缺什么

诚实清单，按值得做的顺序：

1. **队列没有「只通知、不干活」的记录类型**。watcher 的通知只好自己发（webhook/telegram），
   绕过了队列的事件流 —— 也就是说，网页上的队列看不到这些通知。加一个 `notice` 记录
   （状态直接 `done`、不跑模型）会让出口统一。
2. **没有 watcher 基类**。现在一个源一个脚本，各自常驻、各自一套 state。第二个源落地时
   应该把 `run_round` / `State` / `Lock` / `Notifier` 抽成 `sched/watch.py`，源只实现三个方法。
3. **agent 侧没有回调**（`docs/pending_task.md` 里 FR-01 的 `callback_url` 还缺）。所以
   「任务做完」只能靠机器人前端或轮询知道。
4. **Gmail 只有应用专用密码**。企业 Workspace 关了这条通道就得写 XOAUTH2（`imaplib.authenticate`，
   要一个 refresh token 换 access token 的循环）—— 代码里留的是 `login()` 一个口子。
5. **没有闭环**。我回了信之后，标签不会自动改；现在只按收件箱里的新信走。

---

## 验证

```bash
python3 tests/smoke_mail_watch.py     # 95 条：配置、解码、判定、动作、通知、书签、锁、digest
python3 tests/smoke_py36.py           # mail_watch 也要能在 3.6 上跑（只标准库）
```

测试里没有真邮箱（不该为了跑测试去动别人的收件箱），但 IMAP 这一层是**真的协议对话**：
`tests/smoke_mail_watch.py` 里有一个几百行的假 IMAP 服务器，CAPABILITY / LOGIN / SELECT /
UID SEARCH / UID FETCH（带 literal）/ UID STORE（`X-GM-LABELS`）/ APPEND 都真的走一遍，
所以「标签贴没贴上、信有没有移出收件箱、草稿是不是回复」是拿协议结果验的，不是看函数被调用过。
agent / 队列 / Telegram / webhook 也都是本地假服务。

真账号的联调只能手动：`check` → `peek` → `run --once --dry-run` → `run --once`。

← 回到 [README](../README.md) · 队列见 [任务队列](tasks.md) · 工具层见 [MCP](mcp.md)
