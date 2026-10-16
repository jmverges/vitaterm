"""End-to-end tests over a real socket against a real PTY.

Nothing here is mocked. The claim being tested is that a client which knows only
the wire format and the key can drive a shell and see the result -- and that one
which does not know the key cannot reach the PTY at all.
"""

import socket
import threading
import time

import pytest

from dsterm.auth import generate_key
from dsterm.client import AuthError, Client
from dsterm.grid import COLS, ROWS
from dsterm.protocol import MsgType, encode
from dsterm.server import Server


class ServerHarness:
    """Runs a Server on a background thread bound to an ephemeral port."""

    def __init__(self, command=None, key=None, auth_timeout=2.0, **server_kwargs):
        self.key = key or generate_key()
        self.command = command or ["bash", "--noprofile", "--norc", "-i"]
        self.events: list[str] = []
        # Short auth window for tests. The shipped default is deliberately
        # generous -- a handheld printing diagnostics between handshake steps is
        # slow -- but waiting that out here would make the suite crawl.
        self.server = Server(
            self.command,
            self.key,
            host="127.0.0.1",
            port=0,
            fps=30.0,
            on_event=self.events.append,
            auth_timeout=auth_timeout,
            **server_kwargs,
        )
        self._thread: threading.Thread | None = None

    def __enter__(self):
        self.server.start()
        self.port = self.server.bound_port
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"max_seconds": 25}, daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc):
        # The loop first, then the teardown, on one thread: stop() sends to
        # cards (the mirror's goodbye) and must not do it while the loop's
        # own send holds the same buffer -- that raced, rarely, as a
        # BufferError on the card's pending bytes.
        self.server._running = False
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.server.stop()

    def wait_for_event(self, needle: str, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(needle in e for e in self.events):
                return True
            time.sleep(0.02)
        return False


def wait_for_text(client: Client, needle: str, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client.poll(0.1)
        if needle in client.grid.to_text():
            return True
    return False


def wait_for_frames(client: Client, more_than: int, timeout: float = 8.0) -> bool:
    """Wait until the client has applied a frame beyond `more_than`.

    The counterpart to wait_for_text for cases where the screen content does not
    change: a resync of a screen the client already holds is invisible in the
    text but is exactly what is being tested.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client.poll(0.1)
        if client.frames_applied > more_than:
            return True
    return False


def stable_cursor(client: Client, seconds: float = 2.0, quiet: float = 0.4):
    """The cursor after it has stopped moving for `quiet` seconds.

    A caret moves several times while a command line is echoed and its output
    printed, so an immediate read would catch it mid-flight. This returns the
    position once it has held still, which is what an idle terminal shows.
    """
    deadline = time.monotonic() + seconds
    last = client.cursor
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        client.poll(0.1)
        if client.cursor != last:
            last = client.cursor
            last_change = time.monotonic()
        elif time.monotonic() - last_change >= quiet:
            return last
    return last


class TestHandshake:
    def test_correct_key_authenticates(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                assert c.grid is not None
            assert h.wait_for_event("authenticated")

    def test_wrong_key_is_rejected(self):
        with ServerHarness() as h:
            with pytest.raises(AuthError):
                Client("127.0.0.1", h.port, generate_key()).connect()
            assert h.wait_for_event("bad MAC")

    def test_full_frame_sent_on_auth(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                assert c.frames_applied >= 1
                assert len(c.grid.cells) == COLS * ROWS


class TestAuthorization:
    def test_keys_before_auth_drop_the_connection(self):
        # The whole point of the gate: no PTY access without the key.
        with ServerHarness() as h:
            sock = socket.create_connection(("127.0.0.1", h.port), timeout=5)
            sock.recv(4096)                       # the nonce
            sock.sendall(encode(MsgType.KEY, b"whoami\r"))
            assert h.wait_for_event("before authentication")
            sock.close()

    def test_resync_before_auth_drops_the_connection(self):
        with ServerHarness() as h:
            sock = socket.create_connection(("127.0.0.1", h.port), timeout=5)
            sock.recv(4096)
            sock.sendall(encode(MsgType.RESYNC))
            assert h.wait_for_event("before authentication")
            sock.close()

    def test_garbage_drops_the_connection(self):
        with ServerHarness() as h:
            sock = socket.create_connection(("127.0.0.1", h.port), timeout=5)
            sock.recv(4096)
            sock.sendall(b"\xff\xff\xff\xff\xff\xff\xff\xff")
            assert h.wait_for_event("protocol error")
            sock.close()

    def test_silent_client_is_expired(self):
        # Uses the harness's short auth window rather than the shipped default,
        # so raising that default does not break this test again.
        with ServerHarness(auth_timeout=1.0) as h:
            sock = socket.create_connection(("127.0.0.1", h.port), timeout=5)
            sock.recv(4096)
            assert h.wait_for_event("timed out", timeout=10)
            sock.close()

    def test_auth_timeout_default_is_generous(self):
        # The handheld prints diagnostics between handshake steps; hanging up at
        # ten seconds truncated the output that explained a real failure.
        from dsterm.server import AUTH_TIMEOUT
        assert AUTH_TIMEOUT >= 30.0


class TestTerminalOverTheWire:
    def test_keys_reach_the_pty_and_output_comes_back(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c.send_keys(b"echo wire-ok-4b7\r")
                assert wait_for_text(c, "wire-ok-4b7")

    def test_client_grid_matches_server_grid(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c.send_keys(b"echo mirror-check\r")
                assert wait_for_text(c, "mirror-check")
                time.sleep(0.4)
                c.poll(0.3)
                # The diff stream must reconstruct the server's screen exactly;
                # any drift here becomes permanent on-screen corruption.
                assert c.grid.to_text() == h.server._session.grid.to_text()

    def test_combining_marks_reach_the_card_and_the_server_keeps_going(self):
        # "x" + U+0301 has no composed form, so pyte keeps both in one cell;
        # folding that cell raised and ended the server's loop on its next
        # snapshot. Through the real PTY: the card gets the base letter, and
        # the server is still answering afterwards.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c.send_keys(b"printf 'mark-x\\314\\201-end\\n'\r")
                assert wait_for_text(c, "mark-x-end")
                c.send_keys(b"echo still-serving-7c2\r")
                assert wait_for_text(c, "still-serving-7c2")
                assert h.server._running

    def test_control_characters_are_delivered(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c.send_keys(b"echo before-ctrl\r")
                assert wait_for_text(c, "before-ctrl")
                c.send_keys(b"sleep 30\r")
                time.sleep(0.5)
                c.send_keys(b"\x03")            # Ctrl-C
                c.send_keys(b"echo after-ctrl\r")
                assert wait_for_text(c, "after-ctrl")


class TestScrollPrimitive:
    def test_incremental_scrolling_uses_the_scroll_message(self):
        # A scroll measures ~900 of 1008 cells. The primitive only applies
        # when the screen scrolls *between* ticks -- a burst that replaces the
        # whole screen in one tick is correctly sent as a full frame instead,
        # which is why this paces the output.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c.send_keys(
                    b"for i in $(seq 1 45); do echo scroll-$i-xyz; sleep 0.05; done\r"
                )
                assert wait_for_text(c, "scroll-45-xyz", timeout=20)
                assert c.scrolls_applied > 0

    def test_burst_output_falls_back_to_a_full_frame(self):
        # The complement: no SCROLL is claimed when the screen did not scroll.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                before = c.frames_applied
                c.send_keys(b"for i in $(seq 1 80); do echo burst-$i; done\r")
                assert wait_for_text(c, "burst-80", timeout=10)
                assert c.frames_applied > before

    def test_screen_still_matches_after_scrolling(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c.send_keys(b"for i in $(seq 1 40); do echo line-$i; done\r")
                assert wait_for_text(c, "line-40", timeout=10)
                time.sleep(0.5)
                c.poll(0.3)
                assert c.grid.to_text() == h.server._session.grid.to_text()


class TestSmallMessages:
    """Every message must fit one small TCP segment.

    Measured on hardware: the handheld acknowledged every small message and never
    acknowledged a single 3030-byte FRAME_FULL, retransmitting past 10 KB on a
    connection that stayed ESTABLISHED. Its MSS is 536, the IPv4 minimum. This is
    not a tuning preference -- exceed it and the screen never updates.
    """

    def test_message_size_stays_within_a_small_segment(self):
        from dsterm.server import CELLS_PER_MESSAGE
        from dsterm.protocol import DIFF_ENTRY_SIZE, HEADER_SIZE
        assert CELLS_PER_MESSAGE * DIFF_ENTRY_SIZE + HEADER_SIZE <= 536

    def test_server_never_emits_an_oversized_message(self):
        # Drives a real repaint and inspects every byte the server actually
        # writes, rather than trusting the constant above.
        sizes: list[int] = []

        with ServerHarness() as h:
            real_send = h.server._send_to

            def spy(c, data: bytes) -> None:
                sizes.append(len(data))
                real_send(c, data)

            h.server._send_to = spy
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c.send_keys(b"clear; for i in $(seq 1 40); do echo size-$i; done\r")
                assert wait_for_text(c, "size-40", timeout=10)
                time.sleep(0.4)
                c.poll(0.3)

        assert sizes, "server sent nothing"
        # Sealed: the spy sees frames before _send_to adds the 8-byte tag.
        from dsterm.protocol import SEAL_TAG_SIZE
        assert max(sizes) + SEAL_TAG_SIZE <= 536, f"largest message was {max(sizes)} bytes"

    def test_a_full_resync_still_reconstructs_the_whole_screen(self):
        # The screen now arrives as many small diffs instead of one full frame;
        # the result must be identical.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c.send_keys(b"echo resync-whole-screen\r")
                assert wait_for_text(c, "resync-whole-screen")
                c.request_resync()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    c.poll(0.2)
                    if c.grid.to_text() == h.server._session.grid.to_text():
                        break
                assert c.grid.to_text() == h.server._session.grid.to_text()


class TestPacing:
    """Screen updates are released a few per tick, never as one burst.

    Worth being clear that this did not fix the stall it was written for -- the
    handheld was not polling its socket at all, so no sending pattern would have
    reached it. It stays for the constraint that is independently true: the DS
    drains a small Wi-Fi receive buffer once per VBlank, and the release rate is
    derived from that rather than chosen.
    """

    def _screen_sends_per_tick(self, h) -> list[int]:
        """Count FRAME_DIFF/SCROLL sends falling between consecutive ticks."""
        from dsterm.protocol import MsgType

        windows = [0]
        real_send, real_tick = h.server._send_to, h.server._tick

        def spy_send(c, data: bytes) -> None:
            if data[3] in (MsgType.FRAME_DIFF, MsgType.SCROLL):
                windows[-1] += 1
            real_send(c, data)

        def spy_tick() -> None:
            windows.append(0)
            real_tick()

        h.server._send_to = spy_send
        h.server._tick = spy_tick
        return windows

    def test_no_tick_releases_more_than_the_pacing_limit(self):
        from dsterm.server import MESSAGES_PER_TICK

        with ServerHarness() as h:
            windows = self._screen_sends_per_tick(h)
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c.send_keys(b"clear; for i in $(seq 1 40); do echo pace-$i; done\r")
                assert wait_for_text(c, "pace-40", timeout=10)
                time.sleep(0.4)
                c.poll(0.3)

        assert sum(windows) > 0, "no screen updates were sent"
        assert max(windows) <= MESSAGES_PER_TICK

    def test_a_resync_spans_several_ticks(self):
        # The specific burst that failed on hardware: a whole screen at once.
        from dsterm.server import FULL_RESYNC_MESSAGES, MESSAGES_PER_TICK

        with ServerHarness() as h:
            windows = self._screen_sends_per_tick(h)
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                deadline = time.monotonic() + 5
                while sum(windows) < FULL_RESYNC_MESSAGES and time.monotonic() < deadline:
                    c.poll(0.2)

        assert sum(windows) >= FULL_RESYNC_MESSAGES
        used = len([w for w in windows if w])
        assert used >= FULL_RESYNC_MESSAGES // MESSAGES_PER_TICK

    def test_a_backlog_collapses_into_a_resync_without_corrupting_the_screen(self):
        # Output can outrun the release rate. The queue is then replaced with a
        # resync, which depends on nothing the client already holds -- so the
        # screen must still match afterwards, not merely survive.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c.send_keys(
                    b"for i in $(seq 1 400); do echo backlog-$i-padding-text; done\r"
                )
                assert wait_for_text(c, "backlog-400", timeout=20)
                assert h.wait_for_event("collapsing to a resync")

                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    c.poll(0.2)
                    if c.grid.to_text() == h.server._session.grid.to_text():
                        break
                assert c.grid.to_text() == h.server._session.grid.to_text()


class TestChildRestart:
    """The child exiting must not take the connection with it.

    Observed on hardware: the DS was driving a real session, the child later
    exited, the server shut down, and the console halted with
    "server closed (errno=128)". Recovering meant walking to the PC -- the exact
    trip this project exists to avoid.
    """

    def test_the_client_survives_the_child_exiting(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c.send_keys(b"echo before-exit-7d1\r")
                assert wait_for_text(c, "before-exit-7d1")

                c.send_keys(b"exit\r")
                assert h.wait_for_event("restarted", timeout=15)

                # Still connected, and driving the replacement shell.
                c.send_keys(b"echo after-restart-3e6\r")
                assert wait_for_text(c, "after-restart-3e6", timeout=15)

    def test_a_child_that_cannot_start_is_abandoned(self):
        # Restarting is right when a session ends and wrong when the command is
        # broken. Unthrottled, a child that dies instantly spins forever, and
        # each attempt clears the screen -- so the error explaining the failure
        # is wiped by the next attempt and nobody ever reads it. Observed for
        # real: `claude` was missing from the systemd PATH, tmux printed
        # "[exited]", and the loop erased it several times a second.
        from dsterm.server import CHILD_MAX_RAPID_FAILURES

        with ServerHarness(command=["false"]) as h:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not h.server.child_abandoned:
                time.sleep(0.05)

            assert h.server.child_abandoned
            assert h.wait_for_event("not restarting again")
            # Bounded, and the bound is the one that was configured.
            assert h.server.counters.child_restarts <= CHILD_MAX_RAPID_FAILURES

    def test_a_child_that_ran_a_while_is_still_restarted(self):
        # The complement: abandoning must not punish a session that worked and
        # then ended normally, which is the ordinary case of typing `exit`.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c.send_keys(b"echo lived-long-enough-6a4\r")
                assert wait_for_text(c, "lived-long-enough-6a4")
                time.sleep(5.2)          # outlive CHILD_MIN_LIFETIME

                c.send_keys(b"exit\r")
                assert h.wait_for_event("restarted", timeout=15)
                assert not h.server.child_abandoned

    def test_the_screen_is_resynced_rather_than_diffed(self):
        # The replacement child's screen shares nothing with the old one, so a
        # diff against the stale mirror would render as garbage.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c.send_keys(b"echo gone-after-exit-5b2\r")
                assert wait_for_text(c, "gone-after-exit-5b2")

                c.send_keys(b"exit\r")
                assert h.wait_for_event("restarted", timeout=15)

                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    c.poll(0.2)
                    if c.grid.to_text() == h.server._session.grid.to_text():
                        break
                assert c.grid.to_text() == h.server._session.grid.to_text()


class TestCursor:
    """The caret is display state, sent when it moves and not otherwise.

    Typing blind is what this feature exists to prevent -- without a cursor the
    DS keyboard would have no feedback -- so the server must
    report where the caret is, and must not spam it every tick either.
    """

    def test_cursor_is_emitted_when_it_moves(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                before = stable_cursor(c)
                assert before is not None, "client never received a cursor"

                # A command line is echoed and its output printed; the caret
                # ends up a line further down than it was.
                c.send_keys(b"echo cursor-move-9d2\r")
                assert wait_for_text(c, "cursor-move-9d2")
                after = stable_cursor(c)
                assert after != before, f"cursor did not move: {before} -> {after}"

    def test_cursor_not_emitted_when_static(self):
        # Asserting the client's cursor value is unchanged does not prove the
        # doc's "not when it does not" half: a server that emitted an identical
        # CURSOR frame every tick would leave the value untouched and pass.
        # Count the frames the server actually writes instead.
        from dsterm.protocol import MsgType

        cursor_frames: list[int] = []

        with ServerHarness() as h:
            real_send = h.server._send_to

            def spy(c, data: bytes) -> None:
                if data[3] == MsgType.CURSOR:
                    cursor_frames.append(len(data))
                real_send(c, data)

            h.server._send_to = spy
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                before = stable_cursor(c)
                assert before is not None
                baseline = len(cursor_frames)
                time.sleep(1.0)
                c.poll(0.5)
                assert c.cursor == before, "idle screen must not move the cursor"

        idle_frames = len(cursor_frames) - baseline
        assert idle_frames == 0, f"server sent {idle_frames} CURSOR frames while idle"

    def test_a_reconnecting_client_receives_the_cursor(self):
        # A resync must restate the caret, or a reconnecting client -- which is
        # the normal recovery path after a power cycle -- types blind.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as first:
                first.poll(0.5)
                first.send_keys(b"echo persist-cur-6a1\r")
                assert wait_for_text(first, "persist-cur-6a1")

            time.sleep(0.3)
            with Client("127.0.0.1", h.port, h.key) as second:
                second.poll(0.8)
                got = stable_cursor(second)
                assert got is not None
                assert got == h.server._session.cursor


class TestReconnect:
    def test_reconnect_resyncs_with_a_full_frame(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as first:
                first.poll(0.5)
                first.send_keys(b"echo persist-2c9\r")
                assert wait_for_text(first, "persist-2c9")

            time.sleep(0.3)
            # The PTY outlives the connection: the shell is still there and the
            # new client is given the whole screen rather than a diff against
            # something it never saw.
            #
            # The whole screen arrives as a paced sequence of diff messages (a
            # handful per tick -- that pacing is what survives the handheld's
            # stack), so a single poll() can catch only a prefix of it. Wait
            # until the pre-existing content actually reappears; a reconnect
            # that sent a diff instead of the full screen never would.
            with Client("127.0.0.1", h.port, h.key) as second:
                assert wait_for_text(second, "persist-2c9")
                assert second.frames_applied >= 1

    def test_explicit_resync_returns_the_screen(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c.send_keys(b"echo resync-me\r")
                assert wait_for_text(c, "resync-me")
                before = c.frames_applied
                c.request_resync()
                # Waiting on the text would be waiting for nothing: this client
                # already has "resync-me" on its grid from before the request,
                # so wait_for_text returns on the first poll and the assert below
                # races the first message of a resync paced over several ticks.
                # Frames arriving is the thing that actually distinguishes a
                # served resync from an ignored one.
                assert wait_for_frames(c, more_than=before)


class TestSessionOwnership:
    """One session, and the key decides who holds it.

    An earlier version refused every second connection outright. That looked
    safe and was not: the handheld can vanish without closing its socket -- off,
    battery out, out of range -- leaving an ESTABLISHED connection this side
    cannot distinguish from a live one. The console then came back to "server
    refused: busy" and stayed locked out until the server was restarted by hand.
    """

    def test_a_reconnecting_client_takes_over_from_a_stale_one(self):
        with ServerHarness() as h:
            first = Client("127.0.0.1", h.port, h.key)
            first.connect()
            first.poll(0.5)

            with Client("127.0.0.1", h.port, h.key) as second:
                assert h.wait_for_event("replaced by a newer authenticated client")
                second.poll(0.8)
                assert second.frames_applied >= 1
                second.send_keys(b"echo took-over-9f2\r")
                assert wait_for_text(second, "took-over-9f2")

            first.close()

    def test_an_unauthenticated_connection_cannot_displace_the_session(self):
        # The takeover is earned by proving the key, so occupying the spare slot
        # must not be enough to interrupt anyone.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as owner:
                owner.poll(0.5)

                intruder = socket.create_connection(("127.0.0.1", h.port), timeout=5)
                intruder.recv(4096)              # takes the nonce, answers nothing

                owner.send_keys(b"echo still-mine-4c8\r")
                assert wait_for_text(owner, "still-mine-4c8")
                intruder.close()

    def test_a_wrong_key_cannot_displace_the_session(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as owner:
                owner.poll(0.5)

                with pytest.raises(AuthError):
                    Client("127.0.0.1", h.port, generate_key()).connect()
                assert h.wait_for_event("bad MAC")

                owner.send_keys(b"echo unbothered-1a5\r")
                assert wait_for_text(owner, "unbothered-1a5")

    def test_a_third_connection_is_refused(self):
        # The slots are bounded: a session plus one challenger.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as owner:
                owner.poll(0.5)

                challenger = socket.create_connection(("127.0.0.1", h.port), timeout=5)
                challenger.recv(4096)

                with pytest.raises((AuthError, ConnectionError, OSError)):
                    Client("127.0.0.1", h.port, h.key).connect()
                assert h.wait_for_event("no free slot")
                challenger.close()


class TestWorkerLifecycle:
    """The view worker is a daemon, but a server must still let it finish."""

    def test_stop_wakes_the_view_worker_on_a_never_started_server(self):
        # Constructing a Server is enough to start the thread; stopping a
        # server that never ran must still let it exit.
        server = ServerHarness().server
        try:
            thread = server._view_thread
            assert thread.is_alive()
        finally:
            server.stop()
        thread.join(timeout=3)
        assert not thread.is_alive(), "the view worker never woke for shutdown"

    def test_the_view_worker_exits_with_a_running_server(self):
        with ServerHarness() as h:
            thread = h.server._view_thread
            assert thread.is_alive()
        thread.join(timeout=3)
        assert not thread.is_alive(), "the view worker survived server.stop()"
