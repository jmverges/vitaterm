"""A PTY running a command, emulated at a fixed 42x24.

The size is set once, at spawn, and never changes. This is the whole point: the
child believes it is on a 42x24 terminal and reflows to that, so there is no
SIGWINCH and no cropping. tmux cannot do this for two clients at once, which is
why the DS gets its own PTY rather than sharing the desktop's session.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
import time
from dataclasses import dataclass, field
from pathlib import Path

import pyte

from .grid import COLS, ROWS, Grid

# Read chunk size. Claude Code streams in small bursts; larger reads just block
# less often and cost nothing.
_READ_SIZE = 65536

# Emulating output is the one piece of work on the server's loop whose cost a
# program chooses: per byte it varies about a hundredfold -- a line of text is
# ~3 ms a KB, ESC # 8 (fill the screen) ~240 ms a KB. So pump() feeds the
# emulator in slices and stops once its budget is spent, keeping the rest for
# the loop's next pass: a card's keys and PINGs are never more than one
# budget and one slice behind, whatever the program prints. The slice bounds
# the overshoot (64 bytes of ESC # 8 is ~15 ms); while a backlog waits nothing
# more is read, so the PTY fills and the child is slowed to match.
_FEED_SLICE = 64
_PUMP_BUDGET = 0.008

# Lines of scrollback the emulator keeps. Matched to the tmux server's
# history-limit (dsterm.tmux.conf) rather than left at pyte's default: the DS
# can only reach what this holds, so a smaller number here silently threw away
# history tmux had already kept, and the handheld hit a wall two and a half
# thousand lines before the session did.
HISTORY_LINES = 5000

# DECSET 1049, as pyte stores it: private modes are shifted left by five.
_ALT_SCREEN_MODE = 1049 << 5

# Children that own their own scrollback and repaint their pane. What reaches us
# from one of these is not the inner application's byte stream, so nothing about
# the inner application can be read off it.
_MULTIPLEXERS = frozenset({"tmux", "screen"})

# How often the pane is asked about the inner application. The regime changes on
# human timescales -- someone starts or quits Claude Code -- so a second is not a
# compromise, and it keeps a subprocess out of the 30 Hz render path.
_MODE_POLL_SECONDS = 1.0


def _parse_tmux_target(argv: list[str]) -> tuple[str, str] | None:
    """Pull (socket, session name) out of a tmux command line.

    Read back off the argv we were handed rather than passed in beside it,
    because the argv is the thing that is actually true: serve.py builds it,
    tests build it, and a future caller that builds one by hand gets the same
    behaviour without having to know there is a second parameter to keep in step.
    Returns None when either half is absent, and the caller then declines to
    guess rather than querying the wrong pane.
    """
    socket_name = session = None
    for i, arg in enumerate(argv[:-1]):
        if arg == "-L" and socket_name is None:
            socket_name = argv[i + 1]
        elif arg == "-s" and session is None:
            session = argv[i + 1]
    if socket_name is None or session is None:
        return None
    return socket_name, session


class DstermHistoryScreen(pyte.HistoryScreen):
    """HistoryScreen with the xterm SU/SD operations tmux emits.

    pyte's stock CSI table leaves ``CSI Pn S`` and ``CSI Pn T`` unhandled.
    Those commands are terminal scrolling, not decorative cursor movement: if
    they are dropped, neither the visible grid nor scrollback records the
    lines that moved off screen.
    """

    # Capped at the screen's height, as a terminal does: scrolling further
    # leaves the same blank screen, and an uncapped count let ten bytes of
    # ``CSI 99999 S`` cost a hundred thousand passes of index().
    def scroll_up(self, count: int = 1) -> None:
        for _ in range(min(max(1, count), self.lines)):
            self.index()

    def scroll_down(self, count: int = 1) -> None:
        for _ in range(min(max(1, count), self.lines)):
            self.reverse_index()

    def report_device_status(self, mode: int = 0, private: bool = False) -> None:
        """Accept DSR in both its forms, including the private one.

        pyte 0.8.2 declares this without `private`, so ``CSI ? 6 n`` -- which
        tmux 3.7 sends during startup -- raises TypeError out of stream.feed()
        and takes the session down with it. Observed: the child had barely
        started and pump() died on the first read.

        Nothing is reported back either way. The reply would have to be written
        into the child's PTY, and pyte only does that through a
        write_process_input callback this session deliberately does not set:
        the DS is the only thing allowed to put bytes into that PTY, and a
        terminal that answers on its own behalf is a second writer.
        """
        del mode, private


class DstermStream(pyte.ByteStream):
    """ByteStream extended only with xterm's scroll-up/down CSI commands."""

    csi = dict(pyte.ByteStream.csi, S="scroll_up", T="scroll_down")


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    """Tell the PTY its dimensions. Called once, before exec."""
    packed = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)


