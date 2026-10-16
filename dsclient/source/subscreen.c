#include "subscreen.h"

#include <nds.h>
#include <stdio.h>
#include <string.h>

#include "camera.h"
#include "chime.h"
#include "ext.h"
#include "focus.h"
#include "kbd.h"
#include "project.h"
#include "mirror.h"
#include "privacy.h"
#include "log.h"
#include "render.h"
#include "sessions.h"
#include "sound.h"
#include "status.h"

// The sub engine has no LCD framebuffer mode -- MODE_FB0 is main-engine only --
// so this is an extended-rotoscale 16-bit bitmap background, 256x256. Written
// exactly like the top screen's framebuffer, so the 6x8 grid and the ORIGIN_X
// offset carry over: 42 cols x 24 rows at 6x8 fills 256x192 of it.
#define SUB_W 256
#define SUB_H 256

// Tile shades, shared by the keyboard keys and the larger Control tiles. Keys
// read as light-on-dark with the 1px gaps as separators; a held key inverts to
// a bright tile with dark text so a press is visible in a photograph.
//
// Restrained on purpose: the highlight is a warm amber rather than white, one
// accent against a near-black screen. The DS Phat/Lite panels are dim and a
// saturated palette blooms, and a status screen that has to be read at a glance
// while something is running is worse for every colour added to it.
#define KBD_TILE    ARGB16(1, 5, 6, 8)
#define KBD_TILE_HI ARGB16(1, 27, 22, 12)
#define KBD_TEXT    ARGB16(1, 24, 25, 27)
#define KBD_TEXT_HI ARGB16(1, 2, 2, 2)
#define SUB_BG      ARGB16(1, 0, 0, 0)
// The Control tiles' small second line, and the bevel that makes a tile read
// as something to press rather than a box drawn round a word: one row of
// light along the top, one of shadow along the bottom.
#define TILE_SUB    ARGB16(1, 14, 15, 17)
#define TILE_LIGHT  ARGB16(1, 9, 10, 13)
#define TILE_SHADE  ARGB16(1, 2, 2, 3)
// Text that explains rather than reports: the button legend.
#define HINT_DIM    ARGB16(1, 13, 13, 14)

// What the face buttons do now (subscreen_set_button_legend), for the
// Control screen's legend and the approval hint.
static char legend_buttons[48] = "A enter  B delete  X esc  Y tab";
static char legend_enter[8] = "A";
static char legend_stop[8] = "START";
static char legend_tab[8] = "Y";


static uint16_t *sub;
// The lower background and where the shake has it, mirroring the top
// screen's pair for the same reason: a landing down here should move this
// panel, not the other one.
static int sub_bg_id;
static int sub_shake_y;
static int composer_active;
static int quick_active;
static int quick_level;
static int quick_selected;
static int quick_top;
static const quick_commands_t *quick_cmds;
static const quick_recent_t *quick_recent;
// Whether this card has a camera (a DSi): QUICK's PHOTO row exists only
// where there is a sensor behind it.
static int quick_camera;
// The camera screen: the two strips' text, pushed in composed.  The rows
// between them are the viewfinder's, written by subscreen_camera_blit() and
// never touched by the layout draw.
static int camera_active;
static int camera_error;
static char camera_top[LOG_COLS + 1];
static char camera_hint[LOG_COLS + 1];
static char claude_mode[QUICK_MODE_MAX + 1];
static int rewind_on;
static int remote_active;
static const remote_list_t *remote_list;
static const remote_computers_t *remote_comps;
static const remote_ui_t *remote_ui_shown;
static const char *remote_here;
// The clipboard, as the STATUS screen's CLIP tab should show it. Pushed in
// like everything else here: main composes the summary and the wrapped lines,
// this module only decides where they go.
static char clip_summary_line[CLIP_LINE_COLS + 1];
static char clip_lines[SUBSCREEN_CLIP_LINES_MAX][CLIP_LINE_COLS + 1];
static unsigned clip_line_count;
static int clip_can_paste;

static settings_t settings_shown;
static settings_ui_t settings_ui_shown;
static project_action_t project_action_list[PROJECT_ACTIONS_MAX];
static unsigned project_action_count;
static unsigned project_action_highlight;
static unsigned project_action_dropped;
static int project_god_mode;
static char project_lines[SUBSCREEN_PROJECT_LINES_MAX][44];
static unsigned project_line_count;
static unsigned project_tab_sel;
void subscreen_set_shake(int offset_y)
{
    int dy = offset_y;
    if (dy > (int)RENDER_SHAKE_MAX) dy = (int)RENDER_SHAKE_MAX;
    if (dy < -(int)RENDER_SHAKE_MAX) dy = -(int)RENDER_SHAKE_MAX;
    if (dy == sub_shake_y) return;
    sub_shake_y = dy;
    bgSetScroll(sub_bg_id, 0, dy);
    bgUpdate();
}

static cell_t text_cell(uint8_t glyph, uint8_t fg)
{
    cell_t c = {glyph, (uint8_t)((fg << 4) | PAL_BLACK), 0};
    return c;
}

// The bottom screen as text, for the computer's view (mirror.h): every
// glyph and fill below is recorded here as it is drawn.
static mirror_t sub_mirror;

const mirror_t *subscreen_mirror(void) { return &sub_mirror; }

// A glyph at a pixel position: drawn, and recorded in the mirror.
static void glyph_px(int x, int y, uint8_t glyph, uint16_t fg, uint16_t bg)
{
    render_glyph_into(sub, SUB_W, x, y, glyph, fg, bg);
    mirror_glyph_px(&sub_mirror, x, y, glyph, fg, bg);
}

// A filled rectangle, likewise.
static void fill_px(int x, int y, int w, int h, uint16_t colour)
{
    for (int yy = y; yy < y + h; yy++)
        for (int xx = x; xx < x + w; xx++) sub[yy * SUB_W + xx] = colour;
    mirror_fill_px(&sub_mirror, x, y, w, h, colour);
}

