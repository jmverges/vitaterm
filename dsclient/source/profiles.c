#include "profiles.h"

#include <sys/types.h>   // before dirent.h: newlib's needs off_t
#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

static void fail(char *err, size_t n, const char *msg)
{
    if (err && n) {
        strncpy(err, msg, n - 1);
        err[n - 1] = '\0';
    }
}

static char *trim(char *s)
{
    while (*s == ' ' || *s == '\t') s++;
    char *end = s + strlen(s);
    while (end > s && (end[-1] == ' ' || end[-1] == '\t' ||
                       end[-1] == '\r' || end[-1] == '\n')) {
        *--end = '\0';
    }
    return s;
}

// Copy with a bound; 0 if it would not fit.  Empty is allowed here and
// rejected (or not) by the caller, which knows whether the field may be empty.
static int put(char *dst, size_t cap, const char *src)
{
    const size_t n = strlen(src);
    if (n >= cap) return 0;
    memcpy(dst, src, n + 1);
    return 1;
}

// ------------------------------------------------------------ validation ---

int addr_valid(const char *s)
{
    if (!s) return 0;
    int parts = 0;
    const char *p = s;
    while (parts < 4) {
        if (*p < '0' || *p > '9') return 0;
        int value = 0, digits = 0;
        while (*p >= '0' && *p <= '9') {
            value = value * 10 + (*p - '0');
            if (++digits > 3) return 0;
            p++;
        }
        if (value > 255) return 0;
        parts++;
        if (parts < 4) {
            if (*p != '.') return 0;
            p++;
        }
    }
    return *p == '\0';
}

int host_port_parse(const char *in, char host[CFG_ADDR_MAX], uint16_t *port,
                    uint16_t default_port)
{
    if (!in) return 0;
    char buf[CFG_ADDR_MAX + 8];
    if (strlen(in) >= sizeof(buf)) return 0;
    strcpy(buf, in);
    char *colon = strchr(buf, ':');
    long value = default_port;
    if (colon) {
        *colon = '\0';
        const char *digits = colon + 1;
        if (!*digits || strlen(digits) > 5) return 0;
        for (const char *d = digits; *d; d++) {
            if (*d < '0' || *d > '9') return 0;
        }
        value = strtol(digits, NULL, 10);
        if (value < 1 || value > 65535) return 0;
    }
    if (!addr_valid(buf)) return 0;
    strcpy(host, buf);
    *port = (uint16_t)value;
    return 1;
}

void profile_name_from(const char *in, char out[PROFILE_NAME_MAX])
{
    size_t n = 0;
    for (const char *p = in ? in : ""; *p && n < PROFILE_NAME_MAX - 1; p++) {
        const char c = *p;
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
            (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.') {
            out[n++] = c;
        }
    }
    out[n] = '\0';
    // A name of only dots would be "." or ".." on disk.
    int only_dots = 1;
    for (size_t i = 0; i < n; i++) {
        if (out[i] != '.') only_dots = 0;
    }
    if (n == 0 || only_dots) strcpy(out, "server");
}

