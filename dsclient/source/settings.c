#include "settings.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "privacy.h"

static void fail(char *err, size_t err_len, const char *message)
{
    if (err && err_len) (void)snprintf(err, err_len, "%s", message);
}

static int ascii_equal(const char *a, const char *b)
{
    for (; *a && *b; a++, b++) {
        const char ca = (*a >= 'A' && *a <= 'Z') ? (char)(*a + 32) : *a;
        const char cb = (*b >= 'A' && *b <= 'Z') ? (char)(*b + 32) : *b;
        if (ca != cb) return 0;
    }
    return *a == '\0' && *b == '\0';
}

// ------------------------------------------------------------- the tables ---
//
// One table per row, and every other question about a row -- how many chips it
// has, how wide they are, where a touch lands, what it types -- is answered
// from it. That is the whole reason the geometry below is computed rather than
// written down: a chip's rect comes from its own label, so adding an option
// cannot leave a button that is not where it looks.

typedef struct {
    const char *label;      // on the chip
    const char *sends;      // typed into the session, or NULL
    const char *file_word;  // written to the card, or NULL if the row is an
                            // action and has nothing to persist
} option_t;

// Aliases, not identifiers: see the enum's comment in the header. UNSET is
// first so it is what a zeroed struct means, and it has no chip -- you cannot
// pick "I have not asked for anything", you can only not have asked yet.
static const option_t MODEL_OPTIONS[] = {
    { "OPUS",   "/model opus",   "opus"   },
    { "SONNET", "/model sonnet", "sonnet" },
    { "HAIKU",  "/model haiku",  "haiku"  },
};

// The three session commands worth a button. Not a general command launcher:
// each of these is a fixed literal, and the list is short because the bar is
// "would you rather not type this on glass keys", not "is this a command".
static const option_t SESSION_OPTIONS[] = {
    { "COMPACT", "/compact", NULL },
    { "COST",    "/cost",    NULL },
    { "STATUS",  "/status",  NULL },
};

static const option_t BOOT_OPTIONS[] = {
    { "CTRL", NULL, "control" },
    { "KEYS", NULL, "keys"    },
    { "PROJ", NULL, "project" },
};

static const option_t SOUND_OPTIONS[] = {
    // Shown as SOFT; written as "snes", the word cards already have.
    { "SOFT", NULL, "snes" },
    { "CHIP", NULL, "chip" },
    { "OFF",  NULL, "off"  },
};

static const option_t VOLUME_OPTIONS[] = {
    { "LOW",  NULL, "low"  },
    { "MID",  NULL, "mid"  },
    { "HIGH", NULL, "high" },
};

// How much soft typing sound while Claude writes.
static const option_t BLIPS_OPTIONS[] = {
    { "OFF",  NULL, "off"  },
    { "LOW",  NULL, "low"  },
    { "MID",  NULL, "mid"  },
    { "HIGH", NULL, "high" },
};

// A button's action: the order and words of buttons.h.
static const option_t BUTTON_OPTIONS[] = {
    { "ENT",  NULL, "enter"  },
    { "SPC",  NULL, "space"  },
    { "DEL",  NULL, "delete" },
    { "ESC",  NULL, "esc"    },
    { "TAB",  NULL, "tab"    },
    { "STOP", NULL, "stop"   },
};

// The fixed buttons: one chip each, saying what the button does.  Pressing
// it does nothing; the note says why.
static const option_t FIXED_SELECT_OPTIONS[] = { { "SCREENS", NULL, NULL } };
static const option_t FIXED_DPAD_OPTIONS[]   = { { "ARROWS",  NULL, NULL } };
static const option_t FIXED_L_OPTIONS[]      = { { "CTRL",    NULL, NULL } };
static const option_t FIXED_R_OPTIONS[]      = { { "SHIFT",   NULL, NULL } };

// The camera screen's shutter, in buttons.h's order.  File words are the
// lowercase key names.
static const option_t SHUTTER_OPTIONS[] = {
    { "R", NULL, "r" },
    { "L", NULL, "l" },
    { "A", NULL, "a" },
};

static const option_t TYPING_OPTIONS[] = {
    { "ON",  NULL, "on"  },
    { "OFF", NULL, "off" },
};

// Claude's own clay first: it is the default and the colour the rest of the
// tables were drawn around.
static const option_t THEME_OPTIONS[] = {
    { "CLAY", NULL, "clay" },
    { "SEA",  NULL, "sea"  },
    { "MOSS", NULL, "moss" },
    { "MONO", NULL, "mono" },
};

static const option_t AUTOCONNECT_OPTIONS[] = {
    { "ON",  NULL, "on"  },
    { "OFF", NULL, "off" },
};

static const option_t ADDRESSES_OPTIONS[] = {
    { "SHOW", NULL, "show" },
    { "HIDE", NULL, "hide" },
};

static const option_t VIEW_TAPS_OPTIONS[] = {
    { "OFF", NULL, "off" },
    { "ON",  NULL, "on"  },
};

// The still's wire size, in settings_picture_t order.
static const option_t PICTURE_OPTIONS[] = {
    { "FULL",  NULL, "full"  },
    { "SMALL", NULL, "small" },
};

// The computer's live view, in settings_liveview_t order.
static const option_t LIVEVIEW_OPTIONS[] = {
    { "GREY",   NULL, "grey"   },
    { "COLOUR", NULL, "colour" },
};

static const option_t GOD_OPTIONS[] = {
    { "OFF",   NULL, "off" },
    { "ARMED", NULL, "on"  },
};

// One chip whose label changes once it is armed, rather than two chips where
// one of them is never the one you want. The confirm lives in settings_ui_t,
// so it cannot be saved and cannot survive leaving the page.
static const option_t DEFAULTS_OPTIONS[] = {
    { "RESET", NULL, NULL },
};

