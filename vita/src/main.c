// Claude Terminal for PS Vita: the start screen, then the terminal.
//
//   Cross   Enter (answers Claude's question when one is open)
//   Circle  Esc (stops Claude)
//   Square  the keyboard: type, then Enter is sent with it
//   Triangle the menu: sessions, slash commands, snippets, keys, camera
//   D-pad   arrows          L / R  Shift+Tab / Tab
//   Left stick, right stick or a finger: scroll back; Select: back to live
//   Start   the camera;  L+R held: this list of buttons
#include <stdio.h>
#include <string.h>
#include <time.h>

#include <psp2/apputil.h>
#include <psp2/ctrl.h>
#include <psp2/kernel/processmgr.h>
#include <psp2/net/net.h>
#include <psp2/net/netctl.h>
#include <psp2/power.h>
#include <psp2/sysmodule.h>
#include <psp2/rtc.h>
#include <sys/stat.h>

#include "app.h"
#include "chime.h"
#include "ext.h"
#include "log.h"
#include "sound.h"

int _newlib_heap_size_user = 64 * 1024 * 1024;

static unsigned long frames;

unsigned long frame_count(void)
{
    return frames;
}

unsigned long now_us(void)
{
    return (unsigned long)sceKernelGetProcessTimeWide();
}

void app_tick(void)
{
    frames++;
    input_poll();
    link_pump();
    chime_tick();
    audio_tick();
    // A terminal being watched must not put the Vita to sleep.
    if (conn.state == LINK_UP) sceKernelPowerTick(SCE_KERNEL_POWER_TICK_DISABLE_AUTO_SUSPEND);
}

static void net_init(void)
{
    sceSysmoduleLoadModule(SCE_SYSMODULE_NET);
    static char memory[1 * 1024 * 1024];
    SceNetInitParam param;
    param.memory = memory;
    param.size = sizeof(memory);
    param.flags = 0;
    sceNetInit(&param);         // "already initialised" is as good
    sceNetCtlInit();
}

// --------------------------------------------------------------- the bar ---

static const char *claude_word(unsigned st, unsigned *colour)
{
    switch (st) {
    case PROTO_STATE_WORKING:  *colour = COL_ACCENT; return "Claude is working";
    case PROTO_STATE_APPROVAL: *colour = COL_WARN;   return "Claude needs your OK";
    case PROTO_STATE_INPUT:    *colour = COL_WARN;   return "Claude asks you";
    case PROTO_STATE_IDLE:     *colour = COL_GOOD;   return "ready";
    default:                   *colour = COL_DIM;    return "";
    }
}

static void draw_bar(void)
{
    gfx_rect(0, 0, SCREEN_W, BAR_H, 0xFF1C1612u);
    gfx_rect(0, BAR_H - 1, SCREEN_W, 1, 0xFF3A2E26u);

    // Left: where.
    unsigned dot = COL_BAD;
    if (conn.state == LINK_UP) dot = COL_GOOD;
    else if (conn.state == LINK_CONNECTING) dot = COL_WARN;
    gfx_rect(12, 11, 10, 10, dot);
    gfx_text(30, 23, COL_TEXT, 0.95f, conn.server_name[0] ? conn.server_name : "not connected");
    int x = 40 + gfx_text_width(0.95f, conn.server_name[0] ? conn.server_name : "not connected");

    // Middle: what Claude is doing, or the latest news.
    if (conn.status[0] && frames - conn.status_at < 60 * 4) {
        gfx_text(x + 20, 23, COL_DIM, 0.9f, conn.status);
    } else if (conn.state != LINK_UP) {
        gfx_text(x + 20, 23, COL_WARN, 0.9f, conn.status[0] ? conn.status : "connecting...");
    } else {
        unsigned colour;
        const char *word = claude_word(conn.claude_state, &colour);
        if (word[0]) {
            char buf[80];
            if (conn.claude_detail[0]) snprintf(buf, sizeof(buf), "%s: %s", word, conn.claude_detail);
            else snprintf(buf, sizeof(buf), "%s", word);
            gfx_text(x + 20, 23, colour, 0.95f, buf);
        }
    }

    // Right: history, round trip, battery, clock.
    char right[96];
    int n = 0;
    if (conn.lines_back) {
        n += snprintf(right + n, sizeof(right) - n, "history -%u   ", conn.lines_back);
    }
    if (conn.rtt_known) n += snprintf(right + n, sizeof(right) - n, "%ums   ", conn.rtt_ms);
    const int battery = scePowerGetBatteryLifePercent();
    if (battery >= 0) {
        n += snprintf(right + n, sizeof(right) - n, "%d%%%s   ", battery,
                      scePowerIsBatteryCharging() ? "+" : "");
    }
    SceDateTime t;
    if (sceRtcGetCurrentClockLocalTime(&t) >= 0) {
        n += snprintf(right + n, sizeof(right) - n, "%02d:%02d", t.hour, t.minute);
    }
    const int w = gfx_text_width(0.9f, right);
    gfx_text(SCREEN_W - 14 - w, 23, conn.lines_back ? COL_WARN : COL_DIM, 0.9f, right);
}

