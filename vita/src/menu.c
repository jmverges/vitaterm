// The Triangle menu, over the terminal: Claude sessions on the computer,
// its slash commands, snippets, keys the buttons do not have, the camera,
// and sound.
#include <stdio.h>
#include <string.h>

#include <psp2/ctrl.h>

#include "app.h"
#include "sound.h"

typedef struct {
    char label[64];
    char note[40];
    unsigned colour;
} item_t;

#define ITEMS_MAX 40

static int list(const char *title, const item_t *items, int n, int *sel, const char *empty)
{
    int top = 0;
    const int visible = 9;
    for (;;) {
        app_tick();
        if (n > 0) {
            if (in.repeat & SCE_CTRL_UP) *sel = (*sel + n - 1) % n;
            if (in.repeat & SCE_CTRL_DOWN) *sel = (*sel + 1) % n;
        }
        if (*sel < top) top = *sel;
        if (*sel >= top + visible) top = *sel - visible + 1;

        const int w = 600, x = SCREEN_W - w - 20, y = BAR_H + 16;
        gfx_begin();
        app_draw_terminal();
        gfx_rect(0, BAR_H, SCREEN_W, SCREEN_H - BAR_H, 0x80000000u);
        gfx_panel(x, y, w, 44 + visible * 42 + 12);
        gfx_text(x + 20, y + 32, COL_ACCENT, 1.2f, title);
        if (n == 0) gfx_wrap(x + 20, y + 52, w - 40, COL_DIM, 1.0f, empty);
        for (int i = top; i < n && i < top + visible; i++) {
            const int ry = y + 44 + (i - top) * 42;
            if (i == *sel) gfx_rect(x + 8, ry, w - 16, 40, COL_PANEL_HI);
            gfx_text(x + 22, ry + 28, items[i].colour ? items[i].colour : COL_TEXT, 1.05f,
                     items[i].label);
            if (items[i].note[0]) {
                const int nw = gfx_text_width(0.85f, items[i].note);
                gfx_text(x + w - 22 - nw, ry + 27, COL_DIM, 0.85f, items[i].note);
            }
            if (in.touch_up && in.tx >= x && in.ty >= ry && in.ty < ry + 40) {
                *sel = i;
                in.down |= SCE_CTRL_CROSS;
            }
        }
        static const char *const hints[] = { "cross", "Choose", "circle", "Back" };
        gfx_hints(hints, 2);
        gfx_end();
        if ((in.down & SCE_CTRL_CROSS) && n > 0) return *sel;
        if (in.down & (SCE_CTRL_CIRCLE | SCE_CTRL_TRIANGLE)) return -1;
    }
}

static void sessions_menu(void)
{
    link_remote_open(1);
    int sel = 0;
    for (;;) {
        item_t items[REMOTE_MAX + 1];
        int n = 0;
        for (unsigned i = 0; i < remote.count && n < REMOTE_MAX; i++) {
            const remote_session_t *s = &remote.items[i];
            snprintf(items[n].label, sizeof(items[n].label), "%s", s->name);
            snprintf(items[n].note, sizeof(items[n].note), "%s%s",
                     remote_state_word(s->state),
                     (s->flags & REMOTE_FLAG_HERE) ? "  (here)" : "");
            items[n].colour = s->state == REMOTE_STATE_NEEDS_YOU ? COL_WARN : 0;
            n++;
        }
        const int sessions = n;
        if (remote.ready) {
            snprintf(items[n].label, sizeof(items[n].label), "Start a new Claude session");
            items[n].note[0] = '\0';
            items[n].colour = COL_ACCENT;
            n++;
        }
        const char *empty = !remote.ready ? "Asking the computer..."
                            : remote.note[0] ? remote.note : "No Claude sessions.";
        const int pick = list("Claude sessions", items, n, &sel,
                              empty);
        if (pick < 0) break;
        if (pick == sessions) {
            link_remote_pick(0, REMOTE_PICK_NEW);
        } else {
            const remote_session_t *s = &remote.items[pick];
            link_remote_pick((unsigned)pick, (s->flags & REMOTE_FLAG_LIVE) || !(s->flags & REMOTE_FLAG_COPY)
                                                 ? REMOTE_PICK_OPEN : REMOTE_PICK_COPY);
        }
        break;
    }
    link_remote_open(0);
}

