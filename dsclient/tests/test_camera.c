// The camera's pure half: the same functions the DSi build runs, tested on
// the host and pinned against dsterm/camera.py so the card and the computer
// cannot drift apart.
#include "../source/camera.h"
#include "../source/proto.h"
#include "../source/sha256.h"
#include "check.h"
#include <stdio.h>
#include <string.h>

// check.h's CHECK needs a message; the FAIL line already names the file and
// line, and gcc refuses a zero-length format, so this prints an empty %s.
#define OK(cond) CHECK(cond, "%s", "")

static void hex(const uint8_t *data, size_t len)
{
    for (size_t i = 0; i < len; i++) printf("%02x", data[i]);
}

// The synthetic frame the Python reference generates identically:
// r=(x*31/255), g=(y*31/191), b=((x+y)&31), bit 15 set.
static void synth_frame(uint16_t frame[CAMERA_SRC_W * CAMERA_SRC_H])
{
    for (int y = 0; y < CAMERA_SRC_H; y++) {
        for (int x = 0; x < CAMERA_SRC_W; x++) {
            frame[y * CAMERA_SRC_W + x] =
                (uint16_t)(0x8000 | (x * 31 / 255) | ((y * 31 / 191) << 5) |
                           (((x + y) & 31) << 10));
        }
    }
}

static unsigned level_at(const uint8_t *packed, int x, int y)
{
    const uint8_t v = packed[(y * CAMERA_PREVIEW_W + x) / 2];
    return (x & 1) ? (v & 15u) : (unsigned)(v >> 4);
}

static void test_downscale_solid_and_bounds(void)
{
    static uint16_t src[CAMERA_SRC_W * CAMERA_SRC_H];
    static uint8_t dst[CAMERA_PREVIEW_BYTES];

    memset(src, 0, sizeof(src));        // black, bit 15 ignored
    for (size_t i = 0; i < 192; i++) src[i] |= 0x8000;
    camera_downscale_preview(src, dst);
    OK(level_at(dst, 0, 0) == 0 && level_at(dst, 127, 95) == 0);

    // White is the top level everywhere; a mid grey a middle one.
    for (size_t i = 0; i < sizeof(src) / sizeof(*src); i++) src[i] = 0x7FFF;
    camera_downscale_preview(src, dst);
    for (size_t i = 0; i < CAMERA_PREVIEW_BYTES; i++) OK(dst[i] == 0xFF);
    for (size_t i = 0; i < sizeof(src) / sizeof(*src); i++)
        src[i] = 0x8000 | (16 | 16 << 5 | 16 << 10);
    camera_downscale_preview(src, dst);
    OK(level_at(dst, 5, 5) == 8 && level_at(dst, 6, 5) == 8);

    // Each output pixel is its own 2x2 block: one white block in black
    // lights exactly one level, in the right nibble of the right byte.
    memset(src, 0, sizeof(src));
    const int bx = 7, by = 3;           // odd x: the low nibble
    for (int y = 2 * by; y < 2 * by + 2; y++)
        for (int x = 2 * bx; x < 2 * bx + 2; x++) src[y * CAMERA_SRC_W + x] = 0x7FFF;
    camera_downscale_preview(src, dst);
    for (int y = 0; y < CAMERA_PREVIEW_H; y++)
        for (int x = 0; x < CAMERA_PREVIEW_W; x++)
            OK(level_at(dst, x, y) == ((x == bx && y == by) ? 15u : 0u));

    // Luma, not one channel: pure green is brighter than pure blue.
    for (size_t i = 0; i < sizeof(src) / sizeof(*src); i++) src[i] = 31 << 5;
    camera_downscale_preview(src, dst);
    const unsigned green = level_at(dst, 0, 0);
    for (size_t i = 0; i < sizeof(src) / sizeof(*src); i++) src[i] = 31 << 10;
    camera_downscale_preview(src, dst);
    OK(green > level_at(dst, 0, 0));

    camera_downscale_preview(NULL, dst);
    camera_downscale_preview(src, NULL);
}