static const struct {
    const char *label;
    const char *note;
    const option_t *options;
    unsigned count;
    unsigned tab;
    int option_x;      // where the chips start; 0 for SETTINGS_OPTION_X
} ROWS[SETTINGS_ROW_COUNT] = {
    { "MODEL", "types it into the session, right now",
      MODEL_OPTIONS, 3, SETTINGS_TAB_SESSION, 0 },
    { "SESSION", "the three worth not typing by hand",
      SESSION_OPTIONS, 3, SETTINGS_TAB_SESSION, 0 },
    // Said plainly. This is the one row that turns a safety rule off, and the
    // person turning it off should know what it was for.
    { "GOD MODE", "PROJECT actions fire without a read",
      GOD_OPTIONS, 2, SETTINGS_TAB_SESSION, 0 },
    { "SOUND", "soft desk sounds, square wave, or none",
      SOUND_OPTIONS, 3, SETTINGS_TAB_CUSTOMIZE, 0 },
    { "VOLUME", "for the room you are in; plays a sample",
      VOLUME_OPTIONS, 3, SETTINGS_TAB_CUSTOMIZE, 0 },
    { "TYPING", "your own key clicks; alerts stay on",
      TYPING_OPTIONS, 2, SETTINGS_TAB_CUSTOMIZE, 0 },
    { "WRITING", "soft typing while Claude writes; how much",
      BLIPS_OPTIONS, 4, SETTINGS_TAB_CUSTOMIZE, 0 },
    { "THEME", "the accent colour, Claude's included",
      THEME_OPTIONS, 4, SETTINGS_TAB_CUSTOMIZE, 0 },
    { "ON BOOT", "which screen the card comes up on",
      BOOT_OPTIONS, 3, SETTINGS_TAB_CUSTOMIZE, 0 },
    { "AUTOCONNECT", "at startup, to the last computer",
      AUTOCONNECT_OPTIONS, 2, SETTINGS_TAB_CUSTOMIZE, 0 },
    { "ADDRESSES", "hide IP addresses, for a demo or a photo",
      ADDRESSES_OPTIONS, 2, SETTINGS_TAB_CUSTOMIZE, 0 },
    { "VIEW TAPS", "clicks in dsterm view tap this DS",
      VIEW_TAPS_OPTIONS, 2, SETTINGS_TAB_CUSTOMIZE, 0 },
    { "PICTURE", "640x480, or 320x240 to send it 4x faster",
      PICTURE_OPTIONS, 2, SETTINGS_TAB_CUSTOMIZE, 0 },
    { "LIVE VIEW", "computer's view: sharp greys, or colour",
      LIVEVIEW_OPTIONS, 2, SETTINGS_TAB_CUSTOMIZE, 0 },
    { "DEFAULTS", "press twice; resets every tab",
      DEFAULTS_OPTIONS, 1, SETTINGS_TAB_CUSTOMIZE, 0 },
    // Short labels and chips that start further left: six actions a row.
    { "A", "what A sends on Control and KEYS",
      BUTTON_OPTIONS, 6, SETTINGS_TAB_BUTTONS, 46 },
    { "B", "what B sends on Control and KEYS",
      BUTTON_OPTIONS, 6, SETTINGS_TAB_BUTTONS, 46 },
    { "X", "X sends this on Control; still backs out",
      BUTTON_OPTIONS, 6, SETTINGS_TAB_BUTTONS, 46 },
    { "Y", "what Y sends on Control and KEYS",
      BUTTON_OPTIONS, 6, SETTINGS_TAB_BUTTONS, 46 },
    { "START", "what START sends on Control and KEYS",
      BUTTON_OPTIONS, 6, SETTINGS_TAB_BUTTONS, 46 },
    { "SHUTTER", "the camera screen's shutter; L+R still leaves",
      SHUTTER_OPTIONS, 3, SETTINGS_TAB_BUTTONS, 46 },
    { "SELECT", "fixed: cycles the bottom screens",
      FIXED_SELECT_OPTIONS, 1, SETTINGS_TAB_BUTTONS, 46 },
    { "D-PAD", "fixed: arrows; on Control, scrolls back",
      FIXED_DPAD_OPTIONS, 1, SETTINGS_TAB_BUTTONS, 46 },
    { "L", "fixed: Ctrl for the next key; L+R focus",
      FIXED_L_OPTIONS, 1, SETTINGS_TAB_BUTTONS, 46 },
    { "R", "fixed: Shift for the next key; takes the camera's picture",
      FIXED_R_OPTIONS, 1, SETTINGS_TAB_BUTTONS, 46 },
    { "RESET", "press twice; these buttons back as they were",
      DEFAULTS_OPTIONS, 1, SETTINGS_TAB_BUTTONS, 46 },
};

static const char *const TAB_LABELS[SETTINGS_TAB_COUNT] = {
    "SESSION", "CUSTOMIZE", "BUTTONS",
};

// Whether this card has a camera, set once at boot from isDSiMode().  Default
// off: a DS Lite build must behave exactly as if the SHUTTER row were never
// in the table.
static int camera_available = 0;

void settings_set_camera_available(int on)
{
    camera_available = on != 0;
}

int settings_camera_available(void)
{
    return camera_available;
}

// The runtime-conditional rows: a row that is not "in the table" today is
// skipped by every count, rect, hit test and move below.
static int row_present(settings_row_t row)
{
    if (camera_available) return 1;
    return row != SETTINGS_ROW_SHUTTER && row != SETTINGS_ROW_PICTURE &&
           row != SETTINGS_ROW_LIVEVIEW;
}

// RGB15, like render.c's palette: clay is that palette's orange.
static const uint16_t THEME_RGB[SETTINGS_THEME_COUNT] = {
    [SETTINGS_THEME_CLAY] = (uint16_t)(27 | 15 << 5 | 11 << 10 | 1 << 15),
    [SETTINGS_THEME_SEA]  = (uint16_t)(9 | 18 << 5 | 29 << 10 | 1 << 15),
    [SETTINGS_THEME_MOSS] = (uint16_t)(13 | 24 << 5 | 11 << 10 | 1 << 15),
    [SETTINGS_THEME_MONO] = (uint16_t)(23 | 23 << 5 | 23 << 10 | 1 << 15),
};

void settings_defaults(settings_t *settings)
{
    if (!settings) return;
    settings->model = SETTINGS_MODEL_UNSET;
    settings->boot = SETTINGS_BOOT_CONTROL;
    settings->sound = SETTINGS_SOUND_CHIP;
    settings->volume = SETTINGS_VOLUME_MID;
    // Texture is opt-in: a card should not start chattering at somebody who
    // never asked it to.
    // Soft typing while Claude writes, at the middle level: a session should
    // sound alive, and the scheduler thins it out on a long answer.
    settings->blips = SETTINGS_WRITING_MID;
    button_map_defaults(&settings->buttons);
    settings->typing = 1;
    settings->theme = SETTINGS_THEME_CLAY;
    settings->autoconnect = 1;
    settings->hide_addresses = 0;
    settings->view_taps = 0;
    // The one setting whose default is not a taste. It is a safety rule, and a
    // safety rule that ships switched off is not one.
    settings->god_mode = 0;
    settings->camera_shutter = SHUTTER_R;
    settings->picture = SETTINGS_PICTURE_FULL;
    settings->liveview = SETTINGS_LIVEVIEW_GREY;
}

void settings_ui_init(settings_ui_t *ui)
{
    if (!ui) return;
    ui->row = 0;
    ui->option = 0;
    ui->reset_armed = 0;
    ui->scroll = 0;
    ui->refused = 0;
}

