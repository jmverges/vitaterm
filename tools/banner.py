#!/usr/bin/env python3
"""Draw the README's banner: docs/assets/banner.png, 1280x640.

    tools/screenshots.sh && python3 tools/banner.py

The title is set in the DS's own 6x8 font (dsterm/dsfont.py), scaled up. The
console is a DSi, drawn plainly: matte black, the hinge with its three LEDs
and inner camera, the d-pad and A/B/X/Y. Its two screens are real: Claude
asking to approve an edit, and the DS asking you, as the card's own drawing
code rendered them for the screenshots
(dsclient/build/screens/control-approval.ppm), then given the backlit LCD's
look, where dark colours come up brighter and bluer than they are in the
framebuffer. Claude Code's colours
throughout. 1280x640 is also what GitHub wants for a social preview.

Standard library only.
"""

from __future__ import annotations

import colorsys
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dsterm import dsscreen  # noqa: E402
from dsterm.dsfont import FIRST, FONTS  # noqa: E402

W, H = 1280, 640
BG = (20, 20, 19)            # Claude's near-black
CLAY = (217, 119, 87)
CREAM = (240, 238, 230)
GREY = (135, 134, 127)
BODY = (27, 27, 29)          # the DSi's matte black
BODY_EDGE = (46, 46, 50)
BEZEL = (12, 12, 13)
PART = (36, 36, 40)          # the d-pad and buttons
PART_EDGE = (56, 56, 62)
SCREEN_SHOT = REPO / "dsclient" / "build" / "screens" / "control-approval.ppm"
OUT = REPO / "docs" / "assets" / "banner.png"


class Image:
    def __init__(self, w: int, h: int, fill):
        self.w, self.h = w, h
        self.px = bytearray(bytes(fill) * (w * h))

    def rect(self, x, y, w, h, colour, radius=0):
        c = bytes(colour)
        for yy in range(max(0, y), min(self.h, y + h)):
            for xx in range(max(0, x), min(self.w, x + w)):
                if radius:
                    dx = max(x + radius - xx - 1, 0, xx - (x + w - radius))
                    dy = max(y + radius - yy - 1, 0, yy - (y + h - radius))
                    if dx * dx + dy * dy > radius * radius:
                        continue
                at = (yy * self.w + xx) * 3
                self.px[at:at + 3] = c

    def circle(self, cx, cy, r, colour):
        self.rect(cx - r, cy - r, 2 * r, 2 * r, colour, radius=r)

    def text(self, x, y, text, colour, scale):
        """The DS's own 6x8 glyphs, each pixel a scale x scale square."""
        glyphs = FONTS[(6, 8)][0]
        for i, ch in enumerate(text):
            rows = glyphs[ord(ch) - FIRST] if 0x20 <= ord(ch) <= 0x7E else glyphs[0]
            for gy, bits in enumerate(rows):
                for gx in range(6):
                    if bits >> (5 - gx) & 1:
                        self.rect(x + (i * 6 + gx) * scale, y + gy * scale,
                                  scale, scale, colour)

    def paste(self, x, y, w, h, rgb):
        for row in range(h):
            at = ((y + row) * self.w + x) * 3
            self.px[at:at + w * 3] = rgb[row * w * 3:(row + 1) * w * 3]


def lcd(rgb: bytes) -> bytes:
    """The framebuffer as a DSi's backlit screen shows it: darks lifted and,
    the darker they are, the more saturated; black a touch above black;
    white and the bright colours as they are."""
    cache: dict[bytes, bytes] = {}
    out = bytearray(len(rgb))
    for i in range(0, len(rgb), 3):
        px = rgb[i:i + 3]
        got = cache.get(px)
        if got is None:
            h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in px))
            v2 = v ** 0.6
            s2 = min(1.0, s * (1 + 0.9 * (1 - v)))
            r, g, b = colorsys.hsv_to_rgb(h, s2, v2)
            got = bytes(min(255, int(c * 255) + 10) for c in (r, g, b))
            cache[px] = got
        out[i:i + 3] = got
    return bytes(out)


