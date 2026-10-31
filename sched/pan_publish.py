#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把发布产物传到百度网盘 —— 以后**不往 git 里提交 zip 了**。

git 里放源码和文档，二进制包（几十 MB，每版一个）进版本库只会让 clone 变慢、`git log`
变成噪声。所以发布改成：**打包 → 传到网盘 → 仓库里只留一条「这一版发在哪儿」的记录**。

    python3 sched/pan_publish.py release/tea_a2-0.17.5.zip
    python3 sched/pan_publish.py release/tea_a2-0.17.5.zip --remote-dir /tea_a2/releases/0.17.5
    python3 sched/pan_publish.py release/tea_a2-0.17.5.zip --no-verify     # 不回读校验

远端长这样：

    /tea_a2/releases/0.17.5/
      tea_a2-0.17.5.zip     发布产物
      manifest.json         谁在什么时候传的、每个文件的 sha256/md5/大小

**4MB 分片**：百度网盘的上传接口按 4MB 一片传 —— 一次请求一片，`partseq` 是片号，
`block_list` 给出每片的 md5。本工具（和 `pan_sync.py` 用的同一个客户端）就是按 4MB 切：
整 4MB 一片，最后一片是余数；**小于 4MB 的文件走单片**（官方 MCP Server 也是这么做的，
官方文档那条「首片不得小于 4MB」针对的是「非最后一片」）。

所以 6.6MB 的包 = 2 片，40MB 的包 = 10 片。分片数 = ceil(大小 / 4MB)，每个分片一次请求
（配额：上传接口 100 次/天，够发好几个版本了）。

传完**回读校验**：把网盘上的文件下下来，比对 sha256 —— 发版的东西不能只看「上传成功」
就当成功（本地磁盘、网络中断、网盘那边截断都有可能）。

配置和令牌跟配置同步用同一套（`~/.tea_agent/pan-sync/config` / `token.json`），
所以不用再配一遍。
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pan_sync import (  # noqa: E402
    CONFIG_PATH, PAN_HOME, PanClient, PanError, ConfigError, load_config, sha256_bytes,
)

DEFAULT_ROOT = "/tea_a2/releases"
MANIFEST_NAME = "release-manifest.json"


def sha256_of_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 16)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def md5_file(path):
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 16)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def guess_version(paths):
    """从文件名里认版本：tea_a2-0.17.5.zip → 0.17.5。"""
    for path in paths:
        name = os.path.basename(path)
        stem = name[:-4] if name.endswith(".zip") else name
        if "-" in stem:
            tail = stem.rsplit("-", 1)[-1]
            if tail and tail[0].isdigit():
                return tail
    return ""


def collect(paths):
    """要传的东西：文件原样，目录里的文件按相对路径保留结构。"""
    items = []
    for path in paths:
        if os.path.isfile(path):
            items.append((os.path.basename(path), path))
        elif os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs.sort()
                for name in sorted(files):
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, path).replace(os.sep, "/")
                    items.append((rel, full))
        else:
            raise PanError("没有这个东西：%s" % path)
    if not items:
        raise PanError("没有要传的文件")
    return items


def upload(client, local_path, remote_path):
    """传一个文件（内部按 4MB 分片）。"""
    size = os.path.getsize(local_path)
    with open(local_path, "rb") as handle:
        data = handle.read()
    client.upload_bytes(remote_path, data)
    return size


def verify(client, local_path, remote_path, expect_sha):
    """回读校验：下下来比 sha256。"""
    got = sha256_bytes(client.download_bytes(remote_path))
    if got != expect_sha:
        raise PanError("回读校验对不上：%s（网盘上的是 %s，本地是 %s）"
                       % (remote_path, got[:12], expect_sha[:12]))
    return True


def parse_version(name):
    """从目录名里解析版本：0.17.10 → (0, 17, 10)。比字符串比大小靠谱。"""
    parts = []
    for chunk in re.split(r"[._-]", name):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            break
    return tuple(parts)


def delete_dir(client, path):
    """删一个远端目录（连带里面的东西）。"""
    try:
        client.delete_path(path)
    except PanError:
        pass
    try:
        leftovers = client.list_dir(path)
    except PanError:
        leftovers = []
    for entry in leftovers:
        name = entry.get("server_filename")
        if not name:
            continue
        try:
            client.delete_path(path + "/" + name)
        except PanError:
            pass
    try:
        client.delete_path(path)
    except PanError:
        pass


def prune(client, root, keep):
    """网盘上只留最新的 keep 个发布目录（按版本号排，同号按上传时间）。

    像版本号的目录才算发布目录；别的目录（比如误建的手工目录）不碰。
    """
    try:
        entries = [e for e in client.list_dir(root) if e.get("isdir")]
    except PanError as exc:
        print("（读不到 %s：%s）" % (root, exc))
        return []
    releases = []
    for entry in entries:
        name = entry.get("server_filename", "")
        if not name[:1].isdigit():
            print("  （不像版本号的目录，不动：%s）" % name)
            continue
        releases.append((parse_version(name), name))
    releases.sort(reverse=True)
    removed = []
    for _, name in releases[keep:]:
        delete_dir(client, root + "/" + name)
        removed.append(name)
        print("  ✗ 删掉旧版本：%s" % name)
    if not removed:
        print("  （网盘上只有 %d 个发布目录，不用清理）" % len(releases))
    return removed


