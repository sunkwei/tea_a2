#!/usr/bin/env python3
"""通过聊天通道启动隧道侧载，把这一次的 session 码回传。

为什么是「死」的一条命令：开隧道这件事必须**确定**地发生——
交给模型判断会时灵时不灵，还要烧一次模型调用。
命令是死的，回复也是死的。

    /tunnel          起隧道（已经在跑就把现有的码给你）
    /tunnel stop     把隧道停掉

家里怎么用：
    1. 给 bot 发「/tunnel」→ 它回一条命令和一个 session 码
    2. 在家里那台机器上跑：
           a2_tunnel --role client --session <码>
    3. 本机多一个端口（默认 9931）：http://127.0.0.1:9931/ 就是那台设备的 a2
       （隧道里跑的是原始 TCP，所以端口后面就是它原本的 8383）

**谁可以开**：默认是「能给它发消息的人」。想收紧就在
``~/.tea_agent/tunnel_allow.txt`` 里写上允许的 user_id（一行一个，
空文件=谁都不许）。每次开隧道都会把 user_id 记进日志。
"""

import os
import re
import subprocess
import sys
import time

# 触发词。写法宽松一点：家里发「开隧道」「隧道」也算。
START_WORDS = ("/tunnel", "隧道", "开隧道", "启动隧道", "开一下隧道")
STOP_WORDS = ("/tunnel stop", "隧道关闭", "关掉隧道", "停隧道")

# 侧载的单元名与二进制（都能用环境变量改，方便别的机器用别的名字）。
UNIT = os.environ.get("TEA_TUNNEL_UNIT", "a2_tunnel")
BINARY = os.environ.get("TEA_TUNNEL_BIN", "")
# 侧载写的日志（单元里的 ExecStart 就是写到这里）。
LOG = os.environ.get("TEA_TUNNEL_LOG",
                      os.path.join(os.path.expanduser("~"), ".tea_agent", "logs",
                                   "a2_tunnel.log"))
# 家里那侧的本地端口（client 侧的隧道入口）。
CLIENT_PORT = int(os.environ.get("TEA_TUNNEL_CLIENT_PORT", "9931"))
# 等侧载打印出 session 码要多久（它起来只要一秒，DNS 慢一点也就几秒）。
WAIT_SECONDS = float(os.environ.get("TEA_TUNNEL_WAIT", "20"))
# 轮询的间隔：侧载起来只要一秒，轮询密一点，回话就快一点。
POLL = float(os.environ.get("TEA_TUNNEL_POLL", "0.2"))
# 侧载等对端的时限（秒）。微信这头要人看着手机、抄命令、跑到另一台机器上，
# 5 分钟有点紧 → 单元里可以调大（TEA_TUNNEL_TIMEOUT）。
TIMEOUT = int(os.environ.get("TEA_TUNNEL_TIMEOUT", "300"))

# 挂在 /help 后面的一行（设备的 channel.py 是定制过的，不能动它）。
HELP_LINE = "· /tunnel 开隧道（回一条 session 码，家里那台机器用它连进来）"

_TOKEN_RE = re.compile(r"room token\s*:\s*([0-9a-f]+)")
# 侧载打印的那条「给外面的人用的命令」：a2_tunnel --role client --session <码>
# 有时后面还跟着 --tls-pin <指纹>（broker 自签证书的指纹，客户端拿它认 broker）。
_CLIENT_CMD_RE = re.compile(r"a2_tunnel --role client --session ([0-9a-f]+)"
                            r"(?: --tls-pin ([0-9a-f]{64}))?")
_TIME_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]")


