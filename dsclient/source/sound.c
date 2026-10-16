// Log the conductor's decisions so a photograph of the STATUS screen answers
// "is it firing at all" on hardware.  Ad-hoc phrases are deliberately
// excluded: texture can fire several times a second and would flood the
// 24-line ring that is this program's only debugging surface.
#include "log.h"

#include "sound.h"

#include <stddef.h>
#include <string.h>

// All phrases live in C major and share a small set of registers.  This is a
// vocabulary rather than a sound-effect collection: high means a sent line is
// sitting in quiet, low means activity has arrived, and the middle register
// resolves when the observed output settles.
//
// `volume` is a percentage of the selected room volume.  Keeping both it and
// duty on every note lets a quieter lantern feel related to DONE without just
// being DONE played softer by the hardware.
static const sound_note_t CUE_SENT[] = {
    {523, 4, SOUND_DUTY_50, 88}, {784, 5, SOUND_DUTY_50, 100},
};
static const sound_note_t CUE_THINK[] = {
    {880, 4, SOUND_DUTY_37, 72}, {988, 4, SOUND_DUTY_37, 76},
    {1047, 6, SOUND_DUTY_50, 82},
};
static const sound_note_t CUE_OUTPUT[] = {
    {220, 3, SOUND_DUTY_50, 86}, {262, 6, SOUND_DUTY_50, 92},
};
static const sound_note_t CUE_DONE[] = {
    {523, 4, SOUND_DUTY_37, 72}, {659, 4, SOUND_DUTY_50, 84},
    {784, 7, SOUND_DUTY_50, 100},
};
static const sound_note_t CUE_DROP[] = {
    {440, 4, SOUND_DUTY_50, 90}, {294, 7, SOUND_DUTY_37, 78},
};
static const sound_note_t CUE_LINK[] = {
    {294, 3, SOUND_DUTY_37, 72}, {440, 3, SOUND_DUTY_50, 84},
    {587, 6, SOUND_DUTY_50, 100},
};

// The lantern borrows the opening and resolution of DONE, but is intentionally
// too quiet and brief to sound like an answer on the session currently shown.
static const sound_note_t CUE_LANTERN[] = {
    {523, 2, SOUND_DUTY_25, 44}, {784, 4, SOUND_DUTY_37, 56},
};
static const sound_note_t CUE_FOCUS[] = {
    {659, 2, SOUND_DUTY_25, 52}, {784, 3, SOUND_DUTY_37, 60},
};
static const sound_note_t CUE_SWITCH[] = {
    {392, 3, SOUND_DUTY_37, 68}, {523, 4, SOUND_DUTY_50, 76},
};
static const sound_note_t CUE_PREVIEW[] = {
    {523, 3, SOUND_DUTY_37, 68}, {659, 3, SOUND_DUTY_50, 80},
    {784, 6, SOUND_DUTY_50, 94},
};

// The typing cue is two short, quiet blips: a middle-register tick for a
// character and a lower one for delete, so deleting feels like deleting
// without either note leaving the C-major family or the 200..1100 Hz band.
// Two frames is the shortest note the conductor can voice; at 60 fps that is
// 33 ms per keypress, and the 2-frame cooldown caps the stream at 15/s even
// at hold-repeat speed.
static const sound_note_t CUE_KEY_CHAR[] = {
    {660, 2, SOUND_DUTY_25, 32},
};
static const sound_note_t CUE_KEY_BS[] = {
    {392, 2, SOUND_DUTY_25, 30},
};

// A press that could not do anything: one low, short, quiet note -- a shrug,
// not an alarm.  Below the drop's register so it never reads as the link.
static const sound_note_t CUE_DENY[] = {
    {247, 3, SOUND_DUTY_25, 50}, {220, 4, SOUND_DUTY_25, 40},
};
// The chip's boot phrase is the link's, a step higher: the same family.
static const sound_note_t CUE_BOOT[] = {
    {392, 3, SOUND_DUTY_37, 64}, {523, 3, SOUND_DUTY_50, 76},
    {659, 3, SOUND_DUTY_50, 84}, {784, 6, SOUND_DUTY_50, 92},
};
// The two cues that ask something of you.  APPROVE is the most distinct
// shape in the set -- a quick repeated high note, then a step down, a knock
// at the door -- because it is the one sound worth looking up for.  QUESTION
// is a rising sixth, the interval of a spoken "hm?".
static const sound_note_t CUE_APPROVE[] = {
    {1047, 3, SOUND_DUTY_50, 84}, {0, 2, 0, 0}, {1047, 3, SOUND_DUTY_50, 88},
    {784, 8, SOUND_DUTY_50, 100},
};
static const sound_note_t CUE_QUESTION[] = {
    {523, 5, SOUND_DUTY_37, 80}, {880, 8, SOUND_DUTY_50, 96},
};
// The chip's blip: one pentatonic note, chosen by the glyph that arrived.
static const uint16_t BLIP_PENTATONIC[] = { 523, 587, 659, 784, 880 };
static sound_note_t blip_note[1];


