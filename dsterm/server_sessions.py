"""The SESSIONS list: tmux's windows, advertised to the card, and switching
between them.

Part of the server, split out by feature: the methods below are
mixed into Server (server.py) and share its state through `self`.
"""

from __future__ import annotations

import time
from collections import deque
from . import sessions
from .protocol import (
    MsgType,
    encode,
    encode_session,
    encode_sessions_empty,
    SESSION_ACTIVE,
    SESSION_RUNNING,
    SESSION_NEW_OUTPUT,
    Message,
    ProtocolError,
    decode_session_pick,
)
from .connection import (
    AdvertisedSession,
    ClientState,
    OutboundBatch,
    OutputRevision,
    SessionSnapshotBatch,
)


# How often the tmux window list is re-read. A subprocess per second is
# affordable; one per frame is not, and windows appear and vanish on human
# timescales rather than frame ones.
SESSIONS_POLL_SECONDS = 1.0


class SessionsMixin:
    """Owns tmux's window catalogue and its output revisions (_init_sessions);
    per connection, ClientState.sessions."""

    def _invalidate_session_catalog(self) -> None:
        """Forget mappings/revisions from a replaced or unreachable tmux server."""
        self._tmux_epoch = None
        self._window_output.clear()
        self._last_windows = None
        # Not the window ids REMOTE attached in: an unreachable tmux is most
        # often a timed-out query, and those ids cannot be found again. With
        # no epoch they badge nothing (_remote_set), and the next successful
        # listing either confirms their lifetime or clears them
        # (_note_remote_windows).
        for c in self._clients:
            # Remove only queued work that describes the vanished tmux
            # lifetime. An unwrapped child has no tmux catalog at all; clearing
            # its ordinary visual resync here would make authentication lose
            # the initial screen. Kernel-accepted bytes cannot be recalled,
            # but any remaining SessionSnapshotBatch or switch barrier must
            # never promote a dead epoch.
            c.outbox = deque(
                item for item in c.outbox
                if not isinstance(item, SessionSnapshotBatch)
                and not (isinstance(item, OutboundBatch)
                         and item.promote_rows is not None)
            )
            c.sessions.rows.clear()
            c.sessions.seen.clear()
            c.sessions.signature = ()
            c.sessions.initialized = False
            c.sessions.batch = None
            c.sessions.next_snapshot = None
            c.sessions.switch_pending = False
            c.sessions.switch_wait_ticks = 0
            c.sessions.switch_barrier = None
        # No selected window that can be named any more, so no session on
        # screen that can be: STATE says "unknown" until tmux answers again.
        self._refresh_state()

    def _observe_windows(self) -> sessions.WindowSnapshot | None:
        """Read the live tmux catalog and advance observed output revisions."""
        target = self._tmux_target()
        if target is None:
            self._invalidate_session_catalog()
            return None
        snapshot = sessions.list_windows(*target)
        if snapshot is None:
            self._invalidate_session_catalog()
            return None

        if self._tmux_epoch is not None and snapshot.epoch != self._tmux_epoch:
            # @0 may now mean an entirely new window. No old mapping or unseen
            # marker is allowed to survive this boundary.
            self._invalidate_session_catalog()
        self._tmux_epoch = snapshot.epoch
        now = time.monotonic()
        present: set[tuple[sessions.TmuxEpoch, str]] = set()
        for w in snapshot.windows:
            key = (snapshot.epoch, w.window_id)
            present.add(key)
            record = self._window_output.get(key)
            if record is None:
                self._window_output[key] = OutputRevision(w.activity, 0, now)
            elif w.activity != record.activity_marker:
                record.activity_marker = w.activity
                record.revision += 1
                record.last_output_monotonic = now
        self._window_output = {
            key: record for key, record in self._window_output.items() if key in present
        }
        previous = self._active_window_id()
        self._last_windows = snapshot
        self._note_remote_windows(snapshot)
        if self._active_window_id() != previous:
            # Selected from a PC, or by the card: STATE must follow at once.
            self._active_window_changed()
        return snapshot

    def _snapshot_batch(self, c: ClientState,
                        snapshot: sessions.WindowSnapshot | None) -> SessionSnapshotBatch:
        """Build one complete, non-interleavable wire snapshot for ``c``."""
        if snapshot is None or not snapshot.windows:
            return SessionSnapshotBatch([encode_sessions_empty()], (), (), None)

        now = time.monotonic()
        rows: list[AdvertisedSession] = []
        messages: list[bytes] = []
        signature_parts = []
        for i, w in enumerate(snapshot.windows):
            key = (snapshot.epoch, w.window_id)
            # ``snapshot`` normally comes straight from _observe_windows(),
            # but keep a supplied/current list safe at the race boundary too:
            # a just-discovered live row has revision zero, not an invented
            # unseen notification.
            output = self._window_output.get(key)
            if output is None:
                output = OutputRevision(w.activity, 0, now)
                self._window_output[key] = output
            # An active window is being viewed. Its observed output is already
            # seen by this client; background windows are not granted that.
            if w.active:
                c.sessions.seen[key] = output.revision
            seen = c.sessions.seen.get(key, 0)
            flags = SESSION_RUNNING
            if w.active:
                flags |= SESSION_ACTIVE
            elif output.revision > seen:
                flags |= SESSION_NEW_OUTPUT
            idle = sessions.idle_seconds(output.last_output_monotonic, now)
            rows.append(AdvertisedSession(snapshot.epoch, w.window_id, w.index))
            messages.append(encode_session(i, len(snapshot.windows), flags, idle, w.name))
            # Idle age is deliberately absent: it changes every second and that
            # is a refresh, not a new identity. Flags include revision state.
            signature_parts.append((w.window_id, w.index, w.name, flags))
        return SessionSnapshotBatch(messages, tuple(rows), tuple(signature_parts), snapshot.epoch)

    def _session_batch_queued_or_active(self, c: ClientState) -> bool:
        return c.sessions.batch is not None or any(
            isinstance(item, SessionSnapshotBatch) for item in c.outbox
        )

    def _start_coalesced_session_snapshot(self, c: ClientState) -> None:
        """Append one deferred snapshot after the current batch completes."""
        pending = c.sessions.next_snapshot
        c.sessions.next_snapshot = None
        if pending is not None and c in self._clients:
            c.outbox.append(pending)

    def _advertise_sessions(self, c: ClientState, *, force: bool = False,
                            snapshot: sessions.WindowSnapshot | None = None) -> None:
        """Queue a complete SESSIONS snapshot without interleaving snapshots."""
        if c not in self._clients:
            return
        # The switch barrier already owns the mandated snapshot.  A periodic
        # refresh that lands while it drains waits for the next poll instead of
        # appending a second list immediately behind the ordered one.
        if c.sessions.switch_pending or c.sessions.switch_barrier is not None:
            return
        if snapshot is None:
            snapshot = self._observe_windows()
        batch = self._snapshot_batch(c, snapshot)
        changed = (batch.signature != c.sessions.signature or
                   batch.epoch != self._tmux_epoch or not c.sessions.initialized)
        # Idle age alone is not a change of session facts. The contract names
        # exactly three moments -- after authentication, after a successful
        # switch, and when the facts change -- and an age that ticks is none of
        # them. Publishing one per poll turned a rare message into a permanent
        # 1 Hz stream, and the handheld, which stages every entry of every
        # snapshot, data aborts a few seconds into it. A slightly stale age is
        # the cheaper wrong thing: identity, flags and NEW_OUTPUT stay exact,
        # because each of those moves the signature and is sent immediately.
        # Note the batch is still BUILT every poll: _snapshot_batch is what
        # keeps sessions.seen current for the window being viewed, so dropping
        # it here must not become "skip the observation".
        if not force and not changed:
            return
        c.sessions.initialized = True
        if self._session_batch_queued_or_active(c):
            # Latest complete fact set wins, but never breaks the snapshot the
            # DS is currently staging.
            c.sessions.next_snapshot = batch
            return
        c.outbox.append(batch)

    def _handle_session_pick(self, c: ClientState, index: int) -> None:
        target = self._tmux_target()
        if target is None or not 0 <= index < len(c.sessions.rows):
            self._send_to(c, encode(MsgType.ERROR, b"no such session"))
            self._emit(f"client {c.addr} picked session {index}: no such row")
            return

        mapped = c.sessions.rows[index]
        why = self._switch_to_window(c, mapped.window_id, epoch=mapped.epoch)
        if why is not None:
            self._send_to(c, encode(MsgType.ERROR, b"session unavailable"))
            self._emit(f"client {c.addr} picked session {index}: {why}")
            return
        self._emit(f"client {c.addr} switched to session {index}")

    def _switch_to_window(self, c: ClientState, window_id: str, *,
                          epoch: sessions.TmuxEpoch | None = None) -> str | None:
        """Select ``window_id`` and arm the switch barrier for ``c``.

        The one window-select path: SESSION_PICK resolves its row to a window
        and comes here, and so does a REMOTE pick, so both give the card the
        same repaint/VIEW_POS/MODE/SESSIONS barrier. Returns None on success,
        otherwise why not -- the caller decides how to tell the card.
        ``epoch`` pins the tmux lifetime the caller's window id came from;
        None accepts the current one (a window this server just created).
        """
        target = self._tmux_target()
        if target is None:
            return "no tmux session"
        snapshot = self._observe_windows()
        if snapshot is None or (epoch is not None and snapshot.epoch != epoch):
            return "stale epoch"
        current = {w.window_id: w for w in snapshot.windows}
        window = current.get(window_id)
        if window is None:
            return "stale window"

        socket_name, name = target
        if not sessions.select(socket_name, name, window_id):
            return "tmux refused"

        # The next one-second tmux poll will authoritatively confirm this, but
        # output can arrive on the PTY in the tick immediately after a
        # successful select. Mark the known-selected identity active now so
        # that output is seen by the correct client/window during that narrow
        # interval rather than credited to the window just left.
        self._last_windows = sessions.WindowSnapshot(
            snapshot.epoch,
            tuple(
                sessions.Window(w.window_id, w.index, w.name,
                                w.window_id == window_id, w.activity)
                for w in snapshot.windows
            ),
        )
        self._active_window_changed()
        key = (snapshot.epoch, window_id)
        c.sessions.seen[key] = self._window_output[key].revision
        self._view_to_live(c)
        # A successful pick itself proves this card knows the display-state
        # messages that came with session switching: it clears its view and
        # mode state while it waits for both fresh facts.
        c.caps.view_pos = True
        c.caps.mode = True
        if self._session is not None:
            self._session.invalidate_mode()
        # Stale render output must not leak ahead of the selected screen. The
        # next ordinary tick installs the strict repaint/metadata barrier.
        self._clear_unsent_outbox(c)
        c.sessions.switch_barrier = None
        with self._ext_lock:
            # A previously queued ExtensionFrame was discarded with the old
            # visual work.  Leave ``c.ext.pending`` intact so the latest
            # message is released after the ordered switch batch.
            self._forget_queued_extension(c)
        c.sessions.switch_pending = True
        c.sessions.switch_wait_ticks = 1
        return None

    def _install_switch_barrier(self, c: ClientState) -> None:
        """Queue the selected view and its metadata in the mandated order."""
        if self._session is None or c not in self._clients:
            return
        if self._keep_final_session_tail(c):
            # An already-started old snapshot has one final TCP tail that
            # cannot be revoked. Let it finish and promote its own mapping
            # before this new strict barrier begins.
            return
        snapshot = self._observe_windows()
        sessions_batch = self._snapshot_batch(c, snapshot)
        # Retain the old completed mapping until the final snapshot entry has
        # crossed the local FIFO. The DS deliberately has no new complete list
        # until then, and a hostile early pick cannot select a half-advertised
        # row either.
        c.sessions.initialized = True
        c.sessions.batch = None
        c.sessions.next_snapshot = None

        messages = self._resync_messages(c)
        view_pos = self._view_pos_frame(c, force=True)
        if view_pos is not None:
            messages.append(view_pos)
        mode = self._mode_frame(c, force=True)
        if mode is not None:
            messages.append(mode)
        messages.extend(sessions_batch.messages)

        # A switch invalidates every unsent visual message. The one normal-tick
        # barrier contains no literal FRAME_FULL because every hardware frame
        # must remain below the measured 536-byte ceiling.
        self._clear_unsent_outbox(c)
        with self._ext_lock:
            self._forget_queued_extension(c)
        barrier = OutboundBatch(
            messages,
            promote_rows=sessions_batch.rows,
            promote_signature=sessions_batch.signature,
        )
        c.outbox.append(barrier)
        c.sessions.switch_barrier = barrier
        c.sessions.switch_pending = False
        c.sessions.switch_wait_ticks = 0

    def _on_session_pick(self, c: ClientState, msg: Message) -> None:
        """SESSION_PICK: an index into the SESSIONS list this card was sent."""
        try:
            index = decode_session_pick(msg.payload)
        except ProtocolError as exc:
            self._drop(c, f"bad session pick: {exc}")
            return
        self._handle_session_pick(c, index)

    # -- what the core calls (server.py) -------------------------------------

    def _init_sessions(self) -> None:
        """tmux's window catalogue: its identity, each window's output
        revisions, and the last listing."""
        self._sessions_polled_at = 0.0
        # Only tmux exposes background-window activity. Its marker advances on
        # output; we turn that observation into a monotonic revision timeline.
        self._tmux_epoch: sessions.TmuxEpoch | None = None
        self._window_output: dict[tuple[sessions.TmuxEpoch, str], OutputRevision] = {}
        self._last_windows: sessions.WindowSnapshot | None = None

    def _switch_pending(self, c: ClientState) -> bool:
        """A window switch has been asked for and not yet painted."""
        return c.sessions.switch_pending

    def _switch_in_progress(self, c: ClientState) -> bool:
        """A switch is pending or its barrier is still going out: nothing
        else may be put between its frames."""
        return c.sessions.switch_pending or c.sessions.switch_barrier is not None

    def _session_list_staged(self, c: ClientState) -> bool:
        """A SESSIONS list is being sent to this card, frame by frame."""
        return c.sessions.batch is not None

    def _listing(self) -> sessions.WindowSnapshot | None:
        """tmux's window list as last read; None before the first."""
        return self._last_windows

    def _tmux_lifetime(self) -> sessions.TmuxEpoch | None:
        """Which tmux server the window ids belong to (a restarted tmux
        reuses them for different windows)."""
        return self._tmux_epoch

    def _list_windows_soon(self) -> None:
        """Have the next tick read tmux's window list rather than wait out
        SESSIONS_POLL_SECONDS: something just changed it."""
        self._sessions_polled_at = 0.0

    def _current_windows(self) -> sessions.WindowSnapshot | None:
        """The last listing, or a fresh one when there is none yet."""
        return self._last_windows or self._observe_windows()

    def _sessions_tick(self, now: float, bytes_in: int) -> sessions.WindowSnapshot | None:
        """Sessions come and go while the handheld is looking at one of them --
        a window closes, a shell starts, another one produces output. tmux
        polling is bounded to human time; active PTY output is observed at the
        normal render cadence. Returns a fresh listing when one was taken."""
        snapshot: sessions.WindowSnapshot | None = None
        if now - self._sessions_polled_at >= SESSIONS_POLL_SECONDS:
            self._sessions_polled_at = now
            snapshot = self._observe_windows()
        self._note_active_pty_output(bytes_in)
        return snapshot

    def _note_active_pty_output(self, bytes_in: int) -> None:
        """Turn observed active-window PTY output into a monotonic revision."""
        if not bytes_in or self._last_windows is None:
            return
        active = next((w for w in self._last_windows.windows if w.active), None)
        if active is None:
            return
        key = (self._last_windows.epoch, active.window_id)
        record = self._window_output.get(key)
        if record is None:
            return
        record.revision += 1
        record.last_output_monotonic = time.monotonic()
        # There is only one authenticated owner, but keep this per ClientState
        # so ownership/reconnect behavior cannot accidentally become durable.
        for client in self._clients:
            if client.authed:
                client.sessions.seen[key] = record.revision

    def _active_idle_seconds(self, now: float) -> int | None:
        """Seconds since the ACTIVE window last produced output, or None.

        Deliberately the same number SESSIONS already carries as ``idle_s``,
        taken from the same record through the same helper. The page must not
        invent a second measurement of the same thing: two numbers that can
        disagree are worse than one, and the wire already states this one.

        Per-active-window rather than global, because that is what the question
        means. A server with one quiet window and one busy one is not quiet.

        None when there is no active window to ask about -- silence, not a zero,
        since a zero would read as "producing output right now".
        """
        if self._last_windows is None:
            return None
        active = next((w for w in self._last_windows.windows if w.active), None)
        if active is None:
            return None
        record = self._window_output.get(
            (self._last_windows.epoch, active.window_id))
        if record is None:
            return None
        return sessions.idle_seconds(record.last_output_monotonic, now)
