"""Pairing a handheld by comparing a code, and the per-device keys it makes.

Both screens show the same six-digit code and a person confirms it on both:
the numeric comparison of Bluetooth's Secure Connections, over X25519.

    card   -> PAIR_HELLO    device id, X25519 public key A
    server -> PAIR_REPLY    X25519 public key B,
                            commitment C = SHA-256(label | A | B | Ns)
    card   -> PAIR_CONFIRM  card nonce Nc (16 random bytes)
    server -> PAIR_OK       server nonce Ns, which must open C
    both:  code   = SHA-256(label | A | B | Nc | Ns), as six digits
           shared = X25519(own private, peer public)
           okm    = HKDF-SHA256(Nc | Ns, shared, label | id | A | B)
           confirm key = okm[0:32], device key = okm[32:48]
    person: the same code on both screens?  A on the DS, y on the computer.
    card   -> PAIR_ACCEPT   HMAC(confirm key, "... ds"  | A | B)
    server -> PAIR_DONE     HMAC(confirm key, "... srv" | A | B)
                            (or PAIR_FAIL)

* **Nothing secret crosses the link.** The device key is derived on both
  ends from the Diffie-Hellman secret; it is never sent.
* **An attacker in the middle cannot make the codes match.** It would have to
  run one exchange with the card and another with the server, and get the
  same six digits out of both.  The server commits to its nonce before it
  sees the card's, and reveals it only after, so neither side's nonce can be
  chosen to steer the other's code: one chance in a million per attempt,
  and a failed attempt is a person seeing two different codes.  There is
  nothing to guess offline -- the weakness of the PIN this replaced, whose
  confirmation let an impostor server test every PIN at leisure.
* **Both people agree, and both keys match.** PAIR_ACCEPT and PAIR_DONE are
  MACs under the derived key: each side proves it holds the same one, and
  neither saves a key until the other has.
* **A low-order public key is refused**: it would make the shared secret zero.

The window is a file, not a socket: `dsterm pair` writes it (mode 600), the
running server writes the code into it when a card asks, and `dsterm pair`
writes the person's answer to a second file.  Nothing listens for it, so
there is no new endpoint for another local user to poke, and the dashboard
-- which binds loopback without authentication -- never shows the code.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from . import x25519
from .auth import KEY_BYTES
from .protocol import DEVICE_ID_BYTES, PAIR_NONCE_SIZE, is_device_id

CONFIG_DIR = Path.home() / ".config" / "ds-claude-terminal"
WINDOW_FILE = "pairing"
ANSWER_FILE = "pairing-answer"
DEVICES_DIR = "devices"

CODE_DIGITS = 6
WINDOW_SECONDS = 120.0

_LABEL = b"dsterm pair v2"
_COMMIT_LABEL = b"dsterm pair v2 commit"
_CODE_LABEL = b"dsterm pair v2 code"
_ACCEPT_LABEL = b"dsterm pair v2 accept "
ROLE_CARD = b"ds"
ROLE_SERVER = b"srv"


class PairingError(Exception):
    """A pairing attempt that must stop. The message is shown to the person
    pairing, on the console, so it says what to do."""


# -- the derivation ------------------------------------------------------------


def hkdf_sha256(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869. Written out rather than imported: it is a dozen lines, and the
    card has the same dozen lines in C that the crosscheck holds to these."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def commitment(card_public: bytes, server_public: bytes, server_nonce: bytes) -> bytes:
    return hashlib.sha256(_COMMIT_LABEL + card_public + server_public +
                          server_nonce).digest()


def code(card_public: bytes, server_public: bytes, card_nonce: bytes,
         server_nonce: bytes) -> str:
    """The six digits both screens show."""
    digest = hashlib.sha256(_CODE_LABEL + card_public + server_public +
                            card_nonce + server_nonce).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 10 ** CODE_DIGITS:0{CODE_DIGITS}d}"


def derive(shared: bytes, card_nonce: bytes, server_nonce: bytes, device_id: bytes,
           card_public: bytes, server_public: bytes) -> tuple[bytes, bytes]:
    """(confirm key, device key) from one exchange."""
    if hmac.compare_digest(shared, bytes(32)):
        # A low-order point: the "shared secret" is zero whatever our key
        # was, and the device key would be public.
        raise PairingError("the card sent a key that cannot be used")
    if (len(device_id) != DEVICE_ID_BYTES or len(card_nonce) != PAIR_NONCE_SIZE or
            len(server_nonce) != PAIR_NONCE_SIZE):
        raise PairingError("malformed pairing parameters")
    info = _LABEL + device_id + card_public + server_public
    okm = hkdf_sha256(card_nonce + server_nonce, shared, info, 32 + KEY_BYTES)
    return okm[:32], okm[32:]


def accept_mac(confirm_key: bytes, role: bytes,
               card_public: bytes, server_public: bytes) -> bytes:
    return hmac.new(confirm_key, _ACCEPT_LABEL + role + card_public + server_public,
                    hashlib.sha256).digest()


@dataclass
class ServerHalf:
    """One pairing attempt, as the server holds it from REPLY to DONE."""
    device_id: str
    device_id_bytes: bytes
    card_public: bytes
    server_public: bytes
    shared: bytes
    server_nonce: bytes
    commit: bytes
    # After PAIR_CONFIRM:
    code: str = ""
    confirm_key: bytes = b""
    device_key: bytes = b""
    # After PAIR_ACCEPT:
    card_accepted: bool = False


def server_respond(device_id: bytes, card_public: bytes) -> ServerHalf:
    """Everything the server does on PAIR_HELLO: a fresh key pair, the shared
    secret, a fresh nonce and the commitment to it.  The private key goes out
    of scope here."""
    private = x25519.generate_private()
    server_public = x25519.public_key(private)
    shared = x25519.x25519(private, card_public)
    if hmac.compare_digest(shared, bytes(32)):
        raise PairingError("the card sent a key that cannot be used")
    nonce = os.urandom(PAIR_NONCE_SIZE)
    return ServerHalf(device_id.hex(), device_id, card_public, server_public, shared,
                      nonce, commitment(card_public, server_public, nonce))


def server_reveal(half: ServerHalf, card_nonce: bytes) -> None:
    """PAIR_CONFIRM: the card's nonce arrives; the code and both keys follow."""
    half.confirm_key, half.device_key = derive(
        half.shared, card_nonce, half.server_nonce, half.device_id_bytes,
        half.card_public, half.server_public)
    half.code = code(half.card_public, half.server_public, card_nonce,
                     half.server_nonce)