#define PROFILE_BIT(p) (1u << (unsigned)(p))
#define PROFILE_ALL (PROFILE_BIT(SOUND_PROFILE_STUDIO) | \
                     PROFILE_BIT(SOUND_PROFILE_COMPANION) | \
                     PROFILE_BIT(SOUND_PROFILE_FIELD))

enum {
    PRIORITY_MURMUR = 10,
    PRIORITY_BLIP = 12,
    // A keypress sits just above the murmur texture and below every named
    // cue.  It may replace a murmur -- that is the model's voice, and the
    // user's own fingers should answer over it -- but it can never interrupt
    // FOCUS, SENT, DONE, or anything else a cue is for.
    PRIORITY_KEY = 15,
    PRIORITY_FOCUS = 25,
    PRIORITY_DENY = 30,
    PRIORITY_THINK = 42,
    PRIORITY_SENT = 48,
    PRIORITY_LANTERN = 52,
    PRIORITY_OUTPUT = 60,
    PRIORITY_SWITCH = 68,
    PRIORITY_DONE = 72,
    PRIORITY_QUESTION = 76,
    PRIORITY_APPROVE = 80,
    PRIORITY_LINK = 88,
    PRIORITY_DROP = 100,
};

typedef struct {
    const sound_note_t *notes;
    uint8_t count;
    uint8_t priority;
    uint8_t cooldown;
    uint8_t profile_mask;
} cue_def_t;

// Studio says only the consequential things.  Companion fills in the honest
// response arc.  Field adds deliberately restrained navigation feedback.  All
// three retain the same pitches, so changing profile feels like changing the
// room rather than changing to a different device.
//
// FOCUS stays Field-only: per-move ticking while walking a list is exactly the
// tactile distinction Field exists for.  SWITCH is Companion too -- a session
// change is a deliberate, consequential action, and a silent one feels broken
// in the warm profile.  Both stay out of Studio.
static const cue_def_t DEF_SENT = {
    CUE_SENT, 2, PRIORITY_SENT, 8, PROFILE_ALL,
};
static const cue_def_t DEF_THINK = {
    CUE_THINK, 3, PRIORITY_THINK, 45,
    PROFILE_BIT(SOUND_PROFILE_COMPANION) | PROFILE_BIT(SOUND_PROFILE_FIELD),
};
static const cue_def_t DEF_OUTPUT = {
    CUE_OUTPUT, 2, PRIORITY_OUTPUT, 16,
    PROFILE_BIT(SOUND_PROFILE_COMPANION) | PROFILE_BIT(SOUND_PROFILE_FIELD),
};
static const cue_def_t DEF_DONE = {
    CUE_DONE, 3, PRIORITY_DONE, 90, PROFILE_ALL,
};
static const cue_def_t DEF_DROP = {
    CUE_DROP, 2, PRIORITY_DROP, 30, PROFILE_ALL,
};
static const cue_def_t DEF_LINK = {
    CUE_LINK, 3, PRIORITY_LINK, 30, PROFILE_ALL,
};
static const cue_def_t DEF_LANTERN = {
    CUE_LANTERN, 2, PRIORITY_LANTERN, 120, PROFILE_ALL,
};
static const cue_def_t DEF_FOCUS = {
    CUE_FOCUS, 2, PRIORITY_FOCUS, 12, PROFILE_BIT(SOUND_PROFILE_FIELD),
};
static const cue_def_t DEF_SWITCH = {
    CUE_SWITCH, 2, PRIORITY_SWITCH, 20,
    PROFILE_BIT(SOUND_PROFILE_COMPANION) | PROFILE_BIT(SOUND_PROFILE_FIELD),
};
static const cue_def_t DEF_PREVIEW = {
    CUE_PREVIEW, 3, PRIORITY_DONE, 20, PROFILE_ALL,
};