static void commands_menu(void)
{
    item_t items[QUICK_COMMANDS_MAX];
    int n = 0;
    for (unsigned i = 0; i < commands.count && n < QUICK_COMMANDS_MAX; i++) {
        snprintf(items[n].label, sizeof(items[n].label), "%s", commands.names[i]);
        snprintf(items[n].note, sizeof(items[n].note), "%s",
                 i < quick_commands_builtin() ? "" : "this computer");
        items[n].colour = 0;
        n++;
    }
    static int sel;
    const int pick = list("Slash commands", items, n, &sel, "None yet.");
    // Typed, not sent: Cross on the terminal is the deliberate Enter.
    if (pick >= 0) link_text(commands.names[pick], strlen(commands.names[pick]));
}

static void snippets_menu(void)
{
    item_t items[SNIPPETS_MAX];
    int n = 0;
    for (int i = 0; i < snippets.count && n < SNIPPETS_MAX; i++) {
        snprintf(items[n].label, sizeof(items[n].label), "%s", snippets.items[i].label);
        snprintf(items[n].note, sizeof(items[n].note), "%.30s", snippets.items[i].body);
        items[n].colour = 0;
        n++;
    }
    static int sel;
    const int pick = list("Snippets", items, n, &sel,
                          "No snippets. Put them in ux0:data/vitaterm/snippets.txt, "
                          "or let the computer push its own.");
    if (pick >= 0) link_text(snippets.items[pick].body, strlen(snippets.items[pick].body));
}

static void keys_menu(void)
{
    static const struct { const char *label, *note, *seq; } keys[] = {
        { "Ctrl+C", "interrupt / clear the input", "\x03" },
        { "Esc Esc", "rewind the conversation", "\x1b\x1b" },
        { "Shift+Tab", "switch mode (plan, accept edits)", "\x1b[Z" },
        { "Tab", "complete", "\t" },
        { "Ctrl+R", "search history", "\x12" },
        { "Ctrl+O", "show the full transcript", "\x0f" },
        { "Ctrl+D", "exit", "\x04" },
        { "Ctrl+L", "redraw", "\x0c" },
        { "Backspace", "", "\x7f" },
        { "Home", "", "\x1b[H" },
        { "End", "", "\x1b[F" },
    };
    item_t items[sizeof(keys) / sizeof(keys[0])];
    const int n = (int)(sizeof(keys) / sizeof(keys[0]));
    for (int i = 0; i < n; i++) {
        snprintf(items[i].label, sizeof(items[i].label), "%s", keys[i].label);
        snprintf(items[i].note, sizeof(items[i].note), "%s", keys[i].note);
        items[i].colour = 0;
    }
    static int sel;
    const int pick = list("Keys", items, n, &sel, "");
    if (pick >= 0) link_key(keys[pick].seq, strlen(keys[pick].seq));
}

int menu_run(void)
{
    static int sel;
    for (;;) {
        item_t items[8];
        int n = 0;
        unsigned waiting = remote_needs_you(&remote);
        snprintf(items[n].label, sizeof(items[n].label), "Claude sessions");
        if (waiting) snprintf(items[n].note, sizeof(items[n].note), "%u need you", waiting);
        else items[n].note[0] = '\0';
        items[n++].colour = 0;
        snprintf(items[n].label, sizeof(items[n].label), "Slash commands");
        items[n].note[0] = '\0';
        items[n++].colour = 0;
        snprintf(items[n].label, sizeof(items[n].label), "Snippets");
        items[n].note[0] = '\0';
        items[n++].colour = 0;
        snprintf(items[n].label, sizeof(items[n].label), "Keys");
        snprintf(items[n].note, sizeof(items[n].note), "Ctrl+C, Esc Esc, Tab...");
        items[n++].colour = 0;
        snprintf(items[n].label, sizeof(items[n].label), "Camera");
        snprintf(items[n].note, sizeof(items[n].note), "%s",
                 conn.camera_ready ? "send Claude a picture" : "not on this computer");
        items[n++].colour = 0;
        snprintf(items[n].label, sizeof(items[n].label), "Sound");
        snprintf(items[n].note, sizeof(items[n].note), "%s", sound_muted() ? "off" : "on");
        items[n++].colour = 0;
        snprintf(items[n].label, sizeof(items[n].label), "Other computer");
        snprintf(items[n].note, sizeof(items[n].note), "disconnect");
        items[n++].colour = 0;

        const int pick = list("Menu", items, n, &sel, "");
        switch (pick) {
        case 0: sessions_menu(); return 0;
        case 1: commands_menu(); return 0;
        case 2: snippets_menu(); return 0;
        case 3: keys_menu(); return 0;
        case 4: cam_run(); return 0;
        case 5: sound_set_muted(!sound_muted()); continue;
        case 6: return 1;
        default: return 0;
        }
    }
}
