"""The Linux desktop for the extension layer: screen, mouse, keyboard.

The extension layer grows a new tool with one .py file (see
`docs/a2_ext_py.md`).  The three tools a GUI test needs - a screenshot, the
mouse, the keyboard - are the example modules in `examples/`.  On Linux they
need two backends, and which one is used depends on what the machine
actually is:

* **uinput** - a virtual device in the kernel.  The events are *real* input
  events, so every program sees them: the compositor hands them to the
  client that has the focus, Wayland programs included.  This is the only
  way to drive a native Wayland program - XTest (X11) stops at the XWayland
  boundary, so on a Wayland desktop it reaches nothing but the X11 programs.
* **the compositor's own tools** - a screenshot is taken by spectacle (KDE),
  grim (wlroots), gnome-screenshot or maim, whichever is installed.  A
  rootless XWayland server cannot show what is on the screen (it only knows
  the X11 programs), so on Wayland the compositor is the only one who knows.

Only the standard library is used: ctypes for /dev/uinput and libX11, zlib for
the PNG container, subprocess for the tools that are already installed.

Nothing here is a daemon: the virtual device is created when it is needed
and destroyed again right after - the kernel forgets it, the compositor
notices the new device and takes it into account.

The coordinates are **the pixels of a screenshot** (the physical pixels of
the desktop).  A click on a pixel of the picture has to land on that pixel,
so the picture and the pointer have to live in the same space.
"""

import ctypes
import fcntl
import json
import os
import os.path as osp
import re
import shutil
import struct
import subprocess
import time
import zlib

# --- uinput ----------------------------------------------------------------

UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_SET_ABSBIT = 0x40045567

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0, 1, 2, 3
SYN_REPORT = 0
ABS_X, ABS_Y = 0x00, 0x01
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
REL_WHEEL, REL_HSCROLL = 0x08, 0x06

KEY_LEFTSHIFT = 42
KEY_RIGHTCTRL = 97

ABS_CNT = 64
UINPUT_MAX_NAME_SIZE = 80


def _ioctl(fd, request, arg=0):
    fcntl.ioctl(fd, request, arg)


class VirtualDevice:
    """A virtual input device in the kernel (/dev/uinput).

    The device is created when it is needed and destroyed again right after:
    the kernel forgets it, and the compositor notices the new device.
    """

    def __init__(self, name, abs_max=None, keys=(), rels=(), buttons=()):
        try:
            self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            raise RuntimeError(
                "cannot open /dev/uinput (%s). On Linux the virtual device "
                "needs write permission on /dev/uinput." % exc)
        try:
            self._create(name, abs_max or {}, keys, rels, buttons)
        except Exception:
            os.close(self.fd)
            raise

    def _create(self, name, abs_max, keys, rels, buttons):
        # struct uinput_user_dev: name, input_id, ff_effects_max, then the
        # four ABS_CNT long arrays (absmax, absmin, absfuzz, absflat).
        blob = bytearray()
        blob += name.encode("utf-8")[:UINPUT_MAX_NAME_SIZE - 1].ljust(UINPUT_MAX_NAME_SIZE, b"\0")
        blob += struct.pack("HHHH", 0x03, 0x1234, 0x5678, 1)      # BUS_USB and ids
        blob += struct.pack("I", 0)                               # ff_effects_max
        high = [0] * ABS_CNT
        low = [0] * ABS_CNT
        for code, (bottom, top) in abs_max.items():
            low[code] = bottom
            high[code] = top
        blob += struct.pack("%di" % ABS_CNT, *high)
        blob += struct.pack("%di" % ABS_CNT, *low)
        blob += struct.pack("%di" % ABS_CNT, *([0] * ABS_CNT))    # absfuzz
        blob += struct.pack("%di" % ABS_CNT, *([0] * ABS_CNT))    # absflat
        os.write(self.fd, bytes(blob))

        if abs_max:
            _ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
            for code in abs_max:
                _ioctl(self.fd, UI_SET_ABSBIT, code)
        wanted = list(keys) + list(buttons)
        if wanted:
            _ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            for code in wanted:
                _ioctl(self.fd, UI_SET_KEYBIT, code)
        if rels:
            _ioctl(self.fd, UI_SET_EVBIT, EV_REL)
            for code in rels:
                _ioctl(self.fd, UI_SET_RELBIT, code)
        _ioctl(self.fd, UI_DEV_CREATE)
        # The compositor needs a moment to notice the new device - otherwise
        # the first events land nowhere.
        time.sleep(0.2)

    def emit(self, etype, code, value):
        """One input event (struct input_event: time, type, code, value)."""
        now = time.time()
        seconds = int(now)
        micros = int((now - seconds) * 1000000)
        os.write(self.fd, struct.pack("qqHHi", seconds, micros, etype, code, value))

    def sync(self):
        """End the frame - the kernel sends the events to the clients then."""
        self.emit(EV_SYN, SYN_REPORT, 0)

    def close(self):
        try:
            _ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


# --- the screen ------------------------------------------------------------

