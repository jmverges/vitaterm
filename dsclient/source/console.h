// Which console this is, and what that means for Wi-Fi.
//
// The same ROM runs on every DS: a DS or DS Lite from a flashcart, a DSi or
// DSi XL in DSi mode (or DS mode from a flashcart), and every 3DS and 2DS
// through TWiLight Menu++ in DSi mode (or DS mode).  What differs is mostly
// Wi-Fi -- DS mode can only join open networks, DSi mode can join WPA2 -- and
// the advice for a network the console cannot join has to name the fix that
// applies to *this* console, or it is advice to try something that cannot
// work (docs/HARDWARE.md).
//
// Detection reads two firmware bytes (GBATEK, "DS Firmware Header" and "Wifi
// Calibration Data"): 0x1D is the console type, 0x1FD the Wi-Fi board, whose
// value 03h means a 3DS.  In DSi mode a 3DS also has 32 MB of RAM, which
// libnds reports as isHwDebugger().  The classifier is pure; boot.c reads.

#ifndef DSTERM_CONSOLE_H
#define DSTERM_CONSOLE_H

#include <stdint.h>

typedef enum {
    CONSOLE_UNKNOWN = 0,
    CONSOLE_DS,          // original "Phat" DS, and the iQue DS
    CONSOLE_DS_LITE,     // DS Lite, and the iQue DS Lite
    CONSOLE_DSI,         // DSi and DSi XL
    CONSOLE_3DS,         // 3DS, 3DS XL, 2DS, New 3DS, New 3DS XL, New 2DS XL
} console_family_t;

typedef struct {
    console_family_t family;
    int dsi_mode;        // running in DSi mode (WPA2 possible)
} console_t;

console_family_t console_classify(uint8_t fw_type, uint8_t fw_board,
                                  int dsi_mode, int ram32);

// "DSi (DSi mode)", "3DS (DS mode)", "DS Lite" -- for the log and ABOUT.
const char *console_name(const console_t *c);

// What to say when a network needs WPA/WPA2 and this console is in DS mode:
// the fix differs between a DS that cannot do it at all and a DSi or 3DS
// that was merely started the wrong way.
const char *console_wpa_advice(const console_t *c);

// What to say when a scan hears nothing at all.
const char *console_silent_advice(const console_t *c);

#endif  // DSTERM_CONSOLE_H