def draw_dsi(img: Image, x: int, y: int, top: bytes, bottom: bytes) -> None:
    """A DSi, open, screens 1:1, its top-left corner at (x, y)."""
    w, sw, sh = 432, 256, 192
    sx = x + (w - sw) // 2
    half = 236

    # The top half: the lid and its screen.
    img.rect(x, y, w, half, BODY_EDGE, radius=24)
    img.rect(x + 2, y + 2, w - 4, half - 4, BODY, radius=22)
    img.rect(sx - 10, y + 20, sw + 20, sh + 20, BEZEL, radius=6)
    img.paste(sx, y + 30, sw, sh, top)

    # The hinge: three LEDs on the left, the inner camera in the middle.
    hy = y + half + 4
    img.rect(x + 14, hy, w - 28, 22, (22, 22, 24), radius=10)
    for i, colour in enumerate(((240, 214, 90), (230, 236, 255), (90, 150, 255))):
        img.circle(x + 40 + i * 14, hy + 11, 3, colour)
    img.circle(x + w // 2, hy + 11, 5, (8, 8, 9))
    img.circle(x + w // 2, hy + 11, 2, (40, 44, 60))

    # The bottom half: the screen, the d-pad, A/B/X/Y and the power button.
    by = hy + 26
    img.rect(x, by, w, half + 8, BODY_EDGE, radius=24)
    img.rect(x + 2, by + 2, w - 4, half + 4, BODY, radius=22)
    img.rect(sx - 10, by + 20, sw + 20, sh + 20, BEZEL, radius=6)
    img.paste(sx, by + 30, sw, sh, bottom)

    cx, cy = x + 44, by + 96                          # the d-pad
    img.rect(cx - 25, cy - 8, 50, 16, PART_EDGE, radius=3)
    img.rect(cx - 8, cy - 25, 16, 50, PART_EDGE, radius=3)
    img.rect(cx - 23, cy - 6, 46, 12, PART, radius=2)
    img.rect(cx - 6, cy - 23, 12, 46, PART, radius=2)

    bx = x + w - 44                                   # A/B/X/Y
    for dx, dy, letter in ((0, -16, "X"), (16, 0, "A"), (0, 16, "B"), (-16, 0, "Y")):
        img.circle(bx + dx, cy + dy, 9, PART_EDGE)
        img.circle(bx + dx, cy + dy, 8, PART)
        img.text(bx + dx - 2, cy + dy - 4, letter, (78, 78, 86), 1)

    img.circle(x + 40, by + 196, 7, PART_EDGE)        # power
    img.circle(x + 40, by + 196, 5, PART)
    img.text(x + 22, by + 210, "POWER", (66, 66, 72), 1)


def screens() -> tuple[bytes, bytes]:
    """The approval screenshot's two screens at the DS's own 256x192. The
    render is 2x: the top screen, a 16-pixel hinge, then the bottom."""
    if not SCREEN_SHOT.exists():
        raise SystemExit(f"no {SCREEN_SHOT.relative_to(REPO)}: run tools/screenshots.sh first")
    data = SCREEN_SHOT.read_bytes()
    parts = data.split(maxsplit=4)
    w, h, rgb = int(parts[1]), int(parts[2]), parts[4]

    def half(y0: int) -> bytes:
        out = bytearray()
        for y in range(192):
            src = (y0 + 2 * y) * w * 3
            for x in range(256):
                out += rgb[src + 6 * x: src + 6 * x + 3]
        return bytes(out)

    return half(0), half(h - 384)


def main() -> int:
    top, bottom = screens()
    top, bottom = lcd(top), lcd(bottom)
    img = Image(W, H, BG)

    # Left: the name, in the DS's own font, and what it is.
    x0 = 96
    img.text(x0, 176, "DS CLAUDE", CLAY, 8)
    img.text(x0, 252, "TERMINAL", CLAY, 8)
    img.text(x0, 352, "Claude Code on a Nintendo DS", CREAM, 3)
    img.rect(x0, 400, 72, 4, CLAY)
    img.text(x0, 428, "encrypted link  .  paired by code", GREY, 2)
    img.text(x0, 456, "C on the DS  .  Python on the computer", GREY, 2)

    # Right: the console, open, both screens real.
    draw_dsi(img, 768, 50, top, bottom)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(dsscreen.png(bytes(img.px), W, H))
    print(f"wrote {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
