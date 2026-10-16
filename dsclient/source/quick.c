#include "quick.h"

#include <stdio.h>
#include <string.h>

static const struct {
    const char *label;
    const char *note;
} ROWS[QUICK_ROW_COUNT] = {
    [QUICK_ROW_MODE]     = { "MODE",     "A: Claude's next mode" },
    [QUICK_ROW_UNDO]     = { "UNDO",     "rewind to an earlier point" },
    [QUICK_ROW_CHANGES]  = { "CHANGES",  "what Claude changed" },
    [QUICK_ROW_COMMANDS] = { "COMMANDS", "slash commands" },
    [QUICK_ROW_AGAIN]    = { "AGAIN",    "your recent prompts" },
    [QUICK_ROW_PHOTO]    = { "PHOTO",    "see it, send it" },
};

unsigned quick_row_count(int camera)
{
    return (unsigned)QUICK_ROW_COUNT - (camera ? 0u : 1u);
}

const char *quick_row_label(quick_row_t row)
{
    return (unsigned)row < QUICK_ROW_COUNT ? ROWS[row].label : "";
}

// Whether this computer can hold a picture is only known when CAMERA_READY
// lands, so PHOTO's note can be replaced by the connection's truth.
static const char *photo_note = NULL;

void quick_set_photo_note(const char *note)
{
    photo_note = note;
}

const char *quick_row_note(quick_row_t row)
{
    if (row == QUICK_ROW_PHOTO && photo_note) return photo_note;
    return (unsigned)row < QUICK_ROW_COUNT ? ROWS[row].note : "";
}

// ------------------------------------------------------------------ MODE ---

static int is_lower(char c) { return c >= 'a' && c <= 'z'; }

// The word before " mode on", or "edits" / "bypass" for the two modes Claude
// Code names another way.
int quick_mode_from_line(const char *line, char out[QUICK_MODE_MAX + 1])
{
    if (!line || !out) return 0;
    if (strstr(line, "accept edits on")) {
        snprintf(out, QUICK_MODE_MAX + 1, "edits");
        return 1;
    }
    if (strstr(line, "bypass permissions on")) {
        snprintf(out, QUICK_MODE_MAX + 1, "bypass");
        return 1;
    }
    const char *at = strstr(line, " mode on");
    if (!at || at == line) {
        // Claude Code versions that name no mode for the ordinary one show
        // only this hint where the mode would be.
        if (strstr(line, "? for shortcuts")) {
            snprintf(out, QUICK_MODE_MAX + 1, "normal");
            return 1;
        }
        return 0;
    }
    const char *start = at;
    while (start > line && is_lower(start[-1])) start--;
    const size_t n = (size_t)(at - start);
    if (n == 0 || n > QUICK_MODE_MAX) return 0;
    memcpy(out, start, n);
    out[n] = '\0';
    return 1;
}

// ------------------------------------------------------------- COMMANDS ---

// Always offered: the ones worth a button and not worth typing.  /clear and
// /compact reach the composer like the rest, so even those take an A.
static const char *const BUILTIN[] = {
    "/compact", "/context", "/clear", "/resume", "/review", "/init",
};
#define BUILTIN_COUNT (sizeof(BUILTIN) / sizeof(BUILTIN[0]))

unsigned quick_commands_builtin(void) { return (unsigned)BUILTIN_COUNT; }

void quick_commands_init(quick_commands_t *c)
{
    if (!c) return;
    memset(c, 0, sizeof(*c));
    for (unsigned i = 0; i < BUILTIN_COUNT; i++) {
        snprintf(c->names[i], sizeof(c->names[i]), "%s", BUILTIN[i]);
    }
    c->count = (unsigned)BUILTIN_COUNT;
}