// The keypress def carries no notes of its own: sound_key_typed() selects the
// character or backspace variant and reuses this def for priority, cooldown,
// and profile gating.  The cooldown slot is shared between the variants, so
// alternating char/delete cannot double the rate.
static const cue_def_t DEF_KEY = {
    NULL, 1, PRIORITY_KEY, 2, PROFILE_ALL,
};
// Every profile: like the key tick, it answers the user's own press.  The
// cooldown is long because a refusal repeated is a nag, not information.
static const cue_def_t DEF_DENY = {
    CUE_DENY, 2, PRIORITY_DENY, 30, PROFILE_ALL,
};
// Once, at power-on; it is the link's priority so nothing else at boot can
// cut it off, and LINK itself follows it a few seconds later.
static const cue_def_t DEF_BOOT = {
    CUE_BOOT, 4, PRIORITY_LINK, 0, PROFILE_ALL,
};
// Blips carry no table: sound_blip() writes the one note.  Companion and
// Field only -- Studio is the profile that promises to say nothing but the
// consequential -- and only when the BLIPS setting is on.
static const cue_def_t DEF_BLIP = {
    blip_note, 1, PRIORITY_BLIP, 6,
    PROFILE_BIT(SOUND_PROFILE_COMPANION) | PROFILE_BIT(SOUND_PROFILE_FIELD),
};

// Every profile, Studio included: being asked for something is exactly the
// consequential kind of event Studio exists to keep.  Above DONE, so a turn
// that ends on a question is heard as a question.
static const cue_def_t DEF_APPROVE = {
    CUE_APPROVE, 4, PRIORITY_APPROVE, 60, PROFILE_ALL,
};
static const cue_def_t DEF_QUESTION = {
    CUE_QUESTION, 2, PRIORITY_QUESTION, 60, PROFILE_ALL,
};

static const cue_def_t *cue_def(sound_cue_t cue)
{
    switch (cue) {
    case SOUND_APPROVE:  return &DEF_APPROVE;
    case SOUND_QUESTION: return &DEF_QUESTION;
    case SOUND_DENY:    return &DEF_DENY;
    case SOUND_BOOT:    return &DEF_BOOT;
    case SOUND_SENT:    return &DEF_SENT;
    case SOUND_THINK:   return &DEF_THINK;
    case SOUND_OUTPUT:  return &DEF_OUTPUT;
    case SOUND_DONE:    return &DEF_DONE;
    case SOUND_DROP:    return &DEF_DROP;
    case SOUND_LINK:    return &DEF_LINK;
    case SOUND_LANTERN: return &DEF_LANTERN;
    case SOUND_FOCUS:   return &DEF_FOCUS;
    case SOUND_SWITCH:  return &DEF_SWITCH;
    case SOUND_PREVIEW: return &DEF_PREVIEW;
    default:            return NULL;
    }
}

static const char *cue_name(sound_cue_t cue)
{
    switch (cue) {
    case SOUND_SENT:    return "SENT";
    case SOUND_THINK:   return "THINK";
    case SOUND_OUTPUT:  return "OUTPUT";
    case SOUND_DONE:    return "DONE";
    case SOUND_DROP:    return "DROP";
    case SOUND_LINK:    return "LINK";
    case SOUND_LANTERN: return "LANTERN";
    case SOUND_FOCUS:   return "FOCUS";
    case SOUND_SWITCH:  return "SWITCH";
    case SOUND_PREVIEW: return "PREVIEW";
    case SOUND_KEY:     return "KEY";
    case SOUND_DENY:    return "DENY";
    case SOUND_BOOT:    return "BOOT";
    case SOUND_BLIP:    return "BLIP";
    case SOUND_APPROVE: return "APPROVE";
    case SOUND_QUESTION: return "QUESTION";
    default:            return "?";
    }
}

// These are real PSG volume values, not percentages.  Per-note percentages
// above are multiplied by one of these values before a voice reaches main.c.
static const uint8_t VOLUME_LEVEL[SOUND_VOLUME_COUNT] = {
    24, 48, 72,
};

static const char *const PROFILE_NAMES[SOUND_PROFILE_COUNT] = {
    "STUDIO", "COMPANION", "FIELD",
};
static const char *const VOLUME_NAMES[SOUND_VOLUME_COUNT] = {
    "LOW", "NEAR", "ROOM",
};

// The PCM channels' equivalents.  Samples are mastered to a common peak by
// tools/sfx_build.py, so these are the whole loudness story apart from each
// sample's own `gain` -- which is how a key tick stays a tick beside a chime.
static const uint8_t PCM_LEVEL[SOUND_VOLUME_COUNT] = {
    40, 76, 116,
};

