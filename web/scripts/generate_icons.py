#!/usr/bin/env python3
"""Generate the PWA icons into web/public/.

A flat near-black tile with a bold emerald ring, three-quarters filled — the
same mark the app itself draws. Run with `npm run icons` from web/.

    python scripts/generate_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

PUBLIC = Path(__file__).resolve().parent.parent / "public"

BACKGROUND = (7, 9, 12, 255)
RING_TRACK = (35, 43, 54, 255)
RING = (52, 211, 153, 255)

SUPERSAMPLE = 4
FILLED_FRACTION = 0.78


def draw_icon(size: int) -> Image.Image:
    canvas = size * SUPERSAMPLE
    image = Image.new("RGBA", (canvas, canvas), BACKGROUND)
    draw = ImageDraw.Draw(image)

    margin = canvas * 0.20
    box = (margin, margin, canvas - margin, canvas - margin)
    width = int(canvas * 0.105)

    draw.arc(box, start=0, end=360, fill=RING_TRACK, width=width)
    draw.arc(
        box,
        start=-90,
        end=-90 + 360 * FILLED_FRACTION,
        fill=RING,
        width=width,
    )

    # A short inner bar keeps the mark from reading as a plain circle at 60px.
    bar_half_width = canvas * 0.035
    bar_half_height = canvas * 0.115
    center = canvas / 2
    draw.rounded_rectangle(
        (
            center - bar_half_width,
            center - bar_half_height,
            center + bar_half_width,
            center + bar_half_height,
        ),
        radius=bar_half_width,
        fill=RING,
    )

    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    targets = {
        "icon-192.png": 192,
        "icon-512.png": 512,
        # iOS uses this for the home-screen icon and ignores the manifest icons.
        "apple-touch-icon.png": 180,
    }
    for filename, size in targets.items():
        icon = draw_icon(size)
        if filename == "apple-touch-icon.png":
            # iOS masks the corners itself and does not support transparency.
            icon = icon.convert("RGB")
        icon.save(PUBLIC / filename, "PNG")
        print(f"wrote {PUBLIC / filename} ({size}x{size})")


if __name__ == "__main__":
    main()
