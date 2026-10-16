#include "bootui.h"

#include <stdio.h>
#include <string.h>


// ------------------------------------------------------------ text fields ---

static unsigned kind_max(field_kind_t kind)
{
    switch (kind) {
    case FIELD_TEXT:       return CFG_SSID_MAX - 1;      // 32
    case FIELD_PASSPHRASE: return CFG_WIFIKEY_MAX - 1;   // 63
    case FIELD_ADDRESS:    return CFG_ADDR_MAX - 1 + 6;  // 15 + ":65535"
    case FIELD_KEY:        return 48;                    // 32 hex + separators
    }
    return 0;
}

static int kind_allows(field_kind_t kind, uint8_t c)
{
    if (c < 0x20 || c > 0x7e) return 0;
    switch (kind) {
    case FIELD_TEXT:
    case FIELD_PASSPHRASE:
        return 1;
    case FIELD_ADDRESS:
        return (c >= '0' && c <= '9') || c == '.' || c == ':';
    case FIELD_KEY:
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
               (c >= 'A' && c <= 'F') || c == ' ' || c == '-';
    }
    return 0;
}

void field_init(field_t *f, field_kind_t kind, const char *initial)
{
    memset(f, 0, sizeof(*f));
    f->kind = kind;
    f->max = kind_max(kind);
    if (f->max >= FIELD_MAX) f->max = FIELD_MAX - 1;
    if (initial) {
        for (const char *p = initial; *p && f->len < f->max; p++) {
            if (kind_allows(kind, (uint8_t)*p)) f->buf[f->len++] = *p;
        }
    }
    f->buf[f->len] = '\0';
}

int field_complete(const field_t *f)
{
    switch (f->kind) {
    case FIELD_TEXT:
        return f->len >= 1;
    case FIELD_PASSPHRASE:
        return f->len == 0 || (f->len >= 8 && f->len <= 63);
    case FIELD_ADDRESS: {
        char host[CFG_ADDR_MAX];
        uint16_t port;
        return host_port_parse(f->buf, host, &port, 8347);
    }
    case FIELD_KEY: {
        uint8_t key[CFG_PSK_BYTES];
        char err[48];
        return config_parse_psk(f->buf, key, err, sizeof(err));
    }
    }
    return 0;
}

field_result_t field_input(field_t *f, uint8_t byte)
{
    if (byte == '\r' || byte == '\n') {
        return field_complete(f) ? FIELD_DONE : FIELD_INCOMPLETE;
    }
    if (byte == 0x1b) return FIELD_CANCEL;
    if (byte == 0x7f || byte == 0x08) {
        if (f->len == 0) return FIELD_IGNORED;
        f->buf[--f->len] = '\0';
        return FIELD_CHANGED;
    }
    if (!kind_allows(f->kind, byte) || f->len >= f->max) return FIELD_IGNORED;
    f->buf[f->len++] = (char)byte;
    f->buf[f->len] = '\0';
    return FIELD_CHANGED;
}

void field_display(const field_t *f, char out[BOOTUI_COLS + 1])
{
    // Text plus a one-cell cursor, keeping the tail if it is too long.
    const unsigned room = BOOTUI_COLS - 1;
    const unsigned skip = f->len > room ? f->len - room : 0;
    const unsigned n = f->len - skip;
    memcpy(out, f->buf + skip, n);
    out[n] = '_';
    out[n + 1] = '\0';
}

const char *field_prompt(field_kind_t kind)
{
    switch (kind) {
    case FIELD_TEXT:       return "Network name (SSID)";
    case FIELD_PASSPHRASE: return "Wi-Fi password (empty if open)";
    case FIELD_ADDRESS:    return "Server address, e.g. 192.168.0.10";
    case FIELD_KEY:        return "Pairing key: 32 hex characters";
    }
    return "";
}

// ------------------------------------------------------------------ lists ---

void menu_list_set(menu_list_t *l, unsigned count)
{
    l->count = count;
    if (count == 0) {
        l->selected = l->top = 0;
        return;
    }
    if (l->selected >= count) l->selected = count - 1;
    if (l->top > l->selected) l->top = l->selected;
}

void menu_list_move(menu_list_t *l, int delta, unsigned visible)
{
    if (l->count == 0) return;
    long next = (long)l->selected + delta;
    if (next < 0) next = 0;
    if (next >= (long)l->count) next = (long)l->count - 1;
    l->selected = (unsigned)next;
    if (visible == 0) visible = 1;
    if (l->selected < l->top) l->top = l->selected;
    if (l->selected >= l->top + visible) l->top = l->selected - visible + 1;
}

// ---------------------------------------------------------------- countdown ---

void countdown_start(countdown_t *c, unsigned frames)
{
    c->frames_left = frames;
    c->active = frames > 0;
}

