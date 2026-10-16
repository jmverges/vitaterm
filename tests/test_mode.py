"""The MODE message and the HELLO capability channel it rides on.

The thing being protected here is a physical button. On the alternate screen
there is no scrollback to move through, so the handheld's scroll control has to
send page keys to the application instead of asking the server for a view. The
card cannot work out which regime it is in, and a stale answer does not read as
a stale readout -- it reads as a button that does the wrong thing.

The other half is the compatibility gate. An unknown message type is fatal in
both directions: the card halts on one, and this server raises inside the frame
reader and drops the connection, which a reconnecting card turns into a loop. So
MODE goes only to a client that asked for it in its HELLO, and a card built
before any of this existed must be provably unaffected.
"""

import shutil
import subprocess
import time

import pytest

from dsterm.client import Client
from dsterm.grid import COLS, ROWS
from dsterm.protocol import (
    FEATURE_MODE,
    HEADER_SIZE,
    MODE_APP_SCROLLS,
    MsgType,
    ProtocolError,
    decode_mode,
    encode_hello,
    encode_mode,
    hello_features,
)
from dsterm.session import Session

from dsterm.serve import tmux_command
from test_server import ServerHarness

ALT_ON = b"\x1b[?1049h"
ALT_OFF = b"\x1b[?1049l"

PLAIN_CHILD = ["bash", "--noprofile", "--norc", "-c",
               "for i in $(seq 1 90); do echo mode-line-$i; done; sleep 20"]

# NEVER the default socket. tmux_command() defaults to the one the shipped
# service uses, so a test that takes that default is creating and killing
# sessions on the live server -- which is exactly how a running Claude Code
# session was destroyed while this file was being written. app_scrolls reads the
# socket back out of argv, so pointing the tests elsewhere needs nothing else.
TEST_SOCKET = "dsterm-test"


