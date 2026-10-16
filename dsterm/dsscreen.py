"""The DS's two screens as pixels, for `dsterm view`, and the terminal image
protocols that show them.

The card draws its screens from cells: a glyph from its bitmap font in a
foreground and a background colour (dsclient/source/render.c for the top,
subscreen.c for the bottom). This does the same, with the same fonts
(dsfont.py, copied from the card's headers), the same palette and the same
rules for bold, reverse and the caret, into a 256x192 RGB image per screen --
what the DS shows, at its real size and shape.

A terminal that can show images draws those at any size, crisp and in
proportion: the kitty graphics protocol (kitty, Ghostty, WezTerm, Konsole) or
iTerm2's inline images (iTerm2, WezTerm). Anything else gets the text view.
"""

from __future__ import annotations

import base64
import fcntl
import functools
import os
import struct
import termios
import zlib

from .dsfont import EXT_BASE, FIRST, FONTS
from .extmap import TO_WIRE

SCREEN_W, SCREEN_H = 256, 192

# render.c's palette, RGB15 and slightly below full brightness, as the card
# keeps it. "orange" is the accent, which the card's THEME can change; the
# view shows the default, Claude's clay.
_PALETTE15 = {
    "default": (27, 27, 27), "black": (0, 0, 0), "red": (26, 5, 5),
    "green": (6, 26, 6), "yellow": (27, 24, 6), "blue": (8, 12, 29),
    "magenta": (26, 8, 26), "cyan": (6, 25, 26), "white": (31, 31, 31),
    "orange": (27, 15, 11),
}


def _rgb(c: tuple[int, int, int]) -> bytes:
    return bytes(v << 3 | v >> 2 for v in c)


