#include "camera.h"

#include <stdio.h>
#include <string.h>

// ------------------------------------------------------------ the pixels ---

void camera_downscale_preview(const uint16_t src[CAMERA_SRC_W * CAMERA_SRC_H],
                              uint8_t dst[CAMERA_PREVIEW_BYTES])
{
    if (!src || !dst) return;
    for (int dy = 0; dy < CAMERA_PREVIEW_H; dy++) {
        const uint16_t *row0 = src + (size_t)(2 * dy) * CAMERA_SRC_W;
        const uint16_t *row1 = row0 + CAMERA_SRC_W;
        for (int dx = 0; dx < CAMERA_PREVIEW_W; dx++) {
            const uint16_t p[4] = { row0[2 * dx], row0[2 * dx + 1],
                                    row1[2 * dx], row1[2 * dx + 1] };
            unsigned rs = 0, gs = 0, bs = 0;
            for (int k = 0; k < 4; k++) {
                rs += p[k] & 31;
                gs += (p[k] >> 5) & 31;
                bs += (p[k] >> 10) & 31;
            }
            const unsigned r = (rs + 2) >> 2, g = (gs + 2) >> 2, b = (bs + 2) >> 2;
            const unsigned y = (77 * r + 150 * g + 29 * b + 128) >> 8;
            const unsigned level = (y * 15 + 15) / 31;
            uint8_t *out = &dst[(dy * CAMERA_PREVIEW_W + dx) / 2];
            if (dx & 1) *out = (uint8_t)((*out & 0xF0) | level);
            else        *out = (uint8_t)((level << 4) | (*out & 0x0F));
        }
    }
}

uint8_t camera_gray4_to_8(uint8_t level)
{
    return (uint8_t)((level & 15) * 17);
}

void camera_downscale_colour(const uint16_t src[CAMERA_SRC_W * CAMERA_SRC_H],
                             uint8_t dst[CAMERA_COLOUR_BYTES])
{
    if (!src || !dst) return;
    for (int dy = 0; dy < CAMERA_COLOUR_H; dy++) {
        const int y0 = dy * CAMERA_SRC_H / CAMERA_COLOUR_H;
        const int y1 = (dy + 1) * CAMERA_SRC_H / CAMERA_COLOUR_H;
        for (int dx = 0; dx < CAMERA_COLOUR_W; dx++) {
            const int x0 = dx * CAMERA_SRC_W / CAMERA_COLOUR_W;
            const int x1 = (dx + 1) * CAMERA_SRC_W / CAMERA_COLOUR_W;
            const unsigned n = (unsigned)((x1 - x0) * (y1 - y0));
            unsigned rs = 0, gs = 0, bs = 0;
            for (int y = y0; y < y1; y++) {
                for (int x = x0; x < x1; x++) {
                    const uint16_t p = src[y * CAMERA_SRC_W + x];
                    rs += p & 31;
                    gs += (p >> 5) & 31;
                    bs += (p >> 10) & 31;
                }
            }
            const unsigned r = (rs + n / 2) / n, g = (gs + n / 2) / n,
                           b = (bs + n / 2) / n;
            const unsigned r3 = (r * 7 + 15) / 31, g3 = (g * 7 + 15) / 31,
                           b2 = (b * 3 + 15) / 31;
            dst[dy * CAMERA_COLOUR_W + dx] = (uint8_t)((r3 << 5) | (g3 << 2) | b2);
        }
    }
}

void camera_rgb332_to_rgb888(uint8_t px, uint8_t *r, uint8_t *g, uint8_t *b)
{
    const unsigned rv = px >> 5, gv = (px >> 2) & 7, bv = px & 3;
    if (r) *r = (uint8_t)((rv * 255 + 3) / 7);
    if (g) *g = (uint8_t)((gv * 255 + 3) / 7);
    if (b) *b = (uint8_t)(bv * 85);
}

size_t camera_view_bytes(int format)
{
    return format == CAMERA_VIEW_GREY ? CAMERA_PREVIEW_BYTES
         : format == CAMERA_VIEW_COLOUR ? CAMERA_COLOUR_BYTES : 0;
}

