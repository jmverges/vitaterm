"""Tests for the PTY session.

The load-bearing claim is that the child reflows to 42x24 and stays there. If
that is wrong, everything downstream renders cropped garbage, so it is asserted
against the child's own view of its terminal rather than against our config.
"""

import time

import pytest

from dsterm.grid import COLS, ROWS
from dsterm.session import DstermHistoryScreen, DstermStream, Session


def _run(argv, seconds=4.0, settle=0.15):
    """Run a command to completion (or timeout) and return the final grid."""
    with Session(argv) as sess:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            sess.pump()
            if not sess.alive():
                time.sleep(settle)   # let the last output drain
                sess.pump()
                break
            time.sleep(0.02)
        grid, _, _ = sess.snapshot()
        return grid


class TestSizing:
    def test_child_sees_42_columns(self):
        # tput reports what the PTY told the child, not what we intended.
        grid = _run(["bash", "-lc", "tput cols"])
        assert grid.to_text().split("\n")[0].strip() == str(COLS)

    def test_child_sees_24_rows(self):
        grid = _run(["bash", "-lc", "tput lines"])
        assert grid.to_text().split("\n")[0].strip() == str(ROWS)

    def test_columns_env_matches(self):
        grid = _run(["bash", "-lc", "echo $COLUMNS"])
        assert grid.to_text().split("\n")[0].strip() == str(COLS)


class TestOutput:
    def test_simple_output_is_captured(self):
        grid = _run(["bash", "-lc", "echo marker-9f3a"])
        assert "marker-9f3a" in grid.to_text()

    def test_long_line_wraps_within_the_grid(self):
        grid = _run(["bash", "-lc", "printf 'A%.0s' $(seq 1 100); echo"])
        text = grid.to_text()
        # 100 chars wraps across whatever COLS allows -- three rows at 42, two
        # at 64 -- and must not exceed the grid at either width.
        assert all(len(line) <= COLS for line in text.split("\n"))
        assert text.replace("\n", "").count("A") == 100

    def test_grid_is_full_size_after_heavy_output(self):
        grid = _run(["bash", "-lc", "seq 1 500"])
        assert len(grid.cells) == COLS * ROWS


class TestDiffing:
    def test_first_snapshot_reports_changes_then_settles(self):
        with Session(["bash", "-lc", "echo hello; sleep 2"]) as sess:
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                sess.pump()
                time.sleep(0.05)
            _, first, _ = sess.snapshot()
            _, second, stats = sess.snapshot()
            assert first, "expected the initial paint to register changes"
            assert second == [], "idle screen must produce an empty diff"
            assert stats.cells_changed == 0

    def test_stats_count_bytes(self):
        with Session(["bash", "-lc", "echo abc; sleep 1"]) as sess:
            time.sleep(0.6)
            sess.pump()
            _, _, stats = sess.snapshot()
            assert stats.bytes_in > 0
            assert stats.tick == 1


class TestInput:
    def test_written_input_reaches_the_child(self):
        with Session(["bash", "--noprofile", "--norc", "-i"]) as sess:
            time.sleep(0.5)
            sess.pump()
            sess.write(b"echo round-trip-ok\r")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                sess.pump()
                if "round-trip-ok" in sess.snapshot()[0].to_text():
                    break
                time.sleep(0.05)
            assert "round-trip-ok" in sess.grid.to_text()

    def test_a_write_the_pty_will_not_take_at_once_is_finished_later(self, monkeypatch):
        # The PTY is non-blocking: a busy child's full buffer takes part of a
        # write, or none of it. What it did not take is kept, in order.
        import dsterm.session as session_mod

        taken = bytearray()
        room = [3]

        def stingy_write(fd, data):
            if room[0] == 0:
                raise BlockingIOError
            n = min(room[0], len(data))
            room[0] -= n
            taken.extend(bytes(data[:n]))
            return n

        with Session(["cat"]) as sess:
            monkeypatch.setattr(session_mod.os, "write", stingy_write)
            assert sess.write(b"hello world")
            assert bytes(taken) == b"hel" and sess.input_pending == 8
            assert sess.write(b"!")                   # queued behind the rest
            assert not sess.flush_input()
            room[0] = 100
            assert sess.flush_input()
            assert bytes(taken) == b"hello world!" and sess.input_pending == 0
            monkeypatch.undo()

    def test_a_child_that_stops_reading_is_not_fed_without_end(self, monkeypatch):
        import dsterm.session as session_mod

        def full(fd, data):
            raise BlockingIOError

        with Session(["cat"]) as sess:
            monkeypatch.setattr(session_mod.os, "write", full)
            chunk = b"x" * 4096
            accepted = 0
            while sess.write(chunk):
                accepted += len(chunk)
            assert accepted <= session_mod.INPUT_PENDING_LIMIT
            assert sess.input_pending == accepted
            monkeypatch.undo()