static void test_colour_view(void)
{
    static uint16_t src[CAMERA_SRC_W * CAMERA_SRC_H];
    static uint8_t dst[CAMERA_COLOUR_BYTES];
    // Pure red, green and blue land on the top of their own field only.
    const struct { uint16_t px; uint8_t want; } solid[] = {
        { 31, 0xE0 }, { 31 << 5, 0x1C }, { 31 << 10, 0x03 }, { 0x7FFF, 0xFF },
        { 0, 0x00 },
    };
    for (size_t k = 0; k < sizeof(solid) / sizeof(solid[0]); k++) {
        for (size_t i = 0; i < sizeof(src) / sizeof(*src); i++) src[i] = solid[k].px;
        camera_downscale_colour(src, dst);
        OK(dst[0] == solid[k].want && dst[CAMERA_COLOUR_BYTES - 1] == solid[k].want);
    }
    uint8_t r, g, b;
    camera_rgb332_to_rgb888(0xFF, &r, &g, &b);
    OK(r == 255 && g == 255 && b == 255);
    camera_rgb332_to_rgb888(0x20, &r, NULL, NULL);       // nullable outs
    OK(r == (uint8_t)((255 + 3) / 7));
    OK(camera_view_bytes(CAMERA_VIEW_GREY) == 6144 &&
       camera_view_bytes(CAMERA_VIEW_COLOUR) == 6912 && camera_view_bytes(1) == 0);
    OK(camera_view_downscale(9, src, dst) == 0);
    camera_downscale_colour(NULL, dst);
    camera_downscale_colour(src, NULL);
}

static void test_gray4_expand(void)
{
    OK(camera_gray4_to_8(0) == 0);
    OK(camera_gray4_to_8(15) == 255);
    OK(camera_gray4_to_8(8) == 136);
    OK(camera_gray4_to_8(0x1F) == 255);    // only the low nibble counts
}

static void test_yuyv_formula(void)
{
    uint8_t r, g, b;
    // Neutral grey: Cb = Cr = 0 after the -0x80 shift.
    camera_yuyv_to_rgb888(128, 0, 0, &r, &g, &b);
    OK(r == 128 && g == 128 && b == 128);
    camera_yuyv_to_rgb888(0, 0, 0, &r, &g, &b);
    OK(r == 0 && g == 0 && b == 0);
    // Clamps at both ends of every channel.  These values are what the
    // formula produces -- the cam-yuyv INTEROP vector pins the same list.
    camera_yuyv_to_rgb888(0, -128, -128, &r, &g, &b);
    OK(r == 0 && g == 136 && b == 0);
    camera_yuyv_to_rgb888(0, 0, 127, &r, &g, &b);
    OK(r == 176 && g == 0 && b == 0);
    camera_yuyv_to_rgb888(0, 127, 0, &r, &g, &b);
    OK(r == 0 && g == 0 && b == 222);
    camera_yuyv_to_rgb888(255, 127, 127, &r, &g, &b);
    OK(r == 255 && g == 126 && b == 255);
    camera_yuyv_to_rgb888(255, -128, -128, &r, &g, &b);
    OK(r == 75 && g == 255 && b == 29);
    // A negative shift goes through the portable sar(), not a host quirk:
    // cr = -1 => -1 + (-1>>2)=-1 + (-1>>3)=-1 + (-1>>5)=-1 => r = 100-4.
    camera_yuyv_to_rgb888(100, 0, -1, &r, &g, &b);
    OK(r == 96);
}

static void test_thumbnail_picks_right_pixels(void)
{
    static uint8_t photo[CAMERA_PHOTO_BYTES];
    static uint16_t thumb[CAMERA_SRC_W * CAMERA_SRC_H];
    memset(photo, 0x80, sizeof(photo));
    // Colour every pixel: Y ramps with x so even/odd Y selection shows.
    for (int y = 0; y < CAMERA_PHOTO_H; y++) {
        for (int p = 0; p < CAMERA_PHOTO_W / 2; p++) {
            uint8_t *pair = photo + ((size_t)y * 320 + p) * 4;
            pair[0] = (uint8_t)((p * 2) & 0xFF);        // Y0
            pair[1] = 0x80;                              // Cb -> 0
            pair[2] = (uint8_t)((p * 2 + 1) & 0xFF);    // Y1
            pair[3] = 0x80;                              // Cr -> 0
        }
    }
    camera_photo_thumbnail(photo, thumb);
    // dx=0 maps to sx=0 (Y0 of pair 0) => 0; bit 15 set.
    OK(thumb[0] == (uint16_t)0x8000);
    // dx=255 -> sx=(255*640)/256=637, odd: Y1 of pair 318, written as
    // (2*318+1)&0xFF = 637&0xFF.
    const int exp_y = 637 & 0xFF;
    OK(thumb[255] == (uint16_t)(0x8000 | ((exp_y >> 3) & 31) |
                                   (((exp_y >> 3) & 31) << 5) |
                                   (((exp_y >> 3) & 31) << 10)));
    camera_photo_thumbnail(NULL, thumb);
    camera_photo_thumbnail(photo, NULL);
}

