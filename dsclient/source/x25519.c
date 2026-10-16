// X25519, after TweetNaCl's crypto_scalarmult (public domain).  See x25519.h.
//
// Two departures from the original, both for the host suite's sanitizers
// rather than for the arithmetic: a carry is subtracted as c * 65536 instead
// of c << 16 (a left shift of a negative value is undefined in C), and the
// ladder's working storage is named rather than packed into one array.

#include "x25519.h"

#include <string.h>

typedef int64_t gf[16];

static const gf GF_121665 = {0xDB41, 1};

static void car25519(gf o)
{
    for (int i = 0; i < 16; i++) {
        o[i] += (int64_t)1 << 16;
        const int64_t c = o[i] >> 16;
        // Limb 15 wraps into limb 0 times 38 (2^256 = 38 mod p); the -1
        // undoes the bias added above.
        if (i < 15) o[i + 1] += c - 1;
        else        o[0] += 38 * (c - 1);
        o[i] -= c * 65536;
    }
}

// Constant-time conditional swap: b is 0 or 1.
static void sel25519(gf p, gf q, int b)
{
    const int64_t mask = ~((int64_t)b - 1);
    for (int i = 0; i < 16; i++) {
        const int64_t t = mask & (p[i] ^ q[i]);
        p[i] ^= t;
        q[i] ^= t;
    }
}

static void pack25519(uint8_t o[32], const gf n)
{
    gf m, t;
    memcpy(t, n, sizeof(gf));
    car25519(t);
    car25519(t);
    car25519(t);
    for (int j = 0; j < 2; j++) {
        m[0] = t[0] - 0xffed;
        for (int i = 1; i < 15; i++) {
            m[i] = t[i] - 0xffff - ((m[i - 1] >> 16) & 1);
            m[i - 1] &= 0xffff;
        }
        m[15] = t[15] - 0x7fff - ((m[14] >> 16) & 1);
        const int b = (int)((m[15] >> 16) & 1);
        m[14] &= 0xffff;
        sel25519(t, m, 1 - b);
    }
    for (int i = 0; i < 16; i++) {
        o[2 * i] = (uint8_t)(t[i] & 0xff);
        o[2 * i + 1] = (uint8_t)((t[i] >> 8) & 0xff);
    }
}

static void unpack25519(gf o, const uint8_t n[32])
{
    for (int i = 0; i < 16; i++) o[i] = n[2 * i] + ((int64_t)n[2 * i + 1] << 8);
    o[15] &= 0x7fff;
}

static void add(gf o, const gf a, const gf b)
{
    for (int i = 0; i < 16; i++) o[i] = a[i] + b[i];
}

static void sub(gf o, const gf a, const gf b)
{
    for (int i = 0; i < 16; i++) o[i] = a[i] - b[i];
}

static void mul(gf o, const gf a, const gf b)
{
    int64_t t[31];
    for (int i = 0; i < 31; i++) t[i] = 0;
    for (int i = 0; i < 16; i++) {
        for (int j = 0; j < 16; j++) t[i + j] += a[i] * b[j];
    }
    for (int i = 0; i < 15; i++) t[i] += 38 * t[i + 16];
    for (int i = 0; i < 16; i++) o[i] = t[i];
    car25519(o);
    car25519(o);
}

static void sqr(gf o, const gf a)
{
    mul(o, a, a);
}

// a^(p-2) = a^-1 by Fermat.
static void inv25519(gf o, const gf in)
{
    gf c;
    memcpy(c, in, sizeof(gf));
    for (int a = 253; a >= 0; a--) {
        sqr(c, c);
        if (a != 2 && a != 4) mul(c, c, in);
    }
    memcpy(o, c, sizeof(gf));
}

void x25519(uint8_t out[X25519_SIZE], const uint8_t scalar[X25519_SIZE],
            const uint8_t point[X25519_SIZE])
{
    uint8_t z[32];
    gf x, a, b, c, d, e, f;

    memcpy(z, scalar, 32);
    z[31] = (uint8_t)((z[31] & 127) | 64);
    z[0] &= 248;

    unpack25519(x, point);
    for (int i = 0; i < 16; i++) {
        b[i] = x[i];
        a[i] = c[i] = d[i] = 0;
    }
    a[0] = d[0] = 1;

    for (int i = 254; i >= 0; --i) {
        const int r = (z[i >> 3] >> (i & 7)) & 1;
        sel25519(a, b, r);
        sel25519(c, d, r);
        add(e, a, c);
        sub(a, a, c);
        add(c, b, d);
        sub(b, b, d);
        sqr(d, e);
        sqr(f, a);
        mul(a, c, a);
        mul(c, b, e);
        add(e, a, c);
        sub(a, a, c);
        sqr(b, a);
        sub(c, d, f);
        mul(a, c, GF_121665);
        add(a, a, d);
        mul(c, c, a);
        mul(a, d, f);
        mul(d, b, x);
        sqr(b, e);
        sel25519(a, b, r);
        sel25519(c, d, r);
    }

    inv25519(c, c);
    mul(a, a, c);
    pack25519(out, a);

    // The clamped scalar is a copy of the private key; do not leave it on
    // the stack for whatever runs next.
    memset(z, 0, sizeof(z));
}

void x25519_public(uint8_t out[X25519_SIZE], const uint8_t scalar[X25519_SIZE])
{
    static const uint8_t base[32] = {9};
    x25519(out, scalar, base);
}
