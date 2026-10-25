// The connection to the computer: connect, prove the key, keep the seal,
// apply what arrives, and reconnect for as long as the app runs.
//
// The DS client does this with blocking loops (main.c); here it is a state
// machine stepped once a frame, so the screen, the sound and the buttons
// never wait on the network.
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>

#include "app.h"
#include "backoff.h"
#include "chime.h"
#include "ext.h"
#include "seal.h"
#include "sessions.h"
#include "sha256.h"

link_info_t conn;
dsterm_config_t cfg;
grid_t grid;
remote_list_t remote;
quick_commands_t commands;
snippets_t snippets;
unsigned photo_pending_id;

typedef enum {
    STEP_IDLE = 0,      // not started, or waiting out the backoff
    STEP_CONNECT,       // TCP connect in progress, waiting for NONCE
    STEP_AUTH,          // HELLO and AUTH sent, waiting for AUTH_OK
    STEP_UP,
} step_t;

static step_t step;
static int sock = -1;
static framereader_t reader;
static seal_t seal;
static unsigned long step_at;       // frame the step started
static unsigned long retry_at;      // frame to try again at
static int started;

static char caps[PROTO_HELLO_MAX + 1];
static int caps_len;
static uint8_t nonce[PROTO_NONCE_SIZE];

static int ask_known, ask_open;
static uint32_t ask_id;
static sessions_t sessions;
static int remote_shown;

static unsigned long probe_sent_us;
static int probe_outstanding;
static unsigned long last_ping_frame;

void link_status(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(conn.status, sizeof(conn.status), fmt, ap);
    va_end(ap);
    conn.status_at = frame_count();
}

static void close_socket(void)
{
    if (sock >= 0) close(sock);
    sock = -1;
}

// Everything that belonged to the connection that ended (main.c's
// connection_forget): the next server proves its capabilities again.
static void forget(void)
{
    close_socket();
    framereader_init(&reader);
    seal_reset(&seal);
    sessions_init(&sessions);
    remote_init(&remote);
    quick_commands_init(&commands);
    chime_claude_reset();
    ask_known = ask_open = 0;
    probe_outstanding = 0;
    conn.rtt_known = 0;
    conn.app_scrolls = conn.app_scrolls_known = 0;
    conn.lines_back = conn.lines_available = 0;
    conn.camera_ready = conn.camera_can_save = 0;
    conn.claude_state = PROTO_STATE_UNKNOWN;
    conn.claude_detail[0] = '\0';
    photo_pending_id = 0;
    cam_forget();
}

static void fail(const char *why)
{
    const int was_up = step == STEP_UP;
    forget();
    conn.attempt++;
    if (was_up) {
        chime_link_changed(0);
        conn.attempt = 1;
    }
    conn.state = LINK_CONNECTING;
    step = STEP_IDLE;
    retry_at = frame_count() + backoff_frames(conn.attempt);
    link_status("%s", why);
}

void link_start(const char *name)
{
    snprintf(conn.server_name, sizeof(conn.server_name), "%s", name);
    conn.refusal[0] = '\0';
    conn.attempt = 0;
    forget();
    grid_clear(&grid);
    term_all(&grid);
    started = 1;
    step = STEP_IDLE;
    retry_at = frame_count();
    conn.state = LINK_CONNECTING;
}

void link_stop(void)
{
    if (step == STEP_UP) chime_link_changed(0);
    forget();
    started = 0;
    step = STEP_IDLE;
    conn.state = LINK_DOWN;
}

// ------------------------------------------------------------- sending ---

static int send_raw(const uint8_t *buf, size_t len)
{
    size_t sent = 0;
    int stalls = 0;
    while (sent < len) {
        const int n = send(sock, buf + sent, len - sent, 0);
        if (n > 0) {
            sent += (size_t)n;
            stalls = 0;
            continue;
        }
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) && ++stalls < 200) {
            usleep(5000);       // a second at most, then the link is gone
            continue;
        }
        return 0;
    }
    return 1;
}

