#!/usr/bin/env python3
# nat_probe.py -- 现场验证「这两台机器之间能不能直接打洞」。
#
# 为什么需要它：隧道能不能走 P2P，取决于两边 NAT 的行为。客户口头说
# 「我们这支持打洞」不算数——要在这两台真实机器之间跑一次，看 UDP 是不是
# 真能双向通。
#
# ## 协议（和真实 ICE 一样：两边都知道对方的地址，同时主动打洞）
#
#   第 1 步，两台机器各跑一次，拿到自己的「公网视角地址」：
#       python3 nat_probe.py probe
#       -> 打印 NAT 判定 + 一行 token（<ip>:<port>:<rand>）
#
#   第 2 步，把两边的 token 互抄给对方（人工复制粘贴）：
#       A 拿到 B 的 token，B 拿到 A 的 token
#
#   第 3 步，两台机器同时跑（时间对齐，最好同一分钟）：
#       python3 nat_probe.py punch --mine <自己的token> --peer <对方的token>
#
#   第 4 步：两边都打印「P2P 打洞成立 / 打不通」。
#
# ⚠️ **两边必须用同一个 STUN 服务器**：对称 NAT 的映射端口随目标变，
# 只有「双方经同一个服务器看到的地址」才是可比的。
#
# 纯标准库，Python 3.6+ 可跑，不需要编译、不需要 root、不装任何东西。

from __future__ import print_function

import argparse
import os
import socket
import struct
import sys
import time

# ---- STUN (RFC 5389) --------------------------------------------------------
STUN_MAGIC = 0x2112A442
STUN_BINDING_REQUEST = 0x0001
STUN_BINDING_RESPONSE = 0x0101
ATTR_MAPPED_ADDRESS = 0x0001
ATTR_XOR_MAPPED_ADDRESS = 0x0020

# 默认服务器是挑过的：
#   * stun.nextcloud.com 的 3478 和 443 是**同一个 IP 的两个端口** ——
#     这是判断「映射是不是连端口都依赖目标」的唯一有效对照；
#   * stun.l.google.com / stun1.l.google.com 是**同一个 IP**（74.125.250.129），
#     它们端口一样**说明不了任何事**；
#   * stun.miwifi.com 是国内的一个，省得客户那边出不去 google。
DEFAULT_STUN = [
    "stun.l.google.com:19302",
    "stun.cloudflare.com:3478",
    "stun.nextcloud.com:3478",
    "stun.nextcloud.com:443",
    "stun.miwifi.com:3478",
]

SEND_INTERVAL = 0.2  # 打洞包：5 个/秒，够穿透 NAT，又不像在打洪水


def stun_binding_request(txid):
    return struct.pack(">HHI", STUN_BINDING_REQUEST, 0, STUN_MAGIC) + txid


def _parse_stun(data):
    """-> (mtype, {attr_type: value}) or None"""
    if len(data) < 20:
        return None
    mtype, mlen, cookie = struct.unpack(">HHI", data[:8])
    if cookie != STUN_MAGIC:
        return None
    attrs = {}
    off = 20
    end = min(len(data), 20 + mlen)
    while off + 4 <= end:
        atype, alen = struct.unpack(">HH", data[off:off + 4])
        attrs[atype] = data[off + 4:off + 4 + alen]
        off += 4 + alen + ((4 - (alen % 4)) % 4)
    return mtype, attrs


def _addr_from_attr(value, xor):
    if len(value) < 8:
        return None
    port = struct.unpack(">H", value[2:4])[0]
    ip = value[4:8]
    if xor:
        port ^= (STUN_MAGIC >> 16) & 0xFFFF
        mask = bytes(bytearray([(STUN_MAGIC >> 24) & 0xFF, (STUN_MAGIC >> 16) & 0xFF,
                                (STUN_MAGIC >> 8) & 0xFF, STUN_MAGIC & 0xFF]))
        ip = bytes(bytearray(b ^ m for b, m in zip(ip, mask)))
    return ("%d.%d.%d.%d" % tuple(bytearray(ip)), port)


def stun_reflexive(sock, server, timeout=2.0):
    """问 STUN 服务器「你看到我是谁」-> (ip, port) 或 None"""
    host, _, port = server.rpartition(":")
    try:
        addr = (host, int(port))
    except ValueError:
        return None
    txid = os.urandom(12)
    sock.settimeout(timeout)
    try:
        sock.sendto(stun_binding_request(txid), addr)
        data, _ = sock.recvfrom(2048)
    except (socket.timeout, OSError):
        return None
    parsed = _parse_stun(data)
    if not parsed:
        return None
    mtype, attrs = parsed
    if mtype != STUN_BINDING_RESPONSE:
        return None
    if ATTR_XOR_MAPPED_ADDRESS in attrs:
        return _addr_from_attr(attrs[ATTR_XOR_MAPPED_ADDRESS], True)
    if ATTR_MAPPED_ADDRESS in attrs:
        return _addr_from_attr(attrs[ATTR_MAPPED_ADDRESS], False)
    return None


