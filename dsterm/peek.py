"""Show what the handheld should be displaying.

    dsterm screen                        # this computer's server
    dsterm screen 192.0.2.10             # another server, over the LAN

On this computer it reads the screen from the server's own read-only
dashboard (127.0.0.1:8348), which is a look, not a connection: the DS stays
connected while you look. The protocol allows one handheld at a time, so the
fallback -- connecting as a client, which is the only way to reach another
machine's server -- takes the DS's place until it reconnects, and says so.
"""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DASHBOARD = "http://127.0.0.1:8348/api/state"
FEED_SOCK = Path.home() / ".config" / "ds-claude-terminal" / "run" / "camera.sock"
LOCAL = ("127.0.0.1", "localhost", "::1")


def feed_still(path: "Path | str" = FEED_SOCK, timeout: float = 1.0,
               cols: int = 42, rows: int = 12) -> list[str] | None:
    """One camera frame from the feed socket, as half-block lines -- or None,
    which is the ordinary case (no server, no feed, camera off)."""
    from .camera import FeedReader, half_blocks
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(path))
        reader = FeedReader()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = sock.recv(65536)
            if not data:
                break
            for header, rgb in reader.feed(data):
                if header.get("on") and rgb:
                    return half_blocks(rgb, int(header["w"]), int(header["h"]),
                                       cols, rows)
                return None
    except (OSError, ValueError):
        return None
    finally:
        try:
            sock.close()
        except (OSError, UnboundLocalError):
            pass
    return None


def from_dashboard(url: str = DASHBOARD, timeout: float = 2.0) -> str | None:
    """The screen as framed text, or None if no dashboard answers."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            state = json.load(resp)
    except (OSError, urllib.error.URLError, ValueError):
        return None
    screen = state.get("screen")
    geometry = state.get("geometry") or {}
    cols = int(geometry.get("cols") or (len(screen[0]) if screen else 42))
    if not screen:
        return "the server is running, but no session is on screen yet"
    rows = ["".join(cell.get("ch") or " " for cell in row)[:cols].ljust(cols)
            for row in screen]
    title = f"{state.get('listening', 'dsterm')}  (from the dashboard)"
    bar = "-" * cols
    lines = [f"+-{bar}-+", f"| {title.ljust(cols)[:cols]} |", f"+-{bar}-+"]
    lines += [f"| {row} |" for row in rows]
    lines.append(f"+-{bar}-+")
    client = (state.get("client") or {}).get("addr")
    lines.append(f"handheld: {client or 'none connected'}")
    camera = state.get("camera") or {}
    if camera.get("on"):
        name = camera.get("camera") or "?"
        lines.append(f"camera: on ({name}) -- dsterm camera shows it")
        still = feed_still()
        if still:
            lines += still
    return "\n".join(lines)


def settle(c, seconds: float = 3.0, quiet: float = 0.5) -> None:
    """Read until the screen has stopped changing for `quiet`.

    Client.poll() returns as soon as it has handled anything, which was enough
    when a whole screen arrived in one message. The server now releases updates
    a couple per tick, so a single poll() sees a fraction of the screen and this
    tool would print a half-drawn frame.
    """
    deadline = time.monotonic() + seconds
    last_change = time.monotonic()
    seen = c.grid.to_text()
    while time.monotonic() < deadline:
        c.poll(0.1)
        now = c.grid.to_text()
        if now != seen:
            seen, last_change = now, time.monotonic()
        elif time.monotonic() - last_change >= quiet:
            return


def as_client(host: str, port: int, keys: str | None) -> int:
    # Imported here: the dashboard path needs none of it, and the grid's size
    # is read when dsterm.grid is first imported (cli.cmd_screen applies
    # --grid before that).
    from dsterm.auth import load_key
    from dsterm.client import Client
    from dsterm.protocol import ProtocolError
    from dsterm.render import framed

    try:
        key = load_key()
    except (FileNotFoundError, PermissionError) as exc:
        print(f"dsterm screen: {exc}", file=sys.stderr)
        return 1
    print(f"connecting to {host}:{port} as a handheld -- a connected DS is "
          "replaced until it reconnects (a second or two) ...")
    try:
        with Client(host, port, key) as c:
            settle(c)
            if keys:
                c.send_keys(keys.encode().decode("unicode_escape").encode())
                settle(c, seconds=6.0)
            print()
            print(framed(c.grid, f"{host}:{port}"))
            print()
            print(f"frames applied: {c.frames_applied}   scrolls: {c.scrolls_applied}")
    except ProtocolError as exc:
        print(f"dsterm screen: {exc}\n(if that server runs another grid size, "
              "pass the same --grid it uses, e.g. dsterm screen --grid 64x32 HOST)",
              file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"dsterm screen: cannot reach {host}:{port} ({exc.strerror or exc})",
              file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    from dsterm.server import DEFAULT_PORT

    host = argv[0] if argv else "127.0.0.1"
    port = int(argv[1]) if len(argv) > 1 else DEFAULT_PORT
    keys = argv[2] if len(argv) > 2 else None
    if host in LOCAL and keys is None:
        text = from_dashboard()
        if text is not None:
            print(text)
            return 0
    return as_client(host, port, keys)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
