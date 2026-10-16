"""REMOTE and STATE: what `claude agents` says, offered to the card as rows and
as the session's state, and the picks that act on it.

Part of the server, split out by feature: the methods below are
mixed into Server (server.py) and share its state through `self`.
"""

from __future__ import annotations

import os
import time
from collections import deque
from . import remote
from . import sessions
from .protocol import (
    MsgType,
    encode_remote_note,
    encode_state,
    STATE_UNKNOWN,
    REMOTE_ACTION_COPY,
    REMOTE_ACTION_NEW,
    REMOTE_ACTION_OPEN,
    REMOTE_COPY,
    REMOTE_HERE,
    REMOTE_LIVE,
    REMOTE_NEW_INDEX,
    Message,
    ProtocolError,
    decode_remote_open,
    decode_remote_pick,
)
from .connection import (
    ClientState,
    RemoteSnapshotBatch,
)


# One REMOTE pick per second per client. A pick can start a process -- a new
# tmux window running claude -- so a held button or a replayed frame must not
# be able to open a window per message. A second press inside the window is a
# double press, and the first one's REMOTE_NOTE is already on its way.
REMOTE_PICK_INTERVAL = 1.0

# A REMOTE set's ages are not part of its identity (see remote.Row.signature),
# so an unchanged list is resent for its ages at most this often while the
# screen is open: often enough that "12m" does not sit there for an hour, and
# far from the 1 Hz stream that once data-aborted the handheld via SESSIONS.
REMOTE_AGE_REFRESH_SECONDS = 60.0

# A STATE reading older than this is not told to a card that has just
# connected. Polling stops when the last card leaves, so the server's last
# reading can be from any time ago; while a card is connected it is never
# older than the slow cadence plus one ask. The same bound applies to an ask
# that was in flight when the last card left and finished with nobody
# ticking: it is thrown away at harvest rather than published as the present
# (RemotePoller.poll's max_age).
STATE_STALE_SECONDS = 30.0

# A card that has sent nothing for this long is not counted as owed a
# `claude agents` reading. The card PINGs every second, so ten seconds is ten
# missed pings: switched off, or out of range, on a socket TCP has not yet
# noticed is half-open -- which, with no keepalive, it may never notice. Only
# the polling stops; the connection is not dropped. Dropping is the one thing
# that cannot be taken back, a reconnecting card already displaces its
# predecessor at AUTH, and a client that never pings (`dsterm screen`, a
# test) has done nothing wrong by being quiet. The first byte it sends counts
# it again.
SILENT_CARD_SECONDS = 10.0

# How long `claude agents` may keep failing before STATE stops vouching for
# the last good reading. One failed ask is not news about the session (the
# reading is kept, rather than sounding an edge through "unknown" and back),
# but a CLI that fails every time for half a minute -- uninstalled under the
# server, logged out, broken by an update -- says nothing about it either, and
# the card must not show "working" forever. After this the reading is 0 and
# the asks drop to the slow cadence until one succeeds.
AGENTS_FAILING_SECONDS = 30.0


# How soon after a REMOTE pick opened a window it is checked for still being
# there. The note at the pick says "starting", because tmux confirming a
# window says nothing about whether `claude attach` found its session; a
# window that is gone by now ran a command that failed at once, and the card
# is told so rather than left with a "starting" that never started.
REMOTE_START_CHECK_SECONDS = 3.0

