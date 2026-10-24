// The start screen: the computers this Vita knows, finding new ones on the
// network, and pairing by comparing a six-digit code (pair.h).
//
// The Vita joins Wi-Fi itself (Settings > Network), so unlike the DS card
// there is no network wizard here: only the computers.
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>

#include <psp2/ctrl.h>
#include <psp2/kernel/rng.h>
#include <psp2/net/netctl.h>

#include "app.h"
#include "discover.h"
#include "entropy.h"
#include "pair.h"
#include "profiles.h"
#include "sha256.h"
#include "x25519.h"

#define SERVER_PORT_DEFAULT 8347

static entropy_t pool;
static int pool_ready;
static profiles_t prof;
static wifi_profile_t no_wifi;      // the Vita's own connection; DHCP

void setup_entropy_u32(uint32_t v)
{
    if (!pool_ready) {
        entropy_init(&pool);
        pool_ready = 1;
    }
    entropy_add_u32(&pool, v);
}

// The system's generator, stirred with everything the person has done.
void setup_random(uint8_t *out, size_t n)
{
    uint8_t sys[ENTROPY_SIZE];
    sceKernelGetRandomNumber(sys, sizeof(sys));
    setup_entropy_u32((uint32_t)now_us());
    entropy_add(&pool, sys, sizeof(sys));
    while (n) {
        uint8_t r[ENTROPY_SIZE];
        entropy_take(&pool, r);
        const size_t take = n < sizeof(r) ? n : sizeof(r);
        memcpy(out, r, take);
        out += take;
        n -= take;
    }
    memset(sys, 0, sizeof(sys));
}

static void ensure_device_id(void)
{
    if (device_id_valid(prof.device_id)) return;
    uint8_t r[4];
    setup_random(r, sizeof(r));
    device_id_from_bytes(r, prof.device_id);
    (void)profiles_save_device_id(DATA_ROOT, prof.device_id);
}

static int own_address(uint32_t *ip, uint32_t *mask)
{
    SceNetCtlInfo info;
    if (sceNetCtlInetGetInfo(SCE_NETCTL_INFO_GET_IP_ADDRESS, &info) < 0) return 0;
    if (!ipv4_parse(info.ip_address, ip)) return 0;
    if (sceNetCtlInetGetInfo(SCE_NETCTL_INFO_GET_NETMASK, &info) < 0 ||
        !ipv4_parse(info.netmask, mask)) {
        *mask = 0xFFFFFF00u;
    }
    return 1;
}

// ------------------------------------------------------------- dialogs ---

static int back_pressed(void)
{
    return (in.down & SCE_CTRL_CIRCLE) != 0;
}

static void message(const char *title, const char *body)
{
    for (;;) {
        app_tick();
        gfx_working(title, body);
        if (in.down & (SCE_CTRL_CROSS | SCE_CTRL_CIRCLE)) return;
    }
}

static int confirm(const char *title, const char *body)
{
    for (;;) {
        app_tick();
        gfx_begin();
        app_draw_terminal();
        gfx_rect(0, 0, SCREEN_W, SCREEN_H, 0xB0000000u);
        const int w = 640, x = (SCREEN_W - w) / 2, y = 110;
        gfx_panel(x, y, w, 300);
        gfx_text(x + 24, y + 44, COL_ACCENT, 1.3f, title);
        gfx_wrap(x + 24, y + 64, w - 48, COL_TEXT, 1.0f, body);
        static const char *const hints[] = { "cross", "Yes", "circle", "No" };
        gfx_hints(hints, 2);
        gfx_end();
        if (in.down & SCE_CTRL_CROSS) return 1;
        if (in.down & SCE_CTRL_CIRCLE) return 0;
    }
}

// ----------------------------------------------------------- the wire ---

static framereader_t boot_reader;

