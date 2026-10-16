#!/usr/bin/env python3
"""Generate the DS client's 6x8 bitmap font from a BDF, and let a human check it.

42 columns on a 256px screen needs 6px-wide glyphs. libnds' console font is 8x8,
which would give 32 columns and break the fixed geometry the protocol assumes.

TrueType faces rasterized at 8px were tried first and were unusable: hinting
mangles the shapes at that size and '_' came out entirely blank in all three
faces available, which would silently delete underscores from source code on
screen. A font actually designed as pixel art is the right input, so this reads
a BDF and prints ASCII art so the glyphs are judged by eye rather than trusted.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

CELL_W, CELL_H = 6, 8
FIRST, LAST = 0x20, 0x7E


def parse_bdf(path: str) -> tuple[dict[int, list[int]], dict[str, str]]:
    """Minimal BDF reader: encoding, BBX, and the bitmap rows. Enough for a
    fixed-width pixel font, and small enough to audit."""
    glyphs: dict[int, list[int]] = {}
    meta: dict[str, str] = {}
    enc: int | None = None
    bbx: tuple[int, int, int, int] | None = None
    rows: list[int] | None = None
    font_h = CELL_H

    with open(path, "r", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if line.startswith("FONT "):
                meta["font"] = line[5:]
            elif line.startswith("FONTBOUNDINGBOX"):
                parts = line.split()
                meta["bbox"] = " ".join(parts[1:])
                font_h = int(parts[2])
            elif line.startswith("ENCODING"):
                enc = int(line.split()[1])
            elif line.startswith("BBX"):
                w, h, xo, yo = (int(v) for v in line.split()[1:5])
                bbx = (w, h, xo, yo)
            elif line == "BITMAP":
                rows = []
            elif line == "ENDCHAR":
                if enc is not None and 0 <= enc <= 0x10FFFF and rows is not None and bbx:
                    glyphs[enc] = _place(rows, bbx, font_h)
                enc, bbx, rows = None, None, None
            elif rows is not None and line:
                rows.append(int(line, 16))
    return glyphs, meta


def _place(rows: list[int], bbx: tuple[int, int, int, int], font_h: int) -> list[int]:
    """Position a glyph's bitmap inside the 6x8 cell.

    BDF rows are left-aligned in whole bytes and positioned by the bounding box
    offsets, with y measured up from the baseline. Getting this wrong shifts
    every glyph by a pixel, which is subtle enough to miss without the art dump.
    """
    gw, gh, xoff, yoff = bbx
    # Baseline: BDF y-offset counts up from it. Spleen 5x8 has a descent of 1.
    descent = 1
    top = font_h - descent - gh - yoff
    out = [0] * CELL_H
    for i, bits in enumerate(rows):
        y = top + i
        if not (0 <= y < CELL_H):
            continue
        # Row is stored left-aligned in ceil(gw/8) bytes; take the top gw bits.
        byte_w = (gw + 7) // 8 * 8
        acc = 0
        for x in range(gw):
            if bits & (1 << (byte_w - 1 - x)):
                cx = x + xoff
                if 0 <= cx < CELL_W:
                    acc |= 1 << (CELL_W - 1 - cx)
        out[y] = acc
    return out


def drop_to(rows: list[int], out_w: int, out_h: int) -> list[int]:
    """Downsample one 6x8 glyph row list to a smaller cell.

    The drops are chosen against Spleen's actual ink, verified by the art
    dumps in this file's review output:

      5x7: drop column 5 (the spacing column) and row 0 (the empty top
           bearing) -- every glyph keeps its full shape and descenders.
      4x6: drop columns 0 and 5 and rows 0 and 6.  Column 0 is empty for
           most glyphs and M/W/N keep their left stems; dropping row 6
           instead of 7 keeps one pixel of every descender and the '_'
           underline, which live in row 7.
    """
    if (out_w, out_h) == (CELL_W, CELL_H):
        return list(rows)
    if out_w == 5 and out_h == 7:
        keep_cols = range(0, 5)
        keep_rows = range(1, 8)
    elif out_w == 4 and out_h == 6:
        # Dropping column 0 was tried first and broke every glyph whose left
        # stem lives there (A, M, W, N, H, I, T).  Dropping the middle column
        # keeps both stems and only thins the body.
        keep_cols = (0, 1, 3, 4)
        keep_rows = (1, 2, 3, 4, 5, 7)
    else:
        raise SystemExit(f"no drop policy for {out_w}x{out_h} cells")
    out = []
    for y in keep_rows:
        bits = rows[y]
        acc = 0
        for out_x, src_x in enumerate(keep_cols):
            if bits & (1 << (CELL_W - 1 - src_x)):
                acc |= 1 << (out_w - 1 - out_x)
        out.append(acc)
    return out


def art(glyphs: dict[int, list[int]], sample: str) -> str:
    out = []
    for ch in sample:
        rows = glyphs.get(ord(ch))
        if rows is None:
            out.append(f"  '{ch}'  MISSING")
            continue
        out.append(f"  '{ch}'")
        for bits in rows:
            out.append("   " + "".join(
                "#" if bits & (1 << (CELL_W - 1 - x)) else "." for x in range(CELL_W)
            ))
    return "\n".join(out)


def audit(glyphs: dict[int, list[int]]) -> tuple[list[str], list[str]]:
    """Blank and missing glyphs are silent failures -- the screen shows spaces
    where characters should be, and nothing errors."""
    missing = [chr(c) for c in range(FIRST, LAST + 1) if c not in glyphs]
    blank = [
        chr(c) for c in range(FIRST, LAST + 1)
        if c != 0x20 and c in glyphs and not any(glyphs[c])
    ]
    return missing, blank


def build_extended(glyphs: dict[int, list[int]]) -> list[tuple[int, list[int], str]]:
    """Resolve the extended table into concrete bitmaps.

    Returns (codepoint, rows, note). Raises if a "bdf" entry names a glyph the
    font does not actually have -- that would silently render as blank, which is
    worse than the '?' this whole change is replacing.
    """
    from extglyphs import EXTENDED

    out = []
    for cp, src in EXTENDED:
        kind = src[0]
        if kind == "bdf":
            rows = glyphs.get(src[1])
            if rows is None or not any(rows):
                raise SystemExit(
                    f"U+{cp:04X}: font lacks U+{src[1]:04X}; use copy or bits instead"
                )
            note = f"from U+{src[1]:04X}"
        elif kind == "copy":
            rows = glyphs.get(src[1])
            if rows is None or not any(rows):
                raise SystemExit(f"U+{cp:04X}: cannot copy missing U+{src[1]:04X}")
            note = f"substituted with U+{src[1]:04X}"
        elif kind == "bits":
            rows = list(src[1])
            if len(rows) != CELL_H:
                raise SystemExit(f"U+{cp:04X}: bits must be {CELL_H} rows")
            note = "drawn"
        else:
            raise SystemExit(f"U+{cp:04X}: unknown source {kind}")
        out.append((cp, rows, note))
    return out


def emit_pymap(ext: list[tuple[int, list[int], str]]) -> str:
    from extglyphs import WIRE_BASE

    lines = [
        '"""Generated by tools/genfont.py -- do not edit by hand.',
        "",
        "Maps Unicode codepoints the DS can render to their wire bytes. Generated",
        "from the same table as the C font, so the two ends cannot disagree about",
        'what a byte above 0x7E means."""',
        "",
        f"WIRE_BASE = 0x{WIRE_BASE:02X}",
        "",
        "# codepoint -> wire byte",
        "TO_WIRE = {",
    ]
    for i, (cp, _, note) in enumerate(ext):
        lines.append(f"    0x{cp:04X}: 0x{WIRE_BASE + i:02X},  # {chr(cp)}  {note}")
    lines += ["}", "", "# wire byte -> codepoint", "FROM_WIRE = {v: k for k, v in TO_WIRE.items()}", ""]
    return "\n".join(lines)


