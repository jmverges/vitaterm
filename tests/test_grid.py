"""Tests for the cell format.

These cover the two decisions that are frozen into the wire format --
glyph folding and colour quantization -- plus the diff, since a wrong diff shows
up as permanent on-screen corruption rather than as an exception.
"""

import pyte
import pytest

from dsterm.grid import (
    BLANK,
    COLS,
    REPLACEMENT,
    ROWS,
    Cell,
    Grid,
    fold_glyph,
    quantize_color,
)


class TestFoldGlyph:
    def test_ascii_passes_through(self):
        for ch in "aZ0 ~!@#$%^&*()_+-=[]{}|;:'\",.<>/?":
            assert fold_glyph(ch) == ch

    def test_wide_characters_are_replaced(self):
        # A double-width glyph in a one-char-per-cell grid would shift every
        # following cell on the row and never resynchronise.
        for ch in ["世", "界", "あ", "한", "😀"]:
            assert fold_glyph(ch) == REPLACEMENT

    def test_combining_marks_are_replaced(self):
        assert fold_glyph("́") == REPLACEMENT   # combining acute
        assert fold_glyph("​") == REPLACEMENT   # zero-width space

    def test_a_cluster_becomes_one_character(self):
        # pyte keeps a combining mark in the cell before it, so a cell can hold
        # two code points; wcwidth() takes one and raised on the pair, which
        # took the server's loop down on the first "x" + U+0301 printed.
        assert fold_glyph("e\u0301") == "é"          # composes
        assert fold_glyph("x\u0301") == "x"          # no composed form: the base
        assert fold_glyph("a\u0301\u0302") == "á"    # composes what it can
        assert fold_glyph("q\u0308\u0323") == "q"
        assert fold_glyph("\u263a\ufe0f") == "\u263a"  # a variation selector
        assert fold_glyph("\u4e2d\u0301") == REPLACEMENT  # a wide base stays wide

    def test_null_and_empty_become_space(self):
        assert fold_glyph("") == " "
        assert fold_glyph("\x00") == " "

    def test_box_drawing_survives(self):
        # Claude Code's borders are single-width; they must not be mangled.
        for ch in "╭╮╰╯│─┌┐└┘├┤┬┴┼":
            assert fold_glyph(ch) == ch


class TestQuantizeColor:
    def test_palette_names_pass_through(self):
        for name in ["red", "green", "blue", "cyan", "magenta", "yellow", "white", "black"]:
            assert quantize_color(name) == name

    def test_default_and_empty(self):
        assert quantize_color("default") == "default"
        assert quantize_color("") == "default"

    def test_aliases_collapse(self):
        assert quantize_color("brown") == "yellow"
        assert quantize_color("brightred") == "red"
        assert quantize_color("brightblack") == "black"

    @pytest.mark.parametrize(
        "hexcode,expected",
        [
            ("ff0000", "red"),
            ("00ff00", "green"),
            ("0000ff", "blue"),
            ("ffffff", "white"),
            ("000000", "black"),
            ("00ffff", "cyan"),
        ],
    )
    def test_hex_snaps_to_nearest(self, hexcode, expected):
        # Claude Code emits 256-colour SGR, which pyte hands back as hex. This
        # is the common path, not an edge case.
        assert quantize_color(hexcode) == expected

    @pytest.mark.parametrize(
        "hexcode,expected,why",
        [
            # The regression this was written for. Claude Code paints its mark,
            # its box rules and its prompt in 256-colour 174 -- #d78787 -- and
            # plain Euclidean distance made white the nearest entry, because
            # white is light and so is the mark. It arrived on the handheld
            # colourless.
            ("d78787", "orange", "Claude's mark and box rules"),
            ("d97757", "orange", "Claude's brand orange"),
            # Only colours with no hue may become white or black.
            ("949494", "white", "dim terminal grey"),
            ("101010", "black", "near-invisible grey"),
            ("200000", "black", "too dark to render as a vivid red"),
            # A hue survives being pale: this is the class that used to wash
            # out to white. Colours picked to sit squarely on a hue -- a pale
            # colour exactly between two entries is genuinely ambiguous and
            # this deliberately does not pin which way it falls.
            ("87d787", "green", "pale green"),
            ("8787d7", "blue", "pale blue"),
            ("d78787", "orange", "pale red-orange is Claude's"),
        ],
    )
    def test_hue_beats_brightness(self, hexcode, expected, why):
        assert quantize_color(hexcode) == expected, why

    def test_malformed_hex_is_default(self):
        assert quantize_color("zzzzzz") == "default"

    def test_result_is_always_representable(self):
        from dsterm.grid import PALETTE
        for probe in ["brown", "ff8800", "123456", "nonsense", "", "brightcyan",
                      "d78787", "010101", "ffffff"]:
            assert quantize_color(probe) in PALETTE


class TestGrid:
    def test_blank_grid_has_exact_size(self):
        g = Grid.blank()
        assert len(g.cells) == COLS * ROWS

    def test_wrong_size_rejected(self):
        with pytest.raises(ValueError):
            Grid((BLANK,) * 10)

    def test_diff_of_identical_grids_is_empty(self):
        assert Grid.blank().diff(Grid.blank()) == []

    def test_diff_reports_row_and_col(self):
        cells = list(Grid.blank().cells)
        cells[3 * COLS + 7] = Cell(char="X", fg="red")
        changed = Grid(tuple(cells)).diff(Grid.blank())
        assert len(changed) == 1
        assert (changed[0].row, changed[0].col) == (3, 7)
        assert changed[0].cell.char == "X"

    def test_diff_detects_attribute_only_change(self):
        # Same character, different colour, must still be sent.
        base = list(Grid.blank().cells)
        base[0] = Cell(char="A", fg="red")
        after = list(base)
        after[0] = Cell(char="A", fg="blue")
        changed = Grid(tuple(after)).diff(Grid(tuple(base)))
        assert len(changed) == 1
        assert changed[0].cell.fg == "blue"

    def test_full_repaint_diff_is_bounded_by_grid_size(self):
        filled = Grid(tuple(Cell(char="#") for _ in range(COLS * ROWS)))
        assert len(filled.diff(Grid.blank())) == COLS * ROWS


