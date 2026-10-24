// The camera: aim, shoot, send the picture to the computer, then write what
// Claude should do with it.
//
// The wire is the DSi camera's (docs/PROTOCOL.md, "Camera").  The Vita's
// sensor gives 640x480 4:2:0 planes directly, which is exactly the FULL
// picture format: no conversion, only a copy.
#include <stdio.h>
#include <string.h>

#include <psp2/camera.h>
#include <psp2/ctrl.h>

#include "app.h"
#include "camera.h"

#define CAM_W 640
#define CAM_H 480
#define VIEW_W 320
#define VIEW_H 240

static uint8_t plane_y[CAM_W * CAM_H] __attribute__((aligned(256)));
static uint8_t plane_u[CAM_W * CAM_H / 4] __attribute__((aligned(256)));
static uint8_t plane_v[CAM_W * CAM_H / 4] __attribute__((aligned(256)));
static uint8_t packed[CAMERA_PHOTO_FULL_BYTES];

static vita2d_texture *view;
static int device = SCE_CAMERA_DEVICE_BACK;
static int open_device = -1;

typedef enum { UI_LIVE, UI_REVIEW, UI_SENDING, UI_SAVING, UI_SAVED, UI_FAILED } ui_t;
static ui_t ui;
static char fail_text[96];
static camera_upload_t upload;
static camera_pacer_t pacer;
static uint32_t crc;
static unsigned long sending_at, progress_at;
static unsigned last_percent;
static photo_saved_t saved_answer;

static int wire_camera(void)
{
    return device == SCE_CAMERA_DEVICE_BACK ? PROTO_CAMERA_OUTER : PROTO_CAMERA_INNER;
}

static int send_state(int state)
{
    if (!conn.camera_ready) return 0;
    uint8_t out[PROTO_HEADER_SIZE + 1];
    return link_send_frame(out, proto_encode_camera(out, state));
}

static int sensor_open(void)
{
    SceCameraInfo info;
    memset(&info, 0, sizeof(info));
    info.size = sizeof(info);
    info.priority = SCE_CAMERA_PRIORITY_SHARE;
    info.format = SCE_CAMERA_FORMAT_YUV420_PLANE;
    info.resolution = SCE_CAMERA_RESOLUTION_640_480;
    info.framerate = SCE_CAMERA_FRAMERATE_30_FPS;
    info.width = CAM_W;
    info.height = CAM_H;
    info.range = 1;
    info.sizeIBase = sizeof(plane_y);
    info.sizeUBase = sizeof(plane_u);
    info.sizeVBase = sizeof(plane_v);
    info.pIBase = plane_y;
    info.pUBase = plane_u;
    info.pVBase = plane_v;
    info.pitch = 0;
    info.buffer = 0;            // the buffers given here, not per read
    if (sceCameraOpen(device, &info) < 0) return 0;
    if (sceCameraStart(device) < 0) {
        sceCameraClose(device);
        return 0;
    }
    open_device = device;
    return 1;
}

static void sensor_close(void)
{
    if (open_device < 0) return;
    sceCameraStop(open_device);
    sceCameraClose(open_device);
    open_device = -1;
}

static int sensor_read(void)
{
    if (open_device < 0 || !sceCameraIsActive(open_device)) return 0;
    SceCameraRead r;
    memset(&r, 0, sizeof(r));
    r.size = sizeof(r);
    r.mode = 0;     // wait for the next frame
    return sceCameraRead(open_device, &r) >= 0;
}

static inline uint8_t clamp8(int v) { return v < 0 ? 0 : v > 255 ? 255 : (uint8_t)v; }

// The viewfinder: every other pixel of the planes, to a 320x240 texture
// drawn at twice the size.  The front camera is shown mirrored, as a mirror.
static void update_view(void)
{
    uint32_t *px = vita2d_texture_get_datap(view);
    const unsigned stride = vita2d_texture_get_stride(view) / 4;
    const int mirror = device == SCE_CAMERA_DEVICE_FRONT;
    for (int y = 0; y < VIEW_H; y++) {
        const uint8_t *ys = plane_y + (size_t)(2 * y) * CAM_W;
        const uint8_t *us = plane_u + (size_t)y * (CAM_W / 2);
        const uint8_t *vs = plane_v + (size_t)y * (CAM_W / 2);
        uint32_t *dst = px + (size_t)y * stride;
        for (int x = 0; x < VIEW_W; x++) {
            const int yy = ys[2 * x], u = us[x] - 128, v = vs[x] - 128;
            const uint8_t r = clamp8(yy + ((359 * v) >> 8));
            const uint8_t g = clamp8(yy - ((88 * u + 183 * v) >> 8));
            const uint8_t b = clamp8(yy + ((454 * u) >> 8));
            dst[mirror ? VIEW_W - 1 - x : x] = 0xFF000000u | ((uint32_t)b << 16) |
                                               ((uint32_t)g << 8) | r;
        }
    }
}

