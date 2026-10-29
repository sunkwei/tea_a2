# -*- coding: utf-8 -*-
"""收件箱 → 判读 → 归档/贴标签/写草稿 → 只在该我参与时通知。

tea_a2 本身是**被动**的：有人跟它说话它才动。这个脚本补的是另一半 ——
让「外面发生的事」自己走进来。它是 sched/ 这一侧的老路子：

    IMAP（谁来的信） → agent 的 /api/llm/chat（判读） → IMAP（动作）
                                                     → 通知 / 队列（需要人）

    python3 sched/mail_watch.py check            # 配置、登录、agent 是否都在
    python3 sched/mail_watch.py peek --limit 3   # 只看判读结果，不动邮箱
    python3 sched/mail_watch.py run --once       # 跑一轮
    python3 sched/mail_watch.py run              # 常驻（默认 5 分钟一轮）
    python3 sched/mail_watch.py run --dry-run    # 常驻但不动邮箱
    python3 sched/mail_watch.py notify-test      # 试一下通知走不走得通
    python3 sched/mail_watch.py status           # 处理到哪儿了

配置：`~/.tea_agent/mail/config`（`TEA_MAIL_CONFIG` 可改），格式是
`键: 值` 的纯文本，模板见 `sched/mail-watch.template`。

几条刻意的选择：

  * **不发信**。能做的最后一步是「把草稿放进 Gmail 草稿箱」；发不发由人按。
    自动发信不是一个「以后再说」的功能，是一条不该越过的线。
  * **判读失败就不动它**：模型答非所问时按「需要我」处理（留在收件箱、通知
    我），而不是猜一个 archive。宁可多一次打扰，不可丢一封信。
  * **每次动作都留痕**：`~/.tea_agent/mail/digest/YYYY-MM-DD.md` 里按天记，
    通知只是它的摘要。就算通知没发出去，也知道自己处理过什么。
  * **通用 IMAP**，不是 Gmail 专用：`host` 换掉就是 QQ 邮箱/Outlook/自建。
    Gmail 的标签用 `X-GM-LABELS`（不建文件夹、不动邮件本身），别的服务器
    退回成「建文件夹 + COPY」。
  * **标准库**：imaplib / email / urllib。判读借 agent 的 `/api/llm/chat`，
    所以 provider.yaml 里的 key、模型、超时都不用在这儿再抄一遍。

想接别的应用（日历、Slack、RSS、工单），照这三个缝换就行：`fetch_new()`
拿新东西、`judge()` 判读、`apply_decision()` 动手 —— 循环和留痕不用改。
见 docs/integrations.md。
"""

import argparse
import email
import email.header
import email.mime.text
import email.utils
import hashlib
import imaplib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tea_api import ApiError, Queue, Tea  # noqa: E402

HOME = os.path.expanduser("~")
CONFIG_DIR = os.environ.get("TEA_MAIL_HOME") or os.path.join(HOME, ".tea_agent", "mail")
CONFIG_PATH = os.environ.get("TEA_MAIL_CONFIG") or os.path.join(CONFIG_DIR, "config")
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")
DIGEST_DIR = os.path.join(CONFIG_DIR, "digest")
LOCK_PATH = os.path.join(CONFIG_DIR, "watcher.lock")

# 正文给模型看多少：够判断「这封信要我干什么」，又不会把一整串引用塞进去。
BODY_LIMIT = 4000
# 一封信最多通知一次；一轮里最多打扰几次（剩下的写进 digest，下轮再说）。
MAX_NOTIFY_PER_ROUND = 5
# state 里记多少「处理过的信」，超了就丢最旧的。
MAX_REMEMBERED = 2000

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
# 认识哪些键。写错一个键要带行号报错，不能悄悄不生效 —— 和任务单一个规矩。
DEFAULTS = {
    # 邮箱
    "address": "",
    "password": "",            # Gmail：应用专用密码（不是账号密码）
    "host": "imap.gmail.com",
    "port": "993",
    "mailbox": "INBOX",
    # 动作
    "label_done": "tea/done",
    "label_needs_me": "tea/needs-me",
    "archive": "yes",          # 不需要我参与的，移出收件箱
    "mark_seen": "yes",
    "drafts": "on",            # 需要我回复时，把草稿留在 Gmail 草稿箱
    "drafts_folder": "",       # 空 = 按 host 猜（Gmail 是 [Gmail]/Drafts）
    # 节奏
    "lookback_days": "3",      # 第一次跑往回看几天
    "max_per_round": "10",
    "interval": "300",
    # 判读
    "agent": "http://127.0.0.1:8282",
    "provider": "",
    "model": "",
    "rules": "",               # 额外的人话规则，原样塞进提示词
    "signature": "",           # 草稿的落款
    # 出口
    "notify": "file",          # telegram | webhook | file | none
    "telegram_api": "https://api.telegram.org",
    "telegram_token": "",
    "telegram_chat": "",
    "webhook": "",
    "tasks": "",               # 队列地址；留空 = 不排任务
    "workspace": "",           # 排任务用的工作区（路径或 id）
    "queue_followups": "yes",  # 判读出「要实际做一件事」时排进队列
}

BLOCK_KEYS = ("rules", "signature")

KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
TRUE_WORDS = ("1", "yes", "on", "true", "y", "开", "是")
FALSE_WORDS = ("0", "no", "off", "false", "n", "关", "否")


class ConfigError(Exception):
    """配置读不了。消息里带行号。"""


class Config(object):
    """一张扁平的表：键 -> 字符串。判空、转数字、转布尔在这里做一次。"""

    def __init__(self, values=None, path=""):
        self.values = dict(DEFAULTS)
        self.values.update(values or {})
        self.path = path

    def get(self, key, default=""):
        value = self.values.get(key)
        if value is None:
            return default
        return value

    def text(self, key):
        return (self.get(key) or "").strip()

    def integer(self, key):
        raw = self.text(key)
        try:
            return int(raw)
        except ValueError:
            raise ConfigError("%s 要一个数字，写的是 %r" % (key, raw))

    def boolean(self, key):
        raw = self.text(key).lower()
        if not raw:
            return False
        if raw in TRUE_WORDS:
            return True
        if raw in FALSE_WORDS:
            return False
        raise ConfigError("%s 要 yes/no，写的是 %r" % (key, raw))


