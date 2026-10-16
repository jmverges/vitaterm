"""`dsterm view`: the server's view socket and the terminal view's own logic.

The socket carries the screens out and keys and taps in, so the tests pin who
can reach it, what a malformed line costs (the view, never the server), that
a card hears whether it is watched, and that a click becomes exactly one tap
at the cell's centre -- which the card then decides about, with its own
VIEW TAPS setting.
"""

import base64
import json
import os
import re
import socket
import stat
import tempfile
import time
import unicodedata
from pathlib import Path

import pytest

from dsterm import viewer as dsviewer
from dsterm.client import Client
from dsterm.protocol import (
    FEATURE_MIRROR,
    MIRROR_FLAG_CAMERA,
    SUBSCREEN_COLS,
    SUBSCREEN_ROWS,
    ProtocolError,
    decode_subscreen,
    encode_subscreen_payload,
    encode_touch,
)

from test_server import ServerHarness, wait_for_text


def _cells(char="x", camera_row=None):
    rows = []
    for r in range(SUBSCREEN_ROWS):
        flags = MIRROR_FLAG_CAMERA if r == camera_row else 0
        rows.append([(ord(char), flags, 0x7FFF, 0x0000) for _ in range(SUBSCREEN_COLS)])
    return rows


class ViewSocket:
    """A test view: the server's line protocol, by hand."""

    def __init__(self, path: Path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(path))
        self.sock.settimeout(0.2)
        self.buf = b""

    def next(self, kind: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while b"\n" in self.buf:
                raw, self.buf = self.buf.split(b"\n", 1)
                msg = json.loads(raw)
                if msg.get("t") == kind:
                    return msg
            try:
                data = self.sock.recv(1 << 20)
            except socket.timeout:
                continue
            if not data:
                raise ConnectionError("view socket closed")
            self.buf += data
        raise TimeoutError(kind)

    def send(self, obj) -> None:
        self.sock.sendall((json.dumps(obj) + "\n").encode())

    def closed_within(self, seconds=3.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if not self.sock.recv(1 << 20):
                    return True
            except socket.timeout:
                continue
            except OSError:
                return True
        return False

    def close(self):
        self.sock.close()


@pytest.fixture
def config_dir():
    # A short path: AF_UNIX sockets have a ~100-byte limit on macOS.
    return Path(tempfile.mkdtemp(dir="/tmp", prefix="dvw"))


def _wait(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.05)
    return False


class TestTheSubscreenFrame:
    def test_round_trip_and_bounds(self):
        payload = encode_subscreen_payload(_cells("A", camera_row=5), 1)
        sub = decode_subscreen(payload)
        assert sub.taps and sub.cells[0][0].char == "A"
        assert sub.cells[5][3].camera and not sub.cells[4][3].camera
        for bad in (payload[:-1], payload + b"x", b"\x40" + payload[1:],
                    payload[:2] + b"\x02" + payload[3:]):
            with pytest.raises(ProtocolError):
                decode_subscreen(bad)

    def test_no_byte_reaches_the_terminal_as_a_control_character(self):
        cells = _cells()
        for i, glyph in enumerate(list(range(0x20)) + [0x7F] + list(range(0x80, 0x100))):
            r, c = divmod(i, SUBSCREEN_COLS)
            cells[r][c] = (glyph, 0, 0x7FFF, 0)
        sub = decode_subscreen(encode_subscreen_payload(cells))
        for row in sub.cells:
            for cell in row:
                assert len(cell.char) == 1, cell.char
                assert unicodedata.category(cell.char)[0] != "C", hex(ord(cell.char))

    def test_a_tap_must_be_on_the_screen(self):
        encode_touch(255, 191)
        for x, y in ((256, 0), (0, 192), (-1, 0)):
            with pytest.raises(ProtocolError):
                encode_touch(x, y)


class TestTheSocket:
    def test_only_this_user_can_open_it(self, config_dir):
        with ServerHarness(config_dir=config_dir) as h:
            path = config_dir / "run" / "view.sock"
            assert _wait(path.exists)
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
            assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700

    def test_a_view_gets_the_top_screen_and_types_into_it(self, config_dir):
        with ServerHarness(config_dir=config_dir) as h:
            v = ViewSocket(config_dir / "run" / "view.sock")
            try:
                top = v.next("top")
                assert len(top["rows"]) == 24
                v.send({"t": "keys", "b64": base64.b64encode(b"echo view-typed-4c1\r").decode()})
                deadline = time.monotonic() + 8
                seen = False
                while time.monotonic() < deadline and not seen:
                    top = v.next("top")
                    seen = any("view-typed-4c1" in "".join(run[0] for run in row)
                               for row in top["rows"])
                assert seen
            finally:
                v.close()

    def test_a_malformed_line_ends_that_view_only(self, config_dir):
        with ServerHarness(config_dir=config_dir) as h:
            v = ViewSocket(config_dir / "run" / "view.sock")
            v.sock.sendall(b"not json\n")
            assert v.closed_within()
            v.close()
            for bad in ({"t": "keys", "b64": "!!!"},
                        {"t": "keys", "b64": base64.b64encode(b"x" * 5000).decode()},
                        {"t": "tap", "x": 300, "y": 0},
                        {"t": "tap", "x": "1", "y": 1}):
                v = ViewSocket(config_dir / "run" / "view.sock")
                v.send(bad)
                assert v.closed_within(), bad
                v.close()
            # And the server is still there for the next one.
            v = ViewSocket(config_dir / "run" / "view.sock")
            v.next("top")
            v.close()

    def test_a_deeply_nested_line_ends_that_view_only(self, config_dir):
        # Valid JSON a few thousand levels deep raises RecursionError, which
        # is not a ValueError: it once escaped and ended the server's loop.
        with ServerHarness(config_dir=config_dir) as h:
            v = ViewSocket(config_dir / "run" / "view.sock")
            v.sock.sendall(b"[" * 4000 + b"]" * 4000 + b"\n")
            assert v.closed_within()
            v.close()
            other = ViewSocket(config_dir / "run" / "view.sock")
            other.next("top")
            other.close()
            assert h.server._running

    def test_the_line_limit_holds_for_a_whole_line_too(self, config_dir):
        # 70 KB with its newline, in one write: over the 64 KB limit even
        # though it ends, and refused before it is parsed.
        from dsterm.view import VIEW_LINE_MAX
        with ServerHarness(config_dir=config_dir) as h:
            v = ViewSocket(config_dir / "run" / "view.sock")
            pad = "x" * (VIEW_LINE_MAX + 6000)
            v.sock.sendall(('{"t": "ping", "pad": "' + pad + '"}\n').encode())
            assert v.closed_within()
            v.close()
            ok = ViewSocket(config_dir / "run" / "view.sock")
            ok.send({"t": "ping", "pad": "x" * 1000})
            ok.next("top")
            assert not ok.closed_within(0.5)
            ok.close()
            assert h.server._running

    def test_a_failure_behind_a_view_costs_that_view(self, config_dir):
        # A bug in what a line reaches (here, the session refusing keys by
        # raising) drops that view, logs why, and leaves the server serving.
        with ServerHarness(config_dir=config_dir) as h:
            def boom(_data):
                raise RuntimeError("synthetic")
            h.server._view._on_keys = boom
            v = ViewSocket(config_dir / "run" / "view.sock")
            v.send({"t": "keys", "b64": base64.b64encode(b"x").decode()})
            assert v.closed_within()
            v.close()
            assert h.wait_for_event("view dropped: RuntimeError: synthetic")
            other = ViewSocket(config_dir / "run" / "view.sock")
            other.next("top")
            other.close()


class TestTheHub:
    def test_a_view_gone_before_its_first_screen_is_counted_once_each_way(self, tmp_path):
        # A viewer that connects and closes before it is sent anything: the
        # first send fails and drops it. That used to log "none watching"
        # twice and never "1 watching".
        from dsterm.view import ViewHub
        counts = []
        listener_dir = Path(tempfile.mkdtemp(dir="/tmp", prefix="dvh"))
        hub = ViewHub(listener_dir, on_keys=lambda d: None, on_tap=lambda x, y: None,
                      on_count=counts.append, emit=lambda m: None,
                      register=lambda *a: None, modify=lambda *a: None,
                      unregister=lambda *a: None)
        assert hub.open()
        try:
            hub.last_top = b'{"t":"top"}\n' * 50000          # more than a buffer holds
            peer = socket.socket(socket.AF_UNIX)
            peer.connect(str(hub.path))
            peer.close()
            time.sleep(0.05)
            hub.accept()
            assert counts == [1, 0]
        finally:
            hub.close()


class TestWithACard:
    def test_the_card_hears_it_is_watched_and_its_screen_reaches_the_view(self, config_dir):
        with ServerHarness(config_dir=config_dir) as h:
            with Client("127.0.0.1", h.port, h.key, features=(FEATURE_MIRROR,)) as card:
                assert _wait(lambda: (card.poll(0.05), card.mirror_watching is False)[1])
                v = ViewSocket(config_dir / "run" / "view.sock")
                try:
                    assert _wait(lambda: (card.poll(0.05), card.mirror_watching is True)[1])
                    card.send_subscreen(encode_subscreen_payload(_cells("Q"), 1))
                    bottom = v.next("bottom")
                    assert bottom["taps"] is True
                    assert bottom["rows"][0][0][0].startswith("Q" * 42)
                    status = v.next("status")
                    assert status["card"] and status["mirror"]

                    # A click is one tap, forwarded as asked.
                    v.send({"t": "tap", "x": 3 + 6 * 5, "y": 4 + 8 * 7})
                    assert _wait(lambda: (card.poll(0.05), bool(card.touches))[1])
                    assert card.touches.popleft() == (33, 60)
                finally:
                    v.close()
                # The last view gone: the card is told to stop sending.
                assert _wait(lambda: (card.poll(0.05), card.mirror_watching is False)[1])

    def test_the_bottom_screen_leaves_with_the_card(self, config_dir):
        with ServerHarness(config_dir=config_dir) as h:
            v = ViewSocket(config_dir / "run" / "view.sock")
            try:
                with Client("127.0.0.1", h.port, h.key, features=(FEATURE_MIRROR,)) as card:
                    assert _wait(lambda: (card.poll(0.05), card.mirror_watching is True)[1])
                    card.send_subscreen(encode_subscreen_payload(_cells("Q")))
                    assert v.next("bottom")["rows"]
                assert v.next("bottom")["rows"] is None
                # And a view that opens now is not handed the stale one.
                late = ViewSocket(config_dir / "run" / "view.sock")
                late.next("top")
                assert h.server._view.last_bottom is None or \
                    json.loads(h.server._view.last_bottom)["rows"] is None
                late.close()
            finally:
                v.close()

    def test_the_top_screen_is_what_the_ds_shows_scrolled_back_too(self, config_dir):
        with ServerHarness(config_dir=config_dir) as h:
            with Client("127.0.0.1", h.port, h.key, features=(FEATURE_MIRROR,)) as card:
                card.send_keys(b"seq 1 200\r")
                assert wait_for_text(card, "200")
                v = ViewSocket(config_dir / "run" / "view.sock")
                try:
                    def top_until(pred):
                        deadline = time.monotonic() + 8
                        while time.monotonic() < deadline:
                            card.poll(0.02)
                            try:
                                top = v.next("top", timeout=0.3)
                            except TimeoutError:
                                continue
                            if pred(top):
                                return top
                        raise AssertionError("no such top screen")

                    def text(top):
                        return "\n".join("".join(r[0] for r in row) for row in top["rows"])

                    live = top_until(lambda t: "200" in text(t))
                    assert live["back"] == 0 and live["cursor"][2]
                    card.send_view(-12)
                    back = top_until(lambda t: t["back"] == 12)
                    assert "200" not in text(back) and not back["cursor"][2]
                    card.send_view(12)
                    top_until(lambda t: t["back"] == 0 and "200" in text(t))
                finally:
                    v.close()

    def test_a_card_that_did_not_announce_mirror_is_never_told(self, config_dir):
        with ServerHarness(config_dir=config_dir) as h:
            with Client("127.0.0.1", h.port, h.key) as card:
                v = ViewSocket(config_dir / "run" / "view.sock")
                try:
                    v.next("top")
                    v.send({"t": "tap", "x": 10, "y": 10})
                    deadline = time.monotonic() + 1.0
                    while time.monotonic() < deadline:
                        card.poll(0.05)
                    assert card.mirror_watching is None and not card.touches
                    # And it may not send a SUBSCREEN.
                    card.send_subscreen(encode_subscreen_payload(_cells()))
                    with pytest.raises((ConnectionError, OSError)):
                        deadline = time.monotonic() + 3
                        while time.monotonic() < deadline:
                            card.poll(0.05)
                finally:
                    v.close()


class TestTheTerminalView:
    def test_a_click_on_the_bottom_screen_is_a_tap_at_the_cells_centre(self):
        view = dsviewer.View(Path("/nonexistent"))
        sent = []

        class FakeConn:
            def send(self, obj):
                sent.append(obj)

        view.conn = FakeConn()
        view.status = {"taps": True}
        view.compose(160, 60)
        bx, by, cols, rows = view.bottom_rect
        view.mouse(0, bx + 5, by + 7, True)
        assert sent == [{"t": "tap", "x": 33, "y": 60}]
        view.mouse(0, bx - 1, by, True)          # outside: nothing
        view.mouse(0, bx, by, False)             # a release: nothing
        assert len(sent) == 1

    def test_taps_off_on_the_ds_says_so_and_sends_nothing(self):
        view = dsviewer.View(Path("/nonexistent"))
        sent = []
        view.conn = type("C", (), {"send": lambda self, o: sent.append(o)})()
        view.status = {"taps": False}
        view.compose(160, 60)
        bx, by, _, _ = view.bottom_rect
        view.mouse(0, bx, by, True)
        assert not sent and "VIEW TAPS" in view.note

    def test_ctrl_bracket_is_the_only_key_it_keeps(self):
        view = dsviewer.View(Path("/nonexistent"))
        sent = []
        view.conn = type("C", (), {"send": lambda self, o: sent.append(o)})()
        assert view.key_bytes(b"hi\x1b[A")
        assert base64.b64decode(sent[-1]["b64"]) == b"hi\x1b[A"
        assert view.key_bytes(b"\x1d\x1d")       # twice: one goes through
        assert base64.b64decode(sent[-1]["b64"]) == b"\x1d"
        assert view.key_bytes(b"\x1d") and not view.key_bytes(b"q")

    def test_it_lays_out_at_any_reasonable_size(self):
        view = dsviewer.View(Path("/nonexistent"))
        view.bottom = {"t": "bottom", "taps": True, "rows": [
            [[("A" if r == 0 else "Z" if r == 23 else "m") * 42, "#ffffff", "#000000", 0]]
            for r in range(24)]}
        strip = re.compile(r"\x1b\[[0-9;]*m")
        for w, h in ((100, 30), (120, 34), (160, 60), (232, 63), (260, 80)):
            lines = [strip.sub("", line) for line in view.compose(w, h)]
            assert len(lines) == h
            assert all(len(line) == w for line in lines), (w, h)
            assert any("TOP SCREEN" in line for line in lines)
            # The bottom screen is drawn exactly where a click is mapped from:
            # its first and last rows, all 42 cells of each.
            bx, by, cols, rows = view.bottom_rect
            assert lines[by - 1][bx - 1:bx - 1 + cols] == "A" * 42, (w, h)
            assert lines[by + rows - 2][bx - 1:bx - 1 + cols] == "Z" * 42, (w, h)
        lines = [strip.sub("", line) for line in view.compose(80, 24)]
        assert any("bigger" in line for line in lines)
