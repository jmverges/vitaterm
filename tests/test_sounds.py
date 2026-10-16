"""Your own sounds: the computer checks a WAV by the card's rules, and its cue
names are the card's."""

import ast
import struct
import wave
from pathlib import Path

import pytest

from dsterm import sounds

REPO = Path(__file__).resolve().parent.parent


def write_wav(path: Path, *, rate=22050, width=2, channels=1, frames=100):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x00" * frames * width * channels)


def test_cue_names_are_the_cards():
    """tools/sfx_build.py's BANKS generate the card's file names; the two
    lists must be the same list, in the same order."""
    tree = ast.parse((REPO / "tools" / "sfx_build.py").read_text())
    banks = next(ast.literal_eval(node.value) for node in ast.walk(tree)
                 if isinstance(node, ast.Assign)
                 and getattr(node.targets[0], "id", "") == "BANKS")
    assert banks == sounds.CUE_NAMES


def test_names_and_variations():
    assert sounds.cue_of("done.wav") == "done"
    assert sounds.cue_of("DONE-2.WAV") == "done"
    assert sounds.cue_of("done-3.wav") == "done"
    assert sounds.cue_of("done-4.wav") is None
    assert sounds.cue_of("ding.wav") is None
    assert sounds.cue_of("done.mp3") is None


def test_check_matches_the_card(tmp_path):
    ok = tmp_path / "done.wav"
    write_wav(ok)
    assert sounds.check(ok)[0] is None
    for kwargs, why in [
        ({"rate": 96000}, "Hz"),
        ({"width": 3}, "bit"),
        ({"frames": 22050 * 4}, "3 seconds"),
        ({"frames": 0}, "empty"),
    ]:
        bad = tmp_path / "bad.wav"
        write_wav(bad, **kwargs)
        reason, _ = sounds.check(bad)
        assert reason and why in reason, (kwargs, reason)
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"RIFF....not a wav")
    assert "not a WAV" in sounds.check(junk)[0]


def test_plan_keeps_the_good_and_says_why_not(tmp_path):
    write_wav(tmp_path / "done.wav")
    write_wav(tmp_path / "done-2.wav", channels=2, width=1)
    write_wav(tmp_path / "ding.wav")
    write_wav(tmp_path / "boot.wav", rate=2000)
    (tmp_path / "notes.txt").write_text("not a sound")
    good, problems = sounds.plan(tmp_path)
    # In the order the card loads them: cue order, then take 1, 2, 3.
    assert [p.name for p in good] == ["done.wav", "done-2.wav"]
    assert any("ding.wav" in p and "cue name" in p for p in problems)
    assert any("boot.wav" in p and "Hz" in p for p in problems)
    assert not any("notes.txt" in p for p in problems)


def test_usage_lists_every_cue():
    text = sounds.usage()
    for name in sounds.CUE_NAMES:
        assert f"{name}.wav" in text


# -- the card's rules, byte by byte (dsclient/source/wav.c) -----------------------
#
# Built by hand rather than with `wave`, because the cases that matter are the
# ones `wave` cannot write or reads differently from the card: EXTENSIBLE, a
# bits field that is not the sample width, sizes that lie.


def chunk(ident: bytes, body: bytes, size: int | None = None) -> bytes:
    pad = b"\x00" if len(body) & 1 else b""
    return ident + struct.pack("<I", len(body) if size is None else size) + body + pad


