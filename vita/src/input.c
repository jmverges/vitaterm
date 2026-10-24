// Buttons, sticks, the front touch panel and the system keyboard.
#include <stdio.h>
#include <string.h>

#include <psp2/ctrl.h>
#include <psp2/ime_dialog.h>
#include <psp2/touch.h>
#include <psp2/common_dialog.h>

#include "app.h"

input_t in;

#define REPEAT_DELAY 18     // frames before a held button repeats
#define REPEAT_RATE 4

static unsigned held_frames[32];

void input_init(void)
{
    sceCtrlSetSamplingMode(SCE_CTRL_MODE_ANALOG);
    sceTouchSetSamplingState(SCE_TOUCH_PORT_FRONT, SCE_TOUCH_SAMPLING_STATE_START);
    memset(&in, 0, sizeof(in));
}

static int stick(unsigned char v)
{
    const int d = (int)v - 128;
    if (d > -28 && d < 28) return 0;
    return d;
}

void input_poll(void)
{
    SceCtrlData pad;
    memset(&pad, 0, sizeof(pad));
    sceCtrlPeekBufferPositive(0, &pad, 1);
#ifdef VITA_SCRIPT
    // Test builds only: buttons from ux0:data/vitaterm/script.txt, one
    // "frame hex-buttons" per line, for an emulator that takes none.
    {
        static unsigned at[64], buttons[64];
        static int count = -1;
        static unsigned frame;
        if (count < 0) {
            count = 0;
            FILE *f = fopen(DATA_ROOT "/script.txt", "r");
            if (f) {
                while (count < 64 && fscanf(f, "%u %x", &at[count], &buttons[count]) == 2) count++;
                fclose(f);
            }
        }
        frame++;
        for (int i = 0; i < count; i++) {
            if (frame >= at[i] && frame < at[i] + 6) pad.buttons |= buttons[i];
        }
    }
#endif
    const unsigned now = pad.buttons;
    in.down = now & ~in.held;
    in.repeat = in.down;
    for (int b = 0; b < 32; b++) {
        if (now & (1u << b)) {
            held_frames[b]++;
            if (held_frames[b] > REPEAT_DELAY &&
                (held_frames[b] - REPEAT_DELAY) % REPEAT_RATE == 0) {
                in.repeat |= 1u << b;
            }
        } else {
            held_frames[b] = 0;
        }
    }
    in.held = now;
    in.lx = stick(pad.lx);
    in.ly = stick(pad.ly);
    in.rx = stick(pad.rx);
    in.ry = stick(pad.ry);

    SceTouchData touch;
    memset(&touch, 0, sizeof(touch));
    sceTouchPeek(SCE_TOUCH_PORT_FRONT, &touch, 1);
    const int was = in.touching;
    in.touching = touch.reportNum > 0;
    in.touch_down = in.touching && !was;
    in.touch_up = !in.touching && was;
    if (in.touching) {
        // The panel reports 1920x1088 for the 960x544 screen.
        in.tx = touch.report[0].x / 2;
        in.ty = touch.report[0].y / 2;
        if (in.touch_down) {
            in.start_x = in.tx;
            in.start_y = in.ty;
        }
    }
    setup_entropy_u32(now ^ ((unsigned)pad.lx << 8) ^ ((unsigned)pad.ry << 16) ^
                      (unsigned)touch.report[0].x);
}

// UTF-8 <-> UTF-16 for the dialog.  The PTY takes UTF-8 as typed: Claude
// reads accents fine, and only the card's own font folds them for display.
static void utf8_to_16(const char *s, uint16_t *out, size_t cap)
{
    size_t n = 0;
    const unsigned char *p = (const unsigned char *)s;
    while (*p && n + 1 < cap) {
        unsigned c = *p++;
        if (c >= 0xC0 && c < 0xE0 && *p) c = ((c & 0x1F) << 6) | (*p++ & 0x3F);
        else if (c >= 0xE0 && p[0] && p[1]) {
            c = ((c & 0x0F) << 12) | ((p[0] & 0x3F) << 6) | (p[1] & 0x3F);
            p += 2;
        }
        out[n++] = (uint16_t)c;
    }
    out[n] = 0;
}

static void utf16_to_8(const uint16_t *s, char *out, size_t cap)
{
    size_t n = 0;
    for (; *s; s++) {
        const unsigned c = *s;
        if (c < 0x80) {
            if (n + 1 >= cap) break;
            // Line breaks from the dialog become spaces: Enter is a separate,
            // deliberate press.
            out[n++] = (c == '\n' || c == '\r') ? ' ' : (char)c;
        } else if (c < 0x800) {
            if (n + 2 >= cap) break;
            out[n++] = (char)(0xC0 | (c >> 6));
            out[n++] = (char)(0x80 | (c & 0x3F));
        } else {
            if (n + 3 >= cap) break;
            out[n++] = (char)(0xE0 | (c >> 12));
            out[n++] = (char)(0x80 | ((c >> 6) & 0x3F));
            out[n++] = (char)(0x80 | (c & 0x3F));
        }
    }
    out[n] = '\0';
}

int input_text(const char *title, const char *initial, char *out, size_t cap,
               int numeric)
{
    static uint16_t title16[SCE_IME_DIALOG_MAX_TITLE_LENGTH];
    static uint16_t init16[SCE_IME_DIALOG_MAX_TEXT_LENGTH + 1];
    static uint16_t buf16[SCE_IME_DIALOG_MAX_TEXT_LENGTH + 1];
    utf8_to_16(title, title16, SCE_IME_DIALOG_MAX_TITLE_LENGTH);
    utf8_to_16(initial ? initial : "", init16, SCE_IME_DIALOG_MAX_TEXT_LENGTH);
    memset(buf16, 0, sizeof(buf16));

    SceImeDialogParam param;
    sceImeDialogParamInit(&param);
    param.supportedLanguages = 0;
    param.languagesForced = SCE_FALSE;
    param.type = numeric ? SCE_IME_TYPE_NUMBER : SCE_IME_TYPE_DEFAULT;
    param.option = numeric ? 0 : SCE_IME_OPTION_MULTILINE;
    param.title = title16;
    param.maxTextLength = cap / 3 < SCE_IME_DIALOG_MAX_TEXT_LENGTH
                              ? (SceUInt32)(cap / 3) : SCE_IME_DIALOG_MAX_TEXT_LENGTH;
    if (param.maxTextLength < 1) param.maxTextLength = 1;
    param.initialText = init16;
    param.inputTextBuffer = buf16;
    if (sceImeDialogInit(&param) < 0) return 0;

    int ok = 0;
    for (;;) {
        const SceCommonDialogStatus st = sceImeDialogGetStatus();
        if (st == SCE_COMMON_DIALOG_STATUS_FINISHED) {
            SceImeDialogResult result;
            memset(&result, 0, sizeof(result));
            sceImeDialogGetResult(&result);
            ok = result.button == SCE_IME_DIALOG_BUTTON_ENTER;
            break;
        }
        if (st == SCE_COMMON_DIALOG_STATUS_NONE) break;
        app_tick();
        gfx_begin();
        app_draw_terminal();
        gfx_end();
    }
    sceImeDialogTerm();
    // Whatever was held to open the keyboard must not act again on return.
    input_poll();
    in.down = in.repeat = 0;
    if (ok) utf16_to_8(buf16, out, cap);
    return ok;
}