static const char *const STYLE_NAMES[SOUND_STYLE_COUNT] = {
    "SOFT", "CHIP",
};

static sound_profile_t profile = SOUND_PROFILE_COMPANION;
static sound_volume_t volume = SOUND_VOLUME_NEAR;
static sound_style_t style = SOUND_STYLE_CHIP;
static int muted;
static int blips;

// Field has a little more tactile edge without changing pitch or loudness.
// Studio stays square and clean.  This is only a fallback for notes that ask
// for the usual 50% pulse; distinctive note duties remain distinctive.
static uint8_t profile_duty(uint8_t duty)
{
    if (profile == SOUND_PROFILE_FIELD && duty == SOUND_DUTY_50) {
        return SOUND_DUTY_37;
    }
    return duty;
}

static const sound_note_t *cur;
static unsigned cur_count;
static unsigned idx;
static unsigned left;
static unsigned cur_priority;
static sound_cue_t active_cue;
static unsigned frame_no;
static unsigned gap_left;
static sound_voice_t sounding;

// New extension values fit within this small sparse index range.  Keeping it
// indexed by enum gives every cue its own cooldown without dynamic allocation.
#define CUE_SLOT_COUNT 26
static unsigned last_started[CUE_SLOT_COUNT];
static uint8_t started[CUE_SLOT_COUNT];

// The two sample lanes.  `left` counts the frames the hardware is still
// playing; `pending` is what main.c has not collected yet.
typedef struct {
    unsigned left;
    unsigned priority;
    sound_cue_t cue;
    int pending;
    sound_pcm_t want;
} lane_t;
static lane_t lanes[SOUND_LANES];
static uint32_t pcm_serial;

// The last variant each bank played, so the same take never plays twice in a
// row when there is another -- the cheapest cure for a sound that nags.
static uint8_t last_variant[SFX_BANK_COUNT];
// ...and the last of the three pitch variations (sound.c, pick_pitch()).
static uint8_t last_pitch[SFX_BANK_COUNT];
static uint32_t rng_state = 0x2545F491u;

static uint32_t rng_next(void)
{
    // xorshift32: deterministic for the host tests, varied enough by ear.
    uint32_t x = rng_state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    rng_state = x ? x : 0x2545F491u;
    return rng_state;
}

#define SOUND_GAP_FRAMES 1
#define MINOR_PRIORITY_MAX PRIORITY_LANTERN

static int valid_profile(sound_profile_t p)
{
    return p >= SOUND_PROFILE_STUDIO && p < SOUND_PROFILE_COUNT;
}

static int valid_volume(sound_volume_t v)
{
    return v >= SOUND_VOLUME_LOW && v < SOUND_VOLUME_COUNT;
}

static int cue_allowed(const cue_def_t *def)
{
    return def && (def->profile_mask & PROFILE_BIT(profile)) != 0;
}

static void stop_playback(int gap)
{
    cur = NULL;
    cur_count = 0;
    idx = 0;
    left = 0;
    cur_priority = 0;
    active_cue = SOUND_NONE;
    gap_left = gap ? SOUND_GAP_FRAMES : 0;
}

static void skip_empty_notes(void)
{
    while (cur && idx < cur_count && cur[idx].frames == 0) idx++;
    if (!cur || idx >= cur_count) {
        stop_playback(1);
        return;
    }
    left = cur[idx].frames;
}

static void start_cue(sound_cue_t cue, const cue_def_t *def)
{
    cur = def->notes;
    cur_count = def->count;
    idx = 0;
    left = 0;
    cur_priority = def->priority;
    active_cue = cue;
    gap_left = 0;
    skip_empty_notes();
    log_printf("sound: %s", cue_name(cue));

    if ((unsigned)cue < CUE_SLOT_COUNT) {
        last_started[cue] = frame_no;
        started[cue] = 1;
    }
}

static int voice_same(const sound_voice_t *a, const sound_voice_t *b)
{
    return a->hz == b->hz && a->duty == b->duty && a->volume == b->volume;
}

static sound_voice_t current_voice(void)
{
    sound_voice_t voice = {0, 0, 0};
    if (!cur || muted || idx >= cur_count) return voice;

    const sound_note_t *note = &cur[idx];
    unsigned scaled = (unsigned)VOLUME_LEVEL[volume] * (unsigned)note->volume;
    scaled = (scaled + 50u) / 100u;
    if (scaled == 0) scaled = 1;
    if (scaled > SOUND_PSG_VOLUME_MAX) scaled = SOUND_PSG_VOLUME_MAX;

    voice.hz = note->hz;
    voice.duty = profile_duty(note->duty);
    voice.volume = (uint8_t)scaled;
    return voice;
}