# -- files ---------------------------------------------------------------------


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_private(path: Path, text: str) -> None:
    """Created 600 from the start, and renamed into place, so a reader never
    sees a half-written file and nobody else ever sees any of it."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _read_private(path: Path) -> str | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(f"{path} is readable by other users; chmod 600 it")
    return path.read_text()


# -- the window ----------------------------------------------------------------


@dataclass
class Window:
    expires: float
    # Set by the server while a card is comparing: the code its screen shows,
    # and which card.  Empty while the window waits for a card.
    code: str = ""
    device: str = ""
    # Why the last attempt ended, for `dsterm pair` to say ("" while live).
    ended: str = ""

    def remaining(self, now: float | None = None) -> float:
        return max(0.0, self.expires - (now if now is not None else time.time()))


def _write_window(config_dir: Path, window: Window) -> None:
    _write_private(_private_dir(config_dir) / WINDOW_FILE,
                   json.dumps({"expires": window.expires, "code": window.code,
                               "device": window.device, "ended": window.ended}))


def open_window(config_dir: Path = CONFIG_DIR, seconds: float = WINDOW_SECONDS) -> Window:
    window = Window(expires=time.time() + seconds)
    clear_answer(config_dir)
    _write_window(config_dir, window)
    return window


def read_window(config_dir: Path = CONFIG_DIR) -> Window | None:
    """The window file as written, open or not (None if absent or unreadable).
    An unreadable or malformed file reads as absent: the failure mode of a
    bad window file is "cannot pair", never "can pair"."""
    try:
        raw = _read_private(config_dir / WINDOW_FILE)
    except PermissionError:
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return Window(expires=float(data["expires"]), code=str(data.get("code", "")),
                      device=str(data.get("device", "")), ended=str(data.get("ended", "")))
    except (ValueError, KeyError, TypeError):
        return None


def current_window(config_dir: Path = CONFIG_DIR) -> Window | None:
    """The open window: present, not expired, and not ended."""
    window = read_window(config_dir)
    if window is None or window.remaining() <= 0 or window.ended:
        return None
    return window


def show_code(code_text: str, device_id: str, config_dir: Path = CONFIG_DIR) -> bool:
    """The server, on PAIR_CONFIRM: put the code where `dsterm pair` reads it.
    False if the window closed in the meantime."""
    window = current_window(config_dir)
    if window is None:
        return False
    window.code, window.device = code_text, device_id
    _write_window(config_dir, window)
    return True


def end_window(why: str, config_dir: Path = CONFIG_DIR) -> None:
    """Close the window with a reason `dsterm pair` shows: paired, rejected,
    the console left.  Any one attempt ending closes it -- a mismatch may be
    an attack, and the next try should be a fresh, deliberate `dsterm pair`."""
    window = read_window(config_dir)
    if window is None:
        return
    window.ended = why
    _write_window(config_dir, window)
    clear_answer(config_dir)


def close_window(config_dir: Path = CONFIG_DIR) -> None:
    for name in (WINDOW_FILE, ANSWER_FILE):
        try:
            (config_dir / name).unlink()
        except FileNotFoundError:
            pass


def write_answer(code_text: str, accepted: bool, config_dir: Path = CONFIG_DIR) -> None:
    """`dsterm pair`, after the person answered: the code they were shown,
    and whether it matched the console.  The code is part of the answer so a
    yes can only ever approve the exchange it was given for."""
    _write_private(_private_dir(config_dir) / ANSWER_FILE,
                   json.dumps({"code": code_text, "accepted": bool(accepted)}))


def read_answer(config_dir: Path = CONFIG_DIR) -> tuple[str, bool] | None:
    try:
        raw = _read_private(config_dir / ANSWER_FILE)
    except PermissionError:
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return str(data["code"]), bool(data["accepted"])
    except (ValueError, KeyError, TypeError):
        return None


def clear_answer(config_dir: Path = CONFIG_DIR) -> None:
    try:
        (config_dir / ANSWER_FILE).unlink()
    except FileNotFoundError:
        pass


# -- device keys ---------------------------------------------------------------


@dataclass
class Device:
    device_id: str
    paired_at: float
    name: str
    address: str


def _devices(config_dir: Path) -> Path:
    return config_dir / DEVICES_DIR


def save_device(device_id: str, key: bytes, *, name: str = "", address: str = "",
                config_dir: Path = CONFIG_DIR) -> Path:
    if not is_device_id(device_id):
        raise ValueError(f"not a device id: {device_id!r}")
    if len(key) != KEY_BYTES:
        raise ValueError("device keys are 16 bytes")
    d = _private_dir(_devices(config_dir))
    _write_private(d / f"{device_id}.psk", key.hex())
    _write_private(d / f"{device_id}.json",
                   json.dumps({"paired_at": time.time(), "name": name,
                               "address": address}))
    return d / f"{device_id}.psk"


def load_device_key(device_id: str, config_dir: Path = CONFIG_DIR) -> bytes | None:
    """The key a paired device must prove, or None if it has none. Refuses a
    key file other users can read, like the main key."""
    if not is_device_id(device_id):
        return None
    raw = _read_private(_devices(config_dir) / f"{device_id}.psk")
    if raw is None:
        return None
    key = bytes.fromhex(raw.strip())
    if len(key) != KEY_BYTES:
        raise ValueError(f"device key {device_id} is not {KEY_BYTES} bytes")
    return key


def list_devices(config_dir: Path = CONFIG_DIR) -> list[Device]:
    out = []
    d = _devices(config_dir)
    if not d.is_dir():
        return out
    for key_file in sorted(d.glob("*.psk")):
        device_id = key_file.stem
        meta: dict = {}
        try:
            meta = json.loads((d / f"{device_id}.json").read_text())
        except (OSError, ValueError):
            pass
        out.append(Device(device_id, float(meta.get("paired_at", 0.0)),
                          str(meta.get("name", "")), str(meta.get("address", ""))))
    return out


def forget_device(device_id: str, config_dir: Path = CONFIG_DIR) -> bool:
    if not is_device_id(device_id):
        return False
    found = False
    for suffix in (".psk", ".json"):
        try:
            (_devices(config_dir) / f"{device_id}{suffix}").unlink()
            found = True
        except FileNotFoundError:
            pass
    return found


def new_device_id() -> str:
    return secrets.token_hex(DEVICE_ID_BYTES)


def server_name() -> str:
    """What this machine calls itself to a card: the short hostname, reduced to
    characters the card's font has and cut to what its menu row can show."""
    raw = socket.gethostname().split(".")[0]
    clean = "".join(c for c in raw if c.isalnum() or c in "-_")
    return (clean or "dsterm")[:20]
