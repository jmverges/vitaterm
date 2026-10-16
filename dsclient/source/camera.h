// The DSi camera's pure half: the pixel maths, the CRC-32, the pacing and the
// screen state, with no libnds dependency so the host suite and the Python
// reference (dsterm/camera.py, tools/crosscheck.py) pin every byte of it.
//
// What lives here is what must be exactly right and exactly the same on both
// ends: a preview frame the computer draws, a picture the computer converts
// with the same integer YUV formula, and a checksum they both compute.

#ifndef DSTERM_CAMERA_H
#define DSTERM_CAMERA_H

#include <stddef.h>
#include <stdint.h>

// ----------------------------------------------------------------- sizes ---
//
// The live preview is the viewfinder's 256x192 RGB555 frame boxed down to
// CAMERA_PREVIEW_W x CAMERA_PREVIEW_H in sixteen greys, two pixels a byte.
// Greys rather than colours because the computer's view is for aiming:
// 128x96 in greys shows the layout of a screen or a page, where 64x48 in
// 256 dithered colours (the first version) was noise -- for the same bytes.
// The picture itself is the full-colour still.  The still is the sensor's
// 640x480 YUV 4:2:2, byte order Y0 Cb Y1 Cr.
#define CAMERA_SRC_W 256
#define CAMERA_SRC_H 192
#define CAMERA_PREVIEW_W 128
#define CAMERA_PREVIEW_H 96
#define CAMERA_PREVIEW_PIXELS (CAMERA_PREVIEW_W * CAMERA_PREVIEW_H)   // 12288
#define CAMERA_PREVIEW_BYTES (CAMERA_PREVIEW_PIXELS / 2)              // 6144

// The colour live view, for whoever picks LIVE VIEW: COLOUR: 96x72 in 256
// colours (RGB332), no dither -- the colour that fits the same one frame.
#define CAMERA_COLOUR_W 96
#define CAMERA_COLOUR_H 72
#define CAMERA_COLOUR_BYTES (CAMERA_COLOUR_W * CAMERA_COLOUR_H)       // 6912
#define CAMERA_VIEW_BYTES_MAX CAMERA_COLOUR_BYTES

// The two live views, by the wire's format byte (format 1 was the first
// version's 64x48 dithered RGB332, never sent now).
#define CAMERA_VIEW_GREY   2
#define CAMERA_VIEW_COLOUR 3
#define CAMERA_PHOTO_W 640
#define CAMERA_PHOTO_H 480
#define CAMERA_PHOTO_BYTES (CAMERA_PHOTO_W * CAMERA_PHOTO_H * 2)      // 614400

// The wire formats: what actually crosses is 4:2:0 planar (the Y plane, then
// Cb, then Cr), packed from the YUYV capture by camera_photo_pack().  FULL is
// every luma, SMALL a quarter of them -- a picture that sends four times
// faster.
#define CAMERA_PHOTO_FORMAT_FULL  1
#define CAMERA_PHOTO_FORMAT_SMALL 2
#define CAMERA_PHOTO_FULL_BYTES  (CAMERA_PHOTO_W * CAMERA_PHOTO_H * 3 / 2)   // 460800
#define CAMERA_PHOTO_SMALL_BYTES (CAMERA_PHOTO_W / 2 * CAMERA_PHOTO_H / 2 * 3 / 2)  // 115200
#define CAMERA_PHOTO_BYTES_MAX   CAMERA_PHOTO_FULL_BYTES

// ------------------------------------------------------------ the pixels ---
//
// camera_downscale_preview: each output pixel is the 2x2 box average of the
// source's RGB555 (rows 2*dy and 2*dy+1, columns 2*dx and 2*dx+1), per
// channel in 5-bit space ((sum + 2) >> 2, bit 15 ignored), then
//
//     y = (77*r + 150*g + 29*b + 128) >> 8        0..31, BT.601's weights
//     level = (y*15 + 15) / 31                    0..15
//
// packed two pixels a byte, the left one in the high nibble, row-major, top
// row first.  All integer, so the card and dsterm/camera.py cannot disagree.
void camera_downscale_preview(const uint16_t src[CAMERA_SRC_W * CAMERA_SRC_H],
                              uint8_t dst[CAMERA_PREVIEW_BYTES]);

// The expansion the receiving end applies to every level: 0..15 to 0..255.
uint8_t camera_gray4_to_8(uint8_t level);

// camera_downscale_colour: each output pixel is the area box of the source
// over the integer rectangle [dx*256/96, (dx+1)*256/96) x [dy*192/72,
// (dy+1)*192/72), averaged per channel in 5-bit space ((sum + n/2) / n),
// then quantized without dither -- R and G to (a*7 + 15) / 31, B to
// (a*3 + 15) / 31 -- into one byte: R in bits 7-5, G in 4-2, B in 1-0.
void camera_downscale_colour(const uint16_t src[CAMERA_SRC_W * CAMERA_SRC_H],
                             uint8_t dst[CAMERA_COLOUR_BYTES]);

