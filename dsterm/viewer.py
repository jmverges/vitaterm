"""`dsterm view`: the DS on this computer, both screens, in a terminal.

    dsterm view

The top screen is the Claude session, as the DS shows it, and typing goes
straight to it -- what `dsterm attach` does, framed like the device instead of
a small island in a terminal of dots. In a terminal that can show images
(kitty, Ghostty, WezTerm, iTerm2) the two screens are drawn as the DS draws
them, pixel for pixel at 256x192 each, top over bottom like an emulator, and
scaled to the window; anywhere else, as text. The bottom screen is the DS's own,
mirrored as text while you watch (the card sends it only then); a click on it
taps the DS, when the DS allows it (SETTINGS -> CUSTOMIZE -> VIEW TAPS). While
the camera is open its live view gets a panel of its own, and a click on
WINDOW opens it in a separate window at full sharpness.

Ctrl-] is the one key the view keeps: Ctrl-] then q leaves, Ctrl-] then w
opens the camera window, Ctrl-] twice sends a Ctrl-] through.

It talks to the server over two Unix sockets only this user can open
(view.py): the screens and the input on one, the camera on the other.
"""

from __future__ import annotations

import base64
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import camera as dscamera
from . import dsscreen

# The look: Claude's clay on a near-black device, the screens' own colours
# inside them.
CLAY = (217, 119, 87)
INK = (232, 230, 220)
DIM = (120, 118, 110)
FRAME = (84, 81, 76)
PAGE = (19, 19, 18)          # the terminal behind everything
DEVICE = (37, 36, 33)        # a panel: the device's shell around a screen
SCREEN_BG = (0, 0, 0)

# The top screen's colour names (grid.py), drawn the way the DS draws them.
PALETTE = {
    "default": (201, 201, 201), "black": (0, 0, 0), "red": (210, 75, 75),
    "green": (79, 184, 79), "yellow": (214, 193, 74), "blue": (90, 127, 224),
    "magenta": (201, 95, 201), "cyan": (72, 195, 204), "white": (255, 255, 255),
    "orange": CLAY,
}

ESC = "\x1b"
CTRL_RBRACKET = b"\x1d"
_MOUSE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")


def fg(rgb) -> str:
    return f"{ESC}[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def bg(rgb) -> str:
    return f"{ESC}[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def hex_rgb(text: str) -> tuple[int, int, int]:
    return int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16)


# -- the connection -------------------------------------------------------------


class Lines:
    """A socket that speaks one JSON object a line."""

    def __init__(self, path: Path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(path))
        # Read only when select() says so; a send may wait a moment, so a
        # line is never cut in half.
        self.sock.settimeout(1.0)
        self.buf = bytearray()

    def fileno(self) -> int:
        return self.sock.fileno()

    def read(self) -> list[dict] | None:
        try:
            data = self.sock.recv(1 << 20)
        except (BlockingIOError, InterruptedError, socket.timeout):
            return []
        except OSError:
            return None
        if not data:
            return None
        self.buf += data
        out = []
        while True:
            nl = self.buf.find(b"\n")
            if nl < 0:
                return out
            raw = bytes(self.buf[:nl])
            del self.buf[:nl + 1]
            try:
                out.append(json.loads(raw))
            except ValueError:
                continue

    def send(self, obj: dict) -> None:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        try:
            self.sock.sendall(data)
        except OSError:
            pass                         # the read side notices it has gone

    def close(self) -> None:
        self.sock.close()


class Camera:
    """The camera feed socket (server.py), read without blocking."""

    def __init__(self, path: Path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(path))
        self.sock.setblocking(False)
        self.reader = dscamera.FeedReader()

    def fileno(self) -> int:
        return self.sock.fileno()

    def read(self):
        try:
            data = self.sock.recv(1 << 20)
        except (BlockingIOError, InterruptedError):
            return []
        except OSError:
            return None
        if not data:
            return None
        return self.reader.feed(data)

    def close(self) -> None:
        self.sock.close()


# -- the picture ----------------------------------------------------------------

_PAINT = re.compile(r"\x1b\[([0-9;]*)m|(.)", re.S)