def _kde_outputs():
    """The outputs of a Plasma session: name, physical size, scale."""
    path = osp.expanduser("~/.config/kwinoutputconfig.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    outputs = []
    for section in data or []:
        if not isinstance(section, dict) or section.get("name") != "outputs":
            continue
        for entry in section.get("data") or []:
            mode = entry.get("mode") or {}
            width = int(mode.get("width") or 0)
            height = int(mode.get("height") or 0)
            if not width or not height:
                continue
            outputs.append({
                "name": entry.get("connectorName") or "",
                "physical": (width, height),
                "scale": float(entry.get("scale") or 1.0),
            })
    return outputs


def _xrandr_outputs():
    """The outputs as the X server sees them: name and logical geometry."""
    if not shutil.which("xrandr"):
        return []
    try:
        done = subprocess.run(["xrandr", "--current"], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    outputs = []
    pattern = re.compile(r"^(\S+) connected.*?(\d+)x(\d+)\+(-?\d+)\+(-?\d+)")
    for line in done.stdout.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        outputs.append({
            "name": match.group(1),
            "geometry": (int(match.group(4)), int(match.group(5)),
                         int(match.group(2)), int(match.group(3))),
        })
    return outputs


def _drm_mode():
    """The biggest mode the graphics card offers (the last resort)."""
    best = None
    if not osp.isdir("/sys/class/drm"):
        return None
    for name in sorted(os.listdir("/sys/class/drm")):
        if not name.startswith("card"):
            continue
        try:
            with open("/sys/class/drm/%s/modes" % name, encoding="utf-8") as handle:
                first = handle.readline().strip()
        except OSError:
            continue
        match = re.match(r"^(\d+)x(\d+)$", first)
        if not match:
            continue
        size = (int(match.group(1)), int(match.group(2)))
        if best is None or size[0] * size[1] > best[0] * best[1]:
            best = size
    return best


def screen_geometry():
    """The desktop: (width, height, scale).

    The width and the height are the pixels of a picture - the physical
    pixels of the desktop.  The scale says how much bigger the picture is
    than the logical desktop (1.0 when nothing is scaled).
    """
    kde = _kde_outputs()
    xrandr = _xrandr_outputs()
    known = dict((out["name"], out) for out in kde)

    if xrandr:
        left = top = 0
        right = bottom = 0
        scale = 1.0
        for out in xrandr:
            x, y, width, height = out["geometry"]
            info = known.get(out["name"])
            factor = info["scale"] if info else 1.0
            scale = max(scale, factor)
            left = min(left, x)
            top = min(top, y)
            right = max(right, x + width)
            bottom = max(bottom, y + height)
        if len(xrandr) == 1:
            info = known.get(xrandr[0]["name"])
            if info:
                # One screen: the mode of the screen is the exact answer.
                return info["physical"][0], info["physical"][1], info["scale"]
        return (int(round((right - left) * scale)),
                int(round((bottom - top) * scale)), scale)

    if kde:
        # No X server (a pure Wayland session): one screen, or several.
        if len(kde) == 1:
            return kde[0]["physical"][0], kde[0]["physical"][1], kde[0]["scale"]
        width = sum(out["physical"][0] for out in kde)
        height = max(out["physical"][1] for out in kde)
        return width, height, 1.0

    mode = _drm_mode()
    if mode:
        return mode[0], mode[1], 1.0
    raise RuntimeError("cannot find out how big the screen is")


def screen_size():
    """The desktop in the pixels of a picture."""
    width, height, _scale = screen_geometry()
    return width, height


# --- the PNG container -----------------------------------------------------

def _png_chunk(tag, payload):
    return (struct.pack(">I", len(payload)) + tag + payload +
            struct.pack(">I", zlib.crc32(tag + payload) & 0xffffffff))


def write_png(path, width, height, rows):
    """Write a PNG. `rows` is a list of byte strings, one per line, RGB."""
    raw = bytearray()
    for row in rows:
        raw.append(0)                      # filter type 0 (none)
        raw += row
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    blob = (b"\x89PNG\r\n\x1a\n" +
            _png_chunk(b"IHDR", header) +
            _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6)) +
            _png_chunk(b"IEND", b""))
    directory = osp.dirname(path)
    if directory and not osp.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(blob)
    return len(blob)


def read_png(path):
    """Read a PNG: (width, height, channels, rows).

    Only what the pictures of this module need: 8 bit, RGB or RGBA, not
    interlaced.  Anything else raises - the caller then keeps the picture as
    it is instead of guessing.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    pos = 8
    parts = []
    width = height = depth = color = 0
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            width, height, depth, color, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", payload)
            if depth != 8 or interlace:
                raise ValueError("unsupported PNG (depth %d, interlace %d)"
                                 % (depth, interlace))
        elif tag == b"IDAT":
            parts.append(payload)
        elif tag == b"IEND":
            break
        pos += 12 + length
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
    if not channels:
        raise ValueError("unsupported PNG colour type %d" % color)
    raw = zlib.decompress(b"".join(parts))
    stride = width * channels
    rows = []
    previous = bytearray(stride)
    pos = 0
    for _row in range(height):
        kind = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if kind == 1:
            for index in range(channels, stride):
                line[index] = (line[index] + line[index - channels]) & 0xFF
        elif kind == 2:
            for index in range(stride):
                line[index] = (line[index] + previous[index]) & 0xFF
        elif kind == 3:
            for index in range(stride):
                left = line[index - channels] if index >= channels else 0
                line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif kind == 4:
            for index in range(stride):
                a = line[index - channels] if index >= channels else 0
                b = previous[index]
                c = previous[index - channels] if index >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                guess = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[index] = (line[index] + guess) & 0xFF
        elif kind != 0:
            raise ValueError("unsupported PNG filter %d" % kind)
        rows.append(bytes(line))
        previous = line
    return width, height, channels, rows


def png_dimensions(path):
    """The size of a PNG, read from its header (None when it cannot be read).

    Only the header - decoding a whole picture in Python takes seconds, and
    the size is all that is needed here.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(33)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def png_size(path):
    """The size of a PNG (None when it cannot be read)."""
    return png_dimensions(path)


