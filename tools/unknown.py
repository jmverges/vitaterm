#!/usr/bin/env python3
"""Report characters the wire format cannot represent, from a real session.

Guessing which glyphs a TUI uses wastes card round-trips. This runs the real
program at 42x24 and lists every character that would reach the DS as '?', with
its codepoint and how often it appeared -- so the extended glyph table is driven
by evidence rather than by reading screenshots.

    python tools/unknown.py --duration 20 -- claude
"""

from __future__ import annotations

import argparse
import sys
import time
import unicodedata
from collections import Counter

from dsterm.extmap import TO_WIRE
from dsterm.grid import COLS, ROWS
from dsterm.session import Session


def representable(ch: str) -> bool:
    code = ord(ch)
    return 0x20 <= code <= 0x7E or code in TO_WIRE


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=20.0)
    p.add_argument("--send", default=None, help="input to send after 6s")
    p.add_argument("argv", nargs=argparse.REMAINDER)
    a = p.parse_args(argv)

    command = a.argv[1:] if a.argv and a.argv[0] == "--" else a.argv
    if not command:
        print("usage: unknown.py [--duration N] -- COMMAND", file=sys.stderr)
        return 2

    counts: Counter[str] = Counter()
    started = time.monotonic()
    sent = False

    with Session(command) as s:
        while time.monotonic() - started < a.duration:
            s.pump()
            if a.send and not sent and time.monotonic() - started > 6:
                s.write(a.send.encode().decode("unicode_escape").encode())
                sent = True
            # Read the emulator directly: grid.from_pyte has already folded
            # wide characters, and those are a separate decision.
            buf = s._screen.buffer
            for row in range(ROWS):
                line = buf[row]
                for col in range(COLS):
                    ch = line[col].data
                    if ch and ch != " " and not representable(ch):
                        counts[ch] += 1
            time.sleep(0.2)

    if not counts:
        print("every character on screen is representable")
        return 0

    print(f"{len(counts)} distinct unrepresentable characters:\n")
    for ch, n in counts.most_common():
        code = ord(ch)
        try:
            name = unicodedata.name(ch)
        except ValueError:
            name = "?"
        print(f"  U+{code:04X}  seen {n:6d}  {name}")
    print("\nAdd the wanted ones to tools/extglyphs.py and regenerate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
