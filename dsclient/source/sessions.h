// A bounded, display-safe session list assembled from MSG_SESSIONS chunks.
//
// This module deliberately has no libnds dependency.  The wire decoder lives
// in proto.c; this layer owns the stateful rules: chunks are staged per list
// and only replace the visible list once the complete set is valid.  Keeping
// that boundary here gives the UI one trustworthy, small list to draw and a
// simple readiness gate for SESSION_PICK.

#ifndef DSTERM_SESSIONS_H
#define DSTERM_SESSIONS_H

#include <stddef.h>
#include <stdint.h>

// Eight rows fit the bottom-screen picker without paging.  Names are clipped
// to the printable portion that has room in its row; the terminating NUL is
// always present in a committed entry.
#define SESSIONS_MAX      8
#define SESSION_NAME_MAX 24

// MSG_SESSIONS entry flags.  Unknown wire bits are deliberately ignored: a
// newer server may add presentation information without making this card act
// on it or disconnecting it.
#define SESSION_FLAG_RUNNING    0x01
#define SESSION_FLAG_ACTIVE     0x02
#define SESSION_FLAG_NEW_OUTPUT 0x04
#define SESSION_FLAG_KNOWN \
    (SESSION_FLAG_RUNNING | SESSION_FLAG_ACTIVE | SESSION_FLAG_NEW_OUTPUT)

// A formatting-safe entry.  `name` is printable ASCII (unsafe bytes become
// '?'), is no more than SESSION_NAME_MAX bytes, and is NUL-terminated.  Use
// name_len rather than trusting strlen() when calculating a drawing bound.
typedef struct {
    uint8_t index;                 // server-owned row index for SESSION_PICK
    uint8_t flags;                 // SESSION_FLAG_* only
    uint16_t idle_s;               // elapsed seconds since output, wire value
    uint8_t name_len;
    char name[SESSION_NAME_MAX + 1];
} session_t;

// One decoded MSG_SESSIONS payload.  A zero count represents a complete empty
// list, and its `entry` fields are zeroed by the protocol decoder.
typedef struct {
    uint8_t count;
    session_t entry;
} session_chunk_t;

typedef struct {
    // Committed state.  Consumers must use the accessors below rather than
    // staging fields, so an incomplete or bad update is never rendered.
    session_t items[SESSIONS_MAX];
    uint8_t count;
    int selection;                 // row, or -1 when there is nothing to pick
    uint8_t ready;                 // at least one complete MSG_SESSIONS list

    // A bit per current row, latched on the most recent completed update when
    // a non-active session newly gained SESSION_FLAG_NEW_OUTPUT.  The UI can
    // consume it once to make a quiet lantern cue without sounding repeatedly
    // while the dot remains set.
    uint8_t new_output_mask;

    // Private in practice (C cannot make this opaque without heap storage):
    // an in-flight snapshot.  Do not draw or pick from it.
    session_t staged[SESSIONS_MAX];
    uint8_t staged_count;          // 0 = no non-empty snapshot in flight
    uint8_t staged_received;       // one bit per staged row
} sessions_t;

void sessions_init(sessions_t *sessions);

// Apply one decoded chunk.  Returns 1 only when it completes and atomically
// commits a list, 0 while a non-empty list is still accumulating, and -1 for
// invalid input.  An invalid chunk drops only the in-flight snapshot; the last
// complete list and readiness gate remain intact.  The zero-count special case
// commits an empty list immediately.
int sessions_apply_chunk(sessions_t *sessions, const session_chunk_t *chunk);

// Whether this connection has received a complete, valid MSG_SESSIONS list.
// This is the capability gate: do not emit MSG_SESSION_PICK until it is true.
int sessions_ready(const sessions_t *sessions);
unsigned sessions_count(const sessions_t *sessions);
const session_t *sessions_item(const sessions_t *sessions, unsigned row);

// Selection helpers for a picker.  Selecting active falls back to row zero if
// there is no active flag and returns 1 only when an active row was found.
// Movement wraps, matching the snippet picker; it returns nonzero if the
// selected row changed (including changing from no selection to a valid row).
int sessions_select_active(sessions_t *sessions);
int sessions_selection(const sessions_t *sessions);
const session_t *sessions_selected(const sessions_t *sessions);
int sessions_move_selection(sessions_t *sessions, int delta);

// Read or consume the latest completed-list transition mask.  Bits are rows in
// the current committed list, not names or paths.  `take` clears the latch.
uint8_t sessions_new_output_mask(const sessions_t *sessions);
uint8_t sessions_take_new_output_mask(sessions_t *sessions);

// Copy a raw wire name into a bounded printable C string.  The result is
// clamped to both SESSION_NAME_MAX and `out_cap - 1`; bytes outside printable
// ASCII become '?'.  `out_cap == 0` performs no write and returns zero.
size_t session_name_sanitize(char *out, size_t out_cap,
                             const uint8_t *src, size_t len);

#endif  // DSTERM_SESSIONS_H
