"""Draw the brand images Home Assistant shows for Mentat.

Run once from the repository root with the project's Python, then commit the PNGs:

    .venv/bin/python scripts/make_brand.py

Home Assistant 2026.3 and later serve a custom integration's images from the
`brand/` folder inside it (`homeassistant/components/brands`), so nothing has to
go to the brands repository. The rules follow that repository's: the icon is
square at 256 and 512 px, the logo landscape at 256 x 128 and 512 x 256, both on
a transparent background with no more margin than antialiasing needs. The dark
variants draw the same shapes in light colours for a dark theme.

The mark is a probability: a ring with three quarters of its arc drawn, round a
disc split into yes and no. Everything is drawn at four times the size and scaled
down, which is how PIL antialiases a shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BRAND = Path(__file__).resolve().parent.parent / "custom_components" / "mentat" / "brand"
SUPERSAMPLE = 4
# The share of the ring the arc covers, read clockwise from the top.
ARC_SHARE = 0.72
WORD = "Mentat"

type RGBA = tuple[int, int, int, int]


@dataclass(frozen=True)
class Palette:
    track: RGBA
    arc: RGBA
    yes: RGBA
    no: RGBA
    text: RGBA


LIGHT = Palette(
    track=(203, 213, 225, 255),
    arc=(13, 148, 136, 255),
    yes=(15, 23, 42, 255),
    no=(148, 163, 184, 255),
    text=(15, 23, 42, 255),
)
DARK = Palette(
    track=(71, 85, 105, 255),
    arc=(45, 212, 191, 255),
    yes=(241, 245, 249, 255),
    no=(100, 116, 139, 255),
    text=(241, 245, 249, 255),
)


def mark(side: int, palette: Palette) -> Image.Image:
    """The ring and the split disc, `side` pixels square, transparent around them."""
    big = side * SUPERSAMPLE
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    margin = round(big * 0.02)
    ring = round(big * 0.12)
    box = (margin, margin, big - margin, big - margin)
    draw.ellipse(box, outline=palette.track, width=ring)
    # PIL measures angles clockwise from three o'clock; the arc starts at twelve.
    draw.arc(box, start=-90, end=-90 + 360 * ARC_SHARE, fill=palette.arc, width=ring)
    inset = margin + ring + round(big * 0.08)
    disc = (inset, inset, big - inset, big - inset)
    draw.pieslice(disc, start=90, end=270, fill=palette.yes)
    draw.pieslice(disc, start=-90, end=90, fill=palette.no)
    return canvas.resize((side, side), Image.Resampling.LANCZOS)


def logo(width: int, height: int, palette: Palette) -> Image.Image:
    """The mark at the logo's height with the word beside it."""
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.alpha_composite(mark(height, palette), (0, 0))
    gap = round(height * 0.1)
    room = width - height - gap
    big = Image.new("RGBA", (room * SUPERSAMPLE, height * SUPERSAMPLE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)
    size = height * SUPERSAMPLE
    # The word fills the room beside the mark, and its capitals stay under half
    # the logo's height so it does not shout over the mark.
    max_width, max_height = room * SUPERSAMPLE, height * 0.45 * SUPERSAMPLE
    while True:
        font = ImageFont.load_default(size=size)
        left, top, right, bottom = draw.textbbox((0, 0), WORD, font=font)
        if right - left <= max_width and bottom - top <= max_height:
            break
        size -= SUPERSAMPLE
    x = -left
    y = (height * SUPERSAMPLE - (bottom - top)) // 2 - top
    draw.text((x, y), WORD, font=font, fill=palette.text)
    text = big.resize((room, height), Image.Resampling.LANCZOS)
    canvas.alpha_composite(text, (height + gap, 0))
    return canvas


def main() -> None:
    BRAND.mkdir(exist_ok=True)
    for prefix, palette in (("", LIGHT), ("dark_", DARK)):
        for suffix, scale in (("", 1), ("@2x", 2)):
            mark(256 * scale, palette).save(BRAND / f"{prefix}icon{suffix}.png")
            logo(256 * scale, 128 * scale, palette).save(BRAND / f"{prefix}logo{suffix}.png")


if __name__ == "__main__":
    main()
