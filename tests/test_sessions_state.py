"""Unit-level invariants for session switching's tmux identity and snapshot state machine."""

from __future__ import annotations

import socket
import subprocess

from dsterm import sessions
from dsterm.auth import generate_key
from dsterm.grid import Grid
from dsterm.protocol import (
    HEADER_SIZE,
    SESSION_NEW_OUTPUT,
    MsgType,
    decode_session,
)
from dsterm.connection import AdvertisedSession, ClientState
from dsterm.server import Server


def _epoch(pid: int = 100, created: int = 200) -> sessions.TmuxEpoch:
    return sessions.TmuxEpoch(pid, created, "$0")


def _snapshot(epoch, *rows):
    """rows are (window_id, index, name, active, activity_marker)."""
    return sessions.WindowSnapshot(
        epoch,
        tuple(sessions.Window(*row) for row in rows),
    )


def _server_client():
    left, right = socket.socketpair()
    left.setblocking(False)
    right.setblocking(False)
    server = Server(["true"], generate_key(), host="127.0.0.1", port=0)
    client = ClientState(left, ("test", 1), reader=None, nonce=b"n", authed=True,
                         mirror=Grid.blank())
    client.caps.mode = True
    # FrameReader is unused by these direct state-machine tests.
    server._clients.append(client)
    server._tmux_target = lambda: ("scratch", "m6")  # type: ignore[method-assign]
    return server, client, right


def _close(server, client, peer):
    try:
        client.sock.close()
    finally:
        peer.close()
        server._sel.close()


def _rows_from(messages):
    return [decode_session(message[HEADER_SIZE:]) for message in messages
            if message[3] == MsgType.SESSIONS]


def test_discovery_keeps_window_id_and_epoch_separate_from_display_index(monkeypatch):
    epoch = _epoch()
    snapshot = _snapshot(epoch, ("@42", 7, "shell", True, 11))
    monkeypatch.setattr(sessions, "list_windows", lambda *_: snapshot)
    server, client, peer = _server_client()
    try:
        got = server._observe_windows()
        assert got == snapshot
        assert got.windows[0].window_id == "@42"
        assert got.windows[0].index == 7
        assert got.epoch == epoch
        batch = server._snapshot_batch(client, got)
        assert batch.rows[0].display_index == 7
    finally:
        _close(server, client, peer)


def test_tmux_discovery_parses_epoch_and_all_live_row_fields(monkeypatch):
    monkeypatch.setattr(
        sessions,
        "_run",
        lambda _args: subprocess.CompletedProcess(
            _args, 0,
            "991\t1234\t$5\t@42\t7\t1\t5678\tclau\tde\n"
            "991\t1234\t$5\t@43\t9\t0\t5679\tshell\n",
            "",
        ),
    )
    got = sessions.list_windows("scratch", "m6")
    assert got is not None
    assert got.epoch == sessions.TmuxEpoch(991, 1234, "$5")
    assert [(w.window_id, w.index, w.name, w.active, w.activity)
            for w in got.windows] == [
                ("@42", 7, "claude", True, 5678),
                ("@43", 9, "shell", False, 5679),
            ]


def test_new_output_is_revision_based_and_clears_when_viewed(monkeypatch):
    epoch = _epoch()
    states = [
        _snapshot(epoch,
                  ("@0", 0, "front", True, 10),
                  ("@1", 1, "back", False, 10)),
    ]
    monkeypatch.setattr(sessions, "list_windows", lambda *_: states[0])
    server, client, peer = _server_client()
    try:
        first = server._observe_windows()
        first_rows = _rows_from(server._snapshot_batch(client, first).messages)
        assert not first_rows[1][2] & SESSION_NEW_OUTPUT

        # tmux reports new background activity: one observed revision.
        states[0] = _snapshot(epoch,
                              ("@0", 0, "front", True, 10),
                              ("@1", 1, "back", False, 11))
        second_rows = _rows_from(server._snapshot_batch(client, server._observe_windows()).messages)
        assert second_rows[1][2] & SESSION_NEW_OUTPUT

        # Viewing the target records its current revision and clears the bit.
        states[0] = _snapshot(epoch,
                              ("@0", 0, "front", False, 10),
                              ("@1", 1, "back", True, 11))
        viewed_rows = _rows_from(server._snapshot_batch(client, server._observe_windows()).messages)
        assert not viewed_rows[1][2] & SESSION_NEW_OUTPUT

        states[0] = _snapshot(epoch,
                              ("@0", 0, "front", True, 10),
                              ("@1", 1, "back", False, 11))
        quiet_rows = _rows_from(server._snapshot_batch(client, server._observe_windows()).messages)
        assert not quiet_rows[1][2] & SESSION_NEW_OUTPUT

        states[0] = _snapshot(epoch,
                              ("@0", 0, "front", True, 10),
                              ("@1", 1, "back", False, 12))
        later_rows = _rows_from(server._snapshot_batch(client, server._observe_windows()).messages)
        assert later_rows[1][2] & SESSION_NEW_OUTPUT
    finally:
        _close(server, client, peer)


