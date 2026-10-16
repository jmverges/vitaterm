"""The whole stack at once: tmux -> PTY -> emulator -> wire -> client.

Every other suite tests one seam. This one runs the arrangement the handheld
actually talks to -- a child inside the dedicated tmux server, the real Session
driving pyte, the real Server framing it, and the real Client parsing it back --
and asserts on what a person would see.

It exists because the two worst bugs in this project were both invisible to the
per-seam tests. The emulator crashed on an escape sequence tmux sends at startup
(nothing else fed it real tmux output), and Claude Code's colour arrived white
(nothing else fed it a real 256-colour SGR). Both are one assertion here.

Skipped without tmux, which is not a way of excusing the gap: the tmux path is
the shipped configuration, so a run without it is a partial run and says so.
"""

import shutil
import threading
import time

import pytest

from dsterm.client import Client
from dsterm.grid import COLS, ROWS
from dsterm.serve import tmux_command

from test_server import ServerHarness, wait_for_text

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="tmux is not installed; the shipped configuration cannot be exercised",
)

SOCKET = "dstermtest"


def tmux_session(name: str, command: list[str]) -> list[str]:
    """A child inside a throwaway tmux server, never the real dsterm socket."""
    return tmux_command(name, command, COLS, ROWS, socket=SOCKET)


def kill_socket() -> None:
    import subprocess

    subprocess.run(["tmux", "-L", SOCKET, "kill-server"], capture_output=True)


@pytest.fixture(autouse=True)
def _clean_socket():
    kill_socket()
    yield
    kill_socket()


# Through tmux, history comes from tmux itself (capture-pane), not from the
# emulator's history -- tmux repaints by addressing cells, so whether pyte
# caught anything was a property of the box: tmux 3.7b yielded 67 lines for a
# 90-line child, tmux 3.4 yielded 0. The pane's own history is authoritative
# on every version, so these tests run unconditionally wherever tmux exists.
# See docs/design/SCROLLBACK_DRAIN_RATE.md and docs/design/SCROLLBACK_MEASUREMENT.md.


