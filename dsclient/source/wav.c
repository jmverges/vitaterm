#include "wav.h"

#include <stdio.h>
#include <string.h>

typedef struct {
    uint16_t format;
    uint16_t channels;
    uint32_t hz;
    uint16_t bits;
    const uint8_t *data;
    uint32_t data_len;
} wav_info_t;

static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static uint16_t le16(const uint8_t *p)
{
    return (uint16_t)(p[0] | (p[1] << 8));
}

static int fail(char *why, size_t cap, const char *text)
{
    if (why && cap) snprintf(why, cap, "%s", text);
    return 0;
}

// Walk the chunks.  Every length is checked against what is actually there
// before it is used, so a file that lies about a size is refused rather than
// read past its end.
static int parse(const uint8_t *d, size_t len, wav_info_t *info,
                 char *why, size_t cap)
{
    memset(info, 0, sizeof(*info));
    if (!d || len < 12 || memcmp(d, "RIFF", 4) != 0 ||
        memcmp(d + 8, "WAVE", 4) != 0) {
        return fail(why, cap, "not a WAV file");
    }
    int have_fmt = 0;
    size_t at = 12;
    while (at + 8 <= len) {
        const uint32_t size = le32(d + at + 4);
        const uint8_t *body = d + at + 8;
        if (size > len - (at + 8)) return fail(why, cap, "truncated WAV");
        if (memcmp(d + at, "fmt ", 4) == 0) {
            if (size < 16) return fail(why, cap, "bad WAV format chunk");
            info->format = le16(body);
            info->channels = le16(body + 2);
            info->hz = le32(body + 4);
            info->bits = le16(body + 14);
            have_fmt = 1;
        } else if (memcmp(d + at, "data", 4) == 0) {
            info->data = body;
            info->data_len = size;
        }
        // Chunks are padded to an even length.
        at += 8 + (size_t)size + (size & 1u);
    }
    if (!have_fmt) return fail(why, cap, "WAV has no format chunk");
    if (!info->data) return fail(why, cap, "WAV has no sound data");
    // WAVE_FORMAT_EXTENSIBLE (0xFFFE) with plain PCM inside is common from
    // modern editors; its sub-format is not checked, and 8/16-bit is the
    // test that matters below.
    if (info->format != 1 && info->format != 0xFFFE) {
        return fail(why, cap, "compressed WAV; save it as PCM");
    }
    if (info->bits != 8 && info->bits != 16) {
        return fail(why, cap, "WAV must be 8- or 16-bit");
    }
    if (info->channels < 1 || info->channels > 2) {
        return fail(why, cap, "WAV must be mono or stereo");
    }
    if (info->hz < WAV_MIN_HZ || info->hz > WAV_MAX_HZ) {
        return fail(why, cap, "WAV rate must be 4-48 kHz");
    }
    return 1;
}

size_t wav_measure(const uint8_t *data, size_t len, uint32_t *hz,
                   char *why, size_t why_cap)
{
    wav_info_t info;
    if (!parse(data, len, &info, why, why_cap)) return 0;
    const size_t frame = (size_t)info.channels * (info.bits / 8u);
    const size_t samples = info.data_len / frame;
    if (samples == 0) {
        (void)fail(why, why_cap, "WAV is empty");
        return 0;
    }
    if (samples > (size_t)info.hz * WAV_MAX_SECONDS) {
        (void)fail(why, why_cap, "WAV longer than 3 seconds");
        return 0;
    }
    if (hz) *hz = info.hz;
    return samples + (samples & 1u);
}

int wav_decode(const uint8_t *data, size_t len, wav_t *out, size_t cap,
               char *why, size_t why_cap)
{
    if (!out || !out->pcm) return fail(why, why_cap, "no buffer");
    uint32_t hz = 0;
    const size_t want = wav_measure(data, len, &hz, why, why_cap);
    if (!want) return 0;
    if (want > cap) return fail(why, why_cap, "WAV does not fit");
    wav_info_t info;
    (void)parse(data, len, &info, NULL, 0);
    const size_t frame = (size_t)info.channels * (info.bits / 8u);
    const size_t samples = info.data_len / frame;
    for (size_t i = 0; i < samples; i++) {
        const uint8_t *f = info.data + i * frame;
        int32_t sum = 0;
        for (unsigned ch = 0; ch < info.channels; ch++) {
            // 8-bit WAV is unsigned with 128 as silence; 16-bit is signed.
            sum += info.bits == 8 ? ((int32_t)f[ch] - 128) * 256
                                  : (int16_t)le16(f + ch * 2u);
        }
        out->pcm[i] = (int16_t)(sum / (int32_t)info.channels);
    }
    for (size_t i = samples; i < want; i++) out->pcm[i] = 0;
    out->samples = want;
    out->hz = hz;
    return 1;
}
