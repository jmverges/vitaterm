// X25519 (RFC 7748): the Diffie-Hellman function pairing is built on.
//
// Derived from TweetNaCl's crypto_scalarmult (public domain), which is the
// smallest well-studied implementation there is: sixteen 16-bit limbs held in
// int64_t, a constant-time Montgomery ladder, no tables, no allocation.  It
// runs once per pairing, so small and obviously-correct beats fast.  The
// RFC's own vectors pin it in the host suite, and tools/crosscheck.py holds it
// to the server's Python implementation.
//
// Pure C, no libnds.
#ifndef DSTERM_X25519_H
#define DSTERM_X25519_H

#include <stdint.h>

#define X25519_SIZE 32

// out = X25519(scalar, point).  The scalar is clamped as the RFC specifies;
// the point's top bit is ignored.
void x25519(uint8_t out[X25519_SIZE], const uint8_t scalar[X25519_SIZE],
            const uint8_t point[X25519_SIZE]);

// out = X25519(scalar, 9): the public key for a private scalar.
void x25519_public(uint8_t out[X25519_SIZE], const uint8_t scalar[X25519_SIZE]);

#endif  // DSTERM_X25519_H