def fmt(tag=1, channels=1, rate=22050, bits=16, extra=b"", block=None) -> bytes:
    block = block if block is not None else channels * ((bits + 7) // 8)
    return chunk(b"fmt ", struct.pack("<HHIIHH", tag, channels, rate, rate * block,
                                      block, bits) + extra)


def riff(*chunks: bytes) -> bytes:
    body = b"WAVE" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def pcm(samples: int, width: int = 2, channels: int = 1) -> bytes:
    return chunk(b"data", b"\x00" * samples * width * channels)


EXTENSIBLE_TAIL = (struct.pack("<HHI", 22, 16, 0x4)      # cbSize, valid bits, mask
                   + bytes.fromhex("0100000000001000800000aa00389b71"))   # PCM GUID


@pytest.mark.parametrize("data,cost", [
    (riff(fmt(), pcm(100)), 200),
    (riff(fmt(), pcm(101)), 204),                         # padded to whole words
    (riff(fmt(tag=0xFFFE, extra=EXTENSIBLE_TAIL), pcm(100)), 200),
    (riff(fmt(bits=8), pcm(100, width=1)), 200),
    (riff(fmt(channels=2), pcm(100, channels=2)), 200),   # mixed down to mono
    (riff(fmt(rate=4000), pcm(10)), 20),
    (riff(fmt(rate=48000), pcm(48000 * 3)), 48000 * 3 * 2),   # exactly 3 s
    (riff(chunk(b"LIST", b"odd"), fmt(), pcm(10)), 20),   # odd chunk, padded
    (riff(fmt(), pcm(10)) + b"\x00" * 7, 20),             # a tail too short to be a chunk
    (riff(fmt(), chunk(b"data", b"\x00" * 7)), 8),         # 3 whole frames, padded to 4
])
def test_what_the_card_plays(data, cost):
    assert sounds.measure(data) == (None, cost // 2)


@pytest.mark.parametrize("data,why", [
    (b"RIFX" + riff(fmt(), pcm(10))[4:], "not a WAV"),
    (riff(fmt(), pcm(10))[:11], "not a WAV"),
    # A declared size larger than the file, on the data chunk or any other.
    (riff(fmt(), chunk(b"data", b"\x00" * 10, size=4000)), "truncated"),
    (riff(chunk(b"LIST", b"ab", size=99), fmt(), pcm(10)), "truncated"),
    (riff(chunk(b"fmt ", b"\x01\x00" * 7), pcm(10)), "format chunk"),   # 14 bytes
    (riff(pcm(10)), "no format chunk"),
    (riff(fmt()), "no sound data"),
    (riff(fmt(tag=3, bits=32), pcm(10, width=4)), "save it as PCM"),       # float
    (riff(fmt(tag=2, bits=4), pcm(10)), "save it as PCM"),                 # ADPCM
    # 12-bit audio in 16-bit words: `wave` called it 16-bit; the card reads
    # the bits field and refuses it.
    (riff(fmt(bits=12, block=2), pcm(10)), "12-bit"),
    (riff(fmt(bits=24), pcm(10, width=3)), "24-bit"),
    (riff(fmt(channels=3), pcm(10, channels=3)), "3 channels"),
    (riff(fmt(channels=0, block=2), pcm(10)), "0 channels"),
    (riff(fmt(rate=3999), pcm(10)), "3999 Hz"),
    (riff(fmt(rate=48001), pcm(10)), "48001 Hz"),
    (riff(fmt(), chunk(b"data", b"")), "empty"),
    (riff(fmt(rate=4000), pcm(4000 * 3 + 1)), "3 seconds"),
])
def test_what_the_card_refuses(data, why):
    reason, cost = sounds.measure(data)
    assert reason and why in reason, reason
    assert cost == 0


def test_the_file_size_limits(tmp_path):
    big = tmp_path / "done.wav"
    big.write_bytes(riff(fmt(rate=48000, channels=2), pcm(48000 * 3, channels=2))
                    + chunk(b"junk", b"\x00" * 40000))
    assert big.stat().st_size > sounds.MAX_FILE
    assert "600 KB" in sounds.check(big)[0]
    empty = tmp_path / "boot.wav"
    empty.write_bytes(b"")
    assert "empty" in sounds.check(empty)[0]


def _three_seconds(path: Path) -> Path:
    """288,000 bytes of the card's 1 MB once decoded: three of them fit, four
    do not."""
    path.write_bytes(riff(fmt(rate=48000), pcm(48000 * 3)))
    return path


def test_the_budget_is_spent_in_card_order_across_old_and_new(tmp_path):
    card = tmp_path / "card"
    card.mkdir()
    new = tmp_path / "new"
    new.mkdir()
    # Already on the card, and staying: sent, think, output load first.
    remaining = [_three_seconds(card / f"{cue}.wav") for cue in ("sent", "think", "output")]
    _three_seconds(new / "done.wav")              # loads after them: no room
    write_wav(new / "boot.wav", frames=2000)       # small: fits
    good, problems = sounds.plan(new, remaining)
    assert [p.name for p in good] == ["boot.wav"]
    assert any(p.startswith("done.wav:") and "1 MB" in p for p in problems)
    # Without them, the same folder fits.
    assert [p.name for p in sounds.plan(new)[0]] == ["done.wav", "boot.wav"]


def test_an_old_file_the_new_ones_crowd_out_is_named(tmp_path):
    card = tmp_path / "card"
    card.mkdir()
    new = tmp_path / "new"
    new.mkdir()
    remaining = [_three_seconds(card / "think.wav"), _three_seconds(card / "boot.wav")]
    _three_seconds(new / "sent.wav")
    _three_seconds(new / "sent-2.wav")
    good, problems = sounds.plan(new, remaining)
    # sent, sent-2, think: 864,000 bytes. boot loads last and does not fit.
    assert [p.name for p in good] == ["sent.wav", "sent-2.wav"]
    assert any(p.startswith("boot.wav (already on the card)") and "skips it" in p
               for p in problems)
    # A bad file already on the card is named too, and costs nothing.
    (card / "done.wav").write_bytes(riff(fmt(bits=24), pcm(10, width=3)))
    _, problems = sounds.plan(new, remaining + [card / "done.wav"])
    assert any(p.startswith("done.wav (already on the card)") and "24-bit" in p
               for p in problems)


def test_a_new_file_replaces_its_namesake_and_a_refused_one_does_not(tmp_path):
    card = tmp_path / "card"
    card.mkdir()
    new = tmp_path / "new"
    new.mkdir()
    remaining = [_three_seconds(card / f"{cue}.wav") for cue in ("sent", "think", "output")]
    write_wav(new / "sent.wav", frames=100)        # replaces the big one
    _three_seconds(new / "done.wav")               # so this fits now
    good, _ = sounds.plan(new, remaining)
    assert [p.name for p in good] == ["sent.wav", "done.wav"]
    write_wav(new / "sent.wav", rate=96000)         # refused: the big one stays
    good, problems = sounds.plan(new, remaining)
    assert [p.name for p in good] == []
    assert any(p.startswith("sent.wav:") and "Hz" in p for p in problems)


def test_a_replaced_cue_sets_its_old_takes_aside(tmp_path):
    card = tmp_path / "card"
    card.mkdir()
    new = tmp_path / "new"
    new.mkdir()
    for name in ("done.wav", "DONE-2.WAV", "sent.wav"):
        write_wav(card / name)
    write_wav(new / "done.wav")
    copy, aside, _ = sounds.plan_card(new, sorted(card.iterdir()))
    assert [p.name for p in copy] == ["done.wav"]
    assert [p.name for p in aside] == ["DONE-2.WAV"]     # sent is not touched
    # A cue whose only new file the card would refuse is not being replaced.
    (new / "done.wav").unlink()
    write_wav(new / "done-3.wav", width=3)
    copy, aside, problems = sounds.plan_card(new, sorted(card.iterdir()))
    assert copy == [] and aside == []
    # One that is valid but over the budget IS replaced, and its namesake on
    # the card goes aside too rather than staying to be loaded in its place.
    (new / "done-3.wav").unlink()
    for cue in ("sent", "think", "output"):
        _three_seconds(card / f"{cue}.wav")
    _three_seconds(new / "done.wav")
    write_wav(new / "boot.wav")
    copy, aside, _ = sounds.plan_card(new, sorted(card.iterdir()))
    assert [p.name for p in copy] == ["boot.wav"]
    assert sorted(p.name for p in aside) == ["DONE-2.WAV", "done.wav"]
