# -*- coding: utf-8 -*-
"""知乎登录态 → 内容抓取 → LLM 摘要 → markdown digest。

和 sched/bili.py 一个路子：个人工具，纯 HTTP + cookie，不依赖 tea_agent 二进制，
只共用 ~/.tea_agent/provider.yaml 里的 provider 配置。

    python3 sched/zhihu.py whoami                       # 账号信息 + 登录态有效期
    python3 sched/zhihu.py hot --limit 20               # 热榜
    python3 sched/zhihu.py search "大模型" --limit 10    # 搜索（综合/内容/用户）
    python3 sched/zhihu.py collections                  # 我的收藏夹
    python3 sched/zhihu.py collection 737068447         # 某个收藏夹里的东西
    python3 sched/zhihu.py fetch --collection 737068447 # 抓语料
    python3 sched/zhihu.py summarize                    # LLM 结构化摘要
    python3 sched/zhihu.py digest                       # markdown digest
    python3 sched/zhihu.py all --collection 737068447   # 一条龙

登录态来自一个 Playwright storageState 文件（cookie 明文，跨 OS 通用）：
  $ZHIHU_STATE  >  ~/.tea_agent/zhihu_state.json  >  /mnt/c/Users/Hetin/Documents/pw_zhihu.json

知乎接口现状（2026-10 实测，别照着老博客改）：
  * 能用（带 cookie 就行，不需要签名）：
      GET /api/v4/me                              账号信息
      GET /api/v4/search_v3                       搜索（general/content/people）
      GET /api/v4/answers/{id}                    回答正文（HTML）
      GET /api/v4/questions/{id}/answers          某问题下的回答列表
      GET /api/v4/people/{token}/collections      收藏夹列表
      GET /api/v4/collections/{id}/items          收藏夹内容
      GET /api/v3/moments/{token}/activities      自己的动态
      GET api.zhihu.com/topstory/hot-lists/total  热榜
      GET /api/v4/creators/rank/hot               创作热榜
      GET https://zhuanlan.zhihu.com/api/articles/{id}   文章正文（老接口，还在）
  * 不能用（知乎上了 zse-ck 签名 / 风控）：
      GET /api/v4/questions/{id}                  403 请求参数异常
      GET www.zhihu.com/question/{id}             403 风控页（连浏览器都拦）
      收藏夹以外的一些 xxx 也要 include 参数才放行
  * 写操作（发回答/文章/想法）没在这里默认开启，见文末「发布」一节。
"""
import argparse
import html as htmllib
import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("需要 requests：pip3 install requests")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import bili                     # 复用 provider 选择 / LLM 调用 / JSON 抽取
except Exception as exc:            # pragma: no cover
    sys.exit("导入 sched/bili.py 失败（%s），LLM 相关子命令不可用" % exc)

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".tea_agent", "zhihu")
CORPUS = os.path.join(STATE_DIR, "corpus.json")
SUMMARIES = os.path.join(STATE_DIR, "summaries.json")
DIGEST = os.path.join(STATE_DIR, "digest.md")

STATE_CANDIDATES = [
    os.environ.get("ZHIHU_STATE", ""),
    os.path.join(HOME, ".tea_agent", "zhihu_state.json"),
    "/mnt/c/Users/Hetin/Documents/pw_zhihu.json",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 写接口要带的 csrf（= _xsrf cookie）
TOKEN = os.environ.get("ZHIHU_TOKEN", "")


# --------------------------------------------------------------------------
# 登录态
# --------------------------------------------------------------------------
def load_cookies():
    for path in STATE_CANDIDATES:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                st = json.load(fh)
            jar = [(c["name"], c["value"], c["domain"]) for c in st.get("cookies", [])
                   if c["domain"].endswith("zhihu.com")]
            print("[zhihu] 登录态: %s (%d 个 cookie)" % (path, len(jar)))
            return jar
    raise SystemExit("找不到登录态文件。放到 %s，或用 ZHIHU_STATE=/path 指定。"
                     % STATE_CANDIDATES[-1])


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Referer": "https://www.zhihu.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "x-requested-with": "fetch",
    })
    for name, value, domain in load_cookies():
        s.cookies.set(name, value, domain=domain, path="/")
        if name == "_xsrf" and not TOKEN:
            s.headers["x-xsrftoken"] = value
    return s


def api(sess, url, tries=3, host="www.zhihu.com"):
    """调知乎 API，返回解析后的 json；失败重试。"""
    last = None
    for i in range(tries):
        try:
            r = sess.get(url, timeout=25)
            if r.status_code == 200:
                return r.json()
            last = "http %s: %s" % (r.status_code, r.text[:160])
        except Exception as exc:
            last = repr(exc)
        time.sleep(1.0 + i)
    raise RuntimeError("接口失败 %s: %s" % (url, last))