def emit_header(glyphs: dict[int, list[int]], source: str, out_w: int = CELL_W,
                out_h: int = CELL_H, guard: str = "DSTERM_FONT_H",
                table: str = "font6x8", ext_table: str = "font6x8_ext",
                include_guard_comment: bool = True,
                width_macro: str = "FONT_W",
                height_macro: str = "FONT_H") -> str:
    lines = [
        "// Generated by tools/genfont.py -- do not edit by hand.",
        f"// Source: {source}",
        "// Spleen is Copyright (c) 2018-2026 Frederic Cambus, BSD 2-Clause.",
        "//",
        f"// {out_w}x{out_h} cells for ASCII 0x20..0x7E: one byte per row, bit "
        f"{out_w - 1} leftmost.",
    ]
    if include_guard_comment:
        lines.append("// 42 columns x 24 rows fills a 256x192 DS screen exactly.")
    lines += [
        "",
        f"#ifndef {guard}",
        f"#define {guard}",
        "",
        "#include <stdint.h>",
        "",
        f"#define FONT_FIRST 0x{FIRST:02X}",
        f"#define FONT_LAST  0x{LAST:02X}",
        f"#define {width_macro} {out_w}",
        f"#define {height_macro} {out_h}",
        "",
        f"static const uint8_t {table}[{LAST - FIRST + 1}][{out_h}] = {{",
    ]
    for code in range(FIRST, LAST + 1):
        rows = drop_to(glyphs.get(code, [0] * CELL_H), out_w, out_h)
        packed = ", ".join(f"0x{b:02X}" for b in rows)
        # Never end a // comment with a backslash: it continues the comment onto
        # the next line, silently commenting out the following glyph and
        # shifting every entry after it. gcc's -Wcomment caught this.
        label = {0x20: "space", 0x5C: "backslash"}.get(code, chr(code))
        lines.append(f"    {{ {packed} }},  // 0x{code:02X} {label}")
    lines.append("};")

    ext = build_extended(glyphs)
    from extglyphs import WIRE_BASE

    lines += [
        "",
        "// Extended glyphs, wire bytes 0x%02X and up. Claude Code frames its whole" % WIRE_BASE,
        "// UI in box drawing; without these the screen renders as question marks.",
        "",
        f"#define FONT_EXT_BASE  0x{WIRE_BASE:02X}",
        f"#define FONT_EXT_COUNT {len(ext)}",
        "",
        f"static const uint8_t {ext_table}[{len(ext)}][{out_h}] = {{",
    ]
    for i, (cp, rows, note) in enumerate(ext):
        packed = ", ".join(f"0x{b:02X}" for b in drop_to(rows, out_w, out_h))
        lines.append(f"    {{ {packed} }},  // 0x{WIRE_BASE + i:02X} U+{cp:04X} {note}")
    lines += ["};", "", f"#endif  // {guard}", ""]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("bdf")
    p.add_argument("--sample", default="AWgm_`#@|jQ")
    p.add_argument("--emit", help="write the C header here")
    p.add_argument("--emit-pymap", help="write the Python wire mapping here")
    a = p.parse_args()

    glyphs, meta = parse_bdf(a.bdf)
    print(f"font: {meta.get('font', '?')}")
    print(f"bbox: {meta.get('bbox', '?')}   parsed glyphs: {len(glyphs)}")

    missing, blank = audit(glyphs)
    print(f"missing in 0x20..0x7E: {len(missing)} {''.join(missing)}")
    print(f"blank   in 0x20..0x7E: {len(blank)} {''.join(blank)}")
    print()
    print(art(glyphs, a.sample))

    if a.emit or a.emit_pymap:
        if missing or blank:
            print("\nREFUSING to emit: missing or blank glyphs would render as spaces.")
            return 1

    if a.emit:
        with open(a.emit, "w") as fh:
            fh.write(emit_header(glyphs, a.bdf))
        print(f"\nwrote {a.emit}")

    # The terminal font is the geometry's cell size: 6x8 at 42x24, 5x7 at
    # 51x27, 4x6 at 64x32.  The 6x8 header doubles as the sub screen's UI
    # font (its layout is fixed), so all three are generated here and the
    # renderer picks by cell size at build time.
    for out_w, out_h, name in ((5, 7, "font5x7.h"), (4, 6, "font4x6.h")):
        path = Path(a.emit).parent / name if a.emit else Path(name)
        guard = f"DSTERM_FONT_{out_w}X{out_h}_H"
        with open(path, "w") as fh:
            fh.write(emit_header(glyphs, a.bdf, out_w, out_h, guard,
                                 "font_term", "font_term_ext",
                                 include_guard_comment=False,
                                 width_macro="TERM_FONT_W",
                                 height_macro="TERM_FONT_H"))
        print(f"wrote {path}")

    if a.emit_pymap:
        ext = build_extended(glyphs)
        with open(a.emit_pymap, "w") as fh:
            fh.write(emit_pymap(ext))
        print(f"wrote {a.emit_pymap}  ({len(ext)} extended glyphs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
