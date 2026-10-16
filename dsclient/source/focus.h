#ifndef DSTERM_FOCUS_H
#define DSTERM_FOCUS_H

#include "kbd.h"

// D-pad focus: the d-pad walks a highlight over the bottom
// screen's controls -- and, on the keyboard, over individual keys -- and A
// activates the lit item. Focus is an explicitly entered mode (L+R) and a
// visible one: the lit item is filled in Claude's orange and the footer says
// what the d-pad does while it is on. An invisible mode on a five-button
// device is how a control stops being trusted.
//
// Pure C with no libnds dependency so the movement rules are host-tested: a
// cursor that jumps the wrong way under a thumb is exactly as broken as one
// that hangs.

typedef enum {
    FOCUS_OFF = 0,
    FOCUS_CONTROL,   // the Control screen's tiles
    FOCUS_KEYS,      // the keyboard, one key lit at a time
} focus_place_t;

void focus_init(void);
void focus_on(focus_place_t place);
void focus_off(void);
int focus_active(void);
focus_place_t focus_place(void);

// Move the cursor one step; an edge simply holds. The keyboard wraps within
// a row (a row of ten keys with no wrap is nine presses of backtracking for
// one miss); the tile grid stops at its edges. Vertical movement stops at
// the first and last rows; focus_at_top() tells the caller that Up on the
// top row should leave focus instead.
void focus_move(int dx, int dy);
int focus_at_top(void);

// The item under the cursor, for activation and drawing.
int focus_control_id(void);   // FOCUS_CONTROL: a kbd_control_t id
int focus_key_id(void);       // FOCUS_KEYS: a keyboard key id

// Focus keyboard shortcuts are intentionally a small dedicated layout: A
// presses the highlighted key, B inserts space, Y deletes, and R submits.
// They apply only while the key grid is the active focus surface.  The Control
// tiles retain their own B-as-back behavior, and the normal non-Focus keyboard
// keeps its physical-button map.  Keeping the predicates here makes the
// DS-only input loop and host regression tests share one policy boundary.
int focus_b_is_space(void);
int focus_y_is_backspace(void);
int focus_r_is_enter(void);

#endif  // DSTERM_FOCUS_H
