"""Render Marshal's mark + social card from the same polygon source as assets/logo.svg."""
from PIL import Image, ImageDraw, ImageFont

ORANGE = (255, 87, 20)
# The three paths of assets/logo.svg, in its 100x100 viewBox. All straight lines (M/L/Z).
CROWN = [
    [(50, 22), (63, 68), (55, 68), (50, 57), (45, 68), (37, 68)],
    [(8, 38), (34.5, 68), (15, 68)],
    [(92, 38), (65.5, 68), (85, 68)],
]
SS = 8  # supersample factor for antialiasing


def crown(size, colour=ORANGE):
    """The mark, alpha-composited, at `size` px square."""
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for poly in CROWN:
        d.polygon([(x / 100 * big, y / 100 * big) for x, y in poly], fill=colour + (255,))
    return img.resize((size, size), Image.LANCZOS)


def font(path, size):
    return ImageFont.truetype(path, size)


if __name__ == "__main__":
    import sys

    out = sys.argv[1]

    # 1. Favicon-scale mark.
    crown(32).save(f"{out}/logo-mark-32.png")

    # 2. Social card: 1280x640, GitHub's recommended size.
    W, H = 1280, 640
    card = Image.new("RGB", (W, H), (13, 13, 15))
    d = ImageDraw.Draw(card)

    mark = crown(176)
    card.paste(mark, (int(W / 2 - 88), 104), mark)

    bold = font("/System/Library/Fonts/Supplemental/Futura.ttc", 96)
    body = font("/System/Library/Fonts/Supplemental/Futura.ttc", 30)

    def centre(text, y, f, fill):
        w = d.textbbox((0, 0), text, font=f)[2]
        d.text(((W - w) / 2, y), text, font=f, fill=fill)

    centre("MARSHAL", 300, bold, (255, 255, 255))
    centre("Run a fleet of AI coding agents in parallel,", 432, body, (150, 150, 158))
    centre("in isolated git worktrees — and know what each one cost.", 474, body, (150, 150, 158))
    d.rectangle([(W / 2 - 40, 544), (W / 2 + 40, 548)], fill=ORANGE)

    card.save(f"{out}/social-card.png")
    print("wrote logo-mark-32.png + social-card.png")
