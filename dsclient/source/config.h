// Runtime configuration, read from a file on the SD card.
//
// Nothing here is compiled in. The Wi-Fi passphrase and the pairing key would
// otherwise be extractable from the binary with `strings`, and worse, would end
// up committed the first time anyone pushed a working build. Keeping them on
// the card also means the same .nds runs on a different network without a
// rebuild.
//
// Parsing is split from file I/O so it can be tested on the host.

#ifndef DSTERM_CONFIG_H
#define DSTERM_CONFIG_H

#include <stddef.h>
#include <stdint.h>

#define CFG_SSID_MAX   33   // 802.11 SSID is at most 32 bytes
#define CFG_WIFIKEY_MAX 64  // WPA2 passphrase, 8..63 chars
#define CFG_ADDR_MAX   16   // "255.255.255.255"
#define CFG_PSK_BYTES  16   // pairing key, 32 hex chars

#define CFG_DEVICE_ID_MAX 9  // eight hex digits

// Everything the connection needs, resolved: the Wi-Fi network, the server,
// and the key.  The legacy flat file parses straight into this; the profile
// store (profiles.h) builds one from a Wi-Fi profile and a server profile.
typedef struct {
    char     ssid[CFG_SSID_MAX];
    char     wifi_key[CFG_WIFIKEY_MAX];   // empty for an open network
    // Empty means "ask the router" (DHCP).  The legacy file always sets it.
    char     ip[CFG_ADDR_MAX];
    char     netmask[CFG_ADDR_MAX];
    char     gateway[CFG_ADDR_MAX];
    char     server[CFG_ADDR_MAX];
    uint16_t port;
    uint8_t  psk[CFG_PSK_BYTES];
    int      have_psk;
    // This console's id, announced in HELLO so a paired server checks the
    // right key.  Empty for a console that has never made one.
    char     device_id[CFG_DEVICE_ID_MAX];
} dsterm_config_t;

// Parse key=value lines. Blank lines and those starting with '#' are ignored.
// Returns 1 on success; on failure returns 0 and writes a human-readable reason
// into `err`, which is printed on the DS screen -- so it must say what to fix.
int config_parse(const char *text, dsterm_config_t *cfg, char *err, size_t err_len);

// The pairing key in any of the forms people type it: 32 hex digits, with
// spaces, dashes or colons anywhere.  Returns 1 and fills `out`, or 0 with a
// reason in `err`.
int config_parse_psk(const char *src, uint8_t out[CFG_PSK_BYTES],
                     char *err, size_t err_len);

// Convenience for the DS build: read `path` and parse it.
int config_load(const char *path, dsterm_config_t *cfg, char *err, size_t err_len);

#endif  // DSTERM_CONFIG_H
