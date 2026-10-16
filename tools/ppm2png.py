#!/usr/bin/env python3
"""PPM (P6) to PNG with the standard library, and nothing in the PNG but the
image: no EXIF, no text chunks, no timestamps.  So a screenshot is the same
bytes on every machine, and carries nothing the export check would have to
look inside (PNG metadata chunks are where text hides in an image).

    python3 tools/ppm2png.py IN.ppm OUT.png
"""

import struct
import sys
import zlib


def read_ppm(path):
    data = open(path, "rb").read()
    parts, at = [], 0
    while len(parts) < 4:
        while data[at:at + 1].isspace():
            at += 1
        end = at
        while not data[end:end + 1].isspace():
            end += 1
        parts.append(data[at:end])
        at = end
    if parts[0] != b"P6" or parts[3] != b"255":
        sys.exit(f"{path}: not an 8-bit P6 PPM")
    w, h = int(parts[1]), int(parts[2])
    return w, h, data[at + 1:at + 1 + w * h * 3]


def write_png(path, w, h, rgb):
    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body +
                struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))
    rows = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))
    png = (b"\x89PNG\r\n\x1a\n" +
           chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(rows, 9)) +
           chunk(b"IEND", b""))
    open(path, "wb").write(png)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    write_png(sys.argv[2], *read_ppm(sys.argv[1]))
