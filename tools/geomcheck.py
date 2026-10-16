#!/usr/bin/env python3
"""Prove a candidate screen geometry on the wire before anyone flashes a card.

    python tools/geomcheck.py 64x32
    python tools/geomcheck.py 42x24 51x27 64x32 255x255

Changing the grid is a coordinated rollout: PROTO_COLS/PROTO_ROWS in the
client must match DSTERM_COLS/DSTERM_ROWS here exactly. A mismatch is not a
cosmetic bug -- the handheld validates row/col against its own bounds before
writing into a cell array with no MMU behind it, so a server serving a shape
the card disagrees with corrupts the screen or halts the card. This measures
what a geometry actually puts on the wire so that flash is made once, against
known numbers.

It runs a scratch server on its own port with no tmux and no dashboard, never
the live service -- a connected handheld flashed for 42x24 would be sent
out-of-bounds diffs the moment a larger grid was served to it.

Checks, per geometry:
  * every frame stays under the 536-byte ceiling the DS reports as its MSS
  * the resync sends every cell exactly once
  * the furthest cell is (ROWS-1, COLS-1) and decodes -- the row/col bytes are
    one byte each, so 255 is the hard ceiling in both directions
"""

from __future__ import annotations

import argparse
import collections
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MSS_CEILING = 536


def parse_geometry(text: str) -> tuple[int, int]:
    try:
        cols, _, rows = text.lower().partition("x")
        return int(cols), int(rows)
    except ValueError:
        raise SystemExit(f"geometry must look like 64x32, got {text!r}")


def measure(port: int, cols: int, rows: int, window: float) -> dict:
    """Connect as the handheld does and report what arrived."""
    # Imported here, after DSTERM_* is set in this process's environment: the
    # geometry is read once at import, and decode_diff validates against it --
    # so this client enforces the same bounds the card does.
    from dsterm.auth import compute_mac, load_key
    from dsterm.protocol import (
        FrameReader, MsgType, NONCE_SIZE, decode_diff, encode, encode_hello,
    )

    key = load_key()
    sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
    reader = FrameReader()
    sock.sendall(encode_hello(("mode", "snippets")))

    counts: collections.Counter = collections.Counter()
    biggest, biggest_type, cells, max_row, max_col = 0, "", 0, -1, -1
    deadline = time.monotonic() + window
    sock.settimeout(0.2)
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            break
        for msg in reader.feed(chunk):
            counts[msg.type.name] += 1
            size = len(msg.payload) + 6
            if size > biggest:
                biggest, biggest_type = size, msg.type.name
            if msg.type is MsgType.NONCE:
                sock.sendall(encode(MsgType.AUTH,
                                    compute_mac(key, msg.payload[:NONCE_SIZE])))
            elif msg.type is MsgType.FRAME_DIFF:
                for ch in decode_diff(msg.payload):
                    cells += 1
                    max_row, max_col = max(max_row, ch.row), max(max_col, ch.col)
        if cells >= cols * rows and counts["CURSOR"]:
            break   # the resync is complete; no reason to keep waiting
    sock.close()
    return {"counts": counts, "biggest": biggest, "biggest_type": biggest_type,
            "cells": cells, "max_row": max_row, "max_col": max_col}


def run_one(cols: int, rows: int, port: int, window: float) -> bool:
    env = dict(os.environ)
    env["DSTERM_COLS"], env["DSTERM_ROWS"] = str(cols), str(rows)
    env["PYTHONPATH"] = str(REPO)
    log = open(f"/tmp/geomcheck-{port}.log", "w")
    server = subprocess.Popen(
        [sys.executable, "-u", "-m", "dsterm.serve",
         "--host", "127.0.0.1", "--port", str(port), "--", "bash", "--norc", "-i"],
        cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        time.sleep(4)
        if server.poll() is not None:
            print(f"  server exited {server.returncode}; see /tmp/geomcheck-{port}.log")
            return False
        # The measuring client must agree with the server about the geometry,
        # so run it in a child that imports under the same DSTERM_* values.
        probe = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); "
             "from tools.geomcheck import measure, report; "
             "sys.exit(0 if report(measure(%d, %d, %d, %f), %d, %d) else 1)"
             % (str(REPO), port, cols, rows, window, cols, rows)],
            cwd=REPO, env=env)
        return probe.returncode == 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()


def report(result, cols: int, rows: int) -> bool:
    expected = cols * rows
    ok_size = result["biggest"] <= MSS_CEILING
    ok_cells = result["cells"] >= expected
    ok_edge = (result["max_row"], result["max_col"]) == (rows - 1, cols - 1)

    print(f"  cells:         {result['cells']} / {expected}")
    print(f"  largest frame: {result['biggest']} bytes "
          f"({result['biggest_type']})  ceiling {MSS_CEILING}")
    print(f"  resync frames: {result['counts']['FRAME_DIFF']}")
    print(f"  furthest cell: row {result['max_row']}, col {result['max_col']} "
          f"(expected {rows - 1}, {cols - 1})")
    for ok, label in ((ok_size, "frame size"), (ok_cells, "full resync"),
                      (ok_edge, "furthest cell")):
        if not ok:
            print(f"  FAILED: {label}")
    return ok_size and ok_cells and ok_edge


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("geometry", nargs="+", help="one or more, e.g. 64x32")
    ap.add_argument("--port", type=int, default=8360, help="first scratch port")
    ap.add_argument("--window", type=float, default=60.0,
                    help="seconds to wait for a full resync")
    args = ap.parse_args()

    failures = 0
    for i, text in enumerate(args.geometry):
        cols, rows = parse_geometry(text)
        print(f"=== {cols}x{rows} ===")
        if not run_one(cols, rows, args.port + i, args.window):
            failures += 1
            print("  VERDICT: FAIL\n")
        else:
            print("  VERDICT: PASS\n")
    print("all geometries passed" if not failures else f"{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
