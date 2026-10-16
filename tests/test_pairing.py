"""Pairing: the construction, the window, the device keys, and the wire.

The vectors below are RFC 7748 and RFC 5869's own, checked against OpenSSL
when they were written. The card runs the same functions in C, and
tools/crosscheck.py holds the two to each other; these hold the Python half
to the standards.

What is tested beyond "it pairs" is the security argument: both sides show
the same code only when nobody is in the middle, a server that does not keep
its commitment is refused, a "no" at either end saves nothing and closes the
window, a closed window refuses, a low-order key is refused, a pairing
connection never reaches the PTY, and the legacy key keeps working.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import time

import pytest

from dsterm import pairing, x25519
from dsterm.client import AuthError, Client, PairError, pair
from dsterm.protocol import (
    FrameReader,
    MsgType,
    encode,
    encode_hello,
    encode_pair_hello,
    hello_device_id,
)

from test_server import ServerHarness

H = bytes.fromhex


# -- the primitives ----------------------------------------------------------


class TestX25519:
    def test_rfc7748_scalar_multiplication(self):
        assert x25519.x25519(
            H("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4"),
            H("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c"),
        ) == H("c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552")

    def test_rfc7748_diffie_hellman(self):
        alice = H("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
        bob = H("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
        assert x25519.public_key(alice) == H(
            "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
        assert x25519.public_key(bob) == H(
            "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f")
        shared = H("4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742")
        assert x25519.x25519(alice, x25519.public_key(bob)) == shared
        assert x25519.x25519(bob, x25519.public_key(alice)) == shared

    def test_a_low_order_point_gives_zero(self):
        # The point this module's callers refuse. u = 0 is order 1.
        assert x25519.x25519(os.urandom(32), bytes(32)) == bytes(32)


class TestHkdf:
    def test_rfc5869_case_1(self):
        assert pairing.hkdf_sha256(bytes(range(13)), b"\x0b" * 22,
                                   bytes(range(0xF0, 0xFA)), 42) == H(
            "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
            "34007208d5b887185865")


# -- the construction ----------------------------------------------------------


def _exchange(device_id=None, card_nonce=None):
    """One honest exchange, both halves: (card view, server half)."""
    card_private = os.urandom(32)
    card_public = x25519.public_key(card_private)
    device_id = device_id or os.urandom(4)
    server = pairing.server_respond(device_id, card_public)
    card_nonce = card_nonce or os.urandom(16)
    pairing.server_reveal(server, card_nonce)
    shared = x25519.x25519(card_private, server.server_public)
    confirm, key = pairing.derive(shared, card_nonce, server.server_nonce, device_id,
                                  card_public, server.server_public)
    card_code = pairing.code(card_public, server.server_public, card_nonce,
                             server.server_nonce)
    return (confirm, key, card_code, card_public), server


class TestDerivation:
    def test_both_sides_derive_the_same_keys_and_code(self):
        (confirm, key, card_code, _), server = _exchange()
        assert confirm == server.confirm_key and key == server.device_key
        assert len(key) == 16
        assert card_code == server.code and len(card_code) == 6 and card_code.isdigit()

    def test_the_commitment_binds_the_servers_nonce(self):
        (_, _, _, card_public), server = _exchange()
        assert pairing.commitment(card_public, server.server_public,
                                  server.server_nonce) == server.commit
        assert pairing.commitment(card_public, server.server_public,
                                  os.urandom(16)) != server.commit

    def test_a_man_in_the_middle_gets_two_different_codes(self):
        # The attacker runs one exchange with the card (as a server) and one
        # with the real server (as a card), with its own keys in each.  It
        # commits to its nonce towards the card before it sees the card's,
        # and must send a nonce to the server before the server reveals its
        # own -- so it cannot steer either code onto the other.
        card_private = os.urandom(32)
        card_public = x25519.public_key(card_private)
        device_id = os.urandom(4)
        towards_card = pairing.server_respond(device_id, card_public)      # fake server
        fake_card_public = x25519.public_key(os.urandom(32))
        real_server = pairing.server_respond(device_id, fake_card_public)
        card_nonce = os.urandom(16)
        pairing.server_reveal(towards_card, card_nonce)
        pairing.server_reveal(real_server, card_nonce)   # its best move: relay the nonce
        card_code = pairing.code(card_public, towards_card.server_public, card_nonce,
                                 towards_card.server_nonce)
        assert card_code != real_server.code

    def test_the_two_acceptances_differ(self):
        # Otherwise a server could echo the card's own MAC back as its own.
        k, a, b = os.urandom(32), os.urandom(32), os.urandom(32)
        assert (pairing.accept_mac(k, pairing.ROLE_CARD, a, b) !=
                pairing.accept_mac(k, pairing.ROLE_SERVER, a, b))

    def test_a_low_order_card_key_is_refused(self):
        with pytest.raises(pairing.PairingError):
            pairing.server_respond(os.urandom(4), bytes(32))

    def test_nonces_must_be_sixteen_bytes(self):
        with pytest.raises(pairing.PairingError):
            pairing.derive(os.urandom(32), os.urandom(15), os.urandom(16), os.urandom(4),
                           os.urandom(32), os.urandom(32))


# -- the window and the device keys ---------------------------------------------


class TestWindow:
    def test_open_then_current(self, tmp_path):
        w = pairing.open_window(tmp_path)
        assert pairing.current_window(tmp_path).expires == w.expires
        assert w.code == ""

    def test_the_file_is_private(self, tmp_path):
        pairing.open_window(tmp_path)
        assert stat.S_IMODE(os.stat(tmp_path / pairing.WINDOW_FILE).st_mode) == 0o600

    def test_a_readable_window_file_is_treated_as_closed(self, tmp_path):
        pairing.open_window(tmp_path)
        os.chmod(tmp_path / pairing.WINDOW_FILE, 0o644)
        assert pairing.current_window(tmp_path) is None

    def test_it_expires(self, tmp_path):
        pairing.open_window(tmp_path, seconds=-1)
        assert pairing.current_window(tmp_path) is None

    def test_the_code_is_shown_and_an_ending_closes_it(self, tmp_path):
        pairing.open_window(tmp_path)
        assert pairing.show_code("123456", "0a1b2c3d", tmp_path)
        assert pairing.current_window(tmp_path).code == "123456"
        pairing.end_window("the console left", tmp_path)
        assert pairing.current_window(tmp_path) is None
        assert pairing.read_window(tmp_path).ended == "the console left"

    def test_an_answer_names_its_code(self, tmp_path):
        pairing.open_window(tmp_path)
        pairing.write_answer("123456", True, tmp_path)
        assert pairing.read_answer(tmp_path) == ("123456", True)
        assert stat.S_IMODE(os.stat(tmp_path / pairing.ANSWER_FILE).st_mode) == 0o600
        pairing.open_window(tmp_path)           # a new window forgets old answers
        assert pairing.read_answer(tmp_path) is None

    def test_a_malformed_file_is_closed_not_open(self, tmp_path):
        path = tmp_path / pairing.WINDOW_FILE
        tmp_path.mkdir(exist_ok=True)
        path.write_text("{not json")
        os.chmod(path, 0o600)
        assert pairing.current_window(tmp_path) is None


class TestDevices:
    def test_save_load_list_forget(self, tmp_path):
        key = os.urandom(16)
        pairing.save_device("0a1b2c3d", key, address="192.0.2.5", config_dir=tmp_path)
        assert pairing.load_device_key("0a1b2c3d", tmp_path) == key
        [d] = pairing.list_devices(tmp_path)
        assert d.device_id == "0a1b2c3d" and d.address == "192.0.2.5"
        assert pairing.forget_device("0a1b2c3d", tmp_path)
        assert pairing.load_device_key("0a1b2c3d", tmp_path) is None

    def test_key_files_and_directory_are_private(self, tmp_path):
        pairing.save_device("0a1b2c3d", os.urandom(16), config_dir=tmp_path)
        d = tmp_path / pairing.DEVICES_DIR
        assert not d.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        assert not (d / "0a1b2c3d.psk").stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)

    def test_a_readable_device_key_is_refused(self, tmp_path):
        pairing.save_device("0a1b2c3d", os.urandom(16), config_dir=tmp_path)
        os.chmod(tmp_path / pairing.DEVICES_DIR / "0a1b2c3d.psk", 0o644)
        with pytest.raises(PermissionError):
            pairing.load_device_key("0a1b2c3d", tmp_path)

    def test_ids_are_validated_before_touching_a_path(self, tmp_path):
        for bad in ("../../etc", "0A1B2C3D", "0a1b2c3", "zzzzzzzz"):
            assert pairing.load_device_key(bad, tmp_path) is None
            assert not pairing.forget_device(bad, tmp_path)
            with pytest.raises(ValueError):
                pairing.save_device(bad, os.urandom(16), config_dir=tmp_path)

    def test_the_hello_token(self):
        assert hello_device_id(frozenset({"mode", "id=0a1b2c3d"})) == "0a1b2c3d"
        assert hello_device_id(frozenset({"id=../x", "id="})) is None


# -- over the wire ---------------------------------------------------------------


@pytest.fixture
def harness(tmp_path):
    with ServerHarness(config_dir=tmp_path) as h:
        h.config_dir = tmp_path
        yield h


def _person_at_the_computer(config_dir, says=True):
    """What `dsterm pair` does: read the code the server shows, answer."""
    def accept(card_code):
        window = pairing.read_window(config_dir)
        pairing.write_answer(window.code, says and window.code == card_code, config_dir)
        return True     # the person at the DS says yes
    return accept


class TestPairingOverTheWire:
    def test_a_card_pairs_and_then_authenticates_with_its_own_key(self, harness):
        pairing.open_window(harness.config_dir)
        key = pair("127.0.0.1", harness.port, "0a1b2c3d",
                   accept=_person_at_the_computer(harness.config_dir))
        assert pairing.load_device_key("0a1b2c3d", harness.config_dir) == key
        assert pairing.current_window(harness.config_dir) is None, "one use"
        assert harness.wait_for_event("paired as device 0a1b2c3d")
        with Client("127.0.0.1", harness.port, key, device_id="0a1b2c3d") as c:
            c.poll(0.2)

    def test_the_legacy_key_still_works_with_and_without_an_id(self, harness):
        with Client("127.0.0.1", harness.port, harness.key) as c:
            c.poll(0.1)
        with Client("127.0.0.1", harness.port, harness.key, device_id="99999999") as c:
            c.poll(0.1)

    def test_another_devices_key_does_not_open_this_one(self, harness):
        pairing.save_device("0a1b2c3d", os.urandom(16), config_dir=harness.config_dir)
        with pytest.raises(AuthError):
            Client("127.0.0.1", harness.port, os.urandom(16),
                   device_id="0a1b2c3d").connect()

    def test_a_no_at_the_computer_saves_nothing_and_closes(self, harness):
        pairing.open_window(harness.config_dir)
        with pytest.raises(PairError, match="did not match"):
            pair("127.0.0.1", harness.port, "0a1b2c3d",
                 accept=_person_at_the_computer(harness.config_dir, says=False))
        assert pairing.load_device_key("0a1b2c3d", harness.config_dir) is None
        assert pairing.current_window(harness.config_dir) is None

    def test_a_no_at_the_ds_saves_nothing_and_closes(self, harness):
        pairing.open_window(harness.config_dir)
        with pytest.raises(PairError, match="did not match"):
            pair("127.0.0.1", harness.port, "0a1b2c3d", accept=lambda code: False)
        assert harness.wait_for_event("dropped")
        assert pairing.load_device_key("0a1b2c3d", harness.config_dir) is None
        assert pairing.current_window(harness.config_dir) is None
        assert "left" in pairing.read_window(harness.config_dir).ended

    def test_no_window_no_pairing(self, harness):
        with pytest.raises(PairError, match="not open"):
            pair("127.0.0.1", harness.port, "0a1b2c3d")

    def test_a_server_that_breaks_its_commitment_is_refused(self, harness, monkeypatch):
        # The server's nonce is swapped after it committed: the card must
        # refuse before it computes a code, let alone shows one.
        real_reveal = pairing.server_reveal

        def cheating(half, card_nonce):
            real_reveal(half, card_nonce)
            half.server_nonce = os.urandom(16)
        monkeypatch.setattr(pairing, "server_reveal", cheating)
        pairing.open_window(harness.config_dir)
        with pytest.raises(PairError, match="commitment"):
            pair("127.0.0.1", harness.port, "0a1b2c3d",
                 accept=_person_at_the_computer(harness.config_dir))

    def test_a_pairing_connection_never_goes_on_to_authenticate(self, harness):
        # The card closes the connection after PAIR_DONE and authenticates on
        # a new one. A peer that instead carries on -- here a HELLO, which is
        # allowed before authentication anywhere else -- is dropped.
        def carry_on(sock):
            sock.sendall(encode_hello(("seal",)))
            sock.settimeout(5)
            try:
                while sock.recv(4096):
                    pass
            except OSError:
                pass

        pairing.open_window(harness.config_dir)
        pair("127.0.0.1", harness.port, "0a1b2c3d",
             accept=_person_at_the_computer(harness.config_dir), then=carry_on)
        assert harness.wait_for_event("HELLO after pairing")

    def test_a_pairing_connection_cannot_reach_the_pty(self, harness):
        # Start pairing, then on the same unauthenticated connection try to
        # type. The server must drop it: pairing grants a key file, never the
        # session.
        pairing.open_window(harness.config_dir)
        sock = socket.create_connection(("127.0.0.1", harness.port), timeout=5)
        try:
            card_public = x25519.public_key(os.urandom(32))
            sock.sendall(encode_pair_hello(bytes(4), card_public))
            sock.sendall(encode(MsgType.KEY, b"echo PWNED\r"))
            reader, seen = FrameReader(), []
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                data = sock.recv(4096)
                if not data:
                    break
                seen.extend(m.type for m in reader.feed(data))
        finally:
            sock.close()
        assert MsgType.PAIR_REPLY in seen
        assert harness.wait_for_event("KEY before authentication")

    def test_one_console_at_a_time(self, harness):
        pairing.open_window(harness.config_dir)
        first = socket.create_connection(("127.0.0.1", harness.port), timeout=5)
        try:
            first.sendall(encode_pair_hello(bytes(4), x25519.public_key(os.urandom(32))))
            assert harness.wait_for_event("pairing as device")
            with pytest.raises(PairError, match="another console"):
                pair("127.0.0.1", harness.port, "0a1b2c3d")
        finally:
            first.close()

    def test_discovery_reports_the_window(self, tmp_path):
        from dsterm import discovery

        with ServerHarness(config_dir=tmp_path, discovery_port=0) as h:
            ans = h.server._discovery_answer()
            assert ans.pairing is False and ans.port == h.port
            pairing.open_window(tmp_path)
            assert h.server._discovery_answer().pairing is True
            assert discovery.decode_answer(discovery.encode_answer(ans)) == ans
