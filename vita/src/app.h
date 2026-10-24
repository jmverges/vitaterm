// The PS Vita client: one 960x544 screen, the Vita's own keyboard, buttons,
// touch and camera, on the same wire as the DS card (dsclient/source).
//
// The DS's own UI (two screens, the touch keyboard, the boot menus) is not
// used here.  Everything that is the protocol (framing, the seal, pairing,
// discovery, profiles, the session lists, the chimes) is the DS client's own
// code, compiled unchanged.
#ifndef DSTERM_VITA_APP_H
#define DSTERM_VITA_APP_H

#include <stddef.h>
#include <stdint.h>

#include <vita2d.h>

#include "config.h"
#include "proto.h"
#include "quick.h"
#include "remote.h"
#include "snippets.h"

#define SCREEN_W 960
#define SCREEN_H 544

// The terminal: 8x16 cells under a 32 px status bar.
#define CELL_W 8
#define CELL_H 16
#define BAR_H 32
#define TERM_W (PROTO_COLS * CELL_W)
#define TERM_H (PROTO_ROWS * CELL_H)

#define DATA_ROOT "ux0:data/dsterm"

// Colours, RGBA8 as vita2d takes them (0xAABBGGRR).
#define COL_BG       0xFF000000u
#define COL_PANEL    0xF2201A16u
#define COL_PANEL_HI 0xFF4A3A30u
#define COL_TEXT     0xFFE6E6E6u
#define COL_DIM      0xFF8C8C8Cu
#define COL_ACCENT   0xFF5A8CE0u   // Claude's orange
#define COL_GOOD     0xFF6CC86Cu
#define COL_BAD      0xFF5050E0u
#define COL_WARN     0xFF40C8E8u

// ------------------------------------------------------------------ input ---

typedef struct {
    unsigned held;
    unsigned down;      // pressed this frame
    unsigned repeat;    // pressed, or held long enough to repeat
    int lx, ly, rx, ry; // sticks, -128..127, dead zone removed
    int touching;       // front panel
    int tx, ty;         // in screen pixels
    int touch_down;     // started this frame
    int touch_up;       // ended this frame
    int start_x, start_y;
} input_t;

extern input_t in;
void input_init(void);
void input_poll(void);
// The system keyboard over the current screen.  Returns 1 with `out` filled
// (folded to the ASCII the wire carries), 0 if cancelled.
int input_text(const char *title, const char *initial, char *out, size_t cap,
               int numeric);

// ---------------------------------------------------------------- drawing ---

extern vita2d_font *font;
void gfx_init(void);
void gfx_begin(void);
void gfx_end(void);         // swaps on the vertical blank
void gfx_text(int x, int y, unsigned colour, float scale, const char *text);
int gfx_text_width(float scale, const char *text);
void gfx_textf(int x, int y, unsigned colour, float scale, const char *fmt, ...)
    __attribute__((format(printf, 5, 6)));
void gfx_rect(int x, int y, int w, int h, unsigned colour);
void gfx_panel(int x, int y, int w, int h);
// Word-wrapped text; returns the height used.
int gfx_wrap(int x, int y, int w, unsigned colour, float scale, const char *text);
// A message box over whatever the screen showed, for the blocking steps.
void gfx_working(const char *title, const char *body);
// Button hints along the bottom: pairs of glyph name and label.
void gfx_hints(const char *const *pairs, int n);

// --------------------------------------------------------------- terminal ---

void term_init(void);
void term_cell(int row, int col, cell_t c);
void term_all(const grid_t *g);
void term_cursor(int row, int col, int visible);
void term_draw(void);

// ------------------------------------------------------------------- link ---

typedef enum {
    LINK_DOWN = 0,
    LINK_CONNECTING,
    LINK_UP,
} link_state_t;

typedef struct {
    link_state_t state;
    char server_name[24];
    unsigned rtt_ms;
    int rtt_known;
    unsigned claude_state;      // PROTO_STATE_*
    char claude_detail[PROTO_STATE_DETAIL_MAX + 1];
    unsigned lines_back, lines_available;
    int app_scrolls, app_scrolls_known;
    int camera_ready, camera_can_save;
    char status[96];            // the last thing worth telling
    unsigned long status_at;
    char refusal[96];
    unsigned attempt;
} link_info_t;

extern link_info_t conn;
extern dsterm_config_t cfg;
extern grid_t grid;

void link_start(const char *name);
void link_stop(void);
void link_pump(void);           // once a frame: read, ping, reconnect
int link_send_frame(const uint8_t *frame, size_t n);
int link_key(const char *seq, size_t n);
int link_text(const char *text, size_t n);
void link_scroll(int lines);    // + forward, - back
void link_live(void);
void link_status(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

extern remote_list_t remote;
extern quick_commands_t commands;
extern snippets_t snippets;
void link_remote_open(int open);
void link_remote_pick(unsigned index, int action);
// A saved photo's id while its caption is being written (0: none).
extern unsigned photo_pending_id;
void link_photo_discard(void);

// ----------------------------------------------------------------- camera ---

void cam_run(void);
void cam_on_ack(uint8_t kind, uint32_t value);
void cam_on_saved(const photo_saved_t *saved);
void cam_forget(void);

// ------------------------------------------------------------------ setup ---

// The start screen: pick a saved computer, find one, or pair.  Returns 1
// with `cfg` ready to connect and `name` filled.
int setup_run(char *name, size_t cap);
void setup_entropy_u32(uint32_t v);
void setup_random(uint8_t *out, size_t n);

// ------------------------------------------------------------------- menu ---

// Returns 1 if the person asked to go back to the start screen.
int menu_run(void);

// ------------------------------------------------------------------ audio ---

void audio_init(void);
void audio_tick(void);

// ------------------------------------------------------------------- misc ---

unsigned long frame_count(void);
unsigned long now_us(void);
// One frame of a blocking step: input, the link, sound.  The caller draws.
void app_tick(void);
// Draws the terminal screen (status bar and grid); the caller may add an
// overlay before gfx_end().
void app_draw_terminal(void);

#endif
