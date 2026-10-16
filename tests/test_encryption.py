"""SEAL v3 on the wire: what crosses the network can no longer be read, and a
recorded session cannot be replayed into an old keystream (docs/SEAL.md).

test_seal.py pins the construction byte for byte. These tests stand where an
eavesdropper stands -- a proxy between a card and the server, keeping every
byte -- and check the two promises the encryption makes:

* Nothing typed and nothing on screen can be read off the wire. The proxy
  plays the attacker as well as it can: it decodes every frame header (they
  are in the clear), strips the tags, and feeds the payloads to the card's
  own screen decoder. The real client sees the text; the attacker's copy of
  the screen never does.
* A server session recorded and replayed at a card gets nowhere. The card's
  fresh nonce gives it keys the recording was not made with, so the first
  replayed frame fails its tag, and nothing of it is decrypted or acted on.
"""

import os
import socket
import threading
import time

import pytest

from dsterm.client import Client
from dsterm.protocol import (
    HEADER,
    HEADER_SIZE,
    SEAL_TAG_SIZE,
    FrameReader,
    Message,
    MsgType,
    SealError,
)

from test_server import ServerHarness, wait_for_text


class RecordingProxy:
    """A TCP relay that keeps every byte, each direction apart.

    With ``replay`` set it does not relay at all: it answers the next
    connection with those recorded server bytes, as someone in the middle
    who saved an earlier session would.
    """

    def __init__(self, upstream_port: int, replay: bytes | None = None) -> None:
        self.upstream_port = upstream_port
        self.replay = replay
        self.from_card = bytearray()
        self.from_server = bytearray()
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._threads: list[threading.Thread] = []
        self._socks: list[socket.socket] = []
        accept = threading.Thread(target=self._accept, daemon=True)
        accept.start()
        self._threads.append(accept)

    def _accept(self) -> None:
        try:
            card, _ = self._listener.accept()
        except OSError:
            return
        self._socks.append(card)
        if self.replay is not None:
            # Play the server's part from the recording, in step: the old
            # NONCE after the HELLO, then -- once the card has answered with
            # its AUTH -- the old AUTH_OK and every sealed frame after it.
            card.settimeout(5)
            nonce_end = HEADER_SIZE + HEADER.unpack_from(self.replay, 0)[3]
            try:
                card.recv(65536)                       # HELLO
                card.sendall(self.replay[:nonce_end])  # the recorded NONCE
                card.recv(65536)                       # AUTH
                card.sendall(self.replay[nonce_end:])
                while card.recv(65536):
                    pass
            except OSError:
                pass
            return
        server = socket.create_connection(("127.0.0.1", self.upstream_port), timeout=5)
        self._socks.append(server)
        for src, dst, log in ((card, server, self.from_card),
                              (server, card, self.from_server)):
            t = threading.Thread(target=self._pipe, args=(src, dst, log), daemon=True)
            t.start()
            self._threads.append(t)

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket, log: bytearray) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                log += data
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def close(self) -> None:
        for s in [self._listener, *self._socks]:
            try:
                s.close()
            except OSError:
                pass


def frames(stream: bytes) -> list[tuple[int, bytes]]:
    """Every frame in a recorded stream, as an eavesdropper reads it: the
    header in the clear, the payload as sent."""
    out, offset = [], 0
    while offset + HEADER_SIZE <= len(stream):
        _, _, raw_type, length = HEADER.unpack_from(stream, offset)
        end = offset + HEADER_SIZE + length
        if end > len(stream):
            break
        out.append((raw_type, bytes(stream[offset + HEADER_SIZE:end])))
        offset = end
    return out


def after(recorded: list[tuple[int, bytes]], last_clear: MsgType) -> list[tuple[int, bytes]]:
    """The sealed frames: those after the last unsealed one, AUTH_OK from the
    server and AUTH from the card."""
    for i, (raw_type, _) in enumerate(recorded):
        if raw_type == last_clear:
            return recorded[i + 1:]
    raise AssertionError(f"no {last_clear.name} on the wire")


def attackers_screen(server_frames: list[tuple[int, bytes]]) -> str:
    """The screen as someone on the network would rebuild it: every frame's
    payload, tag stripped, through the card's own decoders."""
    spy = Client("127.0.0.1", 0, b"\0" * 16)
    for raw_type, payload in server_frames:
        try:
            spy._apply(Message(MsgType(raw_type), payload[:-SEAL_TAG_SIZE]))
        except Exception:
            continue   # garbage, as it should be: keep trying the rest
    return spy.grid.to_text()


