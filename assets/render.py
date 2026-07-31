"""Regenerate the raster brand assets from assets/logo.svg.

    uv run --with pillow python assets/render.py assets

Writes logo-mark-32.png (favicon scale) and social-card.png (1280x640, GitHub's social
preview size). logo.svg is the single source for the mark: its paths are parsed here, so
editing the SVG is enough and the geometry is never restated.

Requires Pillow, and a font for the card's wordmark - the candidates below are macOS system
fonts; add your own path to FONTS on another platform.
"""
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONTS = [
    "/System/Library/Fonts/Supplemental/Futura.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
SS = 8  # supersample factor; the mark is downsampled from this for antialiasing
VIEWBOX = 100.0  # logo.svg's coordinate space


def load_mark(svg_path):
    """Parse logo.svg into (polygons, fill). Its paths are straight lines only (M/L/Z)."""
    svg = Path(svg_path).read_text()
    fill = re.search(r'fill="#([0-9A-Fa-f]{6})"', svg)
    colour = tuple(int(fill.group(1)[i : i + 2], 16) for i in (0, 2, 4)) if fill else (255, 87, 20)
    polys = []
    for d in re.findall(r'\sd="([^"]+)"', svg):
        if re.search(r"[CcSsQqTtAa]", d):
            raise ValueError(f"path has curves, which this renderer cannot draw: {d}")
        nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", d)]
        polys.append(list(zip(nums[::2], nums[1::2])))
    if not polys:
        raise ValueError(f"no paths found in {svg_path}")
    return polys, colour


def crown(polys, colour, size):
    """The mark, transparent-backed, at `size` px square."""
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for poly in polys:
        d.polygon([(x / VIEWBOX * big, y / VIEWBOX * big) for x, y in poly], fill=colour + (255,))
    return img.resize((size, size), Image.LANCZOS)


def font(size):
    for path in FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    raise SystemExit(f"no usable font found; add one to FONTS in {__file__}")


def main(out):
    out = Path(out)
    polys, colour = load_mark(out / "logo.svg")

    crown(polys, colour, 32).save(out / "logo-mark-32.png")

    W, H = 1280, 640
    card = Image.new("RGB", (W, H), (13, 13, 15))
    d = ImageDraw.Draw(card)
    mark = crown(polys, colour, 176)
    card.paste(mark, (W // 2 - 88, 104), mark)

    bold, body = font(96), font(30)

    def centre(text, y, f, fill):
        d.text(((W - d.textbbox((0, 0), text, font=f)[2]) / 2, y), text, font=f, fill=fill)

    centre("MARSHAL", 300, bold, (255, 255, 255))
    centre("Run a fleet of AI coding agents in parallel,", 432, body, (150, 150, 158))
    centre("in isolated git worktrees — and know what each one cost.", 474, body, (150, 150, 158))
    d.rectangle([(W / 2 - 40, 544), (W / 2 + 40, 548)], fill=colour)
    card.save(out / "social-card.png")
    print(f"wrote {out}/logo-mark-32.png and {out}/social-card.png")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent)
