#include "support.h"

#include "../source/chime.h"

#include "check.h"

int failures = 0;
int checks = 0;

// ----------------------------------------------------------------- sound ----

// Walk a cue to completion, returning how many frames it sounded and recording
// every distinct frequency it visited.
int play_out(sound_cue_t cue, unsigned *seen, int max_seen, int *n_seen)
{
    reset_sound();
    sound_cue(cue);
    *n_seen = 0;
    int frames = 0;
    for (int i = 0; i < SOUND_MAX_FRAMES * 4; i++) {
        unsigned hz = 0;
        sound_tick(&hz);
        if (hz == 0 && frames > 0) break;   // cue finished
        if (hz) {
            frames++;
            if (*n_seen == 0 || seen[*n_seen - 1] != hz) {
                if (*n_seen < max_seen) seen[(*n_seen)++] = hz;
            }
        }
    }
    return frames;
}

int drain_current(unsigned *seen, int max_seen, int *n_seen)
{
    // Drain whatever the sequencer is playing to silence, recording the
    // distinct frequencies. Unlike play_out() this does not start a cue: it
    // reads the state chime.c left behind.
    *n_seen = 0;
    int frames = 0;
    for (int i = 0; i < SOUND_MAX_FRAMES * 4; i++) {
        unsigned hz = 0;
        sound_tick(&hz);
        if (hz == 0 && frames > 0) break;
        if (hz) {
            frames++;
            if (*n_seen == 0 || seen[*n_seen - 1] != hz) {
                if (*n_seen < max_seen) seen[(*n_seen)++] = hz;
            }
        }
    }
    return frames;
}

void chime_run(int frames)
{
    for (int i = 0; i < frames; i++) chime_tick();
}

void sound_restore_defaults(void)
{
    // The chip voice is what the suite's PSG checks are about; tests of the
    // SNES instrument select it for themselves (tests/test_sound_snes.c).
    sound_set_style(SOUND_STYLE_CHIP);
    sound_set_blips(0);
    sound_set_typing(1);
    sound_set_muted(0);
    sound_set_profile(SOUND_PROFILE_COMPANION);
    sound_set_volume(SOUND_VOLUME_NEAR);
    reset_sound();
}