// One 6x8 cell. apply_caret is 0: the sub screen must never take the caret
// inversion, which belongs to the main terminal.
static void paint_char(int row, int col, uint8_t glyph, uint8_t fg)
{
    render_cell_into(sub, SUB_W, 0, row, col, text_cell(glyph, fg));
    mirror_glyph_px(&sub_mirror, col * 6, row * 8, glyph,
                    render_palette_rgb(fg), render_palette_rgb(PAL_BLACK));
}

// A whole line, space-padded to the full width so a shorter line never leaves
// stale glyphs from the previous frame on the row.
static void draw_line(int row, const char *s, uint8_t fg)
{
    s = privacy_text(s);
    int col = 0;
    for (; col < LOG_COLS && s[col]; col++) paint_char(row, col, (uint8_t)s[col], fg);
    for (; col < LOG_COLS; col++) paint_char(row, col, ' ', fg);
}

// The same row, in two colours: everything before `split` in `fg`, everything
// from it on in `fg2`.  One line, one row, no layout of its own -- the split
// exists only so the quip can be told apart from the state in front of it.
static void draw_line_split(int row, const char *s, uint8_t fg,
                            unsigned split, uint8_t fg2)
{
    s = privacy_text(s);
    int col = 0;
    for (; col < LOG_COLS && s[col]; col++) {
        paint_char(row, col, (uint8_t)s[col],
                   (unsigned)col >= split ? fg2 : fg);
    }
    for (; col < LOG_COLS; col++) paint_char(row, col, ' ', fg);
}

// A selected row needs a fill, not merely orange letters: on a dim panel an
// outline disappears among terminal text.  This stays cell-aligned so the
// same 42-column clipping rule as draw_line() applies to every session name.
static void draw_selected_line(int row, const char *s, int selected)
{
    const uint16_t bg = selected ? render_palette_rgb(PAL_ORANGE) : SUB_BG;
    const uint8_t fg = selected ? PAL_BLACK : PAL_DEFAULT;
    const char *p = s ? privacy_text(s) : "";
    for (int col = 0; col < LOG_COLS; col++) {
        const uint8_t glyph = *p ? (uint8_t)*p++ : (uint8_t)' ';
        // render_cell_into takes a palette attribute, not a raw background;
        // draw the glyph directly so the selected orange field is exact.
        glyph_px(col * 6, row * 8, glyph,
                          render_palette_rgb(fg), bg);
    }
}

void subscreen_draw_line(int row, const char *s, uint8_t fg)
{
    draw_line(row, s, fg);
}

void subscreen_draw_line_split(int row, const char *s, uint8_t fg,
                               unsigned split, uint8_t fg2)
{
    draw_line_split(row, s, fg, split, fg2);
}

void subscreen_draw_selected_line(int row, const char *s, int selected)
{
    draw_selected_line(row, s, selected);
}

// The newest (bottom - top + 1) lines of the log, newest at `bottom`, blank
// above once the log has fewer lines than the region.
static void draw_log_region(int top, int bottom)
{
    const int n = log_count();
    for (int r = top; r <= bottom; r++) {
        const int idx = n - (bottom - r) - 1;   // newest line lands on `bottom`
        draw_line(r, idx < 0 ? "" : log_get(idx), PAL_DEFAULT);
    }
}

// Raw-pixel tile shared by keyboard keys, the scroll controls, and the Control
// screen's buttons. Raw pixels rather than cells because none of them are 6x8
// multiples: each is a filled rectangle with its label centred.
static void draw_tile(int x, int y, int w, int h, const char *label,
                     int held, int focused)
{
    // Focus is painted in Claude's own orange -- palette index 9, the colour
    // of his mark and box rules -- so the lit item reads as part of the thing
    // this device is driving. A fill, not an outline: it has to read at a
    // glance from arm's length on a dim panel, and nothing subtle does.
    const uint16_t tile = focused ? render_palette_rgb(PAL_ORANGE)
                                  : held ? KBD_TILE_HI : KBD_TILE;
    const uint16_t text = (focused || held) ? KBD_TEXT_HI : KBD_TEXT;
    fill_px(x, y, w, h, tile);
    const int len = (int)strlen(label);
    const int tx = x + (w - len * 6) / 2;
    const int ty = y + (h - 8) / 2;
    for (int i = 0; i < len; i++)
        glyph_px(tx + i * 6, ty, (uint8_t)label[i], text, tile);
}

// A Control tile: the destination in capitals and, under it, what it is for
// in a quieter grey -- so a person who has never held the card can tell
// SESSIONS from REMOTE without opening both.  Focus and held states are the
// plain tile's, so the d-pad highlight looks the same everywhere.
static void draw_text_px(int x, int y, const char *text, uint16_t fg,
                         uint16_t bg)
{
    for (int i = 0; text[i]; i++) {
        glyph_px(x + i * 6, y, (uint8_t)text[i], fg, bg);
    }
}

static void draw_tile2(int x, int y, int w, int h, const char *label,
                       const char *sub_label, int held, int focused)
{
    const int lit = focused || held;
    const uint16_t tile = focused ? render_palette_rgb(PAL_ORANGE)
                                  : held ? KBD_TILE_HI : KBD_TILE;
    for (int yy = y; yy < y + h; yy++) {
        const uint16_t c = lit ? tile : yy == y ? TILE_LIGHT
                                      : yy == y + h - 1 ? TILE_SHADE : tile;
        for (int xx = x; xx < x + w; xx++) sub[yy * SUB_W + xx] = c;
    }
    mirror_fill_px(&sub_mirror, x, y, w, h, tile);
    const int has_sub = sub_label && sub_label[0];
    const int ly = y + (h - 8) / 2 - (has_sub ? 5 : 0);
    const int llen = (int)strlen(label);
    draw_text_px(x + (w - llen * 6) / 2, ly, label,
                 lit ? KBD_TEXT_HI : KBD_TEXT, tile);
    if (has_sub) {
        const int slen = (int)strlen(sub_label);
        draw_text_px(x + (w - slen * 6) / 2, ly + 11, sub_label,
                     lit ? KBD_TEXT_HI : TILE_SUB, tile);
    }
}