def _to_rgb(row, channels):
    """One line of pixels as RGB (the PNG writer writes no other colour)."""
    if channels == 1:
        return bytes(b for value in row for b in (value, value, value))
    if channels == 2:
        return bytes(row[index] for index in range(0, len(row), 2) for _ in range(3))
    out = bytearray(len(row) // channels * 3)
    out[0::3] = row[0::channels]
    out[1::3] = row[1::channels]
    out[2::3] = row[2::channels]
    return bytes(out)


def crop_and_scale(path, region=None, max_width=0):
    """Cut a region out of a PNG and shrink it. Returns (width, height).

    ImageMagick does it when it is installed (fast); otherwise the standard
    library does it (a few seconds for a full screen).
    """
    if not region and not max_width:
        size = png_size(path)
        return size if size else (0, 0)

    if shutil.which("convert"):
        target = path + ".a2.png"
        command = ["convert", path]
        if region:
            x, y, width, height = region
            command += ["-crop", "%dx%d+%d+%d" % (width, height, x, y), "+repage"]
        if max_width:
            command += ["-resize", "%dx" % int(max_width)]
        command += [target]
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=90)
        except (OSError, subprocess.SubprocessError):
            done = None
        if done is not None and done.returncode == 0 and osp.exists(target):
            os.replace(target, path)
            size = png_size(path)
            if size:
                return size
        if osp.exists(target):
            os.remove(target)

    # The standard library: decode, cut, shrink, write again.
    width, height, channels, rows = read_png(path)
    if channels != 3:
        # write_png writes RGB - grey and alpha pictures are converted first.
        rows = [_to_rgb(row, channels) for row in rows]
        channels = 3
    if region:
        x, y, cut_width, cut_height = region
        x = max(0, min(width - 1, int(x)))
        y = max(0, min(height - 1, int(y)))
        cut_width = max(1, min(width - x, int(cut_width)))
        cut_height = max(1, min(height - y, int(cut_height)))
        rows = [row[x * channels:(x + cut_width) * channels]
                for row in rows[y:y + cut_height]]
        width, height = cut_width, cut_height
    if max_width and width > int(max_width):
        factor = max(1, int(round(width / float(max_width))))
        new_width = width // factor
        new_height = max(1, height // factor)
        shrunk = []
        for row in range(new_height):
            source = rows[min(len(rows) - 1, row * factor)]
            line = bytearray()
            for column in range(new_width):
                start = min(max(0, len(source) - channels), column * factor * channels)
                line += source[start:start + channels]
            shrunk.append(bytes(line))
        rows = shrunk
        width, height = new_width, new_height
    write_png(path, width, height, rows)
    return width, height


# --- taking a picture ------------------------------------------------------

_SCREENSHOT_TOOLS = (
    ("spectacle", lambda path: ["spectacle", "-b", "-n", "-f", "-p", "-o", path]),
    ("grim", lambda path: ["grim", path]),
    ("gnome-screenshot", lambda path: ["gnome-screenshot", "-f", path]),
    ("maim", lambda path: ["maim", path]),
)


def screenshot_tool():
    """The program that takes the picture on this machine (None if there is none)."""
    for name, _command in _SCREENSHOT_TOOLS:
        if shutil.which(name):
            return name
    return None


def _parse_region(text, screen):
    """'x,y,width,height' -> the rectangle; empty -> the whole screen."""
    if not text:
        return screen
    parts = [part.strip() for part in str(text).replace(" ", ",").split(",") if part.strip()]
    if len(parts) != 4:
        raise ValueError("region must be 'x,y,width,height'")
    x, y, width, height = (int(float(part)) for part in parts)
    if width <= 0 or height <= 0:
        raise ValueError("the region must have a positive width and height")
    return (x, y, width, height)


def _bgr_to_rgb(row):
    """One line of BGR pixels as RGB (what the X server hands out)."""
    out = bytearray(len(row))
    out[0::3] = row[2::3]
    out[1::3] = row[1::3]
    out[2::3] = row[0::3]
    return bytes(out)


def _x11_capture(width, height):
    """Grab the X11 root window. Returns (width, height, rows in RGB)."""
    lib = _x11_library()
    if lib is None:
        raise RuntimeError("libX11 is not installed")
    display = lib.XOpenDisplay(None)
    if not display:
        raise RuntimeError("cannot open the X display (DISPLAY=%r)"
                           % os.environ.get("DISPLAY", ""))
    try:
        screen = lib.XDefaultScreen(display)
        root = lib.XRootWindow(display, screen)
        image = lib.XGetImage(display, root, 0, 0, width, height, 0xffffffff, 2)
        if not image:
            raise RuntimeError("XGetImage failed")
        try:
            info = image.contents
            if info.bits_per_pixel not in (24, 32):
                raise RuntimeError("the X server gives %d bits per pixel - "
                                   "cannot read the picture" % info.bits_per_pixel)
            raw = ctypes.string_at(info.data, info.bytes_per_line * info.height)
            rows = []
            for row in range(info.height):
                start = row * info.bytes_per_line
                line = raw[start:start + info.width * 3]
                rows.append(_bgr_to_rgb(line))
            return info.width, info.height, rows
        finally:
            lib.XDestroyImage(image)
    finally:
        lib.XCloseDisplay(display)


def _capture(path):
    """Put a picture of the whole screen into `path`. Returns (width, height)."""
    tool = screenshot_tool()
    if tool:
        for name, command in _SCREENSHOT_TOOLS:
            if name != tool:
                continue
            done = subprocess.run(command(path), capture_output=True, text=True, timeout=90)
            if done.returncode != 0 or not osp.exists(path):
                raise RuntimeError("%s failed: %s" % (name, (done.stderr or "").strip()))
            size = png_size(path)
            if size:
                return size
            return (0, 0)
    # No compositor tool: the X server has to show the screen itself.
    width, height = screen_size()
    captured_width, captured_height, rows = _x11_capture(width, height)
    write_png(path, captured_width, captured_height, rows)
    return captured_width, captured_height


def screenshot(path="", region="", max_width=0):
    """Take a screenshot and save it as a PNG.

    Returns the answer for the model: the text *and* the picture, so the
    model sees the screen instead of only being told where the file is.
    """
    screen = screen_size()
    box = None
    if region:
        x, y, width, height = _parse_region(region, screen)
        if x + width > screen[0] or y + height > screen[1]:
            raise ValueError("the region is outside the screen (%dx%d)"
                             % (screen[0], screen[1]))
        box = (x, y, width, height)
    if not path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = "screenshot-%s.png" % stamp
        if osp.isdir(".tea_agent_run"):
            # A workspace: the picture belongs to the session.
            path = osp.join(".tea_agent_run", "artifacts", name)
        else:
            # The extension server runs outside a workspace - the picture goes
            # to the extension layer's own directory.
            path = osp.join(osp.expanduser("~"), ".tea_agent", "artifacts", name)
    if not osp.isabs(path):
        path = osp.abspath(path)

    _capture(path)
    width, height = crop_and_scale(path, box, max_width)
    size = os.path.getsize(path)
    return {"text": ("saved %s (%dx%d, %d bytes). The picture is attached to "
                     "this message; the coordinates of the mouse are the pixels "
                     "of this picture." % (path, width, height, size)),
            "image": path}


# --- the keyboard ----------------------------------------------------------

# The evdev key codes of the keys that have a name a human would use.
NAMED_KEYS = {
    "esc": 1, "escape": 1,
    "backspace": 14, "back": 14,
    "tab": 15,
    "enter": 28, "return": 28,
    "shift": 42, "lshift": 42, "rshift": 54,
    "ctrl": 29, "control": 29, "lctrl": 29, "rctrl": KEY_RIGHTCTRL,
    "alt": 56, "lalt": 56, "ralt": 100,
    "meta": 125, "super": 125, "win": 125, "lwin": 125, "rwin": 126,
    "menu": 139, "apps": 139, "compose": 127,
    "capslock": 58,
    "numlock": 69, "scrolllock": 70,
    "pause": 119,
    "printscreen": 99, "prtsc": 99, "sysrq": 99,
    "insert": 110, "ins": 110,
    "delete": 111, "del": 111,
    "home": 102,
    "end": 107,
    "pageup": 104, "pgup": 104,
    "pagedown": 109, "pgdn": 109,
    "left": 105, "up": 103, "right": 106, "down": 108,
    "space": 57, "spacebar": 57,
    "semicolon": 39, "equals": 13, "comma": 51, "minus": 12,
    "period": 52, "slash": 53, "backslash": 43,
    "backtick": 41, "grave": 41,
    "lbracket": 26, "rbracket": 27, "quote": 40,
}
for _index in range(1, 25):
    if _index <= 10:
        NAMED_KEYS["f%d" % _index] = 59 + _index - 1
    elif _index <= 12:
        NAMED_KEYS["f%d" % _index] = 87 + _index - 11
    else:
        NAMED_KEYS["f%d" % _index] = 183 + _index - 13

# The US layout: what a key produces without and with shift.
_US_TEXT = {
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34, "h": 35,
    "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49, "o": 24, "p": 25,
    "q": 16, "r": 19, "s": 31, "t": 20, "u": 22, "v": 47, "w": 17, "x": 45,
    "y": 21, "z": 44,
    "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9,
    "9": 10, "0": 11,
    " ": 57, "\t": 15, "\n": 28, "\r": 28,
    "-": 12, "=": 13, "[": 26, "]": 27, "\\": 43, ";": 39, "'": 40,
    "`": 41, ",": 51, ".": 52, "/": 53,
}
_US_SHIFTED = {
    "!": 2, "@": 3, "#": 4, "$": 5, "%": 6, "^": 6, "&": 7, "*": 8,
    "(": 9, ")": 10, "_": 12, "+": 13, "{": 26, "}": 27, "|": 43,
    ":": 39, '"': 40, "~": 41, "<": 51, ">": 52, "?": 53,
}


def _keysym(char):
    """The X keysym of a character (the keys the layout is built from)."""
    code = ord(char)
    if code <= 0xFF:
        return code
    return 0x01000000 + code          # the Unicode keysyms


class _XImage(ctypes.Structure):
    """The first fields of struct XImage - enough to read the pixels."""

    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
        ("obdata", ctypes.c_void_p),
    ]


