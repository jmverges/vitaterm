// SEAL on the card: every payload is encrypted with ChaCha20 under the
// direction's own key, the tag is HMAC-SHA256 over exactly the bytes
// docs/SEAL.md names (the ciphertext among them), and anything other than the
// server's next genuine frame is refused -- before a byte of it is decrypted.
#include <stdio.h>
#include <string.h>

#include "check.h"
#include "support.h"

#include "../source/chacha20.h"
#include "../source/seal.h"
#include "../source/sha256.h"

static const char HELLO[] = "ds-term/1 mode seal seal3 grid=42x24 id=deadbeef "
                            "cn=00112233445566778899aabbccddeeff";

static void fixture(seal_t *s)
{
    uint8_t key[16], nonce[PROTO_NONCE_SIZE];
    for (int i = 0; i < 16; i++) key[i] = (uint8_t)i;
    for (int i = 0; i < PROTO_NONCE_SIZE; i++) nonce[i] = (uint8_t)(0xF0 - i);
    seal_reset(s);
    seal_start(s, key, sizeof(key), nonce, (const uint8_t *)HELLO, sizeof(HELLO) - 1);
}

// The tag recomputed the long way: one buffer, one-shot HMAC.
static void reference_tag(const seal_t *s, uint8_t dir, uint32_t seq,
                          const uint8_t *frame, size_t n, uint8_t out[SEAL_TAG])
{
    uint8_t msg[256];
    msg[0] = dir;
    msg[1] = (uint8_t)(seq >> 24); msg[2] = (uint8_t)(seq >> 16);
    msg[3] = (uint8_t)(seq >> 8);  msg[4] = (uint8_t)seq;
    memcpy(msg + 5, frame, n);
    uint8_t full[32];
    hmac_sha256(s->k_mac, SEAL_KEY, msg, 5 + n, full);
    memcpy(out, full, SEAL_TAG);
}

static void nonce_for(uint32_t seq, uint8_t iv[CHACHA20_NONCE])
{
    memset(iv, 0, CHACHA20_NONCE);
    iv[8] = (uint8_t)(seq >> 24); iv[9] = (uint8_t)(seq >> 16);
    iv[10] = (uint8_t)(seq >> 8); iv[11] = (uint8_t)seq;
}

