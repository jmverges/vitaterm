"""SEAL: every frame after AUTH_OK is encrypted and carries a tag (docs/SEAL.md).

Authentication proves the card held the key once. Before SEAL nothing after
that was protected, and anyone who could see the TCP stream -- anyone in radio
range, on the open networks a DS Lite can only join -- could splice a KEY frame
into it and type into the Claude session. These tests pin the construction
byte for byte (the card's C half is written against the same vectors), then
show each way a frame can be not-the-peer's-own ending the connection, and that
there is no way around it: a card that does not ask is refused, and a HELLO
edited on the way leaves the two ends with different keys.
"""

import hashlib
import hmac
import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest

from dsterm import chacha20, pairing
from dsterm.auth import compute_mac, generate_key
from dsterm.client import AuthError, Client
from dsterm.protocol import (
    FEATURE_MODE,
    CARD_NONCE_SIZE,
    FEATURE_SEAL,
    FEATURE_SEAL3,
    FEATURE_SNIPPETS,
    HEADER_SIZE,
    MAX_PAYLOAD,
    SEAL_AUTH_OK,
    SEAL_FROM_CARD,
    SEAL_FROM_SERVER,
    SEAL_TAG_SIZE,
    FrameReader,
    MsgType,
    ProtocolError,
    Seal,
    SealError,
    decode_diff,
    encode,
    encode_hello,
    encode_key,
    seal_frame,
    seal_key,
    seal_keys,
    seal_tag,
    unseal,
)
from dsterm.server import ClientState, OutboundBatch, Server

from test_backpressure import FakeSock
from test_server import ServerHarness, wait_for_text

REPO = Path(__file__).resolve().parent.parent

# The vectors docs/SEAL.md's C half checks against. Inputs chosen so that no
# byte is zero or repeated by accident: the key counts up, the nonce down.
VEC_KEY = bytes(range(16))
VEC_NONCE = bytes((0xF0 - i) & 0xFF for i in range(32))
VEC_HELLO = (b"ds-term/1 mode seal seal3 grid=42x24 id=deadbeef "
             b"cn=00112233445566778899aabbccddeeff")
VEC_K = "5634f3433eb47e49561135a05ccc3bdc3f98354b117f8c9e75b878af2ed2a180"
# Card to server, seq 0: KEY "a".
VEC_KEY_A_C0 = "445301100009e17bcd5f81a8043f30"
# Server to card, seq 5: an empty PONG.
VEC_PONG_S5 = "445301930008f44621ea9341ac14"
# Server to card, seq 6: ERROR "hi".
VEC_ERROR_S6 = "445301ff000abe72b8c5680c2d9df4ad"


def pair_of_seals(k: bytes | None = None) -> tuple[Seal, Seal]:
    k = k or seal_key(generate_key(), os.urandom(32), VEC_HELLO)
    return Seal.for_card(k), Seal.for_server(k)


def sealed_reader(seal: Seal) -> FrameReader:
    reader = FrameReader()
    reader.start_sealing(seal)
    return reader


# -- the construction -----------------------------------------------------------