static void lane_stop(unsigned lane)
{
    if (lane >= SOUND_LANES) return;
    lane_t *l = &lanes[lane];
    const int was = l->left > 0 || (l->pending && l->want.sample);
    l->left = 0;
    l->priority = 0;
    l->cue = SOUND_NONE;
    if (was) {
        l->pending = 1;
        l->want.sample = NULL;
        l->want.hz = 0;
        l->want.volume = 0;
    }
}

static void lanes_stop(void)
{
    for (unsigned i = 0; i < SOUND_LANES; i++) lane_stop(i);
}

void sound_reset(void)
{
    stop_playback(0);
    frame_no = 0;
    memset(last_started, 0, sizeof(last_started));
    memset(started, 0, sizeof(started));
    sounding.hz = 0;
    sounding.duty = 0;
    sounding.volume = 0;
    memset(lanes, 0, sizeof(lanes));
    memset(last_variant, 0xFF, sizeof(last_variant));
    memset(last_pitch, 0, sizeof(last_pitch));
    rng_state = 0x2545F491u;
}

int sound_set_style(sound_style_t s)
{
    if (s < SOUND_STYLE_SNES || s >= SOUND_STYLE_COUNT) return 0;
    if (style != s) {
        style = s;
        stop_playback(0);
        lanes_stop();
    }
    return 1;
}

sound_style_t sound_style(void)
{
    return style;
}

const char *sound_style_name(sound_style_t s)
{
    return (s >= SOUND_STYLE_SNES && s < SOUND_STYLE_COUNT) ? STYLE_NAMES[s]
                                                            : "UNKNOWN";
}

// Settings' WRITING: 0 off, 1 low, 2 mid, 3 high (chime.c spaces them).
void sound_set_blips(int level)
{
    blips = level < 0 ? 0 : level > 3 ? 3 : level;
}

// Settings' TYPING: the clicks of your own keys and of the cursor.  Every
// sound that reports on Claude or the link is untouched by it.
static int typing = 1;

void sound_set_typing(int on)
{
    typing = on ? 1 : 0;
}

int sound_typing(void)
{
    return typing;
}

int sound_blips(void)
{
    return blips;
}

// Which bank voices a cue in the SNES style, or SFX_BANK_COUNT for none --
// in which case the cue falls back to the chip voice rather than going
// silent.  `backspace` picks the delete take for a key.
static sfx_bank_t cue_bank(sound_cue_t cue, int backspace)
{
    switch (cue) {
    case SOUND_SENT:    return SFX_SENT;
    case SOUND_THINK:   return SFX_THINK;
    case SOUND_OUTPUT:  return SFX_OUTPUT;
    case SOUND_DONE:    return SFX_DONE;
    case SOUND_PREVIEW: return SFX_DONE;
    case SOUND_DROP:    return SFX_DROP;
    case SOUND_LINK:    return SFX_LINK;
    case SOUND_LANTERN: return SFX_LANTERN;
    case SOUND_FOCUS:   return SFX_CURSOR;
    case SOUND_SWITCH:  return SFX_SWITCH;
    case SOUND_KEY:     return backspace ? SFX_DELETE : SFX_KEY;
    case SOUND_DENY:    return SFX_DENY;
    case SOUND_BOOT:    return SFX_BOOT;
    case SOUND_BLIP:    return SFX_BLIP;
    case SOUND_APPROVE: return SFX_APPROVE;
    case SOUND_QUESTION: return SFX_QUESTION;
    default:            return SFX_BANK_COUNT;
    }
}

// The small sounds of your own hands go on the tick lane; everything that
// reports on the session goes on the cue lane.
static unsigned cue_lane(sound_cue_t cue)
{
    return (cue == SOUND_KEY || cue == SOUND_FOCUS || cue == SOUND_BLIP)
               ? SOUND_LANE_TICK : SOUND_LANE_CUE;
}

// The card's own takes, where it has them (sound_set_custom).
static const sfx_sample_t *custom_takes[SFX_BANK_COUNT];
static uint8_t custom_count[SFX_BANK_COUNT];

