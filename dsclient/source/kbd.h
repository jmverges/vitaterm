// On-screen keyboard for the sub screen: 10 columns x 5 rows, three layers
// (base / shift / sym), Ctrl one-shot, and key-repeat timing. Pure C with no
// libnds dependency so the geometry, layer cycling, Ctrl folding, and repeat
// clock are all host-tested -- a misaligned key or a wrongly folded byte is a
// wrong character in a terminal, which is worse than a crash.
//
// Layout from docs/design/INPUT_AND_KEYBOARD.md: key n at x = 4 + col*25, y = 20 + row*27,
// size 24x26. The space key on row 4 spans two grid cells (cols 4-5).

#ifndef DSTERM_KBD_H
#define DSTERM_KBD_H

#include <stdint.h>

#define KBD_COLS 10
#define KBD_ROWS 5

#define KBD_ORIGIN_X 4      // first key's left edge
#define KBD_ORIGIN_Y 20     // first key's top edge
#define KBD_W 24            // key width
#define KBD_H 26            // key height
#define KBD_STEP_X 25
#define KBD_STEP_Y 27

// The first pixel row below the bottom key, and the first whole 8px text row
// clear of it.  Everything from there to the bottom of the screen is the
// extension's floor while the keyboard is up: no guide text is drawn in that
// strip, so an extension always has room to draw that is never under the
// user's fingers.
#define KBD_BOTTOM_Y (KBD_ORIGIN_Y + (KBD_ROWS - 1) * KBD_STEP_Y + KBD_H)
#define KBD_FLOOR_ROW ((KBD_BOTTOM_Y + 7) / 8)
#define KBD_FLOOR_Y (KBD_FLOOR_ROW * 8)

// Lines moved per scroll request. Scrolling is a modifier chord now (Shift
// armed, then Up or Down) rather than a pair of touch tiles: the tiles
// duplicated the d-pad on the Control screen, and a chord costs no screen.
#define KBD_VIEW_STEP 12

typedef enum {
    KBD_NONE = 0,  // invalid key id
    KBD_CHAR,      // sends one ASCII byte; the label varies per layer
    KBD_SPACE,     // sends ' '
    KBD_ENTER,     // sends '\r'
    KBD_BKSP,      // sends 0x7f
    KBD_ESC,       // sends 0x1b
    KBD_TAB,       // sends 0x09
    KBD_S_TAB,     // sends "\x1b[Z" (shift-tab)
    KBD_SHFT,      // cycles base -> shift -> sym -> base
    KBD_CTRL,      // arms Ctrl for the next character key
} kbd_action_t;

typedef enum {
    KBD_LAYER_BASE = 0,
    KBD_LAYER_SHIFT = 1,
    KBD_LAYER_SYM = 2,
} kbd_layer_t;

// Number of keys (49; the space key spans two grid cells).
int kbd_count(void);

// Reset layer, Ctrl state, and held key to their defaults. For tests and boot.
void kbd_reset(void);

// Key id under a touch pixel, or -1 outside every key. A pixel on a 1px
// boundary between keys belongs to the key above/left of it.
int kbd_hit(int x, int y);

// Key id at a grid cell (row, col). Rows 0-3 are row*10+col; row 4 is the
// special-key row, where the space key owns columns 4 and 5. -1 outside the
// grid. This is the same mapping kbd_hit() inverts, so d-pad focus navigation
// and touch agree on where a key is.
int kbd_id_at_grid(int row, int col);

// Key rect in pixels. `layer` selects the layer's geometry -- all three layers
// currently share one table, and the host test enforces that they stay
// identical. Space is double-width; the other 48 keys are 24x26.
void kbd_key_rect(int id, kbd_layer_t layer, int *x, int *y, int *w, int *h);

// What the key does when pressed.
kbd_action_t kbd_action(int id);

