"""The camera types' codecs.

Every bound in the plan's protocol table is checked twice: once by the
encoder (so this server cannot emit a malformed frame) and again by the
decoder (so the card's C decoder has a reference it must match byte for
byte). The refusals below are the contract, not a sample of it.
"""

import struct

import pytest

from dsterm.protocol import (
    ACK_PHOTO,
    ACK_PREVIEW,
    CAMERA_FRAME_SIZE,
    CAMERA_INNER,
    CAMERA_OFF,
    CAMERA_OUTER,
    HEADER_SIZE,
    PHOTO_BYTES,
    PHOTO_BYTES_FULL,
    PHOTO_BYTES_SMALL,
    PHOTO_CHUNK_MAX,
    PHOTO_FORMAT_FULL,
    PHOTO_FORMAT_SMALL,
    PHOTO_PATH_MAX,
    PHOTO_SAVED_BAD_NAME,
    PHOTO_SAVED_BUSY,
    PHOTO_SAVED_DISK,
    PHOTO_SAVED_INTEGRITY,
    PHOTO_SAVED_TOO_SOON,
    PHOTO_SAVED_OK,
    PREVIEW_FLAG_STILL,
    PREVIEW_BYTES,
    READY_FLAG_CAN_SAVE,
    MsgType,
    ProtocolError,
    decode_camera,
    decode_camera_ack,
    decode_camera_frame,
    decode_camera_ready,
    decode_photo_chunk,
    decode_photo_discard,
    decode_photo_end,
    decode_photo_saved,
    encode_camera,
    encode_camera_ack,
    encode_camera_frame,
    encode_camera_ready,
    encode_photo_chunk,
    encode_photo_discard,
    encode_photo_end,
    encode_photo_saved,
)


def payload(frame: bytes) -> bytes:
    return frame[HEADER_SIZE:]


class TestCamera:
    def test_round_trip(self):
        for state in (CAMERA_OFF, CAMERA_INNER, CAMERA_OUTER):
            assert decode_camera(payload(encode_camera(state))) == state

    def test_bad_state_refused(self):
        with pytest.raises(ProtocolError):
            encode_camera(3)
        with pytest.raises(ProtocolError):
            decode_camera(b"\x03")
        with pytest.raises(ProtocolError):
            decode_camera(b"")
        with pytest.raises(ProtocolError):
            decode_camera(b"\x01\x02")


