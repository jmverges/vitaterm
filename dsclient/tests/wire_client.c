// The card's protocol half against a real server, over a real socket.
//
//   wire_client PORT KEYHEX SECONDS [TEXT|camera]
//
// With TEXT "camera" it is the card's camera path instead: HELLO announces
// the feature, CAMERA_READY is required, then CAMERA 2, one preview frame,
// a SMALL picture uploaded through the real flow-control window, the saved
// path printed as "PHOTO <id> <path>", a beat so the harness can look at the
// file, then PHOTO_DISCARD and CAMERA 0.
//
// Every unit test of the client decodes payloads it built itself, and every
// interop check compares encodings byte for byte -- and neither noticed when
// the card's frame reader refused three new message types the server sends
// right after login, which on a console is a reconnect loop.  This closes
// that gap: it is main.c's connection path (HELLO, NONCE, AUTH, SEAL, the
// frame reader, every decoder, REMOTE_OPEN, PING) built from the same
// sources, run against `dsterm serve`.  tests/test_wire.py runs it.
//
// It prints one line per message type it saw ("SAW 0x91") and ends with "OK"
// and exit 0, or "FAIL <why>" and exit 1.  With TEXT, it types TEXT and a CR
// and requires TEXT to appear in the grid.
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include "../source/camera.h"
#include "../source/proto.h"
#include "../source/remote.h"
#include "../source/seal.h"
#include "../source/sessions.h"
#include "../source/sha256.h"

static int sock = -1;
static framereader_t reader;
static seal_t seal;
static grid_t grid;
static int saw[256];

static double now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static void fail(const char *why)
{
    printf("FAIL %s\n", why);
    exit(1);
}

// main.c's send_all, minus the frame_wait: one whole frame, sealed if on.
static void send_frame(const uint8_t *frame, size_t n)
{
    uint8_t buf[PROTO_HEADER_SIZE + PROTO_MAX_CARD_PAYLOAD + 64];
    if (n + SEAL_TAG > sizeof(buf)) fail("frame too large to send");
    memcpy(buf, frame, n);
    if (seal.on) {
        n = seal_frame(&seal, buf, n, sizeof(buf));
        if (!n) fail("could not seal a frame");
    }
    if (send(sock, buf, n, 0) != (ssize_t)n) fail("send failed");
}

// Next whole frame, with the tag checked and stripped when sealed.  0 on
// timeout.
static int next_frame(msg_type_t *type, const uint8_t **payload, uint16_t *len,
                      double until)
{
    for (;;) {
        const proto_result_t r = framereader_next(&reader, type, payload, len);
        if (r == PROTO_OK) {
            if (seal.on) {
                static uint8_t plain[PROTO_MAX_PAYLOAD];
                const int body = seal_open(&seal, *type, *payload, *len, plain);
                if (body < 0) fail("integrity check failed");
                *payload = plain;
                *len = (uint16_t)body;
            }
            saw[*type] = 1;
            return 1;
        }
        if (r != PROTO_NEED_MORE) {
            static char why[64];
            snprintf(why, sizeof(why), "protocol error: %s (type 0x%02X)",
                     proto_strerror(r),
                     reader.len > reader.pos + 3 ? reader.buf[reader.pos + 3] : 0);
            fail(why);
        }
        if (now() > until) return 0;
        uint8_t chunk[2048];
        const ssize_t got = recv(sock, chunk, sizeof(chunk), 0);
        if (got == 0) fail("server closed the connection");
        if (got < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) continue;
            fail("recv failed");
        }
        if ((size_t)got > framereader_space(&reader)) fail("receive overflow");
        framereader_feed(&reader, chunk, (size_t)got);
    }
}

// -------------------------------------------------------------- the camera ---
//
// The same upload the card runs: synth_yuyv() is the frame both ends agree
// on, camera_photo_pack / camera_upload_* are the card's real planner, and
// every ACK comes back through the same frame reader.

// The synthetic capture dsterm/camera.py's synth_yuyv_frame() writes:
// Y0 = 2p+3y, Cb = p+5y, Y1 = 2p+1+3y, Cr = 7p+y (all mod 256).
static void synth_yuyv(uint8_t yuyv[CAMERA_PHOTO_BYTES])
{
    for (int y = 0; y < CAMERA_PHOTO_H; y++) {
        for (int p = 0; p < CAMERA_PHOTO_W / 2; p++) {
            uint8_t *pair = yuyv + ((size_t)y * (CAMERA_PHOTO_W / 2) + p) * 4;
            pair[0] = (uint8_t)(p * 2 + y * 3);
            pair[1] = (uint8_t)(p + y * 5);
            pair[2] = (uint8_t)(p * 2 + 1 + y * 3);
            pair[3] = (uint8_t)(p * 7 + y);
        }
    }
}