int settings_valid(const settings_t *settings)
{
    if (!settings) return 0;
    if (settings->model < 0 || settings->model >= SETTINGS_MODEL_COUNT) return 0;
    if (settings->boot < 0 || settings->boot >= SETTINGS_BOOT_COUNT) return 0;
    if (settings->sound < 0 || settings->sound >= SETTINGS_SOUND_COUNT) return 0;
    if (settings->volume < 0 || settings->volume >= SETTINGS_VOLUME_COUNT) {
        return 0;
    }
    if (settings->blips < SETTINGS_WRITING_OFF || settings->blips > SETTINGS_WRITING_HIGH) {
        return 0;
    }
    if (!button_map_valid(&settings->buttons)) return 0;
    if (settings->autoconnect != 0 && settings->autoconnect != 1) return 0;
    if (settings->hide_addresses != 0 && settings->hide_addresses != 1) return 0;
    if (settings->view_taps != 0 && settings->view_taps != 1) return 0;
    if (settings->typing != 0 && settings->typing != 1) return 0;
    if (settings->theme < 0 || settings->theme >= SETTINGS_THEME_COUNT) return 0;
    if (settings->camera_shutter < 0 ||
        settings->camera_shutter >= SHUTTER_COUNT) {
        return 0;
    }
    if (settings->liveview < 0 || settings->liveview >= SETTINGS_LIVEVIEW_COUNT) {
        return 0;
    }
    if (settings->picture < 0 || settings->picture >= SETTINGS_PICTURE_COUNT) {
        return 0;
    }
    return settings->god_mode == 0 || settings->god_mode == 1;
}

const char *settings_row_label(settings_row_t row)
{
    if ((unsigned)row >= SETTINGS_ROW_COUNT) return "";
    return ROWS[row].label;
}

int settings_row_fixed(settings_row_t row)
{
    return row == SETTINGS_ROW_FIXED_SELECT || row == SETTINGS_ROW_FIXED_DPAD ||
           row == SETTINGS_ROW_FIXED_L || row == SETTINGS_ROW_FIXED_R;
}

unsigned settings_tab_rows(unsigned tab)
{
    unsigned n = 0;
    for (unsigned i = 0; i < SETTINGS_ROW_COUNT; i++) {
        if (ROWS[i].tab == tab && row_present((settings_row_t)i)) n++;
    }
    return n;
}

unsigned settings_row_tab(settings_row_t row)
{
    if ((unsigned)row >= SETTINGS_ROW_COUNT) return SETTINGS_TAB_COUNT;
    return ROWS[row].tab;
}

const char *settings_tab_label(unsigned tab)
{
    return tab < SETTINGS_TAB_COUNT ? TAB_LABELS[tab] : "";
}

uint16_t settings_theme_rgb(settings_theme_t theme)
{
    if (theme < 0 || theme >= SETTINGS_THEME_COUNT) theme = SETTINGS_THEME_CLAY;
    return THEME_RGB[theme];
}

const char *settings_row_note(settings_row_t row)
{
    if ((unsigned)row >= SETTINGS_ROW_COUNT) return "";
    return ROWS[row].note;
}

unsigned settings_option_count(settings_row_t row)
{
    if ((unsigned)row >= SETTINGS_ROW_COUNT) return 0;
    return ROWS[row].count;
}

static const option_t *option_at(settings_row_t row, unsigned option)
{
    if ((unsigned)row >= SETTINGS_ROW_COUNT) return NULL;
    if (option >= ROWS[row].count) return NULL;
    return &ROWS[row].options[option];
}

const char *settings_option_label(settings_row_t row, unsigned option)
{
    const option_t *entry = option_at(row, option);
    return entry ? entry->label : "";
}

const char *settings_option_sends(settings_row_t row, unsigned option)
{
    const option_t *entry = option_at(row, option);
    return entry ? entry->sends : NULL;
}

unsigned settings_option_current(const settings_t *settings, settings_row_t row)
{
    if (!settings) return SETTINGS_OPTIONS_MAX;
    switch (row) {
    case SETTINGS_ROW_MODEL:
        // UNSET has no chip, so the chip index is one behind the enum. Nothing
        // is current until something has been asked for.
        if (settings->model == SETTINGS_MODEL_UNSET) return SETTINGS_OPTIONS_MAX;
        return (unsigned)settings->model - 1u;
    case SETTINGS_ROW_BOOT:
        return (unsigned)settings->boot;
    case SETTINGS_ROW_SOUND:
        return (unsigned)settings->sound;
    case SETTINGS_ROW_VOLUME:
        return (unsigned)settings->volume;
    case SETTINGS_ROW_BLIPS:
        return (unsigned)settings->blips;
    case SETTINGS_ROW_BUTTON_A:
    case SETTINGS_ROW_BUTTON_B:
    case SETTINGS_ROW_BUTTON_X:
    case SETTINGS_ROW_BUTTON_Y:
    case SETTINGS_ROW_BUTTON_START:
        return settings->buttons.action[row - SETTINGS_ROW_BUTTON_A];
    case SETTINGS_ROW_SHUTTER:
        return (unsigned)settings->camera_shutter;
    case SETTINGS_ROW_PICTURE:
        return (unsigned)settings->picture;
    case SETTINGS_ROW_LIVEVIEW:
        return (unsigned)settings->liveview;
    case SETTINGS_ROW_AUTOCONNECT:
        return settings->autoconnect ? 0u : 1u;      // ON is the first chip
    case SETTINGS_ROW_ADDRESSES:
        return settings->hide_addresses ? 1u : 0u;   // SHOW is the first chip
    case SETTINGS_ROW_VIEW_TAPS:
        return settings->view_taps ? 1u : 0u;        // OFF is the first chip
    case SETTINGS_ROW_TYPING:
        return settings->typing ? 0u : 1u;
    case SETTINGS_ROW_THEME:
        return (unsigned)settings->theme;
    case SETTINGS_ROW_GOD_MODE:
        return settings->god_mode ? 1u : 0u;
    case SETTINGS_ROW_SESSION:
    case SETTINGS_ROW_DEFAULTS:
    case SETTINGS_ROW_COUNT:
    default:
        // Things you do, not things you are.
        return SETTINGS_OPTIONS_MAX;
    }
}

// ----------------------------------------------------------- the presses ---

void settings_ui_move(const settings_t *settings, settings_ui_t *ui,
                      int cols, int rows)
{
    (void)settings;
    if (!ui) return;
    if (rows) {
        // Rows wrap: five of them on one screen is a ring you can feel the
        // shape of, and wrapping saves four presses to get from the last back
        // to the first.
        // Within the tab on screen: the other tab's rows are not there.
        const int step = rows > 0 ? 1 : -1;
        const unsigned tab = ui->row < SETTINGS_ROW_COUNT ? ROWS[ui->row].tab
                                                          : SETTINGS_TAB_SESSION;
        unsigned next = ui->row < SETTINGS_ROW_COUNT ? ui->row : 0u;
        do {
            next = (unsigned)(((int)next + step + SETTINGS_ROW_COUNT) %
                              SETTINGS_ROW_COUNT);
        } while (ROWS[next].tab != tab ||
                 !row_present((settings_row_t)next));
        ui->row = next;
        ui->refused = 0;
        // Keep the highlight on screen: a tab longer than the screen scrolls
        // under it, one row at a time, and back to the top on wrapping.
        unsigned index = 0;
        for (unsigned i = 0; i < next; i++) {
            if (ROWS[i].tab == tab) index++;
        }
        if (index < ui->scroll) ui->scroll = index;
        if (index >= ui->scroll + SETTINGS_VISIBLE_ROWS) {
            ui->scroll = index - SETTINGS_VISIBLE_ROWS + 1u;
        }
        // Land somewhere real in the new row. Rows do not all have the same
        // number of chips, and a highlight left past the end of a shorter one
        // is a highlight on nothing.
        const unsigned count = settings_option_count((settings_row_t)ui->row);
        if (count == 0) ui->option = 0;
        else if (ui->option >= count) ui->option = count - 1;
        // Moving off a row abandons its confirm. Coming back to DEFAULTS
        // should not find it still half-pressed from a minute ago.
        ui->reset_armed = 0;
    }
    if (cols) {
        // Options clamp rather than wrap. A chip strip is read left to right
        // and a highlight that jumps from the last back to the first reads as
        // a missed press.
        const unsigned count = settings_option_count((settings_row_t)ui->row);
        if (count == 0) { ui->option = 0; return; }
        const int step = cols > 0 ? 1 : -1;
        int next = (int)ui->option + step;
        if (next < 0) next = 0;
        if (next >= (int)count) next = (int)count - 1;
        ui->option = (unsigned)next;
    }
}

