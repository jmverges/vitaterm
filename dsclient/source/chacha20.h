// ChaCha20 (RFC 8439): the stream cipher SEAL v3 encrypts every frame with.
//
// Chosen over AES because a DS has no AES hardware, and software AES is both
// slower on an ARM9 and, with its table lookups, leaky about timing.  ChaCha20
// is additions, rotations and XORs on 32-bit words: constant time with no
// effort, and cheaper per byte than the SHA-256 the card already runs over
// every frame for its tag.  docs/SEAL.md says how the keys and nonces are
// chosen; this is only the cipher, pinned to the RFC's own vectors in the
// host suite and held to the server's Python by tools/crosscheck.py.
//
// Pure C, no libnds.
#ifndef DSTERM_CHACHA20_H
#define DSTERM_CHACHA20_H

#include <stddef.h>
#include <stdint.h>

#define CHACHA20_KEY 32
#define CHACHA20_NONCE 12
#define CHACHA20_BLOCK 64

// One 64-byte keystream block for (key, nonce, counter).
void chacha20_block(uint8_t out[CHACHA20_BLOCK], const uint8_t key[CHACHA20_KEY],
                    const uint8_t nonce[CHACHA20_NONCE], uint32_t counter);

// XOR `len` bytes of `in` with the keystream that starts at block `counter`,
// into `out`.  `out` may be `in`: encrypting and decrypting are the same act.
void chacha20_xor(uint8_t *out, const uint8_t *in, size_t len,
                  const uint8_t key[CHACHA20_KEY],
                  const uint8_t nonce[CHACHA20_NONCE], uint32_t counter);

#endif  // DSTERM_CHACHA20_H
