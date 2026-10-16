// An entropy pool for the one secret this console ever makes: the private key
// it uses for a pairing.
//
// A DS has no random number generator.  What it has is a person: when they
// press a button, how long they hold it, and above all the raw touchscreen
// ADC readings while their finger is down, whose low bits are analogue noise.
// The boot menu feeds every one of those in, plus the RTC, the Wi-Fi MAC and
// the scan results (signal strengths drift), and by the time somebody has
// found a server and picked it to pair with, the pool has been stirred by
// hundreds of samples.
//
// The pool is a SHA-256 chain: adding hashes the input into the state, and
// taking hashes the state with a counter and then ratchets it forward, so an
// output never reveals the state that produced the next one.
//
// Pure C.  What to feed it is boot.c's business.
#ifndef DSTERM_ENTROPY_H
#define DSTERM_ENTROPY_H

#include <stddef.h>
#include <stdint.h>

#define ENTROPY_SIZE 32

typedef struct {
    uint8_t state[ENTROPY_SIZE];
    uint32_t samples;     // how many add() calls; a rough gauge, not a measure
    uint32_t taken;
} entropy_t;

void entropy_init(entropy_t *e);
void entropy_add(entropy_t *e, const void *data, size_t len);
void entropy_add_u32(entropy_t *e, uint32_t value);

// 32 bytes derived from everything added so far.  Each call returns
// different bytes, and the state moves on afterwards.
void entropy_take(entropy_t *e, uint8_t out[ENTROPY_SIZE]);

#endif  // DSTERM_ENTROPY_H
