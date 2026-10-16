"""Reference client.

This is the desktop stand-in for the DS: it does exactly what the handheld will
do, no more. Anything this client needs that the DS cannot manage is a design
error caught here rather than in C on a 67 MHz ARM9.
"""

from __future__ import annotations

import os
import socket
import time
from collections import deque
from dataclasses import dataclass, field

from .auth import compute_mac
from .grid import COLS, ROWS, Grid
from .protocol import (
    ACK_PHOTO,
    CARD_NONCE_SIZE,
    FEATURE_SEAL,
    FEATURE_SEAL3,
    FrameReader,
    HEADER_SIZE,
    MAX_PAYLOAD,
    MODE_APP_SCROLLS,
    MsgType,
    NONCE_SIZE,
    PHOTO_SAVED_OK,
    ProtocolError,
    SEAL_AUTH_OK,
    SEAL_AUTH_OK_V2,
    Seal,
    seal_key,
    decode_camera_ack,
    decode_camera_ready,
    decode_cursor,
    decode_diff,
    decode_full_frame,
    decode_photo_saved,
    decode_scroll,
    decode_mode,
    decode_remote,
    decode_remote_note,
    decode_state,
    decode_view_pos,
    encode,
    encode_camera,
    encode_camera_frame,
    encode_hello,
    encode_key,
    encode_photo_chunk,
    encode_photo_discard,
    encode_photo_end,
    encode_remote_open,
    encode_remote_pick,
    encode_view,
)


class AuthError(Exception):
    pass


