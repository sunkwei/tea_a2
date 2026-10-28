#!/usr/bin/env python3
"""post llm: read what was read lately, let the model write a new piece.

The zhihu browser history is gone (the API answers 404 and the page is a 404
page too), so "what was read lately" comes from the collections and the local
corpus. What is pinned here:

  * the pieces are collected, de-duplicated and ordered by time;
  * the body of a collection entry is fetched (the entry itself has only an
    excerpt);
  * the model is asked twice - once to analyse, once to write - and its JSON
    is what the tool reads;
  * the publish payload is the one the web editor sends (action "article",
    the body in data.hybrid.html, the title in data.title.title, the draft id
    in data.draft.id);
  * without --yes nothing is published.

No network here: the zhihu session and the model are fakes.

Usage: python3 tests/smoke_zhihu_llm_post.py
"""

import json
import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "sched"))

import zhihu  # noqa: E402

failures = []
checks = []


def check(name, ok, detail=""):
    checks.append(name)
    print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)) if not ok else ""))
    if not ok:
        failures.append(name)


class FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.text = json.dumps(data, ensure_ascii=False) if data is not None else ""

    def json(self):
        return self._data


class FakeSession:
    """A session that answers the two write calls and records them."""

    def __init__(self):
        self.headers = {}
        self.posts = []
        self.draft_id = "2098817807966381909"

    def get(self, url, **kwargs):
        return FakeResponse({})

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs.get("json")))
        if url.endswith("/api/articles/drafts"):
            return FakeResponse({"id": self.draft_id, "title": "t"})
        if url.endswith("/api/v4/content/publish"):
            return FakeResponse({"data": {"id": "2099999999999999999"}})
        return FakeResponse({})


class FakeBili:
    """The LLM: it answers with the JSON the prompts ask for."""

    def __init__(self):
        self.calls = []

    def pick_provider(self, name=None, model=None):
        return "fake", {"api_key": "x"}, "fake-model"

    def llm_chat(self, prov, model, messages, **kwargs):
        self.calls.append(messages)
        system = messages[0]["content"]
        if "文学的编辑" in system:
            return json.dumps({
                "themes": ["尺度"], "mother_theme": "人在宇宙里的位置",
                "motifs": ["星舰"], "angles": ["a", "b", "c"],
                "materials": ["曲速"], "voice": "冷峻",
            }, ensure_ascii=False)
        return json.dumps({
            "title": "沉默的季末",
            "body": "第一段。\n\n第二段。\n\n第三段。",
        }, ensure_ascii=False)

    def extract_json(self, text):
        return json.loads(text)


def install_fakes(monkey, sess, bili, collections, corpus, answers):
    """Point zhihu's network functions at the fakes."""
    monkey["get_me"] = lambda s: {"url_token": "sunkw-19", "name": "sunkw"}
    monkey["get_collections"] = lambda s, token: collections
    monkey["get_collection_items"] = lambda s, cid, limit=20, offset=0: cid_items[cid]
    monkey["get_answer"] = lambda s, aid: answers.get(aid, {})
    monkey["get_article"] = lambda s, aid: answers.get(aid, {})
    monkey["load_corpus"] = lambda: corpus
    monkey["make_session"] = lambda: sess
    monkey["_bili"] = lambda: bili


cid_items = {}


