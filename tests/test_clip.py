"""The clipboard bridge: what crosses, what is refused, and what is never said.

The wire half and the reading half are tested separately, and then the seam
between them is tested on its own -- because the three defects this project has
shipped (a dead latch, an unwired parameter, a murmur generator that never
played) were all cases where the pieces passed and nothing ran the assembly.

The refusals matter more than the acceptances here. This is the one message type
that carries whatever the user last copied, on a link with no encryption, so a
frame that should have been refused is a password on a screen.
"""

from __future__ import annotations

import threading
import time

import pytest

from dsterm import clip, protocol
from dsterm.protocol import (
    CLIP_CHUNK_MAX,
    CLIP_CHUNKS_MAX,
    CLIP_FLAG_TRANSLITERATED,
    CLIP_FLAG_TRUNCATED,
    CLIP_FLAG_WITHHELD,
    CLIP_TEXT_MAX,
    FrameReader,
    MsgType,
    ProtocolError,
    decode_clip_chunk,
    encode_clip_chunk,
    encode_clip_empty,
)

MSS_CEILING = 536


def only(frame: bytes):
    reader = FrameReader()
    messages = list(reader.feed(frame))
    assert len(messages) == 1
    return messages[0]


class TestTheWire:
    def test_a_chunk_round_trips(self):
        frame = encode_clip_chunk(0, 1, 7, 0, 5, b"hello")
        message = only(frame)
        assert message.type is MsgType.CLIP
        assert decode_clip_chunk(message.payload) == (1, 0, 7, 0, 5, b"hello")

    def test_every_frame_clears_the_mss_ceiling(self):
        # 536 is the DS's IPv4 minimum MSS, not a guideline.
        frame = encode_clip_chunk(0, 1, 0, 0, CLIP_CHUNK_MAX,
                                  b"x" * CLIP_CHUNK_MAX)
        assert len(frame) <= MSS_CEILING

    def test_an_empty_clipboard_is_a_frame_not_a_silence(self):
        message = only(encode_clip_empty())
        assert decode_clip_chunk(message.payload) == (0, 0, 0, 0, 0, b"")

    def test_withheld_is_distinguishable_from_empty(self):
        # The whole reason the empty frame carries flags: an empty pane for a
        # concealed copy invites the user to copy the password again.
        empty = decode_clip_chunk(only(encode_clip_empty()).payload)
        held = decode_clip_chunk(
            only(encode_clip_empty(CLIP_FLAG_WITHHELD)).payload)
        assert empty[3] == 0
        assert held[3] == CLIP_FLAG_WITHHELD
        assert empty != held

    def test_total_is_u32_so_a_big_clipboard_is_not_a_wrong_number(self):
        # u16 would saturate at 65535 and the card would draw "of 65.5K" as
        # fact for a 200 KB clipboard.
        frame = encode_clip_chunk(0, 1, 0, CLIP_FLAG_TRUNCATED, 200_000, b"x")
        assert decode_clip_chunk(only(frame).payload)[4] == 200_000

    @pytest.mark.parametrize("count", [0, CLIP_CHUNKS_MAX + 1, 255])
    def test_a_bad_count_is_refused(self, count):
        with pytest.raises(ProtocolError, match="count"):
            encode_clip_chunk(0, count, 0, 0, 1, b"x")

    def test_an_index_outside_the_count_is_refused(self):
        with pytest.raises(ProtocolError, match="index"):
            encode_clip_chunk(2, 2, 0, 0, 1, b"x")

    def test_an_over_long_chunk_is_refused(self):
        with pytest.raises(ProtocolError, match="chunk length"):
            encode_clip_chunk(0, 1, 0, 0, 1, b"x" * (CLIP_CHUNK_MAX + 1))

    def test_an_empty_chunk_is_refused(self):
        # A zero-length chunk is what an empty frame is for; two shapes meaning
        # the same thing is how decoders drift apart.
        with pytest.raises(ProtocolError, match="chunk length"):
            encode_clip_chunk(0, 1, 0, 0, 0, b"")

    @pytest.mark.parametrize("body", [b"\x00", b"\x1b[31m", b"\xff", b"\x07"])
    def test_non_printable_bytes_are_refused(self, body):
        # Escape sequences especially: this text is destined for a terminal.
        with pytest.raises(ProtocolError, match="printable ASCII"):
            encode_clip_chunk(0, 1, 0, 0, len(body), body)

    def test_newline_survives_because_the_composer_holds_it(self):
        frame = encode_clip_chunk(0, 1, 0, 0, 3, b"a\nb")
        assert decode_clip_chunk(only(frame).payload)[5] == b"a\nb"

    def test_the_decoder_refuses_what_the_encoder_refuses(self):
        # The interop vector proves both ends ACCEPT the same frames; this
        # proves they REFUSE the same ones, which is the half that matters when
        # the text is somebody's password.
        payload = bytes((1, 0, 0, 0)) + b"\x00\x00\x00\x01" + bytes((1,)) + b"\x00"
        with pytest.raises(ProtocolError, match="printable ASCII"):
            decode_clip_chunk(payload)

    def test_a_declared_length_that_lies_is_refused(self):
        payload = bytes((1, 0, 0, 0)) + b"\x00\x00\x00\x05" + bytes((5,)) + b"ab"
        with pytest.raises(ProtocolError, match="declared length"):
            decode_clip_chunk(payload)


