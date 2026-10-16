#!/usr/bin/env python3
"""Measure the server under a combined workload, and print a Markdown report.

    .venv/bin/python tools/measure.py             # about a minute and a half
    .venv/bin/python tools/measure.py --seconds 60

A real server runs in its own process (so its memory and CPU are its own),
on loopback, with a child that echoes what is typed while another process
prints a line every 20 ms behind it. A test card logs in the way the DS
does -- HELLO, AUTH, SEAL -- and while it types a short line every 200 ms
and times each until the line is on its screen, it also streams the
camera's live view at 3 frames a second and its bottom screen at 4 a
second to a `dsterm view` that is watching. Then the card is dropped and
reconnects, and the server is restarted, and both are timed until the card
has its screen back.

What this is not: the DS's own radio. Loopback takes the network out, so
the latencies here are the server's share; the DS's Control screen shows its
own PING round trip on top (a few milliseconds to a few tens on a home
network). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import secrets
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dsterm.client import Client                       # noqa: E402
from dsterm.protocol import (                          # noqa: E402
    FEATURE_CAMERA,
    FEATURE_MIRROR,
    PREVIEW_BYTES,
    PREVIEW_FORMAT_GRAY4,
    SUBSCREEN_COLS,
    SUBSCREEN_ROWS,
    encode_subscreen_payload,
)

CHILD = ("stty -echo; "
         "(i=0; while :; do i=$((i+1)); printf 'output line %06d\\n' $i; sleep 0.02; done) & "
         "exec cat")


# -- the server, in a process of its own ---------------------------------------


def serve(config_dir: str, key_hex: str, child: str = CHILD) -> None:
    from dsterm.server import Server
    server = Server(["bash", "--noprofile", "--norc", "-c", child], bytes.fromhex(key_hex),
                    host="127.0.0.1", port=0, config_dir=Path(config_dir),
                    camera_popups=False, cwd=config_dir)
    server.start()
    print(server.bound_port, flush=True)
    server.serve_forever()


class ServerProcess:
    def __init__(self, config_dir: Path, key: bytes, child: str = CHILD):
        self.config_dir, self.key, self.child = config_dir, key, child
        self.proc: subprocess.Popen | None = None
        self.port = 0

    def start(self) -> float:
        began = time.monotonic()
        self.proc = subprocess.Popen(
            [sys.executable, __file__, "--serve", str(self.config_dir), self.key.hex(),
             self.child],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=REPO)
        self.port = int(self.proc.stdout.readline())
        return time.monotonic() - began

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait(timeout=10)
            self.proc = None

    def sample(self) -> tuple[int, float]:
        """(RSS in KB, CPU seconds so far) from ps, portable to macOS and Linux."""
        out = subprocess.run(["ps", "-o", "rss=,time=", "-p", str(self.proc.pid)],
                             capture_output=True, text=True).stdout.split()
        rss = int(out[0])
        parts = [float(p) for p in out[1].replace("-", ":").split(":")]
        cpu = 0.0
        for p in parts:
            cpu = cpu * 60 + p
        return rss, cpu


# -- a dsterm view, counting what it is sent -----------------------------------


class ViewReader(threading.Thread):
    def __init__(self, path: Path):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(path))
        self.bytes = 0
        self.lines = {"top": 0, "bottom": 0, "status": 0}

    def run(self) -> None:
        buf = b""
        while True:
            try:
                data = self.sock.recv(1 << 20)
            except OSError:
                return
            if not data:
                return
            self.bytes += len(data)
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                kind = json.loads(raw).get("t")
                if kind in self.lines:
                    self.lines[kind] += 1


# -- the workload --------------------------------------------------------------


def percentile(values: list[float], p: float) -> float:
    values = sorted(values)
    k = (len(values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def typed_line_latencies(card: Client, seconds: float,
                         extra=None) -> tuple[list[float], int, float]:
    """Type a short unique line every 200 ms; time each until it is on screen.
    Returns the times in milliseconds, how many never arrived, and the
    first line's time, set aside."""
    waiting: dict[str, float] = {}
    done: list[float] = []
    end = time.monotonic() + seconds
    next_key = time.monotonic()
    n = 0
    while time.monotonic() < end or waiting:
        now = time.monotonic()
        if now >= next_key and now < end:
            n += 1
            token = f"k{n:05d}"
            waiting[token] = now
            card.send_keys(token.encode() + b"\r")
            # Jittered over one frame interval, so keys land at every point
            # between the server's ticks rather than in step with them.
            next_key = now + 0.2 + random.uniform(0, 1 / 15)
        if extra is not None:
            extra(now)
        card.poll(0.002)
        if waiting:
            text = card.grid.to_text()
            arrived = time.monotonic()
            for token in [t for t in waiting if t in text]:
                done.append((arrived - waiting.pop(token)) * 1000)
            for token in [t for t, at in waiting.items() if arrived - at > 3]:
                waiting.pop(token)                 # lost: counted below
        if time.monotonic() > end + 5:
            break
    # The first line on a connection is a warm-up (the child's first echo,
    # the first frames): reported apart, not in the distribution.
    return done[1:], n - len(done), (done[0] if done else 0.0)