class Canvas:
    """The terminal as a grid of cells, painted in order: what is drawn later
    covers what was drawn before, so a panel's fill goes down first and the
    screen inside it on top."""

    def __init__(self, width: int, height: int, fill=PAGE):
        self.w, self.h = width, height
        blank = (fg(INK), bg(fill), False, " ")
        self.cells = [[blank] * width for _ in range(height)]

    def ansi(self, y: int, x: int, text: str) -> None:
        """A string carrying its own SGR colours. A cell it gives no
        background keeps the one beneath."""
        if not 0 <= y < self.h:
            return
        row = self.cells[y]
        f, b, bold = fg(INK), None, False
        col = x
        for m in _PAINT.finditer(text):
            ch = m.group(2)
            if ch is None:
                code = m.group(1)
                if code.startswith("38;"):
                    f = m.group(0)
                elif code.startswith("48;"):
                    b = m.group(0)
                elif code == "1":
                    bold = True
                elif code == "22":
                    bold = False
                elif code in ("", "0"):
                    f, b, bold = fg(INK), None, False
                continue
            if 0 <= col < self.w:
                row[col] = (f, b if b is not None else row[col][1], bold, ch)
            col += 1

    def text(self, y: int, x: int, text: str, colour=INK, bold=False) -> None:
        self.ansi(y, x, fg(colour) + ("\x1b[1m" if bold else "") + text)

    def fill(self, y: int, x: int, w: int, h: int, colour) -> None:
        cell = (fg(INK), bg(colour), False, " ")
        for yy in range(max(0, y), min(self.h, y + h)):
            row = self.cells[yy]
            for xx in range(max(0, x), min(self.w, x + w)):
                row[xx] = cell

    def hole(self, y: int, x: int, w: int, h: int) -> None:
        """Cells an image covers: drawn as nothing, so the image stays."""
        for yy in range(max(0, y), min(self.h, y + h)):
            row = self.cells[yy]
            for xx in range(max(0, x), min(self.w, x + w)):
                row[xx] = None

    def segments(self, y: int) -> list[tuple[int, str]]:
        """Row `y` as (column, ANSI text) runs that skip the holes."""
        out, start = [], None
        row = self.cells[y]
        for x in range(self.w + 1):
            live = x < self.w and row[x] is not None
            if live and start is None:
                start = x
            elif not live and start is not None:
                out.append((start, self._paint(row[start:x])))
                start = None
        return out

    @staticmethod
    def _paint(cells) -> str:
        parts, last = [], None
        for f, b, bold, ch in cells:
            if (f, b, bold) != last:
                parts.append(f + b + ("\x1b[1m" if bold else "\x1b[22m"))
                last = (f, b, bold)
            parts.append(ch)
        return "".join(parts) + "\x1b[0m"

    def lines(self) -> list[str]:
        out = []
        for row in self.cells:
            row = [c if c is not None else (fg(INK), bg(SCREEN_BG), False, " ")
                   for c in row]
            parts, last = [], None
            for f, b, bold, ch in row:
                if (f, b, bold) != last:
                    parts.append(f + b + ("\x1b[1m" if bold else "\x1b[22m"))
                    last = (f, b, bold)
                parts.append(ch)
            out.append("".join(parts) + "\x1b[0m")
        return out


def panel(cv: Canvas, y: int, x: int, w: int, h: int, title: str,
          right: str = "", right_colour=DIM, pad: int = 2) -> None:
    """A solid panel in the device's colour, its title on its first row.

    No line-drawing frame: a box-drawing line sits in the middle of its
    cell, so around a filled panel it reads as a second edge half a cell
    inside the first. A fill's edge is the cell's edge, exact at any size."""
    cv.fill(y, x, w, h, DEVICE)
    cv.text(y, x + pad, title, CLAY, bold=True)
    right = right[:max(0, w - 2 * pad - len(title) - 2)]
    if right:
        cv.text(y, x + w - pad - len(right), right, right_colour)


def top_cells(state: dict | None, cols: int, rows: int) -> list[str]:
    """The top screen as ANSI lines of exactly `cols` cells."""
    lines = []
    if not state:
        msg = "waiting for the server..."
        for r in range(rows):
            text = msg if r == rows // 2 else ""
            lines.append(bg(SCREEN_BG) + fg(DIM) + text.center(cols)[:cols])
        return lines
    cur = state.get("cursor") or [0, 0, False]
    for r, runs in enumerate(state.get("rows", [])[:rows]):
        out = []
        col = 0
        for text, fgn, bgn, flags in runs:
            f = PALETTE.get(fgn, PALETTE["default"])
            b = SCREEN_BG if bgn == "default" else PALETTE.get(bgn, SCREEN_BG)
            if flags & 2:
                f, b = b, f
            for ch in text:
                if col >= cols:
                    break
                if cur[2] and r == cur[0] and col == cur[1]:
                    out.append(bg(INK) + fg(SCREEN_BG) + ch)
                else:
                    out.append(bg(b) + fg(f) + ("\x1b[1m" if flags & 1 else "")
                               + ch + ("\x1b[22m" if flags & 1 else ""))
                col += 1
        out.append(bg(SCREEN_BG) + " " * max(0, cols - col))
        lines.append("".join(out))
    while len(lines) < rows:
        lines.append(bg(SCREEN_BG) + " " * cols)
    return lines


