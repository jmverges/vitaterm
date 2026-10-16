#include "remote.h"

#include <stdio.h>
#include <string.h>

static void sanitize(char *out, size_t cap, const char *src, size_t len)
{
    if (!cap) return;
    size_t n = len < cap - 1 ? len : cap - 1;
    for (size_t i = 0; i < n; i++) {
        const unsigned char ch = (unsigned char)(src ? src[i] : 0);
        out[i] = (ch >= 0x20 && ch <= 0x7e) ? (char)ch : '?';
    }
    out[n] = '\0';
}

void remote_init(remote_list_t *r)
{
    if (!r) return;
    memset(r, 0, sizeof(*r));
}

int remote_apply_chunk(remote_list_t *r, const remote_chunk_t *chunk)
{
    if (!r || !chunk || chunk->count > REMOTE_MAX) return -1;
    if (chunk->count == 0) {
        r->count = 0;
        r->status = chunk->status;
        r->ready = 1;
        r->staged_count = 0;
        r->staged_received = 0;
        return 1;
    }
    const remote_session_t *e = &chunk->entry;
    if (e->index >= chunk->count) return -1;
    // A chunk for a different set size than the one in flight starts over:
    // the server began a fresh snapshot, and the old half is worthless.
    if (r->staged_count != chunk->count) {
        r->staged_count = chunk->count;
        r->staged_received = 0;
    }
    const uint8_t bit = (uint8_t)(1u << e->index);
    if (r->staged_received & bit) {
        // The same row twice: a new snapshot of the same size has begun.
        r->staged_received = 0;
    }
    r->staged[e->index] = *e;
    r->staged_received |= bit;
    const uint8_t full = (uint8_t)((1u << chunk->count) - 1u);
    if (r->staged_received != full) return 0;

    memcpy(r->items, r->staged, sizeof(r->items[0]) * chunk->count);
    r->count = chunk->count;
    r->status = REMOTE_STATUS_NONE;
    r->ready = 1;
    r->staged_count = 0;
    r->staged_received = 0;
    return 1;
}

void remote_set_note(remote_list_t *r, const char *text, size_t len)
{
    if (!r) return;
    sanitize(r->note, sizeof(r->note), text, len);
}

uint16_t remote_row_check(const remote_session_t *row)
{
    uint32_t h = 0x811C9DC5u;
    if (!row) return 0;
    for (const char *p = row->name; *p; p++) {
        h ^= (uint8_t)*p;
        h *= 0x01000193u;
    }
    h ^= 0;
    h *= 0x01000193u;
    for (const char *p = row->detail; *p; p++) {
        h ^= (uint8_t)*p;
        h *= 0x01000193u;
    }
    return (uint16_t)((h >> 16) ^ (h & 0xFFFFu));
}

unsigned remote_needs_you(const remote_list_t *r)
{
    unsigned n = 0;
    if (!r) return 0;
    for (unsigned i = 0; i < r->count; i++) {
        if (r->items[i].state == REMOTE_STATE_NEEDS_YOU) n++;
    }
    return n;
}

const char *remote_state_word(uint8_t state)
{
    switch (state) {
    case REMOTE_STATE_WORKING:   return "WORKING";
    case REMOTE_STATE_NEEDS_YOU: return "NEEDS YOU";
    case REMOTE_STATE_DONE:      return "DONE";
    case REMOTE_STATE_IDLE:      return "IDLE";
    case REMOTE_STATE_FAILED:    return "FAILED";
    case REMOTE_STATE_STOPPED:   return "STOPPED";
    default:                     return "?";
    }
}

// ------------------------------------------------------------ the rows ---

void remote_ui_init(remote_ui_t *ui)
{
    if (!ui) return;
    ui->selection = -1;
    ui->copy_armed = 0;
}

static unsigned push(remote_row_t *rows, unsigned n, unsigned max,
                     remote_row_kind_t kind, unsigned index)
{
    if (n >= max) return n;
    rows[n].kind = (uint8_t)kind;
    rows[n].index = (uint8_t)index;
    return n + 1;
}

unsigned remote_rows(const remote_list_t *r, const remote_computers_t *c,
                     remote_row_t *rows, unsigned max)
{
    unsigned n = 0;
    n = push(rows, n, max, REMOTE_ROW_HEADER, 0);
    if (r && r->count) {
        for (unsigned i = 0; i < r->count; i++) {
            n = push(rows, n, max, REMOTE_ROW_SESSION, i);
        }
    } else {
        n = push(rows, n, max, REMOTE_ROW_NOTE, 0);
    }
    // A new session is always offerable once the server has shown it speaks
    // REMOTE, and only then: before that, a pick would be a message an older
    // server drops the connection over.
    if (r && r->ready && r->status != REMOTE_STATUS_NO_CLAUDE) {
        n = push(rows, n, max, REMOTE_ROW_NEW, 0);
    }
    n = push(rows, n, max, REMOTE_ROW_HEADER, 1);
    if (c && c->count) {
        for (unsigned i = 0; i < c->count; i++) {
            n = push(rows, n, max, REMOTE_ROW_COMPUTER, i);
        }
    } else {
        n = push(rows, n, max, REMOTE_ROW_NOTE, 1);
    }
    return n;
}

