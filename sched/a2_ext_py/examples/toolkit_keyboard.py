"""toolkit_keyboard -- type on this machine, the way a person would.

Everything goes through SendInput, so the events look like a real keyboard to
the window that has the focus (a program that watches for real input sees real
input).

Text is sent as Unicode characters (KEYEVENTF_UNICODE), not as virtual-key
presses: that way Chinese, accents, emoji and the characters that need a
Shift on one keyboard layout but not on another all arrive unchanged. A named
key (Enter, Tab, Esc ...) and a hotkey (Ctrl+V, Alt+F4) are keyboard *events*
instead - those are what the window's accelerators look at.

Only the standard library is needed: the Win32 calls through ctypes.

Note: the text goes to whatever window has the focus. Use toolkit_mouse (or
AppActivate) to give the right window the focus first, and take a
toolkit_screenshot afterwards to see what actually happened.
"""

import ctypes
import ctypes.wintypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

# The virtual-key codes of the keys that have a name a human would use.
NAMED_KEYS = {
    "backspace": 0x08, "back": 0x08,
    "tab": 0x09,
    "enter": 0x0D, "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "menu": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "spacebar": 0x20,
    "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C, "prtsc": 0x2C,
    "insert": 0x2D, "ins": 0x2D,
    "delete": 0x2E, "del": 0x2E,
    "win": 0x5B, "lwin": 0x5B,
    "rwin": 0x5C,
    "apps": 0x5D,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "semicolon": 0xBA, "equals": 0xBB, "comma": 0xBC,
    "minus": 0xBD, "period": 0xBE, "slash": 0xBF,
    "backtick": 0xC0, "grave": 0xC0,
    "lbracket": 0xDB, "backslash": 0xDC, "rbracket": 0xDD, "quote": 0xDE,
}

# Keys that are physically on the extended part of the keyboard: without the
# flag, the arrow keys and friends arrive as their numeric-keypad twins.
EXTENDED = {"pageup", "pgup", "pagedown", "pgdn", "end", "home", "left", "up",
            "right", "down", "insert", "ins", "delete", "del", "printscreen",
            "prtsc", "rwin", "apps", "numlock"}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
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


def _key_event(vk, scan, flags):
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.union.ki = KEYBDINPUT(vk, scan, flags, 0, None)
    return item


def _resolve_key(name):
    key = (name or "").strip().lower()
    if key in NAMED_KEYS:
        return NAMED_KEYS[key], key in EXTENDED
    if len(key) == 1:
        # A single character used as a key name: a letter or a digit.
        upper = key.upper()
        if "A" <= upper <= "Z" or "0" <= upper <= "9":
            return ord(upper), False
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return 0x70 + int(key[1:]) - 1, False
    raise ValueError("unknown key %r; use a name from %s, or a single character"
                     % (name, ", ".join(sorted(NAMED_KEYS))))


def _tap_key(name, hold_ms=0, times=1):
    vk, extended = _resolve_key(name)
    flags = KEYEVENTF_EXTENDEDKEY if extended else 0
    for _ in range(max(1, int(times))):
        _send(_key_event(vk, 0, flags))
        if hold_ms:
            time.sleep(max(0, hold_ms) / 1000.0)
        else:
            time.sleep(0.01)
        _send(_key_event(vk, 0, flags | KEYEVENTF_KEYUP))
        time.sleep(0.015)


def _type_text(text, delay_ms=8):
    """Send text as Unicode characters - one pair of events per code unit."""
    if not text:
        return 0
    for char in text:
        data = char.encode("utf-16-le")
        units = [int.from_bytes(data[i:i + 2], "little")
                 for i in range(0, len(data), 2)]
        for unit in units:
            _send(_key_event(0, unit, KEYEVENTF_UNICODE))
            _send(_key_event(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        if delay_ms:
            # A small gap: some apps (and the IMEs behind them) drop keys that
            # arrive faster than they can process them.
            time.sleep(max(0, delay_ms) / 1000.0)
    return len(text)


def toolkit_keyboard(action="type", text="", key="", keys=None, delay_ms=8,
                     hold_ms=0, times=1):
    """Type text, press a key, or press a combination.

    action: type | key | hotkey
    text:   what action=type types (any Unicode text, including Chinese).
    key:    what action=key presses (enter, tab, esc, up, f5, delete, "a" ...).
    keys:   action=hotkey: a list such as ["ctrl", "v"] or a string "ctrl+v".
    delay_ms: pause between the characters of action=type (a slow IME needs a
              bigger one).
    hold_ms: how long a key of action=key is held down.
    times:  how many times action=key is pressed.

    The keys go to the window that has the focus right now: bring it to the
    front first (toolkit_mouse), and take a toolkit_screenshot afterwards to
    confirm what happened.
    """
    action = (action or "type").strip().lower()

    if action == "type":
        if not text:
            raise ValueError("action=type needs text")
        count = _type_text(text, delay_ms)
        return "typed %d character(s): %s" % (count, text)

    if action == "key":
        if not key:
            raise ValueError("action=key needs key (enter, tab, esc, f5, a ...)")
        _tap_key(key, hold_ms, times)
        return "pressed %s%s" % (key, "" if int(times) == 1 else " %d times" % int(times))

    if action == "hotkey":
        sequence = keys
        if isinstance(sequence, str):
            sequence = [part for part in sequence.replace("+", " ").split() if part]
        if not sequence:
            raise ValueError("action=hotkey needs keys, e.g. [\"ctrl\", \"v\"]")
        resolved = [_resolve_key(part) for part in sequence]
        # Every key down in order, then every key up in reverse: that is what a
        # human does with a chord.
        for vk, extended in resolved:
            _send(_key_event(vk, 0, KEYEVENTF_EXTENDEDKEY if extended else 0))
            time.sleep(0.012)
        time.sleep(0.02)
        for vk, extended in reversed(resolved):
            flags = KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if extended else 0)
            _send(_key_event(vk, 0, flags))
            time.sleep(0.012)
        return "pressed %s" % "+".join(str(part) for part in sequence)

    raise ValueError("unknown action %r; use type, key or hotkey" % action)


def meta_toolkit_keyboard():
    return {
        "name": "toolkit_keyboard",
        "description": ("Type on this machine: text (any Unicode, including "
                        "Chinese), one named key (enter, tab, esc, up, f5 ...) "
                        "or a combination (ctrl+v, alt+f4). The keys go to the "
                        "window that has the focus - bring it forward with "
                        "toolkit_mouse first, then take a toolkit_screenshot to "
                        "check the result."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "description": "type | key | hotkey",
                           "enum": ["type", "key", "hotkey"]},
                "text": {"type": "string",
                         "description": "action=type: the text to type"},
                "key": {"type": "string",
                        "description": "action=key: enter, tab, esc, backspace, "
                                       "delete, up, down, left, right, home, end, "
                                       "pageup, pagedown, space, f1..f24, a..z, 0..9"},
                "keys": {"type": "array", "items": {"type": "string"},
                         "description": "action=hotkey: e.g. [\"ctrl\", \"v\"]"},
                "delay_ms": {"type": "integer",
                             "description": "pause between characters (default 8; "
                                            "a slow IME needs 30-50)"},
                "hold_ms": {"type": "integer", "description": "how long a key is held"},
                "times": {"type": "integer", "description": "how many times action=key presses"},
            },
            "required": ["action"],
        },
    }
