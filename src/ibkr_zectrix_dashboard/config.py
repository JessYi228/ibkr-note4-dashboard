"""Runtime settings from a private KEY=VALUE file plus process environment."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import DashboardError

DEFAULT_ENV_FILE = Path("~/.config/ibkr-zectrix-dashboard/env").expanduser()
DEFAULT_STATE_DIR = Path("~/.local/state/ibkr-zectrix-dashboard").expanduser()

ENV_TEMPLATE = """# ibkr-zectrix-dashboard private settings. Keep this file mode 0600.
# Never paste these values into chat, issues, commits or screenshots.

# IBKR Flex Web Service (read-only reports).
# Main query: XML, period "Last 30 Calendar Days", sections listed in docs/SETUP.zh-CN.md.
IBKR_FLEX_QUERY_ID=
# Optional second query, period "Last Business Day", for true daily P&L.
IBKR_FLEX_DAILY_QUERY_ID=
IBKR_FLEX_TOKEN=

# ZECTRIX Open API.
ZECTRIX_API_KEY=
# Optional when exactly one device is bound to the account.
ZECTRIX_DEVICE_ID=
ZECTRIX_PAGE_ID=1

# Paper-broker page (local read-only dashboards; pushed to its own page).
PAPER_PAGE_ID=2
# These may also live in a separate non-secret paper.env next to this file.
# Comma-separated KEY=loopback URL of each paper dashboard exposing /api/status.
# PAPER_BOOKS=MAIN=http://127.0.0.1:8000,TEST=http://127.0.0.1:8001
# Optional short display names: account_id=NAME,...
# PAPER_LABELS=trend_a=TREND A,carry_b=CARRY B
# Flag a book whose runner has not committed the session this long after the close.
PAPER_STALE_AFTER_MINUTES=240

# Display.
DASHBOARD_TIMEZONE=Asia/Shanghai
DASHBOARD_MAX_POSITIONS=4

# Refresh policy (minutes after the NYSE close).
# Start polling Flex this long after the close of each session.
REFRESH_READY_AFTER_MINUTES=60
# If the session's report is still missing this long after the close,
# the display switches to a STALE warning instead of silently showing old data.
REFRESH_STALE_AFTER_MINUTES=600
# Minimum spacing between Flex attempts while waiting for a new report.
REFRESH_RETRY_MINUTES=25
"""


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines without overriding the process environment."""
    if not path.exists():
        return
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise DashboardError(f"{path} must not be readable by group/other; run: chmod 600 {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value[:1] in {"'", '"'} and len(value) >= 2:
            value = value[1:-1]
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _minutes(name: str, default: int) -> timedelta:
    raw = env(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise DashboardError(f"{name} must be an integer number of minutes") from exc
    if value < 0:
        raise DashboardError(f"{name} must not be negative")
    return timedelta(minutes=value)


@dataclass(frozen=True)
class RefreshPolicy:
    ready_after: timedelta = timedelta(minutes=60)
    stale_after: timedelta = timedelta(minutes=600)
    retry_every: timedelta = timedelta(minutes=25)


@dataclass(frozen=True)
class Settings:
    flex_query_id: str
    flex_daily_query_id: str
    flex_token: str
    zectrix_api_key: str
    zectrix_device_id: str
    zectrix_page_id: str
    timezone: ZoneInfo
    max_positions: int
    policy: RefreshPolicy
    state_dir: Path

    @classmethod
    def from_env(cls) -> "Settings":
        tz_name = env("DASHBOARD_TIMEZONE", "Asia/Shanghai")
        try:
            timezone = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError as exc:
            raise DashboardError(f"Unknown DASHBOARD_TIMEZONE: {tz_name}") from exc
        try:
            max_positions = max(1, int(env("DASHBOARD_MAX_POSITIONS", "4")))
        except ValueError as exc:
            raise DashboardError("DASHBOARD_MAX_POSITIONS must be an integer") from exc
        policy = RefreshPolicy(
            ready_after=_minutes("REFRESH_READY_AFTER_MINUTES", 60),
            stale_after=_minutes("REFRESH_STALE_AFTER_MINUTES", 600),
            retry_every=_minutes("REFRESH_RETRY_MINUTES", 25),
        )
        if policy.stale_after < policy.ready_after:
            raise DashboardError("REFRESH_STALE_AFTER_MINUTES must be >= REFRESH_READY_AFTER_MINUTES")
        return cls(
            flex_query_id=env("IBKR_FLEX_QUERY_ID"),
            flex_daily_query_id=env("IBKR_FLEX_DAILY_QUERY_ID"),
            flex_token=env("IBKR_FLEX_TOKEN"),
            zectrix_api_key=env("ZECTRIX_API_KEY"),
            zectrix_device_id=env("ZECTRIX_DEVICE_ID"),
            zectrix_page_id=env("ZECTRIX_PAGE_ID", "1") or "1",
            timezone=timezone,
            max_positions=max_positions,
            policy=policy,
            state_dir=Path(env("DASHBOARD_STATE_DIR") or DEFAULT_STATE_DIR).expanduser(),
        )

    def missing(self) -> list[str]:
        names = {
            "IBKR_FLEX_QUERY_ID": self.flex_query_id,
            "IBKR_FLEX_TOKEN": self.flex_token,
            "ZECTRIX_API_KEY": self.zectrix_api_key,
        }
        return [name for name, value in names.items() if not value]