class TestTheVectors:
    """Byte for byte. A one-bit disagreement with the card is a handheld that
    authenticates and then drops on its first frame, every time."""

    def test_key_derivation(self):
        k = seal_key(VEC_KEY, VEC_NONCE, VEC_HELLO)
        assert k.hex() == VEC_K
        # Spelled out independently, so the vector is not just this module
        # agreeing with itself.
        assert k == hmac.new(VEC_KEY, b"dsterm seal v3" + VEC_NONCE +
                             hashlib.sha256(VEC_HELLO).digest(),
                             hashlib.sha256).digest()

    def test_the_hello_is_in_the_key(self):
        k = seal_key(VEC_KEY, VEC_NONCE, VEC_HELLO)
        assert seal_key(VEC_KEY, VEC_NONCE, VEC_HELLO.replace(b" seal3", b"")) != k
        assert seal_key(VEC_KEY, VEC_NONCE, VEC_HELLO + b" ") != k
        # The card nonce is in the HELLO, so a new one is a new key: what
        # makes a replayed server useless against the card.
        other = VEC_HELLO.replace(b"cn=0011", b"cn=0012")
        assert seal_key(VEC_KEY, VEC_NONCE, other) != k

    def test_the_three_keys(self):
        k = seal_key(VEC_KEY, VEC_NONCE, VEC_HELLO)
        mac, from_card, from_server = seal_keys(k)
        for key, label in ((mac, b"mac"), (from_card, b"enc C"), (from_server, b"enc S")):
            assert key == hmac.new(k, label, hashlib.sha256).digest()
        assert len({mac, from_card, from_server}) == 3

    def test_a_card_frame(self):
        k = seal_key(VEC_KEY, VEC_NONCE, VEC_HELLO)
        sealed = seal_frame(k, b"C", 0, encode(MsgType.KEY, b"a"))
        assert sealed.hex() == VEC_KEY_A_C0
        # Spelled out: encrypt with the card's key and nonce 0, then tag the
        # ciphertext with the tag key.
        mac, from_card, _ = seal_keys(k)
        header = bytes([0x44, 0x53, 0x01, 0x10, 0x00, 0x09])   # len 1 + 8
        ciphertext = chacha20.xor(from_card, bytes(12), b"a")
        assert ciphertext != b"a"
        tag = hmac.new(mac, b"C" + bytes(4) + header + ciphertext,
                       hashlib.sha256).digest()[:8]
        assert sealed == header + ciphertext + tag

    def test_the_server_encrypts_with_its_own_key_and_the_sequence(self):
        k = seal_key(VEC_KEY, VEC_NONCE, VEC_HELLO)
        sealed = bytes.fromhex(VEC_ERROR_S6)
        _, _, from_server = seal_keys(k)
        nonce = bytes(8) + (6).to_bytes(4, "big")
        assert sealed[HEADER_SIZE:-SEAL_TAG_SIZE] == chacha20.xor(from_server, nonce, b"hi")
        assert seal_frame(k, b"S", 6, encode(MsgType.ERROR, b"hi")) == sealed

    def test_a_server_frame(self):
        k = seal_key(VEC_KEY, VEC_NONCE, VEC_HELLO)
        assert seal_frame(k, b"S", 5, encode(MsgType.PONG)).hex() == VEC_PONG_S5

    def test_the_card_vector_opens_with_the_seal_class(self):
        k = seal_key(VEC_KEY, VEC_NONCE, VEC_HELLO)
        msgs = list(sealed_reader(Seal.for_server(k)).feed(bytes.fromhex(VEC_KEY_A_C0)))
        assert [(m.type, m.payload) for m in msgs] == [(MsgType.KEY, b"a")]

    def test_the_length_counts_the_tag_and_the_tag_covers_it(self):
        k = seal_key(VEC_KEY, VEC_NONCE, VEC_HELLO)
        frame = encode(MsgType.KEY, b"hello")
        sealed = seal_frame(k, b"C", 0, frame)
        assert len(sealed) == len(frame) + SEAL_TAG_SIZE
        assert int.from_bytes(sealed[4:6], "big") == 5 + SEAL_TAG_SIZE
        # The header as sent, +8 length and all, is what the tag covers.
        assert sealed[-8:] == seal_tag(k, b"C", 0, sealed[:HEADER_SIZE],
                                       sealed[HEADER_SIZE:-SEAL_TAG_SIZE])


class TestTheConstantsMatchTheCard:
    """Read from the C sources, not copied, like the snippet limits are."""

    @staticmethod
    def _source(name: str) -> str:
        path = REPO / "dsclient" / "source" / name
        if not path.exists():
            pytest.skip(f"{name} is not in this tree")
        return path.read_text()

    def test_tag_and_key_sizes(self):
        text = self._source("seal.h")
        assert int(re.search(r"^#define\s+SEAL_TAG\s+(\d+)", text, re.M).group(1)) \
            == SEAL_TAG_SIZE
        assert int(re.search(r"^#define\s+SEAL_KEY\s+(\d+)", text, re.M).group(1)) == 32

    def test_the_label(self):
        assert '"dsterm seal v3"' in self._source("seal.c")

    def test_the_card_asks(self):
        caps = re.search(r'#define\s+PROTO_HELLO_CAPS\s+"([^"]*)"',
                         self._source("proto.h")).group(1)
        assert FEATURE_SEAL in caps.split() and FEATURE_SEAL3 in caps.split()

    def test_the_card_nonce_size(self):
        text = self._source("proto.h")
        assert int(re.search(r"^#define\s+PROTO_CARD_NONCE_BYTES\s+(\d+)", text,
                             re.M).group(1)) == CARD_NONCE_SIZE


