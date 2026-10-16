// Bottom-screen mode and the status/counter/link lines.
//
// SELECT cycles the modes. Pure C with no libnds dependency so the mode
// machine and every line's formatting are host-tested: a line that overruns its
// 42 columns corrupts the screen it was added to explain.

#ifndef DSTERM_STATUS_H
#define DSTERM_STATUS_H

#include "log.h"

typedef enum {
    // Default. Reading and approving: the scrollback readout, the link line,
    // and large touch controls. First because it is the screen the handheld is
    // most often picked up to use, and because it is the only one where a
    // mis-touch cannot type into the terminal.
    SCREEN_MODE_CONTROL = 0,
    SCREEN_MODE_KEYS,    // two status lines on top, the keyboard below
    SCREEN_MODE_STATUS,  // full-screen log with the counters pinned last
    // What is going on at the other end: branch, diff, health, and the one-tap
    // actions.  The only mode that takes the top screen as well, because it is
    // the only one whose content needs room to read; see project.h.
    SCREEN_MODE_PROJECT,
    // How the device behaves, as opposed to who is on it.  Everything about
    // an extension stays behind its own tile; this is the session's model and
    // the switch that turns a safety rule off.
    //
    // Deliberately the last value, and deliberately not on the SELECT cycle:
    // see SCREEN_MODE_CYCLE_COUNT.
    SCREEN_MODE_SETTINGS,
    SCREEN_MODE_COUNT,
} screen_mode_t;

// How many modes SELECT walks.  Settings is a place you go on purpose, once,
// to change something -- not a station you pass through four times an hour on
// the way back to the keyboard, which is what putting it on the cycle made it.
// It has its own tile on the Control screen instead.
//
// Written as "every mode before SETTINGS" rather than as a number, so a sixth
// mode added above it joins the cycle by existing.  A mode that wants to be
// off the cycle goes after this one.
#define SCREEN_MODE_CYCLE_COUNT SCREEN_MODE_SETTINGS

screen_mode_t status_mode_get(void);

// CONTROL -> KEYS -> STATUS -> PROJECT -> CONTROL.  SELECT.
//
// From a mode that is not on the cycle at all, the next one is CONTROL: the
// way out of somewhere SELECT cannot reach is the way back to the start, not
// an arbitrary station one past where you happen to be.
void status_mode_next(void);

// The STATUS screen's tabs.
//
// It was a full-screen log and nothing else, which is a lot of screen for a
// thing you glance at twice a session. The clipboard preview shares it: both
// are read rather than driven, both want the whole panel, and neither is worth
// a stop of its own on a button that already has four.
//
// Touch only, deliberately. PROJECT and SETTINGS can bind the d-pad because
// they own input entirely and the terminal is not on screen; STATUS keeps the
// terminal on the top panel, so a left/right that switched tabs would cost
// cursor-left and cursor-right on a screen somebody may well be typing at.
// This is the bottom panel of a touchscreen console -- a tap is not a
// compromise here, it is the native gesture.
typedef enum {
    STATUS_TAB_LOG = 0,
    STATUS_TAB_CLIP,
    STATUS_TAB_COUNT,
} status_tab_t;

status_tab_t status_tab_get(void);
void status_tab_set(status_tab_t tab);
const char *status_tab_name(status_tab_t tab);

// The strip, and the one action the clipboard tab offers. Drawing and
// hit-testing both come from these, so a tab cannot be shown in one place and
// selected in another -- the rule every picker on this device keeps.
#define STATUS_TAB_STRIP_Y 0
#define STATUS_TAB_STRIP_H 16
int status_tab_rect(status_tab_t tab, int *x, int *y, int *w, int *h);
status_tab_t status_tab_hit(int x, int y);

// "LOAD INTO COMPOSER", on the clipboard tab only. It sits above the two
// counter rows rather than in the strip: the strip is a tab strip, and a
// button living among tabs is a button people press expecting a tab.
#define STATUS_CLIP_ACTION_Y 150
#define STATUS_CLIP_ACTION_H 20
void status_clip_action_rect(int *x, int *y, int *w, int *h);
int status_clip_action_hit(int x, int y);