// The run's steps: -1 off, 0 waiting for READY, 1 preview ACK pending, 2
// upload in flight, 3 PHOTO_SAVED pending, 4 holding the file for the
// harness, 5 discard sent -- done.
static int cam_step = -1;
static uint8_t cam_yuyv[CAMERA_PHOTO_BYTES];
static uint8_t cam_packed[CAMERA_PHOTO_SMALL_BYTES];
static camera_upload_t cam_up;
static uint32_t cam_crc = 0;
static uint16_t cam_saved_id = 0;
static double cam_hold_until = 0;

static int grid_contains(const char *text)
{
    const size_t n = strlen(text);
    for (int r = 0; r < PROTO_ROWS; r++) {
        char row[PROTO_COLS + 1];
        for (int c = 0; c < PROTO_COLS; c++) {
            row[c] = (char)grid.cells[r * PROTO_COLS + c].glyph;
        }
        row[PROTO_COLS] = '\0';
        if (n && strstr(row, text)) return 1;
    }
    return 0;
}

// READY landed: the outer camera on, then one preview frame in flight.
static void cam_on_ready(void)
{
    uint8_t out[PROTO_HEADER_SIZE + PROTO_CAMERA_FRAME_SIZE];
    send_frame(out, proto_encode_camera(out, PROTO_CAMERA_OUTER));
    static uint8_t px[PROTO_PREVIEW_BYTES];
    for (unsigned i = 0; i < PROTO_PREVIEW_BYTES; i++) {
        px[i] = (uint8_t)(i * 7 + (i >> 6));
    }
    send_frame(out, proto_encode_camera_frame(out, PROTO_PREVIEW_FORMAT_GRAY4, 0, 0, px));
    cam_step = 1;
}

static void cam_on_ack(uint8_t kind, uint32_t value)
{
    if (kind == PROTO_ACK_PREVIEW) {
        // The one preview is answered; the picture starts its window.
        if (cam_step != 1 || value != 0) return;
        synth_yuyv(cam_yuyv);
        camera_photo_pack(cam_yuyv, CAMERA_PHOTO_FORMAT_SMALL, cam_packed);
        cam_crc = camera_crc32_update(0, cam_packed, sizeof(cam_packed));
        camera_upload_init(&cam_up, CAMERA_PHOTO_FORMAT_SMALL);
        cam_step = 2;
        return;
    }
    // A photo credit: feed the planner; a bad credit is a wire fault.
    // Step 3 too: the chunks' last ACKs keep landing after PHOTO_END.
    if ((cam_step != 2 && cam_step != 3) ||
        !camera_upload_ack(&cam_up, value)) {
        static char why[80];
        snprintf(why, sizeof(why), "bad photo ack: %lu (sent %lu, acked %lu)",
                 (unsigned long)value, (unsigned long)cam_up.sent,
                 (unsigned long)cam_up.acked);
        fail(why);
    }
}

static void cam_on_saved(const photo_saved_t *saved)
{
    if (cam_step != 3) fail("PHOTO_SAVED out of turn");
    if (saved->status != PROTO_SAVED_OK) {
        static char why[64];
        snprintf(why, sizeof(why), "photo refused: status %u", saved->status);
        fail(why);
    }
    cam_saved_id = saved->id;
    printf("PHOTO %u %s\n", saved->id, saved->path);
    fflush(stdout);
    // The harness checks the file exists before the discard below removes
    // it: hold a beat so a polling test sees it.
    cam_hold_until = now() + 1.5;
    cam_step = 4;
}

