#include "kbd.h"

#include "ext.h"

#include <string.h>

// Rows 0-3 are character keys: one ASCII byte per key, and the byte (and the
// label) come from the layer's row string. Row 4 is special keys; it is laid
// out by hand below. The sym layer keeps rows 1-4 at their base labels, only
// row 0 differs -- that is docs/design/INPUT_AND_KEYBOARD.md's "Sym layer ... replaces row
// 0 only".
static const char base_rows[4][11] = {
    "1234567890", "qwertyuiop", "asdfghjkl;", "zxcvbnm,./",
};
static const char shift_rows[4][11] = {
    "!@#$%^&*()", "QWERTYUIOP", "ASDFGHJKL:", "ZXCVBNM<>?",
};
static const char sym_rows[4][11] = {
    "`[]\\'|{}~\"", "qwertyuiop", "asdfghjkl;", "zxcvbnm,./",
};

typedef struct {
    kbd_action_t action[3];  // base, shift, sym
    const char *label[3];    // base, shift, sym display labels
} kbd_row4_t;

// Actions are per-layer because the TAB key becomes shift-tab in the shift
// layer ("\x1b[Z" instead of "\t"); every other row-4 key has the same action
// in all three layers.
static const kbd_row4_t row4[9] = {
    {{KBD_SHFT,  KBD_SHFT,  KBD_SHFT},  {"SHFT", "SHFT", "SHFT"}},
    {{KBD_CTRL,  KBD_CTRL,  KBD_CTRL},  {"CTRL", "CTRL", "CTRL"}},
    {{KBD_ESC,   KBD_ESC,   KBD_ESC},   {"ESC",  "ESC",  "ESC"}},
    // Shift-tab is shown as "S-TB": a 24px key fits four 6px glyphs and
    // "S-TAB" would overflow into its neighbours. The byte is the same
    // ("\x1b[Z") either way.
    {{KBD_TAB,   KBD_S_TAB, KBD_TAB},   {"TAB",  "S-TB", "TAB"}},
    {{KBD_SPACE, KBD_SPACE, KBD_SPACE}, {"SPC",  "SPC",  "SPC"}},
    {{KBD_CHAR,  KBD_CHAR,  KBD_CHAR},  {"-",    "_",    "-"}},
    {{KBD_CHAR,  KBD_CHAR,  KBD_CHAR},  {"=",    "+",    "="}},
    {{KBD_ENTER, KBD_ENTER, KBD_ENTER}, {"ENT",  "ENT",  "ENT"}},
    {{KBD_BKSP,  KBD_BKSP,  KBD_BKSP},  {"BKSP", "BKSP", "BKSP"}},
};

// Grid column of each row-4 key id. Column 5 is inside the space key, so ids
// 44 (space) and 45 (the '-' key) sit next to each other without an id for
// column 5.
static const int row4_col[9] = {0, 1, 2, 3, 4, 6, 7, 8, 9};

// Key id for each grid column of row 4; columns 4 and 5 are the space key.
static const int row4_id[10] = {40, 41, 42, 43, 44, 44, 45, 46, 47, 48};

static kbd_layer_t layer = KBD_LAYER_BASE;
static int ctrl_armed = 0;
static int shift_armed = 0;
static int held = -1;

void kbd_reset(void)
{
    layer = KBD_LAYER_BASE;
    ctrl_armed = 0;
    shift_armed = 0;
    held = -1;
}

int kbd_count(void)
{
    return 40 + 9;  // 40 char keys + 9 row-4 keys
}

// ------------------------------------------------------------------ layers --

kbd_layer_t kbd_layer(void) { return layer; }

void kbd_layer_cycle(void) { layer = (kbd_layer_t)((layer + 1) % 3); }

void kbd_layer_set(kbd_layer_t l) { layer = l; }

// -------------------------------------------------------------------- ctrl --

void kbd_ctrl_arm(void) { ctrl_armed = !ctrl_armed; }
int kbd_ctrl_armed(void) { return ctrl_armed; }

int kbd_ctrl_fold(int byte)
{
    ctrl_armed = 0;
    return byte & 0x1f;
}

void kbd_shift_arm(void) { shift_armed = !shift_armed; }
int kbd_shift_armed(void) { return shift_armed; }
void kbd_shift_consume(void) { shift_armed = 0; }

// ---------------------------------------------------------------- geometry --

static const char *row_string(int row, kbd_layer_t l)
{
    if (l == KBD_LAYER_SHIFT) return shift_rows[row];
    if (l == KBD_LAYER_SYM)   return sym_rows[row];
    return base_rows[row];
}