size_t camera_view_downscale(int format,
                             const uint16_t src[CAMERA_SRC_W * CAMERA_SRC_H],
                             uint8_t dst[CAMERA_VIEW_BYTES_MAX])
{
    if (!src || !dst) return 0;
    if (format == CAMERA_VIEW_GREY) camera_downscale_preview(src, dst);
    else if (format == CAMERA_VIEW_COLOUR) camera_downscale_colour(src, dst);
    else return 0;
    return camera_view_bytes(format);
}

// An arithmetic right shift written portably: >> on a negative int is
// implementation-defined (gcc does the arithmetic shift we want, and the
// DS build is gcc, but the host tests run under UBSan and this form is
// exact everywhere): floor(v / 2^n) using only positive shifts.
static int sar(int v, int n)
{
    if (v >= 0) return v >> n;
    return -(((-v) + (1 << n) - 1) >> n);
}

static int clamp8(int v)
{
    return v < 0 ? 0 : v > 255 ? 255 : v;
}

void camera_yuyv_to_rgb888(unsigned y, int cb, int cr,
                           uint8_t *r, uint8_t *g, uint8_t *b)
{
    const int yi = (int)y;
    // The SDK example's integer approximation of the BT.601 matrix, kept
    // term for term so the review frame and the server's PNG agree.
    const int ri = yi + cr + sar(cr, 2) + sar(cr, 3) + sar(cr, 5);
    const int gi = yi - (sar(cb, 2) + sar(cb, 4) + sar(cb, 5))
                    - (sar(cr, 1) + sar(cr, 3) + sar(cr, 4) + sar(cr, 5));
    const int bi = yi + cb + sar(cb, 1) + sar(cb, 2) + sar(cb, 6);
    if (r) *r = (uint8_t)clamp8(ri);
    if (g) *g = (uint8_t)clamp8(gi);
    if (b) *b = (uint8_t)clamp8(bi);
}

void camera_photo_thumbnail(const uint8_t yuyv[CAMERA_PHOTO_BYTES],
                            uint16_t dst[CAMERA_SRC_W * CAMERA_SRC_H])
{
    if (!yuyv || !dst) return;
    for (int dy = 0; dy < CAMERA_SRC_H; dy++) {
        const int sy = (dy * CAMERA_PHOTO_H) / CAMERA_SRC_H;
        for (int dx = 0; dx < CAMERA_SRC_W; dx++) {
            const int sx = (dx * CAMERA_PHOTO_W) / CAMERA_SRC_W;
            const uint8_t *pair = yuyv + ((size_t)sy * (CAMERA_PHOTO_W / 2)
                                          + (size_t)(sx >> 1)) * 4;
            const unsigned yy = pair[(sx & 1) ? 2 : 0];
            uint8_t r, g, b;
            camera_yuyv_to_rgb888(yy, (int)pair[1] - 0x80,
                                  (int)pair[3] - 0x80, &r, &g, &b);
            dst[dy * CAMERA_SRC_W + dx] =
                (uint16_t)(0x8000 | ((r >> 3) & 31) |
                           (((g >> 3) & 31) << 5) | (((b >> 3) & 31) << 10));
        }
    }
}

size_t camera_photo_preview(const uint8_t yuyv[CAMERA_PHOTO_BYTES],
                            uint16_t scratch[CAMERA_SRC_W * CAMERA_SRC_H],
                            int format, uint8_t dst[CAMERA_VIEW_BYTES_MAX])
{
    if (!scratch || !camera_view_bytes(format)) return 0;
    camera_photo_thumbnail(yuyv, scratch);
    return camera_view_downscale(format, scratch, dst);
}

// ----------------------------------------------------------------- CRC-32 ---