void remote_ui_follow(remote_ui_t *ui, const remote_list_t *r,
                      const remote_computers_t *c, const char *name,
                      const char *detail)
{
    if (!ui) return;
    ui->copy_armed = 0;
    remote_row_t rows[REMOTE_ROWS_MAX];
    const unsigned n = remote_rows(r, c, rows, REMOTE_ROWS_MAX);
    if (name && r) {
        for (unsigned i = 0; i < n; i++) {
            if (rows[i].kind != REMOTE_ROW_SESSION) continue;
            const remote_session_t *s = &r->items[rows[i].index];
            if (strcmp(s->name, name) == 0 &&
                strcmp(s->detail, detail ? detail : "") == 0) {
                ui->selection = (int)i;
                return;
            }
        }
    }
    remote_ui_settle(ui, rows, n);
}

int remote_row_selectable(const remote_row_t *row)
{
    return row && (row->kind == REMOTE_ROW_SESSION ||
                   row->kind == REMOTE_ROW_NEW ||
                   row->kind == REMOTE_ROW_COMPUTER);
}

void remote_ui_settle(remote_ui_t *ui, const remote_row_t *rows, unsigned n)
{
    if (!ui) return;
    if (ui->selection >= 0 && (unsigned)ui->selection < n &&
        remote_row_selectable(&rows[ui->selection])) {
        return;
    }
    ui->selection = -1;
    ui->copy_armed = 0;
    for (unsigned i = 0; i < n; i++) {
        if (remote_row_selectable(&rows[i])) {
            ui->selection = (int)i;
            return;
        }
    }
}

int remote_ui_move(remote_ui_t *ui, const remote_row_t *rows, unsigned n,
                   int delta)
{
    if (!ui || !n || !delta) return 0;
    remote_ui_settle(ui, rows, n);
    if (ui->selection < 0) return 0;
    const int step = delta > 0 ? 1 : -1;
    int at = ui->selection;
    for (unsigned tries = 0; tries < n; tries++) {
        at = (at + step + (int)n) % (int)n;
        if (remote_row_selectable(&rows[at])) break;
    }
    if (at == ui->selection) return 0;
    ui->selection = at;
    ui->copy_armed = 0;
    return 1;
}

static void format_age(unsigned minutes, char out[8])
{
    // The wire carries at most 65535 minutes (45 days), but gcc sizes %u at
    // ten digits; the clamps are what let it prove every case fits.
    if (minutes > 0xFFFFu) minutes = 0xFFFFu;
    if (minutes < 60) snprintf(out, 8, "%um", minutes % 60u);
    else if (minutes < 60 * 48) snprintf(out, 8, "%uh", (minutes / 60u) % 100u);
    else snprintf(out, 8, "%ud", (minutes / (60u * 24u)) % 1000u);
}

static void pad(char *out, size_t cols)
{
    size_t len = strlen(out);
    if (len > cols) len = cols;
    while (len < cols) out[len++] = ' ';
    out[cols] = '\0';
}

static void upper_copy(char *out, size_t cap, const char *in)
{
    size_t i = 0;
    for (; in && in[i] && i + 1 < cap; i++) {
        const char ch = in[i];
        out[i] = (ch >= 'a' && ch <= 'z') ? (char)(ch - 32) : ch;
    }
    if (cap) out[i] = '\0';
}