def test_active_pty_output_advances_viewed_revision(monkeypatch):
    epoch = _epoch()
    state = _snapshot(epoch,
                      ("@0", 0, "front", True, 10),
                      ("@1", 1, "back", False, 10))
    monkeypatch.setattr(sessions, "list_windows", lambda *_: state)
    server, client, peer = _server_client()
    try:
        server._observe_windows()
        key = (epoch, "@0")
        server._note_active_pty_output(9)
        assert server._window_output[key].revision == 1
        assert client.sessions.seen[key] == 1
    finally:
        _close(server, client, peer)


def test_idle_age_uses_observed_monotonic_time(monkeypatch):
    epoch = _epoch()
    state = _snapshot(epoch, ("@0", 0, "front", True, 10))
    monkeypatch.setattr(sessions, "list_windows", lambda *_: state)
    monkeypatch.setattr("dsterm.server.time.monotonic", lambda: 100.9)
    server, client, peer = _server_client()
    try:
        server._observe_windows()
        server._window_output[(epoch, "@0")].last_output_monotonic = 90.0
        row = _rows_from(server._snapshot_batch(client, state).messages)[0]
        assert row[3] == 10
        assert sessions.idle_seconds(90.0, now=90.0 + 999_999) == 0xFFFF
    finally:
        _close(server, client, peer)


def test_a_ticking_idle_age_alone_publishes_nothing(monkeypatch):
    """Polling must not turn SESSIONS into a permanent 1 Hz stream.

    The handheld stages every entry of every snapshot it is sent. A snapshot
    per poll is a message class it receives forever rather than a handful of
    times, and the flashed card data aborts a few seconds into that stream.
    The contract is after-auth, after-switch, on-change -- so an age that has
    merely advanced is not a reason to publish.
    """
    epoch = _epoch()
    state = _snapshot(epoch, ("@0", 0, "front", True, 10))
    clock = [100.0]
    monkeypatch.setattr(sessions, "list_windows", lambda *_: state)
    monkeypatch.setattr("dsterm.server.time.monotonic", lambda: clock[0])
    server, client, peer = _server_client()
    sent = []
    server._send_to = lambda _client, data: sent.append(data)  # type: ignore[method-assign]
    try:
        snapshot = server._observe_windows()
        # After authentication: forced, and therefore published.
        server._advertise_sessions(client, force=True, snapshot=snapshot)
        server._flush_outbox()
        assert len(_rows_from(sent)) == 1

        # Thirty polls later, with nothing but the clock moving, still one.
        for _ in range(30):
            clock[0] += 1.0
            server._advertise_sessions(client, snapshot=snapshot)
            server._flush_outbox()
        assert len(_rows_from(sent)) == 1

        # A real change is still published immediately, and carries the age
        # that had accrued by then rather than a frozen one.
        renamed = _snapshot(epoch, ("@0", 0, "back", True, 10))
        server._advertise_sessions(client, snapshot=renamed)
        server._flush_outbox()
        rows = _rows_from(sent)
        assert len(rows) == 2
        assert rows[1][4] == "back"
        assert rows[1][3] > rows[0][3]
    finally:
        _close(server, client, peer)


def test_snapshots_are_serialized_and_mapping_promotes_only_after_completion(monkeypatch):
    epoch = _epoch()
    first = _snapshot(epoch,
                      ("@0", 0, "one", True, 10),
                      ("@1", 1, "two", False, 10))
    second = _snapshot(epoch,
                       ("@0", 0, "one", True, 10),
                       ("@2", 2, "three", False, 10))
    monkeypatch.setattr(sessions, "list_windows", lambda *_: first)
    monkeypatch.setattr("dsterm.server.MESSAGES_PER_TICK", 1)
    server, client, peer = _server_client()
    sent = []
    server._send_to = lambda _client, data: sent.append(data)  # type: ignore[method-assign]
    try:
        server._observe_windows()
        server._advertise_sessions(client, force=True, snapshot=first)
        server._advertise_sessions(client, force=True, snapshot=second)

        server._flush_outbox()
        assert client.sessions.rows == []  # first of [0, 1] is not complete
        server._flush_outbox()
        assert [row.window_id for row in client.sessions.rows] == ["@0", "@1"]
        server._flush_outbox()
        assert [row.window_id for row in client.sessions.rows] == ["@0", "@1"]
        server._flush_outbox()
        assert [row.window_id for row in client.sessions.rows] == ["@0", "@2"]

        decoded = _rows_from(sent)
        assert [(r[0], r[1], r[4]) for r in decoded] == [
            (2, 0, "one"), (2, 1, "two"),
            (2, 0, "one"), (2, 1, "three"),
        ]
    finally:
        _close(server, client, peer)


