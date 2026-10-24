// The chimes: sound.c decides what plays (the same cues as the DS card),
// and this small mixer plays it on the Vita: one square-wave voice and two
// sample lanes, mixed on a thread of their own.
#include <stdlib.h>
#include <string.h>

#include <psp2/audioout.h>
#include <psp2/kernel/threadmgr.h>

#include "app.h"
#include "sfx_assets.h"
#include "sound.h"

#define MIX_RATE 48000
#define GRAIN 512
#define VOICES (1 + SOUND_LANES)

typedef struct {
    int active;
    int square;
    float phase, step, duty, volume;
    const int16_t *pcm;
    uint32_t frames;
} voice_t;

static voice_t voices[VOICES];
static SceKernelLwMutexWork lock;
static int port = -1;

static void mix(int16_t *out, int n)
{
    for (int i = 0; i < n; i++) {
        float acc = 0;
        for (int c = 0; c < VOICES; c++) {
            voice_t *v = &voices[c];
            if (!v->active) continue;
            if (v->square) {
                acc += (v->phase < v->duty ? 1.0f : -1.0f) * v->volume * 0.2f;
                v->phase += v->step;
                if (v->phase >= 1.0f) v->phase -= 1.0f;
            } else {
                const uint32_t at = (uint32_t)v->phase;
                if (at >= v->frames) {
                    v->active = 0;
                    continue;
                }
                acc += v->pcm[at] / 32768.0f * v->volume;
                v->phase += v->step;
            }
        }
        if (acc > 1) acc = 1;
        if (acc < -1) acc = -1;
        out[i] = (int16_t)(acc * 32000);
    }
}

static int audio_thread(SceSize args, void *argp)
{
    (void)args;
    (void)argp;
    static int16_t buf[2][GRAIN];
    int which = 0;
    for (;;) {
        sceKernelLockLwMutex(&lock, 1, NULL);
        mix(buf[which], GRAIN);
        sceKernelUnlockLwMutex(&lock, 1);
        sceAudioOutOutput(port, buf[which]);    // blocks until it plays
        which ^= 1;
    }
    return 0;
}

void audio_init(void)
{
    sceKernelCreateLwMutex(&lock, "vitaterm_audio", 0, 0, NULL);
    port = sceAudioOutOpenPort(SCE_AUDIO_OUT_PORT_TYPE_BGM, GRAIN, MIX_RATE,
                               SCE_AUDIO_OUT_MODE_MONO);
    if (port < 0) return;
    const SceUID t = sceKernelCreateThread("vitaterm_audio", audio_thread, 0x10000100,
                                           0x10000, 0, 0, NULL);
    if (t >= 0) sceKernelStartThread(t, 0, NULL);
}

// The DS's IMA-ADPCM: a 4-byte header (first sample, step index), then two
// samples a byte, low nibble first.  Decoded once per sample and kept.
static const int16_t *decoded(const sfx_sample_t *s, uint32_t *frames)
{
    static struct { const sfx_sample_t *s; int16_t *pcm; uint32_t frames; } cache[64];
    for (int i = 0; i < 64; i++) {
        if (cache[i].s == s) {
            *frames = cache[i].frames;
            return cache[i].pcm;
        }
    }
    if (s->format == SFX_FORMAT_PCM16) {
        *frames = s->bytes / 2;
        return (const int16_t *)s->data;
    }
    static const int index_table[16] = { -1, -1, -1, -1, 2, 4, 6, 8,
                                         -1, -1, -1, -1, 2, 4, 6, 8 };
    static const int step_table[89] = {
        7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50,
        55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279,
        307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282,
        1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871,
        5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818,
        18500, 20350, 22385, 24623, 27086, 29794, 32767,
    };
    if (s->bytes < 4) {
        *frames = 0;
        return NULL;
    }
    int pred = (int16_t)(s->data[0] | (s->data[1] << 8));
    int index = s->data[2] > 88 ? 88 : s->data[2];
    const uint32_t n = (s->bytes - 4) * 2;
    int16_t *out = malloc(n * sizeof(int16_t));
    if (!out) {
        *frames = 0;
        return NULL;
    }
    for (uint32_t i = 0; i < n; i++) {
        const uint8_t b = s->data[4 + i / 2];
        const int nib = (i & 1) ? (b >> 4) : (b & 15);
        const int step = step_table[index];
        int diff = step >> 3;
        if (nib & 1) diff += step >> 2;
        if (nib & 2) diff += step >> 1;
        if (nib & 4) diff += step;
        pred += (nib & 8) ? -diff : diff;
        if (pred > 32767) pred = 32767;
        if (pred < -32768) pred = -32768;
        index += index_table[nib];
        if (index < 0) index = 0;
        if (index > 88) index = 88;
        out[i] = (int16_t)pred;
    }
    for (int i = 0; i < 64; i++) {
        if (!cache[i].s) {
            cache[i].s = s;
            cache[i].pcm = out;
            cache[i].frames = n;
            break;
        }
    }
    *frames = n;
    return out;
}

void audio_tick(void)
{
    if (port < 0) return;
    sound_voice_t voice;
    if (sound_tick_voice(&voice)) {
        static const float duty_high[8] = { .125f, .25f, .375f, .5f, .625f, .75f, .875f, 0 };
        sceKernelLockLwMutex(&lock, 1, NULL);
        voice_t *v = &voices[0];
        memset(v, 0, sizeof(*v));
        if (voice.hz) {
            v->active = 1;
            v->square = 1;
            v->duty = duty_high[voice.duty & 7];
            v->step = (float)voice.hz / MIX_RATE;
            v->volume = voice.volume / 127.0f;
        }
        sceKernelUnlockLwMutex(&lock, 1);
    }
    for (unsigned lane = 0; lane < SOUND_LANES; lane++) {
        sound_pcm_t pcm;
        if (!sound_take_pcm(lane, &pcm)) continue;
        uint32_t frames = 0;
        const int16_t *data = pcm.sample ? decoded(pcm.sample, &frames) : NULL;
        sceKernelLockLwMutex(&lock, 1, NULL);
        voice_t *v = &voices[1 + lane];
        memset(v, 0, sizeof(*v));
        if (data) {
            v->active = 1;
            v->pcm = data;
            v->frames = frames;
            v->step = (float)pcm.hz / MIX_RATE;
            v->volume = pcm.volume / 127.0f;
        }
        sceKernelUnlockLwMutex(&lock, 1);
    }
}
