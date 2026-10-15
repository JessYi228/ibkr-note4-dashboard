"""Display snapshot: the only portfolio data this program keeps.

Account IDs, raw broker responses and tokens never enter a snapshot.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from .errors import DashboardError


def optional_float(value: Any) -> float | None:
    """Parse a finite number while preserving unavailable data as None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        cleaned = str(value).replace(",", "").replace("$", "").strip().split(" ", 1)[0]
        try:
            number = float(cleaned)
        except ValueError:
            return None
    return number if math.isfinite(number) else None


@dataclass
class Position:
    symbol: str
    quantity: float
    market_price: float | None
    market_value: float | None
    daily_pnl: float | None = None
    unrealized_pnl: float | None = None


@dataclass
class Snapshot:
    report_date: date
    currency: str
    net_liquidation: float
    cash: float | None
    daily_pnl: float | None
    unrealized_pnl: float | None
    positions: list[Position] = field(default_factory=list)
    nav_history: list[tuple[date, float]] = field(default_factory=list)
    # Calendar days covered by the broker report (e.g. 30 for "Last 30 Calendar Days").
    window_days: int | None = None

    @property
    def window_label(self) -> str:
        """Chart window in calendar days; nav_history only has trading days."""
        days = self.window_days
        if days is None and len(self.nav_history) >= 2:
            days = (self.nav_history[-1][0] - self.nav_history[0][0]).days + 1
        return f"{days}D" if days else ""

    @property
    def daily_pnl_pct(self) -> float | None:
        if self.daily_pnl is None:
            return None
        previous = self.net_liquidation - self.daily_pnl
        return self.daily_pnl / abs(previous) * 100 if previous else None

    @property
    def period_change_pct(self) -> float | None:
        """NAV change across the history window. Includes deposits/withdrawals."""
        if len(self.nav_history) < 2 or not self.nav_history[0][1]:
            return None
        first, last = self.nav_history[0][1], self.nav_history[-1][1]
        return (last - first) / abs(first) * 100

    def validate(self) -> None:
        if not math.isfinite(self.net_liquidation):
            raise DashboardError("Snapshot has no valid net liquidation value")
        if not self.currency:
            raise DashboardError("Snapshot has no base currency")

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["report_date"] = self.report_date.isoformat()
        payload["nav_history"] = [[day.isoformat(), value] for day, value in self.nav_history]
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "Snapshot":
        positions = [
            Position(
                symbol=str(item.get("symbol") or "?")[:12],
                quantity=optional_float(item.get("quantity")) or 0.0,
                market_price=optional_float(item.get("market_price")),
                market_value=optional_float(item.get("market_value")),
                daily_pnl=optional_float(item.get("daily_pnl")),
                unrealized_pnl=optional_float(item.get("unrealized_pnl")),
            )
            for item in payload.get("positions", [])
            if isinstance(item, dict)
        ]
        net_liquidation = optional_float(payload.get("net_liquidation"))
        if net_liquidation is None:
            raise DashboardError("Snapshot JSON has no valid net_liquidation")
        try:
            report_date = date.fromisoformat(str(payload["report_date"]))
            history = [(date.fromisoformat(str(day)), float(value)) for day, value in payload.get("nav_history", [])]
        except (KeyError, ValueError, TypeError) as exc:
            raise DashboardError("Snapshot JSON has an invalid report_date or nav_history") from exc
        snapshot = cls(
            report_date=report_date,
            currency=str(payload.get("currency") or "USD").upper(),
            net_liquidation=net_liquidation,
            cash=optional_float(payload.get("cash")),
            daily_pnl=optional_float(payload.get("daily_pnl")),
            unrealized_pnl=optional_float(payload.get("unrealized_pnl")),
            positions=positions,
            nav_history=history,
            window_days=int(payload["window_days"]) if payload.get("window_days") else None,
        )
        snapshot.validate()
        return snapshot
