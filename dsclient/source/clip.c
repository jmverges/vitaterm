#include "clip.h"

#include <stdio.h>
#include <string.h>

void clip_init(clip_t *clip)
{
    if (!clip) return;
    memset(clip, 0, sizeof(*clip));
}

void clip_wire_reset(clip_t *clip)
{
    if (!clip) return;
    memset(clip, 0, sizeof(*clip));
}

// Abandon whatever is being assembled, without touching what is committed. A
// bad frame must not take away the clipboard you already had.
static void discard(clip_t *clip)
{
    clip->staging = 0;
    clip->stage_len = 0;
    clip->stage_seen = 0;
    clip->stage_count = 0;
    clip->stage_total = 0;
    clip->stage_flags = 0;
}

static void commit(clip_t *clip, uint16_t len, uint32_t total, uint8_t flags)
{
    memcpy(clip->text, clip->stage, len);
    clip->text[len] = '\0';
    clip->len = len;
    clip->total = total;
    clip->flags = flags;
    clip->have = 1;
    discard(clip);
}

int clip_wire_chunk(clip_t *clip, unsigned count, unsigned index,
                    unsigned seq, unsigned flags, uint32_t total,
                    const char *bytes, unsigned len)
{
    if (!clip) return 0;

    // An empty frame is a complete answer on its own: the clipboard is empty,
    // or the flags say it was withheld. It replaces whatever was there, and it
    // cancels anything in flight -- an empty clipboard arriving mid-snapshot
    // means the old snapshot is no longer what is on the desktop.
    if (count == 0) {
        discard(clip);
        clip->text[0] = '\0';
        clip->len = 0;
        clip->total = 0;
        clip->flags = (uint8_t)flags;
        clip->have = 1;
        return 1;
    }

    if (count > CLIP_CHUNKS_MAX) return 0;
    if (index >= count) return 0;
    if (len == 0 || len > CLIP_CHUNK_MAX) return 0;
    if (!bytes) return 0;

    // Printable ASCII or newline, re-checked here rather than trusted. The
    // server refuses to encode anything else, which is exactly why this has to
    // refuse to decode it -- the two halves have to reject the same frames,
    // not merely accept the same ones, or the card is relying on the server
    // being the version it thinks it is.
    for (unsigned i = 0; i < len; i++) {
        const unsigned char byte = (unsigned char)bytes[i];
        if (byte != CLIP_LF && (byte < 0x20 || byte > 0x7E)) return 0;
    }

    // Index 0 starts a snapshot, and so does a frame from a newer seq. Both,
    // deliberately: a copy made while the previous one was still arriving must
    // not be able to splice two clipboards into one paste, and leaving that to
    // a single rule means the other one is an assumption nobody checks.
    if (index == 0 || !clip->staging || (unsigned)clip->stage_seq != seq ||
        (unsigned)clip->stage_count != count) {
        if (index != 0) {
            // A later chunk with nothing to attach to. Not an error worth
            // shouting about -- it is what a card that connected mid-snapshot
            // sees -- but it cannot be assembled either.
            discard(clip);
            return 0;
        }
        discard(clip);
        clip->staging = 1;
        clip->stage_seq = (uint8_t)seq;
        clip->stage_count = (uint8_t)count;
        clip->stage_total = total;
        clip->stage_flags = (uint8_t)flags;
    }

    // Chunks arrive in order. Out of order means a frame was lost, and a
    // clipboard reassembled around a hole is worse than no clipboard.
    if (index != clip->stage_seen) {
        discard(clip);
        return 0;
    }
    // Every frame of one snapshot has to agree about it. A disagreement is two
    // snapshots being confused for one.
    if (clip->stage_total != total || clip->stage_flags != (uint8_t)flags) {
        discard(clip);
        return 0;
    }
    if ((unsigned)clip->stage_len + len > CLIP_TEXT_MAX) {
        discard(clip);
        return 0;
    }

    memcpy(clip->stage + clip->stage_len, bytes, len);
    clip->stage_len = (uint16_t)(clip->stage_len + len);
    clip->stage_seen++;

    if (clip->stage_seen < clip->stage_count) return 0;

    // The invariant the wire is built to carry, checked on the frame that
    // completes the set: the bytes received must be exactly the converted
    // length, or that length capped. This is the single assertion that would
    // catch the two encoders drifting apart -- the failure this protocol's
    // history says to expect -- so a snapshot that fails it is thrown away
    // whole rather than shown with a caveat.
    const uint32_t expect = clip->stage_total > CLIP_TEXT_MAX
                                ? (uint32_t)CLIP_TEXT_MAX
                                : clip->stage_total;
    if ((uint32_t)clip->stage_len != expect) {
        discard(clip);
        return 0;
    }
    // And the flag has to match the arithmetic. A snapshot that says it was
    // shortened but fits, or fits but does not say so, is one of the two ends
    // being wrong about which.
    const int truncated = (clip->stage_flags & CLIP_FLAG_TRUNCATED) != 0;
    if (truncated != (clip->stage_total > CLIP_TEXT_MAX)) {
        discard(clip);
        return 0;
    }

    commit(clip, clip->stage_len, clip->stage_total, clip->stage_flags);
    return 1;
}

