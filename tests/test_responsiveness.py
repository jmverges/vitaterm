"""The server answers while a program prints the most expensive output there is.

Emulating terminal output is the one piece of work on the server's loop
whose cost the program chooses: per byte it varies about a hundredfold. Ten
bytes of ``CSI 99999 S`` once cost a hundred thousand passes of index(), and
a stream of ``ESC # 8`` (fill the screen) costs ~240 ms a KB; either held a
card's PING for seconds. Session.pump now works to a time budget and keeps
the rest for the loop's next pass, and the scroll counts are capped.

These tests hold the latency, not just survival: under a flood of the
costliest sequences a PING is answered in a bounded time. The bound is
generous for a slow CI runner -- the failure it catches took seconds.
"""

import statistics
import time

import pytest

from dsterm import session as dssession
from dsterm.client import Client
from dsterm.grid import COLS, ROWS
from dsterm.session import DstermHistoryScreen, DstermStream, Session

from test_server import ServerHarness

PING_BOUND_MS = 500          # the stall this guards against was 2,600+ ms


def _ping_times(client: Client, count: int = 25) -> list[float]:
    times = []
    for _ in range(count):
        before = client.pongs
        sent = time.monotonic()
        client.send_ping()
        while client.pongs == before and time.monotonic() - sent < 10:
            client.poll(0.002)
        times.append((time.monotonic() - sent) * 1000)
        time.sleep(0.02)
    return times


class TestTheCosts:
    def test_a_scroll_count_is_capped_at_the_screen(self):
        screen = DstermHistoryScreen(COLS, ROWS, history=100, ratio=0.5)
        calls = []
        screen.index = lambda: calls.append(1)            # type: ignore[method-assign]
        screen.reverse_index = lambda: calls.append(-1)   # type: ignore[method-assign]
        screen.scroll_up(99999)
        screen.scroll_down(99999)
        assert len(calls) == 2 * ROWS

    def test_scrolling_past_the_screen_looks_the_same_as_scrolling_it(self):
        def shown(count):
            screen = DstermHistoryScreen(COLS, ROWS, history=100, ratio=0.5)
            stream = DstermStream(screen)
            stream.feed(b"".join(b"line %d\r\n" % i for i in range(ROWS)))
            stream.feed(b"\x1b[%dS" % count)
            return screen.display
        assert shown(ROWS) == shown(99999)

    def test_pump_stops_at_its_budget_and_keeps_the_rest(self):
        # A child that prints 30 KB of ESC # 8 at once: unbounded, a single
        # pump() took seconds. Now each returns after its budget, with the
        # rest waiting, and repeated pumps finish the work.
        s = Session(["sh", "-c", "printf '\\033#8%.0s' $(seq 1 10000); sleep 30"])
        s.start()
        try:
            deadline = time.monotonic() + 5
            while not s.backlog and time.monotonic() < deadline:
                s.pump()
                time.sleep(0.01)
            assert s.backlog, "the burst was never large enough to need a second pass"
            slowest = 0.0
            passes = 0
            while s.backlog and passes < 5000:
                t = time.monotonic()
                s.pump()
                slowest = max(slowest, time.monotonic() - t)
                passes += 1
            assert not s.backlog
            assert slowest < dssession._PUMP_BUDGET + 0.1, f"a pass took {slowest * 1000:.0f} ms"
            assert s.snapshot()[0].to_text().split("\n")[0] == "E" * COLS
        finally:
            s.stop()


FLOODS = {
    "fill-the-screen": "while :; do printf '\\033#8%.0s' $(seq 1 3000); done",
    "huge-scroll-counts": "while :; do printf '\\033[99999S\\033[99999T%.0s' $(seq 1 300); done",
    "plain-output": "exec yes 'a line of output, as fast as the pipe takes it'",
}


@pytest.mark.parametrize("name", sorted(FLOODS))
def test_a_ping_is_answered_while_the_child_floods_the_terminal(name):
    with ServerHarness(command=["sh", "-c", FLOODS[name]]) as h:
        with Client("127.0.0.1", h.port, h.key) as c:
            c.poll(0.5)
            times = _ping_times(c)
            assert max(times) < PING_BOUND_MS, (
                f"{name}: PONG took up to {max(times):.0f} ms "
                f"(median {statistics.median(times):.0f})")