def resolve(host):
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM)
        return sorted(set(i[4][0] for i in infos))
    except socket.gaierror:
        return []


def ask_stun(sock, servers):
    """问一圈 STUN 服务器 -> [(label, server_ip, server_port, refl_ip, refl_port)]"""
    seen = []
    for srv in servers:
        host, _, port = srv.rpartition(":")
        try:
            port = int(port)
        except ValueError:
            continue
        got = stun_reflexive(sock, srv)
        if got:
            ips = resolve(host)
            seen.append((srv, ips[0] if ips else "?", port, got[0], got[1]))
    return seen


# ---- NAT 行为的判定 --------------------------------------------------------
# 两个维度分开看，再取更糟的那个：
#
#   ① 同一个 IP 换端口 → 映射端口变不变？ 变了 = symmetric（最坏）
#   ② 换个目标 IP     → 映射端口变不变？ 变了 = address-dependent
#
# ⚠️ 只有「同一个 IP 的两个端口都测到」才能排除 symmetric。
# 少了这条对照，结论要标「未排除对称」，不能装作已经测出来。
def classify_nat(seen):
    if not seen:
        return "unknown", "没有 STUN 服务器回应，UDP 出站可能不通"

    by_ip = {}
    for label, ip, sport, rip, rport in seen:
        by_ip.setdefault(ip, []).append((sport, rport))

    same_ip_port_dep = None   # None = 没测出来
    for ip, items in by_ip.items():
        sports = set(s for s, _ in items)
        rports = set(r for _, r in items)
        if len(sports) > 1:
            same_ip_port_dep = len(rports) > 1
            break

    all_rports = set(entry[4] for entry in seen)
    addr_dep = len(all_rports) > 1

    if same_ip_port_dep is True:
        return "symmetric", ("同一个 IP 换个端口，映射端口就变（%s）"
                             % ", ".join(str(p) for p in sorted(all_rports)))
    if same_ip_port_dep is None:
        why = ("换个目标 IP，映射端口%s（%s）；**同 IP 不同端口的对照没测到，"
               "对称 NAT 尚未排除**"
               % ("就变" if addr_dep else "不变",
                  ", ".join(str(p) for p in sorted(all_rports))))
        return "unknown-port", why
    if addr_dep:
        why = ("换个目标 IP，映射端口就变（%s）" % ", ".join(str(p) for p in sorted(all_rports)))
        why += "；但同一个 IP 换端口映射不变（不是最坏的对称 NAT）"
        return "address-dep", why
    return "cone", "所有 STUN 服务器看到的映射端口都一样（%d）" % list(all_rports)[0]


VERDICTS = {
    "cone": "打洞基本必成。",
    "address-dep": ("映射端口随目标 IP 变（但与目标端口无关）——打洞**有可能**成，"
                    "但取决于过滤规则，必须实测；不通就走 TURN 中继。"),
    "symmetric": ("打洞大概率失败（这是教科书里的对称 NAT）——"
                  "隧道必须能走 TURN 中继，不能只靠打洞。"),
    "unknown-port": ("只测到「换 IP 换端口」，**对称 NAT 还没排除**——"
                     "重跑一次让同一个 IP 的两个端口都应答（默认列表里已有，"
                     "可能是那台机器出不去那个服务器）。"),
    "unknown": "UDP 出站可能不通，先查防火墙。",
}


# ---- 打洞用的应用层包 ------------------------------------------------------
MAGIC = b"A2NAT1"


def token_encode(ip, port):
    return "%s:%d:%s" % (ip, port, os.urandom(3).hex())


def token_decode(token):
    parts = token.strip().split(":")
    if len(parts) < 2:
        raise ValueError("token 坏了：应该长成 <ip>:<port>:<rand>")
    return parts[0], int(parts[1])


def payload(ip, port):
    return MAGIC + ("%s:%d" % (ip, port)).encode("ascii")


def parse_payload(data):
    if not data.startswith(MAGIC):
        return None
    try:
        host, _, port = data[len(MAGIC):].decode("ascii").rpartition(":")
        return host, int(port)
    except (ValueError, UnicodeDecodeError):
        return None