def _bold(c: tuple[int, int, int]) -> tuple[int, int, int]:
    # render.c brightens halfway to white rather than keeping a second palette.
    return tuple(v + (31 - v) // 2 for v in c)


def top_fg(name: str, bold: bool) -> bytes:
    c = _PALETTE15.get(name, _PALETTE15["default"])
    return _rgb(_bold(c) if bold else c)


def top_bg(name: str) -> bytes:
    # "default" behind the terminal is black, not the foreground's grey.
    if name == "default":
        return b"\0\0\0"
    return _rgb(_PALETTE15.get(name, (0, 0, 0)))


def glyph_code(ch: str) -> int:
    """The byte the card would draw for a character: ASCII as itself, the
    extended table through extmap, anything else as '?', as render.c does."""
    o = ord(ch) if len(ch) == 1 else 0x3F
    if 0x20 <= o <= 0x7E:
        return o
    return TO_WIRE.get(o, 0x3F)


@functools.lru_cache(maxsize=4096)
def _glyph_rows(font: tuple[int, int], code: int, fg: bytes, bg: bytes) -> tuple[bytes, ...]:
    """One glyph in two colours, as `h` rows of `w` RGB pixels."""
    w, h = font
    ascii_glyphs, ext_glyphs = FONTS[font]
    if FIRST <= code < FIRST + len(ascii_glyphs):
        rows = ascii_glyphs[code - FIRST]
    elif EXT_BASE <= code < EXT_BASE + len(ext_glyphs):
        rows = ext_glyphs[code - EXT_BASE]
    else:
        rows = ascii_glyphs[ord("?") - FIRST]
    return tuple(b"".join(fg if bits >> (w - 1 - x) & 1 else bg for x in range(w))
                 for bits in rows)


def _font_for(cols: int, rows: int) -> tuple[int, int]:
    # The cell that fills the screen: 42x24 is 6x8, 51x27 is 5x7, 64x32 is 4x6.
    for font in sorted(FONTS, reverse=True):
        if cols * font[0] <= SCREEN_W and rows * font[1] <= SCREEN_H:
            return font
    return min(FONTS)


def _blank() -> bytearray:
    return bytearray(SCREEN_W * SCREEN_H * 3)


def _put(img: bytearray, px: int, py: int, glyph: tuple[bytes, ...]) -> None:
    stride = SCREEN_W * 3
    for i, row in enumerate(glyph):
        y = py + i
        if 0 <= y < SCREEN_H and 0 <= px and px * 3 + len(row) <= stride:
            at = y * stride + px * 3
            img[at:at + len(row)] = row


def render_top(state: dict | None) -> bytes:
    """The top screen as the card draws it: the terminal's cells centred on
    the screen, bold brightened, reverse swapped, the caret a second swap."""
    img = _blank()
    rows = (state or {}).get("rows") or []
    if not rows:
        return bytes(img)
    cols = max(sum(len(run[0]) for run in row) for row in rows)
    font = _font_for(cols, len(rows))
    fw, fh = font
    ox, oy = (SCREEN_W - cols * fw) // 2, (SCREEN_H - len(rows) * fh) // 2
    cur = (state or {}).get("cursor") or [0, 0, False]
    for r, row in enumerate(rows):
        c = 0
        for text, fgn, bgn, flags in row:
            fg, bg = top_fg(fgn, bool(flags & 1)), top_bg(bgn)
            if flags & 2:
                fg, bg = bg, fg
            for ch in text:
                f, b = fg, bg
                if cur[2] and r == cur[0] and c == cur[1]:
                    f, b = b, f
                _put(img, ox + c * fw, oy + r * fh, _glyph_rows(font, glyph_code(ch), f, b))
                c += 1
    return bytes(img)


def _hex(text: str) -> bytes:
    text = text.lstrip("#")
    try:
        return bytes.fromhex(text) if len(text) == 6 else b"\0\0\0"
    except ValueError:
        return b"\0\0\0"


def render_bottom(state: dict | None, camera: tuple[dict, bytes] | None = None) -> bytes:
    """The bottom screen from its mirrored cells (6x8, from the top-left, as
    subscreen.c lays it out), with the camera's live view in the cells the
    card marks as its viewfinder."""
    img = _blank()
    rows = (state or {}).get("rows") or []
    box = None
    for r, row in enumerate(rows):
        c = 0
        for text, fgx, bgx, flags in row:
            fg, bg = _hex(fgx), _hex(bgx)
            for ch in text:
                _put(img, c * 6, r * 8, _glyph_rows((6, 8), glyph_code(ch), fg, bg))
                if flags & 4:
                    x0, y0, x1, y1 = box or (c, r, c, r)
                    box = (min(x0, c), min(y0, r), max(x1, c), max(y1, r))
                c += 1
    if box and camera:
        header, rgb = camera
        x0, y0, x1, y1 = box
        _paste(img, rgb, header["w"], header["h"],
               x0 * 6, y0 * 8, (x1 - x0 + 1) * 6, (y1 - y0 + 1) * 8)
    return bytes(img)


def _paste(img: bytearray, rgb: bytes, w: int, h: int, bx: int, by: int,
           bw: int, bh: int) -> None:
    """The picture fitted into a box of the screen, aspect kept, centred."""
    if w <= 0 or h <= 0 or len(rgb) != w * h * 3:
        return
    scale = min(bw / w, bh / h)
    pw, ph = max(1, int(w * scale)), max(1, int(h * scale))
    ox, oy = bx + (bw - pw) // 2, by + (bh - ph) // 2
    stride = SCREEN_W * 3
    for y in range(ph):
        sy = min(h - 1, int(y / scale))
        src = sy * w * 3
        line = b"".join(rgb[src + min(w - 1, int(x / scale)) * 3:
                            src + min(w - 1, int(x / scale)) * 3 + 3] for x in range(pw))
        at = (oy + y) * stride + ox * 3
        img[at:at + len(line)] = line


# -- PNG, for terminals that want a file format ----------------------------------


def png(rgb: bytes, w: int, h: int) -> bytes:
    """An RGB image as a PNG: no filters, quick compression, no metadata."""
    raw = b"".join(b"\0" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


# -- the terminal ------------------------------------------------------------------

KITTY, ITERM, TEXT = "kitty", "iterm", "text"


def graphics(env=os.environ) -> str:
    """Which image protocol this terminal speaks. DSTERM_VIEW_GRAPHICS=kitty,
    iterm or text overrides the guess."""
    forced = env.get("DSTERM_VIEW_GRAPHICS", "").lower()
    if forced in (KITTY, ITERM, TEXT):
        return forced
    program = env.get("TERM_PROGRAM", "")
    term = env.get("TERM", "")
    if env.get("KITTY_WINDOW_ID") or term in ("xterm-kitty", "xterm-ghostty") \
            or program.lower() == "ghostty" or env.get("GHOSTTY_RESOURCES_DIR"):
        return KITTY
    if program in ("iTerm.app", "WezTerm") or env.get("ITERM_SESSION_ID"):
        return ITERM
    if env.get("KONSOLE_VERSION"):
        return KITTY
    return TEXT


def cell_aspect(fd: int) -> float:
    """A terminal cell's height over its width, from the window's size in
    pixels where the terminal reports it; 2.0, the usual, where it does not."""
    try:
        rows, cols, xpix, ypix = struct.unpack(
            "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        return 2.0
    if not (rows and cols and xpix and ypix):
        return 2.0
    aspect = (ypix / rows) / (xpix / cols)
    return aspect if 1.2 <= aspect <= 3.0 else 2.0


def kitty_image(image_id: int, rgb: bytes, w: int, h: int, cols: int, rows: int) -> str:
    """Show `rgb` in the next `cols` x `rows` cells, replacing what image
    `image_id` showed before; the cursor stays where it is."""
    data = base64.standard_b64encode(zlib.compress(rgb, 1)).decode()
    head = (f"a=T,f=24,o=z,s={w},v={h},i={image_id},p=1,c={cols},r={rows},"
            f"C=1,q=2")
    chunks = [data[i:i + 4096] for i in range(0, len(data), 4096)] or [""]
    out = []
    for i, part in enumerate(chunks):
        more = 1 if i < len(chunks) - 1 else 0
        keys = f"{head},m={more}" if i == 0 else f"m={more}"
        out.append(f"\x1b_G{keys};{part}\x1b\\")
    return "".join(out)


def kitty_clear() -> str:
    return "\x1b_Ga=d,d=A,q=2\x1b\\"


def iterm_image(rgb: bytes, w: int, h: int, cols: int, rows: int) -> str:
    """Show `rgb` stretched over the next `cols` x `rows` cells (the cells are
    already in proportion), cursor saved and restored around it."""
    data = base64.standard_b64encode(png(rgb, w, h)).decode()
    return (f"\x1b7\x1b]1337;File=inline=1;width={cols};height={rows};"
            f"preserveAspectRatio=0:{data}\x07\x1b8")