_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_X11_LIBRARY = None


def _x11_library():
    """libX11, with an error handler that does not kill the process.

    The default Xlib error handler exits the process - one bad request
    would take the extension server (and with it every tool) down.
    """
    global _X11_LIBRARY
    if _X11_LIBRARY is not None:
        return _X11_LIBRARY
    try:
        lib = ctypes.CDLL("libX11.so.6")
    except OSError:
        return None
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    lib.XDefaultScreen.argtypes = [ctypes.c_void_p]
    lib.XDefaultScreen.restype = ctypes.c_int
    lib.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.XRootWindow.restype = ctypes.c_ulong
    lib.XGetImage.restype = ctypes.POINTER(_XImage)
    lib.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                               ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
                               ctypes.c_ulong, ctypes.c_int]
    lib.XDestroyImage.argtypes = [ctypes.POINTER(_XImage)]
    lib.XDisplayKeycodes.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                      ctypes.POINTER(ctypes.c_int)]
    lib.XGetKeyboardMapping.restype = ctypes.POINTER(ctypes.c_ulong)
    lib.XGetKeyboardMapping.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_int,
                                         ctypes.POINTER(ctypes.c_int)]
    lib.XFree.argtypes = [ctypes.c_void_p]
    lib.XSetErrorHandler.argtypes = [ctypes.c_void_p]
    lib.XSetErrorHandler.restype = ctypes.c_void_p

    def _ignore(_display, _event):
        return 0

    handler = _ERROR_HANDLER(_ignore)
    lib.XSetErrorHandler(handler)
    # The handler has to stay alive as long as the library is used.
    lib._a2_error_handler = handler
    _X11_LIBRARY = lib
    return lib


