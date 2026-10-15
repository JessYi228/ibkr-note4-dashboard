"""One-bit drawing primitives shared by the IBKR and paper-account screens.

Pixel digits are adapted from ibkr-note4-dashboard (MIT, see NOTICE).
"""

from __future__ import annotations

import io
from datetime import date, datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from .errors import DashboardError

WIDTH, HEIGHT = 400, 300
FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/System/Library/Fonts/Supplemental")
PIXEL_DIGITS = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
}


def _font(size: int, bold: bool = False, path: str = "") -> ImageFont.FreeTypeFont:
    names = ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", "Arial Bold.ttf" if bold else "Arial.ttf"]
    candidates = [path] if path else []
    candidates += [f"{directory}/{name}" for directory in FONT_DIRS for name in names] + names
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    raise DashboardError("No TrueType font found; install DejaVu Sans or set DASHBOARD_FONT_PATH")


def load_fonts(path: str = "") -> dict[str, ImageFont.FreeTypeFont]:
    return {
        "r10": _font(10, path=path),
        "r12": _font(12, path=path),
        "bold10": _font(10, True, path),
        "bold12": _font(12, True, path),
        "bold16": _font(16, True, path),
        "bold25": _font(25, True, path),
    }


def new_canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("L", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=0, width=1)
    return image, draw


def to_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("1", dither=Image.Dither.NONE).save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], value: str, font, fill: int = 0) -> None:
    """Draw text, replacing digits with crisp 5x7 glyphs at small sizes."""
    if int(getattr(font, "size", 99)) > 12 or not any(c in PIXEL_DIGITS for c in value):
        draw.text(xy, value, font=font, fill=fill)
        return
    x, y = xy
    index = 0
    while index < len(value):
        character = value[index]
        if character not in PIXEL_DIGITS:
            end = index + 1
            while end < len(value) and value[end] not in PIXEL_DIGITS:
                end += 1
            draw.text((x, y), value[index:end], font=font, fill=fill)
            x += float(draw.textlength(value[index:end], font=font))
            index = end
            continue
        advance = float(draw.textlength(character, font=font))
        top, bottom = draw.textbbox((x, y), character, font=font)[1::2]
        glyph_x, glyph_y = round(x + (advance - 5) / 2), round(top + ((bottom - top) - 7) / 2)
        for row, bits in enumerate(PIXEL_DIGITS[character]):
            for column, bit in enumerate(bits):
                if bit == "1":
                    draw.point((glyph_x + column, glyph_y + row), fill=fill)
        x += advance
        index += 1


def right(draw: ImageDraw.ImageDraw, x_right: float, y: float, value: str, font, fill: int = 0) -> None:
    text(draw, (x_right - draw.textlength(value, font=font), y), value, font, fill)


def fit(draw: ImageDraw.ImageDraw, value: str, font, width: int) -> str:
    if draw.textlength(value, font=font) <= width:
        return value
    while value and draw.textlength(value + "...", font=font) > width:
        value = value[:-1]
    return value + "..."


def arrow(draw: ImageDraw.ImageDraw, x: int, cy: int, value: float | None, fill: int = 0) -> None:
    """Solid 7x5 triangle: up for gains, down for losses, dash for flat/unknown."""
    if value is not None and value > 0:
        draw.polygon([(x, cy + 2), (x + 6, cy + 2), (x + 3, cy - 2)], fill=fill)
    elif value is not None and value < 0:
        draw.polygon([(x, cy - 2), (x + 6, cy - 2), (x + 3, cy + 2)], fill=fill)
    else:
        draw.line((x + 1, cy, x + 5, cy), fill=fill)


def arrow_right(draw: ImageDraw.ImageDraw, x_right: float, y: float, value: float | None, label: str, font,
                fill: int = 0) -> None:
    """Right-aligned label with a direction triangle hugging its left side."""
    right(draw, x_right, y, label, font, fill)
    width = draw.textlength(label, font=font)
    arrow(draw, round(x_right - width) - 10, round(y + font.size * 0.6), value, fill)


def signed(value: float | None) -> str:
    if value is None:
        return "N/A"
    if abs(value) >= 10_000:
        return f"{value / 1_000:+,.1f}K"
    return f"{value:+,.2f}" if value else "0.00"


def pct(value: float | None, digits: int = 2) -> str:
    return "" if value is None else f"{value:+.{digits}f}%"


def change_pct(change: float | None, current: float | None) -> float | None:
    """Change relative to the value before the change (day open or cost)."""
    if change is None or current is None or not current - change:
        return None
    return change / abs(current - change) * 100


def axis(value: float, span: float) -> str:
    """Unitless tick label with enough precision that neighbouring ticks differ."""
    sign, absolute = ("-" if value < 0 else ""), abs(value)
    if absolute < 10_000:
        return f"{sign}{absolute:,.0f}"
    if absolute >= 1_000_000:
        return f"{sign}{absolute / 1_000_000:.{1 if span >= 400_000 else 2}f}M"
    return f"{sign}{absolute / 1_000:.{1 if span >= 400 else 2}f}K"


