"""The dashboard: what it shows, and what it must never be.

Two of these tests are about the shape of the data. The other two are the ones
that matter: the page must not be reachable from the network, and it must not be
able to type. Either would defeat the pairing key -- an unauthenticated way to
read the screen, or an unauthenticated way to drive the shell.
"""

import json
import urllib.error
import urllib.request

import pytest

from dsterm import dashboard
from dsterm.auth import generate_key
from dsterm.grid import COLS, ROWS
from dsterm.server import Server


class Harness:
    """A started server with a dashboard on an ephemeral loopback port."""

    def __enter__(self):
        self.server = Server(
            ["bash", "--noprofile", "--norc", "-i"],
            generate_key(),
            host="127.0.0.1",
            port=0,
            on_event=lambda _: None,
        )
        self.server.start()
        self.httpd = dashboard.start(self.server, 0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.server.stop()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, r.read()

    def state(self):
        return json.loads(self.get("/api/state")[1])


class TestNotReachableFromTheNetwork:
    def test_binding_a_routable_address_is_refused(self):
        # The page shows the screen with no authentication at all. Served on the
        # LAN it would be a strictly easier way in than the protocol the pairing
        # key exists to protect.
        server = Server(["true"], generate_key(), host="127.0.0.1", port=0,
                        on_event=lambda _: None)
        with pytest.raises(dashboard.NotLoopback):
            dashboard.start(server, 0, host="0.0.0.0")

    def test_binding_a_lan_address_is_refused(self):
        server = Server(["true"], generate_key(), host="127.0.0.1", port=0,
                        on_event=lambda _: None)
        with pytest.raises(dashboard.NotLoopback):
            dashboard.start(server, 0, host="192.0.2.10")

    def test_loopback_is_allowed(self):
        with Harness() as h:
            assert h.get("/")[0] == 200


class TestCannotType:
    def test_no_handler_writes_to_the_pty(self):
        # Enumerated rather than assumed: adding an endpoint that sends keys
        # would create a client that skipped the handshake, so a future one has
        # to break this test deliberately rather than by forgetting.
        text = open(dashboard.__file__).read()
        assert "session.write" not in text
        assert "_session.write" not in text

    def test_the_page_only_talks_to_its_own_origin(self):
        # The UI file is the part most likely to be edited casually, including
        # by an agent. It may fetch its own state and nothing else -- an
        # absolute URL would mean the screen contents leaving this machine.
        page = dashboard.PAGE_PATH.read_text()
        for needle in ("http://", "https://", "//cdn", "src="):
            assert needle not in page, f"the page must not reference {needle!r}"

    def test_unknown_paths_are_refused(self):
        with Harness() as h:
            with pytest.raises(urllib.error.HTTPError) as exc:
                h.get("/api/keys")
            assert exc.value.code == 404


class TestOtherPagesInTheSameBrowser:
    """A browser on this machine is on loopback too. A web page it opens must
    not be able to read the screen or restart the child."""

    def _request(self, h, path, method="GET", headers=None):
        req = urllib.request.Request(h.base + path, method=method,
                                     headers=headers or {}, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_a_rebound_domain_is_answered_with_nothing(self):
        # DNS rebinding: evil.example now resolves to 127.0.0.1, so the
        # browser connects here -- but still says which site it thinks it is.
        with Harness() as h:
            assert self._request(h, "/api/state", headers={"Host": "evil.example"}) == 403
            assert self._request(h, "/", headers={"Host": "evil.example:8348"}) == 403
            assert self._request(h, "/api/state") == 200

    def test_a_restart_without_the_pages_header_is_refused(self):
        # What a cross-site form or a plain fetch can send: no custom header.
        with Harness() as h:
            assert self._request(h, "/api/restart-child", "POST") == 403
            assert not h.server._restart_requested.is_set()

    def test_the_restart_is_done_by_the_servers_own_loop(self):
        with Harness() as h:
            before = h.server._session
            status = self._request(h, "/api/restart-child", "POST",
                                   headers={dashboard.RESTART_HEADER: "1"})
            assert status == 202
            # Asked for, not done: this thread never touches the session.
            assert h.server._session is before
            assert h.server._restart_requested.is_set()
            h.server.serve_forever(max_seconds=0.3)
            assert h.server._session is not before
            assert not h.server._restart_requested.is_set()

    @pytest.mark.parametrize("host,ok", [
        ("127.0.0.1:8348", True), ("localhost", True), ("[::1]:8348", True),
        ("LOCALHOST:1", True), ("evil.example", False), ("127.0.0.1.evil.example", False),
        ("", False), (None, False),
    ])
    def test_host_is_loopback(self, host, ok):
        assert dashboard.host_is_loopback(host) is ok


class TestThePageIsAFile:
    """The UI lives in dashboard.html so it can be edited on its own."""

    def test_the_page_file_exists_and_is_served(self):
        assert dashboard.PAGE_PATH.exists()
        with Harness() as h:
            status, body = h.get("/")
            assert status == 200
            assert b"DS Claude Terminal" in body

    def test_edits_are_picked_up_without_a_restart(self):
        # The reason it is read per request rather than at import. Without this
        # every UI tweak costs a service restart and a child respawn.
        original = dashboard.PAGE_PATH.read_bytes()
        try:
            with Harness() as h:
                dashboard.PAGE_PATH.write_bytes(original + b"<!-- edited -->")
                assert b"<!-- edited -->" in h.get("/")[1]
        finally:
            dashboard.PAGE_PATH.write_bytes(original)

class TestState:
    def test_reports_the_listening_address_and_a_live_child(self):
        with Harness() as h:
            s = h.state()
            assert s["listening"].startswith("127.0.0.1:")
            assert s["child"]["alive"] is True

    def test_screen_is_the_full_grid(self):
        with Harness() as h:
            screen = h.state()["screen"]
            assert len(screen) == ROWS
            assert all(len(row) == COLS for row in screen)
            assert set(screen[0][0]) == {"ch", "fg", "bg", "b", "r"}

    def test_state_publishes_the_geometry_it_drew(self):
        """The page sizes its font from these numbers rather than its own copy.

        A second hardcoded 42x24 in the JavaScript would draw the wrong shape
        the first time the handheld's font changes, and would do it silently --
        the cells still arrive, so the only symptom is a clipped or undersized
        terminal that looks like a CSS problem.
        """
        with Harness() as h:
            s = h.state()
            assert s["geometry"] == {"cols": COLS, "rows": ROWS}
            # The advertised shape must match the screen actually sent.
            assert len(s["screen"]) == s["geometry"]["rows"]
            assert all(len(row) == s["geometry"]["cols"] for row in s["screen"])

    def test_counters_move_when_a_client_connects(self):
        import time

        from dsterm.client import Client

        with Harness() as h:
            before = h.state()["counters"]
            assert before["connects"] == 0

            import threading
            t = threading.Thread(
                target=h.server.serve_forever, kwargs={"max_seconds": 8},
                daemon=True)
            t.start()

            with Client("127.0.0.1", h.server.bound_port, h.server.key) as c:
                c.poll(0.5)
                after = h.state()["counters"]
                assert after["connects"] == 1
                assert after["auth_ok"] == 1
                assert after["bytes_out"] > 0
                assert after["bytes_in"] > 0

            h.server.stop()
            t.join(timeout=5)

    def test_events_are_bounded(self):
        from dsterm.server import EVENT_HISTORY

        with Harness() as h:
            for i in range(EVENT_HISTORY + 50):
                h.server._emit(f"filler {i}")
            assert len(h.server.events) == EVENT_HISTORY
            # The page shows a window of the most recent, not everything.
            assert len(h.state()["events"]) <= 60

    def test_snapshot_exposes_scrollback_line_count(self):
        with Harness() as h:
            state = h.state()
            assert state["child"]["scrollback_lines"] == 0
