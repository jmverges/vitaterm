"""Answers: an Enter from the card reaches a permission prompt only if it was
meant for that prompt (server_agents.py, "questions"; docs/PROTOCOL.md,
"Answers")."""

from __future__ import annotations

import time

import pytest

from dsterm import server_agents
from dsterm.protocol import (
    ASK_FLAG_DROPPED,
    HEADER_SIZE,
    STATE_IDLE,
    STATE_NEEDS_APPROVAL,
    STATE_WORKING,
    Message,
    MsgType,
    ProtocolError,
    decode_answer,
    decode_state,
    decode_state_ask,
    encode_answer,
    encode_state,
)

# The module, not its classes: imported by name, pytest would run them twice.
import test_remote


class FakeSession:
    def __init__(self):
        self.written: list[bytes] = []

    def write(self, keys: bytes) -> bool:
        self.written.append(keys)
        return True

    def invalidate_mode(self) -> None:
        pass


@pytest.fixture
def armed(monkeypatch):
    """An "ask" card, a session, and a reading that says a prompt is waiting."""
    server, client, peer, tmux = test_remote.TestStateServer()._armed(monkeypatch)
    client.caps.ask = True
    server._session = FakeSession()
    sent: list[bytes] = []
    server._send_to = lambda _c, data: sent.append(data)  # type: ignore[method-assign]
    server._resync = lambda *a, **k: None                 # type: ignore[method-assign]
    try:
        yield server, client, sent
    finally:
        server._session = None
        test_remote._close(server, client, peer)


def _states(sent):
    return [decode_state_ask(d[HEADER_SIZE:]) for d in sent if d[3] == MsgType.STATE]


def _answer(server, client, ask_id, keys=b"\r"):
    server._handle(client, Message(MsgType.ANSWER, encode_answer(ask_id, keys)[HEADER_SIZE:]))


class TestWire:
    def test_state_round_trips_with_and_without_the_tail(self):
        plain = encode_state(STATE_IDLE, "")[HEADER_SIZE:]
        assert decode_state_ask(plain) == (STATE_IDLE, "", None)
        asked = encode_state(STATE_NEEDS_APPROVAL, "permission prompt",
                             ask=(0xDEADBEEF, ASK_FLAG_DROPPED))[HEADER_SIZE:]
        assert decode_state_ask(asked) == (STATE_NEEDS_APPROVAL, "permission prompt",
                                           (0xDEADBEEF, ASK_FLAG_DROPPED))
        # The two-value decode an older caller uses still reads both.
        assert decode_state(asked) == (STATE_NEEDS_APPROVAL, "permission prompt")

    @pytest.mark.parametrize("tail", [b"\x00", b"\x00\x00\x00\x00", b"\x00" * 6])
    def test_a_tail_of_any_other_length_is_refused(self, tail):
        with pytest.raises(ProtocolError):
            decode_state_ask(bytes((STATE_IDLE, 0)) + tail)

    def test_answer_round_trips_and_is_bounded_like_key(self):
        assert decode_answer(encode_answer(7, b"\r")[HEADER_SIZE:]) == (7, b"\r")
        for bad in (b"", b"\x00\x00\x00\x07", b"\x00\x00\x00\x07" + b"x" * 65):
            with pytest.raises(ProtocolError):
                decode_answer(bad)
        with pytest.raises(ProtocolError):
            encode_answer(1 << 32, b"\r")


