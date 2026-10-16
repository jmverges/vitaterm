"""Fixed-size cell grid, snapshots, and diffs.

This module is the single place that decides what a "cell" is, because that
decision is frozen into the wire format and is expensive to change
afterwards.

The grid is fixed for the life of the process but no longer hardcoded: the
handheld's zoom level is a function of the font its renderer ships, and that is
a client-side decision the server should follow rather than dictate. Geometry is
therefore read from the environment once, at import, so every existing
``from .grid import COLS, ROWS`` keeps working and pyte, the PTY winsize,
COLUMNS/LINES and tmux's -x/-y all follow from the same two numbers.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from typing import Iterator

from wcwidth import wcwidth

# The DS panel is 256x192. 42x24 is the 6x8 font that shipped first: 42*6=252
# and 24*8=192, so it fills the screen exactly. A smaller font zooms out --
# a 4x6 would be 64x32 -- and only the card knows which one it has.
#
# Bounds are not taste. A diff entry encodes row and col as one byte each
# (DIFF_ENTRY_SIZE in protocol.py), so 255 is the hard ceiling in both
# directions, and the client validates against these before touching its cell
# array -- it has no MMU to catch an overrun. The lower bound keeps a
# misconfiguration from producing a screen no TUI can draw into.
_COLS_MIN, _COLS_MAX = 20, 255
_ROWS_MIN, _ROWS_MAX = 8, 255


def _geometry(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not an integer") from None
    if not low <= value <= high:
        raise ValueError(f"{name}={value} outside {low}..{high}")
    return value


COLS = _geometry("DSTERM_COLS", 42, _COLS_MIN, _COLS_MAX)
ROWS = _geometry("DSTERM_ROWS", 24, _ROWS_MIN, _ROWS_MAX)

# Substituted for anything the DS font cannot represent. Chosen because it is
# visually obviously "missing" rather than plausibly part of the output.
REPLACEMENT = "?"

# The DS renders a small fixed palette. pyte hands back xterm colour names and
# "brown"/"brightblack" style variants; everything collapses to one of these.
PALETTE = (
    "default",
    "black",
    "red",
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "white",
    # Not an ANSI colour. Claude Code paints its own mark, its box rules and
    # its prompt in 256-colour 174 and neighbours, which is the one colour this
    # program renders more than any other -- and eight ANSI slots contain
    # nothing within reach of it. The attribute byte already carries four bits
    # per colour and only nine were spoken for, so this costs no wire and no
    # protocol version: a card that predates it falls back to default rather
    # than mis-rendering.
    "orange",
)

# pyte emits some names that are not in PALETTE, plus raw hex for 256-colour and
# truecolour SGR sequences. Map the names we know; hex is handled numerically.
_COLOR_ALIASES = {
    "brown": "yellow",
    "brightblack": "black",
    "brightred": "red",
    "brightgreen": "green",
    "brightbrown": "yellow",
    "brightyellow": "yellow",
    "brightblue": "blue",
    "brightmagenta": "magenta",
    "brightcyan": "cyan",
    "brightwhite": "white",
}

# Reference RGB for each palette entry, used to snap unknown hex colours to the
# nearest representable one.
_PALETTE_RGB = {
    "black": (0, 0, 0),
    "red": (205, 0, 0),
    "green": (0, 205, 0),
    "yellow": (205, 205, 0),
    "blue": (0, 0, 238),
    "magenta": (205, 0, 205),
    "cyan": (0, 205, 205),
    "white": (229, 229, 229),
    "orange": (216, 120, 88),
}

# Where each palette entry sits on the colour wheel. White, black and default
# are absent on purpose: they are what a colour collapses to when it has no hue
# at all, and letting them compete on plain distance is what turned Claude's
# mark white.
#
# Hue rather than RGB distance, because distance has no idea what a colour *is*:
# it made white the nearest entry to Claude's #d78787 (white is light, and so is
# the mark), and once orange was added it made orange the nearest entry to a
# pale green (orange sits near the middle of the cube and pale colours cluster
# there). Hue answers the only question worth asking here -- which of ten
# crayons is this -- and brightness is handled separately above.
_HUES = (
    ("red", 0.0),
    ("orange", 15.0),
    ("yellow", 60.0),
    ("green", 120.0),
    ("cyan", 180.0),
    ("blue", 240.0),
    ("magenta", 300.0),
)

# Below this saturation a colour is treated as grey. 0.25 keeps Claude's
# 256-colour 174 (saturation 0.37) chromatic while dim terminal greys stay grey.
_ACHROMATIC_NUM, _ACHROMATIC_DEN = 1, 4

# A red below this saturation is a salmon rather than a red, and the closest
# thing this palette has to a salmon is its orange -- which is, not
# coincidentally, where Claude's own colours live. #d78787 lands here.
_PALE_RED_NUM, _PALE_RED_DEN = 3, 5

# At or below this a colour is black whatever its hue. The DS panel is dim and a
# nearly invisible dark red must not arrive as a vivid one -- it would be the
# brightest thing on a mostly black screen.
_TOO_DARK = 48


def quantize_color(name: str) -> str:
    """Collapse any pyte colour to one of PALETTE.

    pyte gives us either a colour name or a 6-digit hex string (from 256-colour
    and truecolour SGR sequences). Claude Code uses 256-colour, so the hex path
    is the common one, not an edge case.
    """
    if not name or name == "default":
        return "default"
    if name in PALETTE:
        return name
    alias = _COLOR_ALIASES.get(name)
    if alias is not None:
        return alias
    if len(name) == 6:
        try:
            r, g, b = (int(name[i : i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return "default"
        return _nearest(r, g, b)
    return "default"


def _nearest(r: int, g: int, b: int) -> str:
    """Snap an RGB triple to one palette entry.

    Hue first, brightness second -- the reverse of plain nearest-distance, and
    the reason this exists. Claude Code paints its mark in 256-colour 174,
    #d78787, and straight Euclidean distance made *white* the closest entry
    because white is light and so is the mark. Every coloured thing on the
    screen that was not fully saturated arrived on the handheld grey-white, and
    the mark in particular lost its colour entirely.

    So achromatic colours are the only ones allowed to become white or black,
    and anything with a hue competes only against entries that have one.
    """
    hi, lo = max(r, g, b), min(r, g, b)
    if hi <= _TOO_DARK:
        return "black"

    chroma = hi - lo
    if chroma * _ACHROMATIC_DEN < hi * _ACHROMATIC_NUM:
        return "white" if hi >= 96 else "black"

    if hi == r:
        hue = 60.0 * (((g - b) / chroma) % 6)
    elif hi == g:
        hue = 60.0 * (((b - r) / chroma) + 2)
    else:
        hue = 60.0 * (((r - g) / chroma) + 4)

    def _apart(target: float) -> float:
        d = abs(hue - target) % 360.0
        return min(d, 360.0 - d)

    best = min(_HUES, key=lambda entry: _apart(entry[1]))[0]
    if best == "red" and chroma * _PALE_RED_DEN < hi * _PALE_RED_NUM:
        best = "orange"
    return best


def fold_glyph(ch: str) -> str:
    """Reduce a character to something one DS cell can hold.

    Wide characters (CJK, most emoji) occupy two terminal columns. A grid that
    assumes one character per cell desynchronises permanently the first time one
    appears, so they are replaced rather than passed through. Combining marks
    collapse to nothing and would silently shift a row, so they go too.

    pyte keeps a combining mark in the cell of the character before it, so a
    cell can hold a cluster -- "x" + U+0301 -- and wcwidth() takes exactly one
    code point. A cluster becomes its composed character where Unicode has one
    ("e" + U+0301 is "é"), and otherwise its base alone: a DS cell holds one
    glyph, and the letter is what reads.
    """
    if not ch or ch == "\x00":
        return " "
    if len(ch) > 1:
        composed = unicodedata.normalize("NFC", ch)
        ch = composed if len(composed) == 1 else composed[0]
    width = wcwidth(ch)
    if width is None or width < 1:
        return REPLACEMENT
    if width > 1:
        return REPLACEMENT
    return ch


@dataclass(frozen=True, slots=True)
class Cell:
    """One character position. Frozen so grids can be compared cheaply."""

    char: str = " "
    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    reverse: bool = False

    def is_blank(self) -> bool:
        return (
            self.char == " "
            and self.bg == "default"
            and not self.reverse
        )


BLANK = Cell()


@dataclass(frozen=True, slots=True)
class CellChange:
    """A single cell that differs from the previous frame."""

    row: int
    col: int
    cell: Cell


class Grid:
    """An immutable COLS x ROWS snapshot of a terminal screen."""

    __slots__ = ("cells",)

    def __init__(self, cells: tuple[Cell, ...]) -> None:
        if len(cells) != COLS * ROWS:
            raise ValueError(
                f"grid must hold exactly {COLS * ROWS} cells, got {len(cells)}"
            )
        self.cells = cells

    @classmethod
    def blank(cls) -> "Grid":
        return cls(tuple(BLANK for _ in range(COLS * ROWS)))

    @classmethod
    def from_lines(cls, lines) -> "Grid":
        """Build a grid from any sequence of pyte buffer lines.

        Separate from from_pyte so a scrollback view can be assembled out of
        history without touching the live screen. pyte's own pagination
        (prev_page/next_page) mutates screen.buffer and snaps back to the bottom
        on every stream event, so paging with it would collapse the moment the
        child wrote another byte -- exactly while a long answer is being read.
        """
        out: list[Cell] = []
        for row in range(ROWS):
            line = lines[row] if row < len(lines) else {}
            for col in range(COLS):
                ch = line[col]
                out.append(
                    Cell(
                        char=fold_glyph(ch.data),
                        fg=quantize_color(ch.fg),
                        bg=quantize_color(ch.bg),
                        bold=bool(ch.bold),
                        reverse=bool(ch.reverse),
                    )
                )
        return cls(tuple(out))

    @classmethod
    def from_pyte(cls, screen) -> "Grid":
        """Snapshot a pyte screen, folding glyphs and quantizing colour."""
        buf = screen.buffer
        out: list[Cell] = []
        for row in range(ROWS):
            line = buf[row]
            for col in range(COLS):
                ch = line[col]
                out.append(
                    Cell(
                        char=fold_glyph(ch.data),
                        fg=quantize_color(ch.fg),
                        bg=quantize_color(ch.bg),
                        bold=bool(ch.bold),
                        reverse=bool(ch.reverse),
                    )
                )
        return cls(tuple(out))

    def at(self, row: int, col: int) -> Cell:
        return self.cells[row * COLS + col]

    def diff(self, previous: "Grid") -> list[CellChange]:
        """Cells that changed since `previous`, in row-major order."""
        changes: list[CellChange] = []
        prev = previous.cells
        cur = self.cells
        for idx in range(COLS * ROWS):
            if cur[idx] is not prev[idx] and cur[idx] != prev[idx]:
                changes.append(CellChange(idx // COLS, idx % COLS, cur[idx]))
        return changes

    def rows(self) -> Iterator[tuple[Cell, ...]]:
        for row in range(ROWS):
            yield self.cells[row * COLS : (row + 1) * COLS]

    def shifted(self, lines: int) -> "Grid":
        """This grid scrolled by `lines` (positive = up), vacated rows blanked.

        Used to compute what a client would hold *after* applying a SCROLL, so
        the diff that follows only covers what the scroll could not reproduce.
        """
        if lines == 0:
            return self
        out: list[Cell] = []
        for row in range(ROWS):
            src = row + lines
            if 0 <= src < ROWS:
                out.extend(self.cells[src * COLS : (src + 1) * COLS])
            else:
                out.extend(BLANK for _ in range(COLS))
        return Grid(tuple(out))

    def detect_scroll(
        self, previous: "Grid", max_lines: int = ROWS - 1, max_candidates: int = 4
    ) -> int:
        """Lines `previous` scrolled up to best explain this grid, else 0.

        A scroll measures as ~900 changed cells of 1008. Recognising it turns
        that into a 3-byte message plus a diff of the rows the scroll could not
        account for.

        The test is cost, not exact equality. A terminal that scrolls also writes
        into the row it just exposed, so the kept region never matches perfectly
        -- an earlier version required it to and consequently never fired on a
        real PTY. A scroll is claimed only when it genuinely sends fewer cells
        than the plain diff would, which makes the claim self-justifying.

        Only upward scrolls are considered: that is what a terminal does when
        output runs past the bottom, and it is the case worth optimising.
        """
        best_lines = 0
        best_cost = len(self.diff(previous))
        if best_cost == 0:
            return 0

        examined = 0
        top_row = self.cells[0:COLS]
        for lines in range(1, max_lines + 1):
            # Cheap prefilter: our top row must be where the scroll would put it.
            if top_row != previous.cells[lines * COLS : (lines + 1) * COLS]:
                continue
            cost = len(self.diff(previous.shifted(lines)))
            if cost < best_cost:
                best_lines, best_cost = lines, cost
            examined += 1
            if examined >= max_candidates:
                break
        return best_lines

    def to_text(self) -> str:
        """Plain text, no attributes. For tests and eyeballing."""
        return "\n".join(
            "".join(c.char for c in row).rstrip() for row in self.rows()
        )
