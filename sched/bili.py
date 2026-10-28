# -*- coding: utf-8 -*-
"""bilibili 观看历史 → 字幕 → LLM 摘要 → markdown digest。

个人工具（不是 tea_a2 产品的一部分），沿用 sched/ 这一侧 Python 的位置，
但完全独立：不依赖 tea_agent 二进制，只共用 ~/.tea_agent/provider.yaml
里的 provider 配置。

    python3 sched/bili.py --login                           # 登录：开有头浏览器登录并存 storageState
    python3 sched/bili.py fetch      --pages 2 --limit 20     # 拉历史+字幕，存语料
    python3 sched/bili.py summarize  --limit 6                # 调 LLM 生成结构化摘要
    python3 sched/bili.py digest                              # 输出 markdown digest
    python3 sched/bili.py digest     --overview               # 额外做一次跨视频综述
    python3 sched/bili.py all                                 # fetch+summarize+digest 一条龙

设计要点：
  * 不用浏览器：B 站这些接口都是「纯 HTTP + cookie」。登录态从一个
    Playwright storageState 文件里读（cookie 是明文，跨 OS 通用）。
  * 字幕要**认准视频**：B 站的 player/v2 会随机串台，把别的视频的字幕轨
    一起返回（同样 lan='ai-zh'，id 和内容却是另一个视频的）。实测命中率
    只有一半，所以要多试。认准的办法有两个，两个都用上：
      1) x/web-interface/view 的 subtitle.list 是稳定的权威轨道列表，
         用轨道 id 跟它比对（view 的 id 偶尔会少最后一位，所以还加前缀匹配）；
      2) 正确的字幕 URL 形如 /bfs/ai_subtitle/prod/{aid}{cid}{hash}，
         里面连着 aid 和 cid。
    两条都不满足的轨道一律丢掉，宁可没有字幕也不要串台的。
  * 字幕不重抓：corpus 里已有的 bvid 直接复用（--refresh 强制重抓）。
  * 摘要用结构化 JSON 输出，方便下游（微信推送、Web UI）消费。
"""
import argparse
import json
import os
import re
import sys
import time

# 延迟依赖：--login 只要标准库，裸 Python 也能跑
try:
    import requests
except ImportError:
    requests = None
try:
    import yaml
except ImportError:
    yaml = None

HOME = os.path.expanduser("~")
STATE_DIR = os.environ.get("BILI_HOME") or os.path.join(HOME, ".tea_agent", "bili")
CORPUS = os.path.join(STATE_DIR, "corpus.json")
SUMMARIES = os.path.join(STATE_DIR, "summaries.json")
DIGEST = os.path.join(STATE_DIR, "digest.md")
PROVIDER_YAML = os.path.join(HOME, ".tea_agent", "provider.yaml")

# 登录态来源：优先本机的一份，其次是 Windows 侧导出的那份
WIN_DOCS = os.path.join(HOME, "Documents")
STATE_CANDIDATES = [
    os.environ.get("BILI_STATE", ""),
    os.path.join(HOME, ".tea_agent", "bili_state.json"),
    "/mnt/c/Users/Hetin/Documents/pw_bilibili.json",
    os.path.join(WIN_DOCS, "pw_bilibili.json"),
]
if os.name == "nt":
    STATE_DEFAULT = os.path.join(WIN_DOCS, "pw_bilibili.json")
else:
    STATE_DEFAULT = "/mnt/c/Users/Hetin/Documents/pw_bilibili.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 字幕语言优先级：人写的 > AI 中文 > 其它（英文/日文翻译稿也能用）
LAN_PRIORITY = {"zh-CN": 0, "zh-Hans": 0, "zh-Hant": 1, "ai-zh": 2, "zh": 3,
                "ai-en": 5, "ai-ja": 6}


# --------------------------------------------------------------------------
# 登录态
# --------------------------------------------------------------------------
def load_cookies():
    for path in STATE_CANDIDATES:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                st = json.load(fh)
            jar = [(c["name"], c["value"], c["domain"]) for c in st.get("cookies", [])
                   if c["domain"].endswith(("bilibili.com", "bilibili.cn",
                                            "biligame.com", "huasheng.cn"))]
            print("[bili] 登录态: %s (%d 个 cookie)" % (path, len(jar)))
            return jar
    raise SystemExit("找不到登录态文件。先跑 .tea_agent_run/scripts/bili_login_refresh.mjs 生成，"
                     "或用 BILI_STATE=/path/to/state.json 指定。")