void app_draw_terminal(void)
{
    draw_bar();
    term_draw();
}

// ----------------------------------------------------------- the terminal ---

static void keyboard(void)
{
    static char text[1024];
    if (!input_text("Message for Claude (Enter is sent after it)", "", text, sizeof(text), 0)) {
        return;
    }
    if (text[0]) link_text(text, strlen(text));
    link_key("\r", 1);
}

// The buttons, shown along the bottom for a while after connecting.
static void draw_help(void)
{
    static const char *const hints[] = {
        "cross", "Enter", "circle", "Esc", "square", "Type", "triangle", "Menu",
        "L", "Mode", "START", "Camera", "SELECT", "Live",
    };
    gfx_hints(hints, 7);
}

static int terminal(void)
{
    int drag_lines = 0;
    unsigned long stick_at = 0;
    unsigned long help_until = frames + 60 * 10;
    for (;;) {
        app_tick();

        if (in.repeat & SCE_CTRL_UP) link_key("\x1b[A", 3);
        if (in.repeat & SCE_CTRL_DOWN) link_key("\x1b[B", 3);
        if (in.repeat & SCE_CTRL_RIGHT) link_key("\x1b[C", 3);
        if (in.repeat & SCE_CTRL_LEFT) link_key("\x1b[D", 3);
        if (in.down & SCE_CTRL_CROSS) link_key("\r", 1);
        if (in.down & SCE_CTRL_CIRCLE) link_key("\x1b", 1);
        // L and R act on release, so holding both (the help) types nothing.
        static unsigned shoulders, chord;
        const unsigned lr = in.held & (SCE_CTRL_LTRIGGER | SCE_CTRL_RTRIGGER);
        if (lr == (SCE_CTRL_LTRIGGER | SCE_CTRL_RTRIGGER)) chord = 1;
        const unsigned released = shoulders & ~lr;
        if (released && !chord) {
            if (released & SCE_CTRL_LTRIGGER) link_key("\x1b[Z", 3);
            if (released & SCE_CTRL_RTRIGGER) link_key("\t", 1);
        }
        if (!lr) chord = 0;
        shoulders = lr;
        if (in.down & SCE_CTRL_SELECT) link_live();
        if (in.down & SCE_CTRL_SQUARE) keyboard();
        if (in.down & SCE_CTRL_START) cam_run();
        if (in.down & SCE_CTRL_TRIANGLE) {
            if (menu_run()) return 1;
        }

        // Scrolling: a stick pushed further scrolls faster.
        const int sy = in.ly ? in.ly : in.ry;
        if (sy && frames - stick_at >= 6) {
            stick_at = frames;
            const int mag = sy < 0 ? -sy : sy;
            const int lines = mag > 110 ? 8 : mag > 70 ? 3 : 1;
            link_scroll(sy < 0 ? -lines : lines);
        }
        // A finger drags the text: down reveals what is above.
        if (in.touching && !in.touch_down) {
            const int moved = (in.ty - in.start_y) / CELL_H - drag_lines;
            if (moved) {
                link_scroll(-moved);
                drag_lines += moved;
            }
        }
        if (!in.touching) drag_lines = 0;

        if (in.down && frames > 60) help_until = frames + 60 * 3 < help_until ? frames + 60 * 3
                                                                             : help_until;
        if (in.held & SCE_CTRL_LTRIGGER && in.held & SCE_CTRL_RTRIGGER) help_until = frames + 2;
        gfx_begin();
        app_draw_terminal();
        if (frames < help_until) draw_help();
        gfx_end();
    }
}

int main(void)
{
    scePowerSetArmClockFrequency(444);
    scePowerSetBusClockFrequency(222);
    mkdir(DATA_ROOT, 0777);

    SceAppUtilInitParam init;
    SceAppUtilBootParam boot;
    memset(&init, 0, sizeof(init));
    memset(&boot, 0, sizeof(boot));
    sceAppUtilInit(&init, &boot);      // the keyboard dialog needs it

    gfx_init();
    input_init();
    net_init();
    log_init();
    ext_init();
    chime_init();
    audio_init();
    term_init();
    quick_commands_init(&commands);
    snippets_init(&snippets);
    char err[64];
    (void)snippets_load(DATA_ROOT "/snippets.txt", &snippets, err, sizeof(err));
    setup_entropy_u32((uint32_t)time(NULL));

    for (;;) {
        char name[32];
        link_stop();
        if (!setup_run(name, sizeof(name))) continue;
        link_start(name);
        terminal();
    }
    return 0;
}
