#include "discover.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int discover_probe(char *out, size_t cap, const char *device_id)
{
    int n;
    if (device_id_valid(device_id)) {
        n = snprintf(out, cap, "DSTM probe 1 id=%s", device_id);
    } else {
        n = snprintf(out, cap, "DSTM probe 1");
    }
    if (n < 0 || (size_t)n >= cap) return -1;
    return n;
}

int discover_parse_answer(const uint8_t *data, size_t len, discover_entry_t *out)
{
    if (!data || len == 0 || len > DISCOVER_DATAGRAM_MAX) return 0;
    char text[DISCOVER_DATAGRAM_MAX + 1];
    for (size_t i = 0; i < len; i++) {
        // Printable ASCII only; the server never sends anything else.
        if (data[i] < 0x20 || data[i] > 0x7e) return 0;
        text[i] = (char)data[i];
    }
    text[len] = '\0';
    if (strncmp(text, "DSTM here ", 10) != 0) return 0;

    memset(out, 0, sizeof(*out));
    long port = 0;
    int have_name = 0;
    for (char *w = text + 10; *w; ) {
        // Split on spaces by hand: strtok_r is not in strict C11.
        char *end_of_word = strchr(w, ' ');
        char *after = end_of_word ? end_of_word + 1 : w + strlen(w);
        if (end_of_word) *end_of_word = '\0';
        if (strncmp(w, "name=", 5) == 0 && w[5]) {
            profile_name_from(w + 5, out->name);
            have_name = 1;
        } else if (strncmp(w, "port=", 5) == 0) {
            char *end = NULL;
            port = strtol(w + 5, &end, 10);
            if (!end || *end != '\0') return 0;
        } else if (strcmp(w, "pair=1") == 0) {
            out->pairing = 1;
        }
        w = after;
    }
    if (!have_name || port < 1 || port > 65535) return 0;
    out->port = (uint16_t)port;
    return 1;
}

void discover_list_init(discover_list_t *l)
{
    memset(l, 0, sizeof(*l));
}

int discover_list_add(discover_list_t *l, const discover_entry_t *e)
{
    for (unsigned i = 0; i < l->count; i++) {
        discover_entry_t *have = &l->entries[i];
        if (strcmp(have->host, e->host) == 0 && have->port == e->port) {
            if (!e->swept) *have = *e;          // a broadcast answer knows more
            return 1;
        }
    }
    if (l->count >= DISCOVER_MAX) return 0;
    l->entries[l->count++] = *e;
    return 1;
}

int ipv4_parse(const char *s, uint32_t *out)
{
    if (!addr_valid(s)) return 0;
    uint32_t v = 0;
    const char *p = s;
    for (int part = 0; part < 4; part++) {
        v = (v << 8) | (uint32_t)strtoul(p, NULL, 10);
        p = strchr(p, '.');
        if (p) p++;
    }
    *out = v;
    return 1;
}

void ipv4_format(uint32_t a, char out[CFG_ADDR_MAX])
{
    snprintf(out, CFG_ADDR_MAX, "%u.%u.%u.%u",
             (unsigned)(a >> 24) & 255, (unsigned)(a >> 16) & 255,
             (unsigned)(a >> 8) & 255, (unsigned)a & 255);
}

int discover_sweep_init(discover_sweep_t *s, uint32_t self, uint32_t netmask)
{
    memset(s, 0, sizeof(*s));
    // Narrower than a /24 is honoured; wider is cut to the /24 around us.
    uint32_t mask = netmask | 0xFFFFFF00u;
    // A mask must be contiguous ones; anything else is not a network we can
    // reason about, so fall back to the /24.
    const uint32_t inv = ~mask;
    if ((inv & (inv + 1)) != 0) mask = 0xFFFFFF00u;
    const uint32_t network = self & mask;
    const uint32_t broadcast = network | ~mask;
    if (broadcast - network < 2) return 0;      // a /31 or /32 has no peers
    s->self = self;
    s->next = network + 1;
    s->last = broadcast - 1;
    s->total = (unsigned)(s->last - s->next + 1);
    if (self >= s->next && self <= s->last) s->total--;
    return 1;
}

int discover_sweep_next(discover_sweep_t *s, uint32_t *addr)
{
    while (s->next != 0 && s->next <= s->last) {
        const uint32_t a = s->next++;
        if (a == s->self) continue;
        s->done++;
        *addr = a;
        return 1;
    }
    return 0;
}