// Connect and wait for the NONCE the server sends on accept: the proof that
// this is a dsterm server at all.  Returns the socket, or -1.
static int open_server(uint32_t addr, uint16_t port, int frames, const char *what)
{
    const int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return -1;
    fcntl(s, F_SETFL, fcntl(s, F_GETFL, 0) | O_NONBLOCK);
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_port = htons(port);
    sa.sin_addr.s_addr = htonl(addr);
    (void)connect(s, (struct sockaddr *)&sa, sizeof(sa));
    framereader_init(&boot_reader);
    for (int f = 0; f < frames; f++) {
        app_tick();
        gfx_working("Pairing", what);
        if (back_pressed()) break;
        uint8_t chunk[256];
        const int n = recv(s, chunk, sizeof(chunk), 0);
        if (n > 0) {
            if ((size_t)n > framereader_space(&boot_reader)) break;
            framereader_feed(&boot_reader, chunk, (size_t)n);
            msg_type_t type;
            const uint8_t *payload;
            uint16_t len;
            const proto_result_t r = framereader_next(&boot_reader, &type, &payload, &len);
            if (r == PROTO_OK && type == MSG_NONCE) return s;
            if (r != PROTO_NEED_MORE) break;
            continue;
        }
        if (n == 0) break;
        if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINPROGRESS &&
            errno != ENOTCONN && errno != EALREADY) {
            break;
        }
    }
    close(s);
    return -1;
}

static int send_frame(int s, msg_type_t type, const uint8_t *payload, uint16_t len)
{
    uint8_t out[PROTO_HEADER_SIZE + 64];
    if (len > sizeof(out) - PROTO_HEADER_SIZE) return 0;
    const size_t n = proto_encode(out, type, payload, len);
    size_t sent = 0;
    for (int stalls = 0; sent < n && stalls < 120; ) {
        const int w = send(s, out + sent, n - sent, 0);
        if (w > 0) { sent += (size_t)w; continue; }
        if (w < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            stalls++;
            usleep(8000);
            continue;
        }
        return 0;
    }
    return sent == n;
}

static int await_pair(int s, msg_type_t *type, const uint8_t **payload,
                      uint16_t *len, int frames, const char *title, const char *body)
{
    for (int f = 0; f < frames; f++) {
        const proto_result_t r = framereader_next(&boot_reader, type, payload, len);
        if (r == PROTO_OK) {
            if (*type == MSG_PAIR_REPLY || *type == MSG_PAIR_OK ||
                *type == MSG_PAIR_DONE || *type == MSG_PAIR_FAIL || *type == MSG_ERROR) {
                return 1;
            }
            continue;
        }
        if (r != PROTO_NEED_MORE) return 0;
        uint8_t chunk[256];
        const int n = recv(s, chunk, sizeof(chunk), 0);
        if (n > 0) {
            if ((size_t)n > framereader_space(&boot_reader)) return 0;
            framereader_feed(&boot_reader, chunk, (size_t)n);
            continue;
        }
        if (n == 0) return 0;
        if (errno != EAGAIN && errno != EWOULDBLOCK) return 0;
        app_tick();
        gfx_working(title, body);
        if (back_pressed()) return 0;
    }
    return 0;
}

static void fail_reason(const uint8_t *payload, uint16_t len, char *why, size_t n)
{
    char text[65];
    size_t k = 0;
    for (uint16_t i = 0; i < len && k < sizeof(text) - 1; i++) {
        const uint8_t c = payload[i];
        text[k++] = (c >= 0x20 && c <= 0x7e) ? (char)c : '?';
    }
    text[k] = '\0';
    snprintf(why, n, "The computer said: %s", text);
}

