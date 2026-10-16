#include "config.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CFG_FILE_MAX 4096

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

static int copy_field(char *dst, size_t cap, const char *src,
                      const char *name, char *err, size_t err_len)
{
    const size_t n = strlen(src);
    if (n == 0) {
        char buf[64];
        snprintf(buf, sizeof(buf), "%s is empty", name);
        fail(err, err_len, buf);
        return 0;
    }
    if (n >= cap) {
        char buf[64];
        snprintf(buf, sizeof(buf), "%s too long (max %u)", name, (unsigned)(cap - 1));
        fail(err, err_len, buf);
        return 0;
    }
    memcpy(dst, src, n + 1);
    return 1;
}

static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

int config_parse_psk(const char *src, uint8_t out[CFG_PSK_BYTES],
                     char *err, size_t err_len)
{
    // Accept the grouped form the server prints ("aabb ccdd ...") so it can be
    // typed off the screen without stripping spaces.
    char packed[CFG_PSK_BYTES * 2 + 1];
    size_t n = 0;
    for (const char *p = src; *p; p++) {
        if (*p == ' ' || *p == '-' || *p == ':') continue;
        if (n >= sizeof(packed) - 1) {
            fail(err, err_len, "psk too long (want 32 hex chars)");
            return 0;
        }
        packed[n++] = *p;
    }
    packed[n] = '\0';

    if (n != CFG_PSK_BYTES * 2) {
        char buf[64];
        snprintf(buf, sizeof(buf), "psk must be 32 hex chars, got %u", (unsigned)n);
        fail(err, err_len, buf);
        return 0;
    }
    for (size_t i = 0; i < CFG_PSK_BYTES; i++) {
        const int hi = hexval(packed[i * 2]);
        const int lo = hexval(packed[i * 2 + 1]);
        if (hi < 0 || lo < 0) {
            fail(err, err_len, "psk contains a non-hex character");
            return 0;
        }
        out[i] = (uint8_t)((hi << 4) | lo);
    }
    return 1;
}

int config_parse(const char *text, dsterm_config_t *cfg, char *err, size_t err_len)
{
    memset(cfg, 0, sizeof(*cfg));
    cfg->port = 8347;
    strcpy(cfg->netmask, "255.255.255.0");

    char line[512];
    const char *p = text;
    int lineno = 0;

    while (*p) {
        const char *nl = strchr(p, '\n');
        size_t len = nl ? (size_t)(nl - p) : strlen(p);
        if (len >= sizeof(line)) len = sizeof(line) - 1;
        memcpy(line, p, len);
        line[len] = '\0';
        p = nl ? nl + 1 : p + strlen(p);
        lineno++;

        char *s = trim(line);
        if (*s == '\0' || *s == '#') continue;

        char *eq = strchr(s, '=');
        if (!eq) {
            char buf[64];
            snprintf(buf, sizeof(buf), "line %d: expected key=value", lineno);
            fail(err, err_len, buf);
            return 0;
        }
        *eq = '\0';
        char *key = trim(s);
        char *val = trim(eq + 1);

        int ok = 1;
        if (strcmp(key, "ssid") == 0) {
            // Not trimmed further: SSIDs may legitimately contain spaces, and
            // plenty of real ones do.
            ok = copy_field(cfg->ssid, sizeof(cfg->ssid), val, "ssid", err, err_len);
        } else if (strcmp(key, "wifi_key") == 0 || strcmp(key, "key") == 0) {
            ok = copy_field(cfg->wifi_key, sizeof(cfg->wifi_key), val, "wifi_key", err, err_len);
        } else if (strcmp(key, "ip") == 0) {
            ok = copy_field(cfg->ip, sizeof(cfg->ip), val, "ip", err, err_len);
        } else if (strcmp(key, "netmask") == 0) {
            ok = copy_field(cfg->netmask, sizeof(cfg->netmask), val, "netmask", err, err_len);
        } else if (strcmp(key, "gateway") == 0) {
            ok = copy_field(cfg->gateway, sizeof(cfg->gateway), val, "gateway", err, err_len);
        } else if (strcmp(key, "server") == 0) {
            ok = copy_field(cfg->server, sizeof(cfg->server), val, "server", err, err_len);
        } else if (strcmp(key, "port") == 0) {
            const long v = strtol(val, NULL, 10);
            if (v < 1 || v > 65535) {
                fail(err, err_len, "port must be 1..65535");
                return 0;
            }
            cfg->port = (uint16_t)v;
        } else if (strcmp(key, "psk") == 0) {
            ok = config_parse_psk(val, cfg->psk, err, err_len);
            cfg->have_psk = ok;
        }
        // Unknown keys are ignored on purpose: a newer config on an older build
        // should still boot rather than refuse.

        if (!ok) return 0;
    }

    // Required fields. Reported one at a time, most fundamental first, because
    // the message goes on a 42-column screen.
    if (cfg->ssid[0] == '\0')     { fail(err, err_len, "missing ssid");    return 0; }
    if (cfg->wifi_key[0] == '\0') { fail(err, err_len, "missing wifi_key"); return 0; }
    if (cfg->ip[0] == '\0')       { fail(err, err_len, "missing ip");      return 0; }
    if (cfg->gateway[0] == '\0')  { fail(err, err_len, "missing gateway"); return 0; }
    if (cfg->server[0] == '\0')   { fail(err, err_len, "missing server");  return 0; }
    if (!cfg->have_psk)           { fail(err, err_len, "missing psk");     return 0; }

    return 1;
}

int config_load(const char *path, dsterm_config_t *cfg, char *err, size_t err_len)
{
    FILE *fh = fopen(path, "rb");
    if (!fh) {
        char buf[96];
        snprintf(buf, sizeof(buf), "cannot open %s", path);
        fail(err, err_len, buf);
        return 0;
    }

    static char text[CFG_FILE_MAX];
    const size_t n = fread(text, 1, sizeof(text) - 1, fh);
    fclose(fh);
    text[n] = '\0';

    return config_parse(text, cfg, err, err_len);
}
