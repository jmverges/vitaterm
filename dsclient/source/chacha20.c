#include "chacha20.h"

#include <string.h>

static uint32_t load32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 |
           (uint32_t)p[3] << 24;
}

static void store32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

#define ROTL(v, n) (((v) << (n)) | ((v) >> (32 - (n))))
#define QR(a, b, c, d)                                 \
    do {                                               \
        a += b; d ^= a; d = ROTL(d, 16);               \
        c += d; b ^= c; b = ROTL(b, 12);               \
        a += b; d ^= a; d = ROTL(d, 8);                \
        c += d; b ^= c; b = ROTL(b, 7);                \
    } while (0)

void chacha20_block(uint8_t out[CHACHA20_BLOCK], const uint8_t key[CHACHA20_KEY],
                    const uint8_t nonce[CHACHA20_NONCE], uint32_t counter)
{
    // "expand 32-byte k", then the key, the counter and the nonce (RFC 8439
    // section 2.3), all little-endian words.
    uint32_t s[16] = {
        0x61707865u, 0x3320646eu, 0x79622d32u, 0x6b206574u,
    };
    for (int i = 0; i < 8; i++) s[4 + i] = load32(key + 4 * i);
    s[12] = counter;
    for (int i = 0; i < 3; i++) s[13 + i] = load32(nonce + 4 * i);

    uint32_t x[16];
    memcpy(x, s, sizeof(x));
    for (int round = 0; round < 10; round++) {
        QR(x[0], x[4], x[8], x[12]);
        QR(x[1], x[5], x[9], x[13]);
        QR(x[2], x[6], x[10], x[14]);
        QR(x[3], x[7], x[11], x[15]);
        QR(x[0], x[5], x[10], x[15]);
        QR(x[1], x[6], x[11], x[12]);
        QR(x[2], x[7], x[8], x[13]);
        QR(x[3], x[4], x[9], x[14]);
    }
    for (int i = 0; i < 16; i++) store32(out + 4 * i, x[i] + s[i]);
}

void chacha20_xor(uint8_t *out, const uint8_t *in, size_t len,
                  const uint8_t key[CHACHA20_KEY],
                  const uint8_t nonce[CHACHA20_NONCE], uint32_t counter)
{
    uint8_t ks[CHACHA20_BLOCK];
    while (len) {
        chacha20_block(ks, key, nonce, counter++);
        const size_t n = len < CHACHA20_BLOCK ? len : CHACHA20_BLOCK;
        for (size_t i = 0; i < n; i++) out[i] = (uint8_t)(in[i] ^ ks[i]);
        out += n;
        in += n;
        len -= n;
    }
    memset(ks, 0, sizeof(ks));
}