void remote_row_text(const remote_list_t *r, const remote_computers_t *c,
                     const remote_row_t *row, const char *here,
                     char *out, size_t cols)
{
    // `out` holds cols + 1 bytes.  Everything is formatted into a scratch
    // line wide enough for the widest geometry and then cut to the row.
    char line[128];
    line[0] = '\0';
    if (!row) { pad(out, cols); return; }
    switch ((remote_row_kind_t)row->kind) {
    case REMOTE_ROW_HEADER:
        if (row->index == 0) {
            char host[REMOTE_HOST_MAX + 1];
            upper_copy(host, sizeof(host), here && here[0] ? here : "this computer");
            snprintf(line, sizeof(line), "CLAUDE ON %s", host);
        } else {
            snprintf(line, sizeof(line), "OTHER COMPUTERS");
        }
        break;
    case REMOTE_ROW_NOTE:
        if (row->index == 0) {
            const uint8_t status = r ? r->status : REMOTE_STATUS_NOT_ASKED;
            snprintf(line, sizeof(line), "  %s",
                     // The usual reason is an older server: say the fix.
                     !r || !r->ready ? "Update dsterm on it to list sessions" :
                     status == REMOTE_STATUS_NOT_ASKED ? "Asking Claude..." :
                     status == REMOTE_STATUS_NO_CLAUDE ?
                         "Claude Code isn't installed there" :
                     status == REMOTE_STATUS_FAILED ? "Couldn't read the list" :
                     "No other sessions running");
        } else {
            snprintf(line, sizeof(line), "  Listening for servers...");
        }
        break;
    case REMOTE_ROW_SESSION: {
        if (!r || row->index >= r->count) break;
        const remote_session_t *s = &r->items[row->index];
        char age[8];
        format_age(s->age_min, age);
        const char mark = s->state == REMOTE_STATE_NEEDS_YOU ? '!' :
                          (s->flags & REMOTE_FLAG_HERE) ? '>' : ' ';
        // name | state | age, the name taking whatever the row has left.
        const int name_w = (int)cols - 2 - 1 - 9 - 1 - 4;
        snprintf(line, sizeof(line), "%c %-*.*s %-9s %4s", mark,
                 name_w > 4 ? name_w : 4, name_w > 4 ? name_w : 4, s->name,
                 remote_state_word(s->state), age);
        break;
    }
    case REMOTE_ROW_NEW:
        snprintf(line, sizeof(line), "+ New Claude session");
        break;
    case REMOTE_ROW_COMPUTER: {
        if (!c || row->index >= c->count) break;
        const remote_computer_t *m = &c->items[row->index];
        // Short tags, so a computer's name gets the room: at 42 columns a
        // 15-character hostname (DESKTOP-ABC1234) fits whole.
        const char *tag = m->current ? "HERE" :
                          m->moved ? "MOVED" :
                          m->saved ? (m->online ? "PAIRED" : "QUIET") :
                          m->pairing ? "PAIR NOW" : "NEW";
        const int name_w = (int)cols - 2 - 1 - 15 - 1 - 8;
        snprintf(line, sizeof(line), "%c %-*.*s %-15s %s",
                 m->current ? '>' : ' ', name_w > 6 ? name_w : 6,
                 name_w > 6 ? name_w : 6, m->name, m->host, tag);
        break;
    }
    default:
        break;
    }
    snprintf(out, cols + 1, "%s", line);
    pad(out, cols);
}

static const remote_row_t *selected_row(const remote_ui_t *ui,
                                        const remote_row_t *rows, unsigned n)
{
    if (!ui || ui->selection < 0 || (unsigned)ui->selection >= n) return NULL;
    return &rows[ui->selection];
}

remote_do_t remote_ui_activate(remote_ui_t *ui, const remote_list_t *r,
                               const remote_computers_t *c,
                               const remote_row_t *rows, unsigned n,
                               uint8_t *index)
{
    const remote_row_t *row = selected_row(ui, rows, n);
    if (!row) return REMOTE_DO_NOTHING;
    if (index) *index = row->index;
    switch ((remote_row_kind_t)row->kind) {
    case REMOTE_ROW_SESSION: {
        if (!r || row->index >= r->count) return REMOTE_DO_NOTHING;
        const remote_session_t *s = &r->items[row->index];
        if (s->flags & (REMOTE_FLAG_HERE | REMOTE_FLAG_LIVE)) {
            ui->copy_armed = 0;
            return REMOTE_DO_OPEN;
        }
        if (s->flags & REMOTE_FLAG_COPY) {
            // A copy is a new conversation branching off somebody else's;
            // it gets a second, informed press.
            if (!ui->copy_armed) {
                ui->copy_armed = 1;
                return REMOTE_DO_ARM_COPY;
            }
            ui->copy_armed = 0;
            return REMOTE_DO_COPY;
        }
        return REMOTE_DO_REFUSE;
    }
    case REMOTE_ROW_NEW:
        if (index) *index = REMOTE_PICK_NEW_INDEX;
        return REMOTE_DO_NEW;
    case REMOTE_ROW_COMPUTER:
        if (!c || row->index >= c->count) return REMOTE_DO_NOTHING;
        return c->items[row->index].current ? REMOTE_DO_ALREADY
                                            : REMOTE_DO_SWITCH;
    default:
        return REMOTE_DO_NOTHING;
    }
}