// One full-width row in an arbitrary colour, for the quieter text the palette
// has no entry for.
static void draw_line_rgb(int row, const char *s, uint16_t fg)
{
    s = privacy_text(s);
    int col = 0;
    for (; col < LOG_COLS && s[col]; col++) {
        glyph_px(col * 6, row * 8, (uint8_t)s[col], fg,
                          SUB_BG);
    }
    for (; col < LOG_COLS; col++) {
        glyph_px(col * 6, row * 8, ' ', fg, SUB_BG);
    }
}

// PROJECT: the tab strip and the selected tab's lines.
//
// The content proper is on the top screen, where there is room to read it.
// This half exists because the top screen cannot be touched, so the tabs and
// the buttons have to live down here -- which is the natural shape for the
// device rather than a compromise.
static void draw_project_screen(void)
{
    char line[LOG_COLS + 1];
    status_format_link(line);
    draw_line(0, line, PAL_WHITE);
    // Row 1 is the extension's when it has a line for it, same as on Control:
    // this is a screen you sit on for a while.
    if (!ext_subscreen_draw(EXT_DRAW_PROJECT_STATUS)) {
        status_format_view_pos(line);
        draw_line(1, line, PAL_DEFAULT);
    }

    // Rows 2--4 are the tab strip's pixels.  Blank them as rows first so the
    // two pixel rows above the tiles are clean, then draw the tiles over the
    // top; the strip is inset by the same two pixels the Control tiles use.
    for (int row = 2; row <= 4; row++) draw_line(row, "", PAL_DEFAULT);
    for (int i = 0; i < PROJECT_TAB_COUNT; i++) {
        int x, y, w, h;
        if (!project_tab_rect((project_tab_t)i, &x, &y, &w, &h)) continue;
        draw_tile(x, y, w, h, project_tab_name((project_tab_t)i), 0,
                  (unsigned)i == project_tab_sel);
    }

    if (project_tab_sel == PROJECT_TAB_DO) {
        // The DO tab spends this half on buttons instead of text.  The command
        // each one types is on the top screen next to its label, so nothing is
        // pressed unread unless somebody deliberately armed that.
        for (int row = 5; row <= 19; row++) draw_line(row, "", PAL_DEFAULT);
        for (unsigned i = 0; i < project_action_count; i++) {
            int x, y, w, h;
            if (!project_action_rect(i, &x, &y, &w, &h)) continue;
            draw_tile(x, y, w, h, project_action_list[i].label, 0,
                      i == project_action_highlight);
        }
        if (project_god_mode) {
            draw_line(18, "GOD MODE: a tap runs it immediately", PAL_RED);
        } else {
            draw_line(18, "a tap loads it; A sends", PAL_DEFAULT);
        }
        // Never a silent cap: a truncated list that does not say so reads as
        // the whole list.
        if (project_action_dropped) {
            char note[LOG_COLS + 1];
            (void)snprintf(note, sizeof(note), "%u more not shown",
                           project_action_dropped);
            draw_line(19, note, PAL_ORANGE);
        }
    } else {
        for (int row = 5; row <= 19; row++) {
            const unsigned i = (unsigned)(row - 5);
            draw_line(row, i < project_line_count ? project_lines[i] : "",
                      PAL_DEFAULT);
        }
    }
    // Rows 20--23 stay genuinely empty, exactly as on Control: that strip is
    // left clear for whatever an extension puts there, and a sentence in it
    // would be a sentence something stands on.
    for (int row = 20; row <= 23; row++) draw_line(row, "", PAL_DEFAULT);
}