// ~5 cycles a byte, ~45 ms per picture, spread across the chunks.  The table
// is the bitwise definition unrolled: entry n is the CRC state step for a
// byte equal to n, which the host tests verify index by index.
static const uint32_t CRC_TABLE[256] = {
    0x00000000u, 0x77073096u, 0xee0e612cu, 0x990951bau, 0x076dc419u, 0x706af48fu, 0xe963a535u, 0x9e6495a3u,
    0x0edb8832u, 0x79dcb8a4u, 0xe0d5e91eu, 0x97d2d988u, 0x09b64c2bu, 0x7eb17cbdu, 0xe7b82d07u, 0x90bf1d91u,
    0x1db71064u, 0x6ab020f2u, 0xf3b97148u, 0x84be41deu, 0x1adad47du, 0x6ddde4ebu, 0xf4d4b551u, 0x83d385c7u,
    0x136c9856u, 0x646ba8c0u, 0xfd62f97au, 0x8a65c9ecu, 0x14015c4fu, 0x63066cd9u, 0xfa0f3d63u, 0x8d080df5u,
    0x3b6e20c8u, 0x4c69105eu, 0xd56041e4u, 0xa2677172u, 0x3c03e4d1u, 0x4b04d447u, 0xd20d85fdu, 0xa50ab56bu,
    0x35b5a8fau, 0x42b2986cu, 0xdbbbc9d6u, 0xacbcf940u, 0x32d86ce3u, 0x45df5c75u, 0xdcd60dcfu, 0xabd13d59u,
    0x26d930acu, 0x51de003au, 0xc8d75180u, 0xbfd06116u, 0x21b4f4b5u, 0x56b3c423u, 0xcfba9599u, 0xb8bda50fu,
    0x2802b89eu, 0x5f058808u, 0xc60cd9b2u, 0xb10be924u, 0x2f6f7c87u, 0x58684c11u, 0xc1611dabu, 0xb6662d3du,
    0x76dc4190u, 0x01db7106u, 0x98d220bcu, 0xefd5102au, 0x71b18589u, 0x06b6b51fu, 0x9fbfe4a5u, 0xe8b8d433u,
    0x7807c9a2u, 0x0f00f934u, 0x9609a88eu, 0xe10e9818u, 0x7f6a0dbbu, 0x086d3d2du, 0x91646c97u, 0xe6635c01u,
    0x6b6b51f4u, 0x1c6c6162u, 0x856530d8u, 0xf262004eu, 0x6c0695edu, 0x1b01a57bu, 0x8208f4c1u, 0xf50fc457u,
    0x65b0d9c6u, 0x12b7e950u, 0x8bbeb8eau, 0xfcb9887cu, 0x62dd1ddfu, 0x15da2d49u, 0x8cd37cf3u, 0xfbd44c65u,
    0x4db26158u, 0x3ab551ceu, 0xa3bc0074u, 0xd4bb30e2u, 0x4adfa541u, 0x3dd895d7u, 0xa4d1c46du, 0xd3d6f4fbu,
    0x4369e96au, 0x346ed9fcu, 0xad678846u, 0xda60b8d0u, 0x44042d73u, 0x33031de5u, 0xaa0a4c5fu, 0xdd0d7cc9u,
    0x5005713cu, 0x270241aau, 0xbe0b1010u, 0xc90c2086u, 0x5768b525u, 0x206f85b3u, 0xb966d409u, 0xce61e49fu,
    0x5edef90eu, 0x29d9c998u, 0xb0d09822u, 0xc7d7a8b4u, 0x59b33d17u, 0x2eb40d81u, 0xb7bd5c3bu, 0xc0ba6cadu,
    0xedb88320u, 0x9abfb3b6u, 0x03b6e20cu, 0x74b1d29au, 0xead54739u, 0x9dd277afu, 0x04db2615u, 0x73dc1683u,
    0xe3630b12u, 0x94643b84u, 0x0d6d6a3eu, 0x7a6a5aa8u, 0xe40ecf0bu, 0x9309ff9du, 0x0a00ae27u, 0x7d079eb1u,
    0xf00f9344u, 0x8708a3d2u, 0x1e01f268u, 0x6906c2feu, 0xf762575du, 0x806567cbu, 0x196c3671u, 0x6e6b06e7u,
    0xfed41b76u, 0x89d32be0u, 0x10da7a5au, 0x67dd4accu, 0xf9b9df6fu, 0x8ebeeff9u, 0x17b7be43u, 0x60b08ed5u,
    0xd6d6a3e8u, 0xa1d1937eu, 0x38d8c2c4u, 0x4fdff252u, 0xd1bb67f1u, 0xa6bc5767u, 0x3fb506ddu, 0x48b2364bu,
    0xd80d2bdau, 0xaf0a1b4cu, 0x36034af6u, 0x41047a60u, 0xdf60efc3u, 0xa867df55u, 0x316e8eefu, 0x4669be79u,
    0xcb61b38cu, 0xbc66831au, 0x256fd2a0u, 0x5268e236u, 0xcc0c7795u, 0xbb0b4703u, 0x220216b9u, 0x5505262fu,
    0xc5ba3bbeu, 0xb2bd0b28u, 0x2bb45a92u, 0x5cb36a04u, 0xc2d7ffa7u, 0xb5d0cf31u, 0x2cd99e8bu, 0x5bdeae1du,
    0x9b64c2b0u, 0xec63f226u, 0x756aa39cu, 0x026d930au, 0x9c0906a9u, 0xeb0e363fu, 0x72076785u, 0x05005713u,
    0x95bf4a82u, 0xe2b87a14u, 0x7bb12baeu, 0x0cb61b38u, 0x92d28e9bu, 0xe5d5be0du, 0x7cdcefb7u, 0x0bdbdf21u,
    0x86d3d2d4u, 0xf1d4e242u, 0x68ddb3f8u, 0x1fda836eu, 0x81be16cdu, 0xf6b9265bu, 0x6fb077e1u, 0x18b74777u,
    0x88085ae6u, 0xff0f6a70u, 0x66063bcau, 0x11010b5cu, 0x8f659effu, 0xf862ae69u, 0x616bffd3u, 0x166ccf45u,
    0xa00ae278u, 0xd70dd2eeu, 0x4e048354u, 0x3903b3c2u, 0xa7672661u, 0xd06016f7u, 0x4969474du, 0x3e6e77dbu,
    0xaed16a4au, 0xd9d65adcu, 0x40df0b66u, 0x37d83bf0u, 0xa9bcae53u, 0xdebb9ec5u, 0x47b2cf7fu, 0x30b5ffe9u,
    0xbdbdf21cu, 0xcabac28au, 0x53b39330u, 0x24b4a3a6u, 0xbad03605u, 0xcdd70693u, 0x54de5729u, 0x23d967bfu,
    0xb3667a2eu, 0xc4614ab8u, 0x5d681b02u, 0x2a6f2b94u, 0xb40bbe37u, 0xc30c8ea1u, 0x5a05df1bu, 0x2d02ef8du,
};