void remote_explain(const remote_ui_t *ui, const remote_list_t *r,
                    const remote_computers_t *c, const remote_row_t *rows,
                    unsigned n, const char *here,
                    char *title, size_t title_cap,
                    char *body, size_t body_cap,
                    char *footer, size_t footer_cap)
{
    const char *host = here && here[0] ? here : "this computer";
    const remote_row_t *row = selected_row(ui, rows, n);
    snprintf(title, title_cap, "REMOTE");
    snprintf(footer, footer_cap, "UP/DOWN CHOOSE   X BACK");
    if (!row) {
        snprintf(body, body_cap,
                 "Other Claude sessions on %s,\n"
                 "and other computers running dsterm.\n\n"
                 "Nothing to choose yet.", host);
        return;
    }
    char age[8];
    switch ((remote_row_kind_t)row->kind) {
    case REMOTE_ROW_SESSION: {
        const remote_session_t *s = &r->items[row->index];
        format_age(s->age_min, age);
        snprintf(title, title_cap, "%s -- %s", s->name,
                 remote_state_word(s->state));
        const char *what = s->kind == REMOTE_KIND_BACKGROUND
                               ? "A background Claude session"
                               : "A Claude session open in another app";
        if (s->flags & REMOTE_FLAG_HERE) {
            snprintf(body, body_cap,
                     "%s\nin %s\nstarted %s ago.\n\n"
                     "It is already open on this card.\n"
                     "A switches the screen to it.",
                     what, s->detail[0] ? s->detail : "?", age);
            snprintf(footer, footer_cap, "A SWITCH TO IT   X BACK");
        } else if (s->flags & REMOTE_FLAG_LIVE) {
            snprintf(body, body_cap,
                     "%s\nin %s\nstarted %s ago.\n\n"
                     "A brings it here, live: you see it and\n"
                     "type into it as if you had attached\n"
                     "in a terminal. It keeps running when\n"
                     "you switch away.",
                     what, s->detail[0] ? s->detail : "?", age);
            snprintf(footer, footer_cap, "A OPEN LIVE   X BACK");
        } else if (s->flags & REMOTE_FLAG_COPY) {
            snprintf(body, body_cap,
                     "%s\nin %s\nstarted %s ago.\n\n"
                     "Two screens typing into one session\n"
                     "would mix their messages, so this card\n"
                     "opens a COPY: the same conversation,\n"
                     "continuing separately here.\n\n"
                     "To bring the original over instead,\n"
                     "press Left on its empty prompt (or\n"
                     "type /bg) where it runs; it then shows\n"
                     "here as one you can open live.",
                     what, s->detail[0] ? s->detail : "?", age);
            snprintf(footer, footer_cap, ui->copy_armed
                                             ? "A AGAIN: OPEN A COPY   B CANCEL"
                                             : "A OPEN A COPY   X BACK");
        } else {
            snprintf(body, body_cap,
                     "%s\nin %s\nstarted %s ago.\n\n"
                     "It can't be opened from here.",
                     what, s->detail[0] ? s->detail : "?", age);
            snprintf(footer, footer_cap, "X BACK");
        }
        return;
    }
    case REMOTE_ROW_NEW:
        snprintf(title, title_cap, "New Claude session");
        snprintf(body, body_cap,
                 "Starts a fresh Claude Code session on\n"
                 "%s in a new window, and shows it.\n\n"
                 "The session you were in keeps running.\n"
                 "SESSIONS on the Control screen switches\n"
                 "between them.", host);
        snprintf(footer, footer_cap, "A START IT   X BACK");
        return;
    case REMOTE_ROW_COMPUTER: {
        const remote_computer_t *m = &c->items[row->index];
        snprintf(title, title_cap, "%s", m->name);
        if (m->current) {
            snprintf(body, body_cap, "This card is connected to\n%s (%s).",
                     m->name, m->host);
            snprintf(footer, footer_cap, "X BACK");
        } else if (m->moved) {
            snprintf(body, body_cap,
                     "%s\nanswers at a new address: %s\n"
                     "(it was %s).\n\n"
                     "A switches there with the key this card\n"
                     "already has, and remembers the new\n"
                     "address once it has answered. Everything\n"
                     "on %s keeps running.",
                     m->name, m->host, m->old_host, host);
            snprintf(footer, footer_cap, "A SWITCH   X BACK");
        } else if (m->saved) {
            snprintf(body, body_cap,
                     "Move this card to %s\n(%s).\n\n"
                     "It is paired already, so this takes a\n"
                     "few seconds. Everything on %s\n"
                     "keeps running; come back here to\n"
                     "return.%s",
                     m->name, m->host, host,
                     m->online ? "" : "\n\nIt did not answer just now; A tries\n"
                                      "anyway.");
            snprintf(footer, footer_cap, "A SWITCH   X BACK");
        } else {
            snprintf(body, body_cap,
                     "%s (%s)\nhas not been paired with this card yet.\n\n"
                     "On that computer run:  dsterm pair\n"
                     "then press A here, and compare the\n"
                     "code on both screens.",
                     m->name, m->host);
            snprintf(footer, footer_cap, "A PAIR AND SWITCH   X BACK");
        }
        return;
    }
    default:
        snprintf(body, body_cap, " ");
        return;
    }
}
