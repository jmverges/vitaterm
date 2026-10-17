"""The answer rule under random interleavings (server_agents.py, "questions").

A model of what is really on the computer's screen -- which prompt, if
any -- runs beside the real server code, and a model card that hears the
STATEs the server sends and presses keys the way the DS does. Prompts come
and go, readings land late, someone answers at the computer, the card
presses Enter, Escape and the arrows, and the clock moves. Whenever an Enter
reaches the session, it must have been meant for the prompt it lands on.

DSTERM_ANSWER_RUNS sets the number of runs (default 200, for CI); a soak
sets it in the thousands.
"""

from __future__ import annotations

import os
import random

import pytest

from dsterm import remote, server_agents
from dsterm.protocol import (
    HEADER_SIZE,
    STATE_NEEDS_APPROVAL,
    Message,
    MsgType,
    decode_state_ask,
    encode_answer,
)

import test_remote

RUNS = int(os.environ.get("DSTERM_ANSWER_RUNS", "200"))
STEPS = 120


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Session:
    """The terminal: Enter answers whatever prompt is up."""

    def __init__(self, world):
        self.world = world

    def write(self, keys: bytes) -> bool:
        self.world.typed(keys)
        return True

    def invalidate_mode(self) -> None:
        pass


class World:
    def __init__(self, rng, server, client, clock):
        self.rng, self.server, self.client, self.clock = rng, server, client, clock
        self.prompt: int | None = None       # the prompt really on screen
        self.next_prompt = 1
        self.frame = 0                       # bumps whenever the screen changes
        # The card: the last STATE it heard, and which prompt was on screen
        # when the server sent it (what the person was looking at).
        self.card_state = None
        self.card_ask = None
        self.card_belief = None
        self.in_flight: tuple | None = None  # what the card is sending now
        # For the plain-Enter rule: when the last answer reached the session,
        # whether a reading has landed since, and whether the server has had
        # a reading while the current prompt was up.
        self.answered_at = None
        self.read_since_answer = True
        self.prompt_read = False
        self.prompt_since = None
        # The ids under which the computer changed what was asked within
        # ASK_ECHO_SECONDS of an arrow from the card: the server takes that
        # change for the key's own echo, so that one id may name more than
        # one prompt (docs/PROTOCOL.md, "Answers": the documented limit).
        self.masked: set[int] = set()

    # -- the computer's side ---------------------------------------------

    def screen(self):
        return (self.prompt, self.frame)

    def echo_masks(self) -> bool:
        last = self.server._ask_arrow_at
        return last is not None and self.clock.now - last <= server_agents.ASK_ECHO_SECONDS

    def computer_changes(self):
        if self.server._ask_open and self.echo_masks():
            self.masked.add(self.server._ask_id)

    def show_prompt(self):
        self.computer_changes()
        self.prompt = self.next_prompt
        self.next_prompt += 1
        self.frame += 1
        self.prompt_read = False
        self.prompt_since = self.clock.now

    def answer_at_computer(self):
        self.computer_changes()
        if self.prompt is not None:
            self.prompt = None
            self.frame += 1

    def reading(self):
        approval = self.prompt is not None
        runner = test_remote.FakeRunner(
            [test_remote._ia(test_remote.SID_A,
                             "waiting" if approval else "busy", pid=500,
                             **({"waitingFor": "permission prompt"} if approval else {}))],
            panes="77 301 @4\n", ps="301 77\n400 301\n500 400\n")
        obs = remote.observe(("scratch", "rm"), runner=runner,
                             which=lambda: test_remote.CLAUDE, now=self.clock.now)
        self.server._agents._latest = obs
        self.server._refresh_state()
        self.read_since_answer = True
        if self.prompt is not None:
            self.prompt_read = True

    def tick(self):
        self.server._ask_tick(self.screen(), self.client, self.clock.now)

    # -- the card's side -------------------------------------------------

    def hear_state(self):
        sent = []
        self.server._send_to = lambda _c, data: sent.append(data)
        self.server._advertise_state(self.client)
        for d in sent:
            state, _, ask = decode_state_ask(d[HEADER_SIZE:])
            self.card_state = state
            self.card_ask = ask[0] if ask else None
            self.card_belief = self.prompt if state == STATE_NEEDS_APPROVAL else None

    def press(self, keys: bytes):
        self.client.last_key_at = self.clock.now
        if (self.card_state == STATE_NEEDS_APPROVAL and self.card_ask is not None
                and b"\r" in keys):
            self.in_flight = ("answer", self.card_belief, self.card_ask)
            payload = encode_answer(self.card_ask, keys)[HEADER_SIZE:]
            self.server._handle(self.client, Message(MsgType.ANSWER, payload))
        else:
            self.in_flight = ("plain", None, None)
            self.server._handle(self.client, Message(MsgType.KEY, keys))
        self.in_flight = None

    def typed(self, keys: bytes):
        """The session received bytes from the card: check, then act."""
        if b"\r" in keys and self.prompt is not None:
            kind, belief, ask_id = self.in_flight
            if kind == "answer":
                assert belief == self.prompt or ask_id in self.masked, (
                    f"an Enter meant for prompt {belief} answered prompt {self.prompt}")
            else:
                assert not self.prompt_read, (
                    f"a plain Enter answered prompt {self.prompt}, which the "
                    "server had already read")
                fresh = (self.answered_at is not None and not self.read_since_answer
                         and self.prompt_since >= self.answered_at
                         and self.clock.now - self.answered_at
                         <= server_agents.ASK_SETTLE_SECONDS)
                assert not fresh, (
                    f"a plain Enter answered prompt {self.prompt}, which came up "
                    "right after the last answer")
            self.prompt = None
            self.frame += 1
            self.answered_at = self.clock.now
            self.read_since_answer = False
        elif keys == b"\x1b" and self.prompt is not None:
            self.prompt = None
            self.frame += 1
        elif keys.startswith(b"\x1b["):
            self.frame += 1                  # the highlight moves


@pytest.mark.parametrize("seed", range(RUNS))
def test_an_enter_only_ever_answers_the_prompt_it_was_meant_for(seed, monkeypatch):
    rng = random.Random(seed)
    clock = Clock()
    monkeypatch.setattr(server_agents.time, "monotonic", clock)
    server, client, peer, _ = test_remote.TestStateServer()._armed(monkeypatch)
    try:
        client.caps.ask = True
        server._resync = lambda *a, **k: None
        world = World(rng, server, client, clock)
        server._session = Session(world)
        server._agents._latest = None
        server._ask_open = False
        server._state_reading = (0, "")
        world.reading()
        actions = [
            (world.show_prompt, 3), (world.answer_at_computer, 2),
            (world.reading, 4), (world.hear_state, 5), (world.tick, 5),
            (lambda: world.press(b"\r"), 5), (lambda: world.press(b"\x1b"), 1),
            (lambda: world.press(b"\x1b[B"), 2), (lambda: world.press(b"x"), 1),
        ]
        funcs = [f for f, w in actions for _ in range(w)]
        for _ in range(STEPS):
            clock.now += rng.choice((0.0, 0.05, 0.2, 0.6, 1.5, 4.0))
            rng.choice(funcs)()
            # The server's loop looks at the screen many times a second, so
            # it has always seen the latest one before the next event.
            clock.now += 0.02
            world.tick()
    finally:
        server._session = None
        test_remote._close(server, client, peer)