def make_session():
    if requests is None:
        raise SystemExit("需要 requests：pip3 install requests")
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    for name, value, domain in load_cookies():
        s.cookies.set(name, value, domain=domain, path="/")
    return s


def api(sess, url, tries=3):
    """调 B 站 API，返回 data；失败重试。"""
    last = None
    for i in range(tries):
        try:
            r = sess.get(url, timeout=20)
            j = r.json()
            if j.get("code") == 0:
                return j.get("data")
            last = "code=%s msg=%s" % (j.get("code"), j.get("message"))
        except Exception as exc:                      # 网络抖动 / 返回不是 json
            last = repr(exc)
        time.sleep(1.0 + i)
    raise RuntimeError("接口失败 %s: %s" % (url, last))


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------
def fetch_history(sess, pages=2, ps=30):
    """观看历史（按时间倒序），带翻页。"""
    items, cursor = [], None
    for _ in range(pages):
        url = ("https://api.bilibili.com/x/web-interface/history/cursor"
               "?ps=%d&type=archive" % ps)
        if cursor:
            url += "&max=%s" % cursor
        data = api(sess, url)
        lst = data.get("list") or []
        if not lst:
            break
        for v in lst:
            items.append({
                "bvid": (v.get("history") or {}).get("bvid"),
                "cid": (v.get("history") or {}).get("cid"),
                "aid": (v.get("history") or {}).get("oid"),
                "title": v.get("title"),
                "author": v.get("author_name"),
                "tag": v.get("tag_name"),
                "view_at": v.get("view_at"),
                "progress": v.get("progress"),
                "duration": v.get("duration"),
                "url": v.get("uri"),
            })
        cursor = (data.get("cursor") or {}).get("max")
        if not cursor:
            break
    return items


def auth_track_ids(sess, bvid):
    """这个视频**真正的**字幕轨道 id 集合。来源：view 的 subtitle.list（稳定）。"""
    view = api(sess, "https://api.bilibili.com/x/web-interface/view?bvid=%s" % bvid)
    lst = (view.get("subtitle") or {}).get("list") or []
    return [str(t.get("id")) for t in lst if t.get("id")]


def id_matches(tid, auth_ids):
    """轨道 id 和权威 id 比对。

    view 的 list 里的 id 偶尔会少最后一位（208297600337016268 对上
    2082976003370162688），所以不能只比相等，前缀相同也算。
    """
    tid = str(tid or "")
    if not tid:
        return False
    for a in auth_ids:
        a = str(a)
        if tid == a or tid.startswith(a) or a.startswith(tid):
            return True
    return False


def pick_track(tracks, auth_ids, aid, cid):
    """从 player/v2 的返回里挑属于本视频的轨道。

    先用权威 id 匹配，再用「URL 里含 {aid}{cid}」匹配；都挑不到就返回 None
    （宁可没有字幕，也不要串台的）。
    """
    by_id = [t for t in tracks if id_matches(t.get("id"), auth_ids)]
    if by_id:
        by_id.sort(key=lambda t: LAN_PRIORITY.get(t.get("lan"), 9))
        return by_id[0]
    if aid and cid:
        exact = "%s%s" % (aid, cid)
        by_url = [t for t in tracks if exact in (t.get("subtitle_url") or "")]
        if by_url:
            by_url.sort(key=lambda t: LAN_PRIORITY.get(t.get("lan"), 9))
            return by_url[0]
    return None


def fetch_subtitle(sess, bvid, cid, aid=None, tries=24):
    """字幕正文。返回 (lines, lan)。拿不到就抛异常。

    player/v2 每次返回的轨道集合是随机的（会串台），实测 bvid&cid 这一路的
    命中率约一半，所以要多试几次。
    """
    auth_ids = auth_track_ids(sess, bvid)
    if not auth_ids:
        raise RuntimeError("这个视频没有字幕")
    reason = "player/v2 返回的轨道都不是本视频的（串台）"
    for attempt in range(tries):
        try:
            pv = api(sess, "https://api.bilibili.com/x/player/v2?bvid=%s&cid=%s" % (bvid, cid))
        except Exception as exc:
            reason = str(exc)
            time.sleep(0.4)
            continue
        tracks = ((pv.get("subtitle") or {}).get("subtitles")) or []
        track = pick_track(tracks, auth_ids, aid, cid)
        if not track:
            time.sleep(0.3)
            continue
        url = track.get("subtitle_url") or ""
        if not url:
            reason = "权威轨道没有 url"
            time.sleep(0.3)
            continue
        if url.startswith("//"):
            url = "https:" + url
        try:
            body = sess.get(url, timeout=30).json()
        except Exception as exc:
            reason = "字幕正文下载失败 %r" % (exc,)
            time.sleep(0.3)
            continue
        lines = body.get("body") or []
        if not lines:
            reason = "字幕正文为空"
            time.sleep(0.3)
            continue
        return lines, track.get("lan")
    raise RuntimeError("取不到字幕: %s" % reason)


