// REMOTE: every other place this card could be working, in one list.
//
// The Control screen's REMOTE tile opens a picker with two halves:
//
//   ON <this computer>   the Claude Code sessions running on the machine the
//                        card is connected to, as the server reads them from
//                        `claude agents --json` (docs/REMOTE.md), and a row
//                        to start a new one;
//   OTHER COMPUTERS      dsterm servers this card has saved or can hear on
//                        the network, so moving to the PC is one press.
//
// This module is the pure half: the wire list (staged and committed exactly
// like sessions.c, so a half-received list is never drawn), the computers
// list boot.c fills, the rows as they are drawn, and what A on a row means.
// main.c owns the sockets and subscreen.c the pixels.

#ifndef DSTERM_REMOTE_H
#define DSTERM_REMOTE_H

#include <stddef.h>
#include <stdint.h>

#define REMOTE_MAX 8
#define REMOTE_NAME_MAX 24
#define REMOTE_DETAIL_MAX 28
#define REMOTE_NOTE_MAX 40
#define REMOTE_COMPUTERS_MAX 6
#define REMOTE_HOST_MAX 21          // a profile name: 20 characters and NUL

// Wire values (docs/REMOTE.md).  Unknown values are drawn as UNKNOWN, never
// rejected: a newer server may report a state this card has no word for.
typedef enum {
    REMOTE_KIND_BACKGROUND = 0,
    REMOTE_KIND_INTERACTIVE = 1,
} remote_kind_t;

typedef enum {
    REMOTE_STATE_UNKNOWN = 0,
    REMOTE_STATE_WORKING,
    REMOTE_STATE_NEEDS_YOU,
    REMOTE_STATE_DONE,
    REMOTE_STATE_IDLE,
    REMOTE_STATE_FAILED,
    REMOTE_STATE_STOPPED,
    REMOTE_STATE_COUNT,
} remote_state_t;

#define REMOTE_FLAG_HERE 0x01      // already open in a dsterm window
#define REMOTE_FLAG_LIVE 0x02      // can be attached, live
#define REMOTE_FLAG_COPY 0x04      // can be opened as a copy
#define REMOTE_FLAG_KNOWN (REMOTE_FLAG_HERE | REMOTE_FLAG_LIVE | REMOTE_FLAG_COPY)

// The empty set's status byte.
typedef enum {
    REMOTE_STATUS_NOT_ASKED = 0,
    REMOTE_STATUS_NONE = 1,
    REMOTE_STATUS_NO_CLAUDE = 2,
    REMOTE_STATUS_FAILED = 3,
} remote_status_t;

// Pick actions on the wire.
#define REMOTE_PICK_OPEN 0
#define REMOTE_PICK_COPY 1
#define REMOTE_PICK_NEW 2
#define REMOTE_PICK_NEW_INDEX 0xFF

typedef struct {
    uint8_t index;
    uint8_t kind;
    uint8_t state;
    uint8_t flags;
    uint16_t age_min;
    char name[REMOTE_NAME_MAX + 1];       // printable ASCII, NUL-terminated
    char detail[REMOTE_DETAIL_MAX + 1];
} remote_session_t;

// One decoded MSG_REMOTE frame.  count == 0 is a complete set; `status` is
// meaningful only then.
typedef struct {
    uint8_t count;
    uint8_t status;
    remote_session_t entry;
} remote_chunk_t;

typedef struct {
    // Committed: what is drawn and what a pick's index refers to.
    remote_session_t items[REMOTE_MAX];
    uint8_t count;
    uint8_t status;
    // At least one complete set has arrived on this connection.  That is the
    // proof the server speaks REMOTE, and nothing may be sent before it.
    uint8_t ready;
    // The server's last REMOTE_NOTE, shown on the picker until replaced.
    char note[REMOTE_NOTE_MAX + 1];

    // In flight.  Never drawn.
    remote_session_t staged[REMOTE_MAX];
    uint8_t staged_count;
    uint8_t staged_received;
} remote_list_t;

void remote_init(remote_list_t *r);
// 1 when the chunk completed and committed a set, 0 while one is still
// arriving, -1 for a chunk that does not fit the set in flight (which drops
// only the set in flight).
int remote_apply_chunk(remote_list_t *r, const remote_chunk_t *chunk);
void remote_set_note(remote_list_t *r, const char *text, size_t len);
// The check a pick carries (docs/REMOTE.md): FNV-1a over the row's name, a
// zero byte and its detail, folded to 16 bits.  The server recomputes it for
// the row at that index and refuses the pick if the list re-sorted under the
// person's thumb, rather than opening whatever session moved into the row.
uint16_t remote_row_check(const remote_session_t *row);
// How many committed sessions are waiting on the person holding the card --
// the badge on the REMOTE tile.
unsigned remote_needs_you(const remote_list_t *r);