// The synthetic capture dsterm/camera.py's synth_yuyv_frame() writes:
// Y0 = 2p+3y, Cb = p+5y, Y1 = 2p+1+3y, Cr = 7p+y (all mod 256).
static void synth_yuyv(uint8_t yuyv[CAMERA_PHOTO_BYTES])
{
    for (int y = 0; y < CAMERA_PHOTO_H; y++) {
        for (int p = 0; p < CAMERA_PHOTO_W / 2; p++) {
            uint8_t *pair = yuyv + ((size_t)y * (CAMERA_PHOTO_W / 2) + p) * 4;
            pair[0] = (uint8_t)(p * 2 + y * 3);
            pair[1] = (uint8_t)(p + y * 5);
            pair[2] = (uint8_t)(p * 2 + 1 + y * 3);
            pair[3] = (uint8_t)(p * 7 + y);
        }
    }
}

static void test_photo_pack(void)
{
    static uint8_t yuyv[CAMERA_PHOTO_BYTES];
    static uint8_t packed[CAMERA_PHOTO_BYTES_MAX];
    synth_yuyv(yuyv);

    // FULL: the luma is carried whole, the chroma halved vertically.
    OK(camera_photo_bytes(CAMERA_PHOTO_FORMAT_FULL) == 460800);
    OK(camera_photo_pack(yuyv, CAMERA_PHOTO_FORMAT_FULL, packed) == 460800);
    // Y(0,0)=0, Y(1,0)=1, Y(639,0)=639&0xFF=127, Y(0,479)=(0+3*479)&0xFF.
    OK(packed[0] == 0 && packed[1] == 1 && packed[639] == 127);
    OK(packed[479 * 640] == (uint8_t)(3 * 479));
    // Cb(0,0) averages rows 0 and 1 of pair 0: (0 + 5 + 1)>>1 = 3.
    OK(packed[640 * 480] == 3);
    // Cr(319,239): pair 319 on rows 478/479, whose bytes are (7*319+478)&0xFF
    // and (7*319+479)&0xFF = 151, 152 -> (151+152+1)>>1 = 152.
    OK(packed[640 * 480 + 320 * 240 + 239 * 320 + 319] == 152);

    // SMALL: every plane quarters.
    OK(camera_photo_bytes(CAMERA_PHOTO_FORMAT_SMALL) == 115200);
    OK(camera_photo_pack(yuyv, CAMERA_PHOTO_FORMAT_SMALL, packed) == 115200);
    // Y(0,0) = (0 + 1 + 3 + 4 + 2)>>2 = 2; Y(319,239) checks the far corner.
    OK(packed[0] == 2);
    {
        const int x = 319, y = 239;
        const int p = x;   /* luma block is pair x on rows 2y, 2y+1 */
        int sum = ((p * 2 + (2 * y) * 3) & 0xFF) + ((p * 2 + 1 + (2 * y) * 3) & 0xFF)
                + ((p * 2 + (2 * y + 1) * 3) & 0xFF)
                + ((p * 2 + 1 + (2 * y + 1) * 3) & 0xFF);
        OK(packed[y * 320 + x] == ((sum + 2) >> 2));
    }
    // Cb(0,0) averages pairs 0,1 on rows 0,1: (0 + 1 + 5 + 6 + 2)>>2 = 3.
    OK(packed[320 * 240] == 3);

    OK(camera_photo_pack(yuyv, 0, packed) == 0);
    OK(camera_photo_pack(yuyv, 3, packed) == 0);
    OK(camera_photo_pack(NULL, CAMERA_PHOTO_FORMAT_FULL, packed) == 0);
    OK(camera_photo_pack(yuyv, CAMERA_PHOTO_FORMAT_FULL, NULL) == 0);
    OK(camera_photo_bytes(0) == 0);
}

