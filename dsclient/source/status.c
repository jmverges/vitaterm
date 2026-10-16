#include "status.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

static screen_mode_t mode = SCREEN_MODE_CONTROL;
static int view_known;
static unsigned view_back, view_available;
static unsigned link_rtt, link_battery;
static int link_rtt_known, link_battery_known, link_external, link_dsi;
static int link_rssi, link_rssi_known, link_muted;
static int scroll_regime_known = 0;
static int scroll_app_scrolls = 0;

static void pad(char *line)
{
    size_t n = strlen(line);
    if (n > LOG_COLS) n = LOG_COLS;
    memset(line + n, ' ', LOG_COLS - n);
    line[LOG_COLS] = '\0';
}

screen_mode_t status_mode_get(void) { return mode; }
// Derived from the enum, not written out again.  A literal 3 here is how a
// fourth mode gets added and then silently skipped by the one button that
// reaches it.
void status_mode_next(void)
{
    // A mode off the cycle has no successor on it; wrapping (mode + 1) would
    // land somewhere that depends on where SETTINGS happens to sit in the
    // enum, which is not a decision anybody made.  Out means back to the top.
    if (mode >= SCREEN_MODE_CYCLE_COUNT) {
        mode = SCREEN_MODE_CONTROL;
        return;
    }
    mode = (screen_mode_t)((mode + 1) % SCREEN_MODE_CYCLE_COUNT);
}

// ------------------------------------------------------- the STATUS tabs ---

static status_tab_t tab = STATUS_TAB_LOG;

status_tab_t status_tab_get(void) { return tab; }

void status_tab_set(status_tab_t next)
{
    if (next >= STATUS_TAB_LOG && next < STATUS_TAB_COUNT) tab = next;
}

const char *status_tab_name(status_tab_t which)
{
    switch (which) {
    case STATUS_TAB_LOG:  return "LOG";
    case STATUS_TAB_CLIP: return "CLIP";
    case STATUS_TAB_COUNT:
    default:              return "";
    }
}

// The strip spans the screen with the same two-pixel inset the control tiles
// use, divided evenly. Two tabs today; the arithmetic is written for however
// many there are, so a third does not need this function edited.
#define TAB_STRIP_X 2
#define TAB_STRIP_W 252
#define TAB_W (TAB_STRIP_W / STATUS_TAB_COUNT)

int status_tab_rect(status_tab_t which, int *x, int *y, int *w, int *h)
{
    if ((unsigned)which >= STATUS_TAB_COUNT) return 0;
    if (x) *x = TAB_STRIP_X + (int)which * TAB_W;
    if (y) *y = STATUS_TAB_STRIP_Y;
    if (w) *w = TAB_W;
    if (h) *h = STATUS_TAB_STRIP_H;
    return 1;
}

status_tab_t status_tab_hit(int px, int py)
{
    for (int i = 0; i < STATUS_TAB_COUNT; i++) {
        int x, y, w, h;
        (void)status_tab_rect((status_tab_t)i, &x, &y, &w, &h);
        if (px >= x && px < x + w && py >= y && py < y + h) {
            return (status_tab_t)i;
        }
    }
    return STATUS_TAB_COUNT;
}

void status_clip_action_rect(int *x, int *y, int *w, int *h)
{
    if (x) *x = TAB_STRIP_X;
    if (y) *y = STATUS_CLIP_ACTION_Y;
    if (w) *w = TAB_STRIP_W;
    if (h) *h = STATUS_CLIP_ACTION_H;
}

int status_clip_action_hit(int px, int py)
{
    int x, y, w, h;
    status_clip_action_rect(&x, &y, &w, &h);
    return px >= x && px < x + w && py >= y && py < y + h;
}

int status_mode_back(void)
{
    if (mode == SCREEN_MODE_CONTROL) return 0;
    mode = SCREEN_MODE_CONTROL;
    return 1;
}
void status_mode_set(screen_mode_t next)
{
    if (next >= SCREEN_MODE_CONTROL && next < SCREEN_MODE_COUNT) mode = next;
}

void status_link_set(unsigned rtt_ms, int rtt_known,
                     unsigned battery, int battery_known,
                     int external_power, int dsi_mode,
                     int rssi, int rssi_known, int muted)
{
    link_rtt = rtt_ms;
    link_rtt_known = rtt_known;
    link_battery = battery;
    link_battery_known = battery_known;
    link_external = external_power;
    link_dsi = dsi_mode;
    link_rssi = rssi;
    link_rssi_known = rssi_known;
    link_muted = muted;
}

int status_muted(void) { return link_muted; }

static char link_host[STATUS_HOST_MAX + 1];

void status_link_host(const char *name)
{
    snprintf(link_host, sizeof(link_host), "%s", name ? name : "");
}

