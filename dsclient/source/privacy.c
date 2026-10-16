#include "privacy.h"

#include <string.h>

static int hiding;

void privacy_set_hide(int hide) { hiding = hide ? 1 : 0; }
int privacy_hiding(void) { return hiding; }

static int is_digit(char c) { return c >= '0' && c <= '9'; }
static int is_hex(char c)
{
    return is_digit(c) || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

// A dotted quad starting at `s`: four runs of one to three digits, each at
// most 255, joined by dots, and not glued to more digits or another dot-digit
// on either side (so "1.2.3.4.5" and version strings like "v1.2.3" are left
// alone).  Returns its length, or 0.
static size_t quad_at(const char *text, size_t at)
{
    if (at > 0 && (is_digit(text[at - 1]) ||
                   (text[at - 1] == '.' && at > 1 && is_digit(text[at - 2])))) {
        return 0;
    }
    size_t i = at;
    for (int part = 0; part < 4; part++) {
        if (part > 0) {
            if (text[i] != '.') return 0;
            i++;
        }
        unsigned value = 0;
        size_t digits = 0;
        while (is_digit(text[i]) && digits < 4) {
            value = value * 10u + (unsigned)(text[i] - '0');
            i++;
            digits++;
        }
        if (digits == 0 || digits > 3 || value > 255) return 0;
    }
    if (is_digit(text[i])) return 0;
    if (text[i] == '.' && is_digit(text[i + 1])) return 0;
    return i - at;
}

// aa:bb:cc:dd:ee:ff (or with dashes).
static size_t mac_at(const char *text, size_t at)
{
    if (at > 0 && (is_hex(text[at - 1]) || text[at - 1] == ':')) return 0;
    size_t i = at;
    char sep = 0;
    for (int part = 0; part < 6; part++) {
        if (part > 0) {
            if (text[i] != ':' && text[i] != '-') return 0;
            if (sep && text[i] != sep) return 0;
            sep = text[i];
            i++;
        }
        if (!is_hex(text[i]) || !is_hex(text[i + 1])) return 0;
        i += 2;
    }
    if (is_hex(text[i]) || text[i] == ':') return 0;
    return i - at;
}

void privacy_mask(char *text)
{
    if (!hiding || !text) return;
    for (size_t i = 0; text[i]; i++) {
        size_t n = 0;
        if (is_digit(text[i])) n = quad_at(text, i);
        if (!n && is_hex(text[i])) n = mac_at(text, i);
        if (!n) continue;
        for (size_t k = 0; k < n; k++) {
            if (text[i + k] != '.' && text[i + k] != ':' && text[i + k] != '-') {
                text[i + k] = '*';
            }
        }
        i += n - 1;
    }
}

#define PRIVACY_BUFFERS 4
#define PRIVACY_TEXT_MAX 768

const char *privacy_text(const char *text)
{
    if (!hiding || !text) return text;
    static char buffers[PRIVACY_BUFFERS][PRIVACY_TEXT_MAX];
    static unsigned next;
    char *out = buffers[next++ % PRIVACY_BUFFERS];
    size_t n = strlen(text);
    if (n >= PRIVACY_TEXT_MAX) n = PRIVACY_TEXT_MAX - 1;
    memcpy(out, text, n);
    out[n] = '\0';
    privacy_mask(out);
    return out;
}