// The card side of pairing (boot.c's pair_with), step for step.
static int pair_with(uint32_t addr, uint16_t port, const char *name,
                     uint8_t key[PAIR_KEY_SIZE], char *why, size_t why_len)
{
    snprintf(why, why_len, "The connection to the computer was lost. Nothing was saved.");
    ensure_device_id();
    uint8_t id[PROTO_DEVICE_ID_BYTES];
    device_id_to_bytes(prof.device_id, id);

    uint8_t priv[X25519_SIZE], pub[X25519_SIZE];
    setup_random(priv, sizeof(priv));
    x25519_public(pub, priv);
    uint8_t card_nonce[PAIR_NONCE_SIZE];
    setup_random(card_nonce, sizeof(card_nonce));

    const int s = open_server(addr, port, 6 * 60, "Connecting...");
    if (s < 0) {
        memset(priv, 0, sizeof(priv));
        snprintf(why, why_len, "No vitaterm server answered at that address.");
        return 0;
    }

    uint8_t hello[PAIR_HELLO_SIZE];
    memcpy(hello, id, PROTO_DEVICE_ID_BYTES);
    memcpy(hello + PROTO_DEVICE_ID_BYTES, pub, X25519_SIZE);
    int ok = send_frame(s, MSG_PAIR_HELLO, hello, sizeof(hello));

    msg_type_t type;
    const uint8_t *payload;
    uint16_t len;
    uint8_t server_pub[X25519_SIZE], commit[PAIR_COMMIT_SIZE];
    uint8_t confirm_key[PAIR_CONFIRM_KEY_SIZE];
    uint8_t server_nonce[PAIR_NONCE_SIZE];
    if (ok) ok = await_pair(s, &type, &payload, &len, 10 * 60, "Pairing", "Exchanging keys...");
    if (!ok) {
        snprintf(why, why_len, "The computer closed the connection. Is its pairing "
                 "window open? Run  vitaterm pair  there first.");
    } else if (type != MSG_PAIR_REPLY) {
        fail_reason(payload, len, why, why_len);
        ok = 0;
    } else if (len != PAIR_REPLY_SIZE) {
        snprintf(why, why_len, "That computer pairs the old way: update vitaterm there.");
        ok = 0;
    } else {
        memcpy(server_pub, payload, X25519_SIZE);
        memcpy(commit, payload + X25519_SIZE, PAIR_COMMIT_SIZE);
        ok = send_frame(s, MSG_PAIR_CONFIRM, card_nonce, sizeof(card_nonce));
    }

    if (ok && !await_pair(s, &type, &payload, &len, 10 * 60, "Pairing", "Exchanging keys...")) {
        snprintf(why, why_len, "The computer stopped answering.");
        ok = 0;
    } else if (ok && type != MSG_PAIR_OK) {
        fail_reason(payload, len, why, why_len);
        ok = 0;
    } else if (ok && !pair_commitment_kept(pub, server_pub, payload, len, commit)) {
        snprintf(why, why_len, "The computer did not keep its word in the exchange, "
                 "which is what an attacker would do. Nothing was saved.");
        ok = 0;
    } else if (ok) {
        memcpy(server_nonce, payload, PAIR_NONCE_SIZE);
        uint8_t shared[X25519_SIZE];
        x25519(shared, priv, server_pub);
        ok = pair_derive(shared, card_nonce, server_nonce, id, pub, server_pub,
                         confirm_key, key);
        memset(shared, 0, sizeof(shared));
        if (!ok) snprintf(why, why_len, "The computer's key was unusable.");
    }
    memset(priv, 0, sizeof(priv));

    if (ok) {
        const uint32_t code = pair_code(pub, server_pub, card_nonce, server_nonce);
        char body[256];
        snprintf(body, sizeof(body),
                 "This Vita shows:   %03u %03u\n\n"
                 "Does %s show the same code?\n\n"
                 "If its code is different, press Circle: someone else may be "
                 "trying to pair.",
                 (unsigned)(code / 1000u), (unsigned)(code % 1000u), name);
        if (!confirm("Compare the codes", body)) {
            snprintf(why, why_len, "The codes did not match, so nothing was saved.");
            ok = 0;
        }
    }
    if (ok) {
        uint8_t mac[PAIR_MAC_SIZE];
        pair_accept_mac(confirm_key, PAIR_ROLE_CARD, pub, server_pub, mac);
        ok = send_frame(s, MSG_PAIR_ACCEPT, mac, sizeof(mac));
    }
    if (ok) {
        if (!await_pair(s, &type, &payload, &len, 120 * 60, "Pairing",
                        "Now answer  y  on the computer.\n\n(Circle stops.)")) {
            snprintf(why, why_len, "The computer did not confirm (no answer, or it said "
                     "no). Nothing was saved.");
            ok = 0;
        } else if (type != MSG_PAIR_DONE) {
            fail_reason(payload, len, why, why_len);
            ok = 0;
        } else if (!pair_server_done(confirm_key, pub, server_pub, payload, len)) {
            snprintf(why, why_len, "The computer's confirmation did not check out. "
                     "Nothing was saved.");
            ok = 0;
        }
    }
    memset(confirm_key, 0, sizeof(confirm_key));
    close(s);
    return ok;
}

static void unique_name(const char *want, const char *host, uint16_t port,
                        char out[PROFILE_NAME_MAX])
{
    char base[PROFILE_NAME_MAX];
    profile_name_from(want, base);
    snprintf(out, PROFILE_NAME_MAX, "%s", base);
    for (int n = 2; n < 100; n++) {
        const int i = profiles_find(&prof, out);
        if (i < 0) return;
        const server_profile_t *s = &prof.servers[i];
        if (strcmp(s->host, host) == 0 && s->port == port) return;
        char suffix[8];
        snprintf(suffix, sizeof(suffix), "-%d", n);
        const size_t room = PROFILE_NAME_MAX - 1 - strlen(suffix);
        snprintf(out, PROFILE_NAME_MAX, "%.*s%s", (int)room, base, suffix);
    }
}

