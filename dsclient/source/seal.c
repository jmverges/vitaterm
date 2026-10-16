#include "seal.h"

#include <string.h>

#include "chacha20.h"
#include "sha256.h"

// HMAC-SHA256 over several pieces, so a tag can cover direction, sequence,
// header and payload without copying the payload into one buffer first.
typedef struct {
    sha256_ctx inner;
    uint8_t opad[64];
} hmac_ctx;

static void hmac_begin(hmac_ctx *h, const uint8_t *key, size_t key_len)
{
    uint8_t k[64];
    memset(k, 0, sizeof(k));
    if (key_len > 64) sha256(key, key_len, k);
    else memcpy(k, key, key_len);
    uint8_t ipad[64];
    for (int i = 0; i < 64; i++) {
        ipad[i] = (uint8_t)(k[i] ^ 0x36);
        h->opad[i] = (uint8_t)(k[i] ^ 0x5c);
    }
    sha256_init(&h->inner);
    sha256_update(&h->inner, ipad, sizeof(ipad));
}

static void hmac_end(hmac_ctx *h, uint8_t out[SHA256_DIGEST_SIZE])
{
    uint8_t inner[SHA256_DIGEST_SIZE];
    sha256_final(&h->inner, inner);
    sha256_ctx outer;
    sha256_init(&outer);
    sha256_update(&outer, h->opad, sizeof(h->opad));
    sha256_update(&outer, inner, sizeof(inner));
    sha256_final(&outer, out);
}

void seal_reset(seal_t *s)
{
    if (!s) return;
    memset(s, 0, sizeof(*s));
}

static void hmac_label(const uint8_t base[SEAL_KEY], const char *label,
                       uint8_t out[SEAL_KEY])
{
    hmac_ctx h;
    hmac_begin(&h, base, SEAL_KEY);
    sha256_update(&h.inner, (const uint8_t *)label, strlen(label));
    hmac_end(&h, out);
}

void seal_start(seal_t *s, const uint8_t *key, size_t key_len,
                const uint8_t nonce[PROTO_NONCE_SIZE],
                const uint8_t *hello, size_t hello_len)
{
    static const char LABEL[] = "dsterm seal v3";
    uint8_t hello_hash[SHA256_DIGEST_SIZE];
    sha256(hello, hello_len, hello_hash);
    hmac_ctx h;
    hmac_begin(&h, key, key_len);
    sha256_update(&h.inner, (const uint8_t *)LABEL, sizeof(LABEL) - 1);
    sha256_update(&h.inner, nonce, PROTO_NONCE_SIZE);
    sha256_update(&h.inner, hello_hash, sizeof(hello_hash));
    uint8_t base[SEAL_KEY];
    hmac_end(&h, base);
    // One key for the tags and one cipher key per direction: the card's
    // frame 7 and the server's frame 7 share a sequence number, and must
    // never share a keystream.
    hmac_label(base, "mac", s->k_mac);
    hmac_label(base, "enc C", s->k_tx);
    hmac_label(base, "enc S", s->k_rx);
    memset(base, 0, sizeof(base));
    s->tx = 0;
    s->rx = 0;
    s->on = 1;
}

// The cipher's nonce is the frame's sequence number: eight zero bytes and
// the 32-bit count, big-endian.  Each direction has its own key and its own
// count from zero, so no (key, nonce) pair is ever used twice -- and the
// connection ends before a count could wrap (seal_frame, seal_open).
static void nonce_of(uint32_t seq, uint8_t out[CHACHA20_NONCE])
{
    memset(out, 0, CHACHA20_NONCE);
    out[8] = (uint8_t)(seq >> 24);
    out[9] = (uint8_t)(seq >> 16);
    out[10] = (uint8_t)(seq >> 8);
    out[11] = (uint8_t)seq;
}

static void tag_of(const seal_t *s, uint8_t dir, uint32_t seq,
                   const uint8_t header[PROTO_HEADER_SIZE],
                   const uint8_t *payload, size_t len, uint8_t out[SEAL_TAG])
{
    hmac_ctx h;
    hmac_begin(&h, s->k_mac, SEAL_KEY);
    const uint8_t pre[5] = {
        dir, (uint8_t)(seq >> 24), (uint8_t)(seq >> 16), (uint8_t)(seq >> 8),
        (uint8_t)seq,
    };
    sha256_update(&h.inner, pre, sizeof(pre));
    sha256_update(&h.inner, header, PROTO_HEADER_SIZE);
    if (len) sha256_update(&h.inner, payload, len);
    uint8_t full[SHA256_DIGEST_SIZE];
    hmac_end(&h, full);
    memcpy(out, full, SEAL_TAG);
}

size_t seal_frame(seal_t *s, uint8_t *frame, size_t n, size_t cap)
{
    if (!s || !frame || n < PROTO_HEADER_SIZE || n + SEAL_TAG > cap) return 0;
    if (s->tx == UINT32_MAX) return 0;   // a wrapped count would repeat a keystream
    const size_t payload_len = ((size_t)frame[4] << 8) | frame[5];
    if (payload_len + PROTO_HEADER_SIZE != n) return 0;   // not one frame
    const size_t wire = payload_len + SEAL_TAG;
    if (wire > PROTO_MAX_PAYLOAD) return 0;
    frame[4] = (uint8_t)(wire >> 8);
    frame[5] = (uint8_t)wire;
    // Encrypt, then tag what was encrypted: the receiver checks the tag
    // before it decrypts a byte.
    uint8_t iv[CHACHA20_NONCE];
    nonce_of(s->tx, iv);
    chacha20_xor(frame + PROTO_HEADER_SIZE, frame + PROTO_HEADER_SIZE,
                 payload_len, s->k_tx, iv, 0);
    tag_of(s, 'C', s->tx, frame, frame + PROTO_HEADER_SIZE, payload_len,
           frame + n);
    s->tx++;
    return n + SEAL_TAG;
}

int seal_open(seal_t *s, msg_type_t type, const uint8_t *payload, uint16_t len,
              uint8_t *plain)
{
    if (!s || len < SEAL_TAG || (!payload && len)) return -1;
    if (s->rx == UINT32_MAX) return -1;
    const size_t body = (size_t)len - SEAL_TAG;
    if (body && !plain) return -1;
    const uint8_t header[PROTO_HEADER_SIZE] = {
        PROTO_MAGIC0, PROTO_MAGIC1, PROTO_VERSION, (uint8_t)type,
        (uint8_t)(len >> 8), (uint8_t)len,
    };
    uint8_t want[SEAL_TAG];
    tag_of(s, 'S', s->rx, header, payload, body, want);
    if (!ct_equal(want, payload + body, SEAL_TAG)) return -1;
    uint8_t iv[CHACHA20_NONCE];
    nonce_of(s->rx, iv);
    if (body) chacha20_xor(plain, payload, body, s->k_rx, iv, 0);
    s->rx++;
    return (int)body;
}
