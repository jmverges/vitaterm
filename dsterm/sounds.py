"""Your own sounds for the handheld: the cue names, and checking a WAV the way
the card will.

The card plays /dsterm/sounds/<cue>.wav in place of its built-in sound for that
cue, and <cue>-2.wav, <cue>-3.wav as variations it rotates through
(dsclient/source/wav.c reads them; docs/CUSTOMIZING.md is the guide). This
module is the computer's half: `dsterm sounds` lists the cues, and `dsterm card
--sounds DIR` checks a folder of WAVs against exactly the rules the card
applies and copies the good ones -- so a file the card would skip is refused
here, with the reason, rather than discovered silently on the console.

The cue names are the sound banks in tools/sfx_build.py, in the same order;
tests/test_sounds.py fails if the two lists ever disagree.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

# (file name, when the card plays it)
CUES: list[tuple[str, str]] = [
    ("sent", "you sent a prompt"),
    ("think", "Claude started working"),
    ("output", "the answer started arriving (silent unless you add this file)"),
    ("done", "Claude finished: your turn"),
    ("link", "connected to the computer"),
    ("drop", "the connection dropped"),
    ("lantern", "another session has new output, or the one reminder"),
    ("cursor", "menu ticks (silent in the standard sound profile)"),
    ("switch", "switched session or computer"),
    ("key", "a key typed on the touch keyboard"),
    ("delete", "backspace"),
    ("deny", "a press that could not do anything"),
    ("blip", "soft typing while Claude writes (the WRITING setting)"),
    ("boot", "the card started"),
    ("approve", "Claude needs your OK (a permission prompt)"),
    ("question", "Claude asked you something"),
]
CUE_NAMES = [name for name, _ in CUES]

# The card's numbers (dsclient/source/wav.h, and main.c's
# CUSTOM_SOUND_BUDGET / CUSTOM_SOUND_FILE_MAX).
MAX_SECONDS = 3
MIN_HZ, MAX_HZ = 4000, 48000
MAX_FILE = 600 * 1024
MAX_TOTAL = 1024 * 1024          # the card's budget for all of them, decoded
TAKES = 3                        # <cue>.wav, <cue>-2.wav, <cue>-3.wav

_NAME = re.compile(r"^([a-z]+)(?:-([23]))?\.wav$")


def slot_of(filename: str) -> tuple[str, int] | None:
    """(cue, take 1-3) a file name fills, or None if the card would ignore it.

    Case-blind, because the card's FAT lookup is: it opens "done.wav" and
    finds DONE.WAV just the same.
    """
    m = _NAME.match(filename.lower())
    if not m or m.group(1) not in CUE_NAMES:
        return None
    return m.group(1), int(m.group(2) or 1)


def cue_of(filename: str) -> str | None:
    """The cue a file name plays, or None if the card would ignore it."""
    slot = slot_of(filename)
    return slot[0] if slot else None


def card_name(cue: str, take: int) -> str:
    """The file name the card opens for ``take`` of ``cue``."""
    return f"{cue}.wav" if take == 1 else f"{cue}-{take}.wav"


def measure(data: bytes) -> tuple[str | None, int]:
    """(reason the card refuses ``data`` or None, 16-bit samples it decodes to).

    A port of dsclient/source/wav.c -- parse() and wav_measure() -- rule for
    rule and in the same order, so that what this says the card will do is
    what the card does. Python's ``wave`` module was used before and disagreed
    with the card both ways: it refused WAVE_FORMAT_EXTENSIBLE, which the card
    plays and modern editors write, and it rounded a 12-bit-in-16 file up to
    two bytes a sample and passed it, which the card refuses on the
    bits-per-sample field.

    The walk: RIFF/WAVE header, then chunks of id (4), size (u32 LE), body,
    padded to an even length. Every size is checked against what is left
    before it is used, so a declared size larger than the file is refused
    ("truncated") whichever chunk it is on, rather than read past. The last
    "fmt " and the last "data" chunk win, as they do on the card. The RIFF
    size field itself is not checked; the card does not either.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return "not a WAV file", 0
    fmt: tuple[int, int, int, int] | None = None
    data_len: int | None = None
    at = 12
    while at + 8 <= len(data):
        size = struct.unpack_from("<I", data, at + 4)[0]
        if size > len(data) - (at + 8):
            return "truncated: a chunk claims more bytes than the file has", 0
        ident = data[at:at + 4]
        if ident == b"fmt ":
            if size < 16:
                return "bad format chunk (shorter than 16 bytes)", 0
            # format, channels, rate, then bits-per-sample at offset 14 --
            # the field, not the block alignment: 12-bit audio stored in
            # 16-bit words says 12 here, and the card refuses it.
            fmt_tag, channels, rate = struct.unpack_from("<HHI", data, at + 8)
            bits = struct.unpack_from("<H", data, at + 8 + 14)[0]
            fmt = (fmt_tag, channels, rate, bits)
        elif ident == b"data":
            data_len = size
        at += 8 + size + (size & 1)
    if fmt is None:
        return "no format chunk", 0
    if data_len is None:
        return "no sound data (no data chunk)", 0
    fmt_tag, channels, rate, bits = fmt
    # 0xFFFE is WAVE_FORMAT_EXTENSIBLE; the card does not look at its
    # sub-format, and the bit depth below is the test that matters.
    if fmt_tag not in (1, 0xFFFE):
        return f"compressed or float WAV (format 0x{fmt_tag:04x}); save it as PCM", 0
    if bits not in (8, 16):
        return f"{bits}-bit; the card plays 8- or 16-bit", 0
    if channels not in (1, 2):
        return f"{channels} channels; mono or stereo only", 0
    if not MIN_HZ <= rate <= MAX_HZ:
        return f"{rate} Hz; 4000 to 48000 only", 0
    samples = data_len // (channels * (bits // 8))
    if samples == 0:
        return "empty", 0
    if samples > rate * MAX_SECONDS:
        return f"{samples / rate:.1f} s long; 3 seconds at most", 0
    return None, samples + (samples & 1)


