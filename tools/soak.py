#!/usr/bin/env python3
"""Watch a running server through a long session with a real DS, and report.

    .venv/bin/python tools/soak.py --hours 4            # then use the DS as usual
    .venv/bin/python tools/soak.py --hours 4 --out soak.md

Reads the dashboard's /api/state (loopback, read-only) every ten seconds and the
server process's memory and CPU from ps, and keeps every event the server
logs. Use the DS the way you would: type, scroll, close the lid and open it
again, walk out of range, open the camera, send a picture. At the end -- or
on Ctrl-C -- it writes what happened: how many times the card connected and
why each connection ended, how long it took to come back, what the camera
did, and the server's memory hour by hour.

It changes nothing and needs no key: the dashboard is the server's own
read-only page. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import sys
import time
import urllib.request
from collections import Counter

DASHBOARD = "http://127.0.0.1:8348/api/state"


def state() -> dict | None:
    try:
        with urllib.request.urlopen(DASHBOARD, timeout=3) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def server_pid() -> int | None:
    out = subprocess.run(["lsof", "-ti", "tcp:8347", "-sTCP:LISTEN"],
                         capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def ps(pid: int) -> tuple[int, float] | None:
    out = subprocess.run(["ps", "-o", "rss=,time=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.split()
    if len(out) < 2:
        return None
    cpu = 0.0
    for part in out[1].replace("-", ":").split(":"):
        cpu = cpu * 60 + float(part)
    return int(out[0]), cpu


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--every", type=float, default=10.0,
                    help="seconds between samples; the dashboard keeps its last 60 events")
    ap.add_argument("--out", help="also write the report to this file")
    args = ap.parse_args()

    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("now", True))
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("now", True))

    began = time.time()
    end = began + args.hours * 3600
    events: dict[float, str] = {}
    samples: list[tuple[float, int, float, bool]] = []   # t, rss KB, cpu s, card up
    first_counters = last_counters = None
    pid = server_pid()
    while not stop["now"] and time.time() < end:
        st = state()
        if st is not None:
            for e in st.get("events", []):
                events[e["t"]] = e["msg"]
            last_counters = st.get("counters")
            first_counters = first_counters or last_counters
            pid = pid or server_pid()
            usage = ps(pid) if pid else None
            if usage:
                card_up = bool((st.get("client") or {}).get("addr"))
                samples.append((time.time(), usage[0], usage[1], card_up))
        deadline = time.time() + args.every
        while not stop["now"] and time.time() < deadline:
            time.sleep(1)

    report = render(began, events, samples, first_counters, last_counters)
    print(report)
    if args.out:
        with open(args.out, "w") as f:
            f.write(report)
    return 0


DROP = re.compile(r"dropped: (.*)")
CLIENT = re.compile(r"client (\(.*?\))")


def render(began, events, samples, first, last) -> str:
    lines = []
    hours = (time.time() - began) / 3600
    lines.append(f"## Soak: {hours:.1f} hours with a real DS\n")
    ordered = sorted(events.items())
    auths = [t for t, m in ordered if "authenticated" in m]
    # Only a card's connections: one that logged in and then ended. A peer
    # that connected and left without authenticating is not the DS dropping.
    authed_addrs = {CLIENT.search(m).group(1) for _, m in ordered
                    if "authenticated" in m and CLIENT.search(m)}
    drops = [(t, DROP.search(m).group(1)) for t, m in ordered
             if DROP.search(m) and CLIENT.search(m)
             and CLIENT.search(m).group(1) in authed_addrs]
    why = Counter(re.sub(r"\(.*?\)|\d+", "", reason).strip() for _, reason in drops)
    # Time back: from each drop of an authenticated card to the next auth.
    back = []
    for t, _ in drops:
        nxt = [a for a in auths if a > t]
        if nxt:
            back.append(nxt[0] - t)
    camera_on = sum(1 for _, m in ordered if m.startswith("camera on"))
    saved = sum(1 for _, m in ordered if m.startswith("picture saved"))
    lines.append("| Measure | Value |")
    lines.append("|---|---:|")
    lines.append(f"| Card logins | {len(auths)} |")
    lines.append(f"| Connections ended | {len(drops)} |")
    if back:
        back.sort()
        lines.append(f"| Back after a drop (median, max) | {back[len(back) // 2]:.0f} s, {back[-1]:.0f} s |")
    lines.append(f"| Camera opened | {camera_on} |")
    lines.append(f"| Pictures saved | {saved} |")
    if first and last:
        for key in ("bytes_out", "bytes_in", "resyncs", "child_restarts"):
            lines.append(f"| {key.replace('_', ' ')} | {last[key] - first[key]:,} |")
    if samples:
        up = sum(1 for s in samples if s[3])
        minutes = up * (samples[-1][0] - samples[0][0]) / max(1, len(samples) - 1) / 60
        lines.append(f"| Card connected | {minutes:.0f} min ({100 * up / len(samples):.1f}% of samples) |")
        lines.append(f"| CPU | {100 * (samples[-1][2] - samples[0][2]) / max(1, samples[-1][0] - samples[0][0]):.2f}% of one core |")
    lines.append("")
    if why:
        lines.append("Why connections ended:\n")
        for reason, n in why.most_common():
            lines.append(f"- {n} x {reason}")
        lines.append("")
    if samples:
        lines.append("Server memory (RSS):\n")
        lines.append("| Hour | MB |")
        lines.append("|---:|---:|")
        t0 = samples[0][0]
        step = max(1, len(samples) // 12)
        for t, rss, _, _ in samples[::step] + [samples[-1]]:
            lines.append(f"| {(t - t0) / 3600:.1f} | {rss / 1024:.1f} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
