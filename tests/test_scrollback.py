"""Scrollback: reaching the history the emulator already keeps.

The screen is only ROWS tall -- 24 by default, 32 on the shape the handheld
now renders -- and Claude Code's chrome takes most of it, so a long answer
scrolls past and is gone. pyte has kept 2000 lines of it all along; there
was simply no way to ask for them.

The history stays on the server and the handheld sends an intent. That is not
only a memory argument: a client that stored history would have to understand
what a line is, and the whole design rests on the DS knowing nothing but cells
and key bytes.
"""

import time

import pytest

from dsterm.client import Client
from dsterm.grid import COLS, ROWS
from dsterm.protocol import (
    VIEW_MAX_DELTA,
    MsgType,
    ProtocolError,
    decode_view,
    encode,
    encode_view,
)
from dsterm.session import Session

from test_server import ServerHarness, wait_for_text


class TestViewMessage:
    def test_round_trips_both_directions(self):
        for delta in (-1000, -24, -1, 0, 1, 24, 1000):
            assert decode_view(encode_view(delta)[6:]) == delta

    def test_an_oversized_delta_is_refused_when_encoding(self):
        with pytest.raises(ProtocolError):
            encode_view(VIEW_MAX_DELTA + 1)

    def test_an_oversized_delta_is_clamped_when_decoding(self):
        # A client asking to go further than the history reaches means "as far
        # as you can". There is nothing useful to refuse, but a server that
        # walked an unbounded number of lines on request would be a lever.
        import struct
        assert decode_view(struct.pack(">h", 32767)) == VIEW_MAX_DELTA
        assert decode_view(struct.pack(">h", -32768)) == -VIEW_MAX_DELTA

    def test_a_wrong_length_payload_is_refused(self):
        with pytest.raises(ProtocolError):
            decode_view(b"\x00")
        with pytest.raises(ProtocolError):
            decode_view(b"\x00\x00\x00")

    def test_view_is_not_allowed_before_authentication(self):
        import socket as _socket

        with ServerHarness() as h:
            sock = _socket.create_connection(("127.0.0.1", h.port), timeout=5)
            sock.recv(4096)
            sock.sendall(encode_view(-10))
            assert h.wait_for_event("before authentication")
            sock.close()


def fill_history(c: Client, h, history: int | None = None) -> None:
    """Print until at least `history` lines have scrolled off the top.

    Asked for in lines of SCROLLBACK, not lines printed, because the screen
    height is configuration now (DSTERM_ROWS). A fixed print count silently
    means different things at different geometries: 60 printed lines leave 36
    in history at 24 rows but only 28 at 32, which is exactly how the same call
    satisfied `scrollback_lines > ROWS` on the default and failed on the shape
    the handheld actually renders.

    The default clears ROWS with a margin, so the "more history than fits on
    the screen" assertions hold at any geometry.
    """
    if history is None:
        history = ROWS + 8
    lines = ROWS + history
    c.send_keys(f"for i in $(seq 1 {lines}); do echo hist-$i; done\r".encode())
    assert wait_for_text(c, f"hist-{lines}", timeout=15)
    time.sleep(0.4)
    c.poll(0.3)


