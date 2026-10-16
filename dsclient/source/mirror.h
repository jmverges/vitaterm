// The bottom screen as text, for `dsterm view` on the computer.
//
// The bottom screen is drawn as pixels, but almost all of it is 6x8 glyphs
// and filled rectangles, so it fits the same 42x24 grid of cells the top
// screen does.  Every glyph and fill the bottom screen draws is recorded here
// as it is drawn (subscreen.c's helpers): a glyph at pixel (x, y) lands in
// the cell under its centre, and a fill paints the cells whose centres it
// covers.  The computer draws the grid as text -- crisp at any size, where
// a pixel copy would be either huge or unreadable -- and a click on a cell
// becomes a tap at that cell's centre, which the DS hit-tests as it would a
// finger (main.c, only with SETTINGS -> COMPUTER TAPS on).
//
// The camera's viewfinder is pixels, so its rows are flagged rather than
// copied; the computer draws its own live view there.
//
// Pure C, host-tested.

#ifndef DSTERM_MIRROR_H
#define DSTERM_MIRROR_H

#include <stddef.h>
#include <stdint.h>

#define MIRROR_COLS 42
#define MIRROR_ROWS 24
#define MIRROR_CELL_W 6
#define MIRROR_CELL_H 8

// Cell flags.
#define MIRROR_FLAG_CAMERA 0x01   // the camera's viewfinder is under this cell

typedef struct {
    uint8_t glyph;     // the DS font's byte: ASCII, or the extended table
    uint8_t flags;
    uint16_t fg;       // RGB15, as the DS draws it
    uint16_t bg;
} mirror_cell_t;

typedef struct {
    mirror_cell_t cells[MIRROR_ROWS][MIRROR_COLS];
} mirror_t;

void mirror_clear(mirror_t *m, uint16_t bg);
// One glyph drawn with its top-left at pixel (x, y).
void mirror_glyph_px(mirror_t *m, int x, int y, uint8_t glyph,
                     uint16_t fg, uint16_t bg);
// A filled rectangle: the cells whose centres it covers become blanks in `bg`.
void mirror_fill_px(mirror_t *m, int x, int y, int w, int h, uint16_t bg);
// Flag the cell rows that pixel rows y0..y1 (inclusive) fall in.
void mirror_flag_rows(mirror_t *m, int y0, int y1, uint8_t flag);

// The wire form (SUBSCREEN): cols, rows, flags, then each cell row-major as
// glyph, flags, fg (u16be), bg (u16be).  `frame_flags` bit 0 says computer
// taps are on.  Returns the length, or 0 if it does not fit `cap`.
#define MIRROR_CELL_BYTES 6
#define MIRROR_PAYLOAD_SIZE (3 + MIRROR_ROWS * MIRROR_COLS * MIRROR_CELL_BYTES)  // 6051
#define MIRROR_FRAME_TAPS 0x01
size_t mirror_encode(const mirror_t *m, uint8_t frame_flags, uint8_t *out,
                     size_t cap);

// A cheap fingerprint, so an unchanged screen is not sent again.
uint32_t mirror_hash(const mirror_t *m);

#endif  // DSTERM_MIRROR_H