def _keymap_offset(mapping):
    """How much the X key codes differ from the kernel ones.

    Normally it is eight.  When the layout is the US one the difference can
    be measured, and then it is exact even if the server counts differently.
    """
    differences = {}
    for char, code in _US_TEXT.items():
        if not char.isalpha():
            continue
        entry = mapping.get(_keysym(char))
        if not entry:
            continue
        difference = entry[0] - code
        differences[difference] = differences.get(difference, 0) + 1
    if differences:
        best = max(differences.items(), key=lambda item: item[1])
        if best[1] >= 20:              # most of the letters agree
            return best[0]
    return 8


def _read_keymap():
    """The keyboard layout of the desktop: keysym -> (kernel code, shift).

    The X server knows the layout (Xwayland gets it from the compositor), so
    the key codes are the ones a person would press.  The kernel numbers are
    the X numbers minus eight - that is how the X server counts them.
    """
    lib = _x11_library()
    if lib is None:
        return None
    display = lib.XOpenDisplay(None)
    if not display:
        return None
    try:
        low = ctypes.c_int()
        high = ctypes.c_int()
        lib.XDisplayKeycodes(display, ctypes.byref(low), ctypes.byref(high))
        count = high.value - low.value + 1
        per = ctypes.c_int()
        table = lib.XGetKeyboardMapping(display, ctypes.c_ubyte(low.value), count,
                                        ctypes.byref(per))
        if not table or per.value <= 0:
            return None
        mapping = {}
        for index in range(count):
            keycode = low.value + index
            for level in range(per.value):
                keysym = table[index * per.value + level]
                if keysym and keysym not in mapping:
                    mapping[keysym] = (keycode, level > 0)
        lib.XFree(table)
        offset = _keymap_offset(mapping)
        return dict((sym, (code - offset, shift))
                    for sym, (code, shift) in mapping.items())
    finally:
        lib.XCloseDisplay(display)


_KEYMAP = None


def keymap():
    """The keyboard layout (read once, then remembered)."""
    global _KEYMAP
    if _KEYMAP is None:
        try:
            _KEYMAP = _read_keymap() or {}
        except Exception:              # noqa: BLE001 - a broken X server is not fatal
            _KEYMAP = {}
    return _KEYMAP