def bottom_cells(state: dict | None, cols: int, rows: int,
                 camera_rows: dict[int, str] | None) -> list[str]:
    """The bottom screen, with the camera's rows drawn from its feed."""
    lines = []
    if not state:
        for r in range(rows):
            text = ""
            if r == rows // 2 - 1:
                text = "the DS sends its bottom screen"
            elif r == rows // 2:
                text = "while this view is open"
            lines.append(bg(SCREEN_BG) + fg(DIM) + text.center(cols)[:cols])
        return lines
    for r, runs in enumerate(state.get("rows", [])[:rows]):
        if camera_rows and r in camera_rows and any(run[3] & 4 for run in runs):
            lines.append(camera_rows[r])
            continue
        out = []
        col = 0
        for text, fgx, bgx, _flags in runs:
            out.append(bg(hex_rgb(bgx)) + fg(hex_rgb(fgx)) + text[:cols - col])
            col += len(text[:cols - col])
        out.append(bg(SCREEN_BG) + " " * max(0, cols - col))
        lines.append("".join(out))
    while len(lines) < rows:
        lines.append(bg(SCREEN_BG) + " " * cols)
    return lines


def _message_screen(lines: list[str], ui: bool = False) -> dict:
    """A screen saying `lines`, centred, in the DS's grey: what the pixel
    view shows before the server or the DS has sent anything."""
    rows = []
    first = 12 - len(lines) // 2
    for r in range(24):
        text = lines[r - first] if 0 <= r - first < len(lines) else ""
        rows.append([[text.center(42)[:42], "default", "default", 0]])
    return {"rows": rows, "cursor": [0, 0, False]}


