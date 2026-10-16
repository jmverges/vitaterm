"""Session switching, against the contract the client half implements.

Two asymmetries make this different from every other feature here.

SESSIONS (0x97) is a coordinated rollout with no capability token. An older card
cannot parse it and there is nothing to hide behind, so server and card ship
together -- which is only acceptable because the alternative, a speculative
token, would have to be invented before either side existed.

SESSION_PICK (0x15) is gated the other way round. An unknown CLIENT->SERVER type
makes the frame reader raise and the connection drop, which a reconnecting card
turns into a loop. So the card sends a pick only after receiving a complete
list, and receiving the list is itself the proof the server understands picks.

The index in a pick means "the row you showed me", nothing more. It is never a
tmux window number, a name, a path or a persistent identity -- and a row that has
gone away produces an ERROR rather than the nearest survivor, because switching
someone to a session they did not touch is worse than telling them it vanished.
"""

import shutil
import subprocess
import time

import pytest

from dsterm import sessions
from dsterm.client import Client
from dsterm.protocol import (
    FEATURE_MODE,
    HEADER_SIZE,
    SESSION_ACTIVE,
    SESSION_FLAGS,
    SESSION_NEW_OUTPUT,
    SESSION_RUNNING,
    SESSIONS_MAX,
    MsgType,
    ProtocolError,
    decode_session,
    decode_session_pick,
    encode_session,
    encode_session_pick,
    encode_sessions_empty,
)
from dsterm.grid import COLS, ROWS
from dsterm.serve import tmux_command

from test_server import ServerHarness, wait_for_text

SOCKET = "dstermtest-m6"


def tmux(*args):
    return subprocess.run(["tmux", "-L", SOCKET, *args],
                          capture_output=True, text=True)


