"""Regenerate the raster app icons from the icon.svg geometry.

Dev-only helper (needs Pillow); the PNGs it writes are committed, so neither the
app nor the smoke test depends on it. iOS ignores SVG icons, so the home-screen
icon has to be a real PNG.

Usage: python tools/make_icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
BG = "#0c0d0c"
FG = "#00e05f"
SS = 4  # supersample factor, downscaled at the end for smooth edges
SIZES = {"apple-touch-icon.png": 180, "icon-192.png": 192, "icon-512.png": 512}

# same 512-unit box as icon.svg: the M strokes, then the bar under it
M_POINTS = [(158, 332), (158, 164), (256, 282), (354, 164), (354, 332)]
M_WIDTH = 44
BAR = (96, 380, 416, 408)


def render(px: int) -> Image.Image:
    img = Image.new("RGB", (512 * SS, 512 * SS), BG)
    d = ImageDraw.Draw(img)
    d.line([(x * SS, y * SS) for x, y in M_POINTS], fill=FG,
           width=M_WIDTH * SS, joint="curve")
    for x, y in M_POINTS:  # round off the joints the "curve" joint leaves open
        r = M_WIDTH * SS / 2
        d.ellipse([x * SS - r, y * SS - r, x * SS + r, y * SS + r], fill=FG)
    d.rectangle([c * SS for c in BAR], fill=FG)
    return img.resize((px, px), Image.LANCZOS)


if __name__ == "__main__":
    for name, px in SIZES.items():
        out = STATIC / name
        render(px).save(out, "PNG", optimize=True)
        print(f"wrote {out} ({px}x{px})")