def parse_config(text):
    """读配置：`键: 值`、缩进续行、`键: |` 的多行块、`#` 注释。

    不是 yaml —— 标准库里没有 yaml 解析器，而这个脚本要能裸 Python 跑。
    """
    values = {}
    errors = []
    block = None          # 正在攒的多行块的键
    lines = (text or "").splitlines()

    for number, raw in enumerate(lines, 1):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1] in (" ", "\t"):
            if block is None:
                errors.append("第 %d 行：这一行缩进了，但它属于上面哪个键？" % number)
                continue
            values[block].append(stripped)
            continue
        match = KEY_RE.match(stripped)
        if not match:
            errors.append("第 %d 行：看不懂这一行：%r" % (number, stripped[:40]))
            block = None
            continue
        key, value = match.group(1), match.group(2).strip()
        if key not in DEFAULTS:
            errors.append("第 %d 行：不认识的键 %r（认识的：%s）"
                          % (number, key, "、".join(sorted(DEFAULTS))))
        # 值后面的注释：只有「空格 + #」才算，邮箱里的 # 不受影响
        mark = value.find(" #")
        if mark >= 0:
            value = value[:mark].strip()
        if value in ("|", ">"):
            block = key
            values[key] = []
            continue
        block = None
        values[key] = value

    if errors:
        raise ConfigError("\n".join(errors))

    out = {}
    for key, value in values.items():
        if isinstance(value, list):
            out[key] = "\n".join(value).strip()
        else:
            out[key] = value
    return out


def load_config(path=None):
    path = path or CONFIG_PATH
    if not os.path.exists(path):
        raise ConfigError("没有配置文件：%s\n照 sched/mail-watch.template 抄一份，"
                          "填上地址和应用专用密码。" % path)
    with open(path, "r", encoding="utf-8") as handle:
        return Config(parse_config(handle.read()), path)


def guess_drafts_folder(host):
    if "gmail" in (host or "").lower():
        return "[Gmail]/Drafts"
    return "Drafts"


# ---------------------------------------------------------------------------
# 一封信
# ---------------------------------------------------------------------------
def decode_header_value(raw):
    """主题/发件人里的 =?UTF-8?B?…?= 要解成人能读的字。"""
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
    except Exception:  # noqa: BLE001 - 坏 header 不该让一整封信读不出来
        return str(raw)
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            out.append(text.decode(charset or "utf-8", "replace"))
        else:
            out.append(text)
    return re.sub(r"\s+", " ", "".join(out)).strip()


HTML_TAG = re.compile(r"<[^>]+>")
HTML_BLOCK = re.compile(r"(?i)</?(p|div|br|tr|li|h[1-6])[^>]*>")