static void test_photo_preview_composes(void)
{
    static uint8_t photo[CAMERA_PHOTO_BYTES];
    static uint16_t scratch[CAMERA_SRC_W * CAMERA_SRC_H];
    static uint8_t prev[CAMERA_VIEW_BYTES_MAX];
    memset(photo, 0, sizeof(photo));
    for (size_t i = 1; i < sizeof(photo); i += 2) photo[i] = 0x80;
    OK(camera_photo_preview(photo, scratch, CAMERA_VIEW_GREY, prev) ==
       CAMERA_PREVIEW_BYTES);
    for (size_t i = 0; i < CAMERA_PREVIEW_BYTES; i++) OK(prev[i] == 0);
    OK(camera_photo_preview(photo, scratch, CAMERA_VIEW_COLOUR, prev) ==
       CAMERA_COLOUR_BYTES);
    for (size_t i = 0; i < CAMERA_COLOUR_BYTES; i++) OK(prev[i] == 0);
    OK(camera_photo_preview(photo, NULL, CAMERA_VIEW_GREY, prev) == 0);
    OK(camera_photo_preview(photo, scratch, 1, prev) == 0);     // the old format
}

static void test_crc32(void)
{
    OK(camera_crc32_update(0, (const uint8_t *)"123456789", 9) == 0xCBF43926u);
    // Incremental matches one-shot.
    uint8_t buf[100];
    for (int i = 0; i < 100; i++) buf[i] = (uint8_t)(i * 7);
    uint32_t a = camera_crc32_update(0, buf, 100);
    uint32_t b = camera_crc32_update(0, buf, 30);
    b = camera_crc32_update(b, buf + 30, 70);
    OK(a == b);
    OK(camera_crc32_update(0, buf, 0) == 0);
    // Every table entry is the bitwise definition's step for that byte.
    for (int i = 0; i < 256; i++) {
        const uint8_t byte = (uint8_t)i;
        uint32_t c = 0xFFFFFFFFu ^ byte;
        for (int bit = 0; bit < 8; bit++) {
            c = (c & 1) ? (c >> 1) ^ 0xEDB88320u : c >> 1;
        }
        OK(camera_crc32_update(0, &byte, 1) == ~c);
    }
}

static void test_pacer(void)
{
    camera_pacer_t p;
    camera_pacer_init(&p);
    OK(camera_pacer_ready(&p, 0));            // nothing sent yet: ready
    camera_pacer_sent(&p, 0);
    OK(!camera_pacer_ready(&p, 100));         // in flight wins over spacing
    OK(p.seq == 1);
    OK(!camera_pacer_ack(&p, 0));             // wrong seq: ignored
    OK(p.in_flight);
    OK(camera_pacer_ack(&p, 1));
    OK(!p.in_flight);
    // The spacing is CAMERA_PREVIEW_INTERVAL_FRAMES (3 fps at 60 Hz).
    const uint32_t gap = CAMERA_PREVIEW_INTERVAL_FRAMES;
    OK(!camera_pacer_ready(&p, gap - 1));
    OK(camera_pacer_ready(&p, gap));
    camera_pacer_sent(&p, gap);
    OK(p.seq == 2);
    camera_pacer_ack(&p, 2);
    OK(camera_pacer_ready(&p, 2 * gap));
    // Acking the same seq twice: the second is stale, returns 0.
    OK(!camera_pacer_ack(&p, 2));
    // Seq wraps at 256.
    p.seq = 255;
    camera_pacer_sent(&p, 30);
    OK(p.seq == 0);
    OK(!camera_pacer_ready(NULL, 0));
}