def resolve_key(name):
    """The kernel code of a named key ('enter', 'f5', 'a' ...)."""
    key = (name or "").strip().lower()
    if key in NAMED_KEYS:
        return NAMED_KEYS[key]
    if len(key) == 1:
        entry = keymap().get(_keysym(key))
        if entry:
            return entry[0]
        if key in _US_TEXT:
            return _US_TEXT[key]
        if key in _US_SHIFTED:
            return _US_SHIFTED[key]
        if key.isupper() and key.lower() in _US_TEXT:
            return _US_TEXT[key.lower()]
    if key.startswith("f") and key[1:].isdigit():
        return NAMED_KEYS.get(key)
    raise ValueError("unknown key %r; use a name from %s, or a single character"
                     % (name, ", ".join(sorted(NAMED_KEYS))))


class Keyboard:
    """The keyboard, as a virtual device in the kernel."""

    def __init__(self):
        self._device = None

    def _dev(self):
        if self._device is None:
            codes = set(NAMED_KEYS.values())
            codes |= set(_US_TEXT.values())
            codes |= set(_US_SHIFTED.values())
            codes |= set(code for code, _shift in keymap().values())
            self._device = VirtualDevice("a2-virtual-keyboard", keys=sorted(codes))
        return self._device

    def close(self):
        if self._device is not None:
            self._device.close()
            self._device = None

    def tap(self, code, hold_ms=0, times=1):
        """Press a key and let it go again."""
        device = self._dev()
        for index in range(max(1, int(times))):
            device.emit(EV_KEY, code, 1)
            device.sync()
            time.sleep(max(0, hold_ms) / 1000.0 if hold_ms else 0.01)
            device.emit(EV_KEY, code, 0)
            device.sync()
            if index + 1 < max(1, int(times)):
                time.sleep(0.015)

    def chord(self, codes):
        """Press several keys at once, let them go in reverse order."""
        device = self._dev()
        for code in codes:
            device.emit(EV_KEY, code, 1)
            device.sync()
            time.sleep(0.012)
        time.sleep(0.02)
        for code in reversed(codes):
            device.emit(EV_KEY, code, 0)
            device.sync()
            time.sleep(0.012)

    def key_for(self, char):
        """The key (and the shift) that produces a character, or None."""
        entry = keymap().get(_keysym(char))
        if entry:
            return entry
        if char in _US_TEXT:
            return (_US_TEXT[char], False)
        if char in _US_SHIFTED:
            return (_US_SHIFTED[char], True)
        if len(char) == 1 and char.isupper() and char.lower() in _US_TEXT:
            return (_US_TEXT[char.lower()], True)
        return None

    def type_text(self, text, delay_ms=8):
        """Type text with the keys. Returns (typed, missing characters)."""
        typed = 0
        missing = []
        for char in text:
            entry = self.key_for(char)
            if entry is None:
                missing.append(char)
                continue
            code, shift = entry
            device = self._dev()
            if shift:
                device.emit(EV_KEY, KEY_LEFTSHIFT, 1)
                device.sync()
            device.emit(EV_KEY, code, 1)
            device.sync()
            time.sleep(0.01)
            device.emit(EV_KEY, code, 0)
            device.sync()
            if shift:
                device.emit(EV_KEY, KEY_LEFTSHIFT, 0)
                device.sync()
            typed += 1
            if delay_ms:
                time.sleep(max(0, delay_ms) / 1000.0)
        return typed, missing


# --- the input method ------------------------------------------------------