class TestQuestions:
    def test_the_card_hears_the_question_and_its_answer_goes_through(self, armed):
        server, client, sent = armed
        server._advertise_state(client)
        [(state, _, (ask_id, flags))] = _states(sent)
        assert state == STATE_NEEDS_APPROVAL and flags == 0
        _answer(server, client, ask_id)
        assert server._session.written == [b"\r"]
        # Answered: the card hears that Claude is on with it, at once.
        server._advertise_state(client)
        assert _states(sent)[-1][0] == STATE_WORKING

    def test_a_second_press_does_not_answer_the_next_prompt(self, armed):
        server, client, sent = armed
        server._advertise_state(client)
        ask_id = _states(sent)[0][2][0]
        _answer(server, client, ask_id)
        # The next prompt: a reading taken after the answer says approval again.
        server._agents._latest = test_remote.TestStateMapping()._observation()
        server._refresh_state()
        assert server._state_reading[0] == STATE_NEEDS_APPROVAL
        _answer(server, client, ask_id)                  # a late or double press
        assert server._session.written == [b"\r"]
        server._advertise_state(client)
        state, _, (new_id, flags) = _states(sent)[-1]
        assert new_id != ask_id and flags & ASK_FLAG_DROPPED
        _answer(server, client, new_id)                  # looked, pressed again
        assert server._session.written == [b"\r", b"\r"]

    def test_a_double_press_that_arrives_as_a_plain_enter_is_held(self, armed):
        # The card heard "working" between the presses, so the second is a
        # plain KEY; no reading has followed the answer, so the next prompt
        # may be up already with no question open for it.
        server, client, sent = armed
        server._advertise_state(client)
        _answer(server, client, _states(sent)[0][2][0])
        server._handle(client, Message(MsgType.KEY, b"\r"))
        assert server._session.written == [b"\r"]
        assert client.agents.ask_dropped
        # A reading taken since the answer settles it: Enter types again.
        server._agents._latest = test_remote.TestStateMapping()._observation()
        server._refresh_state()
        assert not server._ask_settling and server._ask_open

    def test_settling_ends_by_itself(self, armed, monkeypatch):
        server, client, sent = armed
        server._advertise_state(client)
        _answer(server, client, _states(sent)[0][2][0])
        server._ask_closed_at -= server_agents.ASK_SETTLE_SECONDS + 1
        server._handle(client, Message(MsgType.KEY, b"\r"))
        assert server._session.written == [b"\r", b"\r"]

    def test_a_reading_from_before_the_answer_does_not_reopen_it(self, armed):
        server, client, sent = armed
        old = server._agents._latest                    # taken before the answer
        server._advertise_state(client)
        _answer(server, client, _states(sent)[0][2][0])
        server._agents._latest = old
        server._refresh_state()
        assert server._state_reading == (STATE_WORKING, "")
        assert not server._ask_open

    def test_a_plain_enter_sent_before_the_card_saw_the_question_is_held(self, armed):
        server, client, sent = armed
        server._handle(client, Message(MsgType.KEY, b"\r"))
        assert server._session.written == []
        server._advertise_state(client)
        assert _states(sent)[-1][2][1] & ASK_FLAG_DROPPED
        # Not Escape, not the arrows, not ordinary typing.
        for keys in (b"\x1b[B", b"x", b"\x1b"):
            server._handle(client, Message(MsgType.KEY, keys))
        assert server._session.written == [b"\x1b[B", b"x", b"\x1b"]
        assert not server._ask_open                     # Escape declined it

    def test_a_card_without_ask_types_as_it_always_has(self, armed):
        server, client, sent = armed
        client.caps.ask = False
        server._handle(client, Message(MsgType.KEY, b"\r"))
        assert server._session.written == [b"\r"]
        server._advertise_state(client)
        assert _states(sent)[-1][2] is None             # no tail for it

    def test_answer_from_a_card_that_did_not_announce_ask_drops_it(self, armed):
        server, client, _ = armed
        client.caps.ask = False
        _answer(server, client, server._ask_id)
        assert client not in server._clients
        assert server._session.written == []

    def test_an_answer_to_a_question_that_is_not_open_is_dropped(self, armed):
        server, client, _ = armed
        _answer(server, client, server._ask_id + 1)
        assert server._session.written == []
        assert client.agents.ask_dropped and client in server._clients

    def test_the_screen_changing_under_it_is_a_new_question(self, armed):
        server, client, sent = armed
        server._advertise_state(client)
        ask_id = _states(sent)[0][2][0]
        now = time.monotonic()
        a, b = ("a",) * 3, ("b",) * 3
        server._ask_tick(a, client, now)
        server._ask_tick(a, client, now + 1)
        assert server._ask_id == ask_id                 # nothing moved
        # Answered at the computer, and Claude has asked something else.
        server._ask_tick(b, client, now + 2)
        assert server._ask_id != ask_id
        _answer(server, client, ask_id)
        assert server._session.written == []

    def test_the_cards_own_keys_moving_the_highlight_are_not(self, armed):
        server, client, sent = armed
        server._advertise_state(client)
        ask_id = _states(sent)[0][2][0]
        now = time.monotonic()
        server._ask_tick(("a",), client, now)
        server._handle(client, Message(MsgType.KEY, b"\x1b[B"))
        server._ask_tick(("b",), client, client.last_key_at + 0.1)
        assert server._ask_id == ask_id
        _answer(server, client, ask_id)
        assert server._session.written == [b"\x1b[B", b"\r"]

    def test_another_window_is_another_question(self, armed):
        server, client, sent = armed
        ask_id = server._ask_id
        assert server._switch_to_window(client, "@0") is None
        assert server._ask_id != ask_id

    def test_answers(self):
        for keys in (b"\r", b"\n", b"text\r"):
            assert server_agents.accepts(keys) and server_agents.answers(keys)
        assert server_agents.answers(b"\x1b") and not server_agents.accepts(b"\x1b")
        for keys in (b"\x1b[A", b"\x1b[B", b"y", b"\t"):
            assert not server_agents.answers(keys)