const char *remote_state_word(uint8_t state);   // "NEEDS YOU", "WORKING" ...

// ----------------------------------------------------------- computers ---

typedef struct {
    char name[REMOTE_HOST_MAX];
    char host[16];
    uint16_t port;
    uint8_t current;     // the one this card is connected to
    uint8_t saved;       // paired: this card holds its key
    uint8_t online;      // answered discovery just now
    uint8_t pairing;     // its pairing window is open
    // Saved under another address and now answering at `host` (the router
    // moved it).  `old_host` is where the card has it saved.
    uint8_t moved;
    char old_host[16];
} remote_computer_t;

typedef struct {
    remote_computer_t items[REMOTE_COMPUTERS_MAX];
    unsigned count;
} remote_computers_t;

// ------------------------------------------------------------ the picker ---

typedef enum {
    REMOTE_ROW_HEADER = 0,   // a section title; not selectable
    REMOTE_ROW_SESSION,      // a Claude session on this computer
    REMOTE_ROW_NEW,          // "+ New Claude session"
    REMOTE_ROW_COMPUTER,     // another dsterm server
    REMOTE_ROW_NOTE,         // a line of explanation; not selectable
} remote_row_kind_t;

#define REMOTE_ROWS_MAX (2 + REMOTE_MAX + 1 + REMOTE_COMPUTERS_MAX + 2)

typedef struct {
    uint8_t kind;            // remote_row_kind_t
    uint8_t index;           // into the sessions or the computers
} remote_row_t;

typedef struct {
    int selection;           // a row index, or -1
    int copy_armed;          // A once on a copy row: the second A confirms
} remote_ui_t;

void remote_ui_init(remote_ui_t *ui);
// After a new set commits: forget an armed copy (it was armed for a row that
// may now be another session) and keep the selection on the session it was
// on, found by name and detail, rather than on the same row number.
void remote_ui_follow(remote_ui_t *ui, const remote_list_t *r,
                      const remote_computers_t *c, const char *name,
                      const char *detail);

// The rows, in drawing order.  `here` names this computer for its header.
unsigned remote_rows(const remote_list_t *r, const remote_computers_t *c,
                     remote_row_t *rows, unsigned max);
int remote_row_selectable(const remote_row_t *row);
// Put the selection on the first selectable row if it is not on one; move
// it by `delta` selectable rows, wrapping.  Returns 1 when it moved.
void remote_ui_settle(remote_ui_t *ui, const remote_row_t *rows, unsigned n);
int remote_ui_move(remote_ui_t *ui, const remote_row_t *rows, unsigned n,
                   int delta);

// One row as text, padded to `cols`.  Never longer than `cols`.
void remote_row_text(const remote_list_t *r, const remote_computers_t *c,
                     const remote_row_t *row, const char *here,
                     char *out, size_t cols);

// What pressing A on the selected row does.
typedef enum {
    REMOTE_DO_NOTHING = 0,
    REMOTE_DO_OPEN,          // send PICK index OPEN
    REMOTE_DO_ARM_COPY,      // first press on a copy row: explain, wait
    REMOTE_DO_COPY,          // send PICK index COPY
    REMOTE_DO_NEW,           // send PICK 0xFF NEW
    REMOTE_DO_SWITCH,        // move the card to computer `index`
    REMOTE_DO_ALREADY,       // it is the computer you are on
    REMOTE_DO_REFUSE,        // a row that cannot be opened (failed ...)
} remote_do_t;

remote_do_t remote_ui_activate(remote_ui_t *ui, const remote_list_t *r,
                               const remote_computers_t *c,
                               const remote_row_t *rows, unsigned n,
                               uint8_t *index);

// The top screen's explanation of the selected row: a title and a body of
// short lines, and the footer naming what A will do.  Written so somebody
// who has never seen the card knows what the press will do before making it.
void remote_explain(const remote_ui_t *ui, const remote_list_t *r,
                    const remote_computers_t *c, const remote_row_t *rows,
                    unsigned n, const char *here,
                    char *title, size_t title_cap,
                    char *body, size_t body_cap,
                    char *footer, size_t footer_cap);

#endif  // DSTERM_REMOTE_H
