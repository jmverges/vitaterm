// The console's sound cues.
//
// Conducted one frame at a time, in one of two styles:
//
//   SNES  sampled cues (dsclient/sounds/, compiled in by tools/sfx_build.py)
//         on two hardware PCM channels -- one for the cues that mean
//         something, one for the small ticks of your own hands -- so typing
//         never cuts off "your answer is ready", and the answer never mutes
//         your typing.  The default.
//   CHIP  the original single square-wave voice, note tables below.
//
// The conductor never queues sounds: an important event may interrupt a
// lesser one, while little events are simply allowed to pass.  That is
// deliberate.  A handheld that remembers every small thing it wanted to say
// becomes a handheld that never stops talking.  Both styles share the same
// priorities, cooldowns and profile gates, so switching style changes the
// instrument and never what is said or how often.
//
// This file is pure C with no libnds dependency.  sound.c owns note tables,
// sample choice and scheduling; main.c maps sound_voice_t to the PSG calls and
// sound_pcm_t to the sample calls.  Keeping that seam plain makes the musical
// rules host-testable.

#ifndef DSTERM_SOUND_H
#define DSTERM_SOUND_H

#include <stdint.h>

#include "sfx_assets.h"

typedef enum {
    SOUND_STYLE_SNES = 0,         // sampled cues (default)
    SOUND_STYLE_CHIP,             // the square-wave voice
    SOUND_STYLE_COUNT
} sound_style_t;

typedef enum {
    SOUND_PROFILE_STUDIO = 0,     // sparse, crisp, consequential only
    SOUND_PROFILE_COMPANION,      // the warm, complete response arc (default)
    SOUND_PROFILE_FIELD,          // Companion plus focus and switch feedback
    SOUND_PROFILE_COUNT
} sound_profile_t;

typedef enum {
    SOUND_VOLUME_LOW = 0,         // close-held / quiet room
    SOUND_VOLUME_NEAR,            // normal handheld distance (default)
    SOUND_VOLUME_ROOM,            // audible across a small room
    SOUND_VOLUME_COUNT
} sound_volume_t;

// These values intentionally match libnds's PSG duty order, but sound.h does
// not include libnds.  main.c can cast SOUND_DUTY_50 to DutyCycle directly.
typedef enum {
    SOUND_DUTY_12 = 0,
    SOUND_DUTY_25,
    SOUND_DUTY_37,
    SOUND_DUTY_50,
    SOUND_DUTY_62,
    SOUND_DUTY_75,
    SOUND_DUTY_87,
} sound_duty_t;

// What the hardware should sound this frame.  hz == 0 means silence; duty and
// volume are then zero too.  Volume is in the libnds PSG range 0..127.
typedef struct {
    uint16_t hz;
    uint8_t duty;
    uint8_t volume;
} sound_voice_t;

// A voiced note is a pitch, duration, duty and relative loudness (0..100
// percent of the selected room volume).  It is public so host tests and future
// cue work can describe a note precisely; cue tables themselves remain private
// so callers cannot mutate the conductor.
typedef struct {
    uint16_t hz;
    uint8_t frames;
    uint8_t duty;
    uint8_t volume;
} sound_note_t;

typedef enum {
    SOUND_NONE = 0,
    SOUND_SENT = 1,    // a lone CR left the console
    SOUND_THINK = 2,   // sent, then quiet
    SOUND_OUTPUT = 3,  // sustained activity followed the quiet
    SOUND_DONE = 4,    // observed output settled
    SOUND_DROP = 5,    // the link went away (may interrupt)
    SOUND_LINK = 6,    // the link came up

    // Keep the original 1..6 range stable.  Older host tests intentionally
    // probe SOUND_LINK + 1 as invalid, so new local-only events live in a
    // separate extension range rather than silently changing that contract.
    SOUND_LANTERN = 16, // a non-active session gained output
    SOUND_FOCUS = 17,   // focus entered or moved (Field only)
    SOUND_SWITCH = 18,  // session switch requested
    SOUND_PREVIEW = 19, // confirm a newly chosen audible profile
    SOUND_KEY = 20,     // the user's own keypress (every profile; see below)
    SOUND_DENY = 21,    // a press that could not do anything (every profile)
    SOUND_BOOT = 22,    // the card started (once per power-on)
    SOUND_BLIP = 23,    // texture while an answer streams (BLIPS setting)
    SOUND_APPROVE = 24, // the session is waiting for a permission decision
    SOUND_QUESTION = 25,// the session is waiting for an answer
} sound_cue_t;

// Preserve the original contiguous core range for callers that iterate from
// SOUND_SENT through SOUND_LINK.  Extension cues intentionally are not
// contiguous with it (see the enum note above).
#define SOUND_CUE_COUNT 6
#define SOUND_CUE_TOTAL_COUNT 16

// The typing cue is the one cue that is feedback on the user's own action
// rather than an observation of the link, so it lives in every profile
// including Studio: sparseness governs how much the *model* is allowed to
// speak, never whether the user's own fingers answer.  Enter is deliberately
// excluded -- a submitted line already owns SENT -- and navigation (arrows,
// scroll chords, overlays) never types.
void sound_key_typed(int backspace);

// The conductor's face for anything that wants to play under the cues.
//
// sound_phrase_start() voices an ad-hoc phrase at the lowest priority: it is
// refused while anything at all is sounding or in its release gap, any cue
// replaces it, and it has no cooldown slot -- whoever builds phrases owns
// their spacing.  sound_phrase_stop() ends one and touches no cue.
// sound_idle() is the refusal test, and sound_frame() the conductor's own
// VBlank count, so a caller can keep its spacing on the same clock.
int sound_phrase_start(const sound_note_t *notes, unsigned count);
void sound_phrase_stop(void);
int sound_idle(void);
unsigned sound_frame(void);