static void test_seal_key_and_tag(void)
{
    seal_t s;
    fixture(&s);
    CHECK(s.on && s.tx == 0 && s.rx == 0, "seal_start did not start");
    // base = HMAC(key, "dsterm seal v3" || nonce || SHA-256(hello)), then one
    // HMAC of the base per key.
    uint8_t key[16], msg[14 + PROTO_NONCE_SIZE + 32], base[32], k[32];
    for (int i = 0; i < 16; i++) key[i] = (uint8_t)i;
    memcpy(msg, "dsterm seal v3", 14);
    for (int i = 0; i < PROTO_NONCE_SIZE; i++) msg[14 + i] = (uint8_t)(0xF0 - i);
    sha256((const uint8_t *)HELLO, sizeof(HELLO) - 1, msg + 14 + PROTO_NONCE_SIZE);
    hmac_sha256(key, 16, msg, sizeof(msg), base);
    hmac_sha256(base, 32, (const uint8_t *)"mac", 3, k);
    CHECK(memcmp(k, s.k_mac, 32) == 0, "the tag key is not HMAC(base, \"mac\")");
    hmac_sha256(base, 32, (const uint8_t *)"enc C", 5, k);
    CHECK(memcmp(k, s.k_tx, 32) == 0, "the card's key is not HMAC(base, \"enc C\")");
    hmac_sha256(base, 32, (const uint8_t *)"enc S", 5, k);
    CHECK(memcmp(k, s.k_rx, 32) == 0, "the server's key is not HMAC(base, \"enc S\")");
    CHECK(memcmp(s.k_tx, s.k_rx, 32) != 0, "both directions share a cipher key");

    // One byte of the HELLO changed on the way is a different key -- and so
    // is a different card nonce, which is the replay defence.
    seal_t other;
    uint8_t nonce[PROTO_NONCE_SIZE];
    for (int i = 0; i < PROTO_NONCE_SIZE; i++) nonce[i] = (uint8_t)(0xF0 - i);
    char edited[sizeof(HELLO)];
    memcpy(edited, HELLO, sizeof(HELLO));
    edited[sizeof("ds-term/1 mode ") - 1] = 'X';
    seal_start(&other, key, sizeof(key), nonce, (const uint8_t *)edited,
               sizeof(edited) - 1);
    CHECK(memcmp(other.k_mac, s.k_mac, 32) != 0, "an edited HELLO gave the same key");
    memcpy(edited, HELLO, sizeof(HELLO));
    edited[sizeof(HELLO) - 2] ^= 1;   // the last hex digit of the card nonce
    seal_start(&other, key, sizeof(key), nonce, (const uint8_t *)edited,
               sizeof(edited) - 1);
    CHECK(memcmp(other.k_tx, s.k_tx, 32) != 0 && memcmp(other.k_rx, s.k_rx, 32) != 0,
          "a new card nonce under a replayed server nonce gave the same keys");

    uint8_t frame[64];
    size_t n = proto_encode(frame, MSG_KEY, (const uint8_t *)"a", 1);
    const size_t sealed = seal_frame(&s, frame, n, sizeof(frame));
    CHECK(sealed == n + SEAL_TAG && frame[5] == 1 + SEAL_TAG && s.tx == 1,
          "a sealed frame is not one tag longer, with its length saying so");
    uint8_t iv[CHACHA20_NONCE], ks[CHACHA20_BLOCK];
    nonce_for(0, iv);
    chacha20_block(ks, s.k_tx, iv, 0);
    CHECK(frame[PROTO_HEADER_SIZE] == (uint8_t)('a' ^ ks[0]),
          "the payload is not 'a' XOR the card's keystream at seq 0");
    uint8_t want[SEAL_TAG];
    reference_tag(&s, 'C', 0, frame, n, want);
    CHECK(memcmp(frame + n, want, SEAL_TAG) == 0,
          "the tag is not HMAC(k_mac, 'C' || seq || header || ciphertext)");
    CHECK(seal_frame(&s, frame, n, n + SEAL_TAG - 1) == 0,
          "a frame was sealed past the end of its buffer");
    CHECK(seal_frame(&s, frame, n - 1, sizeof(frame)) == 0,
          "a partial frame was sealed");

    // The count never wraps: the last value is refused both ways, so no
    // keystream can come round again.
    s.tx = UINT32_MAX;
    n = proto_encode(frame, MSG_PING, NULL, 0);
    CHECK(seal_frame(&s, frame, n, sizeof(frame)) == 0, "a wrapping count sealed");
}

// Builds what the server would send: the body encrypted under the server's
// key at `seq`, then 'S', seq and the tag over the ciphertext.
static size_t server_frame(const seal_t *s, uint32_t seq, msg_type_t type,
                           const char *body, uint8_t *out)
{
    const size_t blen = strlen(body);
    const size_t n = proto_encode(out, type, (const uint8_t *)body, (uint16_t)blen);
    out[4] = (uint8_t)((blen + SEAL_TAG) >> 8);
    out[5] = (uint8_t)(blen + SEAL_TAG);
    uint8_t iv[CHACHA20_NONCE];
    nonce_for(seq, iv);
    chacha20_xor(out + PROTO_HEADER_SIZE, out + PROTO_HEADER_SIZE, blen,
                 s->k_rx, iv, 0);
    reference_tag(s, 'S', seq, out, n, out + n);
    return n + SEAL_TAG;
}