static void test_upload(void)
{
    camera_upload_t u;
    uint32_t off, len;
    camera_upload_init(&u, CAMERA_PHOTO_FORMAT_FULL);
    OK(u.total == 460800u);
    // Eight 1 KB chunks fill the window; the ninth waits for credit.
    for (int i = 0; i < 8; i++) {
        OK(camera_upload_next(&u, &off, &len));
        OK(off == (uint32_t)i * 1024 && len == 1024);
    }
    OK(!camera_upload_next(&u, &off, &len));
    // Credit rules: cumulative byte counts.  Above `sent` is a fault; a
    // smaller or repeated value is legal -- it is where the count already
    // sits or credits nothing new.
    OK(!camera_upload_ack(&u, 9000));
    OK(camera_upload_ack(&u, 100));
    OK(camera_upload_next(&u, &off, &len));
    OK(off == 8192 && len == 1024);
    OK(!camera_upload_next(&u, &off, &len));   // window full again
    OK(!camera_upload_ack(&u, 50));            // below `acked`: a fault
    OK(camera_upload_ack(&u, 2048));
    OK(camera_upload_next(&u, &off, &len) && off == 9216);
    // Drain to the end: each window-full must be credited before more bytes
    // move, the way the live upload paces itself on ACKs.
    for (;;) {
        while (camera_upload_next(&u, &off, &len)) { }
        if (camera_upload_all_sent(&u)) break;
        OK(camera_upload_ack(&u, u.sent));
    }
    OK(camera_upload_all_sent(&u));
    // 460800 is exactly 450 chunks, so the last one is full.
    OK(off == 460800 - 1024 && len == 1024);
    camera_upload_ack(&u, 460800);
    OK(camera_upload_percent(&u) == 100);
    // The small picture is the same planner with a quarter of the bytes.
    camera_upload_init(&u, CAMERA_PHOTO_FORMAT_SMALL);
    OK(u.total == 115200u);
    OK(camera_upload_percent(&u) == 0);
    // A format that is not one totals zero: nothing can ever be sent.
    camera_upload_init(&u, 0);
    OK(!camera_upload_next(&u, &off, &len));
    OK(!camera_upload_next(NULL, &off, &len));
}

static void test_shutter(void)
{
    camera_shutter_state_t s;
    camera_shutter_reset(&s);
    // Press, hold, release: fires once.
    OK(!camera_shutter_input(&s, CAMERA_KEY_R, CAMERA_KEY_L,
                                CAMERA_KEY_R, CAMERA_KEY_R));
    OK(!camera_shutter_input(&s, CAMERA_KEY_R, CAMERA_KEY_L,
                                CAMERA_KEY_R, 0));
    OK(camera_shutter_input(&s, CAMERA_KEY_R, CAMERA_KEY_L, 0, 0));
    // The chord that wasn't: R goes down, L joins for a frame, R releases
    // -- spoiled, no picture, and the state is clean for the next press.
    OK(!camera_shutter_input(&s, CAMERA_KEY_R, CAMERA_KEY_L,
                                CAMERA_KEY_R, CAMERA_KEY_R));
    OK(!camera_shutter_input(&s, CAMERA_KEY_R, CAMERA_KEY_L,
                                CAMERA_KEY_R | CAMERA_KEY_L, CAMERA_KEY_L));
    OK(!camera_shutter_input(&s, CAMERA_KEY_R, CAMERA_KEY_L,
                                CAMERA_KEY_L, 0));
    // A fresh press after the spoiled one: the edge arms, the release fires.
    OK(!camera_shutter_input(&s, CAMERA_KEY_R, CAMERA_KEY_L,
                               CAMERA_KEY_R, CAMERA_KEY_R));
    OK(camera_shutter_input(&s, CAMERA_KEY_R, CAMERA_KEY_L, 0, 0));
    // The shoulder already held on the press edge spoils immediately.
    OK(!camera_shutter_input(&s, CAMERA_KEY_L, CAMERA_KEY_R,
                                CAMERA_KEY_R | CAMERA_KEY_L, CAMERA_KEY_L));
    OK(!camera_shutter_input(&s, CAMERA_KEY_L, CAMERA_KEY_R, 0, 0));
    // A as the shutter spoils on nothing.
    OK(!camera_shutter_input(&s, 1u, 0, 1u, 1u));
    OK(camera_shutter_input(&s, 1u, 0, 0, 0));
    // A release with no press armed fires nothing.
    OK(!camera_shutter_input(&s, CAMERA_KEY_R, CAMERA_KEY_L, 0, 0));
    OK(!camera_shutter_input(NULL, 1, 0, 0, 0));
}

static void test_lr_chord(void)
{
    OK(camera_lr_chord(CAMERA_KEY_L | CAMERA_KEY_R, CAMERA_KEY_L));
    OK(camera_lr_chord(CAMERA_KEY_L | CAMERA_KEY_R, CAMERA_KEY_R));
    OK(!camera_lr_chord(CAMERA_KEY_L | CAMERA_KEY_R, 0));   // held, no edge
    OK(!camera_lr_chord(CAMERA_KEY_L, CAMERA_KEY_L));       // only one
    OK(!camera_lr_chord(0, 0));
}

