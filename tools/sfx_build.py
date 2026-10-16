#!/usr/bin/env python3
"""Compile the terminal's sounds into the ROM.

    python3 tools/sfx_build.py            # writes dsclient/source/sfx_assets.[ch]
    python3 tools/sfx_build.py --check    # exits 1 if they are out of date

Reads dsclient/sounds/sounds.txt -- one line per take, `bank file gain` -- and
the mastered WAVs beside it (mono, 16-bit), and writes them as the DS's own
4-bit IMA-ADPCM: a quarter of the size of 16-bit PCM, decoded by the sound
hardware for free, and noticeably cleaner than 8-bit PCM on a sound's quiet
tail.  It is the same trade the Super Nintendo made with its 4-bit BRR
samples.

Takes shorter than a quarter of a second -- the clicks of keys, the cursor
tick, blips -- are stored as plain 16-bit PCM instead: ADPCM's step size lags
a sharp attack, and on a click that lag is most of the sound (16 dB against
33 dB on a longer sound, measured), while the bytes saved on a 50 ms tick are not
worth having.

A bank is one cue; several takes of a bank are variations the conductor
rotates through (sound.c never plays the same take twice in a row).  `gain`
is the take's loudness as a percentage of the room volume: a key tick at 30
and a knock at 84 are both mastered to the same peak and differ only here.

The C files are generated and committed, so building the ROM needs neither
Python nor the WAVs.  Python 3.9, standard library only.
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOUNDS = REPO / "dsclient" / "sounds"
MANIFEST = SOUNDS / "sounds.txt"
OUT_C = REPO / "dsclient" / "source" / "sfx_assets.c"
OUT_H = REPO / "dsclient" / "source" / "sfx_assets.h"

# In the order of sfx_bank_t.  Adding one here and to sound.c's cue_bank() is
# all a new cue needs; a bank with no takes falls back to the chip voice.
BANKS = [
    "sent", "think", "output", "done", "link", "drop", "lantern", "cursor",
    "switch", "key", "delete", "deny", "blip", "boot", "approve", "question",
]

# The DS plays a sample at any rate; this one keeps everything its speaker can
# reproduce and costs 8 KB a second as ADPCM.
TARGET_HZ = 16384
FRAME_HZ = 59.8261   # the DS's VBlank rate
PCM16_BELOW = TARGET_HZ // 4      # samples: shorter than this stays PCM16

FORMAT_ADPCM = 0
FORMAT_PCM16 = 1

_STEP = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41,
    45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209,
    230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876,
    963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749,
    3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630,
    9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623,
    27086, 29794, 32767,
]
_INDEX = [-1, -1, -1, -1, 2, 4, 6, 8]


def _decode_nibble(nibble: int, pred: int, index: int) -> tuple[int, int]:
    """One nibble, exactly as the DS sound hardware decodes it.

    The difference from textbook IMA is the clamp: the DS saturates at
    -0x7FFF..+0x7FFF, not -0x8000.  The encoder runs this same decoder so its
    choices track what the speaker will actually play, not an idealised copy.
    """
    step = _STEP[index]
    diff = step >> 3
    if nibble & 1:
        diff += step >> 2
    if nibble & 2:
        diff += step >> 1
    if nibble & 4:
        diff += step
    pred = pred - diff if nibble & 8 else pred + diff
    pred = max(-0x7FFF, min(0x7FFF, pred))
    index = max(0, min(88, index + _INDEX[nibble & 7]))
    return pred, index


def encode_adpcm(samples: list[int]) -> bytes:
    """IMA-ADPCM in the DS's layout: a 4-byte header (first sample, step
    index, a zero) and then two samples a byte, low nibble first, padded to a
    whole word with silence because the hardware counts length in words."""
    if not samples:
        samples = [0]
    pred = max(-0x7FFF, min(0x7FFF, samples[0]))
    index = 0
    # A better starting step than zero for a sample that opens loud.
    if len(samples) > 1:
        first_delta = abs(samples[1] - samples[0])
        while index < 88 and _STEP[index] < first_delta:
            index += 1
    header = (pred & 0xFFFF).to_bytes(2, "little") + bytes([index, 0])
    body = bytearray()
    nibbles: list[int] = []
    for s in samples[1:]:
        # Greedy search of all sixteen codes against the hardware decoder:
        # sixteen tries a sample is nothing at build time and it is exact.
        best = None
        for code in range(16):
            p, _ = _decode_nibble(code, pred, index)
            err = abs(p - s)
            if best is None or err < best[0]:
                best = (err, code)
        code = best[1]
        pred, index = _decode_nibble(code, pred, index)
        nibbles.append(code)
    while (len(nibbles) % 8) != 0:          # 4 bytes of body = 8 nibbles
        # Decay toward silence rather than repeating a code, which could
        # leave a DC step on the last word.
        target = 0
        best = None
        for code in range(16):
            p, _ = _decode_nibble(code, pred, index)
            err = abs(p - target)
            if best is None or err < best[0]:
                best = (err, code)
        pred, index = _decode_nibble(best[1], pred, index)
        nibbles.append(best[1])
    for i in range(0, len(nibbles), 2):
        body.append(nibbles[i] | (nibbles[i + 1] << 4))
    return header + bytes(body)


def encode_pcm16(samples: list[int]) -> bytes:
    data = b"".join(max(-32767, min(32767, v)).to_bytes(2, "little", signed=True)
                    for v in samples)
    return data + bytes((-len(data)) % 4)


def read_wav(path: Path) -> list[int]:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            sys.exit(f"sfx_build: {path.name}: must be mono 16-bit "
                     f"(got {w.getnchannels()} ch, {8 * w.getsampwidth()}-bit)")
        if w.getframerate() != TARGET_HZ:
            sys.exit(f"sfx_build: {path.name}: must be {TARGET_HZ} Hz "
                     f"(got {w.getframerate()})")
        raw = w.readframes(w.getnframes())
    return [int.from_bytes(raw[i:i + 2], "little", signed=True)
            for i in range(0, len(raw), 2)]


def read_manifest() -> list[tuple[str, str, int]]:
    takes = []
    for n, line in enumerate(MANIFEST.read_text().splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 3:
            sys.exit(f"sfx_build: sounds.txt:{n}: want `bank file gain`")
        bank, name, gain = parts
        if bank not in BANKS:
            sys.exit(f"sfx_build: sounds.txt:{n}: unknown bank {bank!r}")
        if not gain.isdigit() or not 1 <= int(gain) <= 100:
            sys.exit(f"sfx_build: sounds.txt:{n}: gain is a percentage, 1..100")
        takes.append((bank, name, int(gain)))
    return takes


def generate() -> tuple[str, str]:
    takes = read_manifest()
    # Stable order: by bank, then as listed, so a bank is one contiguous range.
    ordered = sorted(enumerate(takes), key=lambda t: (BANKS.index(t[1][0]), t[0]))
    samples = []
    for _, (bank, name, gain) in ordered:
        pcm = read_wav(SOUNDS / name)
        fmt = FORMAT_PCM16 if len(pcm) < PCM16_BELOW else FORMAT_ADPCM
        data = encode_pcm16(pcm) if fmt == FORMAT_PCM16 else encode_adpcm(pcm)
        frames = max(1, int((len(pcm) / TARGET_HZ) * FRAME_HZ + 0.999))
        samples.append((bank, name, gain, data, frames, fmt))

    total = sum(len(s[3]) for s in samples)
    h = [
        "// Generated by tools/sfx_build.py from dsclient/sounds/; do not hand edit.",
        "//",
        "// The terminal's sampled sounds, as DS IMA-ADPCM or 16-bit PCM.  See sound.h for how",
        "// the conductor uses them and tools/sfx_build.py for the format.",
        "#ifndef DSTERM_SFX_ASSETS_H",
        "#define DSTERM_SFX_ASSETS_H",
        "",
        "#include <stdint.h>",
        "",
        "typedef struct {",
        "    const uint8_t *data;   // see `format`",
        "    uint32_t bytes;        // a whole number of words",
        "    uint16_t hz;           // the rate it was mastered at",
        "    uint16_t frames;       // playing time in frames, rounded up",
        "    uint8_t gain;          // percent of the room volume",
        "    uint8_t format;        // SFX_FORMAT_*",
        "} sfx_sample_t;",
        "",
        "// ADPCM: the DS's IMA-ADPCM, a 4-byte header then nibbles.  PCM16:",
        "// signed little-endian samples, for the short clicks ADPCM smears.",
        "#define SFX_FORMAT_ADPCM 0",
        "#define SFX_FORMAT_PCM16 1",
        "",
        "typedef enum {",
    ]
    h += [f"    SFX_{b.upper()}," for b in BANKS]
    h += [
        "    SFX_BANK_COUNT",
        "} sfx_bank_t;",
        "",
        "typedef struct {",
        "    uint8_t first;         // index into SFX_SAMPLES",
        "    uint8_t count;         // takes; 0 means the chip voice answers",
        "} sfx_range_t;",
        "",
        f"#define SFX_SAMPLE_COUNT {len(samples)}",
        f"#define SFX_TOTAL_BYTES {total}",
        "",
        f"extern const sfx_sample_t SFX_SAMPLES[{max(1, len(samples))}];",
        "extern const sfx_range_t SFX_BANKS[SFX_BANK_COUNT];",
        "// Each bank's file name on the card: /dsterm/sounds/<name>.wav",
        "// replaces it (docs/CUSTOMIZING.md).",
        "extern const char *const SFX_BANK_NAMES[SFX_BANK_COUNT];",
        "",
        "#endif  // DSTERM_SFX_ASSETS_H",
        "",
    ]

    c = [
        "// Generated by tools/sfx_build.py from dsclient/sounds/; do not hand edit.",
        f"// {len(samples)} takes, {total} bytes of sample data.",
        '#include "sfx_assets.h"',
        "",
    ]
    for i, (bank, name, gain, data, frames, fmt) in enumerate(samples):
        kind = "pcm16" if fmt == FORMAT_PCM16 else "adpcm"
        c.append(f"// {name}: {bank}, {frames} frames, gain {gain}, {kind}")
        c.append(f"static const uint8_t take_{i}[{len(data)}] "
                 "__attribute__((aligned(4))) = {")
        for j in range(0, len(data), 16):
            c.append("    " + ", ".join(f"0x{b:02x}" for b in data[j:j + 16]) + ",")
        c.append("};")
        c.append("")
    c.append(f"const sfx_sample_t SFX_SAMPLES[{max(1, len(samples))}] = {{")
    if not samples:
        c.append("    { 0, 0, 0, 0, 0, 0 },")
    for i, (bank, name, gain, data, frames, fmt) in enumerate(samples):
        c.append(f"    {{ take_{i}, {len(data)}u, {TARGET_HZ}u, {frames}u, {gain}u, "
                 f"{fmt}u }},")
    c.append("};")
    c.append("")
    c.append("const char *const SFX_BANK_NAMES[SFX_BANK_COUNT] = {")
    for bank in BANKS:
        c.append(f'    "{bank}",')
    c.append("};")
    c.append("")
    c.append("const sfx_range_t SFX_BANKS[SFX_BANK_COUNT] = {")
    for bank in BANKS:
        idx = [i for i, s in enumerate(samples) if s[0] == bank]
        first = idx[0] if idx else 0
        c.append(f"    {{ {first}u, {len(idx)}u }},   // {bank}")
    c.append("};")
    c.append("")
    return "\n".join(h), "\n".join(c)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="fail if the generated files are out of date")
    args = ap.parse_args()
    h, c = generate()
    if args.check:
        stale = [p.name for p, want in ((OUT_H, h), (OUT_C, c))
                 if not p.exists() or p.read_text() != want]
        if stale:
            print(f"sfx_build: out of date: {', '.join(stale)}; "
                  "run python3 tools/sfx_build.py", file=sys.stderr)
            return 1
        return 0
    OUT_H.write_text(h)
    OUT_C.write_text(c)
    print(f"sfx_build: wrote {OUT_C.relative_to(REPO)} and "
          f"{OUT_H.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