int link_send_frame(const uint8_t *frame, size_t n)
{
    if (sock < 0) return 0;
    static uint8_t sealed[PROTO_HEADER_SIZE + PROTO_MAX_CARD_PAYLOAD + SEAL_TAG];
    if (seal.on) {
        if (n + SEAL_TAG > sizeof(sealed)) return 0;
        memcpy(sealed, frame, n);
        n = seal_frame(&seal, sealed, n, sizeof(sealed));
        if (!n) return 0;
        frame = sealed;
    }
    if (send_raw(frame, n)) return 1;
    fail("send failed; reconnecting");
    return 0;
}

int link_key(const char *seq, size_t n)
{
    if (step != STEP_UP || n == 0 || n > PROTO_MAX_KEY_PAYLOAD) return 0;
    static uint8_t out[PROTO_HEADER_SIZE + PROTO_ANSWER_MAX];
    size_t len;
    // Enter while Claude asks: tied to that prompt, so it reaches it or
    // nothing (docs/PROTOCOL.md, "Answers").
    if (ask_known && ask_open && memchr(seq, '\r', n)) {
        len = proto_encode_answer(out, ask_id, (const uint8_t *)seq, n);
    } else {
        len = proto_encode(out, MSG_KEY, (const uint8_t *)seq, (uint16_t)n);
    }
    if (!len || !link_send_frame(out, len)) return 0;
    if (n == 1 && seq[0] == '\r') chime_sent();
    return 1;
}

int link_text(const char *text, size_t n)
{
    size_t at = 0;
    while (at < n) {
        size_t take = n - at;
        if (take > PROTO_MAX_KEY_PAYLOAD) take = PROTO_MAX_KEY_PAYLOAD;
        if (!link_key(text + at, take)) return 0;
        at += take;
    }
    return 1;
}

static void send_view(int lines)
{
    if (step != STEP_UP) return;
    if (lines > PROTO_VIEW_MAX_DELTA) lines = PROTO_VIEW_MAX_DELTA;
    if (lines < -PROTO_VIEW_MAX_DELTA) lines = -PROTO_VIEW_MAX_DELTA;
    chime_view_sent();
    const uint16_t wire = (uint16_t)(int16_t)lines;
    const uint8_t payload[2] = { (uint8_t)(wire >> 8), (uint8_t)wire };
    uint8_t out[PROTO_HEADER_SIZE + 2];
    link_send_frame(out, proto_encode(out, MSG_VIEW, payload, 2));
}

void link_scroll(int lines)
{
    if (!conn.app_scrolls_known) return;
    if (conn.app_scrolls) {
        // The application owns its scrollback: PageUp/PageDown.
        link_key(lines < 0 ? "\x1b[5~" : "\x1b[6~", 4);
        return;
    }
    send_view(lines);
}

void link_live(void)
{
    if (conn.lines_back) send_view(PROTO_VIEW_MAX_DELTA);
}

void link_remote_open(int open)
{
    remote_shown = open;
    if (step != STEP_UP || !remote.ready) return;
    uint8_t out[PROTO_HEADER_SIZE + 1];
    link_send_frame(out, proto_encode_remote_open(out, open));
}

void link_remote_pick(unsigned index, int action)
{
    if (step != STEP_UP || !remote.ready) return;
    uint8_t out[PROTO_HEADER_SIZE + 4];
    uint16_t check = 0;
    uint8_t wire_index = REMOTE_PICK_NEW_INDEX;
    if (action != REMOTE_PICK_NEW && index < remote.count) {
        check = remote_row_check(&remote.items[index]);
        wire_index = remote.items[index].index;
    }
    link_send_frame(out, proto_encode_remote_pick(out, wire_index, (uint8_t)action, check));
}

void link_photo_discard(void)
{
    const unsigned id = photo_pending_id;
    photo_pending_id = 0;
    if (!id || !conn.camera_ready || step != STEP_UP) return;
    uint8_t out[PROTO_HEADER_SIZE + 2];
    link_send_frame(out, proto_encode_photo_discard(out, (uint16_t)id));
}

// ------------------------------------------------------------ messages ---

