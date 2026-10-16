#include "entropy.h"

#include <string.h>

#include "sha256.h"

void entropy_init(entropy_t *e)
{
    memset(e, 0, sizeof(*e));
}

void entropy_add(entropy_t *e, const void *data, size_t len)
{
    sha256_ctx ctx;
    sha256_init(&ctx);
    sha256_update(&ctx, (const uint8_t *)"add", 3);
    sha256_update(&ctx, e->state, ENTROPY_SIZE);
    if (data && len) sha256_update(&ctx, (const uint8_t *)data, len);
    sha256_final(&ctx, e->state);
    e->samples++;
}

void entropy_add_u32(entropy_t *e, uint32_t value)
{
    const uint8_t b[4] = {
        (uint8_t)value, (uint8_t)(value >> 8),
        (uint8_t)(value >> 16), (uint8_t)(value >> 24),
    };
    entropy_add(e, b, sizeof(b));
}

void entropy_take(entropy_t *e, uint8_t out[ENTROPY_SIZE])
{
    e->taken++;
    const uint8_t counter[4] = {
        (uint8_t)e->taken, (uint8_t)(e->taken >> 8),
        (uint8_t)(e->taken >> 16), (uint8_t)(e->taken >> 24),
    };

    sha256_ctx ctx;
    sha256_init(&ctx);
    sha256_update(&ctx, (const uint8_t *)"out", 3);
    sha256_update(&ctx, e->state, ENTROPY_SIZE);
    sha256_update(&ctx, counter, sizeof(counter));
    sha256_final(&ctx, out);

    // Ratchet: the next state is a different hash of this one, so knowing an
    // output tells you nothing about what comes after it.
    sha256_init(&ctx);
    sha256_update(&ctx, (const uint8_t *)"next", 4);
    sha256_update(&ctx, e->state, ENTROPY_SIZE);
    sha256_update(&ctx, counter, sizeof(counter));
    sha256_final(&ctx, e->state);
}