class TestSealFrameRefusesWhatIsNotOneFrame:
    K = seal_key(VEC_KEY, VEC_NONCE, VEC_HELLO)

    @pytest.mark.parametrize("data", [
        b"",
        b"DS\x01",                                          # half a header
        encode(MsgType.KEY, b"ab")[:-1],                    # half a payload
        encode(MsgType.PING) + encode(MsgType.PING),        # two frames
        b"XY\x01\x12\x00\x00",                              # not ours
    ])
    def test_refused(self, data):
        with pytest.raises(ProtocolError):
            seal_frame(self.K, b"S", 0, data)

    def test_a_sealed_frame_must_still_fit_the_readers_cap(self):
        # Both readers refuse a declared length over MAX_PAYLOAD on the header
        # alone, so the +8 may not push a legal payload past it.
        seal_frame(self.K, b"S", 0, encode(MsgType.KEY, b"x" * (MAX_PAYLOAD - 8)))
        with pytest.raises(ProtocolError):
            seal_frame(self.K, b"S", 0, encode(MsgType.KEY, b"x" * (MAX_PAYLOAD - 7)))

    def test_direction_and_sequence_are_checked(self):
        with pytest.raises(ValueError):
            seal_frame(self.K, b"X", 0, encode(MsgType.PING))
        with pytest.raises(ProtocolError):
            seal_frame(self.K, b"S", -1, encode(MsgType.PING))
        with pytest.raises(ProtocolError):
            seal_frame(self.K, b"S", 1 << 32, encode(MsgType.PING))
        # The last count is never used: the connection ends before the
        # cipher's nonce could come round again.
        with pytest.raises(ProtocolError):
            seal_frame(self.K, b"S", (1 << 32) - 1, encode(MsgType.PING))
        seal_frame(self.K, b"S", (1 << 32) - 2, encode(MsgType.PING))

    def test_the_nonce_must_be_a_nonce(self):
        with pytest.raises(ProtocolError):
            seal_key(VEC_KEY, b"short", VEC_HELLO)


# -- round trip and every way a frame can fail ------------------------------------


class TestRoundTrip:
    def test_payloads_come_out_as_they_went_in(self):
        card, server = pair_of_seals()
        frames = [encode(MsgType.KEY, b"ls\r"), encode(MsgType.PING),
                  encode(MsgType.VIEW, b"\xff\xf4"), encode(MsgType.KEY, bytes(range(32)))]
        reader = sealed_reader(server)
        got = [(m.type, m.payload) for m in reader.feed(b"".join(card.seal(f) for f in frames))]
        assert got == [(MsgType(f[3]), f[HEADER_SIZE:]) for f in frames]
        assert card.sent == server.received == len(frames)

    def test_a_blob_of_frames_is_sealed_one_counter_step_each(self):
        # The server's outbox can hold several frames in one bytes item; each
        # must be sealed on its own, which is what the card verifies.
        card, server = pair_of_seals()
        blob = encode(MsgType.PONG) + encode(MsgType.CURSOR, b"\x01\x02\x01") \
            + encode(MsgType.PONG)
        sealed = server.seal(blob)
        assert server.sent == 3
        assert len(sealed) == len(blob) + 3 * SEAL_TAG_SIZE
        msgs = list(sealed_reader(card).feed(sealed))
        assert [m.type for m in msgs] == [MsgType.PONG, MsgType.CURSOR, MsgType.PONG]

    def test_byte_at_a_time(self):
        card, server = pair_of_seals()
        wire = card.seal(encode(MsgType.KEY, b"abc") + encode(MsgType.PING))
        reader, got = sealed_reader(server), []
        for i in range(len(wire)):
            got.extend(reader.feed(wire[i:i + 1]))
        assert [(m.type, m.payload) for m in got] == [(MsgType.KEY, b"abc"),
                                                      (MsgType.PING, b"")]

    def test_a_blob_that_is_not_whole_frames_is_refused_and_not_counted(self):
        _, server = pair_of_seals()
        with pytest.raises(ProtocolError):
            server.seal(encode(MsgType.PONG) + b"DS\x01")
        assert server.sent == 0, "a refused blob moved the counter"

    def test_the_switch_happens_at_the_frame_boundary_mid_feed(self):
        # AUTH_OK and the first sealed frames commonly share one segment. The
        # reader must switch between them, not after the whole buffer.
        card, server = pair_of_seals()
        wire = encode(MsgType.AUTH_OK, SEAL_AUTH_OK) + server.seal(
            encode(MsgType.PONG) + encode(MsgType.MODE, b"\x01"))
        reader, got = FrameReader(), []
        for msg in reader.feed(wire):
            got.append((msg.type, msg.payload))
            if msg.type is MsgType.AUTH_OK:
                reader.start_sealing(card)
        assert got == [(MsgType.AUTH_OK, SEAL_AUTH_OK), (MsgType.PONG, b""),
                       (MsgType.MODE, b"\x01")]