// The settings rows, as a label and a strip of pickable chips.
//
// The version this replaced drew one fat tile per row whose value advanced by
// one step every time you pressed it, so choosing the third of four options
// meant pressing a button three times and watching a word change. You could
// not see the choices, only the one you were on. Chips fix that: every option
// is on the screen, and the one that is set is lit.
//
// Nothing here decides where a chip goes -- settings.c computes each rect from
// its own label, and this draws what that says. The alternative is two lists
// of coordinates, which is how a button comes to be somewhere other than where
// it looks.
static void draw_settings_screen(void)
{
    for (int row = 0; row <= 23; row++) draw_line(row, "", PAL_DEFAULT);

    // The two tabs, on the line the page title used to have to itself, and
    // how to move between them.  The tab on screen is the highlighted row's.
    const unsigned tab = settings_row_tab((settings_row_t)settings_ui_shown.row);
    for (unsigned t = 0; t < SETTINGS_TAB_COUNT; t++) {
        int tx, ty, tw, th;
        if (!settings_tab_rect(t, &tx, &ty, &tw, &th)) continue;
        draw_tile(tx, ty, tw, th, settings_tab_label(t), t == tab, 0);
    }
    static const char hint[] = "L/R  X back";
    const int hint_x = SUB_W - 4 - (int)(sizeof(hint) - 1) * 6;
    for (int c = 0; hint[c]; c++) {
        glyph_px(hint_x + c * 6, 4, (uint8_t)hint[c],
                          HINT_DIM, SUB_BG);
    }

    const unsigned scroll = settings_ui_shown.scroll;
    for (int i = 0; i < SETTINGS_ROW_COUNT; i++) {
        const settings_row_t row = (settings_row_t)i;
        if (settings_row_tab(row) != tab) continue;
        int x, y, w, h;
        if (!settings_row_rect(row, scroll, &x, &y, &w, &h)) continue;
        const int row_selected = (unsigned)i == settings_ui_shown.row;

        // The label is text on the background rather than a tile of its own:
        // it is not pressable, and drawing it as a button would say it was.
        const char *label = settings_row_label(row);
        for (int c = 0; label[c]; c++) {
            glyph_px(x + c * 6, y + (h - 8) / 2,
                              (uint8_t)label[c],
                              render_palette_rgb(row_selected ? PAL_ORANGE
                                                              : PAL_DEFAULT),
                              SUB_BG);
        }

        const unsigned count = settings_option_count(row);
        const unsigned current = settings_option_current(&settings_shown, row);
        for (unsigned o = 0; o < count; o++) {
            int cx, cy, cw, ch;
            if (!settings_option_rect(row, o, scroll, &cx, &cy, &cw, &ch)) continue;
            // The DEFAULTS chip is the only one that renames itself, and only
            // while its confirm is pending: a destructive button that looks
            // identical armed and unarmed is a button you press twice by
            // accident.
            const char *text = settings_option_label(row, o);
            if ((row == SETTINGS_ROW_DEFAULTS || row == SETTINGS_ROW_BUTTONS_RESET) &&
                settings_ui_shown.reset_armed &&
                settings_ui_shown.row == (unsigned)row) {
                text = "SURE?";
            }
            // A fixed button's one chip is a label, not a switch: drawn flat
            // in the hint colour, and never lit.
            if (settings_row_fixed(row)) {
                for (int c = 0; text[c]; c++) {
                    glyph_px(cx + 5 + c * 6, cy + (ch - 8) / 2,
                                      (uint8_t)text[c], HINT_DIM, SUB_BG);
                }
                continue;
            }
            // Two different kinds of highlight, because they mean two
            // different things: `held` marks the option that is *set*, and the
            // orange focus fill marks the one the d-pad is *on*. On a value
            // row they are usually the same chip; on an action row nothing is
            // ever set, so only the focus fill appears.
            const int is_current = o == current;
            const int is_focused = row_selected && o == settings_ui_shown.option;
            draw_tile(cx, cy, cw, ch, text, is_current, is_focused);
        }
    }

    // A tab longer than the screen: arrows at the right edge say there is
    // more, and touching one scrolls.
    if (settings_more_above(scroll)) {
        glyph_px(SETTINGS_ARROW_X + 2,
                          SETTINGS_ROW_Y + (SETTINGS_ROW_H - 8) / 2, (uint8_t)'^',
                          render_palette_rgb(PAL_ORANGE), SUB_BG);
    }
    if (settings_more_below(tab, scroll)) {
        glyph_px(SETTINGS_ARROW_X + 2,
                          SETTINGS_ROW_Y + (SETTINGS_VISIBLE_ROWS - 1) * SETTINGS_ROW_PITCH +
                              (SETTINGS_ROW_H - 8) / 2,
                          (uint8_t)'v', render_palette_rgb(PAL_ORANGE), SUB_BG);
    }
}

// The STATUS screen: a tab strip, then either the log or the clipboard.
//
// The two counter rows at the bottom are drawn by the caller and stay put on
// both tabs -- the L<number> line a photograph checks to confirm which build
// is running has to be there whatever you are looking at.
static void draw_status_screen(void)
{
    const status_tab_t tab = status_tab_get();

    // Rows 0 and 1 are the strip. Clear them as text first so a shorter tab
    // label cannot leave the previous one's glyphs showing beside it.
    draw_line(0, "", PAL_DEFAULT);
    draw_line(1, "", PAL_DEFAULT);
    for (int i = 0; i < STATUS_TAB_COUNT; i++) {
        int x, y, w, h;
        if (!status_tab_rect((status_tab_t)i, &x, &y, &w, &h)) continue;
        draw_tile(x, y, w, h, status_tab_name((status_tab_t)i),
                  0, (status_tab_t)i == tab);
    }

    if (tab == STATUS_TAB_LOG) {
        draw_log_region(2, LOG_ROWS - 3);
        return;
    }

    for (int row = 2; row <= LOG_ROWS - 3; row++) draw_line(row, "", PAL_DEFAULT);
    // Row 2 is what the clipboard is and what was done to it, in orange
    // because it is the line that says whether what follows is the whole
    // thing. Rows 4.. are the text itself.
    draw_line(2, clip_summary_line, PAL_ORANGE);
    for (unsigned i = 0; i < clip_line_count; i++) {
        const int row = 4 + (int)i;
        if (row > LOG_ROWS - 5) break;
        draw_line(row, clip_lines[i], PAL_DEFAULT);
    }

    int ax, ay, aw, ah;
    status_clip_action_rect(&ax, &ay, &aw, &ah);
    // Drawn even when there is nothing to paste, and drawn plainly then. A
    // button that vanishes is a button somebody looks for; one that is visibly
    // inert says the clipboard is empty, which is the fact they wanted.
    draw_tile(ax, ay, aw, ah,
              clip_can_paste ? "LOAD INTO COMPOSER" : "NOTHING TO PASTE",
              0, 0);
}

static void draw_keyboard(void)
{
    const kbd_layer_t layer = kbd_layer();
    const int focused = focus_active() && focus_place() == FOCUS_KEYS;
    for (int id = 0; id < kbd_count(); id++) {
        int x, y, w, h;
        kbd_key_rect(id, layer, &x, &y, &w, &h);
        draw_tile(x, y, w, h, kbd_label(id, layer),
                  id == kbd_held(), focused && id == focus_key_id());
    }
}

// The keyboard screen's own state line, drawn above the keys rather than
// below them.
//
// Three rows used to sit under the bottom key row: the view position, the
// modifier state, and a line of button guides.  They are gone, because that
// strip is the only floor an extension has while the keyboard is up, and a
// companion who can only exist on one screen is not much of one.  The guides
// went entirely -- they taught two chords, and a chord you have learned is a
// line you are only reading past.  The state did not go: which layer you are
// on and whether Ctrl is armed both change what the next key does, so they
// move up here onto a row that was already status.
static void draw_keyboard_state(int row)
{
    const char *layer = kbd_layer() == KBD_LAYER_SHIFT ? "SHIFT" :
                        kbd_layer() == KBD_LAYER_SYM ? "SYM" : "BASE";
    char view[LOG_COLS + 1], line[LOG_COLS + 1];
    status_format_view_pos(view);
    // The modifier prefix is at most 18 columns ("SHIFT CTRL SHIFT" plus the
    // gap), so bounding the view field at 24 proves the whole line fits the
    // 42-column row rather than relying on the view text staying short.
    snprintf(line, sizeof(line), "%s%s%s  %.24s", layer,
             kbd_ctrl_armed() ? " CTRL" : "", kbd_shift_armed() ? " SHIFT" : "",
             view);
    // Focus mode keeps its colour: while it is on a physical direction moves
    // the highlight instead of the cursor, and that has to read at a glance
    // rather than be inferred from a lit key.
    draw_line(row, line, focus_active() ? PAL_ORANGE : PAL_DEFAULT);
}

