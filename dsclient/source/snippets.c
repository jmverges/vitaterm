#include "snippets.h"

#include <stdio.h>
#include <string.h>

// Staging for an in-flight pushed set. The wire may deliver chunks in any
// order (TCP preserves order, but the format allows it and the accumulator
// must not depend on it), so each index is recorded in a bitmap and applied
// only when every chunk has arrived.
typedef struct {
    char label[SNIPPET_LABEL_MAX + 1];
    char body[SNIPPET_BODY_MAX + 1];
} staged_t;

static staged_t staged[SNIPPETS_MAX];
static int push_total;           // 0 = no push in flight
static unsigned push_received;   // bitmap of received indexes

// A bad chunk discards the whole in-flight push (docs/design/SNIPPETS_PUSH.md).
// The state must actually go: a stale push_total would reject every chunk
// of the next set forever.
static int push_discard(void)
{
    push_total = 0;
    push_received = 0;
    return -1;
}

static int fail(char *err, size_t cap, int line, const char *why)
{
    if (err && cap) snprintf(err, cap, "line %d: %s", line, why);
    return 0;
}

void snippets_init(snippets_t *s)
{
    memset(s, 0, sizeof(*s));
    push_total = 0;
    push_received = 0;
}

static int decode_body(const char *src, char *dst, int line,
                       char *err, size_t err_len)
{
    size_t n = 0;
    for (size_t i = 0; src[i]; i++) {
        unsigned char ch = (unsigned char)src[i];
        if (ch == '\\') {
            ch = (unsigned char)src[++i];
            if (ch == 'n') ch = '\n';
            else if (ch == 't') ch = '\t';
            else if (ch != '\\') return fail(err, err_len, line, "bad escape");
        }
        if (ch < 0x20 && ch != '\n' && ch != '\t') {
            return fail(err, err_len, line, "control byte in body");
        }
        if (n >= SNIPPET_BODY_MAX) return fail(err, err_len, line, "body too long");
        dst[n++] = (char)ch;
    }
    dst[n] = '\0';
    return 1;
}

int snippets_parse(const char *text, snippets_t *s, char *err, size_t err_len)
{
    snippets_init(s);
    const char *p = text;
    int line_no = 0;
    while (*p) {
        line_no++;
        const char *nl = strchr(p, '\n');
        size_t n = nl ? (size_t)(nl - p) : strlen(p);
        if (n && p[n - 1] == '\r') n--;
        if (n > 511) return fail(err, err_len, line_no, "line too long");
        char line[512];
        memcpy(line, p, n);
        line[n] = '\0';
        p = nl ? nl + 1 : p + strlen(p);

        char *start = line;
        while (*start == ' ' || *start == '\t') start++;
        if (!*start || *start == '#') continue;
        char *eq = strchr(start, '=');
        if (!eq) return fail(err, err_len, line_no, "expected label=body");
        *eq = '\0';
        char *end = eq;
        while (end > start && (end[-1] == ' ' || end[-1] == '\t')) *--end = '\0';
        const size_t label_len = strlen(start);
        if (!label_len || label_len > SNIPPET_LABEL_MAX) {
            return fail(err, err_len, line_no, "bad label length");
        }
        if (s->count >= SNIPPETS_MAX) return fail(err, err_len, line_no, "too many snippets");
        for (int i = 0; i < s->count; i++) {
            if (strcmp(s->items[i].label, start) == 0) {
                return fail(err, err_len, line_no, "duplicate label");
            }
        }
        snippet_t *item = &s->items[s->count];
        memcpy(item->label, start, label_len + 1);
        if (!decode_body(eq + 1, item->body, line_no, err, err_len)) return 0;
        s->count++;
    }
    return 1;
}

int snippets_apply_pushed(snippets_t *s, unsigned index, unsigned total,
                          const char *label, size_t label_len,
                          const char *body, size_t body_len)
{
    // The same limits the file parser enforces, enforced here on the wire;
    // the structural half already ran in proto_apply_snippet, and this is
    // the content rule (printable ASCII plus \n and \t only).
    if (total < 1 || total > SNIPPETS_MAX) return push_discard();
    if (index >= total) return push_discard();
    if (label_len < 1 || label_len > SNIPPET_LABEL_MAX) return push_discard();
    if (body_len > SNIPPET_BODY_MAX) return push_discard();
    for (size_t i = 0; i < body_len; i++) {
        const unsigned char b = (unsigned char)body[i];
        if (!((b >= 0x20 && b <= 0x7e) || b == '\n' || b == '\t'))
            return push_discard();
    }

    if (push_total == 0) {
        push_total = (int)total;
        push_received = 0;
        memset(staged, 0, sizeof(staged));
    } else if (push_total != (int)total) {
        return push_discard();   // a different set arrived mid-flight
    }
    if (push_received & (1u << index)) return push_discard();   // duplicate
    push_received |= (1u << index);

    memcpy(staged[index].label, label, label_len);
    staged[index].label[label_len] = '\0';
    memcpy(staged[index].body, body, body_len);
    staged[index].body[body_len] = '\0';

    if (push_received != (unsigned)((1u << total) - 1)) return 0;  // incomplete

    // Complete and valid: only now does the pushed list replace the current
    // one. Any failure above discarded the whole set, so the card list has
    // stood untouched until this moment.
    s->count = (int)total;
    for (unsigned i = 0; i < total; i++) {
        memcpy(s->items[i].label, staged[i].label, SNIPPET_LABEL_MAX + 1);
        memcpy(s->items[i].body, staged[i].body, SNIPPET_BODY_MAX + 1);
    }
    push_total = 0;
    push_received = 0;
    return 1;
}

int snippet_load_picked(const snippets_t *s, int pick, composer_t *c)
{
    // The composer gate (docs/design/SNIPPETS_PUSH.md). This is the only function
    // that accepts a snippet body, and it hands it to composer_load: the
    // body is shown in full and a second deliberate commit is required
    // before any byte reaches the wire.
    if (!s || pick < 0 || pick >= s->count) return 0;
    return composer_load(c, s->items[pick].body);
}

int snippets_load(const char *path, snippets_t *s, char *err, size_t err_len)
{
    FILE *fh = fopen(path, "rb");
    if (!fh) {
        if (err && err_len) snprintf(err, err_len, "cannot open %s", path);
        return 0;
    }
    char text[SNIPPETS_FILE_MAX + 1];
    const size_t n = fread(text, 1, SNIPPETS_FILE_MAX + 1, fh);
    fclose(fh);
    if (n > SNIPPETS_FILE_MAX) {
        if (err && err_len) snprintf(err, err_len, "snippet file too large");
        return 0;
    }
    text[n] = '\0';
    return snippets_parse(text, s, err, err_len);
}