def main():
    global cid_items
    sess = FakeSession()
    bili = FakeBili()

    collections = [
        {"id": "c1", "title": "我的收藏", "item_count": 3},
        {"id": "c2", "title": "技术", "item_count": 1},
    ]
    cid_items = {
        "c1": [
            {"type": "answer", "id": "a1", "created": 300,
             "title": "旧的收藏", "excerpt": "摘要一",
             "url": "https://www.zhihu.com/question/1/answer/a1"},
            {"type": "article", "id": "p1", "created": 200,
             "title": "较新的收藏", "excerpt": "摘要二",
             "url": "https://zhuanlan.zhihu.com/p/p1"},
            {"type": "answer", "id": "a2", "created": 100,
             "title": "重复的那条", "excerpt": "摘要三",
             "url": "https://www.zhihu.com/question/1/answer/a2"},
        ],
        "c2": [
            {"type": "article", "id": "p2", "created": 250,
             "title": "技术收藏", "excerpt": "摘要四",
             "url": "https://zhuanlan.zhihu.com/p/p2"},
        ],
    }
    corpus = {
        "answer:old": {"type": "answer", "id": "old", "created": 500,
                       "title": "语料里最新的", "text": "语料正文",
                       "url": "https://www.zhihu.com/question/9/answer/old"},
        "answer:a2": {"type": "answer", "id": "a2", "created": 100,
                      "title": "重复的那条", "text": "语料正文",
                      "url": "https://www.zhihu.com/question/1/answer/a2"},
    }
    answers = {
        "a1": {"content": "<p>回答一正文</p>",
               "question": {"name": "问题一"}},
        "a2": {"content": "<p>回答二正文</p>", "question": {"name": "问题二"}},
        "p1": {"content": "<p>文章一正文</p>", "title": "专栏文章一"},
        "p2": {"content": "<p>文章二正文</p>", "title": "专栏文章二"},
    }

    monkey = {}
    install_fakes(monkey, sess, bili, collections, corpus, answers)
    saved = {name: getattr(zhihu, name) for name in monkey}
    for name, fn in monkey.items():
        setattr(zhihu, name, fn)

    try:
        # ---- 1. what was read lately --------------------------------
        items = zhihu.recent_reading(sess, limit=10)
        check("the pieces are collected from the collections and the corpus",
              len(items) == 5, [i.get("id") for i in items])
        check("they are ordered by time, newest first",
              [i.get("_at") for i in items] == sorted([i.get("_at") for i in items],
                                                    reverse=True),
              [i.get("_at") for i in items])
        ids = [i.get("id") for i in items]
        check("the same piece is not counted twice",
              len(ids) == len(set(ids)), ids)
        check("the body of a collection entry is fetched",
              any(i.get("text") == "回答一正文" for i in items),
              [i.get("text") for i in items])
        check("a missing body does not stop the collection",
              len(items) == 5)

        few = zhihu.recent_reading(sess, limit=2)
        check("--count is honoured", len(few) == 2, len(few))

        # ---- 2. the model is asked twice -----------------------------
        analysis = zhihu.analyze_reading(bili, {"api_key": "x"}, "fake-model", items)
        check("the analysis is read out of the model's JSON",
              analysis.get("mother_theme") == "人在宇宙里的位置", analysis)
        article = zhihu.compose_article(bili, {"api_key": "x"}, "fake-model",
                                        items, analysis)
        check("the title comes from the model", article.get("title") == "沉默的季末",
              article)
        check("the body comes from the model", "第二段" in (article.get("body") or ""),
              article)
        check("the model was asked twice", len(bili.calls) == 2, len(bili.calls))
        check("the writing step sees the analysis",
              "人在宇宙里的位置" in bili.calls[1][1]["content"])

        # ---- 3. the publish payload ---------------------------------
        aid, raw = zhihu.publish_article(sess, "沉默的季末", "<p>第一段。</p><p>第二段。</p>")
        check("a draft is created first",
              sess.posts and sess.posts[0][0].endswith("/api/articles/drafts"),
              sess.posts[0][0] if sess.posts else None)
        check("the draft carries the title and the body",
              sess.posts[0][1].get("title") == "沉默的季末"
              and "<p>第一段。</p>" in (sess.posts[0][1].get("content") or ""),
              sess.posts[0][1])
        publish = [p for p in sess.posts if p[0].endswith("/api/v4/content/publish")]
        check("the publish call is made", len(publish) == 1, len(publish))
        body = publish[0][1] if publish else {}
        check("the action is 'article'", body.get("action") == "article",
              body.get("action"))
        data = body.get("data") or {}
        check("the body travels in data.hybrid.html",
              data.get("hybrid", {}).get("html") == "<p>第一段。</p><p>第二段。</p>",
              data.get("hybrid"))
        check("the title travels in data.title.title",
              data.get("title", {}).get("title") == "沉默的季末", data.get("title"))
        check("the draft id travels in data.draft.id",
              data.get("draft", {}).get("id") == sess.draft_id, data.get("draft"))
        biz = (data.get("extra_info") or {}).get("pc_business_params")
        try:
            biz = json.loads(biz)
        except Exception:
            biz = {}
        check("pc_business_params is a json string with the body and the title",
              biz.get("content") == "<p>第一段。</p><p>第二段。</p>"
              and biz.get("title") == "沉默的季末", biz)

        # ---- 4. without --yes nothing is published -------------------
        class Args:
            kind = "llm"
            count = 5
            provider = None
            model = None
            out = None
            max_chars = 24000
            yes = False
            file = None
            question = None
            title = None

        out_dir = tempfile.mkdtemp(prefix="zhihu-llm-")
        args = Args()
        args.out = os.path.join(out_dir, "draft.md")
        before = len(sess.posts)
        zhihu.cmd_post_llm(args, sess)
        check("a dry-run does not publish", len(sess.posts) == before,
              [p[0] for p in sess.posts[before:]])
        check("the piece is written to a file",
              os.path.exists(args.out) and "沉默的季末" in open(args.out, encoding="utf-8").read(),
              args.out)

        args.yes = True
        zhihu.cmd_post_llm(args, sess)
        check("--yes publishes it",
              any(p[0].endswith("/api/v4/content/publish") for p in sess.posts[before:]),
              [p[0] for p in sess.posts[before:]])
    finally:
        for name, fn in saved.items():
            setattr(zhihu, name, fn)

    print("")
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