// A key's action in a specific layer: rows 0-3 are always KBD_CHAR, row 4 is
// per-layer (TAB becomes S-TAB in the shift layer).
static kbd_action_t kbd_action_in_layer(int id, kbd_layer_t l)
{
    if (id < 0 || id >= kbd_count()) return KBD_NONE;
    if (id < 40) return KBD_CHAR;
    return row4[id - 40].action[l];
}

kbd_action_t kbd_action(int id)
{
    return kbd_action_in_layer(id, layer);
}

void kbd_key_rect(int id, kbd_layer_t layer, int *x, int *y, int *w, int *h)
{
    // The layer parameter exists so the host test can prove the geometry is
    // identical across layers; all three currently share one rect table.
    (void)layer;
    if (id == 44) {  // space spans grid columns 4-5
        *x = KBD_ORIGIN_X + 4 * KBD_STEP_X;
        *y = KBD_ORIGIN_Y + 4 * KBD_STEP_Y;
        *w = 2 * KBD_STEP_X;   // 50 px, edge-to-edge with the '-' key
        *h = KBD_H;
        return;
    }
    const int row = id < 40 ? id / 10 : 4;
    const int col = id < 40 ? id % 10 : row4_col[id - 40];
    *x = KBD_ORIGIN_X + col * KBD_STEP_X;
    *y = KBD_ORIGIN_Y + row * KBD_STEP_Y;
    *w = KBD_W;
    *h = KBD_H;
}

int kbd_id_at_grid(int row, int col)
{
    if (row < 0 || row >= KBD_ROWS || col < 0 || col >= KBD_COLS) return -1;
    if (row < 4) return row * 10 + col;
    return row4_id[col];
}

int kbd_hit(int x, int y)
{
    if (x < KBD_ORIGIN_X || y < KBD_ORIGIN_Y) return -1;
    const int col = (x - KBD_ORIGIN_X) / KBD_STEP_X;
    const int row = (y - KBD_ORIGIN_Y) / KBD_STEP_Y;
    if (col < 0 || col >= KBD_COLS || row < 0 || row >= KBD_ROWS) return -1;
    if (row < 4) return row * 10 + col;
    return row4_id[col];
}

// ----------------------------------------------------------------- labels --

const char *kbd_label(int id, kbd_layer_t l)
{
    // A single static buffer is fine: callers use it immediately.
    static char buf[6];
    if (id < 0 || id >= kbd_count()) return "";
    if (id < 40) {
        buf[0] = row_string(id / 10, l)[id % 10];
        buf[1] = 0;
        return buf;
    }
    return row4[id - 40].label[l];
}

// ------------------------------------------------------------------ press --

int kbd_press(int id, uint8_t *out, int max)
{
    const kbd_action_t a = kbd_action_in_layer(id, layer);
    if (a == KBD_SHFT) { kbd_layer_cycle(); return -1; }
    if (a == KBD_CTRL) { kbd_ctrl_arm(); return -1; }
    if (max < 1) return 0;

    // A one-shot Shift ("R arms Shift for the next key") turns Tab into
    // Shift-Tab, the "S-TAB" touch key or the shift-layer byte for a character
    // key. Every byte-producing key consumes it; the two modifier touches and
    // the physical shoulders do not consume each other.
    if (a == KBD_TAB || a == KBD_S_TAB) {
        const int s_tab = (a == KBD_S_TAB) || shift_armed;
        if (s_tab) {
            if (max < 3) return 0;
            shift_armed = 0;
            out[0] = 0x1b; out[1] = '['; out[2] = 'Z';
            return 3;
        }
        out[0] = '\t';
        return 1;
    }

    int byte;
    switch (a) {
    case KBD_SPACE: byte = ' ';   break;
    case KBD_ENTER: byte = '\r';  break;
    case KBD_BKSP:  byte = 0x7f;  break;
    case KBD_ESC:   byte = 0x1b;  break;
    case KBD_CHAR:
        // A shifted character key sends its shift-layer byte regardless of the
        // touch layer; otherwise the current layer's byte.
        if (shift_armed) { byte = (uint8_t)kbd_label(id, KBD_LAYER_SHIFT)[0]; shift_armed = 0; }
        else             byte = (uint8_t)kbd_label(id, layer)[0];
        break;
    default:        return 0;  // KBD_NONE
    }

    // Character keys (letters, digits, symbols, space) fold through a one-shot
    // Ctrl and disarm it. Enter/Escape/Backspace leave it armed: the doc says
    // "the next character key sends byte & 0x1f and disarms".
    if ((a == KBD_CHAR || a == KBD_SPACE) && ctrl_armed) {
        byte = kbd_ctrl_fold(byte);
    }
    // A one-shot Shift is consumed by any byte-producing key ("for the next
    // key"); character keys already consumed it in the switch above.
    shift_armed = 0;
    out[0] = (uint8_t)byte;
    return 1;
}