// The computer's live view: 128x96 in sixteen greys, two pixels a byte.
static void send_preview(int still)
{
    if (!conn.camera_ready || !camera_pacer_ready(&pacer, (uint32_t)frame_count())) return;
    static uint8_t greys[CAMERA_PREVIEW_BYTES];
    for (int y = 0; y < CAMERA_PREVIEW_H; y++) {
        for (int x = 0; x < CAMERA_PREVIEW_W; x += 2) {
            const uint8_t *row = plane_y + (size_t)(y * 5) * CAM_W;
            const unsigned a = row[x * 5] * 15u / 255u, b = row[(x + 1) * 5] * 15u / 255u;
            greys[(y * CAMERA_PREVIEW_W + x) / 2] = (uint8_t)((a << 4) | b);
        }
    }
    camera_pacer_sent(&pacer, (uint32_t)frame_count());
    static uint8_t out[PROTO_HEADER_SIZE + 3 + CAMERA_VIEW_BYTES_MAX];
    const size_t n = proto_encode_camera_frame(out, CAMERA_VIEW_GREY,
                                               still ? PROTO_PREVIEW_FLAG_STILL : 0,
                                               pacer.seq, greys);
    if (n) link_send_frame(out, n);
}

// The shutter: the planes as they are now, before the sensor writes the next
// frame over them.
static void capture(void)
{
    memcpy(packed, plane_y, sizeof(plane_y));
    memcpy(packed + sizeof(plane_y), plane_u, sizeof(plane_u));
    memcpy(packed + sizeof(plane_y) + sizeof(plane_u), plane_v, sizeof(plane_v));
    crc = camera_crc32_update(0, packed, CAMERA_PHOTO_FULL_BYTES);
}

static void start_upload(void)
{
    camera_upload_init(&upload, CAMERA_PHOTO_FORMAT_FULL);
    sending_at = progress_at = frame_count();
    last_percent = 0;
    ui = UI_SENDING;
}

static void fail(const char *why)
{
    snprintf(fail_text, sizeof(fail_text), "%s", why);
    ui = UI_FAILED;
}

static void upload_tick(void)
{
    uint32_t off = 0, len = 0;
    while (camera_upload_next(&upload, &off, &len)) {
        static uint8_t out[PROTO_HEADER_SIZE + 4 + PROTO_PHOTO_CHUNK_MAX];
        const size_t n = proto_encode_photo_chunk(out, off, packed + off, (uint16_t)len);
        if (!link_send_frame(out, n)) {
            fail("the connection dropped");
            return;
        }
    }
    const unsigned pc = camera_upload_percent(&upload);
    if (pc != last_percent) {
        last_percent = pc;
        progress_at = frame_count();
    } else if (frame_count() - progress_at > 10 * 60) {
        fail("the computer stopped taking the picture");
        return;
    }
    if (camera_upload_all_sent(&upload)) {
        uint8_t out[PROTO_HEADER_SIZE + 10];
        const size_t n = proto_encode_photo_end(out, CAMERA_PHOTO_FORMAT_FULL,
                                                (uint8_t)wire_camera(), crc);
        if (!link_send_frame(out, n)) {
            fail("the connection dropped");
            return;
        }
        ui = UI_SAVING;
        sending_at = frame_count();
    }
}

void cam_on_ack(uint8_t kind, uint32_t value)
{
    if (kind == PROTO_ACK_PREVIEW) {
        (void)camera_pacer_ack(&pacer, (uint8_t)value);
    } else if (ui == UI_SENDING || ui == UI_SAVING) {
        if (!camera_upload_ack(&upload, value)) fail("the computer lost count of the picture");
    }
}

void cam_on_saved(const photo_saved_t *saved)
{
    if (ui != UI_SAVING) {
        // Out of turn: nothing will name it, so it is not kept.
        if (saved->status == PROTO_SAVED_OK && saved->id && conn.camera_ready) {
            uint8_t out[PROTO_HEADER_SIZE + 2];
            link_send_frame(out, proto_encode_photo_discard(out, saved->id));
        }
        return;
    }
    if (saved->status != PROTO_SAVED_OK) {
        static const char *const why[] = {
            "", "the computer is busy; try again", "it arrived damaged; try again",
            "the computer could not save it", "wait a moment, then try again",
            "the photo folder's name is not plain ASCII",
        };
        fail(saved->status <= PROTO_SAVED_MAX ? why[saved->status] : "the computer refused it");
        return;
    }
    saved_answer = *saved;
    ui = UI_SAVED;
}

void cam_forget(void)
{
    if (ui == UI_SENDING || ui == UI_SAVING) fail("the connection dropped");
    camera_pacer_init(&pacer);
}