int device_id_valid(const char *hex)
{
    if (!hex) return 0;
    for (int i = 0; i < 8; i++) {
        const char c = hex[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return 0;
    }
    return hex[8] == '\0';
}

void device_id_from_bytes(const uint8_t b[4], char out[CFG_DEVICE_ID_MAX])
{
    static const char digits[] = "0123456789abcdef";
    for (int i = 0; i < 4; i++) {
        out[2 * i] = digits[b[i] >> 4];
        out[2 * i + 1] = digits[b[i] & 15];
    }
    out[8] = '\0';
}

int device_id_to_bytes(const char *hex, uint8_t out[4])
{
    if (!device_id_valid(hex)) return 0;
    for (int i = 0; i < 4; i++) {
        const char hi = hex[2 * i], lo = hex[2 * i + 1];
        const int h = hi <= '9' ? hi - '0' : hi - 'a' + 10;
        const int l = lo <= '9' ? lo - '0' : lo - 'a' + 10;
        out[i] = (uint8_t)((h << 4) | l);
    }
    return 1;
}

// ------------------------------------------------------- parse and format ---

// Walk key = value lines, calling `field` for each.  The same rules as the
// legacy file: blank lines and # comments skipped, unknown keys ignored by
// the callback, a line with no '=' an error.
typedef int (*field_fn)(void *ctx, const char *key, const char *val,
                        char *err, size_t n);

static int walk(const char *text, void *ctx, field_fn field, char *err, size_t n)
{
    char line[256];
    const char *p = text ? text : "";
    int lineno = 0;
    while (*p) {
        const char *start = p;
        const char *nl = strchr(p, '\n');
        size_t len = nl ? (size_t)(nl - p) : strlen(p);
        p = nl ? nl + 1 : p + len;
        lineno++;
        if (len >= sizeof(line)) {
            char buf[48];
            snprintf(buf, sizeof(buf), "line %d too long", lineno);
            fail(err, n, buf);
            return 0;
        }
        memcpy(line, start, len);
        line[len] = '\0';
        char *s = trim(line);
        if (*s == '\0' || *s == '#') continue;
        char *eq = strchr(s, '=');
        if (!eq) {
            char buf[48];
            snprintf(buf, sizeof(buf), "line %d: expected key=value", lineno);
            fail(err, n, buf);
            return 0;
        }
        *eq = '\0';
        if (!field(ctx, trim(s), trim(eq + 1), err, n)) return 0;
    }
    return 1;
}

static int too_long(const char *what, char *err, size_t n)
{
    char buf[48];
    snprintf(buf, sizeof(buf), "%s too long", what);
    fail(err, n, buf);
    return 0;
}

static int wifi_field(void *ctx, const char *key, const char *val,
                      char *err, size_t n)
{
    wifi_profile_t *w = ctx;
    if (strcmp(key, "ssid") == 0) {
        if (!put(w->ssid, sizeof(w->ssid), val)) return too_long("ssid", err, n);
    } else if (strcmp(key, "wifi_key") == 0 || strcmp(key, "key") == 0) {
        if (!put(w->wifi_key, sizeof(w->wifi_key), val)) {
            return too_long("wifi_key", err, n);
        }
    } else if (strcmp(key, "ip") == 0) {
        if (strcmp(val, "dhcp") == 0 || val[0] == '\0') {
            w->dhcp = 1;
            w->ip[0] = '\0';
        } else {
            if (!put(w->ip, sizeof(w->ip), val)) return too_long("ip", err, n);
            w->dhcp = 0;
        }
    } else if (strcmp(key, "netmask") == 0) {
        if (!put(w->netmask, sizeof(w->netmask), val)) return too_long("netmask", err, n);
    } else if (strcmp(key, "gateway") == 0) {
        if (!put(w->gateway, sizeof(w->gateway), val)) return too_long("gateway", err, n);
    }
    return 1;
}

int wifi_profile_parse(const char *text, wifi_profile_t *w, char *err, size_t n)
{
    memset(w, 0, sizeof(*w));
    w->dhcp = 1;
    strcpy(w->netmask, "255.255.255.0");
    if (!walk(text, w, wifi_field, err, n)) return 0;
    if (w->ssid[0] == '\0') { fail(err, n, "missing ssid"); return 0; }
    const size_t keylen = strlen(w->wifi_key);
    if (keylen != 0 && (keylen < 8 || keylen > 63)) {
        fail(err, n, "wifi_key must be 8..63 characters (or empty if open)");
        return 0;
    }
    if (!w->dhcp) {
        if (!addr_valid(w->ip))      { fail(err, n, "ip is not an address");      return 0; }
        if (!addr_valid(w->netmask)) { fail(err, n, "netmask is not an address"); return 0; }
        if (!addr_valid(w->gateway)) { fail(err, n, "gateway is not an address"); return 0; }
    }
    return 1;
}

int wifi_profile_format(const wifi_profile_t *w, char *out, size_t cap)
{
    if (!w || w->ssid[0] == '\0') return -1;
    int len;
    if (w->dhcp) {
        len = snprintf(out, cap,
                       "# DS Claude Terminal: the Wi-Fi network.\n"
                       "# Written by the console's Wi-Fi menu; safe to edit.\n"
                       "ssid = %s\nwifi_key = %s\nip = dhcp\n",
                       w->ssid, w->wifi_key);
    } else {
        len = snprintf(out, cap,
                       "# DS Claude Terminal: the Wi-Fi network.\n"
                       "# Written by the console's Wi-Fi menu; safe to edit.\n"
                       "ssid = %s\nwifi_key = %s\nip = %s\nnetmask = %s\n"
                       "gateway = %s\n",
                       w->ssid, w->wifi_key, w->ip, w->netmask, w->gateway);
    }
    if (len < 0 || (size_t)len >= cap) return -1;
    return len;
}

typedef struct {
    server_profile_t *s;
    int have_psk;
} server_ctx_t;

static int server_field(void *ctx, const char *key, const char *val,
                        char *err, size_t n)
{
    server_ctx_t *c = ctx;
    server_profile_t *s = c->s;
    if (strcmp(key, "name") == 0) {
        profile_name_from(val, s->name);
    } else if (strcmp(key, "host") == 0 || strcmp(key, "server") == 0) {
        if (!put(s->host, sizeof(s->host), val)) return too_long("host", err, n);
    } else if (strcmp(key, "port") == 0) {
        const long v = strtol(val, NULL, 10);
        if (v < 1 || v > 65535) { fail(err, n, "port must be 1..65535"); return 0; }
        s->port = (uint16_t)v;
    } else if (strcmp(key, "psk") == 0) {
        if (!config_parse_psk(val, s->psk, err, n)) return 0;
        c->have_psk = 1;
    }
    return 1;
}

int server_profile_parse(const char *text, server_profile_t *s, char *err, size_t n)
{
    memset(s, 0, sizeof(*s));
    s->port = 8347;
    server_ctx_t ctx = { s, 0 };
    if (!walk(text, &ctx, server_field, err, n)) return 0;
    if (!addr_valid(s->host)) { fail(err, n, "host is not an address"); return 0; }
    if (!ctx.have_psk)        { fail(err, n, "missing psk");            return 0; }
    if (s->name[0] == '\0') profile_name_from(s->host, s->name);
    return 1;
}

int server_profile_format(const server_profile_t *s, char *out, size_t cap)
{
    if (!s || !addr_valid(s->host) || s->name[0] == '\0') return -1;
    char hex[CFG_PSK_BYTES * 2 + 1];
    for (int i = 0; i < CFG_PSK_BYTES; i++) {
        snprintf(hex + 2 * i, 3, "%02x", s->psk[i]);
    }
    const int len = snprintf(out, cap,
                             "# DS Claude Terminal: one paired server.\n"
                             "# The psk is this console's key for it; keep "
                             "this file on the card.\n"
                             "name = %s\nhost = %s\nport = %u\npsk = %s\n",
                             s->name, s->host, (unsigned)s->port, hex);
    if (len < 0 || (size_t)len >= cap) return -1;
    return len;
}

void profiles_resolve(const wifi_profile_t *w, const server_profile_t *s,
                      const char *device_id, dsterm_config_t *out)
{
    memset(out, 0, sizeof(*out));
    strcpy(out->ssid, w->ssid);
    strcpy(out->wifi_key, w->wifi_key);
    if (!w->dhcp) {
        strcpy(out->ip, w->ip);
        strcpy(out->gateway, w->gateway);
    }
    strcpy(out->netmask, w->netmask[0] ? w->netmask : "255.255.255.0");
    strcpy(out->server, s->host);
    out->port = s->port;
    memcpy(out->psk, s->psk, CFG_PSK_BYTES);
    out->have_psk = 1;
    if (device_id_valid(device_id)) strcpy(out->device_id, device_id);
}

void profiles_from_legacy(const dsterm_config_t *cfg, wifi_profile_t *w,
                          server_profile_t *s)
{
    memset(w, 0, sizeof(*w));
    strcpy(w->ssid, cfg->ssid);
    strcpy(w->wifi_key, cfg->wifi_key);
    w->dhcp = cfg->ip[0] == '\0';
    strcpy(w->ip, cfg->ip);
    strcpy(w->netmask, cfg->netmask[0] ? cfg->netmask : "255.255.255.0");
    strcpy(w->gateway, cfg->gateway);

    memset(s, 0, sizeof(*s));
    profile_name_from(cfg->server, s->name);
    strcpy(s->host, cfg->server);
    s->port = cfg->port ? cfg->port : 8347;
    memcpy(s->psk, cfg->psk, CFG_PSK_BYTES);
}

int profiles_find(const profiles_t *p, const char *name)
{
    for (unsigned i = 0; i < p->count; i++) {
        if (strcmp(p->servers[i].name, name) == 0) return (int)i;
    }
    return -1;
}

int profiles_find_host(const profiles_t *p, const char *host, uint16_t port)
{
    for (unsigned i = 0; i < p->count; i++) {
        if (strcmp(p->servers[i].host, host) == 0 && p->servers[i].port == port) {
            return (int)i;
        }
    }
    return -1;
}

static int same_name(const char *a, const char *b)
{
    for (; *a && *b; a++, b++) {
        const char x = (*a >= 'A' && *a <= 'Z') ? (char)(*a - 'A' + 'a') : *a;
        const char y = (*b >= 'A' && *b <= 'Z') ? (char)(*b - 'A' + 'a') : *b;
        if (x != y) return 0;
    }
    return *a == *b;
}

int profiles_find_moved(const profiles_t *p, const char *name, const char *host,
                        uint16_t port)
{
    if (!p || !name || !name[0] || !host || !host[0]) return -1;
    if (profiles_find_host(p, host, port) >= 0) return -1;
    for (unsigned i = 0; i < p->count; i++) {
        const server_profile_t *s = &p->servers[i];
        if (s->port == port && strcmp(s->host, host) != 0 && same_name(s->name, name)) {
            return (int)i;
        }
    }
    return -1;
}

int profile_name_is_address(const char *name)
{
    if (!name || !name[0]) return 0;
    for (const char *c = name; *c; c++) {
        if (!((*c >= '0' && *c <= '9') || *c == '.' || *c == '-')) return 0;
    }
    return 1;
}

// ----------------------------------------------------------------- files ---

int profiles_write_file(const char *path, const char *text, size_t len)
{
    char temporary[160];
    const int tn = snprintf(temporary, sizeof(temporary), "%s.tmp", path);
    if (tn < 0 || (size_t)tn >= sizeof(temporary)) return 0;
    (void)remove(temporary);
    FILE *fh = fopen(temporary, "wb");
    if (!fh) return 0;
    int ok = fwrite(text, 1, len, fh) == len;
    if (fflush(fh) != 0) ok = 0;
    if (fclose(fh) != 0) ok = 0;
    if (!ok) {
        (void)remove(temporary);
        return 0;
    }
    // libfat's rename refuses to replace its destination (see settings.c), so
    // clear the way first.  Between the two calls only the .tmp exists, and
    // profiles_read_file() falls back to it.
    (void)remove(path);
    return rename(temporary, path) == 0;
}

int profiles_read_file(const char *path, char *buf, size_t cap)
{
    if (cap == 0) return -1;
    FILE *fh = fopen(path, "rb");
    if (!fh) {
        char temporary[160];
        const int tn = snprintf(temporary, sizeof(temporary), "%s.tmp", path);
        if (tn < 0 || (size_t)tn >= sizeof(temporary)) return -1;
        fh = fopen(temporary, "rb");
        if (!fh) return -1;
    }
    const size_t n = fread(buf, 1, cap - 1, fh);
    fclose(fh);
    buf[n] = '\0';
    return (int)n;
}

static int join(char *out, size_t cap, const char *a, const char *b)
{
    const int n = snprintf(out, cap, "%s/%s", a, b);
    return n > 0 && (size_t)n < cap;
}

static void ensure_dir(const char *path)
{
    // Already existing is the usual case and not an error; anything else
    // shows up as the write that follows failing.
    (void)mkdir(path, 0700);
}

static int read_line_file(const char *path, char *out, size_t cap)
{
    char buf[64];
    if (profiles_read_file(path, buf, sizeof(buf)) < 0) return 0;
    char *s = trim(buf);
    char *nl = strchr(s, '\n');
    if (nl) *nl = '\0';
    s = trim(s);
    if (strlen(s) >= cap) return 0;
    strcpy(out, s);
    return 1;
}

static int ends_with_cfg(const char *name)
{
    const size_t n = strlen(name);
    return n > 4 && strcmp(name + n - 4, ".cfg") == 0;
}

int profiles_load(const char *root, profiles_t *p)
{
    memset(p, 0, sizeof(*p));
    static char text[PROFILE_FILE_MAX];
    char path[160];
    char err[64];

    if (!join(path, sizeof(path), root, "wifi.cfg")) return 0;
    if (profiles_read_file(path, text, sizeof(text)) >= 0 &&
        wifi_profile_parse(text, &p->wifi, err, sizeof(err))) {
        p->have_wifi = 1;
    }

    char dir[160];
    if (!join(dir, sizeof(dir), root, "servers")) return 0;
    DIR *d = opendir(dir);
    if (d) {
        struct dirent *e;
        while ((e = readdir(d)) != NULL) {
            if (!ends_with_cfg(e->d_name)) continue;
            if (p->count >= PROFILES_MAX) { p->skipped++; continue; }
            if (!join(path, sizeof(path), dir, e->d_name)) { p->skipped++; continue; }
            server_profile_t *s = &p->servers[p->count];
            if (profiles_read_file(path, text, sizeof(text)) >= 0 &&
                server_profile_parse(text, s, err, sizeof(err))) {
                // The file name is the identity: a hand-edited name = line
                // that disagrees with it would make "forget" miss the file.
                char stem[PROFILE_NAME_MAX];
                const size_t n = strlen(e->d_name) - 4;
                if (n < sizeof(stem)) {
                    memcpy(stem, e->d_name, n);
                    stem[n] = '\0';
                    profile_name_from(stem, s->name);
                }
                if (profiles_find(p, s->name) < 0) p->count++;
                else p->skipped++;
            } else {
                p->skipped++;
            }
        }
        closedir(d);
    }

    if (join(path, sizeof(path), root, "last")) {
        char last[PROFILE_NAME_MAX];
        if (read_line_file(path, last, sizeof(last))) {
            profile_name_from(last, p->last);
        }
    }
    if (join(path, sizeof(path), root, "id")) {
        char id[CFG_DEVICE_ID_MAX];
        if (read_line_file(path, id, sizeof(id)) && device_id_valid(id)) {
            strcpy(p->device_id, id);
        }
    }
    return 1;
}

int profiles_save_wifi(const char *root, const wifi_profile_t *w,
                       char *err, size_t n)
{
    char text[PROFILE_FILE_MAX], path[160];
    const int len = wifi_profile_format(w, text, sizeof(text));
    if (len < 0) { fail(err, n, "cannot format the Wi-Fi profile"); return 0; }
    ensure_dir(root);
    if (!join(path, sizeof(path), root, "wifi.cfg") ||
        !profiles_write_file(path, text, (size_t)len)) {
        fail(err, n, "cannot write wifi.cfg");
        return 0;
    }
    return 1;
}

int profiles_save_server(const char *root, const server_profile_t *s,
                         char *err, size_t n)
{
    char text[PROFILE_FILE_MAX], dir[160], file[PROFILE_NAME_MAX + 4], path[160];
    const int len = server_profile_format(s, text, sizeof(text));
    if (len < 0) { fail(err, n, "cannot format the server profile"); return 0; }
    ensure_dir(root);
    if (!join(dir, sizeof(dir), root, "servers")) { fail(err, n, "path too long"); return 0; }
    ensure_dir(dir);
    snprintf(file, sizeof(file), "%s.cfg", s->name);
    if (!join(path, sizeof(path), dir, file) ||
        !profiles_write_file(path, text, (size_t)len)) {
        fail(err, n, "cannot write the server profile");
        return 0;
    }
    return 1;
}

int profiles_forget_server(const char *root, const char *name)
{
    char clean[PROFILE_NAME_MAX], file[PROFILE_NAME_MAX + 4], dir[160], path[160];
    profile_name_from(name, clean);
    snprintf(file, sizeof(file), "%s.cfg", clean);
    if (!join(dir, sizeof(dir), root, "servers") ||
        !join(path, sizeof(path), dir, file)) {
        return 0;
    }
    char temporary[170];
    snprintf(temporary, sizeof(temporary), "%s.tmp", path);
    (void)remove(temporary);
    return remove(path) == 0;
}

int profiles_save_last(const char *root, const char *name)
{
    char path[160], text[PROFILE_NAME_MAX + 2];
    char clean[PROFILE_NAME_MAX];
    profile_name_from(name, clean);
    const int len = snprintf(text, sizeof(text), "%s\n", clean);
    ensure_dir(root);
    return join(path, sizeof(path), root, "last") &&
           profiles_write_file(path, text, (size_t)len);
}

int profiles_save_device_id(const char *root, const char *hex)
{
    if (!device_id_valid(hex)) return 0;
    char path[160], text[CFG_DEVICE_ID_MAX + 1];
    const int len = snprintf(text, sizeof(text), "%s\n", hex);
    ensure_dir(root);
    return join(path, sizeof(path), root, "id") &&
           profiles_write_file(path, text, (size_t)len);
}
