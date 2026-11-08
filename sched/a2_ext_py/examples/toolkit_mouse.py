"""Drive the mouse: move, click, drag, scroll - for GUI tests.

Two platforms, one tool:

* **Windows** - SendInput, so the events look like a real mouse to the window
  under test (a program that watches for real input sees real input).
* **Linux** - a virtual device in the kernel (/dev/uinput).  On a Wayland
  desktop that is the only way to move the pointer: XTest (X11) stops at the
  XWayland boundary and never reaches a native Wayland program.  The Linux
  part lives in the extension layer (`a2_ext_py/desktop_linux.py`).

Only the standard library is needed: the Win32 calls through ctypes on
Windows, ctypes for /dev/uinput on Linux.

The coordinates are the pixels of a screenshot (the physical pixels of the
desktop), so a click on a pixel of the picture lands on that pixel.
"""

import os
import time

if os.name == "nt":
    import ctypes
    import ctypes.wintypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    # The screen metrics are in *logical* pixels unless the process says it is
    # DPI aware, while SendInput and GetCursorPos work in physical pixels. Without
    # this the mouse lands in the wrong place on a scaled display.
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        except Exception:
            pass

    INPUT_MOUSE = 0
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP = 0x0040
    MOUSEEVENTF_WHEEL = 0x0800
    MOUSEEVENTF_HWHEEL = 0x1000
    MOUSEEVENTF_ABSOLUTE = 0x8000
    MOUSEEVENTF_VIRTUALDESK = 0x4000
    WHEEL_DELTA = 120

    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]

    def _send(*inputs):
        if not inputs:
            return
        array = (INPUT * len(inputs))(*inputs)
        sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
        if sent != len(inputs):
            raise RuntimeError("SendInput delivered %d of %d events (error %d)"
                               % (sent, len(inputs), ctypes.get_last_error()))

    def _mouse_event(flags, dx=0, dy=0, data=0):
        item = INPUT()
        item.type = INPUT_MOUSE
        item.union.mi = MOUSEINPUT(dx, dy, data, flags, 0, None)
        return item

    def _virtual_screen():
        x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if w <= 0 or h <= 0:
            w = user32.GetSystemMetrics(0)
            h = user32.GetSystemMetrics(1)
            x = y = 0
        return x, y, w, h

    def _position():
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            raise RuntimeError("GetCursorPos failed")
        return point.x, point.y

    def _absolute(x, y):
        """Screen pixels -> the normalized coordinates SendInput wants."""
        left, top, width, height = _virtual_screen()
        span_x = max(1, width - 1)
        span_y = max(1, height - 1)
        nx = int(round((x - left) * 65535.0 / span_x))
        ny = int(round((y - top) * 65535.0 / span_y))
        nx = max(0, min(65535, nx))
        ny = max(0, min(65535, ny))
        return nx, ny

    def _move_to(x, y):
        nx, ny = _absolute(x, y)
        _send(_mouse_event(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny))
        time.sleep(0.02)

    def _click(button="left", clicks=1, hold_ms=40):
        if button == "right":
            down, up = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
        elif button == "middle":
            down, up = MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP
        else:
            down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
        for index in range(max(1, int(clicks))):
            _send(_mouse_event(down))
            time.sleep(max(0, hold_ms) / 1000.0)
            _send(_mouse_event(up))
            if index + 1 < max(1, int(clicks)):
                time.sleep(0.06)

    def _mouse_windows(action="position", x=-1, y=-1, button="left", clicks=1,
                      to_x=-1, to_y=-1, steps=20, dx=0, dy=0, hold_ms=40,
                      image_width=0):
        current_x, current_y = _position()
        left, top, screen_width, _height = _virtual_screen()
        factor = 1.0
        if image_width and screen_width:
            # The model clicked on a picture that was scaled down - the
            # coordinates are the pixels of that picture.
            factor = screen_width / float(image_width)

        if action == "position":
            # The picture starts at the left upper corner of the desktop, so
            # the position is given the way the picture counts.
            return "the mouse is at %d,%d" % (current_x - left, current_y - top)

        if action == "move":
            target_x = current_x if x < 0 else int(x)
            target_y = current_y if y < 0 else int(y)
            target_x = left + int(target_x * factor)
            target_y = top + int(target_y * factor)
            _move_to(target_x, target_y)
            return "moved the mouse to %d,%d" % (target_x, target_y)

        if action in ("click", "double_click", "right_click", "middle_click"):
            target_x = current_x if x < 0 else int(x)
            target_y = current_y if y < 0 else int(y)
            target_x = left + int(target_x * factor)
            target_y = top + int(target_y * factor)
            _move_to(target_x, target_y)
            if action == "double_click":
                button, clicks = "left", 2
            elif action == "right_click":
                button = "right"
            elif action == "middle_click":
                button = "middle"
            _click(button, clicks, hold_ms)
            return ("clicked %s at %d,%d" % (button, target_x, target_y))

        if action == "drag":
            start_x = current_x if x < 0 else int(x)
            start_y = current_y if y < 0 else int(y)
            end_x = start_x if to_x < 0 else int(to_x)
            end_y = start_y if to_y < 0 else int(to_y)
            start_x = left + int(start_x * factor)
            start_y = top + int(start_y * factor)
            end_x = left + int(end_x * factor)
            end_y = top + int(end_y * factor)
            _move_to(start_x, start_y)
            _send(_mouse_event(MOUSEEVENTF_LEFTDOWN))
            time.sleep(0.05)
            count = max(1, int(steps))
            for index in range(1, count + 1):
                part = index / float(count)
                _move_to(int(start_x + (end_x - start_x) * part),
                         int(start_y + (end_y - start_y) * part))
                time.sleep(0.01)
            time.sleep(max(0, hold_ms) / 1000.0)
            _send(_mouse_event(MOUSEEVENTF_LEFTUP))
            return "dragged from %d,%d to %d,%d in %d steps" % (start_x, start_y, end_x, end_y, count)

        if action == "scroll":
            vertical = int(dy)
            horizontal = int(dx)
            if vertical:
                _send(_mouse_event(MOUSEEVENTF_WHEEL, data=vertical * WHEEL_DELTA))
            if horizontal:
                _send(_mouse_event(MOUSEEVENTF_HWHEEL, data=horizontal * WHEEL_DELTA))
            time.sleep(0.05)
            return "scrolled by %d (vertical) and %d (horizontal) at %d,%d" % (
                vertical, horizontal, current_x, current_y)

        raise ValueError("unknown action %r; use position, move, click, double_click, "
                         "right_click, middle_click, drag or scroll" % action)