// Everything below the bottom key row is left clear, for an extension to
// stand on.  Keeping the clear explicit means a stale glyph from another
// screen can never end up underfoot looking like terrain.
static void clear_keyboard_floor(void)
{
    for (int row = KBD_FLOOR_ROW; row < LOG_ROWS; row++) {
        draw_line(row, "", PAL_DEFAULT);
    }
}

// QUICK's main list: two text rows per entry, so each is a thumb's height.
#define QUICK_FIRST_LINE 3
#define QUICK_LIST_FIRST_LINE 2

static void draw_quick_main(void)
{
    draw_line(0, "QUICK   UP/DOWN choose   A do   X back", PAL_ORANGE);
    draw_line_rgb(1, "COMMANDS and AGAIN open the composer", HINT_DIM);
    for (int line = 2; line < LOG_ROWS; line++) draw_line(line, "", PAL_DEFAULT);
    for (int i = 0; i < (int)quick_row_count(quick_camera); i++) {
        char row[LOG_COLS + 1];
        const quick_row_t r = (quick_row_t)i;
        if (r == QUICK_ROW_MODE) {
            snprintf(row, sizeof(row), " %-9s %-8s A: next mode", quick_row_label(r),
                     claude_mode[0] ? claude_mode : "--");
        } else if (r == QUICK_ROW_COMMANDS && quick_cmds) {
            snprintf(row, sizeof(row), " %-9s %-23s %2u", quick_row_label(r),
                     quick_row_note(r), quick_cmds->count);
        } else if (r == QUICK_ROW_AGAIN && quick_recent) {
            snprintf(row, sizeof(row), " %-9s %-23s %2u", quick_row_label(r),
                     quick_row_note(r), quick_recent->count);
        } else {
            snprintf(row, sizeof(row), " %-9s %s", quick_row_label(r), quick_row_note(r));
        }
        draw_selected_line(QUICK_FIRST_LINE + 2 * i, row, i == quick_selected);
    }
    draw_line_rgb(LOG_ROWS - 1, "the tile shows Claude's mode", HINT_DIM);
}

static void draw_quick_list(void)
{
    const int commands = quick_level == SUBSCREEN_QUICK_COMMANDS;
    draw_line(0, commands ? "COMMANDS   A compose   X back"
                          : "AGAIN   A compose   Y forget   X back", PAL_ORANGE);
    draw_line_rgb(1, commands ? "built in, then this computer's own"
                              : "what you sent lately, newest first", HINT_DIM);
    const unsigned count = commands ? (quick_cmds ? quick_cmds->count : 0)
                                    : (quick_recent ? quick_recent->count : 0);
    for (int line = QUICK_LIST_FIRST_LINE; line < LOG_ROWS; line++) {
        const int index = quick_top + (line - QUICK_LIST_FIRST_LINE);
        if (line - QUICK_LIST_FIRST_LINE >= SUBSCREEN_QUICK_LIST_ROWS ||
            index < 0 || (unsigned)index >= count) {
            draw_line(line, "", PAL_DEFAULT);
            continue;
        }
        char row[LOG_COLS + 1];
        if (commands) {
            snprintf(row, sizeof(row), " %s", quick_cmds->names[index]);
        } else {
            char text[LOG_COLS];
            quick_recent_row(quick_recent->items[index], text, LOG_COLS - 2);
            snprintf(row, sizeof(row), " %s", text);
        }
        draw_selected_line(line, row, index == quick_selected);
    }
    if (!commands && count == 0) {
        draw_line(QUICK_LIST_FIRST_LINE + 1, " Nothing yet: prompts you send", PAL_DEFAULT);
        draw_line(QUICK_LIST_FIRST_LINE + 2, " from here land in this list.", PAL_DEFAULT);
    }
}

int subscreen_quick_row_at(int level, int top, int py)
{
    const int line = py / 8;
    if (level == SUBSCREEN_QUICK_MAIN) {
        const int i = (line - QUICK_FIRST_LINE + 1) / 2;
        if (line < QUICK_FIRST_LINE - 1 || i < 0 ||
            i >= (int)quick_row_count(quick_camera)) return -1;
        return i;
    }
    const int row = line - QUICK_LIST_FIRST_LINE;
    if (row < 0 || row >= SUBSCREEN_QUICK_LIST_ROWS) return -1;
    return top + row;
}

// REMOTE: a title, the server's last word on a pick, then the rows.  The
// explanation of the selected row is on the top screen (main.c), where there
// is room to say what A will do before it is pressed.
#define REMOTE_FIRST_ROW 2

int subscreen_remote_row_at(int py)
{
    const int row = py / 8 - REMOTE_FIRST_ROW;
    return row >= 0 && row < LOG_ROWS - REMOTE_FIRST_ROW ? row : -1;
}