class TestItCanActuallyFindPowerShell:
    """The one line that touches the real environment, tested where it runs.

    Shipped broken and silent. The systemd unit sets PATH explicitly -- that
    line exists so the child can find `claude` -- and the value it sets does not
    include /mnt/c/Windows/System32/WindowsPowerShell/v1.0. So under the service
    `powershell.exe` did not resolve, subprocess.run raised OSError, read()
    returned None, and "failure is silence" swallowed it on every poll forever.

    Every other test in this file injects a fake reader, which is exactly why
    none of them caught it: the pieces passed and nothing ran the assembly in
    the environment that matters. This pins the resolution itself.
    """

    UNIT_PATH = ("/home/user/.local/bin:/home/user/bin:/usr/local/bin:"
                 "/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin")

    def test_it_resolves_under_the_units_stripped_path(self, monkeypatch):
        monkeypatch.setenv("PATH", self.UNIT_PATH)
        found = clip.powershell_path()
        if found is None:
            pytest.skip("no Windows interop on this host at all")
        assert found.startswith("/"), "must be absolute, not PATH-dependent"

    def test_it_prefers_the_path_when_there_is_one(self, monkeypatch):
        monkeypatch.setattr(clip.shutil, "which",
                            lambda name: "/somewhere/powershell.exe")
        assert clip.powershell_path() == "/somewhere/powershell.exe"

    def test_it_falls_back_to_the_known_location(self, monkeypatch):
        monkeypatch.setattr(clip.shutil, "which", lambda name: None)
        monkeypatch.setattr(clip.os.path, "exists",
                            lambda p: p == clip._POWERSHELL_CANDIDATES[0])
        assert clip.powershell_path() == clip._POWERSHELL_CANDIDATES[0]

    def test_none_when_there_is_no_powershell_anywhere(self, monkeypatch):
        monkeypatch.setattr(clip.shutil, "which", lambda name: None)
        monkeypatch.setattr(clip.os.path, "exists", lambda p: False)
        assert clip.powershell_path() is None

    def test_read_gives_up_rather_than_raising_when_it_cannot_be_found(
            self, monkeypatch):
        monkeypatch.setattr(clip, "powershell_path", lambda: None)
        assert clip.read() is None


