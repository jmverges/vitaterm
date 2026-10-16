"""Scrolling back: a card's view offset into history, the worker that captures
it from tmux, and the VIEW_POS it is told.

Part of the server, split out by feature: the methods below are
mixed into Server (server.py) and share its state through `self`.
"""

from __future__ import annotations

import queue
import threading
import time

from .grid import (
    ROWS,
    Grid,
)
from .protocol import (
    encode_view_pos,
    Message,
    ProtocolError,
    decode_view,
)
from .session import HISTORY_LINES
from .connection import (
    ClientState,
    log,
)


# A live reader cannot act on a moving history count every frame. It still
# needs timely evidence that scrolling works, so report periodically or after
# one screen of newly available history.
LIVE_VIEW_REPORT_SECONDS = 1.0


class ScrollbackMixin:
    """Owns the capture worker and its queues (_init_scrollback); per
    connection, ClientState.scroll."""

    # -- view --------------------------------------------------------------

    def _view_to_live(self, c: ClientState) -> bool:
        """Return this client to the live screen. True if it was scrolled back.

        Always bumps the generation: a capture already in flight belongs to a
        view the user has left, and must not land. The pending sum is dropped
        with it -- those presses asked for a view the user has left. The
        in-flight flag stays: the old job is still outstanding and only its
        result frees the gate.
        """
        c.scroll.gen += 1
        c.scroll.anchor = None
        c.scroll.grid = None
        c.scroll.pending = 0
        if not c.scroll.offset:
            return False
        c.scroll.offset = 0
        return True

    def _scroll_view(self, c: ClientState, delta: int) -> None:
        """Move this client's view through the history.

        Negative is back. Clamped to what exists, because a client asking to go
        further than the history reaches means "as far as you can" -- there is
        nothing useful to refuse. Through tmux the capture itself is a
        subprocess, so the request is queued to the view worker and the answer
        arrives on a later tick; the select loop never waits on tmux.
        """
        if self._session is None:
            return

        if self._session.multiplexed:
            c.scroll.pending += delta
            if not c.scroll.inflight:
                self._dispatch_view(c)
            return

        available = self._session.scrollback_lines
        # Negative delta scrolls back, so it increases the offset.
        wanted = max(0, min(available, c.scroll.offset - delta))
        moved = wanted != c.scroll.offset
        if moved:
            c.scroll.offset = wanted
            # The new view shares nothing with the old, so a diff against the mirror
            # would be nonsense: send the screen instead.
            self._resync()
        # Told, not inferred. Without it the handheld cannot know whether a press
        # did anything, or that it has reached the top -- and a scroll control
        # that gives no feedback reads as broken even when it works.
        self._send_view_pos(c, force=True)
        suffix = "" if moved else " (unchanged)"
        self._emit(f"client {c.addr} view {wanted} of {available} lines back{suffix}")

    def _dispatch_view(self, c: ClientState) -> None:
        """Hand the accumulated delta to the worker as one job.

        The job is a snapshot -- the pending sum and the anchor as the loop
        last set it -- so the worker reads no mutable client state, and presses
        that arrive while a capture runs wait in scroll.pending instead of all
        computing against the same stale anchor. At most one job per client is
        ever in flight.
        """
        c.scroll.gen += 1
        self._view_jobs.put(
            (c, c.scroll.gen, c.scroll.pending, c.scroll.anchor))
        c.scroll.pending = 0
        c.scroll.inflight = True

    def _view_worker(self) -> None:
        """Serve VIEW requests through tmux, off the select loop.

        The offset is recomputed here from the job's anchor rather than trusted
        from the request: history grows while a capture runs, and the user
        asked to keep looking at the same lines, not the same distance from
        the bottom.
        """
        while True:
            job = self._view_jobs.get()
            if job is None:
                return
            c, gen, delta, anchor = job
            history = 0
            try:
                session = self._session
                if session is None or not session.multiplexed:
                    self._view_results.put((c, gen, None, history, None))
                    continue
                history = session.pane_history_size(force=True)
                offset = history - anchor if anchor is not None else 0
                offset = max(0, min(min(history, HISTORY_LINES),
                                    offset - delta))
                grid = session.capture_view(offset, history) if offset else None
                self._view_results.put((c, gen, offset, history, grid))
            except Exception as exc:  # noqa: BLE001 -- the worker outlives jobs
                # An exception here must not kill the thread: a dead worker is
                # scrolling that silently does nothing -- the very symptom
                # this path exists to fix.
                log.warning("view capture job failed: %r", exc)
                self._view_results.put((c, gen, None, history, None))

    def _collect_view_results(self) -> None:
        """Apply what the view worker captured, on the loop thread."""
        while True:
            try:
                c, gen, offset, history, grid = \
                    self._view_results.get_nowait()
            except queue.Empty:
                return
            # Every result frees the gate, even a stale or orphaned one.
            c.scroll.inflight = False
            if c not in self._clients:
                continue
            if gen == c.scroll.gen:
                if offset is None or (offset > 0 and grid is None):
                    # A failed job or a failed capture moves nothing: the
                    # view stays where it was, or _view_for would fall back
                    # to pyte's empty history and show the live screen while
                    # VIEW_POS claims to be scrolled back.
                    self._send_view_pos(c, force=True)
                    self._emit(f"client {c.addr} view capture failed; still "
                               f"{c.scroll.offset} of {history}")
                else:
                    moved = offset != c.scroll.offset
                    c.scroll.offset = offset
                    c.scroll.anchor = None if offset == 0 else history - offset
                    c.scroll.grid = grid
                    if moved:
                        self._resync(c)
                    self._send_view_pos(c, force=True)
                    suffix = "" if moved else " (unchanged)"
                    self._emit(
                        f"client {c.addr} view {offset} of {history} "
                        f"lines back{suffix}")
            # Presses queued while this job ran -- including during a result
            # its gen already outlived, like the scroll that followed a KEY --
            # go next, computed from the anchor as the loop owns it now.
            if c.scroll.pending:
                self._dispatch_view(c)

    def _view_pos_frame(self, c: ClientState, *, force: bool = False) -> bytes | None:
        """Return a fresh VIEW_POS frame and record it, if this client knows it."""
        if self._session is None or not c.caps.view_pos:
            return None

        pos = (c.scroll.offset, self._session.scrollback_lines)
        if not force and pos == c.view_pos:
            return None

        if not force and c.scroll.offset == 0 and c.view_pos is not None and c.view_pos[0] == 0:
            now = time.monotonic()
            advanced = pos[1] - c.scroll.live_available
            if (now - c.scroll.live_report_at < LIVE_VIEW_REPORT_SECONDS and
                    advanced < ROWS):
                return None
            c.scroll.live_report_at = now
            c.scroll.live_available = pos[1]
        elif c.scroll.offset == 0:
            c.scroll.live_report_at = time.monotonic()
            c.scroll.live_available = pos[1]

        c.view_pos = pos
        return encode_view_pos(*pos)

    def _send_view_pos(self, c: ClientState, *, force: bool = False) -> None:
        data = self._view_pos_frame(c, force=force)
        if data is not None:
            self._queue(data)

    def _view_for(self, c: ClientState) -> Grid:
        if self._session is None:
            return Grid.blank()
        if (self._session.multiplexed and c.scroll.offset > 0
                and c.scroll.grid is not None):
            # The worker's capture is a frozen snapshot: like a real terminal
            # scrolled back, what the user is reading stays put while output
            # lands below.
            return c.scroll.grid
        return self._session.view(c.scroll.offset)

    def _on_view(self, c: ClientState, msg: Message) -> None:
        """VIEW: scroll this card's view by a number of lines."""
        try:
            delta = decode_view(msg.payload)
        except ProtocolError as exc:
            self._drop(c, f"bad view payload: {exc}")
            return
        c.caps.view_pos = True
        self._scroll_view(c, delta)

    # -- what the core calls (server.py) -------------------------------------

    def _lines_back(self, c: ClientState) -> int:
        """How far back this card is reading; 0 is the live screen."""
        return c.scroll.offset

    def _init_scrollback(self) -> None:
        """The capture worker's queues, and the worker."""
        # VIEW through tmux is a subprocess, so it is worked off the select
        # loop: (client, gen, pending delta, anchor snapshot) jobs in,
        # (client, gen, offset, history, grid) results out. One worker, and
        # at most one job per client in flight, so a stale capture can never
        # overtake a newer one.
        self._view_jobs: queue.Queue = queue.Queue()
        self._view_results: queue.Queue = queue.Queue()
        self._view_thread = threading.Thread(
            target=self._view_worker, daemon=True)
        self._view_thread.start()

    def _stop_scrollback(self) -> None:
        # Wake the view worker so an idle server does not leave its daemon
        # thread parked on the queue forever. No join: a capture in flight is
        # a bounded subprocess and the thread is a daemon.
        self._view_jobs.put(None)
