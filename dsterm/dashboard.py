"""A read-only status page, on loopback only.

Most of this project's hard debugging needed two views at once: what the
handheld believed, and what the server believed. There was no way to see both,
so three wrong diagnoses came from reasoning about one while guessing at the
other. This shows the server's half -- counters, recent events, and the screen
the DS should be displaying.

Two rules hold it in place.

It binds 127.0.0.1 and refuses anything else. Not a default that can be
overridden: a refusal. The entire security posture of this project is that the
handheld gets the PTY and nothing else, reached only by proving it holds the
pairing key. A status page reachable from the LAN would be a second way to read
the screen with no key at all -- strictly worse than the thing the protocol was
designed to prevent.

And nothing here writes to the PTY. There is no endpoint that sends a keystroke,
because that would be a client that skipped the handshake. Restarting the child
is the single mutation, and it destroys a session rather than driving one.

Loopback is not the whole of it, because a browser on this machine is on
loopback too. Every request must name a loopback Host -- so a web page that
rebinds its own domain to 127.0.0.1 is answered with nothing -- and the restart
needs a header that only this page sends: a form or a plain fetch from another
site cannot set it without asking first, and nothing here answers that asking.
The restart itself is handed to the server's own loop rather than run on this
thread, where it would race the loop over the same session.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

from .grid import COLS, ROWS

if TYPE_CHECKING:
    from .server import Server

# The entire user interface, in one file that knows nothing about this one
# beyond the shape of /api/state. Kept separate so it can be edited -- by a
# person or an agent -- without touching anything that could affect the
# protocol, the session, or the handheld.
PAGE_PATH = Path(__file__).resolve().parent / "dashboard.html"


def read_page() -> bytes:
    """Read the page fresh on every request.

    Deliberately not cached at import. A status page is worked on by looking at
    it, and re-reading costs a few microseconds on a local request while turning
    the edit loop into "save, refresh" instead of "save, restart the service,
    wait for the child, refresh".
    """
    return PAGE_PATH.read_bytes()

# Loopback only. IPv6 loopback is included because a host may resolve
# "localhost" to ::1 first, and refusing it would look like the page is down.
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})

# The header the page's own restart button sends (see the module docstring).
RESTART_HEADER = "X-Dsterm-Restart"


def host_is_loopback(host_header: str | None) -> bool:
    """Whether a request's Host names this machine's loopback, port aside."""
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):                       # [::1]:8348
        host = host[1:host.find("]")] if "]" in host else host
    elif host.count(":") == 1:                     # 127.0.0.1:8348
        host = host.rsplit(":", 1)[0]
    return host in LOOPBACK

# Keyed by the colour names Cell already carries, so this cannot drift out of
# step with the palette the wire and the DS agree on. Chosen to sit close to
# what the handheld's own palette renders, since the point of the preview is to
# match what is physically on the screen.
_FG = {
    "default": "#c9c9c9", "black": "#000000", "red": "#d24b4b",
    "green": "#4fb84f", "yellow": "#d6c14a", "blue": "#5a7fe0",
    "magenta": "#c95fc9", "cyan": "#48c3cc", "white": "#ffffff",
}
# "default" background is the terminal background, not the default foreground,
# so the two tables cannot be one table.
_BG = dict(_FG, default="#000000")


class NotLoopback(ValueError):
    """Raised when asked to serve the dashboard to anything but this machine."""


def cells_to_json(grid) -> list[list[dict]]:
    """The screen as rows of {glyph, fg, bg, bold, reverse}."""
    out = []
    for r in range(ROWS):
        row = []
        for c in range(COLS):
            cell = grid.cells[r * COLS + c]
            row.append({
                "ch": cell.char,
                "fg": _FG.get(cell.fg, _FG["default"]),
                "bg": _BG.get(cell.bg, _BG["default"]),
                "b": bool(cell.bold),
                "r": bool(cell.reverse),
            })
        out.append(row)
    return out


def snapshot(server: "Server") -> dict:
    """Everything the page shows, as plain data."""
    client = None
    for c in server._clients:
        if c.authed:
            client = c
            break

    session = server._session
    grid = session.grid if session is not None else None

    view = server.camera_view
    return {
        "now": time.time(),
        "started_at": server.started_at,
        "listening": f"{server.host}:{server.port}",
        # Status only -- the pixels never come over HTTP. They leave the
        # server on a socket only this user can open; a loopback page that a
        # browser can drive is not where a camera frame belongs.
        "camera": {"on": view.on,
                   "camera": {1: "inner", 2: "outer"}.get(view.camera)},
        "client": {
            "addr": f"{client.addr[0]}:{client.addr[1]}" if client else None,
            "connected_at": client.connected_at if client else None,
            "queued": len(client.outbox) if client else 0,
        },
        "pending": len(server._clients),
        # The page draws the screen itself and so needs the geometry. Sent
        # rather than duplicated in the JavaScript, because the grid is no
        # longer a constant: a second copy of 42x24 would silently draw the
        # wrong shape the first time someone zooms the handheld out.
        "geometry": {"cols": COLS, "rows": ROWS},
        "child": {
            "alive": bool(session is not None and session.alive()),
            "command": server.command,
            "scrollback_lines": session.scrollback_lines if session is not None else 0,
        },
        "counters": vars(server.counters).copy(),
        "events": [{"t": t, "msg": m} for t, m in list(server.events)[-60:]],
        "screen": cells_to_json(grid) if grid is not None else None,
    }


def _handler(server: "Server"):
    class Handler(BaseHTTPRequestHandler):
        # Quiet: every poll would otherwise print a line and bury the server's
        # own events in the journal.
        def log_message(self, *args):
            pass

        def _json(self, code: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _from_loopback(self) -> bool:
            if host_is_loopback(self.headers.get("Host")):
                return True
            self._json(403, {"error": "this page answers to localhost only"})
            return False

        def do_GET(self):
            if not self._from_loopback():
                return
            if self.path == "/":
                raw = read_page()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif self.path == "/api/state":
                self._json(200, snapshot(server))
            else:
                self._json(404, {"error": "no such path"})

        def do_POST(self):
            if not self._from_loopback():
                return
            if self.path == "/api/restart-child":
                if self.headers.get(RESTART_HEADER) != "1":
                    self._json(403, {"error": f"missing {RESTART_HEADER}"})
                    return
                server.request_restart()
                self._json(202, {"ok": True})
            else:
                self._json(404, {"error": "no such path"})

    return Handler


def start(server: "Server", port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Serve the dashboard on a daemon thread. Loopback addresses only."""
    if host not in LOOPBACK:
        raise NotLoopback(
            f"the dashboard binds loopback only, not {host!r}: it shows the "
            f"screen with no authentication, so exposing it would bypass the "
            f"pairing key entirely"
        )
    httpd = ThreadingHTTPServer((host, port), _handler(server))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