void settings_ui_tab(const settings_t *settings, settings_ui_t *ui,
                     unsigned tab)
{
    if (!ui || tab >= SETTINGS_TAB_COUNT) return;
    for (unsigned i = 0; i < SETTINGS_ROW_COUNT; i++) {
        if (ROWS[i].tab != tab || !row_present((settings_row_t)i)) continue;
        ui->row = i;
        const unsigned current = settings_option_current(settings, (settings_row_t)i);
        ui->option = current < ROWS[i].count ? current : 0u;
        ui->reset_armed = 0;
        ui->refused = 0;
        ui->scroll = 0;
        return;
    }
}

settings_press_t settings_press(settings_t *settings, settings_ui_t *ui,
                                settings_row_t row, unsigned option,
                                const char **send)
{
    // Cleared first and unconditionally: a caller that ignores the return
    // value must not be able to find a live pointer here from a press that
    // did not send anything.
    if (send) *send = NULL;
    if (!settings || !ui) return SETTINGS_PRESS_NOTHING;
    const option_t *entry = option_at(row, option);
    if (!entry) return SETTINGS_PRESS_NOTHING;

    ui->row = (unsigned)row;
    ui->option = option;
    ui->refused = 0;
    // Any press that is not a reset chip disarms the confirm.
    if (row != SETTINGS_ROW_DEFAULTS && row != SETTINGS_ROW_BUTTONS_RESET) {
        ui->reset_armed = 0;
    }

    switch (row) {
    case SETTINGS_ROW_MODEL:
        settings->model = (settings_model_t)(option + 1u);
        if (send) *send = entry->sends;
        return SETTINGS_PRESS_SENDS;

    case SETTINGS_ROW_SESSION:
        if (send) *send = entry->sends;
        return SETTINGS_PRESS_SENDS;

    case SETTINGS_ROW_BOOT:
        if (settings->boot == (settings_boot_t)option) {
            return SETTINGS_PRESS_NOTHING;
        }
        settings->boot = (settings_boot_t)option;
        return SETTINGS_PRESS_CHANGED;

    case SETTINGS_ROW_SOUND:
        if (settings->sound == (settings_sound_t)option) {
            return SETTINGS_PRESS_NOTHING;
        }
        settings->sound = (settings_sound_t)option;
        return SETTINGS_PRESS_CHANGED;

    case SETTINGS_ROW_VOLUME:
        // Pressing the level you are already on is still worth answering:
        // it is how you hear what that level sounds like.  So it reports a
        // change either way, and the caller's preview plays.
        settings->volume = (settings_volume_t)option;
        return SETTINGS_PRESS_CHANGED;

    case SETTINGS_ROW_BLIPS:
        // Pressing the level already set still answers, like VOLUME: it is
        // how you hear what that level sounds like.
        settings->blips = (int)option;
        return SETTINGS_PRESS_CHANGED;

    case SETTINGS_ROW_BUTTON_A:
    case SETTINGS_ROW_BUTTON_B:
    case SETTINGS_ROW_BUTTON_X:
    case SETTINGS_ROW_BUTTON_Y:
    case SETTINGS_ROW_BUTTON_START: {
        const button_t b = (button_t)(row - SETTINGS_ROW_BUTTON_A);
        if (settings->buttons.action[b] == option) return SETTINGS_PRESS_NOTHING;
        if (!button_map_set(&settings->buttons, b, (button_action_t)option)) {
            // That was the last button on Enter.
            ui->refused = 1;
            return SETTINGS_PRESS_NOTHING;
        }
        return SETTINGS_PRESS_CHANGED;
    }

    case SETTINGS_ROW_SHUTTER:
        // Unreachable by touch on a card with no camera, but reachable by a
        // caller that did not check -- the row does not exist there.
        if (!camera_available) return SETTINGS_PRESS_NOTHING;
        if (settings->camera_shutter == (camera_shutter_t)option) {
            return SETTINGS_PRESS_NOTHING;
        }
        settings->camera_shutter = (camera_shutter_t)option;
        return SETTINGS_PRESS_CHANGED;

    case SETTINGS_ROW_PICTURE:
        // Absent where SHUTTER is: a card with no camera has no picture size
        // to pick.
        if (!camera_available) return SETTINGS_PRESS_NOTHING;
        if (settings->picture == (settings_picture_t)option) {
            return SETTINGS_PRESS_NOTHING;
        }
        settings->picture = (settings_picture_t)option;
        return SETTINGS_PRESS_CHANGED;

    case SETTINGS_ROW_LIVEVIEW:
        if (!camera_available) return SETTINGS_PRESS_NOTHING;
        if (settings->liveview == (settings_liveview_t)option) {
            return SETTINGS_PRESS_NOTHING;
        }
        settings->liveview = (settings_liveview_t)option;
        return SETTINGS_PRESS_CHANGED;

    case SETTINGS_ROW_FIXED_SELECT:
    case SETTINGS_ROW_FIXED_DPAD:
    case SETTINGS_ROW_FIXED_L:
    case SETTINGS_ROW_FIXED_R:
        return SETTINGS_PRESS_NOTHING;

    case SETTINGS_ROW_BUTTONS_RESET:
        if (!ui->reset_armed) {
            ui->reset_armed = 1;
            return SETTINGS_PRESS_NOTHING;
        }
        button_map_defaults(&settings->buttons);
        settings->camera_shutter = SHUTTER_R;
        ui->reset_armed = 0;
        return SETTINGS_PRESS_CHANGED;

    case SETTINGS_ROW_TYPING: {
        const int want = option == 0 ? 1 : 0;
        if (settings->typing == want) return SETTINGS_PRESS_NOTHING;
        settings->typing = want;
        return SETTINGS_PRESS_CHANGED;
    }

    case SETTINGS_ROW_THEME:
        if (settings->theme == (settings_theme_t)option) {
            return SETTINGS_PRESS_NOTHING;
        }
        settings->theme = (settings_theme_t)option;
        return SETTINGS_PRESS_CHANGED;

    case SETTINGS_ROW_AUTOCONNECT: {
        const int want = option == 0 ? 1 : 0;
        if (settings->autoconnect == want) return SETTINGS_PRESS_NOTHING;
        settings->autoconnect = want;
        return SETTINGS_PRESS_CHANGED;
    }

    case SETTINGS_ROW_VIEW_TAPS: {
        const int want = option ? 1 : 0;
        if (settings->view_taps == want) return SETTINGS_PRESS_NOTHING;
        settings->view_taps = want;
        return SETTINGS_PRESS_CHANGED;
    }

    case SETTINGS_ROW_ADDRESSES: {
        const int want = option ? 1 : 0;
        if (settings->hide_addresses == want) return SETTINGS_PRESS_NOTHING;
        settings->hide_addresses = want;
        return SETTINGS_PRESS_CHANGED;
    }

    case SETTINGS_ROW_GOD_MODE: {
        const int want = option ? 1 : 0;
        if (settings->god_mode == want) return SETTINGS_PRESS_NOTHING;
        settings->god_mode = want;
        return SETTINGS_PRESS_CHANGED;
    }

    case SETTINGS_ROW_DEFAULTS:
        if (!ui->reset_armed) {
            ui->reset_armed = 1;
            return SETTINGS_PRESS_NOTHING;
        }
        settings_defaults(settings);
        ui->reset_armed = 0;
        return SETTINGS_PRESS_RESET;

    case SETTINGS_ROW_COUNT:
    default:
        return SETTINGS_PRESS_NOTHING;
    }
}