static void test_seal_refuses_what_is_not_genuine(void)
{
    seal_t s;
    fixture(&s);
    uint8_t f[64], plain[64];
    size_t n = server_frame(&s, 0, MSG_PONG, "", f);
    CHECK(seal_open(&s, MSG_PONG, f + PROTO_HEADER_SIZE,
                    (uint16_t)(n - PROTO_HEADER_SIZE), plain) == 0 && s.rx == 1,
          "a genuine empty frame was refused");
    n = server_frame(&s, 1, MSG_ERROR, "hi", f);
    CHECK(seal_open(&s, MSG_ERROR, f + 6, (uint16_t)(n - 6), plain) == 2 &&
              memcmp(plain, "hi", 2) == 0,
          "a genuine frame did not come back as its two bytes");
    // Replayed (seq 1 again), reordered (seq 3 before 2), wrong type,
    // tampered payload, tampered tag, and a frame too short to hold a tag.
    CHECK(seal_open(&s, MSG_ERROR, f + 6, (uint16_t)(n - 6), plain) < 0, "a replay passed");
    n = server_frame(&s, 3, MSG_ERROR, "hi", f);
    CHECK(seal_open(&s, MSG_ERROR, f + 6, (uint16_t)(n - 6), plain) < 0, "a reorder passed");
    n = server_frame(&s, 2, MSG_ERROR, "hi", f);
    CHECK(seal_open(&s, MSG_KEY, f + 6, (uint16_t)(n - 6), plain) < 0,
          "a frame passed under another type");
    f[6] ^= 1;
    CHECK(seal_open(&s, MSG_ERROR, f + 6, (uint16_t)(n - 6), plain) < 0,
          "a tampered payload passed");
    f[6] ^= 1;
    f[n - 1] ^= 0x80;
    CHECK(seal_open(&s, MSG_ERROR, f + 6, (uint16_t)(n - 6), plain) < 0,
          "a tampered tag passed");
    f[n - 1] ^= 0x80;
    CHECK(seal_open(&s, MSG_ERROR, f + 6, (uint16_t)(n - 6), plain) == 2 && s.rx == 3,
          "the genuine frame was refused after the forgeries");
    CHECK(seal_open(&s, MSG_PONG, f + 6, SEAL_TAG - 1, plain) < 0,
          "a frame shorter than a tag passed");
    // A frame the card itself sent ('C') cannot be reflected back at it.
    uint8_t mine[64];
    const size_t mn = proto_encode(mine, MSG_PONG, NULL, 0);
    seal_t me;
    fixture(&me);
    me.tx = s.rx;
    const size_t ms = seal_frame(&me, mine, mn, sizeof(mine));
    CHECK(seal_open(&s, MSG_PONG, mine + 6, (uint16_t)(ms - 6), plain) < 0,
          "the card's own frame was accepted as the server's");
}

// The Python server's own outputs for the same key and nonce
// (tests/test_seal.py): the two halves agree byte for byte.
static void unhex(const char *hex, uint8_t *out, size_t n)
{
    for (size_t i = 0; i < n; i++) {
        unsigned v = 0;
        sscanf(hex + 2 * i, "%2x", &v);
        out[i] = (uint8_t)v;
    }
}

static void test_seal_matches_the_server(void)
{
    seal_t s;
    fixture(&s);

    uint8_t frame[32], want[15];
    const size_t n = proto_encode(frame, MSG_KEY, (const uint8_t *)"a", 1);
    const size_t sealed = seal_frame(&s, frame, n, sizeof(frame));
    unhex("445301100009e17bcd5f81a8043f30", want, 15);
    CHECK(sealed == 15 && memcmp(frame, want, 15) == 0,
          "a sealed KEY frame differs from the server's expectation");

    // The server's PONG at sequence 5 and its ERROR "hi" at 6 open on a card
    // that has read five, and the ERROR decrypts to "hi".
    uint8_t pong[14], err[16], plain[8];
    unhex("445301930008f44621ea9341ac14", pong, 14);
    unhex("445301ff000abe72b8c5680c2d9df4ad", err, 16);
    s.rx = 5;
    CHECK(seal_open(&s, MSG_PONG, pong + 6, 8, plain) == 0,
          "the server's sealed PONG did not open");
    CHECK(seal_open(&s, MSG_ERROR, err + 6, 10, plain) == 2 &&
              memcmp(plain, "hi", 2) == 0,
          "the server's sealed ERROR did not open to \"hi\"");
}