class TestAPermanentFailureIsNotSilent:
    """Silence is right for a hiccup and wrong for a broken deployment.

    The PATH bug was invisible because every failed read looked exactly like
    every other failed read: nothing. A transient failure should stay quiet, but
    a reader that has never once succeeded should say so, once, or the operator
    is left staring at a handheld that simply never receives anything.
    """

    def test_the_first_failure_is_reported(self):
        problems: list[str] = []

        def broken():
            raise OSError("powershell.exe: No such file or directory")

        reader = clip.ClipReader(interval=0.0, reader=broken,
                                 on_problem=problems.append)
        try:
            self._spin(reader, 0.5)
            assert problems, "a reader that never works must say so"
            assert "clip" in problems[0].lower()
        finally:
            reader.stop()

    def test_it_is_reported_once_and_not_every_poll(self):
        # Otherwise a broken deployment writes a line every three seconds
        # forever, which is its own kind of unreadable.
        problems: list[str] = []
        reader = clip.ClipReader(interval=0.0, reader=lambda: None,
                                 on_problem=problems.append)
        try:
            self._spin(reader, 0.6)
            assert len(problems) == 1
        finally:
            reader.stop()

    def test_recovery_is_reported_too(self):
        problems: list[str] = []
        state = {"broken": True}

        def flaky():
            if state["broken"]:
                return None
            return clip.convert("back")

        reader = clip.ClipReader(interval=0.0, reader=flaky,
                                 on_problem=problems.append)
        try:
            self._spin(reader, 0.4)
            assert len(problems) == 1
            state["broken"] = False
            self._spin(reader, 0.4)
            assert len(problems) == 2
            assert "again" in problems[1].lower() or \
                   "recovered" in problems[1].lower()
        finally:
            reader.stop()

    def test_the_problem_message_never_carries_clipboard_text(self):
        problems: list[str] = []
        secret = "hunter2-correct-horse"

        def broken():
            raise OSError(f"failed while holding {secret}")

        reader = clip.ClipReader(interval=0.0, reader=broken,
                                 on_problem=problems.append)
        try:
            self._spin(reader, 0.4)
            assert problems
            assert all(secret not in p for p in problems)
        finally:
            reader.stop()

    @staticmethod
    def _spin(reader, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            reader.poll()
            time.sleep(0.01)


class TestTheInteropVector:
    """The exact bytes the C side must print, pinned here until it does.

    tools/crosscheck.py cannot carry this expectation yet: with no counterpart
    from test_host.c it fails as MISSING and reds the client's gate over a
    vector nobody has been asked to emit, blocking a flash of unrelated work.
    Both sides of an interop vector have to land together.

    So the layout is pinned here in the meantime. If a field moves on this side
    while the C side is being written, this fails immediately rather than at the
    moment the two are finally compared -- which is the failure the crosscheck
    exists to prevent, arriving too late to be cheap.
    """

    VECTOR = "4453019b000c02012a030001117003610a62"

    def test_the_chunk_vector_is_exactly_these_bytes(self):
        frame = encode_clip_chunk(
            1, 2, 0x2A, CLIP_FLAG_TRUNCATED | CLIP_FLAG_TRANSLITERATED,
            70_000, b"a\nb")
        assert frame.hex() == self.VECTOR

    def test_the_vector_exercises_what_two_readers_get_wrong(self):
        # Both flag bits at once, so a swapped pair shows up. A total above
        # 65535, so a u16 implementation fails loudly rather than saturating
        # quietly. A newline in the body, because it is the one control byte
        # that survives the fold and a decoder rejecting it drops every
        # multi-line paste.
        count, index, seq, flags, total, body = decode_clip_chunk(
            bytes.fromhex(self.VECTOR)[6:])
        assert (count, index, seq) == (2, 1, 0x2A)
        assert flags == CLIP_FLAG_TRUNCATED | CLIP_FLAG_TRANSLITERATED
        assert total == 70_000 > 0xFFFF
        assert body == b"a\nb"

    def test_the_empty_and_withheld_vectors(self):
        assert encode_clip_empty().hex() == "4453019b00020000"
        assert encode_clip_empty(CLIP_FLAG_WITHHELD).hex() == "4453019b00020004"


class TestTheFold:
    def test_plain_text_is_untouched_and_says_so(self):
        snap = clip.convert("hello world")
        assert snap.text == "hello world"
        assert snap.flags == 0
        assert snap.total == 11

    def test_crlf_and_lone_cr_become_lf(self):
        assert clip.convert("a\r\nb\rc").text == "a\nb\nc"

    @pytest.mark.parametrize("raw,expected", [
        ("em—dash", "em-dash"),
        ("curly’quote", "curly'quote"),
        ("smart “quotes”", 'smart "quotes"'),
        ("ellipsis…", "ellipsis..."),
        ("nb space", "nb space"),
    ])
    def test_common_punctuation_folds_to_something_that_reads_the_same(
            self, raw, expected):
        assert clip.convert(raw).text == expected

    def test_any_change_at_all_sets_the_flag(self):
        # One byte different is enough: a paste that silently became different
        # text does something other than what was copied.
        assert clip.convert("a—b").flags & CLIP_FLAG_TRANSLITERATED
        assert not clip.convert("a-b").flags & CLIP_FLAG_TRANSLITERATED

    def test_accents_decompose_rather_than_vanish(self):
        assert clip.convert("café").text == "cafe"

    def test_truncation_reports_the_true_total(self):
        snap = clip.convert("x" * 5000)
        assert len(snap.text) == CLIP_TEXT_MAX
        assert snap.total == 5000
        assert snap.flags & CLIP_FLAG_TRUNCATED

    def test_the_invariant_that_ties_chunks_to_total(self):
        # sum(len) == min(total, CLIP_TEXT_MAX). This is the property that stops
        # the two encoders agreeing in testing and disagreeing on the wire.
        for size in (1, 191, 192, 193, 1023, 1024, 4096):
            snap = clip.convert("x" * size)
            frames = clip.chunks(snap, seq=0)
            decoded = [decode_clip_chunk(only(f).payload) for f in frames]
            sent = sum(len(d[5]) for d in decoded)
            assert sent == min(snap.total, CLIP_TEXT_MAX), f"at {size}"

    def test_a_full_snapshot_never_exceeds_the_chunk_budget(self):
        frames = clip.chunks(clip.convert("x" * 4096), seq=0)
        assert len(frames) <= CLIP_CHUNKS_MAX


class TestConcealedContent:
    def test_a_concealed_clipboard_yields_withheld_and_no_text(self):
        reader = _fake_powershell("CONCEALED\n")
        snap = reader()
        assert snap.withheld
        assert snap.text == ""
        assert snap.flags & CLIP_FLAG_WITHHELD

    def test_withheld_puts_no_text_on_the_wire(self):
        frames = clip.chunks(clip.WITHHELD, seq=0)
        assert len(frames) == 1
        count, _, _, flags, _, body = decode_clip_chunk(only(frames[0]).payload)
        assert count == 0 and body == b"" and flags == CLIP_FLAG_WITHHELD

    def test_an_unrecognised_probe_answer_is_refused_not_trusted(self):
        # A malformed answer must never be mistaken for a clear clipboard: that
        # is the direction where a password gets through.
        assert _fake_powershell("something went wrong\n")() is None

    def test_the_probe_checks_both_concealment_markers(self):
        # The two formats Windows defines for "do not sync or keep this".
        assert "ExcludeClipboardContentFromMonitorProcessing" in clip._PROBE
        assert "CanIncludeInClipboardHistory" in clip._PROBE

    def test_the_probe_guards_against_a_zero_format_id(self):
        # RegisterClipboardFormat returns 0 on failure; without the guard every
        # read would look concealed, which fails safe but silently kills the
        # feature. So it is guarded.
        assert "-ne 0" in clip._PROBE

    def test_these_tests_do_not_need_a_windows_host(self, monkeypatch):
        # The regression that made this file fail on macOS while passing on
        # WSL: with no powershell.exe anywhere, read() gave up before reaching
        # the stub and the failure read as a broken concealed path. Simulate a
        # host with no interop at all and prove the parsing still runs.
        monkeypatch.setattr(clip.shutil, "which", lambda name: None)
        monkeypatch.setattr(clip.os.path, "exists", lambda p: False)
        assert clip.powershell_path() is None, "no interop, as intended"
        snap = _fake_powershell("CONCEALED\n")()
        assert snap is not None and snap.withheld

    def test_the_check_and_the_read_are_one_invocation(self):
        # Two spawns would leave a ~200ms window -- measured, that is what one
        # spawn costs here -- for a password copied in between to be returned
        # as ordinary text.
        assert clip._PROBE.count("Get-Clipboard") == 1
        assert "IsClipboardFormatAvailable" in clip._PROBE
        assert clip._PROBE.index("IsClipboardFormatAvailable") < \
            clip._PROBE.index("Get-Clipboard")


class TestTextThatIsNotUtf8:
    """The bridge died on exactly the text it was built to handle.

    Observed on the live server, after the bridge had already delivered 85
    bytes successfully:

        UnicodeDecodeError: 'utf-8' codec can't decode byte 0xf2 in
        position 1095: invalid continuation byte
        clip: cannot read the clipboard (.../powershell.exe)

    subprocess.run(text=True) decodes stdout as UTF-8. PowerShell writes the
    console code page, which is not UTF-8, so one non-ASCII byte anywhere in
    the clipboard raised inside read() -- and read() catches only OSError and
    SubprocessError, so it escaped to ClipReader, which reported the bridge as
    broken.

    The bitter part is which text triggers it. An em dash, a curly quote, an
    accented name: precisely the characters the fold exists to turn into
    something a 6x8 ASCII font can draw. The feature failed only when it was
    needed, and looked fine on plain ASCII forever.

    Two fixes, because either alone leaves a hole. The probe now declares UTF-8
    output, so the characters arrive intact and can be folded properly rather
    than mangled. And the decode cannot raise, so a byte that still slips
    through costs one replacement character instead of the whole clipboard.
    """

    def test_the_probe_asks_powershell_for_utf8(self):
        assert "OutputEncoding" in clip._PROBE
        assert "UTF8" in clip._PROBE

    def test_a_byte_that_is_not_utf8_does_not_kill_the_read(self, monkeypatch):
        # The exact byte from the live failure.
        raw = ("CLEAR\nhello " + b"\xf2".decode("latin-1") + " world")

        class Result:
            returncode = 0
            stdout = raw

        monkeypatch.setattr(clip, "powershell_path",
                            lambda: "/fake/powershell.exe")
        monkeypatch.setattr(clip.subprocess, "run",
                            lambda *a, **k: Result())
        snap = clip.read()
        assert snap is not None, "one odd byte must not lose the whole clipboard"
        assert "hello" in snap.text and "world" in snap.text

    def test_the_decode_is_told_not_to_raise(self):
        # Belt and braces for whatever the console code page does next: the
        # call itself must be incapable of raising on undecodable output.
        import inspect
        source = inspect.getsource(clip.read)
        assert "errors=" in source, "subprocess.run must not decode strictly"

    def test_a_decode_error_would_still_be_survived(self, monkeypatch):
        # If some future path raises it anyway, the reader reports a problem
        # and keeps the last snapshot -- it does not take the server down.
        def boom(*a, **k):
            raise UnicodeDecodeError("utf-8", b"\xf2", 0, 1, "invalid")

        monkeypatch.setattr(clip, "powershell_path",
                            lambda: "/fake/powershell.exe")
        monkeypatch.setattr(clip.subprocess, "run", boom)
        assert clip.read() is None


class TestNothingEverLogsTheText:
    def test_the_snapshot_repr_does_not_contain_the_text(self):
        # repr ends up in exception messages and debugger output. A clipboard in
        # journalctl is permanent and at rest, which is worse than the LAN
        # exposure that was actually accepted.
        secret = "hunter2-correct-horse"
        assert secret not in repr(clip.convert(secret))

    def test_the_repr_still_says_enough_to_debug_with(self):
        text = repr(clip.convert("abc"))
        assert "3B" in text and "total=3" in text


class TestTheReaderNeverBlocksTheFrameLoop:
    """One spawn costs 0.18-0.22s here, measured. The tick cannot wait for it.

    Same property, same shape, and the same reason as project.BackgroundCollector
    -- which exists because gathering the PROJECT page inline stalled the frame
    loop for 1.59s out of every 3s.
    """

    def test_polling_returns_at_once_while_a_read_is_running(self):
        release = threading.Event()

        def slow():
            release.wait(timeout=5.0)
            return clip.EMPTY

        reader = clip.ClipReader(reader=slow)
        try:
            start = time.monotonic()
            for _ in range(5):
                reader.poll()
            elapsed = time.monotonic() - start
            assert elapsed < 0.1, f"polling blocked for {elapsed:.3f}s"
        finally:
            release.set()
            reader.stop()

    def test_one_read_runs_at_a_time(self):
        release = threading.Event()
        calls: list[int] = []

        def slow():
            calls.append(1)
            release.wait(timeout=5.0)
            return clip.EMPTY

        reader = clip.ClipReader(reader=slow)
        try:
            for _ in range(50):
                reader.poll()
            assert len(calls) == 1
        finally:
            release.set()
            reader.stop()

    def test_an_unchanged_clipboard_does_not_re_push(self):
        # Otherwise the same six frames go out every interval forever.
        snap = clip.convert("steady")
        reader = clip.ClipReader(interval=0.0, reader=lambda: snap)
        try:
            assert self._poll_until_change(reader) is True
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                assert reader.poll() is False
                time.sleep(0.01)
        finally:
            reader.stop()

    def test_the_sequence_advances_only_on_a_real_change(self):
        state = {"text": "one"}
        reader = clip.ClipReader(interval=0.0,
                                 reader=lambda: clip.convert(state["text"]))
        try:
            assert self._poll_until_change(reader) is True
            first = reader.seq
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                reader.poll()
                time.sleep(0.01)
            assert reader.seq == first
            state["text"] = "two"
            assert self._poll_until_change(reader) is True
            assert reader.seq == ((first + 1) & 0xFF)
        finally:
            reader.stop()

    def test_a_read_that_fails_keeps_the_last_snapshot(self):
        state = {"fail": False}

        def flaky():
            if state["fail"]:
                raise OSError("powershell fell over")
            return clip.convert("kept")

        reader = clip.ClipReader(interval=0.0, reader=flaky)
        try:
            assert self._poll_until_change(reader) is True
            state["fail"] = True
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                reader.poll()
                time.sleep(0.01)
            assert reader.snapshot is not None
            assert reader.snapshot.text == "kept"
        finally:
            reader.stop()

    def test_a_read_returning_none_is_not_a_snapshot(self):
        # None is "no answer"; EMPTY is "really empty". Conflating them would
        # push an empty clipboard every time PowerShell hiccuped.
        reader = clip.ClipReader(interval=0.0, reader=lambda: None)
        try:
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                assert reader.poll() is False
                time.sleep(0.01)
            assert reader.snapshot is None
        finally:
            reader.stop()

    @staticmethod
    def _poll_until_change(reader, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if reader.poll():
                return True
            time.sleep(0.01)
        return False


class TestTheServerGates:
    """Four gates, and every one of them must hold before a byte goes out.

    Enabled by --clip, the card advertised the token, the client is
    authenticated, and there is a reading to send. These are tested at the seam
    rather than by inspecting the flags, because "the feature is wired" and "the
    feature is correct" are different claims and this project has shipped the
    second without the first three times.
    """

    @staticmethod
    def _server(**kwargs):
        from dsterm.server import Server
        return Server(["true"], b"k" * 32, host="127.0.0.1", port=0, **kwargs)

    @staticmethod
    def _client(server, *, authed=True, knows_clip=True):
        import socket
        from dsterm.grid import Grid
        from dsterm.protocol import FrameReader
        from dsterm.server import ClientState
        left, right = socket.socketpair()
        left.setblocking(False)
        client = ClientState(left, ("test", 1), FrameReader(), b"nonce",
                             authed=authed, mirror=Grid.blank())
        client.caps.clip = knows_clip
        server._clients.append(client)
        return client, right

    def test_disabled_by_default(self):
        server = self._server()
        assert server.clip_enabled is False
        assert server._clip is None

    def test_nothing_is_sent_when_the_server_did_not_enable_it(self):
        server = self._server()
        client, _ = self._client(server)
        server._advertise_clip(client)
        assert not client.outbox

    def test_nothing_is_sent_to_a_card_that_never_asked(self):
        server = self._server(clip_enabled=True)
        server._clip = _StubReader(clip.convert("secret-ish"))
        client, _ = self._client(server, knows_clip=False)
        server._advertise_clip(client)
        assert not client.outbox

    def test_nothing_is_sent_before_authentication(self):
        server = self._server(clip_enabled=True)
        server._clip = _StubReader(clip.convert("secret-ish"))
        client, _ = self._client(server, authed=False)
        server._advertise_clip(client)
        assert not client.outbox

    def test_an_authenticated_card_that_asked_gets_it(self):
        server = self._server(clip_enabled=True)
        server._clip = _StubReader(clip.convert("hello"))
        client, _ = self._client(server)
        server._advertise_clip(client)
        assert len(client.outbox) == 1
        decoded = decode_clip_chunk(only(client.outbox[0]).payload)
        assert decoded[5] == b"hello"

    def test_the_same_snapshot_is_not_sent_twice(self):
        server = self._server(clip_enabled=True)
        server._clip = _StubReader(clip.convert("hello"))
        client, _ = self._client(server)
        server._advertise_clip(client)
        server._advertise_clip(client)
        assert len(client.outbox) == 1

    def test_a_reconnect_is_told_again(self):
        # The HELLO resets clip_seq_sent, because a card that went away has been
        # told nothing whatever this object remembers.
        server = self._server(clip_enabled=True)
        server._clip = _StubReader(clip.convert("hello"))
        client, _ = self._client(server)
        server._advertise_clip(client)
        client.feeds.clip_seq = -1
        client.outbox.clear()
        server._advertise_clip(client)
        assert len(client.outbox) == 1

    def test_the_tick_hands_the_clipboard_to_the_clients(self):
        # The assertion that the wiring exists at all.
        server = self._server(clip_enabled=True)
        server._clip = _StubReader(clip.convert("typed"), changed=True)
        client, _ = self._client(server)
        server._poll_clip(time.monotonic())
        assert client.outbox, "the tick never advertised the clipboard"

    def test_a_disabled_server_polls_nothing(self):
        server = self._server()
        server._poll_clip(time.monotonic())  # must not raise

    def test_the_clipboard_is_not_read_when_nobody_asked_for_it(self):
        # A read spawns PowerShell and costs ~0.2s. Doing that when no card has
        # advertised the token is a process every interval forever in exchange
        # for frames that can never be sent -- and it means the clipboard is
        # not being read at all until something actually wants it, which is the
        # better default for a feature that handles whatever people copy.
        server = self._server(clip_enabled=True)
        reader = _CountingReader()
        server._clip = reader
        self._client(server, knows_clip=False)
        server._poll_clip(time.monotonic())
        assert reader.polls == 0

    def test_an_unauthenticated_client_does_not_start_the_reader(self):
        server = self._server(clip_enabled=True)
        reader = _CountingReader()
        server._clip = reader
        self._client(server, authed=False, knows_clip=True)
        server._poll_clip(time.monotonic())
        assert reader.polls == 0

    def test_one_card_that_asked_is_enough(self):
        server = self._server(clip_enabled=True)
        reader = _CountingReader()
        server._clip = reader
        self._client(server, knows_clip=False)
        self._client(server, knows_clip=True)
        server._poll_clip(time.monotonic())
        assert reader.polls == 1

    def test_the_event_log_never_contains_the_text(self):
        # journalctl and the dashboard both read these. A clipboard there is
        # permanent and at rest, which is not the bargain that was struck.
        events: list[str] = []
        server = self._server(clip_enabled=True, on_event=events.append)
        secret = "hunter2-correct-horse-battery"
        server._clip = _StubReader(clip.convert(secret))
        client, _ = self._client(server)
        server._advertise_clip(client)
        assert events, "nothing was emitted at all"
        assert all(secret not in e for e in events)
        assert any("clip" in e for e in events)

    def test_a_withheld_clipboard_says_so_without_saying_what(self):
        events: list[str] = []
        server = self._server(clip_enabled=True, on_event=events.append)
        server._clip = _StubReader(clip.WITHHELD)
        client, _ = self._client(server)
        server._advertise_clip(client)
        assert any("withheld" in e for e in events)
        decoded = decode_clip_chunk(only(client.outbox[0]).payload)
        assert decoded[0] == 0 and decoded[3] == CLIP_FLAG_WITHHELD


class TestTheHandshakeSaysWhatItNegotiated:
    """What the card asked for, in the log, because nothing else records it.

    The handshake is one-way: the card announces its tokens and this server
    announces nothing back. When the clipboard bridge did not light up, the only
    way to establish that the card was not advertising "clip" was to reason from
    the absence of other log lines and then read the flashed source. One line
    would have answered it.
    """

    @staticmethod
    def _server(events):
        from dsterm.server import Server
        return Server(["true"], b"k" * 32, host="127.0.0.1", port=0,
                      on_event=events.append)

    @staticmethod
    def _hello(server, payload: bytes):
        import socket
        from dsterm.grid import Grid
        from dsterm.protocol import FrameReader, Message, MsgType
        from dsterm.server import ClientState
        left, _right = socket.socketpair()
        left.setblocking(False)
        client = ClientState(left, ("test", 1), FrameReader(), b"nonce",
                             mirror=Grid.blank())
        server._clients.append(client)
        server._handle(client, Message(MsgType.HELLO, payload))
        return client

    def test_the_tokens_are_named(self):
        events: list[str] = []
        server = self._server(events)
        self._hello(server, b"ds-term/1 mode snippets project clip")
        said = " ".join(events)
        for token in ("mode", "snippets", "project", "clip"):
            assert token in said

    def test_a_card_with_no_tokens_says_so_rather_than_nothing(self):
        # Silence would be indistinguishable from the line not existing.
        events: list[str] = []
        server = self._server(events)
        self._hello(server, b"ds-term/1")
        assert any("nothing optional" in e for e in events)

    def test_an_old_card_missing_clip_is_visible_at_a_glance(self):
        # The exact question that cost a round trip.
        events: list[str] = []
        server = self._server(events)
        self._hello(server, b"ds-term/1 mode snippets project")
        line = next(e for e in events if "asked for" in e)
        assert "project" in line and "clip" not in line


class _CountingReader:
    """A ClipReader that only records whether it was polled."""

    def __init__(self) -> None:
        self.snapshot = None
        self.seq = 0
        self.polls = 0

    def poll(self, now=None) -> bool:
        self.polls += 1
        return False

    def stop(self, timeout: float = 2.0) -> None:
        pass


class _StubReader:
    """A ClipReader with a fixed answer, for the server-side gates."""

    def __init__(self, snapshot, changed: bool = False) -> None:
        self.snapshot = snapshot
        self.seq = 1
        self._changed = changed

    def poll(self, now=None) -> bool:
        return self._changed

    def stop(self, timeout: float = 2.0) -> None:
        pass


def _fake_powershell(stdout: str):
    """A clip.read() wired to a canned PowerShell answer.

    Stubs ``powershell_path`` as well as ``subprocess.run``, and it has to.

    This test failed on macOS for days while passing here. The PATH fix at
    A change added an early return to ``read()`` -- resolve the binary first,
    give up if there is none -- which runs BEFORE ``subprocess.run``. Stubbing
    only the latter meant that on a host with no powershell.exe the fake was
    never reached: ``read()`` returned None and the assertion died on
    ``NoneType.withheld``, looking for all the world like the concealed path
    was broken.

    It was not a platform gate and it should not become a skip. What these
    tests exercise is the parsing of CONCEALED versus CLEAR, which is string
    handling with no host in it at all. A skip would lose that coverage
    everywhere except Windows, for a reason that was never true.
    """
    class Result:
        returncode = 0

    def fake_run(*args, **kwargs):
        result = Result()
        result.stdout = stdout
        return result

    def reader():
        import dsterm.clip as module
        real_run = module.subprocess.run
        real_path = module.powershell_path
        module.subprocess.run = fake_run
        module.powershell_path = lambda: "/fake/powershell.exe"
        try:
            return module.read()
        finally:
            module.subprocess.run = real_run
            module.powershell_path = real_path

    return reader