static int name_char(char ch)
{
    return is_lower(ch) || (ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9') ||
           ch == '_' || ch == '-' || ch == ':' || ch == '.';
}

static int valid_name(const char *s, size_t n)
{
    if (n < 2 || n > QUICK_COMMAND_MAX || s[0] != '/') return 0;
    if (!name_char(s[1]) || s[1] == '-' || s[1] == ':' || s[1] == '.') return 0;
    for (size_t i = 1; i < n; i++) {
        if (!name_char(s[i])) return 0;
    }
    return 1;
}

unsigned quick_commands_apply(quick_commands_t *c, const uint8_t *payload,
                              size_t len)
{
    if (!c) return 0;
    quick_commands_init(c);
    if (!payload) return 0;
    unsigned taken = 0;
    size_t start = 0;
    for (size_t i = 0; i <= len && c->count < QUICK_COMMANDS_MAX; i++) {
        if (i < len && payload[i] != '\n') continue;
        const char *name = (const char *)payload + start;
        const size_t n = i - start;
        start = i + 1;
        if (!valid_name(name, n)) continue;
        int duplicate = 0;
        for (unsigned k = 0; k < c->count; k++) {
            if (strlen(c->names[k]) == n && memcmp(c->names[k], name, n) == 0) {
                duplicate = 1;
                break;
            }
        }
        if (duplicate) continue;
        memcpy(c->names[c->count], name, n);
        c->names[c->count][n] = '\0';
        c->count++;
        taken++;
    }
    return taken;
}

// ---------------------------------------------------------------- AGAIN ---

void quick_recent_init(quick_recent_t *r)
{
    if (r) memset(r, 0, sizeof(*r));
}

int quick_recent_add(quick_recent_t *r, const char *text)
{
    if (!r || !text) return 0;
    size_t n = strlen(text);
    while (n && (text[n - 1] == ' ' || text[n - 1] == '\r' ||
                 text[n - 1] == '\n' || text[n - 1] == '\t')) {
        n--;
    }
    if (n == 0 || n > QUICK_RECENT_TEXT_MAX) return 0;

    // Already there: move it to the front.  Not there: everything moves down
    // one, and the oldest falls off a full list.
    int found = -1;
    for (unsigned i = 0; i < r->count; i++) {
        if (strlen(r->items[i]) == n && memcmp(r->items[i], text, n) == 0) {
            found = (int)i;
            break;
        }
    }
    if (found == 0) return 0;
    unsigned shift;
    if (found > 0) {
        shift = (unsigned)found;
    } else {
        shift = r->count < QUICK_RECENT_MAX ? r->count : QUICK_RECENT_MAX - 1;
        if (r->count < QUICK_RECENT_MAX) r->count++;
    }
    memmove(r->items[1], r->items[0], (size_t)shift * sizeof(r->items[0]));
    memcpy(r->items[0], text, n);
    r->items[0][n] = '\0';
    return 1;
}

int quick_recent_remove(quick_recent_t *r, unsigned index)
{
    if (!r || index >= r->count) return 0;
    memmove(r->items[index], r->items[index + 1],
            (size_t)(r->count - index - 1) * sizeof(r->items[0]));
    r->count--;
    memset(r->items[r->count], 0, sizeof(r->items[0]));
    return 1;
}

size_t quick_recent_format(const quick_recent_t *r, char *out, size_t cap)
{
    if (!r || !out || cap == 0) return 0;
    size_t at = 0;
    for (unsigned i = 0; i < r->count; i++) {
        for (const char *p = r->items[i]; *p; p++) {
            const char *esc = *p == '\n' ? "\\n" : *p == '\\' ? "\\\\" : NULL;
            const size_t need = esc ? 2 : 1;
            if (at + need + 2 > cap) return 0;
            if (esc) {
                out[at++] = esc[0];
                out[at++] = esc[1];
            } else {
                out[at++] = *p;
            }
        }
        if (at + 2 > cap) return 0;
        out[at++] = '\n';
    }
    out[at] = '\0';
    return at;
}

void quick_recent_parse(quick_recent_t *r, const char *text)
{
    if (!r) return;
    quick_recent_init(r);
    if (!text) return;
    const char *line = text;
    while (*line && r->count < QUICK_RECENT_MAX) {
        const char *end = strchr(line, '\n');
        const size_t n = end ? (size_t)(end - line) : strlen(line);
        char decoded[QUICK_RECENT_TEXT_MAX + 1];
        size_t k = 0;
        int ok = 1;
        for (size_t i = 0; i < n && ok; i++) {
            char ch = line[i];
            if (ch == '\\') {
                if (i + 1 >= n) { ok = 0; break; }
                const char next = line[++i];
                ch = next == 'n' ? '\n' : next == '\\' ? '\\' : 0;
                if (!ch) { ok = 0; break; }
            } else if (ch == '\r') {
                continue;
            } else if ((unsigned char)ch < 0x20 || (unsigned char)ch > 0x7e) {
                ok = 0;
                break;
            }
            if (k >= QUICK_RECENT_TEXT_MAX) { ok = 0; break; }
            decoded[k++] = ch;
        }
        decoded[k] = '\0';
        if (ok && k) {
            memcpy(r->items[r->count], decoded, k + 1);
            r->count++;
        }
        if (!end) break;
        line = end + 1;
    }
}

void quick_recent_row(const char *text, char *out, size_t cols)
{
    if (!out || cols == 0) return;
    size_t at = 0;
    for (const char *p = text ? text : ""; *p && at < cols; p++) {
        if (*p == '\n') {
            for (const char *sep = " / "; *sep && at < cols; sep++) out[at++] = *sep;
        } else {
            out[at++] = *p;
        }
    }
    out[at] = '\0';
}

void quick_line_reset(quick_line_t *l)
{
    if (l) memset(l, 0, sizeof(*l));
}

int quick_line_feed(quick_line_t *l, const char *bytes, size_t n,
                    char *out, size_t cap)
{
    if (!l || !bytes) return 0;
    int done = 0;
    for (size_t i = 0; i < n; i++) {
        const unsigned char ch = (unsigned char)bytes[i];
        if (ch == '\r' || ch == '\n') {
            if (!l->lost && l->len && out && cap > l->len) {
                memcpy(out, l->buf, l->len);
                out[l->len] = '\0';
                done = 1;
            }
            quick_line_reset(l);
        } else if (ch == 0x7f || ch == 0x08) {
            if (l->len) l->len--;
        } else if (ch == 0x15 || ch == 0x03) {
            // Ctrl-U clears the line; Ctrl-C abandons it.
            quick_line_reset(l);
        } else if (ch >= 0x20 && ch <= 0x7e) {
            if (l->len < QUICK_RECENT_TEXT_MAX) {
                l->buf[l->len++] = (char)ch;
            } else {
                l->lost = 1;
            }
        } else {
            // Esc (and every arrow, which starts with it), Tab, other control
            // keys: the line on screen is no longer the one typed here.
            l->lost = 1;
        }
    }
    return done;
}