class View:
    def __init__(self, config_dir: Path, out=None):
        self.config_dir = config_dir
        self.out = out or sys.stdout
        self.conn: Lines | None = None
        self.cam: Camera | None = None
        self.top: dict | None = None
        self.bottom: dict | None = None
        self.status: dict = {}
        self.frame: tuple[dict, bytes] | None = None
        self.prev: list[str] = []
        self.dirty = True
        self.bottom_rect = None      # (x, y, cols, rows) of the bottom cells, 1-based
        self.window_rect = None      # the WINDOW button
        self.prefix = False
        # How the screens are drawn: dsscreen.TEXT, KITTY or ITERM (run()
        # asks the terminal), and a cell's height over its width.
        self.gfx = dsscreen.TEXT
        self.aspect = 2.0
        self.images: list[dict] = []
        self.shown: dict[int, tuple] = {}
        self.canvas: Canvas | None = None
        self.bottom_px = False
        self.note = ""
        self.note_until = 0.0
        self.cam_tried = 0.0

    # -- connecting --------------------------------------------------------

    def connect(self) -> bool:
        run = self.config_dir / "run"
        try:
            self.conn = Lines(run / "view.sock")
        except OSError:
            self.conn = None
            return False
        self.connect_camera()
        return True

    def connect_camera(self) -> None:
        self.cam_tried = time.monotonic()
        try:
            self.cam = Camera(self.config_dir / "run" / "camera.sock")
        except OSError:
            self.cam = None

    # -- layout ------------------------------------------------------------

    # Spacing, airy first: (columns either side of a screen, rows between
    # a panel's title and its screen, rows under the screen, columns between
    # panels, blank rows around them). A terminal too small for the first
    # gets the second.
    SPACINGS = ((2, 1, 1, 3, 1), (1, 0, 0, 2, 0))

    def layout(self, width: int, height: int) -> dict | None:
        """Where everything goes, or None when the terminal is too small."""
        rows = self.top.get("rows") if self.top else None
        trows = len(rows) if rows else 24
        tcols = max((sum(len(r[0]) for r in row) for row in rows),
                    default=42) if rows else 42
        for bx, gy, under, gap, air in self.SPACINGS:
            tw, th = tcols + 2 * bx, 1 + gy + trows + under
            bw, bh = 42 + 2 * bx, 1 + gy + 24 + under
            room_w, room_h = width - 2, height - 2 - 2 * air
            side = {"top": (0, 0), "bottom": (tw + gap, 0),
                    "w": tw + gap + bw, "h": max(th, bh)}
            stack = {"top": (0, 0), "bottom": (0, th + air),
                     "w": max(tw, bw), "h": th + air + bh}
            for L in (stack, side):
                if L["w"] <= room_w and L["h"] <= room_h:
                    break
            else:
                continue
            L.update(tcols=tcols, trows=trows, tw=tw, th=th, bw=bw, bh=bh,
                     bezel=(bx, 1 + gy), gap=gap, air=air, cam=None)
            # The camera's panel, beside the screens and as tall as them: the
            # live view at 4:3 (a half-block cell is two square pixels), then
            # a line about it and the window button.
            room = room_w - L["w"] - gap
            inner_rows = L["h"] - 1 - gy - under
            irows = min(inner_rows - 4, 30)
            icols = min(irows * 8 // 3, room - 2 * bx)
            irows = min(irows, icols * 3 // 8)
            if icols >= 30:
                L["cam"] = {"x": L["w"] + gap, "w": icols + 2 * bx,
                            "img": (icols, irows)}
                L["w"] += gap + L["cam"]["w"]
            L["x0"] = max(1, (width - L["w"]) // 2)
            L["y0"] = max(0, (height - (L["h"] + 2 + 2 * air)) // 2)
            return L
        return None

    def compose(self, width: int, height: int) -> list[str]:
        self.images = []
        self.bottom_px = False
        if self.gfx != dsscreen.TEXT:
            lines = self.compose_pixels(width, height)
            if lines is not None:
                return lines
        cv = Canvas(width, height)
        self.canvas = cv
        L = self.layout(width, height)
        self.bottom_rect = None
        self.window_rect = None
        if L is None:
            msg = "make this terminal a little bigger: the screens need about 96x28"
            msg = msg[:max(0, width - 2)]
            cv.text(height // 2, max(0, (width - len(msg)) // 2), msg, DIM)
            return cv.lines()

        x0, y0, air = L["x0"], L["y0"], L["air"]
        bx, by = L["bezel"]
        py = y0 + 1 + air                       # the panels' top row

        # The header, over the panels and as wide as them.
        card = self.status.get("card")
        cv.text(y0, x0, "◆ DS CLAUDE TERMINAL", CLAY, bold=True)
        right = "● DS connected" if card else "○ no DS connected"
        cv.text(y0, x0 + L["w"] - len(right), right, CLAY if card else DIM)

        # The top screen: the session, as the DS shows it.
        tx, ty = x0 + L["top"][0], py + L["top"][1]
        back = (self.top or {}).get("back") or 0
        panel(cv, ty, tx, L["tw"], L["th"], "TOP SCREEN",
              f"↑ {back} lines back" if back else "Claude",
              CLAY if back else DIM, pad=bx)
        for i, row in enumerate(top_cells(self.top, L["tcols"], L["trows"])):
            cv.ansi(ty + by + i, tx + bx, row)

        # The bottom screen: the DS's own, mirrored.
        cam_on = bool(self.frame and self.frame[0].get("on") and self.frame[1])
        sx, sy = x0 + L["bottom"][0], py + L["bottom"][1]
        taps = self.status.get("taps")
        panel(cv, sy, sx, L["bw"], L["bh"], "BOTTOM SCREEN",
              "click to tap" if taps else "taps off on the DS", pad=bx)
        cam_rows = self._camera_rows_for_bottom(42) if cam_on and self.bottom else None
        for i, row in enumerate(bottom_cells(self.bottom, 42, 24, cam_rows)):
            cv.ansi(sy + by + i, sx + bx, row)
        self.bottom_rect = (sx + bx + 1, sy + by + 1, 42, 24)   # 1-based

        # The camera: a panel of its own while there is room for it.
        cam = L["cam"]
        if cam is not None:
            cx, cy, cw, ch = x0 + cam["x"], py, cam["w"], L["h"]
            icols, irows = cam["img"]
            ix, iy = cx + bx, cy + by
            if cam_on:
                header, rgb = self.frame
                name = (header.get("camera") or "camera").upper()
                kind = "STILL" if header.get("still") else "LIVE"
                panel(cv, cy, cx, cw, ch, "CAMERA", f"● {name} · {kind}", CLAY, pad=bx)
                for i, line in enumerate(dscamera.half_blocks(
                        rgb, header["w"], header["h"], icols, irows)):
                    cv.ansi(iy + i, ix, line)
                info = f"{header['w']}x{header['h']}  ·  live, never recorded"
            else:
                panel(cv, cy, cx, cw, ch, "CAMERA", "○ off", pad=bx)
                cv.fill(iy, ix, icols, irows, SCREEN_BG)
                words = [("the camera's live view shows here", DIM), ("", DIM),
                         ("open it on the DS:", DIM), ("tap PHOTO", INK)]
                top = iy + (irows - len(words)) // 2
                for i, (text, colour) in enumerate(words):
                    text = text[:icols]
                    cv.text(top + i, ix + (icols - len(text)) // 2, text, colour)
                info = "pictures are saved only when you press A"
            cv.text(iy + irows + 1, ix, info[:icols], DIM)
            # Plain glyphs only: one a font lacks can be drawn two cells wide,
            # and the panel's edge on that row would step out of line.
            button = "[ OPEN IN A WINDOW ↗ ]" if icols >= 24 else "[ WINDOW ↗ ]"
            yb = iy + irows + 3
            if yb < cy + ch:
                cv.text(yb, ix, button, CLAY, bold=True)
                self.window_rect = (ix + 1, yb + 1, len(button))

        # The footer: what the keys do, under the panels.
        fy = py + L["h"] + air
        if self.note and time.monotonic() < self.note_until:
            cv.text(fy, x0, self.note[:L["w"]], CLAY)
        else:
            x = x0
            hints = (("type", "to Claude"), ("click", "to tap the DS"),
                     ("ctrl-] e", "real pixels, in a window"),
                     ("ctrl-] w", "camera window"), ("ctrl-] q", "leave"))
            for i, (key, what) in enumerate(hints):
                sep = "   ·   " if i else ""
                if x + len(sep) + len(key) + 1 + len(what) > x0 + L["w"]:
                    break
                cv.text(fy, x, sep, FRAME)
                x += len(sep)
                cv.text(fy, x, key, INK, bold=True)
                cv.text(fy, x + len(key) + 1, what, DIM)
                x += len(key) + 1 + len(what)
        return cv.lines()

    # The pixel view: the device's shell with both screens at 4:3, the camera
    # beside it while there is room. Rows: the header, a blank, the shell
    # (a row of bezel, the top screen, the hinge, the bottom screen, a row of
    # bezel), a blank, the footer.
    PIXEL_CHROME = 7

    def compose_pixels(self, width: int, height: int) -> list[str] | None:
        a = self.aspect
        sh = (height - self.PIXEL_CHROME) // 2
        sw = round(sh * a * 4 / 3)
        if sw > width - 8:
            sw = width - 8
            sh = round(sw * 3 / (4 * a))
        if sh < 6 or sw < 16:
            return None
        shell_w, shell_h = sw + 6, 2 * sh + 3
        gap = 3
        room = width - shell_w - gap - 2
        cam_cols = min(sw, room - 4)
        cam_rows = round(cam_cols * 3 / (4 * a))
        cam = cam_cols >= 20 and cam_rows + 6 <= shell_h
        total_w = shell_w + (gap + cam_cols + 4 if cam else 0)
        x0 = max(0, (width - total_w) // 2)
        y0 = max(0, (height - (shell_h + 4)) // 2)

        cv = Canvas(width, height)
        self.canvas = cv
        card = self.status.get("card")
        cv.text(y0, x0, "◆ DS CLAUDE TERMINAL", CLAY, bold=True)
        right = "● DS connected" if card else "○ no DS connected"
        cv.text(y0, x0 + total_w - len(right), right, CLAY if card else DIM)

        # The shell: the device around both screens, the hinge between them.
        sy = y0 + 2
        cv.fill(sy, x0, shell_w, shell_h, DEVICE)
        tx, ty = x0 + 3, sy + 1
        bx, by = tx, ty + sh + 1
        hinge = "━" * min(12, sw // 3)
        cv.text(ty + sh, x0 + (shell_w - len(hinge)) // 2, hinge, FRAME)
        back = (self.top or {}).get("back") or 0
        if back:
            note = f"↑ {back} lines back"
            cv.text(ty + sh, x0 + shell_w - 2 - len(note), note, CLAY)

        cam_on = bool(self.frame and self.frame[0].get("on") and self.frame[1])
        top = self.top if self.top else _message_screen(["waiting for the server..."])
        bottom = self.bottom if self.bottom else _message_screen(
            ["the DS sends its bottom screen", "while this view is open"], ui=True)
        self._image(cv, 1, tx, ty, sw, sh, dsscreen.render_top(top))
        self._image(cv, 2, bx, by, sw, sh, dsscreen.render_bottom(
            bottom, self.frame if cam_on else None) if self.bottom else
            dsscreen.render_top(bottom))
        self.bottom_rect = (bx + 1, by + 1, sw, sh)        # 1-based
        self.bottom_px = True

        if cam:
            cx = x0 + shell_w + gap
            cv.fill(sy, cx, cam_cols + 4, shell_h, DEVICE)
            ix, iy = cx + 2, sy + 2
            if cam_on:
                header, rgb = self.frame
                name = (header.get("camera") or "camera").upper()
                kind = "STILL" if header.get("still") else "LIVE"
                cv.text(sy, cx + 2, "CAMERA", CLAY, bold=True)
                tag = f"● {name} · {kind}"[:max(0, cam_cols - 8)]
                cv.text(sy, cx + cam_cols + 2 - len(tag), tag, CLAY)
                self._image(cv, 3, ix, iy, cam_cols, cam_rows, rgb,
                            header["w"], header["h"])
                info = f"{header['w']}x{header['h']}  ·  live, never recorded"
            else:
                cv.text(sy, cx + 2, "CAMERA", CLAY, bold=True)
                cv.text(sy, cx + cam_cols + 2 - len("○ off"), "○ off", DIM)
                cv.fill(iy, ix, cam_cols, cam_rows, SCREEN_BG)
                words = [("the camera's live view shows here", DIM), ("", DIM),
                         ("open it on the DS:", DIM), ("tap PHOTO", INK)]
                top_y = iy + (cam_rows - len(words)) // 2
                for i, (text, colour) in enumerate(words):
                    text = text[:cam_cols]
                    cv.text(top_y + i, ix + (cam_cols - len(text)) // 2, text, colour)
                info = "pictures are saved only when you press A"
            cv.text(iy + cam_rows + 1, ix, info[:cam_cols], DIM)
            button = "[ OPEN IN A WINDOW ↗ ]" if cam_cols >= 24 else "[ WINDOW ↗ ]"
            yb = iy + cam_rows + 3
            if yb < sy + shell_h:
                cv.text(yb, ix, button, CLAY, bold=True)
                self.window_rect = (ix + 1, yb + 1, len(button))

        fy = sy + shell_h + 1
        if self.note and time.monotonic() < self.note_until:
            cv.text(fy, x0, self.note[:total_w], CLAY)
        else:
            taps = self.status.get("taps")
            hints = (("type", "to Claude"),
                     ("click", "the bottom screen to tap" if taps else "taps off on the DS"),
                     ("ctrl-] w", "camera window"), ("ctrl-] q", "leave"))
            x = x0
            for i, (key, what) in enumerate(hints):
                sep = "   ·   " if i else ""
                if x + len(sep) + len(key) + 1 + len(what) > x0 + total_w:
                    break
                cv.text(fy, x, sep, FRAME)
                x += len(sep)
                cv.text(fy, x, key, INK, bold=True)
                cv.text(fy, x + len(key) + 1, what, DIM)
                x += len(key) + 1 + len(what)
        return cv.lines()

    def _image(self, cv: Canvas, image_id: int, x: int, y: int, cols: int,
               rows: int, rgb: bytes, w: int = dsscreen.SCREEN_W,
               h: int = dsscreen.SCREEN_H) -> None:
        cv.hole(y, x, cols, rows)
        self.images.append({"id": image_id, "x": x, "y": y, "cols": cols,
                            "rows": rows, "rgb": rgb, "w": w, "h": h})

    def _camera_rows_for_bottom(self, cols: int) -> dict[int, str]:
        """The live view drawn into the bottom screen's viewfinder rows."""
        rows = [r for r, runs in enumerate(self.bottom.get("rows", []))
                if any(run[3] & 4 for run in runs)]
        if not rows:
            return {}
        header, rgb = self.frame
        lines = dscamera.half_blocks(rgb, header["w"], header["h"], cols, len(rows))
        return {r: bg(SCREEN_BG) + line for r, line in zip(rows, lines)}

    # -- drawing -------------------------------------------------------------

    def draw(self) -> None:
        size = shutil.get_terminal_size((120, 40))
        lines = self.compose(size.columns, size.lines)
        if not self.images:
            if self.shown:
                # The pixel view has gone (the window got too small): take
                # the pictures down before the text goes where they were.
                if self.gfx == dsscreen.KITTY:
                    self.out.write(dsscreen.kitty_clear())
                self.shown = {}
                self.prev = []
            whole = len(self.prev) != len(lines)
            buf = [f"{ESC}[0m{ESC}[2J"] if whole else []
            for i, line in enumerate(lines):
                if whole or self.prev[i] != line:
                    buf.append(f"{ESC}[{i + 1};1H{line}")
            self.prev = lines
            if buf:
                self.out.write("".join(buf))
                self.out.flush()
            return
        cv = self.canvas
        rows = [cv.segments(y) for y in range(cv.h)]
        whole = len(self.prev) != len(rows)
        buf = []
        if whole:
            buf.append(f"{ESC}[0m{ESC}[2J")
            if self.gfx == dsscreen.KITTY:
                buf.append(dsscreen.kitty_clear())
            self.shown = {}
        for i, segs in enumerate(rows):
            if whole or self.prev[i] != segs:
                buf.append(f"{ESC}[{i + 1};1H{ESC}[0m{bg(PAGE)}{ESC}[2K")
                for x, text in segs:
                    buf.append(f"{ESC}[{i + 1};{x + 1}H{text}")
                # A rewritten row in iTerm2 loses the picture under it.
                if self.gfx == dsscreen.ITERM:
                    for im in self.images:
                        if im["y"] <= i < im["y"] + im["rows"]:
                            self.shown.pop(im["id"], None)
        self.prev = rows
        live = set()
        for im in self.images:
            live.add(im["id"])
            key = (im["x"], im["y"], im["cols"], im["rows"], hash(im["rgb"]))
            if self.shown.get(im["id"]) == key:
                continue
            buf.append(f"{ESC}[{im['y'] + 1};{im['x'] + 1}H")
            if self.gfx == dsscreen.KITTY:
                buf.append(dsscreen.kitty_image(im["id"], im["rgb"], im["w"], im["h"],
                                                im["cols"], im["rows"]))
            else:
                buf.append(dsscreen.iterm_image(im["rgb"], im["w"], im["h"],
                                                im["cols"], im["rows"]))
            self.shown[im["id"]] = key
        for gone in set(self.shown) - live:
            if self.gfx == dsscreen.KITTY:
                buf.append(f"\x1b_Ga=d,d=i,i={gone},q=2\x1b\\")
            self.shown.pop(gone)
        if buf:
            self.out.write("".join(buf))
            self.out.flush()

    # -- input -----------------------------------------------------------------

    def say(self, text: str) -> None:
        self.note = text
        self.note_until = time.monotonic() + 3.0
        self.dirty = True

    def key_bytes(self, data: bytes) -> bool:
        """Handle what was typed. Returns False to leave."""
        while data:
            m = _MOUSE.search(data)
            if m and m.start() == 0:
                self.mouse(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                           m.group(4) == b"M")
                data = data[m.end():]
                continue
            cut = m.start() if m else len(data)
            chunk, data = data[:cut], data[cut:]
            send = bytearray()
            for b in chunk:
                if self.prefix:
                    self.prefix = False
                    if b in (ord("q"), ord("Q")):
                        return False
                    if b in (ord("w"), ord("W")):
                        self.open_window()
                        continue
                    if b in (ord("e"), ord("E")):
                        self.open_emulator()
                        continue
                    if b == 0x1d:
                        send.append(0x1d)
                    continue
                if b == 0x1d:
                    self.prefix = True
                    self.say("ctrl-]  q leave   e emulator window   w camera window   "
                             "ctrl-] send ctrl-]")
                    continue
                send.append(b)
            if send and self.conn is not None:
                self.conn.send({"t": "keys",
                                "b64": base64.b64encode(bytes(send)).decode()})
        return True

    def mouse(self, button: int, x: int, y: int, press: bool) -> None:
        if not press or button & 0b11 != 0:      # left button presses only
            return
        if self.window_rect:
            wx, wy, wn = self.window_rect
            if y == wy and wx <= x < wx + wn:
                self.open_window()
                return
        if self.bottom_rect:
            bx, by, cols, rows = self.bottom_rect
            col, row = x - bx, y - by
            if 0 <= col < cols and 0 <= row < rows:
                if not self.status.get("taps"):
                    self.say("taps are off: turn on SETTINGS → CUSTOMIZE → VIEW TAPS on the DS")
                    return
                if self.conn is not None:
                    if self.bottom_px:
                        # A cell of the picture, at its centre, in DS pixels.
                        x = int((col + 0.5) * dsscreen.SCREEN_W / cols)
                        y = int((row + 0.5) * dsscreen.SCREEN_H / rows)
                    else:
                        x, y = col * 6 + 3, row * 8 + 4
                    self.conn.send({"t": "tap", "x": x, "y": y})

    def open_emulator(self) -> None:
        """Both screens at real pixels in a window of their own (viewwindow.py),
        for a terminal that cannot show images."""
        try:
            subprocess.Popen([sys.executable, "-P", "-m", "dsterm.cli", "view",
                              "--window"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, cwd=str(self.config_dir))
            self.say("opening the emulator window...")
        except OSError as exc:
            self.say(f"could not open the emulator window: {exc}")

    def open_window(self) -> None:
        try:
            subprocess.Popen([sys.executable, "-P", "-m", "dsterm.cli", "camera",
                              "--window"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True,
                             cwd=str(self.config_dir))
            self.say("opening the camera window...")
        except OSError as exc:
            self.say(f"could not open the camera window: {exc}")

    # -- the loop --------------------------------------------------------------

    def pump(self, timeout: float) -> bool:
        """One round of reading. Returns False when the server has gone."""
        if self.cam is None and time.monotonic() - self.cam_tried > 2.0:
            self.connect_camera()
        fds = [sys.stdin]
        if self.conn is not None:
            fds.append(self.conn)
        if self.cam is not None:
            fds.append(self.cam)
        ready, _, _ = select.select(fds, [], [], timeout)
        for r in ready:
            if r is sys.stdin:
                data = os.read(sys.stdin.fileno(), 4096)
                if not data or not self.key_bytes(data):
                    raise SystemExit(0)
            elif r is self.conn:
                msgs = self.conn.read()
                if msgs is None:
                    return False
                for m in msgs:
                    kind = m.get("t")
                    if kind == "top":
                        self.top = m
                    elif kind == "bottom":
                        self.bottom = m if m.get("rows") else None
                    elif kind == "status":
                        self.status = m
                    self.dirty = True
            elif r is self.cam:
                frames = self.cam.read()
                if frames is None:
                    self.cam.close()
                    self.cam = None
                    self.frame = None
                    self.dirty = True
                    continue
                for header, rgb in frames:
                    self.frame = (header, rgb) if header.get("on") else None
                    self.dirty = True
        return True


def run(config_dir: Path) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("dsterm view needs a terminal", file=sys.stderr)
        return 1
    view = View(config_dir)
    view.gfx = dsscreen.graphics()
    view.aspect = dsscreen.cell_aspect(sys.stdout.fileno())
    if not view.connect():
        print("no dsterm server on this computer answered (is it running? "
              "dsterm start)", file=sys.stderr)
        return 1
    import termios
    import tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    out = sys.stdout
    resized = [True]
    signal.signal(signal.SIGWINCH, lambda *_: resized.__setitem__(0, True))
    try:
        tty.setraw(fd)
        out.write(f"{ESC}[?1049h{ESC}[?25l{ESC}[?1000h{ESC}[?1006h")
        out.flush()
        last = 0.0
        while True:
            if not view.pump(0.05):
                break
            now = time.monotonic()
            if resized[0]:
                resized[0] = False
                view.prev = []
                view.aspect = dsscreen.cell_aspect(sys.stdout.fileno())
                view.dirty = True
            if view.dirty and now - last >= 1 / 30:
                view.dirty = False
                last = now
                view.draw()
            if view.note and now >= view.note_until:
                view.note = ""
                view.dirty = True
    except SystemExit:
        pass
    finally:
        if view.gfx == dsscreen.KITTY:
            out.write(dsscreen.kitty_clear())
        out.write(f"{ESC}[?1006l{ESC}[?1000l{ESC}[0m{ESC}[?25h{ESC}[?1049l")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        if view.conn is not None:
            view.conn.close()
        if view.cam is not None:
            view.cam.close()
    return 0
