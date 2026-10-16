// Pairing: a per-device key from an X25519 exchange that both people confirm
// by comparing a six-digit code -- the numeric comparison of Bluetooth's
// Secure Connections.  No secret and no key ever crosses the link.
//
//   card   -> PAIR_HELLO    device id (4), X25519 public key A (32)
//   server -> PAIR_REPLY    X25519 public key B (32),
//                           commitment C = SHA-256(label | A | B | Ns) (32)
//   card   -> PAIR_CONFIRM  card nonce Nc (16)
//   server -> PAIR_OK       server nonce Ns (16), which must open C
//   both:  code = SHA-256(label | A | B | Nc | Ns) as six digits
//          okm  = HKDF-SHA256(Nc | Ns, X25519(own, peer),
//                             "dsterm pair v2" | id | A | B)
//          confirm key = okm[0..32), device key = okm[32..48)
//   person: same code on both screens?  A here, y on the computer.
//   card   -> PAIR_ACCEPT   HMAC(confirm key, "dsterm pair v2 accept ds"  | A | B)
//   server -> PAIR_DONE     HMAC(confirm key, "dsterm pair v2 accept srv" | A | B)
//
// The argument is in dsterm/pairing.py, which is the other half of this file
// and is held to it by tools/crosscheck.py.  In short: an attacker in the
// middle would have to make two independent exchanges produce the same six
// digits, and the commitment stops it steering either one -- one chance in a
// million per attempt, with nothing to guess offline.
//
// Pure C.  The private key, the nonce and their randomness are the caller's:
// see entropy.h.

#ifndef DSTERM_PAIR_H
#define DSTERM_PAIR_H

#include <stddef.h>
#include <stdint.h>

#include "proto.h"
#include "x25519.h"

#define PAIR_KEY_SIZE 16
#define PAIR_CONFIRM_KEY_SIZE 32
#define PAIR_MAC_SIZE 32
#define PAIR_NONCE_SIZE 16
#define PAIR_COMMIT_SIZE 32
#define PAIR_CODE_DIGITS 6
#define PAIR_HELLO_SIZE (PROTO_DEVICE_ID_BYTES + X25519_SIZE)
#define PAIR_REPLY_SIZE (X25519_SIZE + PAIR_COMMIT_SIZE)

// C = SHA-256("dsterm pair v2 commit" | A | B | Ns).
void pair_commitment(const uint8_t card_public[X25519_SIZE],
                     const uint8_t server_public[X25519_SIZE],
                     const uint8_t server_nonce[PAIR_NONCE_SIZE],
                     uint8_t out[PAIR_COMMIT_SIZE]);

// Whether PAIR_OK's nonce opens the commitment PAIR_REPLY carried.
// Constant time.  A server that fails this is refused before any code shows.
int pair_commitment_kept(const uint8_t card_public[X25519_SIZE],
                         const uint8_t server_public[X25519_SIZE],
                         const uint8_t *server_nonce, size_t nonce_len,
                         const uint8_t commit[PAIR_COMMIT_SIZE]);

// The six digits both screens show, 0..999999.
uint32_t pair_code(const uint8_t card_public[X25519_SIZE],
                   const uint8_t server_public[X25519_SIZE],
                   const uint8_t card_nonce[PAIR_NONCE_SIZE],
                   const uint8_t server_nonce[PAIR_NONCE_SIZE]);

// Both keys from one exchange.  Returns 0, writing nothing, if the shared
// secret is all zero -- a low-order public key from the peer.
int pair_derive(const uint8_t shared[X25519_SIZE],
                const uint8_t card_nonce[PAIR_NONCE_SIZE],
                const uint8_t server_nonce[PAIR_NONCE_SIZE],
                const uint8_t device_id[PROTO_DEVICE_ID_BYTES],
                const uint8_t card_public[X25519_SIZE],
                const uint8_t server_public[X25519_SIZE],
                uint8_t confirm_key[PAIR_CONFIRM_KEY_SIZE],
                uint8_t device_key[PAIR_KEY_SIZE]);

typedef enum { PAIR_ROLE_CARD = 0, PAIR_ROLE_SERVER = 1 } pair_role_t;

// PAIR_ACCEPT (the card's) or PAIR_DONE (the server's).
void pair_accept_mac(const uint8_t confirm_key[PAIR_CONFIRM_KEY_SIZE],
                     pair_role_t role,
                     const uint8_t card_public[X25519_SIZE],
                     const uint8_t server_public[X25519_SIZE],
                     uint8_t out[PAIR_MAC_SIZE]);

// Whether PAIR_DONE's payload is the server's MAC under the same derived key.
// Constant time.
int pair_server_done(const uint8_t confirm_key[PAIR_CONFIRM_KEY_SIZE],
                     const uint8_t card_public[X25519_SIZE],
                     const uint8_t server_public[X25519_SIZE],
                     const uint8_t *payload, size_t len);

#endif  // DSTERM_PAIR_H