def test_mapping_waits_for_a_partially_sent_final_snapshot_frame(monkeypatch):
    epoch = _epoch()
    state = _snapshot(epoch,
                      ("@0", 0, "one", True, 10),
                      ("@1", 1, "two", False, 10))
    monkeypatch.setattr(sessions, "list_windows", lambda *_: state)
    monkeypatch.setattr("dsterm.server.MESSAGES_PER_TICK", 1)
    server, client, peer = _server_client()
    sent = []

    def partially_send(_client, data):
        sent.append(data)
        if len(sent) == 2:
            # Model a non-blocking socket accepting only part of the final
            # entry. _flush_outbox must retain the previous row map until the
            # pending tail is pushed on the next tick.
            client.pending.extend(b"tail")

    server._send_to = partially_send  # type: ignore[method-assign]
    try:
        snapshot = server._observe_windows()
        server._advertise_sessions(client, snapshot=snapshot)
        server._flush_outbox()
        server._flush_outbox()
        assert client.sessions.rows == []
        server._flush_outbox()
        assert [row.window_id for row in client.sessions.rows] == ["@0", "@1"]
    finally:
        _close(server, client, peer)


def test_epoch_change_invalidates_same_window_id_before_it_can_be_reused(monkeypatch):
    old_epoch = _epoch(100, 200)
    new_epoch = _epoch(101, 201)
    states = [_snapshot(old_epoch, ("@0", 0, "old", True, 10))]
    monkeypatch.setattr(sessions, "list_windows", lambda *_: states[0])
    server, client, peer = _server_client()
    try:
        server._observe_windows()
        client.sessions.rows = [AdvertisedSession(old_epoch, "@0", 0)]
        client.sessions.seen[(old_epoch, "@0")] = 7
        # An already-queued advertisement from the old tmux lifetime must not
        # survive to promote its now-reused @window ID.
        client.outbox.append(server._snapshot_batch(client, states[0]))

        states[0] = _snapshot(new_epoch, ("@0", 0, "new", True, 10))
        server._observe_windows()
        assert client.sessions.rows == []
        assert client.sessions.seen == {}
        assert not client.outbox
        assert set(server._window_output) == {(new_epoch, "@0")}
    finally:
        _close(server, client, peer)


def test_each_client_row_map_resolves_its_own_window_identity(monkeypatch):
    epoch = _epoch()
    current = _snapshot(epoch,
                        ("@0", 0, "first", True, 10),
                        ("@1", 1, "second", False, 10))
    monkeypatch.setattr(sessions, "list_windows", lambda *_: current)
    selected = []
    monkeypatch.setattr(sessions, "select", lambda socket_name, target, window_id:
                        selected.append((socket_name, target, window_id)) or True)
    server, first, peer = _server_client()
    second_sock, second_peer = socket.socketpair()
    second_sock.setblocking(False)
    second = ClientState(second_sock, ("second", 2), reader=None, nonce=b"n",
                         authed=True, mirror=Grid.blank())
    server._clients.append(second)
    try:
        first.sessions.rows = [AdvertisedSession(epoch, "@0", 0)]
        second.sessions.rows = [AdvertisedSession(epoch, "@1", 1)]
        server._handle_session_pick(second, 0)
        assert selected == [("scratch", "m6", "@1")]
        assert first.sessions.rows[0].window_id == "@0"
        assert next(w.window_id for w in server._last_windows.windows if w.active) == "@1"
        server._note_active_pty_output(1)
        assert server._window_output[(epoch, "@1")].revision == 1
        assert server._window_output[(epoch, "@0")].revision == 0
        assert second.sessions.seen[(epoch, "@1")] == 1
    finally:
        second_sock.close()
        second_peer.close()
        _close(server, first, peer)


def test_stale_or_dead_row_is_refused_without_selecting_a_neighbour(monkeypatch):
    epoch = _epoch()
    current = _snapshot(epoch, ("@2", 2, "replacement", True, 10))
    monkeypatch.setattr(sessions, "list_windows", lambda *_: current)
    selected = []
    monkeypatch.setattr(sessions, "select", lambda *_: selected.append(_) or True)
    server, client, peer = _server_client()
    sent = []
    server._send_to = lambda _client, data: sent.append(data)  # type: ignore[method-assign]
    try:
        client.sessions.rows = [AdvertisedSession(epoch, "@1", 1)]
        server._handle_session_pick(client, 0)
        assert selected == []
        assert sent and sent[-1][3] == MsgType.ERROR
        assert not client.sessions.switch_pending
    finally:
        _close(server, client, peer)


def test_selection_uses_window_id_and_not_display_index(monkeypatch):
    epoch = _epoch()
    current = _snapshot(epoch, ("@42", 7, "target", True, 10))
    monkeypatch.setattr(sessions, "list_windows", lambda *_: current)
    selected = []
    monkeypatch.setattr(sessions, "select", lambda socket_name, target, window_id:
                        selected.append((socket_name, target, window_id)) or True)
    server, client, peer = _server_client()
    try:
        client.sessions.rows = [AdvertisedSession(epoch, "@42", 7)]
        client.scroll.offset = 12
        server._handle_session_pick(client, 0)
        assert selected == [("scratch", "m6", "@42")]
        assert client.scroll.offset == 0
        assert client.caps.view_pos and client.caps.mode
        assert client.sessions.switch_pending
    finally:
        _close(server, client, peer)