class TestEveryForgeryEndsIt:
    """A wrong, missing, replayed, reordered, reflected or skipped frame is a
    SealError: the reader raises, and the connection is over."""

    def _feed(self, server: Seal, wire: bytes) -> list:
        return [(m.type, m.payload) for m in sealed_reader(server).feed(wire)]

    def test_a_tampered_payload(self):
        card, server = pair_of_seals()
        wire = bytearray(card.seal(encode(MsgType.KEY, b"ls\r")))
        wire[HEADER_SIZE] ^= 0x01
        with pytest.raises(SealError):
            self._feed(server, bytes(wire))

    def test_a_tampered_header(self):
        # KEY becomes RESYNC: same length, different type. The type is in the
        # tag, so it cannot be edited any more than the payload can.
        card, server = pair_of_seals()
        wire = bytearray(card.seal(encode(MsgType.KEY, b"x")))
        wire[3] = MsgType.RESYNC
        with pytest.raises(SealError):
            self._feed(server, bytes(wire))

    def test_a_tampered_tag(self):
        card, server = pair_of_seals()
        wire = bytearray(card.seal(encode(MsgType.KEY, b"x")))
        wire[-1] ^= 0x80
        with pytest.raises(SealError):
            self._feed(server, bytes(wire))

    def test_a_missing_tag(self):
        # An unsealed frame on a sealed connection: an injector who does not
        # have the key. A zero-length PING cannot even hold a tag.
        _, server = pair_of_seals()
        with pytest.raises(SealError):
            self._feed(server, encode(MsgType.PING))
        _, server = pair_of_seals()
        with pytest.raises(SealError):
            self._feed(server, encode(MsgType.KEY, b"echo PWNED\r"))

    def test_a_replayed_frame(self):
        card, server = pair_of_seals()
        f0 = card.seal(encode(MsgType.KEY, b"y"))
        reader = sealed_reader(server)
        assert len(list(reader.feed(f0))) == 1
        with pytest.raises(SealError):
            list(reader.feed(f0))

    def test_reordered_frames(self):
        card, server = pair_of_seals()
        f0 = card.seal(encode(MsgType.KEY, b"a"))
        f1 = card.seal(encode(MsgType.KEY, b"b"))
        with pytest.raises(SealError):
            self._feed(server, f1 + f0)

    def test_a_dropped_frame(self):
        card, server = pair_of_seals()
        f0 = card.seal(encode(MsgType.KEY, b"a"))
        card.seal(encode(MsgType.KEY, b"b"))          # lost in the air
        f2 = card.seal(encode(MsgType.KEY, b"c"))
        reader = sealed_reader(server)
        assert len(list(reader.feed(f0))) == 1
        with pytest.raises(SealError):
            list(reader.feed(f2))

    def test_a_reflected_frame(self):
        # The server's own frame sent back at it: same key, same counter, but
        # the direction byte differs.
        k = seal_key(generate_key(), os.urandom(32), VEC_HELLO)
        server = Seal.for_server(k)
        echoed = server.seal(encode(MsgType.KEY, b"q"))
        with pytest.raises(SealError):
            self._feed(Seal.for_server(k), echoed)

    def test_a_frame_from_another_connection(self):
        # Same key, different nonce: a recording from yesterday.
        key = generate_key()
        old = Seal.for_card(seal_key(key, os.urandom(32), VEC_HELLO))
        now = Seal.for_server(seal_key(key, os.urandom(32), VEC_HELLO))
        with pytest.raises(SealError):
            self._feed(now, old.seal(encode(MsgType.KEY, b"x")))

    def test_an_unknown_type_is_refused_as_forged_not_as_unknown(self):
        # The tag is checked before the type is looked at.
        _, server = pair_of_seals()
        with pytest.raises(SealError):
            self._feed(server, b"DS\x01\x7e\x00\x08" + bytes(8))

    def test_unseal_is_the_same_check(self):
        k = seal_key(VEC_KEY, VEC_NONCE, VEC_HELLO)
        wire = bytes.fromhex(VEC_KEY_A_C0)
        assert unseal(k, SEAL_FROM_CARD, 0, wire[:6], wire[6:]) == b"a"
        for direction, seq in ((SEAL_FROM_SERVER, 0), (SEAL_FROM_CARD, 1)):
            with pytest.raises(SealError):
                unseal(k, direction, seq, wire[:6], wire[6:])


