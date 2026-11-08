#!/usr/bin/env python3
"""Several WeChat accounts in one process, one task queue for all of them.

Is the protocol one bot per client? No. The protocol itself asks the
client for the bot tokens it knows (`local_token_list`, up to ten) - so a
client is expected to be able to hold several bots. What every bot needs
of its own is the credentials, the cursor and the context tokens, and this
process gives every account a bot object of its own.

What *is* one to one: one WeChat account binds one bot. Scan again with the
same phone and the server says "this one is already connected" (the
protocol calls it `binded_redirect`), so the client takes the credentials
over instead of making a second bot. And the platform refuses to bind one
WeChat account to two bots ("暂不支持连接多个 OpenClaw").

Why one process instead of one service per account:
  * one place to log in, one place to look at;
  * an account that has been scanned is picked up by itself - no unit to
    write, nothing to start;
  * all of them share one task queue (a2_task): one quota, one schedule,
    one place where the work is listed. Every account still works in its
    own workspace, so the tasks of two accounts do not mix.

The price: a crash takes every account with it (the supervisor restarts
them), and an account cannot be stopped on its own. An account that
already has a service of its own (a2_wechat@alice.service) is left alone -
two bots polling with the same cursor would eat each other's messages.

Usage:

  python3 sched/wechat_multi.py                     # every account in ~/.tea_agent/wechat
  python3 sched/wechat_multi.py --accounts alice,bob
  python3 sched/wechat_multi.py --accounts-dir ~/.tea_agent/wechat --scan-interval 5

  python3 sched/wechat_multi.py --login alice       # one account: a link to scan

The login of one account:

  python3 sched/wechat_multi.py --login alice

prints the link of a QR code (the page that shows the code and asks the phone
to confirm it) and waits for the scan. The credentials land in that account's
own directory (~/.tea_agent/wechat/alice/credentials.json), and from then on
that account is one of the accounts this process serves: its messages become
tasks in the queue, in its own workspace ("微信_alice").

So the order is: log in as many accounts as you like, then start the service -
it picks up every account that has been scanned.
"""

import argparse
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wechat_bot  # noqa: E402

log = wechat_bot.log
# The unit of one account - if it is running, that account is not ours.
UNIT_TEMPLATE = "a2_wechat@%s.service"
DEFAULT_UNIT = "a2_wechat.service"
MUL_UNIT = "a2_wechat_mul.service"


