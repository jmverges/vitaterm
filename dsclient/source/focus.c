#include "focus.h"

// The Control screen's tiles as a row-major grid, derived from kbd.c's rect
// table on first use rather than declared: tiles sharing a top edge are one
// row, ordered left to right, and rows are ordered top to bottom. One source
// of truth -- a tile moved in kbd.c moves in focus automatically.
#define FOCUS_MAX_ROWS KBD_CONTROL_COUNT
#define FOCUS_MAX_COLS KBD_CONTROL_COUNT

typedef struct {
    int y0;                          // row's top edge, from the rect table
    int count;
    int ids[FOCUS_MAX_COLS];
} focus_row_t;

static focus_row_t ctrl_rows[FOCUS_MAX_ROWS];
static int ctrl_row_count;
static int ctrl_built;

static int active;
static focus_place_t place;

// Cursor as a grid position. For the keyboard that is the 10x5 key grid
// (row 4 has the special keys, with the space key owning columns 4-5); for
// the Control screen it is (row, col) into ctrl_rows.
static int row, col;

static void build_control_grid(void)
{
    if (ctrl_built) return;

    // A rebuild starts from a clean slate. This is the regression: the row
    // table is static state, focus_init() cleared only the "built" flag, and
    // a second build appended the controls to the rows left by the first --
    // counts grew past the arrays (an out-of-bounds write on the card) and
    // every control appeared twice. The grid is derived from a fixed rect
    // table, so a rebuild is deterministic and must not accumulate.
    ctrl_row_count = 0;
    for (int i = 0; i < FOCUS_MAX_ROWS; i++) {
        ctrl_rows[i].y0 = 0;
        ctrl_rows[i].count = 0;
    }

    for (int id = KBD_CONTROL_KEYS; id <= KBD_CONTROL_COUNT; id++) {
        int x, y, w, h;
        kbd_control_rect((kbd_control_t)id, &x, &y, &w, &h);
        if (w <= 0 || h <= 0) continue;   // fail closed on a bad table

        int r = -1;
        for (int i = 0; i < ctrl_row_count; i++) {
            if (ctrl_rows[i].y0 == y) { r = i; break; }
        }
        if (r < 0) {
            // The fixed control set cannot make more rows or a row wider than
            // KBD_CONTROL_COUNT, and these bounds derive from the same constant as
            // the arrays; the guards are tripwires for a corrupt rect table,
            // not paths that can legitimately run. A guard that fires leaves
            // the build incomplete and the grid empty -- focus_control_id()
            // then reports NONE, so navigation fails closed rather than
            // writing past the table.
            if (ctrl_row_count >= FOCUS_MAX_ROWS) return;
            r = ctrl_row_count++;
            ctrl_rows[r].y0 = y;
            ctrl_rows[r].count = 0;
        }
        if (ctrl_rows[r].count >= FOCUS_MAX_COLS) return;
        int n = ctrl_rows[r].count;
        ctrl_rows[r].ids[n] = id;
        // Bubble left to keep the row ordered by x.
        while (n > 0) {
            int px, py, pw, ph;
            kbd_control_rect((kbd_control_t)ctrl_rows[r].ids[n - 1],
                             &px, &py, &pw, &ph);
            if (px <= x) break;
            ctrl_rows[r].ids[n] = ctrl_rows[r].ids[n - 1];
            ctrl_rows[r].ids[n - 1] = id;
            n--;
        }
        ctrl_rows[r].count++;
    }

    // Rows appear in first-seen (enum) order; put them in visual order.
    for (int i = 0; i < ctrl_row_count - 1; i++) {
        for (int j = i + 1; j < ctrl_row_count; j++) {
            if (ctrl_rows[j].y0 < ctrl_rows[i].y0) {
                const focus_row_t t = ctrl_rows[i];
                ctrl_rows[i] = ctrl_rows[j];
                ctrl_rows[j] = t;
            }
        }
    }
    ctrl_built = 1;
}

void focus_init(void)
{
    active = 0;
    place = FOCUS_OFF;
    row = col = 0;
    ctrl_built = 0;
}

void focus_on(focus_place_t p)
{
    active = 1;
    place = p;
    row = 0;
    col = 0;
    if (place == FOCUS_CONTROL) {
        build_control_grid();
        // Home is the KEYS tile: the primary action is typing, so the cursor
        // lands on the way to it.
    } else if (place == FOCUS_KEYS) {
        row = 1;                       // home is 'a', where words start
    }
}

void focus_off(void)
{
    active = 0;
}

int focus_active(void) { return active; }
focus_place_t focus_place(void) { return place; }

void focus_move(int dx, int dy)
{
    if (!active) return;

    if (place == FOCUS_KEYS) {
        if (dy) {
            const int nr = row + dy;
            if (nr < 0 || nr >= KBD_ROWS) return;   // vertical edges stop
            row = nr;
            return;
        }
        // Horizontal wraps within the row. Row 4's space key owns columns
        // 4-5, so a step onto the duplicate cell keeps going to the next
        // distinct key instead of appearing not to move.
        const int from = kbd_id_at_grid(row, col);
        for (int i = 0; i < KBD_COLS; i++) {
            col = (col + dx + KBD_COLS) % KBD_COLS;
            if (kbd_id_at_grid(row, col) != from) return;
        }
        return;
    }

    // FOCUS_CONTROL: the tile grid. Rows stop at their edges; moving down
    // clamps the column to the target row's width so a narrow row does not
    // strand the cursor in empty space.
    if (dx) {
        const int nc = col + dx;
        if (nc < 0 || nc >= ctrl_rows[row].count) return;
        col = nc;
        return;
    }
    const int nr = row + dy;
    if (nr < 0 || nr >= ctrl_row_count) return;
    if (col >= ctrl_rows[nr].count) col = ctrl_rows[nr].count - 1;
    row = nr;
}

int focus_at_top(void)
{
    return active && row == 0;
}

int focus_control_id(void)
{
    if (!active || place != FOCUS_CONTROL) return KBD_CONTROL_NONE;
    build_control_grid();
    if (row < 0 || row >= ctrl_row_count) return KBD_CONTROL_NONE;
    if (col < 0 || col >= ctrl_rows[row].count) return KBD_CONTROL_NONE;
    return ctrl_rows[row].ids[col];
}

int focus_key_id(void)
{
    if (!active || place != FOCUS_KEYS) return -1;
    return kbd_id_at_grid(row, col);
}

// The three focused typing shortcuts (B is space, Y is backspace, R is
// Enter) are the same question -- "is the highlight on a key?" -- asked for
// three buttons.  One helper keeps them from drifting apart: if the typing
// surface ever changes, all three follow.
static int on_keys(void)
{
    return active && place == FOCUS_KEYS;
}

int focus_b_is_space(void)
{
    return on_keys();
}

int focus_y_is_backspace(void)
{
    return on_keys();
}

int focus_r_is_enter(void)
{
    return on_keys();
}