static int save_server(server_profile_t *s)
{
    char err[64];
    if (!profiles_save_server(DATA_ROOT, s, err, sizeof(err))) {
        message("Saving", "Paired, but the memory card would not take the profile.");
        return 0;
    }
    profiles_load(DATA_ROOT, &prof);
    return 1;
}

// Pair with a computer, or take its key typed by hand.  The new profile's
// index, or -1.
static int add_server(const char *name, const char *host, uint16_t port, int pairing_open)
{
    uint32_t addr;
    if (!ipv4_parse(host, &addr)) return -1;
    server_profile_t s;
    memset(&s, 0, sizeof(s));
    unique_name(name, host, port, s.name);
    snprintf(s.host, sizeof(s.host), "%s", host);
    s.port = port;

    char title[64];
    snprintf(title, sizeof(title), "New computer: %.20s", name);
    if (!pairing_open &&
        !confirm(title, "On that computer, run:\n\n      vitaterm pair\n\nthen press Cross "
                        "here. Both screens will show a code to compare.\n\n"
                        "(To type its key instead, press Circle and choose "
                        "\"Type a key\" from the start screen.)")) {
        return -1;
    }
    char why[200];
    if (pair_with(addr, port, s.name, s.psk, why, sizeof(why))) {
        if (!save_server(&s)) return -1;
        message("Paired", "This Vita now has its own key for that computer. "
                          "Nothing secret was sent.");
        return profiles_find(&prof, s.name);
    }
    message("Not paired", why);
    return -1;
}

static int add_by_key(void)
{
    char addr_text[40];
    if (!input_text("Computer address (add :port if not 8347)", "", addr_text,
                    sizeof(addr_text), 0)) {
        return -1;
    }
    char host[CFG_ADDR_MAX];
    uint16_t port;
    if (!host_port_parse(addr_text, host, &port, SERVER_PORT_DEFAULT)) {
        message("Address", "That is not an IPv4 address.");
        return -1;
    }
    char key_text[80];
    if (!input_text("Its key: the 32 hex digits in ~/.config/ds-claude-terminal/psk",
                    "", key_text, sizeof(key_text), 0)) {
        return -1;
    }
    server_profile_t s;
    memset(&s, 0, sizeof(s));
    unique_name(host, host, port, s.name);
    snprintf(s.host, sizeof(s.host), "%s", host);
    s.port = port;
    char err[64];
    if (!config_parse_psk(key_text, s.psk, err, sizeof(err))) {
        message("Key", err);
        return -1;
    }
    if (!save_server(&s)) return -1;
    return profiles_find(&prof, s.name);
}

// ------------------------------------------------------------- finding ---

static void find_by_broadcast(discover_list_t *list)
{
    const int s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0) return;
    const int yes = 1;
    (void)setsockopt(s, SOL_SOCKET, SO_BROADCAST, &yes, sizeof(yes));
    fcntl(s, F_SETFL, fcntl(s, F_GETFL, 0) | O_NONBLOCK);
    uint32_t ip = 0, mask = 0xFFFFFF00u;
    (void)own_address(&ip, &mask);

    char probe[DISCOVER_DATAGRAM_MAX];
    const int plen = discover_probe(probe, sizeof(probe), prof.device_id);
    struct sockaddr_in to[2];
    memset(to, 0, sizeof(to));
    to[0].sin_family = to[1].sin_family = AF_INET;
    to[0].sin_port = to[1].sin_port = htons(DISCOVER_PORT);
    to[0].sin_addr.s_addr = htonl(0xFFFFFFFFu);
    to[1].sin_addr.s_addr = htonl(ip | ~mask);

    for (int f = 0; f < 3 * 60; f++) {
        if (f % 20 == 0 && plen > 0) {
            for (int i = 0; i < 2; i++) {
                (void)sendto(s, probe, (size_t)plen, 0, (struct sockaddr *)&to[i],
                             sizeof(to[i]));
            }
        }
        for (;;) {
            uint8_t buf[DISCOVER_DATAGRAM_MAX + 1];
            struct sockaddr_in from;
            socklen_t fl = sizeof(from);
            const int n = recvfrom(s, buf, sizeof(buf), 0, (struct sockaddr *)&from, &fl);
            if (n <= 0) break;
            discover_entry_t e;
            if (discover_parse_answer(buf, (size_t)n, &e)) {
                ipv4_format(ntohl(from.sin_addr.s_addr), e.host);
                discover_list_add(list, &e);
            }
        }
        char body[96];
        snprintf(body, sizeof(body), "Asking the network...\n\n%u found", list->count);
        app_tick();
        gfx_working("Find computers", body);
        if (back_pressed()) break;
    }
    close(s);
}