@dataclass
class FrameStats:
    """Per-tick measurements.

    `cells_changed` is the number that decides whether the cell-diff plan works:
    if the TUI repaints wholesale, this sits near COLS*ROWS every frame and the
    bandwidth win evaporates.
    """

    tick: int = 0
    bytes_in: int = 0
    cells_changed: int = 0
    elapsed_ms: float = 0.0


# What the child is told it is drawing on: the emulator below is an xterm.
CHILD_TERM = "xterm-256color"

# Input waiting for a child that is not reading. The PTY is non-blocking, so a
# write the kernel will not take all of at once -- a long paste into a busy
# child -- is kept here and finished by flush_input() rather than cut short.
# A DS types a few bytes a second; this much unread means the child has
# stopped reading, and more is refused rather than held without end.
INPUT_PENDING_LIMIT = 256 * 1024


@dataclass
class Session:
    """Owns the child process, its PTY, and the emulated screen."""

    argv: list[str]
    cwd: str | None = None
    env: dict[str, str] | None = None

    _master: int = field(default=-1, init=False)
    _input: bytearray = field(default_factory=bytearray, init=False)
    _proc: subprocess.Popen | None = field(default=None, init=False)
    _screen: pyte.Screen = field(init=False)
    _stream: pyte.ByteStream = field(init=False)
    _grid: Grid = field(default_factory=Grid.blank, init=False)
    _tick: int = field(default=0, init=False)
    _pending_bytes: int = field(default=0, init=False)
    # Sequences the emulator refused, and the last one. Counted rather than
    # raised (see pump): a running total that stays at zero is evidence, and a
    # number that climbs points straight at the emulator instead of the wire.
    emulator_errors: int = field(default=0, init=False)
    last_emulator_error: str = field(default="", init=False)
    # Output read from the PTY and not yet emulated (see _PUMP_BUDGET), and
    # how far into it the emulator has got.
    _backlog: bytes = field(default=b"", init=False)
    _backlog_at: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        # history=0 would drop scrollback; we keep pyte's default so the DS can
        # later scroll back, since only ~10 rows are usable after Claude's chrome.
        self._screen = DstermHistoryScreen(COLS, ROWS, history=HISTORY_LINES, ratio=0.5)
        self._stream = DstermStream(self._screen)
        # A multiplexer repaints rather than scrolls, so it can never feed our
        # history. Taken from the command because that is a fact we already
        # hold, not a guess about the output: no sniffing, no heuristic, and it
        # is true from the first byte rather than after enough of them.
        self._multiplexed = bool(self.argv) and Path(self.argv[0]).name in _MULTIPLEXERS
        self._tmux_target = _parse_tmux_target(self.argv) if self._multiplexed else None
        # True until tmux says otherwise: see _pane_alt_screen for why the
        # opening guess goes this way.
        self._mode_cached = self._multiplexed
        self._mode_checked_at = 0.0
        # The pane's own scrollback, read alongside the mode in the same 1 Hz
        # query. This is the authoritative history under tmux: pyte's history
        # only fills when tmux happens to scroll rather than repaint.
        self._pane_history = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("session already started")

        master, slave = pty.openpty()
        _set_winsize(slave, COLS, ROWS)

        # The terminal this child draws on is pyte's, an xterm, whatever
        # terminal happened to start the server -- an inherited TERM=dumb
        # (a CI runner, an editor's task panel) would have Claude Code and
        # tmux draw for a screen that is not there.  So an inherited TERM is
        # replaced; only an env the caller built (serve.py's, where the env
        # file may set TERM on purpose) is taken at its word.
        if self.env is None:
            env = dict(os.environ)
            env["TERM"] = CHILD_TERM
        else:
            env = dict(self.env)
            env.setdefault("TERM", CHILD_TERM)
        # Claude Code reflows to COLUMNS/LINES as well as to the ioctl size.
        env["COLUMNS"] = str(COLS)
        env["LINES"] = str(ROWS)

        self._proc = subprocess.Popen(
            self.argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=self.cwd,
            env=env,
            preexec_fn=os.setsid,
            close_fds=True,
        )
        os.close(slave)

        flags = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self._master = master

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        if self._master >= 0:
            try:
                os.close(self._master)
            except OSError:
                pass
            self._master = -1

    def __enter__(self) -> "Session":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- io ----------------------------------------------------------------

    @property
    def fd(self) -> int:
        return self._master

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        """The child's pid -- which, under tmux, is the attached client, so
        tmux's own list-clients can name it "ours" by pid."""
        return self._proc.pid if self._proc is not None else None

    def pump(self) -> int:
        """Read what the child printed and feed it to the emulator, for at
        most _PUMP_BUDGET seconds.

        Returns bytes emulated. Never blocks. What a budget did not reach
        stays in a backlog, fed first next time, and while there is one
        nothing more is read: `backlog` tells the server's loop to call again
        without waiting. Reading is decoupled from snapshotting on purpose:
        the emulator stays current while frames are only produced on the tick.
        """
        if self._master < 0:
            return 0
        started = time.monotonic()
        total = self._feed_backlog(started)
        while not self.backlog and time.monotonic() - started < _PUMP_BUDGET:
            try:
                chunk = os.read(self._master, _READ_SIZE)
            except BlockingIOError:
                break
            except OSError as exc:
                # EIO is the normal signal that the child closed the PTY.
                if exc.errno in (errno.EIO, errno.EBADF):
                    break
                raise
            if not chunk:
                break
            self._backlog, self._backlog_at = chunk, 0
            total += self._feed_backlog(started)
            if len(chunk) < _READ_SIZE:
                break
        self._pending_bytes += total
        return total

    @property
    def backlog(self) -> bool:
        """Output is waiting to be emulated: call pump() again soon."""
        return self._backlog_at < len(self._backlog)

    def _feed_backlog(self, started: float) -> int:
        fed = 0
        data = self._backlog
        while self._backlog_at < len(data):
            piece = data[self._backlog_at:self._backlog_at + _FEED_SLICE]
            self._backlog_at += len(piece)
            fed += len(piece)
            # An escape sequence this emulator does not implement must never
            # take the terminal down. pyte raises out of feed() for unhandled
            # private parameters and a few malformed sequences, and this is
            # the server's read loop: an exception here kills the thread, the
            # screen freezes, and the handheld shows a still image of a session
            # that is actually still running. pyte resets its parser on an
            # error, so the cost is the rest of this slice -- a garbled frame
            # at most, which the next resync repairs.
            try:
                self._stream.feed(piece)
            except Exception as exc:  # noqa: BLE001 -- deliberately broad
                self.emulator_errors += 1
                self.last_emulator_error = f"{type(exc).__name__}: {exc}"
            if time.monotonic() - started >= _PUMP_BUDGET:
                break
        if self._backlog_at >= len(data):
            self._backlog, self._backlog_at = b"", 0
        return fed

    def write(self, data: bytes) -> bool:
        """Send input to the child. This is the only channel the DS ever gets.

        Whatever the PTY will not take now is kept, in order, and sent by
        flush_input(): a partial write is never the end of it. Returns False,
        keeping nothing of `data`, only when INPUT_PENDING_LIMIT is already
        waiting -- a child that has stopped reading.
        """
        if self._master < 0:
            raise RuntimeError("session is not running")
        if len(self._input) + len(data) > INPUT_PENDING_LIMIT:
            return False
        self._input += data
        self.flush_input()
        return True

    def flush_input(self) -> bool:
        """Write as much waiting input as the PTY takes now. True when none is
        left. The server calls this every pass of its loop."""
        while self._input and self._master >= 0:
            try:
                n = os.write(self._master, self._input)
            except (BlockingIOError, InterruptedError):
                return False
            except OSError:
                # The child is gone (EIO); the server restarts it, and input
                # meant for this one must not be typed into the next.
                self._input.clear()
                return True
            del self._input[:n]
        return not self._input

    @property
    def input_pending(self) -> int:
        """Bytes written to the session that the child has not taken yet."""
        return len(self._input)

    # -- frames ------------------------------------------------------------

    def snapshot(self) -> tuple[Grid, list, FrameStats]:
        """Produce one frame: the current grid, its diff, and measurements."""
        began = time.monotonic()
        new = Grid.from_pyte(self._screen)
        changes = new.diff(self._grid)
        self._grid = new
        self._tick += 1
        stats = FrameStats(
            tick=self._tick,
            bytes_in=self._pending_bytes,
            cells_changed=len(changes),
            elapsed_ms=(time.monotonic() - began) * 1000.0,
        )
        self._pending_bytes = 0
        return new, changes, stats

    @property
    def grid(self) -> Grid:
        return self._grid

    @property
    def multiplexed(self) -> bool:
        """True when the child is tmux or screen, and history is theirs."""
        return self._multiplexed

    @property
    def scrollback_lines(self) -> int:
        """How many lines exist above the live screen.

        Through tmux this is the pane's own history_size, not pyte's: tmux
        repaints by addressing cells, so the emulator history stays empty on
        exactly the systems that have the most to scroll back through.
        """
        if self._multiplexed:
            # The cached pane query carries history_size in the same answer,
            # so this is never an extra subprocess.
            self._pane_alt_screen()
            return min(self._pane_history, HISTORY_LINES)
        return len(self._screen.history.top)

    @property
    def alt_screen(self) -> bool:
        """True while the child is drawing on the alternate screen.

        Which is the whole reason scrollback looks broken on the handheld: the
        alternate screen has no history by definition, so nothing this class
        holds -- and nothing tmux holds either -- can be scrolled back through.
        Measured, not assumed: an 80-line answer from Claude Code left
        history_size at 0 on both sides. See docs/design/SCROLLBACK_MEASUREMENT.md.

        pyte records the mode without implementing the buffer swap, which is
        enough for us; we only need to know. Private modes are stored shifted
        left by five, so DECSET 1049 appears as 33568.
        """
        return _ALT_SCREEN_MODE in self._screen.mode

    @property
    def app_scrolls(self) -> bool:
        """True when the application owns scrolling, so the handheld must send
        page keys instead of asking us for a view.

        The signal is the INNER application's alternate-screen state, because
        that is the thing that actually predicts an empty history. Getting it
        requires asking tmux, and two cheaper derivations were tried and
        measured wrong first (docs/design/SCROLLBACK_DRAIN_RATE.md, and the correction
        in docs/design/SCROLLBACK_MEASUREMENT.md):

          - `multiplexed` alone is a constant. Every session we ship is wrapped,
            so it cannot distinguish the case it is asked to distinguish, and it
            reports True over a shell with perfectly good history behind it.
          - The pyte alt bit reports the OUTER terminal, which under tmux is
            tmux's own alternate screen and never the inner application's. It
            reads True under tmux 3.7b and False under 3.4 for identical
            children, so it is unusable in both directions rather than merely
            incomplete.

        Whether history accumulates through tmux at all turned out to depend on
        the tmux version and on how fast the PTY is drained -- 0 lines on 3.4 at
        any drain rate, 67 on 3.7b when drained tightly. That is precisely why
        this cannot be inferred locally: the same child gives different answers
        on different boxes, while the pane query is right on all of them.
        """
        if self._multiplexed:
            return self._pane_alt_screen()
        return self.alt_screen

    def _pane_alt_screen(self) -> bool:
        """Ask tmux whether the inner application is on the alternate screen.

        Cached for _MODE_POLL_SECONDS so this cannot land in the render path at
        frame rate. On any failure -- tmux gone, session not up yet, a target we
        could not parse -- the last known answer stands, and True is the opening
        assumption: a wrapped child that has not answered yet is more likely to
        be an application that owns its screen than a shell, and guessing that
        way costs a working PageUp rather than a scroll into history that is
        probably empty anyway.
        """
        now = time.monotonic()
        if self._mode_checked_at and now - self._mode_checked_at < _MODE_POLL_SECONDS:
            return self._mode_cached
        return self._query_pane_alt_screen(now)

    def invalidate_mode(self) -> None:
        """Drop the cached regime so the next read queries tmux.

        Called on a session switch: a shell window and a Claude Code window are
        in different scrolling regimes, and a cached answer from the window the
        user just left is not merely stale, it makes a physical button do the
        wrong thing on the window they just arrived at.
        """
        self._mode_checked_at = 0.0

    def _query_pane_alt_screen(self, now: float) -> bool:
        self._mode_checked_at = now

        if self._tmux_target is None:
            return self._mode_cached

        socket_name, target = self._tmux_target
        try:
            out = subprocess.run(
                ["tmux", "-L", socket_name, "display-message", "-p", "-t", target,
                 "#{alternate_on} #{history_size}"],
                capture_output=True, text=True, timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            return self._mode_cached
        answer = out.stdout.split()
        if (out.returncode != 0 or len(answer) != 2
                or answer[0] not in ("0", "1")
                or not answer[1].isdigit()):
            return self._mode_cached
        self._mode_cached = answer[0] == "1"
        self._pane_history = int(answer[1])
        return self._mode_cached

    def pane_history_size(self, *, force: bool = False) -> int:
        """The pane's tmux history_size, cached like the regime.

        `force` drops the cache: a scroll worker wants the size now, not the
        value from before the latest output arrived.
        """
        if force:
            self._mode_checked_at = 0.0
        self._pane_alt_screen()
        return self._pane_history

    def capture_view(self, offset: int, history_size: int) -> Grid | None:
        """The pane as it looked `offset` lines back, read from tmux.

        tmux numbers pane lines from the top of the visible screen, negative
        into history, so the window `offset` lines up is -offset through
        ROWS-1-offset -- exactly ROWS lines, colours included (-e). The output
        is replayed into a fresh emulator so the same glyph and colour folding
        as the live grid applies; a short answer pads at the top, a long one is
        scrolled off the top by the emulator itself.
        """
        if self._tmux_target is None or offset <= 0:
            return None
        socket_name, target = self._tmux_target
        try:
            out = subprocess.run(
                ["tmux", "-L", socket_name, "capture-pane", "-p", "-e",
                 "-t", target, "-S", str(-offset), "-E", str(ROWS - 1 - offset)],
                capture_output=True, text=True, timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        lines = out.stdout.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        screen = pyte.Screen(COLS, ROWS)
        stream = pyte.ByteStream(screen)
        pad = max(0, ROWS - len(lines))
        body = "\x1b[0m" + "\r\n\x1b[0m".join(lines[:ROWS])
        try:
            stream.feed(("\r\n" * pad + body).encode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 -- a malformed sequence drops the view
            return None
        return Grid.from_pyte(screen)

    def view(self, offset: int) -> Grid:
        """The screen as it looked `offset` lines further back.

        offset 0 is the live screen and returns the current grid unchanged, so
        the ordinary path costs nothing.

        Read-only by construction: it composes history lines with the live
        buffer and never touches the screen. pyte does offer prev_page(), but it
        rewrites screen.buffer and its before_event hook snaps back to the
        bottom on every stream event -- so a reader scrolled up would be yanked
        to the bottom by the next byte the child wrote, which is precisely while
        a long answer is worth reading.
        """
        if offset <= 0:
            return self._grid

        top = self._screen.history.top
        offset = min(offset, len(top))
        if offset == 0:
            return self._grid

        # The window straddles the boundary: `offset` lines from the end of
        # history, then the rest from the top of the live screen.
        from_history = list(top)[len(top) - offset:]
        from_screen = [self._screen.buffer[y] for y in range(max(0, ROWS - offset))]
        return Grid.from_lines((from_history + from_screen)[:ROWS])

    @property
    def cursor(self) -> tuple[int, int, bool]:
        """Visible caret position as (row, col, visible).

        The cursor can sit in scrollback above the visible screen (a HistoryScreen
        leaves it at its logical position), which has no representable cell -- so
        off-screen positions report visible=False and the client draws nothing.
        """
        row, col = self._screen.cursor.y, self._screen.cursor.x
        if not (0 <= row < ROWS and 0 <= col < COLS):
            return (0, 0, False)
        return (row, col, not self._screen.cursor.hidden)
