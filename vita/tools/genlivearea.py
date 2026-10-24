"""Draw the LiveArea images: vita/sce_sys/icon0.png and livearea/contents/*.

    python3 vita/tools/genlivearea.py DejaVuSansMono-Bold.ttf DejaVuSans.ttf
"""
import pathlib
import sys

from PIL import Image, ImageDraw, ImageFont

OUT = pathlib.Path(__file__).resolve().parents[1] / "sce_sys"
BG = (22, 18, 16)
ORANGE = (224, 140, 90)
TEXT = (230, 230, 230)
mono, sans = sys.argv[1], sys.argv[2]


def prompt(img, size, xy):
    d = ImageDraw.Draw(img)
    d.text(xy, ">_", font=ImageFont.truetype(mono, size), fill=ORANGE, anchor="mm")


def save(img, path):
    # The Vita wants 8-bit indexed PNGs for these.
    img.convert("RGB").quantize(colors=128).save(path, optimize=True)


icon = Image.new("RGB", (128, 128), BG)
prompt(icon, 64, (64, 62))
save(icon, OUT / "icon0.png")

bg = Image.new("RGB", (840, 500), BG)
d = ImageDraw.Draw(bg)
for y in range(0, 500, 20):
    d.line([(0, y), (840, y)], fill=(30, 25, 22))
save(bg, OUT / "livearea/contents/bg.png")

start = Image.new("RGB", (280, 158), BG)
prompt(start, 52, (140, 62))
ImageDraw.Draw(start).text((140, 122), "vitaterm", font=ImageFont.truetype(sans, 24),
                           fill=TEXT, anchor="mm")
save(start, OUT / "livearea/contents/startup.png")
