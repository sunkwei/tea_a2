"""Generate resources/appstub.ico: the green disc as a real icon resource.

A .ico with 16/32/48 pixel entries, each a 32bpp BMP (colour + AND mask), the
classic format every tool understands.
"""
import os
import struct

GREEN = (0x2d, 0x8f, 0x5f)  # rgb


def disc_alpha(size):
    """alpha (0..255) per pixel of a filled circle"""
    c = (size - 1) / 2.0
    r = size * 0.42
    out = []
    for y in range(size):
        for x in range(size):
            dx = x - c
            dy = y - c
            inside = (dx * dx + dy * dy) <= r * r
            out.append(255 if inside else 0)
    return out


def entry(size):
    alpha = disc_alpha(size)
    xor = bytearray()
    # BMP rows are bottom-up
    for y in range(size - 1, -1, -1):
        for x in range(size):
            a = alpha[y * size + x]
            r, g, b = GREEN
            if a == 0:
                r = g = b = 0
            xor += bytes((b, g, r, a))
    mask_stride = ((size + 31) // 32) * 4
    mask = bytearray(mask_stride * size)
    for y in range(size - 1, -1, -1):
        for x in range(size):
            if alpha[y * size + x] == 0:
                mask[y * mask_stride + (x // 8)] |= 0x80 >> (x % 8)
    header = struct.pack('<IiiHHIIiiII', 40, size, size * 2, 1, 32, 0,
                          len(xor) + len(mask), 0, 0, 0, 0)
    return header + bytes(xor) + bytes(mask)


def main():
    sizes = [16, 32, 48]
    images = [entry(s) for s in sizes]
    out = bytearray(struct.pack('<HHH', 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for s, data in zip(sizes, images):
        out += struct.pack('<BBBBHHII', s if s < 256 else 0, s if s < 256 else 0,
                           0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for data in images:
        out += data
    path = os.path.join('resources', 'appstub.ico')
    os.makedirs('resources', exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(out)
    print('wrote %s (%d bytes, %s)' % (path, len(out), 'x'.join(str(s) for s in sizes)))


if __name__ == '__main__':
    main()