def log(message):
    sys.stderr.write("%s  tunnel_cmd: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    sys.stderr.flush()


def allowed(user_id):
    """Is this user allowed to open a tunnel?

    The allowlist is optional: without it, whoever can write to the bot can
    open a tunnel to this machine. That is a real door, so the file is worth
    having - but a machine with one account and one owner does not need it.
    """
    path = os.path.join(os.path.expanduser("~"), ".tea_agent", "tunnel_allow.txt")
    try:
        with open(path, encoding="utf-8") as handle:
            allowed_ids = [line.strip() for line in handle]
    except OSError:
        return True
    return str(user_id) in allowed_ids


def _user_env():
    """The environment a `systemctl --user` needs.

    A service started by systemd (or by a user session) has XDG_RUNTIME_DIR;
    a script run by cron or by ssh without a login shell does not - and then
    systemctl --user cannot find the bus.
    """
    env = dict(os.environ)
    if not env.get("XDG_RUNTIME_DIR"):
        runtime = "/run/user/%d" % os.getuid()
        if os.path.isdir(runtime):
            env["XDG_RUNTIME_DIR"] = runtime
            bus = "unix:path=%s/bus" % runtime
            if os.path.exists("%s/bus" % runtime):
                env["DBUS_SESSION_BUS_ADDRESS"] = bus
    return env


def _systemctl(*args):
    try:
        proc = subprocess.run(["systemctl", "--user"] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              env=_user_env(), timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return 255, str(exc)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def unit_active():
    code, out = _systemctl("is-active", UNIT)
    return code == 0 and out.strip().splitlines()[0].strip() == "active"


def log_size():
    """How big the log is right now (the byte offset a new run starts at)."""
    try:
        return os.path.getsize(LOG)
    except OSError:
        return 0


def read_session(offset=0):
    """The session code of the last run, and when it was printed.

    `offset` skips what an earlier run wrote: the log is appended, so without
    it a fresh start would hand out the **previous** session code - which the
    broker has already forgotten.
    """
    try:
        with open(LOG, encoding="utf-8", errors="replace") as handle:
            if offset:
                handle.seek(offset)
            lines = handle.readlines()
    except OSError:
        return "", None
    code = ""
    stamp = None
    pin = ""
    for line in reversed(lines):
        # 那条「给外面的人用的命令」里带着 session 码（和指纹），
        # 从它这里取最准：它就是应该交给对方的那一行。
        cmd_match = _CLIENT_CMD_RE.search(line)
        if cmd_match:
            code = cmd_match.group(1)
            pin = cmd_match.group(2) or ""
            break
        match = _TOKEN_RE.search(line)
        if match:
            code = match.group(1)
            time_match = _TIME_RE.match(line)
            if time_match:
                hour, minute, second = (int(part) for part in time_match.groups())
                today = time.localtime()
                stamp = time.mktime((today.tm_year, today.tm_mon, today.tm_mday,
                                     hour, minute, second, 0, 0, -1))
            break
    return code, stamp, pin


def start_sidecar():
    """Start the sidecar (or use the one that is already running).

    Returns (session_code, error_message).
    """
    if unit_active():
        code, _, _ = read_session()
        if code:
            return code, ""
    # 从这里开始写的日志才是这一次的（旧的那几行不算）。
    offset = log_size()
    # 先停再起：每次都换一个 session 码，旧的会过期。
    _systemctl("stop", UNIT)
    code, out = _systemctl("start", UNIT)
    if code != 0:
        # systemd 不可用（没有 linger、没有单元）→ 直接把二进制拉起来。
        binary = BINARY or os.path.join(os.path.expanduser("~"), "bin", "a2_tunnel")
        if not os.path.exists(binary):
            return "", "起不来：systemctl 报错（%s），也找不到 %s" % (out.strip()[:120], binary)
        log("systemctl failed (%s) - starting the binary directly" % out.strip()[:120])
        try:
            subprocess.Popen([binary, "--role", "agent"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as exc:
            return "", "起不来：%s" % exc
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        code, _, _ = read_session(offset)
        if code:
            return code, ""
        time.sleep(POLL)
    return "", "侧载起来了但没打印出 session 码（看 %s）" % LOG


def stop_sidecar():
    _systemctl("stop", UNIT)
    return "隧道已停。"


def reply_for(code, pin=""):
    """The answer that goes back through the chat channel."""
    # 有指纹就带上：客户端靠它认出 broker（broker 是自签证书，
    # 光看名字认不出来，broker 换地址也不影响）。
    client_cmd = "a2_tunnel --role client --session %s" % code
    if pin:
        client_cmd += " --tls-pin %s" % pin
    lines = ["隧道已启动。家里那台机器上跑这条：", "",
             "    %s" % client_cmd, "",
             "连上以后本机多一个端口 %d，浏览器打开 http://127.0.0.1:%d/ 就是这台设备的 a2。"
             % (CLIENT_PORT, CLIENT_PORT),
             "（隧道里跑的是原始 TCP，端口后面就是它原本的 8383。）"]
    _, stamp, _ = read_session()
    if stamp:
        left = int(TIMEOUT - (time.time() - stamp))
        if left > 0:
            lines.append("侧载 %d 分钟没人连就自己退出，抓紧。" % max(1, left // 60))
    return "\n".join(lines)


def maybe_handle(text, user_id=""):
    """Handle a tunnel command. Returns the reply, or None if this is not one."""
    message = (text or "").strip()
    if not message:
        return None
    lowered = message.lower()
    if lowered in STOP_WORDS or message in STOP_WORDS:
        return stop_sidecar()
    if not (lowered in START_WORDS or message in START_WORDS):
        return None
    if not allowed(user_id):
        log("refused: %s is not on the allowlist" % user_id)
        return "没有开隧道的权限。"
    log("opening the tunnel for %s" % (user_id or "?"))
    code, error = start_sidecar()
    if error:
        return "隧道没起来：%s" % error
    _, _, pin = read_session()
    return reply_for(code, pin)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(__doc__)
        return 0
    if argv[0] in ("start", "开"):
        code, error = start_sidecar()
        if error:
            sys.stderr.write(error + "\n")
            return 1
        _, _, pin = read_session()
        sys.stdout.write(reply_for(code, pin) + "\n")
        return 0
    if argv[0] in ("stop", "关"):
        sys.stdout.write(stop_sidecar() + "\n")
        return 0
    sys.stderr.write("用法：tunnel_cmd.py [start|stop]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
