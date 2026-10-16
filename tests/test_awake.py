"""Keep-awake: held while a handheld is connected, let go when the last leaves."""

from __future__ import annotations

import sys
import time

from dsterm import awake
from dsterm.client import Client

from test_server import ServerHarness


class FakeProc:
    def __init__(self):
        self.ended = False

    def poll(self):
        return 0 if self.ended else None

    def terminate(self):
        self.ended = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.ended = True


class Spawner:
    def __init__(self):
        self.started: list[list[str]] = []
        self.procs: list[FakeProc] = []

    def __call__(self, cmd, **_kw):
        self.started.append(cmd)
        self.procs.append(FakeProc())
        return self.procs[-1]


def test_hold_starts_once_and_release_stops_it():
    spawn, events = Spawner(), []
    k = awake.KeepAwake(command=["inhibit"], spawn=spawn, on_event=events.append)
    k.hold()
    k.hold()
    assert spawn.started == [["inhibit"]] and k.holding
    k.release()
    assert spawn.procs[0].ended and not k.holding
    k.release()                                    # a second release is a no-op
    assert sum("released" in e for e in events) == 1


def test_disabled_never_starts_anything():
    spawn = Spawner()
    k = awake.KeepAwake(enabled=False, command=["inhibit"], spawn=spawn)
    k.hold()
    assert spawn.started == []


def test_nowhere_to_hold_says_so_once_and_carries_on():
    events = []
    k = awake.KeepAwake(command=[], spawn=Spawner(), on_event=events.append)
    k.hold()
    k.hold()
    assert not k.holding
    assert len(events) == 1 and "not available" in events[0]


def test_a_command_that_will_not_start_is_a_log_line():
    def broken(*_a, **_kw):
        raise OSError("no such file")
    events = []
    k = awake.KeepAwake(command=["inhibit"], spawn=broken, on_event=events.append)
    k.hold()
    assert not k.holding and "could not start" in events[0]


def test_the_command_ends_with_this_process():
    cmd = awake.inhibit_command(pid=4242)
    if sys.platform == "darwin":
        assert cmd == ["caffeinate", "-i", "-w", "4242"]
    elif cmd is not None:
        assert cmd[0] == "systemd-inhibit" and "--pid=4242" in cmd


def test_the_server_holds_while_a_card_is_connected():
    spawn = Spawner()
    k = awake.KeepAwake(command=["inhibit"], spawn=spawn)
    with ServerHarness(keep_awake=k) as h:
        assert not k.holding
        with Client("127.0.0.1", h.port, h.key):
            assert h.wait_for_event("authenticated")
            assert k.holding
        assert h.wait_for_event("dropped")
        deadline = time.monotonic() + 3
        while k.holding and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not k.holding, "the last card left and the machine is still held"
    assert len(spawn.started) == 1
