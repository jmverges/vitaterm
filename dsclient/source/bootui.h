// The boot menu's pure half: the text fields you type an address, a key or a
// passphrase into, list navigation, the auto-connect countdown, and every
// label the menu shows.  boot.c owns the screens, the radio and the sockets;
// everything here is host-tested, at both grid sizes.
//
// The menu, as a person meets it:
//
//    DS CLAUDE TERMINAL
//
//    > Connect to wsl-desktop        (3)      <- last server, counts down
//      Find servers on this network
//      Enter an address
//      Saved servers                 (2)
//      Wi-Fi: Example Network
//
//    A select  B back  START connect
#ifndef DSTERM_BOOTUI_H
#define DSTERM_BOOTUI_H

#include <stddef.h>
#include <stdint.h>

#include "profiles.h"

// Every label fits the narrower grid with room for the highlight's margin.
#define BOOTUI_COLS 40
#define BOOTUI_ROWS_MAX 20       // what render_overlay_list shows at 42x24

// ------------------------------------------------------------ text fields ---

typedef enum {
    FIELD_TEXT = 0,     // an SSID: any printable ASCII
    FIELD_PASSPHRASE,   // a WPA2 passphrase: printable ASCII, 8..63 (or empty)
    FIELD_ADDRESS,      // a.b.c.d or a.b.c.d:port
    FIELD_KEY,          // 32 hex digits; spaces and dashes are allowed
} field_kind_t;

#define FIELD_MAX 72

typedef struct {
    field_kind_t kind;
    char buf[FIELD_MAX];
    unsigned len;
    unsigned max;
} field_t;

typedef enum {
    FIELD_IGNORED = 0,   // the byte does not belong in this field
    FIELD_CHANGED,
    FIELD_DONE,          // Enter, and the field is complete
    FIELD_INCOMPLETE,    // Enter, but the field is not yet valid
    FIELD_CANCEL,        // Escape
} field_result_t;

void field_init(field_t *f, field_kind_t kind, const char *initial);
// One byte from the keyboard: printable characters the kind allows are
// appended, 0x7f and 0x08 delete, '\r' finishes, 0x1b cancels.
field_result_t field_input(field_t *f, uint8_t byte);
int field_complete(const field_t *f);
// The field as shown: its text and a trailing cursor.  Never longer than
// BOOTUI_COLS; a long field shows its tail, which is where you are typing.
void field_display(const field_t *f, char out[BOOTUI_COLS + 1]);
// What the person is asked for, one line.
const char *field_prompt(field_kind_t kind);

// ------------------------------------------------------------------ lists ---

typedef struct {
    unsigned count;
    unsigned selected;
    unsigned top;        // first row shown
} menu_list_t;

void menu_list_set(menu_list_t *l, unsigned count);
// Move by `delta`, clamped to the ends, scrolling the window so the
// selection stays visible within `visible` rows.
void menu_list_move(menu_list_t *l, int delta, unsigned visible);

// ---------------------------------------------------------------- countdown ---

typedef struct {
    unsigned frames_left;
    int active;
} countdown_t;

void countdown_start(countdown_t *c, unsigned frames);
void countdown_cancel(countdown_t *c);
// Advance one frame.  Returns 1 exactly once, on the frame it expires.
int countdown_tick(countdown_t *c);
// Whole seconds left, rounded up, for the label; 0 when inactive.
unsigned countdown_seconds(const countdown_t *c);

// ---------------------------------------------------------------- the menu ---

typedef enum {
    BOOT_ROW_CONNECT_LAST = 0,
    BOOT_ROW_FIND,
    BOOT_ROW_ADDRESS,
    BOOT_ROW_SAVED,
    BOOT_ROW_WIFI,
} boot_row_t;

#define BOOT_MAIN_ROWS 5

// The main menu's rows for this card.  "Connect to" appears only when the
// last server is still saved and Wi-Fi is set up; the countdown (seconds, 0
// for none) is drawn on it.  Returns the row count.
unsigned bootui_main_rows(const profiles_t *p, unsigned countdown_s,
                          boot_row_t rows[BOOT_MAIN_ROWS],
                          char labels[BOOT_MAIN_ROWS][BOOTUI_COLS + 1]);

// A server as a list row: "name   host:port", with a note on the right
// ("saved", "pair", or nothing).
void bootui_server_row(const char *name, const char *host, uint16_t port,
                       const char *note, char out[BOOTUI_COLS + 1]);

// ------------------------------------------------------------------ Wi-Fi ---

typedef enum {
    AP_OPEN = 0,
    AP_WEP,
    AP_WPA,
    AP_WPA2,
} ap_security_t;

typedef struct {
    char ssid[CFG_SSID_MAX];
    int rssi;               // dBm in DSi mode; a raw 0-255 strength in DS mode
    ap_security_t security;
    // From dswifi's per-mode flags: whether this console, in the mode it is
    // running in, can join it at all, and whether it advertised WPA of any
    // kind.  DS mode reports every WPA2 network as "WPA" (dswifi cannot tell
    // them apart there), so the flag is what decides, not the type.
    uint8_t compatible;
    uint8_t wpa;
    uint8_t auth;           // 0 none, 1 802.1X, 2 PSK, 8 SAE (WPA3)
    uint8_t dsi_mode;       // which scale `rssi` is on
} bootui_ap_t;

#define BOOTUI_APS_MAX 16

// Why a network can or cannot be joined, from this console in this mode.
typedef enum {
    AP_VERDICT_OK = 0,
    AP_VERDICT_NEEDS_DSI,    // WPA/WPA2, and this console is in DS mode
    AP_VERDICT_WEP,          // WEP: refused, it protects nothing
    AP_VERDICT_WPA1,         // WPA (TKIP) in DSi mode
    AP_VERDICT_NOT_PSK,      // Enterprise (802.1X)
    AP_VERDICT_NO_11B,       // open, but no 802.11b rates (DS mode)
} ap_verdict_t;

ap_verdict_t bootui_ap_verdict(const bootui_ap_t *ap);
// The message for a verdict other than OK; `wpa_advice` is the console's own
// (console_wpa_advice) for the NEEDS_DSI case.
const char *bootui_verdict_text(ap_verdict_t v, const char *wpa_advice);

// The three steps a card that has never met a server shows under the menu.
// One copy, so the screenshots in the docs are the text the DS shows.
extern const char BOOTUI_FIRST_BOOT_GUIDE[];

// Whether this console can join it: open or WPA2 (WEP and WPA are refused
// with a reason rather than offered and then failed).
int bootui_ap_supported(ap_security_t s);
// Collapse duplicate SSIDs to their strongest access point, drop hidden ones,
// and sort: the ones this console can join first, strongest first within.
// Returns the new count.
unsigned bootui_aps_tidy(bootui_ap_t *aps, unsigned count);
// "Example Network         WPA2  ####" -- name, security, a four-step bar;
// a network this console cannot join says why instead ("needs DSi mode").
void bootui_ap_row(const bootui_ap_t *ap, char out[BOOTUI_COLS + 1]);

#endif  // DSTERM_BOOTUI_H