// The receiving end's expansion of one RGB332 byte: (v*255 + 3) / 7 for R
// and G, v*85 for B.
void camera_rgb332_to_rgb888(uint8_t px, uint8_t *r, uint8_t *g, uint8_t *b);

// A view format's payload size (0 for one that is not), and the downscale
// for it into `dst` (CAMERA_VIEW_BYTES_MAX); returns the bytes written, or 0.
size_t camera_view_bytes(int format);
size_t camera_view_downscale(int format,
                             const uint16_t src[CAMERA_SRC_W * CAMERA_SRC_H],
                             uint8_t dst[CAMERA_VIEW_BYTES_MAX]);

// The SDK's integer YUV -> RGB, byte for byte what the server applies when
// it makes the PNG.  `cb` and `cr` arrive already minus 0x80 (signed, -128
// .. 127); `y` is 0 .. 255.  Each output is clamped to 0 .. 255.
void camera_yuyv_to_rgb888(unsigned y, int cb, int cr,
                           uint8_t *r, uint8_t *g, uint8_t *b);

// The still's review frame: a nearest-neighbour 256x192 RGB555 thumbnail of
// the 640x480 YUYV capture, with bit 15 set.  Source x is (dx*640)/256 and
// source y is (dy*480)/192; a pair holds Y0 Cb Y1 Cr, so an even source x
// reads Y0 and an odd one Y1.
void camera_photo_thumbnail(const uint8_t yuyv[CAMERA_PHOTO_BYTES],
                            uint16_t dst[CAMERA_SRC_W * CAMERA_SRC_H]);

// The still's preview for CAMERA_FRAME: thumbnail first (into `scratch`,
// which the caller owns because 96 KB does not belong on this stack), then
// the same downscale the live view uses in `format`, so a still and a frame
// of the same scene agree.  Returns the bytes written, or 0.
size_t camera_photo_preview(const uint8_t yuyv[CAMERA_PHOTO_BYTES],
                            uint16_t scratch[CAMERA_SRC_W * CAMERA_SRC_H],
                            int format, uint8_t dst[CAMERA_VIEW_BYTES_MAX]);

// ----------------------------------------------------------------- CRC-32 ---
//
// IEEE 802.3, reflected, polynomial 0xEDB88320 -- the CRC zlib.crc32
// computes, which is what the server checks the picture against.
// `crc` is the running value: start at 0, feed each chunk, and the result
// of the last call is the CRC ("123456789" gives 0xCBF43926).
uint32_t camera_crc32_update(uint32_t crc, const uint8_t *data, size_t len);

// -------------------------------------------------------------- the pacer ---
//
// At most one preview frame in flight, at most one every
// CAMERA_PREVIEW_INTERVAL_FRAMES frames (20 is 3 fps at 60 Hz: 6 KB a frame,
// about 18 KB/s).  Frames are dropped, never queued: ready() answers whether
// a new one may be built.
#define CAMERA_PREVIEW_INTERVAL_FRAMES 20u
typedef struct {
    int in_flight;
    uint8_t seq;
    uint32_t last_sent_frame;
    int has_sent;
} camera_pacer_t;

void camera_pacer_init(camera_pacer_t *p);
int camera_pacer_ready(const camera_pacer_t *p, uint32_t now_frame);
void camera_pacer_sent(camera_pacer_t *p, uint32_t now_frame);
// Returns 1 when the ack cleared the frame in flight.  A wrong seq is
// ignored and returns 0 -- it is stale, not a fault.
int camera_pacer_ack(camera_pacer_t *p, uint8_t seq);

// The still's wire form: pack the 640x480 YUYV capture into 4:2:0 planar.
// FULL's Y plane is the YUYV luma unchanged and its chroma the average of
// each pair's two rows ((a+b+1)>>1).  SMALL's Y is the 2x2 luma block
// average ((sum+2)>>2) and its chroma the four samples under it.
// camera_photo_bytes returns a format's packed size, 0 for one that is not.
size_t camera_photo_bytes(int format);
size_t camera_photo_pack(const uint8_t *yuyv, int format, uint8_t *out);

// ------------------------------------------------------------- the upload ---
//
// The packed bytes go out in 1 KB chunks with at most 8 KB in flight:
// lwIP's TCP_SND_BUF is 11680, so the window leaves room for a KEY frame and
// send() never stalls.  The server returns credits as CAMERA_ACKs whose
// value is the byte count it holds.
#define CAMERA_UPLOAD_CHUNK 1024u
#define CAMERA_UPLOAD_WINDOW 8192u

typedef struct {
    uint32_t total;
    uint32_t sent;
    uint32_t acked;
} camera_upload_t;

