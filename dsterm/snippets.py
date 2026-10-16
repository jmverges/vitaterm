"""Snippets the server pushes to the handheld.

The mirror of dsclient/source/snippets.c. Both parsers read the same file format
and enforce the same limits, and tests/test_snippets.py reads the C header to
check the constants have not drifted.

What a snippet is worth being clear about, because it decides how paranoid this
file has to be: a snippet is text the handheld will later type into the PTY. It
does not execute here and it cannot execute there without passing through the
composer, where the user sees the whole body on the top screen and commits it
with a second press. That gate is the entire safety case for pushing these over
a link with no per-message integrity -- see docs/design/SNIPPETS_PUSH.md. (A card with
SEAL, docs/SEAL.md, has that integrity; an older card does not, and still
connects, so the gate stays.) Nothing in this module may assume the gate exists;
it validates as if it did not.
"""

from __future__ import annotations

from dataclasses import dataclass

# Mirrors of dsclient/source/{snippets,composer}.h.
SNIPPETS_MAX = 8
LABEL_MAX = 15
BODY_MAX = 255
LINE_MAX = 511


class SnippetError(ValueError):
    """A malformed snippet file. Carries the line number, like the C parser."""


@dataclass(frozen=True)
class Snippet:
    label: str
    body: str


def _body_ok(body: str) -> bool:
    """Printable ASCII plus newline and tab.

    Deliberately stricter than the C file parser, which rejects only bytes below
    0x20 and so lets 0x7f (DEL) through. The pushed path on the card rejects DEL
    and discards the whole set when it sees one, so a file the server accepted
    but could never successfully push would fail on the handheld with a message
    about a bad chunk -- pointing at the wire rather than at the file that caused
    it. Refusing it here means the two ends cannot disagree.
    """
    return all(0x20 <= ord(ch) <= 0x7E or ch in "\n\t" for ch in body)


def _decode_body(raw: str, line_no: int) -> str:
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\":
            i += 1
            if i >= len(raw):
                raise SnippetError(f"line {line_no}: bad escape")
            nxt = raw[i]
            if nxt == "n":
                ch = "\n"
            elif nxt == "t":
                ch = "\t"
            elif nxt == "\\":
                ch = "\\"
            else:
                raise SnippetError(f"line {line_no}: bad escape")
        if not _body_ok(ch):
            raise SnippetError(f"line {line_no}: control byte in body")
        if len(out) >= BODY_MAX:
            raise SnippetError(f"line {line_no}: body too long")
        out.append(ch)
        i += 1
    return "".join(out)


def parse(text: str) -> list[Snippet]:
    """Parse `label=body` lines. Raises SnippetError with a line number.

    Refuses rather than salvages. A snippet file that is half understood would
    put text the user did not write behind a button they will press without
    reading it twice.
    """
    items: list[Snippet] = []
    seen: set[str] = set()

    for line_no, raw in enumerate(text.splitlines(), start=1):
        if len(raw) > LINE_MAX:
            raise SnippetError(f"line {line_no}: line too long")
        stripped = raw.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise SnippetError(f"line {line_no}: expected label=body")
        label, _, body = stripped.partition("=")
        label = label.rstrip(" \t")
        if not 1 <= len(label) <= LABEL_MAX:
            raise SnippetError(f"line {line_no}: bad label length")
        if not _body_ok(label):
            raise SnippetError(f"line {line_no}: control byte in label")
        if len(items) >= SNIPPETS_MAX:
            raise SnippetError(f"line {line_no}: too many snippets")
        if label in seen:
            raise SnippetError(f"line {line_no}: duplicate label")
        seen.add(label)
        items.append(Snippet(label, _decode_body(body, line_no)))

    return items


def load(path) -> list[Snippet]:
    """Read a snippet file. A missing file is not an error -- it means no
    snippets, which is a legitimate configuration, exactly as on the card."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except UnicodeDecodeError as exc:
        raise SnippetError(f"{path}: not UTF-8 ({exc})") from exc
    return parse(text)


def validate(items: list[Snippet]) -> None:
    """The wire rules, checked before anything is encoded.

    The file parser already enforces these, but this runs on whatever is about
    to be sent regardless of where it came from: the card discards an entire
    push on one bad chunk, so an invalid set must never leave here.
    """
    if len(items) > SNIPPETS_MAX:
        raise SnippetError(f"{len(items)} snippets exceeds {SNIPPETS_MAX}")
    for s in items:
        if not 1 <= len(s.label.encode()) <= LABEL_MAX:
            raise SnippetError(f"label {s.label!r} is not 1..{LABEL_MAX} bytes")
        if len(s.body.encode()) > BODY_MAX:
            raise SnippetError(f"body for {s.label!r} exceeds {BODY_MAX} bytes")
        if not _body_ok(s.body) or not _body_ok(s.label):
            raise SnippetError(f"{s.label!r} contains a byte the card will refuse")
