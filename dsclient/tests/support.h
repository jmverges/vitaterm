// Helpers both suites use, and the two each suite provides its own way.
#ifndef DSTERM_TESTS_SUPPORT_H
#define DSTERM_TESTS_SUPPORT_H

#include "../source/sound.h"

// Provided by the suite: the terminal's resets the conductor, an extension's
// resets its own sound too; the key tick is the composite main.c performs.
void reset_sound(void);
void key_typed(int backspace);

// Start a cue and play it out to silence, recording the distinct
// frequencies; returns how many frames sounded.  drain_current() does the
// same for whatever is already playing, without starting anything.
int play_out(sound_cue_t cue, unsigned *seen, int max_seen, int *n_seen);
int drain_current(unsigned *seen, int max_seen, int *n_seen);

// Tick the chime `frames` times.
void chime_run(int frames);

// The defaults a fresh console boots with, and a reset.
void sound_restore_defaults(void);

// The connection tests (tests/test_connect.c): pairing, profiles,
// discovery and the boot menu.  Called from the terminal suite's main().
void test_connect_all(void);

// The SNES instrument, its lanes and its settings (tests/test_sound_snes.c).
void test_sound_snes_all(void);

// REMOTE: decoder, list, rows and actions (tests/test_remote.c).
void test_remote_all(void);

// SEAL: per-frame integrity (tests/test_seal.c).
void test_seal_all(void);

// The camera's pure half: pixels, checksum, pacing and state
// (tests/test_camera.c).
void test_camera_all(void);

// Seeded fuzzing of every parser that takes outside bytes, under the
// sanitizers (tests/test_fuzz.c).
void test_fuzz_all(void);

#endif  // DSTERM_TESTS_SUPPORT_H