static void test_ui_state_and_strips(void)
{
    OK(camera_ui_leave() == CAMERA_UI_OFF);
    OK(camera_ui_live(CAMERA_UI_STARTING) == CAMERA_UI_LIVE);
    OK(camera_ui_live(CAMERA_UI_LIVE) == CAMERA_UI_LIVE);
    OK(camera_ui_shot(CAMERA_UI_LIVE) == CAMERA_UI_REVIEW);
    OK(camera_ui_shot(CAMERA_UI_OFF) == CAMERA_UI_OFF);
    OK(camera_ui_send(CAMERA_UI_REVIEW) == CAMERA_UI_SENDING);
    OK(camera_ui_send(CAMERA_UI_LIVE) == CAMERA_UI_LIVE);
    OK(camera_ui_retake(CAMERA_UI_REVIEW) == CAMERA_UI_LIVE);
    OK(camera_ui_sending(CAMERA_UI_SENDING) == CAMERA_UI_SAVING);
    OK(camera_ui_saved(CAMERA_UI_SAVING) == CAMERA_UI_OFF);
    OK(camera_ui_failed(CAMERA_UI_SENDING) == CAMERA_UI_FAILED);
    OK(camera_ui_failed(CAMERA_UI_OFF) == CAMERA_UI_OFF);
    OK(camera_ui_is_error(CAMERA_UI_FAILED));
    OK(!camera_ui_is_error(CAMERA_UI_LIVE));

    char line[64];
    camera_ui_top(CAMERA_UI_LIVE, 2, line, sizeof(line));
    OK(strcmp(line, "\xa6 CAMERA ON  OUTER") == 0);
    camera_ui_top(CAMERA_UI_LIVE, 1, line, sizeof(line));
    OK(strcmp(line, "\xa6 CAMERA ON  INNER") == 0);
    camera_ui_top(CAMERA_UI_OFF, 1, line, sizeof(line));
    OK(line[0] == '\0');
    camera_ui_hint(CAMERA_UI_LIVE, "R", 0, line, sizeof(line));
    OK(strcmp(line, "R take  Y flip  B back") == 0);
    camera_ui_hint(CAMERA_UI_LIVE, "L", 0, line, sizeof(line));
    OK(line[0] == 'L');
    camera_ui_hint(CAMERA_UI_REVIEW, "R", 0, line, sizeof(line));
    OK(strcmp(line, "A send to computer  B retake") == 0);
    camera_ui_hint(CAMERA_UI_SENDING, "R", 42, line, sizeof(line));
    OK(strcmp(line, "sending 42%   B cancel") == 0);
    camera_ui_hint(CAMERA_UI_SAVING, "R", 0, line, sizeof(line));
    OK(strcmp(line, "saving on the computer...  B cancel") == 0);
    camera_ui_hint(CAMERA_UI_FAILED, "R", 0, line, sizeof(line));
    OK(strcmp(line, "camera failed  B back") == 0);
    camera_ui_hint(CAMERA_UI_STARTING, "R", 0, line, sizeof(line));
    OK(strcmp(line, "starting the camera...") == 0);
    // Every line fits a 42-column row.
    for (camera_ui_t st = CAMERA_UI_STARTING; st <= CAMERA_UI_FAILED;
         st = (camera_ui_t)(st + 1)) {
        camera_ui_top(st, 2, line, sizeof(line));
        OK(strlen(line) <= 42);
        camera_ui_hint(st, "R", 99, line, sizeof(line));
        OK(strlen(line) <= 42);
    }
}

static void test_geometry(void)
{
    // The FLIP tile sits at the right of row 0 and misses the viewfinder.
    OK(CAMERA_FLIP_Y + CAMERA_FLIP_H <= CAMERA_VIEW_Y0);
    OK(CAMERA_FLIP_X + CAMERA_FLIP_W <= 256);
    OK(camera_flip_hit(CAMERA_FLIP_X, 0));
    OK(camera_flip_hit(CAMERA_FLIP_X + CAMERA_FLIP_W - 1, CAMERA_FLIP_H - 1));
    OK(!camera_flip_hit(CAMERA_FLIP_X - 1, 0));
    OK(!camera_flip_hit(CAMERA_FLIP_X, CAMERA_FLIP_H));
    OK(!camera_flip_hit(CAMERA_FLIP_X, CAMERA_VIEW_Y0));
    // Strips bracket the viewfinder.
    OK(CAMERA_STRIP_TOP_H == CAMERA_VIEW_Y0);
    OK(CAMERA_VIEW_Y1 + 1 == CAMERA_STRIP_BOTTOM_Y);
    OK(CAMERA_STRIP_BOTTOM_Y + CAMERA_STRIP_BOTTOM_H == 192);
}

