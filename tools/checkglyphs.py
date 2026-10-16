#!/usr/bin/env python3
"""Which of the characters Claude Code actually draws does Spleen have?

The wire format currently folds everything outside ASCII to '?', which turns a
screen full of boxes into a screen full of question marks. Before extending the
format, check the font can actually render the replacements -- otherwise the
change buys nothing.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "tools")
from genfont import parse_bdf  # noqa: E402

# Observed in the real 42x24 capture, plus the rest of each family so a box is
# never half-renderable.
WANTED = {
    "light box": [0x2500, 0x2502, 0x250C, 0x2510, 0x2514, 0x2518,
                  0x251C, 0x2524, 0x252C, 0x2534, 0x253C],
    "rounded corners": [0x256D, 0x256E, 0x256F, 0x2570],
    "heavy box": [0x2501, 0x2503, 0x250F, 0x2513, 0x2517, 0x251B],
    "double box": [0x2550, 0x2551, 0x2554, 0x2557, 0x255A, 0x255D],
    "blocks": [0x2580, 0x2584, 0x2588, 0x258C, 0x2590, 0x2591, 0x2592, 0x2593],
    "bullets/marks": [0x25CF, 0x25CB, 0x25AA, 0x2022, 0x00B7],
    "arrows": [0x2190, 0x2192, 0x2191, 0x2193, 0x276F, 0x203A],
    "misc ui": [0x2713, 0x2717, 0x2026, 0x00A0],
}


def main() -> int:
    glyphs, meta = parse_bdf("tools/spleen-5x8.bdf")
    print(f"font: {meta.get('font', '?')}   glyphs: {len(glyphs)}\n")

    have_total = 0
    want_total = 0
    for group, codes in WANTED.items():
        have = [c for c in codes if c in glyphs]
        miss = [c for c in codes if c not in glyphs]
        have_total += len(have)
        want_total += len(codes)
        print(f"{group:18s} {len(have)}/{len(codes)}")
        if miss:
            print("   missing: " + " ".join(f"U+{c:04X}" for c in miss))

    print(f"\ntotal {have_total}/{want_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
