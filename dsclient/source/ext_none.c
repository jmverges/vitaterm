// No extension.
//
// This is the build with nothing attached: every hook in ext.h, doing
// nothing.  It is also the file the host suite links, so the terminal's own
// tests run against exactly what ships when nothing is attached.
//
// Pure C, no libnds, on purpose -- the same reason kbd.c and settings.c are:
// anything that cannot be compiled on the host cannot be tested there.

#include "ext.h"

const char *ext_hello_tokens(void) { return ""; }

void ext_init(void) {}
void ext_boot(void) {}
void ext_boot_late(void) {}

void ext_vblank(void) {}
void ext_frame(void) {}

void ext_link(int up) { (void)up; }
void ext_battery(int low) { (void)low; }
void ext_terminal_changed(void) {}
void ext_terminal_traffic(void) {}
void ext_cursor(const cursor_t *cur) { (void)cur; }
void ext_event(ext_event_t event) { (void)event; }

void ext_cue(ext_cue_t cue) { (void)cue; }
void ext_quiet_moment(int streaming, int silent) { (void)streaming; (void)silent; }
int  ext_key_tick(int backspace) { (void)backspace; return 0; }

int ext_message(uint8_t type, const uint8_t *payload, uint16_t len)
{
    (void)type;
    (void)payload;
    (void)len;
    return 0;
}

int  ext_menu_is_open(void) { return 0; }
void ext_menu_open(void) {}
void ext_menu_reset(void) {}
void ext_menu_input(unsigned raw, unsigned edge) { (void)raw; (void)edge; }
void ext_overlay_sync(void) {}

void ext_touch_down(void) {}
int  ext_touch_track(void) { return 0; }
int  ext_touch_hit(int px, int py) { (void)px; (void)py; return 0; }
void ext_touch_control_opened(void) {}
void ext_touch_up(void) {}

int ext_subscreen_draw(ext_draw_t what) { (void)what; return 0; }

const char *ext_control_label(void) { return NULL; }

const char *ext_about_label(void) { return NULL; }
const char *ext_about_value(void) { return NULL; }
const char *ext_now_label(void) { return NULL; }
const char *ext_now_value(void) { return NULL; }
