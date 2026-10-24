"""Build the Vita terminal's glyph atlas: vita/src/atlas.h.

Every wire byte the server can send (ASCII plus the 0x80.. table in
dsclient/source/font_wire.py) drawn into an 8x16 cell as 8-bit coverage,
regular and bold, from DejaVu Sans Mono with Unifont for what it lacks.

    python3 vita/tools/genatlas.py DejaVuSansMono.ttf DejaVuSansMono-Bold.ttf unifont.otf
"""
import pathlib
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dsclient" / "source"))
from font_wire import FROM_WIRE  # noqa: E402

W, H = 8, 16
SIZE = 13


CMAPS = {}


def has_glyph(font, ch):
    if ch == " ":
        return True
    return ord(ch) in CMAPS[font.path]


def draw(fonts, ch):
    img = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(img)
    for font, size_hint in fonts:
        if has_glyph(font, ch):
            if size_hint == "unifont":
                # Unifont is drawn at its native 16 px; a wide glyph is squeezed.
                g = Image.new("L", (16, 16), 0)
                ImageDraw.Draw(g).text((0, 0), ch, font=font, fill=255)
                bbox = g.getbbox()
                if bbox and bbox[2] > 8:
                    g = g.resize((8, 16), Image.LANCZOS)
                else:
                    g = g.crop((0, 0, 8, 16))
                img.paste(g, (0, 0))
            else:
                d.text((0, 12), ch, font=font, fill=255, anchor="ls")
            return img
    return img


def block(code):
    """Block elements by geometry, so they tile without seams."""
    img = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(img)
    full = [(0, 0, 7, 15)]
    q = {"tl": (0, 0, 3, 7), "tr": (4, 0, 7, 7), "bl": (0, 8, 3, 15), "br": (4, 8, 7, 15)}
    shapes = {
        0x2588: full, 0x2580: [(0, 0, 7, 7)], 0x2584: [(0, 8, 7, 15)],
        0x258C: [(0, 0, 3, 15)], 0x2590: [(4, 0, 7, 15)],
        0x2596: [q["bl"]], 0x2597: [q["br"]], 0x2598: [q["tl"]], 0x259D: [q["tr"]],
        0x2599: [q["tl"], q["bl"], q["br"]], 0x259A: [q["tl"], q["br"]],
        0x259B: [q["tl"], q["tr"], q["bl"]], 0x259C: [q["tl"], q["tr"], q["br"]],
        0x259E: [q["tr"], q["bl"]], 0x259F: [q["tr"], q["bl"], q["br"]],
    }
    if code in (0x2591, 0x2592, 0x2593):
        level = {0x2591: 64, 0x2592: 128, 0x2593: 192}[code]
        return Image.new("L", (W, H), level)
    if code not in shapes:
        return None
    for r in shapes[code]:
        d.rectangle(r, fill=255)
    return img


def box(code):
    """Box drawing joined to the cell edges, so lines are continuous."""
    arms = {  # left, right, up, down
        0x2500: "lr", 0x2502: "ud", 0x250C: "rd", 0x2510: "ld", 0x2514: "ru",
        0x2518: "lu", 0x251C: "rud", 0x2524: "lud", 0x252C: "lrd", 0x2534: "lru",
        0x253C: "lrud",
        0x256D: "rd", 0x256E: "ld", 0x256F: "lu", 0x2570: "ru",
        0x2501: "lr", 0x2503: "ud", 0x2550: "lr", 0x2551: "ud",
    }
    if code not in arms:
        return None
    img = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(img)
    cx, cy = 3, 7
    a = arms[code]
    if "l" in a: d.rectangle((0, cy, cx, cy), fill=255)
    if "r" in a: d.rectangle((cx, cy, 7, cy), fill=255)
    if "u" in a: d.rectangle((cx, 0, cx, cy), fill=255)
    if "d" in a: d.rectangle((cx, cy, cx, 15), fill=255)
    if code in (0x2501, 0x2503):            # heavy
        if "l" in a: d.rectangle((0, cy - 1, 7, cy), fill=255)
        if "u" in a: d.rectangle((cx, 0, cx + 1, 15), fill=255)
    if code in (0x2550, 0x2551):            # double
        if "l" in a:
            d.rectangle((0, cy, 7, cy), fill=0)
            d.rectangle((0, cy - 1, 7, cy - 1), fill=255)
            d.rectangle((0, cy + 1, 7, cy + 1), fill=255)
        if "u" in a:
            d.rectangle((cx, 0, cx, 15), fill=0)
            d.rectangle((cx - 1, 0, cx - 1, 15), fill=255)
            d.rectangle((cx + 1, 0, cx + 1, 15), fill=255)
    if code in (0x256D, 0x256E, 0x256F, 0x2570):   # rounded: soften the corner
        img.putpixel((cx, cy), 0)
        dx = 1 if "r" in a else -1
        dy = 1 if "d" in a else -1
        img.putpixel((cx + dx, cy + dy), 160)
        img.putpixel((cx, cy + dy), 255)
        img.putpixel((cx + dx, cy), 255)
    return img


def main():
    from fontTools.ttLib import TTFont
    for path in sys.argv[1:4]:
        CMAPS[path] = set(TTFont(path).getBestCmap())
    regular = ImageFont.truetype(sys.argv[1], SIZE)
    bold = ImageFont.truetype(sys.argv[2], SIZE)
    uni = ImageFont.truetype(sys.argv[3], 16)
    atlas = Image.new("L", (16 * W, 16 * H * 2), 0)
    for style, main_font in enumerate((regular, bold)):
        fonts = [(main_font, None), (uni, "unifont")]
        for b in range(256):
            if 0x20 <= b <= 0x7E:
                code = b
            elif b in FROM_WIRE:
                code = FROM_WIRE[b]
            else:
                continue
            img = block(code) or box(code) or draw(fonts, chr(code))
            x, y = (b % 16) * W, (b // 16) * H + style * 16 * H
            atlas.paste(img, (x, y))
    out = ROOT / "vita" / "src" / "atlas.h"
    out.parent.mkdir(exist_ok=True)
    data = atlas.tobytes()
    lines = ["// Generated by vita/tools/genatlas.py -- do not edit by hand.",
             "// 16x16 cells of 8x16 coverage per style (regular, then bold),",
             "// indexed by the wire byte.  DejaVu Sans Mono, Unifont fallback.",
             "#define ATLAS_W %d" % atlas.width, "#define ATLAS_H %d" % atlas.height,
             "static const unsigned char atlas_px[%d] = {" % len(data)]
    for i in range(0, len(data), 24):
        lines.append("    " + ", ".join(str(v) for v in data[i:i + 24]) + ",")
    lines.append("};")
    out.write_text("\n".join(lines) + "\n")
    atlas.resize((atlas.width * 3, atlas.height * 3), Image.NEAREST).save(ROOT / "vita" / "build-atlas.png")
    print("wrote", out)


main()