int status_wifi_bars(int rssi, int dsi_mode)
{
    // DSi mode reports dBm, but through an unsigned byte: -55 dBm arrives as
    // 201.  Read it back as the signed byte it was.  DS mode reports a raw
    // 0-255 strength with no published scale, so it has no bars to give.
    if (!dsi_mode) return -1;
    const int dbm = (int)(int8_t)(uint8_t)(rssi & 0xFF);
    if (dbm >= 0) return -1;              // not a dBm reading at all
    return dbm >= -55 ? 4 : dbm >= -65 ? 3 : dbm >= -75 ? 2 : 1;
}

unsigned status_battery_bars(unsigned level)
{
    // The DSi's own menu draws these readings as four bars: 15 full, then
    // 11, 7, 3, and 1 or 0 as empty.
    return level >= 15 ? 4 : level >= 11 ? 3 : level >= 7 ? 2 : level >= 3 ? 1 : 0;
}

void status_format_link(char line[LOG_COLS + 1])
{
    char rtt[12], battery[16], wifi[20];
    if (link_rtt_known) snprintf(rtt, sizeof(rtt), "%ums", link_rtt);
    else snprintf(rtt, sizeof(rtt), "--ms");
    if (!link_battery_known) snprintf(battery, sizeof(battery), "bat --");
    else if (link_dsi) snprintf(battery, sizeof(battery), "bat %u/4%s",
                                status_battery_bars(link_battery),
                                link_external ? "+" : "");
    else snprintf(battery, sizeof(battery), "bat %s%s",
                  link_battery <= 3 ? "LOW" : "ok", link_external ? "+" : "");
    const int bars = link_rssi_known ? status_wifi_bars(link_rssi, link_dsi) : -2;
    if (bars >= 0) snprintf(wifi, sizeof(wifi), "wifi %u/4", (unsigned)bars & 7u);
    else if (bars == -1) snprintf(wifi, sizeof(wifi), "wifi ok");
    else snprintf(wifi, sizeof(wifi), "wifi --");

    // Composed into a wide buffer and then trimmed, rather than formatted
    // straight into a 42-column line. Every part is individually bounded and
    // the four together cannot fill the row, but gcc cannot see that and warns
    // about a truncation that the padding below would make harmless anyway.
    // A warning on a status line is noise that hides the next real one.
    //
    // Where the card is connected comes first: with REMOTE able to move it
    // between computers, "which one am I on" is the first question.
    char all[80];
    snprintf(all, sizeof(all), "%s%s%s  %s  %s%s",
             link_host, link_host[0] ? "  " : "", rtt, wifi, battery,
             link_muted ? "  MUTE" : "");
    size_t n = strlen(all);
    if (n > LOG_COLS) n = LOG_COLS;
    memcpy(line, all, n);
    line[n] = '\0';
    pad(line);
}

void status_format_counters(unsigned long rx, unsigned long msgs,
                            unsigned long tx, unsigned long loops,
                            unsigned raw, unsigned lib,
                            char line1[LOG_COLS + 1], char line2[LOG_COLS + 1])
{
    snprintf(line1, LOG_COLS + 1, "in %lu B  %lu msgs  sent %lu", rx, msgs, tx);
    snprintf(line2, LOG_COLS + 1, "loop %lu  keys raw %03X lib %03X", loops, raw, lib);
    pad(line1);
    pad(line2);
}

void status_scroll_regime_set(int app_scrolls)
{
    scroll_regime_known = 1;
    scroll_app_scrolls = app_scrolls ? 1 : 0;
}

void status_scroll_regime_clear(void)
{
    scroll_regime_known = 0;
    scroll_app_scrolls = 0;
}

void status_view_pos_clear(void)
{
    view_known = 0;
    view_back = view_available = 0;
}

void status_view_pos_set(unsigned lines_back, unsigned available)
{
    view_known = 1;
    view_back = lines_back;
    view_available = available;
}

int status_view_pos_get(unsigned *lines_back, unsigned *available)
{
    if (!view_known) return 0;
    if (lines_back) *lines_back = view_back;
    if (available) *available = view_available;
    return 1;
}

void status_format_view_pos(char line[LOG_COLS + 1])
{
    // While the application owns its scrollback the position readout would
    // always report "no history" -- the server has nothing to move through --
    // so the line says which regime is live instead. This is the regime
    // indicator: it makes the scroll gesture legible before a press, instead
    // of leaving the user to discover PageUp by what happened on screen.
    //
    // It speaks only when there is something to say.  At the live edge, with
    // nothing to scroll or an application scrolling for itself, the line is
    // empty: a readout that always says "0 of 0" teaches you to stop reading
    // it, and then misses the one time it mattered.
    if (scroll_regime_known && scroll_app_scrolls) {
        line[0] = '\0';
    } else if (!view_known || view_available == 0 || view_back == 0) {
        line[0] = '\0';
    } else {
        // Through a wide buffer: gcc sizes %u at ten digits and would warn
        // about a truncation the wire's 16-bit values can never cause.
        char wide[80];
        snprintf(wide, sizeof(wide), "%u of %u lines up - LIVE returns",
                 view_back, view_available);
        snprintf(line, LOG_COLS + 1, "%.42s", wide);
    }
    pad(line);
}