// Setters return 1 for an accepted value and 0 for an invalid enum value.
// Changing a profile stops any old-profile phrase; it never plays a preview on
// its own, so a caller can deliberately keep mute visual-only.
int sound_set_profile(sound_profile_t profile);
sound_profile_t sound_profile(void);
const char *sound_profile_name(sound_profile_t profile);  // uppercase UI label

int sound_set_volume(sound_volume_t volume);
sound_volume_t sound_volume(void);
const char *sound_volume_name(sound_volume_t volume);     // uppercase UI label

// Start a named cue.  The conductor applies the profile, priority, cooldown,
// and no-queue rules.  SOUND_DROP can interrupt; lower-priority arrivals are
// dropped while a stronger phrase is playing.
void sound_cue(sound_cue_t cue);

// Play the current profile's short "settled" confirmation phrase.  It is a
// no-op while muted, which lets the UI confirm MUTE visually only.
void sound_preview(void);


// Mute covers every cue, phrase, and keypress.  Muting mid-cue abandons it on
// the next tick rather than letting a hidden phrase resume after unmute.
void sound_set_muted(int muted);
int sound_muted(void);

// The instrument.  Changing it stops whatever the old one was playing.
int sound_set_style(sound_style_t style);
sound_style_t sound_style(void);
const char *sound_style_name(sound_style_t style);       // uppercase UI label

// Blips: the text-box chatter of a 16-bit RPG while an answer streams in.
// Off by default.  When on, sound_blip() is the conductor's half -- one small
// sound, refused if it would talk over anything -- and chime.c owns how often
// it is asked, which thins out the longer an answer runs (see chime.h), so a
// long answer becomes quieter rather than a metronome.
// WRITING: 0 off, 1 low, 2 mid, 3 high.  Any non-zero level enables blips;
// how often they come is chime.c's, by level.
void sound_set_blips(int level);
// Settings' TYPING: off silences your own key and cursor clicks only.
void sound_set_typing(int on);
int sound_typing(void);
int sound_blips(void);
// One blip, its pitch nudged by `glyph` so different text sounds different.
// Returns 1 if it sounded.
int sound_blip(unsigned char glyph);

// --------------------------------------------------- your own sounds ---
//
// A bank's built-in takes can be replaced by up to SOUND_CUSTOM_TAKES of the
// card's own (main.c loads /dsterm/sounds/<bank>.wav, <bank>-2.wav,
// <bank>-3.wav; wav.h reads them).  The takes are borrowed and must outlive
// the conductor.  A custom take plays at its bank's built-in loudness, so a
// replacement mastered to full scale sits where the original did.  Passing
// count 0 restores the built-in takes.
#define SOUND_CUSTOM_TAKES 3
int sound_set_custom(unsigned bank, const sfx_sample_t *takes, unsigned count);
unsigned sound_custom_banks(void);   // how many banks are the card's own
// The gain a bank's built-in takes carry, for a custom take to borrow.
uint8_t sound_bank_gain(unsigned bank);

// ------------------------------------------------------- the PCM lanes ---
//
// The SNES style plays samples on two hardware channels.  Each lane holds at
// most one sound; a new start replaces what the lane was playing only by the
// same rules the PSG voice follows.  main.c collects what changed once per
// frame with sound_take_pcm() and turns it into soundPlaySampleChannel() /
// soundKill().
enum {
    SOUND_LANE_CUE = 0,    // named cues: SENT, DONE, LINK, DROP, ...
    SOUND_LANE_TICK = 1,   // keys, the menu cursor, blips
    SOUND_LANES = 2,
};

typedef struct {
    const sfx_sample_t *sample;   // NULL: silence the lane
    uint16_t hz;                  // playback rate, a small jitter applied
    uint8_t volume;               // 0..127, already scaled by the room volume
    uint32_t serial;              // bumps on every start, even of one sample
} sound_pcm_t;

// 1 when the lane must change -- start `out->sample`, or stop when it is NULL
// -- since the last call.  0 means leave the hardware alone.  Call once per
// frame per lane, after sound_tick_voice().
int sound_take_pcm(unsigned lane, sound_pcm_t *out);
// Whether a lane is sounding, for tests and the diagnostics line.
int sound_lane_busy(unsigned lane);

// Advance the conductor one frame.  Returns 1 only when hz, duty, or volume
// differs from the previous frame, so the hardware is restarted only when it
// truly needs a new voice.
int sound_tick_voice(sound_voice_t *voice);

// Compatibility wrapper for the original hardware path.  It advances exactly
// once and writes just the pitch.  New main.c code should use
// sound_tick_voice() so profile duty and volume reach the PSG.
int sound_tick(unsigned *hz);

// Introspection for UI/tests.  SOUND_NONE means no phrase is currently active.
sound_cue_t sound_active_cue(void);
int sound_playing(void);

// Longest a normal cue may sound, in frames.  The old six cues stay within the
// same small budget; extension cues are shorter still.
#define SOUND_MAX_FRAMES 32

// Frequency range every note must sit inside.  Below this the DS speakers are
// muddy; above it the note is piercing at arm's length.
#define SOUND_HZ_MIN 200
#define SOUND_HZ_MAX 1100

#define SOUND_PSG_VOLUME_MAX 127

// Forget playback and cooldown state while preserving the user's selected
// profile, volume, and mute setting.  This is used by host tests and is not a
// preference reset.
void sound_reset(void);

#endif  // DSTERM_SOUND_H