@dataclass(slots=True)
class Client:
    host: str
    port: int
    key: bytes
    timeout: float = 10.0
    # Feature tokens announced in the HELLO. Empty mirrors a card built before
    # the feature existed, which is the case worth being able to test.
    features: tuple[str, ...] = ()
    # The 8-hex device id a paired card announces, so the server checks its
    # AUTH against that device's own key. None is a card that was never paired.
    device_id: str | None = None
    # Ask for SEAL (docs/SEAL.md). On by default, unlike ``features``, because
    # the card always asks and the server refuses a card that does not. False
    # mirrors a card from before SEAL, which is how that refusal is tested.
    seal: bool = True
    # Ask for SEAL v3, the encrypted one, with a fresh card nonce. False (with
    # ``seal`` on) mirrors a card from before encryption: it announces only
    # "seal", which a current server refuses by name.
    encrypt: bool = True

    _sock: socket.socket | None = field(default=None, init=False)
    # Set between NONCE and AUTH_OK, when the reader must watch for the
    # AUTH_OK that decides sealing; None before and after.
    _auth_nonce: bytes | None = field(default=None, init=False)
    # The HELLO payload this connection sent, which the seal's key covers.
    _hello: bytes = field(default=b"", init=False)
    # This connection's seal, once AUTH_OK said "seal". None is unsealed.
    _seal: Seal | None = field(default=None, init=False)
    _reader: FrameReader = field(default_factory=FrameReader, init=False)
    _pending: deque = field(default_factory=deque, init=False)
    grid: Grid = field(default_factory=Grid.blank, init=False)
    frames_applied: int = field(default=0, init=False)
    scrolls_applied: int = field(default=0, init=False)
    # (row, col, visible), or None until the first CURSOR arrives.
    cursor: tuple[int, int, bool] | None = field(default=None, init=False)
    # (lines_back, available), or None until the first VIEW_POS arrives. The
    # server only sends VIEW_POS to a client that has proved it understands
    # scrolling by sending a VIEW, so None here means "never asked", not
    # "already at live" -- the same distinction the handheld draws as
    # "-- of -- back" rather than "0 of 0".
    view_pos: tuple[int, int] | None = field(default=None, init=False)
    # Raw MODE flags byte, or None until the first MODE arrives. None is a third
    # state and the card treats it as one: it means the server has not said, not
    # that the child is on the ordinary screen.
    mode_flags: int | None = field(default=None, init=False)
    # Extension messages, verbatim. The terminal does not read them: whatever
    # asked for them with a HELLO token does, and a body it cannot make sense
    # of is its problem to ignore, never a reason to end the terminal.
    extension_frames: deque[bytes] = field(default_factory=deque, init=False)
    # REMOTE rows and notes, decoded, in arrival order: ("row", tuple) as
    # decode_remote returns it, or ("note", text). Kept raw-ish rather than
    # assembled into sets, because what the tests pin is the frame sequence.
    remote_frames: deque[tuple[str, object]] = field(default_factory=deque, init=False)
    # Every STATE reading, in order: (state, detail). The card plays its cues
    # from the edges between them, so the sequence is the thing to test.
    states: deque[tuple[int, str]] = field(default_factory=deque, init=False)
    # CAMERA_READY as decoded (can_save), or None until it arrives.
    camera_ready: bool | None = field(default=None, init=False)
    pongs: int = field(default=0, init=False)
    # CAMERA_ACKs in order: (kind, value) -- preview seq or photo bytes so far.
    camera_acks: deque[tuple[int, int]] = field(default_factory=deque, init=False)
    # PHOTO_SAVED in order: (status, id, path).
    photo_saved: deque[tuple[int, int, str]] = field(default_factory=deque, init=False)
    # `dsterm view`: the last MIRROR (None before one), and the taps asked for.
    mirror_watching: bool | None = field(default=None, init=False)
    touches: deque[tuple[int, int]] = field(default_factory=deque, init=False)

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        # A seal belongs to one connection's nonce. A reconnect starts unsealed
        # with a fresh reader, as the card does, until its own AUTH_OK decides.
        self._reader = FrameReader()
        self._seal = None
        self._auth_nonce = None
        self._handshake()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> "Client":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- handshake ---------------------------------------------------------

    def _handshake(self) -> None:
        tokens = self.features
        if self.seal and FEATURE_SEAL not in tokens:
            tokens += (FEATURE_SEAL,)
        if self.seal and self.encrypt:
            # The card nonce: this connection's own, so a server replayed at
            # this client can never steer it back into an old keystream.
            tokens += (FEATURE_SEAL3, f"cn={os.urandom(CARD_NONCE_SIZE).hex()}")
        tokens += (f"id={self.device_id}",) if self.device_id else ()
        hello = encode_hello(tokens)
        self._hello = hello[HEADER_SIZE:]
        self._send(hello)
        nonce_msg = self._await(MsgType.NONCE, MsgType.ERROR)
        if nonce_msg.type is MsgType.ERROR:
            raise AuthError(f"server refused: {nonce_msg.payload.decode(errors='replace')}")
        if len(nonce_msg.payload) != NONCE_SIZE:
            raise ProtocolError(f"nonce must be {NONCE_SIZE} bytes")

        # The key is never sent -- only proof that we hold it. On an open or WEP
        # network anyone can read this exchange and still learn nothing reusable.
        self._auth_nonce = nonce_msg.payload
        self._send(encode(MsgType.AUTH, compute_mac(self.key, nonce_msg.payload)))

        result = self._await(MsgType.AUTH_OK, MsgType.AUTH_FAIL, MsgType.ERROR)
        if result.type is MsgType.ERROR:
            raise AuthError(f"server refused: {result.payload.decode(errors='replace')}")
        if result.type is MsgType.AUTH_FAIL:
            raise AuthError("authentication rejected")
        if self._seal is None:
            if result.payload == SEAL_AUTH_OK_V2:
                raise AuthError("the computer runs an older dsterm: update it there")
            raise AuthError("the server does not encrypt its frames")

    def _decide_seal(self, auth_ok_payload: bytes) -> None:
        """Turn sealing on when this AUTH_OK carries "seal3".

        Called by _pump the instant the AUTH_OK is parsed, before the next
        frame in the same recv() is: the server commonly sends AUTH_OK and the
        first sealed frames in one segment, and those must be read with the
        tag checked and stripped, not as payload. An AUTH_OK without it leaves
        the connection unsealed, and _handshake refuses it, as a card does.
        """
        nonce, self._auth_nonce = self._auth_nonce, None
        if auth_ok_payload == SEAL_AUTH_OK and nonce is not None:
            self._seal = Seal.for_card(seal_key(self.key, nonce, self._hello))
            self._reader.start_sealing(self._seal)

    @property
    def sealed(self) -> bool:
        """Whether this connection's frames are sealed, both ways."""
        return self._seal is not None

    # -- io ----------------------------------------------------------------

    def _send(self, data: bytes) -> None:
        if self._sock is None:
            raise RuntimeError("not connected")
        # Sealed one frame at a time, in the order they go out; after AUTH_OK
        # that is every frame, whatever its type.
        if self._seal is not None:
            data = self._seal.seal(data)
        self._sock.sendall(data)

    def _await(self, *types: MsgType, timeout: float | None = None):
        """Block for the next message of one of `types`.

        Messages that arrive ahead of it are applied, not dropped: the server
        commonly puts AUTH_OK and the first FRAME_FULL in one segment, and an
        earlier version of this method discarded everything after the match.
        """
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            self._pump()
            while self._pending:
                msg = self._pending.popleft()
                if msg.type in types:
                    return msg
                self._apply(msg)
            if time.monotonic() > deadline:
                raise TimeoutError(f"timed out awaiting {[t.name for t in types]}")

    def _pump(self, timeout: float = 0.1) -> int:
        """Read whatever is available, waiting at most `timeout` for it, and
        queue every whole message it yielded."""
        if self._sock is None:
            raise RuntimeError("not connected")
        self._sock.settimeout(max(0.001, timeout))
        try:
            data = self._sock.recv(MAX_PAYLOAD)
        except socket.timeout:
            return 0
        if not data:
            raise ConnectionError("server closed the connection")
        before = len(self._pending)
        # One frame at a time rather than extend(): the reader's mode can
        # change between two frames of the same recv(), at the AUTH_OK.
        for msg in self._reader.feed(data):
            self._pending.append(msg)
            if msg.type is MsgType.AUTH_OK and self._auth_nonce is not None:
                self._decide_seal(msg.payload)
        return len(self._pending) - before

    # -- frames ------------------------------------------------------------

    def _apply(self, msg) -> None:
        if msg.type is MsgType.FRAME_FULL:
            self.grid = decode_full_frame(msg.payload)
            self.frames_applied += 1
        elif msg.type is MsgType.FRAME_DIFF:
            cells = list(self.grid.cells)
            for change in decode_diff(msg.payload):
                cells[change.row * COLS + change.col] = change.cell
            self.grid = Grid(tuple(cells))
            self.frames_applied += 1
        elif msg.type is MsgType.SCROLL:
            _, _, lines = decode_scroll(msg.payload)
            self.grid = self.grid.shifted(lines)
            self.scrolls_applied += 1
        elif msg.type is MsgType.CURSOR:
            self.cursor = decode_cursor(msg.payload)
        elif msg.type is MsgType.VIEW_POS:
            self.view_pos = decode_view_pos(msg.payload)
        elif msg.type is MsgType.MODE:
            self.mode_flags = decode_mode(msg.payload)
        elif msg.type is MsgType.EXTENSION:
            self.extension_frames.append(bytes(msg.payload))
        elif msg.type is MsgType.REMOTE:
            self.remote_frames.append(("row", decode_remote(msg.payload)))
        elif msg.type is MsgType.REMOTE_NOTE:
            self.remote_frames.append(("note", decode_remote_note(msg.payload)))
        elif msg.type is MsgType.STATE:
            self.states.append(decode_state(msg.payload))
        elif msg.type is MsgType.MIRROR:
            if len(msg.payload) != 1 or msg.payload[0] & ~1:
                raise ProtocolError("bad MIRROR payload")
            self.mirror_watching = bool(msg.payload[0])
        elif msg.type is MsgType.TOUCH:
            if len(msg.payload) != 2 or msg.payload[1] >= 192:
                raise ProtocolError("bad TOUCH payload")
            self.touches.append((msg.payload[0], msg.payload[1]))
        elif msg.type is MsgType.CAMERA_READY:
            self.camera_ready = decode_camera_ready(msg.payload)
        elif msg.type is MsgType.CAMERA_ACK:
            self.camera_acks.append(decode_camera_ack(msg.payload))
        elif msg.type is MsgType.PHOTO_SAVED:
            self.photo_saved.append(decode_photo_saved(msg.payload))
        elif msg.type is MsgType.PONG:
            self.pongs += 1
        elif msg.type is MsgType.ERROR:
            raise ConnectionError(msg.payload.decode(errors="replace"))

    def poll(self, seconds: float = 0.1) -> int:
        """Apply whatever has arrived. Returns messages handled."""
        deadline = time.monotonic() + seconds
        handled = 0
        while True:
            # Never wait past the deadline: a poll(0.002) that blocked 100 ms
            # for data locked a caller's timing to the server's frames.
            self._pump(min(0.1, deadline - time.monotonic()))
            while self._pending:
                self._apply(self._pending.popleft())
                handled += 1
            if handled or time.monotonic() >= deadline:
                break
        return handled

    # -- input -------------------------------------------------------------

    @property
    def app_scrolls(self) -> bool | None:
        """Whether the application owns scrolling; None until told.

        Masks bit 0 and ignores the rest, exactly as the card does, so claiming
        a reserved bit later cannot change what this returns.
        """
        if self.mode_flags is None:
            return None
        return bool(self.mode_flags & MODE_APP_SCROLLS)

    def send_keys(self, data: bytes) -> None:
        """The only thing this client can ask the server to do."""
        self._send(encode_key(data))

    def request_resync(self) -> None:
        self._send(encode(MsgType.RESYNC))

    def send_view(self, lines: int) -> None:
        """Move this client's view through history. Negative is back.

        An intent, not content: the server owns the history and decides what is
        representable. Sending this is also what tells the server this client
        understands VIEW_POS, so nothing arrives until the first one goes out.
        """
        self._send(encode_view(lines))

    def send_ping(self) -> None:
        self._send(encode(MsgType.PING))

    def send_remote_open(self, is_open: bool) -> None:
        """The REMOTE screen opened or closed. The card sends this only after
        it has received a REMOTE frame; this client leaves that to its caller,
        so a test can also show what happens when the rule is broken."""
        self._send(encode_remote_open(is_open))

    def send_remote_pick(self, index: int, action: int, check: int = 0) -> None:
        """``check`` is protocol.remote_row_check() of the row at ``index`` as
        it was received; left at 0 it names no row, which is right only for
        NEW -- a pick of a real row with it is refused as a list that moved."""
        self._send(encode_remote_pick(index, action, check))

    # -- camera --------------------------------------------------------------

    def send_subscreen(self, payload: bytes) -> None:
        self._send(encode(MsgType.SUBSCREEN, payload))

    def send_camera(self, state: int) -> None:
        """The camera screen's state: 0 off, 1 inner, 2 outer."""
        self._send(encode_camera(state))

    def send_preview(self, flags: int, seq: int, pixels: bytes,
                     fmt: int | None = None) -> None:
        if fmt is None:
            self._send(encode_camera_frame(flags, seq, pixels))
        else:
            self._send(encode_camera_frame(flags, seq, pixels, fmt))

    def send_photo(self, packed: bytes, camera: int, fmt: int,
                   chunk: int = 1024, window: int = 8192,
                   timeout: float = 30.0) -> tuple[int, int, str]:
        """Upload one packed planar picture: 1 KB chunks, never more than
        `window` unacknowledged bytes in flight, then END. Returns the
        PHOTO_SAVED tuple the server answers with."""
        import zlib
        total = len(packed)
        sent = acked = 0
        deadline = time.monotonic() + timeout
        while sent < total or acked < total:
            while sent < total and sent - acked < window:
                end = min(total, sent + chunk)
                self._send(encode_photo_chunk(sent, packed[sent:end]))
                sent = end
            self.poll(0.05)
            while self.camera_acks:
                kind, value = self.camera_acks.popleft()
                if kind == ACK_PHOTO:
                    acked = max(acked, value)
            if time.monotonic() > deadline:
                raise TimeoutError("photo upload stalled")
        self._send(encode_photo_end(fmt, camera, zlib.crc32(packed) & 0xFFFFFFFF))
        while not self.photo_saved:
            self.poll(0.05)
            if time.monotonic() > deadline:
                raise TimeoutError("no PHOTO_SAVED")
        return self.photo_saved.popleft()

    def send_discard(self, photo_id: int) -> None:
        self._send(encode_photo_discard(photo_id))


