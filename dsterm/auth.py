"""Challenge-response authentication.

The server sends a fresh random nonce; the client proves it holds the shared key
by returning HMAC-SHA256(key, nonce). The key itself never crosses the link, so
capturing a session does not yield anything replayable -- which matters because
the DS may be on an open or WEP network, where "encrypted" means nothing.

This is authentication, not confidentiality. Everything after the handshake is
plaintext. See README.md: this is a lock on a screen door. A card that asks for
SEAL gets every later frame tagged with a key derived from this same key and
nonce (protocol.seal_key, docs/SEAL.md) -- integrity for the rest of the
connection, still not secrecy.
"""

from __future__ import annotations

import hmac
import os
import secrets
import stat
from hashlib import sha256
from pathlib import Path

from .protocol import MAC_SIZE, NONCE_SIZE, ProtocolError

DEFAULT_KEY_PATH = Path.home() / ".config" / "ds-claude-terminal" / "psk"

# 16 bytes of key material, rendered as 32 hex characters for hand-entry on a
# touchscreen. Shorter would be temptingly typo-proof and cryptographically weak.
KEY_BYTES = 16


def generate_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def generate_nonce() -> bytes:
    """Fresh per connection. Reusing one would make captured MACs replayable."""
    return os.urandom(NONCE_SIZE)


def compute_mac(key: bytes, nonce: bytes) -> bytes:
    if len(nonce) != NONCE_SIZE:
        raise ProtocolError(f"nonce must be {NONCE_SIZE} bytes, got {len(nonce)}")
    return hmac.new(key, nonce, sha256).digest()


def verify_mac(key: bytes, nonce: bytes, presented: bytes) -> bool:
    """Constant-time comparison. A timing-variable check leaks the MAC byte by
    byte to anyone who can reconnect in a loop, which any LAN peer can."""
    if len(presented) != MAC_SIZE:
        return False
    return hmac.compare_digest(compute_mac(key, nonce), presented)


# -- key storage -----------------------------------------------------------


def load_key(path: Path | None = None) -> bytes:
    path = path or DEFAULT_KEY_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"no pairing key at {path}; run `dsterm setup` to create one"
        )
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(
            f"{path} is readable by other users; chmod 600 it before use"
        )
    raw = path.read_text().strip()
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise ValueError(f"{path} does not contain a hex key") from exc
    if len(key) != KEY_BYTES:
        raise ValueError(f"key must be {KEY_BYTES} bytes, got {len(key)}")
    return key


def save_key(key: bytes, path: Path | None = None) -> Path:
    path = path or DEFAULT_KEY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with restrictive permissions from the outset rather than
    # chmod-ing afterwards, which would leave a window where it is world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key.hex())
    return path


def format_for_entry(key: bytes) -> str:
    """Group the hex into fours so it can be read off a screen and typed on a
    touchscreen keyboard without losing your place."""
    h = key.hex()
    return " ".join(h[i : i + 4] for i in range(0, len(h), 4))