int sound_set_custom(unsigned bank, const sfx_sample_t *takes, unsigned count)
{
    if (bank >= SFX_BANK_COUNT || count > SOUND_CUSTOM_TAKES ||
        (count && !takes)) {
        return 0;
    }
    custom_takes[bank] = count ? takes : NULL;
    custom_count[bank] = (uint8_t)count;
    last_variant[bank] = 0xFF;
    return 1;
}

unsigned sound_custom_banks(void)
{
    unsigned n = 0;
    for (unsigned b = 0; b < SFX_BANK_COUNT; b++) n += custom_count[b] != 0;
    return n;
}

uint8_t sound_bank_gain(unsigned bank)
{
    if (bank >= SFX_BANK_COUNT || !SFX_BANKS[bank].count) return 70;
    return SFX_SAMPLES[SFX_BANKS[bank].first].gain;
}

static unsigned bank_takes(sfx_bank_t bank)
{
    if (bank >= SFX_BANK_COUNT) return 0;
    return custom_count[bank] ? custom_count[bank] : SFX_BANKS[bank].count;
}

// Pick a take: never the one just played when there is another.
static const sfx_sample_t *pick_variant(sfx_bank_t bank, unsigned salt)
{
    const unsigned count = bank_takes(bank);
    if (!count) return NULL;
    unsigned v = (rng_next() + salt) % count;
    if (count > 1 && v == last_variant[bank]) v = (v + 1) % count;
    last_variant[bank] = (uint8_t)v;
    return custom_count[bank] ? &custom_takes[bank][v]
                              : &SFX_SAMPLES[SFX_BANKS[bank].first + v];
}

// Every cue comes in three variations -- as recorded, a whole tone higher, a
// whole tone lower -- and never plays the same one twice running.  The whole
// phrase moves together, so a rising cue still rises and DONE still
// resolves: it is the same word said three ways, which is what keeps the
// fortieth DONE of a day from sounding like the first thirty-nine.  The
// ratios are 2^(+-2/12) in thousandths.
static const uint16_t VARIATION_PERMILLE[3] = { 1000, 1122, 891 };

// Repeated small sounds get a small, random change of pitch -- a few percent,
// below the point where a tick sounds like a different tick, above the point
// where forty of them sound like a machine.  Cues keep their pitch: their
// melody is what they mean.
static uint16_t jitter_hz(const sfx_sample_t *s, sound_cue_t cue, unsigned salt,
                          unsigned permille)
{
    unsigned spread = 0;          // in tenths of a percent, either way
    if (cue == SOUND_KEY || cue == SOUND_FOCUS) spread = 40;
    else if (cue == SOUND_BLIP) spread = 90;
    int step = 0;
    if (spread) {
        step = (int)((rng_next() + salt * 7u) % (2u * spread + 1u)) -
               (int)spread;
    }
    const long hz = (long)s->hz * (long)permille / 1000L * (1000L + step) /
                    1000L;
    return (uint16_t)(hz < 1000 ? 1000 : hz > 48000 ? 48000 : hz);
}

// Which of the three variations plays next for `bank`: any but the last.
static unsigned pick_pitch(sfx_bank_t bank)
{
    unsigned v = rng_next() % 3u;
    if (v == last_pitch[bank]) v = (v + 1u + rng_next() % 2u) % 3u;
    last_pitch[bank] = (uint8_t)v;
    return VARIATION_PERMILLE[v];
}