# -- the server's half, without a socket -------------------------------------------


def sealed_server_client(capacity=1 << 20):
    srv = Server(["true"], b"k" * 16, host="127.0.0.1", port=0)
    sock = FakeSock(capacity=capacity)
    c = ClientState(sock=sock, addr=("test", 1), reader=FrameReader(),
                    nonce=os.urandom(32), authed=True)
    c.hello = VEC_HELLO
    k = seal_key(srv.key, c.nonce, c.hello)
    c.seal = Seal.for_server(k)
    srv._clients.append(c)
    return srv, c, sock, Seal.for_card(k)


class TestTheServerSealsAtTheSocket:
    def test_every_frame_of_a_blob_is_sealed_in_order(self):
        srv, c, sock, card = sealed_server_client()
        srv._send_to(c, encode(MsgType.PONG) + encode(MsgType.CURSOR, b"\x00\x00\x01"))
        srv._send_to(c, encode(MsgType.PONG))
        msgs = list(sealed_reader(card).feed(bytes(sock.written)))
        assert [m.type for m in msgs] == [MsgType.PONG, MsgType.CURSOR, MsgType.PONG]
        assert c.seal.sent == 3

    def test_a_stalled_socket_keeps_the_sealed_order(self):
        # Sealed at _send_to, before the kernel: what waits in c.pending is
        # already sealed, in the order the card will read it.
        srv, c, sock, card = sealed_server_client(capacity=0)
        srv._send_to(c, encode(MsgType.PONG))
        srv._send_to(c, encode(MsgType.MODE, b"\x00"))
        sock.capacity = 1 << 20
        srv._push(c)
        msgs = list(sealed_reader(card).feed(bytes(sock.written)))
        assert [m.type for m in msgs] == [MsgType.PONG, MsgType.MODE]

    def test_discarded_outbox_work_never_takes_a_counter_step(self):
        # The reason sealing is not done at enqueue time: a resync clears the
        # outbox, and a frame sealed then thrown away would leave a hole in
        # the counter that ends the connection on the next frame.
        srv, c, sock, card = sealed_server_client()
        c.outbox.extend(encode(MsgType.FRAME_DIFF, b"") for _ in range(3))
        c.outbox.append(OutboundBatch([encode(MsgType.PONG) for _ in range(6)]))
        srv._flush_outbox()                      # releases MESSAGES_PER_TICK
        srv._clear_unsent_outbox(c)              # the rest is discarded
        c.outbox.append(OutboundBatch([encode(MsgType.CURSOR, b"\x01\x01\x01"),
                                       encode(MsgType.PONG)]))
        srv._flush_outbox()
        msgs = list(sealed_reader(card).feed(bytes(sock.written)))
        assert [m.type for m in msgs] == [MsgType.FRAME_DIFF] * 3 + [
            MsgType.PONG, MsgType.CURSOR, MsgType.PONG]
        assert card.received == c.seal.sent == 6

    def test_something_unsealable_ends_the_connection_rather_than_going_out(self):
        srv, c, sock, _ = sealed_server_client()
        srv._send_to(c, b"not a frame")
        assert c not in srv._clients
        assert sock.written == b""

    def test_nothing_is_sealed_before_auth_ok(self):
        srv = Server(["true"], b"k" * 16, host="127.0.0.1", port=0)
        sock = FakeSock(capacity=1 << 20)
        c = ClientState(sock=sock, addr=("test", 1), reader=FrameReader(),
                        nonce=os.urandom(32), authed=True)
        srv._clients.append(c)
        assert c.seal is None and not c.wants_seal
        srv._send_to(c, encode(MsgType.PONG))
        assert bytes(sock.written) == encode(MsgType.PONG)


# -- over a real socket ------------------------------------------------------------


def current_seal_tokens() -> tuple[str, ...]:
    return (FEATURE_SEAL, FEATURE_SEAL3, f"cn={os.urandom(CARD_NONCE_SIZE).hex()}")


