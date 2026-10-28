# -*- coding: utf-8 -*-
"""登录入口：开一个有头浏览器让你登录，登录完把 storageState 存下来。

给 sched/bili.py 和 sched/zhihu.py 用（`--login`）。只依赖标准库，
所以在一台没装 requests/yaml 的机器上也能跑。

    python3 sched/zhihu.py --login
    python3 sched/bili.py  --login

流程：
  1) 找到 state 文件该写哪（环境变量 > 已存在的候选 > 默认路径）
  2) 调 node sched/login_state.mjs 开浏览器（headless=False）
  3) 你在窗口里登录 → 关掉窗口 → 它把 storageState 写下来
  4) 回读文件确认 cookie 在，并打印有效期

装依赖：node 能找到 playwright-core 就行（npm i playwright-core），
浏览器用 playwright 自带的（ms-playwright 目录）或 CHROME_BIN 指一个。
"""
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "login_state.mjs")


def state_path(env_var, candidates, default):
    """登录态写哪：环境变量 > 已存在的候选文件 > 默认路径。"""
    env = os.environ.get(env_var)
    if env:
        return env
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return default


def find_node():
    node = shutil.which("node")
    if node:
        return node
    for c in (r"C:\Program Files\nodejs\node.exe",
              r"C:\Program Files\Volta\node.exe",
              "/usr/local/bin/node", "/usr/bin/node"):
        if os.path.exists(c):
            return c
    return None


def ensure_playwright_core():
    """保证 helper 脚本能 import 到 playwright-core，返回它的目录（没有就 None）。

    Windows：装一份到 %LOCALAPPDATA%\Temp\tea_playwright（一次几秒钟）。
    其它平台：优先用仓库里的 .tea_agent_run/node_modules（那是本机装的）。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    if os.name != "nt":
        for c in (os.path.join(repo, ".tea_agent_run", "node_modules", "playwright-core"),
                  os.path.join(repo, "node_modules", "playwright-core"),
                  os.path.join(here, "node_modules", "playwright-core")):
            if os.path.isdir(c):
                return c
        return None

    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\AppData\Local")
    prefix = os.path.join(local, "Temp", "tea_playwright")
    target = os.path.join(prefix, "node_modules", "playwright-core")
    if os.path.isdir(target):
        return target
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        print("[login] Windows 上没找到 npm，无法自动装 playwright-core。")
        print("[login] 你可以手动：npm i --prefix %s playwright-core" % prefix)
        return None
    print("[login] 首次运行，正在装 playwright-core（Windows 侧）…")
    try:
        subprocess.check_call([npm, "install", "--prefix", prefix, "playwright-core"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    except Exception as exc:
        print("[login] 装 playwright-core 失败：%s" % exc)
        return None
    return target if os.path.isdir(target) else None


def login(url, out, cookie, label, headless=False, timeout=600, quiet=False):
    """开浏览器登录，存 storageState。返回 (ok, 信息)。"""
    node = find_node()
    if not node:
        raise SystemExit("找不到 node。装一个 Node.js（https://nodejs.org），再跑一次。")
    if not os.path.exists(HELPER):
        raise SystemExit("缺少 %s" % HELPER)

    # Windows 上如果脚本在 WSL 的 UNC 路径里，node 处理 UNC 路径会出各种怪问题，
    # 所以先把助手脚本复制一份到 Windows 本地临时目录再跑。
    helper = HELPER
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\AppData\Local")
        tmp = os.path.join(local, "Temp", "tea_login")
        if not os.path.isdir(tmp):
            os.makedirs(tmp)
        helper = os.path.join(tmp, "login_state.mjs")
        shutil.copyfile(HELPER, helper)

    out_dir = os.path.dirname(os.path.abspath(out))
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir)

    cmd = [node, helper, "--url", url, "--out", out, "--cookie", cookie,
           "--label", label, "--timeout", str(timeout)]
    if headless:
        cmd.append("--headless")

    env = dict(os.environ)
    pw = ensure_playwright_core()
    if pw:
        env["PLAYWRIGHT_CORE_PATH"] = pw
    elif os.name == "nt":
        env.setdefault("PLAYWRIGHT_CORE_PATH", "")

    print("[login] node: %s" % node)
    print("[login] 目标: %s" % out)
    if headless:
        print("[login] 无头模式：稍后会给出二维码截图，扫一下就行。")
    else:
        print("[login] 在弹出的浏览器里登录，登录完把窗口关掉就行。")
    sys.stdout.flush()
    try:
        rc = subprocess.call(cmd, env=env)
    except KeyboardInterrupt:
        print("\n[login] 被打断了")
        return False, "中断"
    if rc != 0:
        return False, "登录没成功（退出码 %s）" % rc
    return True, "ok"


def read_state(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def check_state(path, cookie):
    """回读 state 文件，确认关键 cookie 在，并返回到期日。"""
    try:
        st = read_state(path)
    except Exception as exc:
        return False, "读不了 %s：%s" % (path, exc)
    cookies = st.get("cookies") or []
    hit = None
    for c in cookies:
        if c.get("name") == cookie and c.get("value"):
            hit = c
            break
    if not hit:
        return False, "%s 里没有 %s cookie" % (path, cookie)
    exp = hit.get("expires", -1)
    if exp and exp > 0:
        days = int((exp - time.time()) / 86400)
        return True, "%s 到期 %s（还有 %d 天）" % (
            cookie, time.strftime("%Y-%m-%d", time.localtime(exp)), days)
    return True, "%s 是会话 cookie" % cookie