# What a program can print that costs the most to emulate (tests/test_responsiveness.py
# holds a bound on each; this measures them).
FLOODS = (
    ("`ESC # 8` (fill the screen), without pause",
     "while :; do printf '\\033#8%.0s' $(seq 1 3000); done"),
    ("`CSI 99999 S` (scroll), without pause",
     "while :; do printf '\\033[99999S%.0s' $(seq 1 500); done"),
    ("`yes`", "exec yes 'a line of output, as fast as the pipe takes it'"),
)


def flood_pings(key: bytes, count: int = 40) -> list[tuple[str, list[float]]]:
    """PING round trips while the program floods the terminal."""
    results = []
    for label, child in FLOODS:
        server = ServerProcess(Path(tempfile.mkdtemp(dir="/tmp", prefix="dsf")), key, child)
        server.start()
        try:
            card = Client("127.0.0.1", server.port, key)
            card.connect()
            card.poll(0.5)
            times = []
            for _ in range(count):
                before = card.pongs
                sent = time.monotonic()
                card.send_ping()
                while card.pongs == before and time.monotonic() - sent < 10:
                    card.poll(0.002)
                times.append((time.monotonic() - sent) * 1000)
                time.sleep(random.uniform(0.01, 0.05))
            card.close()
            results.append((label, times))
        finally:
            server.stop()
    return results


