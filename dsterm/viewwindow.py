"""`dsterm view --window`: the DS on this computer, as an emulator would show it.

Both screens at their real 256x192 pixels, top over bottom with the hinge
between, drawn with the card's own font and colours (dsscreen.py) and scaled
up whole-number times so they stay crisp. Typing goes to Claude; a click on
the bottom screen taps the DS at that pixel, when the DS allows it (SETTINGS
-> CUSTOMIZE -> VIEW TAPS); the camera shows in the bottom screen's
viewfinder while it is open, as on the DS. Any terminal can open it, image
support or not.

It reads the same two sockets as `dsterm view` (view.py). Standard library
only (tkinter), like `dsterm camera --window`.
"""

from __future__ import annotations

import base64
import select
import sys
import time
from pathlib import Path

from . import dsscreen
from .viewer import Camera, Lines, _message_screen

CLAY = "#d97757"
DEVICE = "#252421"
HINGE = "#1a1918"
INK = "#e8e6dc"
DIM = "#78766e"
W, H = dsscreen.SCREEN_W, dsscreen.SCREEN_H

# What a terminal would send for the keys that have no character of their own.
KEYS = {
    "Return": b"\r", "KP_Enter": b"\r", "BackSpace": b"\x7f", "Tab": b"\t",
    "ISO_Left_Tab": b"\x1b[Z", "Escape": b"\x1b",
    "Up": b"\x1b[A", "Down": b"\x1b[B", "Right": b"\x1b[C", "Left": b"\x1b[D",
    "Home": b"\x1b[H", "End": b"\x1b[F", "Delete": b"\x1b[3~",
    "Prior": b"\x1b[5~", "Next": b"\x1b[6~",
}
SHIFT = 0x1


def key_bytes(keysym: str, char: str, state: int) -> bytes:
    """A key press as the bytes a terminal sends for it, or b"" for none
    (a modifier on its own)."""
    if keysym == "Tab" and state & SHIFT:
        return b"\x1b[Z"
    if keysym in KEYS:
        return KEYS[keysym]
    if char:
        return char.encode("utf-8")
    return b""


def tap_point(x: int, y: int, scale: int, top: int) -> tuple[int, int] | None:
    """A click in the window as a point on the DS's bottom screen, or None
    when it is not on it."""
    px, py = x // scale, (y - top) // scale
    if 0 <= px < W and 0 <= py < H:
        return px, py
    return None


