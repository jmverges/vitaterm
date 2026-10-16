#include "mirror.h"

#include <string.h>

void mirror_clear(mirror_t *m, uint16_t bg)
{
    if (!m) return;
    for (int r = 0; r < MIRROR_ROWS; r++) {
        for (int c = 0; c < MIRROR_COLS; c++) {
            mirror_cell_t *cell = &m->cells[r][c];
            cell->glyph = ' ';
            cell->flags = 0;
            cell->fg = 0x7FFF;
            cell->bg = bg;
        }
    }
}

void mirror_glyph_px(mirror_t *m, int x, int y, uint8_t glyph,
                     uint16_t fg, uint16_t bg)
{
    if (!m) return;
    const int col = (x + MIRROR_CELL_W / 2) / MIRROR_CELL_W;
    const int row = (y + MIRROR_CELL_H / 2) / MIRROR_CELL_H;
    if (x < 0 || y < 0 || col >= MIRROR_COLS || row >= MIRROR_ROWS) return;
    mirror_cell_t *cell = &m->cells[row][col];
    cell->glyph = glyph;
    cell->fg = fg;
    cell->bg = bg;
}

void mirror_fill_px(mirror_t *m, int x, int y, int w, int h, uint16_t bg)
{
    if (!m || w <= 0 || h <= 0) return;
    for (int r = 0; r < MIRROR_ROWS; r++) {
        const int cy = r * MIRROR_CELL_H + MIRROR_CELL_H / 2;
        if (cy < y || cy >= y + h) continue;
        for (int c = 0; c < MIRROR_COLS; c++) {
            const int cx = c * MIRROR_CELL_W + MIRROR_CELL_W / 2;
            if (cx < x || cx >= x + w) continue;
            mirror_cell_t *cell = &m->cells[r][c];
            cell->glyph = ' ';
            cell->bg = bg;
        }
    }
}

void mirror_flag_rows(mirror_t *m, int y0, int y1, uint8_t flag)
{
    if (!m || y1 < y0) return;
    int r0 = y0 / MIRROR_CELL_H, r1 = y1 / MIRROR_CELL_H;
    if (r0 < 0) r0 = 0;
    if (r1 >= MIRROR_ROWS) r1 = MIRROR_ROWS - 1;
    for (int r = r0; r <= r1; r++) {
        for (int c = 0; c < MIRROR_COLS; c++) m->cells[r][c].flags |= flag;
    }
}

size_t mirror_encode(const mirror_t *m, uint8_t frame_flags, uint8_t *out,
                     size_t cap)
{
    if (!m || !out || cap < MIRROR_PAYLOAD_SIZE) return 0;
    size_t at = 0;
    out[at++] = MIRROR_COLS;
    out[at++] = MIRROR_ROWS;
    out[at++] = frame_flags;
    for (int r = 0; r < MIRROR_ROWS; r++) {
        for (int c = 0; c < MIRROR_COLS; c++) {
            const mirror_cell_t *cell = &m->cells[r][c];
            out[at++] = cell->glyph;
            out[at++] = cell->flags;
            out[at++] = (uint8_t)(cell->fg >> 8);
            out[at++] = (uint8_t)cell->fg;
            out[at++] = (uint8_t)(cell->bg >> 8);
            out[at++] = (uint8_t)cell->bg;
        }
    }
    return at;
}

uint32_t mirror_hash(const mirror_t *m)
{
    if (!m) return 0;
    // FNV-1a over the cells: plenty to tell one screen from the next.
    uint32_t h = 2166136261u;
    const uint8_t *p = (const uint8_t *)m->cells;
    for (size_t i = 0; i < sizeof(m->cells); i++) {
        h ^= p[i];
        h *= 16777619u;
    }
    return h;
}
