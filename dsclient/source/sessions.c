#include "sessions.h"

#include <string.h>

static void discard_staging(sessions_t *sessions)
{
    sessions->staged_count = 0;
    sessions->staged_received = 0;
    memset(sessions->staged, 0, sizeof(sessions->staged));
}

static int entry_matches(const session_t *a, const session_t *b)
{
    return a->index == b->index && a->name_len == b->name_len &&
           memcmp(a->name, b->name, a->name_len) == 0;
}

static int entry_had_new_output(const sessions_t *sessions, const session_t *entry)
{
    for (unsigned i = 0; i < sessions->count; i++) {
        const session_t *old = &sessions->items[i];
        if (entry_matches(old, entry))
            return (old->flags & SESSION_FLAG_NEW_OUTPUT) != 0;
    }
    return 0;
}

size_t session_name_sanitize(char *out, size_t out_cap,
                             const uint8_t *src, size_t len)
{
    if (!out_cap) return 0;

    size_t n = len;
    if (n > SESSION_NAME_MAX) n = SESSION_NAME_MAX;
    if (n >= out_cap) n = out_cap - 1;

    for (size_t i = 0; i < n; i++) {
        const uint8_t ch = src ? src[i] : 0;
        out[i] = ch >= 0x20 && ch <= 0x7e ? (char)ch : '?';
    }
    out[n] = '\0';
    return n;
}

void sessions_init(sessions_t *sessions)
{
    if (!sessions) return;
    memset(sessions, 0, sizeof(*sessions));
    sessions->selection = -1;
}

static int chunk_is_valid(const session_chunk_t *chunk)
{
    if (!chunk) return 0;
    if (chunk->count > SESSIONS_MAX) return 0;
    if (chunk->count == 0) return 1;
    if (chunk->entry.index >= chunk->count) return 0;
    if (chunk->entry.name_len > SESSION_NAME_MAX) return 0;
    return 1;
}

static void commit_empty(sessions_t *sessions)
{
    memset(sessions->items, 0, sizeof(sessions->items));
    sessions->count = 0;
    sessions->selection = -1;
    sessions->ready = 1;
    sessions->new_output_mask = 0;
    discard_staging(sessions);
}

static void commit_staged(sessions_t *sessions)
{
    const int old_selection = sessions->selection;
    uint8_t new_output_mask = 0;

    // A list received at connection start must render its dots but not sound a
    // notification for work that happened before the handheld was listening.
    if (sessions->ready) {
        for (unsigned i = 0; i < sessions->staged_count; i++) {
            const session_t *entry = &sessions->staged[i];
            if ((entry->flags & (SESSION_FLAG_ACTIVE | SESSION_FLAG_NEW_OUTPUT)) ==
                    SESSION_FLAG_NEW_OUTPUT &&
                !entry_had_new_output(sessions, entry)) {
                new_output_mask |= (uint8_t)(1u << i);
            }
        }
    }

    memset(sessions->items, 0, sizeof(sessions->items));
    memcpy(sessions->items, sessions->staged,
           (size_t)sessions->staged_count * sizeof(sessions->items[0]));
    sessions->count = sessions->staged_count;
    sessions->ready = 1;
    sessions->new_output_mask = new_output_mask;

    // A periodic list refresh must not drag a thumb that is walking the list
    // back to the active row.  Row positions are the only stable client-side
    // identifier in this protocol, so keep a still-valid position and let the
    // UI visibly reflect any list reorder.
    if (old_selection >= 0 && old_selection < sessions->count) {
        sessions->selection = old_selection;
    } else {
        sessions_select_active(sessions);
    }
    discard_staging(sessions);
}

int sessions_apply_chunk(sessions_t *sessions, const session_chunk_t *chunk)
{
    if (!sessions || !chunk || !chunk_is_valid(chunk)) {
        if (sessions) discard_staging(sessions);
        return -1;
    }

    // A payload containing only count=0 is a full, valid empty snapshot, not
    // an incomplete update.  It is also the useful answer when the service
    // supports sessions but currently has none, so it opens the pick gate
    // while correctly offering no row to choose.
    if (chunk->count == 0) {
        commit_empty(sessions);
        return 1;
    }

    if (sessions->staged_count == 0) {
        sessions->staged_count = chunk->count;
        sessions->staged_received = 0;
        memset(sessions->staged, 0, sizeof(sessions->staged));
    } else if (sessions->staged_count != chunk->count) {
        discard_staging(sessions);
        return -1;
    }

    const unsigned index = chunk->entry.index;
    const uint8_t bit = (uint8_t)(1u << index);
    if (sessions->staged_received & bit) {
        // A duplicate cannot tell us whether this is retransmission or a new
        // generation of the same-sized list.  TCP supplies ordered delivery,
        // so fail closed and wait for the server's next complete publication.
        discard_staging(sessions);
        return -1;
    }

    session_t *staged = &sessions->staged[index];
    staged->index = (uint8_t)index;
    staged->flags = (uint8_t)(chunk->entry.flags & SESSION_FLAG_KNOWN);
    staged->idle_s = chunk->entry.idle_s;
    staged->name_len = (uint8_t)session_name_sanitize(
        staged->name, sizeof(staged->name),
        (const uint8_t *)chunk->entry.name, chunk->entry.name_len);
    sessions->staged_received |= bit;

    const uint8_t needed = (uint8_t)((1u << sessions->staged_count) - 1u);
    if (sessions->staged_received != needed) return 0;

    commit_staged(sessions);
    return 1;
}

int sessions_ready(const sessions_t *sessions)
{
    return sessions && sessions->ready;
}

unsigned sessions_count(const sessions_t *sessions)
{
    return sessions ? sessions->count : 0;
}

const session_t *sessions_item(const sessions_t *sessions, unsigned row)
{
    if (!sessions || row >= sessions->count) return NULL;
    return &sessions->items[row];
}

int sessions_select_active(sessions_t *sessions)
{
    if (!sessions || sessions->count == 0) {
        if (sessions) sessions->selection = -1;
        return 0;
    }
    for (unsigned i = 0; i < sessions->count; i++) {
        if (sessions->items[i].flags & SESSION_FLAG_ACTIVE) {
            sessions->selection = (int)i;
            return 1;
        }
    }
    sessions->selection = 0;
    return 0;
}

int sessions_selection(const sessions_t *sessions)
{
    if (!sessions || sessions->selection < 0 ||
        sessions->selection >= sessions->count) return -1;
    return sessions->selection;
}

const session_t *sessions_selected(const sessions_t *sessions)
{
    const int selected = sessions_selection(sessions);
    return selected < 0 ? NULL : &sessions->items[selected];
}

int sessions_move_selection(sessions_t *sessions, int delta)
{
    if (!sessions || sessions->count == 0) return 0;

    const int old = sessions_selection(sessions);
    if (old < 0) {
        sessions_select_active(sessions);
        return sessions->selection >= 0;
    }
    if (!delta) return 0;

    const int count = sessions->count;
    int next = (old + (delta % count)) % count;
    if (next < 0) next += count;
    sessions->selection = next;
    return next != old;
}

uint8_t sessions_new_output_mask(const sessions_t *sessions)
{
    return sessions ? sessions->new_output_mask : 0;
}

uint8_t sessions_take_new_output_mask(sessions_t *sessions)
{
    if (!sessions) return 0;
    const uint8_t mask = sessions->new_output_mask;
    sessions->new_output_mask = 0;
    return mask;
}