def wait_for(predicate, client: Client, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client.poll(0.05)
        if predicate():
            return True
    return False


class TestTheBytes:
    def test_exact_frame_for_alt_screen(self):
        # Written out in full because the card decodes these by hand and the
        # client half was built against this literal before either side landed.
        assert encode_mode(True) == bytes.fromhex("445301960001") + b"\x01"

    def test_reserved_bits_are_sent_as_zero(self):
        # The card masks bit 0 and ignores the rest, so claiming bit 1 later is
        # not a flag day -- but only if we never put anything up there by
        # accident in the meantime.
        assert encode_mode(False)[HEADER_SIZE:] == b"\x00"
        assert encode_mode(True)[HEADER_SIZE:] == b"\x01"

    def test_payload_is_one_byte(self):
        assert len(encode_mode(True)) == HEADER_SIZE + 1

    @pytest.mark.parametrize("payload", [b"", b"\x01\x00", b"\x00" * 4])
    def test_decode_rejects_wrong_length(self, payload):
        with pytest.raises(ProtocolError):
            decode_mode(payload)

    def test_decode_returns_raw_flags_so_the_mask_is_the_readers_job(self):
        assert decode_mode(b"\x01") == 0x01
        assert decode_mode(b"\xff") == 0xFF
        assert decode_mode(b"\xff") & MODE_APP_SCROLLS


class TestHelloIsACapabilityChannel:
    def test_old_hello_announces_nothing(self):
        # What every card in existence sends today.
        assert hello_features(b"ds-term/1") == frozenset()

    def test_feature_tokens_are_parsed(self):
        assert FEATURE_MODE in hello_features(b"ds-term/1 mode")

    def test_unknown_tokens_are_ignored_not_rejected(self):
        # The whole point: neither side needs the other to be current, so a
        # token from a newer card must be inert rather than fatal.
        feats = hello_features(b"ds-term/1 mode telepathy 9000")
        assert FEATURE_MODE in feats
        assert "telepathy" in feats  # ignored by the server, but not an error

    @pytest.mark.parametrize("payload", [b"", b"\xff\xfe", b"   ", b"ds-term/1 \x00"])
    def test_malformed_hello_never_raises(self, payload):
        # This arrives before authentication. If a bad hello could raise, an
        # unauthenticated peer could drop the connection with six bytes.
        assert isinstance(hello_features(payload), frozenset)

    def test_encode_hello_round_trips(self):
        payload = encode_hello((FEATURE_MODE,))[HEADER_SIZE:]
        assert payload.startswith(b"ds-term/1")
        assert hello_features(payload) == frozenset({FEATURE_MODE})


class TestSessionDetectsTheAlternateScreen:
    def test_flag_follows_the_child(self):
        # Against a real PTY, not a hand-fed emulator: the mode has to survive
        # the actual read path.
        with Session(["bash", "--noprofile", "--norc", "-i"]) as s:
            for _ in range(40):
                s.pump()
                time.sleep(0.02)
            assert s.alt_screen is False

            s.write(b"printf '\\033[?1049h'\n")
            assert wait_for_session(s, lambda: s.alt_screen is True)

            s.write(b"printf '\\033[?1049l'\n")
            assert wait_for_session(s, lambda: s.alt_screen is False)


def wait_for_session(s: Session, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s.pump()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def drain_until(s: Session, predicate, timeout: float) -> bool:
    """Tight drain, deliberately: the Server pumps on selector readability
    (server.py) rather than on a timer, and how fast the PTY is emptied has been
    measured to change what tmux emits. A probe that sleeps between reads is
    measuring the probe."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s.pump()
        s.snapshot()
        if predicate():
            return True
    return False


class TestTheFlagFollowsTheInnerApplication:
    """The case that killed two cheaper derivations, pinned so it stays dead.

    A shell inside tmux scrolls and accumulates history; the same session then
    launches something that takes the alternate screen. The flag must flip while
    the history already gathered stays exactly where it is -- that history
    belongs to the shell, and scrolling into it while an application is on screen
    would show the user the wrong transcript.

    Neither candidate derivation can do this. `multiplexed` is constant across
    both halves. The pyte alt bit reports the OUTER terminal, which under tmux is
    tmux's own alternate screen: measured True under tmux 3.7b and False under
    3.4 for identical children, so it is wrong in both directions rather than
    merely incomplete. Only asking the pane distinguishes them, which is why
    app_scrolls shells out.
    """

    @pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
    def test_flag_flips_when_the_inner_app_takes_the_screen(self):
        name = "mode-mixed"
        argv = tmux_command(name, ["bash", "--noprofile", "--norc", "-i"], COLS, ROWS, socket=TEST_SOCKET)
        try:
            with Session(argv) as s:
                # A shell, scrolling normally. Whatever history this yields
                # belongs to the shell, and is what must not move later.
                assert drain_until(s, lambda: s.app_scrolls is False, 15), (
                    "a bare shell under tmux must not claim to own scrolling"
                )
                s.write(b"for i in $(seq 1 90); do echo mixed-line-$i; done\n")
                drain_until(s, lambda: False, 3)
                shell_history = s.scrollback_lines

                # Now something that takes the alternate screen, in the same
                # session, with no change to the argv the server was handed.
                s.write(b"printf '\\033[?1049h'; sleep 30\n")
                assert drain_until(s, lambda: s.app_scrolls is True, 15), (
                    "the flag never noticed the inner application"
                )
                # Through tmux this is the pane's history_size, which keeps
                # the shell's lines and may grow by the command line itself.
                # What must never happen is the history the shell built being
                # thrown away.
                assert s.scrollback_lines >= shell_history, (
                    "the shell's history was lost when the application started"
                )
        finally:
            subprocess.run(["tmux", "-L", TEST_SOCKET, "kill-session", "-t", name],
                           capture_output=True)

    def test_an_unwrapped_child_needs_no_tmux(self):
        # The non-multiplexed branch reads the emulator directly, so a server run
        # without --tmux keeps working on a box with no tmux installed at all.
        with Session(PLAIN_CHILD) as s:
            assert drain_until(s, lambda: s.scrollback_lines > ROWS, 10)
            assert s.app_scrolls is False


class TestServerSendsMode:
    def test_a_client_that_did_not_ask_is_never_sent_one(self):
        # The safety property. A card built before MODE existed halts on an
        # unknown type and can only be recovered by reflashing from another
        # machine, so this is the test that matters most in the file.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key) as c:   # no features
                c.poll(0.5)
                c.send_keys(b"printf '\\033[?1049h'\n")
                time.sleep(1.0)
                c.poll(0.5)
                assert c.mode_flags is None
                assert c.app_scrolls is None

    def test_sent_after_auth_even_when_false(self):
        # "Not on the alternate screen" and "the server has not said yet" need
        # different behaviour on the card, so the first send is unconditional.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key, features=(FEATURE_MODE,)) as c:
                assert wait_for(lambda: c.mode_flags is not None, c)
                assert c.app_scrolls is False

    def test_follows_the_child_in_both_directions(self):
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key, features=(FEATURE_MODE,)) as c:
                assert wait_for(lambda: c.mode_flags is not None, c)

                c.send_keys(b"printf '\\033[?1049h'\n")
                assert wait_for(lambda: c.app_scrolls is True, c), "never entered"

                c.send_keys(b"printf '\\033[?1049l'\n")
                assert wait_for(lambda: c.app_scrolls is False, c), "never left"

    def test_not_repeated_while_the_regime_holds(self):
        # It is not rate-limited, deliberately -- so it has to be edge-driven
        # instead, or it is a message per tick forever.
        sent = []

        with ServerHarness() as h:
            real = h.server._send_to

            def spy(client, data):
                if len(data) > 3 and data[3] == MsgType.MODE:
                    sent.append(data)
                real(client, data)

            h.server._send_to = spy
            with Client("127.0.0.1", h.port, h.key, features=(FEATURE_MODE,)) as c:
                assert wait_for(lambda: c.mode_flags is not None, c)
                after_first = len(sent)

                c.send_keys(b"echo still-on-the-ordinary-screen\n")
                time.sleep(1.0)
                c.poll(0.3)

        assert after_first == 1, "the first MODE should be exactly one message"
        assert len(sent) == after_first, "kept restating an unchanged regime"

    def test_reconnecting_client_is_told_again(self):
        # mode_flags lives on ClientState, so it resets with the connection. A
        # reconnecting card has forgotten, and must not be left guessing.
        with ServerHarness() as h:
            with Client("127.0.0.1", h.port, h.key, features=(FEATURE_MODE,)) as c:
                assert wait_for(lambda: c.mode_flags is not None, c)

            with Client("127.0.0.1", h.port, h.key, features=(FEATURE_MODE,)) as c2:
                assert wait_for(lambda: c2.mode_flags is not None, c2)

    def test_stays_under_the_mtu_bound(self):
        # Every server->client message has to clear 536 bytes on real traffic.
        assert len(encode_mode(True)) < 536