// Start a sample for `cue` on its lane, under the PSG voice's rules.  Returns
// 1 if it is playing, 0 if it was refused, -1 if there is no sample for it
// (and the chip voice should try instead).
static int start_sample(sound_cue_t cue, const cue_def_t *def, int backspace,
                        unsigned salt)
{
    const sfx_bank_t bank = cue_bank(cue, backspace);
    if (bank >= SFX_BANK_COUNT || !bank_takes(bank)) return -1;
    const unsigned lane = cue_lane(cue);
    lane_t *l = &lanes[lane];

    if (lane == SOUND_LANE_CUE && l->left) {
        // The PSG rule exactly: equal or lower never talks over what plays.
        if (def->priority <= l->priority) return 0;
    }
    // On the tick lane the newest press always wins: it is the user's own
    // hand, and a tick is shorter than the gap between two of them anyway.

    const sfx_sample_t *s = pick_variant(bank, salt);
    if (!s) return -1;
    unsigned scaled = (unsigned)PCM_LEVEL[volume] * (unsigned)s->gain;
    scaled = (scaled + 50u) / 100u;
    if (scaled == 0) scaled = 1;
    if (scaled > 127u) scaled = 127u;

    // A lower variation plays slower and so longer: the lane's clock scales
    // with it, or a low DONE would be cut short by the next cue.
    const unsigned permille = pick_pitch(bank);
    const uint16_t hz = jitter_hz(s, cue, salt, permille);
    // The clock from the rate that will actually play -- after jitter and
    // the 48 kHz clamp, which a high-rate custom sound can hit -- not from
    // the variation asked for, or the lane frees early and the next cue
    // cuts the tail off.
    const unsigned frames =
        (unsigned)(((unsigned long)(s->frames ? s->frames : 1u) * s->hz +
                    hz - 1u) / hz) + 1u;
    l->left = frames;
    l->priority = def->priority;
    l->cue = cue;
    l->pending = 1;
    l->want.sample = s;
    l->want.hz = hz;
    l->want.volume = (uint8_t)scaled;
    l->want.serial = ++pcm_serial;
    if (cue != SOUND_KEY && cue != SOUND_BLIP && cue != SOUND_FOCUS) {
        log_printf("sound: %s", cue_name(cue));
    }
    if ((unsigned)cue < CUE_SLOT_COUNT) {
        last_started[cue] = frame_no;
        started[cue] = 1;
    }
    return 1;
}

int sound_take_pcm(unsigned lane, sound_pcm_t *out)
{
    if (lane >= SOUND_LANES || !lanes[lane].pending) return 0;
    lanes[lane].pending = 0;
    if (out) *out = lanes[lane].want;
    return 1;
}

int sound_lane_busy(unsigned lane)
{
    return lane < SOUND_LANES && lanes[lane].left > 0 && !muted;
}

int sound_set_profile(sound_profile_t p)
{
    if (!valid_profile(p)) return 0;
    if (profile != p) {
        profile = p;
        // A phrase voiced for the old room must not keep playing under the
        // new one.  The UI deliberately asks for an explicit preview next.
        stop_playback(0);
    }
    return 1;
}

sound_profile_t sound_profile(void)
{
    return profile;
}

const char *sound_profile_name(sound_profile_t p)
{
    return valid_profile(p) ? PROFILE_NAMES[p] : "UNKNOWN";
}

int sound_set_volume(sound_volume_t v)
{
    if (!valid_volume(v)) return 0;
    volume = v;
    return 1;
}

sound_volume_t sound_volume(void)
{
    return volume;
}

const char *sound_volume_name(sound_volume_t v)
{
    return valid_volume(v) ? VOLUME_NAMES[v] : "UNKNOWN";
}

// Shared start path for sound_cue() and sound_key_typed().  `notes` is the
// cue's own table except for SOUND_KEY, where the caller selects the
// character/backspace variant and everything else (priority, cooldown,
// profile mask) comes from DEF_KEY.
static int sound_start_cue(sound_cue_t cue, const cue_def_t *def,
                           const sound_note_t *notes, int backspace,
                           unsigned salt)
{
    if (muted || !cue_allowed(def)) return 0;
    if ((unsigned)cue >= CUE_SLOT_COUNT) return 0;
    if (!typing && (cue == SOUND_KEY || cue == SOUND_FOCUS)) return 0;

    if (started[cue] && frame_no - last_started[cue] < def->cooldown) return 0;

    // The sampled instrument first.  A cue with no sample in the bank falls
    // through to the chip voice below, so a partial bank is never a mute one.
    if (style == SOUND_STYLE_SNES) {
        // The sampled set stays quiet when an answer starts to arrive: it
        // was the fourth sound of every turn, between THINK and DONE, and
        // said nothing they do not.  A custom output sound still plays (the
        // card's own choice); the chip voice keeps its full arc.
        if (cue == SOUND_OUTPUT && !custom_count[SFX_OUTPUT]) return 0;
        const int r = start_sample(cue, def, backspace, salt);
        if (r >= 0) return r;
    }

    // A cue with a lower or equal priority never talks over the current one.
    // The exception is implicitly SOUND_DROP: it has the highest priority and
    // therefore may interrupt whatever the user was hearing when the link dies.
    // The one deliberate reversal: a keypress may replace the murmur texture
    // (the model's voice), because the user's own fingers should always
    // answer -- but it can never interrupt a named cue.
    if (cur) {
        int blocked = def->priority <= cur_priority;
        if (cur_priority == PRIORITY_MURMUR && def->priority > PRIORITY_MURMUR) {
            blocked = 0;
        }
        if (blocked) return 0;
    }

    // A one-frame release gap is for small, decorative events only.  Important
    // traffic transitions are never delayed or queued behind an old note.
    if (!cur && gap_left && def->priority <= MINOR_PRIORITY_MAX) return 0;

    cue_def_t chosen = *def;
    chosen.notes = notes;
    start_cue(cue, &chosen);
    return 1;
}