static void handle(msg_type_t type, const uint8_t *payload, uint16_t len)
{
    proto_result_t r;
    switch (type) {
    case MSG_FRAME_FULL:
        if (proto_apply_full(&grid, payload, len) == PROTO_OK) {
            term_all(&grid);
            chime_cells(len ? payload[0] : ' ');
        }
        break;
    case MSG_FRAME_DIFF:
        if (proto_apply_diff(&grid, payload, len) != PROTO_OK) {
            link_status("bad diff");
            break;
        }
        for (uint16_t i = 0; i < len / PROTO_DIFF_ENTRY; i++) {
            const uint8_t *p = payload + (size_t)i * PROTO_DIFF_ENTRY;
            term_cell(p[0], p[1], grid.cells[(size_t)p[0] * PROTO_COLS + p[1]]);
        }
        chime_cells(len >= PROTO_DIFF_ENTRY ? payload[2] : ' ');
        break;
    case MSG_SCROLL: {
        int lines = 0;
        if (proto_apply_scroll(&grid, payload, len, &lines) == PROTO_OK) {
            term_all(&grid);
            chime_cells(' ');
        }
        break;
    }
    case MSG_CURSOR: {
        cursor_t cur;
        if (proto_apply_cursor(&cur, payload, len) == PROTO_OK) {
            term_cursor(cur.row, cur.col, cur.visible);
        }
        break;
    }
    case MSG_PONG:
        if (probe_outstanding) {
            probe_outstanding = 0;
            conn.rtt_ms = (unsigned)((now_us() - probe_sent_us) / 1000UL);
            conn.rtt_known = 1;
        }
        break;
    case MSG_VIEW_POS: {
        view_pos_t pos;
        if (proto_apply_view_pos(&pos, payload, len) == PROTO_OK) {
            conn.lines_back = pos.lines_back;
            conn.lines_available = pos.available;
            chime_history(pos.lines_back > 0);
        }
        break;
    }
    case MSG_MODE: {
        uint8_t flags = 0;
        if (proto_apply_mode(&flags, payload, len) == PROTO_OK) {
            conn.app_scrolls = flags & PROTO_MODE_APP_SCROLLS;
            conn.app_scrolls_known = 1;
        }
        break;
    }
    case MSG_SESSIONS: {
        session_chunk_t chunk;
        if (proto_apply_sessions(&chunk, payload, len) == PROTO_OK &&
            sessions_apply_chunk(&sessions, &chunk) > 0 &&
            sessions_take_new_output_mask(&sessions)) {
            chime_session_lantern();
        }
        break;
    }
    case MSG_COMMANDS:
        (void)quick_commands_apply(&commands, payload, len);
        break;
    case MSG_SNIPPETS: {
        char label[SNIPPET_LABEL_MAX + 1];
        char body[SNIPPET_BODY_MAX + 1];
        size_t label_len = 0, body_len = 0;
        unsigned index = 0, total = 0;
        if (proto_apply_snippet(&index, &total, label, sizeof(label), &label_len,
                                body, sizeof(body), &body_len, payload, len) == PROTO_OK) {
            (void)snippets_apply_pushed(&snippets, index, total, label, label_len,
                                        body, body_len);
        }
        break;
    }
    case MSG_REMOTE: {
        remote_chunk_t chunk;
        if (proto_apply_remote(&chunk, payload, len) != PROTO_OK) break;
        const int was_ready = remote.ready;
        if (remote_apply_chunk(&remote, &chunk) > 0 && !was_ready && remote_shown) {
            link_remote_open(1);
        }
        break;
    }
    case MSG_REMOTE_NOTE: {
        char note[REMOTE_NOTE_MAX + 1];
        if (proto_apply_remote_note(note, sizeof(note), payload, len) == PROTO_OK) {
            remote_set_note(&remote, note, strlen(note));
            link_status("%s", note);
        }
        break;
    }
    case MSG_STATE: {
        uint8_t st = 0, flags = 0;
        int has_ask = 0;
        uint32_t id = 0;
        if (proto_apply_state_ask(&st, conn.claude_detail, sizeof(conn.claude_detail),
                                  &has_ask, &id, &flags, payload, len) == PROTO_OK) {
            if (has_ask) {
                ask_known = 1;
                ask_id = id;
                ask_open = st == PROTO_STATE_APPROVAL;
                if (flags & PROTO_ASK_FLAG_DROPPED) {
                    link_status("not sent: the screen changed. Look, then press again");
                }
            }
            conn.claude_state = st;
            chime_claude_state(st);
        }
        break;
    }
    case MSG_CAMERA_READY: {
        int can_save = 0;
        if (proto_decode_camera_ready(&can_save, payload, len) == PROTO_OK) {
            conn.camera_ready = 1;
            conn.camera_can_save = can_save;
        }
        break;
    }
    case MSG_CAMERA_ACK: {
        uint8_t kind = 0;
        uint32_t value = 0;
        if (proto_decode_camera_ack(&kind, &value, payload, len) == PROTO_OK) {
            cam_on_ack(kind, value);
        }
        break;
    }
    case MSG_PHOTO_SAVED: {
        photo_saved_t saved;
        if (proto_decode_photo_saved(&saved, payload, len) == PROTO_OK) {
            cam_on_saved(&saved);
        }
        break;
    }
    case MSG_CLIP:
    case MSG_PROJECT:
    case MSG_MIRROR:
    case MSG_TOUCH:
        // Announced by the DS card only; this one never asks for them.
        break;
    case MSG_ERROR:
        link_status("server: %.*s", (int)(len < 80 ? len : 80), (const char *)payload);
        break;
    default:
        r = PROTO_ERR_TYPE;
        (void)r;
        break;
    }
}

