"""`dsterm view`: telling a card whether a view is watching, and what the view
hub's keys, taps and screens do.

Part of the server, split out by feature: the methods below are
mixed into Server (server.py) and share its state through `self`.
"""

from __future__ import annotations

import selectors
import time

from . import view as dsview
from .protocol import (
    encode_mirror,
    encode_touch,
    Message,
    ProtocolError,
    decode_subscreen,
)
from .connection import ClientState


class MirrorMixin:
    """Owns the view hub (view.py) and the card's last bottom screen
    (_init_mirror); per connection, ClientState.mirror_told."""

    def _tell_mirror(self, c: ClientState) -> None:
        """Tell a card that announced "mirror" whether a view is watching, when
        that has changed since it was last told."""
        if not c.caps.mirror or not c.authed or self._view is None:
            return
        watching = bool(self._view.clients)
        if c.mirror_told == watching:
            return
        c.mirror_told = watching
        self._send_to(c, encode_mirror(watching))

    def _view_count(self, count: int) -> None:
        self._emit(f"view: {count} watching" if count else "view: none watching")
        c = self._authed()
        if c is not None:
            self._tell_mirror(c)
        self._view_top_sent = None          # a new view gets the whole screen
        self._view_status_at = 0.0

    def _view_keys(self, data: bytes) -> None:
        """Typing in `dsterm view`: straight to the session, as `tmux attach`
        would. The socket is this user's only (view.py)."""
        if self._session is not None and not self._session.write(data):
            self._emit("input dropped: the session has stopped reading "
                       "what is typed")

    def _view_tap(self, x: int, y: int) -> None:
        """A click on the card's bottom screen in `dsterm view`. The card
        acts on it only with its own VIEW TAPS setting on."""
        c = self._authed()
        if c is None or not c.caps.mirror:
            return
        self._send_to(c, encode_touch(x, y))

    def _view_tick(self, live) -> None:
        if self._view is None:
            return
        self._view.housekeeping()
        if not self._view.clients:
            return
        now = time.monotonic()
        if now - self._view_status_at >= 1.0:
            self._view_status_at = now
            c = self._authed()
            self._view.broadcast("status", dsview.line({
                "t": "status", "card": c is not None,
                "mirror": bool(c is not None and c.caps.mirror),
                "taps": bool(self.subscreen is not None and self.subscreen.taps),
                "camera": bool(self.camera_view.on),
                "watching": len(self._view.clients)}))
        # The top screen as the DS shows it: scrolled back when the card is,
        # its caret hidden then, as on the card (_resync_messages).
        c = self._authed()
        back = self._lines_back(c) if c is not None and self._session is not None else 0
        if back:
            grid, cur = self._view_for(c), (0, 0, False)
        else:
            grid = live
            cur = self._session.cursor if self._session is not None else (0, 0, False)
        key = (grid.cells, cur, back)
        if key != self._view_top_sent and now - self._view_top_at >= 1 / 15:
            self._view_top_sent = key
            self._view_top_at = now
            self._view.broadcast("top", dsview.line({
                "t": "top", "rows": dsview.top_runs(grid),
                "cursor": [cur[0], cur[1], bool(cur[2])], "back": back}))

    def _on_subscreen(self, c: ClientState, msg: Message) -> None:
        """SUBSCREEN: the card's bottom screen, for `dsterm view`. It sends one
        only after MIRROR, which it only saw because it announced "mirror"."""
        # The card's bottom screen, for `dsterm view`. It sends one only
        # after MIRROR, which it only saw because it announced "mirror".
        if not c.caps.mirror:
            self._drop(c, "unexpected message SUBSCREEN "
                          "(mirror was not negotiated)")
            return
        try:
            self.subscreen = decode_subscreen(msg.payload)
        except ProtocolError as exc:
            self._drop(c, f"bad subscreen: {exc}")
            return
        if self._view is not None and self._view.clients:
            self._view.broadcast("bottom", dsview.line(
                {"t": "bottom", "taps": self.subscreen.taps,
                 "rows": dsview.bottom_runs(self.subscreen)}))

    # -- what the core calls (server.py) -------------------------------------

    def _init_mirror(self) -> None:
        """`dsterm view`'s state on the server."""
        # `dsterm view` (view.py): its socket, the card's bottom screen as it
        # last sent it, and the top screen as last sent to the views.
        self._view: dsview.ViewHub | None = None
        self.subscreen = None
        self._view_top_sent = None
        self._view_top_at = 0.0
        self._view_status_at = 0.0

    def _start_mirror(self) -> None:
        self._view = dsview.ViewHub(
            self.config_dir, on_keys=self._view_keys, on_tap=self._view_tap,
            on_count=self._view_count, emit=self._emit,
            register=lambda sock, data: self._sel.register(
                sock, selectors.EVENT_READ, data),
            modify=lambda sock, writing, data: self._sel.modify(
                sock, selectors.EVENT_READ | (selectors.EVENT_WRITE if writing else 0),
                data),
            unregister=self._sel_forget)
        self._view.open()

    def _stop_mirror(self) -> None:
        if self._view is not None:
            self._view.close()

    def _mirror_event(self, key, mask: int) -> None:
        """The hub's listening socket, or one view's, is ready."""
        if key == "view-hub":
            self._view.accept()  # type: ignore[union-attr]
            return
        if mask & selectors.EVENT_WRITE:
            self._view.write(key)  # type: ignore[union-attr]
        if key in self._view.clients and mask & selectors.EVENT_READ:  # type: ignore[union-attr]
            self._view.read(key)  # type: ignore[union-attr]

    def _mirror_card_gone(self) -> None:
        """An authenticated card's connection ended: its bottom screen goes
        with it, rather than staying up stale."""
        if self.subscreen is not None:
            self.subscreen = None
            if self._view is not None:
                self._view.broadcast("bottom", dsview.line(
                    {"t": "bottom", "taps": False, "rows": None}))