@pytest.fixture
def harness():
    with ServerHarness() as h:
        yield h


def test_nothing_typed_or_shown_can_be_read_off_the_wire(harness):
    typed = f"TYPED-{os.urandom(6).hex()}"
    # Printed by the shell from arithmetic, so the text on screen is never
    # the text typed: what the attacker must not see is the server's output.
    shown = f"SHOWN-{0x5EA1 * 3}-{os.urandom(3).hex()}"
    head, tail = shown.rsplit("-", 1)
    command = f"# {typed}\recho SHOWN-$((0x5EA1 * 3))-{tail}\r"

    proxy = RecordingProxy(harness.port)
    try:
        with Client("127.0.0.1", proxy.port, harness.key) as c:
            assert c.sealed
            c.send_keys(command.encode())
            assert wait_for_text(c, shown), "the session did not work through the proxy"
            assert typed in c.grid.to_text()
            c.request_resync()
            c.poll(0.5)
    finally:
        proxy.close()

    card_stream, server_stream = bytes(proxy.from_card), bytes(proxy.from_server)
    # The screen was drawn (the client read the marker off it), so the frames
    # that drew it are in here; a floor only guards against a proxy that
    # recorded nothing. (A quiet shell sends under 2 KB in all.)
    assert len(server_stream) > 500, "too little traffic to mean anything"
    for marker in (typed, shown, head):
        assert marker.encode() not in card_stream
        assert marker.encode() not in server_stream
    # The keys a card sends: in the clear these would be one KEY payload
    # holding the whole command.
    key_payloads = [p for t, p in after(frames(card_stream), MsgType.AUTH) if t == MsgType.KEY]
    assert key_payloads and all(typed.encode() not in p for p in key_payloads)
    # The screen, rebuilt by the attacker from every frame the server sent.
    spied = attackers_screen(after(frames(server_stream), MsgType.AUTH_OK))
    assert shown not in spied and typed not in spied and head not in spied


def test_a_recorded_session_replayed_at_a_card_gets_nowhere(harness):
    # Record a real session from NONCE on, as someone in the middle could.
    proxy = RecordingProxy(harness.port)
    try:
        with Client("127.0.0.1", proxy.port, harness.key) as c:
            c.send_keys(b"echo recorded\r")
            assert wait_for_text(c, "recorded")
    finally:
        proxy.close()
    recording = bytes(proxy.from_server)

    # Replay it at a new connection holding the same key. The NONCE and the
    # AUTH_OK are accepted -- they always were replayable -- but the card's
    # new nonce makes new keys, so the first sealed frame fails its tag
    # instead of decrypting under an old keystream.
    replayer = RecordingProxy(harness.port, replay=recording)
    try:
        with pytest.raises(SealError):
            with Client("127.0.0.1", replayer.port, harness.key, timeout=3) as c:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    c.poll(0.1)
                assert False, "the replayed session was accepted"
    finally:
        replayer.close()


def test_every_connection_has_its_own_card_nonce(harness):
    # Read off the wire, from the HELLO each real connection sent.
    seen = []
    for _ in range(8):
        proxy = RecordingProxy(harness.port)
        try:
            with Client("127.0.0.1", proxy.port, harness.key) as c:
                assert c.sealed
        finally:
            proxy.close()
        raw_type, hello = frames(bytes(proxy.from_card))[0]
        assert raw_type == MsgType.HELLO
        nonces = [t for t in hello.decode().split() if t.startswith("cn=")]
        assert len(nonces) == 1 and len(nonces[0]) == 3 + 32
        seen.append(nonces[0])
    assert len(set(seen)) == len(seen)


def test_the_frames_keep_their_sizes(harness):
    # A stream cipher adds nothing: a sealed frame is its plaintext plus the
    # same 8-byte tag as before, so every server message still fits one
    # 536-byte segment (test_protocol.py proves the plaintext sizes).
    proxy = RecordingProxy(harness.port)
    try:
        with Client("127.0.0.1", proxy.port, harness.key) as c:
            c.send_keys(b"seq 1 200\r")
            assert wait_for_text(c, "200")
    finally:
        proxy.close()
    sealed = after(frames(bytes(proxy.from_server)), MsgType.AUTH_OK)
    assert sealed and all(HEADER_SIZE + len(p) <= 536 for _, p in sealed)