static void draw_remote_menu(void)
{
    draw_line(0, "REMOTE   UP/DOWN choose   A open   X back", PAL_ORANGE);
    draw_line(1, remote_list && remote_list->note[0] ? remote_list->note : "",
              PAL_YELLOW);
    remote_row_t rows[REMOTE_ROWS_MAX];
    const unsigned n = remote_rows(remote_list, remote_comps, rows,
                                   REMOTE_ROWS_MAX);
    const int selected = remote_ui_shown ? remote_ui_shown->selection : -1;
    int line = REMOTE_FIRST_ROW;
    for (unsigned i = 0; i < n && line < LOG_ROWS; i++, line++) {
        char text[LOG_COLS + 1];
        remote_row_text(remote_list, remote_comps, &rows[i], remote_here, text,
                        LOG_COLS);
        if ((int)i == selected) {
            draw_selected_line(line, text, 1);
        } else if (rows[i].kind == REMOTE_ROW_HEADER) {
            draw_line(line, text, PAL_ORANGE);
        } else if (rows[i].kind == REMOTE_ROW_SESSION && remote_list &&
                   remote_list->items[rows[i].index].state ==
                       REMOTE_STATE_NEEDS_YOU) {
            draw_line(line, text, PAL_YELLOW);
        } else {
            draw_line(line, text, PAL_DEFAULT);
        }
    }
    for (; line < LOG_ROWS; line++) draw_line(line, "", PAL_DEFAULT);
}

static void draw_control_screen(void)
{
    char link[LOG_COLS + 1], view[LOG_COLS + 1];
    status_format_link(link);
    status_format_view_pos(view);
    // Row 0: where the card is and how the link is.  Row 1: the reading
    // position, which is blank at the live edge and only speaks when you
    // have scrolled away.  Row 2: what Claude is doing, in plain words --
    // yellow when it is waiting on you.  Row 3: what the buttons do right
    // now.  RELINK sits at the right of rows 3 and 4, clear of all of it.
    draw_line(0, link, PAL_WHITE);
    draw_line(1, view, PAL_DEFAULT);
    draw_line(2, chime_headline(), chime_needs_you() ? PAL_YELLOW : PAL_ORANGE);
    // At most 30 columns: RELINK starts at column 31.
    char confirm_hint[32];
    snprintf(confirm_hint, sizeof(confirm_hint), "d-pad picks  %s confirms",
             legend_enter[0] ? legend_enter : "A");
    char rewind_hint[48];
    snprintf(rewind_hint, sizeof(rewind_hint), "UNDO: d-pad picks  %s chooses",
             legend_enter[0] ? legend_enter : "A");
    const char *hint =
        focus_active() ? "d-pad moves  A opens  B out" :
        rewind_on ? rewind_hint :
        chime_needs_you() ? confirm_hint :
        "up/down scroll  hold down: now";
    draw_line_rgb(3, hint, HINT_DIM);
    draw_line(4, "", PAL_DEFAULT);
    (void)ext_subscreen_draw(EXT_DRAW_CONTROL_STRIP);

    // Drawn from kbd.c's table, never from coordinates written here: the touch
    // targets come from the same table, so the two cannot drift apart. Only
    // the labels live here, and a wrong label is a cosmetic bug rather than a
    // button that does something other than what it says.
    for (int id = KBD_CONTROL_KEYS; id <= KBD_CONTROL_COUNT; id++) {
        int x, y, w, h;
        kbd_control_rect((kbd_control_t)id, &x, &y, &w, &h);
        const char *label = "", *what = "";
        switch ((kbd_control_t)id) {
        case KBD_CONTROL_KEYS:     label = "KEYS";     what = "type live";   break;
        case KBD_CONTROL_QUICK: {
            static char mode_what[24];
            label = "QUICK";
            if (claude_mode[0]) {
                snprintf(mode_what, sizeof(mode_what), "mode: %s", claude_mode);
                what = mode_what;
            } else {
                what = "mode, undo";
            }
            break;
        }
        case KBD_CONTROL_PHOTO:
            // The camera in one tap; a card without one gets QUICK's AGAIN.
            label = quick_camera ? "PHOTO" : "AGAIN";
            what = quick_camera ? "show Claude" : "your prompts";
            break;
        case KBD_CONTROL_COMPOSE:  label = "COMPOSE";  what = "write first"; break;
        case KBD_CONTROL_REMOTE:   label = "REMOTE";   what = "elsewhere";   break;
        case KBD_CONTROL_SETTINGS: label = "SETTINGS"; what = "sound, model"; break;
        default: break;
        }
        if (w <= 0) continue;
        draw_tile2(x, y, w, h, label, what, 0,
                   focus_active() && focus_place() == FOCUS_CONTROL &&
                       id == focus_control_id());
    }

    // Recovery is intentionally smaller than the destinations above.  It is
    // touch-only so the six primary tiles remain a clean focus rectangle, but
    // an explicit relink is always one deliberate tap away when Wi-Fi stalls.
    int rx, ry, rw, rh;
    kbd_control_rect(KBD_CONTROL_RECONNECT, &rx, &ry, &rw, &rh);
    draw_tile(rx, ry, rw, rh, "RELINK", 0, 0);
    // An extension's own small tile beside it, when there is one.
    kbd_control_rect(KBD_CONTROL_EXTENSION, &rx, &ry, &rw, &rh);
    if (rw > 0 && ext_control_label()) {
        draw_tile(rx, ry, rw, rh, ext_control_label(), 0, 0);
    }

    // The strip under the tiles.  An extension stands there when one is
    // attached; without one it is the place to learn the buttons, which is
    // the one thing everybody needs and nobody should have to look up.
    if (ext_control_label()) {
        for (int row = 20; row <= 23; row++) draw_line(row, "", PAL_DEFAULT);
    } else {
        draw_line(20, "", PAL_DEFAULT);
        // From the BUTTONS tab, so the legend never names a button that
        // does something else now.
        char second[LOG_COLS + 1];
        int at = 0;
        second[0] = '\0';
        if (legend_stop[0]) {
            at += snprintf(second + at, sizeof(second) - (size_t)at, "%s stop  ",
                           legend_stop);
        }
        if (legend_tab[0] && at < (int)sizeof(second)) {
            at += snprintf(second + at, sizeof(second) - (size_t)at, "R+%s mode  ",
                           legend_tab);
        }
        if (at < (int)sizeof(second)) {
            snprintf(second + at, sizeof(second) - (size_t)at, "SELECT screens");
        }
        draw_line_rgb(21, legend_buttons, HINT_DIM);
        draw_line_rgb(22, second, HINT_DIM);
        draw_line_rgb(23, "L ctrl  R shift  L+R pick a tile", HINT_DIM);
    }
}