class RawCard:
    """A hand-rolled card, for the frames the reference client will not send."""

    def __init__(self, port: int, key: bytes, tokens=None,
                 seal_hello: bytes | None = None):
        """``tokens`` defaults to what a current card asks for, with a fresh
        card nonce. ``seal_hello`` derives the seal from a HELLO other than
        the one sent: what a card would do if its HELLO had been edited on
        the way."""
        if tokens is None:
            tokens = current_seal_tokens()
        self.key = key
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.reader = FrameReader()
        self.queue: list = []
        self.seal: Seal | None = None
        hello = encode_hello(tuple(tokens))
        self.hello = seal_hello if seal_hello is not None else hello[HEADER_SIZE:]
        self.sock.sendall(hello)
        self.nonce = self.next(MsgType.NONCE).payload
        self.sock.sendall(encode(MsgType.AUTH, compute_mac(key, self.nonce)))
        self.auth_ok = self.next(MsgType.AUTH_OK)

    def _fill(self) -> None:
        data = self.sock.recv(65536)
        if not data:
            raise ConnectionError("closed")
        for msg in self.reader.feed(data):
            self.queue.append(msg)
            if (msg.type is MsgType.AUTH_OK and self.seal is None
                    and msg.payload == SEAL_AUTH_OK):
                self.seal = Seal.for_card(seal_key(self.key, self.nonce, self.hello))
                self.reader.start_sealing(self.seal)

    def next(self, msg_type: MsgType, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while self.queue:
                msg = self.queue.pop(0)
                if msg.type is msg_type:
                    return msg
            self._fill()
        raise TimeoutError(msg_type.name)

    def send_sealed(self, frame: bytes) -> bytes:
        wire = self.seal.seal(frame)
        self.sock.sendall(wire)
        return wire

    def closed_within(self, seconds: float = 5.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if not self.sock.recv(65536):
                    return True
            except socket.timeout:
                continue
            except OSError:
                return True
        return False

    def close(self) -> None:
        self.sock.close()


# `cat` rather than the harness's interactive bash: the terminal driver echoes
# what is typed just the same, so a keystroke that reaches the PTY is on screen,
# and cat leaves on SIGTERM where bash makes every teardown wait out its three
# seconds.
CHILD = ["cat"]


@pytest.fixture
def harness():
    with ServerHarness(command=CHILD) as h:
        yield h


class TestNegotiation:
    def test_auth_ok_says_seal_when_the_card_asked(self, harness):
        card = RawCard(harness.port, harness.key)
        try:
            assert card.auth_ok.payload == SEAL_AUTH_OK == b"seal3"
            # And from there it is sealed: a sealed PING is answered with a
            # PONG that verifies against the server direction.
            card.send_sealed(encode(MsgType.PING))
            card.next(MsgType.PONG)
            assert card.seal.received >= 1
        finally:
            card.close()
        assert harness.wait_for_event("every frame encrypted and sealed")

    def test_a_card_that_does_not_ask_is_refused(self, harness):
        # No unsealed fallback: a HELLO without the token is a card from
        # before SEAL or one with the word taken out on the way, and the
        # second is the reason the first is refused too.
        sock = socket.create_connection(("127.0.0.1", harness.port), timeout=5)
        try:
            sock.sendall(encode_hello((FEATURE_MODE,)))
            reader = FrameReader()
            msgs: list = []
            nonce = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                data = sock.recv(65536)
                if not data:
                    break
                msgs.extend(reader.feed(data))
                if nonce is None and msgs and msgs[0].type is MsgType.NONCE:
                    nonce = msgs[0].payload
                    sock.sendall(encode(MsgType.AUTH, compute_mac(harness.key, nonce)))
            types = [m.type for m in msgs]
            assert MsgType.AUTH_OK not in types
            assert MsgType.ERROR in types
            why = next(m for m in msgs if m.type is MsgType.ERROR).payload
            assert b"older than the computer's" in why
        finally:
            sock.close()
        assert harness.wait_for_event("did not ask for encrypted frames")

    @pytest.mark.parametrize("tokens", [
        (FEATURE_SEAL,),                                     # a card from before v3
        (FEATURE_SEAL, FEATURE_SEAL3),                       # no card nonce
        (FEATURE_SEAL, FEATURE_SEAL3, "cn=00"),              # a short one
        (FEATURE_SEAL, FEATURE_SEAL3, "cn=" + "G" * 32),     # not hex
    ], ids=["v2-card", "no-nonce", "short-nonce", "bad-nonce"])
    def test_without_seal3_and_a_card_nonce_the_card_is_refused_by_name(
            self, harness, tokens):
        # ERROR with a reason, never AUTH_FAIL: the card's key is good, and a
        # card must not take a version mismatch for a revoked pairing.
        sock = socket.create_connection(("127.0.0.1", harness.port), timeout=5)
        try:
            sock.sendall(encode_hello(tokens))
            reader, msgs, nonce = FrameReader(), [], None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                data = sock.recv(65536)
                if not data:
                    break
                msgs.extend(reader.feed(data))
                if nonce is None and msgs and msgs[0].type is MsgType.NONCE:
                    nonce = msgs[0].payload
                    sock.sendall(encode(MsgType.AUTH, compute_mac(harness.key, nonce)))
            types = [m.type for m in msgs]
            assert MsgType.AUTH_OK not in types and MsgType.AUTH_FAIL not in types
            why = next(m for m in msgs if m.type is MsgType.ERROR).payload
            assert b"copy the current one to the card" in why
        finally:
            sock.close()

    def test_the_reference_client_will_not_go_unsealed(self, harness):
        with Client("127.0.0.1", harness.port, harness.key) as c:
            assert c.sealed
        with pytest.raises(AuthError, match="older than the computer"):
            with Client("127.0.0.1", harness.port, harness.key, seal=False):
                pass
        with pytest.raises(AuthError, match="older than the computer"):
            with Client("127.0.0.1", harness.port, harness.key, encrypt=False):
                pass

    def test_a_card_refuses_an_auth_ok_without_seal(self):
        # The other direction: a server that answers AUTH_OK empty -- too old,
        # or with the word stripped -- gets no unsealed session.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        key = generate_key()

        def old_server():
            conn, _ = listener.accept()
            with conn:
                reader = FrameReader()
                conn.sendall(encode(MsgType.NONCE, os.urandom(32)))
                seen = []
                while not any(m.type is MsgType.AUTH for m in seen):
                    seen.extend(reader.feed(conn.recv(4096)))
                conn.sendall(encode(MsgType.AUTH_OK))
                time.sleep(0.5)

        t = threading.Thread(target=old_server, daemon=True)
        t.start()
        try:
            with pytest.raises(AuthError, match="does not encrypt"):
                with Client("127.0.0.1", port, key):
                    pass
        finally:
            t.join(5)
            listener.close()

    def test_a_card_names_a_server_from_before_encryption(self):
        # A server from before v3 sees the card's "seal" and answers AUTH_OK
        # "seal". The card goes no further -- nothing is sent signed-only --
        # and says which side to update.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def v2_server():
            conn, _ = listener.accept()
            with conn:
                reader = FrameReader()
                conn.sendall(encode(MsgType.NONCE, os.urandom(32)))
                seen = []
                while not any(m.type is MsgType.AUTH for m in seen):
                    seen.extend(reader.feed(conn.recv(4096)))
                conn.sendall(encode(MsgType.AUTH_OK, b"seal"))
                time.sleep(0.5)

        t = threading.Thread(target=v2_server, daemon=True)
        t.start()
        try:
            with pytest.raises(AuthError, match="older dsterm: update it there"):
                with Client("127.0.0.1", port, generate_key()):
                    pass
        finally:
            t.join(5)
            listener.close()


class TestASealedSession:
    def test_typing_and_reading_through_the_seal(self, harness):
        # Everything the server sends after AUTH_OK -- MODE, snippets, the
        # paced resync batch, the session snapshot, every diff and cursor --
        # must verify in order, or the client raises SealError here.
        with Client("127.0.0.1", harness.port, harness.key,
                    features=(FEATURE_MODE, FEATURE_SNIPPETS)) as c:
            assert c.sealed
            c.send_keys(b"echo sealed-e2e-41d\r")
            assert wait_for_text(c, "sealed-e2e-41d")
            c.request_resync()
            c.send_view(-3)
            c.send_view(3)
            c.send_ping()
            c.poll(0.5)
            c.send_keys(b"echo still-sealed-88a\r")
            assert wait_for_text(c, "still-sealed-88a")
            assert c._seal.sent >= 6 and c._seal.received > 0
        assert not any("integrity" in e for e in harness.events)

    def test_a_paired_card_seals_with_its_own_key(self, tmp_path):
        with ServerHarness(command=CHILD, config_dir=tmp_path) as h:
            device_key = generate_key()
            pairing.save_device("0a1b2c3d", device_key, config_dir=tmp_path)
            with Client("127.0.0.1", h.port, device_key, device_id="0a1b2c3d") as c:
                assert c.sealed
                c.send_keys(b"echo paired-sealed-3e9\r")
                assert wait_for_text(c, "paired-sealed-3e9")


class TestTheServerDropsWhatIsNotTheCards:
    """Over the wire: each forgery ends the connection with one clear line."""

    def _sealed_card(self, harness) -> RawCard:
        card = RawCard(harness.port, harness.key)
        assert card.seal is not None
        return card

    def _assert_dropped(self, harness, card: RawCard) -> None:
        try:
            assert card.closed_within(5), "the server kept a forged connection"
        finally:
            card.close()
        assert harness.wait_for_event("integrity check failed")

    def test_an_injected_unsealed_keystroke(self, harness):
        card = self._sealed_card(harness)
        card.sock.sendall(encode_key(b"echo INJECTED-5b2\r"))
        self._assert_dropped(harness, card)
        # And it never reached the shell: a fresh, honest card does not see
        # the command line it would have echoed.
        with Client("127.0.0.1", harness.port, harness.key) as c:
            assert not wait_for_text(c, "INJECTED-5b2", timeout=1.5)

    @pytest.mark.parametrize("where", ["payload", "header", "tag"])
    def test_a_tampered_frame(self, harness, where):
        card = self._sealed_card(harness)
        wire = bytearray(card.seal.seal(encode_key(b"x")))
        index = {"payload": HEADER_SIZE, "header": 3, "tag": -1}[where]
        wire[index] ^= 0x01
        card.sock.sendall(bytes(wire))
        self._assert_dropped(harness, card)

    def test_a_replayed_frame(self, harness):
        card = self._sealed_card(harness)
        wire = card.send_sealed(encode(MsgType.PING))
        card.next(MsgType.PONG)
        card.sock.sendall(wire)
        self._assert_dropped(harness, card)

    def test_reordered_frames(self, harness):
        card = self._sealed_card(harness)
        f0 = card.seal.seal(encode(MsgType.PING))
        f1 = card.seal.seal(encode(MsgType.PING))
        card.sock.sendall(f1 + f0)
        self._assert_dropped(harness, card)

    def test_a_dropped_frame(self, harness):
        card = self._sealed_card(harness)
        card.send_sealed(encode(MsgType.PING))
        card.seal.seal(encode(MsgType.PING))          # never sent
        card.send_sealed(encode(MsgType.PING))
        self._assert_dropped(harness, card)

    def test_a_late_hello_must_be_sealed_too(self, harness):
        # HELLO is unsealed only because it comes before AUTH_OK. After it, a
        # plain one is an injection like any other -- and a sealed one is
        # accepted, as the server has always accepted a later HELLO.
        card = self._sealed_card(harness)
        card.send_sealed(encode_hello((FEATURE_SEAL, FEATURE_MODE)))
        card.send_sealed(encode(MsgType.PING))
        card.next(MsgType.PONG)
        card.sock.sendall(encode_hello((FEATURE_MODE,)))
        self._assert_dropped(harness, card)

    def test_a_second_auth_on_a_sealed_connection(self, harness):
        card = self._sealed_card(harness)
        card.send_sealed(encode(MsgType.AUTH, compute_mac(harness.key, card.nonce)))
        try:
            assert card.closed_within(5)
        finally:
            card.close()
        assert harness.wait_for_event("already sealed")

    def test_a_hello_edited_on_the_way(self, harness):
        # The server keys the seal to the HELLO it received; this card keys it
        # to the one it meant to send.  The keys differ, so its first sealed
        # frame -- here a keystroke -- is refused, and nothing reaches the shell.
        sent = current_seal_tokens()
        meant = encode_hello(sent + (FEATURE_MODE, FEATURE_SNIPPETS))
        card = RawCard(harness.port, harness.key, tokens=sent,
                       seal_hello=meant[HEADER_SIZE:])
        assert card.seal is not None
        card.send_sealed(encode_key(b"echo EDITED-6f1\r"))
        self._assert_dropped(harness, card)
