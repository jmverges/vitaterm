// SEAL: every frame after the handshake is encrypted (ChaCha20) and carries an
// 8-byte HMAC tag over what was encrypted.  Nobody on the network can read the
// session, and a frame injected into the TCP stream -- keystrokes into the
// Claude session, from somebody on the same open network -- ends the
// connection instead of reaching the terminal.  docs/SEAL.md is the contract;
// this is the card's half, pure C over byte buffers so every case is
// host-tested.

#ifndef DSTERM_SEAL_H
#define DSTERM_SEAL_H

#include <stddef.h>
#include <stdint.h>

#include "proto.h"

#define SEAL_TAG 8
#define SEAL_KEY 32

typedef struct {
    int on;                 // after AUTH_OK: every frame from now on
    uint8_t k_mac[SEAL_KEY];    // the tags, both directions
    uint8_t k_tx[SEAL_KEY];     // what the card sends ("enc C")
    uint8_t k_rx[SEAL_KEY];     // what the server sends ("enc S")
    uint32_t tx;            // frames sent since AUTH_OK
    uint32_t rx;            // frames received since AUTH_OK
} seal_t;

void seal_reset(seal_t *s);
// Derive the connection's keys from the pairing key the AUTH used, this
// connection's nonce and the HELLO payload the card sent, and turn sealing
// on.  The HELLO is in the key so that one edited on the way -- a capability
// taken out, a grid changed -- leaves the two ends with different keys, and
// the first sealed frame ends the connection.  It also carries the card's own
// fresh nonce (`cn=`), which is what keeps a replayed server from making the
// card reuse an old connection's keystream.
void seal_start(seal_t *s, const uint8_t *key, size_t key_len,
                const uint8_t nonce[PROTO_NONCE_SIZE],
                const uint8_t *hello, size_t hello_len);

// Seal one complete frame in place: `frame` holds `n` bytes and has room
// for `cap`.  The payload is encrypted, the header's length grows by
// SEAL_TAG, and the tag over the encrypted payload is appended.
// Returns the new length, or 0 for something that is not one frame or does
// not fit.
size_t seal_frame(seal_t *s, uint8_t *frame, size_t n, size_t cap);

// Check a received frame's tag (the last SEAL_TAG bytes of `payload`, whose
// wire length is `len`), and only then decrypt the rest into `plain`, which
// has room for len - SEAL_TAG bytes.  Returns that length, or -1 for a frame
// that is not genuine -- which ends the connection, with nothing of it
// decrypted.
int seal_open(seal_t *s, msg_type_t type, const uint8_t *payload, uint16_t len,
              uint8_t *plain);

#endif  // DSTERM_SEAL_H
