"""The server half of MSG_SNIPPETS.

Two things are being protected. The wire format, which the card decodes by hand
and which caught its own author out once already -- the body length field sits
after a variable-length label, so its offset is not fixed. And the gate: a card
that never announced `snippets` must never be sent 0x98, because an unknown type
halts it and it can only be recovered by reflashing from another machine.

The content rules are deliberately stricter here than the C file parser, which
rejects only bytes below 0x20 and so admits 0x7f. The card's pushed path refuses
DEL and discards the entire set on one bad chunk, so a file this server accepted
but could never push would surface on the handheld as a wire fault. Refusing it
at the source means the two ends cannot disagree.
"""

import re
from pathlib import Path

import pytest

from dsterm import snippets
from dsterm.protocol import (
    FEATURE_MODE,
    FEATURE_SNIPPETS,
    HEADER_SIZE,
    MsgType,
    ProtocolError,
    decode_snippet,
    encode_snippet,
)
from dsterm.client import Client

from test_server import ServerHarness

REPO = Path(__file__).resolve().parent.parent


def c_constant(header: str, name: str) -> int:
    text = (REPO / "dsclient" / "source" / header).read_text()
    m = re.search(rf"^#define\s+{name}\s+(\d+)", text, re.M)
    assert m, f"{name} not found in {header}"
    return int(m.group(1))


class TestTheConstantsMatchTheCard:
    """Read from the C headers, not copied. A limit that drifts silently means
    the server sends a set the handheld throws away in full."""

    def test_max_snippets(self):
        assert snippets.SNIPPETS_MAX == c_constant("snippets.h", "SNIPPETS_MAX")

    def test_label_max(self):
        assert snippets.LABEL_MAX == c_constant("snippets.h", "SNIPPET_LABEL_MAX")

    def test_body_max(self):
        # snippets.h, not composer.h. These were one constant until the card's
        # composer had to hold a pasted desktop clipboard and grew to 1024; a
        # snippet is still a line in a text file and still 255. The value on
        # this side did not change, only which constant governs it -- and this
        # test is what noticed, which is the whole reason it reads the header
        # rather than copying the number.
        assert snippets.BODY_MAX == c_constant("snippets.h", "SNIPPET_BODY_MAX")


class TestTheWireFormat:
    def test_the_exact_frame(self):
        # index 0 of 1, label "yes", body "hi" -- the frame the client half and
        # this half agreed on after their first decoder read the body length
        # from a fixed offset and got the label's first two bytes.
        # Payload is 10 bytes: index, total, label_len, 3 label, 2 body_len,
        # 2 body. The client half's first draft of this frame declared 7 and
        # omitted the body, so the literal is worth writing out in full.
        assert encode_snippet(0, 1, "yes", "hi") == bytes.fromhex(
            "44530198000a" "0001" "03" "796573" "0002" "6869"
        )

    def test_round_trip(self):
        blob = encode_snippet(2, 5, "build", "cargo build --release\n")
        index, total, label, body = decode_snippet(blob[HEADER_SIZE:])
        assert (index, total, label) == (2, 5, "build")
        assert body == "cargo build --release\n"

    def test_a_maximum_chunk_stays_inside_one_segment(self):
        blob = encode_snippet(7, 8, "x" * snippets.LABEL_MAX, "y" * snippets.BODY_MAX)
        assert len(blob) == HEADER_SIZE + 3 + 15 + 2 + 255 == 281
        assert len(blob) < 536, "every server->client message must clear 536 bytes"

    def test_the_body_length_offset_depends_on_the_label(self):
        # The bug that bit the client. Two labels of different lengths, same
        # body: the body must survive both.
        for label in ("a", "abcdefghijklmno"):
            blob = encode_snippet(0, 1, label, "same body")
            _, _, got_label, body = decode_snippet(blob[HEADER_SIZE:])
            assert (got_label, body) == (label, "same body")

    @pytest.mark.parametrize("index,total", [(0, 0), (1, 1), (0, 9), (3, 2)])
    def test_encode_refuses_impossible_indexes(self, index, total):
        with pytest.raises(ProtocolError):
            encode_snippet(index, total, "l", "b")

    def test_encode_refuses_an_oversize_label_or_body(self):
        with pytest.raises(ProtocolError):
            encode_snippet(0, 1, "x" * (snippets.LABEL_MAX + 1), "b")
        with pytest.raises(ProtocolError):
            encode_snippet(0, 1, "l", "y" * (snippets.BODY_MAX + 1))

    @pytest.mark.parametrize("payload", [
        b"",
        b"\x00\x01\x03ye",                      # truncated label
        b"\x00\x01\x03yes\x00",                 # truncated body length
        b"\x00\x01\x03yes\x00\x05hi",           # declared body longer than sent
        b"\x00\x01\x00\x00\x00",                # zero label length
        b"\x00\x09\x01a\x00\x00",               # total above the maximum
        b"\x05\x01\x01a\x00\x00",               # index not below total
    ])
    def test_decode_rejects_malformed_payloads(self, payload):
        with pytest.raises(ProtocolError):
            decode_snippet(payload)