class AgentsMixin:
    """Owns the agents poller, what it last said, and the windows REMOTE has
    opened (_init_agents); per connection, ClientState.agents."""

    # -- remote --------------------------------------------------------------

    def _reset_remote(self, c: ClientState) -> None:
        """Forget everything this connection was told about REMOTE.

        On HELLO, like feeds.project_signature and feeds.clip_seq: whatever this
        object remembers, a card that has just said hello has been told
        nothing. Queued REMOTE frames go too -- the new HELLO may not have
        asked for them, and a card that did not ask halts on the type.
        """
        c.agents.remote_open = False
        c.agents.target = None
        c.agents.rows = []
        c.agents.signature = None
        c.agents.ages = ()
        c.agents.sent_at = 0.0
        c.agents.note = None
        if not self._keep_final_remote_tail(c):
            c.agents.batch = None
        c.outbox = deque(
            item for item in c.outbox
            if not isinstance(item, RemoteSnapshotBatch)
            and not (isinstance(item, bytes) and len(item) > 3
                     and item[3] == MsgType.REMOTE_NOTE)
        )

    def _remote_wanted(self, now: float | None = None) -> bool:
        """Whether any authenticated card that is still there has the REMOTE
        screen open."""
        stamp = time.monotonic() if now is None else now
        return any(c.authed and c.caps.remote and c.agents.remote_open
                   and self._still_there(c, stamp) for c in self._clients)

    @staticmethod
    def _still_there(c: ClientState, now: float) -> bool:
        """Whether ``c`` has been heard from recently (SILENT_CARD_SECONDS)."""
        return now - c.last_heard_at < SILENT_CARD_SECONDS

    def _set_remote_open(self, c: ClientState, is_open: bool) -> None:
        was = c.agents.remote_open
        c.agents.remote_open = is_open
        if is_open and not was:
            # Ask now rather than up to one interval from now: the card has
            # just put up a screen that says "asking".
            self._agents.refresh()
        if is_open != was:
            self._emit(f"client {c.addr} remote screen "
                       f"{'opened' if is_open else 'closed'}")

    def _remote_set(self, obs: remote.Observation | None) -> remote.RemoteSet | None:
        """Build the set every REMOTE card should now have from ``obs``."""
        if obs is None:
            return None
        if obs.status != remote.REMOTE_STATUS_NONE:
            return remote.make_set(obs.status)
        target = self._tmux_target()
        self._prune_remote_windows(obs)
        # HERE only against the tmux lifetime the select loop is tracking:
        # a window id from another lifetime may now name a different window.
        epoch = self._tmux_lifetime()
        here_by_pid: dict[int, str] = {}
        here_by_id: dict[str, str] = {}
        if epoch is not None:
            # HERE is a promise that a pick selects the window, and a pick can
            # only select a window the card's session list holds -- the first
            # eight, as the last listing has them. The pane walk is not capped
            # (list-panes lists every window), so without this a session in a
            # ninth window was HERE, and picking it said "that window is gone"
            # about a window that was open.
            listing = self._listing()
            listed = ({w.window_id for w in listing.windows}
                      if listing is not None and listing.epoch == epoch else set())
            if obs.tmux_pid == epoch.server_pid:
                here_by_pid = {pid: window_id for pid, window_id
                               in obs.here_by_pid.items() if window_id in listed}
            if self._remote_windows_epoch == epoch:
                here_by_id = {agent_id: window_id for window_id, (agent_id, _)
                              in self._remote_windows.items() if window_id in listed}
        rows = remote.build_rows(
            obs.agents, now_ms=int(time.time() * 1000),
            here_by_pid=here_by_pid, here_by_id=here_by_id, here_epoch=epoch,
            can_open=target is not None)
        return remote.make_set(remote.REMOTE_STATUS_NONE, rows)

    def _publish_remote(self, obs: remote.Observation | None) -> None:
        """Make ``obs`` the target of every REMOTE card; the tick sends it."""
        built = self._remote_set(obs)
        if built is None:
            return
        for c in self._clients:
            if c.authed and c.caps.remote:
                c.agents.target = built

    def _prune_remote_windows(self, obs: remote.Observation) -> None:
        """Forget attach windows that tmux no longer lists.

        They are closed (the attach client exits with its window). Only an
        answer that began after the window was opened can say so, and only
        one where tmux answered at all: "unknown" must never read as "closed".
        tmux never reuses a window id within one server's lifetime, and a new
        lifetime clears the whole map (_invalidate_session_catalog).
        """
        if obs.windows is None:
            return
        known = self._remote_windows_epoch
        if known is not None and obs.tmux_pid != known.server_pid:
            # Another tmux lifetime's panes: absence there says nothing
            # about this one's windows.
            return
        for window_id, (_, opened_at) in list(self._remote_windows.items()):
            if window_id not in obs.windows and opened_at < obs.started_at:
                del self._remote_windows[window_id]

    def _poll_interval(self, now: float) -> float | None:
        """How often `claude agents` should be asked right now, or None.

        Only cards that are still there count: a handheld switched off on a
        half-open socket otherwise kept the CLI starting every few seconds
        for as long as the server ran.
        """
        state_cards = [c for c in self._clients if c.authed and c.caps.state
                       and self._still_there(c, now)]
        typed = [now - c.last_key_at for c in state_cards if c.last_key_at is not None]
        return remote.poll_interval(
            remote_open=self._remote_wanted(now),
            state_clients=bool(state_cards),
            reading=self._state_reading[0],
            typed_ago=min(typed) if typed else None,
            failing=self._agents_failing(now))

    def _agents_failing(self, now: float | None = None) -> bool:
        """Whether `claude agents` has failed for AGENTS_FAILING_SECONDS."""
        if self._agents_failing_since is None:
            return False
        stamp = time.monotonic() if now is None else now
        return stamp - self._agents_failing_since >= AGENTS_FAILING_SECONDS

    def _poll_agents(self, now: float) -> None:
        """One tick of the shared reading: publish a finished one, start the next.

        Never blocks -- `claude agents` runs on the poller's thread, and only
        while some card is owed an answer. Split out of ``_tick`` for the
        reason ``_poll_project`` is: the wiring must be testable on its own.
        """
        interval = self._poll_interval(now)
        obs = self._agents.poll(interval is not None, self._tmux_target(),
                                now=now, interval=interval,
                                max_age=STATE_STALE_SECONDS)
        if obs is not None:
            self._take_observation(obs)

    def _take_observation(self, obs: remote.Observation) -> None:
        """A finished reading: the REMOTE set and the STATE reading follow it."""
        if obs.status == remote.REMOTE_STATUS_FAILED:
            if self._agents_failing_since is None:
                self._agents_failing_since = time.monotonic()
        else:
            self._agents_failing_since = None
        self._prune_remote_windows(obs)
        if self.remote_enabled:
            self._publish_remote(obs)
        self._refresh_state()

    # -- state ----------------------------------------------------------------

    def _active_window_id(self) -> str | None:
        """The selected window of the dsterm session, as the loop last saw it."""
        listing = self._listing()
        if listing is None:
            return None
        return next((w.window_id for w in listing.windows if w.active), None)

    def _refresh_state(self) -> None:
        """Recompute what the session on screen is doing from the last reading.

        Called when a reading lands AND when the selected window changes, so
        the card never hears the state of the window it just left: the answer
        for the new window comes from the same reading (its pane map covers
        every window), and a fresh ask is requested by the caller.
        """
        obs = self._agents.latest
        active = self._active_window_id()
        reading = (STATE_UNKNOWN, "")
        if (obs is not None and obs.status == remote.REMOTE_STATUS_FAILED
                and active == self._state_window and not self._agents_failing()):
            # One failed ask is not news about the session. Keep the last
            # reading rather than sounding an edge through "unknown" and back
            # -- but only for the window it was read for, and only for so
            # long: after AGENTS_FAILING_SECONDS of nothing but failures the
            # last reading is a guess, and the card hears "unknown".
            reading = self._state_reading
        elif obs is not None and obs.status == remote.REMOTE_STATUS_NONE:
            epoch = self._tmux_lifetime()
            if epoch is not None:
                here_by_pid = obs.here_by_pid if obs.tmux_pid == epoch.server_pid else {}
                attached = {window_id: agent_id for window_id, (agent_id, _)
                            in self._remote_windows.items()}
                reading = remote.state_reading(obs.agents, here_by_pid,
                                               active, attached)
        self._state_window = active
        if reading != self._state_reading:
            self._state_reading = reading
            names = ("unknown", "idle", "working", "needs approval", "needs input")
            self._emit(f"session on screen: {names[reading[0]]}"
                       + (f" ({reading[1]})" if reading[1] else ""))

    def _state_for_new_card(self) -> None:
        """Drop a reading too old to describe the present (see AUTH).

        For a REMOTE card as much as a STATE one: a pick republishes the
        latest reading as the list, so an old one must not survive to be
        that list either.
        """
        latest = self._agents.latest
        if latest is None or time.monotonic() - latest.started_at > STATE_STALE_SECONDS:
            self._agents.forget()
            self._state_reading = (STATE_UNKNOWN, "")
            # A run of failures that ended with the last card is not a run
            # this card has watched; it starts its own.
            self._agents_failing_since = None
            self._agents.refresh()

    def _active_window_changed(self) -> None:
        """The selected window moved: answer for the new one now, then ask."""
        self._refresh_state()
        self._agents.refresh()

    def _advertise_state(self, c: ClientState) -> None:
        """Send this card the current reading if it differs from its last.

        Sent directly, like MODE, but never between the frames of a list the
        card is staging or inside a switch barrier: after a switch, the card
        hears the new window's state once it can see the new window. Recorded
        only when actually sent, so a deferred reading goes on a later tick.
        """
        if not c.caps.state or not c.authed or c not in self._clients:
            return
        if self._state_reading == c.agents.state_sent:
            return
        if (self._switch_in_progress(c) or self._session_list_staged(c)
                or c.agents.batch is not None):
            return
        c.agents.state_sent = self._state_reading
        self._send_to(c, encode_state(*self._state_reading))

    def _remote_batch_queued_or_active(self, c: ClientState) -> bool:
        return c.agents.batch is not None or any(
            isinstance(item, RemoteSnapshotBatch) for item in c.outbox)

    def _advertise_remote(self, c: ClientState, now: float | None = None) -> None:
        """Queue this client's REMOTE target if it differs from what it has.

        Reconciled rather than pushed: a set is queued whenever the target
        differs from the last one fully sent and none is already on its way,
        so a set cut off by a resync is resent and a newer answer that lands
        mid-send follows the current one rather than interleaving with it.
        """
        if (not self.remote_enabled or not c.caps.remote or not c.authed
                or c not in self._clients or self._switch_pending(c)):
            return
        target = c.agents.target
        if target is None or self._remote_batch_queued_or_active(c):
            return
        stamp = time.monotonic() if now is None else now
        if target.signature == c.agents.signature and (
                target.ages == c.agents.ages
                or stamp - c.agents.sent_at < REMOTE_AGE_REFRESH_SECONDS):
            return
        c.outbox.append(RemoteSnapshotBatch(
            list(target.messages), target.rows, target.signature, target.ages))

    def _release_remote(self, c: ClientState, now: float | None = None) -> None:
        """Queue a waiting REMOTE_NOTE, then any set the card is owed.

        Not while a switch is pending: installing its barrier clears the
        outbox, and a note cleared there is a pick the card never hears
        about. Behind an installed barrier is fine -- the outbox is FIFO, so
        the note lands after the repaint of the window it describes.
        """
        if not c.caps.remote or self._switch_pending(c) or c not in self._clients:
            return
        if c.agents.note is not None:
            c.outbox.append(c.agents.note)
            c.agents.note = None
        self._advertise_remote(c, now)

    def _remote_note(self, c: ClientState, text: str) -> None:
        c.agents.note = encode_remote_note(remote.note_text(text))
        self._emit(f"client {c.addr} remote: {remote.note_text(text)}")

    def _handle_remote_pick(self, c: ClientState, index: int, action: int,
                            check: int = 0) -> None:
        now = time.monotonic()
        if now - c.agents.last_pick_at < REMOTE_PICK_INTERVAL:
            # Dropped quietly: the first press's note is already coming.
            self._emit(f"client {c.addr} remote pick {index}/{action} ignored: "
                       "under a second after the last")
            return
        c.agents.last_pick_at = now
        self._remote_note(c, self._remote_pick(c, index, action, check))
        # After any pick, whatever it did: the tmux window list may have
        # changed, and so may what `claude agents` would say.
        self._list_windows_soon()
        self._agents.refresh()
        # The window map may have changed (an attach is now HERE). Rebuild
        # from the last answer now rather than waiting out a fresh ask.
        self._publish_remote(self._agents.latest)

    def _remote_pick(self, c: ClientState, index: int, action: int,
                     check: int = 0) -> str:
        """Carry out one pick; return the note that says how it went.

        Everything that reaches tmux comes from this server: the row this
        client was last sent (by position, confirmed by its check), that row's
        id or session id as `claude agents` printed it and as remote.py
        validated it, the `claude` on this server's PATH, and this server's
        own child command.
        """
        if action == REMOTE_ACTION_NEW:
            if index != REMOTE_NEW_INDEX or check != 0:
                return "that is not a row"
            argv = self.child_command
            if argv is None:
                claude = self._claude_path()
                if claude is None:
                    return "claude is not installed here"
                argv = [claude]
            why = self._open_remote_window(c, "claude", self.cwd, argv,
                                           what="the new session")
            # "starting", never "started": tmux confirming a window says the
            # command was launched, not that it is running. A window that is
            # gone a few seconds later is reported then (_note_remote_windows).
            return why or "starting a new session"

        if action not in (REMOTE_ACTION_OPEN, REMOTE_ACTION_COPY):
            return "that is not something a row can do"
        if not 0 <= index < len(c.agents.rows):
            return "that session is gone"
        row = c.agents.rows[index]
        if check != row.check:
            # The row at this position is not the one the card drew there: a
            # newer set re-sorted the list (a session started waiting and
            # jumped to the top) between the person reading it and pressing.
            # Acting on the index would open somebody else's session, so
            # nothing is done, and the set is sent again even if this server
            # believes the card already has it -- whatever the card is
            # holding, it is not what this connection's rows say.
            c.agents.signature = None
            return "the list changed; pick again"
        if action == REMOTE_ACTION_OPEN and not row.flags & (REMOTE_HERE | REMOTE_LIVE):
            return "that row can't be opened live"
        if action == REMOTE_ACTION_COPY and not row.flags & REMOTE_COPY:
            return "that row can't be opened as a copy"

        if action == REMOTE_ACTION_OPEN and row.flags & REMOTE_HERE and row.here_window:
            if self._switch_to_window(c, row.here_window,
                                      epoch=row.here_epoch) is None:  # type: ignore[arg-type]
                return f"opening {row.name}"
            if not row.flags & REMOTE_LIVE:
                return "that window is gone"
            # The window closed since the row was sent; a live row can still
            # be attached afresh.

        # From here on a process is started, so the row must still be in the
        # newest answer: a stale row must not attach or resume a session that
        # has since gone, or one that a reused id now names.
        latest = self._agents.latest
        if (latest is not None and latest.status == remote.REMOTE_STATUS_NONE
                and not remote.still_listed(row, latest.agents)):
            return "that session is gone"
        claude = self._claude_path()
        if claude is None:
            return "claude is not installed here"

        if action == REMOTE_ACTION_OPEN:
            if not remote.valid_agent_id(row.agent_id):
                return "that row can't be opened live"
            why = self._open_remote_window(c, row.name, row.cwd,
                                           [claude, "attach", row.agent_id],
                                           attach_id=row.agent_id, what=row.name)
            return why or f"starting {row.name}"

        if not remote.valid_session_id(row.session_id):
            return "that row can't be opened as a copy"
        why = self._open_remote_window(
            c, row.name, row.cwd,
            [claude, "--resume", row.session_id, "--fork-session"], what=row.name)
        return why or f"starting a copy of {row.name}"

    def _claude_path(self) -> str | None:
        """The `claude` the last answer came from, else PATH's."""
        latest = self._agents.latest
        if latest is not None and latest.claude:
            return latest.claude
        return remote.find_claude()

    def _open_remote_window(self, c: ClientState, name: str, cwd: str | None,
                            argv: list[str], *,
                            attach_id: str | None = None,
                            what: str = "it") -> str | None:
        """Open ``argv`` in a new dsterm window and switch ``c`` to it.

        Returns None once tmux has made the window, or the note explaining
        why not. ``what`` names the window in the follow-up note if it has
        closed again by REMOTE_START_CHECK_SECONDS.
        """
        target = self._tmux_target()
        if target is None:
            return "no tmux session to open it in"
        snapshot = self._observe_windows()
        if snapshot is not None and len(snapshot.windows) >= sessions.MAX_SESSIONS:
            # The card's session list holds eight. A ninth window would be one
            # the handheld could never switch back to.
            return f"{sessions.MAX_SESSIONS} windows open: close one first"
        if not cwd or not os.path.isdir(cwd):
            cwd = self.cwd if self.cwd and os.path.isdir(self.cwd) else os.getcwd()
        window_id = remote.new_window(*target, name, cwd, argv)
        if window_id is None:
            return "tmux could not open a window"
        opened_at = time.monotonic()
        if attach_id is not None:
            self._remote_windows[window_id] = (attach_id, opened_at)
        self._remote_starting[window_id] = (c, what, opened_at)
        why = self._switch_to_window(c, window_id)
        if why is not None:
            self._emit(f"client {c.addr} remote window {window_id} opened "
                       f"but not selected: {why}")
        return None

    def _on_remote(self, c: ClientState, msg: Message) -> None:
        """REMOTE_OPEN and REMOTE_PICK, from a card that announced REMOTE."""
        # Refused exactly as an unexpected type is, when this connection
        # never negotiated them: the card sends these only after it has
        # received a REMOTE frame, and a connection that was never sent
        # one is not a card following the contract. An older server drops
        # it in the frame reader for the same frame; this one says why.
        if not c.caps.remote or not self.remote_enabled:
            self._drop(c, f"unexpected message {msg.type.name} "
                          "(remote was not negotiated)")
            return
        if msg.type is MsgType.REMOTE_OPEN:
            try:
                is_open = decode_remote_open(msg.payload)
            except ProtocolError as exc:
                self._drop(c, f"bad remote open: {exc}")
                return
            self._set_remote_open(c, is_open)
            return
        try:
            index, action, check = decode_remote_pick(msg.payload)
        except ProtocolError as exc:
            self._drop(c, f"bad remote pick: {exc}")
            return
        self._handle_remote_pick(c, index, action, check)

    def _note_remote_windows(self, snapshot: sessions.WindowSnapshot) -> None:
        """Reconcile REMOTE's attach windows and pending starts with a listing.

        Only a successful listing gets here, so "absent" means closed -- but
        only when the listing is complete: list_windows stops at eight, and a
        ninth window it did not list is not a window that closed (the
        uncapped pane list in each `claude agents` reading prunes those, in
        _prune_remote_windows). A listing from another tmux lifetime clears
        everything, because there the same ids name other windows.
        """
        if self._remote_windows_epoch != snapshot.epoch:
            if self._remote_windows_epoch is not None:
                self._remote_windows.clear()
                self._remote_starting.clear()
            self._remote_windows_epoch = snapshot.epoch
        present = {w.window_id for w in snapshot.windows}
        complete = len(snapshot.windows) < sessions.MAX_SESSIONS
        if complete:
            for window_id in list(self._remote_windows):
                if window_id not in present:
                    del self._remote_windows[window_id]
        now = time.monotonic()
        for window_id, (c, what, opened_at) in list(self._remote_starting.items()):
            if now - opened_at < REMOTE_START_CHECK_SECONDS:
                continue
            del self._remote_starting[window_id]
            if (window_id in present or not complete or c not in self._clients
                    or not c.caps.remote):
                continue
            # tmux closes a window when its command exits, so a window gone
            # within seconds ran a command that failed straight away: an
            # attach to a session that ended, a resume that found nothing.
            self._remote_note(c, f"{what} exited at once")

    # -- what the core calls (server.py) -------------------------------------

    def _init_agents(self, remote_enabled: bool, remote_poller) -> None:
        """The `claude agents` poller, what it last said, and the windows
        REMOTE has opened or is opening."""
        # REMOTE is on by default, unlike --clip: it lists sessions that are
        # already this user's, on their own machine, to a card that has proved
        # the key -- nothing leaves the machine that the screen does not
        # already show. --no-remote turns it off, and then no card is ever told
        # the server speaks REMOTE, so none ever sends a REMOTE message.
        self.remote_enabled = remote_enabled
        # One `claude agents` reading serves REMOTE and STATE both. It asks
        # only while some card is owed an answer, at the faster of the two
        # cadences (remote.poll_interval). A parameter so tests can hand in
        # one with a fake runner.
        self._agents = remote_poller or remote.RemotePoller()
        # What the session in the selected window is doing, as (STATE byte,
        # detail). Server-wide because the selected window is; each STATE card
        # is sent it when it differs from what that card was last sent.
        self._state_reading: tuple[int, str] = (STATE_UNKNOWN, "")
        self._state_window: str | None = None
        # window_id -> (background id, monotonic time opened), for windows this
        # server opened with `claude attach`. The attached session's own pid is
        # the background worker's, not a descendant of the pane, so this map is
        # the only way such a session can be known to be HERE.
        #
        # Kept across a tmux query that fails. It is rebuilt from nothing --
        # the attach client in the pane is not the session's process, so no
        # later pane walk can find it again -- which made one timed-out
        # list-windows cost every attach window its HERE badge for good.
        # Entries leave only when a later successful listing shows their
        # window gone, or when that listing is from another tmux lifetime
        # (``_remote_windows_epoch``, the lifetime the ids were seen in).
        self._remote_windows: dict[str, tuple[str, float]] = {}
        self._remote_windows_epoch: sessions.TmuxEpoch | None = None
        # window_id -> (client, what was started, monotonic time opened):
        # windows a pick opened whose survival has not been checked yet
        # (REMOTE_START_CHECK_SECONDS).
        self._remote_starting: dict[str, tuple[ClientState, str, float]] = {}
        # When the current run of failed `claude agents` asks began, or None
        # while the last one answered (AGENTS_FAILING_SECONDS).
        self._agents_failing_since: float | None = None

    def _remote_offered(self) -> bool:
        return self.remote_enabled

    def _stop_agents(self) -> None:
        self._agents.stop()