// ----------------------------------------------------------- the menu ---

typedef struct {
    char label[64];
    char note[40];
    int kind;           // 0 saved, 1 found, 2 action
    int index;
} row_t;

enum { ACT_FIND, ACT_ADDRESS, ACT_KEY };

static int resolve(int index, dsterm_config_t *out, const char *host)
{
    if (index < 0 || (unsigned)index >= prof.count) return 0;
    ensure_device_id();
    profiles_resolve(&no_wifi, &prof.servers[index], prof.device_id, out);
    if (host) snprintf(out->server, sizeof(out->server), "%s", host);
    if (strcmp(prof.last, prof.servers[index].name) != 0) {
        snprintf(prof.last, sizeof(prof.last), "%s", prof.servers[index].name);
        (void)profiles_save_last(DATA_ROOT, prof.last);
    }
    return 1;
}

static void draw_home(const row_t *rows, int n, int sel, const char *notice)
{
    gfx_begin();
    gfx_rect(0, 0, SCREEN_W, SCREEN_H, 0xFF14100Cu);
    gfx_text(40, 70, COL_ACCENT, 1.8f, "vitaterm");
    gfx_text(42, 102, COL_DIM, 1.0f,
             "Claude Code on your computer, on this Vita. Run  vitaterm setup  there once.");
    int y = 140;
    for (int i = 0; i < n; i++) {
        if (i > 0 && rows[i].kind != rows[i - 1].kind) y += 14;
        if (i == sel) gfx_rect(30, y, SCREEN_W - 60, 40, COL_PANEL_HI);
        gfx_text(50, y + 28, rows[i].kind == 2 ? COL_ACCENT : COL_TEXT, 1.1f, rows[i].label);
        if (rows[i].note[0]) {
            const int w = gfx_text_width(0.9f, rows[i].note);
            gfx_text(SCREEN_W - 50 - w, y + 27, COL_DIM, 0.9f, rows[i].note);
        }
        y += 42;
    }
    if (notice && notice[0]) gfx_wrap(40, SCREEN_H - 110, SCREEN_W - 80, COL_WARN, 0.9f, notice);
    static const char *const hints[] = { "cross", "Choose", "triangle", "Forget computer" };
    gfx_hints(hints, 2);
    gfx_end();
}