def check(path: Path) -> tuple[str | None, int]:
    """(reason it would be refused or None, bytes it costs the card decoded)."""
    try:
        size = path.stat().st_size
        if size > MAX_FILE:
            return "bigger than 600 KB", 0
        if size == 0:
            return "empty file", 0
        data = path.read_bytes()
    except OSError as exc:
        return f"unreadable ({exc.strerror or exc})", 0
    reason, samples = measure(data)
    return reason, samples * 2


def plan(folder: Path, remaining: list[Path] | None = None
         ) -> tuple[list[Path], list[str]]:
    """The files worth copying, and a line for each one that is not.

    ``remaining`` is what is already in the card's /dsterm/sounds and will
    still be there afterwards. The card loads custom sounds in one order --
    cue by cue as CUE_NAMES lists them, takes 1, 2, 3 within each -- and
    takes each file only if it still fits in its 1 MB of decoded 16-bit mono,
    skipping one that does not and trying the next. So the budget is spent in
    that order across BOTH sets: a new file can be crowded out by an old one
    that loads before it, and an old one by a new one. A new file that would
    not fit is not copied; an old one the card would skip is named, so the
    person knows it is doing nothing.

    A new file replaces the card's file of the same name. One that is refused
    does not, and the card's stays and counts.
    """
    problems: list[str] = []
    new: dict[tuple[str, int], tuple[Path, int]] = {}
    for path in sorted(folder.iterdir()):
        if path.is_dir() or path.name.startswith("."):
            continue
        if path.suffix.lower() != ".wav":
            continue
        slot = slot_of(path.name)
        if slot is None:
            problems.append(f"{path.name}: not a cue name (see `dsterm sounds`)")
            continue
        if slot in new:
            # Done.wav and done.wav are two files here and one on the card.
            problems.append(f"{path.name}: the same file on the card as "
                            f"{new[slot][0].name}")
            continue
        reason, cost = check(path)
        if reason:
            problems.append(f"{path.name}: {reason}")
            continue
        new[slot] = (path, cost)

    old: dict[tuple[str, int], tuple[Path, str | None, int]] = {}
    for path in remaining or ():
        slot = slot_of(path.name)
        if slot is None or slot in new or slot in old:
            continue
        reason, cost = check(path)
        old[slot] = (path, reason, cost)

    good: list[Path] = []
    total = 0
    for cue in CUE_NAMES:
        for take in range(1, TAKES + 1):
            slot = (cue, take)
            if slot in new:
                path, cost = new[slot]
                if total + cost > MAX_TOTAL:
                    problems.append(f"{path.name}: over the card's 1 MB budget "
                                    "for sounds (the card loads them in "
                                    "`dsterm sounds` order)")
                    continue
                total += cost
                good.append(path)
            elif slot in old:
                path, reason, cost = old[slot]
                if reason:
                    problems.append(f"{path.name} (already on the card): the card "
                                    f"skips it: {reason}")
                elif total + cost > MAX_TOTAL:
                    problems.append(f"{path.name} (already on the card): the card "
                                    "skips it: over the 1 MB budget")
                else:
                    total += cost
    return good, problems