// Display label for a key in a layer. Char keys label with their character in
// that layer; special keys with their name ("SHFT", "CTRL", "ESC", "TAB",
// "S-TAB", "SPC", "ENT", "BKSP"). The returned pointer is valid until the next
// call.
const char *kbd_label(int id, kbd_layer_t layer);

// Layer state. kbd_layer_cycle() walks base -> shift -> sym -> base (the SHFT
// key's action).
kbd_layer_t kbd_layer(void);
void kbd_layer_cycle(void);
void kbd_layer_set(kbd_layer_t layer);

// Ctrl one-shot, as the doc's "sticky and one-shot": kbd_ctrl_arm() toggles it
// (a second press before a character key disarms), and the next character key
// press folds its byte with kbd_ctrl_fold() -- byte & 0x1f -- and disarms.
void kbd_ctrl_arm(void);
int kbd_ctrl_armed(void);
int kbd_ctrl_fold(int byte);

// Shift one-shot, the physical R shoulder's analogue of Ctrl: kbd_shift_arm()
// toggles, and the next byte-producing key press consumes it -- a character
// key sends its shift-layer byte, and Tab/Enter/etc. still send their key but
// disarm. The touch SHFT key cycles the sticky layer instead; this is a
// different, one-shot mechanism (docs/design/INPUT_AND_KEYBOARD.md's "sticky and one-shot").
void kbd_shift_arm(void);
int kbd_shift_armed(void);
void kbd_shift_consume(void);

// Press a key. Local actions (SHFT cycles the layer, CTRL arms) perform and
// return -1 -- the caller sends nothing. Everything else writes its key
// sequence into out[] and returns the byte count (1, or 3 for shift-tab). A
// character key press folds through Ctrl if it is armed. Returns 0 if out is
// too small.
int kbd_press(int id, uint8_t *out, int max);

// True for keys that repeat while held: character keys, space, backspace.
int kbd_repeatable(int id);

// Key the sub screen should draw inverted (the touched key), or -1 for none.
// Rendering-only state; the module never sets it itself.
void kbd_set_held(int id);
int kbd_held(void);

// The Control screen's touch targets.
//
// Their geometry lives here, with the keyboard's, for the same reason the
// keyboard's does: subscreen.c draws from this table and main.c hit-tests
// against it, so a tile cannot move on screen without its touch target moving
// with it. Two hand-maintained lists of coordinates is how a MUTE button comes
// to send an escape.
//
// Only actions with no physical button. There used to be seven tiles, four of
// them labelled with buttons -- "A ENTER", "B ESC", "X COMPOSE", "Y KEYS" --
// and two of those labels were false: X sends ESC and Y sends Tab, neither
// opens the composer nor switches mode. The other two duplicated buttons
// already under the same thumb. A screen that names a button has to be right
// about what that button does, so the tiles that could not be are gone.
//
// The six remaining screens/actions form a row-major 3x2 grid. The order is
// intentional: focus derives its traversal directly from the rect table, so
// KEYS -> QUICK -> LIVE is both the visual and d-pad order. The targets are
// still thumb-sized, not stylus-sized.
typedef enum {
    KBD_CONTROL_NONE = 0,
    // The keyboard, one press from the Control screen. It types live: each
    // touch is one KEY message to the PTY, and the top screen stays the
    // terminal. This is the default typing surface.
    KBD_CONTROL_KEYS,
    // QUICK (quick.h): Claude's mode, rewind, the diff, slash commands and
    // recent prompts, in one list.  It took the session picker's place, which
    // REMOTE does better: switching to a window already open here is one of
    // the things REMOTE lists.
    KBD_CONTROL_QUICK,
    KBD_CONTROL_PHOTO,      // the camera in one tap; AGAIN on a card without one
    // The design call was that typing should be live and the compose buffer
    // should leave the default path. It therefore lives on the secondary row,
    // behind an explicit press for prompts worth editing before they reach the
    // PTY.
    //
    // Snippets used to be a seventh destination beside it. They are a pane of
    // this one now: a snippet has only ever been a body loaded into the
    // composer for a second deliberate press, so a separate tile and a
    // separate overlay were two doors into the same room.
    KBD_CONTROL_COMPOSE,
    // Everywhere else this card could be working: the other Claude sessions
    // on the server's computer, a new one, and the other computers running
    // dsterm (remote.h), the windows already open here among them.
    KBD_CONTROL_REMOTE,
    // The slot the snippets tile gave up. Settings came off the SELECT cycle
    // for the reason status.h gives -- it is somewhere you go on purpose, and
    // four presses of a cycling button is not a way to go anywhere on purpose.
    KBD_CONTROL_SETTINGS,

    // A deliberately small, touch-only utility above the 3x2 screen grid.
    // It is not part of focus traversal: keeping the six primary tiles as a
    // rectangle preserves the simple d-pad map while still giving a stuck
    // link an obvious, deliberate recovery affordance.
    KBD_CONTROL_RECONNECT,
    // The extension's tile, if there is an extension: ext_control_label()
    // names it, and when that is NULL it has no rect at all -- not drawn,
    // not hit.  A small touch-only utility beside RELINK, like it outside
    // focus traversal, so the six destinations are the same six with or
    // without an extension attached.
    KBD_CONTROL_EXTENSION,
} kbd_control_t;