int setup_run(char *name, size_t cap)
{
    profiles_load(DATA_ROOT, &prof);
    ensure_device_id();
    static discover_list_t found;
    static int found_valid;
    static char notice[160];
    if (conn.refusal[0]) {
        snprintf(notice, sizeof(notice), "Last time: %s", conn.refusal);
    }
    uint32_t ip, mask;
    if (!own_address(&ip, &mask)) {
        snprintf(notice, sizeof(notice), "This Vita is not on a network. Join one in "
                 "Settings > Network.");
    }
    int sel = 0;
    // At power-on, the last computer after a short countdown, as the DS card
    // does; Circle (or any button) stays here.  Only once per run, and not
    // after a refusal.
    static int booted;
    int countdown = -1;
    if (!booted && !conn.refusal[0]) {
        const int last = profiles_find(&prof, prof.last);
        if (last >= 0) {
            countdown = 3 * 60;
            sel = last;
        }
    }
    booted = 1;
    for (;;) {
        row_t rows[PROFILES_MAX + DISCOVER_MAX + 3];
        int n = 0;
        for (unsigned i = 0; i < prof.count; i++) {
            row_t *r = &rows[n++];
            r->kind = 0;
            r->index = (int)i;
            snprintf(r->label, sizeof(r->label), "%s", prof.servers[i].name);
            snprintf(r->note, sizeof(r->note), "%s%s", prof.servers[i].host,
                     strcmp(prof.servers[i].name, prof.last) == 0 ? "   last" : "");
        }
        if (found_valid) {
            for (unsigned i = 0; i < found.count; i++) {
                const discover_entry_t *e = &found.entries[i];
                if (profiles_find_host(&prof, e->host, e->port) >= 0) continue;
                row_t *r = &rows[n++];
                r->kind = 1;
                r->index = (int)i;
                snprintf(r->label, sizeof(r->label), "%s", e->name);
                snprintf(r->note, sizeof(r->note), "%s   %s", e->host,
                         profiles_find_moved(&prof, e->name, e->host, e->port) >= 0
                             ? "moved" : e->pairing ? "ready to pair" : "new");
            }
        }
        static const char *const actions[] = {
            "Find computers on this network", "Pair by address", "Type a key",
        };
        for (int a = 0; a < 3; a++) {
            row_t *r = &rows[n++];
            r->kind = 2;
            r->index = a;
            snprintf(r->label, sizeof(r->label), "%s", actions[a]);
            r->note[0] = '\0';
        }
        if (sel >= n) sel = n - 1;

        app_tick();
        if (countdown >= 0) {
            snprintf(notice, sizeof(notice), "Connecting to %s in %d...  (any button stays here)",
                     prof.last, countdown / 60 + 1);
            if (in.down || in.touch_down) {
                countdown = -1;
                notice[0] = '\0';
                in.down = 0;
            } else if (--countdown < 0) {
                notice[0] = '\0';
                if (resolve(sel, &cfg, NULL)) {
                    snprintf(name, cap, "%s", prof.servers[sel].name);
                    return 1;
                }
            }
        }
        draw_home(rows, n, sel, notice);
        if (in.repeat & SCE_CTRL_UP) sel = (sel + n - 1) % n;
        if (in.repeat & SCE_CTRL_DOWN) sel = (sel + 1) % n;
        if ((in.down & SCE_CTRL_TRIANGLE) && rows[sel].kind == 0) {
            char body[128];
            snprintf(body, sizeof(body), "Forget %s? Its key is deleted from this Vita.",
                     prof.servers[rows[sel].index].name);
            if (confirm("Forget", body)) {
                (void)profiles_forget_server(DATA_ROOT, prof.servers[rows[sel].index].name);
                profiles_load(DATA_ROOT, &prof);
            }
            continue;
        }
        if (in.touch_up) {
            // A tap on a row picks it (rows start at y 140, 42 apart).
            int y = 140;
            for (int i = 0; i < n; i++) {
                if (i > 0 && rows[i].kind != rows[i - 1].kind) y += 14;
                if (in.ty >= y && in.ty < y + 40) { sel = i; in.down |= SCE_CTRL_CROSS; }
                y += 42;
            }
        }
        if (!(in.down & SCE_CTRL_CROSS)) continue;
        notice[0] = '\0';
        const row_t *r = &rows[sel];
        int index = -1;
        const char *host = NULL;
        if (r->kind == 0) {
            index = r->index;
        } else if (r->kind == 1) {
            const discover_entry_t *e = &found.entries[r->index];
            const int moved = profiles_find_moved(&prof, e->name, e->host, e->port);
            if (moved >= 0) {
                index = moved;
                host = e->host;
                // The same key at a new address: remember where it is now.
                server_profile_t s = prof.servers[moved];
                snprintf(s.host, sizeof(s.host), "%s", e->host);
                char err[64];
                (void)profiles_save_server(DATA_ROOT, &s, err, sizeof(err));
                profiles_load(DATA_ROOT, &prof);
            } else {
                index = add_server(e->name, e->host, e->port, e->pairing);
            }
        } else if (r->index == ACT_FIND) {
            discover_list_init(&found);
            find_by_broadcast(&found);
            found_valid = 1;
            if (!found.count) {
                snprintf(notice, sizeof(notice), "No computer answered. Is  vitaterm  "
                         "running, and on the same network as this Vita?");
            }
            continue;
        } else if (r->index == ACT_ADDRESS) {
            char text[40];
            if (!input_text("Computer address (add :port if not 8347)", "", text,
                            sizeof(text), 0)) {
                continue;
            }
            char h[CFG_ADDR_MAX];
            uint16_t port;
            if (!host_port_parse(text, h, &port, SERVER_PORT_DEFAULT)) {
                snprintf(notice, sizeof(notice), "\"%.40s\" is not an IPv4 address.", text);
                continue;
            }
            index = profiles_find_host(&prof, h, port);
            if (index < 0) index = add_server(h, h, port, 0);
        } else {
            index = add_by_key();
        }
        if (index >= 0 && resolve(index, &cfg, host)) {
            snprintf(name, cap, "%s", prof.servers[index].name);
            return 1;
        }
    }
}
