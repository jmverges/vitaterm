#!/usr/bin/env python3
"""Measure what the DS would have to receive: run a command at the grid size
and count the cells that change.

This exists to answer one question with numbers instead of by eyeballing
flicker: when Claude Code streams, does it repaint the whole screen or only the
parts that change? Its answer is why the wire sends cell diffs and a separate
scroll message (docs/PROTOCOL.md), and it is the tool to reach for before
changing how frames are sent.

Usage:
    .venv/bin/python tools/measure_cells.py --duration 15 -- bash -lc 'ls -la; sleep 5'
    .venv/bin/python tools/measure_cells.py --duration 30 --send 'hi\\r' -- claude
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

from dsterm.grid import COLS, ROWS
from dsterm.render import framed
from dsterm.session import Session

FULL_FRAME = COLS * ROWS


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--duration", type=float, default=15.0, help="seconds to run (default 15)")
    p.add_argument("--fps", type=float, default=15.0, help="snapshot rate (default 15)")
    p.add_argument("--send", action="append", default=[],
                   help="input to write after --send-delay; repeatable. \\r and \\n are unescaped")
    p.add_argument("--send-delay", type=float, default=3.0, help="seconds before sending input")
    p.add_argument("--show", action="store_true", help="print the final grid")
    p.add_argument("argv", nargs=argparse.REMAINDER, help="-- command to run")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    command = args.argv[1:] if args.argv and args.argv[0] == "--" else args.argv
    if not command:
        print("error: no command given (use -- COMMAND)", file=sys.stderr)
        return 2

    interval = 1.0 / args.fps
    pending = [s.replace("\\r", "\r").replace("\\n", "\n").replace("\\t", "\t")
               for s in args.send]

    changed_samples: list[int] = []
    snapshot_ms: list[float] = []
    total_bytes = 0
    nonempty_frames = 0

    started = time.monotonic()
    sent_at = started + args.send_delay
    sent = False

    with Session(command) as sess:
        next_tick = started
        while True:
            now = time.monotonic()
            if now - started >= args.duration:
                break
            if not sess.alive() and sess.pump() == 0:
                break

            sess.pump()

            if not sent and pending and now >= sent_at:
                for text in pending:
                    sess.write(text.encode())
                sent = True

            if now >= next_tick:
                _, changes, stats = sess.snapshot()
                total_bytes += stats.bytes_in
                snapshot_ms.append(stats.elapsed_ms)
                if changes:
                    nonempty_frames += 1
                    changed_samples.append(len(changes))
                next_tick += interval
                if next_tick < now:          # fell behind; do not spiral
                    next_tick = now + interval
            else:
                time.sleep(min(0.002, max(0.0, next_tick - now)))

        final = sess.grid

    elapsed = time.monotonic() - started
    ticks = len(snapshot_ms)

    print(f"\n=== cells changed: {' '.join(command)} ===")
    print(f"grid              {COLS}x{ROWS} = {FULL_FRAME} cells")
    print(f"ran               {elapsed:.1f}s, {ticks} snapshots at {args.fps:g} fps target")
    print(f"pty bytes in      {total_bytes}")
    print(f"frames w/ change  {nonempty_frames} of {ticks}")

    if changed_samples:
        srt = sorted(changed_samples)
        p95 = srt[min(len(srt) - 1, int(len(srt) * 0.95))]
        mean = statistics.mean(changed_samples)
        print("\ncells changed per non-empty frame")
        print(f"  min/median/mean   {min(srt)} / {statistics.median(srt)} / {mean:.1f}")
        print(f"  p95/max           {p95} / {max(srt)}")
        print(f"  mean as % of full {mean / FULL_FRAME * 100:.1f}%")
        full_repaints = sum(1 for c in changed_samples if c > FULL_FRAME * 0.9)
        print(f"  near-full frames  {full_repaints} ({full_repaints / len(changed_samples) * 100:.0f}%)")
        sent_cells = sum(changed_samples)
        print(f"\nbandwidth: {sent_cells} cells diffed vs "
              f"{nonempty_frames * FULL_FRAME} if full frames "
              f"({sent_cells / max(1, nonempty_frames * FULL_FRAME) * 100:.1f}%)")
    else:
        print("\nno cell changes recorded")

    if snapshot_ms:
        print(f"\nsnapshot cost    mean {statistics.mean(snapshot_ms):.2f} ms, "
              f"max {max(snapshot_ms):.2f} ms  (budget {interval * 1000:.0f} ms/frame)")

    if args.show:
        print()
        print(framed(final, f"final @ {COLS}x{ROWS}"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
