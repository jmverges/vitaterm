"""ChaCha20 (RFC 8439): the stream cipher SEAL v3 encrypts every frame with.

The card's half is dsclient/source/chacha20.c; tools/crosscheck.py holds the
two to each other, and both are pinned to the RFC's own vectors. Pure Python,
like x25519.py, so the server still installs with no compiled dependency. It
runs at about 1 MB/s, about half a millisecond for a full 536-byte frame, and
the link carries tens of kilobytes a second at most (docs/PERFORMANCE.md).
"""

import struct

KEY_SIZE = 32
NONCE_SIZE = 12
BLOCK_SIZE = 64

_MASK = 0xFFFFFFFF
_CONSTANTS = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)
_WORDS = struct.Struct("<16I")


def _block_words(key_words: tuple[int, ...], counter: int,
                 nonce_words: tuple[int, ...]) -> bytes:
    s = (*_CONSTANTS, *key_words, counter & _MASK, *nonce_words)
    x0, x1, x2, x3, x4, x5, x6, x7, x8, x9, x10, x11, x12, x13, x14, x15 = s
    for _ in range(10):
        # Columns, then diagonals: RFC 8439 section 2.3, unrolled because a
        # call per quarter round costs more than the round itself.
        x0 = (x0 + x4) & _MASK; x12 ^= x0; x12 = ((x12 << 16) | (x12 >> 16)) & _MASK
        x8 = (x8 + x12) & _MASK; x4 ^= x8; x4 = ((x4 << 12) | (x4 >> 20)) & _MASK
        x0 = (x0 + x4) & _MASK; x12 ^= x0; x12 = ((x12 << 8) | (x12 >> 24)) & _MASK
        x8 = (x8 + x12) & _MASK; x4 ^= x8; x4 = ((x4 << 7) | (x4 >> 25)) & _MASK
        x1 = (x1 + x5) & _MASK; x13 ^= x1; x13 = ((x13 << 16) | (x13 >> 16)) & _MASK
        x9 = (x9 + x13) & _MASK; x5 ^= x9; x5 = ((x5 << 12) | (x5 >> 20)) & _MASK
        x1 = (x1 + x5) & _MASK; x13 ^= x1; x13 = ((x13 << 8) | (x13 >> 24)) & _MASK
        x9 = (x9 + x13) & _MASK; x5 ^= x9; x5 = ((x5 << 7) | (x5 >> 25)) & _MASK
        x2 = (x2 + x6) & _MASK; x14 ^= x2; x14 = ((x14 << 16) | (x14 >> 16)) & _MASK
        x10 = (x10 + x14) & _MASK; x6 ^= x10; x6 = ((x6 << 12) | (x6 >> 20)) & _MASK
        x2 = (x2 + x6) & _MASK; x14 ^= x2; x14 = ((x14 << 8) | (x14 >> 24)) & _MASK
        x10 = (x10 + x14) & _MASK; x6 ^= x10; x6 = ((x6 << 7) | (x6 >> 25)) & _MASK
        x3 = (x3 + x7) & _MASK; x15 ^= x3; x15 = ((x15 << 16) | (x15 >> 16)) & _MASK
        x11 = (x11 + x15) & _MASK; x7 ^= x11; x7 = ((x7 << 12) | (x7 >> 20)) & _MASK
        x3 = (x3 + x7) & _MASK; x15 ^= x3; x15 = ((x15 << 8) | (x15 >> 24)) & _MASK
        x11 = (x11 + x15) & _MASK; x7 ^= x11; x7 = ((x7 << 7) | (x7 >> 25)) & _MASK
        x0 = (x0 + x5) & _MASK; x15 ^= x0; x15 = ((x15 << 16) | (x15 >> 16)) & _MASK
        x10 = (x10 + x15) & _MASK; x5 ^= x10; x5 = ((x5 << 12) | (x5 >> 20)) & _MASK
        x0 = (x0 + x5) & _MASK; x15 ^= x0; x15 = ((x15 << 8) | (x15 >> 24)) & _MASK
        x10 = (x10 + x15) & _MASK; x5 ^= x10; x5 = ((x5 << 7) | (x5 >> 25)) & _MASK
        x1 = (x1 + x6) & _MASK; x12 ^= x1; x12 = ((x12 << 16) | (x12 >> 16)) & _MASK
        x11 = (x11 + x12) & _MASK; x6 ^= x11; x6 = ((x6 << 12) | (x6 >> 20)) & _MASK
        x1 = (x1 + x6) & _MASK; x12 ^= x1; x12 = ((x12 << 8) | (x12 >> 24)) & _MASK
        x11 = (x11 + x12) & _MASK; x6 ^= x11; x6 = ((x6 << 7) | (x6 >> 25)) & _MASK
        x2 = (x2 + x7) & _MASK; x13 ^= x2; x13 = ((x13 << 16) | (x13 >> 16)) & _MASK
        x8 = (x8 + x13) & _MASK; x7 ^= x8; x7 = ((x7 << 12) | (x7 >> 20)) & _MASK
        x2 = (x2 + x7) & _MASK; x13 ^= x2; x13 = ((x13 << 8) | (x13 >> 24)) & _MASK
        x8 = (x8 + x13) & _MASK; x7 ^= x8; x7 = ((x7 << 7) | (x7 >> 25)) & _MASK
        x3 = (x3 + x4) & _MASK; x14 ^= x3; x14 = ((x14 << 16) | (x14 >> 16)) & _MASK
        x9 = (x9 + x14) & _MASK; x4 ^= x9; x4 = ((x4 << 12) | (x4 >> 20)) & _MASK
        x3 = (x3 + x4) & _MASK; x14 ^= x3; x14 = ((x14 << 8) | (x14 >> 24)) & _MASK
        x9 = (x9 + x14) & _MASK; x4 ^= x9; x4 = ((x4 << 7) | (x4 >> 25)) & _MASK
    x = (x0, x1, x2, x3, x4, x5, x6, x7, x8, x9, x10, x11, x12, x13, x14, x15)
    return _WORDS.pack(*[(a + b) & _MASK for a, b in zip(x, s)])


def _check(key: bytes, nonce: bytes) -> None:
    if len(key) != KEY_SIZE:
        raise ValueError(f"ChaCha20 key must be {KEY_SIZE} bytes, got {len(key)}")
    if len(nonce) != NONCE_SIZE:
        raise ValueError(f"ChaCha20 nonce must be {NONCE_SIZE} bytes, got {len(nonce)}")


def block(key: bytes, nonce: bytes, counter: int) -> bytes:
    """One 64-byte keystream block (RFC 8439 section 2.3)."""
    _check(key, nonce)
    return _block_words(struct.unpack("<8I", key), counter,
                        struct.unpack("<3I", nonce))


def xor(key: bytes, nonce: bytes, data: bytes, counter: int = 0) -> bytes:
    """``data`` XOR the keystream from block ``counter`` on. Encrypting and
    decrypting are the same call."""
    _check(key, nonce)
    if not data:
        return b""
    if counter + (len(data) - 1) // BLOCK_SIZE > _MASK:
        raise ValueError("ChaCha20 block counter would wrap")
    key_words = struct.unpack("<8I", key)
    nonce_words = struct.unpack("<3I", nonce)
    stream = b"".join(_block_words(key_words, counter + i, nonce_words)
                      for i in range((len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE))
    n = len(data)
    return (int.from_bytes(data, "little")
            ^ int.from_bytes(stream[:n], "little")).to_bytes(n, "little")