void countdown_cancel(countdown_t *c)
{
    c->active = 0;
    c->frames_left = 0;
}

int countdown_tick(countdown_t *c)
{
    if (!c->active) return 0;
    if (c->frames_left > 0) c->frames_left--;
    if (c->frames_left == 0) {
        c->active = 0;
        return 1;
    }
    return 0;
}

unsigned countdown_seconds(const countdown_t *c)
{
    if (!c->active) return 0;
    return (c->frames_left + 59) / 60;
}

// ---------------------------------------------------------------- the menu ---

// "label .......... note", padded so the note sits at the right edge.
static void row_with_note(const char *label, const char *note,
                          char out[BOOTUI_COLS + 1])
{
    const size_t nl = note ? strlen(note) : 0;
    char left[BOOTUI_COLS + 1];
    snprintf(left, sizeof(left), "%s", label);
    if (nl == 0) {
        snprintf(out, BOOTUI_COLS + 1, "%s", left);
        return;
    }
    size_t ll = strlen(left);
    // Keep at least one space between the label and the note.
    if (ll + 1 + nl > BOOTUI_COLS) {
        ll = BOOTUI_COLS > nl + 1 ? BOOTUI_COLS - nl - 1 : 0;
        left[ll] = '\0';
    }
    snprintf(out, BOOTUI_COLS + 1, "%-*s%s", (int)(BOOTUI_COLS - nl), left, note);
}

unsigned bootui_main_rows(const profiles_t *p, unsigned countdown_s,
                          boot_row_t rows[BOOT_MAIN_ROWS],
                          char labels[BOOT_MAIN_ROWS][BOOTUI_COLS + 1])
{
    unsigned n = 0;
    char buf[BOOTUI_COLS + 1], note[16];

    if (p->have_wifi && p->last[0] && profiles_find(p, p->last) >= 0) {
        snprintf(buf, sizeof(buf), "Connect to %s", p->last);
        note[0] = '\0';
        if (countdown_s) snprintf(note, sizeof(note), "(%u)", countdown_s);
        rows[n] = BOOT_ROW_CONNECT_LAST;
        row_with_note(buf, note, labels[n++]);
    }

    rows[n] = BOOT_ROW_FIND;
    row_with_note("Find servers on this network", "", labels[n++]);
    rows[n] = BOOT_ROW_ADDRESS;
    row_with_note("Enter an address", "", labels[n++]);

    snprintf(note, sizeof(note), "(%u)", p->count);
    rows[n] = BOOT_ROW_SAVED;
    row_with_note("Saved servers", note, labels[n++]);

    if (p->have_wifi) {
        snprintf(buf, sizeof(buf), "Wi-Fi: %s", p->wifi.ssid);
        row_with_note(buf, p->wifi.dhcp ? "dhcp" : "static", labels[n]);
    } else {
        row_with_note("Wi-Fi: not set up", "", labels[n]);
    }
    rows[n++] = BOOT_ROW_WIFI;
    return n;
}

void bootui_server_row(const char *name, const char *host, uint16_t port,
                       const char *note, char out[BOOTUI_COLS + 1])
{
    char left[BOOTUI_COLS + 1];
    // 12 + 1 + "255.255.255.255:65535" + a space + a five-letter note = 40.
    snprintf(left, sizeof(left), "%-12.12s %s:%u", name ? name : "",
             host ? host : "", (unsigned)port);
    row_with_note(left, note ? note : "", out);
}

// ------------------------------------------------------------------ Wi-Fi ---

int bootui_ap_supported(ap_security_t s)
{
    return s == AP_OPEN || s == AP_WPA2;
}

unsigned bootui_aps_tidy(bootui_ap_t *aps, unsigned count)
{
    unsigned n = 0;
    for (unsigned i = 0; i < count; i++) {
        if (aps[i].ssid[0] == '\0') continue;           // hidden network
        int merged = 0;
        for (unsigned j = 0; j < n; j++) {
            if (strcmp(aps[j].ssid, aps[i].ssid) == 0) {
                if (aps[i].rssi > aps[j].rssi) aps[j] = aps[i];
                merged = 1;
                break;
            }
        }
        if (!merged) aps[n++] = aps[i];
    }
    // Insertion sort: joinable first, then strongest first; there are at
    // most a few dozen.  A network you cannot join stays on the list -- it is
    // probably your own, and the row says why -- but below the ones you can.
    for (unsigned i = 1; i < n; i++) {
        bootui_ap_t key = aps[i];
        const int key_ok = bootui_ap_verdict(&key) == AP_VERDICT_OK;
        unsigned j = i;
        while (j > 0) {
            const int prev_ok = bootui_ap_verdict(&aps[j - 1]) == AP_VERDICT_OK;
            if (prev_ok && !key_ok) break;
            if (prev_ok == key_ok && aps[j - 1].rssi >= key.rssi) break;
            aps[j] = aps[j - 1];
            j--;
        }
        aps[j] = key;
    }
    return n;
}

