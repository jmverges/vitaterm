"""Render a Grid to a desktop terminal.

This is the stand-in for the DS screen. It deliberately draws only what the DS
could draw -- folded glyphs, quantized colour -- so that what you see here is
what the handheld would show, not a prettier superset of it.
"""

from __future__ import annotations

from .grid import COLS, ROWS, Cell, Grid

_FG = {
    "default": 39,
    "black": 30,
    "red": 31,
    "green": 32,
    "yellow": 33,
    "blue": 34,
    "magenta": 35,
    "cyan": 36,
    "white": 37,
}
_BG = {name: code + 10 for name, code in _FG.items()}

RESET = "\x1b[0m"


def _sgr(cell: Cell) -> str:
    codes = [0]
    if cell.bold:
        codes.append(1)
    if cell.reverse:
        codes.append(7)
    codes.append(_FG.get(cell.fg, 39))
    codes.append(_BG.get(cell.bg, 49))
    return "\x1b[" + ";".join(str(c) for c in codes) + "m"


def render_rows(grid: Grid) -> list[str]:
    """One escape-coded string per screen row, no trailing newline."""
    out: list[str] = []
    for row in grid.rows():
        parts: list[str] = []
        last: str | None = None
        for cell in row:
            sgr = _sgr(cell)
            if sgr != last:
                parts.append(sgr)
                last = sgr
            parts.append(cell.char)
        parts.append(RESET)
        out.append("".join(parts))
    return out


def framed(grid: Grid, title: str = "DS 42x24") -> str:
    """The grid inside a border, so the DS viewport is unambiguous on a big screen."""
    bar = "-" * COLS
    lines = [f"+-{bar}-+", f"| {title.ljust(COLS)[:COLS]} |", f"+-{bar}-+"]
    for row in render_rows(grid):
        lines.append(f"| {row} |")
    lines.append(f"+-{bar}-+")
    return "\n".join(lines)


def plain(grid: Grid) -> str:
    """Attribute-free text, for tests and diffing."""
    return grid.to_text()


__all__ = ["render_rows", "framed", "plain", "COLS", "ROWS"]
