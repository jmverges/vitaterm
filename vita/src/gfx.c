// Drawing helpers: the system font for everything that is not the terminal.
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "app.h"

vita2d_font *font;

void gfx_init(void)
{
    vita2d_init();
    vita2d_set_clear_color(COL_BG);
    vita2d_set_vblank_wait(1);
    // A font of our own rather than the system's PGF: the same on every
    // firmware, and in Vita3K, whose PGF support crashes vita2d.
    font = vita2d_load_font_file("app0:/font.ttf");
}

void gfx_begin(void)
{
    vita2d_start_drawing();
    vita2d_clear_screen();
}

void gfx_end(void)
{
    vita2d_end_drawing();
    vita2d_common_dialog_update();
    vita2d_swap_buffers();
}

void gfx_text(int x, int y, unsigned colour, float scale, const char *text)
{
    vita2d_font_draw_text(font, x, y, colour, (unsigned)(scale * 20.0f + 0.5f), text);
}

int gfx_text_width(float scale, const char *text)
{
    return vita2d_font_text_width(font, (unsigned)(scale * 20.0f + 0.5f), text);
}

void gfx_textf(int x, int y, unsigned colour, float scale, const char *fmt, ...)
{
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    gfx_text(x, y, colour, scale, buf);
}

void gfx_rect(int x, int y, int w, int h, unsigned colour)
{
    vita2d_draw_rectangle(x, y, w, h, colour);
}

void gfx_panel(int x, int y, int w, int h)
{
    gfx_rect(x, y, w, h, COL_PANEL);
    gfx_rect(x, y, w, 2, COL_ACCENT);
}

int gfx_wrap(int x, int y, int w, unsigned colour, float scale, const char *text)
{
    const int line_h = (int)(26 * scale);
    int used = 0;
    char line[256];
    const char *p = text;
    while (*p) {
        // One paragraph line at a time; an explicit newline always breaks.
        size_t take = 0, fit = 0;
        while (p[take] && p[take] != '\n' && take < sizeof(line) - 1) {
            memcpy(line, p, take + 1);
            line[take + 1] = '\0';
            if (gfx_text_width(scale, line) > w) break;
            take++;
            if (p[take] == ' ' || p[take] == '\0' || p[take] == '\n') fit = take;
        }
        if (p[take] == '\0' || p[take] == '\n') fit = take;
        if (fit == 0) fit = take ? take : 1;
        memcpy(line, p, fit);
        line[fit] = '\0';
        gfx_text(x, y + used + (int)(20 * scale), colour, scale, line);
        used += line_h;
        p += fit;
        while (*p == ' ') p++;
        if (*p == '\n') p++;
    }
    return used;
}

void gfx_working(const char *title, const char *body)
{
    gfx_begin();
    app_draw_terminal();
    gfx_rect(0, 0, SCREEN_W, SCREEN_H, 0xB0000000u);
    const int w = 620, x = (SCREEN_W - w) / 2, y = 150;
    gfx_panel(x, y, w, 240);
    gfx_text(x + 24, y + 44, COL_ACCENT, 1.3f, title);
    gfx_wrap(x + 24, y + 64, w - 48, COL_TEXT, 1.0f, body);
    gfx_end();
}

// The PlayStation button glyphs, drawn rather than taken from a font so
// they look the same everywhere.
static void glyph(int x, int y, const char *which)
{
    const unsigned c = 0xFFD0D0D0u;
    if (!strcmp(which, "cross")) {
        for (int i = -6; i <= 6; i++) {
            gfx_rect(x + i, y + i, 2, 2, 0xFFE0A070u);
            gfx_rect(x + i, y - i, 2, 2, 0xFFE0A070u);
        }
    } else if (!strcmp(which, "circle")) {
        // A ring: every pixel whose distance from the centre is about 6.
        for (int dy = -7; dy <= 7; dy++) {
            for (int dx = -7; dx <= 7; dx++) {
                const int d2 = dx * dx + dy * dy;
                if (d2 >= 30 && d2 <= 50) gfx_rect(x + dx, y + dy, 1, 1, 0xFF6060E0u);
            }
        }
    } else if (!strcmp(which, "triangle")) {
        for (int i = 0; i <= 12; i++) {
            gfx_rect(x - i / 2, y - 6 + i, 2, 2, 0xFF90D060u);
            gfx_rect(x + i / 2, y - 6 + i, 2, 2, 0xFF90D060u);
        }
        gfx_rect(x - 6, y + 6, 13, 2, 0xFF90D060u);
    } else if (!strcmp(which, "square")) {
        gfx_rect(x - 6, y - 6, 13, 2, 0xFFD080D0u);
        gfx_rect(x - 6, y + 5, 13, 2, 0xFFD080D0u);
        gfx_rect(x - 6, y - 6, 2, 13, 0xFFD080D0u);
        gfx_rect(x + 5, y - 6, 2, 13, 0xFFD080D0u);
    } else {
        // A word in a pill: L, R, START, SELECT, the d-pad.
        const int w = gfx_text_width(0.8f, which) + 10;
        gfx_rect(x - 7, y - 9, w, 19, 0xFF505050u);
        gfx_text(x - 2, y + 6, c, 0.8f, which);
    }
}

void gfx_hints(const char *const *pairs, int n)
{
    int x = 20;
    const int y = SCREEN_H - 20;
    gfx_rect(0, SCREEN_H - 40, SCREEN_W, 40, 0xFF100C0Au);
    for (int i = 0; i < n; i++) {
        const char *g = pairs[2 * i], *label = pairs[2 * i + 1];
        glyph(x + 7, y - 1, g);
        const int gw = (!strcmp(g, "cross") || !strcmp(g, "circle") ||
                        !strcmp(g, "triangle") || !strcmp(g, "square"))
                           ? 16 : gfx_text_width(0.8f, g) + 6;
        gfx_text(x + gw + 6, y + 6, COL_TEXT, 0.9f, label);
        x += gw + 6 + gfx_text_width(0.9f, label) + 26;
    }
}