ap_verdict_t bootui_ap_verdict(const bootui_ap_t *ap)
{
    if (!ap) return AP_VERDICT_OK;
    if (!ap->dsi_mode) {
        // DS mode: dswifi's own flag decides.  It is clear for any WPA
        // network and for a router with no 802.11b basic rates.
        if (ap->wpa || ap->security == AP_WPA || ap->security == AP_WPA2) {
            return AP_VERDICT_NEEDS_DSI;
        }
        if (ap->security == AP_WEP) return AP_VERDICT_WEP;
        if (!ap->compatible) return AP_VERDICT_NO_11B;
        return AP_VERDICT_OK;
    }
    if (ap->security == AP_WEP) return AP_VERDICT_WEP;
    if (ap->security == AP_WPA) return AP_VERDICT_WPA1;
    // Only 802.1X (Enterprise) is refused before trying.  A WPA2/WPA3
    // "transition" router lists both a PSK and an SAE suite, and which one
    // the scan reports is not something to bet a working network on: it is
    // tried, and a failure says to check for WPA3 (wifi_associate()).
    if (ap->security == AP_WPA2 && ap->auth == 1) {
        return AP_VERDICT_NOT_PSK;
    }
    return AP_VERDICT_OK;
}

const char BOOTUI_FIRST_BOOT_GUIDE[] =
    "Getting started\n"
    "\n"
    "1. On your computer, install the\n"
    "   server and run:   dsterm setup\n"
    "\n"
    "2. Here, choose  Find servers on this\n"
    "   network  (it sets up Wi-Fi first\n"
    "   if it needs to).\n"
    "\n"
    "3. Run  dsterm pair  on it, pick it\n"
    "   here, and press A if both screens\n"
    "   show the same six-digit code.\n"
    "\n"
    "That is all: next time this card\n"
    "connects by itself.\n"
    "\n"
    "Guide: see the project's README.";

const char *bootui_verdict_text(ap_verdict_t v, const char *wpa_advice)
{
    switch (v) {
    case AP_VERDICT_NEEDS_DSI:
        return wpa_advice ? wpa_advice : "This network needs DSi mode.";
    case AP_VERDICT_WEP:
        return "WEP is not supported: it is broken,\n"
               "and anyone nearby could read the\n"
               "network. Use WPA2-AES, or an open\n"
               "network only you use.";
    case AP_VERDICT_WPA1:
        return "WPA (version 1) is not supported. Set\n"
               "the router to WPA2-AES (not WPA3 or\n"
               "WPA2/WPA3 mixed mode).";
    case AP_VERDICT_NOT_PSK:
        return "This is a WPA2-Enterprise network\n"
               "(a username, not just a password). Use\n"
               "a WPA2-Personal (AES) network instead.";
    case AP_VERDICT_NO_11B:
        return "This router has 802.11b switched off,\n"
               "and a DS can only talk 802.11b. Allow\n"
               "b/g/n mixed mode on the router.";
    case AP_VERDICT_OK:
    default:
        return "";
    }
}

void bootui_ap_row(const bootui_ap_t *ap, char out[BOOTUI_COLS + 1])
{
    static const char *names[] = {"open", "WEP", "WPA", "WPA2"};
    const unsigned s = (unsigned)ap->security < 4 ? (unsigned)ap->security : 0;
    char note[20];
    const ap_verdict_t v = bootui_ap_verdict(ap);
    if (v == AP_VERDICT_NEEDS_DSI) {
        snprintf(note, sizeof(note), "needs DSi mode");
    } else if (v == AP_VERDICT_NO_11B) {
        snprintf(note, sizeof(note), "11g only");
    } else if (v == AP_VERDICT_NOT_PSK) {
        snprintf(note, sizeof(note), "Enterprise");
    } else if (!ap->dsi_mode) {
        // DS mode's strength has no published scale; a bar drawn from it
        // would be a guess, so the row only says what kind of network it is.
        snprintf(note, sizeof(note), "%s", names[s]);
    } else {
        // -50 dBm or better is full; each 10 dB below drops a step.
        const int bars = ap->rssi >= -50 ? 4 : ap->rssi >= -60 ? 3 :
                         ap->rssi >= -70 ? 2 : ap->rssi >= -80 ? 1 : 0;
        snprintf(note, sizeof(note), "%-4s %.*s%.*s", names[s],
                 bars, "####", 4 - bars, "....");
    }
    row_with_note(ap->ssid, note, out);
}