void subscreen_set_composer(int active) { composer_active = active ? 1 : 0; }

static int boot_active;
static int boot_keyboard;
static const char *boot_body;

void subscreen_set_boot_body(const char *body)
{
    boot_body = body;
}

// Lines of `text` from `top`, each its own row, the rest of the screen blank.
static void draw_text_block(int top, const char *text, uint8_t fg)
{
    int row = top;
    const char *line = text;
    while (row < LOG_ROWS && line && *line) {
        const char *end = strchr(line, '\n');
        const size_t len = end ? (size_t)(end - line) : strlen(line);
        char buf[LOG_COLS + 1];
        snprintf(buf, sizeof(buf), "%.*s", (int)(len > LOG_COLS ? LOG_COLS : len),
                 line);
        draw_line(row++, buf, fg);
        line = end ? end + 1 : NULL;
    }
    for (; row < LOG_ROWS; row++) draw_line(row, "", fg);
}
static char boot_head[2][LOG_COLS + 1];

void subscreen_set_boot(int active, int keyboard, const char *line0,
                        const char *line1)
{
    boot_active = active ? 1 : 0;
    boot_keyboard = keyboard ? 1 : 0;
    snprintf(boot_head[0], sizeof(boot_head[0]), "%s", line0 ? line0 : "");
    snprintf(boot_head[1], sizeof(boot_head[1]), "%s", line1 ? line1 : "");
}
void subscreen_set_remote(int active, const remote_list_t *list,
                          const remote_computers_t *computers,
                          const remote_ui_t *ui, const char *here)
{
    remote_active = active ? 1 : 0;
    remote_list = list;
    remote_comps = computers;
    remote_ui_shown = ui;
    remote_here = here;
}

void subscreen_set_quick(int active, int level, int selected, int top,
                         const quick_commands_t *commands,
                         const quick_recent_t *recent)
{
    quick_active = active ? 1 : 0;
    quick_level = level;
    quick_selected = selected;
    quick_top = top;
    quick_cmds = commands;
    quick_recent = recent;
}

void subscreen_set_rewind(int picking) { rewind_on = picking ? 1 : 0; }

void subscreen_set_camera_available(int available) { quick_camera = available != 0; }

void subscreen_set_camera(int active, const char *top, const char *hint,
                          int error)
{
    camera_active = active != 0;
    camera_error = error != 0;
    snprintf(camera_top, sizeof(camera_top), "%s", top ? top : "");
    snprintf(camera_hint, sizeof(camera_hint), "%s", hint ? hint : "");
}

// Rows CAMERA_VIEW_Y0..CAMERA_VIEW_Y1 only: the strips above and below are
// the layout's own.  A frame is one NDMA-shaped block, so one dmaCopy.
void subscreen_camera_blit(const uint16_t *frame)
{
    if (!frame) return;
    mirror_flag_rows(&sub_mirror, CAMERA_VIEW_Y0, CAMERA_VIEW_Y1,
                     MIRROR_FLAG_CAMERA);
    dmaCopy(frame + CAMERA_VIEW_Y0 * SUB_W, sub + CAMERA_VIEW_Y0 * SUB_W,
            (CAMERA_VIEW_Y1 - CAMERA_VIEW_Y0 + 1) * SUB_W * sizeof(*frame));
}

void subscreen_set_claude_mode(const char *mode)
{
    snprintf(claude_mode, sizeof(claude_mode), "%s", mode ? mode : "");
}
void subscreen_set_project_actions(const project_action_t *actions,
                                   unsigned count, unsigned selected,
                                   unsigned dropped, int god_mode)
{
    if (count > PROJECT_ACTIONS_MAX) count = PROJECT_ACTIONS_MAX;
    for (unsigned i = 0; i < count; i++) project_action_list[i] = actions[i];
    project_action_count = count;
    project_action_highlight = selected < count ? selected : 0u;
    project_action_dropped = dropped;
    project_god_mode = god_mode ? 1 : 0;
}

void subscreen_set_clip(const char *summary, const char lines[][CLIP_LINE_COLS + 1],
                        unsigned count, int can_paste)
{
    (void)snprintf(clip_summary_line, sizeof(clip_summary_line), "%s",
                   summary ? summary : "");
    clip_line_count = count > SUBSCREEN_CLIP_LINES_MAX
                          ? SUBSCREEN_CLIP_LINES_MAX : count;
    for (unsigned i = 0; i < clip_line_count; i++) {
        (void)snprintf(clip_lines[i], sizeof(clip_lines[i]), "%s", lines[i]);
    }
    clip_can_paste = can_paste ? 1 : 0;
}

void subscreen_set_button_legend(const char *legend, const char *enter,
                                 const char *stop, const char *tab)
{
    snprintf(legend_buttons, sizeof(legend_buttons), "%s", legend ? legend : "");
    snprintf(legend_enter, sizeof(legend_enter), "%s", enter ? enter : "");
    snprintf(legend_stop, sizeof(legend_stop), "%s", stop ? stop : "");
    snprintf(legend_tab, sizeof(legend_tab), "%s", tab ? tab : "");
}

void subscreen_set_settings(const settings_t *settings,
                            const settings_ui_t *ui)
{
    if (settings) settings_shown = *settings;
    if (ui) settings_ui_shown = *ui;
    if (settings_ui_shown.row >= SETTINGS_ROW_COUNT) settings_ui_shown.row = 0u;
}

void subscreen_set_project(unsigned tab, const char lines[][44], unsigned count)
{
    if (count > SUBSCREEN_PROJECT_LINES_MAX) count = SUBSCREEN_PROJECT_LINES_MAX;
    for (unsigned i = 0; i < count; i++) {
        (void)snprintf(project_lines[i], sizeof(project_lines[i]), "%s",
                       lines[i]);
    }
    project_line_count = count;
    project_tab_sel = tab < PROJECT_TAB_COUNT ? tab : 0u;
}

