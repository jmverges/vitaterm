"""The server's end of `dsterm view`: both screens out, keys and taps in.

`dsterm view` draws the DS on this computer: the Claude session as the top
screen, the card's own bottom screen (which the card sends as text while a
view is watching -- protocol.py's SUBSCREEN), and the camera's live view when
it is on. It connects to a Unix socket, <config_dir>/run/view.sock, mode
0600 in a 0700 directory, with the peer's uid checked where the platform can
(Linux); only this user can open it, the same people who can already
`tmux attach` to the session, so typing through it is no new power.

One JSON object a line in both directions, every line bounded:

    server -> view   {"t": "top", "rows": [[run, ...], ...], "cursor": [r, c, vis],
                      "back": lines the DS is scrolled back, 0 at the live screen}
                     {"t": "bottom", "taps": bool, "rows": [[run, ...], ...] | null}
                     {"t": "status", "card": bool, "watching": int, ...}
    view -> server   {"t": "keys", "b64": "..."}   bytes for the session
                     {"t": "tap", "x": 0..255, "y": 0..191}   a tap on the card

A run is [text, fg, bg, flags]: consecutive cells that look the same, so a
line of text is one entry, not forty-two. Top colours are the palette's names
("default", "orange" ...); bottom colours are "#rrggbb", as the card drew
them. flags: 1 bold, 2 reverse, 4 the camera's viewfinder is under it.
A bottom with null rows: the card has gone, and its last screen with it.

Nothing here touches the card except through the server, which decides
whether a tap goes out (the card only asked for MIRROR, and acts on a TOUCH
only with its own VIEW TAPS setting on).
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import time
from dataclasses import dataclass, field

VIEW_MAX_CLIENTS = 4
VIEW_LINE_MAX = 64 * 1024        # one incoming line, at most
VIEW_KEYS_MAX = 4096             # bytes of keys in one message
VIEW_PENDING_LIMIT = 1024 * 1024
VIEW_STUCK_SECONDS = 5.0
VIEW_TAPS_PER_SECOND = 20
VIEW_SOCK_MAX = 100              # AF_UNIX path limit, with room


@dataclass
class ViewClient:
    sock: socket.socket
    inbox: bytearray = field(default_factory=bytearray)
    pending: bytearray = field(default_factory=bytearray)
    pending_since: float = 0.0
    taps: list = field(default_factory=list)


def top_runs(grid) -> list[list[list]]:
    """A Grid (grid.py) as rows of runs."""
    rows = []
    for row in grid.rows():
        runs: list[list] = []
        for cell in row:
            flags = (1 if cell.bold else 0) | (2 if cell.reverse else 0)
            if runs and runs[-1][1] == cell.fg and runs[-1][2] == cell.bg \
                    and runs[-1][3] == flags:
                runs[-1][0] += cell.char
            else:
                runs.append([cell.char, cell.fg, cell.bg, flags])
        rows.append(runs)
    return rows


def bottom_runs(sub) -> list[list[list]]:
    """A SubScreen (protocol.py) as rows of runs, colours as #rrggbb."""
    from .protocol import rgb15_to_rgb

    def hexcol(v: int) -> str:
        r, g, b = rgb15_to_rgb(v)
        return f"#{r:02x}{g:02x}{b:02x}"

    rows = []
    for row in sub.cells:
        runs: list[list] = []
        for cell in row:
            fg, bg = hexcol(cell.fg), hexcol(cell.bg)
            flags = 4 if cell.camera else 0
            if runs and runs[-1][1] == fg and runs[-1][2] == bg and runs[-1][3] == flags:
                runs[-1][0] += cell.char
            else:
                runs.append([cell.char, fg, bg, flags])
        rows.append(runs)
    return rows


def line(obj: dict) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


