"""A full send buffer is backpressure, not a disconnect.

This is the bug that felt like "it disconnects when I type". The client sockets
are non-blocking, and _send_to used sendall(), which raises BlockingIOError the
instant the kernel buffer fills. That was caught as OSError and treated as a
dead connection, so a burst -- and input triggers a resync, which is the biggest
burst there is -- hung up on a perfectly healthy handheld. It reconnected, played
its connect sound, and did it again on the next few keystrokes.

sendall() is also unsafe here for a second reason worth pinning: it can write
part of the data before raising, so there is no offset to resume from. The fix
sends explicitly and keeps the unaccepted tail.
"""

import errno
import socket
import time

import pytest

from dsterm.protocol import MsgType, encode
from dsterm.server import PENDING_LIMIT, ClientState, Server


class FakeSock:
    """A socket that accepts a fixed number of bytes per send, then refuses."""

    def __init__(self, capacity=0, chunk=None):
        self.capacity = capacity      # bytes it will take before EAGAIN
        self.chunk = chunk            # cap per individual send, for partials
        self.written = bytearray()
        self.closed = False

    def send(self, data):
        room = self.capacity - len(self.written)
        if room <= 0:
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
        n = min(len(data), room)
        if self.chunk is not None:
            n = min(n, self.chunk)
        self.written += bytes(data[:n])
        return n

    def sendall(self, data):            # must never be called on a nonblocking sock
        raise AssertionError("sendall() on a non-blocking socket")

    def close(self):
        self.closed = True

    def fileno(self):
        return -1


def make_server_and_client(capacity=0, chunk=None):
    srv = Server(["true"], b"k" * 32, host="127.0.0.1", port=0)
    sock = FakeSock(capacity=capacity, chunk=chunk)
    c = ClientState(sock=sock, addr=("test", 1), reader=None, nonce=b"", authed=True)
    srv._clients.append(c)
    return srv, c, sock


class TestAFullBufferIsNotADisconnect:
    def test_eagain_keeps_the_client(self):
        srv, c, _ = make_server_and_client(capacity=0)   # refuses everything
        srv._send_to(c, encode(MsgType.PONG))
        assert c in srv._clients, "a full send buffer dropped the client"
        assert srv.counters.drops == 0

    def test_the_refused_bytes_are_kept_not_lost(self):
        srv, c, _ = make_server_and_client(capacity=0)
        payload = encode(MsgType.PONG)
        srv._send_to(c, payload)
        assert bytes(c.pending) == payload, "refused bytes were dropped on the floor"

    def test_the_tail_goes_out_when_the_socket_frees_up(self):
        srv, c, sock = make_server_and_client(capacity=0)
        payload = encode(MsgType.PONG)
        srv._send_to(c, payload)
        sock.capacity = 4096                    # the stall clears
        srv._push(c)
        assert bytes(sock.written) == payload
        assert not c.pending

    def test_a_partial_send_resumes_from_the_right_offset(self):
        # The reason sendall() cannot simply be retried: it can write some bytes
        # and then raise, and a naive retry would repeat them and desynchronise
        # the framing for the rest of the connection.
        srv, c, sock = make_server_and_client(capacity=3, chunk=3)
        payload = encode(MsgType.PONG)          # 6 bytes
        srv._send_to(c, payload)
        assert bytes(sock.written) == payload[:3]
        sock.capacity = 4096
        srv._push(c)
        assert bytes(sock.written) == payload, "resumed from the wrong offset"

    def test_ordering_survives_a_stall(self):
        srv, c, sock = make_server_and_client(capacity=0)
        first, second = encode(MsgType.PONG), encode(MsgType.AUTH_OK)
        srv._send_to(c, first)
        srv._send_to(c, second)
        sock.capacity = 4096
        srv._push(c)
        assert bytes(sock.written) == first + second, "messages were reordered"

    def test_a_real_error_still_drops(self):
        # Backpressure is not an error, but an error is still an error.
        srv, c, sock = make_server_and_client(capacity=0)

        def broken(_data):
            raise OSError(errno.ECONNRESET, "Connection reset by peer")

        sock.send = broken
        srv._send_to(c, encode(MsgType.PONG))
        assert c not in srv._clients, "a reset connection was kept alive"

    def test_a_client_that_never_drains_is_eventually_dropped(self):
        # The other half: backpressure must not become an unbounded buffer.
        srv, c, _ = make_server_and_client(capacity=0)
        blob = b"x" * 8192
        for _ in range((PENDING_LIMIT // len(blob)) + 2):
            if c not in srv._clients:
                break
            srv._send_to(c, blob)
        assert c not in srv._clients, "pending grew past the limit without dropping"


class TestAgainstARealSocket:
    def test_a_burst_larger_than_the_socket_buffer_does_not_drop(self):
        # The end-to-end shape of the original bug, with a real kernel: fill the
        # send buffer by writing far more than a peer that is not reading can
        # take, and require the connection to survive it.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        # Both buffers have to be small, and the receive side has to be set
        # before connect or the kernel has already sized it. Loopback buffers
        # are generous enough to swallow an entire screen otherwise, and the
        # test would pass without ever reaching the condition it is about.
        peer = socket.socket()
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
        peer.connect(listener.getsockname())
        conn, _ = listener.accept()
        conn.setblocking(False)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)

        srv = Server(["true"], b"k" * 32, host="127.0.0.1", port=0)
        c = ClientState(sock=conn, addr=("test", 1), reader=None, nonce=b"", authed=True)
        srv._clients.append(c)
        try:
            # The peer never reads, so this must stall rather than fail. Stop as
            # soon as a tail appears; the point is that the stall is survived,
            # not how many bytes it took to cause one.
            blob = encode(MsgType.PONG) + b"\x00" * 0
            for _ in range(2000):
                if c not in srv._clients or c.pending:
                    break
                srv._send_to(c, blob * 64)
            assert c in srv._clients, "a stalled peer was treated as a dead one"
            assert c.pending, "expected an unsent tail once the buffer filled"

            # Now let the peer drain, and the tail must complete.
            peer.settimeout(2.0)
            for _ in range(50):
                try:
                    if not peer.recv(65536):
                        break
                except socket.timeout:
                    break
                srv._push(c)
                if not c.pending:
                    break
            assert c in srv._clients
        finally:
            for s in (conn, peer, listener):
                try:
                    s.close()
                except OSError:
                    pass
