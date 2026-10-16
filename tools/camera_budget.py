#!/usr/bin/env python3
"""What a camera preview costs, for each size worth considering.

    python3 tools/camera_budget.py

The preview is the reduced stream the DS sends while the camera screen is
open. These candidates are the RGB332-with-dither family the first version
used (64x48); the shipped preview is 128x96 in sixteen greys, two pixels a
byte (dsclient/source/camera.h), chosen after a hardware test showed screens
and pages as noise at 64x48 -- 6 KB a frame at 3 fps. This prints,
per candidate size and rate:

  bytes      the payload, and what crosses the air with header and SEAL tag
  KB/s       upload while the screen is open
  blocks     SHA-256 compressions SEAL spends on one frame's tag
  seal ms    those, plus the ChaCha20 blocks that encrypt it, at 67 MHz,
             from the instruction counts below
  scale ms   the downscale, from the pixels it reads
  CPU        both, as a share of the ARM9 at that rate
  lag ms     how long a keystroke can wait behind one frame in flight
  bars       the finest bar period (viewfinder pixels per black+white pair)
             the preview can carry at all, then the share of contrast left
             on 6-, 8- and 10-pixel bars (capped at 1; the dither can overshoot) -- the stand-in for legible text
  PSNR       luma, reconstruction against the source, over the test card's
             ramp and colour patches

The cycle figures are estimates, labelled as such. sha256_compress compiled
the way the ROM is (-mthumb -mcpu=arm946e-s -O2) is about 6,250
instructions a block: 14 x 16 to load the block, 33 x 48 for the message
schedule, 68 x 64 for the rounds, and about 95 of setup, counted from
`arm-none-eabi-objdump -d`. At 1.0-1.35 cycles an instruction from cache,
that is 6,300-8,500 cycles. The same function in ARM state is about 3,360
instructions. chacha20_block, compiled the same way, runs about 2,170
instructions (a 183-instruction double round ten times, the 16-word store,
and setup), and chacha20_xor adds about 5 a byte: about 2,500 for each 64
bytes encrypted, so 2,500-3,400 cycles. The console prints the real figure on
the camera screen's log line ("seal: N.N ms/frame"); trust that one over
these.

Standard library only. Nothing here is imported by the server.
"""

from __future__ import annotations

import math

SRC_W, SRC_H = 256, 192
CLOCK_HZ = 67_027_964
CYCLES_PER_BLOCK = (6_300, 8_500)
CHACHA_CYCLES_PER_BLOCK = (2_500, 3_400)
# The downscale reads every viewfinder pixel once whatever the target size:
# three masks, three adds, the loop. ~12 cycles a pixel, plus the dither.
CYCLES_PER_SRC_PIXEL = 12
CYCLES_PER_DST_PIXEL = 40
HEADER, TAG, FRAME_FIELDS = 6, 8, 3
LINKS_KBPS = (50, 100, 200)

BAYER = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))


def seal_blocks(payload: int) -> int:
    """HMAC-SHA256 over dir(1) + seq(4) + header(6) + payload: the inner hash
    includes the 64-byte ipad block and 9 bytes of padding; the outer is two
    blocks."""
    message = 1 + 4 + HEADER + payload
    return math.ceil((64 + message + 9) / 64) + 2


def seal_seconds(payload: int) -> list[float]:
    """The tag's SHA-256 blocks and the cipher's ChaCha20 blocks, low and
    high estimates, in seconds at 67 MHz."""
    sha = seal_blocks(payload)
    chacha = math.ceil(payload / 64)
    return [(sha * c + chacha * x) / CLOCK_HZ
            for c, x in zip(CYCLES_PER_BLOCK, CHACHA_CYCLES_PER_BLOCK)]


