#!/usr/bin/env python3
"""
Generate Orbi PWA icons.

Produces:
  icon-192.png            (regular icon, used in browser tab + Android home)
  icon-512.png            (regular icon, large contexts)
  icon-maskable-512.png   (Android adaptive icon — content inside the safe zone)
  icon-favicon.png        (32x32 favicon)

Pure-Python — uses Pillow (the only external dep) and a built-in font fallback.
If Pillow isn't installed:
   pip3 install --break-system-packages pillow

Usage:
   python3 generate_icons.py
   python3 generate_icons.py --letter O           # default
   python3 generate_icons.py --letter J           # for a customer named Joe's
   python3 generate_icons.py --color "#4f8cff"   # primary color
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Pillow is required. Install with: pip3 install pillow")
    sys.exit(1)


HERE = Path(__file__).parent


def hex_to_rgb(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    return tuple(int(s[i:i+2], 16) for i in (0, 2, 4))  # type: ignore


def gradient_circle(size: int, color_a: tuple, color_b: tuple,
                    safe_zone_only: bool = False) -> Image.Image:
    """Render a round-filled square with a diagonal gradient.
    When safe_zone_only=True, the content fits inside the inner 80% (for maskable)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # For maskable icons, background fills the whole square (the platform crops it),
    # and the visible content lives inside the safe-zone (inner 80%).
    if safe_zone_only:
        # Draw a full-bleed gradient background.
        _fill_gradient(draw, (0, 0, size, size), color_a, color_b)
        return img

    # Non-maskable: draw a rounded-square or full circle
    _fill_gradient(draw, (0, 0, size, size), color_a, color_b)
    # Mask to a circle
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((0, 0, size, size), fill=255)
    img.putalpha(mask)
    return img


def _fill_gradient(draw, box, color_a, color_b):
    x0, y0, x1, y1 = box
    h = y1 - y0
    for y in range(y0, y1):
        t = (y - y0) / max(h - 1, 1)
        r = int(color_a[0] * (1 - t) + color_b[0] * t)
        g = int(color_a[1] * (1 - t) + color_b[1] * t)
        b = int(color_a[2] * (1 - t) + color_b[2] * t)
        draw.line([(x0, y), (x1, y)], fill=(r, g, b, 255))


def find_font(size: int) -> ImageFont.FreeTypeFont:
    """Find a bold sans-serif system font that supports a wide range of glyphs."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/Arial.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except (OSError, IOError):
                continue
    return ImageFont.load_default()


def draw_letter(img: Image.Image, letter: str, scale: float = 0.55) -> None:
    """Draw the letter centered, white, at `scale` of the image height."""
    draw = ImageDraw.Draw(img)
    size = img.size[0]
    font_size = int(size * scale)
    font = find_font(font_size)

    # Measure
    bbox = draw.textbbox((0, 0), letter, font=font, anchor="lt")
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    # Optical center adjustment for letters
    x = (size - w) // 2 - bbox[0]
    y = (size - h) // 2 - bbox[1] - int(size * 0.02)
    # Soft shadow
    draw.text((x + 2, y + 3), letter, font=font, fill=(0, 0, 0, 60))
    # Letter
    draw.text((x, y), letter, font=font, fill=(255, 255, 255, 255))


def build_icon(size: int, letter: str, color_a: tuple, color_b: tuple,
               maskable: bool = False) -> Image.Image:
    img = gradient_circle(size, color_a, color_b, safe_zone_only=maskable)
    if maskable:
        # Render letter in the inner 80% safe zone
        inner = int(size * 0.66)
        letter_img = Image.new("RGBA", (inner, inner), (0, 0, 0, 0))
        draw_letter(letter_img, letter, scale=0.62)
        offset = (size - inner) // 2
        img.alpha_composite(letter_img, (offset, offset))
    else:
        draw_letter(img, letter, scale=0.55)
    return img


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Orbi PWA icons")
    parser.add_argument("--letter", default="O",
                        help="Single letter to draw (default: O)")
    parser.add_argument("--color", default="#4f8cff",
                        help="Primary color hex (default: #4f8cff)")
    parser.add_argument("--color2", default="#8b5cf6",
                        help="Gradient end color hex (default: #8b5cf6)")
    parser.add_argument("--out", default=str(HERE),
                        help="Output directory (default: this dir)")
    args = parser.parse_args()

    letter = args.letter[:1].upper()
    color_a = hex_to_rgb(args.color)
    color_b = hex_to_rgb(args.color2)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"Generating icons in {out} (letter={letter}, "
          f"colors={args.color} → {args.color2})")

    targets = [
        ("icon-192.png",           192, False),
        ("icon-512.png",           512, False),
        ("icon-maskable-512.png",  512, True),
        ("icon-favicon.png",        32, False),
    ]
    for name, size, maskable in targets:
        img = build_icon(size, letter, color_a, color_b, maskable=maskable)
        path = out / name
        img.save(path, "PNG", optimize=True)
        print(f"  ✓ {name}  ({path.stat().st_size // 1024} KB)")

    print("\nDone. Reload your PWA to see the new icons.")
    print("On Android, you may need to uninstall + reinstall the home-screen shortcut.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