class Multi(object):
    """The accounts of this machine, one bot each, in one process."""

    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.bots = {}            # account name -> the bot that serves it
        self.stops = {}           # account name -> "stop this account"
        self.stop = threading.Event()
        self.accounts_root = os.path.abspath(os.path.expanduser(args.accounts_dir))
        self.workspaces_root = os.path.abspath(os.path.expanduser(args.workspaces_dir))

    # -- the accounts -----------------------------------------------------
    def names(self):
        """The accounts this process serves."""
        if self.args.accounts:
            return [n.strip() for n in self.args.accounts.split(",") if n.strip()]
        root = self.accounts_root
        out = []
        try:
            names = sorted(os.listdir(root))
        except OSError:
            names = []
        for name in names:
            if name.startswith("."):
                continue
            if os.path.isfile(os.path.join(root, name, "credentials.json")):
                # An account of this machine: it has been scanned, so it has
                # a bot. An account that was never scanned is left to the
                # pairing page (it holds the code and writes the
                # credentials when the phone confirms).
                out.append(name)
        if os.path.isfile(os.path.join(root, "credentials.json")):
            out.insert(0, "default")
        return out

    def state_dir(self, name):
        """Where the credentials of this account live."""
        if wechat_bot.is_default_account(name):
            return self.accounts_root
        return os.path.join(self.accounts_root, wechat_bot.safe_name(name))

    def workspace_path(self, name):
        """The workspace of this account: its own."""
        if wechat_bot.is_default_account(name):
            return os.path.join(self.workspaces_root, "wechat-workspace")
        return os.path.join(self.workspaces_root,
                            "wechat-%s-workspace" % wechat_bot.safe_name(name))

    def unit_of(self, name):
        """The unit that serves this account."""
        if not name or name == "default":
            return DEFAULT_UNIT
        return UNIT_TEMPLATE % wechat_bot.safe_name(name)

    def unit_active(self, name):
        """Is somebody else already serving this account?"""
        if not self.args.skip_units:
            return False
        try:
            proc = subprocess.run(["systemctl", "--user", "is-active", self.unit_of(name)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        return (proc.stdout or b"").decode("utf-8", "replace").strip() == "active"

    # -- the bots ---------------------------------------------------------
    def base_argv(self):
        """The options every account gets."""
        out = ["--base-url", self.args.base_url,
               "--agent", self.args.agent,
               "--tasks", self.args.tasks,
               "--cdn-url", self.args.cdn_url,
               "--login-timeout", str(self.args.login_timeout)]
        if self.args.triggers:
            out.append("--triggers")
        if self.args.speech:
            out.append("--speech")
        if self.args.provider:
            out += ["--provider", self.args.provider]
        for path in self.args.allow_path or []:
            out += ["--allow-path", path]
        return out

    def make_bot(self, name):
        """The bot of one account: its own state, its own workspace."""
        argv = self.base_argv() + ["--account", name]
        # The accounts live in this directory: the bot has to be told where
        # its own directory is (otherwise it would look in the default one).
        argv += ["--state-dir", self.state_dir(name),
                 "--workspace-path", self.workspace_path(name)]
        args = wechat_bot.build_parser().parse_args(argv)
        return wechat_bot.WeChatBot(args)

    def serve(self, name, event):
        """Run one account - and run it again if it dies.

        The event is the account's own "stop" button: when it is set, the
        bot lets go and this thread ends.
        """
        while not self.stop.is_set() and not event.is_set():
            bot = None
            try:
                bot = self.make_bot(name)
                bot.stop_event = event
                with self.lock:
                    self.bots[name] = bot
                bot.run()
            except Exception as exc:  # noqa: BLE001 - one account must not kill the rest
                log("the bot of %s stopped: %s" % (name, exc))
            finally:
                with self.lock:
                    if self.bots.get(name) is bot:
                        del self.bots[name]
            if self.stop.is_set() or event.is_set():
                return
            time.sleep(self.args.retry_delay)

    def start(self, name):
        """Give this account a thread of its own."""
        event = threading.Event()
        with self.lock:
            self.stops[name] = event
        thread = threading.Thread(target=self.serve, args=(name, event), daemon=True,
                                  name="wechat-%s" % name)
        thread.start()
        log("serving the account %s" % name)
        return thread

    def running(self, name):
        """Is this account being served right now?"""
        with self.lock:
            bot = self.bots.get(name)
            event = self.stops.get(name)
        return bot is not None and not (event is not None and event.is_set())

    # -- the loop ---------------------------------------------------------
    def stop_gone(self, names):
        """Stop the accounts that are gone.

        An account whose directory was deleted must not keep polling: a
        second bot with the same cursor would eat the messages of the one
        that serves it.
        """
        with self.lock:
            stops = dict(self.stops)
        for name, event in stops.items():
            if name in names:
                continue
            log("the account %s is gone - stopping its bot" % name)
            event.set()

    def run(self):
        """Watch the accounts: a new one is picked up as soon as it exists."""
        log("accounts: %s" % (", ".join(self.names()) or "(none yet)"))
        while not self.stop.is_set():
            names = self.names()
            for name in names:
                if self.running(name):
                    continue
                if self.unit_active(name):
                    log("the account %s has a service of its own (%s) - left alone"
                        % (name, self.unit_of(name)))
                    continue
                self.start(name)
            self.stop_gone(names)
            self.stop.wait(self.args.scan_interval)
        return 0


def build_parser():
    parser = argparse.ArgumentParser(description="Several WeChat accounts in one process.")
    parser.add_argument("--accounts", default="",
                        help="the accounts to serve (alice,bob); "
                             "default: every account in the directory")
    parser.add_argument("--login", default="",
                        help="log one account in: print the link of a QR code and "
                             "wait for the scan (the credentials land in that "
                             "account's own directory)")
    parser.add_argument("--accounts-dir", default=wechat_bot.DEFAULT_STATE_DIR,
                        help="where the accounts live (default: %s)"
                             % wechat_bot.DEFAULT_STATE_DIR)
    parser.add_argument("--workspaces-dir", default=os.path.expanduser("~/.tea_agent"),
                        help="where the workspaces of the accounts live")
    parser.add_argument("--base-url", default=wechat_bot.DEFAULT_BASE_URL,
                        help="the iLink server")
    parser.add_argument("--agent", default=os.environ.get("TEA_AGENT_URL",
                                                         wechat_bot.DEFAULT_AGENT),
                        help="the agent's HTTP API")
    parser.add_argument("--tasks", default=os.environ.get("TEA_TASK_URL",
                                                         wechat_bot.DEFAULT_TASKS),
                        help="the task server's HTTP API (shared by every account)")
    parser.add_argument("--cdn-url", default=os.environ.get("TEA_WECHAT_CDN",
                                                            wechat_bot.CDN_BASE_URL),
                        help="the WeChat CDN that stores the media")
    parser.add_argument("--triggers", action="store_true",
                        help="go through the queue's trigger entry")
    parser.add_argument("--speech", action="store_true",
                        help="let the router speak")
    parser.add_argument("--provider", default=os.environ.get("TEA_TASK_PROVIDER", ""),
                        help="the provider whose quota gates the work")
    parser.add_argument("--allow-path", action="append", default=[],
                        help="a directory the tasks may touch (repeatable)")
    parser.add_argument("--login-timeout", type=float, default=300.0,
                        help="how long to wait for a scan (default: 300 s)")
    parser.add_argument("--scan-interval", type=float, default=10.0,
                        help="how often to look for new accounts (default: 10 s)")
    parser.add_argument("--retry-delay", type=float, default=5.0,
                        help="how long to wait before an account is tried again")
    parser.add_argument("--no-skip-units", dest="skip_units", action="store_false",
                        help="serve an account even when it has a service of its own")
    parser.set_defaults(skip_units=True)
    return parser


def login(multi, name):
    """One account, one QR code, one scan.

    The link is printed: the page it points at shows the code, and the phone
    confirms it. When that happens the credentials are written into the
    account's own directory - and the service picks the account up by itself.
    """
    bot = multi.make_bot(name)
    print("")
    print("账号：%s" % name)
    print("凭证会存到：%s" % bot.state.path)
    print("工作区：%s（%s）" % (bot.args.workspace_path, bot.args.workspace_name))
    print("")
    ok = bot.login()
    print("")
    if not ok:
        print("没扫成功（超时了）——再跑一次这条命令就行。")
        return 1
    print("登录成功：%s" % bot.state.path)
    print("这个账号现在会用 %s 排任务，工作区 %s。"
          % (multi.args.tasks, bot.args.workspace_name))
    print("服务在跑的话，它下一轮就会接上这个账号（不用重启）。")
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    multi = Multi(args)
    try:
        if args.login:
            return login(multi, args.login)
        return multi.run()
    except KeyboardInterrupt:
        log("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
