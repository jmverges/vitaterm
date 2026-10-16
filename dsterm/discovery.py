"""Finding the server from the handheld: one UDP probe, one answer.

    card   -> 255.255.255.255:8346   "DSTM probe 1 [id=<hex>]"
    server -> card (unicast)         "DSTM here 1 name=<host> port=<tcp> pair=<0|1>"

The answer names nothing secret -- a short hostname, the TCP port, and whether
a pairing window is open -- because anyone on the LAN can send the probe. It is
ASCII and line-free so either side can be read off a packet capture, and both
directions are bounded and parsed strictly: anything that is not exactly a
probe is ignored without an answer, so this socket cannot be used to make the
server say something it did not mean to.

It is also rate-limited. A UDP reply goes to whatever source address the
datagram claims, and an unlimited responder is a (small) reflector; a few
dozen answers a second is far more than a card scanning three times a second
will ever ask for.

Broadcast does not cross WSL2's NAT mode, so the card also has a subnet sweep
(dsclient/source/discover.c). Either way the person sees the same list.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from typing import Callable

DEFAULT_PORT = 8346
MAGIC = "DSTM"
VERSION = "1"
MAX_DATAGRAM = 96
REPLIES_PER_SECOND = 20


def encode_probe(device_id: str | None = None) -> bytes:
    text = f"{MAGIC} probe {VERSION}"
    if device_id:
        text += f" id={device_id}"
    return text.encode("ascii")


def is_probe(data: bytes) -> bool:
    if not data or len(data) > MAX_DATAGRAM:
        return False
    try:
        words = data.decode("ascii").split(" ")
    except UnicodeDecodeError:
        return False
    return len(words) >= 3 and words[0] == MAGIC and words[1] == "probe"


@dataclass(frozen=True)
class Answer:
    name: str
    port: int
    pairing: bool


def encode_answer(answer: Answer) -> bytes:
    name = "".join(c for c in answer.name if c.isalnum() or c in "-_")[:20] or "dsterm"
    return (f"{MAGIC} here {VERSION} name={name} port={answer.port} "
            f"pair={1 if answer.pairing else 0}").encode("ascii")


def decode_answer(data: bytes) -> Answer | None:
    """The card's parser, mirrored for tests and tools. None for anything
    that is not a well-formed answer."""
    if len(data) > MAX_DATAGRAM:
        return None
    try:
        words = data.decode("ascii").split(" ")
    except UnicodeDecodeError:
        return None
    if len(words) < 3 or words[0] != MAGIC or words[1] != "here":
        return None
    fields = dict(w.split("=", 1) for w in words[3:] if "=" in w)
    try:
        port = int(fields.get("port", ""))
    except ValueError:
        return None
    if not 1 <= port <= 65535 or not fields.get("name"):
        return None
    return Answer(fields["name"], port, fields.get("pair") == "1")


class Responder:
    """The server's half. Non-blocking; the server's selector calls
    readable() when a datagram is waiting."""

    def __init__(self, answer: Callable[[], Answer], host: str = "0.0.0.0",
                 port: int = DEFAULT_PORT) -> None:
        self._answer = answer
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.setblocking(False)
        self._window_start = time.monotonic()
        self._sent_in_window = 0
        self.answered = 0
        self.ignored = 0

    @property
    def port(self) -> int:
        return self.sock.getsockname()[1]

    def fileno(self) -> int:
        return self.sock.fileno()

    def readable(self) -> None:
        while True:
            try:
                data, addr = self.sock.recvfrom(MAX_DATAGRAM + 1)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            if not is_probe(data) or not self._allow():
                self.ignored += 1
                continue
            try:
                self.sock.sendto(encode_answer(self._answer()), addr)
                self.answered += 1
            except OSError:
                pass

    def _allow(self) -> bool:
        now = time.monotonic()
        if now - self._window_start >= 1.0:
            self._window_start = now
            self._sent_in_window = 0
        if self._sent_in_window >= REPLIES_PER_SECOND:
            return False
        self._sent_in_window += 1
        return True

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
