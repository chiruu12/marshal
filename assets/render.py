"""Regenerate logo-mark-32.png from assets/logo.svg.

    uv run --with pillow python assets/render.py assets

logo.svg is the single source for the mark: its paths are parsed here, so editing the SVG is
enough and the geometry is never restated. Requires Pillow.

social-card.png is NOT generated - it is a designed asset committed as-is. This script used to
draw a rough approximation of it, which meant re-running the script silently replaced the real
card with the approximation.
"""
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw

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


def main(out):
    out = Path(out)
    polys, colour = load_mark(out / "logo.svg")
    crown(polys, colour, 32).save(out / "logo-mark-32.png")
    print(f"wrote {out}/logo-mark-32.png")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent)