# -- pairing -----------------------------------------------------------------


class PairError(Exception):
    """The server refused to pair, or did not keep its commitment."""


def pair(host: str, port: int, device_id: str, accept=lambda code: True,
         timeout: float = 10.0, then=None) -> bytes:
    """Do what the handheld does in its pairing screen, and return the device
    key.  `accept(code)` is the person comparing codes: it is given the six
    digits this side shows and answers whether they match.  `then(sock)`, for
    tests, runs on the same connection after PAIR_DONE, where the card would
    simply close it.  See dsterm/pairing.py for the construction."""
    import hmac as _hmac
    import os as _os

    from . import pairing, x25519
    from .protocol import (PAIR_NONCE_SIZE, decode_pair_nonce, decode_pair_reply,
                           encode_pair_hello)

    private = x25519.generate_private()
    card_public = x25519.public_key(private)
    id_bytes = bytes.fromhex(device_id)
    sock = socket.create_connection((host, port), timeout=timeout)
    reader = FrameReader()

    def next_message():
        deadline = time.monotonic() + timeout
        while True:
            if pending:
                return pending.pop(0)
            if time.monotonic() > deadline:
                raise TimeoutError("timed out pairing")
            data = sock.recv(MAX_PAYLOAD)
            if not data:
                raise PairError("server closed the connection")
            pending.extend(reader.feed(data))

    def expect(kind):
        while True:
            msg = next_message()
            if msg.type is MsgType.NONCE:
                continue        # sent to every connection; pairing ignores it
            if msg.type is MsgType.PAIR_FAIL:
                raise PairError(msg.payload.decode("ascii", "replace"))
            if msg.type is not kind:
                raise PairError(f"unexpected {msg.type.name}")
            return msg

    pending: list = []
    try:
        sock.sendall(encode_pair_hello(id_bytes, card_public))
        server_public, commit = decode_pair_reply(expect(MsgType.PAIR_REPLY).payload)
        card_nonce = _os.urandom(PAIR_NONCE_SIZE)
        sock.sendall(encode(MsgType.PAIR_CONFIRM, card_nonce))
        server_nonce = decode_pair_nonce(expect(MsgType.PAIR_OK).payload)
        if not _hmac.compare_digest(
                pairing.commitment(card_public, server_public, server_nonce), commit):
            raise PairError("the server did not keep its commitment")
        shared = x25519.x25519(private, server_public)
        try:
            confirm_key, device_key = pairing.derive(
                shared, card_nonce, server_nonce, id_bytes, card_public, server_public)
        except pairing.PairingError as exc:
            raise PairError(str(exc)) from exc
        if not accept(pairing.code(card_public, server_public, card_nonce, server_nonce)):
            raise PairError("the codes did not match")
        sock.sendall(encode(MsgType.PAIR_ACCEPT, pairing.accept_mac(
            confirm_key, pairing.ROLE_CARD, card_public, server_public)))
        done = expect(MsgType.PAIR_DONE)
        expected = pairing.accept_mac(confirm_key, pairing.ROLE_SERVER,
                                      card_public, server_public)
        if not _hmac.compare_digest(expected, done.payload):
            raise PairError("the server's confirmation did not verify")
        if then is not None:
            then(sock)
        return device_key
    finally:
        sock.close()