if os.name == "nt":
    toolkit_mouse = _mouse_windows
else:
    try:
        from a2_ext_py import desktop_linux as _linux
    except ImportError as exc:      # the extension layer is not on the path
        _linux = None
        _linux_error = str(exc)

    def toolkit_mouse(action="position", x=-1, y=-1, button="left", clicks=1,
                      to_x=-1, to_y=-1, steps=20, dx=0, dy=0, hold_ms=40,
                      image_width=0):
        """Move, click, drag or scroll the mouse.

        action: position | move | click | double_click | right_click |
                middle_click | drag | scroll
        x, y: where. -1 means "where the pointer already is".
        button: left | right | middle (for action=click).
        clicks: how many clicks (action=click).
        to_x, to_y: where the drag ends.
        steps: how many moves a drag is made of (slower = more reliable).
        dx, dy: how far to scroll (notches, positive = up / right).
        hold_ms: how long a button is held down.
        image_width: the width of the picture you looked at - when the
                     screenshot was scaled down, the coordinates are the pixels
                     of that picture and the tool scales them up.
        """
        if _linux is None:
            raise RuntimeError("the Linux desktop helper is missing (%s); it is "
                               "part of the extension layer (a2_ext_py/desktop_linux.py)"
                               % _linux_error)
        return _linux.mouse(action=action, x=x, y=y, button=button, clicks=clicks,
                            to_x=to_x, to_y=to_y, steps=steps, dx=dx, dy=dy,
                            hold_ms=hold_ms, image_width=image_width)


def meta_toolkit_mouse():
    return {
        "name": "toolkit_mouse",
        "description": ("Drive the mouse on this machine: move it, click, "
                        "double-click, right-click, drag or scroll. For GUI "
                        "tests: click a button, drag a slider, scroll a list - "
                        "and check the result with toolkit_screenshot. The "
                        "coordinates are the pixels of the picture "
                        "toolkit_screenshot took (the physical pixels of the "
                        "screen)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "description": "position | move | click | double_click | "
                                          "right_click | middle_click | drag | scroll",
                           "enum": ["position", "move", "click", "double_click",
                                    "right_click", "middle_click", "drag", "scroll"]},
                "x": {"type": "integer", "description": "the x coordinate (-1 = keep it)"},
                "y": {"type": "integer", "description": "the y coordinate (-1 = keep it)"},
                "button": {"type": "string", "description": "left | right | middle"},
                "clicks": {"type": "integer", "description": "how many clicks"},
                "to_x": {"type": "integer", "description": "where a drag ends"},
                "to_y": {"type": "integer", "description": "where a drag ends"},
                "steps": {"type": "integer", "description": "how many moves a drag is made of"},
                "dx": {"type": "integer", "description": "scroll: notches to the right (or left)"},
                "dy": {"type": "integer", "description": "scroll: notches up (or down)"},
                "hold_ms": {"type": "integer", "description": "how long a button is held"},
                "image_width": {"type": "integer",
                                "description": "the width of the picture you looked "
                                               "at; the coordinates are then the "
                                               "pixels of that picture"},
            },
            "required": ["action"],
        },
    }
