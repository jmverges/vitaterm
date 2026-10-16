"""Extensions: registering one and queuing its frames behind the terminal's.

Part of the server, split out by feature: the methods below are
mixed into Server (server.py) and share its state through `self`.
"""

from __future__ import annotations


from .protocol import MAX_PAYLOAD
from .connection import (
    ClientState,
    ExtensionFrame,
)


class ExtensionsMixin:
    """Owns the registered extensions (_init_ext); per connection,
    ClientState.ext."""

    # -- extensions ---------------------------------------------------------

    def register(self, extension) -> None:
        """Attach an extension: something with a ``token`` that may offer the
        handheld one deferred message at a time on MsgType.EXTENSION.

        The server owns delivery and knows nothing of what the body means.
        """
        self._extensions.append(extension)
        extension.attach(self)

    def queue_extension(self, token: str, payload: bytes) -> bool:
        """Offer one bounded message to the currently connected capable card.

        ``token`` is the HELLO capability the card must have announced. The
        message is deliberately scoped to this exact connection and is
        overwritten rather than queued; it never survives a disconnect. The
        body is the extension's -- this server frames it and nothing else --
        and the vocabulary it may carry is the extension's to police.
        """
        if not isinstance(token, str) or not token:
            raise TypeError("token must be a non-empty str")
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError("payload must be bytes")
        if len(payload) > MAX_PAYLOAD:
            raise ValueError("payload exceeds the frame size")
        with self._ext_lock:
            c = self._authed()
            if c is None or c not in self._clients or token not in c.caps.tokens:
                if c is not None:
                    self._discard_ext_locked(c)
                return False
            if c.ext.queued is not None:
                c.ext.queued.token = token
                c.ext.queued.payload = bytes(payload)
            else:
                c.ext.pending = ExtensionFrame(token, bytes(payload))
            return True

    def _queue_pending_extension(self, c: ClientState) -> None:
        """Move at most one connection-bound message into this tick's FIFO."""
        with self._ext_lock:
            if (c not in self._clients or self._authed() is not c or
                    self._switch_in_progress(c)):
                return
            frame = c.ext.pending
            if frame is not None and frame.token in c.caps.tokens:
                c.ext.pending = None
                c.ext.queued = frame
                c.outbox.append(frame)

    # -- what the core calls (server.py) -------------------------------------

    def _forget_queued_extension(self, c: ClientState) -> None:
        """A switch discarded the outbox: the frame queued behind it goes with
        it, and the pending one is released after. The caller holds _ext_lock."""
        c.ext.queued = None

    def _init_ext(self) -> None:
        """The registered extensions. Their frames ride the core's outbox,
        under the core's _ext_lock."""
        self._extensions: list = []
