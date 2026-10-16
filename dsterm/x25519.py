"""X25519 (RFC 7748), in pure Python, for pairing.

Pairing runs once per device, so speed is irrelevant and a dependency is not:
pyproject.toml keeps pyte the only runtime requirement on purpose, and this is
thirty lines of arithmetic that the RFC's own test vectors pin exactly
(tests/test_pairing.py). The handheld runs the same function in C
(dsclient/source/x25519.c), and tools/crosscheck.py proves the two agree.

Not constant-time. Python integers cannot be, and it does not matter here: the
server's pairing key is fresh per attempt and thrown away afterwards, so a
timing side channel would leak a secret that is never used twice.
"""

from __future__ import annotations

import os

P = 2**255 - 19
_A24 = 121665
KEY_SIZE = 32
BASE_POINT = (9).to_bytes(32, "little")


def _clamp(scalar: bytes) -> int:
    k = bytearray(scalar)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    return int.from_bytes(k, "little")


def x25519(scalar: bytes, u: bytes) -> bytes:
    """The Montgomery ladder of RFC 7748 section 5."""
    if len(scalar) != KEY_SIZE or len(u) != KEY_SIZE:
        raise ValueError("x25519 takes 32-byte scalars and coordinates")
    k = _clamp(scalar)
    # The top bit of u is ignored, as the RFC requires.
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3 = 1, 0, x1, 1
    swap = 0
    for t in reversed(range(255)):
        bit = (k >> t) & 1
        swap ^= bit
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = bit
        a = (x2 + z2) % P
        aa = a * a % P
        b = (x2 - z2) % P
        bb = b * b % P
        e = (aa - bb) % P
        c = (x3 + z3) % P
        d = (x3 - z3) % P
        da = d * a % P
        cb = c * b % P
        x3 = (da + cb) ** 2 % P
        z3 = x1 * (da - cb) ** 2 % P
        x2 = aa * bb % P
        z2 = e * (aa + _A24 * e) % P
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return (x2 * pow(z2, P - 2, P) % P).to_bytes(32, "little")


def public_key(private: bytes) -> bytes:
    return x25519(private, BASE_POINT)


def generate_private() -> bytes:
    return os.urandom(KEY_SIZE)