uint32_t camera_crc32_update(uint32_t crc, const uint8_t *data, size_t len)
{
    uint32_t c = ~crc;
    for (size_t i = 0; i < len; i++) {
        c = CRC_TABLE[(c ^ data[i]) & 0xFF] ^ (c >> 8);
    }
    return ~c;
}

// -------------------------------------------------------------- the pacer ---

void camera_pacer_init(camera_pacer_t *p)
{
    if (p) memset(p, 0, sizeof(*p));
}

int camera_pacer_ready(const camera_pacer_t *p, uint32_t now_frame)
{
    if (!p || p->in_flight) return 0;
    if (!p->has_sent) return 1;
    // Wrap-safe distance since the last send.
    return (uint32_t)(now_frame - p->last_sent_frame) >=
           CAMERA_PREVIEW_INTERVAL_FRAMES;
}

void camera_pacer_sent(camera_pacer_t *p, uint32_t now_frame)
{
    if (!p) return;
    p->in_flight = 1;
    p->has_sent = 1;
    p->seq++;
    p->last_sent_frame = now_frame;
}

int camera_pacer_ack(camera_pacer_t *p, uint8_t seq)
{
    if (!p || !p->in_flight || p->seq != seq) return 0;
    p->in_flight = 0;
    return 1;
}

// ----------------------------------------------------------- the packing ---
//
// The sensor's YUYV becomes 4:2:0 planar for the wire: full size the luma is
// carried whole and the chroma is halved vertically; small quarters both.

size_t camera_photo_bytes(int format)
{
    switch (format) {
    case CAMERA_PHOTO_FORMAT_FULL:  return CAMERA_PHOTO_FULL_BYTES;
    case CAMERA_PHOTO_FORMAT_SMALL: return CAMERA_PHOTO_SMALL_BYTES;
    default:                        return 0;
    }
}