class TestTheWireFormat:
    def test_the_empty_list_is_one_zero_byte(self):
        # A distinct shape rather than silence: "no sessions" and "the server
        # has not said" have to look different on the handheld.
        assert encode_sessions_empty() == bytes.fromhex("445301970001") + b"\x00"
        assert decode_session(b"\x00") == (0, 0, 0, 0, "")

    def test_the_exact_entry_frame(self):
        # count 2, index 0, RUNNING|ACTIVE, idle 5s, name "claude"
        blob = encode_session(0, 2, SESSION_RUNNING | SESSION_ACTIVE, 5, "claude")
        assert blob == bytes.fromhex("44530197000c" "02" "00" "03" "0005" "06") + b"claude"

    def test_round_trip(self):
        blob = encode_session(1, 3, SESSION_NEW_OUTPUT, 4242, "shell")
        assert decode_session(blob[HEADER_SIZE:]) == (3, 1, SESSION_NEW_OUTPUT, 4242, "shell")

    def test_a_maximum_entry_clears_the_ceiling(self):
        blob = encode_session(7, 8, SESSION_FLAGS, 0xFFFF,
                              "x" * sessions.NAME_MAX)
        assert len(blob) == HEADER_SIZE + 6 + sessions.NAME_MAX
        assert len(blob) < 536

    def test_idle_saturates_rather_than_wrapping(self):
        blob = encode_session(0, 1, 0, 999_999, "n")
        assert decode_session(blob[HEADER_SIZE:])[3] == 0xFFFF

    @pytest.mark.parametrize("index,count", [(0, 0), (0, 9), (2, 2), (5, 3)])
    def test_encode_refuses_impossible_rows(self, index, count):
        with pytest.raises(ProtocolError):
            encode_session(index, count, 0, 0, "n")

    @pytest.mark.parametrize("flags", [0x08, 0x80, 0xFF])
    def test_encode_refuses_reserved_flag_bits(self, flags):
        with pytest.raises(ProtocolError):
            encode_session(0, 1, flags, 0, "n")

    @pytest.mark.parametrize("payload", [
        b"",
        b"\x00\x00",                         # empty form may be one byte only
        b"\x02\x00\x03\x00\x05",              # truncated before name_len
        b"\x02\x00\x03\x00\x05\x06cla",       # name shorter than declared
        b"\x02\x00\x03\x00\x05\x00",          # zero name length
        b"\x09\x00\x03\x00\x05\x01n",         # count above the maximum
        b"\x02\x05\x03\x00\x05\x01n",         # index not below count
        b"\x02\x00\x80\x00\x05\x01n",         # reserved flag bit
        b"\x02\x00\x03\x00\x05\x19" + b"x" * 25,   # name_len above the cap
    ])
    def test_decode_rejects_malformed_entries(self, payload):
        with pytest.raises(ProtocolError):
            decode_session(payload)

    def test_pick_is_exactly_one_byte(self):
        assert encode_session_pick(3) == bytes.fromhex("445301150001") + b"\x03"
        assert decode_session_pick(b"\x03") == 3

    @pytest.mark.parametrize("payload", [b"", b"\x00\x00", b"\x01\x02\x03"])
    def test_pick_rejects_any_other_length(self, payload):
        with pytest.raises(ProtocolError):
            decode_session_pick(payload)

    def test_names_are_clamped_to_printable_ascii(self):
        assert sessions.clamp_name("cla\x00ude\x1b[31m") == "claude[31m"
        assert len(sessions.clamp_name("x" * 100)) == sessions.NAME_MAX
        assert sessions.clamp_name("\x00\x01") == "?"


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
class TestAgainstRealWindows:
    """Two real tmux windows: Claude-Code-shaped and shell-shaped.

    The alt-screen window stands in for Claude Code, because the property that
    matters on a switch is that the scrolling regime differs between the two --
    a cached MODE from the window you left makes a physical button do the wrong
    thing on the window you arrived at.
    """

    @pytest.fixture(autouse=True)
    def _clean(self):
        tmux("kill-server")
        yield
        tmux("kill-server")

    def _harness(self):
        argv = tmux_command("m6", ["bash", "--noprofile", "--norc", "-i"],
                            COLS, ROWS, socket=SOCKET)
        return ServerHarness(command=argv)

    def _wait_rows(self, h, c, n=1, timeout=15, predicate=None):
        """Wait until the client has been advertised at least `n` rows.

        The list right after auth is legitimately empty: the child's tmux server
        is still starting, and "no sessions yet" is a true statement the client
        must be able to receive. The populated list arrives from the
        state-change poll, which is the same path a window opening at any other
        moment takes -- so waiting here exercises the real mechanism rather than
        papering over a race.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            c.poll(0.2)
            if h.server._clients:
                rows = h.server._clients[0].sessions.rows
                if len(rows) >= n and (predicate is None or predicate(rows)):
                    return rows
        raise AssertionError(f"never advertised {n} session row(s)")

    def _collect(self, h, after_event=None):
        seen = []
        real = h.server._send_to

        def spy(c, data):
            ready = after_event is None or any(after_event in e for e in h.events)
            if ready and len(data) > 3 and data[3] in (
                    MsgType.FRAME_DIFF, MsgType.CURSOR, MsgType.SESSIONS,
                    MsgType.MODE, MsgType.VIEW_POS):
                seen.append(data)
            real(c, data)

        h.server._send_to = spy
        return seen

    def _inactive_pick(self, h, rows):
        snapshot = h.server._last_windows
        assert snapshot is not None
        active = next(w.window_id for w in snapshot.windows if w.active)
        return next(i for i, row in enumerate(rows) if row.window_id != active)

    def _drain_switch(self, h, c, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            c.poll(0.05)
            state = h.server._clients[0]
            if not state.sessions.switch_pending and state.sessions.switch_barrier is None:
                # A final poll gives the reference client the frames just
                # handed to the local socket on the last paced tick.
                c.poll(0.1)
                return
        raise AssertionError("switch barrier did not drain")

    def test_a_complete_list_arrives_after_auth(self):
        with self._harness() as h:
            with Client("127.0.0.1", h.port, h.key, features=(FEATURE_MODE,)) as c:
                seen = self._collect(h)
                self._wait_rows(h, c)
                h.server._clients[0].sessions.signature = []   # force one more
                self._wait_rows(h, c)
                c.poll(0.5)
            rows = [decode_session(m[HEADER_SIZE:]) for m in seen
                    if m[3] == MsgType.SESSIONS and m[HEADER_SIZE] != 0]
            assert rows, "no session list after authentication"
            # Every list that arrived is checked, however many: the first after
            # authentication is empty (filtered out above) only while tmux is
            # still starting, and on a fast machine it is already populated --
            # then the forced one makes two. Counting both as one list failed
            # this test nine times in ten on an idle machine.
            lists, current = [], []
            for r in rows:
                if r[1] == 0 and current:
                    lists.append(current)
                    current = []
                current.append(r)
            lists.append(current)
            for entries in lists:
                counts = {r[0] for r in entries}
                assert len(counts) == 1, "entries disagreed about the count"
                count = counts.pop()
                assert sorted(r[1] for r in entries) == list(range(count)), (
                    "the advertised list was not complete")
                assert sum(1 for r in entries if r[2] & SESSION_ACTIVE) == 1
                assert all(r[2] & SESSION_RUNNING for r in entries)

    def test_switching_between_regimes_resends_mode_and_view_pos(self):
        with self._harness() as h:
            with Client("127.0.0.1", h.port, h.key, features=(FEATURE_MODE,)) as c:
                self._wait_rows(h, c)
                # A second window that takes the alternate screen: the regime
                # differs from the shell, which is the point of the test.
                tmux("new-window", "-d", "-t", "m6:", "-n", "altapp",
                     "bash", "--noprofile", "--norc", "-c",
                     "printf '\\033[?1049h'; sleep 30")
                rows = self._wait_rows(h, c, n=2)

                seen = self._collect(h, after_event="switched to session")
                target = self._inactive_pick(h, rows)
                c._send(encode_session_pick(target))
                assert h.wait_for_event("switched to session")
                self._drain_switch(h, c)

        kinds = [m[3] for m in seen]
        view = kinds.index(MsgType.VIEW_POS)
        mode = kinds.index(MsgType.MODE)
        assert all(kind in (MsgType.FRAME_DIFF, MsgType.CURSOR)
                   for kind in kinds[:view])
        assert mode == view + 1
        assert all(kind == MsgType.SESSIONS for kind in kinds[mode + 1:])
        assert MsgType.MODE in kinds, "no MODE after a switch"
        assert MsgType.SESSIONS in kinds, "no fresh list after a switch"
        rows = [decode_session(m[HEADER_SIZE:]) for m in seen
                if m[3] == MsgType.SESSIONS]
        assert sum(1 for r in rows if r[2] & SESSION_ACTIVE) == 1
        assert not any(r[2] & SESSION_NEW_OUTPUT and r[2] & SESSION_ACTIVE
                       for r in rows), "the target kept its NEW_OUTPUT mark"

    def test_mode_is_resent_even_when_the_value_is_unchanged(self):
        # The client clears its own MODE while waiting for a switch, so
        # "unchanged since last time" is measured against a value it has
        # already thrown away. Two shell windows: same regime, must still send.
        with self._harness() as h:
            with Client("127.0.0.1", h.port, h.key, features=(FEATURE_MODE,)) as c:
                self._wait_rows(h, c)
                tmux("new-window", "-d", "-t", "m6:", "-n", "second",
                     "bash", "--noprofile", "--norc", "-i")
                rows = self._wait_rows(h, c, n=2)
                before = c.mode_flags

                seen = self._collect(h, after_event="switched to session")
                c._send(encode_session_pick(self._inactive_pick(h, rows)))
                assert h.wait_for_event("switched to session")
                self._drain_switch(h, c)

        modes = [m for m in seen if m[3] == MsgType.MODE]
        assert modes, "MODE was suppressed as unchanged across a switch"
        assert before is not None

    def test_switching_returns_the_view_to_live(self):
        with self._harness() as h:
            with Client("127.0.0.1", h.port, h.key, features=(FEATURE_MODE,)) as c:
                c.poll(0.8)
                c.send_keys(b"for i in $(seq 1 60); do echo m6-line-$i; done\n")
                assert wait_for_text(c, "m6-line-60", timeout=15)
                c.send_view(-20)
                time.sleep(0.6)
                c.poll(0.5)

                tmux("new-window", "-d", "-t", "m6:", "-n", "second",
                     "bash", "--noprofile", "--norc", "-i")
                time.sleep(1.2)
                c.poll(0.5)

                state = h.server._clients[0]
                rows = state.sessions.rows
                c._send(encode_session_pick(self._inactive_pick(h, rows)))
                assert h.wait_for_event("switched to session")
                self._drain_switch(h, c)
                assert state.scroll.offset == 0, "the view stayed scrolled after a switch"

    def test_an_index_outside_the_advertised_list_errors(self):
        with self._harness() as h:
            with Client("127.0.0.1", h.port, h.key, features=(FEATURE_MODE,)) as c:
                c.poll(0.8)
                before = self._wait_rows(h, c)[:]
                c._send(encode_session_pick(SESSIONS_MAX - 1))
                assert h.wait_for_event("no such row")
                time.sleep(0.4)
                # Refused, and nothing switched underneath the user.
                assert h.server._clients[0].sessions.rows == before

    def test_a_pick_is_not_a_tmux_window_number(self):
        # Move the second window to display index 7. Its wire row is still 1,
        # so a server that passed that row number to tmux would select index 1
        # (or fail) rather than the advertised @window identity.
        with self._harness() as h:
            with Client("127.0.0.1", h.port, h.key, features=(FEATURE_MODE,)) as c:
                self._wait_rows(h, c)
                assert tmux("new-window", "-d", "-t", "m6:", "-n", "keep",
                            "bash", "--noprofile", "--norc", "-i").returncode == 0
                assert tmux("move-window", "-s", "m6:1", "-t", "m6:7").returncode == 0
                live_id = tmux("display-message", "-p", "-t", "m6:7",
                               "#{window_id}").stdout.strip()
                rows = self._wait_rows(
                    h, c, n=2,
                    predicate=lambda got: len(got) == 2 and got[1].window_id == live_id,
                )

                c._send(encode_session_pick(1))
                assert h.wait_for_event("switched to session")
                self._drain_switch(h, c)
                active = tmux("display-message", "-p", "-t", "m6",
                              "#{window_id}").stdout.strip()
                assert active == rows[1].window_id == live_id, (
                    f"wire index was used as a tmux target: active={active}")
