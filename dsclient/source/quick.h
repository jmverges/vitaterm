// QUICK: the Control screen's tile for the things you reach for mid-session,
// in one short list instead of five tiles.
//
//   MODE      Claude Code's mode -- the tile itself shows which one, read off
//             Claude's own footer -- and A moves to the next (Shift-Tab).
//   UNDO      Claude Code's rewind (/rewind): pick the point to go back to
//             with the d-pad, A to restore, X to leave it.
//   CHANGES   the PROJECT page's DIFF tab: what is different in the repo.
//   COMMANDS  slash commands, built in and the computer's own (the project's
//             and your .claude/commands and skills), into the composer.
//   AGAIN     the prompts you sent lately, into the composer to send again.
//   PHOTO     the camera screen -- a DSi-only row.  It is the last row, and
//             a card without a camera does not have it (quick_row_count).
//
// Everything that lands in the composer still needs A to send: COMMANDS can
// carry names the computer chose, and the composer is where they are read
// first (snippets.h has the rule).  MODE and UNDO type fixed keys the person
// just asked for, like the settings rows do.
//
// Pure C with no libnds dependency; host-tested.

#ifndef DSTERM_QUICK_H
#define DSTERM_QUICK_H

#include <stddef.h>
#include <stdint.h>

typedef enum {
    QUICK_ROW_MODE = 0,
    QUICK_ROW_UNDO,
    QUICK_ROW_CHANGES,
    QUICK_ROW_COMMANDS,
    QUICK_ROW_AGAIN,
    QUICK_ROW_PHOTO,
    QUICK_ROW_COUNT,
} quick_row_t;

const char *quick_row_label(quick_row_t row);
// What the row does, in a few words, for the right of its line.
const char *quick_row_note(quick_row_t row);
// PHOTO's note is a fact of the connection, not the card: main sets it when
// CAMERA_READY lands (NULL restores "see it, send it").  Borrowed pointer.
void quick_set_photo_note(const char *note);

// How many rows the menu has on this card: PHOTO is last, so it is simply
// one fewer on a card without a camera.
unsigned quick_row_count(int camera);

// ------------------------------------------------------------------ MODE ---

#define QUICK_MODE_MAX 10

// Claude Code's mode from one line of its screen, as one short word: "auto",
// "manual", "plan", "edits" (accept edits), "bypass", or "normal" where only
// the "? for shortcuts" hint stands in its place.  Returns 1 and fills
// `out` when the line is the footer that names it, 0 otherwise.  The words
// come from Claude Code's own text ("plan mode on", "accept edits on"), so a
// mode it adds later still shows, by its own name.
int quick_mode_from_line(const char *line, char out[QUICK_MODE_MAX + 1]);

// ------------------------------------------------------------- COMMANDS ---

#define QUICK_COMMANDS_MAX 24
#define QUICK_COMMAND_MAX 31          // "/" and up to 30 characters

typedef struct {
    unsigned count;
    char names[QUICK_COMMANDS_MAX][QUICK_COMMAND_MAX + 1];
} quick_commands_t;

// The built-in ones, which are always there, then the computer's.
void quick_commands_init(quick_commands_t *c);
// Replace the computer's part with a COMMANDS frame's payload: names
// separated by newlines, each a slash and then letters, digits and _ - : .
// A name that is not one is skipped; a duplicate of a built-in is dropped.
// Returns how many of the computer's were taken.
unsigned quick_commands_apply(quick_commands_t *c, const uint8_t *payload,
                              size_t len);
// How many of the list are built in (they come first).
unsigned quick_commands_builtin(void);

// ---------------------------------------------------------------- AGAIN ---

#define QUICK_RECENT_MAX 10
#define QUICK_RECENT_TEXT_MAX 400     // a longer prompt is not kept

typedef struct {
    unsigned count;                   // newest first
    char items[QUICK_RECENT_MAX][QUICK_RECENT_TEXT_MAX + 1];
} quick_recent_t;

void quick_recent_init(quick_recent_t *r);
// Remember a prompt that was just sent.  Trailing whitespace is trimmed, an
// empty or overlong one is not kept, and one already in the list moves to the
// front rather than appearing twice.  Returns 1 if the list changed.
int quick_recent_add(quick_recent_t *r, const char *text);
// Forget one.  Returns 1 if there was one to forget.
int quick_recent_remove(quick_recent_t *r, unsigned index);

// The list as a file: one prompt per line, newest first, with a backslash
// escaping newlines (\n) and itself (\\).  Returns the length written, or 0
// if it does not fit `cap`.
size_t quick_recent_format(const quick_recent_t *r, char *out, size_t cap);
// Read it back.  Lines that do not decode are skipped.
void quick_recent_parse(quick_recent_t *r, const char *text);

// One line of the list, for a row: newlines shown as " / ", cut to `cols`.
void quick_recent_row(const char *text, char *out, size_t cols);

// Lines typed on the KEYS screen, so a prompt typed there can be sent again
// too.  Fed every byte that is sent; a carriage return completes the line.
// Anything that moves the cursor or edits in ways this cannot follow (arrows,
// Tab, Esc) gives the line up until the next return: AGAIN keeps what was
// typed, never a guess at it.
typedef struct {
    char buf[QUICK_RECENT_TEXT_MAX + 1];
    size_t len;
    int lost;
} quick_line_t;

void quick_line_reset(quick_line_t *l);
// Returns 1, with the line in `out`, when `bytes` completed one worth keeping.
int quick_line_feed(quick_line_t *l, const char *bytes, size_t n,
                    char *out, size_t cap);

#endif  // DSTERM_QUICK_H