class TestTheFileParser:
    def test_a_plain_file(self):
        items = snippets.parse("build=make -j8\ntest=pytest -q\n")
        assert [(s.label, s.body) for s in items] == [
            ("build", "make -j8"), ("test", "pytest -q")]

    def test_whitespace_around_the_label_goes_and_around_the_body_stays(self):
        # Mirrors snippets.c exactly: the label is trimmed, the body is taken
        # verbatim from the byte after '='. Not an oversight on either side --
        # a body's leading space can be deliberate, and a parser that trims it
        # would silently change what gets typed into the PTY.
        item = snippets.parse("  spaced  = body with space\n")[0]
        assert item.label == "spaced"
        assert item.body == " body with space"

    def test_comments_and_blanks_are_skipped(self):
        assert snippets.parse("# a note\n\n  \nx=y\n")[0].label == "x"

    def test_escapes(self):
        assert snippets.parse(r"a=one\ntwo\tthree\\four")[0].body == "one\ntwo\tthree\\four"

    @pytest.mark.parametrize("text,why", [
        ("no equals sign\n", "expected label=body"),
        ("=body\n", "bad label length"),
        (("x" * 16) + "=b\n", "bad label length"),
        ("a=b\na=c\n", "duplicate label"),
        ("a=" + "y" * 256 + "\n", "body too long"),
        ("a=bad\\q\n", "bad escape"),
    ])
    def test_malformed_files_are_refused_with_a_line_number(self, text, why):
        with pytest.raises(snippets.SnippetError) as exc:
            snippets.parse(text)
        assert why in str(exc.value)
        assert "line" in str(exc.value)

    def test_too_many_snippets(self):
        with pytest.raises(snippets.SnippetError):
            snippets.parse("".join(f"k{i}=v\n" for i in range(snippets.SNIPPETS_MAX + 1)))

    def test_del_is_refused_even_though_the_c_file_parser_allows_it(self):
        # The specific divergence the client half flagged. If this ever passes,
        # the server can produce a set the card discards in full.
        with pytest.raises(snippets.SnippetError):
            snippets.parse("a=b\x7fc\n")

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        assert snippets.load(tmp_path / "nope") == []


class TestTheGate:
    def _harness(self, tmp_path, text):
        path = tmp_path / "snippets"
        path.write_text(text)
        h = ServerHarness()
        h.server.snippets_path = path
        return h

    def test_a_card_that_did_not_ask_is_never_sent_one(self, tmp_path):
        # The property that matters most: an older card halts on 0x98 and can
        # only be recovered by reflashing from another machine.
        sent = []
        with self._harness(tmp_path, "a=b\n") as h:
            real = h.server._send_to

            def spy(c, data):
                if len(data) > 3 and data[3] == MsgType.SNIPPETS:
                    sent.append(data)
                real(c, data)

            h.server._send_to = spy
            with Client("127.0.0.1", h.port, h.key, features=(FEATURE_MODE,)) as c:
                c.poll(0.6)
        assert sent == [], "sent 0x98 to a client that never announced snippets"

    def test_a_card_that_asked_receives_the_whole_set(self, tmp_path):
        sent = []
        with self._harness(tmp_path, "one=1\ntwo=2\nthree=3\n") as h:
            real = h.server._send_to

            def spy(c, data):
                if len(data) > 3 and data[3] == MsgType.SNIPPETS:
                    sent.append(data)
                real(c, data)

            h.server._send_to = spy
            with Client("127.0.0.1", h.port, h.key,
                        features=(FEATURE_SNIPPETS,)) as c:
                c.poll(0.6)

        decoded = [decode_snippet(m[HEADER_SIZE:]) for m in sent]
        assert [d[2] for d in decoded] == ["one", "two", "three"]
        assert {d[1] for d in decoded} == {3}, "every chunk must declare the same total"
        assert sorted(d[0] for d in decoded) == [0, 1, 2], "indexes must cover the set"

    def test_a_malformed_file_sends_nothing_rather_than_a_partial_set(self, tmp_path):
        # A partial push leaves the card on its card-file list, which is the
        # right fallback -- a half menu is worse than an old one.
        sent = []
        with self._harness(tmp_path, "ok=fine\nbroken line\n") as h:
            real = h.server._send_to

            def spy(c, data):
                if len(data) > 3 and data[3] == MsgType.SNIPPETS:
                    sent.append(data)
                real(c, data)

            h.server._send_to = spy
            with Client("127.0.0.1", h.port, h.key,
                        features=(FEATURE_SNIPPETS,)) as c:
                c.poll(0.6)
        assert sent == []
        assert h.wait_for_event("snippets not sent")

    def test_the_file_is_re_read_on_every_connect(self, tmp_path):
        # Editing the file and reconnecting is the entire workflow. Caching at
        # startup would mean a server restart for every snippet change, which is
        # the friction this feature exists to remove.
        path = tmp_path / "snippets"
        path.write_text("first=1\n")
        with ServerHarness() as h:
            h.server.snippets_path = path
            with Client("127.0.0.1", h.port, h.key,
                        features=(FEATURE_SNIPPETS,)) as c:
                c.poll(0.5)

            path.write_text("second=2\n")
            sent = []
            real = h.server._send_to

            def spy(c_, data):
                if len(data) > 3 and data[3] == MsgType.SNIPPETS:
                    sent.append(data)
                real(c_, data)

            h.server._send_to = spy
            with Client("127.0.0.1", h.port, h.key,
                        features=(FEATURE_SNIPPETS,)) as c:
                c.poll(0.6)

        assert [decode_snippet(m[HEADER_SIZE:])[2] for m in sent] == ["second"]
