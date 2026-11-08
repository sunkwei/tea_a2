#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smoke test for the three desktop tools of the extension layer.

The extension layer (`sched/a2_ext_py/`) can grow a tool with one .py file.
The three tools a GUI test needs are the example modules: a screenshot, the
mouse and the keyboard.  They are cross platform (Windows: Win32; Linux:
uinput and the compositor's own tools), and this test pins:

  * the three modules load and their schemas are what the model sees;
  * a module may answer with a picture - the server turns that into an
    image block, so the model *sees* the screen;
  * the Linux helpers: the size of the screen, the picture (a real PNG with
    the right size), the region and the scaling, and the key codes;
  * with --live: the real thing - the pointer moves and the keys arrive
    (the keys are typed into whatever has the focus, so that part is opt in).

Usage: python3 tests/smoke_ext_desktop.py [--live]
"""

import os
import os.path as osp
import shutil
import struct
import sys
import tempfile

REPO_ROOT = osp.dirname(osp.dirname(osp.abspath(__file__)))
sys.path.insert(0, osp.join(REPO_ROOT, "sched"))

from a2_ext_py.toolkit.loader import Toolkit  # noqa: E402
from a2_ext_py.toolkit.mcp_server import Server  # noqa: E402

EXAMPLES = osp.join(REPO_ROOT, "sched", "a2_ext_py", "examples")
IS_LINUX = os.name == "posix" and osp.exists("/dev/uinput")

checks = []
failures = []


def check(name, ok, detail=""):
    checks.append(name)
    if not ok:
        failures.append("%s (%s)" % (name, detail))
    print("%s %s%s" % ("ok  " if ok else "FAIL", name,
                       "" if ok else "  <- " + str(detail)[:400]))


def png_header(path):
    """(width, height) of a PNG, from its header."""
    with open(path, "rb") as handle:
        head = handle.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", head[16:24])


def main():
    live = "--live" in sys.argv[1:]

    # ---- the modules load --------------------------------------------------
    toolkit = Toolkit(EXAMPLES)
    result = toolkit.reload()
    check("the three example modules load", not result["invalid"], result["invalid"])
    names = sorted(result["valid"])
    check("the screenshot, the mouse and the keyboard are there",
          names == ["toolkit_keyboard", "toolkit_mouse", "toolkit_screenshot"], names)

    for name in names:
        schema = result["valid"][name]
        check("%s has a name and a description" % name,
              schema.get("name") == name and len(schema.get("description") or "") > 20,
              schema)
        check("%s has an input schema" % name,
              schema.get("inputSchema", {}).get("type") == "object", schema)

    # ---- a module may hand a picture to the model --------------------------
    with tempfile.TemporaryDirectory() as modules:
        picture = _tiny_png()
        with open(osp.join(modules, "toolkit_picture.py"), "w", encoding="utf-8") as handle:
            handle.write(
                "def toolkit_picture():\n"
                "    \"\"\"A picture.\"\"\"\n"
                "    return {\"text\": \"here it is\",\n"
                "            \"image\": %r}\n"
                "\n"
                "\n"
                "def meta_toolkit_picture():\n"
                "    return {\"name\": \"toolkit_picture\",\n"
                "            \"description\": \"A picture.\",\n"
                "            \"inputSchema\": {\"type\": \"object\", \"properties\": {}}}\n"
                % picture)
        server = Server(module_dir=modules)
        answer = server.call("toolkit_picture", {})
        os.remove(picture)
        content = answer.get("content") or []
        kinds = [block.get("type") for block in content]
        check("a module that answers with a picture gets an image block",
              kinds == ["text", "image"], kinds)
        check("the text comes first", str((content[0] or {}).get("text") or "").startswith("here"),
              content[0] if content else None)
        image = content[1] if len(content) > 1 else {}
        check("the image block carries the picture",
              image.get("mimeType") == "image/png" and image.get("data"), image.get("mimeType"))

    # ---- the Linux helpers -------------------------------------------------
    if IS_LINUX:
        from a2_ext_py import desktop_linux as linux

        width, height = linux.screen_size()
        check("the size of the screen is known", width > 0 and height > 0, (width, height))

        # The picture: cut a region out of it and shrink it.
        with tempfile.TemporaryDirectory() as work:
            shot = osp.join(work, "shot.png")
            try:
                answer = linux.screenshot(path=shot)
            except Exception as exc:                       # noqa: BLE001
                answer = {"text": str(exc)}
            if osp.exists(shot):
                size = png_header(shot)
                check("the screenshot is a PNG", size is not None, shot)
                check("the screenshot has the size of the screen",
                      size == (width, height), (size, (width, height)))
                check("the screenshot comes with the text and the picture",
                      isinstance(answer, dict) and answer.get("image") == shot, answer)
                # The region and the scaling.
                region = osp.join(work, "region.png")
                linux.screenshot(path=region, region="10,20,200,100")
                check("a region is cut out", png_header(region) == (200, 100),
                      png_header(region))
                small = osp.join(work, "small.png")
                linux.screenshot(path=small, max_width=320)
                size = png_header(small)
                check("a small picture keeps the proportion",
                      size and size[0] == 320 and abs(size[1] - int(320 * height / width)) <= 2,
                      size)
            else:
                print("skip  the screenshot (no picture program on this machine)")

        # The keys: the codes are the ones of the layout of this machine.
        check("the letter a has a key code", linux.resolve_key("a") > 0, linux.resolve_key("a"))
        check("enter has a key code", linux.resolve_key("enter") == 28, linux.resolve_key("enter"))
        check("f5 has a key code", linux.resolve_key("f5") == 63, linux.resolve_key("f5"))
        board = linux.Keyboard()
        entry = board.key_for("a")
        check("a letter is on the keyboard", entry is not None, entry)
        check("a Chinese character is not on the keyboard",
              board.key_for("中") is None, board.key_for("中"))
        check("an uppercase letter needs the shift",
              entry is not None and entry[1] is False and
              (board.key_for("A") or (0, True))[1] is True, board.key_for("A"))

        # ---- the live part -------------------------------------------------
        if live:
            # The pointer: moving it to where it already is changes nothing
            # on the screen, but it goes through the whole path.
            answer = linux.mouse(action="move", x=-1, y=-1)
            check("the pointer can be moved", "moved the mouse" in answer, answer)
            answer = linux.mouse(action="position")
            check("the position of the pointer is reported", "the mouse is at" in answer, answer)
            # The keyboard: this types into whatever has the focus.
            answer = linux.keyboard(action="type", text="a")
            check("the keyboard types", "typed" in answer, answer)
        else:
            print("skip  the live part (it types into the window that has the focus; "
                  "run with --live to include it)")

    print("")
    print("%d/%d checks passed" % (len(checks) - len(failures), len(checks)))
    return 1 if failures else 0


def _tiny_png():
    """A one pixel PNG - enough to test the image block."""
    import base64
    import zlib
    raw = b"\x00\xff\x00\x00"          # one row, filter 0, RGB
    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload +
                struct.pack(">I", zlib.crc32(tag + payload) & 0xffffffff))
    blob = (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw)) +
            chunk(b"IEND", b""))
    import tempfile
    handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    handle.write(blob)
    handle.close()
    return handle.name


if __name__ == "__main__":
    sys.exit(main())