// RFC 8439 sections 2.3.2 and 2.4.2, the cipher's own vectors.
static void test_chacha20_vectors(void)
{
    uint8_t key[32], iv[12], out[64], want[64];
    for (int i = 0; i < 32; i++) key[i] = (uint8_t)i;
    unhex("000000090000004a00000000", iv, 12);
    chacha20_block(out, key, iv, 1);
    unhex("10f1e7e4d13b5915500fdd1fa32071c4c7d1f4c733c068030422aa9ac3d46c4e"
          "d2826446079faa0914c2d705d98b02a2b5129cd1de164eb9cbd083e8a2503c4e",
          want, 64);
    CHECK(memcmp(out, want, 64) == 0, "ChaCha20 block differs from RFC 8439 2.3.2");

    static const char PT[] = "Ladies and Gentlemen of the class of '99: If I could "
                             "offer you only one tip for the future, sunscreen "
                             "would be it.";
    uint8_t ct[sizeof(PT) - 1], ref[sizeof(PT) - 1];
    unhex("000000000000004a00000000", iv, 12);
    chacha20_xor(ct, (const uint8_t *)PT, sizeof(ct), key, iv, 1);
    unhex("6e2e359a2568f98041ba0728dd0d6981e97e7aec1d4360c20a27afccfd9fae0b"
          "f91b65c5524733ab8f593dabcd62b3571639d624e65152ab8f530c359f0861d8"
          "07ca0dbf500d6a6156a38e088a22b65e52bc514d16ccf806818ce91ab7793736"
          "5af90bbf74a35be6b40b8eedf2785e42874d", ref, sizeof(ref));
    CHECK(memcmp(ct, ref, sizeof(ct)) == 0,
          "ChaCha20 encryption differs from RFC 8439 2.4.2");
    chacha20_xor(ct, ct, sizeof(ct), key, iv, 1);
    CHECK(memcmp(ct, PT, sizeof(ct)) == 0, "ChaCha20 in place did not round-trip");
}

static void print_hex(const char *name, const uint8_t *in, size_t n)
{
    printf("INTEROP %s ", name);
    for (size_t i = 0; i < n; i++) printf("%02x", in[i]);
    printf("\n");
}

// For tools/crosscheck.py: the cipher over every awkward length (a hash of
// all of it, so a drift anywhere shows), and a large card frame sealed at
// seq 3, which the server must produce byte for byte.
static void print_interop(void)
{
    uint8_t key[CHACHA20_KEY], iv[CHACHA20_NONCE];
    for (int i = 0; i < CHACHA20_KEY; i++) key[i] = (uint8_t)(0xA0 ^ i);
    for (int i = 0; i < CHACHA20_NONCE; i++) iv[i] = (uint8_t)(3 * i + 1);
    static const size_t LENS[] = { 0, 1, 63, 64, 65, 536, 1024, 6147 };
    static uint8_t buf[6147];
    sha256_ctx h;
    sha256_init(&h);
    for (size_t l = 0; l < sizeof(LENS) / sizeof(LENS[0]); l++) {
        for (size_t i = 0; i < LENS[l]; i++) buf[i] = (uint8_t)(i * 7 + l);
        chacha20_xor(buf, buf, LENS[l], key, iv, (uint32_t)l);
        sha256_update(&h, buf, LENS[l]);
    }
    uint8_t digest[SHA256_DIGEST_SIZE];
    sha256_final(&h, digest);
    print_hex("chacha", digest, sizeof(digest));

    seal_t s;
    fixture(&s);
    s.tx = 3;
    static uint8_t frame[PROTO_HEADER_SIZE + 1000 + SEAL_TAG];
    uint8_t body[1000];
    for (int i = 0; i < 1000; i++) body[i] = (uint8_t)(i * 13);
    const size_t n = proto_encode(frame, MSG_KEY, body, sizeof(body));
    const size_t sealed = seal_frame(&s, frame, n, sizeof(frame));
    sha256(frame, sealed, digest);
    print_hex("seal-big", digest, sizeof(digest));
}

void test_seal_all(void)
{
    print_interop();
    test_seal_matches_the_server();
    printf("== seal ==\n");
    test_chacha20_vectors();
    test_seal_key_and_tag();
    test_seal_refuses_what_is_not_genuine();
}
