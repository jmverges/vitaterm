"""UDP discovery: a real socket, a real probe, and everything it must ignore."""

from __future__ import annotations

import socket
import time

import pytest

from dsterm import discovery


@pytest.fixture
def responder():
    r = discovery.Responder(lambda: discovery.Answer("host-1", 8347, False),
                            host="127.0.0.1", port=0)
    yield r
    r.close()


def _ask(r: discovery.Responder, payload: bytes, wait: float = 0.3) -> list[bytes]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.05)
    try:
        s.sendto(payload, ("127.0.0.1", r.port))
        got, deadline = [], time.monotonic() + wait
        while time.monotonic() < deadline:
            r.readable()
            try:
                got.append(s.recvfrom(256)[0])
            except socket.timeout:
                pass
        return got
    finally:
        s.close()


class TestTheExchange:
    def test_a_probe_gets_one_answer(self, responder):
        [reply] = _ask(responder, discovery.encode_probe("0a1b2c3d"))
        assert discovery.decode_answer(reply) == discovery.Answer("host-1", 8347, False)

    def test_the_answer_is_short_ascii_with_nothing_secret(self, responder):
        [reply] = _ask(responder, discovery.encode_probe())
        assert reply == b"DSTM here 1 name=host-1 port=8347 pair=0"

    @pytest.mark.parametrize("junk", [
        b"DSTM", b"DSTM here 1 name=x port=1 pair=1", b"hello",
        b"DSTM probe 1" + b" " * 200, bytes(range(256))[:90],
    ])
    def test_anything_but_a_probe_is_ignored(self, responder, junk):
        assert _ask(responder, junk) == []

    def test_answers_are_rate_limited(self, responder):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.05)
        try:
            for _ in range(discovery.REPLIES_PER_SECOND * 3):
                s.sendto(discovery.encode_probe(), ("127.0.0.1", responder.port))
            time.sleep(0.1)
            responder.readable()
            assert responder.answered == discovery.REPLIES_PER_SECOND
            assert responder.ignored == discovery.REPLIES_PER_SECOND * 2
        finally:
            s.close()


class TestTheAnswerFormat:
    def test_a_hostile_name_is_reduced_to_safe_characters(self):
        raw = discovery.encode_answer(discovery.Answer("evil name=x\nport=1", 8347, True))
        assert discovery.decode_answer(raw) == discovery.Answer("evilnamexport1", 8347, True)

    @pytest.mark.parametrize("bad", [
        b"DSTM here 1 name=x port=0 pair=0",
        b"DSTM here 1 name=x port=99999 pair=0",
        b"DSTM here 1 port=8347 pair=0",
        b"DSTX here 1 name=x port=8347 pair=0",
        b"DSTM here 1 name=x port=abc pair=0",
    ])
    def test_malformed_answers_decode_to_none(self, bad):
        assert discovery.decode_answer(bad) is None