// Back out to CONTROL, the main screen with the buttons on it.  Returns 1 if
// there was somewhere to back out from, 0 if CONTROL was already showing.
//
// The return value is the whole contract: X is only swallowed when it did
// something.  On CONTROL it falls through and still sends Escape, so the
// default typing surface loses nothing to a navigation key -- which is the
// only reason this can be bound to a face button at all.
int status_mode_back(void);

// Jump straight to a mode, for the Control screen's own "KEYS" button. Ignores
// a value outside the enum rather than leaving the sub screen on a mode number
// that has no draw path.
void status_mode_set(screen_mode_t mode);

// Format the two status/counter lines, each padded with spaces to exactly
// LOG_COLS characters so a shorter value never leaves stale glyphs behind:
//
//   rx %lu B msg %lu tx %lu
//   L%lu raw %03X lib %03X
//
// The second line carries the loop counter -- the "L<number>" line a
// photograph checks to confirm the card is running this build.
void status_format_counters(unsigned long rx, unsigned long msgs,
                            unsigned long tx, unsigned long loops,
                            unsigned raw, unsigned lib,
                            char line1[LOG_COLS + 1], char line2[LOG_COLS + 1]);

// The Control screen's top line: round trip, battery, signal, and whether
// tones are muted.
//
// Every reading carries a "known" flag rather than a sentinel value, because
// the honest states are different: an unanswered probe prints "--ms" and a
// battery that has not been read prints "bat --". A stale number is worse than
// no number -- it cannot be told apart from a working one.
//
// `dsi_mode` selects how the battery reads. libnds normalises both consoles to
// a 0-15 scale, but a DS/DS Lite only distinguishes high from low and is given
// 15 or 3 to say so; printing "3/15" there would invent twelve levels of
// precision the hardware does not have, so that case prints "bat low" instead.
void status_link_set(unsigned rtt_ms, int rtt_known,
                     unsigned battery, int battery_known,
                     int external_power, int dsi_mode,
                     int rssi, int rssi_known, int muted);
void status_format_link(char line[LOG_COLS + 1]);
// A DSi battery reading (0-15) as the bars the DSi's own menu shows, 0-4.
unsigned status_battery_bars(unsigned level);

// The name of the computer the card is connected to, first on the link line.
#define STATUS_HOST_MAX 14
void status_link_host(const char *name);
// Signal as 1..4 bars, or -1 when the reading has no scale (DS mode).  DSi
// mode's dBm arrives through an unsigned byte and is read back as signed.
int status_wifi_bars(int rssi, int dsi_mode);
int status_muted(void);

// Last scrollback position reported by MSG_VIEW_POS. It is unknown until the
// first response because the server sends VIEW_POS only after this client has
// proved support by sending VIEW. Clearing on reconnect prevents stale state
// from the previous connection being shown.
void status_view_pos_clear(void);
void status_view_pos_set(unsigned lines_back, unsigned available);

// The last known position, for "return to live": it needs the current depth to
// ask for the one delta that reaches the bottom. Returns 0 while the position
// is still unknown, so a caller cannot mistake "not told yet" for "at live".
int status_view_pos_get(unsigned *lines_back, unsigned *available);

// The scrolling regime the server last announced (MSG_MODE): 1 while the
// application on screen owns its own scrollback. Unknown until the first MODE,
// which arrives right after AUTH_OK, so "not told yet" and "server scrolls"
// are distinct states. Cleared on reconnect, because the server re-announces
// and a stale regime makes a physical button do the wrong thing.
void status_scroll_regime_set(int app_scrolls);
void status_scroll_regime_clear(void);

// Format a space-padded 42-column line: "12 of 40 back" when there is history,
// "no history" when there is none, and "-- of -- back" while the position is
// unknown. The middle case is its own line rather than "0 of 0 back" because a
// fresh session and a broken feedback path both showed zeros, and telling them
// apart was the whole point of reporting the position at all.
void status_format_view_pos(char line[LOG_COLS + 1]);

#endif  // DSTERM_STATUS_H