// ------------------------------------------------------------- the steps ---

// The Vita's HELLO: the DS's tokens without the ones for DS-only screens
// (project, clip, mirror), plus the camera.  Built here because
// proto_hello_payload() always announces the DS set.
static int hello_payload(char *out, size_t cap, const uint8_t *card_nonce)
{
    char cn[2 * PROTO_CARD_NONCE_BYTES + 1];
    for (int i = 0; i < PROTO_CARD_NONCE_BYTES; i++) {
        snprintf(cn + 2 * i, 3, "%02x", card_nonce[i]);
    }
    int n = snprintf(out, cap,
                     "ds-term/1 mode snippets remote state seal seal3 commands ask "
                     "grid=%dx%d camera latin1", PROTO_COLS, PROTO_ROWS);
    if (cfg.device_id[0]) n += snprintf(out + n, cap - (size_t)n, " id=%s", cfg.device_id);
    n += snprintf(out + n, cap - (size_t)n, " cn=%s", cn);
    return n < (int)cap ? n : -1;
}

static void begin_connect(void)
{
    conn.attempt++;
    link_status("connecting to %s (%s)", conn.server_name, cfg.server);
    sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) {
        fail("no socket");
        return;
    }
    const int flags = fcntl(sock, F_GETFL, 0);
    fcntl(sock, F_SETFL, flags | O_NONBLOCK);
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_port = htons(cfg.port);
    if (!inet_pton(AF_INET, cfg.server, &sa.sin_addr)) {
        fail("bad server address");
        return;
    }
    (void)connect(sock, (struct sockaddr *)&sa, sizeof(sa));
    framereader_init(&reader);
    step = STEP_CONNECT;
    step_at = frame_count();
}

static int read_some(void)
{
    uint8_t chunk[2048];
    for (;;) {
        const size_t room = framereader_space(&reader);
        if (room == 0) return 1;
        const int n = recv(sock, chunk, room < sizeof(chunk) ? room : sizeof(chunk), 0);
        if (n > 0) {
            framereader_feed(&reader, chunk, (size_t)n);
            continue;
        }
        if (n == 0) return 0;
        if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINPROGRESS ||
            errno == ENOTCONN || errno == EALREADY) {
            return 1;
        }
        return 0;
    }
}

static void on_nonce(const uint8_t *payload, uint16_t len)
{
    if (len != PROTO_NONCE_SIZE) {
        fail("not a vitaterm server");
        return;
    }
    memcpy(nonce, payload, PROTO_NONCE_SIZE);
    seal_reset(&seal);
    uint8_t card_nonce[PROTO_CARD_NONCE_BYTES];
    setup_random(card_nonce, sizeof(card_nonce));
    caps_len = hello_payload(caps, sizeof(caps), card_nonce);
    if (caps_len < 0) {
        fail("hello too long");
        return;
    }
    uint8_t out[PROTO_HEADER_SIZE + PROTO_HELLO_MAX];
    size_t n = proto_encode(out, MSG_HELLO, (const uint8_t *)caps, (uint16_t)caps_len);
    if (!send_raw(out, n)) {
        fail("could not send HELLO");
        return;
    }
    uint8_t mac[PROTO_MAC_SIZE];
    hmac_sha256(cfg.psk, CFG_PSK_BYTES, nonce, PROTO_NONCE_SIZE, mac);
    n = proto_encode(out, MSG_AUTH, mac, PROTO_MAC_SIZE);
    if (!send_raw(out, n)) {
        fail("could not send AUTH");
        return;
    }
    step = STEP_AUTH;
    step_at = frame_count();
}