size_t camera_photo_pack(const uint8_t *yuyv, int format, uint8_t *out)
{
    if (!yuyv || !out) return 0;
    const size_t total = camera_photo_bytes(format);
    if (!total) return 0;
    const int pw = CAMERA_PHOTO_W / 2;         // chroma columns in the source
    const int row_bytes = CAMERA_PHOTO_W * 2;  // 1280

    if (format == CAMERA_PHOTO_FORMAT_FULL) {
        uint8_t *yp = out;
        uint8_t *cbp = out + CAMERA_PHOTO_W * CAMERA_PHOTO_H;
        uint8_t *crp = cbp + pw * (CAMERA_PHOTO_H / 2);
        for (int y = 0; y < CAMERA_PHOTO_H; y++) {
            const uint8_t *line = yuyv + (size_t)y * row_bytes;
            for (int x = 0; x < CAMERA_PHOTO_W; x++) {
                yp[y * CAMERA_PHOTO_W + x] = line[(x >> 1) * 4 + (x & 1) * 2];
            }
        }
        for (int cy = 0; cy < CAMERA_PHOTO_H / 2; cy++) {
            const uint8_t *r0 = yuyv + (size_t)(2 * cy) * row_bytes;
            const uint8_t *r1 = r0 + row_bytes;
            for (int cx = 0; cx < pw; cx++) {
                cbp[cy * pw + cx] =
                    (uint8_t)((r0[cx * 4 + 1] + r1[cx * 4 + 1] + 1) >> 1);
                crp[cy * pw + cx] =
                    (uint8_t)((r0[cx * 4 + 3] + r1[cx * 4 + 3] + 1) >> 1);
            }
        }
        return total;
    }

    // SMALL: half the width and height on every plane.
    const int sw = CAMERA_PHOTO_W / 2;    // 320
    const int sh = CAMERA_PHOTO_H / 2;    // 240
    const int cw = sw / 2;                // 160
    const int ch = sh / 2;                // 120
    uint8_t *yp = out;
    uint8_t *cbp = out + sw * sh;
    uint8_t *crp = cbp + cw * ch;
    for (int y = 0; y < sh; y++) {
        const uint8_t *r0 = yuyv + (size_t)(2 * y) * row_bytes;
        const uint8_t *r1 = r0 + row_bytes;
        for (int x = 0; x < sw; x++) {
            yp[y * sw + x] = (uint8_t)((r0[x * 4] + r0[x * 4 + 2] +
                                        r1[x * 4] + r1[x * 4 + 2] + 2) >> 2);
        }
    }
    for (int cy = 0; cy < ch; cy++) {
        const uint8_t *r0 = yuyv + (size_t)(2 * cy) * row_bytes;
        const uint8_t *r1 = r0 + row_bytes;
        for (int cx = 0; cx < cw; cx++) {
            cbp[cy * cw + cx] =
                (uint8_t)((r0[(2 * cx) * 4 + 1] + r0[(2 * cx + 1) * 4 + 1] +
                           r1[(2 * cx) * 4 + 1] + r1[(2 * cx + 1) * 4 + 1] +
                           2) >> 2);
            crp[cy * cw + cx] =
                (uint8_t)((r0[(2 * cx) * 4 + 3] + r0[(2 * cx + 1) * 4 + 3] +
                           r1[(2 * cx) * 4 + 3] + r1[(2 * cx + 1) * 4 + 3] +
                           2) >> 2);
        }
    }
    return total;
}

// ------------------------------------------------------------- the upload ---

void camera_upload_init(camera_upload_t *u, int format)
{
    if (!u) return;
    u->total = (uint32_t)camera_photo_bytes(format);
    u->sent = 0;
    u->acked = 0;
}

int camera_upload_next(camera_upload_t *u, uint32_t *off, uint32_t *len)
{
    if (!u || !off || !len) return 0;
    if (u->sent - u->acked >= CAMERA_UPLOAD_WINDOW) return 0;
    if (u->sent >= u->total) return 0;
    const uint32_t left = u->total - u->sent;
    const uint32_t n = left < CAMERA_UPLOAD_CHUNK ? left : CAMERA_UPLOAD_CHUNK;
    *off = u->sent;
    *len = n;
    u->sent += n;
    return 1;
}

int camera_upload_ack(camera_upload_t *u, uint32_t value)
{
    if (!u || value < u->acked || value > u->sent) return 0;
    u->acked = value;
    return 1;
}

int camera_upload_all_sent(const camera_upload_t *u)
{
    return u && u->sent >= u->total;
}

unsigned camera_upload_percent(const camera_upload_t *u)
{
    if (!u || u->total == 0) return 0;
    return (unsigned)((u->acked * 100u) / u->total);
}

// ------------------------------------------------------------- the shutter ---

void camera_shutter_reset(camera_shutter_state_t *s)
{
    if (s) memset(s, 0, sizeof(*s));
}