// ---------------------------------------------------------- the geometry ---

#define SETTINGS_ROW_W 248
#define CHIP_GAP 3
#define CHIP_PAD 10     // five pixels of margin either side of the text
#define GLYPH_W 6

static int chip_width(const char *label)
{
    return (int)strlen(label) * GLYPH_W + CHIP_PAD;
}

// A row's place within its own tab: rows of the other tab are not on screen
// and take no space.
static int row_index_in_tab(settings_row_t row)
{
    int index = 0;
    for (int i = 0; i < (int)row; i++) {
        if (ROWS[i].tab == ROWS[row].tab &&
            row_present((settings_row_t)i)) index++;
    }
    return index;
}

int settings_row_rect(settings_row_t row, unsigned scroll,
                      int *x, int *y, int *w, int *h)
{
    if ((unsigned)row >= SETTINGS_ROW_COUNT || !row_present(row)) return 0;
    const int index = row_index_in_tab(row) - (int)scroll;
    if (index < 0 || index >= SETTINGS_VISIBLE_ROWS) return 0;
    if (x) *x = SETTINGS_LABEL_X;
    if (y) *y = SETTINGS_ROW_Y + index * SETTINGS_ROW_PITCH;
    if (w) *w = SETTINGS_ROW_W;
    if (h) *h = SETTINGS_ROW_H;
    return 1;
}

int settings_option_rect(settings_row_t row, unsigned option, unsigned scroll,
                         int *x, int *y, int *w, int *h)
{
    const option_t *entry = option_at(row, option);
    if (!entry) return 0;
    int ry = 0;
    if (!settings_row_rect(row, scroll, NULL, &ry, NULL, NULL)) return 0;
    // Walk the row from the left, so a chip's position is a function of the
    // labels before it and nothing else.
    int at = ROWS[row].option_x ? ROWS[row].option_x : SETTINGS_OPTION_X;
    for (unsigned i = 0; i < option; i++) {
        at += chip_width(ROWS[row].options[i].label) + CHIP_GAP;
    }
    if (x) *x = at;
    if (y) *y = ry + (SETTINGS_ROW_H - SETTINGS_OPTION_H) / 2;
    if (w) *w = chip_width(entry->label);
    if (h) *h = SETTINGS_OPTION_H;
    return 1;
}

int settings_tab_rect(unsigned tab, int *x, int *y, int *w, int *h)
{
    if (tab >= SETTINGS_TAB_COUNT) return 0;
    int at = SETTINGS_TAB_X;
    for (unsigned i = 0; i < tab; i++) at += chip_width(TAB_LABELS[i]) + CHIP_GAP;
    if (x) *x = at;
    if (y) *y = SETTINGS_TAB_Y;
    if (w) *w = chip_width(TAB_LABELS[tab]);
    if (h) *h = SETTINGS_TAB_H;
    return 1;
}

unsigned settings_tab_hit(int x, int y)
{
    for (unsigned t = 0; t < SETTINGS_TAB_COUNT; t++) {
        int rx, ry, rw, rh;
        (void)settings_tab_rect(t, &rx, &ry, &rw, &rh);
        if (x >= rx && x < rx + rw && y >= ry && y < ry + rh) return t;
    }
    return SETTINGS_TAB_COUNT;
}

settings_row_t settings_row_hit(unsigned tab, unsigned scroll, int x, int y)
{
    for (int i = 0; i < SETTINGS_ROW_COUNT; i++) {
        if (ROWS[i].tab != tab || !row_present((settings_row_t)i)) continue;
        int rx = 0, ry = 0, rw = 0, rh = 0;
        if (!settings_row_rect((settings_row_t)i, scroll, &rx, &ry, &rw, &rh)) continue;
        if (x >= rx && x < rx + rw && y >= ry && y < ry + rh) {
            return (settings_row_t)i;
        }
    }
    return SETTINGS_ROW_COUNT;
}

int settings_more_above(unsigned scroll)
{
    return scroll > 0;
}

int settings_more_below(unsigned tab, unsigned scroll)
{
    return settings_tab_rows(tab) > scroll + SETTINGS_VISIBLE_ROWS;
}

int settings_scroll_hit(unsigned tab, unsigned scroll, int x, int y)
{
    if (x < SETTINGS_ARROW_X) return 0;
    const int top = SETTINGS_ROW_Y;
    const int bottom = SETTINGS_ROW_Y + SETTINGS_VISIBLE_ROWS * SETTINGS_ROW_PITCH;
    if (y >= top && y < top + SETTINGS_ROW_PITCH && settings_more_above(scroll)) return -1;
    if (y >= bottom - SETTINGS_ROW_PITCH && y < bottom &&
        settings_more_below(tab, scroll)) {
        return 1;
    }
    return 0;
}

unsigned settings_option_hit(settings_row_t row, unsigned scroll, int x, int y)
{
    const unsigned count = settings_option_count(row);
    for (unsigned i = 0; i < count; i++) {
        int rx, ry, rw, rh;
        if (!settings_option_rect(row, i, scroll, &rx, &ry, &rw, &rh)) continue;
        if (x >= rx && x < rx + rw && y >= ry && y < ry + rh) return i;
    }
    return SETTINGS_OPTIONS_MAX;
}

// ------------------------------------------------------------- the page ---

void settings_about_init(settings_about_t *about)
{
    if (!about) return;
    memset(about, 0, sizeof(*about));
}