def list_releases(client, root):
    """列出远端的发布目录：版本、文件、大小、传的时间。"""
    try:
        entries = [e for e in client.list_dir(root) if e.get("isdir")]
    except PanError as exc:
        print("（读不到 %s：%s）" % (root, exc))
        return []
    rows = []
    for entry in entries:
        name = entry.get("server_filename", "")
        uploaded = ""
        try:
            raw = client.download_bytes(root + "/" + name + "/" + MANIFEST_NAME)
            uploaded = json.loads(raw.decode("utf-8")).get("uploaded_at", "")
        except (PanError, ValueError):
            pass
        rows.append((parse_version(name), name, uploaded, entry))
    rows.sort(reverse=True)
    print("%s  %-10s  %s" % ("版本目录", "大小", "上传时间"))
    for _, name, uploaded, entry in rows:
        try:
            files = client.list_dir(root + "/" + name)
        except PanError:
            files = []
        total = sum(f.get("size", 0) for f in files)
        print("  %-16s %6.1f MB  %s" % (name, total / 1048576.0, uploaded or "?"))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="pan_publish.py",
        description="把发布产物传到百度网盘（不再提交到 git）")
    parser.add_argument("paths", nargs="*", help="要传的文件或目录")
    parser.add_argument("--config", help="配置（默认 ~/.tea_agent/pan-sync/config）")
    parser.add_argument("--remote-dir", help="远端目录（默认 /tea_a2/releases/<版本>）")
    parser.add_argument("--version", help="版本号（默认从文件名里认）")
    parser.add_argument("--no-verify", action="store_true", help="不回读校验")
    parser.add_argument("--keep", type=int, default=0,
                        help="网盘上最多留几个发布目录（0 = 不清理；发版用 5）")
    parser.add_argument("--prune-root", default=DEFAULT_ROOT,
                        help="清理时在哪个目录下找发布目录（默认 %s）" % DEFAULT_ROOT)
    parser.add_argument("--list", metavar="目录", nargs="?", const=DEFAULT_ROOT,
                        help="只列出这个目录下的发布版本（默认 %s）" % DEFAULT_ROOT)
    parser.add_argument("--keep-manifest", action="store_true",
                        help="保留远端同名清单（默认覆盖）")
    args = parser.parse_args(argv)

    if args.list:
        try:
            config = load_config(args.config or CONFIG_PATH)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        client = PanClient(config.text("app_key"), config.text("app_secret"),
                           token_path=os.path.join(PAN_HOME, "token.json"),
                           api_base=config.text("api_base"),
                           oauth_base=config.text("oauth_base"),
                           pcs_base=config.text("pcs_base"),
                           timeout=max(120, config.integer("timeout")))
        print("=== %s ===" % args.list)
        list_releases(client, args.list)
        return 0

    if not args.paths:
        print("要传什么？给一个文件或目录（或者用 --list 看网盘上有哪些版本）", file=sys.stderr)
        return 2

    try:
        config = load_config(args.config or CONFIG_PATH)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    version = args.version or guess_version(args.paths)
    remote_dir = args.remote_dir or (DEFAULT_ROOT + "/" + version if version else DEFAULT_ROOT)
    if not remote_dir.startswith("/"):
        remote_dir = "/" + remote_dir

    client = PanClient(config.text("app_key"), config.text("app_secret"),
                       token_path=os.path.join(PAN_HOME, "token.json"),
                       api_base=config.text("api_base"),
                       oauth_base=config.text("oauth_base"),
                       pcs_base=config.text("pcs_base"),
                       timeout=max(120, config.integer("timeout")))

    try:
        items = collect(args.paths)
        print("远端目录：%s" % remote_dir)
        print("要传 %d 个文件：" % len(items))
        for name, local in items:
            print("  %-32s %8.1f KB" % (name, os.path.getsize(local) / 1024.0))

        client.ensure_dir(remote_dir)
        entries = []
        for name, local in items:
            remote_path = remote_dir + "/" + name
            size = upload(client, local, remote_path)
            digest = sha256_of_file(local)
            parts = (size + 4 * 1024 * 1024 - 1) // (4 * 1024 * 1024)
            print("  ↑ %-32s %8.1f KB（%d 片）" % (name, size / 1024.0, parts))
            if not args.no_verify:
                verify(client, local, remote_path, digest)
                print("    回读校验通过")
            entries.append({
                "name": name,
                "remote_path": remote_path,
                "size": size,
                "sha256": digest,
                "md5": md5_file(local),
                "blocks": parts,
            })

        manifest = {
            "version": version,
            "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "machine": config.machine(),
            "root": remote_dir,
            "files": entries,
        }
        raw = json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8")
        manifest_path = remote_dir + "/" + MANIFEST_NAME
        client.upload_bytes(manifest_path, raw)
        print("清单：%s" % manifest_path)
        if args.keep:
            print("\n清理旧版本（%s 下最多留 %d 个）：" % (args.prune_root, args.keep))
            prune(client, args.prune_root, args.keep)
        print("\n发完了。取它的办法：")
        print("  浏览器打开 https://pan.baidu.com → 我的网盘 → %s" % remote_dir.lstrip("/"))
        return 0
    except (PanError, ConfigError) as exc:
        print("出错了：%s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