def show_nat(label, seen):
    kind, why = classify_nat(seen)
    print("[%s] NAT 判定：%s" % (label, kind))
    print("[%s]   依据：%s" % (label, why))
    for srv, ip, sport, rip, rport in seen:
        print("[%s]   %-28s ip=%-16s 端口 %-5d -> 反射 %s:%d" % (label, srv, ip, sport, rip, rport))
    print("[%s]   含义：%s" % (label, VERDICTS.get(kind, "")))
    return kind


# ---- 两个阶段 ---------------------------------------------------------------
def run_probe(args):
    """第 1 步：问 STUN，拿到自己的公网视角地址，印 token 出来。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(0.5)

    servers = args.stun or DEFAULT_STUN
    seen = ask_stun(sock, servers)
    if not seen:
        print("[probe] 警告：所有 STUN 服务器都没回。UDP 出站可能不通。")
        show_nat("probe", seen)
        return 1
    print("[probe] 用的 STUN 服务器：%s" % seen[0][0])
    show_nat("probe", seen)
    mine = token_encode(seen[0][3], seen[0][4])
    print("")
    print("  ---- 我的 token（抄给对方）----")
    print("  " + mine)
    print("  --------------------------------")
    print("")
    print("[probe] 下一步：拿到对方的 token 之后，同时跑")
    print("        python3 nat_probe.py punch --mine %s --peer <对方的token>" % mine)
    return 0


def run_punch(args):
    """第 3 步：两边同时主动打洞（和 ICE 一样：都知道对方地址）。"""
    try:
        my_ip, my_port = token_decode(args.mine)
    except ValueError as e:
        print("[punch] 自己的 token 有问题：%s" % e)
        return 2
    try:
        peer_ip, peer_port = token_decode(args.peer)
    except ValueError as e:
        print("[punch] 对端的 token 有问题：%s" % e)
        return 2

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(0.2)

    target = (peer_ip, peer_port)
    body = payload(my_ip, my_port)
    print("[punch] 我 %s:%d -> 打 %s:%d（%d 秒，每秒 5 个包）"
          % (my_ip, my_port, peer_ip, peer_port, args.wait))
    sent = 0
    replies = 0
    peer_seen = None
    rtt = None
    deadline = time.time() + args.wait
    while time.time() < deadline:
        last_sent = time.time()
        try:
            sock.sendto(body, target)
            sent += 1
        except OSError:
            pass
        try:
            data, src = sock.recvfrom(4096)
        except socket.timeout:
            time.sleep(SEND_INTERVAL)
            continue
        except OSError:
            break
        replies += 1
        if rtt is None:
            rtt = time.time() - last_sent
        parsed = parse_payload(data)
        if parsed:
            peer_seen = parsed
        if replies <= 3:
            print("[punch] 收到包 %d 字节，来自 %s:%d%s" %
                  (len(data), src[0], src[1],
                   ("  对端 srflx=%s:%d" % peer_seen) if peer_seen else ""))
        time.sleep(SEND_INTERVAL)

    print("")
    print("[punch] 发了 %d 个包，收到 %d 个回包%s" %
          (sent, replies, ("，延迟 %.0f ms" % (rtt * 1000)) if rtt else ""))
    if replies:
        print("[punch] 结论：P2P 打洞成立（双向都通）。")
        if peer_seen:
            print("[punch] 对端 srflx：%s:%d" % peer_seen)
        return 0
    print("[punch] 结论：打不通。两边都没收到对方的包 → 走 TURN/中继兜底。")
    return 1


def main(argv):
    p = argparse.ArgumentParser(
        description="NAT 打洞验证：probe（拿 token）→ 互抄 token → punch（同时打洞）")
    p.add_argument("role", choices=["probe", "punch"], help="probe=第 1 步，punch=第 3 步")
    p.add_argument("--mine", default="", help="punch 模式：自己的 token")
    p.add_argument("--peer", default="", help="punch 模式：对方的 token")
    p.add_argument("--stun", action="append", default=[],
                   help="STUN 服务器 host:port，可重复；**两边必须用同一个**")
    p.add_argument("--port", type=int, default=0, help="probe 模式：本地 UDP 端口（默认随机）")
    p.add_argument("--wait", type=int, default=60, help="punch 模式：打洞秒数（默认 60）")
    args = p.parse_args(argv)

    if args.role == "probe":
        return run_probe(args)
    if not args.mine or not args.peer:
        print("[punch] 需要 --mine <自己的token> --peer <对方的token>（两边 token 互换）")
        return 2
    return run_punch(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