def wait_for_screen(card: Client, needle: str, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        card.poll(0.01)
        if needle in card.grid.to_text():
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--soak", type=float, default=0.0, metavar="MINUTES",
                    help="run the combined workload this long and report memory over time")
    ap.add_argument("--serve", nargs=3, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.serve:
        serve(*args.serve)
        return 0

    config_dir = Path(tempfile.mkdtemp(dir="/tmp", prefix="dsm"))
    key = secrets.token_bytes(16)
    server = ServerProcess(config_dir, key)
    server.start()
    try:
        return run(args, config_dir, key, server)
    finally:
        server.stop()


def run(args, config_dir: Path, key: bytes, server: ServerProcess) -> int:

    # 1. Quiet: only the typing (the background output still runs: it is the
    #    child's, as a busy Claude session's would be).
    card = Client("127.0.0.1", server.port, key)
    card.connect()
    assert wait_for_screen(card, "output line")
    quiet, quiet_lost, quiet_first = typed_line_latencies(card, args.seconds / 2)
    card.close()

    # 2. Everything at once.
    card = Client("127.0.0.1", server.port, key, features=(FEATURE_CAMERA, FEATURE_MIRROR))
    card.connect()
    assert wait_for_screen(card, "output line")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (card.camera_ready is not None):
        card.poll(0.02)
    view = ViewReader(config_dir / "run" / "view.sock")
    view.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not card.mirror_watching:
        card.poll(0.02)
    card.send_camera(2)
    frame = bytes((i * 7) & 0xFF for i in range(PREVIEW_BYTES))
    cells = [[(65 + (r + c) % 26, 0, 0x7FFF, 0) for c in range(SUBSCREEN_COLS)]
             for r in range(SUBSCREEN_ROWS)]
    sub = encode_subscreen_payload(cells, 1)
    state = {"cam_at": 0.0, "sub_at": 0.0, "seq": 0, "frames": 0, "subs": 0}

    def background(now: float) -> None:
        if now - state["cam_at"] >= 1 / 3:
            state["cam_at"] = now
            state["seq"] = (state["seq"] + 1) & 0xFF
            card.send_preview(0, state["seq"], frame, fmt=PREVIEW_FORMAT_GRAY4)
            state["frames"] += 1
        if card.mirror_watching and now - state["sub_at"] >= 0.25:
            state["sub_at"] = now
            card.send_subscreen(sub)
            state["subs"] += 1

    rss0, cpu0 = server.sample()
    t0 = time.monotonic()
    rss_max = rss0
    samples: list[tuple[float, int]] = [(0.0, rss0)]
    sampler_stop = threading.Event()

    def sampler():
        nonlocal rss_max
        while not sampler_stop.wait(0.5):
            rss = server.sample()[0]
            rss_max = max(rss_max, rss)
            samples.append((time.monotonic() - t0, rss))
    threading.Thread(target=sampler, daemon=True).start()
    loaded_seconds = args.soak * 60 if args.soak else args.seconds
    loaded, loaded_lost, loaded_first = typed_line_latencies(card, loaded_seconds, extra=background)
    wall = time.monotonic() - t0
    sampler_stop.set()
    rss1, cpu1 = server.sample()
    card.send_camera(0)
    card.poll(0.2)

    # 3. Recovery: the card drops and comes back; then the server restarts.
    reconnects = []
    for _ in range(5):
        card.close()
        began = time.monotonic()
        card = Client("127.0.0.1", server.port, key)
        card.connect()
        if wait_for_screen(card, "output line"):
            reconnects.append((time.monotonic() - began) * 1000)
    card.close()
    restarts = []
    for _ in range(3):
        server.stop()
        began = time.monotonic()
        server.start()
        card = Client("127.0.0.1", server.port, key)
        card.connect()
        if wait_for_screen(card, "output line"):
            restarts.append((time.monotonic() - began) * 1000)
        card.close()
    server.stop()

    floods = flood_pings(key)

    def ms(values):
        return (f"{percentile(values, 0.5):.1f}", f"{percentile(values, 0.95):.1f}",
                f"{percentile(values, 0.99):.1f}", f"{max(values):.1f}")

    print(f"## Measured {time.strftime('%Y-%m-%d')} on {platform.system()} "
          f"{platform.machine()}, Python {platform.python_version()}\n")
    print("Typed line to on screen, in milliseconds (server's share; loopback):\n")
    print("| Workload | Lines | p50 | p95 | p99 | max | over 2 ticks | lost |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    slow = lambda xs: sum(1 for x in xs if x > 2000 / 15)  # noqa: E731
    print(f"| Typing, output streaming behind it | {len(quiet)} | "
          + " | ".join(ms(quiet)) + f" | {slow(quiet)} | {quiet_lost} |")
    print(f"| The same, plus camera 3 fps, bottom screen 4/s, `dsterm view` watching | "
          f"{len(loaded)} | " + " | ".join(ms(loaded)) + f" | {slow(loaded)} | {loaded_lost} |")
    print(f"\nThe first line of each connection, set aside as a warm-up: "
          f"{quiet_first:.0f} and {loaded_first:.0f} ms.")
    print()
    print("The server process during the combined run:\n")
    print("| Measure | Value |")
    print("|---|---:|")
    print(f"| CPU | {100 * (cpu1 - cpu0) / wall:.1f}% of one core |")
    print(f"| Memory (RSS) | {rss0 / 1024:.1f} MB at start, {rss_max / 1024:.1f} MB at most |")
    print(f"| Camera frames in | {state['frames'] / wall:.1f}/s, "
          f"{state['frames'] * (3 + PREVIEW_BYTES) / wall / 1024:.1f} KB/s |")
    print(f"| Bottom screens in | {state['subs'] / wall:.1f}/s, "
          f"{state['subs'] * len(sub) / wall / 1024:.1f} KB/s |")
    print(f"| To `dsterm view` | {view.bytes / wall / 1024:.1f} KB/s "
          f"({view.lines['top'] / wall:.1f} top, {view.lines['bottom'] / wall:.1f} bottom "
          f"screens a second) |")
    print()
    if args.soak:
        print(f"Memory over the {args.soak:.0f}-minute combined run (RSS, MB):\n")
        print("| Minute | RSS |")
        print("|---:|---:|")
        step = max(1, len(samples) // 10)
        for t, rss in samples[::step] + [samples[-1]]:
            print(f"| {t / 60:.1f} | {rss / 1024:.1f} |")
        first = [r for t, r in samples if t >= 60][:1] or [samples[0][1]]
        print(f"\nFrom the end of the first minute to the end: "
              f"{(samples[-1][1] - first[0]) / 1024:+.1f} MB.\n")
    print("A PING while the program floods the terminal, in milliseconds:\n")
    print("| Output | PINGs | median | max |")
    print("|---|---:|---:|---:|")
    for label, times in floods:
        print(f"| {label} | {len(times)} | {statistics.median(times):.0f} | {max(times):.0f} |")
    print()
    print("Recovery, until the card has its screen again, in milliseconds:\n")
    print("| Event | Runs | median | max |")
    print("|---|---:|---:|---:|")
    print(f"| The card drops and reconnects | {len(reconnects)} | "
          f"{statistics.median(reconnects):.0f} | {max(reconnects):.0f} |")
    print(f"| The server restarts (process start to screen) | {len(restarts)} | "
          f"{statistics.median(restarts):.0f} | {max(restarts):.0f} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