// Send-side progress, called once a loop: chunks while the window allows,
// PHOTO_END when all are sent, the discard when the hold is up.
static void cam_pump(void)
{
    uint8_t out[PROTO_HEADER_SIZE + 4 + PROTO_PHOTO_CHUNK_MAX];
    if (cam_step == 2) {
        uint32_t off = 0, len = 0;
        while (camera_upload_next(&cam_up, &off, &len)) {
            send_frame(out, proto_encode_photo_chunk(
                           out, off, cam_packed + off, (uint16_t)len));
        }
        if (camera_upload_all_sent(&cam_up)) {
            send_frame(out, proto_encode_photo_end(
                           out, CAMERA_PHOTO_FORMAT_SMALL,
                           PROTO_CAMERA_OUTER, cam_crc));
            cam_step = 3;
        }
    } else if (cam_step == 4 && now() >= cam_hold_until) {
        send_frame(out, proto_encode_photo_discard(out, cam_saved_id));
        send_frame(out, proto_encode_camera(out, PROTO_CAMERA_OFF));
        cam_step = 5;
    }
}

int main(int argc, char **argv)
{
    if (argc < 4) fail("usage: wire_client PORT KEYHEX SECONDS [TEXT]");
    const int port = atoi(argv[1]);
    uint8_t key[16];
    for (int i = 0; i < 16; i++) {
        unsigned v;
        if (sscanf(argv[2] + 2 * i, "%2x", &v) != 1) fail("bad key");
        key[i] = (uint8_t)v;
    }
    const double seconds = atof(argv[3]);
    const char *text = argc > 4 ? argv[4] : NULL;

    sock = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) fail("connect");
    struct timeval tv = { 0, 100000 };
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    framereader_init(&reader);
    seal_reset(&seal);
    grid_clear(&grid);

    const int camera_mode = text && strcmp(text, "camera") == 0;

    // HELLO, exactly as the card builds it -- a DSi's carries the camera.
    // A fresh card nonce, as the card draws one per connection.
    uint8_t card_nonce[PROTO_CARD_NONCE_BYTES];
    FILE *urandom = fopen("/dev/urandom", "rb");
    if (!urandom || fread(card_nonce, 1, sizeof(card_nonce), urandom) !=
                        sizeof(card_nonce)) {
        fail("no /dev/urandom");
    }
    fclose(urandom);
    char caps[PROTO_HELLO_MAX + 1];
    const int cn = proto_hello_payload(caps, sizeof(caps), NULL, card_nonce, "",
                                       camera_mode ? 1 : 0);
    uint8_t out[PROTO_HANDSHAKE_BUF_SIZE];
    send_frame(out, proto_encode(out, MSG_HELLO, (const uint8_t *)caps,
                                 (uint16_t)cn));

    msg_type_t type;
    const uint8_t *payload;
    uint16_t len;
    if (!next_frame(&type, &payload, &len, now() + 5)) fail("no NONCE");
    if (type != MSG_NONCE || len != PROTO_NONCE_SIZE) fail("expected NONCE");
    uint8_t nonce[PROTO_NONCE_SIZE], mac[PROTO_MAC_SIZE];
    memcpy(nonce, payload, sizeof(nonce));
    hmac_sha256(key, sizeof(key), nonce, sizeof(nonce), mac);
    send_frame(out, proto_encode(out, MSG_AUTH, mac, sizeof(mac)));
    if (!next_frame(&type, &payload, &len, now() + 5)) fail("no AUTH_OK");
    if (type == MSG_ERROR) {
        static char why[96];
        snprintf(why, sizeof(why), "refused: %.*s", (int)len, (const char *)payload);
        fail(why);
    }
    if (type != MSG_AUTH_OK) fail("expected AUTH_OK");
    if (len != 5 || memcmp(payload, "seal3", 5) != 0) fail("AUTH_OK without seal3");
    seal_start(&seal, key, sizeof(key), nonce, (const uint8_t *)caps, (size_t)cn);
    printf("SEALED\n");

    static sessions_t sessions;
    static remote_list_t remote;
    sessions_init(&sessions);
    remote_init(&remote);
    int typed = 0, remote_opened = 0;
    cam_step = camera_mode ? 0 : -1;
    double next_ping = now();
    const double end = now() + seconds;
    while (now() < end) {
        if (now() >= next_ping) {
            send_frame(out, proto_encode_empty(out, MSG_PING));
            next_ping = now() + 1.0;
        }
        if (camera_mode) cam_pump();
        if (!next_frame(&type, &payload, &len, now() + 0.2)) continue;
        proto_result_t r = PROTO_OK;
        switch (type) {
        case MSG_FRAME_FULL: r = proto_apply_full(&grid, payload, len); break;
        case MSG_FRAME_DIFF: r = proto_apply_diff(&grid, payload, len); break;
        case MSG_SCROLL: {
            int lines = 0;
            r = proto_apply_scroll(&grid, payload, len, &lines);
            break;
        }
        case MSG_CURSOR: {
            cursor_t cur;
            r = proto_apply_cursor(&cur, payload, len);
            break;
        }
        case MSG_VIEW_POS: {
            view_pos_t pos;
            r = proto_apply_view_pos(&pos, payload, len);
            break;
        }
        case MSG_MODE: {
            uint8_t flags;
            r = proto_apply_mode(&flags, payload, len);
            break;
        }
        case MSG_SESSIONS: {
            session_chunk_t chunk;
            r = proto_apply_sessions(&chunk, payload, len);
            if (r == PROTO_OK && sessions_apply_chunk(&sessions, &chunk) < 0) {
                fail("bad SESSIONS set");
            }
            break;
        }
        case MSG_REMOTE: {
            remote_chunk_t chunk;
            r = proto_apply_remote(&chunk, payload, len);
            if (r == PROTO_OK) {
                if (remote_apply_chunk(&remote, &chunk) < 0) fail("bad REMOTE set");
                // The proof rule: only now may REMOTE_OPEN be sent.
                if (remote.ready && !remote_opened) {
                    send_frame(out, proto_encode_remote_open(out, 1));
                    remote_opened = 1;
                }
            }
            break;
        }
        case MSG_REMOTE_NOTE: {
            char note[REMOTE_NOTE_MAX + 1];
            r = proto_apply_remote_note(note, sizeof(note), payload, len);
            break;
        }
        case MSG_STATE: {
            uint8_t st;
            char detail[32];
            r = proto_apply_state(&st, detail, sizeof(detail), payload, len);
            break;
        }
        case MSG_CAMERA_READY: {
            int can_save = 0;
            r = proto_decode_camera_ready(&can_save, payload, len);
            if (r == PROTO_OK && cam_step == 0) cam_on_ready();
            break;
        }
        case MSG_CAMERA_ACK: {
            uint8_t kind = 0;
            uint32_t value = 0;
            r = proto_decode_camera_ack(&kind, &value, payload, len);
            if (r == PROTO_OK) cam_on_ack(kind, value);
            break;
        }
        case MSG_PHOTO_SAVED: {
            photo_saved_t saved;
            r = proto_decode_photo_saved(&saved, payload, len);
            if (r == PROTO_OK && cam_step == 3) cam_on_saved(&saved);
            break;
        }
        case MSG_ERROR: {
            static char why[96];
            snprintf(why, sizeof(why), "server error: %.*s", (int)len,
                     (const char *)payload);
            fail(why);
            break;
        }
        default:
            break;     // PONG, PROJECT, SNIPPETS, CLIP: framing is the test
        }
        if (r != PROTO_OK) {
            static char why[64];
            snprintf(why, sizeof(why), "bad 0x%02X: %s", type, proto_strerror(r));
            fail(why);
        }
        // Type once the screen has been painted, as a person would.
        if (text && !camera_mode && !typed &&
            (saw[MSG_FRAME_FULL] || saw[MSG_FRAME_DIFF])) {
            const size_t tn = strlen(text);
            for (size_t at = 0; at < tn; at += PROTO_MAX_KEY_PAYLOAD) {
                const size_t take = tn - at < PROTO_MAX_KEY_PAYLOAD
                                        ? tn - at : PROTO_MAX_KEY_PAYLOAD;
                send_frame(out, proto_encode(out, MSG_KEY,
                                             (const uint8_t *)text + at,
                                             (uint16_t)take));
            }
            send_frame(out, proto_encode(out, MSG_KEY, (const uint8_t *)"\r", 1));
            typed = 1;
        }
        // Done when everything the test checks for has arrived. Not at the
        // first echo: REMOTE and STATE are queued behind the repaint, and a
        // fast echo used to end the run before a slower machine sent them.
        if (text && typed && grid_contains(text) && saw[MSG_PONG]
            && saw[MSG_REMOTE] && saw[MSG_STATE]) {
            break;
        }
    }
    for (int t = 0; t < 256; t++) {
        if (saw[t]) printf("SAW 0x%02X\n", t);
    }
    if (camera_mode && cam_step != 5) fail("the camera run never finished");
    if (text && !camera_mode && !grid_contains(text)) {
        fail("the typed text never appeared");
    }
    printf("OK\n");
    return 0;
}