class TestScrollDetection:
    """The optimization the first measurement earned: recognise a scroll instead of
    sending the ~900 changed cells it produces."""

    def _grid_of(self, lines: list[str]) -> Grid:
        cells = []
        for row in range(ROWS):
            text = (lines[row] if row < len(lines) else "").ljust(COLS)[:COLS]
            cells.extend(Cell(char=ch) for ch in text)
        return Grid(tuple(cells))

    @staticmethod
    def _line(i: int) -> str:
        # Dissimilar per row on purpose. Near-identical lines make a plain diff
        # look cheap, which both hides the cost of scrolling and stops the
        # cost-based detector from preferring a scroll -- correctly, but it
        # would make these tests measure the wrong thing.
        import hashlib

        return hashlib.sha256(str(i).encode()).hexdigest()[:COLS]

    def _screen(self, first: int) -> Grid:
        return self._grid_of([self._line(i) for i in range(first, first + ROWS)])

    def test_no_scroll_between_identical_grids(self):
        g = self._screen(0)
        assert g.detect_scroll(g) == 0

    def test_detects_single_line_scroll(self):
        assert self._screen(1).detect_scroll(self._screen(0)) == 1

    def test_detects_multi_line_scroll(self):
        assert self._screen(5).detect_scroll(self._screen(0)) == 5

    def test_detects_scroll_when_the_exposed_row_is_new(self):
        # The case that broke the first implementation: a real terminal writes
        # into the row it just uncovered, so the kept region never matches
        # exactly. Requiring exact equality meant this never fired on a PTY.
        before = self._screen(0)
        rows = [self._line(i) for i in range(1, ROWS)] + ["freshly written line"]
        after = self._grid_of(rows)
        assert after.detect_scroll(before) == 1

    def test_unrelated_screens_are_not_a_scroll(self):
        before = self._grid_of([f"alpha {i}" for i in range(ROWS)])
        after = self._grid_of([f"beta {i}" for i in range(ROWS)])
        assert after.detect_scroll(before) == 0

    def test_no_scroll_claimed_when_a_plain_diff_is_cheaper(self):
        # One character changed. Claiming a scroll here would cost more.
        before = self._screen(0)
        cells = list(before.cells)
        cells[100] = Cell(char="!")
        assert Grid(tuple(cells)).detect_scroll(before) == 0

    def test_shifted_reproduces_the_scrolled_region(self):
        before = self._screen(0)
        after = self._screen(3)
        # The scroll alone accounts for everything except the newly exposed
        # rows, which arrive as a small diff afterwards.
        residual = after.diff(before.shifted(3))
        assert len({c.row for c in residual}) == 3

    def test_shifted_blanks_vacated_rows(self):
        shifted = self._screen(0).shifted(2)
        assert all(c.char == " " for c in list(shifted.rows())[-1])

    def test_shift_by_zero_is_identity(self):
        g = self._grid_of(["x"])
        assert g.shifted(0) is g

    def test_scroll_saves_cells_versus_a_plain_diff(self):
        # The claim in one assertion: same visual result, far fewer cells.
        before, after = self._screen(0), self._screen(1)
        plain_cost = len(after.diff(before))
        scroll_cost = len(after.diff(before.shifted(1)))
        assert plain_cost > COLS * (ROWS - 1) * 0.8, "expected a near-full repaint"
        assert scroll_cost < plain_cost / 10


class TestFromPyte:
    def _screen(self, data: str) -> pyte.Screen:
        screen = pyte.Screen(COLS, ROWS)
        stream = pyte.Stream(screen)
        stream.feed(data)
        return screen

    def test_plain_text_lands_in_row_zero(self):
        grid = Grid.from_pyte(self._screen("hello"))
        assert grid.to_text().split("\n")[0] == "hello"

    def test_grid_is_always_full_size_regardless_of_output(self):
        grid = Grid.from_pyte(self._screen("x" * 5000))
        assert len(grid.cells) == COLS * ROWS

    def test_combining_marks_keep_the_row_in_step(self):
        # z + diaeresis + dot below composes as far as Unicode goes: z with
        # its dot below, the diaeresis left off.
        grid = Grid.from_pyte(self._screen("x\u0301y\u0301z\u0308\u0323 ok"))
        assert grid.to_text().split("\n")[0] == "xý\u1e93 ok"

    def test_wide_char_does_not_shift_the_row(self):
        # The failure this guards against is silent: one wide char and every
        # subsequent cell on the row is off by one, permanently.
        grid = Grid.from_pyte(self._screen("ab世cd"))
        row = "".join(c.char for c in next(grid.rows()))
        assert row.startswith("ab")
        assert len(row) == COLS

    def test_color_is_quantized_not_raw(self):
        from dsterm.grid import PALETTE
        grid = Grid.from_pyte(self._screen("\x1b[38;5;208mX\x1b[0m"))
        assert grid.at(0, 0).fg in PALETTE
