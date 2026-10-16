// The card's own record of where it connects: one Wi-Fi network, any number
// of paired servers (up to PROFILES_MAX), which one was used last, and this
// console's device id.
//
//   /dsterm/wifi.cfg             ssid, wifi_key, ip = dhcp | a.b.c.d, ...
//   /dsterm/servers/<name>.cfg   name, host, port, psk
//   /dsterm/last                 the name of the server to offer first
//   /dsterm/id                   eight hex digits, made once
//
// Written by the console itself -- the boot menu's Wi-Fi wizard and pairing
// -- and readable and editable by a person with a text editor, in the same
// key = value form as the legacy /dsterm.cfg.  That file keeps working: on a
// card with no /dsterm/wifi.cfg it is migrated into this layout on first
// boot, and left where it was.
//
// Every write goes to a temporary file first and replaces the real one only
// once it is complete, so a battery pulled mid-save leaves the old file (or,
// at worst, a complete .tmp the next read recovers).
//
// Parsing and formatting are pure; the file functions use only stdio, dirent
// and mkdir, so the host suite runs them against a temporary directory.
#ifndef DSTERM_PROFILES_H
#define DSTERM_PROFILES_H

#include <stddef.h>
#include <stdint.h>

#include "config.h"

#define PROFILE_NAME_MAX 21          // 20 characters and the NUL
#define PROFILES_MAX 8
#define PROFILE_FILE_MAX 1024
#define PROFILES_ROOT "/dsterm"

typedef struct {
    char ssid[CFG_SSID_MAX];
    char wifi_key[CFG_WIFIKEY_MAX];  // empty: an open network
    int  dhcp;                       // 1: ask the router; 0: the fields below
    char ip[CFG_ADDR_MAX];
    char netmask[CFG_ADDR_MAX];
    char gateway[CFG_ADDR_MAX];
} wifi_profile_t;

typedef struct {
    char     name[PROFILE_NAME_MAX];
    char     host[CFG_ADDR_MAX];
    uint16_t port;
    uint8_t  psk[CFG_PSK_BYTES];
} server_profile_t;

typedef struct {
    int have_wifi;
    wifi_profile_t wifi;
    unsigned count;
    server_profile_t servers[PROFILES_MAX];
    char last[PROFILE_NAME_MAX];
    char device_id[CFG_DEVICE_ID_MAX];
    // Files that existed but did not parse, so the menu can say so rather
    // than silently showing one server fewer.
    unsigned skipped;
} profiles_t;

// ------------------------------------------------------------ validation ---

// A dotted quad with every part 0..255 and nothing else.
int addr_valid(const char *s);
// "a.b.c.d" or "a.b.c.d:port".  Fills host and port (default_port when none
// is given).  Returns 1, or 0 for anything else.
int host_port_parse(const char *in, char host[CFG_ADDR_MAX], uint16_t *port,
                    uint16_t default_port);
// A file-safe, menu-safe name: letters, digits, '.', '-' and '_', at most 20.
// Anything else is dropped; an empty result becomes "server".
void profile_name_from(const char *in, char out[PROFILE_NAME_MAX]);
int device_id_valid(const char *hex);
void device_id_from_bytes(const uint8_t b[4], char out[CFG_DEVICE_ID_MAX]);
int device_id_to_bytes(const char *hex, uint8_t out[4]);

// ------------------------------------------------------- parse and format ---

int wifi_profile_parse(const char *text, wifi_profile_t *w, char *err, size_t n);
// Returns the length written, or -1 if it does not fit or the profile is
// invalid.
int wifi_profile_format(const wifi_profile_t *w, char *out, size_t cap);
int server_profile_parse(const char *text, server_profile_t *s, char *err, size_t n);
int server_profile_format(const server_profile_t *s, char *out, size_t cap);

// The connection a Wi-Fi profile and a server profile make together.
void profiles_resolve(const wifi_profile_t *w, const server_profile_t *s,
                      const char *device_id, dsterm_config_t *out);
// The legacy flat file, split into the two profiles it implies.  The server
// is named after its address.
void profiles_from_legacy(const dsterm_config_t *cfg, wifi_profile_t *w,
                          server_profile_t *s);

// Index of a server by name, or by host and port; -1 if absent.
int profiles_find(const profiles_t *p, const char *name);
int profiles_find_host(const profiles_t *p, const char *host, uint16_t port);

// A saved computer that now answers somewhere else: the router gave it a new
// address.  The index of the saved profile with this `name` (letter case
// ignored) and `port` at a different host -- when nothing is saved at the new
// address already -- or -1.  The name is only a hint: the card's key, which
// a different machine does not have, is what proves it, so callers save the
// new address only after it has authenticated.
int profiles_find_moved(const profiles_t *p, const char *name, const char *host,
                        uint16_t port);

// A profile name that is just an address ("192.168.1.20"): what a card that
// typed the address, or came from the old single file, calls its computer.
// A name the computer announces for itself says more.
int profile_name_is_address(const char *name);

// ----------------------------------------------------------------- files ---

// Load everything under `root`.  Missing files are not errors: a fresh card
// has none of them.  Returns 1; 0 only if `root` itself is unusable.
int profiles_load(const char *root, profiles_t *p);

int profiles_save_wifi(const char *root, const wifi_profile_t *w,
                       char *err, size_t n);
// Saves under the profile's name, replacing a server of the same name.
int profiles_save_server(const char *root, const server_profile_t *s,
                         char *err, size_t n);
int profiles_forget_server(const char *root, const char *name);
int profiles_save_last(const char *root, const char *name);
int profiles_save_device_id(const char *root, const char *hex);

// The write and read every file above goes through.  Exposed for the tests
// and for anything else on the card that wants the same guarantees.
int profiles_write_file(const char *path, const char *text, size_t len);
// Reads at most cap-1 bytes and NUL-terminates.  Falls back to path.tmp when
// path is missing.  Returns the length, or -1 if neither exists.
int profiles_read_file(const char *path, char *buf, size_t cap);

#endif  // DSTERM_PROFILES_H