class TestTheChildsTerm:
    """The child draws on pyte, an xterm -- not on whatever terminal started
    the server. A TERM=dumb inherited from a CI runner once failed ten tests."""

    def _env_of(self, monkeypatch, **kwargs) -> dict:
        import dsterm.session as session_mod
        seen = {}
        real = session_mod.subprocess.Popen

        def spy(*args, **kw):
            seen.update(kw["env"])
            return real(*args, **kw)

        monkeypatch.setattr(session_mod.subprocess, "Popen", spy)
        with Session(["true"], **kwargs):
            pass
        return seen

    def test_an_inherited_term_is_replaced(self, monkeypatch):
        monkeypatch.setenv("TERM", "dumb")
        assert self._env_of(monkeypatch)["TERM"] == "xterm-256color"

    def test_an_env_that_sets_term_is_taken_at_its_word(self, monkeypatch):
        env = {"PATH": "/usr/bin:/bin", "TERM": "screen-256color"}
        assert self._env_of(monkeypatch, env=env)["TERM"] == "screen-256color"

    def test_an_env_without_term_gets_the_emulators(self, monkeypatch):
        env = {"PATH": "/usr/bin:/bin"}
        assert self._env_of(monkeypatch, env=env)["TERM"] == "xterm-256color"


class TestCursor:
    """Session.cursor reflects the emulator's caret.

    The escape sequences come from a real child writing to the PTY -- the same
    path the server reads -- because the line discipline mangles ESC on echo, so
    writing them in would not model what actually reaches the emulator. CUP is
    absolute addressing, so the expected position is deterministic.
    """

    def _await_cursor(self, sess, want, seconds=3.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            sess.pump()
            if sess.cursor == want:
                return True
            time.sleep(0.02)
        return False

    def test_cursor_follows_cup(self):
        with Session(["bash", "-lc", r"printf '\033[5;10H'; sleep 5"]) as sess:
            # CUP is 1-indexed; pyte stores 0-indexed.
            assert self._await_cursor(sess, (4, 9, True))

    def test_cursor_can_be_hidden(self):
        with Session(["bash", "-lc", r"printf '\033[?25l'; sleep 5"]) as sess:
            assert self._await_cursor(sess, (0, 0, False))

    def test_cursor_moves_with_relative_addressing(self):
        with Session(["bash", "-lc", r"printf '\033[3;3HAB'; sleep 5"]) as sess:
            assert self._await_cursor(sess, (2, 4, True))


class TestLifecycle:
    def test_double_start_is_rejected(self):
        sess = Session(["bash", "-lc", "true"])
        sess.start()
        try:
            with pytest.raises(RuntimeError):
                sess.start()
        finally:
            sess.stop()

    def test_stop_is_idempotent(self):
        sess = Session(["bash", "-lc", "sleep 10"])
        sess.start()
        sess.stop()
        sess.stop()          # must not raise
        assert not sess.alive()

    def test_write_after_stop_raises(self):
        sess = Session(["bash", "-lc", "sleep 10"])
        sess.start()
        sess.stop()
        with pytest.raises(RuntimeError):
            sess.write(b"x")


class TestEmulatorRobustness:
    """An escape sequence the emulator does not implement must not kill it.

    pump() is the server's read loop. An exception raised out of stream.feed()
    kills that thread, the screen freezes, and the handheld shows a still image
    of a session that is in fact still running -- which is indistinguishable
    from a dead Wi-Fi link and was diagnosed as one.
    """

    def test_private_device_status_does_not_raise(self):
        # tmux 3.7 sends CSI ? 6 n during startup. pyte 0.8.2 declares
        # report_device_status() without `private`, so this raised TypeError
        # out of feed() and took the session down on the child's first write.
        screen = DstermHistoryScreen(COLS, ROWS, history=2000, ratio=0.5)
        stream = DstermStream(screen)
        stream.feed(b"\x1b[?6n")
        stream.feed(b"after the report\r\n")
        assert "after the report" in "".join(
            screen.buffer[0][x].data for x in range(COLS)
        )

    def test_a_refused_sequence_is_counted_not_raised(self):
        with Session(["bash", "--noprofile", "--norc", "-c",
                      "printf 'before\\n'; printf '\\033[?6n'; printf 'after\\n'; sleep 0.3"]) as s:
            deadline = time.monotonic() + 5
            text = ""
            while time.monotonic() < deadline:
                s.pump()
                # snapshot(), not grid: pump() feeds the emulator, and the grid
                # is only rebuilt on a tick.
                text = s.snapshot()[0].to_text()
                if "after" in text:
                    break
                time.sleep(0.05)
            # Both sides of the offending sequence arrived, and nothing raised.
            assert "before" in text and "after" in text
            assert s.emulator_errors == 0, s.last_emulator_error