class TestPreviewGreys:
    """The 128x96 sixteen-grey preview (camera.h's rule): a 2x2 box, BT.601
    luma, sixteen levels, two pixels a byte with the left one high."""

    def test_one_white_block_lights_one_nibble(self):
        src = bytearray(dc.SRC_W * dc.SRC_H * 2)
        bx, by = 7, 3
        for y in (2 * by, 2 * by + 1):
            for x in (2 * bx, 2 * bx + 1):
                i = (y * dc.SRC_W + x) * 2
                src[i], src[i + 1] = 0xFF, 0x7F
        dst = bytearray(dc.PREVIEW_BYTES)
        dc.downscale_preview(src, dst)
        k = (by * dc.PREVIEW_W + bx) // 2
        assert dst[k] == 0x0F                    # odd x: the low nibble
        assert sum(dst) == 0x0F

    def test_expansion_is_level_times_17(self):
        rgb = dc.expand_preview(bytes((0xF0, 0x08)))
        assert rgb == bytes((255,) * 3 + (0,) * 3 + (0,) * 3 + (136,) * 3)

    def test_the_terminal_view_scales_smoothly(self):
        # A ramp stretched by an uneven 1.25x stays a ramp: every column at
        # least as bright as the one before, none repeated in a run of three
        # (nearest sampling doubled every fourth column, which on text read
        # as stripes).
        w, h = 128, 96
        rgb = bytes(v for y in range(h) for x in range(w) for v in (2 * x,) * 3)
        lines = dc.half_blocks(rgb, w, h, 160, 60)
        import re
        fg = [int(m) for m in re.findall(r"38;2;(\d+);", lines[30])]
        assert fg == sorted(fg)
        assert all(not (a == b == c) for a, b, c in zip(fg, fg[1:], fg[2:]))

    def test_the_colour_view_round_trips(self):
        from dsterm.protocol import PREVIEW_FORMAT_RGB332, COLOUR_BYTES
        pixels = bytes((i * 13) & 0xFF for i in range(COLOUR_BYTES))
        frame = encode_camera_frame(0, 7, pixels, PREVIEW_FORMAT_RGB332)
        assert decode_camera_frame(payload(frame)) == (PREVIEW_FORMAT_RGB332, 0, 7, pixels)
        # Each format at its own size only: greys' length is not colour's.
        with pytest.raises(ProtocolError):
            encode_camera_frame(0, 7, pixels[:PREVIEW_BYTES], PREVIEW_FORMAT_RGB332)
        bad = bytearray(payload(frame))
        bad[0] = 2                        # says greys, carries colour's bytes
        with pytest.raises(ProtocolError):
            decode_camera_frame(bytes(bad))
        w, h, rgb = dc.expand_view(PREVIEW_FORMAT_RGB332, pixels)
        assert (w, h) == (96, 72) and len(rgb) == 96 * 72 * 3
        assert dc.expand_rgb332(bytes((0xE0, 0x1C, 0x03))) == bytes(
            (255, 0, 0, 0, 255, 0, 0, 0, 255))

    def test_the_colour_downscale_keeps_pure_colours(self):
        src = bytearray(dc.SRC_W * dc.SRC_H * 2)
        for word, want in ((31, 0xE0), (31 << 5, 0x1C), (31 << 10, 0x03)):
            for i in range(0, len(src), 2):
                src[i], src[i + 1] = word & 0xFF, word >> 8
            dst = bytearray(dc.COLOUR_BYTES)
            dc.downscale_colour(src, dst)
            assert set(dst) == {want}

    def test_the_live_view_reads_like_a_screen(self):
        # The reason for the change: on a page of text the greys keep the
        # lines apart. A 2-pixel black/white stripe survives as alternating
        # rows of 0 and 15; the old 64x48 box averaged it to one flat grey.
        src = bytearray(dc.SRC_W * dc.SRC_H * 2)
        for y in range(dc.SRC_H):
            if (y // 2) % 2 == 0:
                for x in range(dc.SRC_W):
                    i = (y * dc.SRC_W + x) * 2
                    src[i], src[i + 1] = 0xFF, 0x7F
        dst = bytearray(dc.PREVIEW_BYTES)
        dc.downscale_preview(src, dst)
        rows = {dst[(r * dc.PREVIEW_W) // 2] for r in range(dc.PREVIEW_H)}
        assert rows == {0xFF, 0x00}


class TestCameraFrame:
    def test_round_trip(self):
        pixels = bytes((i * 7) & 0xFF for i in range(PREVIEW_BYTES))
        frame = encode_camera_frame(PREVIEW_FLAG_STILL, 0xAB, pixels)
        assert len(frame) == HEADER_SIZE + 3 + PREVIEW_BYTES
        fmt, flags, seq, back = decode_camera_frame(payload(frame))
        assert fmt == 2
        assert flags == PREVIEW_FLAG_STILL and seq == 0xAB and back == pixels

    def test_every_bound(self):
        pixels = bytes(PREVIEW_BYTES)
        with pytest.raises(ProtocolError):
            encode_camera_frame(0x02, 0, pixels)      # a reserved flag bit
        with pytest.raises(ProtocolError):
            encode_camera_frame(0, 0, pixels[:-1])    # a short picture
        with pytest.raises(ProtocolError):
            encode_camera_frame(0, 0, pixels + b"x")  # a long one
        good = payload(encode_camera_frame(0, 0, pixels))
        with pytest.raises(ProtocolError):
            decode_camera_frame(good[:-1])
        with pytest.raises(ProtocolError):
            decode_camera_frame(good + b"x")
        for fmt in (1, 4):              # the old 64x48 RGB332, and one never made
            bad = bytearray(good)
            bad[0] = fmt
            with pytest.raises(ProtocolError):
                decode_camera_frame(bytes(bad))
        bad = bytearray(good)
        bad[1] = 0x80                                 # reserved flag bits
        with pytest.raises(ProtocolError):
            decode_camera_frame(bytes(bad))


class TestPhotoChunk:
    def test_round_trip(self):
        data = bytes(range(256)) * 4
        offset, back = decode_photo_chunk(payload(encode_photo_chunk(1024, data)))
        assert offset == 1024 and back == data

    def test_bounds(self):
        with pytest.raises(ProtocolError):
            encode_photo_chunk(0, b"")                        # empty
        with pytest.raises(ProtocolError):
            encode_photo_chunk(0, bytes(PHOTO_CHUNK_MAX + 1))  # too big
        with pytest.raises(ProtocolError):
            encode_photo_chunk(PHOTO_BYTES - 4, bytes(5))      # runs past
        with pytest.raises(ProtocolError):
            encode_photo_chunk(PHOTO_BYTES, b"x")              # offset past
        assert decode_photo_chunk(
            payload(encode_photo_chunk(PHOTO_BYTES - 4, b"1234"))) == \
            (PHOTO_BYTES - 4, b"1234")
        with pytest.raises(ProtocolError):
            decode_photo_chunk(b"\x00\x00\x00\x00")            # no data
        with pytest.raises(ProtocolError):
            decode_photo_chunk(b"\xff" * (4 + PHOTO_CHUNK_MAX + 1))


class TestPhotoEnd:
    def test_round_trip(self):
        fmt, camera, crc = decode_photo_end(
            payload(encode_photo_end(PHOTO_FORMAT_FULL, CAMERA_OUTER,
                                     0xCBF43926)))
        assert fmt == PHOTO_FORMAT_FULL and camera == CAMERA_OUTER
        assert crc == 0xCBF43926
        assert len(encode_photo_end(PHOTO_FORMAT_SMALL, CAMERA_INNER, 0)) == \
            HEADER_SIZE + 10

    def test_each_format_names_its_own_total(self):
        for fmt, want in ((PHOTO_FORMAT_FULL, PHOTO_BYTES_FULL),
                          (PHOTO_FORMAT_SMALL, PHOTO_BYTES_SMALL)):
            body = payload(encode_photo_end(fmt, CAMERA_INNER, 1))
            assert struct.unpack(">I", body[2:6])[0] == want
            assert decode_photo_end(body) == (fmt, CAMERA_INNER, 1)

    def test_bounds(self):
        with pytest.raises(ProtocolError):
            encode_photo_end(PHOTO_FORMAT_FULL, CAMERA_OFF, 0)
        with pytest.raises(ProtocolError):
            encode_photo_end(9, CAMERA_INNER, 0)
        good = bytearray(payload(encode_photo_end(PHOTO_FORMAT_FULL,
                                                  CAMERA_INNER, 1)))
        with pytest.raises(ProtocolError):
            decode_photo_end(bytes(good[:-1]))
        bad = bytearray(good)
        bad[0] = 9                                     # a format we do not have
        with pytest.raises(ProtocolError):
            decode_photo_end(bytes(bad))
        bad = bytearray(good)
        bad[1] = 0                                     # off is not a camera
        with pytest.raises(ProtocolError):
            decode_photo_end(bytes(bad))
        bad = bytearray(good)
        bad[5] ^= 1                                    # total no longer the
        with pytest.raises(ProtocolError):             # format's own size
            decode_photo_end(bytes(bad))
        # The small format's total against the full format byte is a lie too.
        bad = bytearray(payload(encode_photo_end(PHOTO_FORMAT_SMALL,
                                                 CAMERA_INNER, 1)))
        bad[0] = PHOTO_FORMAT_FULL
        with pytest.raises(ProtocolError):
            decode_photo_end(bytes(bad))


class TestPhotoDiscard:
    def test_round_trip(self):
        assert decode_photo_discard(payload(encode_photo_discard(7))) == 7
        assert len(encode_photo_discard(0xFFFF)) == HEADER_SIZE + 2

    def test_zero_id_refused(self):
        with pytest.raises(ProtocolError):
            encode_photo_discard(0)
        with pytest.raises(ProtocolError):
            decode_photo_discard(b"\x00\x00")
        with pytest.raises(ProtocolError):
            decode_photo_discard(b"\x00")


class TestCameraReady:
    def test_round_trip(self):
        assert decode_camera_ready(payload(encode_camera_ready(True))) is True
        assert decode_camera_ready(payload(encode_camera_ready(False))) is False

    def test_reserved_bits_refused(self):
        with pytest.raises(ProtocolError):
            decode_camera_ready(b"\x02")
        with pytest.raises(ProtocolError):
            decode_camera_ready(b"\xff")
        with pytest.raises(ProtocolError):
            decode_camera_ready(b"")
        with pytest.raises(ProtocolError):
            decode_camera_ready(b"\x01\x00")


class TestCameraAck:
    def test_round_trip(self):
        assert decode_camera_ack(payload(encode_camera_ack(ACK_PREVIEW, 7))) == \
            (ACK_PREVIEW, 7)
        assert decode_camera_ack(
            payload(encode_camera_ack(ACK_PHOTO, 614400))) == (ACK_PHOTO, 614400)

    def test_bounds(self):
        with pytest.raises(ProtocolError):
            encode_camera_ack(2, 0)
        with pytest.raises(ProtocolError):
            encode_camera_ack(ACK_PREVIEW, 256)         # a seq is one byte
        with pytest.raises(ProtocolError):
            decode_camera_ack(b"\x00\x00\x00\x01\x00")  # the same, off the wire
        with pytest.raises(ProtocolError):
            decode_camera_ack(b"\x02\x00\x00\x00\x00")
        with pytest.raises(ProtocolError):
            decode_camera_ack(b"\x01\x00\x00")


class TestPhotoSaved:
    def test_saved_round_trip(self):
        status, photo_id, path = decode_photo_saved(
            payload(encode_photo_saved(PHOTO_SAVED_OK, 42, "/p/ds-photo.png")))
        assert status == PHOTO_SAVED_OK and photo_id == 42
        assert path == "/p/ds-photo.png"

    def test_unsaved_round_trip(self):
        for status in (PHOTO_SAVED_BUSY, PHOTO_SAVED_INTEGRITY,
                       PHOTO_SAVED_DISK, PHOTO_SAVED_TOO_SOON,
                       PHOTO_SAVED_BAD_NAME):
            frame = encode_photo_saved(status, 0, "")
            assert len(frame) == HEADER_SIZE + 4
            assert decode_photo_saved(payload(frame)) == (status, 0, "")

    def test_unsaved_carries_nothing(self):
        with pytest.raises(ProtocolError):
            encode_photo_saved(PHOTO_SAVED_BUSY, 1, "")
        with pytest.raises(ProtocolError):
            encode_photo_saved(PHOTO_SAVED_DISK, 0, "/x")
        with pytest.raises(ProtocolError):
            decode_photo_saved(bytes((PHOTO_SAVED_DISK, 0, 1, 0)))
        with pytest.raises(ProtocolError):
            decode_photo_saved(bytes((PHOTO_SAVED_DISK, 0, 0, 1)) + b"x")

    def test_saved_needs_id_and_path(self):
        with pytest.raises(ProtocolError):
            encode_photo_saved(PHOTO_SAVED_OK, 0, "/x")
        with pytest.raises(ProtocolError):
            encode_photo_saved(PHOTO_SAVED_OK, 1, "")
        with pytest.raises(ProtocolError):
            encode_photo_saved(PHOTO_SAVED_OK, 1, "x" * (PHOTO_PATH_MAX + 1))
        with pytest.raises(ProtocolError):
            encode_photo_saved(PHOTO_SAVED_OK, 1, "has\x01a\x7fctrl")
        with pytest.raises(ProtocolError):
            encode_photo_saved(PHOTO_SAVED_OK, 1, "accented é")
        # Off the wire: id 0, an empty path, and a non-printable one.
        with pytest.raises(ProtocolError):
            decode_photo_saved(bytes((0, 0, 0, 1)) + b"x")
        with pytest.raises(ProtocolError):
            decode_photo_saved(bytes((0, 0, 7, 0)))
        with pytest.raises(ProtocolError):
            decode_photo_saved(bytes((0, 0, 7, 1)) + b"\x01")
        # The declared length and the payload have to be the same thing.
        with pytest.raises(ProtocolError):
            decode_photo_saved(bytes((0, 0, 7, 2)) + b"x")
        with pytest.raises(ProtocolError):
            decode_photo_saved(bytes((0, 0, 7, 1)) + b"xy")
        with pytest.raises(ProtocolError):
            decode_photo_saved(b"\x00\x00\x07")

    def test_status_range(self):
        with pytest.raises(ProtocolError):
            encode_photo_saved(6, 0, "")
        with pytest.raises(ProtocolError):
            decode_photo_saved(bytes((6, 0, 0, 0)))


class TestNothingIsPreAuth:
    def test_camera_types_still_need_auth(self):
        # The camera messages are sent only after CAMERA_READY, which only
        # ever arrives after AUTH_OK -- none may be reachable before it.
        from dsterm.protocol import PRE_AUTH_ALLOWED
        for t in (MsgType.CAMERA, MsgType.CAMERA_FRAME, MsgType.PHOTO_CHUNK,
                  MsgType.PHOTO_END, MsgType.PHOTO_DISCARD):
            assert t not in PRE_AUTH_ALLOWED


# ----------------------------------------------------------------------------
# The computer side: conversion, PNG, store, feed, popups, and the wire path.
# ----------------------------------------------------------------------------

import os
import socket
import subprocess
import tempfile
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

from dsterm import camera as dc
from dsterm.auth import generate_key
from dsterm.client import Client
from dsterm.protocol import FEATURE_CAMERA

from test_server import ServerHarness


def _png_decode(png: bytes):
    """The test's own PNG reader: parse chunks, decompress, filter 0."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos = 8
    w = h = None
    idat = b""
    while pos < len(png):
        length, tag = struct.unpack(">I4s", png[pos:pos + 8])
        body = png[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", png[pos + 8 + length:pos + 12 + length])
        assert zlib.crc32(tag + body) & 0xFFFFFFFF == crc
        if tag == b"IHDR":
            w, h, depth, colour = struct.unpack(">IIBB", body[:10])
            assert depth == 8 and colour == 2
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = w * 3
    out = bytearray()
    for y in range(h):
        assert raw[y * (stride + 1)] == 0
        out += raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
    return w, h, bytes(out)


class TestPlanarToRgb:
    def test_matches_the_scalar_reference(self):
        yuyv = dc.synth_yuyv_frame()
        for fmt, (w, h) in ((PHOTO_FORMAT_FULL, (640, 480)),
                            (PHOTO_FORMAT_SMALL, (320, 240))):
            packed = dc.photo_pack(yuyv, fmt)
            rgb = dc.planar420_to_rgb(packed, w, h)
            cw, ch = w // 2, h // 2
            for y in range(0, h, 13):
                for x in range(0, w, 13):
                    yv = packed[y * w + x]
                    cb = packed[w * h + (y >> 1) * cw + (x >> 1)]
                    cr = packed[w * h + cw * ch + (y >> 1) * cw + (x >> 1)]
                    i = (y * w + x) * 3
                    assert tuple(rgb[i:i + 3]) == dc.yuyv_to_rgb888(
                        yv, cb - 128, cr - 128), (fmt, x, y)

    def test_mirror_flips_horizontally(self):
        yuyv = dc.synth_yuyv_frame()
        packed = dc.photo_pack(yuyv, PHOTO_FORMAT_FULL)
        rgb = dc.planar420_to_rgb(packed, 640, 480)
        rgbm = dc.planar420_to_rgb(packed, 640, 480, mirror=True)
        assert rgbm[(480 - 1 - 200) * 640 * 3 - 3] != rgb or True
        row = 100
        for x in range(0, 640, 50):
            a = rgb[(row * 640 + x) * 3:(row * 640 + x) * 3 + 3]
            b = rgbm[(row * 640 + 639 - x) * 3:(row * 640 + 639 - x) * 3 + 3]
            assert a == b

    def test_speed(self):
        yuyv = dc.synth_yuyv_frame()
        packed = dc.photo_pack(yuyv, PHOTO_FORMAT_FULL)
        t = time.perf_counter()
        rgb = dc.planar420_to_rgb(packed, 640, 480)
        convert = time.perf_counter() - t
        t = time.perf_counter()
        dc.png_bytes(rgb, 640, 480)
        encode = time.perf_counter() - t
        # The brief's bound is ~2 s for the conversion; tables keep it far
        # under that. Generous margins -- CI is not this Mac.
        assert convert < 2.0 and encode < 2.0
        print(f"\nplanar420_to_rgb {convert:.3f}s, png {encode:.3f}s")


class TestPng:
    def test_round_trip(self):
        w, h = 16, 8
        rgb = bytes((x * 16 + y) % 256 for y in range(h)
                    for x in range(w) for _ in range(3))
        dw, dh, pixels = _png_decode(dc.png_bytes(rgb, w, h))
        assert (dw, dh) == (w, h) and pixels == rgb


class TestAssembler:
    def test_contiguous_then_crc(self):
        asm = dc.PhotoAssembler()
        data = dc.photo_pack(dc.synth_yuyv_frame(), PHOTO_FORMAT_SMALL)
        asm.feed(0, data[:1000])
        asm.feed(1000, data[1000:])
        assert asm.finish(PHOTO_FORMAT_SMALL,
                          zlib.crc32(data) & 0xFFFFFFFF) == data

    def test_gap_is_an_error(self):
        asm = dc.PhotoAssembler()
        asm.feed(0, b"1234")
        with pytest.raises(ProtocolError):
            asm.feed(5, b"x")

    def test_zero_restarts(self):
        asm = dc.PhotoAssembler()
        asm.feed(0, b"1234")
        asm.feed(0, b"ab")
        assert asm.received == 2

    def test_incomplete_finish_is_an_error(self):
        asm = dc.PhotoAssembler()
        asm.feed(0, b"x" * 10)
        with pytest.raises(ProtocolError):
            asm.finish(PHOTO_FORMAT_SMALL, 0)

    def test_bad_crc_is_none_not_an_error(self):
        asm = dc.PhotoAssembler()
        data = dc.photo_pack(dc.synth_yuyv_frame(), PHOTO_FORMAT_SMALL)
        asm.feed(0, data)
        assert asm.finish(PHOTO_FORMAT_SMALL, 0xDEADBEEF) is None


class TestPhotoStore:
    def store(self, tmp_path):
        return dc.PhotoStore.for_project(tmp_path)

    def test_modes(self, tmp_path):
        store = self.store(tmp_path)
        store.ensure()
        assert (tmp_path / ".dsterm").stat().st_mode & 0o777 == 0o700
        assert store.dir.stat().st_mode & 0o777 == 0o700
        p = store.save(b"png")
        assert p.stat().st_mode & 0o777 == 0o600
        assert p.name.startswith("ds-photo-") and p.suffix == ".png"
        # A folder left permissive is tightened, not trusted.
        os.chmod(store.dir, 0o755)
        store.ensure()
        assert store.dir.stat().st_mode & 0o777 == 0o700

    def test_wire_path_is_relative(self, tmp_path):
        store = self.store(tmp_path)
        store.ensure()
        p = store.save(b"png")
        assert dc.wire_path(p, tmp_path) == f".dsterm/photos/{p.name}"
        outside = Path(tempfile.mkdtemp(dir="/tmp")) / "x.png"
        assert dc.wire_path(outside, tmp_path) == str(outside)

    def test_symlinked_dotdsterm_refused(self, tmp_path):
        real = Path(tempfile.mkdtemp(dir="/tmp"))
        os.symlink(real, tmp_path / ".dsterm")
        with pytest.raises(dc.StoreError):
            self.store(tmp_path).ensure()

    def test_prune_keeps_only_the_newest_and_new(self, tmp_path):
        store = dc.PhotoStore(tmp_path / "photos", keep=3)
        store.ensure()
        old = time.time() - 8 * 86400
        made = []
        for i in range(5):
            p = store.dir / f"ds-photo-2026010{i}-120000.png"
            p.write_bytes(b"x")
            os.utime(p, (old if i < 2 else time.time(),) * 2)
            made.append(p)
        store.prune()
        survivors = {p.name for p in store.dir.iterdir()}
        assert len(survivors) == 3
        assert made[0].name not in survivors and made[1].name not in survivors

    def test_prune_never_deletes_other_names_or_symlinks(self, tmp_path):
        store = dc.PhotoStore(tmp_path / "photos", keep=0)
        store.ensure()
        keep = store.dir / "notes.txt"
        keep.write_text("x")
        link = store.dir / "ds-photo-20260101-120000.png"
        os.symlink(keep, link)
        stale = store.dir / ".ds-photo-old.tmp"
        stale.write_text("x")
        os.utime(stale, (time.time() - 7200,) * 2)
        store.prune()
        assert keep.exists() and link.is_symlink() and not stale.exists()

    def test_delete_stays_in_the_folder(self, tmp_path):
        store = self.store(tmp_path)
        store.ensure()
        p = store.save(b"png")
        assert not store.delete(tmp_path / "unrelated.txt")
        assert store.delete(p) and not p.exists()

    def test_git_exclude(self, tmp_path):
        # A non-repo is a quiet no-op.
        assert not dc.git_exclude_dsterm(tmp_path, subprocess.run)
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        assert dc.git_exclude_dsterm(repo, subprocess.run)
        assert dc.git_exclude_dsterm(repo, subprocess.run)
        exclude = (repo / ".git" / "info" / "exclude").read_text()
        assert exclude.splitlines().count(".dsterm/") == 1

    def test_git_exclude_covers_a_project_inside_the_repo(self, tmp_path):
        # Claude started in one package of a monorepo: the rule is not
        # anchored to the repository's root, so photos there are ignored too.
        repo = tmp_path / "repo"
        sub = repo / "packages" / "app"
        sub.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        assert dc.git_exclude_dsterm(sub, subprocess.run)
        store = dc.PhotoStore.for_project(sub)
        store.ensure()
        store.save(b"png")
        status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain",
                                 "--untracked-files=all"],
                                capture_output=True, text=True, check=True).stdout
        assert ".dsterm" not in status

    def test_git_exclude_never_glues_onto_the_last_rule(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        exclude = repo / ".git" / "info" / "exclude"
        exclude.write_text("*.log")                     # no final newline
        assert dc.git_exclude_dsterm(repo, subprocess.run)
        assert exclude.read_text().splitlines()[-2:] == ["*.log", ".dsterm/"]

    def test_git_exclude_refuses_what_is_not_a_plain_file(self, tmp_path):
        # A FIFO would block the save worker forever.
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        exclude = repo / ".git" / "info" / "exclude"
        exclude.unlink()
        os.mkfifo(exclude)
        assert not dc.git_exclude_dsterm(repo, subprocess.run)

    def test_retention_and_discard_refuse_a_linked_dsterm(self, tmp_path):
        # A repository that commits .dsterm as a link elsewhere: nothing is
        # pruned or deleted through it.
        elsewhere = tmp_path / "elsewhere" / "photos"
        elsewhere.mkdir(parents=True)
        victim = elsewhere / "ds-photo-20200101-000000.png"
        victim.write_bytes(b"x")
        old = time.time() - 30 * 86400
        os.utime(victim, (old, old))
        project = tmp_path / "project"
        project.mkdir()
        (project / ".dsterm").symlink_to(tmp_path / "elsewhere")
        store = dc.PhotoStore.for_project(project)
        store.prune()
        assert not store.delete(project / ".dsterm" / "photos" / victim.name)
        assert victim.exists()

    def _repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        return repo

    def test_git_exclude_never_follows_a_symlinked_info_dir(self, tmp_path):
        repo = self._repo(tmp_path)
        # A hostile archive plants .git/info as a link: the append must not
        # land wherever it points.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        target = elsewhere / "exclude"
        target.write_text("sentinel\n")
        info = repo / ".git" / "info"
        for p in info.iterdir():
            p.unlink()
        info.rmdir()
        info.symlink_to(elsewhere)
        assert not dc.git_exclude_dsterm(repo, subprocess.run)
        assert target.read_text() == "sentinel\n"
        assert info.is_symlink()

    def test_git_exclude_never_follows_a_symlinked_exclude_file(
            self, tmp_path):
        repo = self._repo(tmp_path)
        target = tmp_path / "elsewhere.txt"
        target.write_text("sentinel\n")
        exclude = repo / ".git" / "info" / "exclude"
        if exclude.exists():
            exclude.unlink()
        exclude.symlink_to(target)
        assert not dc.git_exclude_dsterm(repo, subprocess.run)
        assert target.read_text() == "sentinel\n"
        assert exclude.is_symlink()


class TestHalfBlocks:
    def test_exact_2x2(self):
        rgb = bytes([255, 0, 0, 0, 255, 0,      # top row: red, green
                     0, 0, 255, 255, 255, 0])   # bottom row: blue, yellow
        lines = dc.half_blocks(rgb, 2, 2, 2, 1)
        assert lines == [
            "\x1b[38;2;255;0;0m\x1b[48;2;0;0;255m▀\x1b[0m"
            "\x1b[38;2;0;255;0m\x1b[48;2;255;255;0m▀\x1b[0m"
        ]

    def test_centers(self):
        lines = dc.half_blocks(bytes(12), 2, 2, 10, 5)
        assert len(lines) == 5 and all(len(_) for _ in lines)


class TestFeedWire:
    def test_roundtrip_in_pieces(self):
        rgb = bytes(range(256)) * 18  # 4608 = 64*24*3... just some bytes
        rgb = rgb[:2 * 3 * 3]
        msg = dc.feed_message(True, CAMERA_OUTER, False, 7, 2, 3, rgb)
        r = dc.FeedReader()
        out = []
        for i in range(0, len(msg), 5):
            out += r.feed(msg[i:i + 5])
        assert out == [({"on": True, "camera": "outer", "still": False,
                         "seq": 7, "w": 2, "h": 3}, rgb)]

    def test_off_carries_no_pixels(self):
        msg = dc.feed_message(False, 0, False, 0, 0, 0)
        (header, rgb), = dc.FeedReader().feed(msg)
        assert header["on"] is False and rgb == b""

    def test_view_lines(self):
        off = dc.camera_view_lines({"on": False}, b"", 40, 10)
        assert "camera is off" in off[0]
        on = dc.camera_view_lines(
            {"on": True, "camera": "outer", "still": False, "w": 2, "h": 2},
            bytes(12), 40, 10)
        assert "CAMERA ON" in on[0] and "outer" in on[0]
        assert "press A" in on[-1]
        still = dc.camera_view_lines(
            {"on": True, "camera": "inner", "still": True, "w": 2, "h": 2},
            bytes(12), 40, 10)
        assert "PICTURE TAKEN" in still[0]


class TestPopupController:
    def _make(self, clients, own_pid=9999):
        """clients: [(pid, name)]. Returns (ctl, sync_calls, spawned)."""
        calls, spawned = [], []

        def runner(argv):
            calls.append(list(argv))
            if argv[-1] == "-V":
                return SimpleNamespace(returncode=0, stdout="tmux 3.7b")
            if "list-clients" in argv:
                text = "".join(f"{pid}\t{name}\n" for pid, name in clients)
                return SimpleNamespace(returncode=0, stdout=text)
            return SimpleNamespace(returncode=0, stdout="")

        ctl = dc.PopupController(runner, "sock", "=sess",
                                 own_pid_fn=lambda: own_pid,
                                 command_fn=lambda: "CAMERA_CMD",
                                 starter=spawned.append)
        return ctl, calls, spawned

    def _wait(self, fn, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if fn():
                return True
            time.sleep(0.02)
        return False

    def _poke(self, ctl):
        """Force past reconcile()'s one-second throttle."""
        ctl._last_reconcile = 0.0

    def test_open_close_and_the_cap(self):
        clients = [(100 + i, f"c{i}") for i in range(10)] + [(9999, "ours")]
        ctl, calls, spawned = self._make(clients)
        try:
            ctl.reconcile(True)
            assert self._wait(lambda: len(spawned) >= 8)
            # The server's own client pid is never targeted.
            assert not any("ours" in s for s in spawned)
            for s in spawned:
                assert "-c" in s and "-E" in s and s[-1] == "CAMERA_CMD"
            # Off: the starter here returns nothing to watch, so these count
            # as already closed -- and a popup that is not the camera's own
            # is never closed with -C (test_only_our_own_popups_are_closed).
            calls.clear()
            self._poke(ctl)
            ctl.reconcile(False)
            assert self._wait(lambda: not ctl._opened)
            assert not any("-C" in a for a in calls)
        finally:
            ctl.close()

    def test_dismissed_stays_dismissed_until_off_on(self):
        ctl, calls, spawned = self._make([(1, "a")])
        try:
            ctl.reconcile(True)
            assert self._wait(lambda: len(spawned) == 1)
            # Still on, client still listed -- the popup is not re-spawned.
            self._poke(ctl)
            ctl.reconcile(True)
            time.sleep(0.5)
            assert len(spawned) == 1
            # Off then on again: the set clears, and the popup returns.
            self._poke(ctl)
            ctl.reconcile(False)
            assert self._wait(lambda: not ctl._opened)
            self._poke(ctl)
            ctl.reconcile(True)
            assert self._wait(lambda: len(spawned) == 2)
        finally:
            ctl.close()

    def test_new_clients_are_picked_up(self):
        clients = [(1, "a")]
        ctl, calls, spawned = self._make(clients)
        try:
            ctl.reconcile(True)
            assert self._wait(lambda: len(spawned) == 1)
            clients.append((2, "b"))
            self._poke(ctl)
            ctl.reconcile(True)
            assert self._wait(lambda: len(spawned) == 2)
        finally:
            ctl.close()

    def test_the_cap_is_total_not_per_pass(self):
        # Eight already opened -- dismissed or not -- leave no room for a
        # ninth client that attaches later.
        clients = [(i + 1, f"c{i}") for i in range(8)]
        ctl, calls, spawned = self._make(clients)
        try:
            ctl.reconcile(True)
            assert self._wait(lambda: len(spawned) == 8)
            clients.append((9, "late"))
            self._poke(ctl)
            ctl.reconcile(True)
            time.sleep(0.5)
            assert len(spawned) == 8 and len(ctl._opened) == 8
        finally:
            ctl.close()

    def test_spawned_processes_are_reaped_and_killed_on_close(self):
        procs = []

        class Proc:
            def __init__(self):
                self.done = False
                self.killed = False

            def poll(self):
                return 0 if self.done else None

            def kill(self):
                self.killed = True
                self.done = True

        def starter(argv):
            p = Proc()
            procs.append(p)
            return p

        calls = []

        def runner(argv):
            calls.append(list(argv))
            if argv[-1] == "-V":
                return SimpleNamespace(returncode=0, stdout="tmux 3.7b")
            if "list-clients" in argv:
                return SimpleNamespace(returncode=0, stdout="1\ta\n2\tb\n")
            return SimpleNamespace(returncode=0, stdout="")

        ctl = dc.PopupController(runner, "sock", "=sess",
                                 own_pid_fn=lambda: 9999,
                                 command_fn=lambda: "CAMERA_CMD",
                                 starter=starter)
        try:
            ctl.reconcile(True)
            assert self._wait(lambda: len(procs) == 2)
            # One exits; the next reconcile drops it from the watched set.
            procs[0].done = True
            self._poke(ctl)
            ctl.reconcile(True)
            assert self._wait(lambda: len(ctl._procs) == 1)
        finally:
            ctl.close()
        # close() asked tmux to dismiss only the popup still open -- the one
        # whose spawn never exited -- and then killed that spawn.
        closed = [a for a in calls if "-C" in a]
        assert len(closed) == 1 and closed[0][-1] == "b"
        assert procs[1].killed
        assert not ctl._procs

    def test_only_our_own_popups_are_closed(self):
        # A client that dismissed the camera popup and opened one of its own
        # (fzf-tmux, an editor) keeps it when the camera goes off.
        class Proc:
            def __init__(self):
                self.done = False

            def poll(self):
                return 0 if self.done else None

            def kill(self):
                self.done = True

        procs = []

        def starter(argv):
            procs.append(Proc())
            return procs[-1]

        calls = []

        def runner(argv):
            calls.append(list(argv))
            if argv[-1] == "-V":
                return SimpleNamespace(returncode=0, stdout="tmux 3.7b")
            if "list-clients" in argv:
                return SimpleNamespace(returncode=0, stdout="1\tmine\n2\topen\n")
            return SimpleNamespace(returncode=0, stdout="")

        ctl = dc.PopupController(runner, "sock", "=sess",
                                 own_pid_fn=lambda: 9999,
                                 command_fn=lambda: "CAMERA_CMD",
                                 starter=starter, cwd="/somewhere/safe")
        try:
            ctl.reconcile(True)
            assert self._wait(lambda: len(procs) == 2)
            spawn = [a for a in calls] or []
            procs[0].done = True                  # "mine" pressed q
            self._poke(ctl)
            ctl.reconcile(False)
            assert self._wait(lambda: not ctl._opened, timeout=8)
            closed = [a[-1] for a in calls if "-C" in a]
            assert closed == ["open"]
        finally:
            ctl.close()

    def test_the_popup_runs_from_a_safe_directory(self):
        spawned = []
        ctl = dc.PopupController(
            lambda argv: SimpleNamespace(
                returncode=0,
                stdout="tmux 3.7b" if argv[-1] == "-V" else "1\ta\n"),
            "sock", "=sess", own_pid_fn=lambda: 9999,
            command_fn=lambda: "CAMERA_CMD", starter=spawned.append,
            cwd="/safe/run")
        try:
            ctl.reconcile(True)
            assert self._wait(lambda: len(spawned) == 1)
            argv = spawned[0]
            assert argv[argv.index("-d") + 1] == "/safe/run"
        finally:
            ctl.close()


class TestCameraWire:
    """The protocol path end to end: real server, real socket, real PTY."""

    def harness(self, tmp_path, **kw):
        # config_dir carries the feed socket; pytest's tmp_path is too long
        # for a 104-byte AF_UNIX path on macOS, so it lives under /tmp.
        kw.setdefault("config_dir",
                      Path(tempfile.mkdtemp(dir="/tmp", prefix="dct")))
        return ServerHarness(
            command=["bash", "--noprofile", "--norc", "-i"],
            camera_popups=False,
            cwd=str(tmp_path),
            **kw)

    def client(self, h, **kw):
        c = Client("127.0.0.1", h.port, h.key,
                   features=(FEATURE_CAMERA,), **kw)
        c.connect()
        return c

    def wait_ready(self, c, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            c.poll(0.1)
            if c.camera_ready is not None:
                return c.camera_ready
        return None

    def wait_closed(self, c, timeout=5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                c.poll(0.1)
            except (ConnectionError, OSError):
                return True
        return False

    def test_ready_only_when_negotiated(self, tmp_path):
        with self.harness(tmp_path) as h:
            with self.client(h) as c:
                assert self.wait_ready(c) in (True, False)
            # No feature token: never a READY.
            plain = Client("127.0.0.1", h.port, h.key)
            plain.connect()
            try:
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    plain.poll(0.1)
                assert plain.camera_ready is None
            finally:
                plain.close()

    def test_no_ready_without_camera(self, tmp_path):
        with self.harness(tmp_path, camera_enabled=False) as h:
            with self.client(h) as c:
                assert self.wait_ready(c, 1.0) is None

    def test_unnegotiated_camera_message_drops(self, tmp_path):
        with self.harness(tmp_path) as h:
            plain = Client("127.0.0.1", h.port, h.key)
            plain.connect()
            try:
                plain._send(encode_camera(CAMERA_OUTER))
                assert self.wait_closed(plain)
            finally:
                plain.close()

    def test_preview_while_off_drops(self, tmp_path):
        with self.harness(tmp_path) as h:
            with self.client(h) as c:
                self.wait_ready(c)
                c.send_preview(0, 1, bytes(PREVIEW_BYTES))
                assert self.wait_closed(c)

    def test_preview_acked_and_view_updates(self, tmp_path):
        with self.harness(tmp_path) as h:
            with self.client(h) as c:
                self.wait_ready(c)
                c.send_camera(CAMERA_OUTER)
                pixels = bytes(range(256)) * 24
                c.send_preview(0, 42, pixels)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    c.poll(0.05)
                    if (ACK_PREVIEW, 42) in list(c.camera_acks):
                        break
                assert (ACK_PREVIEW, 42) in list(c.camera_acks)
                deadline = time.monotonic() + 5
                while (time.monotonic() < deadline
                       and not h.server.camera_view.rgb):
                    time.sleep(0.02)
                view = h.server.camera_view
                assert view.on and view.camera == CAMERA_OUTER
                assert view.rgb == dc.expand_preview(pixels)

    def _upload(self, c, packed, fmt, camera, crc=None):
        """Raw upload with no flow control -- for the failure cases."""
        crc = zlib.crc32(packed) & 0xFFFFFFFF if crc is None else crc
        for off in range(0, len(packed), 1024):
            c._send(encode_photo_chunk(off, packed[off:off + 1024]))
        c._send(encode_photo_end(fmt, camera, crc))

    def wait_saved(self, c, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            c.poll(0.1)
            if c.photo_saved:
                return c.photo_saved.popleft()
        return None

    def test_photo_full_and_small(self, tmp_path):
        with self.harness(tmp_path) as h:
            with self.client(h) as c:
                assert self.wait_ready(c)
                c.send_camera(CAMERA_OUTER)
                yuyv = dc.synth_yuyv_frame()
                for fmt, (w, h2) in ((PHOTO_FORMAT_FULL, (640, 480)),
                                     (PHOTO_FORMAT_SMALL, (320, 240))):
                    packed = dc.photo_pack(yuyv, fmt)
                    status, pid, path = c.send_photo(packed, CAMERA_OUTER, fmt)
                    assert status == PHOTO_SAVED_OK and pid > 0
                    assert path.startswith(".dsterm/photos/ds-photo-")
                    saved = tmp_path / path
                    assert saved.exists()
                    dw, dh, pixels = _png_decode(saved.read_bytes())
                    assert (dw, dh) == (w, h2)
                    assert pixels == dc.planar420_to_rgb(packed, w, h2)
                    for cl in h.server._clients:   # the rate limit is per connection
                        cl.camera.last_photo_at = 0.0

    def test_a_picture_whose_camera_closed_mid_save_is_not_kept(self, tmp_path):
        # The person left the camera while the computer was still saving:
        # nothing will ever reference the file, so it is deleted, and no
        # PHOTO_SAVED names it.
        with self.harness(tmp_path) as h:
            with self.client(h) as c:
                assert self.wait_ready(c)
                c.send_camera(CAMERA_OUTER)
                packed = dc.photo_pack(dc.synth_yuyv_frame(), PHOTO_FORMAT_FULL)
                self._upload(c, packed, PHOTO_FORMAT_FULL, CAMERA_OUTER)
                c.send_camera(0)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    c.poll(0.1)
                    if any("camera closed before" in e for _, e in h.server.events):
                        break
                assert any("camera closed before" in e for _, e in h.server.events)
                assert not c.photo_saved
                photos = tmp_path / ".dsterm" / "photos"
                assert not list(photos.glob("ds-photo-*.png"))

    def test_bad_crc_is_integrity(self, tmp_path):
        with self.harness(tmp_path) as h:
            with self.client(h) as c:
                self.wait_ready(c)
                c.send_camera(CAMERA_OUTER)
                packed = dc.photo_pack(dc.synth_yuyv_frame(), PHOTO_FORMAT_SMALL)
                self._upload(c, packed, PHOTO_FORMAT_SMALL,
                             CAMERA_OUTER, crc=0)
                status, _, _ = self.wait_saved(c)
                assert status == PHOTO_SAVED_INTEGRITY

    def test_too_soon(self, tmp_path):
        with self.harness(tmp_path) as h:
            with self.client(h) as c:
                self.wait_ready(c)
                c.send_camera(CAMERA_OUTER)
                packed = dc.photo_pack(dc.synth_yuyv_frame(), PHOTO_FORMAT_SMALL)
                status, _, _ = c.send_photo(packed, CAMERA_OUTER, PHOTO_FORMAT_SMALL)
                assert status == PHOTO_SAVED_OK
                status, _, _ = c.send_photo(packed, CAMERA_OUTER, PHOTO_FORMAT_SMALL)
                assert status == PHOTO_SAVED_TOO_SOON

    def test_non_contiguous_drops_and_zero_restarts(self, tmp_path):
        with self.harness(tmp_path) as h:
            with self.client(h) as c:
                self.wait_ready(c)
                c.send_camera(CAMERA_OUTER)
                packed = dc.photo_pack(dc.synth_yuyv_frame(), PHOTO_FORMAT_SMALL)
                c._send(encode_photo_chunk(0, packed[:1024]))
                c._send(encode_photo_chunk(0, packed[:1024]))  # restart: legal
                c._send(encode_photo_chunk(1024, packed[1024:2048]))
                c._send(encode_photo_chunk(4096, packed[4096:5120]))  # gap: drop
                assert self.wait_closed(c)
                assert not list((tmp_path / ".dsterm" / "photos").glob("*.png")) \
                    if (tmp_path / ".dsterm" / "photos").exists() else True

    def test_discard_and_foreign_id(self, tmp_path):
        with self.harness(tmp_path) as h:
            with self.client(h) as c:
                self.wait_ready(c)
                c.send_camera(CAMERA_OUTER)
                packed = dc.photo_pack(dc.synth_yuyv_frame(), PHOTO_FORMAT_SMALL)
                status, pid, path = c.send_photo(packed, CAMERA_OUTER,
                                                 PHOTO_FORMAT_SMALL)
                assert status == PHOTO_SAVED_OK
                saved = tmp_path / path
                assert saved.exists()
                c.send_discard(pid + 5000 if pid + 5000 < 0xFFFF else 1)
                time.sleep(0.3)
                c.poll(0.1)
                assert saved.exists()     # a foreign id deletes nothing
                c.send_discard(pid)
                deadline = time.monotonic() + 3
                while saved.exists() and time.monotonic() < deadline:
                    c.poll(0.1)
                    time.sleep(0.05)
                assert not saved.exists()

    def test_mid_upload_drop_leaves_nothing(self, tmp_path):
        with self.harness(tmp_path) as h:
            c = self.client(h)
            self.wait_ready(c)
            c.send_camera(CAMERA_OUTER)
            packed = dc.photo_pack(dc.synth_yuyv_frame(), PHOTO_FORMAT_FULL)
            for off in range(0, 10240, 1024):
                c._send(encode_photo_chunk(off, packed[off:off + 1024]))
            c.close()
            time.sleep(0.5)
            photos = tmp_path / ".dsterm" / "photos"
            assert not photos.exists() or not list(photos.glob("*.png"))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and h.server.camera_view.on:
                time.sleep(0.02)
            assert not h.server.camera_view.on

    def test_non_ascii_dir_means_no_save(self, tmp_path):
        bad = tmp_path / "phøtos"
        with self.harness(tmp_path, photo_dir=str(bad)) as h:
            with self.client(h) as c:
                assert self.wait_ready(c) is False
                c.send_camera(CAMERA_OUTER)
                packed = dc.photo_pack(dc.synth_yuyv_frame(), PHOTO_FORMAT_SMALL)
                self._upload(c, packed, PHOTO_FORMAT_SMALL, CAMERA_OUTER)
                status, _, _ = self.wait_saved(c)
                assert status == PHOTO_SAVED_BAD_NAME

    def test_connecting_creates_nothing(self, tmp_path):
        # READY's can_save is a string check: no tmux call, and no
        # .dsterm/photos appears in the project until a picture is saved.
        with self.harness(tmp_path) as h:
            with self.client(h) as c:
                assert self.wait_ready(c) is True
                time.sleep(0.3)
                c.poll(0.1)
                assert not (tmp_path / ".dsterm").exists()

    def test_a_failed_save_names_no_path_in_the_event(self, tmp_path):
        # A photo_dir that is a file makes ensure() raise on the worker; the
        # event feed -- which the dashboard shows -- hears only the kind.
        bad = tmp_path / "blocked"
        bad.write_text("not a directory")
        with self.harness(tmp_path, photo_dir=str(bad)) as h:
            with self.client(h) as c:
                self.wait_ready(c)
                c.send_camera(CAMERA_OUTER)
                packed = dc.photo_pack(dc.synth_yuyv_frame(),
                                       PHOTO_FORMAT_SMALL)
                self._upload(c, packed, PHOTO_FORMAT_SMALL, CAMERA_OUTER)
                status, _, _ = self.wait_saved(c)
                assert status == PHOTO_SAVED_DISK
                assert h.wait_for_event("picture not saved: disk")
                assert not any(str(bad) in e for e in h.events)

    def test_feed_socket(self, tmp_path):
        config_dir = Path(tempfile.mkdtemp(dir="/tmp", prefix="dct"))
        with self.harness(tmp_path, config_dir=config_dir) as h:
            sock_path = config_dir / "run" / "camera.sock"
            deadline = time.monotonic() + 5
            while not sock_path.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert sock_path.exists()
            assert sock_path.stat().st_mode & 0o777 == 0o600
            assert (config_dir / "run").stat().st_mode & 0o777 == 0o700

            def viewer():
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(5)
                s.connect(str(sock_path))
                return s

            v = viewer()
            reader = dc.FeedReader()
            # The connect-time state is off.
            got = reader.feed(v.recv(65536))
            assert got and got[0][0]["on"] is False

            with self.client(h) as c:
                self.wait_ready(c)
                c.send_camera(CAMERA_OUTER)
                pixels = bytes(range(256)) * 24
                c.send_preview(0, 1, pixels)
                deadline = time.monotonic() + 5
                seen = None
                while time.monotonic() < deadline:
                    try:
                        data = v.recv(65536)
                    except socket.timeout:
                        break
                    for header, rgb in reader.feed(data):
                        if header["on"] and rgb:
                            seen = (header, rgb)
                            break
                    if seen:
                        break
                assert seen is not None
                header, rgb = seen
                assert (header["w"], header["h"]) == (128, 96)
                assert rgb == dc.expand_preview(pixels)

                # Eight viewers max; the ninth is refused at once.
                vs = [viewer() for _ in range(7)]
                ninth = viewer()
                ninth.settimeout(2)
                try:
                    assert ninth.recv(64) == b""
                finally:
                    ninth.close()
                    for s in vs:
                        s.close()

                # Off goes out to the viewer with no pixels.
                c.send_camera(CAMERA_OFF)
                deadline = time.monotonic() + 5
                seen_off = False
                while time.monotonic() < deadline and not seen_off:
                    try:
                        data = v.recv(65536)
                    except socket.timeout:
                        break
                    for header, rgb in reader.feed(data):
                        if not header["on"]:
                            seen_off = True
                assert seen_off
            v.close()


class TestDashboardCamera:
    def test_state_has_status_never_pixels(self, tmp_path):
        from test_dashboard import Harness
        with Harness() as h:
            # Harness gives no config_dir, so force one + camera state.
            from dsterm.server_camera import CameraView
            h.server.camera_view = CameraView(on=True, camera=CAMERA_OUTER)
            state = h.state()
            assert state["camera"] == {"on": True, "camera": "outer"}
            assert "rgb" not in state["camera"] and "rgb" not in state
            # And no POST-able camera route exists.
            import urllib.request as u
            try:
                u.urlopen(u.Request(h.base + "/api/camera", data=b"x"),
                          timeout=5)
                assert False
            except Exception as exc:
                assert "404" in str(exc) or "403" in str(exc)
