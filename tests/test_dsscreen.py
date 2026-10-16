"""`dsterm view`'s pixel view: the DS's screens drawn as the card draws them,
at their real size and shape, and shown through a terminal's image protocol.
"""

import io
import re
import zlib
from pathlib import Path

import pytest

from dsterm import dsfont, dsscreen
from dsterm import viewer as dsviewer

REPO = Path(__file__).resolve().parent.parent


def _state(lines, cursor=(0, 0, False), fg="default", flags=0):
    rows = [[[(lines[r] if r < len(lines) else "").ljust(42)[:42], fg, "default", flags]]
            for r in range(24)]
    return {"rows": rows, "cursor": list(cursor)}


def _px(img: bytes, x: int, y: int) -> bytes:
    at = (y * dsscreen.SCREEN_W + x) * 3
    return img[at:at + 3]


class TestTheFontIsTheCards:
    def test_the_copy_matches_the_headers(self):
        if not (REPO / "dsclient" / "source" / "font.h").exists():
            pytest.skip("no card sources in this tree")
        import importlib.util
        spec = importlib.util.spec_from_file_location("genviewfont",
                                                      REPO / "tools" / "genviewfont.py")
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        fonts = gen.read_fonts()
        for size, (ascii_rows, ext_rows) in fonts.items():
            have_ascii, have_ext = dsfont.FONTS[size]
            assert [list(g) for g in have_ascii] == ascii_rows, size
            assert [list(g) for g in have_ext] == ext_rows, size


class TestThePixels:
    def test_a_glyph_lands_where_render_c_puts_it(self):
        # 42 columns of 6 fill 252 of 256 pixels: the card starts at x = 2.
        img = dsscreen.render_top(_state(["A"]))
        rows = dsfont.FONTS[(6, 8)][0][ord("A") - dsfont.FIRST]
        fg = dsscreen.top_fg("default", False)
        for y, bits in enumerate(rows):
            for x in range(6):
                lit = bits >> (5 - x) & 1
                assert _px(img, 2 + x, y) == (fg if lit else b"\0\0\0"), (x, y)
        assert _px(img, 0, 0) == _px(img, 1, 0) == b"\0\0\0"

    def test_bold_brightens_reverse_swaps_and_the_caret_swaps_again(self):
        plain = dsscreen.top_fg("red", False)
        bold = dsscreen.top_fg("red", True)
        assert bold != plain and all(b >= p for b, p in zip(bold, plain))
        # A space in reverse is a block of the foreground colour.
        img = dsscreen.render_top(_state([" "], flags=2))
        assert _px(img, 4, 4) == dsscreen.top_fg("default", False)
        # The caret over a reversed space swaps it back to black.
        img = dsscreen.render_top(_state([" "], cursor=(0, 0, True), flags=2))
        assert _px(img, 4, 4) == b"\0\0\0"

    def test_extended_glyphs_and_strangers(self):
        assert dsscreen.glyph_code("A") == 0x41
        assert dsscreen.glyph_code("─") == 0x80
        assert dsscreen.glyph_code("漢") == ord("?")

    def test_the_bottom_screen_and_its_viewfinder(self):
        rows = [[["X" * 42, "#ffffff", "#000000", 0]] for _ in range(24)]
        for r in range(4, 16):
            rows[r] = [["  " * 21, "#ffffff", "#000000", 4]]
        red = bytes((255, 0, 0)) * (8 * 6)
        img = dsscreen.render_bottom({"rows": rows}, ({"w": 8, "h": 6}, red))
        assert _px(img, 128, 80) == bytes((255, 0, 0))       # inside the viewfinder
        assert _px(img, 128, 4) != bytes((255, 0, 0))        # the X rows above it

    def test_png_is_a_png(self):
        data = dsscreen.png(bytes(range(12)), 2, 2)
        assert data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"IEND\xaeB`\x82")