// The total comes from the format: FULL is 460800 bytes, SMALL 115200.
void camera_upload_init(camera_upload_t *u, int format);
// Fills `off`/`len` with the next chunk and returns 1 while the window has
// room and bytes remain; 0 when the upload is stalled or done.
int camera_upload_next(camera_upload_t *u, uint32_t *off, uint32_t *len);
// A credit: valid only between `acked` and `sent`; anything else is a
// protocol fault and returns 0.
int camera_upload_ack(camera_upload_t *u, uint32_t value);
int camera_upload_all_sent(const camera_upload_t *u);
unsigned camera_upload_percent(const camera_upload_t *u);

// ------------------------------------------------------------- the shutter ---
//
// The camera screen's shutter: armed on its press edge, spoiled if the other
// shoulder is held at any frame while it is armed, fired on release.  That
// keeps L+R a chord -- the press that becomes a chord never fires.
// Named for what it holds rather than what it is: buttons.h's
// camera_shutter_t is the setting (which key takes the picture).
typedef struct {
    int armed;
    int spoiled;
} camera_shutter_state_t;

void camera_shutter_reset(camera_shutter_state_t *s);
// Feed it the key word and its press edges each frame.  `shutter_mask` is
// the shutter's key bit, `other_mask` the shoulder it spoils with (0 when
// the shutter is A).  Returns 1 the frame a picture fires.
int camera_shutter_input(camera_shutter_state_t *s, unsigned shutter_mask,
                         unsigned other_mask, unsigned raw, unsigned edge);

// L+R together, on either's edge: "leave".  The same semantics as main.c's
// shoulder_chord, kept here so the camera screen and the tests share one
// definition.  The masks are the key word's L and R bits (kbd.h's word:
// register bits 8 and 9).
#define CAMERA_KEY_R (1u << 8)
#define CAMERA_KEY_L (1u << 9)
int camera_lr_chord(unsigned raw, unsigned edge);

// ------------------------------------------------------------ the screen ---
//
// The overlay's states.  OFF is also where a card with no camera lives.
typedef enum {
    CAMERA_UI_OFF = 0,
    CAMERA_UI_STARTING,
    CAMERA_UI_LIVE,
    CAMERA_UI_REVIEW,
    CAMERA_UI_SENDING,
    CAMERA_UI_SAVING,
    CAMERA_UI_FAILED,
} camera_ui_t;

// Pure transitions: every way out names its next state rather than each
// caller deciding.  camera_ui_leave() is any key that closes the screen --
// a chord, B or X, the d-pad; review's A goes to SENDING and its B back to
// LIVE; a finished save goes OFF; a failure stays until left.
camera_ui_t camera_ui_leave(void);
camera_ui_t camera_ui_shot(camera_ui_t state);        // shutter fired
camera_ui_t camera_ui_send(camera_ui_t state);        // review's A
camera_ui_t camera_ui_retake(camera_ui_t state);      // review's B
camera_ui_t camera_ui_sending(camera_ui_t state);     // SENDING finished
camera_ui_t camera_ui_saved(camera_ui_t state);       // SAVING answered OK
camera_ui_t camera_ui_failed(camera_ui_t state);      // anything went wrong
// A live frame is up: STARTING -> LIVE.
camera_ui_t camera_ui_live(camera_ui_t state);

// The two strips, row 0 and row 23, as the plan draws them.  Row 0 is
// "● CAMERA ON  OUTER" (or INNER); row 23 is the hint for the state, with
// the shutter's name in it ("R take  Y flip  B back"), the upload's percent
// while sending, and a plain word while saving or on failure.  Each writes
// a finished line, at most cap-1 chars.
void camera_ui_top(camera_ui_t state, int camera, char *out, size_t cap);
void camera_ui_hint(camera_ui_t state, const char *shutter, unsigned percent,
                    char *out, size_t cap);
// FAILED draws in yellow, the rest in the ordinary dim.
int camera_ui_is_error(camera_ui_t state);

// --------------------------------------------------------- the geometry ---
//
// One table for drawing and hit-testing, the rule every picker here keeps.
// The sub screen is 256x192; text rows are 8 px.
#define CAMERA_STRIP_TOP_Y 0
#define CAMERA_STRIP_TOP_H 8
#define CAMERA_STRIP_BOTTOM_Y 184
#define CAMERA_STRIP_BOTTOM_H 8
// The viewfinder rows sit between the strips.
#define CAMERA_VIEW_Y0 8
#define CAMERA_VIEW_Y1 183
// The FLIP tile: the right end of the top strip, drawn "Y flip".
#define CAMERA_FLIP_X 208
#define CAMERA_FLIP_Y 0
#define CAMERA_FLIP_W 48
#define CAMERA_FLIP_H 8

int camera_flip_hit(int x, int y);

#endif  // DSTERM_CAMERA_H