def ime_state():
    """The state of the input method: None (no fcitx5), 1 (off), 2 (on).

    An input method that is on translates what is typed: "hello" can become
    Chinese (fcitx5 does that with its cloud pinyin).  A tool that types
    text wants the letters, not the translation.
    """
    if not shutil.which("fcitx5-remote"):
        return None
    try:
        done = subprocess.run(["fcitx5-remote"], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    out = done.stdout.decode("utf-8", "replace").strip()
    return int(out) if out.isdigit() else None


def ime_set(active):
    """Turn the input method on or off (fcitx5). True when it worked."""
    if not shutil.which("fcitx5-remote"):
        return False
    try:
        subprocess.run(["fcitx5-remote", "-o" if active else "-c"],
                       capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


# --- the clipboard ---------------------------------------------------------

def _run(command, timeout=15, stdin=None):
    try:
        done = subprocess.run(command, input=stdin, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(str(exc))
    if done.returncode != 0:
        raise RuntimeError(done.stderr.decode("utf-8", "replace").strip()
                           or "%s failed" % command[0])
    return done.stdout.decode("utf-8", "replace")


def set_clipboard(text):
    """Put text into the clipboard. Returns the program that did it."""
    if shutil.which("gdbus"):
        try:
            _run(["gdbus", "call", "--session", "--dest", "org.kde.klipper",
                  "--object-path", "/klipper", "--method",
                  "org.kde.klipper.klipper.setClipboardContents", text or ""])
            return "klipper"
        except RuntimeError:
            pass
    if shutil.which("wl-copy"):
        _run(["wl-copy"], stdin=(text or "").encode("utf-8"))
        return "wl-copy"
    if shutil.which("xclip"):
        _run(["xclip", "-selection", "clipboard"], stdin=(text or "").encode("utf-8"))
        return "xclip"
    if shutil.which("xsel"):
        _run(["xsel", "--clipboard", "--input"], stdin=(text or "").encode("utf-8"))
        return "xsel"
    raise RuntimeError("no clipboard program found (klipper, wl-copy, xclip or xsel)")


def get_clipboard():
    """The text in the clipboard (None when there is none)."""
    if shutil.which("gdbus"):
        try:
            out = _run(["gdbus", "call", "--session", "--dest", "org.kde.klipper",
                        "--object-path", "/klipper", "--method",
                        "org.kde.klipper.klipper.getClipboardContents"])
        except RuntimeError:
            out = ""
        out = out.strip()
        if out.startswith("(") and out.endswith(")"):
            out = out[1:-1]
        if len(out) >= 2 and out[0] == "'" and out[-1] == "'":
            out = out[1:-1]
        if out:
            return out.replace("\\n", "\n")
    if shutil.which("wl-paste"):
        try:
            return _run(["wl-paste", "--no-newline"], timeout=5)
        except RuntimeError:
            pass
    if shutil.which("xclip"):
        try:
            return _run(["xclip", "-selection", "clipboard", "-o"], timeout=5)
        except RuntimeError:
            pass
    return None


# --- the mouse -------------------------------------------------------------

def _x11_pointer():
    """The position of the X11 pointer (None when there is no X server)."""
    if not shutil.which("xdotool"):
        return None
    try:
        out = _run(["xdotool", "getmouselocation", "--shell"], timeout=5)
    except RuntimeError:
        return None
    values = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
    try:
        return int(values["X"]), int(values["Y"])
    except (KeyError, ValueError):
        return None


class Mouse:
    """The pointer, as a virtual absolute device in the kernel."""

    def __init__(self, size=None):
        self.size = size or screen_size()
        self._device = None
        self._position = None

    def _dev(self):
        if self._device is None:
            self._device = VirtualDevice(
                "a2-virtual-mouse",
                abs_max={ABS_X: (0, self.size[0]), ABS_Y: (0, self.size[1])},
                rels=(REL_WHEEL, REL_HSCROLL),
                buttons=(BTN_LEFT, BTN_RIGHT, BTN_MIDDLE))
        return self._device

    def close(self):
        if self._device is not None:
            self._device.close()
            self._device = None

    def move(self, x, y):
        width, height = self.size
        x = max(0, min(width - 1, int(round(x))))
        y = max(0, min(height - 1, int(round(y))))
        device = self._dev()
        device.emit(EV_ABS, ABS_X, x)
        device.emit(EV_ABS, ABS_Y, y)
        device.sync()
        self._position = (x, y)
        time.sleep(0.02)
        return x, y

    def button(self, name="left", down=True):
        code = {"left": BTN_LEFT, "right": BTN_RIGHT,
                "middle": BTN_MIDDLE}.get(name, BTN_LEFT)
        device = self._dev()
        device.emit(EV_KEY, code, 1 if down else 0)
        device.sync()

    def click(self, button="left", clicks=1, hold_ms=40):
        for index in range(max(1, int(clicks))):
            self.button(button, True)
            time.sleep(max(0, hold_ms) / 1000.0)
            self.button(button, False)
            if index + 1 < max(1, int(clicks)):
                time.sleep(0.06)

    def drag(self, x1, y1, x2, y2, steps=20, hold_ms=40):
        self.move(x1, y1)
        self.button("left", True)
        time.sleep(0.05)
        count = max(1, int(steps))
        for index in range(1, count + 1):
            part = index / float(count)
            self.move(x1 + (x2 - x1) * part, y1 + (y2 - y1) * part)
            time.sleep(0.01)
        time.sleep(max(0, hold_ms) / 1000.0)
        self.button("left", False)

    def scroll(self, dx=0, dy=0):
        device = self._dev()
        if dy:
            device.emit(EV_REL, REL_WHEEL, int(dy))
        if dx:
            device.emit(EV_REL, REL_HSCROLL, int(dx))
        device.sync()
        time.sleep(0.05)

    def position(self):
        """Where the pointer is, and where that answer comes from.

        The tool knows where it put the pointer.  xdotool can read the X11
        pointer, but on a Wayland desktop that one only follows the real
        pointer while it is over an X11 window - so it is the fallback, not
        the truth.
        """
        if self._position:
            return self._position, "the last position this tool set"
        found = _x11_pointer()
        if found:
            _width, _height, scale = screen_geometry()
            return (int(round(found[0] * scale)), int(round(found[1] * scale))), \
                   "the X11 pointer"
        return (0, 0), "unknown (the pointer was never moved)"


# --- the tools -------------------------------------------------------------

def mouse(action="position", x=-1, y=-1, button="left", clicks=1, to_x=-1, to_y=-1,
          steps=20, dx=0, dy=0, hold_ms=40, image_width=0):
    """Move, click, drag or scroll the mouse."""
    action = (action or "position").strip().lower()
    device = Mouse()
    try:
        width, height = device.size
        factor = 1.0
        if image_width and width:
            # The model clicked on a picture that was scaled down - the
            # coordinates are the pixels of that picture.
            factor = width / float(image_width)

        def point(px, py):
            return px * factor, py * factor

        if action == "position":
            found, source = device.position()
            return "the mouse is at %d,%d (%s; the screen is %dx%d)" % (
                found[0], found[1], source, width, height)

        if action == "move":
            current = device.position()[0]
            target_x = current[0] if x < 0 else int(x)
            target_y = current[1] if y < 0 else int(y)
            target_x, target_y = point(target_x, target_y)
            device.move(target_x, target_y)
            return "moved the mouse to %d,%d" % (target_x, target_y)

        if action in ("click", "double_click", "right_click", "middle_click"):
            current = device.position()[0]
            target_x = current[0] if x < 0 else int(x)
            target_y = current[1] if y < 0 else int(y)
            target_x, target_y = point(target_x, target_y)
            device.move(target_x, target_y)
            if action == "double_click":
                button, clicks = "left", 2
            elif action == "right_click":
                button = "right"
            elif action == "middle_click":
                button = "middle"
            device.click(button, clicks, hold_ms)
            return "clicked %s at %d,%d" % (button, target_x, target_y)

        if action == "drag":
            current = device.position()[0]
            start_x = current[0] if x < 0 else int(x)
            start_y = current[1] if y < 0 else int(y)
            end_x = start_x if to_x < 0 else int(to_x)
            end_y = start_y if to_y < 0 else int(to_y)
            start_x, start_y = point(start_x, start_y)
            end_x, end_y = point(end_x, end_y)
            device.drag(start_x, start_y, end_x, end_y, steps, hold_ms)
            return "dragged from %d,%d to %d,%d in %d steps" % (
                start_x, start_y, end_x, end_y, max(1, int(steps)))

        if action == "scroll":
            device.scroll(dx, dy)
            current = device.position()[0]
            return "scrolled by %d (vertical) and %d (horizontal) at %d,%d" % (
                int(dy), int(dx), current[0], current[1])

        raise ValueError("unknown action %r; use position, move, click, double_click, "
                         "right_click, middle_click, drag or scroll" % action)
    finally:
        device.close()


def _chord_codes(keys):
    """The key codes of a combination ('ctrl+v' or ["ctrl", "v"])."""
    sequence = keys
    if isinstance(sequence, str):
        sequence = [part for part in sequence.replace("+", " ").split() if part]
    if not sequence:
        raise ValueError("no keys given")
    return [resolve_key(part) for part in sequence]


def keyboard(action="type", text="", key="", keys=None, delay_ms=8, hold_ms=0,
             times=1, method="auto", paste_key="shift+insert", ime="auto"):
    """Type text, press a key, press a combination, or use the clipboard."""
    action = (action or "type").strip().lower()
    board = Keyboard()
    try:
        if action == "type":
            if not text:
                raise ValueError("action=type needs text")
            missing = [char for char in text if board.key_for(char) is None]
            if missing and method in ("auto", "paste"):
                # Some characters are not on the keyboard (Chinese, emoji).
                # The clipboard can carry them: put the text there, paste it.
                try:
                    set_clipboard(text)
                    board.chord(_chord_codes(paste_key))
                    return ("put the text into the clipboard and pasted it (%d "
                            "character(s) are not on this keyboard layout: %s)"
                            % (len(missing), ", ".join(sorted(set(missing)))))
                except RuntimeError as exc:
                    reason = "the clipboard is not usable (%s)" % exc
            else:
                reason = ""
            # An input method that is on translates the letters ("hello" can
            # become Chinese).  The tool wants the letters - so the input
            # method is switched off while typing and back on afterwards.
            state = ime_state() if ime != "keep" else None
            switched = state == 2
            if switched:
                ime_set(False)
            try:
                typed, missing = board.type_text(text, delay_ms)
            finally:
                if switched:
                    ime_set(True)
            if missing:
                return ("typed %d character(s); %d character(s) are not on this "
                        "keyboard layout and were left out (%s)%s"
                        % (typed, len(missing), ", ".join(sorted(set(missing))),
                           ("; " + reason) if reason else ""))
            return "typed %d character(s): %s" % (typed, text)

        if action == "key":
            if not key:
                raise ValueError("action=key needs key (enter, tab, esc, f5, a ...)")
            board.tap(resolve_key(key), hold_ms, times)
            return "pressed %s%s" % (key, "" if int(times) == 1 else " %d times" % int(times))

        if action == "hotkey":
            board.chord(_chord_codes(keys))
            return "pressed %s" % "+".join(
                str(part) for part in (keys if isinstance(keys, list)
                                       else str(keys).replace("+", " ").split()))

        if action == "clipboard":
            set_clipboard(text)
            return "put %d character(s) into the clipboard" % len(text or "")

        if action == "clipboard_read":
            return "the clipboard holds: %r" % (get_clipboard(),)

        if action == "paste":
            board.chord(_chord_codes(paste_key))
            return "pasted"

        raise ValueError("unknown action %r; use type, key, hotkey, clipboard, "
                         "clipboard_read or paste" % action)
    finally:
        board.close()
