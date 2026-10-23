#!/usr/bin/env python3
"""The task order: a text file somebody fills in, the queue reads.

A task order is a plain text file with "key: value" lines:

    title: 把 README 的构建例子跑一遍
    prompt: |
      把 tea_a2 的 README 里的构建例子真的跑一遍，
      遇到问题就记下来。
    workspace: /home/sunkw/work/git/tea_a2
    allow_paths:
      - /tmp
    not_before: +5

The format is deliberately small - a line, an indented continuation, a list,
a nested block of key/value pairs. It is NOT yaml (the standard library has no
yaml parser, and the queue runs on devices where nothing can be installed).

Every key the queue's API understands has a line here, so the order can carry
any setting a task can have. A key nobody knows is an error with the line
number: a typo in a task order must not silently drop a setting.

This module is used by the queue (--submit-order) and by the WeChat bot (a
file named *.order that the user sends).
"""

import re

# The keys the format knows. Anything else is an error.
STRING_KEYS = ("title", "prompt", "workspace", "provider", "model",
               "not_before", "idempotency_key", "business_id")
LIST_KEYS = ("allow_paths", "tags")
INT_KEYS = ("priority",)
DICT_KEYS = ("meta",)

KNOWN_KEYS = set(STRING_KEYS) | set(LIST_KEYS) | set(INT_KEYS) | set(DICT_KEYS)

KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")


class OrderError(Exception):
    """The task order cannot be read. The message says which line is wrong."""


def _split_list(value):
    return [part.strip() for part in value.split(",") if part.strip()]


def _strip_comment(value):
    """A trailing comment on a value: "0   # 越大越先做" -> "0".

    Only a '#' that follows a space starts a comment, and only on the line
    itself - the body of a block (the prompt) is never touched.
    """
    mark = value.find(" #")
    if mark < 0:
        return value.strip()
    return value[:mark].strip()


def parse_order(text):
    """Read a task order. Returns the body for `POST /api/tasks`.

    Raises OrderError with a message that names the line.
    """
    inline = {}          # key -> what was written after the colon
    blocks = {}          # key -> the indented lines that belong to it
    order = []           # the keys, in the order they appeared
    errors = []
    current = None
    seen = set()

    for number, raw in enumerate((text or "").splitlines(), 1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] not in (" ", "\t"):
            match = KEY_RE.match(line.strip())
            if not match:
                errors.append("第 %d 行：看不懂这一行：%r" % (number, line.strip()[:40]))
                current = None
                continue
            key, value = match.group(1), match.group(2).strip()
            if key not in KNOWN_KEYS:
                errors.append("第 %d 行：不认识的键 %r（认识的键：%s）"
                              % (number, key, "、".join(sorted(KNOWN_KEYS))))
            if key in seen:
                errors.append("第 %d 行：%r 写了两次" % (number, key))
            seen.add(key)
            order.append(key)
            inline[key] = value
            blocks[key] = []
            current = key
            continue

        # An indented line belongs to the key above it.
        if current is None:
            errors.append("第 %d 行：这一行缩进了，但它属于上面哪个键？" % number)
            continue
        blocks[current].append(line.strip())

    if errors:
        raise OrderError("\n".join(errors))

    body = {}
    meta = {}
    for key in order:
        first = _strip_comment(inline.get(key) or "")
        rest = blocks.get(key) or []
        if key in LIST_KEYS:
            items = []
            if first and first not in ("|", ">"):
                items.extend(_split_list(first))
            for line in rest:
                if line.startswith("- "):
                    items.append(line[2:].strip())
                elif line:
                    items.extend(_split_list(line))
            body[key] = [item for item in items if item]
        elif key in DICT_KEYS:
            values = {}
            for line in ([first] if first and first not in ("|", ">") else []) + rest:
                match = KEY_RE.match(line)
                if not match:
                    raise OrderError("第 %d 行：%s 下面要写「键: 值」，这一行看不懂：%r"
                                     % (0, key, line[:40]))
                values[match.group(1)] = match.group(2).strip()
            body[key] = values
        elif key in INT_KEYS:
            raw = first.strip()
            if not raw:
                raise OrderError("%s 要一个数字" % key)
            try:
                body[key] = int(raw)
            except ValueError:
                raise OrderError("%s 要一个数字，写的是 %r" % (key, raw[:20]))
        else:
            # A string: the value, then the indented lines (the body).
            if first in ("|", ">"):
                body[key] = "\n".join(rest).strip()
            elif rest:
                body[key] = "\n".join([first] + rest).strip()
            else:
                body[key] = first

    # The workspace may be a path; the queue resolves it to an id.
    workspace = body.pop("workspace", "")
    if workspace:
        body["workspace_id"] = workspace
    # The tags and the business id are business metadata.
    tags = body.pop("tags", [])
    business_id = body.pop("business_id", "")
    if tags:
        meta.setdefault("tags", tags)
    if business_id:
        meta.setdefault("business_id", business_id)
    if meta:
        merged = dict(body.get("meta") or {})
        merged.update(meta)
        body["meta"] = merged
    return body
