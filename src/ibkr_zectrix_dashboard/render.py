"""IBKR portfolio screen (400x300, one bit).

Layout is adapted from ibkr-note4-dashboard (MIT, see NOTICE). New here: the
header always carries the broker data date and the fetch time, and an inverted
STALE banner replaces it when data is overdue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import drawing as d
from .snapshot import Position, Snapshot


@dataclass(frozen=True)
class Banner:
    """What the header says about freshness."""

    status: str  # "ok" | "stale"
    fetched_at: datetime | None
    expected_session: date | None = None
    sessions_behind: int = 0
    reason: str | None = None
    last_try_at: datetime | None = None


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


def _quantity(value: float) -> str:
    if abs(value) >= 10_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,.0f}" if value.is_integer() else f"{value:,.4f}".rstrip("0").rstrip(".")


def select_positions(positions: list[Position], daily: bool, limit: int) -> list[Position]:
    """The biggest movers: largest absolute % change (day, or vs cost without daily data)."""
    def magnitude(position: Position) -> tuple[float, float]:
        change = position.daily_pnl if daily else position.unrealized_pnl
        percent = d.change_pct(change, position.market_value)
        return (abs(percent) if percent is not None else -1.0, abs(position.market_value or 0.0))

    return sorted(positions, key=magnitude, reverse=True)[:limit]


def _header(draw, snapshot: Snapshot, banner: Banner, tz: ZoneInfo, fonts: dict) -> None:
    if banner.status != "stale":
        d.header(draw, "IBKR PORTFOLIO", snapshot.report_date, banner.fetched_at, tz, fonts)
        return
    count = banner.sessions_behind
    behind = f" ({count} SESSION{'S' if count != 1 else ''} BEHIND)" if count else ""
    tried = f"{banner.last_try_at.astimezone(tz):%m/%d %H:%M}" if banner.last_try_at else "never"
    d.alert_header(draw, f"! STALE  CLOSE {snapshot.report_date:%m/%d %a}{behind}".upper(),
                   f"{banner.reason or 'no new report'} · TRIED {tried}", fonts)


def render_png(snapshot: Snapshot, banner: Banner, *, tz: ZoneInfo, max_positions: int = 4, font_path: str = "") -> bytes:
    fonts = d.load_fonts(font_path)
    image, draw = d.new_canvas()
    currency = snapshot.currency
    _header(draw, snapshot, banner, tz, fonts)

    draw.text((12, 41), "NET LIQUIDATION", font=fonts["r10"], fill=0)
    draw.text((12, 54), money(snapshot.net_liquidation, currency, compact=True), font=fonts["bold25"], fill=0)
    draw.text((12, 86), "DAY", font=fonts["bold12"], fill=0)
    d.arrow(draw, 44, 93, snapshot.daily_pnl)
    if snapshot.daily_pnl is None:
        d.text(draw, (56, 88), "N/A - ADD DAILY FLEX QUERY", fonts["r10"])
    else:
        day = f"{money(snapshot.daily_pnl, currency, compact=True, signed=True)}  {d.pct(snapshot.daily_pnl_pct)}"
        d.text(draw, (56, 86), day, fonts["bold12"])
    unreal = money(snapshot.unrealized_pnl, currency, compact=True, signed=True, symbol=False)
    d.text(draw, (12, 104), f"UNREAL  {unreal}", fonts["r10"])
    d.text(draw, (12, 120), f"CASH  {money(snapshot.cash, currency, compact=True, symbol=False)}", fonts["r10"])

    history = snapshot.nav_history
    d.text(draw, (246, 39), f"NAV {snapshot.window_label}".strip(), fonts["bold12"])
    change = snapshot.period_change_pct
    if change is not None:
        d.arrow_right(draw, 388, 39, change, d.pct(change), fonts["bold12"])
    d.area_chart(draw, [value for _, value in history], history[0][0] if history else None,
                 history[-1][0] if history else None, (246, 58, 388, 124), fonts)

    draw.rectangle((10, 150, 390, 272), outline=0, width=1)
    draw.rectangle((10, 150, 390, 172), fill=0)
    # Without the optional daily Flex query there is no per-position day P&L;
    # show unrealized P&L (vs cost) under its own heading instead of a column of N/A.
    daily = any(p.daily_pnl is not None for p in snapshot.positions)
    positions = select_positions(snapshot.positions, daily, min(max_positions, 4))
    heading = "DAY P&L" if daily else "UNREAL P&L"
    draw.text((18, 155), "STOCK", font=fonts["bold12"], fill=255)
    for x_right, label in ((122, "QTY"), (200, "PRICE"), (382, heading)):
        draw.text((x_right - draw.textlength(label, font=fonts["bold12"]), 155), label, font=fonts["bold12"], fill=255)
    if not positions:
        draw.text((18, 182), "No open positions", font=fonts["r12"], fill=0)
    for index, position in enumerate(positions):
        row_top = 176 + index * 23
        y = row_top + 2
        draw.text((18, y), position.symbol, font=fonts["bold12"], fill=0)
        d.right(draw, 122, y, _quantity(position.quantity), fonts["r12"])
        d.right(draw, 200, y, "N/A" if position.market_price is None else f"{position.market_price:,.2f}", fonts["r12"])
        change = position.daily_pnl if daily else position.unrealized_pnl
        d.arrow_right(draw, 310, y, change, d.signed(change), fonts["r12"])
        d.right(draw, 382, y + 1, d.pct(d.change_pct(change, position.market_value)), fonts["r10"])
        if index < len(positions) - 1:
            draw.line((18, row_top + 21, 382, row_top + 21), fill=0, width=1)

    footer = "NAV CHART AND % INCLUDE DEPOSITS/WITHDRAWALS"
    if len(snapshot.positions) > len(positions):
        basis = "DAY %" if daily else "P&L %"
        footer = f"BIGGEST {len(positions)} OF {len(snapshot.positions)} BY {basis} · NAV % INCL. DEPOSITS"
    d.text(draw, (12, 280), d.fit(draw, footer, fonts["r10"], 376), fonts["r10"])
    return d.to_png(image)