class TestTheTerminal:
    @pytest.mark.parametrize("env,want", [
        ({"TERM": "xterm-kitty"}, dsscreen.KITTY),
        ({"TERM_PROGRAM": "ghostty"}, dsscreen.KITTY),
        ({"TERM_PROGRAM": "iTerm.app"}, dsscreen.ITERM),
        ({"TERM_PROGRAM": "WezTerm"}, dsscreen.ITERM),
        ({"TERM_PROGRAM": "Apple_Terminal"}, dsscreen.TEXT),
        ({"TERM_PROGRAM": "iTerm.app", "DSTERM_VIEW_GRAPHICS": "text"}, dsscreen.TEXT),
    ])
    def test_which_protocol(self, env, want):
        assert dsscreen.graphics(env) == want

    def test_a_kitty_image_is_chunked_and_whole(self):
        rgb = bytes(range(256)) * 576            # 256x192x3, incompressible-ish
        out = dsscreen.kitty_image(7, rgb, 256, 192, 60, 21)
        parts = re.findall(r"\x1b_G([^;]*);([^\x1b]*)\x1b\\", out)
        assert parts[0][0].startswith("a=T,f=24,o=z,s=256,v=192,i=7,p=1,c=60,r=21")
        assert [p[0].endswith("m=1") for p in parts[:-1]] == [True] * (len(parts) - 1)
        assert parts[-1][0].endswith("m=0")
        import base64
        assert zlib.decompress(base64.b64decode("".join(p[1] for p in parts))) == rgb

    def test_an_iterm_image_keeps_the_cursor(self):
        out = dsscreen.iterm_image(bytes(12), 2, 2, 10, 4)
        assert out.startswith("\x1b7\x1b]1337;File=inline=1;width=10;height=4;")
        assert out.endswith("\x07\x1b8")


def _pixel_view(aspect=2.1, gfx=dsscreen.KITTY):
    view = dsviewer.View(Path("/nonexistent"), out=io.StringIO())
    view.gfx, view.aspect = gfx, aspect
    view.status = {"card": True, "taps": True}
    return view


class TestThePixelView:
    @pytest.mark.parametrize("aspect", [1.8, 2.0, 2.3])
    @pytest.mark.parametrize("w,h", [(100, 30), (160, 50), (232, 63), (300, 90)])
    def test_both_screens_are_4_3_the_same_size_top_over_bottom(self, w, h, aspect):
        view = _pixel_view(aspect)
        lines = view.compose(w, h)
        assert len(lines) == h
        top, bottom = view.images[0], view.images[1]
        assert (top["cols"], top["rows"]) == (bottom["cols"], bottom["rows"])
        assert top["x"] == bottom["x"] and bottom["y"] == top["y"] + top["rows"] + 1
        shape = top["cols"] / (top["rows"] * aspect)
        assert abs(shape - 4 / 3) < 0.08, shape
        for im in view.images:
            assert im["x"] + im["cols"] <= w and im["y"] + im["rows"] <= h

    def test_a_click_is_a_tap_at_that_point_of_the_ds_screen(self):
        view = _pixel_view()
        sent = []
        view.conn = type("C", (), {"send": lambda self, o: sent.append(o)})()
        view.compose(160, 50)
        bx, by, cols, rows = view.bottom_rect
        view.mouse(0, bx, by, True)                          # top-left cell
        view.mouse(0, bx + cols - 1, by + rows - 1, True)    # bottom-right cell
        (a, b) = sent
        assert a["x"] < 256 / cols + 1 and a["y"] < 192 / rows + 1
        assert b["x"] > 256 - 256 / cols - 1 and b["y"] > 192 - 192 / rows - 1

    def test_images_are_sent_once_and_again_only_when_they_change(self):
        view = _pixel_view()
        view.top = _state(["hello"])
        sizes = [(160, 50)]
        import shutil
        real = shutil.get_terminal_size
        shutil.get_terminal_size = lambda *_: __import__("os").terminal_size(sizes[0])
        try:
            view.draw()
            first = view.out.getvalue()
            assert first.count("\x1b_Ga=T") == 2           # top and bottom
            view.out.seek(0); view.out.truncate()
            view.draw()
            assert "\x1b_Ga=T" not in view.out.getvalue()  # nothing changed
            view.top = _state(["hello", "world"])
            view.out.seek(0); view.out.truncate()
            view.draw()
            assert view.out.getvalue().count("\x1b_Ga=T") == 1   # the top only
        finally:
            shutil.get_terminal_size = real

    def test_too_small_for_pixels_falls_back_to_text(self):
        view = _pixel_view()
        view.compose(40, 16)
        assert view.images == []
