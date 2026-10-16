"""The card's own protocol code against this server, over a real socket.

dsclient/tests/wire_client.c is main.c's connection path -- HELLO, NONCE,
AUTH, SEAL, the frame reader, every decoder, the REMOTE proof rule, PING --
built from the card's sources for this machine. These tests run it against a
live server. They exist because a card that decoded every payload correctly
in its unit tests still refused three message types at the frame reader,
which no test that bypassed the reader could see.

Skipped where there is no C compiler.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from test_server import ServerHarness

REPO = Path(__file__).resolve().parent.parent
CLIENT = REPO / "dsclient"
CC = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")

pytestmark = pytest.mark.skipif(CC is None, reason="no C compiler")


def build(tmp_path: Path, *defines: str) -> Path:
    sources = [line.strip() for line in (CLIENT / "tests" / "sources.txt").read_text().splitlines()
               if line.strip() and not line.startswith("#")]
    exe = tmp_path / "wire_client"
    cmd = [CC, "-std=c11", "-O1", "-Wall", "-Wextra", "-D_DEFAULT_SOURCE",
           "-D_DARWIN_C_SOURCE", *defines, "-o", str(exe),
           str(CLIENT / "tests" / "wire_client.c"), str(CLIENT / "source" / "ext_none.c"),
           *[str(CLIENT / s) for s in sources]]
    subprocess.run(cmd, check=True, capture_output=True)
    return exe


def run(exe: Path, port: int, key: bytes, seconds: float, text: str | None = None):
    args = [str(exe), str(port), key.hex(), str(seconds)]
    if text:
        args.append(text)
    r = subprocess.run(args, capture_output=True, text=True, timeout=seconds + 30)
    return r.returncode, r.stdout


def test_the_card_s_camera_saves_and_discards_a_picture(tmp_path):
    """The card's real upload code end to end: CAMERA_READY, one preview and
    its ACK, a SMALL picture through the 8 KB window, the server-chosen path
    back, then the discard."""
    exe = build(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    # The feed socket needs a config_dir short enough for AF_UNIX.
    config_dir = Path(tempfile.mkdtemp(dir="/tmp", prefix="dct"))
    with ServerHarness(command=["cat"], cwd=str(project),
                       config_dir=config_dir, camera_popups=False) as h:
        proc = subprocess.Popen(
            [str(exe), str(h.port), h.key.hex(), "20", "camera"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        photos = project / ".dsterm" / "photos"
        png: Path | None = None
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            hits = sorted(photos.glob("ds-photo-*.png")) if photos.is_dir() else []
            if hits:
                png = hits[0]
                break
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert png is not None, "the photo was never written"
        # A real PNG, SMALL-sized (320x240 in the IHDR).
        head = png.read_bytes()
        assert head[:8] == b"\x89PNG\r\n\x1a\n"
        assert head[16:24] == (320).to_bytes(4, "big") + (240).to_bytes(4, "big")
        assert (png.stat().st_mode & 0o777) == 0o600
        out, _ = proc.communicate(timeout=30)
        assert proc.returncode == 0, out
        m = re.search(r"^PHOTO (\d+) (\S+)$", out, re.M)
        assert m, out
        # The path the card was told is the project-relative one.
        assert m.group(2).startswith(".dsterm/photos/"), m.group(2)
        assert project.joinpath(m.group(2)).resolve() == png.resolve()
        # The discard the card then sent removed it.
        assert not png.exists()
        # The connection lived through the whole run: no FAIL, no drop.
        assert "OK" in out
        events = "\n".join(h.events)
    assert "picture saved" in events
    assert "picture discarded" in events


def test_the_card_logs_in_signs_reads_everything_and_types(tmp_path):
    exe = build(tmp_path)
    with ServerHarness(command=["cat"]) as h:
        code, out = run(exe, h.port, h.key, 15, "wire-test-7d2")
        events = "\n".join(h.events)
    out = f"{out}\n--- server ---\n{events}"
    assert code == 0, out
    assert "SEALED" in out
    # What the card asks for, and must be able to read: the screen, the
    # scrolling regime, REMOTE (the proof frame) and STATE, and the PONG.
    for msg in ("0x82", "0x91", "0x93", "0x96", "0x9F", "0xA1"):
        assert f"SAW {msg}" in out or (msg == "0x91" and "SAW 0x90" in out), out


def test_a_card_built_for_the_other_grid_is_refused_by_name(tmp_path):
    exe = build(tmp_path, "-DPROTO_COLS=64", "-DPROTO_ROWS=32")
    with ServerHarness(command=["cat"]) as h:
        code, out = run(exe, h.port, h.key, 5)
    assert code == 1
    assert "grid: this server is 42x24, this card is 64x32" in out, out