void subscreen_init(void)
{
    videoSetModeSub(MODE_5_2D);
    vramSetBankC(VRAM_C_SUB_BG);
    sub_bg_id = bgInitSub(3, BgType_Bmp16, BgSize_B16_256x256, 0, 0);
    sub = (uint16_t *)bgGetGfxPtr(sub_bg_id);
    // All 256 rows, not just the visible 192: the impact shake scrolls this
    // background, and the rows past the bottom are uninitialised VRAM until
    // something clears them.  Doing it once here is what lets the shake stay
    // a two-line function.
    for (int i = 0; i < SUB_W * 256; i++) sub[i] = SUB_BG;
    mirror_clear(&sub_mirror, SUB_BG);

}

void subscreen_update(unsigned long rx, unsigned long msgs, unsigned long tx,
                      unsigned long loops, unsigned raw, unsigned lib)
{
    // Clear only when the layout changes, never on every repaint.
    //
    // Blanking 256x192 and redrawing into the bitmap the LCD is currently
    // showing is visible as a flash. Every draw path below already covers its
    // own area -- draw_line() pads to the full width, draw_tile() fills its
    // rect -- so the clear was only ever covering debris left when one layout
    // gives way to another: keyboard tiles still on screen under the log, and
    // so on. That is the only moment it is needed.
    //
    // If a layout leaves debris after this, that layout's draw path is
    // incomplete. Fix the path rather than going back to clearing every frame.
    const int layout = boot_active ? (boot_keyboard ? -5 : -4) :
                       composer_active ? -1 : camera_active ? -7 :
                       quick_active ? -2 :
                       remote_active ? -6 :
                       ext_menu_is_open() ? -3 : (int)status_mode_get();
    static int last_layout = -2;
    if (layout != last_layout) {
        for (int i = 0; i < SUB_W * 192; i++) sub[i] = SUB_BG;
        mirror_clear(&sub_mirror, SUB_BG);
        last_layout = layout;
    }

    char line1[LOG_COLS + 1], line2[LOG_COLS + 1];
    status_format_counters(rx, msgs, tx, loops, raw, lib, line1, line2);

    if (boot_active) {
        draw_line(0, boot_head[0], PAL_ORANGE);
        draw_line(1, boot_head[1], PAL_DEFAULT);
        if (boot_keyboard) {
            draw_keyboard();
            draw_keyboard_state(20);
            draw_line(21, "", PAL_DEFAULT);
        } else if (boot_body) {
            draw_text_block(2, boot_body, PAL_DEFAULT);
        } else {
            draw_log_region(2, LOG_ROWS - 1);
        }
    } else if (composer_active) {
        draw_line(0, "COMPOSE: nothing is sent until you press A", PAL_YELLOW);
        draw_line(1, "A sends   B deletes   X cancels", PAL_DEFAULT);
        draw_keyboard();
        draw_line(20, "ENT adds a new line", PAL_DEFAULT);
        draw_line(21, "Y: snippets (and clipboard, if on)", PAL_DEFAULT);
    } else if (camera_active) {
        // The strips only -- the rows between them are the viewfinder's and
        // belong to subscreen_camera_blit(), which is why a repaint here
        // never smears a frame.
        draw_line(0, camera_top, PAL_RED);
        draw_tile(CAMERA_FLIP_X, CAMERA_FLIP_Y, CAMERA_FLIP_W, CAMERA_FLIP_H,
                  "Y flip", 0, 0);
        draw_line(23, camera_hint, camera_error ? PAL_YELLOW : PAL_DEFAULT);
    } else if (quick_active) {
        if (quick_level == SUBSCREEN_QUICK_MAIN) draw_quick_main();
        else draw_quick_list();
    } else if (remote_active) {
        draw_remote_menu();
    } else if (ext_menu_is_open()) {
        (void)ext_subscreen_draw(EXT_DRAW_OVERLAY);
    } else if (status_mode_get() == SCREEN_MODE_SETTINGS) {
        draw_settings_screen();
    } else if (status_mode_get() == SCREEN_MODE_PROJECT) {
        draw_project_screen();
    } else if (status_mode_get() == SCREEN_MODE_STATUS) {
        draw_status_screen();
        draw_line(LOG_ROWS - 2, line1, PAL_WHITE);
        draw_line(LOG_ROWS - 1, line2, PAL_WHITE);
    } else if (status_mode_get() == SCREEN_MODE_KEYS) {
        // Row 0 is the counters, unless the extension has a line for it: this
        // is the screen you are looking at while you type, so it is the screen
        // most worth a word on, and the counters are the most expendable thing
        // up here.  Row 1 stays the keyboard's own state whatever happens --
        // which layer you are on and whether Ctrl is armed both change what
        // the next key does, and that is not negotiable for a readout.
        if (!ext_subscreen_draw(EXT_DRAW_KEYS_STATUS)) {
            // While Claude waits on you, that is the line worth having here
            // too: you may be typing when the approval appears.
            if (chime_needs_you()) {
                draw_line(0, chime_headline(), PAL_YELLOW);
            } else {
                char link[LOG_COLS + 1];
                status_format_link(link);
                draw_line(0, link, PAL_WHITE);
            }
        }
        draw_keyboard_state(1);
        draw_keyboard();
        clear_keyboard_floor();
        // Without an extension standing there, the floor says what the
        // keyboard's own special keys do -- the two that are not obvious.
        if (!ext_control_label()) {
            draw_line_rgb(KBD_FLOOR_ROW, "Every key goes straight to Claude.",
                          HINT_DIM);
            draw_line_rgb(KBD_FLOOR_ROW + 1, "SHFT: abc ABC !@#   X: back",
                          HINT_DIM);
        }
    } else {
        draw_control_screen();
    }
}
