"""The grid shape is configuration, not a constant.

The handheld's zoom level is a function of the font its renderer ships: a 6x8
font fills the 256x192 panel at 42x24, a smaller one fits more. That is a
client-side decision, so the server follows it rather than dictating it.

Geometry is read from the environment once at import, which keeps every
existing ``from .grid import COLS, ROWS`` working and lets pyte, the PTY
winsize, COLUMNS/LINES and tmux's -x/-y all follow from the same two numbers.
These tests run the module in a subprocess because that import happens once per
process and cannot be re-run in this one.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Report the values the whole server derives from the two variables, so a
# regression shows up as the wrong pyte size or winsize rather than only as the
# wrong constant.
PROBE = """
import json
from dsterm.grid import COLS, ROWS, Grid
from dsterm.protocol import encode_diff, CellChange
from dsterm.grid import BLANK
g = Grid.blank()
print(json.dumps({
    "cols": COLS,
    "rows": ROWS,
    "cells": len(g.cells),
    # The furthest cell must encode: row/col are one byte each on the wire.
    "max_cell_encodes": len(encode_diff([CellChange(ROWS - 1, COLS - 1, BLANK)])),
}))
"""


def _run(env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("DSTERM_COLS", None)
    env.pop("DSTERM_ROWS", None)
    env.update(env_extra)
    env["PYTHONPATH"] = str(REPO)
    return subprocess.run([sys.executable, "-c", PROBE], capture_output=True,
                          text=True, env=env, cwd=REPO)


def _geometry(env_extra: dict[str, str]) -> dict:
    out = _run(env_extra)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_default_is_the_shipped_font():
    """42x24 is the 6x8 font: 42*6 = 252 and 24*8 = 192 on a 256x192 panel."""
    assert _geometry({}) == {"cols": 42, "rows": 24, "cells": 1008,
                             "max_cell_encodes": 11}


def test_a_smaller_font_zooms_the_screen_out():
    """A 4x6 font is 64x32 -- the same panel, more than twice the text."""
    g = _geometry({"DSTERM_COLS": "64", "DSTERM_ROWS": "32"})
    assert (g["cols"], g["rows"]) == (64, 32)
    assert g["cells"] == 64 * 32
    # Frame size is bounded by CELLS_PER_MESSAGE, not by the grid, so the
    # furthest cell still encodes to exactly one small diff.
    assert g["max_cell_encodes"] == 11


def test_each_axis_moves_independently():
    g = _geometry({"DSTERM_COLS": "51"})
    assert (g["cols"], g["rows"]) == (51, 24)


@pytest.mark.parametrize("env,reason", [
    ({"DSTERM_COLS": "256"}, "a diff encodes col in one byte"),
    ({"DSTERM_ROWS": "256"}, "a diff encodes row in one byte"),
    ({"DSTERM_COLS": "0"}, "a zero-width screen has nothing to draw into"),
    ({"DSTERM_COLS": "19"}, "below the floor no TUI can lay itself out"),
    ({"DSTERM_ROWS": "7"}, "below the floor no TUI can lay itself out"),
    ({"DSTERM_COLS": "-42"}, "negative is not a width"),
    ({"DSTERM_COLS": "forty-two"}, "not an integer"),
    ({"DSTERM_COLS": "42.5"}, "not an integer"),
])
def test_an_unusable_geometry_is_refused_at_import(env, reason):
    """Refused loudly at startup, never clamped.

    The client validates row/col against its own bounds before touching a cell
    array it has no MMU to protect, so a server that quietly clamped to
    something the card disagreed with would produce corruption on the handheld
    rather than an error here.
    """
    out = _run(env)
    assert out.returncode != 0, f"{env} should be refused: {reason}"
    assert "DSTERM_" in out.stderr


def test_an_empty_value_is_treated_as_unset():
    """An exported-but-blank variable is a common shell accident."""
    assert _geometry({"DSTERM_COLS": ""})["cols"] == 42


def _client_make_var(cols: int, rows: int, name: str) -> str:
    """Resolve a Make variable without compiling the ARM9 client."""
    out = subprocess.run(
        ["make", "-s", "--no-print-directory", "-pn",
         f"GRID_COLS={cols}", f"GRID_ROWS={rows}", f"NAME={name}"],
        cwd=REPO / "dsclient", capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    # The ROM's own Makefile's value.  A fresh tree has no ARM7 core yet, and
    # its recursive make runs even under -n and prints a database of its own
    # (Makefile.arm7's BUILDDIR); make 3.81 quotes `Makefile', 4.x 'Makefile'.
    match = re.search(r"^# makefile \(from [`']Makefile', line \d+\)\n"
                      r"BUILDDIR\s*:=\s*(.+)$", out.stdout, re.MULTILINE)
    assert match, "Makefile must publish the resolved client build directory"
    return match.group(1)


def test_client_build_products_are_geometry_specific():
    """A geometry switch must not reuse another geometry's ARM9 objects.

    `make GRID_COLS=42 GRID_ROWS=24` previously said "Nothing to be done"
    after a 64x32 build and left the 64x32 ROM in place.  That turns a server
    rollback into an unsafe client/server mismatch, so geometry must be part
    of the build-product identity, not merely a compiler define.
    """
    readable = _client_make_var(42, 24, "dsterm")
    zoomed = _client_make_var(64, 32, "dsterm")
    assert readable != zoomed
    # The extension is part of the identity too (see the Makefile's EXT), so
    # a tag may carry a suffix; the geometry must be in it either way.
    assert re.search(r"dsterm-42x24(-\w+)?$", readable), readable
    assert re.search(r"dsterm-64x32(-\w+)?$", zoomed), zoomed