int camera_shutter_input(camera_shutter_state_t *s, unsigned shutter_mask,
                         unsigned other_mask, unsigned raw, unsigned edge)
{
    if (!s) return 0;
    if (edge & shutter_mask) {
        s->armed = 1;
        s->spoiled = 0;
    }
    // Held at ANY frame while armed: one frame of the other shoulder makes
    // this press a chord's, not a shutter's.
    if (s->armed && (raw & other_mask)) s->spoiled = 1;
    if (s->armed && !(raw & shutter_mask)) {
        const int fire = !s->spoiled;
        camera_shutter_reset(s);
        return fire;
    }
    return 0;
}

int camera_lr_chord(unsigned raw, unsigned edge)
{
    return ((raw & (CAMERA_KEY_L | CAMERA_KEY_R)) == (CAMERA_KEY_L | CAMERA_KEY_R)) &&
           (edge & (CAMERA_KEY_L | CAMERA_KEY_R)) != 0;
}

// ------------------------------------------------------------ the screen ---

camera_ui_t camera_ui_leave(void)
{
    return CAMERA_UI_OFF;
}

camera_ui_t camera_ui_shot(camera_ui_t state)
{
    return state == CAMERA_UI_LIVE ? CAMERA_UI_REVIEW : state;
}

camera_ui_t camera_ui_send(camera_ui_t state)
{
    return state == CAMERA_UI_REVIEW ? CAMERA_UI_SENDING : state;
}

camera_ui_t camera_ui_retake(camera_ui_t state)
{
    return state == CAMERA_UI_REVIEW ? CAMERA_UI_LIVE : state;
}

camera_ui_t camera_ui_sending(camera_ui_t state)
{
    return state == CAMERA_UI_SENDING ? CAMERA_UI_SAVING : state;
}

camera_ui_t camera_ui_saved(camera_ui_t state)
{
    // A saved picture is the exit: the camera powers off and the composer
    // opens, so there is no "review the saved one" to come back to.
    return state == CAMERA_UI_SAVING ? CAMERA_UI_OFF : state;
}

camera_ui_t camera_ui_failed(camera_ui_t state)
{
    return state == CAMERA_UI_OFF ? state : CAMERA_UI_FAILED;
}

camera_ui_t camera_ui_live(camera_ui_t state)
{
    return state == CAMERA_UI_STARTING ? CAMERA_UI_LIVE : state;
}

void camera_ui_top(camera_ui_t state, int camera, char *out, size_t cap)
{
    if (!out || !cap) return;
    out[0] = '\0';
    if (state == CAMERA_UI_OFF) return;
    // The same bullet the dashboards show: a lit dot while the sensor is on.
    // 0xa6 is the wire byte for "●" (dsterm/extmap.py).
    snprintf(out, cap, "\xa6 CAMERA ON  %s",
             camera == 2 ? "OUTER" : camera == 1 ? "INNER" : "OFF");
}

void camera_ui_hint(camera_ui_t state, const char *shutter, unsigned percent,
                    char *out, size_t cap)
{
    if (!out || !cap) return;
    out[0] = '\0';
    const char *name = shutter && shutter[0] ? shutter : "R";
    switch (state) {
    case CAMERA_UI_STARTING:
        snprintf(out, cap, "starting the camera...");
        break;
    case CAMERA_UI_LIVE:
        snprintf(out, cap, "%s take  Y flip  B back", name);
        break;
    case CAMERA_UI_REVIEW:
        snprintf(out, cap, "A send to computer  B retake");
        break;
    case CAMERA_UI_SENDING:
        snprintf(out, cap, "sending %u%%   B cancel", percent % 101);
        break;
    case CAMERA_UI_SAVING:
        snprintf(out, cap, "saving on the computer...  B cancel");
        break;
    case CAMERA_UI_FAILED:
        snprintf(out, cap, "camera failed  B back");
        break;
    case CAMERA_UI_OFF:
    default:
        break;
    }
}

int camera_ui_is_error(camera_ui_t state)
{
    return state == CAMERA_UI_FAILED;
}

int camera_flip_hit(int x, int y)
{
    return x >= CAMERA_FLIP_X && x < CAMERA_FLIP_X + CAMERA_FLIP_W &&
           y >= CAMERA_FLIP_Y && y < CAMERA_FLIP_Y + CAMERA_FLIP_H;
}