// The PHOTO screen's only way onto the wire takes the camera's own types
// and nothing else: whatever goes wrong in photo.c, it cannot type.
static void test_camera_cannot_type(void)
{
    const uint8_t camera_types[] = {MSG_CAMERA, MSG_CAMERA_FRAME, MSG_PHOTO_CHUNK,
                                    MSG_PHOTO_END, MSG_PHOTO_DISCARD};
    for (size_t i = 0; i < sizeof(camera_types); i++) {
        CHECK(proto_camera_type(camera_types[i]), "type 0x%02x is the camera's",
              camera_types[i]);
    }
    int others = 0;
    for (unsigned t = 0; t < 256; t++) {
        int mine = 0;
        for (size_t i = 0; i < sizeof(camera_types); i++) mine |= camera_types[i] == t;
        if (!mine && proto_camera_type((uint8_t)t)) others++;
    }
    CHECK(others == 0, "%d other types passed as the camera's", others);
    CHECK(!proto_camera_type(MSG_KEY), "KEY passed as the camera's");
}

void test_camera_all(void)
{
    printf("== camera ==\n");
    test_camera_cannot_type();
    test_downscale_solid_and_bounds();
    test_gray4_expand();
    test_colour_view();
    test_yuyv_formula();
    test_thumbnail_picks_right_pixels();
    test_photo_pack();
    test_photo_preview_composes();
    test_crc32();
    test_pacer();
    test_upload();
    test_shutter();
    test_lr_chord();
    test_ui_state_and_strips();
    test_geometry();

    // INTEROP: the bytes the Python side must reproduce.
    {
        static uint16_t frame[CAMERA_SRC_W * CAMERA_SRC_H];
        static uint8_t prev[CAMERA_PREVIEW_BYTES];
        synth_frame(frame);
        camera_downscale_preview(frame, prev);
        printf("INTEROP cam-preview ");
        hex(prev, sizeof(prev));
        printf("\n");
        static uint8_t colour[CAMERA_COLOUR_BYTES];
        camera_downscale_colour(frame, colour);
        printf("INTEROP cam-colour ");
        hex(colour, sizeof(colour));
        printf("\n");
    }
    {
        // Clamp extremes and a mid value: the server's converter must land
        // on the same bytes.
        static const struct { unsigned y; int cb, cr; } v[] = {
            { 0, -128, -128 }, { 0, 0, 127 }, { 0, 127, 0 },
            { 255, 127, 127 }, { 255, -128, -128 }, { 128, 0, 0 },
            { 100, 0, -1 }, { 16, 32, -64 },
        };
        printf("INTEROP cam-yuyv ");
        for (size_t i = 0; i < sizeof(v) / sizeof(*v); i++) {
            uint8_t r, g, b;
            camera_yuyv_to_rgb888(v[i].y, v[i].cb, v[i].cr, &r, &g, &b);
            hex(&r, 1); hex(&g, 1); hex(&b, 1);
        }
        printf("\n");
    }
    {
        // The same 4 KB the Python generator builds.
        uint8_t buf[4096];
        for (int i = 0; i < 4096; i++) buf[i] = (uint8_t)(i * 37 + (i >> 5));
        printf("INTEROP cam-crc32 %08lx\n",
               (unsigned long)camera_crc32_update(0, buf, sizeof(buf)));
    }
    {
        // The packed wire forms of the synthetic capture, fingerprinted so
        // the Python packer (dsterm/camera.py photo_pack) can be pinned
        // against them: sha256 plus the first 64 bytes.
        static uint8_t yuyv[CAMERA_PHOTO_BYTES];
        static uint8_t packed[CAMERA_PHOTO_BYTES_MAX];
        uint8_t digest[SHA256_DIGEST_SIZE];
        synth_yuyv(yuyv);
        const int fmts[2] = { CAMERA_PHOTO_FORMAT_FULL,
                              CAMERA_PHOTO_FORMAT_SMALL };
        const char *names[2] = { "cam-pack-full", "cam-pack-small" };
        for (int i = 0; i < 2; i++) {
            const size_t n = camera_photo_pack(yuyv, fmts[i], packed);
            sha256(packed, n, digest);
            printf("INTEROP %s ", names[i]);
            hex(digest, sizeof(digest));
            printf(":");
            hex(packed, 64);
            printf("\n");
        }
    }
}