def plan_card(folder: Path, on_card: list[Path]
              ) -> tuple[list[Path], list[Path], list[str]]:
    """What `dsterm card --sounds` copies, what it moves aside, and why not.

    (copy, move to /dsterm/sounds/old/, problems). A cue the new folder
    supplies is REPLACED, not merged: the card rotates through every take it
    finds, so an old done-2.wav left beside a new done.wav kept playing every
    other time. A cue counts as supplied when the folder has a file for it
    the card can play; every file the card holds for that cue is then either
    overwritten by a copy of the same name or moved aside -- including one
    whose new namesake did not fit the budget and so is not copied, which
    would otherwise stay and be loaded in its place. Never deleted: the card
    command deletes nothing.

    Cues the folder does not touch keep their files, and those count against
    the budget in card order (plan's ``remaining``).
    """
    supplied: set[str] = set()
    for path in folder.iterdir():
        slot = slot_of(path.name)
        if (slot is not None and not path.is_dir() and not path.name.startswith(".")
                and check(path)[0] is None):
            supplied.add(slot[0])
    ours = [p for p in on_card if slot_of(p.name) is not None]
    remaining = [p for p in ours if slot_of(p.name)[0] not in supplied]
    good, problems = plan(folder, remaining)
    copied = {p.name.lower() for p in good}
    aside = [p for p in ours
             if slot_of(p.name)[0] in supplied and p.name.lower() not in copied]
    return good, aside, problems


def usage() -> str:
    lines = [
        "Your own sounds for the handheld.",
        "",
        "Name a WAV after a cue and put it in /dsterm/sounds/ on the SD card",
        "(or let `dsterm card PATH --sounds FOLDER` check and copy it). It",
        "replaces the built-in sound for that cue at the next boot. Add",
        "<cue>-2.wav and <cue>-3.wav for variations the card rotates through.",
        "They play with SOUND set to SOFT on the DS (Settings, CUSTOMIZE);",
        "CHIP, the default, is the square-wave voice and has no samples.",
        "",
        "PCM WAV, 8- or 16-bit, mono or stereo, 4-48 kHz, 3 seconds at most.",
        "",
    ]
    width = max(len(n) for n in CUE_NAMES)
    for name, when in CUES:
        lines.append(f"  {name + '.wav':<{width + 4}}  {when}")
    lines += ["", "Guide: docs/CUSTOMIZING.md"]
    return "\n".join(lines)