def html_to_text(html):
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    text = HTML_BLOCK.sub("\n", text)
    text = HTML_TAG.sub("", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&") \
               .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


QUOTE_START = re.compile(
    r"(?im)^\s*(?:-{2,}\s*(?:原始邮件|Original Message|Forwarded message)|"
    r"On .{0,120} wrote:?|在 .{0,120}写道:?)")


def strip_quoted(text):
    """引用掉的部分对判读没用，砍掉能省一半 token（也少被旧内容带偏）。"""
    text = text or ""
    match = QUOTE_START.search(text)
    if match:
        text = text[:match.start()]
    lines = [line for line in text.splitlines() if not line.lstrip().startswith(">")]
    return "\n".join(lines).strip()


def part_text(part):
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


def body_of(message):
    """先找 text/plain；没有就把 text/html 扒成文本。附件一律不算正文。"""
    plain, html = [], []
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        if disposition.startswith("attachment"):
            continue
        kind = part.get_content_type()
        if kind == "text/plain":
            plain.append(part_text(part))
        elif kind == "text/html":
            html.append(part_text(part))
    text = "\n".join(plain).strip()
    if not text:
        text = html_to_text("\n".join(html)).strip()
    return strip_quoted(text)[:BODY_LIMIT]


def has_attachment(message):
    for part in message.walk():
        disposition = (part.get("Content-Disposition") or "").lower()
        if disposition.startswith("attachment"):
            return True
        if part.get_filename():
            return True
    return False


def to_message(uid, raw):
    """IMAP 拿来的一坨字节 → 判读要用的那个字典。

    这是两个世界之间的唯一转换点：上面是 IMAP 的 uid/标志/标签，下面是
    模型看到的「谁、什么时候、说了什么」。
    """
    message = email.message_from_bytes(raw)
    name, address = email.utils.parseaddr(decode_header_value(message.get("From")))
    to_name, to_address = email.utils.parseaddr(decode_header_value(message.get("To")))
    date_header = message.get("Date") or ""
    try:
        stamp = email.utils.parsedate_to_datetime(date_header).timestamp()
    except Exception:  # noqa: BLE001 - 日期写坏了不算事
        stamp = time.time()
    return {
        "uid": str(uid),
        "message_id": (message.get("Message-ID") or "").strip(),
        "from_name": name,
        "from_addr": address,
        "to": to_address,
        "subject": decode_header_value(message.get("Subject")),
        "date": stamp,
        "list_id": (message.get("List-Id") or "").strip(),
        "auto_submitted": (message.get("Auto-Submitted") or "").strip(),
        "precedence": (message.get("Precedence") or "").strip(),
        "attachment": has_attachment(message),
        "body": body_of(message),
        "raw": message,
    }


def message_key(msg):
    """同一封信的稳定指纹：重跑一轮不该重复处理。"""
    seed = (msg.get("message_id") or "").strip().lower() or ("uid:%s" % msg.get("uid"))
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def mail_link(msg):
    """在 Gmail 里打开这封信的链接（别的服务器给不出来就返回空）。"""
    message_id = (msg.get("message_id") or "").strip()
    if not message_id:
        return ""
    return ("https://mail.google.com/mail/u/0/#search/rfc822msgid%3A"
            + urllib.parse.quote(message_id.strip("<>")))


# ---------------------------------------------------------------------------
# 判读
# ---------------------------------------------------------------------------
TRIAGE_SYSTEM = """你是我的邮件秘书。我一天收到很多信，只有少数需要我亲自看。
你的工作是替我分流：判断这封信要不要我参与，不要我参与的就归档。

判断「要我才行」的标准（从严）：
  * 有人在问我问题、等我决定、要我提供信息；
  * 账单/到期/违约/预约确认这类有时限、要我做动作的；
  * 真人写给我的信（不是群发、不是通知机器人）；
  * 内容里有我需要知道的变化（被抄送了会议决定、账号异常、订单出问题）。
不要我参与的标准：
  * 广告、推广、订阅摘要、社区通知、自动报告、CI/系统告警里不需要我动手的那部分；
  * 群发名单（List-Id 有值）且没有点名问我。
不确定的时候判需要我：多打扰一次比漏一封信便宜。

只输出一个 JSON 对象，不要解释，不要 markdown 代码块：
{
  "category": "工作|私人|账单|通知|广告|垃圾|其他",
  "needs_me": true 或 false,
  "urgency": "high|normal|low",
  "why": "为什么需要我，一句话；不需要时写空字符串",
  "summary": "两句话以内说清这封信要我干什么",
  "action": "archive|label|star|keep",
  "draft_reply": "如果该回，写一段中文回复草稿；不该回就空字符串",
  "follow_up": "如果需要我（或 agent）实际去做一件事，写清做什么；没有就空字符串"
}
action 的含义：archive = 归档（不需要我参与的默认），label = 贴标签但留在收件箱，
star = 加星，keep = 完全不动。"""


def extract_json(text):
    """从模型回复里抠出第一个完整的 JSON 对象（它会加代码块、会加前言）。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    while start >= 0:
        depth = 0
        quoted = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:index + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


CATEGORIES = ("工作", "私人", "账单", "通知", "广告", "垃圾", "其他")
ACTIONS = ("archive", "label", "star", "keep")


def fail_safe_decision(why):
    """判读失败时：不归档、留给人看。"""
    return {
        "category": "其他", "needs_me": True, "urgency": "normal",
        "why": why, "summary": "判读失败，先留在收件箱里。",
        "action": "keep", "draft_reply": "", "follow_up": "", "failed": True,
    }


def normalize_decision(raw):
    if not isinstance(raw, dict):
        return fail_safe_decision("模型没给出 JSON")
    category = str(raw.get("category") or "其他").strip()
    if category not in CATEGORIES:
        category = "其他"
    urgency = str(raw.get("urgency") or "normal").strip().lower()
    if urgency not in ("high", "normal", "low"):
        urgency = "normal"
    action = str(raw.get("action") or "").strip().lower()
    needs_me = raw.get("needs_me")
    if isinstance(needs_me, str):
        needs_me = needs_me.strip().lower() in TRUE_WORDS
    needs_me = bool(needs_me)
    if action not in ACTIONS:
        action = "keep" if needs_me else "archive"
    if needs_me and action == "archive":
        # 需要我的信不该被藏起来
        action = "label"
    return {
        "category": category,
        "needs_me": needs_me,
        "urgency": urgency,
        "why": str(raw.get("why") or "").strip()[:300],
        "summary": str(raw.get("summary") or "").strip()[:600],
        "action": action,
        "draft_reply": str(raw.get("draft_reply") or "").strip()[:4000],
        "follow_up": str(raw.get("follow_up") or "").strip()[:600],
        "failed": False,
    }


def triage_prompt(msg, cfg):
    lines = [
        "发件人：%s <%s>" % (msg.get("from_name") or "", msg.get("from_addr") or ""),
        "收件人：%s" % (msg.get("to") or ""),
        "主题：%s" % (msg.get("subject") or "(无主题)"),
        "时间：%s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(msg.get("date") or time.time())),
        "邮件列表：%s" % (msg.get("list_id") or "无"),
        "自动发送：%s" % ((msg.get("auto_submitted") or msg.get("precedence") or "否")),
        "有附件：%s" % ("是" if msg.get("attachment") else "否"),
        "",
        "正文（已去掉引用）：",
        msg.get("body") or "（空）",
    ]
    rules = cfg.text("rules")
    if rules:
        lines += ["", "我的额外规则：", rules]
    return "\n".join(lines)


class Judge(object):
    """一封信 → 一个决定。借 agent 的 /api/llm/chat，所以不碰 provider 配置。"""

    def __init__(self, cfg, tea=None, out=None):
        self.cfg = cfg
        self.tea = tea or Tea(cfg.text("agent"))
        self.out = out or log

    def __call__(self, msg):
        try:
            answer = self.tea.llm_chat(
                messages=[{"role": "user", "content": triage_prompt(msg, self.cfg)}],
                system=TRIAGE_SYSTEM,
                provider=self.cfg.text("provider") or None,
                model=self.cfg.text("model") or None,
                temperature=0.1,
                max_tokens=1200,
            )
        except ApiError as exc:
            self.out("判读失败（agent）：%s" % exc)
            return fail_safe_decision("模型没答上来：%s" % str(exc)[:120])
        content = (answer.get("content") or "").strip()
        if not content:
            reasoning = (answer.get("reasoning") or "").strip()
            parsed = extract_json(reasoning) if reasoning else None
            if parsed:
                # 有的模型把 JSON 写在思考里、正文空着；能认出 JSON 就用它
                return normalize_decision(parsed)
            self.out("判读失败（空回复，finish_reason=%s）" % answer.get("finish_reason"))
            return fail_safe_decision("模型回了空话")
        return normalize_decision(extract_json(content))


# ---------------------------------------------------------------------------
# 邮箱（IMAP）
# ---------------------------------------------------------------------------
class MailError(Exception):
    """邮箱那边不配合：登录、搜索、动作。"""


# IMAP 的日期必须写成 26-Oct-2026 这种英文月份；strftime 跟着 locale 走，
# 在 zh_CN 下会给你「26-10月-2026」，服务器不认。
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def imap_date(epoch):
    stamp = time.localtime(epoch)
    return "%02d-%s-%04d" % (stamp.tm_mday, MONTHS[stamp.tm_mon - 1], stamp.tm_year)


def quote_label(label):
    return '"%s"' % (label or "").replace("\\", "\\\\").replace('"', '\\"')


class ImapSource(object):
    """一个 IMAP 邮箱：取新信、做动作。

    动作优先走 Gmail 的标签扩展（X-GM-LABELS）：改标签不动邮件本身，
    归档就是去掉 \\Inbox。没有这个扩展的服务器退回成文件夹操作。
    """

    def __init__(self, cfg, out=None, dry_run=False):
        self.cfg = cfg
        self.out = out or log
        self.dry_run = dry_run
        self.conn = None
        self.gmail = False
        self.uidvalidity = 0

    # -- 连接 --------------------------------------------------------------
    def connect(self):
        host = self.cfg.text("host")
        port = self.cfg.integer("port")
        if port == 993:
            self.conn = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context())
        else:
            self.conn = imaplib.IMAP4(host, port)
            if "STARTTLS" in "\n".join(self.conn.capabilities):
                self.conn.starttls(ssl_context=ssl.create_default_context())
        try:
            self.conn.login(self.cfg.text("address"), self.cfg.text("password"))
        except imaplib.IMAP4.error as exc:
            raise MailError("登录失败（%s）：%s\n"
                            "Gmail 要的是「应用专用密码」，不是账号密码；"
                            "而且账号得先开两步验证。" % (self.cfg.text("address"), exc))
        capabilities = "\n".join(self.conn.capabilities or ())
        self.gmail = "X-GM-EXT-1" in capabilities
        typ, data = self.conn.select(self.cfg.text("mailbox") or "INBOX",
                                     readonly=self.dry_run)
        if typ != "OK":
            raise MailError("打不开信箱 %s：%s" % (self.cfg.text("mailbox"), data))
        try:
            count = int(data[0])
        except (TypeError, ValueError, IndexError):
            count = 0
        typ, raw = self.conn.response("UIDVALIDITY")
        if typ == "UIDVALIDITY" and raw and raw[0]:
            try:
                self.uidvalidity = int(raw[0])
            except (TypeError, ValueError):
                self.uidvalidity = 0
        self.out("连上了 %s@%s（%s，%s）"
                 % (self.cfg.text("address"), host,
                    "Gmail 扩展" if self.gmail else "通用 IMAP",
                    "%d 封信" % count))
        return self

    def close(self):
        if not self.conn:
            return
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001 - 关不上就算了
            pass
        try:
            self.conn.logout()
        except Exception:  # noqa: BLE001
            pass
        self.conn = None

    # -- 取信 --------------------------------------------------------------
    def fetch_new(self, state, limit=10):
        criteria = self._criteria(state)
        typ, data = self.conn.uid("search", None, criteria)
        if typ != "OK":
            raise MailError("搜索失败：%s" % data)
        uids = []
        for chunk in data or []:
            for token in (chunk or b"").split():
                try:
                    uids.append(int(token))
                except (TypeError, ValueError):
                    continue
        # IMAP 的老规矩：`n:*` 在 n 大于最大 uid 时也会返回最后一封，
        # 所以「比上次新」要自己再筛一遍。
        last = state.last_uid(self.uidvalidity)
        uids = sorted(uid for uid in uids if uid > last)
        if not uids:
            return []
        # 从新到旧地取：新信更值得先判，剩下下一轮接着来
        picked = list(reversed(uids))[:max(1, limit)]
        picked.sort()
        messages = []
        for uid in picked:
            typ, chunks = self.conn.uid("fetch", str(uid), "(RFC822)")
            if typ != "OK":
                self.out("取信 %s 失败：%s" % (uid, chunks))
                continue
            raw = None
            for chunk in chunks or []:
                if isinstance(chunk, tuple) and len(chunk) > 1 and isinstance(chunk[1], bytes):
                    raw = chunk[1]
                    break
            if not raw:
                continue
            try:
                messages.append(to_message(uid, raw))
            except Exception as exc:  # noqa: BLE001 - 一封坏信不该拖垮一轮
                self.out("解析 %s 失败：%s" % (uid, exc))
        return messages

    def _criteria(self, state):
        last = state.last_uid(self.uidvalidity)
        if last:
            return "UID %d:*" % (last + 1)
        days = max(1, self.cfg.integer("lookback_days"))
        return "SINCE %s" % imap_date(time.time() - days * 86400)

    # -- 动作 --------------------------------------------------------------
    def _store(self, uid, operation, item):
        if self.dry_run:
            self.out("  [dry-run] %s %s %s" % (operation, item, uid))
            return
        typ, data = self.conn.uid("store", str(uid), operation, item)
        if typ != "OK":
            raise MailError("改不了 %s（%s %s）：%s" % (uid, operation, item, data))

    def mark_seen(self, uid):
        self._store(uid, "+FLAGS", r"(\Seen)")

    def star(self, uid):
        self._store(uid, "+FLAGS", r"(\Flagged)")

    def add_labels(self, uid, labels):
        labels = [label for label in labels if label]
        if not labels:
            return
        if self.gmail:
            self._store(uid, "+X-GM-LABELS", "(" + " ".join(quote_label(l) for l in labels) + ")")
            return
        for label in labels:
            if self.dry_run:
                self.out("  [dry-run] COPY %s -> %s" % (uid, label))
                continue
            try:
                self.conn.create(label)
            except imaplib.IMAP4.error:
                pass
            self.conn.uid("copy", str(uid), quote_label(label))

    def archive(self, uid):
        """移出收件箱，但**不删**：Gmail 里就是去掉 \\Inbox 标签。"""
        if self.gmail:
            self._store(uid, "-X-GM-LABELS", r"(\Inbox)")
            return
        if self.dry_run:
            self.out("  [dry-run] 归档 %s（COPY 到 All Mail 再标删除）" % uid)
            return
        for folder in ("Archive", "[Gmail]/All Mail", "All Mail"):
            try:
                typ, _ = self.conn.uid("copy", str(uid), quote_label(folder))
                if typ == "OK":
                    self._store(uid, "+FLAGS", r"(\Deleted)")
                    return
            except imaplib.IMAP4.error:
                continue
        raise MailError("这台服务器上找不到归档文件夹，%s 留在原处" % uid)

    def create_draft(self, msg, text):
        """把回复写进草稿箱 —— 到这里为止。发信是人的事。"""
        raw_message = msg.get("raw")
        subject = msg.get("subject") or ""
        if not re.match(r"(?i)^(re|回复)\s*[:：]", subject):
            subject = "Re: " + subject
        body = text
        signature = self.cfg.text("signature")
        if signature:
            body += "\n\n-- \n" + signature
        draft = email.mime.text.MIMEText(body, "plain", "utf-8")
        draft["From"] = self.cfg.text("address")
        draft["To"] = email.utils.formataddr(
            (msg.get("from_name") or "", msg.get("from_addr") or ""))
        draft["Subject"] = subject
        draft["Date"] = email.utils.formatdate(localtime=True)
        draft["Message-ID"] = email.utils.make_msgid()
        message_id = (msg.get("message_id") or "").strip()
        if message_id:
            draft["In-Reply-To"] = message_id
            references = (raw_message.get("References") or "").strip() if raw_message else ""
            draft["References"] = (references + " " + message_id).strip()
        folder = self.cfg.text("drafts_folder") or guess_drafts_folder(self.cfg.text("host"))
        if self.dry_run:
            self.out("  [dry-run] 草稿写进 %s（%d 字）" % (folder, len(body)))
            return ""
        typ, data = self.conn.append(folder, r"(\Draft)", None,
                                     draft.as_string().encode("utf-8"))
        if typ != "OK":
            raise MailError("草稿写不进 %s：%s" % (folder, data))
        return folder


# ---------------------------------------------------------------------------
# 动作、留痕、通知
# ---------------------------------------------------------------------------
def apply_decision(source, msg, decision, cfg, out=None):
    """把判定变成邮箱里的动作。每一步单独兜底：失败一个不影响别的。"""
    out = out or log
    done = []
    uid = msg.get("uid")
    label = (cfg.text("label_needs_me") or "tea/needs-me") if decision["needs_me"] \
        else (cfg.text("label_done") or "tea/done")
    try:
        source.add_labels(uid, [label])
        done.append("贴标签 %s" % label)
    except MailError as exc:
        out("  贴标签失败：%s" % exc)
    if decision["action"] == "star":
        try:
            source.star(uid)
            done.append("加星")
        except MailError as exc:
            out("  加星失败：%s" % exc)
    if (not decision["needs_me"]) and decision["action"] != "keep" and cfg.boolean("archive"):
        try:
            source.archive(uid)
            done.append("移出收件箱")
        except MailError as exc:
            out("  归档失败：%s" % exc)
    if (decision["needs_me"] and decision["draft_reply"] and cfg.boolean("drafts")):
        try:
            folder = source.create_draft(msg, decision["draft_reply"])
            done.append("草稿进 %s（未发送）" % (folder or "草稿箱"))
        except MailError as exc:
            out("  草稿没写成：%s" % exc)
    if cfg.boolean("mark_seen"):
        try:
            source.mark_seen(uid)
            done.append("标为已读")
        except MailError as exc:
            out("  标已读失败：%s" % exc)
    return done


def notify_text(msg, decision):
    """给手机看的那几行。判定在前，正文在后。"""
    head = "📬 %s" % (msg.get("subject") or "(无主题)")
    who = msg.get("from_name") or msg.get("from_addr") or "?"
    lines = [
        head,
        "%s · %s · %s" % (who, decision["category"],
                          {"high": "紧急", "low": "不急"}.get(decision["urgency"], "一般")),
    ]
    if decision["why"]:
        lines.append("为什么找你：%s" % decision["why"])
    if decision["summary"]:
        lines.append(decision["summary"])
    if decision["follow_up"]:
        lines.append("要做：%s" % decision["follow_up"])
    if decision["draft_reply"]:
        lines.append("草稿（未发）：%s" % decision["draft_reply"][:200])
    link = mail_link(msg)
    if link:
        lines.append(link)
    return "\n".join(lines)


class Notifier(object):
    """往外说一句话。四种出口，一个都不通也不算失败 —— digest 终归有。"""

    def __init__(self, cfg, out=None):
        self.cfg = cfg
        self.out = out or log
        self.kind = (cfg.text("notify") or "file").lower()

    def send(self, text, meta=None):
        if self.kind in ("file", "none", ""):
            self.out(text)
            return True
        if self.kind == "telegram":
            return self._telegram(text)
        if self.kind == "webhook":
            return self._webhook(text, meta or {})
        self.out("不认识的 notify=%r，只写 digest" % self.kind)
        return False

    def _telegram(self, text):
        token = self.cfg.text("telegram_token")
        chat = self.cfg.text("telegram_chat")
        if not token or not chat:
            self.out("notify=telegram 但没配 telegram_token / telegram_chat")
            return False
        api = (self.cfg.text("telegram_api") or "https://api.telegram.org").rstrip("/")
        url = "%s/bot%s/sendMessage" % (api, token)
        body = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
        try:
            answer = post_json(url, body, timeout=20)
        except Exception as exc:  # noqa: BLE001 - 发不出去不该让一轮挂掉
            self.out("Telegram 没发出去：%s" % exc)
            return False
        if not answer.get("ok"):
            self.out("Telegram 说不行：%s" % (answer.get("description") or answer))
            return False
        return True

    def _webhook(self, text, meta):
        url = self.cfg.text("webhook")
        if not url:
            self.out("notify=webhook 但没配 webhook 地址")
            return False
        body = dict(meta)
        body["text"] = text
        try:
            post_json(url, body, timeout=20)
        except Exception as exc:  # noqa: BLE001
            self.out("webhook 没发出去：%s" % exc)
            return False
        return True


def post_json(url, payload, timeout=20):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", "replace")
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw[:200]}


def digest_path(when=None):
    stamp = time.strftime("%Y-%m-%d", time.localtime(when or time.time()))
    return os.path.join(DIGEST_DIR, stamp + ".md")


def record_digest(msg, decision, actions, out=None):
    """按天记一笔。这是「我到底处理了什么」的唯一真相。"""
    out = out or log
    try:
        os.makedirs(DIGEST_DIR, exist_ok=True)
        path = digest_path()
        new_file = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as handle:
            if new_file:
                handle.write("# %s 邮件处理\n\n" % time.strftime("%Y-%m-%d"))
            mark = "❗" if decision["needs_me"] else "·"
            handle.write("- %s %s  **%s** — %s <%s>\n" % (
                time.strftime("%H:%M"), mark,
                (msg.get("subject") or "(无主题)").replace("\n", " ")[:80],
                msg.get("from_name") or "", msg.get("from_addr") or ""))
            if decision["summary"]:
                handle.write("  - %s\n" % decision["summary"].replace("\n", " "))
            if decision["why"]:
                handle.write("  - 为什么找我：%s\n" % decision["why"].replace("\n", " "))
            if decision["follow_up"]:
                handle.write("  - 要做：%s\n" % decision["follow_up"].replace("\n", " "))
            if actions:
                handle.write("  - 动作：%s\n" % "，".join(actions))
            if decision.get("failed"):
                handle.write("  - ⚠️ 判读失败\n")
    except OSError as exc:
        out("写 digest 失败：%s" % exc)


def queue_followup(cfg, msg, decision, out=None):
    """判读出「要实际做一件事」→ 交给 tea_a2 的队列，做完它会通知我。"""
    out = out or log
    if not cfg.boolean("queue_followups"):
        return ""
    if not decision["follow_up"] or not cfg.text("tasks") or not cfg.text("workspace"):
        return ""
    prompt = "\n".join([
        "这封邮件需要实际处理。",
        "",
        "发件人：%s <%s>" % (msg.get("from_name") or "", msg.get("from_addr") or ""),
        "主题：%s" % (msg.get("subject") or "(无主题)"),
        "我的秘书给的摘要：%s" % (decision["summary"] or "(无)"),
        "要做的事：%s" % decision["follow_up"],
        "",
        "正文（截断）：",
        (msg.get("body") or "")[:2000],
        "",
        "做完把结论和需要我拍板的地方说清楚。",
    ])
    key = "mail-%s" % message_key(msg)
    try:
        queue = Queue(cfg.text("tasks"))
        task = queue.create_task(
            prompt,
            workspace_id=cfg.text("workspace"),
            title="邮件：%s" % (msg.get("subject") or "(无主题)")[:60],
            provider=cfg.text("provider") or None,
            meta={"source": "mail_watch", "mail_uid": msg.get("uid"),
                  "subject": msg.get("subject") or ""},
            idempotency_key=key,
        )
    except Exception as exc:  # noqa: BLE001 - 队列不在也不该拖垮收信
        out("排任务失败：%s" % exc)
        return ""
    task_id = (task.get("task") or task or {}).get("id") or ""
    out("  排进队列：%s" % (task_id or "（没拿到 id）"))
    return task_id


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
class State(object):
    """处理到哪了：uidvalidity、最后处理的 uid、处理过的信的指纹。"""

    def __init__(self, path=None):
        self.path = path or STATE_PATH
        self.data = {"uidvalidity": 0, "last_uid": 0, "seen": {}, "rounds": 0,
                     "handled": 0, "notified": 0, "last_run": 0}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, ValueError):
            pass

    def last_uid(self, uidvalidity):
        """UIDVALIDITY 变了（Gmail 会干这事）就意味着 uid 全部作废，从头来。"""
        if uidvalidity and uidvalidity != self.data.get("uidvalidity"):
            self.data["uidvalidity"] = uidvalidity
            self.data["last_uid"] = 0
        return int(self.data.get("last_uid") or 0)

    def seen(self, msg):
        return message_key(msg) in (self.data.get("seen") or {})

    def remember(self, msg, decision, uid=0):
        seen = self.data.setdefault("seen", {})
        seen[message_key(msg)] = {
            "at": int(time.time()),
            "subject": (msg.get("subject") or "")[:120],
            "needs_me": bool(decision.get("needs_me")),
            "category": decision.get("category") or "",
        }
        if uid:
            self.data["last_uid"] = max(int(self.data.get("last_uid") or 0), int(uid))
        self.data["handled"] = int(self.data.get("handled") or 0) + 1
        if decision.get("needs_me"):
            self.data["notified"] = int(self.data.get("notified") or 0) + 1
        # 只留最近的这么多条：state 不该越滚越大
        if len(seen) > MAX_REMEMBERED:
            keep = sorted(seen.items(), key=lambda item: item[1].get("at") or 0)
            for key, _ in keep[:len(seen) - MAX_REMEMBERED]:
                seen.pop(key, None)

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as exc:
            log("state 存不下去：%s" % exc)


class Lock(object):
    """同一时刻只该有一个 watcher：两个一起跑会把同一封信处理两遍。"""

    def __init__(self, path=None):
        self.path = path or LOCK_PATH
        self.held = False

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for _ in range(2):
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pid = 0
                try:
                    with open(self.path, "r", encoding="utf-8") as reader:
                        pid = int((reader.read() or "0").strip() or 0)
                except (OSError, ValueError):
                    pid = 0
                if pid and alive(pid):
                    raise MailError("已经有一个 watcher 在跑（pid %d）。先停掉它，"
                                    "或者删掉 %s 如果它确实死了。" % (pid, self.path))
                # 上一个进程死了，锁是垃圾，收下它
                try:
                    os.unlink(self.path)
                except OSError as exc:
                    raise MailError("锁文件 %s 清不掉：%s" % (self.path, exc))
                continue
            with os.fdopen(handle, "w") as writer:
                writer.write(str(os.getpid()))
            self.held = True
            return self
        raise MailError("锁文件 %s 拿不到" % self.path)

    def __exit__(self, *exc):
        if self.held:
            try:
                os.unlink(self.path)
            except OSError:
                pass
        self.held = False
        return False


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# 一轮、常驻
# ---------------------------------------------------------------------------
def log(message):
    print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message), flush=True)


def run_round(cfg, source, judge, notifier, state, out=None, dry_run=False):
    """取一批新信 → 一封封判 → 动邮箱 → 留痕 → 只在需要我时说话。"""
    out = out or log
    messages = source.fetch_new(state, limit=max(1, cfg.integer("max_per_round")))
    state.data["rounds"] = int(state.data.get("rounds") or 0) + 1
    state.data["last_run"] = int(time.time())
    stats = {"seen": 0, "handled": 0, "needs_me": 0, "notified": 0, "queued": 0}
    notified = 0
    for msg in messages:
        stats["seen"] += 1
        if state.seen(msg):
            out("跳过（处理过）：%s" % (msg.get("subject") or "(无主题)"))
            continue
        decision = judge(msg)
        if not decision:
            decision = fail_safe_decision("判读没给出结果")
        actions = []
        if dry_run:
            out("[dry-run] %s | %s | needs_me=%s | %s"
                % ((msg.get("subject") or "(无主题)")[:60], decision["category"],
                   "yes" if decision["needs_me"] else "no", decision["summary"]))
        else:
            actions = apply_decision(source, msg, decision, cfg, out=out)
        record_digest(msg, decision, actions, out=out)
        out("%s | %s | %s%s"
            % ((msg.get("subject") or "(无主题)")[:60], decision["category"],
               decision["summary"][:80] or decision["why"][:80],
               (" | " + "，".join(actions)) if actions else ""))
        stats["handled"] += 1
        if decision["needs_me"]:
            stats["needs_me"] += 1
            if dry_run:
                out("[dry-run] 本该通知：\n%s" % notify_text(msg, decision))
            elif notified >= MAX_NOTIFY_PER_ROUND:
                out("本轮通知已经够多了，这封只记 digest")
            elif notifier.send(notify_text(msg, decision), {
                    "type": "mail", "category": decision["category"],
                    "urgency": decision["urgency"], "subject": msg.get("subject") or "",
                    "from": msg.get("from_addr") or "", "uid": msg.get("uid"),
                    "needs_me": True}):
                stats["notified"] += 1
                notified += 1
            task_id = queue_followup(cfg, msg, decision, out=out)
            if task_id:
                stats["queued"] += 1
        if not dry_run:
            # 试运行不推进书签：否则下一次真跑会把这几封当成处理过了
            state.remember(msg, decision, uid=msg.get("uid") or 0)
            state.save()
    if not messages:
        out("没有新信")
    return stats


def run_forever(cfg, out=None, dry_run=False, once=False, interval=None):
    out = out or log
    wait = interval if interval is not None else max(30, cfg.integer("interval"))
    judge = Judge(cfg, out=out)
    notifier = Notifier(cfg, out=out)
    state = State()
    with Lock():
        while True:
            source = None
            try:
                source = ImapSource(cfg, out=out, dry_run=dry_run).connect()
                stats = run_round(cfg, source, judge, notifier, state, out=out, dry_run=dry_run)
                out("这一轮：新 %d，处理 %d，需要我 %d，通知 %d，排任务 %d"
                    % (stats["seen"], stats["handled"], stats["needs_me"],
                       stats["notified"], stats["queued"]))
            except (MailError, ApiError, ConfigError) as exc:
                out("这一轮没跑成：%s" % exc)
            except Exception as exc:  # noqa: BLE001 - 常驻进程不该因为一次网络抖动退出
                out("意外：%s: %s" % (type(exc).__name__, exc))
            finally:
                if source is not None:
                    source.close()
                state.save()
            if once:
                return
            time.sleep(wait)


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def cmd_check(args):
    cfg = load_config(args.config)
    problems = []
    if not cfg.text("address"):
        problems.append("没写 address")
    if not cfg.text("password"):
        problems.append("没写 password（Gmail 是应用专用密码）")
    if problems:
        for problem in problems:
            log("配置：%s" % problem)
        return 2
    log("配置：%s" % cfg.path)
    log("邮箱：%s @ %s:%s / %s" % (cfg.text("address"), cfg.text("host"),
                                   cfg.text("port"), cfg.text("mailbox")))
    log("动作：不需要我 → 标签 %s%s；需要我 → 标签 %s；草稿箱 %s"
        % (cfg.text("label_done"),
           "，并移出收件箱" if cfg.boolean("archive") else "，留在收件箱",
           cfg.text("label_needs_me"),
           cfg.text("drafts_folder") or guess_drafts_folder(cfg.text("host"))))
    log("出口：%s" % cfg.text("notify"))
    source = ImapSource(cfg).connect()
    try:
        state = State()
        log("状态：uidvalidity=%s last_uid=%s 处理过 %s 封"
            % (source.uidvalidity, state.last_uid(source.uidvalidity),
               len(state.data.get("seen") or {})))
    finally:
        source.close()
    tea = Tea(cfg.text("agent"))
    try:
        health = tea.health()
        log("agent：%s（%s，provider %s，model %s）"
            % (cfg.text("agent"), health.get("version") or "?",
               health.get("provider") or "?", health.get("model") or "?"))
    except ApiError as exc:
        log("agent 连不上：%s（判读需要它，先把它起来）" % exc)
        return 1
    return 0


def cmd_peek(args):
    """只看判读：不动邮箱、不记账，用来调提示词和规则。"""
    cfg = load_config(args.config)
    judge = Judge(cfg)
    source = ImapSource(cfg, dry_run=True).connect()
    try:
        state = State()
        messages = source.fetch_new(state, limit=max(1, args.limit))
        if not messages:
            log("没有新信（看 --limit 或 lookback_days）")
            return 0
        for msg in messages:
            decision = judge(msg)
            print("")
            print("-" * 70)
            print("UID %s  %s" % (msg.get("uid"), msg.get("subject") or "(无主题)"))
            print("  from: %s <%s>" % (msg.get("from_name"), msg.get("from_addr")))
            print("  判定: %s / %s / needs_me=%s%s"
                  % (decision["category"], decision["urgency"],
                     "yes" if decision["needs_me"] else "no",
                     " / 判读失败" if decision.get("failed") else ""))
            if decision["why"]:
                print("  为啥: %s" % decision["why"])
            print("  摘要: %s" % decision["summary"])
            if decision["draft_reply"]:
                print("  草稿: %s" % decision["draft_reply"][:300].replace("\n", " "))
            if decision["follow_up"]:
                print("  要做: %s" % decision["follow_up"])
            print("  正文节选: %s" % (msg.get("body") or "")[:200].replace("\n", " "))
    finally:
        source.close()
    return 0


def cmd_run(args):
    cfg = load_config(args.config)
    run_forever(cfg, dry_run=args.dry_run, once=args.once, interval=args.interval)
    return 0


def cmd_status(args):
    cfg = load_config(args.config) if os.path.exists(args.config or CONFIG_PATH) else Config()
    state = State()
    data = state.data
    log("配置：%s" % (cfg.path or "（还没有配置文件）"))
    log("处理过 %s 封，其中需要我 %s 封；跑了 %s 轮，最后一轮 %s"
        % (data.get("handled") or 0, data.get("notified") or 0, data.get("rounds") or 0,
           time.strftime("%Y-%m-%d %H:%M", time.localtime(data.get("last_run") or 0))
           if data.get("last_run") else "还没跑过"))
    path = digest_path()
    log("今天的 digest：%s%s" % (path, "" if os.path.exists(path) else "（还没有）"))
    recent = sorted((data.get("seen") or {}).items(),
                    key=lambda item: item[1].get("at") or 0)[-10:]
    for _, entry in recent:
        log("  %s %s %s"
            % (time.strftime("%m-%d %H:%M", time.localtime(entry.get("at") or 0)),
               "❗" if entry.get("needs_me") else "·",
               (entry.get("subject") or "")[:70]))
    return 0


def cmd_notify_test(args):
    cfg = load_config(args.config)
    notifier = Notifier(cfg)
    ok = notifier.send("tea_a2 邮件看门狗：这是一条测试通知。", {"type": "test"})
    log("通知：%s" % ("发出去了" if ok else "没发出去（看上面的原因）"))
    return 0 if ok else 1


def cmd_template(args):
    """把模板打出来 / 写到配置文件的位置，省得用户找。"""
    here = os.path.dirname(os.path.abspath(__file__))
    template = os.path.join(here, "mail-watch.template")
    if not os.path.exists(template):
        log("找不到模板 %s" % template)
        return 1
    target = args.config or CONFIG_PATH
    if os.path.exists(target) and not args.force:
        log("已经有 %s 了；要覆盖加 --force" % target)
        return 1
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(template, "r", encoding="utf-8") as reader:
        text = reader.read()
    with open(target, "w", encoding="utf-8") as writer:
        writer.write(text)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    log("写好 %s —— 填上 address / password 就能 check 了。" % target)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="收件箱看门狗：判读、归档、起草，只在该我参与时通知我。")
    parser.add_argument("--config", default="", help="配置文件路径（默认 %s）" % CONFIG_PATH)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("check", help="配置、登录、agent 是否都通").set_defaults(func=cmd_check)
    peek = sub.add_parser("peek", help="只看判读结果，不动邮箱")
    peek.add_argument("--limit", type=int, default=3)
    peek.set_defaults(func=cmd_peek)
    run = sub.add_parser("run", help="跑（默认常驻）")
    run.add_argument("--once", action="store_true", help="只跑一轮")
    run.add_argument("--dry-run", action="store_true", help="不动邮箱，只打印")
    run.add_argument("--interval", type=int, default=None, help="多少秒一轮")
    run.set_defaults(func=cmd_run)
    sub.add_parser("status", help="处理到哪儿了").set_defaults(func=cmd_status)
    sub.add_parser("notify-test", help="试一下通知").set_defaults(func=cmd_notify_test)
    template = sub.add_parser("init", help="把配置模板写到 ~/.tea_agent/mail/config")
    template.add_argument("--force", action="store_true")
    template.set_defaults(func=cmd_template)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except ConfigError as exc:
        log("配置有问题：\n%s" % exc)
        return 2
    except MailError as exc:
        log("邮箱有问题：%s" % exc)
        return 1
    except KeyboardInterrupt:
        log("停下")
        return 0


if __name__ == "__main__":
    sys.exit(main())
