"""`dsterm screen`: a look at the screen that does not take the DS's place."""

from __future__ import annotations

import socket

from dsterm import peek
from dsterm.grid import COLS

from test_dashboard import Harness


def closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_reads_the_screen_from_the_dashboard():
    with Harness() as h:
        text = peek.from_dashboard(h.base + "/api/state")
    assert text is not None
    lines = text.splitlines()
    assert "(from the dashboard)" in lines[1]
    assert lines[-1] == "handheld: none connected"
    # Every screen row is framed to the grid's width, never wider.
    body = [line for line in lines if line.startswith("| ")]
    assert body and all(len(line) == COLS + 4 for line in body)


def test_no_dashboard_is_none_not_an_error():
    assert peek.from_dashboard(f"http://127.0.0.1:{closed_port()}/api/state",
                               timeout=0.5) is None


def test_another_computer_is_never_read_from_this_ones_dashboard(monkeypatch):
    calls = []
    monkeypatch.setattr(peek, "from_dashboard", lambda *a, **k: calls.append("dash"))
    monkeypatch.setattr(peek, "as_client",
                        lambda host, port, keys: calls.append((host, port)) or 0)
    assert peek.main(["192.0.2.10"]) == 0
    assert calls == [("192.0.2.10", 8347)]


def test_typing_keys_needs_a_connection_even_locally(monkeypatch):
    calls = []
    monkeypatch.setattr(peek, "from_dashboard", lambda *a, **k: calls.append("dash"))
    monkeypatch.setattr(peek, "as_client",
                        lambda host, port, keys: calls.append(keys) or 0)
    assert peek.main(["127.0.0.1", "8347", "ls\\r"]) == 0
    assert calls == ["ls\\r"]


def test_falls_back_to_connecting_when_no_dashboard_answers(monkeypatch):
    calls = []
    monkeypatch.setattr(peek, "from_dashboard", lambda *a, **k: None)
    monkeypatch.setattr(peek, "as_client",
                        lambda host, port, keys: calls.append(host) or 0)
    assert peek.main([]) == 0
    assert calls == ["127.0.0.1"]