def test_card() -> list[tuple[int, int, int]]:
    """256x192 of 5-bit RGB: a grey ramp, six colour patches, and groups of
    black and white bars whose period runs from 2 to 16 viewfinder pixels --
    the stand-in for text on a page or a screen, which is what people point
    this at."""
    img = []
    for y in range(SRC_H):
        for x in range(SRC_W):
            if y < 48:
                v = x * 31 // 255
                px = (v, v, v)
            elif y < 96:
                patch = x // 43
                px = ((31, 0, 0), (0, 31, 0), (0, 0, 31),
                      (31, 31, 0), (0, 31, 31), (31, 0, 31))[min(patch, 5)]
            else:
                period = 2 + ((x // 32) * 2)        # 2, 4, ... 16
                on = (x % period) < period // 2
                px = (2, 2, 2) if on else (29, 29, 29)
            img.append(px)
    return img


def downscale(img, w: int, h: int) -> list[tuple[int, int, int]]:
    out = []
    for dy in range(h):
        y0, y1 = dy * SRC_H // h, (dy + 1) * SRC_H // h
        for dx in range(w):
            x0, x1 = dx * SRC_W // w, (dx + 1) * SRC_W // w
            n = (y1 - y0) * (x1 - x0)
            sums = [0, 0, 0]
            for y in range(y0, y1):
                row = y * SRC_W
                for x in range(x0, x1):
                    p = img[row + x]
                    sums[0] += p[0]
                    sums[1] += p[1]
                    sums[2] += p[2]
            bayer = BAYER[dy & 3][dx & 3]
            px = []
            for c, levels in zip(sums, (8, 8, 4)):
                a = (c + n // 2) // n
                num = a * (levels - 1) * 32 + (2 * bayer - 15) * 31
                q = int(num / 992)                  # C's truncation
                px.append(min(max(q, 0), levels - 1))
            out.append((round(px[0] * 255 / 7), round(px[1] * 255 / 7), px[2] * 85))
    return out


def luma(r: float, g: float, b: float) -> float:
    return 0.299 * r + 0.587 * g + 0.114 * b


def reconstruct(preview, w: int, h: int, x: int, y: int):
    return preview[(y * h // SRC_H) * w + (x * w // SRC_W)]


def psnr(img, preview, w: int, h: int) -> float:
    """Luma PSNR over the ramp and the patches (rows 0-95). The bars are left
    out: no preview size reproduces 2-pixel stripes, and their error would
    swamp the figure that tells the dithers and sizes apart."""
    err = 0.0
    rows = 96
    for y in range(rows):
        for x in range(SRC_W):
            s = img[y * SRC_W + x]
            src = luma(*(c * 255 / 31 for c in s))
            err += (src - luma(*reconstruct(preview, w, h, x, y))) ** 2
    mse = err / (SRC_W * rows)
    return 99.0 if mse == 0 else 10 * math.log10(255 ** 2 / mse)


def finest_period(w: int) -> float:
    """Two preview samples per stripe pair: the smallest bar period, in
    viewfinder pixels, the preview can carry at all (anything finer only
    aliases)."""
    return 2 * SRC_W / w


def bar_contrast(preview, w: int, h: int, period: int) -> float:
    """How much of the black-white contrast survives on bars of `period`
    viewfinder pixels, 0..1: a stand-in for whether large text is legible."""
    group = (period - 2) // 2
    xs = range(group * 32 + 2, group * 32 + 30)
    vals = [luma(*reconstruct(preview, w, h, x, 144)) for x in xs]
    full = 255 * 27 / 31
    return (max(vals) - min(vals)) / full


CANDIDATES = (
    (48, 36, (6.0,)),
    (64, 48, (4.0,)),
    (80, 60, (3.0,)),
    (96, 72, (2.0, 2.5, 3.0)),
)


def main() -> int:
    img = test_card()
    print("preview candidates (RGB332, 4x4 ordered dither), at 67 MHz")
    print(f"{'size':>6} {'fps':>4} {'payload':>7} {'on air':>6} {'KB/s':>5} "
          f"{'blocks':>6} {'seal ms':>9} {'scale ms':>8} {'CPU %':>9} "
          f"{'lag ms @50/100/200 KB/s':>24} {'bars px c6/c8/c10':>19} {'PSNR':>5}")
    for w, h, rates in CANDIDATES:
        payload = FRAME_FIELDS + w * h
        on_air = HEADER + payload + TAG
        blocks = seal_blocks(payload)
        seal_ms = [s * 1000 for s in seal_seconds(payload)]
        scale_ms = ((SRC_W * SRC_H * CYCLES_PER_SRC_PIXEL + w * h * CYCLES_PER_DST_PIXEL)
                    / CLOCK_HZ * 1000)
        preview = downscale(img, w, h)
        quality_psnr = psnr(img, preview, w, h)
        bars = f"{finest_period(w):.1f} " + "/".join(
            f"{min(bar_contrast(preview, w, h, p), 1.0):.2f}" for p in (6, 8, 10))
        lag = "/".join(f"{on_air / (k * 1000) * 1000:.0f}" for k in LINKS_KBPS)
        for fps in rates:
            cpu = [(s + scale_ms) * fps / 10 for s in seal_ms]
            print(f"{w}x{h:<3} {fps:>4.1f} {payload:>7} {on_air:>6} "
                  f"{on_air * fps / 1000:>5.1f} {blocks:>6} "
                  f"{seal_ms[0]:>4.1f}-{seal_ms[1]:<4.1f} {scale_ms:>8.1f} "
                  f"{cpu[0]:>4.1f}-{cpu[1]:<4.1f} {lag:>24} "
                  f"{bars:>19} {quality_psnr:>5.1f}")
    print()
    print("the picture (4:2:0 planar, 1 KB chunks, 8 KB window)")
    for name, size in (("640x480", 460_800), ("320x240", 115_200)):
        chunks = math.ceil(size / 1024)
        blocks = chunks * seal_blocks(4 + 1024)
        cpu_s = [chunks * s for s in seal_seconds(4 + 1024)]
        crc_s = size * 5 / CLOCK_HZ
        times = "/".join(f"{size * 1.02 / (k * 1000):.1f}" for k in LINKS_KBPS)
        print(f"  {name}: {size} bytes, {chunks} chunks, {blocks} SEAL blocks "
              f"({cpu_s[0]:.2f}-{cpu_s[1]:.2f} s of CPU), CRC {crc_s * 1000:.0f} ms, "
              f"{times} s on air at 50/100/200 KB/s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
