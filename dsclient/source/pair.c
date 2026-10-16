#include "pair.h"

#include <string.h>

#include "sha256.h"

static const char INFO_LABEL[] = "dsterm pair v2";
static const char COMMIT_LABEL[] = "dsterm pair v2 commit";
static const char CODE_LABEL[] = "dsterm pair v2 code";
static const char ACCEPT_LABEL[] = "dsterm pair v2 accept ";

void pair_commitment(const uint8_t card_public[X25519_SIZE],
                     const uint8_t server_public[X25519_SIZE],
                     const uint8_t server_nonce[PAIR_NONCE_SIZE],
                     uint8_t out[PAIR_COMMIT_SIZE])
{
    sha256_ctx h;
    sha256_init(&h);
    sha256_update(&h, (const uint8_t *)COMMIT_LABEL, sizeof(COMMIT_LABEL) - 1);
    sha256_update(&h, card_public, X25519_SIZE);
    sha256_update(&h, server_public, X25519_SIZE);
    sha256_update(&h, server_nonce, PAIR_NONCE_SIZE);
    sha256_final(&h, out);
}

int pair_commitment_kept(const uint8_t card_public[X25519_SIZE],
                         const uint8_t server_public[X25519_SIZE],
                         const uint8_t *server_nonce, size_t nonce_len,
                         const uint8_t commit[PAIR_COMMIT_SIZE])
{
    if (!server_nonce || nonce_len != PAIR_NONCE_SIZE) return 0;
    uint8_t expected[PAIR_COMMIT_SIZE];
    pair_commitment(card_public, server_public, server_nonce, expected);
    return ct_equal(expected, commit, PAIR_COMMIT_SIZE);
}

uint32_t pair_code(const uint8_t card_public[X25519_SIZE],
                   const uint8_t server_public[X25519_SIZE],
                   const uint8_t card_nonce[PAIR_NONCE_SIZE],
                   const uint8_t server_nonce[PAIR_NONCE_SIZE])
{
    sha256_ctx h;
    sha256_init(&h);
    sha256_update(&h, (const uint8_t *)CODE_LABEL, sizeof(CODE_LABEL) - 1);
    sha256_update(&h, card_public, X25519_SIZE);
    sha256_update(&h, server_public, X25519_SIZE);
    sha256_update(&h, card_nonce, PAIR_NONCE_SIZE);
    sha256_update(&h, server_nonce, PAIR_NONCE_SIZE);
    uint8_t d[SHA256_DIGEST_SIZE];
    sha256_final(&h, d);
    const uint32_t v = (uint32_t)d[0] << 24 | (uint32_t)d[1] << 16 |
                       (uint32_t)d[2] << 8 | (uint32_t)d[3];
    return v % 1000000u;
}

int pair_derive(const uint8_t shared[X25519_SIZE],
                const uint8_t card_nonce[PAIR_NONCE_SIZE],
                const uint8_t server_nonce[PAIR_NONCE_SIZE],
                const uint8_t device_id[PROTO_DEVICE_ID_BYTES],
                const uint8_t card_public[X25519_SIZE],
                const uint8_t server_public[X25519_SIZE],
                uint8_t confirm_key[PAIR_CONFIRM_KEY_SIZE],
                uint8_t device_key[PAIR_KEY_SIZE])
{
    static const uint8_t zero[X25519_SIZE] = {0};
    if (ct_equal(shared, zero, X25519_SIZE)) return 0;

    uint8_t salt[2 * PAIR_NONCE_SIZE];
    memcpy(salt, card_nonce, PAIR_NONCE_SIZE);
    memcpy(salt + PAIR_NONCE_SIZE, server_nonce, PAIR_NONCE_SIZE);

    uint8_t info[sizeof(INFO_LABEL) - 1 + PROTO_DEVICE_ID_BYTES + 2 * X25519_SIZE];
    size_t n = 0;
    memcpy(info + n, INFO_LABEL, sizeof(INFO_LABEL) - 1);
    n += sizeof(INFO_LABEL) - 1;
    memcpy(info + n, device_id, PROTO_DEVICE_ID_BYTES);
    n += PROTO_DEVICE_ID_BYTES;
    memcpy(info + n, card_public, X25519_SIZE);
    n += X25519_SIZE;
    memcpy(info + n, server_public, X25519_SIZE);
    n += X25519_SIZE;

    uint8_t okm[PAIR_CONFIRM_KEY_SIZE + PAIR_KEY_SIZE];
    if (!hkdf_sha256(salt, sizeof(salt), shared, X25519_SIZE, info, n,
                     okm, sizeof(okm))) {
        return 0;
    }
    memcpy(confirm_key, okm, PAIR_CONFIRM_KEY_SIZE);
    memcpy(device_key, okm + PAIR_CONFIRM_KEY_SIZE, PAIR_KEY_SIZE);
    memset(okm, 0, sizeof(okm));
    return 1;
}

void pair_accept_mac(const uint8_t confirm_key[PAIR_CONFIRM_KEY_SIZE],
                     pair_role_t role,
                     const uint8_t card_public[X25519_SIZE],
                     const uint8_t server_public[X25519_SIZE],
                     uint8_t out[PAIR_MAC_SIZE])
{
    const char *who = role == PAIR_ROLE_SERVER ? "srv" : "ds";
    const size_t who_len = strlen(who);
    uint8_t msg[sizeof(ACCEPT_LABEL) - 1 + 3 + 2 * X25519_SIZE];
    size_t n = 0;
    memcpy(msg + n, ACCEPT_LABEL, sizeof(ACCEPT_LABEL) - 1);
    n += sizeof(ACCEPT_LABEL) - 1;
    memcpy(msg + n, who, who_len);
    n += who_len;
    memcpy(msg + n, card_public, X25519_SIZE);
    n += X25519_SIZE;
    memcpy(msg + n, server_public, X25519_SIZE);
    n += X25519_SIZE;
    hmac_sha256(confirm_key, PAIR_CONFIRM_KEY_SIZE, msg, n, out);
}

int pair_server_done(const uint8_t confirm_key[PAIR_CONFIRM_KEY_SIZE],
                     const uint8_t card_public[X25519_SIZE],
                     const uint8_t server_public[X25519_SIZE],
                     const uint8_t *payload, size_t len)
{
    if (!payload || len != PAIR_MAC_SIZE) return 0;
    uint8_t expected[PAIR_MAC_SIZE];
    pair_accept_mac(confirm_key, PAIR_ROLE_SERVER, card_public, server_public,
                    expected);
    return ct_equal(expected, payload, PAIR_MAC_SIZE);
}