// One cell, bounded. Everything below goes through here so a formatting bug
// can produce a wrong page but never a write outside the grid.
static void put(grid_t *grid, int row, int col, char ch, uint8_t fg)
{
    if (row < 0 || row >= PROTO_ROWS || col < 0 || col >= PROTO_COLS) return;
    cell_t *cell = &grid->cells[row * PROTO_COLS + col];
    cell->glyph = (uint8_t)ch;
    cell->attr = (uint8_t)(fg << 4);
    cell->flags = 0;
}

static int put_text(grid_t *grid, int row, int col, const char *text,
                    uint8_t fg)
{
    if (!text) return col;
    text = privacy_text(text);   // HIDE ADDRESSES (privacy.h)
    for (; *text && col < PROTO_COLS; text++, col++) {
        put(grid, row, col, *text, fg);
    }
    return col;
}

#define FG_PLAIN  0
#define FG_ORANGE 9
#define FG_RED    1

void settings_render(const settings_t *settings, const settings_ui_t *ui,
                     const settings_about_t *about, grid_t *grid)
{
    if (!grid) return;

    // Blank first, every cell of it. A page that paints only its own text
    // leaves the last page showing through in the gaps -- and here it would
    // also leave an extension standing on a line that is no longer there.
    for (int i = 0; i < PROTO_CELLS; i++) {
        grid->cells[i].glyph = ' ';
        grid->cells[i].attr = 0;
        grid->cells[i].flags = 0;
    }
    if (!settings || !ui) return;

    const unsigned tab = ui->row < SETTINGS_ROW_COUNT ? ROWS[ui->row].tab
                                                      : SETTINGS_TAB_SESSION;
    put_text(grid, 0, 0, "SETTINGS", FG_ORANGE);
    put_text(grid, 0, 9, tab == SETTINGS_TAB_SESSION
                             ? "-- SESSION: what reaches Claude"
                             : tab == SETTINGS_TAB_BUTTONS
                             ? "-- BUTTONS: what each one sends"
                             : "-- CUSTOMIZE: this device, yours",
             FG_PLAIN);

    // Rows 2..6: every row and its current value, so the top screen says what
    // is set without needing the bottom one in view. The value column matches
    // the PROJECT page's, because two pages that lay out the same kind of
    // thing differently is two pages to learn.
    const int value_col = 14;
    int shown = 0;
    for (int i = 0; i < SETTINGS_ROW_COUNT; i++) {
        const settings_row_t row = (settings_row_t)i;
        if (ROWS[i].tab != tab || !row_present(row)) continue;
        const int line = 2 + shown++;
        const int selected = (unsigned)i == ui->row;
        put_text(grid, line, 0, selected ? ">" : " ", FG_ORANGE);
        put_text(grid, line, 2, settings_row_label(row),
                 selected ? FG_ORANGE : FG_PLAIN);

        const unsigned current = settings_option_current(settings, row);
        if (current < settings_option_count(row)) {
            put_text(grid, line, value_col,
                     settings_option_label(row, current),
                     row == SETTINGS_ROW_GOD_MODE && settings->god_mode
                         ? FG_RED : FG_PLAIN);
        } else if (settings_row_fixed(row)) {
            put_text(grid, line, value_col, settings_option_label(row, 0), FG_PLAIN);
            put_text(grid, line, value_col + 9, "(fixed)", FG_PLAIN);
        } else if (row == SETTINGS_ROW_MODEL) {
            // Honest about what this card can and cannot see: the model is
            // Claude Code's setting, and the card is never told it.
            put_text(grid, line, value_col, "unknown", FG_PLAIN);
        } else {
            put_text(grid, line, value_col, "--", FG_PLAIN);
        }
    }

    // Row 8: the note for wherever the highlight is, and -- when the
    // highlighted chip types something -- the exact text it will type. That
    // pairing is the point: you read the string on this screen before your
    // thumb commits to the chip on the other one.
    // Everything below the rows is placed from the row count, so a row added
    // to the table moves the page down with it rather than under it.
    const int note_line = 2 + shown + 1;
    put_text(grid, note_line, 0, settings_row_note((settings_row_t)ui->row),
             FG_PLAIN);
    const char *sends = settings_option_sends((settings_row_t)ui->row,
                                              ui->option);
    if (sends) {
        int col = put_text(grid, note_line + 1, 0, "types: ", FG_PLAIN);
        put_text(grid, note_line + 1, col, sends, FG_ORANGE);
    } else if (ui->refused) {
        put_text(grid, note_line + 1, 0, "Enter has to stay on one button",
                 FG_ORANGE);
    } else if (ui->reset_armed) {
        put_text(grid, note_line + 1, 0,
                 ui->row == SETTINGS_ROW_BUTTONS_RESET
                     ? "press SURE? to put these buttons back"
                     : "press SURE? to reset every tab",
                 FG_ORANGE);
    }

    if (settings->god_mode) {
        // Said on the screen it is armed from, every time it is open.
        put_text(grid, note_line + 2, 0, "GOD MODE: PROJECT actions fire unread",
                 FG_RED);
    }

    // Rows 13..: what this card is, which is the other half of why anybody
    // opens a settings page -- and on a device you have to reflash to change,
    // knowing which build is on it is not trivia.
    // The extension's row is the fourth, and only when it has a label: a
    // build with nothing attached shows four rows, not a row saying so.
    const char *const ext_label = about && about->ext_label[0] ?
                                  about->ext_label : NULL;
    const int rows = ext_label ? 5 : 4;
    // A long tab (CUSTOMIZE on a card with a camera) pushes ABOUT down; the
    // gaps around it give way first, so its last line -- whether the
    // settings were saved -- is not the one that falls off the screen.
    int about_line = note_line + 4;
    int gap = 2;
    if (about_line + gap + rows > PROTO_ROWS - 1) about_line = note_line + 3;
    if (about_line + gap + rows > PROTO_ROWS - 1) gap = 1;
    put_text(grid, about_line, 0, "ABOUT", FG_ORANGE);
    const char *const labels[] = {
        "build", "link", "session", ext_label ? ext_label : "card", "card",
    };
    const char *const values[] = {
        about ? about->build : "", about ? about->link : "",
        about ? about->session : "",
        ext_label ? about->ext_value : (about ? about->card : ""),
        about ? about->card : "",
    };
    for (int i = 0; i < rows; i++) {
        const int line = about_line + gap + i;
        if (line >= PROTO_ROWS - 1) break;
        put_text(grid, line, 2, labels[i], FG_PLAIN);
        put_text(grid, line, 12, values[i][0] ? values[i] : "--", FG_PLAIN);
    }
}

// ------------------------------------------------------------- the file ---

static const char *model_word(settings_model_t model)
{
    if (model <= SETTINGS_MODEL_UNSET || model >= SETTINGS_MODEL_COUNT) {
        return "unset";
    }
    return MODEL_OPTIONS[model - 1].file_word;
}

