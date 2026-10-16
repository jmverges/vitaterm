# The pure camera helpers, mirrored byte-for-byte from dsclient/source/camera.c.
# These exist so the server can predict and verify what the card sends: the
# preview is drawn from these bytes, the picture is converted with this same
# integer YUV formula, and the checksum is the CRC both sides compute.
#
# Keep the functions and constants in lockstep with camera.h/camera.c --
# tools/crosscheck.py compares them.

from __future__ import annotations

import os
import queue
import re
import stat
import struct
import threading
import time
import zlib
from pathlib import Path

from .protocol import (
    CAMERA_INNER,
    CAMERA_OUTER,
    PHOTO_BYTES_MAX,
    PHOTO_PATH_MAX,
    ProtocolError,
    photo_bytes,
)

SRC_W = 256
SRC_H = 192
PREVIEW_W = 128
PREVIEW_H = 96
PREVIEW_PIXELS = PREVIEW_W * PREVIEW_H  # 12288
PREVIEW_BYTES = PREVIEW_PIXELS // 2     # 6144: two pixels a byte
PHOTO_W = 640
PHOTO_H = 480
YUYV_BYTES = PHOTO_W * PHOTO_H * 2   # 614400, the capture before packing
PHOTO_FORMAT_FULL = 1
PHOTO_FORMAT_SMALL = 2
PHOTO_BYTES_FULL = PHOTO_W * PHOTO_H * 3 // 2                    # 460800
PHOTO_BYTES_SMALL = (PHOTO_W // 2) * (PHOTO_H // 2) * 3 // 2     # 115200
PHOTO_BYTES_MAX = PHOTO_BYTES_FULL


def photo_bytes(fmt: int) -> int:
    """The packed size of a wire format, 0 for one that is not."""
    return {PHOTO_FORMAT_FULL: PHOTO_BYTES_FULL,
            PHOTO_FORMAT_SMALL: PHOTO_BYTES_SMALL}.get(fmt, 0)

def downscale_preview(src: bytes | bytearray, dst: bytearray) -> None:
    """src is 256x192 RGB555 little-endian words; dst gets PREVIEW_BYTES of
    sixteen greys, two pixels a byte, the left one in the high nibble.

    camera.c's rule exactly: a 2x2 box average per channel in 5-bit space
    ((sum + 2) >> 2), luma y = (77r + 150g + 29b + 128) >> 8, and the level
    (y*15 + 15) // 31."""
    for dy in range(PREVIEW_H):
        r0 = 2 * dy * SRC_W
        r1 = r0 + SRC_W
        for dx in range(PREVIEW_W):
            rs = gs = bs = 0
            for base in (r0, r1):
                for x in (2 * dx, 2 * dx + 1):
                    i = (base + x) * 2
                    p = src[i] | src[i + 1] << 8
                    rs += p & 31
                    gs += (p >> 5) & 31
                    bs += (p >> 10) & 31
            r, g, b = (rs + 2) >> 2, (gs + 2) >> 2, (bs + 2) >> 2
            y = (77 * r + 150 * g + 29 * b + 128) >> 8
            level = (y * 15 + 15) // 31
            k = (dy * PREVIEW_W + dx) // 2
            if dx & 1:
                dst[k] = (dst[k] & 0xF0) | level
            else:
                dst[k] = (level << 4) | (dst[k] & 0x0F)


def gray4_to_8(level: int) -> int:
    return (level & 15) * 17


COLOUR_W = 96
COLOUR_H = 72
COLOUR_BYTES = COLOUR_W * COLOUR_H   # 6912


def downscale_colour(src: bytes | bytearray, dst: bytearray) -> None:
    """src is 256x192 RGB555 little-endian words; dst gets COLOUR_BYTES of
    RGB332. camera.c's rule exactly: the area box over [dx*256/96,
    (dx+1)*256/96) x [dy*192/72, (dy+1)*192/72), per channel in 5-bit space
    ((sum + n/2) // n), then (a*7 + 15) // 31 for R and G, (a*3 + 15) // 31
    for B -- no dither."""
    for dy in range(COLOUR_H):
        y0, y1 = dy * SRC_H // COLOUR_H, (dy + 1) * SRC_H // COLOUR_H
        for dx in range(COLOUR_W):
            x0, x1 = dx * SRC_W // COLOUR_W, (dx + 1) * SRC_W // COLOUR_W
            n = (x1 - x0) * (y1 - y0)
            rs = gs = bs = 0
            for y in range(y0, y1):
                for x in range(x0, x1):
                    i = (y * SRC_W + x) * 2
                    p = src[i] | src[i + 1] << 8
                    rs += p & 31
                    gs += (p >> 5) & 31
                    bs += (p >> 10) & 31
            r, g, b = (rs + n // 2) // n, (gs + n // 2) // n, (bs + n // 2) // n
            dst[dy * COLOUR_W + dx] = (((r * 7 + 15) // 31) << 5 |
                                       ((g * 7 + 15) // 31) << 2 |
                                       ((b * 3 + 15) // 31))


def yuyv_to_rgb888(y: int, cb: int, cr: int) -> tuple[int, int, int]:
    """The SDK's integer YUV -> RGB; cb/cr are already minus 0x80."""
    # C's >> on a negative int floors; Python's does too, so this is a
    # straight translation of camera.c's sar().
    r = y + cr + (cr >> 2) + (cr >> 3) + (cr >> 5)
    g = (
        y
        - ((cb >> 2) + (cb >> 4) + (cb >> 5))
        - ((cr >> 1) + (cr >> 3) + (cr >> 4) + (cr >> 5))
    )
    b = y + cb + (cb >> 1) + (cb >> 2) + (cb >> 6)
    return tuple(min(255, max(0, v)) for v in (r, g, b))


def photo_thumbnail(yuyv: bytes | bytearray, dst: bytearray) -> None:
    """dst gets 256x192 RGB555 little-endian words, bit 15 set."""
    for dy in range(SRC_H):
        sy = dy * PHOTO_H // SRC_H
        for dx in range(SRC_W):
            sx = dx * PHOTO_W // SRC_W
            off = (sy * (PHOTO_W // 2) + (sx >> 1)) * 4
            yy = yuyv[off + (2 if sx & 1 else 0)]
            r, g, b = yuyv_to_rgb888(yy, yuyv[off + 1] - 0x80, yuyv[off + 3] - 0x80)
            w = 0x8000 | (r >> 3) | ((g >> 3) << 5) | ((b >> 3) << 10)
            i = (dy * SRC_W + dx) * 2
            dst[i] = w & 0xFF
            dst[i + 1] = w >> 8


def crc32(data: bytes | bytearray) -> int:
    # The IEEE CRC zlib computes: the same polynomial and convention as
    # camera_crc32_update starting from 0.
    return zlib.crc32(bytes(data)) & 0xFFFFFFFF


def synth_rgb555_frame() -> bytes:
    """The deterministic 256x192 frame the interop vector is built on:
    r=(x*31//255), g=(y*31//191), b=((x+y)&31), bit 15 set."""
    buf = bytearray(SRC_W * SRC_H * 2)
    for y in range(SRC_H):
        for x in range(SRC_W):
            w = 0x8000 | (x * 31 // 255) | ((y * 31 // 191) << 5) | (((x + y) & 31) << 10)
            i = (y * SRC_W + x) * 2
            buf[i] = w & 0xFF
            buf[i + 1] = w >> 8
    return bytes(buf)


def synth_rgb555(x: int, y: int) -> int:
    # The same generator, one pixel at a time (C's loop writes this).
    return 0x8000 | (x * 31 // 255) | ((y * 31 // 191) << 5) | (((x + y) & 31) << 10)


def synth_4k() -> bytes:
    # The deterministic 4 KB buffer the CRC vector is built on.
    return bytes((i * 37 + (i >> 5)) & 0xFF for i in range(4096))


def synth_yuyv_frame() -> bytes:
    """The deterministic 640x480 YUYV the pack vectors are built on; C writes
    the same bytes (test_camera.c's synth_yuyv)."""
    buf = bytearray(YUYV_BYTES)
    for y in range(PHOTO_H):
        for p in range(PHOTO_W // 2):
            i = (y * (PHOTO_W // 2) + p) * 4
            buf[i] = (p * 2 + y * 3) & 0xFF          # Y0
            buf[i + 1] = (p + y * 5) & 0xFF          # Cb
            buf[i + 2] = (p * 2 + 1 + y * 3) & 0xFF  # Y1
            buf[i + 3] = (p * 7 + y) & 0xFF          # Cr
    return bytes(buf)


def photo_pack(yuyv: bytes | bytearray, fmt: int) -> bytes:
    """The 4:2:0 planar packing camera_photo_pack() applies, byte for byte:
    FULL carries every luma and half the chroma rows, SMALL quarters both."""
    if fmt == PHOTO_FORMAT_FULL:
        w, h = PHOTO_W, PHOTO_H
        yplane = bytearray(w * h)
        for y in range(h):
            line = y * w * 2
            for x in range(w):
                yplane[y * w + x] = yuyv[line + (x >> 1) * 4 + (x & 1) * 2]
        cw, ch = w // 2, h // 2
        cbp = bytearray(cw * ch)
        crp = bytearray(cw * ch)
        for cy in range(ch):
            r0 = (2 * cy) * w * 2
            r1 = r0 + w * 2
            for cx in range(cw):
                cbp[cy * cw + cx] = (yuyv[r0 + cx * 4 + 1]
                                     + yuyv[r1 + cx * 4 + 1] + 1) >> 1
                crp[cy * cw + cx] = (yuyv[r0 + cx * 4 + 3]
                                     + yuyv[r1 + cx * 4 + 3] + 1) >> 1
        return bytes(yplane + cbp + crp)
    if fmt == PHOTO_FORMAT_SMALL:
        sw, sh = PHOTO_W // 2, PHOTO_H // 2
        yplane = bytearray(sw * sh)
        for y in range(sh):
            r0 = (2 * y) * PHOTO_W * 2
            r1 = r0 + PHOTO_W * 2
            for x in range(sw):
                yplane[y * sw + x] = (yuyv[r0 + x * 4] + yuyv[r0 + x * 4 + 2]
                                      + yuyv[r1 + x * 4]
                                      + yuyv[r1 + x * 4 + 2] + 2) >> 2
        cw, ch = sw // 2, sh // 2
        cbp = bytearray(cw * ch)
        crp = bytearray(cw * ch)
        for cy in range(ch):
            r0 = (2 * cy) * PHOTO_W * 2
            r1 = r0 + PHOTO_W * 2
            for cx in range(cw):
                cbp[cy * cw + cx] = (yuyv[r0 + 8 * cx + 1]
                                     + yuyv[r0 + 8 * cx + 5]
                                     + yuyv[r1 + 8 * cx + 1]
                                     + yuyv[r1 + 8 * cx + 5] + 2) >> 2
                crp[cy * cw + cx] = (yuyv[r0 + 8 * cx + 3]
                                     + yuyv[r0 + 8 * cx + 7]
                                     + yuyv[r1 + 8 * cx + 3]
                                     + yuyv[r1 + 8 * cx + 7] + 2) >> 2
        return bytes(yplane + cbp + crp)
    return b""


# ----------------------------------------------------------------------------
# Computer side: expansion, conversion, PNG, storage, terminal rendering.
# ----------------------------------------------------------------------------

def expand_rgb332(pixels: bytes) -> bytes:
    """The colour view's RGB332 -> RGB888, as camera.c expands it."""
    out = bytearray(len(pixels) * 3)
    for i, v in enumerate(pixels):
        j = i * 3
        out[j] = ((v >> 5) * 255 + 3) // 7
        out[j + 1] = (((v >> 2) & 7) * 255 + 3) // 7
        out[j + 2] = (v & 3) * 85
    return bytes(out)


def expand_view(fmt: int, pixels: bytes) -> tuple[int, int, bytes]:
    """A preview frame's pixels -> (width, height, RGB888)."""
    from .protocol import PREVIEW_FORMAT_GRAY4, PREVIEW_FORMAT_RGB332
    if fmt == PREVIEW_FORMAT_GRAY4:
        return PREVIEW_W, PREVIEW_H, expand_preview(pixels)
    if fmt == PREVIEW_FORMAT_RGB332:
        return COLOUR_W, COLOUR_H, expand_rgb332(pixels)
    raise ValueError(f"no such preview format {fmt}")


def expand_preview(packed: bytes) -> bytes:
    """The preview's greys -> RGB888, byte per channel: each level times 17,
    the left pixel of a byte from its high nibble."""
    out = bytearray(len(packed) * 6)
    for i, v in enumerate(packed):
        hi, lo = (v >> 4) * 17, (v & 15) * 17
        j = i * 6
        out[j:j + 6] = bytes((hi, hi, hi, lo, lo, lo))
    return bytes(out)


# yuyv_to_rgb888 above is the scalar reference -- it is the interop check, not
# the bulk converter. What a saved picture runs through is planar420_to_rgb,
# which works per 2x2 chroma block and keeps every per-pixel step a table
# lookup, because doing the formula per byte on a 640x480 frame costs ~2 s in
# plain Python and tables bring it under a quarter of that.

# Chroma contributions, one entry per stored chroma byte (value - 128 folded
# in). Identical constants to yuyv_to_rgb888, shared with dsclient's
# camera_yuyv_to_rgb565 by way of the crosscheck vector.
_R_OF_CR = tuple(v + (v >> 2) + (v >> 3) + (v >> 5) for v in range(-128, 128))
_G_OF_CB = tuple(-((v >> 2) + (v >> 4) + (v >> 5)) for v in range(-128, 128))
_G_OF_CR = tuple(-((v >> 1) + (v >> 3) + (v >> 4) + (v >> 5)) for v in range(-128, 128))
_B_OF_CB = tuple(v + (v >> 1) + (v >> 2) + (v >> 6) for v in range(-128, 128))

# y + delta spans roughly -180..430 for the worst chroma; -512..511 covers it
# with room and turns clamping into one lookup.
_CLAMP = bytes(min(255, max(0, i)) for i in range(-512, 512))

# Whether each camera's image is mirrored before it is shown or saved. The
# hardware check against a real DSi decides these; both are False until then.
MIRROR = {CAMERA_INNER: False, CAMERA_OUTER: False}


def planar420_to_rgb(data: bytes, w: int, h: int, mirror: bool = False) -> bytes:
    """I420-style planar 4:2:0 -> RGB888, the same formula as yuyv_to_rgb888
    with chroma sampled at (x>>1, y>>1) -- nearest, matching the packer that
    averaged the source's 2x2 chroma onto exactly that sample point.

    `mirror` is the hardware-check knob (see MIRROR): a horizontal flip of the
    output, applied as the pixels land, not a second pass.
    """
    cw, ch = w // 2, h // 2
    need = w * h + 2 * cw * ch
    if len(data) != need:
        raise ValueError(f"planar 4:2:0 {w}x{h} is {need} bytes, got {len(data)}")
    yplane = memoryview(data)[: w * h]
    cbplane = memoryview(data)[w * h: w * h + cw * ch]
    crplane = memoryview(data)[w * h + cw * ch:]
    out = bytearray(w * h * 3)
    r_of = _R_OF_CR; gcb_of = _G_OF_CB; gcr_of = _G_OF_CR; b_of = _B_OF_CB
    clamp = _CLAMP
    for cy in range(ch):
        crow = cy * cw
        ytop = 2 * cy * w
        ybot = ytop + w
        for cx in range(cw):
            ci = crow + cx
            cb = cbplane[ci]
            cr = crplane[ci]
            rd = r_of[cr]
            gd = gcb_of[cb] + gcr_of[cr]
            bd = b_of[cb]
            for (x, ybase) in ((2 * cx, ytop), (2 * cx + 1, ytop),
                               (2 * cx, ybot), (2 * cx + 1, ybot)):
                dx = (w - 1 - x) if mirror else x
                yy = yplane[ybase + x]
                o = (ybase + dx) * 3
                out[o] = clamp[yy + rd + 512]
                out[o + 1] = clamp[yy + gd + 512]
                out[o + 2] = clamp[yy + bd + 512]
    return bytes(out)


def png_bytes(rgb: bytes, w: int, h: int) -> bytes:
    """RGB888 rows as a PNG. Stdlib only: filter 0 per row, zlib level 6.

    Filter 0 rather than Sub/Paeth: at photo sizes the encode is I/O-bound to
    zlib anyway, and a file a person can decompress by hand is worth more than
    a few percent smaller. The correct CRCs are the part that matters -- every
    reader checks them.
    """
    if len(rgb) != w * h * 3:
        raise ValueError(f"rgb {w}x{h} must be {w * h * 3} bytes")

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    stride = w * 3
    raw = bytearray((stride + 1) * h)
    for y in range(h):
        row = y * (stride + 1)
        raw[row + 1: row + 1 + stride] = rgb[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


class PhotoAssembler:
    """Collects a photo upload. Chunks must land exactly where the count is,
    except that offset 0 abandons the partial picture and starts again --
    which is what the card does after a retake.
    """

    def __init__(self, total_max: int = PHOTO_BYTES_MAX):
        self._buf = bytearray(total_max)
        self.received = 0

    def reset(self) -> None:
        self.received = 0

    def feed(self, offset: int, data: bytes) -> int:
        """Append one chunk; returns the new received count."""
        if offset == 0:
            self.received = 0
        elif offset != self.received:
            raise ProtocolError(
                f"photo chunk at {offset}, expected {self.received}")
        end = offset + len(data)
        if end > len(self._buf):
            raise ProtocolError("photo chunk runs past the buffer")
        self._buf[offset:end] = data
        self.received = end
        return end

    def finish(self, fmt: int, crc: int) -> bytes | None:
        """The packed planar bytes, or None on a CRC mismatch (an integrity
        status back to the card, not a dropped connection)."""
        total = photo_bytes(fmt)
        if not total:
            raise ProtocolError(f"photo format {fmt} is not 1 or 2")
        if self.received != total:
            raise ProtocolError(
                f"photo end at {self.received} of {total} bytes")
        data = bytes(self._buf[:total])
        if zlib.crc32(data) & 0xFFFFFFFF != crc:
            return None
        return data


# -- photo store --------------------------------------------------------------

# Only files named like this are ever deleted by retention or discard --
# a hostile or simply crowded directory loses nothing else.
PHOTO_NAME_RE = re.compile(r"^ds-photo-\d{8}-\d{6}(-\d+)?\.png$")
PHOTO_TMP_RE = re.compile(r"^\.ds-photo-.*\.tmp$")
# ds-photo-YYYYMMDD-HHMMSS + "-NNNN" + ".png"; collision suffixes are bounded
# in save(), so the wire path's length check can be exact.
PHOTO_NAME_MAX = 33


class StoreError(Exception):
    """The photo directory could not be made safe to use."""


def _ascii_ok(text: str) -> bool:
    try:
        raw = text.encode("ascii")
    except UnicodeEncodeError:
        return False
    return all(0x20 <= b <= 0x7E for b in raw)


def wire_path(saved: Path, project: Path) -> str | None:
    """The path PHOTO_SAVED names: relative under the project, absolute
    otherwise. None when it cannot go on the wire (non-ASCII or over
    PHOTO_PATH_MAX)."""
    try:
        text = str(saved.relative_to(project))
    except ValueError:
        text = str(saved)
    if not _ascii_ok(text):
        return None
    if not 1 <= len(text.encode("ascii")) <= PHOTO_PATH_MAX:
        return None
    return text


def git_exclude_dsterm(project: Path, run, on_log=lambda msg: None) -> bool:
    """Add `.dsterm/` to .git/info/exclude once, if the project is a work
    tree. Unanchored, so it also covers a project that is a subdirectory of
    the repository (Claude started inside one package of a monorepo). Returns whether the line is now present. Not a repo and no git are
    both silent no-ops -- photos still save, they are just visible to git.

    A repo unpacked from an archive can carry a .git/info or exclude file
    that is a symlink, so each is lstat-checked and must be a plain file or
    directory owned by this uid -- never followed. The dir is created with
    os.mkdir, not parents-through-links, and the append opens O_NOFOLLOW.
    """
    try:
        out = run(["git", "-C", str(project), "rev-parse",
                   "--git-path", "info/exclude"],
                  capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    if out is None or getattr(out, "returncode", 1) != 0:
        return False
    path = Path(out.stdout.strip())
    if not path.is_absolute():
        path = project / path
    try:
        try:
            st = os.lstat(path.parent)
        except FileNotFoundError:
            os.mkdir(path.parent, 0o755)
        else:
            if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid():
                on_log(f"{path.parent}: not ours to write through")
                return False
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            existing = []
        else:
            # A plain file only: a FIFO here would block the save worker.
            if (not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)
                    or st.st_uid != os.getuid()):
                on_log(f"{path}: not ours to write through")
                return False
            existing = path.read_text(
                encoding="utf-8", errors="replace").splitlines()
        for line in existing:
            if line.strip() in (".dsterm/", ".dsterm", "**/.dsterm/"):
                return True
        fd = os.open(path,
                     os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                     0o644)
        with os.fdopen(fd, "r+b") as fh:
            # Never glue the rule onto a last line with no newline: that
            # would break the user's rule and ignore nothing.
            fh.seek(0, os.SEEK_END)
            if fh.tell() > 0:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    fh.write(b"\n")
            fh.write(b".dsterm/\n")
        return True
    except OSError:
        return False


class PhotoStore:
    """One photo directory: create-if-needed, save, prune, delete.

    `project` set means the directory is <project>/.dsterm/photos and both of
    those components are checked before anything is written: absent (created
    0700) or a real directory owned by this uid -- never a symlink, which a
    checked-out repo could otherwise plant to redirect writes elsewhere.
    `project` None is an explicit --photo-dir: the directory itself gets the
    same check; its parents are created with normal permissions.
    """

    def __init__(self, directory: Path, keep: int = 20,
                 max_age_days: float = 7.0, project: Path | None = None,
                 on_log=lambda msg: None):
        self.dir = Path(directory)
        self.keep = keep
        self.max_age = max_age_days * 86400.0
        self.project = Path(project) if project is not None else None
        self._log = on_log

    @classmethod
    def for_project(cls, project: Path, keep: int = 20,
                    max_age_days: float = 7.0, on_log=lambda msg: None):
        return cls(Path(project) / ".dsterm" / "photos", keep, max_age_days,
                   project=project, on_log=on_log)

    def verify(self) -> None:
        """The same checks as ensure(), creating nothing: every component of
        ours is a real directory owned by this uid, never a symlink. Prune
        and delete run it first, so a .dsterm that a checked-out repository
        made a link is refused rather than deleted through."""
        checked = [self.dir.parent, self.dir] if self.project else [self.dir]
        for d in checked:
            st = os.lstat(d)
            if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
                raise StoreError(f"{d} is not a directory")
            if st.st_uid != os.getuid():
                raise StoreError(f"{d} belongs to uid {st.st_uid}")

    def ensure(self) -> None:
        """Create or tighten the folder to 0700. Raises StoreError on
        anything that is not a plain directory of ours."""
        checked = [self.dir.parent, self.dir] if self.project else [self.dir]
        # Whatever sits above the first checked path is just the filesystem:
        # create it with ordinary permissions.
        checked[0].parent.mkdir(parents=True, exist_ok=True)
        for d in checked:
            try:
                st = os.lstat(d)
            except FileNotFoundError:
                os.mkdir(d, 0o700)
                continue
            # lstat, not stat: a symlink reports the link itself and fails
            # S_ISDIR, which is the refusal -- not the check it defeats.
            if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
                raise StoreError(f"{d} is not a directory")
            if st.st_uid != os.getuid():
                raise StoreError(f"{d} belongs to uid {st.st_uid}")
            if st.st_mode & 0o077:
                os.chmod(d, 0o700)
                self._log(f"{d}: permissions tightened to 0700")

    def wire_prefix(self) -> str:
        """What every saved path will start with on the wire."""
        if self.project is not None:
            return ".dsterm/photos/"
        return str(self.dir) + "/"

    def ascii_ok(self) -> bool:
        """Can paths from here go on the wire at all?"""
        prefix = self.wire_prefix()
        return (_ascii_ok(prefix)
                and len(prefix.encode("ascii")) + PHOTO_NAME_MAX <= PHOTO_PATH_MAX)

    def save(self, png: bytes, now: float | None = None) -> Path:
        """Write `png` under a fresh ds-photo name; returns the final path.

        Temp file first (O_EXCL, O_NOFOLLOW, 0600), then os.replace: a reader
        sees a whole file or none, and a planted link cannot redirect the
        write.
        """
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
        for n in range(0, 10000):
            name = f"ds-photo-{stamp}{'-' + str(n) if n else ''}.png"
            final = self.dir / name
            tmp = self.dir / f".{name}.tmp"
            if final.exists():
                continue
            try:
                fd = os.open(tmp,
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                             0o600)
            except FileExistsError:
                continue
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(png)
                os.replace(tmp, final)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return final
        raise StoreError("no free ds-photo name")

    def _files(self) -> list[tuple[Path, os.stat_result]]:
        out = []
        try:
            entries = list(self.dir.iterdir())
        except OSError:
            return out
        for p in entries:
            try:
                st = os.lstat(p)
            except OSError:
                continue
            out.append((p, st))
        return out

    def prune(self, now: float | None = None) -> None:
        """Keep the newest `keep` photos and nothing older than max_age;
        temp files over an hour old were left by a crash. Only pattern-named
        regular files are touched -- a symlink matching the name is left
        alone rather than deleted through."""
        now = now if now is not None else time.time()
        try:
            self.verify()
        except (OSError, StoreError) as exc:
            self._log(f"photo prune skipped: {exc}")
            return
        named = []
        for p, st in self._files():
            if not stat.S_ISREG(st.st_mode):
                continue
            if PHOTO_NAME_RE.match(p.name):
                named.append((st.st_mtime, p))
            elif PHOTO_TMP_RE.match(p.name) and now - st.st_mtime > 3600:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        named.sort(key=lambda t: t[0], reverse=True)
        for i, (mtime, p) in enumerate(named):
            if i >= self.keep or now - mtime > self.max_age:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def delete(self, path: Path) -> bool:
        """Discard one saved photo: inside this folder, pattern-named, a
        regular file."""
        p = Path(path)
        if p.parent != self.dir or not PHOTO_NAME_RE.match(p.name):
            return False
        try:
            self.verify()
            st = os.lstat(p)
        except OSError:
            return False
        except StoreError:
            return False
        if not stat.S_ISREG(st.st_mode):
            return False
        try:
            os.unlink(p)
            return True
        except OSError:
            return False


# -- terminal rendering -------------------------------------------------------


def _taps(n_out: int, n_in: int) -> list[tuple[int, int, int]]:
    """For each output position, the two source positions it falls between
    and the second one's weight out of 256: linear interpolation, with
    pixel centres aligned so the edges map to the edges."""
    taps = []
    for o in range(n_out):
        pos = ((o + 0.5) * n_in / n_out) - 0.5
        pos = min(max(pos, 0.0), n_in - 1.0)
        i0 = int(pos)
        i1 = min(i0 + 1, n_in - 1)
        taps.append((i0, i1, int((pos - i0) * 256 + 0.5)))
    return taps


def half_blocks(rgb: bytes, w: int, h: int, cols: int, rows: int) -> list[str]:
    """The image fitted into cols x rows of terminal cells, one cell a pixel
    wide and two pixels tall, rendered as a '▀' with the top half's colour as
    the foreground and the bottom's as the background, centred in the box; a
    cell aspect of ~2:1 is what makes a square pixel look square.

    Resampled with linear interpolation, not nearest: the box is rarely a
    whole multiple of the preview, and nearest sampling at, say, 1.25x
    doubles every fourth row, which on a page of text reads as stripes that
    are not in the picture.
    """
    if len(rgb) != w * h * 3:
        raise ValueError(f"rgb {w}x{h} must be {w * h * 3} bytes")
    if cols <= 0 or rows <= 0:
        return []
    # Fit w x h into cols x (2*rows) pixels, aspect preserved.
    scale = min(cols / w, 2.0 * rows / h)
    pw = max(1, int(w * scale))
    ph = max(1, int(h * scale))
    x_off = (cols - pw) // 2
    # ph is in cell-pixels; it occupies ceil(ph/2) terminal rows, centred.
    y_off = max(0, (2 * rows - ph) // 2) // 2
    xt = _taps(pw, w)
    yt = _taps(ph, h)

    def sample(px: int, py: int) -> str:
        x0, x1, fx = xt[px]
        y0, y1, fy = yt[py]
        a, b = (y0 * w + x0) * 3, (y0 * w + x1) * 3
        c, d = (y1 * w + x0) * 3, (y1 * w + x1) * 3
        out = []
        for k in range(3):
            top = rgb[a + k] * (256 - fx) + rgb[b + k] * fx
            bot = rgb[c + k] * (256 - fx) + rgb[d + k] * fx
            out.append(str((top * (256 - fy) + bot * fy + 32768) >> 16))
        return ";".join(out)

    lines = []
    for row in range(rows):
        cells = []
        for col in range(cols):
            px = col - x_off
            out = []
            for half in (0, 1):
                py = (row - y_off) * 2 + half
                if 0 <= px < pw and 0 <= py < ph:
                    out.append(sample(px, py))
                else:
                    out.append(None)
            if out[0] is None and out[1] is None:
                cells.append(" ")
            else:
                fg = out[0] or out[1]
                bg = out[1] or out[0]
                cells.append(f"\x1b[38;2;{fg}m\x1b[48;2;{bg}m▀\x1b[0m")
        lines.append("".join(cells))
    return lines


# -- popups ---------------------------------------------------------------------


class PopupController:
    """A tmux display-popup on every attached client but our own, while the
    camera is on. All tmux calls run on one worker thread behind a queue --
    reconcile() is called from the server's select loop and must never block
    on a subprocess.

    A client that was opened and dismissed (q, Esc, anything) stays dismissed
    until the camera next turns on: `opened` is cleared only on the off->on
    edge, so a popup does not reappear on a client that already closed it,
    while a client that attached after the camera opened still gets one.

    Measured on tmux 3.7b: `display-popup -c NAME -E cmd` run from outside
    tmux BLOCKS until the popup's command exits (a `-E "sleep 3"` returned in
    3.03 s). Waiting on it in the worker would serialise popups -- one per
    client at a time, each gated on the previous client pressing q -- so opens
    go through `starter`, a fire-and-forget spawn, while the synchronous
    `runner` is reserved for queries and the (fast) close.
    """

    MAX_CLIENTS = 8
    MIN_TMUX = (3, 2)

    def __init__(self, runner, socket_name: str, session_name: str,
                 own_pid_fn, command_fn, starter=None,
                 on_log=lambda msg: None, cwd: str | None = None):
        """runner is a callable taking argv and returning a completed process;
        it is always invoked on the worker thread. command_fn() returns the
        shell command the popup runs. starter spawns the popup process and
        returns immediately; it defaults to Popen with output discarded."""
        import subprocess
        self._run = runner
        self._spawn = starter or (
            lambda argv: subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        self._log = on_log
        self._sock = socket_name
        self._session = session_name
        self._own_pid = own_pid_fn
        self._command = command_fn
        self._queue: queue.Queue = queue.Queue()
        # Client name -> the spawn that opened its popup (None if the starter
        # gave nothing back). A name stays here after its popup is dismissed,
        # so it is not reopened until the camera next turns on.
        self._opened: dict[str, object] = {}
        self._cwd = cwd
        # Spawned popup processes, kept so each is poll()ed and eventually
        # reaped -- an unwatched Popen leaves a zombie per popup.
        self._procs: list = []
        self._last_reconcile = 0.0
        self._last_state = False
        self._was_on = False
        self._checked_tmux = False
        self._tmux_ok = True
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()

    # -- selector-thread side --------------------------------------------------

    def reconcile(self, on: bool) -> None:
        """Called from _tick. At most one state reaches the worker a second,
        except an edge, which is never delayed."""
        now = time.monotonic()
        if on == self._last_state and now - self._last_reconcile < 1.0:
            return
        self._last_state = bool(on)
        self._last_reconcile = now
        self._queue.put(bool(on))

    def close(self) -> None:
        self._queue.put(None)
        self._worker.join(timeout=5)

    # -- worker side ------------------------------------------------------------

    def _work(self) -> None:
        last = None
        while True:
            # Drain to the latest state; an old queued poll is worthless.
            state = self._queue.get()
            if state is None:
                self._shutdown()
                return
            try:
                while True:
                    state = self._queue.get_nowait()
                    if state is None:
                        self._shutdown()
                        return
            except queue.Empty:
                pass
            # A steady "off" needs no tmux call; a steady "on" still lists
            # clients, because a client attached after the camera opened is
            # owed a popup of its own.
            if state == last and not state:
                continue
            self._reconcile(state)
            last = state

    def _reap(self) -> None:
        """Drop finished popup processes so none is left a zombie."""
        self._procs = [p for p in self._procs
                       if getattr(p, "poll", lambda: 0)() is None]

    @staticmethod
    def _alive(proc) -> bool:
        poll = getattr(proc, "poll", None)
        return proc is not None and poll is not None and poll() is None

    def _close_ours(self) -> None:
        """Close the camera popups still open, and only those.

        `display-popup -C` closes whatever popup a client has, not just
        ours: a client that dismissed the camera and opened a popup of its
        own (fzf-tmux, an editor) must not lose it. The viewer exits by
        itself a moment after the camera goes off, so it is given that
        moment first; -C is for the ones still up after it, which are still
        the camera's because their spawn has not returned."""
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and any(
                self._alive(p) for p in self._opened.values()):
            time.sleep(0.05)
        for name, proc in self._opened.items():
            if self._alive(proc):
                self._call("display-popup", "-C", "-c", name)
        self._opened.clear()

    def _shutdown(self) -> None:
        """Close every open popup, then give the spawns a moment to exit
        before killing whatever remains."""
        self._close_ours()
        deadline = time.monotonic() + 1.0
        while self._procs and time.monotonic() < deadline:
            self._reap()
            if not self._procs:
                break
            time.sleep(0.05)
        for p in self._procs:
            kill = getattr(p, "kill", None)
            if kill is not None:
                try:
                    kill()
                except Exception:
                    pass
        self._procs.clear()

    def _call(self, *args: str):
        try:
            return self._run(["tmux", "-L", self._sock, *args])
        except Exception:
            return None

    def _reconcile(self, on: bool) -> None:
        if not self._checked_tmux:
            self._checked_tmux = True
            ver = self._call("-V")
            ok = False
            if ver is not None and ver.returncode == 0:
                digits = "".join(
                    ch if ch.isdigit() or ch == "." else " "
                    for ch in ver.stdout).split()
                try:
                    major, minor = (int(p) for p in digits[0].split(".")[:2])
                    ok = (major, minor) >= self.MIN_TMUX
                except (IndexError, ValueError):
                    ok = False
            if not ok:
                self._tmux_ok = False
                self._log("camera popups need tmux >= 3.2; popups are off")
        if not self._tmux_ok:
            return
        self._reap()
        if not on:
            if self._was_on:
                self._close_ours()
            self._was_on = False
            return
        # On: the opened set clears ONLY on the off->on edge -- a popup a
        # client dismissed stays gone until the camera next opens.
        if not self._was_on:
            self._opened.clear()
        self._was_on = True
        listed = self._call("list-clients", "-t", self._session, "-F",
                            "#{client_pid}\t#{client_name}")
        if listed is None or listed.returncode != 0:
            return
        own = self._own_pid()
        cmd = self._command()
        for line in listed.stdout.splitlines():
            # The cap counts everything already opened, not just this pass:
            # _opened keeps dismissed names, so only it bounds the total.
            if len(self._opened) >= self.MAX_CLIENTS:
                break
            parts = line.split("\t")
            if len(parts) != 2:
                continue
            pid_s, name = parts
            try:
                if int(pid_s) == own:
                    continue
            except ValueError:
                pass
            if name in self._opened:
                continue
            argv = ["tmux", "-L", self._sock, "display-popup", "-c", name,
                    "-w", "80%", "-h", "80%", "-T", " DS camera "]
            if self._cwd:
                argv += ["-d", self._cwd]
            try:
                proc = self._spawn(argv + ["-E", cmd])
            except Exception:
                continue
            if proc is not None:
                self._procs.append(proc)
            # Spawned, not confirmed: the popup process exits when the popup
            # closes, and the name stays in _opened either way -- dismissed
            # is the same as still-open until the camera next turns on.
            self._opened[name] = proc


# -- the Unix feed ------------------------------------------------------------

# Server-to-viewer, one way. Per message: a u32be header length, a UTF-8 JSON
# header {"on", "camera", "still", "seq", "w", "h"}, then w*h*3 RGB bytes
# only while on. Nothing flows the other way; a byte from the viewer is read
# and dropped.

CAMERA_NAMES = {CAMERA_INNER: "inner", CAMERA_OUTER: "outer"}
FEED_MAX_HEADER = 512


def feed_message(on: bool, camera: int, still: bool, seq: int,
                 w: int, h: int, rgb: bytes = b"") -> bytes:
    """One feed frame: header length, JSON header, then pixels while on."""
    import json
    header = json.dumps(
        {"on": bool(on), "camera": CAMERA_NAMES.get(camera),
         "still": bool(still), "seq": seq,
         "w": w if on else 0, "h": h if on else 0},
        separators=(",", ":")).encode("utf-8")
    if on and len(rgb) != w * h * 3:
        raise ValueError("an on-frame's pixels are w*h*3 bytes")
    return struct.pack(">I", len(header)) + header + (rgb if on else b"")


class FeedReader:
    """The viewer's end: feed bytes, get (header, rgb-or-empty) messages."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[dict, bytes]]:
        import json
        self._buf += data
        out = []
        while True:
            if len(self._buf) < 4:
                return out
            (hlen,) = struct.unpack(">I", self._buf[:4])
            if hlen > FEED_MAX_HEADER:
                raise ProtocolError(f"feed header {hlen} bytes")
            if len(self._buf) < 4 + hlen:
                return out
            header = json.loads(self._buf[4:4 + hlen].decode("utf-8"))
            pixels = int(header.get("w", 0)) * int(header.get("h", 0)) * 3 \
                if header.get("on") else 0
            # Consume header and pixels together: partial pixels put nothing
            # back, so the buffer only ever holds whole framed units.
            if len(self._buf) < 4 + hlen + pixels:
                return out
            rgb = bytes(self._buf[4 + hlen:4 + hlen + pixels])
            del self._buf[:4 + hlen + pixels]
            out.append((header, rgb))


def camera_view_lines(state: dict, rgb: bytes, cols: int, rows: int,
                      ) -> list[str]:
    """The camera screen as terminal lines: header, fitted image, footer.
    Pure -- the CLI's loop only does I/O and timing around this."""
    on = bool(state.get("on"))
    if not on:
        return ["camera is off — waiting".center(cols)]
    name = state.get("camera") or "?"
    top = (f"● PICTURE TAKEN — DS {name} camera — live, not recorded"
           if state.get("still") else
           f"● CAMERA ON — DS {name} camera — live, not recorded")
    lines = [top[:cols]]
    w, h = int(state.get("w") or 0), int(state.get("h") or 0)
    body = max(0, rows - 2)
    if w and h and rgb:
        lines += half_blocks(rgb, w, h, cols, body)
    else:
        lines += [""] * body   # camera open, no preview yet
    lines.append("pictures are saved only when you press A on the DS"[:cols])
    return lines