int kbd_repeatable(int id)
{
    const kbd_action_t a = kbd_action(id);
    return a == KBD_CHAR || a == KBD_SPACE || a == KBD_BKSP;
}

void kbd_set_held(int id) { held = id; }
int kbd_held(void) { return held; }

static int point_in_rect(int px, int py, int x, int y, int w, int h)
{
    return px >= x && px < x + w && py >= y && py < y + h;
}

// Indexed by kbd_control_t, so entry 0 is the NONE placeholder. A 3x2 grid
// below the two readout lines, well clear of the hint rows at the bottom.
// The primary row is the keyboard first -- a design call: typing is live --
// followed by the two other high-frequency screens. The composer stays below
// it behind an explicit press.
static const struct { int x, y, w, h; } control_rects[] = {
    {  0,   0,   0,  0},   // KBD_CONTROL_NONE
    {  6,  40,  76, 52},   // KEYS     (primary typing surface, types live)
    { 90,  40,  76, 52},   // SESSIONS
    {174,  40,  76, 52},   // LIVE
    {  6, 100,  76, 52},   // COMPOSE  (demoted: explicit press only)
    { 90, 100,  76, 52},   // REMOTE   (other sessions, other computers)
    {174, 100,  76, 52},   // SETTINGS (the slot snippets gave up)
    // Small on purpose: this is recovery, not a seventh destination.  It
    // lives in the right-hand end of the two status rows above the grid --
    // rows 3 and 4, whose text stops well short of it.  NOT row 2: that row
    // is the extension's, and with a readout on it carries a whole sentence
    // that this was sitting on the end of.  Moving down four pixels also
    // makes it two pixels taller, which a recovery button can only benefit
    // from; it still ends exactly where the tile grid begins.
    {191,  24,  59, 16},   // RELINK (touch-only utility)
    // An extension's own tile, the same size, just left of RELINK.
    {128,  24,  59, 16},   // EXTENSION (see kbd_control_rect)
};

void kbd_control_rect(kbd_control_t control, int *x, int *y, int *w, int *h)
{
    if (control < KBD_CONTROL_KEYS || control > KBD_CONTROL_EXTENSION ||
        (control == KBD_CONTROL_EXTENSION && !ext_control_label())) {
        *x = *y = *w = *h = 0;
        return;
    }
    *x = control_rects[control].x;
    *y = control_rects[control].y;
    *w = control_rects[control].w;
    *h = control_rects[control].h;
}

kbd_control_t kbd_control_hit(int x, int y)
{
    int rx, ry, rw, rh;
    kbd_control_rect(KBD_CONTROL_RECONNECT, &rx, &ry, &rw, &rh);
    if (point_in_rect(x, y, rx, ry, rw, rh)) return KBD_CONTROL_RECONNECT;
    kbd_control_rect(KBD_CONTROL_EXTENSION, &rx, &ry, &rw, &rh);
    if (point_in_rect(x, y, rx, ry, rw, rh)) return KBD_CONTROL_EXTENSION;
    for (int id = KBD_CONTROL_KEYS; id <= KBD_CONTROL_COUNT; id++) {
        kbd_control_rect((kbd_control_t)id, &rx, &ry, &rw, &rh);
        if (point_in_rect(x, y, rx, ry, rw, rh)) return (kbd_control_t)id;
    }
    return KBD_CONTROL_NONE;
}

unsigned kbd_held_keyword(unsigned reg_word, unsigned lib_word)
{
    // X and Y live in REG_KEYXY, which only the ARM7 can read, so they
    // arrive through libnds at bits 10 and 11 -- positions disjoint from the
    // ten register bits, so the combine is an OR. Both sides are masked:
    // libnds bits other than X/Y (touch, lid) must never reach a consumer as
    // phantom keys, and a high register bit must never be mistaken for X or Y.
    return (reg_word & KBD_REG_MASK) | (lib_word & (KBD_KEY_X | KBD_KEY_Y));
}

unsigned kbd_edge_keyword(unsigned held, unsigned prev_held)
{
    return held & ~prev_held;
}

int kbd_repeat_should_fire(unsigned held_frames)
{
    if (held_frames < KBD_REPEAT_DELAY) return 0;
    return ((held_frames - KBD_REPEAT_DELAY) % KBD_REPEAT_INTERVAL) == 0;
}