static void draw(void)
{
    gfx_begin();
    gfx_rect(0, 0, SCREEN_W, SCREEN_H, 0xFF000000u);
    vita2d_draw_texture_scale(view, (SCREEN_W - 2 * VIEW_W) / 2 - 80, 24, 2.0f, 2.0f);
    const int px = SCREEN_W - 230;
    gfx_text(px, 60, COL_ACCENT, 1.3f, "Camera");
    gfx_text(px, 90, COL_DIM, 0.9f,
             device == SCE_CAMERA_DEVICE_BACK ? "back camera" : "front camera");
    const char *line = "";
    char buf[96];
    switch (ui) {
    case UI_LIVE:
        line = conn.camera_can_save ? "Aim, then Cross." : "This computer cannot save pictures.";
        break;
    case UI_REVIEW:
        line = "Send this one?";
        break;
    case UI_SENDING:
        snprintf(buf, sizeof(buf), "Sending... %u%%", camera_upload_percent(&upload));
        line = buf;
        gfx_rect(px, 160, 200, 10, 0xFF404040u);
        gfx_rect(px, 160, 2 * (int)camera_upload_percent(&upload), 10, COL_ACCENT);
        break;
    case UI_SAVING:
        line = "Saving on the computer...";
        break;
    case UI_SAVED:
        line = "Saved.";
        break;
    case UI_FAILED:
        snprintf(buf, sizeof(buf), "Not sent: %s.", fail_text);
        line = buf;
        break;
    }
    gfx_wrap(px, 120, 210, COL_TEXT, 0.95f, line);
    if (ui == UI_LIVE) {
        static const char *const h[] = { "cross", "Take", "triangle", "Flip", "circle", "Close" };
        gfx_hints(h, 3);
    } else if (ui == UI_REVIEW) {
        static const char *const h[] = { "cross", "Send to Claude", "square", "Retake",
                                         "circle", "Close" };
        gfx_hints(h, 3);
    } else if (ui == UI_FAILED) {
        static const char *const h[] = { "square", "Try again", "circle", "Close" };
        gfx_hints(h, 2);
    } else {
        static const char *const h[] = { "circle", "Stop" };
        gfx_hints(h, 1);
    }
    gfx_end();
}

void cam_run(void)
{
    if (conn.state != LINK_UP) {
        gfx_working("Camera", "Not connected to a computer.");
        return;
    }
    if (!conn.camera_ready) {
        for (int f = 0; f < 150 && !(in.down & SCE_CTRL_CIRCLE); f++) {
            app_tick();
            gfx_working("Camera", "This computer's vitaterm does not take pictures "
                                  "(it may run with --no-camera).");
        }
        return;
    }
    if (!view) {
        view = vita2d_create_empty_texture_format(VIEW_W, VIEW_H,
                                                  SCE_GXM_TEXTURE_FORMAT_A8B8G8R8);
    }
    if (!sensor_open()) {
        for (int f = 0; f < 150 && !(in.down & SCE_CTRL_CIRCLE); f++) {
            app_tick();
            gfx_working("Camera", "The camera would not open.");
        }
        return;
    }
    ui = UI_LIVE;
    camera_pacer_init(&pacer);
    send_state(wire_camera());

    for (;;) {
        app_tick();
        if (conn.state != LINK_UP && ui != UI_FAILED) fail("the connection dropped");
        if (ui == UI_LIVE && sensor_read()) {
            update_view();
            send_preview(0);
        }
        if (ui == UI_SENDING) upload_tick();
        if (ui == UI_SAVING && frame_count() - sending_at > 30 * 60) {
            fail("the computer did not answer");
        }
        draw();

        if (ui == UI_SAVED) break;
        if (in.down & SCE_CTRL_CIRCLE) {
            if (ui == UI_SENDING || ui == UI_SAVING) {
                fail("stopped");
                continue;
            }
            break;
        }
        if (ui == UI_LIVE && (in.down & SCE_CTRL_TRIANGLE)) {
            sensor_close();
            device = device == SCE_CAMERA_DEVICE_BACK ? SCE_CAMERA_DEVICE_FRONT
                                                      : SCE_CAMERA_DEVICE_BACK;
            if (!sensor_open()) {
                fail("that camera would not open");
                continue;
            }
            send_state(wire_camera());
        }
        if (ui == UI_LIVE && (in.down & SCE_CTRL_CROSS) && conn.camera_can_save) {
            ui = UI_REVIEW;
            capture();
            camera_pacer_init(&pacer);
            send_preview(1);
        } else if (ui == UI_REVIEW && (in.down & SCE_CTRL_CROSS)) {
            start_upload();
        } else if ((ui == UI_REVIEW || ui == UI_FAILED) && (in.down & SCE_CTRL_SQUARE)) {
            ui = UI_LIVE;
            if (open_device < 0 && !sensor_open()) fail("the camera would not open");
        }
    }
    sensor_close();
    if (conn.state == LINK_UP) send_state(PROTO_CAMERA_OFF);
    if (ui != UI_SAVED) return;

    // The caption: what Claude should do with the picture.
    photo_pending_id = saved_answer.id;
    char initial[260], text[1024];
    snprintf(initial, sizeof(initial), "Picture from the Vita camera: %s ", saved_answer.path);
    if (input_text("What should Claude do with the picture?", initial, text, sizeof(text), 0) &&
        text[0]) {
        photo_pending_id = 0;      // sent, not cancelled: the picture stays
        link_text(text, strlen(text));
        link_key("\r", 1);
    } else {
        link_photo_discard();
    }
}