class TestScrollingBack:
    def test_tmux_scroll_up_escape_sequence_enters_history(self):
        session = Session(["true"])
        session._stream.feed(b"\r\n" * ROWS)
        before = session.scrollback_lines

        session._stream.feed(b"\x1b[5S")

        assert session.scrollback_lines == before + 5
    def test_history_accumulates(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                assert h.server._session.scrollback_lines == 0
                fill_history(c, h)
                assert h.server._session.scrollback_lines > ROWS

    def test_scrolling_back_shows_older_output(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                fill_history(c, h)

                # The oldest lines are off the live screen by now.
                assert "hist-1 " not in c.grid.to_text()

                c._send(encode_view(-40))
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    c.poll(0.2)
                    if "hist-1" in c.grid.to_text():
                        break
                assert "hist-1" in c.grid.to_text()

    def test_the_live_screen_is_untouched_by_scrolling(self):
        # The emulator must not be paged around underneath the session. pyte's
        # own prev_page() rewrites screen.buffer, which is exactly why this
        # builds the view read-only instead.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                fill_history(c, h)
                live = h.server._session.grid.to_text()

                c._send(encode_view(-40))
                time.sleep(0.8)
                c.poll(0.3)

                assert h.server._session.grid.to_text() == live

    def test_scrolling_forward_returns_to_live(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                fill_history(c, h)
                live = h.server._session.grid.to_text()

                # Less than fill_history() leaves behind, so it is not
                # clamped: it prints ROWS + history lines, and the default
                # history clears ROWS with margin at any geometry.
                c._send(encode_view(-20))
                assert h.wait_for_event("view 20")

                c._send(encode_view(1000))     # clamped to the bottom
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    c.poll(0.2)
                    if c.grid.to_text() == live:
                        break
                assert c.grid.to_text() == live

    def test_scrolling_back_further_than_history_is_clamped(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                fill_history(c, h)
                available = h.server._session.scrollback_lines

                c._send(encode_view(-VIEW_MAX_DELTA))
                time.sleep(0.8)
                c.poll(0.3)

                client = h.server._authed()
                assert client.scroll.offset == available

    def test_typing_returns_to_the_live_screen(self):
        # Otherwise the keystroke lands somewhere the user cannot see.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                fill_history(c, h)

                # Less than fill_history() leaves behind, so it is not
                # clamped: it prints ROWS + history lines, and the default
                # history clears ROWS with margin at any geometry.
                c._send(encode_view(-20))
                assert h.wait_for_event("view 20")

                c.send_keys(b"echo back-to-live-8c1\r")
                assert wait_for_text(c, "back-to-live-8c1", timeout=15)
                assert h.server._authed().scroll.offset == 0

    def test_the_caret_is_hidden_while_scrolled_back(self):
        # The caret belongs to the live screen. Drawn over history it marks a
        # cell nobody is typing into.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                fill_history(c, h)

                c._send(encode_view(-40))
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    c.poll(0.2)
                    if c.cursor is not None and not c.cursor[2]:
                        break
                assert c.cursor is not None and c.cursor[2] is False

    @staticmethod
    def _settled_text(c, quiet_for: float = 0.4, timeout: float = 5.0) -> str:
        """The screen once it has stopped changing, rather than after a guess.

        ``poll(0.5)`` is not "read for half a second". ``Client.poll`` breaks on
        ``if handled or time.monotonic() >= deadline``, so it returns the moment
        the first batch arrives and the 0.5 is only a ceiling on waiting for
        that first message. A snapshot taken straight afterwards is the grid
        roughly forty milliseconds in.

        So the assumption was never "the shell has finished starting" -- it was
        "the first message is the last message", which the 0.5 made look like a
        duration. When a macOS shell prints its "the default interactive shell
        is now zsh" banner, the PTY chunking decides whether that banner is
        whole at that instant: the observed failure had ``before`` holding a
        truncated "To upd" and ``after`` the finished sentence, so the banner
        arrived *across* the snapshot rather than after it. An assertion about a
        view request then fails on output that had nothing to do with it.

        Measured: 4 of 20 with the fixed poll on the host that has a banner,
        20 of 20 with this, and still 20 of 20 with the quiet window cut to
        50 ms -- once polling is continuous the whole banner lands inside one
        gap. 20 of 20 either way on a host with no banner to split, which is
        why this could not be reproduced where it was fixed.

        The assertion itself is unchanged and no less strict. This only stops
        the test deciding for itself that the stream is done.
        """
        text = c.grid.to_text()
        stable_since = time.monotonic()
        deadline = stable_since + timeout
        while time.monotonic() < deadline:
            c.poll(0.1)
            current = c.grid.to_text()
            if current != text:
                text = current
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= quiet_for:
                return text
        return text

    def test_a_view_with_no_history_does_nothing(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                before = self._settled_text(c)
                c._send(encode_view(-40))
                time.sleep(0.6)
                c.poll(0.3)
                assert h.server._authed().scroll.offset == 0
                assert c.grid.to_text() == before

    def test_a_view_with_no_history_reports_its_position(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c._send(encode_view(-40))
                assert h.wait_for_event("view 0 of 0")
                assert h.server._authed().view_pos == (0, 0)


class TestViewPosition:
    """The handheld is told where it is, because it cannot work it out.

    Scrolling with no feedback reads as broken even when it works: there is no
    way to tell a press that did nothing from one that reached the top.
    """

    def test_position_is_sent_when_the_view_moves(self):
        from dsterm.protocol import decode_view_pos

        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                fill_history(c, h)

                c._send(encode_view(-20))
                assert h.wait_for_event("view 20")

                client = h.server._authed()
                assert client.view_pos is not None
                offset, available = client.view_pos
                assert offset == 20
                assert available >= 20

    def test_position_is_not_resent_when_nothing_changed(self):
        # Once per tick would be 15 pointless messages a second on a link whose
        # whole design is about not wasting them.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                fill_history(c, h)
                c._send(encode_view(-20))          # opts in
                assert h.wait_for_event("view 20")
                time.sleep(0.5)
                client = h.server._authed()
                first = client.view_pos
                time.sleep(0.5)
                assert client.view_pos == first

    def test_available_grows_as_output_scrolls(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                fill_history(c, h, history=8)
                c._send(encode_view(-5))           # opts in
                assert h.wait_for_event("view 5")
                first = h.server._authed().view_pos[1]
                fill_history(c, h, history=8)
                assert h.server._authed().view_pos[1] > first


class TestOlderClientsAreNotBroken:
    """A card that predates VIEW_POS must keep working.

    The framing layer rejects unknown message types outright -- deliberately,
    because a desynchronised length prefix is unrecoverable. That makes any new
    server->client message a breaking change for a card already in a console,
    and the card can only be reflashed from another machine. So VIEW_POS is sent
    only to a client that has proved it knows about it by sending a VIEW.
    """

    def test_a_client_that_never_scrolls_is_never_sent_view_pos(self):
        sent = []

        with ServerHarness() as h:
            real = h.server._send_to

            def spy(c, data):
                if data[3] == MsgType.VIEW_POS:
                    sent.append(data)
                real(c, data)

            h.server._send_to = spy
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c.send_keys(b"echo no-view-pos-please\r")
                assert wait_for_text(c, "no-view-pos-please")
                time.sleep(0.6)
                c.poll(0.3)

        assert sent == [], "an older card would have halted on these"

    def test_scrolling_once_opts_the_client_in(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                fill_history(c, h)
                assert h.server._authed().caps.view_pos is False

                c._send(encode_view(-20))
                assert h.wait_for_event("view 20")
                assert h.server._authed().caps.view_pos is True
                assert h.server._authed().view_pos is not None


class TestViewPosIsRateLimitedAtTheBottom:
    def test_live_history_growth_is_reported_without_per_tick_spam(self):
        # A live client needs feedback that history is growing, but one message
        # per emulator tick would crowd out terminal updates on the DS link.
        sent = []

        with ServerHarness() as h:
            real = h.server._send_to

            def spy(c, data):
                if data[3] == MsgType.VIEW_POS:
                    sent.append(data)
                real(c, data)

            h.server._send_to = spy
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                fill_history(c, h)
                c._send(encode_view(-20))       # opts in, and scrolls up
                assert h.wait_for_event("view 20")

                c.send_keys(b"echo back-to-bottom-2d4\r")   # returns to live
                assert wait_for_text(c, "back-to-bottom-2d4", timeout=15)

                # Settle first. The transition back to live is itself one
                # message and it is queued, so sampling immediately would count
                # it on the wrong side of the comparison.
                time.sleep(1.0)
                c.poll(0.3)
                at_bottom = len(sent)

                fill_history(c, h, history=ROWS + 40)  # a lot more history
                time.sleep(0.5)
                c.poll(0.3)

        assert len(sent) > at_bottom, "live history growth was never reported"
        assert len(sent) <= at_bottom + 3, "live position reports were not rate-limited"