// Number of real controls; ids run 1..KBD_CONTROL_COUNT.
#define KBD_CONTROL_COUNT 6

// Rect for a control, including the small touch-only RECONNECT utility, or all
// zeroes for KBD_CONTROL_NONE and anything out of range -- a zero-size rect
// can never be hit, so a bad id fails closed.
void kbd_control_rect(kbd_control_t control, int *x, int *y, int *w, int *h);

// Control under a touch pixel, or KBD_CONTROL_NONE in the gaps between them.
kbd_control_t kbd_control_hit(int x, int y);

// Key-repeat clock as a pure function of elapsed frames: no repeats for the
// first `delay` frames, then one every `interval`. The defaults are 400 ms
// then every 60 ms at 60 fps, per docs/design/INPUT_AND_KEYBOARD.md.
//
// Fixed, and deliberately so. This was briefly adjustable from a settings row,
// on the theory that repeat rate is a matter of hands -- but the only surface
// that repeats keys here is a touchscreen, where nobody holds a key down, and
// the row was furniture. The setter went with it rather than staying as an
// entry point nothing calls, because an API with no caller cannot be wrong in
// a way any test would catch.
#define KBD_REPEAT_DELAY 24
#define KBD_REPEAT_INTERVAL 4
int kbd_repeat_should_fire(unsigned held_frames);

// -- the key word ---------------------------------------------------------
//
// The ARM9's direct keypad register is ten bits: A, B, SELECT, START, d-pad,
// L, R. X and Y live in REG_KEYXY, which only the ARM7 can read, and arrive
// through libnds at bits 10 and 11 -- the same positions this header uses, so
// the two words share no bits and combine by OR. Assembly is a pure function
// so the host tests can pin it: the shipped bug was that poll_input built its
// word from the register alone, focus_input's X and Y checks were always
// false, and Y-space was dead on hardware while the face-key table quietly
// patched the missing bits for itself.
#define KBD_REG_MASK 0x3FFu        // the ten direct-register bits
#define KBD_KEY_X    0x400u        // mirrors libnds KEY_X (REG_KEYXY)
#define KBD_KEY_Y    0x800u        // mirrors libnds KEY_Y (REG_KEYXY)

// The held key word: the ten register bits plus libnds's X and Y. The libnds
// word is masked here so a stray bit can never reach a consumer as a phantom
// key; the register word is masked so a high bit is never mistaken for X or Y.
unsigned kbd_held_keyword(unsigned reg_word, unsigned lib_word);

// The newly-pressed word: bits that are held now but were not held before.
unsigned kbd_edge_keyword(unsigned held, unsigned prev_held);

#endif  // DSTERM_KBD_H
