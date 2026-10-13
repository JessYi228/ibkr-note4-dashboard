"""NYSE session arithmetic: which report date should already exist at a given moment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd

from .config import RefreshPolicy


@lru_cache(maxsize=1)
def _calendar() -> xcals.ExchangeCalendar:
    return xcals.get_calendar("XNYS")


@dataclass(frozen=True)
class Expectation:
    """The newest session whose Flex report should be fetchable by `now`."""

    session: date
    close: datetime  # UTC
    ready_at: datetime  # start polling
    stale_at: datetime  # warn if still missing

    def is_overdue(self, now: datetime) -> bool:
        return now >= self.stale_at


def session_close(session: date) -> datetime:
    return _calendar().session_close(pd.Timestamp(session)).to_pydatetime().astimezone(timezone.utc)


def expected_session(now: datetime, policy: RefreshPolicy) -> Expectation:
    """Latest NYSE session whose close + ready_after is not after `now`.

    Half days use their early close; holidays and weekends are skipped.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    calendar = _calendar()
    start = pd.Timestamp((now - timedelta(days=14)).date())
    end = pd.Timestamp(now.date())
    for session in reversed(calendar.sessions_in_range(start, end)):
        close = calendar.session_close(session).to_pydatetime().astimezone(timezone.utc)
        if close + policy.ready_after <= now:
            return Expectation(
                session=session.date(),
                close=close,
                ready_at=close + policy.ready_after,
                stale_at=close + policy.stale_after,
            )
    raise ValueError("no NYSE session found in the previous 14 days")


def sessions_between(after: date, up_to: date) -> int:
    """Number of NYSE sessions in (after, up_to]."""
    if up_to <= after:
        return 0
    sessions = _calendar().sessions_in_range(pd.Timestamp(after), pd.Timestamp(up_to))
    return sum(1 for session in sessions if session.date() > after)
