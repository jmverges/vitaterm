// SHA-256 and HMAC-SHA256.
//
// Self-contained rather than pulling in Mbed TLS: the client needs exactly one
// hash for one handshake, and a ~200 line dependency-free implementation can be
// compiled and tested on the host, which a DS-only library build cannot.

#ifndef DSTERM_SHA256_H
#define DSTERM_SHA256_H

#include <stddef.h>
#include <stdint.h>

#define SHA256_DIGEST_SIZE 32
#define SHA256_BLOCK_SIZE  64

typedef struct {
    uint32_t state[8];
    uint64_t bitlen;
    uint8_t  buf[SHA256_BLOCK_SIZE];
    size_t   buflen;
} sha256_ctx;

void sha256_init(sha256_ctx *ctx);
void sha256_update(sha256_ctx *ctx, const uint8_t *data, size_t len);
void sha256_final(sha256_ctx *ctx, uint8_t out[SHA256_DIGEST_SIZE]);

void sha256(const uint8_t *data, size_t len, uint8_t out[SHA256_DIGEST_SIZE]);

// HMAC-SHA256. `out` receives 32 bytes.
void hmac_sha256(const uint8_t *key, size_t key_len,
                 const uint8_t *msg, size_t msg_len,
                 uint8_t out[SHA256_DIGEST_SIZE]);

// Constant-time comparison. Returns 1 on match, 0 otherwise. Used so a peer
// cannot learn a MAC byte-by-byte by timing repeated connections.
int ct_equal(const uint8_t *a, const uint8_t *b, size_t len);

// HKDF-SHA256 (RFC 5869): extract with `salt`, expand with `info` into
// `out_len` bytes.  Bounded rather than general: `info` is at most
// HKDF_INFO_MAX bytes and `out_len` at most 8 blocks, which is far more than
// pairing needs and keeps the expansion buffer on the stack a known size.
// Returns 1, or 0 (writing nothing) if a bound is exceeded.
#define HKDF_INFO_MAX 192
#define HKDF_OUT_MAX (8 * SHA256_DIGEST_SIZE)
int hkdf_sha256(const uint8_t *salt, size_t salt_len,
                const uint8_t *ikm, size_t ikm_len,
                const uint8_t *info, size_t info_len,
                uint8_t *out, size_t out_len);

#endif  // DSTERM_SHA256_H