class TestThroughTmux:
    def test_a_session_inside_tmux_reaches_the_client(self):
        # tmux 3.7 sends CSI ? 6 n during startup, which used to raise straight
        # out of stream.feed() and kill the server's read loop -- the screen
        # froze and the handheld showed a still image of a live session.
        argv = tmux_session("e2e-basic", ["bash", "--noprofile", "--norc", "-i"])
        with ServerHarness(command=argv) as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c.send_keys(b"echo through-tmux-9f2\r")
                assert wait_for_text(c, "through-tmux-9f2", timeout=20)
                assert h.server._session.emulator_errors == 0, (
                    h.server._session.last_emulator_error
                )

    def test_claude_s_colour_survives_the_whole_pipeline(self):
        # The regression the user reported as "the crab is white".
        #
        # 256-colour 174 is what Claude Code paints its mark, its box rules and
        # its prompt in. It has to leave the child as an SGR sequence, survive
        # tmux, be quantised by the emulator, pack into four bits of an
        # attribute byte, cross the wire and come back out the other side still
        # recognisable. Nothing but a full-stack test covers that chain.
        argv = tmux_session(
            "e2e-colour",
            ["bash", "--noprofile", "--norc", "-c",
             r"printf '\033[38;5;174mCLAUDEMARK\033[39m\n'; sleep 30"],
        )
        with ServerHarness(command=argv) as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                assert wait_for_text(c, "CLAUDEMARK", timeout=20)
                row = next(
                    r for r in range(ROWS)
                    if "CLAUDEMARK" in "".join(
                        c.grid.cells[r * COLS + x].char for x in range(COLS)
                    )
                )
                col = "".join(
                    c.grid.cells[row * COLS + x].char for x in range(COLS)
                ).index("CLAUDEMARK")
                cell = c.grid.cells[row * COLS + col]
                assert cell.fg == "orange", (
                    f"Claude's 256-colour 174 arrived as {cell.fg!r}; "
                    "white means the quantiser lost the hue again"
                )

    def test_scrollback_reaches_back_past_the_screen(self):
        argv = tmux_session(
            "e2e-scroll",
            ["bash", "--noprofile", "--norc", "-c",
             # Paced, not one burst. Whether tmux passes a burst on as
             # scrolling or folds it into one repaint depends on how fast the
             # server drains it (docs/design/SCROLLBACK_DRAIN_RATE.md), so a 90-line
             # instant burst made this test a measure of the CI runner's load:
             # it failed 2 runs in 5 on GitHub with 2 and 11 lines of history.
             # Real output -- Claude Code's included -- arrives over time;
             # ten milliseconds a line is still far faster than anyone reads.
             "for i in $(seq 1 90); do echo scrollback-line-$i; sleep 0.01; done; sleep 30"],
        )
        with ServerHarness(command=argv) as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                assert wait_for_text(c, "scrollback-line-90", timeout=20)
                # Line 1 is long gone off a screen only ROWS tall.
                assert "scrollback-line-1\n" not in c.grid.to_text()

                # More than one screen of history, which is the property that
                # matters. Not an exact count: how many of the 90 lines have
                # left the screen depends on where the shell's prompt landed
                # and how much of the run the emulator has consumed, and
                # pinning a number makes this fail under load for no reason.
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if h.server._session.scrollback_lines > ROWS:
                        break
                    c.poll(0.2)
                available = h.server._session.scrollback_lines
                assert available > ROWS, (
                    f"only {available} lines of history, less than one screen"
                )

                # Walk back far enough to see the top of the run, then home.
                c.send_view(-available)
                assert wait_for_text(c, "scrollback-line-1", timeout=10)
                c.send_view(available)
                assert wait_for_text(c, "scrollback-line-90", timeout=10)

    def test_the_view_position_is_reported_and_returns_to_live(self):
        argv = tmux_session(
            "e2e-viewpos",
            ["bash", "--noprofile", "--norc", "-c",
             "for i in $(seq 1 60); do echo pos-line-$i; done; sleep 30"],
        )
        with ServerHarness(command=argv) as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                assert wait_for_text(c, "pos-line-60", timeout=20)
                c.send_view(-12)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    c.poll(0.2)
                    if c.view_pos is not None and c.view_pos[0] > 0:
                        break
                assert c.view_pos is not None, "no VIEW_POS after scrolling"
                back, available = c.view_pos
                assert back > 0 and available >= back

                # A press that cannot move must still answer, or the handheld
                # cannot tell a dead button from the top of the history.
                c.send_view(-available * 2)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    c.poll(0.2)
                    if c.view_pos and c.view_pos[0] == c.view_pos[1]:
                        break
                assert c.view_pos[0] == c.view_pos[1], "did not clamp to the top"

                # Typing returns to live without a VIEW request.
                c.send_keys(b"\r")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    c.poll(0.2)
                    if c.view_pos and c.view_pos[0] == 0:
                        break
                assert c.view_pos[0] == 0, "a keystroke did not return to live"

    def _wait_view(self, c, back, timeout=10.0):
        """Poll until VIEW_POS reports `back` lines back, and return (back, avail)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            c.poll(0.1)
            if c.view_pos is not None and c.view_pos[0] == back:
                return c.view_pos
        return None

    def _anch_numbers(self, c):
        text = c.grid.to_text()
        import re
        return [int(m.group(1)) for m in re.finditer(r"anch-(\d+)", text)]

    def test_a_page_back_shows_the_exact_lines_tmux_kept(self):
        argv = tmux_session(
            "e2e-exact",
            ["bash", "--noprofile", "--norc", "-c",
             "for i in $(seq 1 120); do echo hist-line-$i; done; sleep 30"],
        )
        with ServerHarness(command=argv) as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                assert wait_for_text(c, "hist-line-120", timeout=20)

                c.send_view(-24)
                assert self._wait_view(c, 24) is not None, "view never moved"
                back, available = c.view_pos
                assert available >= 90, (
                    f"tmux kept only {available} lines of a 120-line run")
                # The pane's history is consecutive output lines, so the row
                # at the top of a full-page-back view is the echo that sat
                # `back` lines above the live screen.
                want = f"hist-line-{available - back + 1}"
                assert c.grid.to_text().split("\n")[0].startswith(want), (
                    f"top row is {c.grid.to_text().split(chr(10))[0]!r}, "
                    f"expected {want!r}")

                # Repeated presses reach the top and clamp there -- the view
                # still answers, which is how the handheld knows it is done.
                c.send_view(-available * 3)
                assert self._wait_view(c, available) is not None, (
                    "did not clamp to the top")
                assert c.grid.to_text().split("\n")[0].startswith("hist-line-1")
                c.send_view(-10)
                assert self._wait_view(c, available) is not None

    def test_the_view_stays_anchored_while_output_arrives(self):
        # A timed second burst, not a typed one: a keystroke returns the view
        # to live, which is a different rule. The ten new lines push history
        # ten deeper, so holding the same lines means the offset grows by ten
        # -- and one more press is one line further back, not eleven.
        argv = tmux_session(
            "e2e-anchor",
            ["bash", "--noprofile", "--norc", "-c",
             "for i in $(seq 1 80); do echo anch-$i; done; sleep 4; "
             "for i in $(seq 81 90); do echo anch-$i; done; sleep 30"],
        )
        with ServerHarness(command=argv) as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                assert wait_for_text(c, "anch-80", timeout=20)

                c.send_view(-24)
                assert self._wait_view(c, 24) is not None
                first = self._anch_numbers(c)
                assert first, "no numbered lines in the captured view"

                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if h.server._session.pane_history_size() > 80:
                        break
                    c.poll(0.2)

                c.send_view(-1)
                assert self._wait_view(c, 35) is not None, (
                    f"expected the anchored offset 24+10+1; "
                    f"view_pos is {c.view_pos}")
                second = self._anch_numbers(c)
                assert second
                assert min(second) == min(first) - 1, (
                    "the view drifted with the new output: "
                    f"{min(first)} then {min(second)}")

    def test_colours_survive_the_capture(self):
        argv = tmux_session(
            "e2e-capcolour",
            ["bash", "--noprofile", "--norc", "-c",
             "for i in $(seq 1 40); do echo cap-$i; done; "
             r"printf '\033[31mREDMARK\033[0m\n'; "
             "for i in $(seq 41 80); do echo cap-$i; done; sleep 30"],
        )
        with ServerHarness(command=argv) as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                assert wait_for_text(c, "cap-80", timeout=20)
                c.send_view(-24)
                assert self._wait_view(c, 24) is not None
                text = c.grid.to_text()
                if "REDMARK" not in text:
                    # Not far enough: go to the top and it must be there.
                    c.send_view(-(c.view_pos[1]))
                    assert self._wait_view(c, c.view_pos[1]) is not None
                    text = c.grid.to_text()
                row = next(r for r in range(ROWS)
                           if "REDMARK" in "".join(
                               c.grid.cells[r * COLS + x].char
                               for x in range(COLS)))
                col = "".join(
                    c.grid.cells[row * COLS + x].char
                    for x in range(COLS)).index("REDMARK")
                assert c.grid.cells[row * COLS + col].fg == "red", (
                    "the captured line lost its colour")

    def test_a_keystroke_discards_a_capture_still_in_flight(self):
        argv = tmux_session(
            "e2e-stale",
            ["bash", "--noprofile", "--norc", "-c",
             "for i in $(seq 1 80); do echo stale-$i; done; sleep 30"],
        )
        with ServerHarness(command=argv) as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                assert wait_for_text(c, "stale-80", timeout=20)
                c.send_view(-24)
                assert self._wait_view(c, 24) is not None

                sess = h.server._session
                started = threading.Event()
                release = threading.Event()
                real = sess.capture_view

                def slow(offset, history):
                    started.set()
                    release.wait(10)
                    return real(offset, history)

                sess.capture_view = slow
                try:
                    c.send_view(-10)   # a second request, captured slowly
                    assert started.wait(5), "worker never started the capture"
                    c.send_keys(b"\r")  # typing returns to live: bumps the gen
                    release.set()
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        c.poll(0.1)
                        me = h.server._authed()
                        if me is not None and me.scroll.offset == 0:
                            break
                    me = h.server._authed()
                    assert me is not None
                    assert me.scroll.offset == 0 and me.scroll.anchor is None
                    assert me.scroll.grid is None
                finally:
                    release.set()
                    sess.capture_view = real

    def test_a_failed_capture_leaves_the_view_where_it_was(self):
        argv = tmux_session(
            "e2e-capfail",
            ["bash", "--noprofile", "--norc", "-c",
             "for i in $(seq 1 80); do echo cf-$i; done; sleep 30"],
        )
        with ServerHarness(command=argv) as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                assert wait_for_text(c, "cf-80", timeout=20)
                c.send_view(-24)
                assert self._wait_view(c, 24) is not None
                where = c.grid.to_text()

                sess = h.server._session
                real = sess.capture_view
                sess.capture_view = lambda offset, history: None
                try:
                    c.send_view(-10)
                    assert h.wait_for_event("view capture failed", timeout=10)
                    me = h.server._authed()
                    assert me.scroll.offset == 24
                    assert me.scroll.anchor is not None
                    assert me.scroll.grid is not None
                    # The request was still answered, and the frozen capture
                    # -- not a silent live screen -- is what is shown.
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        c.poll(0.1)
                        if c.view_pos and c.view_pos[0] == 24:
                            break
                    assert c.view_pos[0] == 24
                    assert c.grid.to_text() == where
                finally:
                    sess.capture_view = real

    def test_a_raising_capture_job_does_not_kill_the_worker(self):
        argv = tmux_session(
            "e2e-workerdie",
            ["bash", "--noprofile", "--norc", "-c",
             "for i in $(seq 1 80); do echo wk-$i; done; sleep 30"],
        )
        with ServerHarness(command=argv) as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                assert wait_for_text(c, "wk-80", timeout=20)

                sess = h.server._session
                real = sess.pane_history_size
                calls = []

                def boom(force=False):
                    calls.append(1)
                    if len(calls) == 1:
                        raise RuntimeError("tmux went away")
                    return real(force=force)

                sess.pane_history_size = boom
                try:
                    c.send_view(-24)
                    assert h.wait_for_event("view capture failed", timeout=10)
                    me = h.server._authed()
                    assert me.scroll.offset == 0

                    c.send_view(-24)
                    assert self._wait_view(c, 24) is not None, (
                        "the worker died with the failed job")
                finally:
                    sess.pane_history_size = real

    def test_rapid_scroll_presses_all_land(self):
        # Presses faster than a capture must accumulate, not collide on the
        # anchor: three taps back are thirty lines, not ten.
        argv = tmux_session(
            "e2e-rapid",
            ["bash", "--noprofile", "--norc", "-c",
             "for i in $(seq 1 200); do echo rapid-$i; done; sleep 60"],
        )
        with ServerHarness(command=argv) as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                assert wait_for_text(c, "rapid-200", timeout=20)

                for _ in range(3):
                    c.send_view(-10)
                assert self._wait_view(c, 30, timeout=5) is not None, (
                    f"three presses of -10 left the view at {c.view_pos}")
                assert c.view_pos[1] >= 100

                c.send_keys(b"\r")
                assert self._wait_view(c, 0) is not None

                for _ in range(20):
                    c.send_view(-5)
                want = min(100, c.view_pos[1] if c.view_pos else 100)
                assert self._wait_view(c, want, timeout=5) is not None, (
                    f"twenty presses of -5 left the view at {c.view_pos}")

                # Forward bursts land the same way.
                for _ in range(3):
                    c.send_view(10)
                assert self._wait_view(c, want - 30, timeout=5) is not None

    def test_a_pending_scroll_survives_a_keystroke(self):
        # A capture in flight, a queued scroll, a KEY and another scroll all
        # before the capture finishes: the KEY cancels the queued scroll and
        # the stale result, and the newer scroll still runs.
        argv = tmux_session(
            "e2e-pending",
            ["bash", "--noprofile", "--norc", "-c",
             "for i in $(seq 1 80); do echo pend-$i; done; sleep 60"],
        )
        with ServerHarness(command=argv) as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                assert wait_for_text(c, "pend-80", timeout=20)

                sess = h.server._session
                started = threading.Event()
                release = threading.Event()
                real = sess.capture_view

                def slow(offset, history):
                    started.set()
                    release.wait(15)
                    return real(offset, history)

                sess.capture_view = slow
                try:
                    c.send_view(-10)
                    assert started.wait(5), "worker never started the capture"
                    c.send_view(-10)          # queued behind the capture
                    c.send_keys(b"\r")        # returns to live, drops the queue
                    c.send_view(-5)           # the scroll that must survive
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        c.poll(0.1)
                        me = h.server._authed()
                        if me is not None and me.scroll.pending == -5:
                            break
                    me = h.server._authed()
                    assert me is not None
                    assert me.scroll.pending == -5 and me.scroll.anchor is None, (
                        f"pending={me.scroll.pending} anchor={me.scroll.anchor}")
                    release.set()
                    assert self._wait_view(c, 5, timeout=10) is not None, (
                        f"the post-KEY scroll never landed: {c.view_pos}")
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        me = h.server._authed()
                        if (me is not None and me.scroll.pending == 0
                                and not me.scroll.inflight):
                            break
                        c.poll(0.1)
                    assert me.scroll.pending == 0 and not me.scroll.inflight
                finally:
                    release.set()
                    sess.capture_view = real

    def test_the_select_loop_does_not_wait_on_tmux(self):
        argv = tmux_session(
            "e2e-nonblock",
            ["bash", "--noprofile", "--norc", "-c",
             "for i in $(seq 1 80); do echo nb-$i; done; sleep 30"],
        )
        with ServerHarness(command=argv) as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                assert wait_for_text(c, "nb-80", timeout=20)

                sess = h.server._session
                started = threading.Event()
                release = threading.Event()
                real = sess.capture_view

                def slow(offset, history):
                    started.set()
                    release.wait(10)
                    return real(offset, history)

                sess.capture_view = slow
                try:
                    c.send_view(-10)
                    assert started.wait(5), "worker never started the capture"
                    before = c.pongs
                    c.send_ping()
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline and c.pongs == before:
                        c.poll(0.05)
                    # The PONG itself, not just any message: screen traffic
                    # alone once let this pass with PING handling removed.
                    assert c.pongs > before, (
                        "a PING went unanswered while a capture was pending: "
                        "the loop is blocking on tmux")
                    assert not release.is_set() and started.is_set(), (
                        "the capture must still be blocked when the PONG lands")
                finally:
                    release.set()
                    sess.capture_view = real