def pick_scale(screen_h: int) -> int:
    """The largest whole scale at which both screens and the window's own
    rows fit the display, between 1 and 4."""
    return max(1, min(4, (screen_h - 180) // (2 * H + 12)))


def run(config_dir: Path) -> int:
    try:
        import tkinter as tk
    except ImportError:
        print("the window needs tkinter (on Linux: the python3-tk package; on "
              "WSL: Windows 11's WSLg). `dsterm view` shows the same in a "
              "terminal.", file=sys.stderr)
        return 1
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"no display for a window ({exc}); `dsterm view` shows the same "
              "in a terminal", file=sys.stderr)
        return 1
    run_dir = config_dir / "run"
    root.title("DS Claude Terminal")
    root.configure(bg=DEVICE)
    root.resizable(False, False)
    scale = pick_scale(root.winfo_screenheight())
    hinge = 6 * scale
    bottom_y = H * scale + hinge

    header = tk.Frame(root, bg=DEVICE)
    header.pack(fill="x", padx=18, pady=(14, 8))
    tk.Label(header, text="◆ DS CLAUDE TERMINAL", fg=CLAY, bg=DEVICE,
             font=("Menlo", 13, "bold")).pack(side="left")
    badge = tk.Label(header, text="", fg=DIM, bg=DEVICE, font=("Menlo", 12))
    badge.pack(side="right")

    canvas = tk.Canvas(root, width=W * scale, height=2 * H * scale + hinge,
                       bg=HINGE, highlightthickness=0)
    canvas.pack(padx=18)
    footer = tk.Label(root, text="", fg=DIM, bg=DEVICE, font=("Menlo", 11))
    footer.pack(fill="x", padx=18, pady=(8, 14))

    st = {"conn": None, "cam": None, "top": None, "bottom": None, "status": {},
          "frame": None, "images": {}, "drawn": {}, "note": "", "note_until": 0.0,
          "last_try": 0.0}

    def connect() -> None:
        st["last_try"] = time.monotonic()
        try:
            st["conn"] = Lines(run_dir / "view.sock")
        except OSError:
            st["conn"] = None
        try:
            st["cam"] = Camera(run_dir / "camera.sock")
        except OSError:
            st["cam"] = None

    def say(text: str) -> None:
        st["note"], st["note_until"] = text, time.monotonic() + 3.0

    def show(image_id: str, rgb: bytes, y: int) -> None:
        key = hash(rgb)
        if st["drawn"].get(image_id) == key:
            return
        photo = tk.PhotoImage(data=base64.b64encode(dsscreen.png(rgb, W, H)),
                              format="png")
        if scale > 1:
            photo = photo.zoom(scale, scale)
        st["images"][image_id] = photo            # keep a reference
        canvas.delete(image_id)
        canvas.create_image(0, y, anchor="nw", image=photo, tags=(image_id,))
        st["drawn"][image_id] = key

    def draw() -> None:
        top = st["top"] or _message_screen(
            ["waiting for the server..." if st["conn"] is None else "waiting for the screen..."])
        show("top", dsscreen.render_top(top), 0)
        cam = st["frame"] if st["frame"] and st["frame"][0].get("on") and st["frame"][1] else None
        if st["bottom"]:
            bottom = dsscreen.render_bottom(st["bottom"], cam)
        else:
            bottom = dsscreen.render_top(_message_screen(
                ["the DS sends its bottom screen", "while this view is open"]))
        show("bottom", bottom, bottom_y)
        card = st["status"].get("card")
        badge.configure(text="● DS connected" if card else "○ no DS connected",
                        fg=CLAY if card else DIM)
        if st["note"] and time.monotonic() < st["note_until"]:
            footer.configure(text=st["note"], fg=CLAY)
        else:
            taps = st["status"].get("taps")
            back = (st["top"] or {}).get("back") or 0
            parts = ["type to Claude",
                     "click the bottom screen to tap" if taps else "taps are off on the DS"]
            if back:
                parts.insert(0, f"↑ {back} lines back")
            footer.configure(text="   ·   ".join(p for p in parts if p), fg=DIM)

    def on_key(event) -> str:
        data = key_bytes(event.keysym, event.char, event.state)
        if data and st["conn"] is not None:
            st["conn"].send({"t": "keys", "b64": base64.b64encode(data).decode()})
        return "break"

    def on_click(event) -> None:
        point = tap_point(event.x, event.y, scale, bottom_y)
        if point is None:
            return
        if not st["status"].get("taps"):
            say("taps are off: turn on SETTINGS → CUSTOMIZE → VIEW TAPS on the DS")
            return
        if st["conn"] is not None:
            st["conn"].send({"t": "tap", "x": point[0], "y": point[1]})

    def tick() -> None:
        if st["conn"] is None and time.monotonic() - st["last_try"] > 2:
            connect()
        socks = [s for s in (st["conn"], st["cam"]) if s is not None]
        ready = select.select(socks, [], [], 0)[0] if socks else []
        for s in ready:
            if s is st["conn"]:
                msgs = s.read()
                if msgs is None:
                    s.close()
                    st["conn"], st["top"], st["bottom"], st["status"] = None, None, None, {}
                    continue
                for m in msgs:
                    kind = m.get("t")
                    if kind == "top":
                        st["top"] = m
                    elif kind == "bottom":
                        st["bottom"] = m if m.get("rows") else None
                    elif kind == "status":
                        st["status"] = m
            elif s is st["cam"]:
                frames = s.read()
                if frames is None:
                    s.close()
                    st["cam"], st["frame"] = None, None
                    continue
                for head, rgb in frames:
                    st["frame"] = (head, rgb) if head.get("on") else None
        if st["cam"] is None and st["conn"] is not None \
                and time.monotonic() - st["last_try"] > 2:
            st["last_try"] = time.monotonic()
            try:
                st["cam"] = Camera(run_dir / "camera.sock")
            except OSError:
                pass
        draw()
        root.after(33, tick)

    root.bind("<Key>", on_key)
    canvas.bind("<Button-1>", on_click)
    canvas.create_rectangle(0, H * scale, W * scale, H * scale + hinge,
                            fill=HINGE, outline="")
    connect()
    draw()
    root.after(33, tick)
    root.focus_force()
    try:
        root.mainloop()
    finally:
        for s in (st["conn"], st["cam"]):
            if s is not None:
                s.close()
    return 0