static void on_auth_reply(msg_type_t type, const uint8_t *payload, uint16_t len)
{
    if (type == MSG_ERROR) {
        snprintf(conn.refusal, sizeof(conn.refusal), "%.*s",
                 (int)(len < sizeof(conn.refusal) - 1 ? len : sizeof(conn.refusal) - 1),
                 (const char *)payload);
        for (char *c = conn.refusal; *c; c++) {
            if (*c < 0x20 || *c > 0x7e) *c = '?';
        }
        fail("the computer refused");
        return;
    }
    if (type == MSG_AUTH_FAIL) {
        snprintf(conn.refusal, sizeof(conn.refusal),
                 "the key does not match: pair this Vita again");
        fail("key rejected");
        return;
    }
    if (type != MSG_AUTH_OK) {
        fail("unexpected answer to AUTH");
        return;
    }
    if (len != 5 || memcmp(payload, "seal3", 5) != 0) {
        snprintf(conn.refusal, sizeof(conn.refusal),
                 "the computer runs an older server: update vitaterm there");
        fail("refused: old server");
        return;
    }
    seal_start(&seal, cfg.psk, CFG_PSK_BYTES, nonce, (const uint8_t *)caps,
               (size_t)caps_len);
    step = STEP_UP;
    conn.state = LINK_UP;
    conn.attempt = 0;
    conn.refusal[0] = '\0';
    chime_link_changed(1);
    chime_view_sent();
    link_status("connected to %s", conn.server_name);
}

void link_pump(void)
{
    if (!started) return;
    if (step == STEP_IDLE) {
        if ((long)(frame_count() - retry_at) >= 0) begin_connect();
        return;
    }
    if (!read_some()) {
        fail(step == STEP_CONNECT ? "no vitaterm server answered" : "connection closed");
        return;
    }
    if (step == STEP_CONNECT && frame_count() - step_at > 60 * 6) {
        fail("no vitaterm server answered");
        return;
    }
    if (step == STEP_AUTH && frame_count() - step_at > 60 * 10) {
        fail("timed out waiting for the computer");
        return;
    }

    for (int guard = 0; guard < 256 && sock >= 0; guard++) {
        msg_type_t type;
        const uint8_t *payload;
        uint16_t len;
        const proto_result_t r = framereader_next(&reader, &type, &payload, &len);
        if (r == PROTO_NEED_MORE) break;
        if (r != PROTO_OK) {
            fail("protocol error");
            return;
        }
        if (step == STEP_CONNECT) {
            if (type == MSG_NONCE) on_nonce(payload, len);
            else if (type == MSG_ERROR) on_auth_reply(type, payload, len);
            continue;
        }
        if (step == STEP_AUTH) {
            if (type == MSG_NONCE) continue;
            on_auth_reply(type, payload, len);
            continue;
        }
        if (seal.on) {
            static uint8_t plain[PROTO_MAX_PAYLOAD];
            const int body = seal_open(&seal, type, payload, len, plain);
            if (body < 0) {
                fail("integrity check failed; reconnecting");
                return;
            }
            payload = plain;
            len = (uint16_t)body;
        }
        handle(type, payload, len);
    }

    if (step == STEP_UP) {
        // Once a second: the round trip.
        if (probe_outstanding && now_us() - probe_sent_us > 3000000UL) {
            probe_outstanding = 0;
            conn.rtt_known = 0;
        }
        if (frame_count() - last_ping_frame >= 60 && !probe_outstanding) {
            last_ping_frame = frame_count();
            uint8_t out[PROTO_HEADER_SIZE];
            if (link_send_frame(out, proto_encode_empty(out, MSG_PING))) {
                probe_outstanding = 1;
                probe_sent_us = now_us();
            }
        }
    }
}
