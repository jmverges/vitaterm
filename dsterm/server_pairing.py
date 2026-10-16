"""Pairing a card, and choosing the key a connection's AUTH is checked
against.

Part of the server, split out by feature: the methods below are
mixed into Server (server.py) and share its state through `self`.
"""

from __future__ import annotations

import hmac

from . import discovery
from . import pairing
from .protocol import (
    MsgType,
    ProtocolError,
    encode,
    decode_pair_hello,
    decode_pair_mac,
    decode_pair_nonce,
    encode_pair_reply,
)
from .connection import ClientState


class PairingMixin:
    """Owns the pairing exchange on a connection (ClientState.pair, .paired) and
    the per-device key lookup; the window itself is pairing.py's."""

    # -- keys and pairing ----------------------------------------------------

    def _key_for(self, c: ClientState) -> bytes:
        """The key this connection's AUTH must prove: the device's own if it
        announced an id that has been paired, otherwise the legacy key every
        card used before pairing existed."""
        if c.device_id:
            try:
                key = pairing.load_device_key(c.device_id, self.config_dir)
            except (PermissionError, ValueError) as exc:
                self._emit(f"device {c.device_id}: key unusable ({exc}); "
                           "checking the legacy key instead")
                key = None
            if key is not None:
                return key
        return self.key

    def _discovery_answer(self) -> discovery.Answer:
        return discovery.Answer(pairing.server_name(), self.bound_port,
                                pairing.current_window(self.config_dir) is not None)

    def _pair_fail(self, c: ClientState, reason: str, log_why: str) -> None:
        if c.pair is not None:
            # Any attempt that ends ends the window: a mismatch may be an
            # attack, and the next try should be a deliberate `dsterm pair`.
            pairing.end_window(reason, self.config_dir)
            c.pair = None
        self._send_to(c, encode(MsgType.PAIR_FAIL, reason.encode("ascii")[:64]))
        self._drop(c, log_why)

    def _pair_hello(self, c: ClientState, payload: bytes) -> None:
        if c.authed or c.pair is not None:
            self._drop(c, "PAIR_HELLO out of turn")
            return
        try:
            device_id, card_public = decode_pair_hello(payload)
        except ProtocolError as exc:
            self._drop(c, f"bad pair hello: {exc}")
            return
        window = pairing.current_window(self.config_dir)
        if window is None:
            self._send_to(c, encode(MsgType.PAIR_FAIL, b"pairing is not open: run dsterm pair"))
            self._drop(c, "pair attempt with no window open")
            return
        if window.code or any(o.pair is not None for o in self._clients):
            # One card at a time: a second would mean two codes for one
            # person to tell apart.
            self._send_to(c, encode(MsgType.PAIR_FAIL, b"another console is pairing"))
            self._drop(c, "pair attempt while another is in progress")
            return
        try:
            c.pair = pairing.server_respond(device_id, card_public)
        except pairing.PairingError as exc:
            self._send_to(c, encode(MsgType.PAIR_FAIL, str(exc).encode("ascii")[:64]))
            self._drop(c, f"pair refused: {exc}")
            return
        self._send_to(c, encode_pair_reply(c.pair.server_public, c.pair.commit))
        self._emit(f"client {c.addr} pairing as device {c.pair.device_id}")

    def _pair_confirm(self, c: ClientState, payload: bytes) -> None:
        half = c.pair
        if c.authed or half is None or half.code:
            self._drop(c, "PAIR_CONFIRM out of turn")
            return
        try:
            card_nonce = decode_pair_nonce(payload)
            pairing.server_reveal(half, card_nonce)
        except (ProtocolError, pairing.PairingError) as exc:
            self._pair_fail(c, "pairing failed", f"bad pair confirm: {exc}")
            return
        # The window is checked again: it may have expired between this
        # card's HELLO and its CONFIRM.
        if not pairing.show_code(half.code, half.device_id, self.config_dir):
            self._pair_fail(c, "pairing window closed: run dsterm pair again",
                            "pair confirm after the window closed")
            return
        self._send_to(c, encode(MsgType.PAIR_OK, half.server_nonce))
        self._emit(f"client {c.addr} comparing a pairing code")

    def _pair_accept(self, c: ClientState, payload: bytes) -> None:
        half = c.pair
        if c.authed or half is None or not half.code or half.card_accepted:
            self._drop(c, "PAIR_ACCEPT out of turn")
            return
        try:
            presented = decode_pair_mac(payload)
        except ProtocolError as exc:
            self._pair_fail(c, "pairing failed", f"bad pair accept: {exc}")
            return
        expected = pairing.accept_mac(half.confirm_key, pairing.ROLE_CARD,
                                      half.card_public, half.server_public)
        if not hmac.compare_digest(expected, presented):
            self._pair_fail(c, "pairing failed: the keys did not match",
                            "pair accept with a wrong MAC")
            return
        half.card_accepted = True
        self._poll_pairing()

    def _poll_pairing(self) -> None:
        """Once a tick, and when the card accepts: has the person at the
        computer answered?  Both yeses save the device; a no ends it."""
        for c in list(self._clients):
            half = c.pair
            if half is None or not half.code:
                continue
            answer = pairing.read_answer(self.config_dir)
            if answer is None or answer[0] != half.code:
                continue
            if not answer[1]:
                self._pair_fail(c, "the computer said the codes did not match",
                                "pair rejected at the computer")
                continue
            if not half.card_accepted:
                continue
            pairing.save_device(half.device_id, half.device_key,
                                address=str(c.addr[0]), config_dir=self.config_dir)
            pairing.end_window("paired", self.config_dir)
            c.pair = None
            c.paired = True
            self._send_to(c, encode(MsgType.PAIR_DONE, pairing.accept_mac(
                half.confirm_key, pairing.ROLE_SERVER,
                half.card_public, half.server_public)))
            self._emit(f"client {c.addr} paired as device {half.device_id}")