def _scale(values: list[float], box: tuple[int, int, int, int], low: float, high: float) -> list[tuple[int, int]]:
    left, top, right_, bottom = box
    span = high - low or 1.0
    count = max(len(values) - 1, 1)
    return [
        (round(left + i * (right_ - left) / count), round(bottom - (v - low) * (bottom - top) / span))
        for i, v in enumerate(values)
    ]


def _dotted_hline(draw: ImageDraw.ImageDraw, x0: int, x1: int, y: int, step: int = 3) -> None:
    for x in range(x0, x1 + 1, step):
        draw.point((x, y), fill=0)


def area_chart(draw: ImageDraw.ImageDraw, values: list[float], first: date | None, last: date | None,
               plot: tuple[int, int, int, int], fonts: dict) -> None:
    """Line + 25% dot-pattern area, hi/lo guides labelled on the left, dates underneath."""
    left, top, right_, bottom = plot
    if len(values) < 2:
        message = "NO HISTORY YET"
        draw.text(((left + right_ - draw.textlength(message, font=fonts["r10"])) / 2, (top + bottom) / 2 - 5),
                  message, font=fonts["r10"], fill=0)
        return
    low, high = min(values), max(values)
    pad = (high - low) * 0.08 or max(abs(high) * 0.001, 1.0)
    points = _scale(values, plot, low - pad, high + pad)

    mask = Image.new("1", (right_ - left + 1, bottom - top + 1), 0)
    ImageDraw.Draw(mask).polygon(
        [(x - left, y - top) for x, y in points] + [(right_ - left, bottom - top), (0, bottom - top)], fill=1
    )
    for y in range(0, bottom - top + 1, 2):
        for x in range((y // 2) % 2, right_ - left + 1, 2):
            if mask.getpixel((x, y)):
                draw.point((left + x, top + y), fill=0)

    y_high = min(y for _, y in points)
    y_low = max(y for _, y in points)
    for value, y in ((high, y_high), (low, y_low)):
        _dotted_hline(draw, left, right_, y)
        right(draw, left - 4, y - 5, axis(value, high - low), fonts["r10"])
    draw.line((left, bottom, right_, bottom), fill=0, width=1)
    draw.line(points, fill=0, width=2)
    x, y = points[-1]
    draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=255, outline=0, width=2)
    if first and last:
        text(draw, (left, bottom + 3), f"{first:%m/%d}", fonts["r10"])
        right(draw, right_, bottom + 3, f"{last:%m/%d}", fonts["r10"])


def sparkline(draw: ImageDraw.ImageDraw, values: list[float], box: tuple[int, int, int, int],
              baseline: float | None = None) -> None:
    """Tiny trend line; a dotted guide marks the starting capital when given."""
    if len(values) < 2:
        left, top, right_, bottom = box
        _dotted_hline(draw, left, right_, (top + bottom) // 2)
        return
    low, high = min(values), max(values)
    if baseline is not None:
        low, high = min(low, baseline), max(high, baseline)
    points = _scale(values, box, low, high)
    if baseline is not None:
        y = _scale([baseline], box, low, high)[0][1]
        _dotted_hline(draw, box[0], box[2], y, step=2)
    draw.line(points, fill=0, width=1)
    x, y = points[-1]
    draw.rectangle((x - 1, y - 1, x + 1, y + 1), fill=0)


def header(draw: ImageDraw.ImageDraw, title: str, data_day: date | None, fetched_at: datetime | None,
           tz: ZoneInfo, fonts: dict) -> None:
    draw.text((12, 8), title, font=fonts["bold16"], fill=0)
    # The trading day whose market close the numbers describe; UPDATED is when we fetched them.
    label = f"CLOSE {data_day:%m/%d %a}".upper() if data_day else "CLOSE N/A"
    right(draw, WIDTH - 12, 4, label, fonts["bold12"])
    updated = f"UPDATED {fetched_at.astimezone(tz):%m/%d %H:%M}" if fetched_at else "UPDATED N/A"
    right(draw, WIDTH - 12, 19, updated, fonts["r10"])
    draw.line((10, 33, WIDTH - 10, 33), fill=0, width=1)


def alert_header(draw: ImageDraw.ImageDraw, line1: str, line2: str, fonts: dict) -> None:
    draw.rectangle((0, 0, WIDTH - 1, 33), fill=0)
    text(draw, (8, 3), fit(draw, line1, fonts["bold12"], WIDTH - 16), fonts["bold12"], 255)
    text(draw, (8, 19), fit(draw, line2.upper(), fonts["r10"], WIDTH - 16), fonts["r10"], 255)