def ratio(item):
    dur = item.get("duration") or 0
    pos = item.get("progress") or 0
    if pos < 0 or dur <= 0:
        return 0.0
    return min(1.0, pos / dur)


def cmd_fetch(args):
    sess = make_session()
    items = fetch_history(sess, pages=args.pages)
    print("[bili] 历史 %d 条" % len(items))

    # 只留「看得比较认真」的：看得过半，或者绝对时长够长
    picked, seen = [], set()
    for it in items:
        if not it["bvid"] or it["bvid"] in seen:
            continue
        r = ratio(it)
        if r < args.min_ratio and (it.get("progress") or 0) < args.min_seconds:
            continue
        seen.add(it["bvid"])
        picked.append(it)
        if len(picked) >= args.limit:
            break
    print("[bili] 入选 %d 条（观看比例 ≥ %.0f%% 或 ≥ %ds）" %
          (len(picked), args.min_ratio * 100, args.min_seconds))

    corpus = {}
    if os.path.exists(CORPUS):
        with open(CORPUS, "r", encoding="utf-8") as fh:
            corpus = json.load(fh)

    got, miss, dropped = 0, [], []
    for n, it in enumerate(picked, 1):
        bvid = it["bvid"]
        old = corpus.get(bvid)
        if old and not args.refresh and old.get("lines"):
            print("  [%2d/%d] %s  (已有字幕，跳过)" % (n, len(picked), it["title"][:28]))
            continue
        try:
            lines, lan = fetch_subtitle(sess, bvid, it["cid"], it.get("aid"))
        except Exception as exc:
            print("  [%2d/%d] %s  字幕失败: %s" % (n, len(picked), it["title"][:28], exc))
            miss.append(it["title"])
            # 旧数据可能是串台留下的，丢掉，宁可没有也不要错的
            if old and args.refresh and corpus.pop(bvid, None):
                dropped.append(it["title"])
            continue
        it["sub_lines"] = len(lines)
        it["sub_chars"] = sum(len(x.get("content", "")) for x in lines)
        it["sub_lan"] = lan
        it["sub_head"] = " / ".join(x.get("content", "") for x in lines[:3])[:80]
        corpus[bvid] = {"meta": it, "lines": lines}
        got += 1
        print("  [%2d/%d] %s  字幕 %d 行 (%s)" % (n, len(picked), it["title"][:28], len(lines), lan))
        time.sleep(0.4)                       # 别把人家接口打爆

    os.makedirs(STATE_DIR, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    with open(CORPUS, "w", encoding="utf-8") as fh:
        json.dump(corpus, fh, ensure_ascii=False, indent=1)
    print("[bili] 语料: %d 个视频 → %s（新抓 %d，无字幕 %d，丢弃旧数据 %d）" %
          (len(corpus), CORPUS, got, len(miss), len(dropped)))


# --------------------------------------------------------------------------
# LLM
# --------------------------------------------------------------------------
def chat_completions_url(api_url):
    """和 tea_a2 的 llm.cpp::endpoint() 保持一致。"""
    base = (api_url or "").strip().rstrip("/")
    suffix = "/chat/completions"
    if base.endswith(suffix):
        return base
    if base.endswith("/v1"):
        return base + suffix
    if "/" not in base.split("://", 1)[-1]:
        return base + "/v1" + suffix
    return base + suffix


def pick_provider(name=None, model=None):
    if yaml is None:
        raise SystemExit("需要 pyyaml：pip3 install pyyaml")
    with open(PROVIDER_YAML, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    providers = cfg.get("providers") or {}
    if not providers:
        raise SystemExit("provider.yaml 里没有 providers")
    # 顺序就是开关：第一个是默认（和 tea_a2 一致）
    pname = name or next(iter(providers))
    p = providers[pname]
    models = p.get("models") or {}
    if isinstance(models, dict):
        mnames = list(models.keys())
    else:
        mnames = list(models)
    mname = model or (mnames[0] if mnames else p.get("default_model"))
    if not mname:
        raise SystemExit("provider %s 没有可用模型" % pname)
    return pname, p, mname


def llm_chat(p, model, messages, max_tokens=2000, temperature=0.2, tries=3):
    if requests is None:
        raise SystemExit("需要 requests：pip3 install requests")
    url = chat_completions_url(p.get("api_url"))
    headers = {"Content-Type": "application/json"}
    headers["Authorization"] = "Bearer " + (p.get("api_key") or "")
    for k, v in (p.get("headers") or {}).items():
        headers[k] = v
    body = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        body["temperature"] = temperature
    last = None
    for i in range(tries):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=180)
            if r.status_code != 200:
                last = "http %s: %s" % (r.status_code, r.text[:200])
                time.sleep(2 + i * 2)
                continue
            j = r.json()
            choice = (j.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            if not text.strip():
                # 有些模型（deepseek-flash 这种）把 token 先花在思考上，正文
                # 就空了。**别把思考过程当答案返回**——那是模型的内心戏，
                # 不是结果；调用方要的是正文。
                usage = j.get("usage") or {}
                last = ("空回复（finish_reason=%s，用掉 %s token，其中思考 %s）：%s"
                        % (choice.get("finish_reason"),
                           usage.get("completion_tokens"),
                           (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                           (msg.get("reasoning_content") or r.text)[:200]))
                time.sleep(2)
                continue
            return text
        except Exception as exc:
            last = repr(exc)
            time.sleep(2 + i * 2)
    raise RuntimeError("LLM 调用失败: %s" % last)


def extract_json(text):
    """从模型回复里抠出第一个完整的 JSON 对象。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        break
        start = text.find("{", start + 1)
    raise ValueError("回复里没有可解析的 JSON: %s" % text[:200])


SUMMARY_PROMPT = """你是一个观看记录整理助手。下面给你一个 B 站视频的标题、UP 主和字幕（可能是自动识别/翻译的，会有错字）。

请只依据字幕内容总结，不要编造字幕里没有的信息。用 JSON 回答，不要加任何解释文字：

{
  "summary": "一到两句话概括这个视频讲了什么（中文）",
  "key_points": ["要点1", "要点2", "要点3"],
  "quotes": ["字幕里最有信息量的一句原话（可以少于3条，没有就留空数组）"],
  "topics": ["主题标签1", "主题标签2"],
  "category": "AI评测|科技数码|知识科普|游戏|娱乐|生活|体育|其他"
}"""

OVERVIEW_PROMPT = """下面是你最近观看的若干 B 站视频的摘要。请写一份「我最近在看什么」的综述。

用 JSON 回答，不要加解释文字：

{
  "headline": "一句话概括这段时间你的关注点",
  "themes": [{"theme": "主题", "detail": "这个主题下你看了什么、有什么共同点"}],
  "notable": ["值得注意的具体内容（模型对比结论、新技术、值得记住的事实等）"],
  "suggestions": ["基于这些内容，你可能还想看/该关注的方向"]
}"""


def cmd_summarize(args):
    if not os.path.exists(CORPUS):
        raise SystemExit("没有语料，先跑 fetch")
    with open(CORPUS, "r", encoding="utf-8") as fh:
        corpus = json.load(fh)
    summaries = {}
    if os.path.exists(SUMMARIES):
        with open(SUMMARIES, "r", encoding="utf-8") as fh:
            summaries = json.load(fh)

    pname, prov, model = pick_provider(args.provider, args.model)
    print("[llm] provider=%s model=%s" % (pname, model))

    todo = [b for b in corpus if args.refresh or not summaries.get(b, {}).get("summary")]
    todo = todo[:args.limit]
    print("[llm] 待处理 %d 个（已有 %d 个摘要）" % (len(todo), len(summaries)))
    if not todo:
        return

    for n, bvid in enumerate(todo, 1):
        entry = corpus[bvid]
        meta = entry["meta"]
        lines = entry["lines"]
        text = "\n".join("[%s] %s" % (round(x.get("from", 0)), x.get("content", ""))
                         for x in lines)
        if len(text) > args.max_chars:
            text = text[:args.max_chars] + "\n…（字幕过长已截断）"
        head = "标题：%s\nUP主：%s\n分区：%s\n时长：%s 秒\n字幕语言：%s\n\n字幕：\n" % (
            meta.get("title"), meta.get("author"), meta.get("tag"),
            meta.get("duration"), meta.get("sub_lan"))
        print("  [%d/%d] %s" % (n, len(todo), (meta.get("title") or "")[:30]))
        try:
            raw = llm_chat(prov, model,
                           [{"role": "system", "content": SUMMARY_PROMPT},
                            {"role": "user", "content": head + text}])
            obj = extract_json(raw)
        except Exception as exc:
            print("      失败: %s" % exc)
            continue
        obj["bvid"] = bvid
        obj["title"] = meta.get("title")
        obj["author"] = meta.get("author")
        obj["tag"] = meta.get("tag")
        obj["url"] = meta.get("url")
        obj["view_at"] = meta.get("view_at")
        obj["watch_ratio"] = round(ratio(meta), 3)
        obj["sub_lan"] = meta.get("sub_lan")
        obj["model"] = model
        obj["at"] = int(time.time())
        summaries[bvid] = obj
        print("      %s" % (obj.get("summary") or "")[:70])

    os.makedirs(STATE_DIR, exist_ok=True)
    with open(SUMMARIES, "w", encoding="utf-8") as fh:
        json.dump(summaries, fh, ensure_ascii=False, indent=1)
    print("[llm] 摘要 → %s（%d 个）" % (SUMMARIES, len(summaries)))


# --------------------------------------------------------------------------
# digest
# --------------------------------------------------------------------------
CAT_ORDER = ["AI评测", "科技数码", "知识科普", "游戏", "娱乐", "生活", "体育", "其他"]


def load_summaries():
    if not os.path.exists(SUMMARIES):
        raise SystemExit("没有摘要，先跑 summarize")
    with open(SUMMARIES, "r", encoding="utf-8") as fh:
        return json.load(fh)


def fmt_time(ts):
    if not ts:
        return ""
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def cmd_digest(args):
    summaries = load_summaries()
    items = [s for s in summaries.values() if s.get("summary")]
    items.sort(key=lambda s: s.get("view_at") or 0, reverse=True)
    if args.limit:
        items = items[:args.limit]

    groups = {}
    for s in items:
        groups.setdefault(s.get("category") or "其他", []).append(s)

    out = []
    out.append("# 我最近在看什么\n")
    out.append("> 生成于 %s ｜ 共 %d 个视频\n" %
               (time.strftime("%Y-%m-%d %H:%M"), len(items)))

    overview = None
    if args.overview and items:
        pname, prov, model = pick_provider(args.provider, args.model)
        print("[llm] 生成跨视频综述 (%s/%s) …" % (pname, model))
        blob = "\n\n".join(
            "#%d %s（UP：%s）\n%s\n要点：%s" %
            (i + 1, s.get("title"), s.get("author"), s.get("summary"),
             "；".join(s.get("key_points") or []))
            for i, s in enumerate(items))
        try:
            overview = extract_json(llm_chat(
                prov, model,
                [{"role": "system", "content": OVERVIEW_PROMPT},
                 {"role": "user", "content": blob}], max_tokens=2000))
        except Exception as exc:
            print("[llm] 综述失败: %s" % exc)

    if overview:
        out.append("## 一句话\n")
        out.append("%s\n" % overview.get("headline", ""))
        for th in overview.get("themes") or []:
            out.append("- **%s**：%s" % (th.get("theme"), th.get("detail")))
        if overview.get("notable"):
            out.append("\n### 值得记住的点\n")
            for n in overview["notable"]:
                out.append("- %s" % n)
        if overview.get("suggestions"):
            out.append("\n### 可能还想看\n")
            for n in overview["suggestions"]:
                out.append("- %s" % n)
        out.append("\n---\n")

    out.append("## 分组浏览\n")
    for cat in CAT_ORDER + [c for c in groups if c not in CAT_ORDER]:
        if cat not in groups:
            continue
        out.append("### %s（%d）\n" % (cat, len(groups[cat])))
        for s in groups[cat]:
            out.append("**%s**  \n%s ｜ UP：%s ｜ %s ｜ 看到 %.0f%%\n" % (
                s.get("title"), fmt_time(s.get("view_at")), s.get("author"),
                "、".join(s.get("topics") or []), (s.get("watch_ratio") or 0) * 100))
            out.append("%s\n" % s.get("summary"))
            for kp in s.get("key_points") or []:
                out.append("- %s" % kp)
            for q in s.get("quotes") or []:
                out.append("  > %s" % q)
            out.append("- 链接：%s\n" % (s.get("url") or
                                         "https://www.bilibili.com/video/%s" % s.get("bvid")))

    text = "\n".join(out)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("[digest] → %s" % args.out)
    else:
        print(text)


def cmd_show(args):
    """终端里快速看一眼：最近 n 条。"""
    summaries = load_summaries()
    items = sorted(summaries.values(), key=lambda s: s.get("view_at") or 0, reverse=True)
    for s in items[:args.limit]:
        print("%s  %s" % (fmt_time(s.get("view_at")), (s.get("title") or "")[:44]))
        print("    %s" % (s.get("summary") or "")[:100])


def cmd_all(args):
    """一条龙：抓 → 摘要 → digest。"""
    cmd_fetch(args)
    cmd_summarize(args)
    args.out = args.out or DIGEST
    args.overview = True
    cmd_digest(args)
    print("\n[digest] %s" % args.out)


def cmd_login(args):
    """登录入口：开有头浏览器登录 B 站，把 storageState 存下来。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import browser_login

    out = browser_login.state_path("BILI_STATE", STATE_CANDIDATES,
                                    STATE_DEFAULT)
    ok, msg = browser_login.login(
        url="https://passport.bilibili.com/login",
        out=out, cookie="SESSDATA", label="bili",
        headless=args.headless, timeout=args.timeout)
    if not ok:
        raise SystemExit("[bili] 登录没成功：%s" % msg)
    good, detail = browser_login.check_state(out, "SESSDATA")
    print("[bili] %s → %s" % (out, detail))
    if not good:
        raise SystemExit("[bili] 登录态不对：" + detail)
    # 顺手验一下：导航接口认不认我们
    try:
        sess = make_session()
        nav = api(sess, "https://api.bilibili.com/x/web-interface/nav")
        print("[bili] isLogin=%s mid=%s uname=%s" % (
            (nav.get("data") or {}).get("isLogin"),
            (nav.get("data") or {}).get("mid"),
            (nav.get("data") or {}).get("uname")))
    except Exception as exc:
        print("[bili] 校验跳过：%s" % exc)


def main():
    ap = argparse.ArgumentParser(description="bilibili 观看历史知识提炼")
    ap.add_argument("--login", action="store_true",
                    help="登录入口：开有头浏览器登录，把 storageState 存下来")
    ap.add_argument("--headless", action="store_true",
                    help="配合 --login：不开窗口，改成扫码")
    ap.add_argument("--timeout", type=int, default=600, help="--login 最多等多久（秒）")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--pages", type=int, default=2, help="历史翻页数（每页 30 条）")
        p.add_argument("--limit", type=int, default=20, help="最多处理多少个视频")
        p.add_argument("--min-ratio", type=float, default=0.4, help="最少看过的比例")
        p.add_argument("--min-seconds", type=int, default=120, help="或者至少看这么多秒")
        p.add_argument("--refresh", action="store_true", help="重新抓/重算")
        p.add_argument("--provider", default=None)
        p.add_argument("--model", default=None)
        p.add_argument("--max-chars", type=int, default=24000, help="送进模型的字幕字符上限")
        p.add_argument("--out", default=None, help="digest 写到哪（默认打印）")
        p.add_argument("--overview", action="store_true", help="额外生成跨视频综述")
        return p

    common(sub.add_parser("fetch", help="拉历史 + 字幕，存本地语料")).set_defaults(func=cmd_fetch)
    common(sub.add_parser("summarize", help="调 LLM 生成结构化摘要")).set_defaults(func=cmd_summarize)
    common(sub.add_parser("digest", help="输出 markdown digest")).set_defaults(func=cmd_digest)
    common(sub.add_parser("all", help="fetch + summarize + digest")).set_defaults(func=cmd_all)

    p = sub.add_parser("show", help="终端快速看一眼")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_show)

    args = ap.parse_args()
    if args.login:
        cmd_login(args)
        return
    if not args.cmd:
        ap.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
