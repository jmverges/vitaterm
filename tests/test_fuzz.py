"""Seeded fuzzing of everything the server reads.

Each input the server takes from outside -- a card's frames before and after
authentication, a view's lines, and whatever a program prints into the
terminal -- is thrown random and half-valid data, and the rules are the same
for all of them: a decoder answers or raises ProtocolError, the grid stays a
grid of single cells, and the server is still serving when the noise stops.

Two real crashes were found this way (a combining mark, a deeply nested
line), so these run on every change, with fixed seeds: a failure here is
reproducible, never flaky. The seeds and counts are small enough to keep the
suite quick; tools/fuzz.py runs the same thing for as long as you like.
"""

import base64
import inspect
import json
import os
import random
import socket
import tempfile
import time
from pathlib import Path

import pytest
from wcwidth import wcwidth

from dsterm import discovery
from dsterm import protocol as P
from dsterm.client import Client
from dsterm.grid import COLS, ROWS, Grid
from dsterm.session import HISTORY_LINES, DstermHistoryScreen, DstermStream

from test_server import ServerHarness, wait_for_text

# tools/fuzz.py sets these for long runs; the defaults keep the suite quick
# and every run of it identical.
SEED = os.environ.get("DSTERM_FUZZ_SEED", "")
SCALE = max(1, int(os.environ.get("DSTERM_FUZZ_SCALE", "1")))
LIVE_SECONDS = min(18, 6 * SCALE)    # the harness stops its server at 25 s

# Sizes worth hitting: empty, tiny, every fixed payload size in the protocol
# (so the length check passes and the contents are what is tested), and the
# limits.
SIZES = [0, 1, 2, 3, 4, 5, 6, 8, 10, 16, 32, 36, 64, 100, 257, 1024, 1028,
         P.SUBSCREEN_SIZE, 3 + P.PREVIEW_BYTES, P.CAMERA_FRAME_SIZE,
         P.MAX_PAYLOAD]

DECODERS = sorted((name, fn) for name, fn in inspect.getmembers(P, inspect.isfunction)
                  if name.startswith("decode_"))


def _bytes(rng: random.Random, n: int) -> bytes:
    return rng.randbytes(n)


class TestDecoders:
    @pytest.mark.parametrize("name,fn", DECODERS + [("decode_answer", discovery.decode_answer)],
                             ids=[n for n, _ in DECODERS] + ["discovery.decode_answer"])
    def test_a_decoder_answers_or_raises_protocol_error(self, name, fn):
        rng = random.Random(f"decoder-{name}{SEED}")
        for _ in range(300 * SCALE):
            data = _bytes(rng, rng.choice(SIZES))
            try:
                fn(data)
            except P.ProtocolError:
                pass

    def test_seal_never_opens_a_frame_it_did_not_seal(self):
        # A random tag is right one time in 2**64: a forged frame of any type
        # and length is refused, and a refusal is SealError, nothing else.
        rng = random.Random(f"seal{SEED}")
        for _ in range(500 * SCALE):
            seal = P.Seal.for_server(_bytes(rng, P.SEAL_KEY_SIZE))
            body = _bytes(rng, rng.choice(SIZES[:-1]))
            header = P.HEADER.pack(P.MAGIC, P.VERSION, rng.randrange(256), len(body))
            with pytest.raises(P.SealError):
                seal.open(header, body)

    def test_the_frame_reader_raises_nothing_but_protocol_error(self):
        rng = random.Random(f"reader{SEED}")
        for _ in range(300 * SCALE):
            reader = P.FrameReader()
            try:
                for _ in range(5):
                    list(reader.feed(_bytes(rng, rng.randint(0, 600))))
            except P.ProtocolError:
                pass


