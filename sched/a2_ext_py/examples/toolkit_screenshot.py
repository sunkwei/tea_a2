"""Capture the screen (or a part of it) and save it as a PNG.

Why it exists: a GUI test needs to see what is on the screen, and the agent's
own tools cannot. This one grabs the picture and writes a PNG.

Two platforms, one tool:

* **Windows** - GDI through ctypes.
* **Linux** - the compositor's own screenshot program (spectacle, grim,
  gnome-screenshot, maim), or the X server itself when there is no such
  program.  The Linux part lives in the extension layer
  (`a2_ext_py/desktop_linux.py`), because it is a lot of code and the other
  two example modules need it too.

It needs nothing but the standard library: GDI through ctypes on Windows,
and on Linux ctypes for /dev/uinput and libX11 plus zlib for the PNG
container. Nothing has to be installed with pip.

The picture is written into the workspace (`.tea_agent_run/artifacts/` by
default) so that the model can pick it up with toolkit_load_image - the tool
itself can only return text.
"""

import os
import struct
import time
import zlib

if os.name == "nt":
    import ctypes
    import ctypes.wintypes
    from ctypes import wintypes

    # --- Win32 ---------------------------------------------------------------

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    # The screen metrics are in *logical* pixels unless the process says it is
    # DPI aware - and GDI draws in physical pixels. Without this the picture
    # covers only the top left corner of the screen (and the mouse lands in
    # the wrong place). Per monitor aware v2 is what a modern app does.
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        except Exception:
            pass

    SRCCOPY = 0x00CC0020
    DIB_RGB_COLORS = 0
    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    def _virtual_screen():
        """The whole desktop, all monitors: (x, y, width, height)."""
        x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if w <= 0 or h <= 0:
            w = user32.GetSystemMetrics(0)
            h = user32.GetSystemMetrics(1)
            x = y = 0
        return x, y, w, h

    def _grab(x, y, width, height):
        """Copy a rectangle of the screen into a BGRA buffer (top-down)."""
        screen_dc = user32.GetDC(None)
        if not screen_dc:
            raise RuntimeError("GetDC failed")
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        if not memory_dc or not bitmap:
            user32.ReleaseDC(None, screen_dc)
            raise RuntimeError("cannot create the offscreen bitmap")
        old = gdi32.SelectObject(memory_dc, bitmap)
        if not gdi32.BitBlt(memory_dc, 0, 0, width, height, screen_dc, x, y, SRCCOPY):
            gdi32.SelectObject(memory_dc, old)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(None, screen_dc)
            raise RuntimeError("BitBlt failed")

        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height        # negative = top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0        # BI_RGB

        buffer = ctypes.create_string_buffer(width * height * 4)
        lines = gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(info), DIB_RGB_COLORS)
        gdi32.SelectObject(memory_dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)
        if lines != height:
            raise RuntimeError("GetDIBits returned %d lines, expected %d" % (lines, height))
        return buffer.raw

    def _png_chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload +
                struct.pack(">I", zlib.crc32(tag + payload) & 0xffffffff))

    def _write_png(path, width, height, bgra):
        """Write a 24 bit RGB PNG (the BGRA buffer is converted on the way)."""
        raw = bytearray()
        stride = width * 4
        for row in range(height):
            raw.append(0)                       # filter type 0 (none)
            base = row * stride
            for column in range(width):
                offset = base + column * 4
                raw += bgra[offset:offset + 3]  # BGR -> RGB
        compressed = zlib.compress(bytes(raw), 6)
        header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        blob = (b"\x89PNG\r\n\x1a\n" +
                _png_chunk(b"IHDR", header) +
                _png_chunk(b"IDAT", compressed) +
                _png_chunk(b"IEND", b""))
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(blob)
        return len(blob)

    def _parse_region(text, screen):
        """'x,y,width,height' -> the rectangle; empty -> the whole screen."""
        if not text:
            return screen
        parts = [p.strip() for p in str(text).replace(" ", ",").split(",") if p.strip()]
        if len(parts) != 4:
            raise ValueError("region must be 'x,y,width,height'")
        x, y, width, height = (int(float(p)) for p in parts)
        if width <= 0 or height <= 0:
            raise ValueError("the region must have a positive width and height")
        return (x, y, width, height)

    def _screenshot_windows(path="", region="", max_width=0):
        screen = _virtual_screen()
        x, y, width, height = _parse_region(region, screen)
        if x + width > screen[0] + screen[2] or y + height > screen[1] + screen[3]:
            raise ValueError("the region is outside the screen (%d,%d %dx%d)"
                             % (screen[0], screen[1], screen[2], screen[3]))

        if not path:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(".tea_agent_run", "artifacts", "screenshot-%s.png" % stamp)
        if not os.path.isabs(path):
            path = os.path.abspath(path)

        pixels = _grab(x, y, width, height)

        if max_width and width > int(max_width):
            factor = max(1, int(round(width / float(max_width))))
            new_width = width // factor
            new_height = max(1, height // factor)
            scaled = bytearray(new_width * new_height * 4)
            for row in range(new_height):
                source = (row * factor) * width * 4
                target = row * new_width * 4
                for column in range(new_width):
                    offset = source + (column * factor) * 4
                    scaled[target:target + 4] = pixels[offset:offset + 4]
                    target += 4
            pixels = bytes(scaled)
            width, height = new_width, new_height

        size = _write_png(path, width, height, pixels)
        # The picture goes with the answer: the model sees the screen instead
        # of only being told where the file is.
        return {"text": ("saved %s (%dx%d, %d bytes). The picture is attached "
                         "to this message; the coordinates of the mouse are "
                         "the pixels of this picture."
                         % (path, width, height, size)),
                "image": path}


if os.name == "nt":
    toolkit_screenshot = _screenshot_windows
else:
    try:
        from a2_ext_py import desktop_linux as _linux
    except ImportError as exc:      # the extension layer is not on the path
        _linux = None
        _linux_error = str(exc)

    def toolkit_screenshot(path="", region="", max_width=0):
        """Take a screenshot and save it as a PNG.

        path: where to write it. Default: .tea_agent_run/artifacts/screenshot-<time>.png
              in the current directory (the workspace).
        region: 'x,y,width,height' in screen coordinates. Empty = the whole
                screen, all monitors.
        max_width: if the picture is wider than this, it is scaled down (nearest
                   neighbour). 0 = keep the original size.
        """
        if _linux is None:
            raise RuntimeError("the Linux desktop helper is missing (%s); it is "
                               "part of the extension layer (a2_ext_py/desktop_linux.py)"
                               % _linux_error)
        return _linux.screenshot(path=path, region=region, max_width=max_width)


def meta_toolkit_screenshot():
    return {
        "name": "toolkit_screenshot",
        "description": ("Take a screenshot of the screen and save it as a PNG "
                        "in the workspace. Use it to see what a GUI looks like "
                        "right now (for a GUI test, or to check what a click "
                        "did). The file is written to "
                        ".tea_agent_run/artifacts/ by default; read it back "
                        "with toolkit_load_image. The coordinates you give the "
                        "mouse are the pixels of this picture."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "where to write the PNG (relative paths "
                                        "are resolved against the workspace)"},
                "region": {"type": "string",
                           "description": "'x,y,width,height' in screen "
                                          "coordinates; empty = the whole screen"},
                "max_width": {"type": "integer",
                              "description": "scale the picture down to this "
                                             "width (0 = keep it)"},
            },
        },
    }