void sound_cue(sound_cue_t cue)
{
    const cue_def_t *def = cue_def(cue);
    (void)sound_start_cue(cue, def, def ? def->notes : NULL, 0, 0);
}

void sound_key_typed(int backspace)
{
    // The user's own fingers answer over any texture that was playing.
    sound_phrase_stop();
    (void)sound_start_cue(SOUND_KEY, &DEF_KEY,
                          backspace ? CUE_KEY_BS : CUE_KEY_CHAR, backspace, 0);
}

int sound_blip(unsigned char glyph)
{
    if (!blips) return 0;
    // Texture never talks over anything, in either instrument: a cue, a
    // keypress or another blip still sounding all refuse it.
    if (cur || sound_lane_busy(SOUND_LANE_CUE) ||
        sound_lane_busy(SOUND_LANE_TICK)) {
        return 0;
    }
    blip_note[0].hz = BLIP_PENTATONIC[glyph % 5u];
    blip_note[0].frames = 2;
    blip_note[0].duty = SOUND_DUTY_25;
    blip_note[0].volume = 26;
    return sound_start_cue(SOUND_BLIP, &DEF_BLIP, blip_note, 0, glyph);
}

int sound_idle(void)
{
    return !cur && !gap_left && !sound_lane_busy(SOUND_LANE_CUE) &&
           !sound_lane_busy(SOUND_LANE_TICK);
}

unsigned sound_frame(void)
{
    return frame_no;
}

int sound_phrase_start(const sound_note_t *notes, unsigned count)
{
    // Texture, never competition: refused while anything at all is sounding
    // or still in its release gap, and started with no cooldown slot of its
    // own -- whoever builds phrases owns their spacing.
    if (muted || cur || gap_left || !notes || !count) return 0;
    if (sound_lane_busy(SOUND_LANE_CUE)) return 0;
    cur = notes;
    cur_count = count;
    idx = 0;
    left = cur->frames;
    cur_priority = PRIORITY_MURMUR;
    active_cue = SOUND_NONE;
    return 1;
}

void sound_phrase_stop(void)
{
    if (cur && cur_priority == PRIORITY_MURMUR) stop_playback(0);
}

void sound_preview(void)
{
    sound_cue(SOUND_PREVIEW);
}

void sound_set_muted(int m)
{
    muted = m ? 1 : 0;
    if (muted) {
        stop_playback(0);
        lanes_stop();
    }
}

int sound_muted(void)
{
    return muted;
}

int sound_tick_voice(sound_voice_t *voice)
{
    const sound_voice_t want = current_voice();
    const int changed = !voice_same(&want, &sounding);
    if (voice) *voice = want;
    sounding = want;

    // The caller invokes this exactly once per VBlank.  Advance after exposing
    // the current note so `frames` means the number of frames actually heard.
    frame_no++;
    if (cur) {
        if (left > 0) left--;
        while (cur && left == 0) {
            idx++;
            if (idx >= cur_count) {
                stop_playback(1);
                break;
            }
            skip_empty_notes();
        }
    } else if (gap_left) {
        gap_left--;
    }
    // The lanes keep time on the same clock.  Nothing needs telling when a
    // sample runs out: the hardware stops a one-shot channel on its own, and
    // the lane only has to stop answering "busy".
    for (unsigned i = 0; i < SOUND_LANES; i++) {
        if (lanes[i].left) {
            if (--lanes[i].left == 0) {
                lanes[i].priority = 0;
                lanes[i].cue = SOUND_NONE;
            }
        }
    }

    return changed;
}

int sound_tick(unsigned *hz)
{
    sound_voice_t voice;
    const int changed = sound_tick_voice(&voice);
    if (hz) *hz = voice.hz;
    return changed;
}

sound_cue_t sound_active_cue(void)
{
    if (active_cue != SOUND_NONE) return active_cue;
    return lanes[SOUND_LANE_CUE].left ? lanes[SOUND_LANE_CUE].cue : SOUND_NONE;
}

int sound_playing(void)
{
    return (cur || lanes[SOUND_LANE_CUE].left || lanes[SOUND_LANE_TICK].left) &&
           !muted;
}
