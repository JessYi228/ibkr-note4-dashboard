from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ibkr_zectrix_dashboard.config import RefreshPolicy
from ibkr_zectrix_dashboard.sessions import expected_session, sessions_between

BJ = ZoneInfo("Asia/Shanghai")
POLICY = RefreshPolicy(ready_after=timedelta(minutes=60), stale_after=timedelta(minutes=600))


def at(*args: int) -> datetime:
    return datetime(*args, tzinfo=BJ)


def test_monday_evening_beijing_still_expects_friday():
    # The old 18:17 Beijing timer ran here and could only ever see Friday.
    assert expected_session(at(2026, 10, 12, 18, 17), POLICY).session == date(2026, 10, 9)


def test_tuesday_morning_beijing_expects_monday():
    # NYSE closed Monday 10/12 at 16:00 EDT = Tuesday 04:00 Beijing; polling starts 05:00.
    assert expected_session(at(2026, 10, 13, 4, 59), POLICY).session == date(2026, 10, 9)
    expectation = expected_session(at(2026, 10, 13, 5, 0), POLICY)
    assert expectation.session == date(2026, 10, 12)
    assert expectation.stale_at.astimezone(BJ) == at(2026, 10, 13, 14, 0)


def test_weekend_keeps_friday():
    assert expected_session(at(2026, 10, 11, 12, 0), POLICY).session == date(2026, 10, 9)


def test_after_dst_ends_close_is_05_beijing():
    expectation = expected_session(at(2026, 11, 3, 7, 0), POLICY)
    assert expectation.session == date(2026, 11, 2)
    assert expectation.close.astimezone(BJ) == at(2026, 11, 3, 5, 0)


def test_thanksgiving_holiday_and_half_day():
    # 11/26 closed; 11/27 closes 13:00 EST = 02:00 Beijing on 11/28.
    assert expected_session(at(2026, 11, 27, 12, 0), POLICY).session == date(2026, 11, 25)
    expectation = expected_session(at(2026, 11, 28, 3, 0), POLICY)
    assert expectation.session == date(2026, 11, 27)
    assert expectation.close.astimezone(BJ) == at(2026, 11, 28, 2, 0)


def test_sessions_between_counts_trading_days_only():
    assert sessions_between(date(2026, 10, 9), date(2026, 10, 12)) == 1
    assert sessions_between(date(2026, 10, 8), date(2026, 10, 12)) == 2
    assert sessions_between(date(2026, 10, 12), date(2026, 10, 12)) == 0
