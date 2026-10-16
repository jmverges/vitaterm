"""QUICK's COMMANDS: the computer's own slash commands, found and sent.

Names only cross the wire, they have to look like names, and the card puts
one in its composer rather than typing it -- so the whole surface is a short
list of strings, and these tests pin which strings.
"""

from pathlib import Path

import pytest

from dsterm import commands
from dsterm.client import Client
from dsterm.protocol import (
    COMMANDS_PAYLOAD_MAX,
    FEATURE_COMMANDS,
    HEADER_SIZE,
    SEAL_TAG_SIZE,
    SEGMENT_MAX,
    MsgType,
    ProtocolError,
    decode_commands,
    encode_commands,
)

from test_server import ServerHarness


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# a command\n")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    _touch(root / ".claude" / "commands" / "deploy.md")
    _touch(root / ".claude" / "commands" / "frontend" / "component.md")
    _touch(root / ".claude" / "commands" / "notes.txt")             # not a command
    _touch(root / ".claude" / "commands" / "bad name; rm.md")       # not a name
    _touch(root / ".claude" / "skills" / "release-notes" / "SKILL.md")
    (root / ".claude" / "skills" / "empty").mkdir(parents=True)      # no SKILL.md
    return root


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    _touch(h / ".claude" / "commands" / "standup.md")
    _touch(h / ".claude" / "commands" / "deploy.md")                  # the project's wins
    _touch(h / ".claude" / "skills" / "tidy" / "SKILL.md")
    return h


class TestDiscovery:
    def test_project_first_then_yours(self, project, home):
        assert commands.discover(str(project), home=home) == (
            "/deploy", "/component", "/release-notes", "/standup", "/tidy")

    def test_nothing_there_is_nothing(self, tmp_path):
        assert commands.discover(str(tmp_path / "nowhere"), home=tmp_path / "nobody") == ()

    def test_bounded(self, tmp_path):
        root = tmp_path / "many"
        for i in range(40):
            _touch(root / ".claude" / "commands" / f"c{i:02d}.md")
        found = commands.discover(str(root), home=tmp_path / "nobody")
        assert len(found) == commands.MAX_COMMANDS
        encode_commands(found)                          # and always sendable

    def test_long_names_stop_at_one_segment(self, tmp_path):
        # Eighteen 30-character names would be a 575-byte payload, a 589-byte
        # frame once sealed: past the DS's 536-byte segment. The list stops
        # at the last name that fits, and what is sent is one segment.
        root = tmp_path / "long"
        for i in range(18):
            _touch(root / ".claude" / "commands" / (f"{i:02d}" + "x" * 28 + ".md"))
        found = commands.discover(str(root), home=tmp_path / "nobody")
        assert 0 < len(found) < 18
        frame = encode_commands(found)
        assert len(frame) + SEAL_TAG_SIZE <= SEGMENT_MAX
        # One more name would not have fitted.
        assert len("\n".join(found + ("/" + "y" * 30,))) > COMMANDS_PAYLOAD_MAX

    def test_a_name_too_long_for_a_row_is_skipped(self, tmp_path):
        root = tmp_path / "p"
        _touch(root / ".claude" / "commands" / ("x" * 31 + ".md"))
        _touch(root / ".claude" / "commands" / "ok.md")
        assert commands.discover(str(root), home=tmp_path / "nobody") == ("/ok",)


class TestTheFrame:
    def test_round_trip(self):
        frame = encode_commands(["/deploy", "/frontend:test"])
        assert frame[3] == MsgType.COMMANDS
        assert frame[HEADER_SIZE:] == b"/deploy\n/frontend:test"
        assert decode_commands(frame[HEADER_SIZE:]) == ("/deploy", "/frontend:test")
        assert decode_commands(b"") == ()

    @pytest.mark.parametrize("bad", [
        ["deploy"], ["/"], ["/a b"], ["/x;rm"], ["/" + "x" * 31], ["/-dash"],
        ["/ok"] * 19, ["/" + "x" * 30] * 17,
    ])
    def test_refused_rather_than_trimmed(self, bad):
        with pytest.raises(ProtocolError):
            encode_commands(bad)

    @pytest.mark.parametrize("payload", [b"/ok\n/a b", b"\xff", b"/ok\n"])
    def test_a_bad_payload_is_refused(self, payload):
        with pytest.raises(ProtocolError):
            decode_commands(payload)


class TestOverTheWire:
    def test_a_card_that_asks_is_sent_the_list(self, project, monkeypatch):
        monkeypatch.setenv("HOME", str(project / "nohome"))
        with ServerHarness(cwd=str(project)) as h:
            with Client("127.0.0.1", h.port, h.key, features=(FEATURE_COMMANDS,)) as c:
                msg = c._await(MsgType.COMMANDS)
                assert decode_commands(msg.payload) == ("/deploy", "/component", "/release-notes")

    def test_a_card_that_does_not_ask_is_sent_nothing(self, project):
        with ServerHarness(cwd=str(project)) as h:
            with Client("127.0.0.1", h.port, h.key) as c:
                with pytest.raises(TimeoutError):
                    c._await(MsgType.COMMANDS, timeout=1.5)