def _terminal_noise(rng: random.Random) -> str:
    """Text, wide and combining characters, CSI with too many or odd
    parameters, controls: what a misbehaving program can print."""
    parts = []
    for _ in range(rng.randint(1, 40)):
        k = rng.random()
        if k < 0.25:
            parts.append(chr(rng.randint(0x20, 0x7E)) * rng.randint(1, 5))
        elif k < 0.45:
            cp = rng.randint(0x80, 0x2FFFF)
            parts.append(chr(0x41 if 0xD800 <= cp <= 0xDFFF else cp))
        elif k < 0.55:
            parts.append(rng.choice(["́", "̈", "̣", "‍", "️",
                                     "⃣", "\U0001F3FD", "ั", "्"])
                         * rng.randint(1, 4))
        elif k < 0.75:
            params = ";".join(str(rng.randint(0, 400)) for _ in range(rng.randint(0, 6)))
            parts.append("\x1b[" + rng.choice(["", "?", ">"]) + params
                         + rng.choice("ABCDEFGHJKLMPSTXZ@`dfhlmnrsu"))
        elif k < 0.85:
            parts.append(rng.choice(["\r", "\n", "\t", "\b", "\x07", "\x0e", "\x0f",
                                     "\x1bM", "\x1b7", "\x1b8", "\x1bc", "\x1b(0",
                                     "\x1b(B", "\x1b]0;title\x07", "\x1bP1$r\x1b\\"]))
        else:
            parts.append("".join(chr(rng.randint(0, 0x1F)) for _ in range(rng.randint(1, 3))))
    return "".join(parts)


class TestTerminalOutput:
    def test_any_output_becomes_a_grid_of_single_cells(self):
        # The server's own emulator -- DstermHistoryScreen and DstermStream,
        # with their scroll caps, DSR handling and history -- fed byte noise
        # as the PTY delivers it. pyte itself raises on some malformed
        # sequences; Session.pump drops that slice and carries on (test
        # below), so here a raise from feed() is allowed and the grid must
        # still be sound afterwards.
        rng = random.Random(f"terminal{SEED}")
        screen = DstermHistoryScreen(COLS, ROWS, history=HISTORY_LINES, ratio=0.5)
        stream = DstermStream(screen)
        for _ in range(400 * SCALE):
            try:
                stream.feed(_terminal_noise(rng).encode("utf-8", "surrogatepass"))
            except Exception:  # noqa: BLE001 -- pyte's, contained by Session.pump
                pass
            grid = Grid.from_pyte(screen)
            assert len(grid.cells) == COLS * ROWS
            for cell in grid.cells:
                assert len(cell.char) == 1, repr(cell.char)
                assert cell.char == " " or wcwidth(cell.char) == 1, repr(cell.char)
            P.encode_full_frame(grid)

    def test_a_malformed_sequence_from_a_program_costs_a_frame_not_the_terminal(self):
        # Through a real PTY: a program prints CSI sequences pyte refuses,
        # then more output. The session counts the error, and what follows
        # still reaches the card.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                c.send_keys(b"printf '\\033[1;2;3A\\033[5;6;7;8n'\r")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not h.server._session.emulator_errors:
                    c.poll(0.05)
                c.send_keys(b"echo after-bad-csi\r")
                assert wait_for_text(c, "after-bad-csi")
                assert h.server._session.emulator_errors >= 1
                c.send_keys(b"echo still-fine-91\r")
                assert wait_for_text(c, "still-fine-91")


# A program that prints seeded noise without pause, through a real PTY: the
# same generator as above, written as fast as the pipe takes it.
NOISY_CHILD = (
    "import random, sys\n"
    "sys.path.insert(0, {tests!r})\n"
    "from test_fuzz import _terminal_noise\n"
    "rng = random.Random({seed!r})\n"
    "out = sys.stdout.buffer\n"
    "while True:\n"
    "    out.write(_terminal_noise(rng).encode('utf-8', 'surrogatepass'))\n"
    "    out.flush()\n"
)


class TestALiveServerUnderNoisyOutput:
    def test_the_server_answers_while_its_program_prints_noise(self):
        tests = str(Path(__file__).resolve().parent)
        child = ["python3", "-c", NOISY_CHILD.format(tests=tests, seed=f"child{SEED}")]
        with ServerHarness(command=child) as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                c.poll(0.5)
                deadline = time.monotonic() + LIVE_SECONDS
                slowest = 0.0
                pings = 0
                while time.monotonic() < deadline:
                    before = c.pongs
                    sent = time.monotonic()
                    c.send_ping()
                    while c.pongs == before and time.monotonic() - sent < 10:
                        c.poll(0.002)
                    slowest = max(slowest, time.monotonic() - sent)
                    pings += 1
                    time.sleep(0.05)
                assert pings > 20
                assert slowest < 0.5, f"a PING took {slowest * 1000:.0f} ms under noise"
            assert h._thread.is_alive()
            with Client("127.0.0.1", h.port, h.key) as fresh:
                fresh.poll(0.5)
                fresh.send_ping()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not fresh.pongs:
                    fresh.poll(0.05)
                assert fresh.pongs