static int parse_model(const char *text, settings_model_t *out)
{
    if (ascii_equal(text, "unset")) { *out = SETTINGS_MODEL_UNSET; return 1; }
    // Version 1 wrote "default" for the same idea. Accepted so an upgraded
    // card reads its old file rather than throwing the whole thing away.
    if (ascii_equal(text, "default")) { *out = SETTINGS_MODEL_UNSET; return 1; }
    for (int i = 0; i < SETTINGS_MODEL_COUNT - 1; i++) {
        if (ascii_equal(text, MODEL_OPTIONS[i].file_word)) {
            *out = (settings_model_t)(i + 1);
            return 1;
        }
    }
    return 0;
}

static int parse_boot(const char *text, settings_boot_t *out)
{
    for (int i = 0; i < SETTINGS_BOOT_COUNT; i++) {
        if (ascii_equal(text, BOOT_OPTIONS[i].file_word)) {
            *out = (settings_boot_t)i;
            return 1;
        }
    }
    return 0;
}

static int parse_word(const char *text, const option_t *options,
                      unsigned count, unsigned *out)
{
    for (unsigned i = 0; i < count; i++) {
        if (ascii_equal(text, options[i].file_word)) {
            *out = i;
            return 1;
        }
    }
    return 0;
}

static int parse_switch(const char *text, int *out)
{
    if (ascii_equal(text, "on") || ascii_equal(text, "yes") ||
        ascii_equal(text, "1")) {
        *out = 1;
        return 1;
    }
    if (ascii_equal(text, "off") || ascii_equal(text, "no") ||
        ascii_equal(text, "0")) {
        *out = 0;
        return 1;
    }
    return 0;
}

int settings_parse(const char *text, settings_t *settings,
                   char *err, size_t err_len)
{
    if (!settings) {
        fail(err, err_len, "no settings destination");
        return 0;
    }
    settings_defaults(settings);
    if (!text) {
        fail(err, err_len, "no settings text");
        return 0;
    }

    settings_t parsed;
    settings_defaults(&parsed);
    int seen_version = 0;
    const char *at = text;

    while (*at) {
        char line[128];
        size_t n = 0;
        while (*at && *at != '\n' && n + 1 < sizeof(line)) line[n++] = *at++;
        while (*at && *at != '\n') at++;         // discard an over-long tail
        if (*at == '\n') at++;
        line[n] = '\0';

        char *cursor = line;
        while (*cursor == ' ' || *cursor == '\t') cursor++;
        if (*cursor == '\0' || *cursor == '#') continue;

        char *equals = strchr(cursor, '=');
        if (!equals) {
            fail(err, err_len, "settings line without a key=value");
            settings_defaults(settings);
            return 0;
        }
        *equals = '\0';
        char *key = cursor;
        char *value = equals + 1;
        // Trim both ends of both halves; a hand-edited file has spaces in it.
        char *key_end = key + strlen(key);
        while (key_end > key && (key_end[-1] == ' ' || key_end[-1] == '\t')) {
            *--key_end = '\0';
        }
        while (*value == ' ' || *value == '\t') value++;
        char *value_end = value + strlen(value);
        while (value_end > value &&
               (value_end[-1] == ' ' || value_end[-1] == '\t' ||
                value_end[-1] == '\r')) {
            *--value_end = '\0';
        }

        if (ascii_equal(key, "version")) {
            // Version 1 is read, not rejected. Its two dead keys fall through
            // to the ignore below, and the one setting it shared with this
            // version survives the upgrade -- which is the whole point of
            // having a version rather than a magic number.
            if (seen_version ||
                !(ascii_equal(value, "1") || ascii_equal(value, "2") ||
                  ascii_equal(value, "3") || ascii_equal(value, "4"))) {
                fail(err, err_len, "unsupported settings version");
                settings_defaults(settings);
                return 0;
            }
            seen_version = 1;
        } else if (ascii_equal(key, "model")) {
            if (!parse_model(value, &parsed.model)) {
                fail(err, err_len, "unknown model");
                settings_defaults(settings);
                return 0;
            }
        } else if (ascii_equal(key, "boot")) {
            if (!parse_boot(value, &parsed.boot)) {
                fail(err, err_len, "unknown boot screen");
                settings_defaults(settings);
                return 0;
            }
        } else if (ascii_equal(key, "sound")) {
            unsigned v;
            // "soft" is what the chip says; "snes" is what cards have on
            // them.  Either one, so a hand edit cannot reset the page.
            if (ascii_equal(value, "soft")) {
                v = SETTINGS_SOUND_SNES;
            } else if (!parse_word(value, SOUND_OPTIONS, SETTINGS_SOUND_COUNT, &v)) {
                fail(err, err_len, "unknown sound");
                settings_defaults(settings);
                return 0;
            }
            parsed.sound = (settings_sound_t)v;
        } else if (ascii_equal(key, "volume")) {
            unsigned v;
            if (!parse_word(value, VOLUME_OPTIONS, SETTINGS_VOLUME_COUNT, &v)) {
                fail(err, err_len, "unknown volume");
                settings_defaults(settings);
                return 0;
            }
            parsed.volume = (settings_volume_t)v;
        } else if (ascii_equal(key, "blips")) {
            // Version 3 wrote on/off; "on" was the one level there was.
            unsigned v;
            if (ascii_equal(value, "on")) {
                v = SETTINGS_WRITING_MID;
            } else if (!parse_word(value, BLIPS_OPTIONS, 4, &v)) {
                fail(err, err_len, "invalid blips");
                settings_defaults(settings);
                return 0;
            }
            parsed.blips = (int)v;
        } else if (ascii_equal(key, "button_a") || ascii_equal(key, "button_b") ||
                   ascii_equal(key, "button_x") || ascii_equal(key, "button_y") ||
                   ascii_equal(key, "button_start")) {
            unsigned v;
            if (!parse_word(value, BUTTON_OPTIONS, BUTTON_ACTION_COUNT, &v)) {
                fail(err, err_len, "unknown button action");
                settings_defaults(settings);
                return 0;
            }
            const char k = (char)(key[7] | 0x20);       // keys match any case
            const button_t b = k == 'a' ? BUTTON_A : k == 'b' ? BUTTON_B :
                               k == 'x' ? BUTTON_X : k == 'y' ? BUTTON_Y
                                        : BUTTON_START;
            parsed.buttons.action[b] = (uint8_t)v;
        } else if (ascii_equal(key, "shutter")) {
            unsigned v;
            if (!parse_word(value, SHUTTER_OPTIONS, SHUTTER_COUNT, &v)) {
                fail(err, err_len, "unknown shutter");
                settings_defaults(settings);
                return 0;
            }
            parsed.camera_shutter = (camera_shutter_t)v;
        } else if (ascii_equal(key, "picture")) {
            unsigned v;
            if (!parse_word(value, PICTURE_OPTIONS, SETTINGS_PICTURE_COUNT,
                            &v)) {
                fail(err, err_len, "unknown picture");
                settings_defaults(settings);
                return 0;
            }
            parsed.picture = (settings_picture_t)v;
        } else if (ascii_equal(key, "liveview")) {
            unsigned v;
            if (!parse_word(value, LIVEVIEW_OPTIONS, SETTINGS_LIVEVIEW_COUNT,
                            &v)) {
                fail(err, err_len, "unknown liveview");
                settings_defaults(settings);
                return 0;
            }
            parsed.liveview = (settings_liveview_t)v;
        } else if (ascii_equal(key, "typing")) {
            if (!parse_switch(value, &parsed.typing)) {
                fail(err, err_len, "invalid typing");
                settings_defaults(settings);
                return 0;
            }
        } else if (ascii_equal(key, "theme")) {
            unsigned v;
            if (!parse_word(value, THEME_OPTIONS, SETTINGS_THEME_COUNT, &v)) {
                fail(err, err_len, "unknown theme");
                settings_defaults(settings);
                return 0;
            }
            parsed.theme = (settings_theme_t)v;
        } else if (ascii_equal(key, "autoconnect")) {
            if (!parse_switch(value, &parsed.autoconnect)) {
                fail(err, err_len, "invalid autoconnect");
                settings_defaults(settings);
                return 0;
            }
        } else if (ascii_equal(key, "viewtaps")) {
            if (!parse_switch(value, &parsed.view_taps)) {
                fail(err, err_len, "invalid viewtaps");
                settings_defaults(settings);
                return 0;
            }
        } else if (ascii_equal(key, "addresses")) {
            unsigned v;
            if (!parse_word(value, ADDRESSES_OPTIONS, 2, &v)) {
                fail(err, err_len, "invalid addresses");
                settings_defaults(settings);
                return 0;
            }
            parsed.hide_addresses = v ? 1 : 0;
        } else if (ascii_equal(key, "god_mode")) {
            if (!parse_switch(value, &parsed.god_mode)) {
                fail(err, err_len, "invalid god_mode");
                settings_defaults(settings);
                return 0;
            }
        }
        // Unknown keys are ignored, so a file written by a later build still
        // loads on this one rather than resetting somebody's whole page -- and
        // so version 1's `dim` and `repeat` cost nothing on the way through.
    }

    if (!seen_version) {
        fail(err, err_len, "settings file has no version");
        settings_defaults(settings);
        return 0;
    }
    if (!settings_valid(&parsed)) {
        fail(err, err_len, "settings failed validation");
        settings_defaults(settings);
        return 0;
    }
    *settings = parsed;
    if (err && err_len) err[0] = '\0';
    return 1;
}