# --------------------------------------------------------------------------
# HTML → 纯文本
# --------------------------------------------------------------------------
_BLOCK = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_BR = re.compile(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</tr>|</blockquote>")
_TAG = re.compile(r"(?s)<[^>]+>")


def html_to_text(raw):
    if not raw:
        return ""
    t = _BLOCK.sub(" ", raw)
    t = _BR.sub("\n", t)
    t = _TAG.sub("", t)
    t = htmllib.unescape(t)
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    t = re.sub(r"\n\s*\n\s*\n+", "\n\n", t)
    return t.strip()


def clean_title(raw):
    """知乎标题里带 <em> 高亮标签。"""
    if not raw:
        return ""
    return html_to_text(raw).strip()


# --------------------------------------------------------------------------
# 各个接口
# --------------------------------------------------------------------------
def get_me(sess):
    inc = ("email,account_status,is_bind_phone,available_message_types,"
           "default_notifications_count,followees_count,follower_count,"
           "votes_count,articles_count,answer_count,question_count,"
           "collections_count,gender,headline,url_token,name")
    return api(sess, "https://www.zhihu.com/api/v4/me?include=" + inc)


def get_hot(sess, limit=30):
    j = api(sess, "https://api.zhihu.com/topstory/hot-lists/total"
                   "?limit=%d&reverse_order=0" % min(50, max(1, limit)),
             host="api.zhihu.com")
    return j.get("data") or []


def search(sess, query, stype="general", limit=10, offset=0):
    url = ("https://www.zhihu.com/api/v4/search_v3?t=%s&q=%s&correction=1"
           "&offset=%d&limit=%d" % (stype, requests.utils.quote(query), offset, limit))
    j = api(sess, url)
    return j.get("data") or []


def get_answer(sess, aid):
    inc = ("content,excerpt,voteup_count,comment_count,favorites_count,created_time,"
           "updated_time,question,author,url,thumbnail_info")
    return api(sess, "https://www.zhihu.com/api/v4/answers/%s?include=%s" % (aid, inc))


def get_question_answers(sess, qid, limit=10, offset=0):
    url = ("https://www.zhihu.com/api/v4/questions/%s/answers?limit=%d&offset=%d"
           "&sort_by=default" % (qid, limit, offset))
    j = api(sess, url)
    return j.get("data") or []


def get_article(sess, aid):
    """文章正文走专栏的老接口，v4 那个要签名会被 403。"""
    return api(sess, "https://zhuanlan.zhihu.com/api/articles/%s" % aid,
               host="zhuanlan.zhihu.com")


def get_collections(sess, token):
    url = ("https://www.zhihu.com/api/v4/people/%s/collections"
           "?include=data[*].updated_time,answer_count,follower_count,creator,"
           "is_favorited,item_count&limit=20" % token)
    j = api(sess, url)
    return j.get("data") or []


def get_collection_items(sess, cid, limit=20, offset=0):
    url = ("https://www.zhihu.com/api/v4/collections/%s/items?limit=%d&offset=%d"
           % (cid, limit, offset))
    j = api(sess, url)
    out = []
    for it in (j.get("data") or []):
        # 收藏夹条目把真正的内容包在 item["content"] 里
        c = it.get("content")
        if isinstance(c, dict):
            c = dict(c)
            c.setdefault("created", it.get("created"))
            out.append(c)
        else:
            out.append(it)
    return out


def get_invited(sess, limit=20, offset=0):
    """「等你来答」：知乎主动邀请你回答的问题列表。"""
    url = ("https://www.zhihu.com/api/v4/notifications/v3/timeline/entry/invite"
           "?invite_with_time_slice=1&limit=%d&offset=%d&invite_domain_score_ab=1"
           % (limit, offset))
    j = api(sess, url)
    return j.get("data") or []


def get_moments(sess, token, limit=20):
    url = ("https://www.zhihu.com/api/v3/moments/%s/activities?limit=%d&desktop=true"
           % (token, limit))
    j = api(sess, url)
    return j.get("data") or []


# --------------------------------------------------------------------------
# 归一化成 corpus 条目
# --------------------------------------------------------------------------
def entry_from_answer(a, source=""):
    q = a.get("question") or {}
    author = a.get("author") or {}
    title = clean_title(q.get("name") or q.get("title") or a.get("title") or "")
    text = html_to_text(a.get("content") or "")
    return {
        "id": "answer:%s" % a.get("id"),
        "kind": "answer",
        "title": title,
        "url": a.get("url") or ("https://www.zhihu.com/question/%s/answer/%s"
                                % (q.get("id"), a.get("id"))),
        "question_id": q.get("id"),
        "author": author.get("name"),
        "author_id": author.get("url_token") or author.get("id"),
        "created": a.get("created_time") or a.get("created"),
        "voteup": a.get("voteup_count"),
        "comment_count": a.get("comment_count"),
        "fav_count": a.get("favorites_count"),
        "text": text,
        "source": source,
    }


def entry_from_article(a, source=""):
    author = a.get("author") or {}
    url = a.get("url") or ""
    if "api.zhihu.com/articles/" in url:
        url = "https://zhuanlan.zhihu.com/p/" + url.rsplit("/", 1)[-1]
    return {
        "id": "article:%s" % a.get("id"),
        "kind": "article",
        "title": clean_title(a.get("title") or ""),
        "url": url or "https://zhuanlan.zhihu.com/p/%s" % a.get("id"),
        "author": author.get("name"),
        "author_id": author.get("url_token") or author.get("id"),
        "created": a.get("created_time") or a.get("created"),
        "voteup": a.get("voteup_count"),
        "comment_count": a.get("comment_count"),
        "fav_count": a.get("favorites_count"),
        "text": html_to_text(a.get("content") or ""),
        "source": source,
    }


def entry_from_generic(o, source=""):
    """收藏夹里可能有视频(zvideo)之类，统统按「标题+简介」收下来。"""
    t = o.get("type") or "item"
    text = html_to_text(o.get("description") or o.get("excerpt")
                        or o.get("content") or "")
    if not text:
        return None
    author = o.get("author") or {}
    return {
        "id": "%s:%s" % (t, o.get("id")),
        "kind": t,
        "title": clean_title(o.get("title") or ""),
        "url": o.get("url") or "",
        "author": author.get("name"),
        "author_id": author.get("url_token") or author.get("id"),
        "created": o.get("created") or o.get("published_at") or o.get("created_at"),
        "voteup": o.get("voteup_count") or o.get("liked_count"),
        "comment_count": o.get("comment_count"),
        "fav_count": None,
        "text": text,
        "source": source,
    }


def entry_from_search_object(o, source=""):
    """搜索结果里的 object → corpus 条目（能拿到的先落下来）。"""
    t = o.get("type")
    if t == "answer" and o.get("id"):
        q = o.get("question") or {}
        e = entry_from_answer({
            "id": o.get("id"),
            "question": {"id": q.get("id"), "name": clean_title(q.get("name"))},
            "author": o.get("author") or {},
            "content": o.get("content") or o.get("excerpt") or "",
            "url": ("https://www.zhihu.com/question/%s/answer/%s"
                    % (q.get("id"), o.get("id"))),
            "created_time": o.get("created_time"),
            "voteup_count": o.get("voteup_count"),
            "comment_count": o.get("comment_count"),
            "favorites_count": o.get("favorites_count"),
        }, source)
        return e
    if t == "article" and o.get("id"):
        return entry_from_article({
            "id": o.get("id"),
            "title": o.get("title"),
            "content": o.get("content") or o.get("excerpt") or "",
            "author": o.get("author") or {},
            "url": o.get("url"),
            "created_time": o.get("created_time"),
            "voteup_count": o.get("voteup_count"),
            "comment_count": o.get("comment_count"),
        }, source)
    return None


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------
def load_corpus():
    if os.path.exists(CORPUS):
        with open(CORPUS, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_corpus(corpus):
    os.makedirs(STATE_DIR, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    with open(CORPUS, "w", encoding="utf-8") as fh:
        json.dump(corpus, fh, ensure_ascii=False, indent=1)


def cmd_whoami(args):
    sess = make_session()
    me = get_me(sess)
    print("账号：%s (%s)" % (me.get("name"), me.get("url_token")))
    for k in ("headline", "gender", "follower_count", "following_count",
              "answer_count", "articles_count", "question_count",
              "collections_count", "default_notifications_count",
              "is_realname", "account_status"):
        if me.get(k) is not None:
            print("  %-26s %s" % (k, me.get(k)))
    # 登录态有效期：z_c0 是真正的长期登录票据
    for c in sess.cookies:
        if c.name == "z_c0":
            print("  z_c0（登录票据）      %s…" % (c.value or "")[:12])
    print("  目录：%s" % STATE_DIR)


def cmd_hot(args):
    sess = make_session()
    items = get_hot(sess, args.limit)
    for i, it in enumerate(items[:args.limit], 1):
        tgt = it.get("target") or {}
        title = clean_title(tgt.get("title") or "")
        url = (tgt.get("url") or "").replace("https://api.zhihu.com/questions/",
                                             "https://www.zhihu.com/question/")
        print("%2d. [%s] %s" % (i, tgt.get("type"), title))
        print("    回答 %s ｜ 关注 %s ｜ %s" % (tgt.get("answer_count"),
                                              tgt.get("follower_count"), url))


def cmd_search(args):
    sess = make_session()
    data = search(sess, args.query, args.type, args.limit)
    print("[zhihu] 搜索「%s」(%s) → %d 条" % (args.query, args.type, len(data)))
    for i, it in enumerate(data, 1):
        o = it.get("object") or {}
        t = o.get("type")
        title = clean_title(o.get("title") or (o.get("question") or {}).get("name") or "")
        print("%2d. [%s] %s" % (i, t, title))
        if t == "answer":
            q = o.get("question") or {}
            print("    id=%s 赞%s 评论%s ｜ %s" % (
                o.get("id"), o.get("voteup_count"), o.get("comment_count"),
                "https://www.zhihu.com/question/%s/answer/%s" % (q.get("id"), o.get("id"))))
        elif t == "article":
            print("    id=%s 赞%s ｜ https://zhuanlan.zhihu.com/p/%s" % (
                o.get("id"), o.get("voteup_count"), o.get("id")))
        else:
            print("    id=%s" % o.get("id"))


def cmd_collections(args):
    sess = make_session()
    me = get_me(sess)
    cols = get_collections(sess, me.get("url_token"))
    print("[zhihu] 收藏夹 %d 个" % len(cols))
    for c in cols:
        print("  id=%-12s %-20s 条目 %-4s 公开 %s" % (
            c.get("id"), c.get("title"), c.get("item_count"), c.get("is_public")))


def cmd_collection(args):
    sess = make_session()
    items = get_collection_items(sess, args.id, args.limit, args.offset)
    print("[zhihu] 收藏夹 %s → %d 条" % (args.id, len(items)))
    for i, it in enumerate(items, 1):
        print("%2d. [%s] %s" % (i, it.get("type"), clean_title(it.get("title") or "")))
        print("     id=%s ｜ %s" % (it.get("id"), it.get("url") or ""))


def cmd_moments(args):
    """自己的动态（想法/回答/文章时间线）。"""
    sess = make_session()
    me = get_me(sess)
    token = me.get("url_token")
    data = get_moments(sess, token, args.limit)
    print("[zhihu] %s 的动态 → %d 条" % (token, len(data)))
    for i, it in enumerate(data, 1):
        tgt = it.get("target") or {}
        q = tgt.get("question") or {}
        title = clean_title(tgt.get("title") or q.get("title") or "")
        txt = clean_title(tgt.get("excerpt") or tgt.get("detail") or "")
        url = tgt.get("url") or ""
        if "api.zhihu.com/answers/" in url:
            url = ("https://www.zhihu.com/question/%s/answer/%s"
                   % (q.get("id"), url.rsplit("/", 1)[-1]))
        elif "api.zhihu.com/articles/" in url:
            url = "https://zhuanlan.zhihu.com/p/" + url.rsplit("/", 1)[-1]
        print("%2d. [%s] %s" % (i, tgt.get("type"), title[:80]))
        if txt:
            print("     %s" % txt.replace("\n", " ")[:110])
        print("     %s ｜ %s" % (url, fmt_time(it.get("created_time"))))


def cmd_invited(args):
    """等你来答 / 被邀请回答的问题。"""
    sess = make_session()
    items = get_invited(sess, args.limit)
    print("[zhihu] 等你来答 %d 条" % len(items))
    for i, it in enumerate(items, 1):
        t = it.get("target") or {}
        print("%2d. %s" % (i, (t.get("title") or "")[:72]))
        print("     id=%s 回答%s ｜ %s" % (
            t.get("id"), t.get("answer_count"),
            (t.get("url") or "").replace("https://api.zhihu.com/questions/",
                                         "https://www.zhihu.com/question/")))


def cmd_fetch(args):
    sess = make_session()
    corpus = load_corpus()
    got = 0
    new = []

    def add(entry):
        nonlocal got
        if not entry or not entry.get("text"):
            return
        if entry["id"] in corpus and not args.refresh:
            return
        corpus[entry["id"]] = entry
        got += 1
        new.append(entry)

    # 1) 搜索
    for q in args.query or []:
        # t=content 现在返回空，用 general
        stype = "general" if args.type in ("general", "content") else args.type
        data = search(sess, q, stype, args.limit)
        print("[fetch] 搜索「%s」→ %d 条" % (q, len(data)))
        for it in data:
            add(entry_from_search_object(it.get("object") or {}, source="search:" + q))
            time.sleep(0.3)

    # 2) 问题下的回答
    for qid in args.question or []:
        ans = get_question_answers(sess, qid, args.limit)
        print("[fetch] 问题 %s → %d 个回答" % (qid, len(ans)))
        for a in ans:
            add(entry_from_answer(a, source="question:" + qid))
            time.sleep(0.3)

    # 3) 指定回答
    for aid in args.answer or []:
        try:
            add(entry_from_answer(get_answer(sess, aid), source="answer:" + aid))
        except Exception as exc:
            print("  回答 %s 失败: %s" % (aid, exc))

    # 4) 指定文章
    for aid in args.article or []:
        try:
            add(entry_from_article(get_article(sess, aid), source="article:" + aid))
        except Exception as exc:
            print("  文章 %s 失败: %s" % (aid, exc))

    # 5) 收藏夹（单页最多 20 条，超了要翻页）
    for cid in args.collection or []:
        items = []
        offset = 0
        while len(items) < args.limit:
            page = get_collection_items(sess, cid, 20, offset)
            if not page:
                break
            items.extend(page)
            offset += 20
            if len(items) >= args.limit:
                break
        items = items[:args.limit]
        print("[fetch] 收藏夹 %s → %d 条" % (cid, len(items)))
        for it in items:
            t = it.get("type")
            if t == "answer":
                if not (it.get("question") or {}).get("name") and it.get("id"):
                    # 收藏夹条目不带所属问题，去回答详情里补标题
                    try:
                        detail = get_answer(sess, it["id"])
                        it = dict(it)
                        it["question"] = detail.get("question") or {}
                    except Exception:
                        pass
                add(entry_from_answer(it, source="collection:" + cid))
            elif t == "article":
                add(entry_from_article(it, source="collection:" + cid))
            else:
                add(entry_from_generic(it, source="collection:" + cid))
            time.sleep(0.3)

    # 6) 热榜（取前 N 个问题，每个问题抓前几个回答）
    if args.hot:
        hot = get_hot(sess, args.hot)
        print("[fetch] 热榜 %d 条" % len(hot))
        for it in hot:
            tgt = it.get("target") or {}
            if tgt.get("type") != "question" or not tgt.get("id"):
                continue
            qid = tgt["id"]
            try:
                ans = get_question_answers(sess, qid, args.hot_answers)
            except Exception as exc:
                print("  问题 %s 失败: %s" % (qid, exc))
                continue
            for a in ans:
                e = entry_from_answer(a, source="hot")
                e["hot_title"] = clean_title(tgt.get("title") or "")
                add(e)
                time.sleep(0.3)

    save_corpus(corpus)
    print("[fetch] 语料 %d 条（新增 %d）→ %s" % (len(corpus), got, CORPUS))
    for e in new[:10]:
        print("  + [%s] %s" % (e["kind"], (e.get("title") or "")[:56]))


# --------------------------------------------------------------------------
# LLM
# --------------------------------------------------------------------------
SUMMARY_PROMPT = """你是一个知乎内容整理助手。下面给你一条知乎内容（回答或文章）的标题、作者和正文。

请只依据正文内容总结，不要编造正文里没有的信息。用 JSON 回答，不要加任何解释文字：

{
  "summary": "一到两句话概括这条内容讲了什么（中文）",
  "key_points": ["要点1", "要点2", "要点3"],
  "quotes": ["正文里最有信息量的一句原话（可以少于3条，没有就留空数组）"],
  "topics": ["主题标签1", "主题标签2"],
  "category": "AI大模型|技术实现|行业商业|职场生活|社会话题|学习考试|情感关系|其他"
}"""

OVERVIEW_PROMPT = """下面是你收集的若干条知乎内容摘要。请写一份综述。

用 JSON 回答，不要加解释文字：

{
  "headline": "一句话概括这些内容",
  "themes": [{"theme": "主题", "detail": "这个主题下的内容有什么共同点"}],
  "notable": ["值得注意的具体结论/事实"],
  "suggestions": ["基于这些内容，你可能还想看/该关注的方向"]
}"""


def cmd_summarize(args):
    corpus = load_corpus()
    if not corpus:
        raise SystemExit("没有语料，先跑 fetch")
    summaries = {}
    if os.path.exists(SUMMARIES):
        with open(SUMMARIES, "r", encoding="utf-8") as fh:
            summaries = json.load(fh)

    pname, prov, model = bili.pick_provider(args.provider, args.model)
    print("[llm] provider=%s model=%s" % (pname, model))

    todo = [k for k in corpus
            if args.refresh or not summaries.get(k, {}).get("summary")]
    todo = todo[:args.limit]
    print("[llm] 待处理 %d 条（已有 %d 条摘要）" % (len(todo), len(summaries)))
    if not todo:
        return

    for n, key in enumerate(todo, 1):
        meta = corpus[key]
        text = meta.get("text") or ""
        if len(text) > args.max_chars:
            text = text[:args.max_chars] + "\n…（正文过长已截断）"
        head = "类型：%s\n标题：%s\n作者：%s\n来源：%s\n\n正文：\n" % (
            meta.get("kind"), meta.get("title"), meta.get("author"),
            meta.get("source"))
        print("  [%d/%d] %s" % (n, len(todo), (meta.get("title") or "")[:34]))
        try:
            raw = bili.llm_chat(prov, model,
                                [{"role": "system", "content": SUMMARY_PROMPT},
                                 {"role": "user", "content": head + text}])
            obj = bili.extract_json(raw)
        except Exception as exc:
            print("      失败: %s" % exc)
            continue
        obj.update({"id": key, "title": meta.get("title"), "kind": meta.get("kind"),
                    "author": meta.get("author"), "url": meta.get("url"),
                    "created": meta.get("created"), "voteup": meta.get("voteup"),
                    "model": model, "at": int(time.time())})
        summaries[key] = obj
        print("      %s" % (obj.get("summary") or "")[:76])

    os.makedirs(STATE_DIR, exist_ok=True)
    with open(SUMMARIES, "w", encoding="utf-8") as fh:
        json.dump(summaries, fh, ensure_ascii=False, indent=1)
    print("[llm] 摘要 → %s（%d 条）" % (SUMMARIES, len(summaries)))


# --------------------------------------------------------------------------
# digest
# --------------------------------------------------------------------------
CAT_ORDER = ["AI大模型", "技术实现", "行业商业", "职场生活", "社会话题",
             "学习考试", "情感关系", "其他"]


def load_summaries():
    if not os.path.exists(SUMMARIES):
        raise SystemExit("没有摘要，先跑 summarize")
    with open(SUMMARIES, "r", encoding="utf-8") as fh:
        return json.load(fh)


def fmt_time(ts):
    if not ts:
        return ""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:
        return ""


def cmd_digest(args):
    summaries = load_summaries()
    items = [s for s in summaries.values() if s.get("summary")]
    items.sort(key=lambda s: s.get("created") or 0, reverse=True)
    if args.limit:
        items = items[:args.limit]

    groups = {}
    for s in items:
        groups.setdefault(s.get("category") or "其他", []).append(s)

    out = ["# 知乎内容提炼\n",
           "> 生成于 %s ｜ 共 %d 条\n" % (time.strftime("%Y-%m-%d %H:%M"), len(items))]

    if args.overview and items:
        pname, prov, model = bili.pick_provider(args.provider, args.model)
        print("[llm] 生成综述 (%s/%s) …" % (pname, model))
        blob = "\n\n".join(
            "#%d %s（作者：%s）\n%s\n要点：%s" %
            (i + 1, s.get("title"), s.get("author"), s.get("summary"),
             "；".join(s.get("key_points") or []))
            for i, s in enumerate(items))
        ov = None
        for attempt in range(2):
            try:
                ov = bili.extract_json(bili.llm_chat(
                    prov, model,
                    [{"role": "system", "content": OVERVIEW_PROMPT},
                     {"role": "user", "content": blob}], max_tokens=2000))
                break
            except Exception as exc:
                print("[llm] 综述失败(第%d次): %s" % (attempt + 1, exc))
                ov = None
        if ov:
            out.append("## 一句话\n")
            out.append("%s\n" % ov.get("headline", ""))
            for th in ov.get("themes") or []:
                out.append("- **%s**：%s" % (th.get("theme"), th.get("detail")))
            if ov.get("notable"):
                out.append("\n### 值得记住的点\n")
                for n in ov["notable"]:
                    out.append("- %s" % n)
            if ov.get("suggestions"):
                out.append("\n### 可能还想看\n")
                for n in ov["suggestions"]:
                    out.append("- %s" % n)
            out.append("\n---\n")

    out.append("## 分组浏览\n")
    for cat in CAT_ORDER + [c for c in groups if c not in CAT_ORDER]:
        if cat not in groups:
            continue
        out.append("### %s（%d）\n" % (cat, len(groups[cat])))
        for s in groups[cat]:
            out.append("**%s**  \n%s ｜ 作者：%s ｜ %s ｜ 赞 %s\n" % (
                s.get("title"), fmt_time(s.get("created")), s.get("author"),
                "、".join(s.get("topics") or []), s.get("voteup")))
            out.append("%s\n" % s.get("summary"))
            for kp in s.get("key_points") or []:
                out.append("- %s" % kp)
            for q in s.get("quotes") or []:
                out.append("  > %s" % q)
            out.append("- 链接：%s\n" % (s.get("url") or ""))

    text = "\n".join(out)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("[digest] → %s" % args.out)
    else:
        print(text)


def cmd_show(args):
    summaries = load_summaries()
    items = sorted(summaries.values(), key=lambda s: s.get("created") or 0, reverse=True)
    for s in items[:args.limit]:
        print("%s  %s" % (fmt_time(s.get("created")), (s.get("title") or "")[:44]))
        print("    %s" % (s.get("summary") or "")[:110])


def cmd_all(args):
    cmd_fetch(args)
    cmd_summarize(args)
    args.out = args.out or DIGEST
    args.overview = True
    cmd_digest(args)
    print("\n[digest] %s" % args.out)


# --------------------------------------------------------------------------
# 发布（写操作，默认只打印 payload，不真发）
# --------------------------------------------------------------------------
def md_to_html(text):
    """把「空行分段 + **加粗**」的纯文本转成知乎编辑器认的 HTML。"""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    out = []
    for b in blocks:
        # 先转义，再把 **加粗** 换成 <strong>（顺序反了会把标签也转义掉）
        b = b.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        b = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", b)
        out.append("<p>%s</p>" % b.replace("\n", "<br/>"))
    return "".join(out)


# 知乎网页端发回答的真实流程（2026-10 抓包确认）：
#   1) POST /api/v4/read_history/add          {"content_token": qid, "content_type": "question"}
#   2) POST /api/v4/questions/{qid}/draft      正文 + settings（编辑器自动存草稿）
#   3) POST /api/v4/content/publish           action=answer + 一大坨 extra_info/pc_business_params
# 直接 POST /api/v4/questions/{qid}/answers 会返回 422（那条老路已经废了）。
PUBLISH_INCLUDE = (
    "is_contain_ai_content,is_visible,paid_info,paid_info_content,has_column,"
    "admin_closed_comment,reward_info,annotation_action,annotation_detail,"
    "collapse_reason,is_normal,is_sticky,collapsed_by,suggest_edit,comment_count,"
    "thanks_count,favlists_count,can_comment,content,editable_content,"
    "voteup_count,reshipment_settings,comment_permission,created_time,"
    "updated_time,review_info,relevant_info,question,excerpt,attachment,"
    "content_source,is_labeled,endorsements,reaction_instruction,reaction,"
    "ip_info,relationship.is_authorized,voting,is_thanked,is_author,"
    "is_nothelp,is_favorited;author.vip_info,kvip_info,badge[*].topics;"
    "settings.table_of_content.enabled")

DRAFT_SETTINGS = {
    "reshipment_settings": "allowed",
    "columns": None,
    "comment_permission": "all",
    "can_reward": False,
    "tagline": "",
    "disclaimer_status": "close",
    "disclaimer_type": "none",
    "commercial_report_info": {"is_report": False},
    "table_of_contents_enabled": False,
    "thank_inviter_status": "close",
    "thank_inviter": "",
}


def _business_params():
    p = dict(DRAFT_SETTINGS)
    p["reward_setting"] = {"can_reward": False, "tagline": ""}
    p["commercial_zhitask_bind_info"] = None
    p["is_report"] = False
    p["push_activity"] = True
    return json.dumps(p, ensure_ascii=False)


def publish_answer(sess, qid, html):
    """按网页端的流程发一条回答，返回 (answer_id, 原始响应)。"""
    # 1) 记一笔阅读历史（网页端都会发，缺了也可能被拒）
    try:
        sess.post("https://www.zhihu.com/api/v4/read_history/add",
                  json={"content_token": str(qid), "content_type": "question"},
                  timeout=15)
    except Exception:
        pass
    # 2) 存草稿
    draft = {"content": html, "delta_time": 2, "draft_type": "normal",
             "settings": DRAFT_SETTINGS}
    r = sess.post("https://www.zhihu.com/api/v4/questions/%s/draft" % qid,
                  json=draft, timeout=30)
    if r.status_code != 200:
        raise RuntimeError("存草稿失败 http %s: %s" % (r.status_code, r.text[:200]))
    # 3) 发布
    payload = {
        "action": "answer",
        "data": {
            "publish": {"traceId": "%d,%s" % (int(time.time() * 1000),
                                             "00000000-0000-0000-0000-000000000000")},
            "hybridInfo": {},
            "draft": {"isPublished": False, "disabled": 1},
            "extra_info": {"question_id": str(qid), "publisher": "pc",
                           "include": PUBLISH_INCLUDE},
            "pc_business_params": _business_params(),
            "hybrid": {"html": html, "textLength": len(html)},
            "reprint": {"reshipment_settings": "allowed"},
            "commentsPermission": {"comment_permission": "all"},
            "appreciate": {"can_reward": False, "tagline": ""},
            "publishSwitch": {"draft_type": "normal"},
            "creationStatement": {"disclaimer_status": "close",
                                  "disclaimer_type": "none"},
            "commercialReportInfo": {"isReport": 0},
            "toFollower": {},
            "contentsTables": {"table_of_contents_enabled": False},
            "thanksInvitation": {"thank_inviter_status": "close",
                                  "thank_inviter": ""},
        },
    }
    r = sess.post("https://www.zhihu.com/api/v4/content/publish",
                  json=payload, timeout=40)
    if r.status_code != 200:
        raise RuntimeError("发布失败 http %s: %s" % (r.status_code, r.text[:300]))
    j = r.json()
    data = j.get("data") or {}
    aid = data.get("id") or data.get("answer_id")
    if not aid:
        # 网页端返回的是 {"data": {"publish_type": ..., "result": "<json 字符串>"}}
        raw = data.get("result")
        if isinstance(raw, str):
            try:
                inner = json.loads(raw)
                aid = inner.get("id") or (inner.get("answer") or {}).get("id")
                j = {"data": inner}
            except Exception:
                pass
    return aid, j


def delete_answer(sess, aid):
    r = sess.delete("https://www.zhihu.com/api/v4/answers/%s" % aid, timeout=25)
    return r.status_code, r.text[:200]


def delete_draft(sess, qid):
    r = sess.delete("https://www.zhihu.com/api/v4/questions/%s/draft" % qid,
                    timeout=25)
    return r.status_code, r.text[:200]


def cmd_post(args):
    """发回答 / 发文章。

    写接口默认只打印要发的东西（dry-run），真发要显式加 --yes。
    内容从文件读，避免命令行里带大段正文。
    """
    sess = make_session()
    with open(args.file, "r", encoding="utf-8") as fh:
        body = fh.read()
    html = md_to_html(body)
    if args.kind == "answer":
        if not args.question:
            raise SystemExit("发回答要带 --question <问题 id>")
        sess.headers["Referer"] = "https://www.zhihu.com/question/%s" % args.question
    sess.headers["Origin"] = "https://www.zhihu.com"
    sess.headers["Content-Type"] = "application/json"
    print("[post] %s ｜ 问题 %s ｜ 正文 %d 字（%d 段）" % (
        args.kind, args.question, len(body), html.count("<p>")))
    if not args.yes:
        print("[post] 这是 dry-run，加 --yes 才真发。")
        print("[post] HTML 前 200 字：%s" % html[:200])
        return
    if args.kind == "answer":
        aid, raw = publish_answer(sess, args.question, html)
        print("[post] 发布完成 answer_id=%s" % aid)
        print("[post] 原始返回：%s" % json.dumps(raw, ensure_ascii=False)[:300])
        if aid:
            print("[post] 链接：https://www.zhihu.com/question/%s/answer/%s"
                  % (args.question, aid))
    else:
        raise SystemExit("发文章暂未实现（文章走的是另一套 content/publish 参数）")


def cmd_rm(args):
    sess = make_session()
    if args.answer:
        print("[rm] 删回答 %s → %s" % (args.answer, delete_answer(sess, args.answer)))
    if args.draft:
        print("[rm] 删草稿(问题 %s) → %s" % (args.draft, delete_draft(sess, args.draft)))
    if not args.answer and not args.draft:
        raise SystemExit("要带 --answer 或 --draft")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="知乎内容抓取 + LLM 提炼")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="账号信息").set_defaults(func=cmd_whoami)

    p = sub.add_parser("hot", help="热榜")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_hot)

    p = sub.add_parser("search", help="搜索")
    p.add_argument("query")
    p.add_argument("--type", default="general",
                   choices=["general", "content", "people", "column", "topic"])
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("invited", help="等你来答（邀请回答的问题）")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_invited)

    sub.add_parser("collections", help="我的收藏夹").set_defaults(func=cmd_collections)

    p = sub.add_parser("moments", help="自己的动态")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_moments)

    p = sub.add_parser("collection", help="收藏夹内容")
    p.add_argument("id")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_collection)

    def common(p):
        p.add_argument("--query", action="append", help="搜索关键词（可重复）")
        p.add_argument("--question", action="append", help="问题 id（可重复）")
        p.add_argument("--answer", action="append", help="回答 id（可重复）")
        p.add_argument("--article", action="append", help="文章 id（可重复）")
        p.add_argument("--collection", action="append", help="收藏夹 id（可重复）")
        p.add_argument("--hot", type=int, default=0, help="抓热榜前 N 个问题")
        p.add_argument("--hot-answers", type=int, default=3, help="每个问题抓几个回答")
        p.add_argument("--type", default="general", help="搜索类型 general/people/column")
        p.add_argument("--limit", type=int, default=20)
        p.add_argument("--refresh", action="store_true")
        p.add_argument("--provider", default=None)
        p.add_argument("--model", default=None)
        p.add_argument("--max-chars", type=int, default=24000)
        p.add_argument("--out", default=None)
        p.add_argument("--overview", action="store_true")
        return p

    common(sub.add_parser("fetch", help="抓语料")).set_defaults(func=cmd_fetch)
    common(sub.add_parser("summarize", help="LLM 摘要")).set_defaults(func=cmd_summarize)
    common(sub.add_parser("digest", help="markdown digest")).set_defaults(func=cmd_digest)
    common(sub.add_parser("all", help="fetch + summarize + digest")).set_defaults(func=cmd_all)

    p = sub.add_parser("show", help="终端看一眼")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("rm", help="删掉自己发的回答 / 草稿")
    p.add_argument("--answer", help="要删的回答 id")
    p.add_argument("--draft", help="要删的草稿所属问题 id")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("post", help="发回答/文章（默认 dry-run）")
    p.add_argument("kind", choices=["answer", "article"])
    p.add_argument("--file", required=True, help="正文文件（markdown/纯文本）")
    p.add_argument("--question", help="回答要发到哪个问题 id")
    p.add_argument("--title", help="文章标题")
    p.add_argument("--yes", action="store_true", help="真发（否则只打印）")
    p.set_defaults(func=cmd_post)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