FEATURES = ("mode", "snippets", "project", "remote", "state", "commands", "camera", "mirror")
CLIENT_TYPES = [t for t in P.MsgType if t.value < 0x80]
PRE_AUTH = [P.MsgType.HELLO, P.MsgType.AUTH, P.MsgType.PAIR_HELLO,
            P.MsgType.PAIR_CONFIRM, P.MsgType.PAIR_ACCEPT]


class TestALiveServer:
    def test_noise_from_cards_and_views_never_stops_the_server(self):
        rng = random.Random(f"live-server{SEED}")
        config_dir = Path(tempfile.mkdtemp(dir="/tmp", prefix="dfz"))
        # The noise includes random KEY frames, so the child must be one that
        # never reads its input: random keystrokes are not for a real shell.
        idle = ["sh", "-c", "echo fuzz-target-up; exec sleep 600"]
        with ServerHarness(command=idle, config_dir=config_dir, camera_popups=False,
                           cwd=str(config_dir)) as h:
            view_path = config_dir / "run" / "view.sock"
            deadline = time.monotonic() + LIVE_SECONDS
            rounds = 0
            while time.monotonic() < deadline:
                rounds += 1
                kind = rng.random()
                try:
                    if kind < 0.6:
                        self._authed_card(rng, h)
                    elif kind < 0.8:
                        self._before_auth(rng, h)
                    else:
                        self._view(rng, view_path)
                except (ConnectionError, OSError, P.ProtocolError, TimeoutError):
                    pass
                assert h._thread.is_alive(), f"the server stopped in round {rounds}"
            assert rounds > 50

            # And it still serves: a fresh card logs in, is sent the whole
            # screen, and has its PING answered.
            with Client("127.0.0.1", h.port, h.key) as c:
                assert wait_for_text(c, "fuzz-target-up")
                c.send_ping()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not c.pongs:
                    c.poll(0.05)
                assert c.pongs

    @staticmethod
    def _authed_card(rng, h):
        features = tuple(rng.sample(FEATURES, rng.randint(0, len(FEATURES))))
        with Client("127.0.0.1", h.port, h.key, features=features) as c:
            c.poll(0.02)
            for _ in range(rng.randint(1, 8)):
                if rng.random() < 0.2:
                    # Raw bytes spliced into the encrypted session, as
                    # someone on the wire would: never sealed, never opened.
                    c._sock.sendall(_bytes(rng, rng.choice(SIZES[:-1])))
                else:
                    msg_type = rng.choice(CLIENT_TYPES)
                    c._send(P.encode(msg_type, _bytes(rng, rng.choice(SIZES[:-1]))))
                c.poll(0.01)

    @staticmethod
    def _before_auth(rng, h):
        with socket.create_connection(("127.0.0.1", h.port), timeout=1) as s:
            for _ in range(rng.randint(1, 5)):
                if rng.random() < 0.5:
                    s.sendall(_bytes(rng, rng.randint(1, 300)))
                else:
                    s.sendall(P.encode(rng.choice(PRE_AUTH),
                                       _bytes(rng, rng.choice([0, 4, 16, 32, 36, 64, 200]))))

    @staticmethod
    def _view(rng, path):
        with socket.socket(socket.AF_UNIX) as v:
            v.connect(str(path))
            for _ in range(rng.randint(1, 6)):
                pick = rng.random()
                if pick < 0.3:
                    line = json.dumps({
                        "t": rng.choice(["keys", "tap", "ping", "x", None, 5]),
                        "b64": rng.choice([base64.b64encode(_bytes(rng, rng.randint(0, 50))).decode(),
                                           "", "@@", 7, None]),
                        "x": rng.choice([0, 255, 256, -1, "1", 1.5, None, True]),
                        "y": rng.choice([0, 191, 192, -5, None, False]),
                    }).encode()
                elif pick < 0.6:
                    line = json.dumps(rng.choice([[], {}, 1, "s", None, [1, {"t": "keys"}],
                                                  {"t": ["keys"]}])).encode()
                elif pick < 0.7:
                    line = b"[" * rng.randint(500, 5000)
                else:
                    line = _bytes(rng, rng.randint(0, 200)).replace(b"\n", b" ")
                v.sendall(line + b"\n")
