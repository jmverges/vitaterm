// Reading a WAV file into something the DS sound hardware can play.
//
// This is what makes custom sounds plug-and-play: copy `done.wav` into
// /dsterm/sounds/ on the SD card and the card plays it in place of its own
// DONE -- no toolchain, no rebuild (docs/CUSTOMIZING.md).  So the reader is
// forgiving about what it accepts and strict about what it trusts:
//
//   accepted   PCM (format 1), 8- or 16-bit, mono or stereo (mixed down),
//              any rate from 4 to 48 kHz; extra chunks (LIST, cue, ...) are
//              skipped
//   refused    compressed formats, 24/32-bit and float, a header that lies
//              about its sizes, and anything longer than WAV_MAX_SECONDS
//
// The result is always 16-bit signed mono, word-aligned and word-padded, as
// the hardware wants it.  Pure C over a byte buffer, so every case is tested
// on the host; the card hands it the file's bytes.

#ifndef DSTERM_WAV_H
#define DSTERM_WAV_H

#include <stddef.h>
#include <stdint.h>

#define WAV_MAX_SECONDS 3
#define WAV_MIN_HZ 4000
#define WAV_MAX_HZ 48000

typedef struct {
    int16_t *pcm;          // caller's buffer; filled with mono samples
    size_t samples;        // how many were written
    uint32_t hz;
} wav_t;

// How many 16-bit samples `data` will decode to, or 0 with `why` set if it
// is not a WAV this card will play.  Use it to size the buffer for
// wav_decode().
size_t wav_measure(const uint8_t *data, size_t len, uint32_t *hz,
                   char *why, size_t why_cap);

// Decode into `out->pcm`, which holds `cap` samples.  Returns 1, or 0 with
// `why` set.  `out->samples` is then rounded up to an even count (a whole
// number of hardware words) with silence.
int wav_decode(const uint8_t *data, size_t len, wav_t *out, size_t cap,
               char *why, size_t why_cap);

#endif  // DSTERM_WAV_H
