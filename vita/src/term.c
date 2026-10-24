// The terminal grid, drawn on the CPU into one texture a cell at a time
// (only what changed), and put on screen with a single draw.  Thousands of
// per-cell GPU draws a frame would cost more than blending 128 pixels on the
// rare cell that changes.
#include <string.h>

#include "app.h"
#include "atlas.h"

static vita2d_texture *tex;
static uint32_t *px;
static unsigned stride;     // in pixels

static cell_t shown[PROTO_CELLS];
static int cur_row = -1, cur_col = -1, cur_visible = 0;

// The DS palette (render.c), as RGB.  Brighter than the DS's: the Vita's
// screen is not the DS Lite's dim one.
enum { PAL_DEFAULT, PAL_BLACK, PAL_RED, PAL_GREEN, PAL_YELLOW, PAL_BLUE,
       PAL_MAGENTA, PAL_CYAN, PAL_WHITE, PAL_ORANGE, PAL_COUNT };
static const uint32_t palette[PAL_COUNT] = {
    [PAL_DEFAULT] = 0xDCDCDC, [PAL_BLACK]   = 0x000000,
    [PAL_RED]     = 0xE05555, [PAL_GREEN]   = 0x55D46A,
    [PAL_YELLOW]  = 0xE6C84A, [PAL_BLUE]    = 0x5C8CF0,
    [PAL_MAGENTA] = 0xD46AD4, [PAL_CYAN]    = 0x4ACCD6,
    [PAL_WHITE]   = 0xFFFFFF, [PAL_ORANGE]  = 0xE08A5A,
};
#define BG_DEFAULT 0x000000u

static uint32_t rgb_of(uint8_t idx, int bold)
{
    uint32_t c = palette[idx < PAL_COUNT ? idx : PAL_DEFAULT];
    if (!bold) return c;
    unsigned r = (c >> 16) & 255, g = (c >> 8) & 255, b = c & 255;
    r += (255 - r) / 3; g += (255 - g) / 3; b += (255 - b) / 3;
    return (r << 16) | (g << 8) | b;
}

static uint32_t bg_of(uint8_t idx)
{
    if (idx == PAL_DEFAULT) return BG_DEFAULT;
    return palette[idx < PAL_COUNT ? idx : PAL_BLACK];
}

// RGB to the texture's ABGR.
static inline uint32_t abgr(uint32_t rgb)
{
    return 0xFF000000u | ((rgb & 0xFF) << 16) | (rgb & 0xFF00) | ((rgb >> 16) & 0xFF);
}

static void paint(int row, int col, cell_t c, int caret)
{
    const int bold = c.flags & CELL_FLAG_BOLD;
    uint32_t fg = rgb_of(c.attr >> 4, bold);
    uint32_t bg = bg_of(c.attr & 0x0F);
    if (c.flags & CELL_FLAG_REVERSE) { const uint32_t t = fg; fg = bg; bg = t; }
    if (caret) { fg = 0x000000; bg = 0xE08A5A; }

    const unsigned fr = (fg >> 16) & 255, fgg = (fg >> 8) & 255, fb = fg & 255;
    const unsigned br = (bg >> 16) & 255, bgg = (bg >> 8) & 255, bb = bg & 255;
    const uint8_t glyph = c.glyph;
    const int ax = (glyph % 16) * CELL_W;
    const int ay = (glyph / 16) * CELL_H + (bold ? 16 * CELL_H : 0);
    uint32_t *dst = px + (size_t)row * CELL_H * stride + (size_t)col * CELL_W;
    for (int y = 0; y < CELL_H; y++) {
        const uint8_t *a = atlas_px + (size_t)(ay + y) * ATLAS_W + ax;
        for (int x = 0; x < CELL_W; x++) {
            const unsigned k = a[x];
            const unsigned r = (fr * k + br * (255 - k)) / 255;
            const unsigned g = (fgg * k + bgg * (255 - k)) / 255;
            const unsigned b = (fb * k + bb * (255 - k)) / 255;
            dst[x] = 0xFF000000u | (b << 16) | (g << 8) | r;
        }
        dst += stride;
    }
}

void term_init(void)
{
    tex = vita2d_create_empty_texture_format(TERM_W, TERM_H,
                                             SCE_GXM_TEXTURE_FORMAT_A8B8G8R8);
    vita2d_texture_set_filters(tex, SCE_GXM_TEXTURE_FILTER_POINT,
                               SCE_GXM_TEXTURE_FILTER_POINT);
    px = vita2d_texture_get_datap(tex);
    stride = vita2d_texture_get_stride(tex) / 4;
    for (unsigned y = 0; y < TERM_H; y++) {
        for (unsigned x = 0; x < TERM_W; x++) px[y * stride + x] = abgr(BG_DEFAULT);
    }
    memset(shown, 0, sizeof(shown));
    for (int i = 0; i < PROTO_CELLS; i++) shown[i].glyph = ' ';
}

void term_cell(int row, int col, cell_t c)
{
    if (row < 0 || row >= PROTO_ROWS || col < 0 || col >= PROTO_COLS) return;
    shown[row * PROTO_COLS + col] = c;
    paint(row, col, c, cur_visible && row == cur_row && col == cur_col);
}

void term_all(const grid_t *g)
{
    for (int r = 0; r < PROTO_ROWS; r++) {
        for (int c = 0; c < PROTO_COLS; c++) {
            term_cell(r, c, g->cells[r * PROTO_COLS + c]);
        }
    }
}

void term_cursor(int row, int col, int visible)
{
    const int old_r = cur_row, old_c = cur_col, old_v = cur_visible;
    cur_row = row;
    cur_col = col;
    cur_visible = visible;
    if (old_v && old_r >= 0 && old_r < PROTO_ROWS && old_c >= 0 && old_c < PROTO_COLS) {
        paint(old_r, old_c, shown[old_r * PROTO_COLS + old_c], 0);
    }
    if (visible && row >= 0 && row < PROTO_ROWS && col >= 0 && col < PROTO_COLS) {
        paint(row, col, shown[row * PROTO_COLS + col], 1);
    }
}

void term_draw(void)
{
    vita2d_draw_texture(tex, 0, BAR_H);
}
