"""Deterministic 400x300 one-bit dashboard renderer.

Layout and pixel digits are adapted from ibkr-note4-dashboard (MIT,
see NOTICE). New here: the header always carries the broker data date and the
fetch time, and an inverted STALE banner replaces it when data is overdue.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from .errors import DashboardError
from .snapshot import Snapshot

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


@dataclass(frozen=True)
class Banner:
    """What the header says about freshness."""

    status: str  # "ok" | "stale"
    fetched_at: datetime | None
    expected_session: date | None = None
    sessions_behind: int = 0
    reason: str | None = None
    last_try_at: datetime | None = None


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


def _symbol(currency: str) -> str:
    return {"USD": "$", "HKD": "HK$", "EUR": "€", "GBP": "£", "CNY": "CN¥", "JPY": "¥"}.get(currency, f"{currency} ")


def money(value: float | None, currency: str, *, compact: bool = False, signed: bool = False,
          symbol: bool = True) -> str:
    if value is None:
        return "N/A"
    sign = "+" if signed and value > 0 else "-" if value < 0 else ""
    absolute = abs(value)
    if compact and absolute >= 1_000_000:
        body = f"{absolute / 1_000_000:.2f}M"
    elif compact and absolute >= 100_000:
        body = f"{absolute / 1_000:.1f}K"
    else:
        body = f"{absolute:,.2f}"
    return f"{sign}{_symbol(currency) if symbol else ''}{body}"


def _axis(value: float, span: float) -> str:
    """Unitless tick label with enough precision that neighbouring ticks differ."""
    sign, absolute = ("-" if value < 0 else ""), abs(value)
    if absolute < 10_000:
        return f"{sign}{absolute:.0f}"
    if absolute >= 1_000_000:
        return f"{sign}{absolute / 1_000_000:.{1 if span >= 400_000 else 2}f}M"
    return f"{sign}{absolute / 1_000:.{1 if span >= 400 else 2}f}K"


def _quantity(value: float) -> str:
    if abs(value) >= 10_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,.0f}" if value.is_integer() else f"{value:,.4f}".rstrip("0").rstrip(".")


def _text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], value: str, font, fill: int) -> None:
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


def _fit(draw: ImageDraw.ImageDraw, value: str, font, width: int) -> str:
    if draw.textlength(value, font=font) <= width:
        return value
    while value and draw.textlength(value + "...", font=font) > width:
        value = value[:-1]
    return value + "..."


def _arrow(draw: ImageDraw.ImageDraw, x: int, cy: int, value: float | None, fill: int = 0) -> None:
    """Solid 7x5 triangle: up for gains, down for losses, dash for flat/unknown."""
    if value is not None and value > 0:
        draw.polygon([(x, cy + 2), (x + 6, cy + 2), (x + 3, cy - 2)], fill=fill)
    elif value is not None and value < 0:
        draw.polygon([(x, cy - 2), (x + 6, cy - 2), (x + 3, cy + 2)], fill=fill)
    else:
        draw.line((x + 1, cy, x + 5, cy), fill=fill)


def _right(draw: ImageDraw.ImageDraw, right: float, y: float, value: str, font, fill: int = 0) -> None:
    _text(draw, (right - draw.textlength(value, font=font), y), value, font, fill)


def _signed(value: float | None) -> str:
    if value is None:
        return "N/A"
    if abs(value) >= 10_000:
        return f"{value / 1_000:+,.1f}K"
    return f"{value:+,.2f}" if value else "0.00"


def _pct(value: float | None) -> str:
    return "" if value is None else f"{value:+.2f}%"


def _change_pct(change: float | None, current: float | None) -> float | None:
    """Change relative to the value before the change (day open or cost)."""
    if change is None or current is None or not current - change:
        return None
    return change / abs(current - change) * 100


def _trend(draw: ImageDraw.ImageDraw, values: list[float], box: tuple[int, int, int, int], font) -> None:
    left, top, right, bottom = box
    draw.rectangle(box, outline=0, width=1)
    plot_left, plot_top, plot_right, plot_bottom = left + 5, top + 8, right - 5, bottom - 8
    if len(values) < 2:
        message = "NO HISTORY YET"
        width = draw.textlength(message, font=font)
        draw.text((left + (right - left - width) / 2, (top + bottom) / 2 - 5), message, font=font, fill=0)
        return
    low, high = min(values), max(values)
    span = high - low or 1.0
    for index in range(5):
        ratio = index / 4
        y = round(plot_top + ratio * (plot_bottom - plot_top))
        label = _axis(high - ratio * (high - low), high - low)
        _text(draw, (left - 4 - draw.textlength(label, font=font), y - 5), label, font, 0)
        for x in range(plot_left, plot_right + 1, 4):
            draw.point((x, y), fill=0)
    points = [
        (round(plot_left + i * (plot_right - plot_left) / (len(values) - 1)),
         round(plot_bottom - (v - low) * (plot_bottom - plot_top) / span))
        for i, v in enumerate(values)
    ]
    draw.line(points, fill=0, width=2)
    x, y = points[-1]
    draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=0)


def _header(draw: ImageDraw.ImageDraw, snapshot: Snapshot, banner: Banner, tz: ZoneInfo, fonts: dict) -> None:
    data_label = f"DATA {snapshot.report_date:%m/%d} {snapshot.report_date:%a}".upper()
    if banner.status == "stale":
        draw.rectangle((0, 0, WIDTH - 1, 33), fill=0)
        behind = f" ({banner.sessions_behind} SESSION{'S' if banner.sessions_behind != 1 else ''} BEHIND)" if banner.sessions_behind else ""
        _text(draw, (8, 3), f"! STALE  {data_label}{behind}", fonts["bold12"], 255)
        tried = f"{banner.last_try_at.astimezone(tz):%m/%d %H:%M}" if banner.last_try_at else "never"
        reason = _fit(draw, f"{banner.reason or 'no new report'} · TRIED {tried}".upper(), fonts["r10"], WIDTH - 16)
        _text(draw, (8, 19), reason, fonts["r10"], 255)
        return
    draw.text((12, 8), "IBKR PORTFOLIO", font=fonts["bold16"], fill=0)
    width = draw.textlength(data_label, font=fonts["bold12"])
    _text(draw, (WIDTH - 12 - width, 4), data_label, fonts["bold12"], 0)
    updated = f"UPDATED {banner.fetched_at.astimezone(tz):%m/%d %H:%M}" if banner.fetched_at else "UPDATED N/A"
    width = draw.textlength(updated, font=fonts["r10"])
    _text(draw, (WIDTH - 12 - width, 19), updated, fonts["r10"], 0)
    draw.line((10, 33, WIDTH - 10, 33), fill=0, width=1)


def render_png(snapshot: Snapshot, banner: Banner, *, tz: ZoneInfo, max_positions: int = 4, font_path: str = "") -> bytes:
    fonts = {
        "r10": _font(10, path=font_path),
        "r12": _font(12, path=font_path),
        "bold12": _font(12, True, font_path),
        "bold16": _font(16, True, font_path),
        "bold25": _font(25, True, font_path),
    }
    image = Image.new("L", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(image)
    currency = snapshot.currency
    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=0, width=1)
    _header(draw, snapshot, banner, tz, fonts)

    draw.text((12, 41), "NET LIQUIDATION", font=fonts["r10"], fill=0)
    draw.text((12, 54), money(snapshot.net_liquidation, currency, compact=True), font=fonts["bold25"], fill=0)
    draw.text((12, 86), "DAY", font=fonts["bold12"], fill=0)
    _arrow(draw, 44, 93, snapshot.daily_pnl)
    if snapshot.daily_pnl is None:
        _text(draw, (56, 88), "N/A - ADD DAILY FLEX QUERY", fonts["r10"], 0)
    else:
        day = f"{money(snapshot.daily_pnl, currency, compact=True, signed=True)}  {_pct(snapshot.daily_pnl_pct)}"
        _text(draw, (56, 86), day, fonts["bold12"], 0)
    unreal = money(snapshot.unrealized_pnl, currency, compact=True, signed=True, symbol=False)
    _text(draw, (12, 104), f"UNREAL  {unreal}", fonts["r10"], 0)
    _text(draw, (12, 120), f"CASH  {money(snapshot.cash, currency, compact=True, symbol=False)}", fonts["r10"], 0)

    title = f"{len(snapshot.nav_history)}D NAV ({currency})" if snapshot.nav_history else f"NAV ({currency})"
    _text(draw, (320 - draw.textlength(title, font=fonts["bold12"]) // 2, 39), title, fonts["bold12"], 0)
    _trend(draw, [value for _, value in snapshot.nav_history], (250, 54, 390, 142), fonts["r10"])

    draw.rectangle((10, 150, 390, 272), outline=0, width=1)
    draw.rectangle((10, 150, 390, 172), fill=0)
    positions = sorted(snapshot.positions, key=lambda p: abs(p.market_value or 0.0), reverse=True)[:min(max_positions, 4)]
    # Without the optional daily Flex query there is no per-position day P&L;
    # show unrealized P&L (vs cost) under its own heading instead of a column of N/A.
    daily = any(p.daily_pnl is not None for p in positions)
    heading = "DAY P&L" if daily else "UNREAL P&L"
    draw.text((18, 155), "STOCK", font=fonts["bold12"], fill=255)
    for right, label in ((122, "QTY"), (200, "PRICE"), (382, heading)):
        draw.text((right - draw.textlength(label, font=fonts["bold12"]), 155), label, font=fonts["bold12"], fill=255)
    if not positions:
        draw.text((18, 182), "No open positions", font=fonts["r12"], fill=0)
    for index, position in enumerate(positions):
        row_top = 176 + index * 23
        y = row_top + 2
        draw.text((18, y), position.symbol, font=fonts["bold12"], fill=0)
        _right(draw, 122, y, _quantity(position.quantity), fonts["r12"])
        _right(draw, 200, y, "N/A" if position.market_price is None else f"{position.market_price:,.2f}", fonts["r12"])
        change = position.daily_pnl if daily else position.unrealized_pnl
        amount = _signed(change)
        _right(draw, 310, y, amount, fonts["r12"])
        _arrow(draw, round(310 - draw.textlength(amount, font=fonts["r12"])) - 11, y + 8, change)
        _right(draw, 382, y + 1, _pct(_change_pct(change, position.market_value)), fonts["r10"])
        if index < len(positions) - 1:
            draw.line((18, row_top + 21, 382, row_top + 21), fill=0, width=1)

    change = snapshot.period_change_pct
    if change is not None:
        footer = f"{len(snapshot.nav_history)}D NAV CHANGE {change:+.2f}% (INCLUDES DEPOSITS/WITHDRAWALS)"
        _text(draw, (12, 280), _fit(draw, footer, fonts["r10"], WIDTH - 24), fonts["r10"], 0)

    buffer = io.BytesIO()
    image.convert("1", dither=Image.Dither.NONE).save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