int settings_load(const char *path, settings_t *settings,
                  char *err, size_t err_len)
{
    if (!settings) return 0;
    settings_defaults(settings);
    if (!path || !*path) {
        fail(err, err_len, "settings path missing");
        return 0;
    }
    FILE *fh = fopen(path, "rb");
    if (!fh) {
        fail(err, err_len, "cannot open settings");
        return 0;
    }
    char text[SETTINGS_FILE_MAX + 1];
    const size_t got = fread(text, 1, sizeof(text) - 1, fh);
    const int read_error = ferror(fh);
    const int close_error = fclose(fh);
    if (read_error || close_error) {
        fail(err, err_len, "cannot read settings");
        return 0;
    }
    text[got] = '\0';
    return settings_parse(text, settings, err, err_len);
}

int settings_save(const char *path, const settings_t *settings,
                  char *err, size_t err_len)
{
    if (!path || !*path) {
        fail(err, err_len, "settings path missing");
        return 0;
    }
    if (!settings_valid(settings)) {
        fail(err, err_len, "invalid settings");
        return 0;
    }

    char text[SETTINGS_FILE_MAX];
    const int length = snprintf(text, sizeof(text),
                                "# DS Claude Terminal settings\n"
                                "version=%d\n"
                                "model=%s\n"
                                "boot=%s\n"
                                "sound=%s\n"
                                "volume=%s\n"
                                "blips=%s\n"
                                "typing=%s\n"
                                "theme=%s\n"
                                "autoconnect=%s\n"
                                "addresses=%s\n"
                                "viewtaps=%s\n"
                                "god_mode=%s\n"
                                "button_a=%s\n"
                                "button_b=%s\n"
                                "button_x=%s\n"
                                "button_y=%s\n"
                                "button_start=%s\n"
                                "shutter=%s\n"
                                "picture=%s\n"
                                "liveview=%s\n",
                                SETTINGS_VERSION,
                                model_word(settings->model),
                                BOOT_OPTIONS[settings->boot].file_word,
                                SOUND_OPTIONS[settings->sound].file_word,
                                VOLUME_OPTIONS[settings->volume].file_word,
                                BLIPS_OPTIONS[settings->blips].file_word,
                                settings->typing ? "on" : "off",
                                THEME_OPTIONS[settings->theme].file_word,
                                settings->autoconnect ? "on" : "off",
                                settings->hide_addresses ? "hide" : "show",
                                settings->view_taps ? "on" : "off",
                                settings->god_mode ? "on" : "off",
                                BUTTON_OPTIONS[settings->buttons.action[BUTTON_A]].file_word,
                                BUTTON_OPTIONS[settings->buttons.action[BUTTON_B]].file_word,
                                BUTTON_OPTIONS[settings->buttons.action[BUTTON_X]].file_word,
                                BUTTON_OPTIONS[settings->buttons.action[BUTTON_Y]].file_word,
                                BUTTON_OPTIONS[settings->buttons.action[BUTTON_START]].file_word,
                                camera_shutter_word(settings->camera_shutter),
                                PICTURE_OPTIONS[settings->picture].file_word,
                                LIVEVIEW_OPTIONS[settings->liveview].file_word);
    if (length < 0 || (size_t)length >= sizeof(text)) {
        fail(err, err_len, "cannot format settings");
        return 0;
    }

    char temporary[512];
    const int tn = snprintf(temporary, sizeof(temporary), "%s.tmp", path);
    if (tn < 0 || (size_t)tn >= sizeof(temporary)) {
        fail(err, err_len, "settings path too long");
        return 0;
    }
    (void)remove(temporary);
    FILE *fh = fopen(temporary, "wb");
    if (!fh) {
        fail(err, err_len, "cannot write settings");
        return 0;
    }
    int ok = fwrite(text, 1, (size_t)length, fh) == (size_t)length;
    if (ok && fflush(fh) != 0) ok = 0;
    if (fclose(fh) != 0) ok = 0;
    if (!ok) {
        (void)remove(temporary);
        fail(err, err_len, "cannot save settings");
        return 0;
    }

    // The same libfat workaround every small preference file here needs, for the
    // same measured reason: POSIX rename replaces its destination and libfat's
    // refuses to, so without clearing the way first every save after the very
    // first one fails silently.  That went unnoticed there for four builds.
    (void)remove(path);
    if (rename(temporary, path) != 0) {
        FILE *direct = fopen(path, "wb");
        int wrote = direct != NULL;
        if (direct) {
            if (fwrite(text, 1, (size_t)length, direct) != (size_t)length) {
                wrote = 0;
            }
            if (fflush(direct) != 0) wrote = 0;
            if (fclose(direct) != 0) wrote = 0;
        }
        (void)remove(temporary);
        if (!wrote) {
            fail(err, err_len, "cannot replace settings");
            return 0;
        }
    }
    if (err && err_len) err[0] = '\0';
    return 1;
}
