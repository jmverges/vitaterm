"""`dsterm camera --window`: the DS camera's live view in a window of its own.

Real pixels, scaled up whole-number times so they stay crisp, in Claude's
clay frame. It reads the same camera feed socket as `dsterm camera` and the
popups (server.py: <config_dir>/run/camera.sock, this user's only), shows
"camera off" while the camera is closed, and keeps waiting. Closing the
window ends it. Standard library only (tkinter).
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

from . import camera as dscamera

CLAY = "#d97757"
DEVICE = "#1a1a19"
INK = "#e8e6dc"
DIM = "#78766e"
SCALE = 4


def run(config_dir: Path) -> int:
    try:
        import tkinter as tk
    except ImportError:
        print("the camera window needs tkinter (on Linux: the python3-tk "
              "package; on WSL: Windows 11's WSLg). `dsterm camera` shows the "
              "same view in a terminal.", file=sys.stderr)
        return 1
    path = config_dir / "run" / "camera.sock"
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"no display for a window ({exc}); `dsterm camera` shows the "
              "same view in a terminal", file=sys.stderr)
        return 1
    root.title("DS camera")
    root.configure(bg=DEVICE)
    root.resizable(False, False)

    header = tk.Frame(root, bg=DEVICE)
    header.pack(fill="x", padx=14, pady=(12, 6))
    badge = tk.Label(header, text="○ CAMERA OFF", fg=DIM, bg=DEVICE,
                     font=("Menlo", 13, "bold"))
    badge.pack(side="left")
    detail = tk.Label(header, text="", fg=DIM, bg=DEVICE, font=("Menlo", 12))
    detail.pack(side="right")

    frame = tk.Frame(root, bg=CLAY, padx=3, pady=3)
    frame.pack(padx=14, pady=(0, 6))
    w0, h0 = 128 * SCALE, 96 * SCALE
    canvas = tk.Canvas(frame, width=w0, height=h0, bg="#000000",
                       highlightthickness=0)
    canvas.pack()
    footer = tk.Label(root, text="live, not recorded  ·  pictures are saved "
                      "only when you press A on the DS", fg=DIM, bg=DEVICE,
                      font=("Menlo", 11))
    footer.pack(padx=14, pady=(0, 12))

    state = {"sock": None, "reader": None, "image": None, "last": 0.0}

    def connect() -> None:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(path))
            s.setblocking(False)
            state["sock"], state["reader"] = s, dscamera.FeedReader()
        except OSError:
            state["sock"] = None
            detail.configure(text="no dsterm server here yet")

    def show(head: dict, rgb: bytes) -> None:
        w, h = head["w"], head["h"]
        scale = max(1, min(w0 // w, h0 // h))
        img = tk.PhotoImage(width=w, height=h)
        rows = []
        for y in range(h):
            row = rgb[y * w * 3:(y + 1) * w * 3]
            rows.append("{" + " ".join(
                f"#{row[i]:02x}{row[i + 1]:02x}{row[i + 2]:02x}"
                for i in range(0, len(row), 3)) + "}")
        img.put(" ".join(rows))
        big = img.zoom(scale, scale)
        state["image"] = big                      # keep a reference
        canvas.delete("all")
        canvas.create_image((w0 - w * scale) // 2, (h0 - h * scale) // 2,
                            anchor="nw", image=big)
        name = head.get("camera") or "camera"
        kind = "still" if head.get("still") else "live"
        badge.configure(text="● CAMERA ON", fg=CLAY)
        detail.configure(text=f"{name} · {kind} · {w}x{h}")

    def off() -> None:
        canvas.delete("all")
        canvas.create_text(w0 // 2, h0 // 2, text="camera off\n\ntap PHOTO on the DS",
                           fill=DIM, font=("Menlo", 14), justify="center")
        badge.configure(text="○ CAMERA OFF", fg=DIM)
        detail.configure(text="")

    def tick() -> None:
        if state["sock"] is None:
            if time.monotonic() - state["last"] > 2:
                state["last"] = time.monotonic()
                connect()
        else:
            try:
                data = state["sock"].recv(1 << 20)
            except (BlockingIOError, InterruptedError):
                data = None                       # nothing new yet
            except OSError:
                data = b""
            if data == b"":                       # the server went away
                state["sock"].close()
                state["sock"] = None
                off()
                detail.configure(text="waiting for the dsterm server")
            elif data:
                latest = None
                for head, rgb in state["reader"].feed(data):
                    latest = (head, rgb)
                if latest is not None:
                    if latest[0].get("on") and latest[1]:
                        show(*latest)
                    else:
                        off()
        root.after(50, tick)

    off()
    connect()
    root.after(50, tick)
    root.mainloop()
    return 0
