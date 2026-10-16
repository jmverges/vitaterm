// The extension seam.
//
// The terminal is complete without anything behind this header.  An
// extension -- something that wants to live on the console beside the
// terminal, watch what it does, and draw itself -- attaches by providing
// these functions.  ext_none.c provides them empty, and that is the build
// with nothing attached.
//
// The core calls every function here unconditionally and never asks whether
// an extension is present; "no extension" is an extension that does nothing.
// That is what keeps the call sites in main.c honest: there is exactly one
// of each, it is always taken, and there is no #ifdef to fall out of sync.
//
// What the seam deliberately does not carry: the socket, the pairing key,
// the config, or a writable grid.  An extension can observe the session and
// draw; it cannot type, and it cannot reach the server.  Anything it needs
// from main.c it asks for through host.h, which keeps the same rule.
#ifndef DSTERM_EXT_H
#define DSTERM_EXT_H

#include <stddef.h>
#include <stdint.h>

#include "proto.h"

// Things the terminal observes and passes on.  Each is a fact about what the
// person or the session just did, never an instruction.
typedef enum {
    EXT_EVENT_LOCAL_SEND,       // a lone CR left the console: a submission
    EXT_EVENT_LOCAL_INPUT,      // a deliberate press or touch, anywhere
    EXT_EVENT_FOCUS,            // the d-pad focus highlight moved
    EXT_EVENT_SPACE,            // the spacebar was typed live
    EXT_EVENT_ESCAPE,           // ESC was typed live
    EXT_EVENT_KEY,              // any other key was typed, live or composed
    EXT_EVENT_LANTERN,          // another session reported new output
    EXT_EVENT_SESSION_SWITCH,   // the viewed session changed
} ext_event_t;

// The moments the sound cues mark, as the chime observes them.  An
// extension may ride each with a sound of its own; the terminal's cue has
// already played.
typedef enum {
    EXT_CUE_SENT,       // a line was submitted
    EXT_CUE_THINK,      // sent, then quiet
    EXT_CUE_OUTPUT,     // sustained activity followed the quiet
    EXT_CUE_DONE,       // observed output settled
    EXT_CUE_LINK,       // the link came up
    EXT_CUE_DROP,       // the link went away
} ext_cue_t;

// Extra HELLO capability tokens, each with a leading space, or "".  At most
// PROTO_HELLO_EXT_MAX bytes; the handshake refuses a longer string rather
// than overflow, and announces only the core tokens.
const char *ext_hello_tokens(void);

// Boot, in order: after both screens are up, after the card is mounted, and
// after the UI settings have been applied.
void ext_init(void);
void ext_boot(void);
void ext_boot_late(void);

// Just after every VBlank yield -- the one moment OAM and the sound
// hardware may be written -- and at the end of every main-loop pass, after
// every message has been handled.
void ext_vblank(void);
void ext_frame(void);

// Facts.
void ext_link(int up);
void ext_battery(int low);
void ext_terminal_changed(void);        // the grid changed for a local reason
void ext_terminal_traffic(void);        // ...because the server sent cells
void ext_cursor(const cursor_t *cur);
void ext_event(ext_event_t event);

// Sound.  cue() follows the terminal's own cue for the same moment.
// quiet_moment() is a moment the chime judges fit for texture while the
// model works: `streaming` says output is arriving, `silent` that nothing
// has repainted for a while (the timer-driven fallback, which should speak
// at most half as often).  key_tick() is the user's own keypress; return 1
// to have voiced it, and the terminal's PSG tick stays quiet.
void ext_cue(ext_cue_t cue);
void ext_quiet_moment(int streaming, int silent);
int  ext_key_tick(int backspace);

// A message the core does not handle.  Returns 1 if the extension took it.
// It sees only types it asked for with a HELLO token; a malformed body is
// the extension's problem to ignore and never a reason to drop the link.
int ext_message(uint8_t type, const uint8_t *payload, uint16_t len);

// The extension's own picker, if it has one, which owns every control while
// it is open.  reset() closes it without a repaint, for a reconnect.
int  ext_menu_is_open(void);
void ext_menu_open(void);
void ext_menu_reset(void);
void ext_menu_input(unsigned raw, unsigned edge);
void ext_overlay_sync(void);            // from overlay_sync(): re-place yourself

// Touch, from poll_touch(): pen down, pen held, pen up.  track() returns 1
// while the extension's picker owns the contact; hit() returns 1 when a
// first-frame tap on the Control screen landed on something of the
// extension's, in which case the control under it is not pressed.
void ext_touch_down(void);
int  ext_touch_track(void);
int  ext_touch_hit(int px, int py);
void ext_touch_control_opened(void);    // the extension's Control tile was hit
void ext_touch_up(void);

// Places on the lower screen an extension may paint, through the row
// painters subscreen.h lends out.  CONTROL_STRIP is rows 2--3 of the
// Control screen (the terminal has drawn its own first) and the empty
// strip at the bottom; KEYS_STATUS is row 0 of the keyboard screen, in
// place of the counters, and PROJECT_STATUS row 1 of the project screen in
// place of the view position; OVERLAY is the whole panel while the extension's
// picker is open.  Return 1 to say something was drawn.
typedef enum {
    EXT_DRAW_CONTROL_STRIP,
    EXT_DRAW_KEYS_STATUS,
    EXT_DRAW_PROJECT_STATUS,
    EXT_DRAW_OVERLAY,
} ext_draw_t;
int ext_subscreen_draw(ext_draw_t what);

// The Control screen's sixth tile.  NULL means there is none: it is not
// drawn, not hit, and focus walks past it.
const char *ext_control_label(void);

// One row for the pages that summarise the device: a label and a value, or
// NULL for none.  about() is what is saved; now() is what is running.
const char *ext_about_label(void);
const char *ext_about_value(void);
const char *ext_now_label(void);
const char *ext_now_value(void);

#endif  // DSTERM_EXT_H