int clip_paste_is_inert(const clip_t *clip)
{
    if (!clip || !clip->have || clip->len == 0) return 0;
    for (uint16_t i = 0; i < clip->len; i++) {
        if (clip->text[i] == CLIP_LF) return 0;
    }
    return 1;
}

// A byte count as something a person reads at a glance. Kept local because the
// only caller is the summary line, and a general-purpose formatter would grow
// options nobody here needs.
static void human(uint32_t bytes, char *out, size_t out_len)
{
    if (bytes < 1024u) {
        (void)snprintf(out, out_len, "%u bytes", (unsigned)bytes);
        return;
    }
    // One decimal place, computed in integers: there is no FPU on this device
    // and pulling in soft-float for a status line is a poor trade.
    const uint32_t whole = bytes / 1024u;
    const uint32_t tenth = ((bytes % 1024u) * 10u) / 1024u;
    (void)snprintf(out, out_len, "%u.%uK", (unsigned)whole, (unsigned)tenth);
}

void clip_summary(const clip_t *clip, char *out, size_t out_len)
{
    if (!out || out_len == 0) return;
    out[0] = '\0';
    if (!clip) return;

    if (!clip->have) {
        (void)snprintf(out, out_len, "nothing received yet");
        return;
    }
    if (clip->flags & CLIP_FLAG_WITHHELD) {
        // Named, not blanked. A pane that looks empty after copying a password
        // is a pane that invites copying the password again.
        (void)snprintf(out, out_len, "withheld: a concealed copy");
        return;
    }
    if (clip->len == 0) {
        (void)snprintf(out, out_len, "the clipboard is empty");
        return;
    }

    char have[16], whole[16];
    human(clip->len, have, sizeof(have));
    if (clip->flags & CLIP_FLAG_TRUNCATED) {
        human(clip->total, whole, sizeof(whole));
        (void)snprintf(out, out_len, "%s of %s%s", have, whole,
                       (clip->flags & CLIP_FLAG_TRANSLITERATED)
                           ? ", changed" : "");
        return;
    }
    (void)snprintf(out, out_len, "%s%s", have,
                   (clip->flags & CLIP_FLAG_TRANSLITERATED)
                       ? ", changed to fit ASCII" : "");
}

unsigned clip_wrap(const clip_t *clip, char lines[][CLIP_LINE_COLS + 1],
                   unsigned max)
{
    if (!clip || !lines || max == 0) return 0;

    unsigned written = 0;
    unsigned col = 0;
    lines[0][0] = '\0';

    for (uint16_t i = 0; i < clip->len && written < max; i++) {
        const char byte = clip->text[i];
        if (byte == CLIP_LF) {
            lines[written][col] = '\0';
            written++;
            col = 0;
            if (written < max) lines[written][0] = '\0';
            continue;
        }
        if (col == CLIP_LINE_COLS) {
            lines[written][col] = '\0';
            written++;
            col = 0;
            if (written >= max) break;
            lines[written][0] = '\0';
        }
        lines[written][col++] = byte;
    }

    // The line being built is a line: a clipboard with no trailing newline
    // still ends in text somebody needs to see.
    if (written < max && col > 0) {
        lines[written][col] = '\0';
        written++;
    }
    return written;
}