class ViewHub:
    """The socket and its clients. The server owns the selector and calls in:
    accept() when the listening socket is readable, read()/write() for a
    client, and on_keys/on_tap are how input reaches the server."""

    def __init__(self, config_dir, on_keys, on_tap, on_count, emit,
                 register, modify, unregister):
        self.path = config_dir / "run" / "view.sock"
        self.sock: socket.socket | None = None
        self.bound = False
        self.clients: list[ViewClient] = []
        self._on_keys = on_keys
        self._on_tap = on_tap
        self._on_count = on_count
        self._emit = emit
        self._register = register
        self._modify = modify
        self._unregister = unregister
        self.last_top: bytes | None = None
        self.last_bottom: bytes | None = None
        self.last_status: bytes | None = None

    # -- the socket ------------------------------------------------------

    def open(self) -> bool:
        text = str(self.path)
        if len(text.encode()) > VIEW_SOCK_MAX:
            self._emit(f"view off: {len(text)}-byte socket path")
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if os.lstat(self.path.parent).st_mode & 0o077:
                os.chmod(self.path.parent, 0o700)
        except OSError as exc:
            self._emit(f"view off: {exc}")
            return False
        if os.path.lexists(self.path):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(text)
            except OSError:
                try:
                    os.unlink(self.path)
                except OSError as exc:
                    self._emit(f"view off: cannot clear {text}: {exc}")
                    return False
            else:
                self._emit(f"view off: {text} is a live socket")
                return False
            finally:
                probe.close()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            old = os.umask(0o177)
            try:
                sock.bind(text)
            finally:
                os.umask(old)
            os.chmod(self.path, 0o600)
            sock.listen(VIEW_MAX_CLIENTS)
            sock.setblocking(False)
        except OSError as exc:
            sock.close()
            self._emit(f"view off: cannot bind {text}: {exc}")
            return False
        self.sock = sock
        self.bound = True
        self._register(sock, "view-hub")
        return True

    def close(self) -> None:
        for c in list(self.clients):
            self.drop(c)
        if self.sock is not None:
            self._unregister(self.sock)
            self.sock.close()
            self.sock = None
        if self.bound:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self.bound = False

    def accept(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except (BlockingIOError, OSError):
            return
        conn.setblocking(False)
        if sys.platform.startswith("linux"):
            try:
                creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                uid = struct.unpack("3i", creds)[1]
            except OSError:
                uid = -1
            if uid != os.getuid():
                conn.close()
                return
        if len(self.clients) >= VIEW_MAX_CLIENTS:
            conn.close()
            return
        c = ViewClient(conn)
        self.clients.append(c)
        self._register(conn, c)
        # Counted before anything is sent: a send that finds the view already
        # gone drops it, and the count must have gone up first for the drop
        # to bring it down -- one "watching" for every "none".
        self._on_count(len(self.clients))
        for msg in (self.last_status, self.last_top, self.last_bottom):
            if msg is not None and c in self.clients:
                self.send(c, msg)

    def drop(self, c: ViewClient) -> None:
        if c not in self.clients:
            return
        self.clients.remove(c)
        self._unregister(c.sock)
        try:
            c.sock.close()
        except OSError:
            pass
        self._on_count(len(self.clients))

    # -- out ---------------------------------------------------------------

    def send(self, c: ViewClient, msg: bytes) -> None:
        if c.pending:
            # Behind: the newest whole screen will be sent when it catches up.
            if len(c.pending) + len(msg) > VIEW_PENDING_LIMIT:
                self.drop(c)
                return
            c.pending += msg
            return
        try:
            sent = c.sock.send(msg)
        except (BlockingIOError, InterruptedError):
            sent = 0
        except OSError:
            self.drop(c)
            return
        if sent < len(msg):
            c.pending += msg[sent:]
            c.pending_since = time.monotonic()
            self._modify(c.sock, True, c)

    def broadcast(self, kind: str, msg: bytes) -> None:
        if kind == "top":
            self.last_top = msg
        elif kind == "bottom":
            self.last_bottom = msg
        elif kind == "status":
            self.last_status = msg
        for c in list(self.clients):
            self.send(c, msg)

    def write(self, c: ViewClient) -> None:
        try:
            sent = c.sock.send(c.pending)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.drop(c)
            return
        del c.pending[:sent]
        if not c.pending:
            self._modify(c.sock, False, c)

    def housekeeping(self) -> None:
        now = time.monotonic()
        for c in list(self.clients):
            if c.pending and now - c.pending_since > VIEW_STUCK_SECONDS:
                self.drop(c)

    # -- in ----------------------------------------------------------------

    def read(self, c: ViewClient) -> None:
        try:
            data = c.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.drop(c)
            return
        if not data:
            self.drop(c)
            return
        c.inbox += data
        while True:
            nl = c.inbox.find(b"\n")
            # The limit holds for every line, ended or not: a whole 70 KB line
            # arriving in one read is as far over it as a partial one.
            if (nl if nl >= 0 else len(c.inbox)) > VIEW_LINE_MAX:
                self.drop(c)
                return
            if nl < 0:
                return
            raw = bytes(c.inbox[:nl])
            del c.inbox[:nl + 1]
            try:
                ok = self.handle(c, raw)
            except Exception as exc:          # noqa: BLE001 -- see below
                # Whatever a view's line sets off -- a parser limit, or a bug
                # in what it reached -- costs that view, never the server
                # every other view and the card depend on.
                self._emit(f"view dropped: {type(exc).__name__}: {exc}")
                ok = False
            if not ok:
                self.drop(c)
                return

    def handle(self, c: ViewClient, raw: bytes) -> bool:
        """One line from a view. False ends that view: anything malformed."""
        try:
            msg = json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            # RecursionError is not a ValueError: a few thousand "[" nest
            # deeper than the parser will go.
            return False
        if not isinstance(msg, dict):
            return False
        kind = msg.get("t")
        if kind == "keys":
            try:
                data = base64.b64decode(msg.get("b64", ""), validate=True)
            except (ValueError, TypeError):
                return False
            if not data or len(data) > VIEW_KEYS_MAX:
                return False
            self._on_keys(data)
            return True
        if kind == "tap":
            x, y = msg.get("x"), msg.get("y")
            if not (isinstance(x, int) and isinstance(y, int)
                    and 0 <= x <= 255 and 0 <= y <= 191):
                return False
            now = time.monotonic()
            c.taps = [t for t in c.taps if now - t < 1.0]
            if len(c.taps) >= VIEW_TAPS_PER_SECOND:
                return True            # too many: this one is dropped
            c.taps.append(now)
            self._on_tap(x, y)
            return True
        return kind == "ping"
